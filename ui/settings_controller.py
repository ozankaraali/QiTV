"""Binds the Settings layer of the drawer to the config and to the key actions.

A step is applied at once (no Save): the row's next choice is written through the values and
the overlay paints again. A section whose settings still live in the Settings window opens
that window on its tab. The controller keeps the key actions it uses and hands every other
one back, so volume, mute and fullscreen still reach the player.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
import logging
from typing import Optional

from PySide6.QtCore import QObject

from services.i18n import translate
from services.settings_model import LEGACY_TABS, Row, Section, choice_of, is_lit, sections, step
from services.settings_values import SettingsValues
from ui.keymap import Action
from ui.overlays.settings_drawer import LEGACY_PREFIX, SettingsDrawer

logger = logging.getLogger(__name__)


def _walk(rows: tuple[Row, ...]) -> Iterator[Row]:
    """Each row, then the rows of the layer it opens, and so on."""
    for row in rows:
        yield row
        if row.section is not None:
            yield from _walk(row.section.rows)


class SettingsController(QObject):
    def __init__(
        self,
        drawer: SettingsDrawer,
        values: SettingsValues,
        system: str,
        open_legacy: Callable[[str], None],
        reopen_drawer: Callable[[], None],
    ) -> None:
        super().__init__(drawer)
        self._drawer = drawer
        self._values = values
        self._system = system
        self._open_legacy = open_legacy
        self._reopen_drawer = reopen_drawer
        self._from_drawer = False
        self._sections: tuple[Section, ...] = ()
        drawer.stepped.connect(self._on_stepped)
        drawer.chosen.connect(self._on_chosen)
        drawer.closed.connect(self._on_closed)

    # ---- what the overlay shows -------------------------------------------------------------

    def value_text(self, row: Row) -> str:
        """The translated text of the row's current choice ("" for a row that only opens or
        acts: it has no config key to ask)."""
        if not row.choices:
            return ""
        return translate(choice_of(row, self._values.get(row.key)).text)

    def lit(self, row: Row) -> bool:
        """Whether the row's value differs from its default."""
        if not row.choices:
            return False
        return is_lit(row, self._values.get(row.key))

    # ---- opening and closing ----------------------------------------------------------------

    def open(self, section_key: str = "", from_drawer: bool = False) -> None:
        """Show Settings at `section_key` (else the first section). `from_drawer`: backing out
        of the sections gives the TV drawer back."""
        self._from_drawer = from_drawer
        self._sections = sections(self._system)
        self._drawer.open_at(self._sections, section_key)

    def visible(self) -> bool:
        return self._drawer.isVisible()

    def close(self) -> None:
        """Hide Settings; the TV drawer is not reopened."""
        self._from_drawer = False
        self._drawer.hide()

    def handle(self, action: Action, payload: object = None) -> bool:
        """Do a key action on the overlay; False for one it does not use."""
        drawer = self._drawer
        if action is Action.NAV_UP:
            drawer.move_by(-1)
        elif action is Action.NAV_DOWN:
            drawer.move_by(1)
        elif action is Action.NAV_PAGE_UP:
            drawer.page(-1)
        elif action is Action.NAV_PAGE_DOWN:
            drawer.page(1)
        elif action is Action.NAV_LEFT:
            drawer.left()
        elif action is Action.NAV_RIGHT:
            drawer.right()
        elif action in (Action.NAV_ACTIVATE, Action.OK):
            drawer.activate()
        elif action in (Action.ESCAPE, Action.NAV_BACKSPACE):
            drawer.back()
        else:
            return False
        return True

    # ---- the overlay's signals --------------------------------------------------------------

    def _find(self, key: str) -> Optional[Row]:
        for section in self._sections:
            for row in _walk(section.rows):
                if row.key == key:
                    return row
        return None

    def _on_stepped(self, key: str, delta: int) -> None:
        row = self._find(key)
        if row is None or not row.choices:
            logger.warning("A step for %r, which is no stepper row", key)
            return
        self._values.set(key, step(row, self._values.get(key), delta))
        self._drawer.refresh()

    def _on_chosen(self, key: str) -> None:
        if not key.startswith(LEGACY_PREFIX):
            return
        tab = LEGACY_TABS.get(key[len(LEGACY_PREFIX) :])
        if tab is None:
            return
        self.close()
        self._open_legacy(tab)

    def _on_closed(self) -> None:
        from_drawer, self._from_drawer = self._from_drawer, False
        if from_drawer:
            self._reopen_drawer()
