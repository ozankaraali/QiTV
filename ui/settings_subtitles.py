"""Settings › Subtitles & AI captions in the drawer (plan 3): the old Settings window's Subtitles
tab, each row applied at once.

- AI captions: the speech model (a config row: the next captions start with it); the space
  their downloaded files take (measured on a worker each time Settings opens, and after a
  delete); and Delete downloaded files…, asked first in a card, then the captions off and their
  download cancelled (CaptionsController.release), the files deleted on a worker once nothing
  holds them, and the space measured again;
- OpenSubtitles: the API key (a secret that shows only its last four characters, kept without
  spaces), the username (kept without the spaces at its ends) and the password (a secret), all
  config rows edited in place; Sign out clears the username and the password, and saves. The
  next search signs in anew (ui.subtitle_search replaces a client whose sign-in changed).

What only the running QiTV knows (whether its build carries an OpenSubtitles key, what the
files take) is the sections' Context (`context`). Keys: the config's for its settings;
`subtitles/captions_files`, `subtitles/delete_captions` and `subtitles/sign_out`.
"""

from __future__ import annotations

from dataclasses import replace
import logging
from typing import Any, Optional

from PySide6.QtCore import QEvent, QObject

from services.i18n import N_, translate
from services.opensubtitles import built_in_key
from services.settings_model import (
    DELETE_CAPTIONS,
    SIGN_OUT,
    SUBTITLES_PREFIX,
    Context,
    Row,
)
from services.settings_values import SettingsValues
from ui.captions_files import DELETE_QUESTION, CaptionsFilesJob, captions_space_text
from ui.captions_texts import size_text
from ui.overlays.settings_card import CANCEL, CANCEL_TEXT, Button, ChoiceCard
from ui.settings_controller import SettingsController

logger = logging.getLogger(__name__)

DELETE = "delete"  # the key of the delete question's Delete
DELETE_TEXT = N_("Delete")  # i18n: a button: deletes AI captions' speech models (verb)


class SubtitlesRows(QObject):
    """The handler of Subtitles' own rows (`subtitles/…`, registered with `settings`) and what
    the section shows (`context`). `window` is the TV window (its jobs, its captions); `drawer`
    the Settings layer the rows show in; `values` the config rows' values (Sign out writes
    through them)."""

    def __init__(
        self, window: Any, settings: SettingsController, drawer: Any, values: SettingsValues
    ) -> None:
        super().__init__(drawer)
        self._window = window
        self._settings = settings
        self._drawer = drawer
        self._values = values
        self._question: Optional[ChoiceCard] = None
        self._size: Optional[int] = None  # bytes; None: not known, or no such files here
        self._measured = False  # the size is known (once measured)
        self._busy = False  # a measure (or a delete) on its way
        self._again = False  # asked for again meanwhile
        self._again_delete = False  # ... with a delete
        # A delete answered, until the measure after it (once the captions let the files go:
        # CaptionsController.release can take a while) says what is left.
        self._deleting = False
        settings.register(SUBTITLES_PREFIX, self)
        drawer.installEventFilter(self)  # Settings opening: the size measured anew
        # Sign out shows while a username is kept: the rows again as it is typed or cleared.
        values.add_hook("opensubtitles_username", settings.refresh_sections)

    @property
    def question(self) -> Optional[ChoiceCard]:
        """The card that asks before the files are deleted (None until it first asked)."""
        return self._question

    # ---- what the section shows -------------------------------------------------------------

    def context(self, base: Context) -> Context:
        """`base` with what Subtitles shows that only the running QiTV knows."""
        if not self._measured:
            space: Optional[str] = ""  # being measured
        else:
            space = captions_space_text(self._size) if self._size is not None else None
        return replace(
            base,
            opensubtitles_built_in=bool(built_in_key()),
            opensubtitles_signed_in=bool(self._values.get("opensubtitles_username")),
            captions_space=space,
            captions_downloaded=self._measured and bool(self._size) and not self._deleting,
        )

    # ---- the files' size --------------------------------------------------------------------

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._drawer and event.type() == QEvent.Type.Show:
            if not self._drawer.isHidden() and not self._deleting:
                self._measure()  # (while a delete waits, the measure after it says it)
        return False

    def _measure(self, delete: bool = False) -> None:
        """The files' size (after deleting them, with `delete`), on a worker; one at a time,
        one asked for meanwhile after it (a delete never lost)."""
        if self._busy:
            self._again = True
            self._again_delete = self._again_delete or delete
            return
        self._busy = True
        self._window.jobs.run_worker(
            CaptionsFilesJob(delete), self._sized, self._unsized, context=delete
        )

    def _sized(self, deleted: object, size: Optional[int]) -> None:
        """A measure's answer (`deleted`: its job deleted the files first). While a delete
        waits for the captions to let go, only the measure after it says what is left."""
        self._busy = False
        if self._again:
            delete, self._again, self._again_delete = self._again_delete, False, False
            self._measure(delete)
            return
        if self._deleting and deleted is not True:
            return  # measured before the files went: Delete stays on its way
        self._size = size
        self._measured = True
        self._deleting = False
        self._settings.refresh_sections()  # the line says it; Delete only with something

    def _unsized(self, deleted: object, message: str) -> None:
        logger.warning("AI captions' files: %s", message)
        self._sized(deleted, None)

    # ---- RowHandler -------------------------------------------------------------------------

    def value(self, row: Row) -> Any:
        return None  # they act, or say their note: no value

    def step(self, row: Row, delta: int) -> None:
        pass

    def edited(self, row: Row, text: str) -> None:
        pass

    def chosen(self, row: Row) -> None:
        if row.key == DELETE_CAPTIONS:
            self._ask_delete()
        elif row.key == SIGN_OUT:
            # The sign-in goes; the next search's client has none (the old one signs out).
            self._values.set("opensubtitles_username", "")
            self._values.set("opensubtitles_password", "")

    # ---- deleting ---------------------------------------------------------------------------

    def _ask_delete(self) -> None:
        """Delete downloaded files…: asked first, Cancel the answer the keys start on."""
        size = self._size
        if not size or self._deleting:
            return
        if self._question is None:
            self._question = ChoiceCard(self._drawer.parentWidget())
            self._question.answered.connect(self._on_delete_answered)
        self._question.ask(
            translate(DELETE_QUESTION, size=size_text(size)),
            "",
            [
                Button(CANCEL, translate(CANCEL_TEXT)),
                Button(DELETE, translate(DELETE_TEXT), danger=True),
            ],
            CANCEL,
        )
        self._settings.show_card(self._question)

    def _on_delete_answered(self, key: str) -> None:
        if key != DELETE or self._deleting:
            return
        self._deleting = True
        self._settings.refresh_sections()  # Delete goes at once: it is on its way
        captions = getattr(self._window, "captions", None)
        if captions is not None and hasattr(captions, "release"):
            # Off, their download cancelled, and the files deleted once nothing holds them open
            # (Windows keeps an open file); `release` calls back on this thread.
            captions.release(lambda: self._measure(delete=True))
        else:
            self._measure(delete=True)
