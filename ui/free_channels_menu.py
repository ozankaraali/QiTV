"""The "Free channels…" menu: iptv-org's free playlists for all countries, the system's region
or a country picked from a list (the welcome card's button and the sources column's row).

The menu only asks; `choose(code)` is the window's `add_free_channels`, with None for all
countries. Cancelling the picker adds nothing.
"""

from __future__ import annotations

from functools import cmp_to_key
from typing import Callable, Optional

from PySide6.QtCore import QCollator, QLocale, QPoint, QTimer
from PySide6.QtWidgets import QInputDialog, QMenu, QWidget

from services import free_channels, i18n
from services.i18n import N_, tr, translate
from ui.context_actions import menu_style

# English (tests compare them); translate() shows them.
ALL_TEXT = N_("All countries")  # i18n: a menu entry: the free channels of every country
OTHER_TEXT = N_("Another country…")  # i18n: a menu entry; opens a list of countries
PICKER_TITLE = N_("Free channels")  # i18n: the country list's window title
PICKER_LABEL = N_("Country:")


def system_territory() -> str:
    """The system's ISO 3166 alpha-2 region ("" when it has none)."""
    locale = QLocale.system()
    try:
        return QLocale.territoryToCode(locale.territory()) or ""
    except Exception:  # an unknown territory has no code
        return ""


def shown_countries() -> list[tuple[str, str]]:
    """(iptv-org's code, name) of every country, named in the active language and in its
    alphabetical order (English keeps the list's own order)."""
    shown = [(code, free_channels.shown_name(code)) for code, _name in free_channels.COUNTRIES]
    if i18n.language() != i18n.SOURCE_LANGUAGE:
        collator = QCollator(i18n.app_locale())

        def by_name(a: tuple[str, str], b: tuple[str, str]) -> int:
            return int(collator.compare(a[1], b[1]))

        shown.sort(key=cmp_to_key(by_name))
    return shown


def pick_country(parent: Optional[QWidget]) -> Optional[str]:
    """A list of every country (typing letters jumps in it): iptv-org's code, None on cancel."""
    countries = shown_countries()
    names = [name for _code, name in countries]
    name, accepted = QInputDialog.getItem(
        parent, translate(PICKER_TITLE), translate(PICKER_LABEL), names, 0, False  # not editable
    )
    if not accepted:
        return None
    return next((code for code, n in countries if n == name), None)


def build_menu(
    parent: Optional[QWidget],
    choose: Callable[[Optional[str]], None],
    *,
    territory: Optional[str] = None,
    picker: Callable[[Optional[QWidget]], Optional[str]] = pick_country,
    styled: bool = True,
) -> QMenu:
    """The menu: All countries, "<Region> (your region)" when the system's region has a list,
    and Another country…. `territory` is the system's region code (None: ask the system).
    `styled`: the player's dark menus' look (not inside the menu bar, which has its own)."""
    menu = QMenu(parent)
    if styled:
        menu.setStyleSheet(menu_style())
    menu.addAction(translate(ALL_TEXT)).triggered.connect(lambda _checked=False: choose(None))
    code = free_channels.region_code(system_territory() if territory is None else territory)
    if code is not None:
        # i18n: a menu entry: the free channels of the system's country, {country}
        text = tr("{country} (your region)", country=free_channels.shown_name(code))
        menu.addAction(text).triggered.connect(lambda _checked=False: choose(code))

    def other() -> None:
        picked = picker(parent)
        if picked is not None:
            choose(picked)

    # After the menu has closed: the dialog is modal and must not open under a popup.
    menu.addAction(translate(OTHER_TEXT)).triggered.connect(
        lambda _checked=False: QTimer.singleShot(0, other)
    )
    return menu


def show_menu(
    parent: Optional[QWidget], global_pos: QPoint, choose: Callable[[Optional[str]], None]
) -> QMenu:
    """Pop the menu up at `global_pos`; it is deleted once closed."""
    menu = build_menu(parent, choose)
    menu.aboutToHide.connect(menu.deleteLater)
    menu.popup(global_pos)
    return menu
