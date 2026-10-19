"""The update's dialogs: the prompt, its progress and its errors (services/update_checker.py
does the checking, downloading and launching).

`check_for_updates` is what the app calls (at startup, Help → Check for Updates, Settings):
the service checks on a QThread and reports to `DIALOGS`.
"""

import logging
import sys
from typing import Optional
import webbrowser

from PySide6.QtWidgets import QCheckBox, QGridLayout, QMessageBox, QProgressDialog

from services import i18n, update_checker
from services.config_manager import ConfigManager, get_app_version
from services.i18n import tr

logger = logging.getLogger(__name__)


def check_for_updates(config_manager: Optional[ConfigManager] = None, manual: bool = False):
    """Check for updates in the background; `manual` also says when there is none."""
    update_checker.check_for_updates(config_manager, manual, DIALOGS)


class _Dialogs:
    """The update service's `UpdateUI`."""

    def up_to_date(self) -> None:
        msg = QMessageBox()
        msg.setIcon(QMessageBox.Information)
        msg.setWindowTitle(tr("No Updates"))  # i18n: a message box's title
        msg.setText(tr("You're running the latest version ({version}).", version=get_app_version()))
        _polish_msgbox(msg)
        msg.exec_()

    def check_failed(self, message: str) -> None:
        msg = QMessageBox()
        msg.setIcon(QMessageBox.Warning)
        msg.setWindowTitle(tr("Update Check Failed"))
        msg.setText(tr("Could not check for updates."))
        msg.setInformativeText(message)  # the network's own error, as it came
        _polish_msgbox(msg)
        msg.exec_()

    def update_available(self, info: dict, config_manager, manual: bool) -> None:
        show_update_dialog(info, config_manager, manual)

    def launch_failed(self, downloaded_path: str, release_url: str) -> None:
        error_dialog = QMessageBox()
        error_dialog.setIcon(QMessageBox.Warning)
        error_dialog.setWindowTitle(tr("Update Failed"))
        error_dialog.setText(tr("Failed to launch the update."))
        error_dialog.setInformativeText(
            tr("The update was downloaded to:\n{path}", path=downloaded_path)
        )
        # i18n: a button: opens the release's web page
        open_btn = error_dialog.addButton(tr("Open Release Page"), QMessageBox.ActionRole)
        error_dialog.addButton(QMessageBox.Close)
        _polish_msgbox(error_dialog)

        error_dialog.exec_()
        if error_dialog.clickedButton() == open_btn:
            webbrowser.open(release_url)

    def downloaded(self, downloaded_path: str) -> None:
        """macOS/Linux: say where the update is, then open the Downloads folder."""
        msg = QMessageBox()
        msg.setIcon(QMessageBox.Information)
        msg.setWindowTitle(tr("Update Downloaded"))
        msg.setText(tr("Update downloaded successfully!"))
        msg.setInformativeText(
            tr(
                "The new version has been saved to:\n{path}\n\n"
                "Please replace the current application with the downloaded file.",
                path=downloaded_path,
            )
        )
        _polish_msgbox(msg)
        msg.exec_()
        update_checker.open_downloads_folder()


DIALOGS = _Dialogs()


def _polish_msgbox(msg: QMessageBox):
    """Reduce icon-to-text spacing and ensure buttons are not truncated.

    Button sizing is deferred to the first show event so that font metrics
    are fully resolved by the platform style.
    """
    layout = msg.layout()
    if layout:
        layout.setHorizontalSpacing(10)
        layout.setContentsMargins(10, 10, 10, 10)
        if isinstance(layout, QGridLayout):
            layout.setColumnMinimumWidth(0, 0)
            layout.setColumnStretch(0, 0)
            layout.setColumnStretch(1, 1)

    # Defer button width calculation until after the dialog is laid out,
    # so font metrics are resolved by the platform style engine.
    original_show = msg.showEvent

    def _on_show(event):
        original_show(event)
        total_buttons_width = 0
        for btn in msg.buttons():
            fm = btn.fontMetrics()
            # Strip & accelerator markers for accurate text measurement
            display_text = btn.text().replace("&&", "\x00").replace("&", "").replace("\x00", "&")
            text_width = fm.horizontalAdvance(display_text) + 32
            btn.setMinimumWidth(text_width)
            total_buttons_width += text_width + 8  # 8px spacing between buttons

        min_dialog_width = max(400, total_buttons_width + 80)
        msg.setMinimumWidth(min_dialog_width)

    msg.showEvent = _on_show  # type: ignore[method-assign]


def show_update_dialog(
    update_info: dict,
    config_manager: Optional[ConfigManager] = None,
    manual: bool = False,
):
    """Show update dialog with download option if available.

    Buttons:
        - Download & Install  (frozen builds with a download URL)
        - Open Release Page
        - Skip This Version   (records the version so automatic checks won't show it again)

    Checkbox:
        - "Do not remind me about updates"  — if checked when the dialog is
          dismissed via *Skip This Version*, automatic update checks are
          disabled entirely (not just for this version).

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

    msg = QMessageBox()
    msg.setIcon(QMessageBox.Information)
    msg.setText(tr("A new version ({version}) is available!", version=version))
    msg.setWindowTitle(tr("Update Available"))

    # "Do not remind me" checkbox — only shown for automatic checks
    mute_checkbox: Optional[QCheckBox] = None
    if not manual and config_manager is not None:
        mute_checkbox = QCheckBox(tr("Do not remind me about updates"))
        msg.setCheckBox(mute_checkbox)

    download_btn = None
    if download_url and is_frozen:
        msg.setInformativeText(tr("Would you like to download and install the update?"))
        # i18n: a button; "&&" shows as "&" (a single "&" would underline the next letter)
        download_btn = msg.addButton(tr("Download && Install"), QMessageBox.AcceptRole)
        browser_btn = msg.addButton(tr("Open Release Page"), QMessageBox.ActionRole)
    else:
        # Fallback: running from source or no download URL available
        msg.setInformativeText(tr("Would you like to open the release page?"))
        browser_btn = msg.addButton(tr("Open Release Page"), QMessageBox.AcceptRole)
    skip_btn = msg.addButton(tr("Skip This Version"), QMessageBox.RejectRole)  # i18n: a button

    _polish_msgbox(msg)
    msg.exec_()
    clicked = msg.clickedButton()

    if download_btn is not None and clicked == download_btn:
        # Clear any previous skip for this version so it doesn't block
        # future checks after the download is complete / cancelled.
        if config_manager:
            config_manager.skipped_update_version = ""
            update_checker.save_settings(config_manager)  # a locked file does not stop it
        start_download(download_url, file_size, release_url)
    elif clicked == browser_btn:
        webbrowser.open(release_url)
    elif clicked == skip_btn:
        if mute_checkbox and mute_checkbox.isChecked():
            update_checker.disable_update_reminders(config_manager)
        else:
            update_checker.skip_version(config_manager, version)


# The progress dialogs of downloads under way (kept from the garbage collector).
_progress_dialogs: list[QProgressDialog] = []


def start_download(download_url: str, file_size: int, release_url: str):
    """Download the update with a progress dialog; then launch it, or say why not."""
    progress = QProgressDialog(tr("Downloading update..."), tr("Cancel"), 0, 100)
    progress.setWindowTitle(tr("Downloading Update"))
    progress.setMinimumDuration(0)
    progress.setValue(0)
    _progress_dialogs.append(progress)

    def on_progress(downloaded: int, total: int):
        if progress.wasCanceled() or total <= 0:
            return
        progress.setValue(int(downloaded * 100 / total))
        mb_downloaded = downloaded / (1024 * 1024)
        mb_total = total / (1024 * 1024)
        progress.setLabelText(
            tr(
                # i18n: done and total are megabytes, as "12.5"
                "Downloading update... {done} / {total} MB",
                done=i18n.decimal(f"{mb_downloaded:.1f}"),
                total=i18n.decimal(f"{mb_total:.1f}"),
            )
        )

    def done():
        progress.canceled.disconnect(cancel)
        progress.close()
        _progress_dialogs.remove(progress)

    def on_finished(path: str):
        done()
        update_checker.perform_update(path, release_url, DIALOGS)

    def on_failed(message: str):
        done()
        if "cancelled" not in message.lower():  # the English DOWNLOAD_CANCELLED: never shown
            _show_download_error(message, release_url)

    cancel = update_checker.start_download(
        download_url, file_size, progress=on_progress, finished=on_finished, failed=on_failed
    )
    progress.canceled.connect(cancel)
    progress.show()


def _show_download_error(error_msg: str, release_url: str):
    """Show download error with option to open release page."""
    error_dialog = QMessageBox()
    error_dialog.setIcon(QMessageBox.Warning)
    error_dialog.setWindowTitle(tr("Download Failed"))
    error_dialog.setText(tr("Failed to download update."))
    error_dialog.setInformativeText(error_msg)
    open_btn = error_dialog.addButton(tr("Open Release Page"), QMessageBox.ActionRole)
    error_dialog.addButton(QMessageBox.Close)
    _polish_msgbox(error_dialog)

    error_dialog.exec_()
    if error_dialog.clickedButton() == open_btn:
        webbrowser.open(release_url)
