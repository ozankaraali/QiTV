"""Free channels and Free radio (2.13) as layers of Settings › Providers (2.13.1: no menu over the
picture, no system dialog): iptv-org's free playlists, and radio-browser.info's lists of
stations, for all countries, the system's region or any one country.

Each layer lists All countries, "<country> (your region)" when the system's region has a list,
then every country, named in the active language and in its alphabetical order (English keeps
the list's own). Typing finds a country there: a find typed inside the layer lists its rows
first, and only there are they looked through (ui/overlays/settings_find.py). The welcome
card's buttons, the channel list's rows, the Add card's other ways and the menu bar's Another
country… open Settings at the layer, the keys on All countries (`FreeLists.open`); Settings ›
Providers' rows open it as any layer.

A choice is the window's `add_free_channels` (`add_free_radio`), None for all countries: the
list is added (only switched to when it is there already), selected and loaded. In Settings
opened for the layer, Settings then goes as backing out of it would (from the channel list, the
list comes back where it was); browsed to, Providers shows, the list's row focused. The menu
bar's Providers menu (the system's own) offers All countries and the region at once
(`quick_choices`), then Another country….
"""

from __future__ import annotations

from functools import cmp_to_key
import logging
from typing import Any, Optional, Sequence

from PySide6.QtCore import QCollator, QLocale, QObject

from services import free_channels, free_radio, i18n
from services.i18n import N_, tr, translate
from services.settings_model import ACTION, FREE_CHANNELS, FREE_RADIO, PROVIDERS, Row
from ui.settings_controller import SettingsController
from ui.settings_providers import layer_key

logger = logging.getLogger(__name__)

# English (tests compare them); translate() shows them.
# i18n: the first row of Free channels' and Free radio's countries, and a menu entry: every one
ALL_TEXT = N_("All countries")
# i18n: the menu bar's Free channels and Free radio entry; opens their countries in Settings
OTHER_TEXT = N_("Another country…")
# i18n: the menu bar's entry, and Settings' layer of countries (its name in the breadcrumb)
PICKER_TITLE = N_("Free channels")
# i18n: the menu bar's entry (2.13), and Settings' layer of countries (in its breadcrumb)
RADIO_TITLE = N_("Free radio")
LAYERS = (FREE_CHANNELS, FREE_RADIO)
ALL_KEY = "@all"  # a layer's All countries row: <layer>/@all; a country's is <layer>/<code>
REGION_KEY = "@region-"  # the region's row: <layer>/@region-<code> (its own row stays too)


def system_territory() -> str:
    """The system's ISO 3166 alpha-2 region ("" when it has none)."""
    locale = QLocale.system()
    try:
        return QLocale.territoryToCode(locale.territory()) or ""
    except Exception:  # an unknown territory has no code
        return ""


def shown_countries(codes: Optional[Sequence[str]] = None) -> list[tuple[str, str]]:
    """(iptv-org's code, name) of every country (of `codes`, in their order), named in the
    active language and in its alphabetical order (English keeps the list's own order)."""
    if codes is None:
        codes = [code for code, _name in free_channels.COUNTRIES]
    shown = [(code, free_channels.shown_name(code)) for code in codes]
    if i18n.language() != i18n.SOURCE_LANGUAGE:
        collator = QCollator(i18n.app_locale())

        def by_name(a: tuple[str, str], b: tuple[str, str]) -> int:
            return int(collator.compare(a[1], b[1]))

        shown.sort(key=cmp_to_key(by_name))
    return shown


def layer_codes(layer: str) -> Sequence[str]:
    """The countries a layer lists: Free radio's are iptv-org's but its International."""
    if layer == FREE_RADIO:
        return free_radio.COUNTRY_CODES
    return [code for code, _name in free_channels.COUNTRIES]


def quick_choices(
    codes: Optional[Sequence[str]] = None, territory: Optional[str] = None
) -> list[tuple[str, Optional[str]]]:
    """(text, code) of All countries (None), then of "<country> (your region)" when the
    system's region has a list among `codes` (by default iptv-org's countries), in the active
    language: the menu bar's entries before Another country…, and a layer's first rows.
    `territory` is the region's ISO 3166 code (None: ask the system)."""
    choices: list[tuple[str, Optional[str]]] = [(translate(ALL_TEXT), None)]
    code = free_channels.region_code(system_territory() if territory is None else territory)
    if code is not None and (codes is None or code in codes):
        # i18n: a row and a menu entry: the free list of the system's country, {country}
        text = tr("{country} (your region)", country=free_channels.shown_name(code))
        choices.append((text, code))
    return choices


def layer_rows(layer: str, territory: Optional[str] = None) -> tuple[Row, ...]:
    """The rows of Free channels' layer (FREE_CHANNELS) or Free radio's (FREE_RADIO): All
    countries, the system's region (`territory` as quick_choices'), then every country of the
    layer's, in the active language and its order; each acts (`FreeLists.chosen`)."""
    codes = layer_codes(layer)
    rows = [
        (
            Row(f"{layer}/{ALL_KEY}", ALL_TEXT, kind=ACTION)
            if code is None
            else Row(f"{layer}/{REGION_KEY}{code}", text, kind=ACTION, literal=True)
        )
        for text, code in quick_choices(codes, territory)
    ]
    rows += [
        Row(f"{layer}/{code}", name, kind=ACTION, literal=True)
        for code, name in shown_countries(codes)
    ]
    return tuple(rows)


class FreeLists(QObject):
    """Settings' Free channels and Free radio layers: their rows (`rows`, for Settings'
    context) and the handler of their keys (`<layer>/…`, registered with `settings`).
    `window` is the TV window: its open_settings, add_free_channels and add_free_radio."""

    def __init__(self, window: Any, settings: SettingsController, parent: QObject) -> None:
        super().__init__(parent)
        self._window = window
        self._settings = settings
        self._made: dict[str, tuple[tuple[str, str], tuple[Row, ...]]] = {}  # layer -> rows
        self._alone = ""  # the layer Settings was opened for (from outside it), until it goes
        for layer in LAYERS:
            settings.register(layer + "/", self)
        settings.hidden.connect(self._on_settings_hidden)

    def rows(self, layer: str) -> tuple[Row, ...]:
        """The layer's rows as they are now: made again for another language or region."""
        stamp = (i18n.language(), system_territory())
        made = self._made.get(layer)
        if made is None or made[0] != stamp:
            made = self._made[layer] = (stamp, layer_rows(layer, stamp[1]))
        return made[1]

    def open(self, layer: str, from_drawer: bool = False, opened_settings: bool = False) -> None:
        """Settings at Providers › `layer`, the keys on All countries: from the channel list
        (`from_drawer`: backing out of it gives the list back where it was), the welcome card,
        the menu bar's Another country… and the Add card (`opened_settings`: the card opened
        Settings for itself). Opened for it, Settings goes once a country is chosen."""
        window = self._window
        if window.closing():
            return
        alone = opened_settings or not self._settings.visible()
        window.open_settings(
            from_drawer=from_drawer,
            keep_place=from_drawer,
            section_key=PROVIDERS,
            row=f"{layer}/{ALL_KEY}",
        )
        if self._settings.visible():
            self._alone = layer if alone else ""

    # ---- RowHandler -------------------------------------------------------------------------

    def value(self, row: Row) -> Any:
        return None  # its rows act: no value

    def step(self, row: Row, delta: int) -> None:
        pass

    def edited(self, row: Row, text: str) -> None:
        pass

    def chosen(self, row: Row) -> None:
        """A country chosen (All countries: None): its list added, selected and loaded; then
        Settings opened for the layer goes, else Providers shows with the list focused."""
        layer, _slash, part = row.key.rpartition("/")
        code: Optional[str] = None
        if part != ALL_KEY:
            code = part[len(REGION_KEY) :] if part.startswith(REGION_KEY) else part
        if layer not in LAYERS or (code is not None and code not in layer_codes(layer)):
            logger.warning("A free list's row %r, which lists no country", row.key)
            return
        window = self._window
        if window.closing():
            return
        if layer == FREE_RADIO:
            window.add_free_radio(code)
        else:
            window.add_free_channels(code)
        if self._alone == layer:
            self._settings.back_out()
            return
        self._settings.refresh_sections()  # its row says "in use" now
        self._settings.show_row(layer_key(str(window.config_manager.selected_provider_name)))

    def _on_settings_hidden(self) -> None:
        self._alone = ""
