"""Settings → General → Open videos with QiTV, and Open music with QiTV: each row's button
registers QiTV with the system for its kind of file, or takes it back (services.file_types),
on a thread of its own; the hint under it says what the system does then. The rows are
independent: each follows its own kind's status. At start, a QiTV that was moved registers
itself again where it is now (`refresh_at_start`).

The service only records what it did in the config; it is saved here, on the GUI thread."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import sys
import threading
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid

from services import file_types
from services.file_types import Status
from services.i18n import N_, translate
from services.redact import safe_message

logger = logging.getLogger(__name__)

PRESS_THREAD = "qitv-file-types"
REFRESH_THREAD = "qitv-file-types-refresh"
DEFAULT_APPS_THREAD = "qitv-default-apps"
ERROR_STYLE = "color: red;"  # as Settings' other failures (Verify Provider, Paste link)

# Each row's label, its button (Settings → General → Open videos with QiTV) and the hints
# under it; the music row's say "music" where the videos' say "videos".
# i18n: a setting (General): QiTV opens video files, as with a double-click on one
VIDEOS_LABEL = N_("Open videos with QiTV")
# i18n: a button (Settings → General): video files open in QiTV, as with a double-click
USE_TEXT = N_("Use QiTV for videos")
# i18n: a button (Settings → General): video files no longer open in QiTV
STOP_TEXT = N_("Stop using QiTV for videos")
# i18n: a setting (General): QiTV opens music files (songs), as with a double-click on one
MUSIC_LABEL = N_("Open music with QiTV")
# i18n: a button (Settings → General): music files (songs) open in QiTV, as with a double-click
MUSIC_USE_TEXT = N_("Use QiTV for music")
# i18n: a button (Settings → General): music files (songs) no longer open in QiTV
MUSIC_STOP_TEXT = N_("Stop using QiTV for music")
# Windows, once registered: back to the page where Windows asks (closed unconfirmed, or later).
# i18n: a button on Windows: opens Windows' Default Apps settings on QiTV's page (its own name)
DEFAULT_APPS_TEXT = N_("Default Apps…")
# i18n: a hint on Windows; "Default Apps": Windows Settings' page, as Windows names it
WINDOWS_HINT = N_("Windows then asks you to confirm in its Default Apps settings.")
# i18n: a hint on macOS (the button makes QiTV their default); keep the formats' names
MAC_HINT = N_("QiTV becomes the default for MKV, MP4, MOV, AVI and WebM.")
# macOS gives the files no app back by itself when QiTV stops.
# i18n: a hint on macOS; Get Info, Open with, Change All: Finder's own names for them
MAC_REGISTERED_HINT = N_("To pick another app, use Get Info → Open with → Change All in Finder.")
# i18n: a hint on macOS (the music button makes QiTV their default); keep the formats' names
MUSIC_MAC_HINT = N_("QiTV becomes the default for MP3, M4A and FLAC.")
# i18n: a hint on Linux (the button makes QiTV the default video player)
LINUX_HINT = N_("QiTV becomes the default video player.")
# i18n: a hint on Linux (the music button makes QiTV the default music player)
MUSIC_LINUX_HINT = N_("QiTV becomes the default music player.")
LINUX_OPEN_WITH_HINT = N_(
    # i18n: a hint on Linux when the system took no default; "Open With": the file manager's menu
    "QiTV is in Open With now. To make it the default, choose it in your file manager's Open With."
)
# i18n: a hint under the disabled button: QiTV runs from a temporary folder or a zip
TEMPORARY_HINT = N_("Move QiTV out of the temporary folder first.")
# i18n: a hint on macOS (QiTV runs from Downloads or its disk image): the Applications folder
MAC_TEMPORARY_HINT = N_("Move QiTV into Applications first.")
# i18n: a hint under the button: telling the system failed (QiTV's log says why)
FAILED_HINT = N_("Couldn't change it. See the log.")


@dataclass(frozen=True)
class RowTexts:
    """A row's own texts (untranslated: each is translated where it shows)."""

    label: str
    use: str
    stop: str
    mac_hint: str
    linux_hint: str


TEXTS = {
    "videos": RowTexts(VIDEOS_LABEL, USE_TEXT, STOP_TEXT, MAC_HINT, LINUX_HINT),
    "music": RowTexts(
        MUSIC_LABEL, MUSIC_USE_TEXT, MUSIC_STOP_TEXT, MUSIC_MAC_HINT, MUSIC_LINUX_HINT
    ),
}
# The dialog's attributes for each row (`<prefix>_button`, `<prefix>_hint`, ...) and its
# widgets' object names (`<name>Button`, ...).
_PREFIXES = {"videos": ("file_types", "fileTypes"), "music": ("music_file_types", "musicTypes")}


def _system() -> str:
    """The system the hints are for: "win32", "darwin" or "linux"."""
    return "linux" if sys.platform.startswith("linux") else sys.platform


def hint_text(
    state: Status, system: str, open_with_only: bool = False, kind: str = "videos"
) -> str:
    """The hint under `kind`'s button ("": none). `open_with_only`: this window registered
    QiTV on Linux without making it the default (no xdg-mime)."""
    if state is Status.REGISTERED and system == "linux" and open_with_only:
        return translate(LINUX_OPEN_WITH_HINT)
    texts = TEXTS[kind]
    hints = {
        (Status.NOT_REGISTERED, "win32"): WINDOWS_HINT,
        (Status.NOT_REGISTERED, "darwin"): texts.mac_hint,
        (Status.NOT_REGISTERED, "linux"): texts.linux_hint,
        (Status.REGISTERED, "darwin"): MAC_REGISTERED_HINT,
        (Status.TEMPORARY, "darwin"): MAC_TEMPORARY_HINT,
    }
    hint = hints.get((state, system))
    if hint is None and state is Status.TEMPORARY:
        hint = TEMPORARY_HINT
    return translate(hint) if hint else ""


def open_default_apps() -> None:
    """Windows' Default Apps page (file_types.open_default_apps), on a thread of its own:
    os.startfile is ShellExecute, which can keep the calling thread waiting."""

    def body() -> None:
        try:
            file_types.open_default_apps()
        except Exception:
            logger.warning("Windows' Default Apps page didn't open", exc_info=True)

    threading.Thread(target=body, name=DEFAULT_APPS_THREAD, daemon=True).start()


def _save(config: Any) -> None:
    try:
        config.save_config()  # written off the GUI thread (ConfigManager.write_in_background)
    except Exception as e:  # a locked config: kept for this run only
        logger.warning("Couldn't save which files QiTV opens: %s", safe_message(e))


class _Answer(QObject):
    """A worker's answer, heard on the GUI thread (this object's) through a queued signal."""

    answered = Signal(object)

    def __init__(self, parent: QObject, handle: Callable[[object], None]) -> None:
        super().__init__(parent)
        self._handle = handle
        self.answered.connect(self._deliver, Qt.ConnectionType.QueuedConnection)

    def run(self, work: Callable[[], object], name: str) -> None:
        """`work()` on a thread of its own; its answer (None when it raised) is handled here."""

        def body() -> None:
            try:
                answer = work()
            except Exception:  # never unanswered: the button would stay disabled
                logger.warning("Telling the system which files QiTV opens failed", exc_info=True)
                answer = None
            try:
                self.answered.emit(answer)
            except RuntimeError:
                pass  # the window closed meanwhile

        threading.Thread(target=body, name=name, daemon=True).start()

    @Slot(object)
    def _deliver(self, answer: object) -> None:
        self._handle(answer)


class FileTypesRow:
    """A kind's row: its button and hint (`button`, `hint`), with Windows' Default Apps beside
    the button once QiTV is registered there (`default_apps_button`); hidden where QiTV can't
    register (a run from source)."""

    def __init__(
        self, dialog: Any, layout: QFormLayout, parent: QWidget, kind: str = "videos"
    ) -> None:
        self.kind = kind
        self._texts = TEXTS[kind]
        name = _PREFIXES[kind][1]
        self._config = dialog.config_manager
        self._window = dialog.parent()  # TVWindow: its DDE follows a press (Windows)
        self._layout = layout
        self._busy = False  # a press's answer is on its way
        self._failed = False  # the last press failed: the hint says so
        self._open_with_only = False  # Linux: this window's register took no default
        self.box = QWidget(parent)
        column = QVBoxLayout(self.box)
        column.setContentsMargins(0, 0, 0, 0)
        self.button = QPushButton(self.box)
        self.button.setObjectName(f"{name}Button")
        self.button.setAutoDefault(False)  # Enter in a field still saves
        self.button.clicked.connect(self.press)
        self.default_apps_button = QPushButton(translate(DEFAULT_APPS_TEXT), self.box)
        self.default_apps_button.setObjectName(f"{name}DefaultApps")
        self.default_apps_button.setAutoDefault(False)
        self.default_apps_button.setFlat(True)  # the row's second action, quieter
        self.default_apps_button.clicked.connect(lambda: open_default_apps())
        buttons = QHBoxLayout()
        buttons.addWidget(self.button)
        buttons.addWidget(self.default_apps_button)
        buttons.addStretch(1)
        column.addLayout(buttons)
        self.hint = QLabel(self.box)
        self.hint.setObjectName(f"{name}Hint")
        self.hint.setWordWrap(True)
        column.addWidget(self.hint)
        layout.addRow(translate(self._texts.label), self.box)
        # Settings' own: an answer that comes after it closed still saves the config.
        self._answer = _Answer(dialog, self._answered)
        self.show()

    def show(self) -> None:
        """The button and hint as the record says now (for this row's kind only)."""
        state = file_types.status(self.kind, self._config)
        self._layout.setRowVisible(self.box, state is not Status.UNAVAILABLE)
        registered = state is Status.REGISTERED
        if not registered:
            self._open_with_only = False
        self.button.setText(translate(self._texts.stop if registered else self._texts.use))
        can_press = state in (Status.REGISTERED, Status.NOT_REGISTERED)
        self.button.setEnabled(can_press and not self._busy)
        self.default_apps_button.setVisible(registered and _system() == "win32")
        if self._failed:
            text = translate(FAILED_HINT)
        else:
            text = hint_text(state, _system(), self._open_with_only, self.kind)
        self.hint.setText(text)
        self.hint.setStyleSheet(ERROR_STYLE if self._failed else "")
        self.hint.setVisible(bool(text))  # (an empty line's height until it says something)

    def press(self) -> None:
        """Use QiTV for this row's kind, or Stop using it: the system is told on a worker."""
        if self._busy:
            return
        config, kind = self._config, self.kind
        state = file_types.status(kind, config)
        work: Callable[[], object]
        if state is Status.REGISTERED:
            work = lambda: (False, file_types.unregister(kind, config))  # noqa: E731
        elif state is Status.NOT_REGISTERED:
            work = lambda: (True, file_types.register(kind, config))  # noqa: E731
        else:
            return
        self._busy = True
        self.button.setEnabled(False)
        self._answer.run(work, PRESS_THREAD)

    def _answered(self, answer: object) -> None:
        """On the GUI thread: saved when it worked (the service recorded it), the window's DDE
        following it (Windows: a double-click reaches this QiTV while any kind is registered,
        or no more), and Windows' Default Apps opened after a register; the row follows while
        Settings is still there."""
        self._busy = False
        registering, result = answer if isinstance(answer, tuple) else (False, None)
        if result:
            _save(self._config)
            follow = getattr(self._window, "dde_follow_registration", None)
            if follow is not None and isValid(self._window):
                follow(file_types.any_registered(self._config))
            if registering and _system() == "win32":
                open_default_apps()  # where Windows asks the user to confirm
        if not (isValid(self.button) and isValid(self.hint)):
            return  # Settings is gone
        self._failed = not result
        if result and registering:
            self._open_with_only = not getattr(result, "defaults", True)
        self.show()


def add_file_types_row(
    dialog: Any, layout: QFormLayout, parent: QWidget, kind: str = "videos"
) -> FileTypesRow:
    """`kind`'s row, at the end of `layout`: `dialog.file_types_button`, `file_types_hint`
    and so on for videos, `dialog.music_file_types_button` ... for music."""
    row = FileTypesRow(dialog, layout, parent, kind)
    prefix = _PREFIXES[kind][0]
    setattr(dialog, f"{prefix}_row", row)
    setattr(dialog, f"{prefix}_button", row.button)
    setattr(dialog, f"{prefix}_hint", row.hint)
    setattr(dialog, f"{prefix}_default_apps_button", row.default_apps_button)
    return row


def refresh_at_start(config: Any, parent: QObject, frozen: Optional[bool] = None) -> None:
    """At start, on a thread of its own: a QiTV that was moved or renamed, or registered with
    other file types (2.11.1), registers itself again where it is now (`file_types.refresh`);
    the config is saved here once that changed the record. Nothing from source, nor with
    nothing registered."""
    packaged = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if not packaged or not config.file_types:
        return

    def work() -> bool:
        def record() -> tuple[dict, dict]:
            return dict(config.file_types), dict(getattr(config, "file_types_registered", {}))

        before = record()
        file_types.refresh(config, frozen=packaged)
        return record() != before

    answer = _Answer(parent, lambda changed: _save(config) if changed else None)
    answer.run(work, REFRESH_THREAD)
