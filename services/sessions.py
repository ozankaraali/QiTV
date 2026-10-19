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
(`open_slot`: through the time-shift pipeline as a PiP does, else directly) only when its
provider's other streams leave it one; else `slotDenied` and nothing opens: a tile never
waits, nor asks again by itself. A tile's memory buffer is small and the focused tile's the
user's memory per stream (`set_large` grows the new focus and shrinks the old one), and only
the focused tile's stay-live changes its rate (the others play at 1x and only jump to live).
A tile's stream that fails (or a direct one that ends) says so (`slotFailed`); `close_slot`
stops one, and a tile closed is muted again. Leaving the grid keeps one tile as the PiP, or
none; an open still on its way for the large or the kept tile goes on.

Audio follows the large picture: the PiP (and a tile) opens muted, and while the slot taking
the large role is muted it takes the user's mute from the old large slot, which is muted in
its place. Once the user makes a small slot audible (the mixer's PiP row raised above 0, or
an explicit unmute), giving it the large role changes no mute: each stream keeps its own, so
both stay audible; muting it again brings the default back. The volume levels belong to the
roles, as a TV's does: the large picture plays at the main level and the PiP at the PiP's
(the shell remembers both), so a swap, a focus moved in the grid or a tile kept big changes
no loudness; a tile without the focus has a level of its own while the grid shows.

The grid's sound follows the focus as above until the user mixes it: a level or a mute of a
tile without the focus changed (`set_level`, `set_muted`: the mixer's rows, a tile's
speaker) makes the grid "mixed" (`mixed()`, `mixChanged`). From then until the grid is left,
moving the focus moves the controls only: no mute changes, and each tile keeps its level
(the main level is the focused tile's own, whichever has the focus). Changes to the focused
tile alone (the volume keys, M, its own row) never start a mix: they are the main sound,
which follows the focus. `follow_focus()` goes back: the focused tile heard, the others
muted. `end_mix()`, as the grid is left (before its tiles stop), and `leave_grid` end it:
the big picture is heard, unless the mix had every tile muted, and the PiP kept from the
grid keeps its mute, as a PiP made audible does; the normal rule holds again.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
import logging
from typing import Any, Callable, Optional, Protocol

from PySide6.QtCore import QObject, Signal, SignalInstance

from services.budgeted_stream import RELEASE_CAP_MS, BudgetedStream
from services.connection_budget import DENIED, ConnectionBudget
from services.i18n import N_, tr_n, translate
from services.live.live_session import LiveSession
from services.live.refusals import REFUSALS
from services.playback import PlaybackState, Session
from services.zapping import ZapContext

logger = logging.getLogger(__name__)

# A slot's streams have its name as their budget purpose: these are the first two slots'.
PURPOSE = "pip"  # the "pip" player's
MAIN_PURPOSE = "main"  # the "main" player's (LiveTuner's default)
MAIN_LEVEL = 100  # libVLC's default volume
PIP_LEVEL = 30  # the PiP stream's level until the mixer sets one (config pip_volume's default)
# `pipFailed` sends these translated; the English values name them (N_).
PIP_FAILED = N_("Couldn't open this channel in picture-in-picture")  # i18n: error toast
# A refusal when the limit is not known.
LIMITED = N_("Your provider limits concurrent streams")  # i18n: error toast


def limit_message(limit: Optional[int]) -> str:
    """The toast for a provider limit that leaves no stream for the PiP."""
    if not limit:
        return translate(LIMITED)
    return tr_n(
        "Your provider allows {n} stream at a time",  # i18n: error toast
        "Your provider allows {n} streams at a time",
        limit,
    )


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
    """A PiP's (or a tile's) channel waiting for the old stream's release or its slot."""

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
    # The grid's sound: True once the user mixed it (each tile keeps its sound), False when
    # it follows the focus again (`follow_focus`, `end_mix`, leaving the grid).
    mixChanged = Signal(bool)

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
        self._levels: dict[str, int] = {}  # a tile's own level while the grid shows
        self._main_level = MAIN_LEVEL  # the large picture's
        self._pip_level = PIP_LEVEL  # the PiP's (the small slot outside the grid)
        self._muted: dict[str, bool] = {}
        self._tuners: dict[str, SlotTuner] = {}
        self._grid = False  # the multiview's grid shows (enter_grid)
        self._mixed = False  # the user mixed the grid's sound (module docstring)
        self._sizes: Optional[tuple[int, int]] = None  # (focused, other) tile buffers
        self._direct: set[str] = set()  # the tiles open_slot opened directly, until they stop
        self._tile_opens: dict[str, _Open] = {}  # tiles waiting for a release or a slot
        self._add(main, PIP_LEVEL, muted=False)
        self._add(pip, PIP_LEVEL, muted=True)
        # A wanted level makes PlaybackSession apply it on every media it opens; without one
        # the main player takes whatever level its audio output opens at.
        main.playback.set_volume(self._level_of(main))

    def add_slot(self, slot: Slot) -> None:
        """Another slot (a multiview tile's): muted, at the PiP's default level, until the
        large role or the user makes it audible."""
        if slot.name in self._slots:
            raise ValueError(f"There is a {slot.name} slot already")
        self._add(slot, PIP_LEVEL, muted=True)
        if self._grid:  # a tile made while the grid shows: small, at 1x
            self._as_tile(slot, focused=False)

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
        states = getattr(slot.playback, "stateChanged", None)
        if states is not None:
            states.connect(partial(self._on_slot_player_state, slot.name))

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

        No stream is played, stopped or started; each takes its new role's level. The slot that was
        large becomes the PiP's when `name` was the PiP's (a swap); else the PiP's slot
        stays. A muted slot taking the role takes the user's mute of the old large one,
        which is muted in its place; one the user made audible leaves both mutes as they are
        (module docstring). In a mixed grid nothing of the sound changes: each tile keeps its
        mute and its level.
        """
        new = self._slot(name)
        old = self._large
        if new is old or self._pending is not None:
            return False
        self._large = new
        if new is self._small:
            self._small = old
        if self.mixed():  # the main level is the focused tile's own: it moves with the focus
            self._levels[old.name], self._main_level = self._main_level, self._levels[new.name]
        elif self._muted[new.name]:  # the default: audio follows the large picture
            users_mute = self._muted[old.name]
            self._set_mute(old, True)
            self._set_mute(new, users_mute)
        self._apply_level(new)  # the levels stay with the roles
        self._apply_level(old)
        if self._grid:  # the buffer and stay-live follow the focus
            self._as_tile(new, focused=True)
            self._as_tile(old, focused=False)
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
        self._apply_level(freed)
        self._apply_level(large)
        self.largeChanged.emit(freed.name, large.name)  # the shell trades what each shows first
        self.pipStateChanged.emit(True)
        return True

    # ---- the multiview's tiles ----------------------------------------------------------------

    def grid(self) -> bool:
        """The multiview's grid shows."""
        return self._grid

    def enter_grid(self, *, full_bytes: int = 0, tile_bytes: int = 0) -> None:
        """The multiview's grid shows: picture in picture stands aside, its slot a tile that
        plays on (no pipStateChanged: the shell hides the PiP's frame and label itself). The
        PiP's open or change on its way, if any, is the caller's to close first.

        With both sizes: a tile's memory buffer holds `tile_bytes` and the focused tile's
        `full_bytes` (the user's memory per stream); `set_large` moves them with the focus
        (a disk buffer keeps the user's size)."""
        self._grid = True
        self._mixed = False  # the sound follows the focus until the user mixes it
        self._open = False
        self._sizes = (full_bytes, tile_bytes) if full_bytes > 0 and tile_bytes > 0 else None
        for slot in self._slots.values():
            if slot is not self._large:
                if slot is self._small:  # the PiP's level goes with it into the grid
                    self._levels[slot.name] = self._pip_level
                self._as_tile(slot, focused=False)

    def leave_grid(self, keep: Optional[str] = None) -> None:
        """Back from the grid: slot `keep` (one that plays, not the large one) is the PiP's and
        PiP opens; None: PiP stays closed. pipStateChanged says which either way. The other
        tiles are the caller's to close first. The kept tile's buffer grows back to the
        memory per stream, as a PiP's. A mix not ended yet ends (`end_mix`)."""
        self.end_mix()
        sizes = self._sizes
        self._grid = False
        # An open still on its way for the large or the kept tile goes on (a zap just before
        # V); the other tiles' are the caller's to have closed.
        spared = {self._large.name, keep}
        self._direct = {name for name in self._direct if name in spared}
        self._tile_opens = {
            name: opening for name, opening in self._tile_opens.items() if name in spared
        }
        for slot in self._slots.values():
            self._background(slot, False)  # a PiP's stay-live steers as ever
        if keep is not None and keep != self._large.name:
            self._small = self._slot(keep)
            self._apply_level(self._small)  # the PiP's level, now it is the PiP
            if sizes is not None:
                self._resize(self._small, sizes[0])
            self._sizes = None
            self._open = True
            self.pipStateChanged.emit(True)
            return
        self._sizes = None
        if self._small is self._large:  # never: set_large keeps them apart
            self._small = next(s for s in self._slots.values() if s is not self._large)
        self._open = False
        self.pipStateChanged.emit(False)

    def tile_bytes(self, focused: bool) -> Optional[int]:
        """The memory buffer a tile opens with (None: as outside the grid)."""
        sizes = self._sizes
        if sizes is None:
            return None
        return sizes[0] if focused else sizes[1]

    def _as_tile(self, slot: Slot, *, focused: bool) -> None:
        """A tile's buffer and stay-live follow its focus: the focused one's buffer is the
        full one, the others' small, and only the focused one's rate changes (the others play
        at 1x and only jump to live: muted, they need no nudge)."""
        size = self.tile_bytes(focused)
        if size is not None:
            self._resize(slot, size)
        self._background(slot, not focused)

    @staticmethod
    def _resize(slot: Slot, size: int) -> None:
        resize = getattr(slot.live, "resize", None)
        if resize is not None:
            resize(size)

    @staticmethod
    def _background(slot: Slot, background: bool) -> None:
        set_background = getattr(slot.live, "set_background", None)
        if set_background is not None:
            set_background(background)

    def open_slot(
        self,
        name: str,
        action: Optional[Callable[[], None]] = None,
        *,
        channel: dict,
        provider: str,
        limit: Optional[int],
        start_kwargs: Optional[dict[str, Any]] = None,
    ) -> None:
        """Open `channel` in tile `name`: what it played stops first. Within `provider`'s
        `limit`, never waiting for a stream (none free: slotDenied(name, limit), nothing
        plays). The tile's streams hold their budget slots under its name.

        With `start_kwargs` through the time-shift pipeline, as a PiP opens: its slot is
        asked for once what the tile played is released, and `LiveSession.start()` follows
        the grant. Without, directly: `action` plays the channel once that is released."""
        slot = self._slot(name)
        tuner = self._tuners[name]
        self._tile_opens.pop(name, None)
        if slot is not self._large:
            self._muted[name] = True  # a tile beside the focus opens muted
        if start_kwargs is not None:
            self._stop(slot)
            self._direct.discard(name)
            slot.channel = channel
            self._tile_opens[name] = _Open(name, provider, limit, dict(start_kwargs))
            self.slotState.emit(name, True)
            self._proceed()
            return
        open_ = getattr(tuner, "open", None)
        if open_ is None or action is None:
            raise TypeError(f"The {name} slot cannot open a channel directly")
        slot.channel = channel
        self._direct.add(name)
        slot.playback.set_volume(self._level_of(slot))
        self._set_mute(slot, self._muted[name])
        self.slotState.emit(name, True)
        open_(action, direct=True, provider=provider, limit=limit)

    def close_slot(self, name: str) -> None:
        """Stop tile `name` (muted first, so nothing is heard while it stops); its stream's
        budget slot goes back once it is released."""
        slot = self._slot(name)
        self._tile_opens.pop(name, None)
        slot.playback.set_muted(True)  # its next start applies its own mute again
        if slot is not self._large:
            self._muted[name] = True  # its next channel is not heard beside the focused one
        self._tuners[name].stop()
        slot.channel = slot.group_key = None
        slot.ticket = None
        self._direct.discard(name)
        self.slotState.emit(name, False)

    def _start_tile(self, opening: _Open) -> None:
        """A tile's budget slot was granted: its live session starts (muted unless focused or
        made audible)."""
        slot = self._slot(opening.slot)
        stream = self._tuners[slot.name].stream
        assert stream is not None
        slot.playback.set_volume(self._level_of(slot))
        self._set_mute(slot, self._muted[slot.name])
        kwargs = dict(opening.start_kwargs)
        size = self.tile_bytes(slot is self._large)  # the focus may have moved since
        if size is not None and not kwargs.get("disk"):
            kwargs["max_bytes"] = size
        try:
            stream.start(**kwargs)
        except Exception as error:  # the stream gave its slot back and stopped the session
            logger.warning("Could not start a tile's channel (%s)", type(error).__name__)
            self.slotFailed.emit(slot.name, "")

    def _on_slot_denied(self, name: str, limit: int) -> None:
        if name in self._direct:
            self._direct.discard(name)
            self._slot(name).channel = None  # it shows nothing
            self.slotDenied.emit(name, limit)

    def _on_slot_error(self, name: str, message: str) -> None:
        """A tile's direct play failed: its player's error (a channel's, never a title's)."""
        if name in self._direct:
            self.slotFailed.emit(name, message)

    def _on_slot_player_state(self, name: str, state: str) -> None:
        """A direct stream of a tile beside the focus ended (the focused one reconnects as
        the big picture does): it failed, so its stream goes back and its card says so."""
        if (
            state == PlaybackState.ENDED.value
            and self._grid
            and name in self._direct
            and self._slot(name) is not self._large
        ):
            self.slotFailed.emit(name, "")

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
        """Ask for the pending channels' slots (the PiP's, the tiles') once their player let
        go of what it played; never queued."""
        for name, opening in list(self._tile_opens.items()):
            slot = self._slot(name)
            tuner = self._tuners[name]
            stream = tuner.stream
            if stream is None or tuner.releasing() or stream.ticket is not None:
                continue
            ticket = stream.request(opening.provider, opening.limit, queue=False)
            slot.ticket = stream.ticket
            if ticket == DENIED:  # only a limit denies, so it is a number
                del self._tile_opens[name]
                slot.channel = None  # it shows nothing
                self.slotDenied.emit(name, opening.limit)
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
        for name, tile in list(self._tile_opens.items()):
            stream = self._tuners[name].stream
            if stream is not None and stream.is_granted() and not stream.running():
                del self._tile_opens[name]
                self._start_tile(tile)
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
        slot.playback.set_volume(self._level_of(slot))
        # Asked for before it starts. A player that still has its audio output takes it at
        # once; after a stop, the next output comes up with the old one's state (muted by
        # _stop) and PlaybackSession applies this within ~30 ms of it opening.
        self._set_mute(slot, self._muted[slot.name])
        self._limit = pending.limit
        try:
            stream.start(**pending.start_kwargs)
        except Exception as error:  # the stream gave its slot back and stopped the session
            logger.warning("Could not start the PiP channel (%s)", type(error).__name__)
            self._fail(translate(PIP_FAILED))
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
        shown = limit_message(self._limit) if message in REFUSALS else translate(PIP_FAILED)
        self._fail(shown, message)

    def _fail(self, message: str, failure: str = "") -> None:
        self.failure = failure
        self.close_pip()
        self.pipFailed.emit(message)

    # ---- audio --------------------------------------------------------------------------------

    def muted(self, slot_name: str) -> bool:
        return self._muted[slot_name]

    def set_muted(self, slot_name: str, muted: bool) -> None:
        """The user's mute for that slot's stream; in the grid, a tile's without the focus
        mixes the grid's sound (module docstring)."""
        slot = self._slot(slot_name)
        if self._grid and slot is not self._large and muted != self._muted[slot.name]:
            self._mix()
        self._set_mute(slot, muted)

    # ---- the grid's mix -----------------------------------------------------------------------

    def mixed(self) -> bool:
        """The grid shows and the user mixed its sound: moving the focus changes none of it."""
        return self._grid and self._mixed

    def follow_focus(self) -> None:
        """The grid's sound follows the focus again (the mixer's "Follow focus"): the focused
        tile is heard, the others are muted; each keeps its level."""
        if not self._grid:
            return
        for slot in self._slots.values():
            self._set_mute(slot, slot is not self._large)
        self._unmix()

    def end_mix(self) -> None:
        """The grid is left (called before its tiles stop): a mix ends. The big picture is
        heard unless every tile was muted; the others keep their mutes (the PiP kept from
        the grid stays audible if it was, as one made audible does)."""
        if not self.mixed():
            return
        if self._muted[self._large.name] and not all(self._muted.values()):
            self._set_mute(self._large, False)
        self._unmix()

    def _mix(self) -> None:
        if not self._mixed:
            self._mixed = True
            logger.info("Multiview: the sound is mixed")
            self.mixChanged.emit(True)

    def _unmix(self) -> None:
        if self._mixed:
            self._mixed = False
            self.mixChanged.emit(False)

    def level(self, slot_name: str) -> int:
        """The level that slot plays at: its role's (module docstring)."""
        return self._level_of(self._slot(slot_name))

    def set_level(self, slot_name: str, value: int) -> None:
        """The volume (0–100) of that slot's role: the main level for the large picture,
        the PiP's for the small one, a tile's own for a tile without the focus. Raised on the
        small slot (or a tile that is not focused), it unmutes it too. A tile's without the
        focus mixes the grid's sound (module docstring)."""
        slot = self._slot(slot_name)
        level = max(0, min(100, int(value)))
        if self._grid and slot is not self._large and level != self._level_of(slot):
            self._mix()
        raised = level > self._level_of(slot)
        if slot is self._large:
            self._main_level = level
        elif self._grid:
            self._levels[slot.name] = level
        else:
            self._pip_level = level
        slot.playback.set_volume(level)
        small = slot is self.small() or (self._grid and slot is not self._large)
        if raised and small:  # only raising it makes the PiP (or a tile) audible
            self._set_mute(slot, False)

    def set_role_levels(self, *, main: int, pip: int) -> None:
        """The main and the PiP's levels as remembered (0–100), applied to the slots that
        have those roles now."""
        self._main_level = max(0, min(100, int(main)))
        self._pip_level = max(0, min(100, int(pip)))
        for slot in self._slots.values():
            self._apply_level(slot)

    def role_levels(self) -> tuple[int, int]:
        """(main, PiP): the levels to remember."""
        return self._main_level, self._pip_level

    def _level_of(self, slot: Slot) -> int:
        if slot is self._large:
            return self._main_level
        if self._grid:
            return self._levels[slot.name]
        return self._pip_level

    def _apply_level(self, slot: Slot) -> None:
        slot.playback.set_volume(self._level_of(slot))

    def _set_mute(self, slot: Slot, muted: bool) -> None:
        self._muted[slot.name] = bool(muted)
        slot.playback.set_muted(bool(muted))

    def _slot(self, name: str) -> Slot:
        return self._slots[name]  # KeyError: there is no such slot

    # ---- timelines ----------------------------------------------------------------------------

    def _on_timeline(self, name: str, state: dict) -> None:
        self.slotTimeline.emit(name, state)
