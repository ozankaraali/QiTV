"""Named playback slots and their roles: which is large, which is the PiP (spec §7, Phase 4).

Each slot has its own player (`Session`) and `LiveSession`: "main" and "pip" from the start,
and a multiview's tiles ("t2" … "t8", `add_slot`) once the grid wants them. One slot is
large: the shell's controls act on it. Of the others, one is the PiP's (`other()`), shown
small while picture in picture is open. Giving another slot the large role (`set_large`; a
swap is that for the small slot) changes the roles only: no slot is stopped, restarted or
re-pointed, and the surfaces trade geometry (the UI does that on `largeChanged`). The slots
are alike: the PiP is whichever is small, so after a swap "Open in picture-in-picture"
changes the small picture's channel and closing PiP stops the small picture, the large one
playing on.

Each slot's streams are its tuner's (`Slot.tuner`, the window's `ui.live_tuner.LiveTuner`
for that player, or one the manager makes for a slot without one): the large slot's tunes
are the shell's, and this manager opens, changes and closes the small one. A tuner keeps its
stream across role changes, and its budget slots carry the slot's name as their purpose
("main", "pip", "t2" …), so the provider's budget counts each stream once and "every stream
but this slot's" is the holders under other names. Opening the small slot:
- it asks the provider's connection budget for a slot with `queue=False`: when none is free,
  `denied(limit)` and nothing opens (a PiP never waits behind the main stream). The budget
  always grants later, so the slot starts once it is granted;
- a stopped small slot (muted first, so nothing is heard while it stops) gives its tickets
  back once its streams are closed, and only then asks for a new one: the old stream is
  always released before the new one opens;
- when the small stream cannot start, fails for good (`LiveSession.failed`) or loses its slot
  on a channel change, PiP closes and `pipFailed` says why in a fixed message (`failure` keeps
  what the stream itself said, for the shell's STB retry).
A swap waits for nothing: while the small slot's channel change is still on its way, it is
refused (False).

While a multiview's grid shows (`enter_grid` … `leave_grid`), picture in picture stands
aside: the slots are tiles, the large one is the focused tile, and a tile opens a channel
directly (`open_slot`: no time-shift) only when its provider's other streams leave it one;
else `slotDenied` and nothing opens: a tile never waits, nor asks again by itself. A tile's
stream that fails says so (`slotFailed`); `close_slot` stops one. Leaving the grid keeps one
tile as the PiP, or none.

Audio follows the large picture: the PiP (and a tile) opens muted, and while the slot taking
the large role is muted it takes the user's mute from the old large slot, which is muted in
its place. Once the user makes a small slot audible (the mixer's PiP row raised above 0, or
an explicit unmute), giving it the large role changes no mute: each stream keeps its own, so
both stay audible; muting it again brings the default back. Each stream also has a volume
level of its own, which always stays with it across role changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
import logging
from typing import Any, Callable, Optional, Protocol

from PySide6.QtCore import QObject, Signal, SignalInstance

from services.budgeted_stream import RELEASE_CAP_MS, BudgetedStream
from services.connection_budget import DENIED, ConnectionBudget
from services.live.live_session import LiveSession
from services.live.refusals import REFUSALS
from services.playback import Session
from services.zapping import ZapContext

logger = logging.getLogger(__name__)

# A slot's streams have its name as their budget purpose: these are the first two slots'.
PURPOSE = "pip"  # the "pip" player's
MAIN_PURPOSE = "main"  # the "main" player's (LiveTuner's default)
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


@dataclass
class Slot:
    name: str  # "main", "pip", or a tile's ("t2" … "t8"): its streams' budget purpose
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


@dataclass
class _Open:
    """A PiP channel waiting for the old stream's release or for its budget slot."""

    slot: str  # the name of the slot it opens in (the small one when it was asked for)
    provider: str
    limit: Optional[int]
    start_kwargs: dict[str, Any]


class SessionManager(QObject):
    largeChanged = Signal(str, str)  # the slot that is large now, and the one that was
    pipStateChanged = Signal(bool)  # PiP open/closed
    pipStarted = Signal()  # the small slot's stream started: PiP opened, or its channel changed
    denied = Signal(int)  # limit (budget refused PiP)
    # A fixed message: the PiP's live session did not start or failed for good (the limit
    # message for a provider refusal), or the budget refused a channel change of a PiP it had
    # granted (then without denied). PiP is closed by then.
    pipFailed = Signal(str)
    slotTimeline = Signal(str, dict)  # slot name, LiveSession timeline dict
    # The multiview's tiles (slot names): a channel was asked for (True) or the tile closed;
    # its provider had no stream to spare (the limit; nothing opened); its stream failed.
    slotState = Signal(str, bool)
    slotDenied = Signal(str, int)
    slotFailed = Signal(str, str)

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
        self._budget = budget
        self._release_cap_ms = release_cap_ms
        self._slots: dict[str, Slot] = {}
        self._large = main
        self._small = pip  # the PiP's slot, open or not (never the large one)
        self._open = False  # pipStateChanged(True) was emitted, and no close since
        self._pending: Optional[_Open] = None
        self._limit: Optional[int] = None  # the provider limit of the PiP stream that starts
        # What the last pipFailed stood for: the small stream's own failure (LiveSession's
        # message, a refusal one of REFUSALS); "" when its start raised or the budget refused.
        self.failure = ""
        self._levels: dict[str, int] = {}
        self._muted: dict[str, bool] = {}
        self._tuners: dict[str, SlotTuner] = {}
        self._grid = False  # the multiview's grid shows (enter_grid)
        self._direct: set[str] = set()  # the tiles open_slot opened, until they stop
        self._add(main, MAIN_LEVEL, muted=False)
        self._add(pip, PIP_LEVEL, muted=True)
        # A wanted level makes PlaybackSession apply it on every media it opens; without one
        # the main player takes whatever level its audio output opens at.
        main.playback.set_volume(self._levels[main.name])

    def add_slot(self, slot: Slot) -> None:
        """Another slot (a multiview tile's): muted, at the PiP's default level, until the
        large role or the user makes it audible."""
        if slot.name in self._slots:
            raise ValueError(f"There is a {slot.name} slot already")
        self._add(slot, PIP_LEVEL, muted=True)

    def _add(self, slot: Slot, level: int, *, muted: bool) -> None:
        tuner = slot.tuner
        if tuner is None:
            tuner = _StreamTuner(slot.live, self._budget, slot.name, self._release_cap_ms, self)
        stream = tuner.stream
        if stream is None:
            raise ValueError(f"The {slot.name} slot has no live session to open")
        self._slots[slot.name] = slot
        self._tuners[slot.name] = tuner
        self._levels[slot.name] = level
        self._muted[slot.name] = muted
        tuner.released.connect(self._proceed)  # a slot let go: a waiting open may go on
        stream.granted.connect(self._on_granted)
        slot.live.failed.connect(partial(self._on_failed, slot.name))
        slot.live.timelineChanged.connect(partial(self._on_timeline, slot.name))
        denied = getattr(tuner, "denied", None)  # LiveTuner's: a tile's direct open
        if denied is not None:
            denied.connect(partial(self._on_slot_denied, slot.name))
        errors = getattr(slot.playback, "errorOccurred", None)
        if errors is not None:
            errors.connect(partial(self._on_slot_error, slot.name))

    # ---- roles --------------------------------------------------------------------------------

    def slots(self) -> list[Slot]:
        """Every slot, in the order they were made ("main", "pip", then the tiles')."""
        return list(self._slots.values())

    def slot(self, name: str) -> Slot:
        """The slot named `name` (KeyError: there is none)."""
        return self._slot(name)

    def large(self) -> Slot:
        return self._large

    def other(self) -> Slot:
        """The PiP's slot, open or not: the one a picture-in-picture opens in."""
        return self._small

    def small(self) -> Optional[Slot]:
        """The PiP's slot while PiP is open; None when it is closed."""
        return self._small if self._open else None

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

    def set_large(self, name: str) -> bool:
        """Give slot `name` the large role; False (and nothing changes) when it has it, or
        the PiP's channel change is still on its way.

        No stream is played, stopped or started, and no level changes. The slot that was
        large becomes the PiP's when `name` was the PiP's (a swap); else the PiP's slot
        stays. A muted slot taking the role takes the user's mute of the old large one,
        which is muted in its place; one the user made audible leaves both mutes as they are
        (module docstring).
        """
        new = self._slot(name)
        old = self._large
        if new is old or self._pending is not None:
            return False
        self._large = new
        if new is self._small:
            self._small = old
        if self._muted[new.name]:  # the default: audio follows the large picture
            users_mute = self._muted[old.name]
            self._set_mute(old, True)
            self._set_mute(new, users_mute)
        self.largeChanged.emit(new.name, old.name)
        return True

    def swap(self) -> bool:
        """Exchange the large and small roles (`set_large` of the small slot); False (and
        nothing changes) when PiP is closed or its channel change is still on its way."""
        small = self.small()
        if small is None or self._pending is not None:
            return False
        return self.set_large(small.name)

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
        freed = self._small
        self._large, self._small = freed, large
        users_mute = self._muted[large.name]
        self._set_mute(large, True)  # the PiP is small and muted, as it opens
        self._set_mute(freed, users_mute)
        self._open = True
        self.largeChanged.emit(freed.name, large.name)  # the shell trades what each shows first
        self.pipStateChanged.emit(True)
        return True

    # ---- the multiview's tiles ----------------------------------------------------------------

    def grid(self) -> bool:
        """The multiview's grid shows."""
        return self._grid

    def enter_grid(self) -> None:
        """The multiview's grid shows: picture in picture stands aside, its slot a tile that
        plays on (no pipStateChanged: the shell hides the PiP's frame and label itself). The
        PiP's open or change on its way, if any, is the caller's to close first."""
        self._grid = True
        self._open = False

    def leave_grid(self, keep: Optional[str] = None) -> None:
        """Back from the grid: slot `keep` (one that plays, not the large one) is the PiP's and
        PiP opens; None: PiP stays closed. pipStateChanged says which either way. The other
        tiles are the caller's to close first."""
        self._grid = False
        self._direct.clear()
        if keep is not None and keep != self._large.name:
            self._small = self._slot(keep)
            self._open = True
            self.pipStateChanged.emit(True)
            return
        if self._small is self._large:  # never: set_large keeps them apart
            self._small = next(s for s in self._slots.values() if s is not self._large)
        self._open = False
        self.pipStateChanged.emit(False)

    def open_slot(
        self,
        name: str,
        action: Callable[[], None],
        *,
        channel: dict,
        provider: str,
        limit: Optional[int],
    ) -> None:
        """Open `channel` in tile `name` directly: what it played stops, and once that is
        released `action` plays the channel, if `provider`'s other streams leave one within
        `limit`; else slotDenied(name, limit) and nothing plays. The tile's tuner holds the
        stream's budget slot, under the tile's name, until it stops."""
        slot = self._slot(name)
        tuner = self._tuners[name]
        open_ = getattr(tuner, "open", None)
        if open_ is None:
            raise TypeError(f"The {name} slot's tuner cannot open a channel directly")
        slot.channel = channel
        self._direct.add(name)
        slot.playback.set_volume(self._levels[name])
        self._set_mute(slot, self._muted[name])
        self.slotState.emit(name, True)
        open_(action, direct=True, provider=provider, limit=limit)

    def close_slot(self, name: str) -> None:
        """Stop tile `name` (muted first, so nothing is heard while it stops); its stream's
        budget slot goes back once it is released."""
        slot = self._slot(name)
        slot.playback.set_muted(True)  # its next start applies its own mute again
        self._tuners[name].stop()
        slot.channel = slot.group_key = None
        slot.ticket = None
        self._direct.discard(name)
        self.slotState.emit(name, False)

    def _on_slot_denied(self, name: str, limit: int) -> None:
        if name in self._direct:
            self._direct.discard(name)
            self._slot(name).channel = None  # it shows nothing
            self.slotDenied.emit(name, limit)

    def _on_slot_error(self, name: str, message: str) -> None:
        """A tile's direct play failed: its player's error (a channel's, never a title's)."""
        if name in self._direct:
            self.slotFailed.emit(name, message)

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

    def _on_failed(self, name: str, message: str) -> None:
        """A slot's stream failed for good: LiveSession recovered what it could. The small
        slot's closes PiP; the large slot's failures are the shell's."""
        slot = self._slot(name)
        stream = self._tuners[name].stream
        if slot is self._large:
            return
        if self._grid:  # a tile's time-shifted stream (one that played before the grid)
            if stream is not None and stream.running():
                self.slotFailed.emit(name, message)
            return
        if slot is not self._small:
            return
        if stream is None or not stream.running():
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
        """That stream's volume (0–100); raised on the small slot (or a tile that is not
        focused), it unmutes it too."""
        slot = self._slot(slot_name)
        level = max(0, min(100, int(value)))
        raised = level > self._levels[slot.name]
        self._levels[slot.name] = level
        slot.playback.set_volume(level)
        small = slot is self.small() or (self._grid and slot is not self._large)
        if raised and small:  # only raising it makes the PiP (or a tile) audible
            self._set_mute(slot, False)

    def _set_mute(self, slot: Slot, muted: bool) -> None:
        self._muted[slot.name] = bool(muted)
        slot.playback.set_muted(bool(muted))

    def _slot(self, name: str) -> Slot:
        return self._slots[name]  # KeyError: there is no such slot

    # ---- timelines ----------------------------------------------------------------------------

    def _on_timeline(self, name: str, state: dict) -> None:
        self.slotTimeline.emit(name, state)
