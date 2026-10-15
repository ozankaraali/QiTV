"""Two playback slots, `main` and `pip`, and which of them is large (spec §7, Phase 4).

Each slot has its own player (`Session`) and `LiveSession`. Swapping exchanges the roles
only: no slot is stopped, restarted or re-pointed, and the surfaces trade geometry (the UI
does that on `largeChanged`).

The main slot's tune path (its budget slot, purpose "main", and the wait for the old stream's
release) is `ui.live_tuner.LiveTuner`'s. This manager opens, changes and closes the PiP slot:
- it asks the provider's connection budget for a slot with purpose "pip" and `queue=False`:
  when none is free, `denied(limit)` and nothing opens (a PiP never waits behind the main
  stream). The budget always grants later, so the slot starts on `granted` for its ticket;
- a stopped PiP slot gives its ticket back once `LiveSession.released` says its upstream is
  closed (or after `RELEASE_CAP_MS`, a backstop to LiveSession's own caps), and only then
  asks for a new one: the old stream is always released before the new one opens.

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
from typing import Any, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from services.connection_budget import DENIED, ConnectionBudget
from services.live.live_session import LiveSession
from services.playback import Session
from services.zapping import ZapContext

logger = logging.getLogger(__name__)

PURPOSE = "pip"  # the budget purpose of the PiP stream (LiveTuner's is "main")
RELEASE_CAP_MS = 2500  # LiveSession caps its own waits for the release at 2 s
MAIN_LEVEL = 100  # libVLC's default volume
PIP_LEVEL = 30  # the PiP stream's level until the mixer sets one (config pip_volume's default)
PIP_FAILED = "Couldn't open this channel in picture-in-picture"


def limit_message(limit: int) -> str:
    """The toast for a provider limit that leaves no stream for the PiP."""
    return f"Your provider allows {limit} stream{'s' if limit != 1 else ''} at a time"


@dataclass
class Slot:
    name: str  # "main" | "pip"
    playback: Session
    live: LiveSession
    channel: Optional[dict] = None
    group_key: Optional[str] = None
    ticket: Optional[int] = None  # the PiP slot's budget ticket (LiveTuner holds main's)


@dataclass
class _Open:
    """A PiP channel waiting for the old stream's release or for its budget slot."""

    provider: str
    limit: Optional[int]
    start_kwargs: dict[str, Any]


class SessionManager(QObject):
    largeChanged = Signal(str)  # name of the slot that is now large
    pipStateChanged = Signal(bool)  # PiP open/closed
    denied = Signal(int)  # limit (budget refused PiP)
    # A fixed message: the PiP's live session did not start, or the budget refused a channel
    # change of a PiP it had granted (then without denied). PiP is closed by then.
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
        self._running = False  # pip.live.start() was called and not stopped since
        self._pending: Optional[_Open] = None
        self._releasing: list[int] = []  # given back once the PiP upstream is closed
        self._live_wait = False  # pip.live.stop() was called; released() has not come
        self._levels = {main.name: MAIN_LEVEL, pip.name: PIP_LEVEL}
        self._muted = {main.name: False, pip.name: True}
        self._cap = QTimer(self, singleShot=True, interval=release_cap_ms)
        self._cap.timeout.connect(self._on_cap)
        budget.granted.connect(self._on_granted)
        pip.live.released.connect(self._on_released)
        main.live.timelineChanged.connect(self._on_main_timeline)
        pip.live.timelineChanged.connect(self._on_pip_timeline)

    # ---- roles --------------------------------------------------------------------------------

    def large(self) -> Slot:
        return self._large

    def small(self) -> Optional[Slot]:
        """The other slot while PiP is open; None when it is closed."""
        if not self._open:
            return None
        return self._pip if self._large is self._main else self._main

    def pip_open(self) -> bool:
        return self._open

    def slot_showing(self, channel: dict) -> Optional[Slot]:
        """The slot showing `channel` (by `ZapContext.same`), large first; None if neither."""
        for slot in (self._large, self.small()):
            if slot is not None and ZapContext.same(slot.channel, channel):
                return slot
        return None

    def swap(self) -> bool:
        """Exchange the large and small roles; False (and nothing changes) when PiP is closed.

        No stream is played, stopped or started, and no level changes. A muted small slot
        takes the user's mute of the large one, which is muted in its place; a small slot the
        user made audible keeps both streams' mutes as they are (module docstring).
        """
        small = self.small()
        if small is None:
            return False
        large = self._large
        self._large = small
        if self._muted[small.name]:  # the default: audio follows the large picture
            users_mute = self._muted[large.name]
            self._set_mute(large, True)  # the new small slot
            self._set_mute(small, users_mute)
        self.largeChanged.emit(small.name)
        return True

    # ---- the PiP slot -------------------------------------------------------------------------

    def open_pip(
        self,
        channel: dict,
        *,
        provider: dict,
        xtream_max: Optional[int],
        start_kwargs: dict,
    ) -> None:
        """Open `channel` in the PiP slot, or change the channel of an open one.

        `start_kwargs` go to `LiveSession.start()` once the budget grants the slot and any
        earlier PiP stream is released.
        """
        self._stop_live()
        self._pip.channel = channel
        limit = self._budget.limit_for(provider, xtream_max)
        self._pending = _Open(str(provider.get("name", "")), limit, dict(start_kwargs))
        self._proceed()

    def close_pip(self) -> None:
        """Stop the PiP slot (a large one swaps back first); its ticket goes back on release."""
        self._pending = None
        if self._open and self._large is self._pip:
            self.swap()
        self._stop_live()
        self._pip.channel = self._pip.group_key = None
        if self._open:
            self._open = False
            self.pipStateChanged.emit(False)

    def _stop_live(self) -> None:
        pip = self._pip
        ticket, pip.ticket = pip.ticket, None
        if self._running:
            self._running = False
            if ticket is not None:
                self._releasing.append(ticket)
            self._live_wait = True
            self._cap.start()
            pip.live.stop()  # released() follows, perhaps at once
        elif ticket is not None:
            self._budget.release(ticket)  # never started: nothing upstream to wait for

    def _proceed(self) -> None:
        pending = self._pending
        if pending is None or self._live_wait or self._pip.ticket is not None:
            return
        ticket = self._budget.request(pending.provider, pending.limit, purpose=PURPOSE, queue=False)
        if ticket == DENIED:  # only a limit denies, so it is a number
            was_open = self._open  # a channel change: the PiP had a slot
            self.close_pip()
            if was_open:
                self.pipFailed.emit(limit_message(int(pending.limit or 0)))
            else:
                self.denied.emit(pending.limit)
            return
        self._pip.ticket = ticket

    def _on_granted(self, ticket: int) -> None:
        pending, pip = self._pending, self._pip
        if pending is None or ticket != pip.ticket:
            return
        self._pending = None
        opening = not self._open
        if opening:
            self._muted[pip.name] = True  # the PiP opens small and muted
        pip.playback.set_volume(self._levels[pip.name])
        self._set_mute(pip, self._muted[pip.name])  # before it starts: no sound escapes
        self._running = True
        try:
            pip.live.start(**pending.start_kwargs)
        except Exception as error:
            logger.warning("Could not start the PiP channel (%s)", type(error).__name__)
            self._running = False
            pip.ticket = None
            self._budget.release(ticket)
            try:
                pip.live.stop()
            except Exception:
                logger.debug("Stopping the PiP live session after a failed start failed too")
            self.close_pip()
            self.pipFailed.emit(PIP_FAILED)
            return
        if opening:
            self._open = True
            self.pipStateChanged.emit(True)

    def _on_released(self) -> None:
        if not self._live_wait:
            return  # a stop this manager did not make
        self._live_wait = False
        self._cap.stop()
        tickets, self._releasing = self._releasing, []
        for ticket in tickets:
            self._budget.release(ticket)
        self._proceed()

    def _on_cap(self) -> None:
        if self._live_wait:
            logger.warning("The PiP channel did not report its stream closed; going on.")
            self._on_released()

    # ---- audio --------------------------------------------------------------------------------

    def muted(self, slot_name: str) -> bool:
        return self._muted[slot_name]

    def set_muted(self, slot_name: str, muted: bool) -> None:
        """The user's mute for that slot's stream."""
        self._set_mute(self._slot(slot_name), muted)

    def level(self, slot_name: str) -> int:
        return self._levels[slot_name]

    def set_level(self, slot_name: str, value: int) -> None:
        """That stream's volume (0–100); above 0 on the small slot, it unmutes it too."""
        slot = self._slot(slot_name)
        level = max(0, min(100, int(value)))
        self._levels[slot.name] = level
        slot.playback.set_volume(level)
        if level > 0 and slot is self.small():
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
