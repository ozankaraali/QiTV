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
streams. It waits for the other picture's tuner, though (`wait_for_each_other`): a closing
PiP's stream may still hold the provider's last slot, so the open runs at that tuner's
`released`. A local file (`local=True`) is no provider's stream: it is stopped and waited for
like a direct play, but holds no slot.

Picture in picture (Phase 4) gives each player a tuner of its own. Its slots carry its role
as their purpose: "main" for the large picture's, "pip" for the small one's
(`set_purpose()`, which a swap calls), and a stream keeps its tuner across swaps, so a swap
moves nothing. `released` says when nothing of the player is left open after a stop: the
PiP's `SessionManager` waits for it before it opens another channel there.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from services.budgeted_stream import BudgetedStream, HeldSlot
from services.config_manager import ConfigManager
from services.connection_budget import ConnectionBudget
from services.live.live_session import LiveSession
from services.playback import Session

logger = logging.getLogger(__name__)

RELEASE_CAP_MS = 2000  # the longest wait for libVLC to let go of a direct stream
PURPOSE = "main"  # the budget purpose of the main live stream (Phase 4 adds "pip")
DEFAULT_RAM_MIB = ConfigManager.DEFAULT_OPTION_TIMESHIFT_RAM_MIB  # a config without the key


@dataclass
class LiveStart:
    """What a pipeline tune opens: the provider URL and how to ask for it."""

    url: str
    user_agent: str
    verify_ssl: bool


def start_kwargs(start: LiveStart, config: Any, *, disk: Optional[bool] = None) -> dict[str, Any]:
    """`LiveSession.start()`'s arguments for `start` with the settings in `config`.

    The time-shift buffer is on disk with the user's consent (`disk=False`: in memory all
    the same), else the memory per stream (`timeshift_ram_mib`). A disk buffer that fails
    goes on in that memory (`ram_bytes`).
    """
    consent = bool(config.timeshift_enabled)
    disk = consent if disk is None else disk and consent
    ram = getattr(config, "timeshift_ram_mib", DEFAULT_RAM_MIB)
    mib = config.timeshift_max_mib if disk else ram
    kwargs = {
        "url": start.url,
        "headers": {"User-Agent": start.user_agent},
        "verify_ssl": start.verify_ssl,
        "latency": config.live_latency,
        "user_agent": start.user_agent,
        "disk": disk,
        "max_bytes": mib * 1024 * 1024,
    }
    if disk:
        kwargs["ram_bytes"] = ram * 1024 * 1024
    return kwargs


def one_disk_ring(first: "LiveTuner", second: "LiveTuner") -> None:
    """The two pictures' tuners: while one keeps its time-shift on disk, the other's tunes
    keep theirs in memory (the user's disk cap is the total; owner's M5 ruling)."""
    first.set_disk_allowed(lambda: not second.on_disk())
    second.set_disk_allowed(lambda: not first.on_disk())


def wait_for_each_other(first: "LiveTuner", second: "LiveTuner") -> None:
    """The two pictures' tuners: a direct open on one that takes a provider slot (a movie, an
    episode, a direct channel) waits while the other's stopped stream may still hold one."""
    first.set_peer(second)
    second.set_peer(first)


class LiveTuner(QObject):
    startFailed = Signal(str)  # LiveSession.start() raised: its message (scrub before showing)
    released = Signal()  # after a stop, nothing of the player is open any more

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
        self._peer: Optional[LiveTuner] = None  # the other picture's (wait_for_each_other)
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
        """Take another role (a PiP swap): the slots this player holds or waits for follow."""
        self.purpose = purpose
        self._held.set_purpose(purpose)
        if self._stream is not None:
            self._stream.set_purpose(purpose)

    def set_disk_allowed(self, allowed: Callable[[], bool]) -> None:
        """Whether a tune may keep its time-shift on disk now (False: the other picture
        does: the user's disk cap is the total, so this one stays in memory)."""
        self._disk_allowed = allowed

    def set_peer(self, peer: "LiveTuner") -> None:
        """The other picture's tuner: a slot-taking direct open waits for its `released`."""
        self._peer = peer
        peer.released.connect(self._on_peer_released)

    def on_disk(self) -> bool:
        """The running stream keeps its time-shift on disk."""
        stream = self._stream
        return stream is not None and stream.running() and stream.disk

    def releasing(self) -> bool:
        """A stopped stream of this player may still hold the provider (`released` follows)."""
        stream = self._stream
        return self._input_wait or (stream is not None and stream.waiting())

    def open(
        self,
        action: Callable[[], None],
        *,
        direct: bool = False,
        local: bool = False,
        provider: Optional[str] = None,
    ) -> None:
        """Stop what plays, then run `action` once it is released: now when nothing waits.

        `direct`: `action` plays a provider URL in the session, whose input the next open
        then waits for; its slot is `provider`'s (by default the selected provider's).
        `local`: it plays a local file instead, which takes no provider slot.
        """
        self.stop()
        self._action, self._action_direct = action, direct or local
        self._action_slot = direct and not local
        self._action_provider = provider
        self._proceed()

    def tune(self, start: LiveStart, provider: str, limit: Optional[int]) -> None:
        """Stop what plays and start `start` through the pipeline with a budget slot."""
        assert self._stream is not None
        self.stop()
        self._pending = start
        self._stream.request(provider, limit)

    def refused(self, provider: str, limit: Optional[int]) -> bool:
        """Whether other streams (Phase 4: picture in picture) take the whole limit."""
        if limit is None:
            return False
        others = [p for p in self.budget.holders(provider) if p != self.purpose]
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
            peer = self._peer
            if self._action_slot and peer is not None and peer.releasing():
                return  # its closing stream may hold the provider's last slot (a PiP's)
            action, self._action = self._action, None
            self._direct = self._action_direct
            if self._action_slot:
                provider = self._action_provider
                if provider is None:
                    provider = str(getattr(self.config, "selected_provider_name", "") or "")
                self._held.take(provider)
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
            stream.start(**start_kwargs(pending, self.config, disk=disk))
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
