"""Settings › Time-shift & recordings in the drawer (plan 3): the old Settings window's Time-shift
tab, each row applied at once.

- Use disk for a longer time-shift buffer: turned on, the kept buffers follow at once and the
  next channel opens on disk (its tune reads the setting); turned off, it asks first in a card
  (Turn off, or Cancel where the keys start), since what the disk kept goes: then the kept
  buffers let their disk ones go and the helper drops what no session holds. Nothing changes
  until Turn off is answered; Settings going with the question up keeps it on;
- Memory per stream: a config row (from the next channel), with how far back it reaches under
  its title;
- Disk cache size: while the disk is on, a value card (256 MiB to 16 GiB, by 256 MiB) with the
  estimated rewind of the example streams; what is kept applies at once to the disk buffers
  (the old window's "on the next channel you open" was wrong since 2.8); while the disk is off
  it only says the size;
- Recordings folder: where recordings go, then Change… (the system's folder picker), Use the
  default (while a folder of one's own is set) and Open folder (the system's file manager; a
  folder not there yet says there are no recordings yet). A folder changed here goes the
  Recordings page's way (recording.use_place): the one used before stays listed, with its
  recordings.

The config's rows are written through the values (services.settings_values), each followed by
its hook (`hooks`, which the TV window gives them). What the section shows of the settings as
they are now is the sections' Context (`context`). Keys: the config's for its settings (the
disk's steps are handled here, by its key); `timeshift/…` for its own rows.
"""

from __future__ import annotations

from dataclasses import replace
import logging
import os
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject

from services import recording, system_open
from services.i18n import N_, translate
from services.settings_model import (
    CHANGE_FOLDER,
    DEFAULT_FOLDER,
    DISK_CACHE,
    DISK_CACHE_TEXT,
    OPEN_FOLDER,
    TIMESHIFT_DISK,
    TIMESHIFT_PREFIX,
    Context,
    Row,
    step,
)
from services.settings_texts import cache_rewind_text, cache_size_text
from services.settings_values import SettingsValues
from ui.overlays import settings_card
from ui.overlays.settings_card import CANCEL, CANCEL_TEXT, Button, ChoiceCard, RangeCard
from ui.settings_controller import SettingsController

logger = logging.getLogger(__name__)

TURN_OFF = "turn_off"  # the key of the question's Turn off
# i18n: asked before the time-shift buffer's disk is turned off (what it kept on disk goes)
TURN_OFF_QUESTION = N_("Turning the disk off removes what it kept")
TURN_OFF_TEXT = N_("Turn off")  # i18n: a button: turns the time-shift buffer's disk off (verb)
CHOOSE_FOLDER_TITLE = N_("Choose the Recordings folder")  # i18n: the title of a folder picker
DISK_CACHE_MIB = (256, 16384, 256)  # the disk cache's smallest and largest size, and its step
MIB = 1024 * 1024


def hooks(window: Any) -> dict[str, Callable[[], None]]:
    """The hooks of Time-shift's config rows (SettingsValues'): what a step changes beyond the
    config, at once. `window` is the TV window; its parts are read as a hook runs."""
    config = window.config_manager

    def disk() -> None:
        on = config.timeshift_enabled is True
        if window.kept is not None:
            # Off: every disk buffer kept goes, and what no session holds; on: from the next
            # channel (its tune reads the setting).
            window.kept.set_disk(on)
            if window.engine is not None and not on:
                window.engine.drop_kept()
        window.settings.refresh_sections()  # the disk cache's size: a card only while on

    def memory() -> None:
        window.settings.refresh_sections()  # how far back it reaches, under its title

    def disk_amount() -> None:
        if window.engine is not None and config.timeshift_enabled is True:
            # 2.8: every disk buffer holds it at once (a kept one gives way first).
            window.engine.set_disk_amount(int(config.timeshift_max_mib) * MIB)
        window.settings.refresh_sections()  # its row says the size

    return {
        TIMESHIFT_DISK: disk,
        "timeshift_ram_mib": memory,
        "timeshift_max_mib": disk_amount,
    }


class TimeshiftRows(QObject):
    """The handler of Time-shift's own rows (`timeshift/…`) and of the disk's steps (its config
    row, registered by its key), with `settings`, and what the section shows (`context`).
    `window` is the TV window; `drawer` the Settings layer the rows show in; `values` the
    config rows' values (each write saves and runs its hook)."""

    def __init__(
        self, window: Any, settings: SettingsController, drawer: Any, values: SettingsValues
    ) -> None:
        super().__init__(drawer)
        self._window = window
        self._settings = settings
        self._drawer = drawer
        self._values = values
        self._question: Optional[ChoiceCard] = None
        self._range: Optional[RangeCard] = None
        settings.register(TIMESHIFT_PREFIX, self)
        settings.register(TIMESHIFT_DISK, self)  # (only that key starts so)

    @property
    def question(self) -> Optional[ChoiceCard]:
        """The card that asks before the disk is turned off (None until it first asked)."""
        return self._question

    @property
    def range_card(self) -> Optional[RangeCard]:
        """The disk cache size's card (None until it first showed)."""
        return self._range

    # ---- what the section shows -------------------------------------------------------------

    def context(self, base: Context) -> Context:
        """`base` with the time-shift settings and the Recordings folder as they are now."""
        config = self._window.config_manager
        return replace(
            base,
            timeshift_disk=config.timeshift_enabled is True,
            timeshift_ram_mib=config.timeshift_ram_mib,
            timeshift_max_mib=config.timeshift_max_mib,
            recordings_folder=recording.folder(config),
            recordings_own=bool(config.recordings_dir.strip()),  # (recording.folder's rule)
        )

    # ---- RowHandler -------------------------------------------------------------------------

    def value(self, row: Row) -> Any:
        if row.key == TIMESHIFT_DISK:
            return self._window.config_manager.timeshift_enabled
        return None  # they act, or say their note: no value

    def step(self, row: Row, delta: int) -> None:
        """The disk turned on at once; off only once asked."""
        if row.key != TIMESHIFT_DISK:
            return
        on = self._window.config_manager.timeshift_enabled is True
        wanted = step(row, on, delta)
        if wanted == on:
            return
        if wanted:
            self._values.set(TIMESHIFT_DISK, True)
        else:
            self._ask_turn_off()

    def edited(self, row: Row, text: str) -> None:
        pass

    def chosen(self, row: Row) -> None:
        if row.key == DISK_CACHE:
            self._ask_size()
        elif row.key == CHANGE_FOLDER:
            self._change_folder()
        elif row.key == DEFAULT_FOLDER:
            self._use_folder(recording.default_folder())
        elif row.key == OPEN_FOLDER:
            self._open_folder()

    # ---- the disk ---------------------------------------------------------------------------

    def _ask_turn_off(self) -> None:
        if self._question is None:
            self._question = ChoiceCard(self._drawer.parentWidget())
            self._question.answered.connect(self._on_turn_off_answered)
        self._question.ask(
            translate(TURN_OFF_QUESTION),
            "",
            [
                Button(CANCEL, translate(CANCEL_TEXT)),
                Button(TURN_OFF, translate(TURN_OFF_TEXT), danger=True),
            ],
            CANCEL,
        )
        self._settings.show_card(self._question)

    def _on_turn_off_answered(self, key: str) -> None:
        if key == TURN_OFF and self._window.config_manager.timeshift_enabled is True:
            self._values.set(TIMESHIFT_DISK, False)  # its hook lets what the disk kept go

    def _ask_size(self) -> None:
        """The disk cache's size on a card, from the size as it is; only while the disk is on
        (its row only says it otherwise)."""
        config = self._window.config_manager
        if config.timeshift_enabled is not True:
            return
        if self._range is None:
            self._range = RangeCard(self._drawer.parentWidget())
            self._range.answered.connect(self._on_size_answered)
        low, high, by = DISK_CACHE_MIB
        self._range.ask(
            translate(DISK_CACHE_TEXT),
            int(config.timeshift_max_mib),
            low,
            high,
            by,
            cache_size_text,
            cache_rewind_text,
        )
        self._settings.show_card(self._range)

    def _on_size_answered(self, value: object) -> None:
        if not isinstance(value, int) or value == self._window.config_manager.timeshift_max_mib:
            return  # cancelled, or kept as it was
        self._values.set("timeshift_max_mib", value)  # its hook applies it at once

    # ---- the Recordings folder --------------------------------------------------------------

    def _change_folder(self) -> None:
        config = self._window.config_manager
        chosen = settings_card.pick_folder(
            self._window, translate(CHOOSE_FOLDER_TITLE), recording.folder(config)
        )
        if chosen:
            self._use_folder(os.path.normpath(chosen))

    def _use_folder(self, path: str) -> None:
        """New recordings go to `path`; the folder they went to stays listed on the Recordings
        page (recording.use_place), saved at once."""
        config = self._window.config_manager
        recording.use_place(config, path)
        try:
            config.save_config()
        except Exception:
            # Only a config without a background writer raises here; the folder is used in
            # memory and the next save writes it.
            logger.warning("Couldn't save the Recordings folder", exc_info=True)
        self._settings.refresh_sections()

    def _open_folder(self) -> None:
        """The folder in the system's file manager (not waited for, as the menu's Recordings
        folder opens it); one not there yet (the first recording makes it) says so."""
        if not system_open.open_folder(recording.folder(self._window.config_manager)):
            self._window.screens.notify(translate(recording.NO_RECORDINGS))
