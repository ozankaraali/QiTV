"""Settings › General, Files and Advanced in the drawer (plan 3): the old Settings window's
General tab, its rows where the spec puts them, each applied at once.

- General: Language (from the next start), Allow Check for Updates (turned on, it checks once,
  as at a start, on the update checker's own thread), Enable Channel Logos, Film and series
  details from TMDB, and the TMDB token (a secret, kept without spaces);
- Files: Open videos with QiTV and Open music with QiTV, what opens such files now at their
  end; a press tells the system on a worker (ui.file_types_settings' FileTypesSwitch, as the
  old window's button), its help the old window's hint, and a press that fails is said. A kind
  QiTV can't be registered for (a run from source) has no row. On Windows, Default Apps… once
  QiTV opens a kind there;
- Advanced: Verify SSL certificates (the providers that follow it connect anew, as the old
  Save had them: ProviderActions.global_ssl_changed), Secure DNS, the detailed log, the image
  cache's largest size (from the next start), and Clear Image Cache…, asked first in a card,
  cleared on a worker, then said.

The config's rows are written through the values (services.settings_values), each followed by
its hook (`hooks`, which the TV window gives them). What only the running QiTV knows (its
languages, read once; whether its build carries a TMDB token; the image cache's size as set;
the Files rows as their status is now) is the sections' Context (`context`). Keys: the config's
for its settings; `files/videos`, `files/music` and `files/default_apps`;
`advanced/clear_image_cache`.
"""

from __future__ import annotations

from functools import partial
import logging
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, Signal

from services import i18n, log_file, secure_dns, tmdb
from services.file_types import Status
from services.i18n import N_, translate
from services.settings_model import (
    ACTION,
    ADVANCED_PREFIX,
    CLEAR_IMAGE_CACHE,
    FILES_PREFIX,
    Context,
    Row,
)
from services.settings_texts import CLEAR_CACHE_QUESTION
from ui.file_types_settings import (
    DEFAULT_APPS_TEXT,
    FAILED_HINT,
    TEXTS,
    FileTypesSwitch,
    default_apps_offered,
    open_default_apps,
)
from ui.overlays.settings_card import CANCEL, CANCEL_TEXT, Button, ChoiceCard
from ui.settings_controller import SettingsController
from ui.tv_controller import ERROR_MS
from ui.update_dialogs import check_for_updates

logger = logging.getLogger(__name__)

VIDEOS = FILES_PREFIX + "videos"
MUSIC = FILES_PREFIX + "music"
DEFAULT_APPS = FILES_PREFIX + "default_apps"
_KINDS = {VIDEOS: "videos", MUSIC: "music"}  # a Files row's key -> its kind of file
# i18n: at the end of Settings → Files → Open videos (or music) with QiTV: such files open in QiTV
IN_QITV_TEXT = N_("QiTV opens them")
# i18n: at the end of Settings → Files → Open videos (or music) with QiTV: they open elsewhere
ELSEWHERE_TEXT = N_("Another app opens them")
# i18n: help under Settings → Files → Default Apps… (Windows' settings page, as Windows names it)
DEFAULT_APPS_HELP = N_("Opens Windows' Default Apps settings on QiTV's page.")
CLEAR = "clear"  # the key of the Clear Image Cache question's Clear
CLEAR_TEXT = N_("Clear")  # i18n: a button: empties it (verb)
# i18n: a toast: Settings → Advanced → Clear Image Cache emptied it
CLEARED_TEXT = N_("Image cache cleared")


def hooks(window: Any) -> dict[str, Callable[[], None]]:
    """The hooks of General's and Advanced's config rows (SettingsValues'): what a step changes
    beyond the config, at once. `window` is the TV window; its parts are read as a hook runs
    (the values are built before them)."""
    config = window.config_manager

    def updates() -> None:
        if config.check_updates:  # turned on: one check now, as at a start (its own thread)
            check_for_updates(config_manager=config)

    def details() -> None:  # TMDB turned off or on, or a token of one's own given or taken
        window.screens.tmdb.settings_changed()

    def ssl() -> None:
        window.provider_rows.actions.global_ssl_changed()
        window.settings.refresh_sections()  # a provider's own check shows its new default

    def dns() -> None:
        on = config.secure_dns is True
        secure_dns.enable(on)  # at once, here ...
        if window.engine is not None:
            window.engine.set_secure_dns(on)  # ... and in the time-shift helper

    def detailed() -> None:
        on = config.detailed_log is True
        log_file.set_detailed(on)
        if window.engine is not None:  # the running helper's detail follows at once
            window.engine.set_detailed_log(on)

    return {
        "check_updates": updates,
        "tmdb_details": details,
        "tmdb_token": details,
        "ssl_verify": ssl,
        "secure_dns": dns,
        "detailed_log": detailed,
    }


class ClearImageCache(QObject):
    """Clear Image Cache's work, run on a JobRunner thread (the image cache locks its index)."""

    finished = Signal(object)
    error = Signal(str)

    def __init__(self, images: Any) -> None:
        super().__init__()
        self._images = images

    def run(self) -> None:
        self._images.clear_cache()
        self.finished.emit(None)


class GeneralRows(QObject):
    """The handler of Files' and Advanced's own rows (`files/…`, `advanced/…`, registered
    with `settings`) and what General, Files and Advanced show (`context`). `window` is the
    TV window; `drawer` the Settings layer the rows show in."""

    def __init__(self, window: Any, settings: SettingsController, drawer: Any) -> None:
        super().__init__(drawer)
        self._window = window
        self._settings = settings
        self._drawer = drawer
        self._languages: Optional[tuple[tuple[tuple[str, str], ...], str]] = None
        # A press's answer is heard here (this outlives Settings: it is the drawer's).
        self._switches = {
            key: FileTypesSwitch(
                window.config_manager, window, kind, self, answered=partial(self._answered, key)
            )
            for key, kind in _KINDS.items()
        }
        self._question: Optional[ChoiceCard] = None
        self._clearing = False
        settings.register(FILES_PREFIX, self)
        settings.register(ADVANCED_PREFIX, self)

    @property
    def question(self) -> Optional[ChoiceCard]:
        """The card that asks before the image cache is cleared (None until it first asked)."""
        return self._question

    # ---- what the sections show -------------------------------------------------------------

    def context(self) -> Context:
        """What General, Files and Advanced show that only the running QiTV knows."""
        if self._languages is None:  # its catalogs' folder: the same while it runs
            available = tuple(i18n.available_languages())
            system = dict(available).get(i18n.system_language(), i18n.SOURCE_NAME)
            self._languages = (available, system)
        languages, system = self._languages
        return Context(
            languages=languages,
            system_language=system,
            tmdb_built_in=bool(tmdb.built_in_token()),
            image_cache_mb=self._window.config_manager.max_cache_image_size,
            files=self._file_rows(),
        )

    def _file_rows(self) -> tuple[Row, ...]:
        """A row per kind QiTV can open (none from source), what opens it now at its end; then
        Windows' Default Apps… once QiTV opens one there."""
        rows = []
        for key, switch in self._switches.items():
            state = switch.status()
            if state is Status.UNAVAILABLE:
                continue
            if state is not Status.REGISTERED:
                switch.open_with_only = False  # (Linux: only while QiTV is registered)
            note = IN_QITV_TEXT if state is Status.REGISTERED else ELSEWHERE_TEXT
            rows.append(
                Row(
                    key,
                    TEXTS[switch.kind].label,
                    switch.hint(state),
                    kind=ACTION,
                    note=translate(note),
                )
            )
        if default_apps_offered(self._window.config_manager):
            rows.append(Row(DEFAULT_APPS, DEFAULT_APPS_TEXT, DEFAULT_APPS_HELP, kind=ACTION))
        return tuple(rows)

    # ---- RowHandler -------------------------------------------------------------------------

    def value(self, row: Row) -> Any:
        return None  # they act: no value

    def step(self, row: Row, delta: int) -> None:
        pass

    def edited(self, row: Row, text: str) -> None:
        pass

    def chosen(self, row: Row) -> None:
        switch = self._switches.get(row.key)
        if switch is not None:
            switch.press()  # (a status that can't be pressed: its help says what to do)
        elif row.key == DEFAULT_APPS:
            open_default_apps()
        elif row.key == CLEAR_IMAGE_CACHE:
            self._ask_clear()

    # ---- the answers ------------------------------------------------------------------------

    def _answered(self, key: str) -> None:
        """A Files row's press was answered (and saved): what opens the files now shows."""
        if self._switches[key].failed:
            self._notify(translate(FAILED_HINT), "error", ERROR_MS)
        self._settings.refresh_sections()

    def _ask_clear(self) -> None:
        """Clear Image Cache…: asked first, Cancel the answer the keys start on."""
        if self._question is None:
            self._question = ChoiceCard(self._drawer.parentWidget())
            self._question.answered.connect(self._on_clear_answered)
        self._question.ask(
            translate(CLEAR_CACHE_QUESTION),
            "",
            [
                Button(CANCEL, translate(CANCEL_TEXT)),
                Button(CLEAR, translate(CLEAR_TEXT), danger=True),
            ],
            CANCEL,
        )
        self._settings.show_card(self._question)

    def _on_clear_answered(self, key: str) -> None:
        if key != CLEAR or self._clearing:
            return
        self._clearing = True
        job = ClearImageCache(self._window.image_manager)
        self._window.jobs.run_worker(job, self._cleared, self._clear_failed)

    def _cleared(self, _context: object, _payload: object) -> None:
        self._clearing = False
        self._notify(translate(CLEARED_TEXT))

    def _clear_failed(self, _context: object, message: str) -> None:
        self._clearing = False
        self._notify(translate(message), "error", ERROR_MS)

    def _notify(self, text: str, kind: str = "info", ms: Optional[int] = None) -> None:
        if ms is None:
            self._window.screens.notify(text, kind)
        else:
            self._window.screens.notify(text, kind, ms)
