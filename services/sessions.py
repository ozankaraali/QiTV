"""Two playback slots, `main` and `pip`, and which of them is large (spec §7, Phase 4).

Each slot has its own player (`Session`) and `LiveSession`. Swapping exchanges the roles
only: no slot is stopped, restarted or re-pointed, and the surfaces trade geometry (the UI
does that on `largeChanged`). The slots are alike: the PiP is whichever is small, so after a
swap "Open in picture-in-picture" changes the small picture's channel and closing PiP stops
the small picture, the large one playing on.

Each slot's streams are its tuner's (`Slot.tuner`, the window's `ui.live_tuner.LiveTuner`
for that player, or one the manager makes for a slot without one): the large slot's tunes
are the shell's, and this manager opens, changes and closes the small one. A tuner keeps its
stream across swaps; a swap gives each tuner its new role as the purpose of its budget slots
("main" for the large picture, "pip" for the small one), so the provider's budget counts each
stream once. Opening the small slot:
- it asks the provider's connection budget for a slot with purpose "pip" and `queue=False`:
  when none is free, `denied(limit)` and nothing opens (a PiP never waits behind the main
  stream). The budget always grants later, so the slot starts once it is granted;
- a stopped small slot (muted first, so nothing is heard while it stops) gives its tickets
  back once its streams are closed, and only then asks for a new one: the old stream is
  always released before the new one opens;
- when the small stream cannot start, fails for good (`LiveSession.failed`) or loses its slot
  on a channel change, PiP closes and `pipFailed` says why in a fixed message (`failure` keeps
  what the stream itself said, for the shell's STB retry).
A swap waits for nothing: while the small slot's channel change is still on its way, it is
refused (False).

Audio follows the large picture: the PiP opens muted, and while the small slot stays muted
a swap mutes the new small slot and moves the user's mute from the old large slot to the new
one. Once the user makes the small slot audible (the mixer's PiP row raised above 0, or an
explicit unmute), a swap changes no mute: each stream keeps its own, so both stay audible;
muting the small slot again brings the default back. Each stream also has a volume level of
its own, which always stays with it across swaps.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Optional, Protocol

from PySide6.QtCore import QObject, Signal, SignalInstance

from services.budgeted_stream import RELEASE_CAP_MS, BudgetedStream
from services.connection_budget import DENIED, ConnectionBudget
from services.live.live_session import LiveSession
from services.live.refusals import REFUSALS
from services.playback import Session
from services.zapping import ZapContext

logger = logging.getLogger(__name__)

PURPOSE = "pip"  # the budget purpose of the small picture's slots
MAIN_PURPOSE = "main"  # the large picture's (LiveTuner's default)
MAIN_LEVEL = 100  # libVLC's default volume
PIP_LEVEL = 30  # the PiP stream's level until the mixer sets one (config pip_volume's default)
PIP_FAILED = "Couldn't open this channel in picture-in-picture"
LIMITED = "Your provider limits concurrent streams"  # a refusal when the limit is not known


def limit_message(limit: Optional[int]) -> str:
    """The toast for a provider limit that leaves no stream for the PiP."""
    if not limit:
        return LIMITED
    return f"Your provider allows {limit} stream{'s' if limit != 1 else ''} at a time"


class SlotTuner(Protocol):
    """What the manager needs of a slot's tuner (`ui.live_tuner.LiveTuner` is one)."""

    @property
    def released(self) -> SignalInstance: ...  # after a stop, nothing of the slot is open

    @property
    def stream(self) -> Optional[BudgetedStream]: ...  # its LiveSession's budget slot

    def stop(self) -> None: ...

    def releasing(self) -> bool: ...  # a stopped stream may still hold the provider

    def set_purpose(self, purpose: str) -> None: ...  # its role: "main" or "pip"


@dataclass
class Slot:
    name: str  # "main" | "pip"
    playback: Session
    live: LiveSession
    channel: Optional[dict] = None
    group_key: Optional[str] = None
    ticket: Optional[int] = None  # its budget ticket while this manager opened it (small)
    tuner: Optional[SlotTuner] = None  # the player's tuner; None: the manager makes one


class _StreamTuner(QObject):
    """A slot's tuner when it has none: its live session's budget slot alone."""

    released = Signal()

    def __init__(
        self,
        live: LiveSession,
        budget: ConnectionBudget,
        purpose: str,
        cap_ms: int,
        parent: QObject,
    ) -> None:
        super().__init__(parent)
        self.stream: Optional[BudgetedStream] = BudgetedStream(
            live, budget, purpose, cap_ms=cap_ms, parent=self
        )
        self.stream.released.connect(self.released)

    def stop(self) -> None:
        if self.stream is not None:
            self.stream.stop()

    def releasing(self) -> bool:
        return self.stream is not None and self.stream.waiting()

    def set_purpose(self, purpose: str) -> None:
        if self.stream is not None:
            self.stream.set_purpose(purpose)


@dataclass
class _Open:
    """A PiP channel waiting for the old stream's release or for its budget slot."""

    slot: str  # the name of the slot it opens in (the small one when it was asked for)
    provider: str
    limit: Optional[int]
    start_kwargs: dict[str, Any]


class SessionManager(QObject):
    largeChanged = Signal(str)  # name of the slot that is now large
    pipStateChanged = Signal(bool)  # PiP open/closed
    pipStarted = Signal()  # the small slot's stream started: PiP opened, or its channel changed
    denied = Signal(int)  # limit (budget refused PiP)
    # A fixed message: the PiP's live session did not start or failed for good (the limit
    # message for a provider refusal), or the budget refused a channel change of a PiP it had
    # granted (then without denied). PiP is closed by then.
    pipFailed = Signal(str)
    slotTimeline = Signal(str, dict)  # slot name, LiveSession timeline dict

    def __init__(
        self,
        main: Slot,
        pip: Slot,
        budget: ConnectionBudget,
        *,
        release_cap_ms: int = RELEASE_CAP_MS,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._main, self._pip = main, pip
        self._budget = budget
        self._large = main
        self._open = False  # pipStateChanged(True) was emitted, and no close since
        self._pending: Optional[_Open] = None
        self._limit: Optional[int] = None  # the provider limit of the PiP stream that starts
        # What the last pipFailed stood for: the small stream's own failure (LiveSession's
        # message, a refusal one of REFUSALS); "" when its start raised or the budget refused.
        self.failure = ""
        self._levels = {main.name: MAIN_LEVEL, pip.name: PIP_LEVEL}
        self._muted = {main.name: False, pip.name: True}
        self._tuners: dict[str, SlotTuner] = {}
        for slot, purpose in ((main, MAIN_PURPOSE), (pip, PURPOSE)):
            tuner = slot.tuner
            if tuner is None:
                tuner = _StreamTuner(slot.live, budget, purpose, release_cap_ms, self)
            stream = tuner.stream
            if stream is None:
                raise ValueError(f"The {slot.name} slot has no live session to open")
            self._tuners[slot.name] = tuner
            tuner.released.connect(self._proceed)  # a slot let go: a waiting open may go on
            stream.granted.connect(self._on_granted)
        # A wanted level makes PlaybackSession apply it on every media it opens; without one
        # the main player takes whatever level its audio output opens at.
        main.playback.set_volume(self._levels[main.name])
        main.live.failed.connect(self._on_main_failed)
        pip.live.failed.connect(self._on_pip_failed)
        main.live.timelineChanged.connect(self._on_main_timeline)
        pip.live.timelineChanged.connect(self._on_pip_timeline)

    # ---- roles --------------------------------------------------------------------------------

    def large(self) -> Slot:
        return self._large

    def other(self) -> Slot:
        """The slot that is not large: the PiP's, open or not."""
        return self._pip if self._large is self._main else self._main

    def small(self) -> Optional[Slot]:
        """The other slot while PiP is open; None when it is closed."""
        return self.other() if self._open else None

    def pip_open(self) -> bool:
        return self._open

    def changing(self) -> bool:
        """The open PiP's channel change waits for its slot or the old stream's release (a
        swap is refused meanwhile)."""
        return self._pending is not None and self._open

    def pip_opening(self) -> bool:
        """A PiP was asked for and has not opened (nor been refused) yet."""
        return self._pending is not None and not self._open

    def slot_showing(self, channel: dict) -> Optional[Slot]:
        """The slot showing `channel` (by `ZapContext.same`), large first; None if neither."""
        for slot in (self._large, self.small()):
            if slot is not None and ZapContext.same(slot.channel, channel):
                return slot
        return None

    def swap(self) -> bool:
        """Exchange the large and small roles; False (and nothing changes) when PiP is closed
        or its channel change is still on its way.

        No stream is played, stopped or started, and no level changes; each tuner's slots
        take its new role. A muted small slot takes the user's mute of the large one, which
        is muted in its place; a small slot the user made audible keeps both streams' mutes
        as they are (module docstring).
        """
        small = self.small()
        if small is None or self._pending is not None:
            return False
        large = self._large
        self._large = small
        self._tuners[small.name].set_purpose(MAIN_PURPOSE)
        self._tuners[large.name].set_purpose(PURPOSE)
        if self._muted[small.name]:  # the default: audio follows the large picture
            users_mute = self._muted[large.name]
            self._set_mute(large, True)  # the new small slot
            self._set_mute(small, users_mute)
        self.largeChanged.emit(small.name)
        return True

    def demote_large(self) -> bool:
        """Make the large picture small while PiP is closed, without reopening its stream.

        The roles trade as in swap(), but the slot that becomes large is the closed one (idle,
        or still releasing what it played): the caller opens something in it, and it waits for
        the release as any open does. The small slot opens muted and the user's mute of the
        large picture moves to the new large slot. False (and nothing changes) when PiP is
        open or opening, or the large slot streams nothing.
        """
        if self._open or self._pending is not None:
            return False
        large = self._large
        stream = self._tuners[large.name].stream
        if stream is None or not stream.running():
            return False
        freed = self.other()
        self._large = freed
        self._tuners[freed.name].set_purpose(MAIN_PURPOSE)
        self._tuners[large.name].set_purpose(PURPOSE)
        users_mute = self._muted[large.name]
        self._set_mute(large, True)  # the PiP is small and muted, as it opens
        self._set_mute(freed, users_mute)
        self._open = True
        self.largeChanged.emit(freed.name)  # the shell trades what each picture shows first
        self.pipStateChanged.emit(True)
        return True

    # ---- the small slot -----------------------------------------------------------------------

    def open_pip(
        self,
        channel: dict,
        *,
        provider: dict,
        xtream_max: Optional[int],
        start_kwargs: dict,
    ) -> None:
        """Open `channel` small, or change the channel of the small picture.

        `start_kwargs` go to `LiveSession.start()` once the budget grants the slot and the
        streams the small slot played before are released.
        """
        slot = self.other()
        self._stop(slot)
        slot.channel = channel
        limit = self._budget.limit_for(provider, xtream_max)
        name = str(provider.get("name", ""))
        self._pending = _Open(slot.name, name, limit, dict(start_kwargs))
        self._proceed()

    def close_pip(self) -> None:
        """Stop the small slot; the large one plays on. Its tickets go back on release."""
        self._pending = None
        slot = self.other()
        self._stop(slot)
        slot.channel = slot.group_key = None
        if self._open:
            self._open = False
            self.pipStateChanged.emit(False)

    def _stop(self, slot: Slot) -> None:
        tuner = self._tuners[slot.name]
        stream = tuner.stream
        if self._open or (stream is not None and stream.running()):
            # Silent while it stops; the next start applies the slot's own mute again.
            slot.playback.set_muted(True)
        tuner.stop()  # its tickets go back once its streams are closed
        slot.ticket = None

    def _proceed(self) -> None:
        """Ask for the pending channel's slot once its player let go of what it played."""
        pending = self._pending
        if pending is None:
            return
        slot = self._slot(pending.slot)
        tuner = self._tuners[slot.name]
        stream = tuner.stream
        if stream is None or tuner.releasing() or stream.ticket is not None:
            return
        ticket = stream.request(pending.provider, pending.limit, queue=False)
        slot.ticket = stream.ticket
        if ticket == DENIED:  # only a limit denies, so it is a number
            if self._open:  # a channel change: the PiP had a slot
                self._fail(limit_message(pending.limit))
            else:
                self.close_pip()
                self.denied.emit(pending.limit)

    def _on_granted(self) -> None:
        pending = self._pending
        if pending is None:
            return
        slot = self._slot(pending.slot)
        stream = self._tuners[slot.name].stream
        if stream is None or not stream.is_granted() or stream.running():
            return  # another stream's grant
        self._pending = None
        opening = not self._open
        if opening:
            self._muted[slot.name] = True  # the PiP opens small and muted
        slot.playback.set_volume(self._levels[slot.name])
        # Asked for before it starts. A player that still has its audio output takes it at
        # once; after a stop, the next output comes up with the old one's state (muted by
        # _stop) and PlaybackSession applies this within ~30 ms of it opening.
        self._set_mute(slot, self._muted[slot.name])
        self._limit = pending.limit
        try:
            stream.start(**pending.start_kwargs)
        except Exception as error:  # the stream gave its slot back and stopped the session
            logger.warning("Could not start the PiP channel (%s)", type(error).__name__)
            self._fail(PIP_FAILED)
            return
        if not stream.running():
            return  # it failed inside start() (_on_failed closed it)
        if opening:
            self._open = True
            self.pipStateChanged.emit(True)
        self.pipStarted.emit()

    def _on_main_failed(self, message: str) -> None:
        self._on_failed(self._main, message)

    def _on_pip_failed(self, message: str) -> None:
        self._on_failed(self._pip, message)

    def _on_failed(self, slot: Slot, message: str) -> None:
        """The small stream failed for good: LiveSession recovered what it could.

        The large slot's failures are the shell's.
        """
        stream = self._tuners[slot.name].stream
        if slot is self._large or stream is None or not stream.running():
            return
        self._fail(limit_message(self._limit) if message in REFUSALS else PIP_FAILED, message)

    def _fail(self, message: str, failure: str = "") -> None:
        self.failure = failure
        self.close_pip()
        self.pipFailed.emit(message)

    # ---- audio --------------------------------------------------------------------------------

    def muted(self, slot_name: str) -> bool:
        return self._muted[slot_name]

    def set_muted(self, slot_name: str, muted: bool) -> None:
        """The user's mute for that slot's stream."""
        self._set_mute(self._slot(slot_name), muted)

    def level(self, slot_name: str) -> int:
        return self._levels[slot_name]

    def set_level(self, slot_name: str, value: int) -> None:
        """That stream's volume (0–100); raised on the small slot, it unmutes it too."""
        slot = self._slot(slot_name)
        level = max(0, min(100, int(value)))
        raised = level > self._levels[slot.name]
        self._levels[slot.name] = level
        slot.playback.set_volume(level)
        if raised and slot is self.small():  # only raising it makes the PiP audible
            self._set_mute(slot, False)

    def _set_mute(self, slot: Slot, muted: bool) -> None:
        self._muted[slot.name] = bool(muted)
        slot.playback.set_muted(bool(muted))

    def _slot(self, name: str) -> Slot:
        for slot in (self._main, self._pip):
            if slot.name == name:
                return slot
        raise KeyError(name)

    # ---- timelines ----------------------------------------------------------------------------

    def _on_main_timeline(self, state: dict) -> None:
        self.slotTimeline.emit(self._main.name, state)

    def _on_pip_timeline(self, state: dict) -> None:
        self.slotTimeline.emit(self._pip.name, state)
