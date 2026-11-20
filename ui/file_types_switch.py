"""Settings › Files › Open videos with QiTV, and Open music with QiTV (the drawer's rows,
ui.settings_general): a kind's press registers QiTV with the system for its kind of file, or
takes it back (services.file_types), on a thread of its own; the row's help says what the
system does then. Each kind follows its own status. A kind's press and its answer are a
`FileTypesSwitch`'s. At start, a QiTV that was moved registers itself again where it is now
(`refresh_at_start`).

The service only records what it did in the config; it is saved here, on the GUI thread."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import sys
import threading
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, Qt, Signal, Slot
from shiboken6 import isValid

from services import file_types
from services.file_types import Status
from services.i18n import N_
from services.redact import safe_message

logger = logging.getLogger(__name__)

PRESS_THREAD = "qitv-file-types"
REFRESH_THREAD = "qitv-file-types-refresh"
DEFAULT_APPS_THREAD = "qitv-default-apps"

# Each row's title (Settings → Files → Open videos with QiTV) and the hints that are its help;
# the music row's say "music" where the videos' say "videos".
# i18n: a setting (General): QiTV opens video files, as with a double-click on one
VIDEOS_LABEL = N_("Open videos with QiTV")
# i18n: a setting (General): QiTV opens music files (songs), as with a double-click on one
MUSIC_LABEL = N_("Open music with QiTV")
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
    mac_hint: str
    linux_hint: str


TEXTS = {
    "videos": RowTexts(VIDEOS_LABEL, MAC_HINT, LINUX_HINT),
    "music": RowTexts(MUSIC_LABEL, MUSIC_MAC_HINT, MUSIC_LINUX_HINT),
}


def _system() -> str:
    """The system the hints are for: "win32", "darwin" or "linux"."""
    return "linux" if sys.platform.startswith("linux") else sys.platform


def hint_source(
    state: Status, system: str, open_with_only: bool = False, kind: str = "videos"
) -> str:
    """The help of `kind`'s row for `state` on `system`, in English as marked (N_; "": none),
    translated where it shows: what the system does on a press, or what must be done first.
    `open_with_only`: this QiTV registered itself on Linux without being made the default (no
    xdg-mime)."""
    if state is Status.REGISTERED and system == "linux" and open_with_only:
        return LINUX_OPEN_WITH_HINT
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
    return hint or ""


def open_default_apps() -> None:
    """Windows' Default Apps page (file_types.open_default_apps), on a thread of its own:
    os.startfile is ShellExecute, which can keep the calling thread waiting."""

    def body() -> None:
        try:
            file_types.open_default_apps()
        except Exception:
            logger.warning("Windows' Default Apps page didn't open", exc_info=True)

    threading.Thread(target=body, name=DEFAULT_APPS_THREAD, daemon=True).start()


def default_apps_offered(config: Any) -> bool:
    """Whether Windows' Default Apps… is offered: on Windows, once QiTV opens a kind of file."""
    return _system() == "win32" and any(
        file_types.status(kind, config) is Status.REGISTERED for kind in TEXTS
    )


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
            except Exception:  # never unanswered: the switch would stay busy
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


class FileTypesSwitch:
    """A kind's Use QiTV and Stop using it (`press`): the system is told on a thread of its
    own, and its answer heard on the GUI thread, through `parent` (an answer after it went is
    dropped: keep it as long as a press may be on its way). The config is saved when it worked,
    the window's DDE follows it (Windows: a double-click reaches this QiTV while any kind is
    registered, or no more) and Windows' Default Apps opens after a register, where Windows asks
    to confirm; then `answered` is called, for the row to follow. `busy` while an answer is on
    its way, `failed` when the last press failed, `open_with_only` when this switch registered
    QiTV on Linux without making it the default (no xdg-mime)."""

    def __init__(
        self,
        config: Any,
        window: Any,
        kind: str,
        parent: QObject,
        answered: Callable[[], None] = lambda: None,
    ) -> None:
        self.kind = kind
        self._config = config
        self._window = window  # TVWindow: its DDE follows a press (Windows)
        self._then = answered
        self.busy = False
        self.failed = False
        self.open_with_only = False
        self._answer = _Answer(parent, self._answered)

    def status(self) -> Status:
        """The kind's status, from the record (no disk, registry or program is touched)."""
        return file_types.status(self.kind, self._config)

    def hint(self, state: Status) -> str:
        """The row's help for `state`, in English as marked (N_; "": none): what the system
        does on a press, or what must be done first."""
        return hint_source(state, _system(), self.open_with_only, self.kind)

    def press(self) -> bool:
        """Use QiTV for the kind, or stop using it: whether the system is being told (not while
        a press is on its way, nor for a status that can't be pressed)."""
        if self.busy:
            return False
        config, kind = self._config, self.kind
        state = file_types.status(kind, config)
        work: Callable[[], object]
        if state is Status.REGISTERED:
            work = lambda: (False, file_types.unregister(kind, config))  # noqa: E731
        elif state is Status.NOT_REGISTERED:
            work = lambda: (True, file_types.register(kind, config))  # noqa: E731
        else:
            return False
        self.busy = True
        self._answer.run(work, PRESS_THREAD)
        return True

    def _answered(self, answer: object) -> None:
        self.busy = False
        registering, result = answer if isinstance(answer, tuple) else (False, None)
        if result:
            _save(self._config)
            follow = getattr(self._window, "dde_follow_registration", None)
            if follow is not None and isValid(self._window):
                follow(file_types.any_registered(self._config))
            if registering and _system() == "win32":
                open_default_apps()  # where Windows asks the user to confirm
        self.failed = not result
        if result and registering:
            self.open_with_only = not getattr(result, "defaults", True)
        self._then()


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
