"""The update's questions, its messages, its download's progress and its errors
(services/update_checker.py does the checking, downloading and launching).

`check_for_updates` is what the app calls (at startup, Help → Check for Updates, Settings):
the service checks on a QThread and reports to `DIALOGS` (the startup check's question waits
for a film opened in QiTV: `_AfterTheFilm`). 2.13.1: each question and message, and the
download's progress, is QiTV's own card over the window (ui.window_cards: the message box's
title, words and buttons, the main action accented), answered through a callback: nothing
waits in an exec(). Esc, Back and a click on the dimmed picture are the safe answer: Not now
(the update is offered again at the next check; never Skip This Version), Keep recording,
Close; on the download's progress they only take the card away.
"""

import logging
import os
import sys
from typing import Callable, Optional

from PySide6.QtCore import QTimer

from services import i18n, system_open, update_checker
from services.config_manager import ConfigManager, get_app_version
from services.i18n import N_, tr, translate
from ui import window_cards
from ui.overlays.settings_card import Button

logger = logging.getLogger(__name__)

WhenIdle = Callable[[Callable[[], None]], None]  # TVWindow.when_idle

# The cards' answers, by key.
DOWNLOAD, RELEASE, SKIP, MUTE = "download", "release", "skip", "mute"
UPDATE_NOW, NOT_NOW, KEEP, QUIT_NOW, CLOSE = "update", "not_now", "keep", "quit", "close"
STOP, HIDE = "stop", "hide"  # the download's Cancel; its Back (the card goes, it goes on)
CLOSE_TEXT = N_("Close")


def check_for_updates(
    config_manager: Optional[ConfigManager] = None,
    manual: bool = False,
    when_idle: Optional[WhenIdle] = None,
):
    """Check for updates in the background; `manual` also says when there is none.

    `when_idle` (the startup check: TVWindow.when_idle): the question waits while a file opened
    in QiTV plays or is on its way, however late the network answers. A check the user asks
    for (Help → Check for Updates) answers at once."""
    ui = DIALOGS if when_idle is None else _AfterTheFilm(when_idle)
    update_checker.check_for_updates(config_manager, manual, ui)


def _lines(*parts: str) -> str:
    """A message box's text and informative text, as the card's words (one under another)."""
    return "\n".join(part for part in parts if part)


def _button_text(text: str) -> str:
    """A translated button's text as painted: "&&" is the "&" a Qt button showed."""
    return text.replace("&&", "&")


def _offer_release_page(title: str, body: str, release_url: str) -> None:
    """A failure, with Open Release Page (the accent) and Close."""
    buttons = [
        Button(CLOSE, translate(CLOSE_TEXT)),
        # i18n: a button: opens the release's web page
        Button(RELEASE, tr("Open Release Page"), accent=True),
    ]

    def answered(key: str) -> None:
        if key == RELEASE:
            system_open.open_url(release_url)

    window_cards.ask(title, body, buttons, RELEASE, answered, back=CLOSE)


class _Dialogs:
    """The update service's `UpdateUI`."""

    def up_to_date(self) -> None:
        window_cards.tell(
            tr("No Updates"),  # i18n: a message box's title
            tr("You're running the latest version ({version}).", version=get_app_version()),
        )

    def check_failed(self, message: str) -> None:
        window_cards.tell(
            tr("Update Check Failed"),
            # the network's own error, as it came
            _lines(tr("Could not check for updates."), message),
        )

    def update_available(self, info: dict, config_manager, manual: bool) -> None:
        show_update_dialog(info, config_manager, manual)

    def launch_failed(self, downloaded_path: str, release_url: str) -> None:
        body = _lines(
            tr("Failed to launch the update."),
            tr("The update was downloaded to:\n{path}", path=downloaded_path),
        )
        _offer_release_page(tr("Update Failed"), body, release_url)

    def confirm_quit_and_open(self, then: Callable[[bool], None]) -> None:
        """macOS: QiTV closes and opens the downloaded disk image (Update Now, the default), or
        not now (Back too): `then(yes)`."""
        body = _lines(
            tr("QiTV will close and open the update."),
            tr(
                # i18n: Applications, Replace: as Finder names them; Not Now: this dialog's button
                "In the window that opens, drag QiTV onto Applications and choose Replace.\n\n"
                "Not Now keeps this version; QiTV offers the update again at its next check."
            ),
        )
        buttons = [
            Button(NOT_NOW, tr("Not Now")),  # i18n: a button
            # i18n: a button: QiTV closes and opens the downloaded update
            Button(UPDATE_NOW, tr("Update Now"), accent=True),
        ]
        window_cards.ask(
            tr("Update Downloaded"),
            body,
            buttons,
            UPDATE_NOW,
            lambda key: then(key == UPDATE_NOW),
            back=NOT_NOW,
        )

    def confirm_stop_recordings(self, question: str, then: Callable[[bool], None]) -> None:
        """2.6: channels record: the update quits and stops them (their files are saved; Quit,
        in red), or not now (Keep recording, the default, and Back): `then(quit)`."""
        from ui.stream_question import KEEP_RECORDING, QUIT

        buttons = [
            Button(KEEP, translate(KEEP_RECORDING)),
            Button(QUIT_NOW, translate(QUIT), danger=True),
        ]
        window_cards.ask(
            tr("Update Downloaded"),
            question,
            buttons,
            KEEP,
            lambda key: then(key == QUIT_NOW),
            back=KEEP,
        )

    def downloaded(self, downloaded_path: str) -> None:
        """A zip on macOS, or Linux: say where the update is, then open its folder."""
        body = _lines(
            tr("Update downloaded successfully!"),
            tr(
                "The new version has been saved to:\n{path}\n\n"
                "Please replace the current application with the downloaded file.",
                path=downloaded_path,
            ),
        )
        folder = os.path.dirname(downloaded_path)

        def read(_key: str) -> None:
            system_open.open_folder(folder)

        window_cards.tell(tr("Update Downloaded"), body, read)


DIALOGS = _Dialogs()


class _AfterTheFilm(_Dialogs):
    """The startup check's dialogs: its update question never opens over a film opened in
    QiTV. While one plays or is on its way (`when_idle` holds), the question waits for it to be
    over, then comes from the event loop: never inside the release itself, which may come in
    the middle of a tune (a channel replacing the film), and once more after a file opened
    meanwhile (a double-click on the next episode)."""

    def __init__(self, when_idle: WhenIdle) -> None:
        self._when_idle = when_idle

    def update_available(self, info: dict, config_manager, manual: bool) -> None:
        self._when_still_idle(lambda: DIALOGS.update_available(info, config_manager, manual))

    def _when_still_idle(self, show: Callable[[], None]) -> None:
        """`show` now when nothing is held; else once the held file is over, from the event
        loop and asked again then."""
        waiting = False

        def ready() -> None:
            if waiting:  # the release runs it: the event loop shows it, if still idle then
                QTimer.singleShot(0, lambda: self._when_still_idle(show))
            else:
                show()

        self._when_idle(ready)  # at once when nothing is held
        waiting = True


def replace_failed(old_path: str, new_path: str) -> None:
    """Windows: the new version runs, but could not take the old one's place (the old QiTV
    still held its file after a while, or it cannot be written): where each one is."""
    body = _lines(
        tr("The update could not replace the old version of QiTV."),
        tr(
            # i18n: {new} and {old} are file paths, each on a line of its own
            "This version runs from:\n{new}\n\n"
            "Use that file from now on, or close QiTV and copy it over the old one:\n{old}",
            new=new_path,
            old=old_path,
        ),
    )
    window_cards.tell(tr("Update Failed"), body)


def show_update_dialog(
    update_info: dict,
    config_manager: Optional[ConfigManager] = None,
    manual: bool = False,
):
    """The update offered, in a card (its answer through a callback: nothing waits).

    Buttons:
        - Download & Install  (frozen builds with a download URL; the accent)
        - Open Release Page   (the accent without a download)
        - Skip This Version   (records the version so automatic checks won't show it again)

    The other way, under them (automatic checks only):
        - "Do not remind me about updates": automatic update checks are disabled entirely
          (the old box's check box with Skip This Version).

    Esc, Back and the dimmed picture: Not now, nothing changes (offered again at the next
    check); skipping is a choice of its own.

    Args:
        update_info: Dict with keys ``version``, ``url``, and optionally
                     ``download_url`` / ``file_size``.
        config_manager: Persists skip-version and reminder preferences.
        manual: True when the user triggered the check explicitly.
    """
    version = update_info["version"]
    release_url = update_info["url"]
    download_url = update_info.get("download_url") or ""
    file_size = update_info.get("file_size", 0)

    # Check if we're running as a frozen executable
    is_frozen = getattr(sys, "frozen", False)
    can_download = bool(download_url and is_frozen)

    text = tr("A new version ({version}) is available!", version=version)
    # i18n: a button
    buttons = [Button(SKIP, tr("Skip This Version"))]
    if can_download:
        question = tr("Would you like to download and install the update?")
        buttons += [
            Button(RELEASE, tr("Open Release Page")),
            Button(
                DOWNLOAD,
                # i18n: a button; "&&" shows as "&" (a single "&" would underline the next letter)
                _button_text(tr("Download && Install")),
                accent=True,
            ),
        ]
    else:
        # Fallback: running from source or no download URL available
        question = tr("Would you like to open the release page?")
        buttons.append(Button(RELEASE, tr("Open Release Page"), accent=True))
    # "Do not remind me" — only offered for automatic checks
    alternates = []
    if not manual and config_manager is not None:
        alternates.append(Button(MUTE, tr("Do not remind me about updates")))

    def answered(key: str) -> None:
        if key == DOWNLOAD:
            # Clear any previous skip for this version so it doesn't block
            # future checks after the download is complete / cancelled.
            if config_manager:
                config_manager.skipped_update_version = ""
                update_checker.save_settings(config_manager)  # a locked file does not stop it
            start_download(download_url, file_size, release_url)
        elif key == RELEASE:
            system_open.open_url(release_url)
        elif key == SKIP:
            update_checker.skip_version(config_manager, version)
        elif key == MUTE:
            update_checker.disable_update_reminders(config_manager)
        # NOT_NOW (Esc, Back, the dimmed picture): nothing; offered again at the next check

    window_cards.ask(
        tr("Update Available"),
        _lines(text, question),
        buttons,
        DOWNLOAD if can_download else RELEASE,
        answered,
        alternates=alternates,
        back=NOT_NOW,
    )


def start_download(download_url: str, file_size: int, release_url: str):
    """Download the update, its progress in a card (2.13.1: QiTV's own, over the page that
    shows): the bar, how much came, and Cancel, which stops it. Back (Esc, the dimmed picture)
    only takes the card away: the download goes on, and what comes after it shows once it is
    done. Then launch it, or say why not."""
    cancel: Callable[[], None] = lambda: None

    def answered(key: str) -> None:
        if key == STOP:
            cancel()

    progress = window_cards.progress(
        tr("Downloading Update"),
        tr("Downloading update..."),
        [Button(STOP, tr("Cancel"))],
        STOP,
        answered,
        back=HIDE,
    )

    def on_progress(downloaded: int, total: int):
        if total <= 0:
            return
        mb_downloaded = downloaded / (1024 * 1024)
        mb_total = total / (1024 * 1024)
        progress.set(
            downloaded / total,
            tr(
                # i18n: done and total are megabytes, as "12.5"
                "Downloading update... {done} / {total} MB",
                done=i18n.decimal(f"{mb_downloaded:.1f}"),
                total=i18n.decimal(f"{mb_total:.1f}"),
            ),
        )

    def on_finished(path: str):
        progress.close()
        update_checker.perform_update(path, release_url, DIALOGS)

    def on_failed(message: str):
        progress.close()
        if "cancelled" not in message.lower():  # the English DOWNLOAD_CANCELLED: never shown
            _show_download_error(message, release_url)

    cancel = update_checker.start_download(
        download_url, file_size, progress=on_progress, finished=on_finished, failed=on_failed
    )


def _show_download_error(error_msg: str, release_url: str):
    """Show download error with option to open release page."""
    body = _lines(tr("Failed to download update."), error_msg)
    _offer_release_page(tr("Download Failed"), body, release_url)
