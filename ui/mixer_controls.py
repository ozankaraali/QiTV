"""The TV shell's volume: the volume keys, mute and the wheel, shown on the mixer (Phase 4).

Every volume action (+/-, the media volume keys, M, ↑/↓ and the wheel during a movie) acts on
the large picture and shows the mixer (`ui/overlays/mixer.py`), as the transport's speaker
does: one row ("Main") without picture in picture, two with it ("Main", the large picture,
and "PiP", the small one). While it shows, the keys act on its selected row: ↑/↓ (and +/-)
change it by 5, ←/→ or Tab select the other row, M mutes it and Esc hides the mixer. The PiP
label's speaker mutes or unmutes the small picture without showing the mixer (one that shows
follows). The arrows are the arrow keys only (a remote's Ch± and PageUp/PageDown still zap),
and ←/→ and Tab are the mixer's only with two rows (else they still seek).

Levels and mutes belong to the streams (SessionManager): a swap moves each with its stream,
so the rows trade values. Raising the small picture's level makes it audible; lowering a
muted one keeps it muted. Config `pip_volume` is the small picture's level: the "pip"
player's, saved (once the mixer hides) only from changes made while it is the small one,
and given back to it at startup.

Without a mixer (tests) or without picture in picture, a toast says the volume instead, and
without a SessionManager the player's own volume and mute are used.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, Qt

from services.redact import safe_message
from services.sessions import PIP_LEVEL, SessionManager, Slot
from ui.keymap import Action
from ui.overlays.mixer import MixerRow

logger = logging.getLogger(__name__)

VOLUME_STEP = 5
MAIN_ROW = 0  # the mixer's first row; the PiP's is the second
MAIN_TITLE, PIP_TITLE = "Main", "PiP"
_UP, _DOWN = Qt.Key.Key_Up.value, Qt.Key.Key_Down.value
_OTHER_ROW = (Action.SEEK_BACK, Action.SEEK_FWD, Action.TAB)


class MixerControls(QObject):
    def __init__(
        self,
        *,
        mixer,
        sessions: Optional[SessionManager],
        session: Callable[[], Any],
        config,
        toast: Callable[..., None],
        names: Callable[[], tuple[str, str]],
        audio_changed: Callable[[], None],
        parent: Optional[QObject] = None,
    ) -> None:
        """`mixer` is the overlay (None: toasts); `session()` the large picture's player;
        `names()` the large and the small picture's channel names; `audio_changed()` runs
        after any level or mute change, and after PiP opened, closed or swapped (the PiP
        label's and the transport's speakers follow)."""
        super().__init__(parent)
        self._overlay = mixer
        self.sessions = sessions
        self._session = session
        self.config = config
        self._toast = toast
        self._names = names
        self._audio_changed = audio_changed
        self._row = MAIN_ROW
        self._pip_level: Optional[int] = None  # the small picture's changed level, to remember
        self._main_muted = False  # without a SessionManager: the player's own mute

    def wire(self) -> None:
        sessions, overlay = self.sessions, self._overlay
        if sessions is not None:
            level = getattr(self.config, "pip_volume", PIP_LEVEL)
            sessions.set_level("pip", int(level))  # the PiP's remembered level
        if overlay is not None:
            overlay.levelRequested.connect(self._on_level)
            overlay.muteRequested.connect(self._on_mute)
            overlay.rowSelected.connect(self._on_selected)
            overlay.closed.connect(self.save)

    # ---- actions ------------------------------------------------------------------------------

    def visible(self) -> bool:
        overlay = self._overlay
        return overlay is not None and overlay.isVisible()

    def change(self, delta: int) -> None:
        """A volume key or the wheel: the selected row while the mixer shows, else Main."""
        row = self._row if self.visible() else MAIN_ROW
        level = max(0, min(100, self._level(row) + delta))
        self._set_level(row, level)
        if self._overlay is None:
            self._toast(f"Volume {level}%")
        self._show(row)

    def mute(self) -> None:
        """M: mute or unmute the selected row while the mixer shows, else Main."""
        row = self._row if self.visible() else MAIN_ROW
        if self.sessions is None and self._overlay is None:
            session = self._session()
            muted = session.toggle_mute()
            self._main_muted = bool(muted)
            self._audio_changed()
            self._toast("Muted" if muted else f"Volume {session.volume()}%")
            return
        muted = not self._muted(row)
        self._set_muted(row, muted)
        if self._overlay is None:
            self._toast("Muted" if muted else f"Volume {self._level(row)}%")
        self._show(row)

    def handle(self, action: Action, key: Optional[int] = None) -> bool:
        """The keys while the mixer shows (False: not the mixer's, or it is hidden).

        `key` is the Qt key pressed: ↑/↓ are the arrow keys' CH_PREV/CH_NEXT only.
        """
        if not self.visible():
            return False
        if action is Action.VOL_UP or (action is Action.CH_PREV and key == _UP):  # ↑ louder
            self.change(VOLUME_STEP)
        elif action is Action.VOL_DOWN or (action is Action.CH_NEXT and key == _DOWN):
            self.change(-VOLUME_STEP)
        elif action in _OTHER_ROW and len(self._slots()) > 1:
            self._show((self._row + 1) % len(self._slots()))
        elif action is Action.MUTE:
            self.mute()
        elif action is Action.ESCAPE:
            self.hide()
        else:
            return False
        return True

    def open_main(self) -> None:
        """The transport's speaker: the mixer on the large picture's row, changing nothing."""
        if self._overlay is None:
            muted = self._muted(MAIN_ROW)
            self._toast("Muted" if muted else f"Volume {self._level(MAIN_ROW)}%")
        self._show(MAIN_ROW)

    def main_muted(self) -> bool:
        """Whether the large picture is muted."""
        return self._muted(MAIN_ROW)

    def toggle_small_mute(self) -> None:
        """The PiP label's speaker: mute or unmute the small picture (its label follows, and
        the mixer while it shows)."""
        sessions = self.sessions
        small = sessions.small() if sessions is not None else None
        if sessions is None or small is None:
            return
        sessions.set_muted(small.name, not sessions.muted(small.name))
        self.refresh()

    def refresh(self) -> None:
        """PiP opened, closed or swapped: the speakers follow, and the rows while the mixer
        shows."""
        if self.visible():
            self._show(self._row)
        else:
            self._audio_changed()

    def hide(self) -> None:
        if self._overlay is not None:
            self._overlay.dismiss()  # closed → save()

    def save(self) -> None:
        """Remember the "pip" player's level (config pip_volume) once it changed."""
        sessions = self.sessions
        level, self._pip_level = self._pip_level, None
        if sessions is None or level is None:
            return
        self.config.pip_volume = level
        try:
            self.config.save_config()
        except Exception as e:
            logger.warning("Couldn't save the picture-in-picture volume: %s", safe_message(e))

    # ---- the overlay --------------------------------------------------------------------------

    def _show(self, row: int) -> None:
        overlay = self._overlay
        self._audio_changed()
        if overlay is None:
            return
        slots = self._slots()
        self._row = max(0, min(row, len(slots) - 1))
        names = self._names()
        rows = [
            MixerRow(title, names[index], self._level(index), self._muted(index))
            for index, title in enumerate((MAIN_TITLE, PIP_TITLE)[: len(slots)])
        ]
        overlay.show_rows(rows, self._row)

    def _on_level(self, row: int, level: int) -> None:
        if row < len(self._slots()):
            self._set_level(row, level)
            self._show(row)

    def _on_mute(self, row: int) -> None:
        if row < len(self._slots()):
            self._set_muted(row, not self._muted(row))
            self._show(row)

    def _on_selected(self, row: int) -> None:
        self._row = row

    # ---- levels and mutes ---------------------------------------------------------------------

    def _slots(self) -> list[Optional[Slot]]:
        """The large picture's slot, then the small one's (None without a SessionManager)."""
        sessions = self.sessions
        if sessions is None:
            return [None]
        small = sessions.small()
        return [sessions.large()] if small is None else [sessions.large(), small]

    def _slot(self, row: int) -> Optional[Slot]:
        slots = self._slots()
        return slots[min(row, len(slots) - 1)]

    def _level(self, row: int) -> int:
        slot = self._slot(row)
        if slot is None or self.sessions is None:
            return int(self._session().volume())
        return self.sessions.level(slot.name)

    def _set_level(self, row: int, level: int) -> None:
        slot = self._slot(row)
        if slot is None or self.sessions is None:
            self._session().set_volume(level)
            return
        self.sessions.set_level(slot.name, level)
        if slot.name == "pip" and slot is self.sessions.small():
            self._pip_level = level  # pip_volume: the small picture's level, as set now

    def _muted(self, row: int) -> bool:
        slot = self._slot(row)
        if slot is None or self.sessions is None:
            return self._main_muted
        return self.sessions.muted(slot.name)

    def _set_muted(self, row: int, muted: bool) -> None:
        slot = self._slot(row)
        if slot is None or self.sessions is None:
            if muted != self._muted(row):
                self._main_muted = bool(self._session().toggle_mute())
            return
        self.sessions.set_muted(slot.name, muted)
