"""The "Free channels…" menu: iptv-org's free playlists for all countries, the system's region
or a country picked from a list (the welcome card's button and the sources column's row).

The menu only asks; `choose(code)` is the window's `add_free_channels`, with None for all
countries. Cancelling the picker adds nothing.
"""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QLocale, QPoint, QTimer
from PySide6.QtWidgets import QInputDialog, QMenu, QWidget

from services import free_channels
from ui.context_actions import menu_style

ALL_TEXT = "All countries"
OTHER_TEXT = "Another country…"
PICKER_TITLE = "Free channels"
PICKER_LABEL = "Country:"


def system_territory() -> str:
    """The system's ISO 3166 alpha-2 region ("" when it has none)."""
    locale = QLocale.system()
    try:
        return QLocale.territoryToCode(locale.territory()) or ""
    except Exception:  # an unknown territory has no code
        return ""


def pick_country(parent: Optional[QWidget]) -> Optional[str]:
    """A list of every country (typing letters jumps in it): iptv-org's code, None on cancel."""
    names = [name for _code, name in free_channels.COUNTRIES]
    name, accepted = QInputDialog.getItem(
        parent, PICKER_TITLE, PICKER_LABEL, names, 0, False  # not editable
    )
    if not accepted:
        return None
    return next((code for code, n in free_channels.COUNTRIES if n == name), None)


def build_menu(
    parent: Optional[QWidget],
    choose: Callable[[Optional[str]], None],
    *,
    territory: Optional[str] = None,
    picker: Callable[[Optional[QWidget]], Optional[str]] = pick_country,
) -> QMenu:
    """The menu: All countries, "<Region> (your region)" when the system's region has a list,
    and Another country…. `territory` is the system's region code (None: ask the system)."""
    menu = QMenu(parent)
    menu.setStyleSheet(menu_style())
    menu.addAction(ALL_TEXT).triggered.connect(lambda _checked=False: choose(None))
    code = free_channels.region_code(system_territory() if territory is None else territory)
    if code is not None:
        text = f"{free_channels.country_name(code)} (your region)"
        menu.addAction(text).triggered.connect(lambda _checked=False: choose(code))

    def other() -> None:
        picked = picker(parent)
        if picked is not None:
            choose(picked)

    # After the menu has closed: the dialog is modal and must not open under a popup.
    menu.addAction(OTHER_TEXT).triggered.connect(lambda _checked=False: QTimer.singleShot(0, other))
    return menu


def show_menu(
    parent: Optional[QWidget], global_pos: QPoint, choose: Callable[[Optional[str]], None]
) -> QMenu:
    """Pop the menu up at `global_pos`; it is deleted once closed."""
    menu = build_menu(parent, choose)
    menu.aboutToHide.connect(menu.deleteLater)
    menu.popup(global_pos)
    return menu
