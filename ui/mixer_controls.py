"""The TV shell's volume: the volume keys, mute and the wheel, shown on the mixer (Phase 4).

Every volume action (+/-, the media volume keys, M, ↑/↓ and the wheel during a movie) acts on
the large picture and shows the mixer (`ui/overlays/mixer.py`), as the transport's speaker
does: one row ("Main") without picture in picture, two with it ("Main", the large picture,
and "PiP", the small one). While it shows, the keys act on its selected row: ↑/↓ (and +/-)
change it by 5, ←/→ or Tab select the other row, M mutes it and Esc hides the mixer. The PiP
label's speaker mutes or unmutes the small picture without showing the mixer (one that shows
follows). The arrows are the arrow keys only (a remote's Ch± and PageUp/PageDown still zap),
and ←/→ and Tab are the mixer's only with two rows (else they still seek).

Mutes belong to the streams (SessionManager); levels to the roles: the large picture plays
at the main level and the small one at the PiP's, whichever stream has the role, so a swap
or a focus moved in the grid changes no loudness. Raising the small picture's level makes it
audible; lowering a muted one keeps it muted. Config `volume` is the main level and
`pip_volume` the PiP's (2.2.1, optional; older builds ignore `volume`): saved once the mixer
hides, from changes made to those rows, and given back at startup.

While the multiview's grid shows, the rows are its tiles that play: the focused one first
(it is the large picture: the keys act on it), then the others in the grid's order, each
named "Tile N" after its place.

Without a mixer (tests) or without picture in picture, a toast says the volume instead, and
without a SessionManager the player's own volume and mute are used.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, Qt

from services.i18n import N_, tr, translate
from services.redact import safe_message
from services.sessions import MAIN_LEVEL, PIP_LEVEL, SessionManager, Slot
from ui.keymap import Action
from ui.overlays.mixer import MixerRow

logger = logging.getLogger(__name__)

VOLUME_STEP = 5
MAIN_ROW = 0  # the mixer's first row; the PiP's is the second
# i18n: the mixer's row for the large picture; max ~8 letters
MAIN_TITLE = N_("Main")
# i18n: the mixer's row for the small picture (picture in picture); max ~8 letters
PIP_TITLE = N_("PiP")
_UP, _DOWN = Qt.Key.Key_Up.value, Qt.Key.Key_Down.value
_OTHER_ROW = (Action.SEEK_BACK, Action.SEEK_FWD, Action.TAB)


def _volume_text(level: int) -> str:
    return tr("Volume {level}%", level=level)  # i18n: a toast; the sound's level in percent


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
        tiles: Optional[Callable[[], Optional[list[tuple[str, str, str]]]]] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        """`mixer` is the overlay (None: toasts); `session()` the large picture's player;
        `names()` the large and the small picture's channel names; `audio_changed()` runs
        after any level or mute change, and after PiP opened, closed or swapped (the PiP
        label's and the transport's speakers follow); `tiles()` the multiview's rows while its
        grid shows ((slot name, title, channel), the focused tile first), else None."""
        super().__init__(parent)
        self._overlay = mixer
        self.sessions = sessions
        self._session = session
        self.config = config
        self._toast = toast
        self._names = names
        self._audio_changed = audio_changed
        self._tiles = tiles
        self._row = MAIN_ROW
        self._pip_level: Optional[int] = None  # the small picture's changed level, to remember
        self._main_level: Optional[int] = None  # the large picture's, to remember
        self._main_muted = False  # without a SessionManager: the player's own mute

    def wire(self) -> None:
        sessions, overlay = self.sessions, self._overlay
        if sessions is not None:  # the remembered levels
            main = getattr(self.config, "volume", MAIN_LEVEL)
            sessions.set_role_levels(
                main=int(main), pip=int(getattr(self.config, "pip_volume", PIP_LEVEL))
            )
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
            self._toast(_volume_text(level))
        self._show(row)

    def mute(self) -> None:
        """M: mute or unmute the selected row while the mixer shows, else Main."""
        row = self._row if self.visible() else MAIN_ROW
        if self.sessions is None and self._overlay is None:
            session = self._session()
            muted = session.toggle_mute()
            self._main_muted = bool(muted)
            self._audio_changed()
            # i18n: a toast: the sound is off (adjective)
            self._toast(tr("Muted") if muted else _volume_text(session.volume()))
            return
        muted = not self._muted(row)
        self._set_muted(row, muted)
        if self._overlay is None:
            self._toast(tr("Muted") if muted else _volume_text(self._level(row)))
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
            self._toast(tr("Muted") if muted else _volume_text(self._level(MAIN_ROW)))
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
        """Remember the main and the PiP's levels (config volume, pip_volume) once changed."""
        sessions = self.sessions
        main, self._main_level = self._main_level, None
        pip, self._pip_level = self._pip_level, None
        if sessions is None or (main is None and pip is None):
            return
        if main is not None:
            self.config.volume = main
        if pip is not None:
            self.config.pip_volume = pip
        try:
            self.config.save_config()
        except Exception as e:
            logger.warning("Couldn't save the volume: %s", safe_message(e))

    # ---- the overlay --------------------------------------------------------------------------

    def _show(self, row: int) -> None:
        overlay = self._overlay
        self._audio_changed()
        if overlay is None:
            return
        shown = self._rows()
        self._row = max(0, min(row, len(shown) - 1))
        rows = [
            MixerRow(title, channel, self._level(index), self._muted(index))
            for index, (_slot, title, channel) in enumerate(shown)
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

    def _rows(self) -> list[tuple[Optional[Slot], str, str]]:
        """Each row's slot, title and channel: the large picture's, then the small one's (the
        multiview's tiles while its grid shows; no slot without a SessionManager)."""
        sessions = self.sessions
        names = self._names()
        if sessions is None:
            return [(None, translate(MAIN_TITLE), names[0])]
        tiles = self._tiles() if self._tiles is not None else None
        if tiles:
            return [(sessions.slot(name), title, channel) for name, title, channel in tiles]
        small = sessions.small()
        slots = [sessions.large()] if small is None else [sessions.large(), small]
        return [
            (slot, translate(title), names[index])
            for index, (slot, title) in enumerate(zip(slots, (MAIN_TITLE, PIP_TITLE)))
        ]

    def _slots(self) -> list[Optional[Slot]]:
        """The rows' slots: the large picture's first (None without a SessionManager)."""
        return [slot for slot, _title, _channel in self._rows()]

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
        sessions = self.sessions
        sessions.set_level(slot.name, level)
        if slot is sessions.large():
            self._main_level = level  # volume: the large picture's level, as set now
        elif slot is sessions.small() and not sessions.grid():
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
