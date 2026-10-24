"""Opens the TV shell's provider streams one at a time (spec §6.9).

Before anything opens, the stream before it is released: providers count connections, and
many allow one or two. "Released" means:
- the time-shift pipeline's `LiveSession.released()`: its upstream is closed and, after
  direct play, libVLC let go of the provider stream (LiveSession bounds both waits);
- the player's own direct play (a movie, an episode, a channel the pipeline cannot read):
  `PlaybackSession.inputEnded`, since libVLC ends its input on a worker after `stop()`.
Each wait is capped (RELEASE_CAP_MS; the LiveSession one by `BudgetedStream`, a little
later, as a backstop to its own caps). Only the newest open waits: a newer one replaces it.

A pipeline tune also takes a slot of the provider's connection budget (purpose "main") and
starts once the slot is granted and the old stream is released; the old slot goes back on
that release. A `BudgetedStream` holds that slot and waits for the release (the PiP slot's
`SessionManager` uses one too). With a limit of one, the new request waits at the head of
the queue, so the freed slot goes to it and to no other consumer. A pause or Go live pressed
during that wait applies once the tune starts.

A direct play (`open(direct=True)`: a channel the pipeline cannot read, a movie, an
episode) holds a "main" slot too (a `HeldSlot`, under the provider the caller names: the
catalog's, as for pipeline tunes), from its start until libVLC let go of its stream, so a
PiP sees the slot taken. It never waits for one: this gate already orders the main player's
streams. It waits for the other pictures' tuners of its provider, though
(`wait_for_each_other`): a closing PiP's stream may still hold the provider's last slot, so
the open runs at that tuner's `released`; another provider's closing stream holds none. A local file (`local=True`) is no provider's stream: it is stopped and waited for
like a direct play, but holds no slot.

Picture in picture and the multiview's tiles give each player a tuner of its own.
Its budget slots carry the player's slot name as their purpose ("main", "pip", "t2" …): a
stream keeps its tuner across swaps, so a swap moves nothing, and `refused` counts every
other player's streams. `released` says when nothing of the player is left open after a
stop: the PiP's `SessionManager` waits for it before it opens another channel there.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from services.budgeted_stream import BudgetedStream, HeldSlot
from services.config_manager import ConfigManager
from services.connection_budget import ConnectionBudget
from services.live.kept import KeptBuffers
from services.live.live_session import LiveSession
from services.playback import Session

logger = logging.getLogger(__name__)

RELEASE_CAP_MS = 2000  # the longest wait for libVLC to let go of a direct stream
PURPOSE = "main"  # the main player's budget purpose (each player's is its slot's name)
DEFAULT_RAM_MIB = ConfigManager.DEFAULT_OPTION_TIMESHIFT_RAM_MIB  # a config without the key


@dataclass
class LiveStart:
    """What a pipeline tune opens: the provider URL and how to ask for it."""

    url: str
    user_agent: str
    verify_ssl: bool
    channel: str = ""  # its key for what it taught (services.live.tuning; "": none)
    referrer: str = ""  # the channel's M3U http-referrer (2.1; "": none)


def start_kwargs(start: LiveStart, config: Any, *, disk: Optional[bool] = None) -> dict[str, Any]:
    """`LiveSession.start()`'s arguments for `start` with the settings in `config`.

    The time-shift buffer is on disk with the user's consent (`disk=False`: in memory all
    the same), else the memory per stream (`timeshift_ram_mib`). A disk buffer that fails
    goes on in that memory (`ram_bytes`). A channel with a key passes it (`channel`), one whose
    M3U names a referrer passes that (`referrer`, and its `Referer` header for the helper).
    """
    consent = bool(config.timeshift_enabled)
    disk = consent if disk is None else disk and consent
    ram = getattr(config, "timeshift_ram_mib", DEFAULT_RAM_MIB)
    mib = config.timeshift_max_mib if disk else ram
    headers = {"User-Agent": start.user_agent}
    if start.referrer:
        headers["Referer"] = start.referrer  # the helper's requests, playlists and segments
    kwargs = {
        "url": start.url,
        "headers": headers,
        "verify_ssl": start.verify_ssl,
        "latency": config.live_latency,
        "user_agent": start.user_agent,
        "disk": disk,
        "max_bytes": mib * 1024 * 1024,
    }
    if disk:
        kwargs["ram_bytes"] = ram * 1024 * 1024
    if start.channel:
        kwargs["channel"] = start.channel
    if start.referrer:
        kwargs["referrer"] = start.referrer  # a direct play's
    return kwargs


def one_disk_ring(*tuners: "LiveTuner") -> None:
    """The pictures' tuners: while one keeps its time-shift on disk, the others' tunes keep
    theirs in memory (the user's disk cap is the total). Called again with
    every tuner when one is added (a multiview tile's)."""

    def allowed(others: list[LiveTuner]) -> Callable[[], bool]:
        # 2.6: a recording in the background or a kept disk buffer taken back does not
        # count: the disk amount is the total of every disk buffer (the helper's DiskBudget).
        return lambda: not any(
            other.on_disk() and not other.background and not other.kept_disk() for other in others
        )

    for tuner in tuners:
        tuner.set_disk_allowed(allowed([other for other in tuners if other is not tuner]))


def wait_for_each_other(*tuners: "LiveTuner") -> None:
    """The pictures' tuners: a direct open on one that takes a provider slot (a movie, an
    episode, a direct channel) waits while another's stopped stream may still hold one.
    Called again with every tuner when one is added: each pair is joined once."""
    for tuner in tuners:
        for other in tuners:
            if other is not tuner:
                tuner.add_peer(other)


class LiveTuner(QObject):
    startFailed = Signal(str)  # LiveSession.start() raised: its message (scrub before showing)
    released = Signal()  # after a stop, nothing of the player is open any more
    denied = Signal(int)  # a direct open with a limit found no stream to spare (the limit)

    def __init__(
        self,
        session: Session,
        config: Any,
        live: Optional[LiveSession] = None,
        budget: Optional[ConnectionBudget] = None,
        parent: Optional[QObject] = None,
        *,
        purpose: str = PURPOSE,
    ) -> None:
        super().__init__(parent)
        self.session = session
        # live_latency, timeshift_*, and selected_provider_name for a slot no caller named
        self.config = config
        self.live = live
        self.budget = budget if budget is not None else ConnectionBudget(self)
        self.purpose = purpose
        self.timeline: Optional[dict] = None  # the running tune's last timelineChanged state
        self.background = False  # 2.6: its slot records in the background (SessionManager)
        self._pending: Optional[LiveStart] = None  # a tune waiting for its slot or the release
        self._disk_allowed: Callable[[], bool] = lambda: True
        # The pipeline tune's budget slot, its start and the wait for its release.
        self._stream: Optional[BudgetedStream] = None
        self._latency: Optional[str] = None  # what the running tune started with
        self._pause = False  # pressed while the pending tune waited: applied at its start
        self._input_wait = False  # a direct play was stopped; inputEnded has not come
        self._direct = False  # the player plays a provider stream an open() asked for
        self._held = HeldSlot(self.budget, purpose)  # that stream's slot, until VLC let go
        self._action: Optional[Callable[[], None]] = None  # an open waiting for the release
        self._action_direct = False
        self._action_slot = False  # that open plays a provider stream: it holds a slot
        self._action_provider: Optional[str] = None  # whose slot it holds
        self._action_limit: Optional[int] = None  # within which it is taken (None: always)
        self._pending_limit: Optional[int] = None  # the provider's limit for the pending tune
        self._peers: list[LiveTuner] = []  # the other pictures' (wait_for_each_other)
        self._input_cap = QTimer(self, singleShot=True, interval=RELEASE_CAP_MS)
        self._input_cap.timeout.connect(self._on_input_cap)
        session.inputEnded.connect(self._on_input_ended)
        if live is not None:
            self._stream = BudgetedStream(live, self.budget, purpose, parent=self)
            self._stream.granted.connect(self._on_granted)
            self._stream.released.connect(self._on_released)
            live.timelineChanged.connect(self._on_timeline)

    # ---- opening ------------------------------------------------------------------------------

    @property
    def stream(self) -> Optional[BudgetedStream]:
        """The live session's budget slot and start (None without the pipeline)."""
        return self._stream

    def set_purpose(self, purpose: str) -> None:
        """Take another budget purpose: the slots this player holds or waits for follow."""
        self.purpose = purpose
        self._held.set_purpose(purpose)
        if self._stream is not None:
            self._stream.set_purpose(purpose)

    def set_kept(self, kept: Optional[KeptBuffers]) -> None:
        """2.6: where its stops keep the channel's buffer, and its tunes find their own."""
        if self._stream is not None:
            self._stream.kept = kept

    def set_disk_allowed(self, allowed: Callable[[], bool]) -> None:
        """Whether a tune may keep its time-shift on disk now (False: another picture
        does: the user's disk cap is the total, so this one stays in memory)."""
        self._disk_allowed = allowed

    def add_peer(self, peer: "LiveTuner") -> None:
        """Another picture's tuner: a slot-taking direct open waits for its `released`
        while it is releasing. Once per peer."""
        if peer is self or peer in self._peers:
            return
        self._peers.append(peer)
        peer.released.connect(self._on_peer_released)

    def on_disk(self) -> bool:
        """The running stream keeps its time-shift on disk."""
        stream = self._stream
        return stream is not None and stream.running() and stream.disk

    def kept_disk(self) -> bool:
        """2.6: the running stream's disk is a kept buffer it took back, not its tune's ask."""
        stream = self._stream
        return stream is not None and stream.running() and stream.disk and not stream.asked_disk

    def releasing(self) -> bool:
        """A stopped stream of this player may still hold the provider (`released` follows)."""
        stream = self._stream
        return self._input_wait or (stream is not None and stream.waiting())

    def releasing_from(self, provider: str) -> bool:
        """A stopped stream of this player may still hold one of `provider`'s slots: a
        direct play libVLC has not let go of, or a time-shift stream not yet released."""
        if self._input_wait and self.budget.provider_of(self._held.ticket) == provider:
            return True
        stream = self._stream
        return stream is not None and stream.waiting() and provider in stream.releasing_providers()

    def ensure_held(self, provider: str, limit: Optional[int]) -> bool:
        """A direct play about to open again (a reconnect) still holds its slot, or takes one
        within `limit` (a multiview tile's: the other tiles' streams count); False: none."""
        if self._held.ticket is not None:
            return True
        return self._held.try_take(provider, limit)

    def open(
        self,
        action: Callable[[], None],
        *,
        direct: bool = False,
        local: bool = False,
        provider: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> None:
        """Stop what plays, then run `action` once it is released: now when nothing waits.

        `direct`: `action` plays a provider URL in the session, whose input the next open
        then waits for; its slot is `provider`'s (by default the selected provider's).
        `local`: it plays a local file instead, which takes no provider slot.
        `limit` (a multiview tile's direct play): the slot is taken only when the provider's
        other streams leave one; else `denied(limit)` and nothing plays.
        """
        self.stop()
        self._action, self._action_direct = action, direct or local
        self._action_slot = direct and not local
        self._action_provider = provider
        self._action_limit = limit
        self._proceed()

    def tune(self, start: LiveStart, provider: str, limit: Optional[int]) -> None:
        """Stop what plays and start `start` through the pipeline with a budget slot."""
        assert self._stream is not None
        self.stop()
        self._pending = start
        self._pending_limit = limit
        self._stream.request(provider, limit)

    def refused(self, provider: str, limit: Optional[int], *, direct: bool = False) -> bool:
        """Whether other players' streams (picture in picture, tiles) take the whole limit.
        `direct`: for a direct open, which waits for another picture's stream that is being
        let go of (`wait_for_each_other`), so that one does not count."""
        if limit is None:
            return False
        others = [p for p in self.budget.holders(provider) if p != self.purpose]
        if direct:
            going = {peer.purpose for peer in self._peers if peer.releasing_from(provider)}
            others = [p for p in others if p not in going]
        return limit <= 0 or len(others) >= limit

    def stop(self) -> None:
        """Stop what plays or waits to; a held slot goes back once its upstream is closed."""
        self._action = None
        self._pending = None
        self._pause = False
        self.timeline = None
        if self._stream is not None:
            self._stream.stop()  # released() follows, at once when nothing is left open
        if self._direct:
            self._direct = False
            self.session.stop()
            if self.session.holds_input():
                self._input_wait = True
                self._input_cap.start()  # the slot goes back with the input
            else:
                self._held.give_back()

    def shutdown(self) -> None:
        direct = self._direct
        self.stop()
        if not direct:
            self.session.stop()  # whatever else the player holds

    def _proceed(self) -> None:
        stream = self._stream
        if self._input_wait or (stream is not None and stream.waiting()):
            return
        if self._action is not None:
            provider = self._action_provider
            if provider is None:
                provider = str(getattr(self.config, "selected_provider_name", "") or "")
            if self._action_slot and any(peer.releasing_from(provider) for peer in self._peers):
                return  # a closing stream may hold the provider's last slot (a PiP's)
            action, self._action = self._action, None
            if self._action_slot:
                limit = self._action_limit
                if limit is None:
                    self._held.take(provider)
                elif not self._held.try_take(provider, limit):
                    logger.info("No stream to spare for a tile: the provider allows %s", limit)
                    self.denied.emit(limit)
                    return  # nothing plays: the slot's stream was stopped already
            self._direct = self._action_direct
            action()
        elif self._pending is not None and stream is not None and stream.is_granted():
            self._start()

    def _start(self) -> None:
        pending, stream = self._pending, self._stream
        if pending is None or stream is None:
            return
        self._pending = None
        self._latency = self.config.live_latency
        try:
            disk = None if self._disk_allowed() else False
            kwargs = start_kwargs(pending, self.config, disk=disk)
            # 2.6.2: a server part's download is no budget slot of its own: with any limit
            # the live upstream parks while one plays (LiveSession.start's one_stream).
            kwargs["one_stream"] = self._pending_limit is not None
            stream.start(**kwargs)
        except Exception as error:  # the stream gave its slot back and stopped the session
            logger.warning("Could not start the live channel (%s)", type(error).__name__)
            self._pause = False
            self.startFailed.emit(str(error))
            return
        if self._pause:
            self._pause = False
            stream.live.toggle_pause()  # LiveSession keeps a pause for the stream that opens next

    # ---- events -------------------------------------------------------------------------------

    def _on_granted(self) -> None:
        if self._pending is not None:
            self._proceed()

    def _on_released(self) -> None:
        self._proceed()
        self._report_released()

    def _on_input_ended(self) -> None:
        if self._input_wait:
            self._input_wait = False
            self._input_cap.stop()
            self._held.give_back()
            self._proceed()
            self._report_released()

    def _on_peer_released(self) -> None:
        if self._action is not None and self._action_slot:
            self._proceed()

    def _report_released(self) -> None:
        if not self.releasing():
            self.released.emit()

    def _on_input_cap(self) -> None:
        if self._input_wait:
            logger.warning("VLC did not let go of the stream in time; going on without it.")
            self._on_input_ended()

    def _on_timeline(self, state: dict) -> None:
        if self.running():
            self.timeline = state

    # ---- controls and state -------------------------------------------------------------------

    def running(self) -> bool:
        """A pipeline tune has started (it may be opening, time-shifting or playing direct)."""
        return self._stream is not None and self._stream.running()

    def opening(self) -> bool:
        """Something will play but does not yet: it waits for a slot, a release or the pipeline."""
        if self._action is not None or self._pending is not None:
            return True
        return self.running() and self.live is not None and not self.live.mode

    def settling(self) -> bool:
        """An open or a tune waits for its budget slot or for the old stream's release."""
        return self._action is not None or self._pending is not None

    def idle(self) -> bool:
        """Nothing plays, and nothing waits to."""
        return not (self.settling() or self.running() or self._direct)

    def playing_direct(self) -> bool:
        """The player plays a provider stream or a file an open() handed it."""
        return self._direct

    def pipeline(self) -> bool:
        """The running tune plays from the time-shift buffer."""
        return self.running() and self.live is not None and self.live.is_pipeline()

    def toggle_pause(self) -> None:
        if self.running() and self.live is not None:
            self.live.toggle_pause()  # plays on direct channels too
        elif self._pending is not None:
            self._pause = not self._pause
        else:
            self.session.toggle_pause()

    def go_live(self) -> bool:
        """Go live on the running tune; a pending one starts live and unpaused anyway."""
        if self.running() and self.live is not None:
            self.live.go_live()
            return True
        if self._pending is not None:
            self._pause = False
            return True
        return False

    def set_latency(self, latency: str) -> None:
        """A new live latency reaches the running tune at once."""
        if self.running() and self.live is not None and latency != self._latency:
            self._latency = latency
            self.live.set_latency(latency)
