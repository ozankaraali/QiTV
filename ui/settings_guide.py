"""Settings › Guide in the drawer (plan 3): the old Settings window's EPG tab, each change
applied at once.

- Guide source steps through no guide, the provider's own, a local file and a web address (the
  config's "No Source", "STB", "Local File" and "URL"). The rows follow a step at once, as the
  old tab's did, and the guide loads again in the background (TVWindow.reload_epg) once the
  steps stop (passing through the sources loads it once; steps back to the source they began
  on, never): the address for a web address (an edit loads it again), the file for a local
  file (the system's picker, then a load), how often it is checked for a web address and the
  provider's guide, the channel
  matching for the XMLTV guides (a file, a web address), and the portal's fetch period for the
  provider's guide while a portal is among the providers;
- Check for updates every opens a card: a number (digits only, 1 to 9999) and its unit, a
  button that steps through Hours, Days, Weeks and Months (an older QiTV's Minutes stays one of
  them while it is set). It loads nothing: the period is read when the guide is next looked at;
- List window and the portal's fetch period step through hours (one the old window took that is
  none of the steps shows as itself), read where they are used;
- Channel matching › lists the config's XMLTV channel map: a row per channel (its name, its IDs
  under it), each opening a card to change or delete it; then ＋ Add, Import… and Export… (a JSON
  file as the old window's, through the system's pickers; the file is read and written on a
  worker). A change re-keys the guide shown, as the old Save did (EpgManager.reindex_programs,
  in memory, documented for the GUI thread), but for the provider's own guide, which is no
  XMLTV guide. The map is replaced, never changed in place: a guide loading meanwhile reads it.

Keys: the config's for its settings (epg_source, epg_url, epg_list_window_hours,
epg_stb_period_hours: through the values, with `hooks`); `guide/…` for the rest (GuideRows').
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QFileDialog, QWidget
import orjson

from services.config_manager import ConfigManager
from services.i18n import N_, number, tr, tr_n, translate
from services.multikeydict import MultiKeyDict
from services.settings_model import ACTION, OPENS, TEXT, Choice, Row, Section
from ui.overlays.settings_card import (
    CANCEL,
    CANCEL_TEXT,
    Button,
    Field,
    FormCard,
    pick_file,
)
from ui.settings_controller import SettingsController, Settled
from ui.tv_controller import ERROR_MS

logger = logging.getLogger(__name__)

GUIDE_PREFIX = "guide/"
FILE = GUIDE_PREFIX + "file"
EVERY = GUIDE_PREFIX + "every"
MATCHING = GUIDE_PREFIX + "matching"
MAP_PREFIX = MATCHING + "/"  # + the entry's place in the map, or one of the list's own rows
ADD = MAP_PREFIX + "@add"
IMPORT = MAP_PREFIX + "@import"
EXPORT = MAP_PREFIX + "@export"

# The guide's sources, as the config has always kept them.
NO_SOURCE = "No Source"
STB = "STB"
LOCAL_FILE = "Local File"
URL = "URL"

# The cards' fields and buttons.
NUMBER = "number"
UNIT = "unit"
SAVE = "save"
DELETE = "delete"
NAME = "name"
ICON = "icon"
IDS = "ids"
EVERY_MOST = 9999  # as the old window's spinner
_COLONS = " :：  "  # after the old window's labels ("Logo URL:")

# 2.9.3: the EPG Source setting's choice "STB" (its value in the config): the guide of the
# provider QiTV is on (an STB portal's or an Xtream panel's).
# i18n: the EPG Source setting's choice: the guide the provider itself gives
PROVIDER_GUIDE_TEXT = N_("Provider's own guide")
SOURCES = (
    Choice(NO_SOURCE, N_("No Source")),  # i18n: the guide source setting's choice: no guide
    Choice(STB, PROVIDER_GUIDE_TEXT),
    Choice(LOCAL_FILE, N_("Local File")),  # i18n: the guide source setting's choice
    Choice(URL, N_("URL")),  # i18n: the guide source setting's choice: a web address
)
WINDOW_HOURS = (0, 6, 12, 24, 48, 72, 168)
PERIOD_HOURS = (1, 2, 3, 5, 8, 12, 24, 48, 168)
# i18n: Settings → Guide → List window's choice: every programme the guide has
UNLIMITED_TEXT = N_("Unlimited")
FILE_TEXT = N_("Guide file")  # i18n: a row of Settings → Guide; opens a file picker
EVERY_TEXT = N_("Check for updates every")  # i18n: a row of Settings → Guide; opens a card
# i18n: the number's label in the card "Check for updates every [2] [Hours]"
EVERY_FIELD_TEXT = N_("Every")
# i18n: said in the card "Check for updates every" when its number is none it takes
RANGE_TEXT = N_("A whole number from 1 to 9999.")
SAVE_TEXT = N_("Save")  # i18n: a button (verb)
DELETE_TEXT = N_("Delete")  # i18n: a button (verb): the channel's match goes
MATCHING_TEXT = N_("Channel matching")  # i18n: a row of Settings → Guide; opens its list
# i18n: the fields of a channel's card in Settings → Guide → Channel matching
CHANNEL_NAME_TEXT = N_("Channel Name:")
LOGO_URL_TEXT = N_("Logo URL:")  # i18n: a field of a channel's card (Channel matching)
# i18n: a field of a channel's card (Channel matching): the XMLTV guide's IDs of the channel
CHANNEL_IDS_TEXT = N_("Channel IDs")
# i18n: what the empty Channel IDs field shows (Settings → Guide → Channel matching)
IDS_PLACEHOLDER = N_("Separated by commas")
# i18n: said in a channel's card (Channel matching) when its name or its IDs are missing
REQUIRED_TEXT = N_("Channel Name and Channel IDs are required.")
ADD_TEXT = N_("Add")  # i18n: a row of Settings → Guide → Channel matching, after "＋ " (verb)
# i18n: rows of Settings → Guide → Channel matching: a JSON file read in, or written (verbs)
IMPORT_TEXT = N_("Import…")
EXPORT_TEXT = N_("Export…")  # i18n: a row of Settings → Guide → Channel matching (verb)
# i18n: a toast: Settings → Guide → Channel matching → Import… read the file in
IMPORTED_TEXT = N_("Channel matching imported")
# i18n: a toast: Settings → Guide → Channel matching → Export… wrote the file
EXPORTED_TEXT = N_("Channel matching exported")
# i18n: a toast: Settings → Guide → Channel matching → Import… could not read the file
IMPORT_FAILED_TEXT = N_("That file holds no channel matching QiTV can read")
# i18n: a toast: Settings → Guide → Channel matching → Export… could not write the file
EXPORT_FAILED_TEXT = N_("Couldn't write the channel matching to that file")


def epg_unit_label(unit: str) -> str:
    """The guide's update period unit (a config value, "Hours") as Settings shows it."""
    labels = {
        # i18n: the unit of the guide's update period: "Check update every [2] [Hours]"
        "Minutes": tr("Minutes"),
        "Hours": tr("Hours"),  # i18n: the unit of "Check update every [2] [Hours]"
        "Days": tr("Days"),  # i18n: the unit of "Check update every [2] [Days]"
        "Weeks": tr("Weeks"),  # i18n: the unit of "Check update every [2] [Weeks]"
        "Months": tr("Months"),  # i18n: the unit of "Check update every [2] [Months]"
    }
    return labels.get(unit, unit)


def every_text(count: int, unit: str) -> str:
    """The guide's update period as its row says it: "2 hours", "30 minutes"."""
    n = number(count)
    if unit == "Minutes":
        return tr_n("{n} minute", "{n} minutes", count, n=n)  # i18n: a period of minutes
    if unit == "Hours":
        return tr_n("{n} hour", "{n} hours", count, n=n)  # i18n: a period of hours
    if unit == "Days":
        return tr_n("{n} day", "{n} days", count, n=n)  # i18n: the guide's update period
    if unit == "Weeks":
        return tr_n("{n} week", "{n} weeks", count, n=n)  # i18n: the guide's update period
    if unit == "Months":
        return tr_n("{n} month", "{n} months", count, n=n)  # i18n: the guide's update period
    return f"{n} {unit}"


def _hours_text(hours: int) -> str:
    return tr_n("{n} hour", "{n} hours", hours, n=number(hours))  # i18n: a period of hours


def _hours(steps: tuple[int, ...], current: Any) -> tuple[Choice, ...]:
    """A stepper's hours, and a number set that is none of them (the old window's spinners
    took any) in its place among them; 0 is the list window's Unlimited."""
    values = list(steps)
    if isinstance(current, int) and not isinstance(current, bool) and current not in values:
        values = sorted(values + [current])
    return tuple(
        Choice(h, translate(UNLIMITED_TEXT) if h == 0 else _hours_text(h), literal=True)
        for h in values
    )


def _count(value: Any) -> int:
    """The update period's number as set (a hand-edited one that is none: the default)."""
    try:
        return int(value)
    except TypeError, ValueError:
        return ConfigManager.DEFAULT_OPTION_EPG_EXPIRATION_VALUE


def _label(text: str) -> str:
    """One of the old window's labels without its colon ("Logo URL:" → "Logo URL")."""
    return translate(text).rstrip(_COLONS)


def _digits(text: str) -> str:
    return "".join(c for c in text if c.isdecimal())


def pick_save_file(parent: Optional[QWidget], title: str, filters: str) -> Optional[str]:
    """The system's picker for a file to write; the file named, or None. Tests patch this
    one place."""
    path, _selected = QFileDialog.getSaveFileName(parent, title, "", filters)
    return path or None


# ---- the file, on a worker ------------------------------------------------------------------


def read_mapping(path: str) -> MultiKeyDict:
    """The channel matching an exported file holds (the old window's format: each channel by
    its first ID, its IDs as "xmltv_id"); an entry without IDs is left out. ValueError for a
    file of another shape, OSError for one that can't be read."""
    data = orjson.loads(Path(path).read_bytes())
    if not isinstance(data, dict):
        raise ValueError("no channel matching")
    mapping = MultiKeyDict()
    for value in data.values():
        if not isinstance(value, dict):
            continue
        ids = value.get("xmltv_id")
        if isinstance(ids, str):
            ids = [ids]
        if not isinstance(ids, list) or not ids:
            continue
        mapping[tuple(str(i) for i in ids)] = {k: v for k, v in value.items() if k != "xmltv_id"}
    return mapping


def write_mapping(path: str, export: dict) -> None:
    """`export` (the old window's format) written to `path` as indented JSON."""
    Path(path).write_bytes(orjson.dumps(export, option=orjson.OPT_INDENT_2))


def _export_of(mapping: MultiKeyDict) -> dict:
    """The old window's export of the map, copied (the map's own entries stay as they are)."""
    return {
        str(ids[0]).strip(): {**value, "xmltv_id": list(ids)}
        for ids, value in mapping.items()
        if ids
    }


class ImportMapping(QObject):
    """Import…'s reading, run on a JobRunner thread: the map the file holds."""

    finished = Signal(object)
    error = Signal(str)

    def __init__(self, path: str) -> None:
        super().__init__()
        self._path = path

    def run(self) -> None:
        try:
            mapping = read_mapping(self._path)
        except (OSError, ValueError) as e:  # (orjson's decode error is a ValueError)
            logger.warning("Couldn't import the channel matching: %s", type(e).__name__)
            self.error.emit(IMPORT_FAILED_TEXT)
            return
        self.finished.emit(mapping)


class ExportMapping(QObject):
    """Export…'s writing, run on a JobRunner thread."""

    finished = Signal(object)
    error = Signal(str)

    def __init__(self, path: str, export: dict) -> None:
        super().__init__()
        self._path = path
        self._export = export

    def run(self) -> None:
        try:
            write_mapping(self._path, self._export)
        except (OSError, TypeError) as e:
            logger.warning("Couldn't export the channel matching: %s", type(e).__name__)
            self.error.emit(EXPORT_FAILED_TEXT)
            return
        self.finished.emit(None)


# ---- the rows -------------------------------------------------------------------------------


def hooks(window: Any) -> dict[str, Callable[[], None]]:
    """The hooks of the Guide's config rows (SettingsValues'): a new source's rows follow it at
    once, and the guide loads again in the background once the steps stop (Settled: passing
    through the sources loads once; steps back to the source they began on, none); a new
    address loads it again. `window` is the TV window, read as a hook runs."""
    # In the background; the guide repaints when ready.
    config = window.config_manager
    reload = Settled(
        lambda: window.reload_epg(),
        lambda: window.closing(),
        window,
        value=lambda: config.epg_source,
    )

    def source() -> None:
        reload.start()
        window.settings.refresh_sections()  # its address, file and matching rows follow

    def address() -> None:
        window.reload_epg()

    return {"epg_source": source, "epg_url": address}


class GuideRows(QObject):
    """What the Guide section lists (`rows`) and the handler of its own rows (`guide/…`,
    registered with `settings`): the guide file, the update period's card, the channel
    matching. `window` is the TV window; `drawer` the Settings layer the rows show in."""

    def __init__(self, window: Any, settings: SettingsController, drawer: Any) -> None:
        super().__init__(drawer)
        self._window = window
        self._settings = settings
        self._drawer = drawer
        self._every: Optional[FormCard] = None
        self._mapping: Optional[FormCard] = None
        self._units: list[str] = []  # the update period's card: the units it steps through
        self._unit = ""  # and the one it is on
        self._editing: Optional[tuple[str, ...]] = None  # the IDs the channel's card changes
        self._importing = False
        self._exporting = False
        settings.register(GUIDE_PREFIX, self)

    @property
    def every_card(self) -> Optional[FormCard]:
        """The update period's card (None until it first showed)."""
        return self._every

    @property
    def mapping_card(self) -> Optional[FormCard]:
        """A channel's card in Channel matching (None until it first showed)."""
        return self._mapping

    # ---- what the section shows -------------------------------------------------------------

    def rows(self) -> tuple[Row, ...]:
        """The Guide's rows for its source as it is now (the old tab's which-shows-when)."""
        config = self._window.config_manager
        source = config.epg_source
        rows = [
            Row(
                "epg_source",
                N_("Guide source"),  # i18n: a row of Settings → Guide
                # i18n: help under Settings → Guide → Guide source
                N_("Where the guide's programmes come from. A change loads the guide again."),
                SOURCES,
                ConfigManager.DEFAULT_OPTION_EPG_SOURCE,
            )
        ]
        if source == URL:
            rows.append(
                Row(
                    "epg_url",
                    N_("Guide address"),  # i18n: a row of Settings → Guide, edited in place
                    # i18n: help under Settings → Guide → Guide address (XMLTV: a guide's format)
                    N_("The web address of an XMLTV guide. A new one loads the guide again."),
                    kind=TEXT,
                )
            )
        if source == LOCAL_FILE:
            rows.append(
                Row(
                    FILE,
                    FILE_TEXT,
                    # i18n: help under Settings → Guide → Guide file (XMLTV: a guide's format)
                    N_("The XMLTV file the guide is read from. Another one loads the guide again."),
                    kind=ACTION,
                    note=str(config.epg_file or ""),
                )
            )
        if source in (URL, STB):
            rows.append(
                Row(
                    EVERY,
                    EVERY_TEXT,
                    # i18n: help under Settings → Guide → Check for updates every
                    N_("How long QiTV keeps the guide before it loads it again."),
                    kind=ACTION,
                    note=every_text(
                        _count(config.epg_expiration_value), config.epg_expiration_unit
                    ),
                )
            )
        rows.append(
            Row(
                "epg_list_window_hours",
                N_("List window"),  # i18n: a row of Settings → Guide: how far ahead it lists
                # i18n: help under Settings → Guide → List window
                N_("How far ahead a channel's programmes are listed."),
                _hours(WINDOW_HOURS, config.epg_list_window_hours),
                ConfigManager.DEFAULT_OPTION_EPG_LIST_WINDOW_HOURS,
            )
        )
        if source == STB and self._lists_a_portal():  # 2.9.3: only a portal's guide has it
            rows.append(
                Row(
                    "epg_stb_period_hours",
                    N_("Portal guide fetch period"),  # i18n: a row of Settings → Guide
                    # i18n: help under Settings → Guide → Portal guide fetch period
                    N_("How many hours of programmes a portal is asked for at a time."),
                    _hours(PERIOD_HOURS, config.epg_stb_period_hours),
                    ConfigManager.DEFAULT_OPTION_EPG_STB_PERIOD_HOURS,
                )
            )
        if source in (URL, LOCAL_FILE):
            rows.append(self._matching_row(config.xmltv_channel_map))
        return tuple(rows)

    def _lists_a_portal(self) -> bool:
        """Whether an STB portal is among the providers."""
        providers = getattr(self._window.provider_manager, "providers", None) or []
        return any(str(p.get("type", "")).upper() == "STB" for p in providers)

    def _matching_row(self, mapping: Any) -> Row:
        """Channel matching ›: a row per channel, in the map's order; then the list's own."""
        entries = self._entries(mapping)
        rows = [
            Row(
                f"{MAP_PREFIX}{index}",
                str(value.get("name") or "") or str(ids[0]),
                # i18n: help under a channel in Settings → Guide → Channel matching
                N_("Its name, logo and guide IDs, changed or deleted in a card."),
                kind=OPENS,
                sub=", ".join(str(i) for i in ids),
                literal=True,
            )
            for index, (ids, value) in enumerate(entries)
        ]
        rows += [
            Row(
                ADD,
                "＋ " + translate(ADD_TEXT),
                # i18n: help under Settings → Guide → Channel matching → Add
                N_("Matches a channel to the guide's IDs for it."),
                kind=ACTION,
                literal=True,
            ),
            Row(
                IMPORT,
                IMPORT_TEXT,
                # i18n: help under Settings → Guide → Channel matching → Import…
                N_("Puts the matching of a JSON file in place of this list."),
                kind=ACTION,
            ),
            Row(
                EXPORT,
                EXPORT_TEXT,
                # i18n: help under Settings → Guide → Channel matching → Export…
                N_("Writes this list to a JSON file."),
                kind=ACTION,
            ),
        ]
        layer = Section(MATCHING, MATCHING_TEXT, rows=tuple(rows))
        return Row(
            MATCHING,
            MATCHING_TEXT,
            # i18n: help under Settings → Guide → Channel matching (XMLTV: a guide's format)
            N_("Which of the XMLTV guide's channels each of your channels shows."),
            section=layer,
            note=number(len(entries)) if entries else "",
        )

    @staticmethod
    def _entries(mapping: Any) -> list[tuple[tuple[str, ...], dict]]:
        """The map's entries that have IDs, in its order (a value that is no dict: none)."""
        if not isinstance(mapping, MultiKeyDict):
            return []
        return [
            (ids, value if isinstance(value, dict) else {}) for ids, value in mapping.items() if ids
        ]

    # ---- RowHandler -------------------------------------------------------------------------

    def value(self, row: Row) -> Any:
        return None  # they act or open: no value

    def step(self, row: Row, delta: int) -> None:
        pass

    def edited(self, row: Row, text: str) -> None:
        pass

    def chosen(self, row: Row) -> None:
        key = row.key
        if key == FILE:
            self._pick_file()
        elif key == EVERY:
            self._open_every()
        elif key == ADD:
            self._open_mapping(None)
        elif key == IMPORT:
            self._import()
        elif key == EXPORT:
            self._export()
        elif key.startswith(MAP_PREFIX) and key[len(MAP_PREFIX) :].isdigit():
            self._open_mapping(int(key[len(MAP_PREFIX) :]))

    # ---- the guide file ---------------------------------------------------------------------

    def _pick_file(self) -> None:
        """Guide file: the system's picker (as the old window's Browse), then the guide loads
        again."""
        path = pick_file(self._drawer.window(), translate(FILE_TEXT), "")
        if not path or self._window.closing():  # (the window may close while it asks)
            return
        self._window.config_manager.epg_file = path
        self._save()
        self._window.reload_epg()
        self._settings.refresh_sections()

    # ---- the update period ------------------------------------------------------------------

    def _open_every(self) -> None:
        config = self._window.config_manager
        unit = config.epg_expiration_unit
        units = list(ConfigManager.EPG_EXPIRATION_CHOICES)
        if unit not in units and unit in ConfigManager.EPG_EXPIRATION_SECONDS:
            units.insert(0, unit)  # "Minutes" from an older version stays as it was
        self._units = units
        self._unit = unit if unit in units else ConfigManager.DEFAULT_OPTION_EPG_EXPIRATION_UNIT
        if self._every is None:
            self._every = FormCard(self._drawer.parentWidget())
            self._every.pressed.connect(self._on_every_pressed)
            self._every.changed.connect(self._on_every_changed)
        self._render_every(str(_count(config.epg_expiration_value)))

    def _render_every(self, text: str) -> None:
        """The card with `text` in its number field (set again while it shows: the focus and
        the caret stay); under it, the period it makes."""
        card = self._every
        if card is None:
            return
        fresh = card.isHidden()
        count = int(text) if text.isdecimal() else 0
        found = every_text(count, self._unit) if 1 <= count <= EVERY_MOST else ""
        card.set_form(
            translate(EVERY_TEXT),
            [Field(NUMBER, translate(EVERY_FIELD_TEXT), text)],
            found,
            [
                Button(UNIT, epg_unit_label(self._unit)),  # a press: the next unit
                Button(CANCEL, translate(CANCEL_TEXT)),
                Button(SAVE, translate(SAVE_TEXT), accent=True),
            ],
            [],
        )
        if fresh:
            self._settings.show_card(card)

    def _on_every_changed(self, key: str, text: str) -> None:
        if key == NUMBER:
            self._render_every(_digits(text))  # digits only

    def _on_every_pressed(self, key: str) -> None:
        card = self._every
        if card is None:
            return
        text = card.values().get(NUMBER, "")
        if key == UNIT and self._units:
            at = self._units.index(self._unit) if self._unit in self._units else -1
            self._unit = self._units[(at + 1) % len(self._units)]
            self._render_every(text)
        elif key == SAVE:
            if not text.isdecimal() or not 1 <= int(text) <= EVERY_MOST:
                card.set_found(translate(RANGE_TEXT), False)
                return
            config = self._window.config_manager
            config.epg_expiration_value = int(text)
            config.epg_expiration_unit = self._unit  # (an old "Monthes" is kept as "Months")
            self._save()
            card.hide()
            self._settings.refresh_sections()

    # ---- the channel matching ---------------------------------------------------------------

    def _open_mapping(self, index: Optional[int]) -> None:
        """A channel's card: the one at `index` in the map to change or delete, or a new one."""
        entries = self._entries(self._window.config_manager.xmltv_channel_map)
        if index is not None and not 0 <= index < len(entries):
            return  # gone meanwhile
        if self._mapping is None:
            self._mapping = FormCard(self._drawer.parentWidget())
            self._mapping.pressed.connect(self._on_mapping_pressed)
        buttons = [
            Button(CANCEL, translate(CANCEL_TEXT)),
            Button(SAVE, translate(SAVE_TEXT), accent=True),
        ]
        if index is None:
            self._editing = None
            title, name, icon, ids = translate(MATCHING_TEXT), "", "", ""
        else:
            keys, value = entries[index]
            self._editing = keys
            name = str(value.get("name") or "")
            icon = str(value.get("icon") or "")
            ids = ", ".join(str(i) for i in keys)
            title = name or ids
            buttons.insert(0, Button(DELETE, translate(DELETE_TEXT), danger=True))
        card = self._mapping
        if not card.isHidden():
            card.hide()  # filled anew
        card.set_form(
            title,
            [
                Field(NAME, _label(CHANNEL_NAME_TEXT), name),
                Field(ICON, _label(LOGO_URL_TEXT), icon, paste=True),
                Field(
                    IDS, translate(CHANNEL_IDS_TEXT), ids, placeholder=translate(IDS_PLACEHOLDER)
                ),
            ],
            "",
            buttons,
            [],
        )
        self._settings.show_card(card)

    def _on_mapping_pressed(self, key: str) -> None:
        card = self._mapping
        if card is None:
            return
        current = self._window.config_manager.xmltv_channel_map
        entries = self._entries(current)
        if key == DELETE and self._editing is not None:
            kept = MultiKeyDict()
            for keys, value in entries:
                if keys != self._editing:
                    kept[keys] = value
            card.hide()
            self._keep(kept)
        elif key == SAVE:
            values = card.values()
            name, icon = values.get(NAME, "").strip(), values.get(ICON, "").strip()
            ids = tuple(part.strip() for part in values.get(IDS, "").split(",") if part.strip())
            if not name or not ids:
                card.set_found(translate(REQUIRED_TEXT), False)
                return
            changed = MultiKeyDict()
            placed = False
            for keys, value in entries:
                if self._editing is not None and keys == self._editing and not placed:
                    changed[ids] = {**value, "name": name, "icon": icon}  # in its place
                    placed = True
                elif self._editing is not None and keys == ids:
                    continue  # the entry that had these IDs gives way to the edit
                else:
                    changed[keys] = value
            if not placed:
                changed[ids] = {"name": name, "icon": icon}
            card.hide()
            self._keep(changed)

    def _keep(self, mapping: MultiKeyDict) -> None:
        """The map in place of the config's (never changed in place: a guide loading on its
        worker reads it), saved, and the guide shown re-keyed, as the old Save did."""
        config = self._window.config_manager
        config.xmltv_channel_map = mapping
        self._save()
        if config.epg_source != STB:  # the provider's own guide is no XMLTV guide
            window = self._window
            (window.catalog.epg_manager or window.epg_manager).reindex_programs()
        self._settings.refresh_sections()

    def _import(self) -> None:
        """Import…: the system's picker, then the file read on a worker; its matching in place
        of the list (as the old window's)."""
        if self._importing:
            return
        path = pick_file(self._drawer.window(), translate(IMPORT_TEXT).rstrip("… ."), "")
        if not path or self._window.closing():
            return
        self._importing = True
        self._window.jobs.run_worker(ImportMapping(path), self._imported, self._import_failed)

    def _imported(self, _context: object, mapping: object) -> None:
        self._importing = False
        if isinstance(mapping, MultiKeyDict):
            self._keep(mapping)
            self._notify(translate(IMPORTED_TEXT))

    def _import_failed(self, _context: object, message: str) -> None:
        self._importing = False
        self._notify(translate(message), "error")

    def _export(self) -> None:
        """Export…: the system's picker for the file (".json" added, as the old window did),
        then the list written on a worker."""
        if self._exporting:
            return
        path = pick_save_file(self._drawer.window(), translate(EXPORT_TEXT).rstrip("… ."), "")
        if not path or self._window.closing():
            return
        if not path.endswith(".json"):
            path += ".json"
        export = _export_of(self._window.config_manager.xmltv_channel_map)
        self._exporting = True
        self._window.jobs.run_worker(
            ExportMapping(path, export), self._exported, self._export_failed
        )

    def _exported(self, _context: object, _payload: object) -> None:
        self._exporting = False
        self._notify(translate(EXPORTED_TEXT))

    def _export_failed(self, _context: object, message: str) -> None:
        self._exporting = False
        self._notify(translate(message), "error")

    # ---- what they share --------------------------------------------------------------------

    def _save(self) -> None:
        try:
            self._window.config_manager.save_config()
        except Exception as e:  # only a config without its background writer raises
            logger.warning("Couldn't save the guide's settings: %s", type(e).__name__)

    def _notify(self, text: str, kind: str = "info") -> None:
        if kind == "error":
            self._window.screens.notify(text, kind, ERROR_MS)
        else:
            self._window.screens.notify(text, kind)
