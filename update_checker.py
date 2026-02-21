import logging
from pathlib import Path
import platform
import subprocess
import sys
from typing import List, Optional, Tuple

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import QApplication, QMessageBox, QProgressDialog
from packaging.version import parse
import requests

from config_manager import get_app_version

logger = logging.getLogger(__name__)

# Platform-specific asset name patterns
ASSET_PATTERNS = {
    "Windows": "qitv-windows.exe",
    "Darwin": "qitv-macos",  # Will match qitv-macos-universal.zip or qitv-macos-intel.zip
    "Linux": "qitv-linux",
}

# Keep references to background threads/workers to avoid premature GC
_update_jobs: List[Tuple[QThread, "UpdateWorker"]] = []


class UpdateWorker(QObject):
    finished = Signal(object)
    error = Signal(str)

    def run(self):
        repo = "ozankaraali/QiTV"
        api_url = f"https://api.github.com/repos/{repo}/releases/latest"
        try:
            response = requests.get(api_url, timeout=5)
            response.raise_for_status()
            latest_release = response.json()
            latest_version = extract_version_from_tag(latest_release.get("name", ""))
            if latest_version and compare_versions(latest_version, get_app_version()):
                assets = latest_release.get("assets", [])
                download_url, file_size = get_download_url_for_platform(assets)
                self.finished.emit(
                    {
                        "version": latest_version,
                        "url": latest_release.get("html_url", ""),
                        "download_url": download_url,
                        "file_size": file_size,
                    }
                )
            else:
                self.finished.emit(None)
        except requests.RequestException as e:
            self.error.emit(str(e))


def check_for_updates():
    # Run update check in a worker thread to avoid blocking UI
    thread = QThread()
    worker = UpdateWorker()
    worker.moveToThread(thread)

    def on_started():
        worker.run()

    def on_finished(result):
        thread.quit()
        if result:
            # Ensure dialog runs on the GUI thread by targeting the app object
            app = QApplication.instance()
            if app is not None:
                QTimer.singleShot(0, app, lambda: show_update_dialog(result))

    def on_error(msg):
        thread.quit()
        logger.warning(f"Error checking for updates: {msg}")

    def _cleanup():
        try:
            _update_jobs.remove((thread, worker))
        except ValueError:
            pass

    thread.started.connect(on_started)
    worker.finished.connect(on_finished, Qt.QueuedConnection)
    worker.error.connect(on_error, Qt.QueuedConnection)
    thread.finished.connect(_cleanup)
    thread.start()
    _update_jobs.append((thread, worker))


def extract_version_from_tag(tag):
    # Handle common tag formats like 'v1.2.3', 'release-1.2.3', or '1.2.3'
    import re

    match = re.search(r"(\d+\.\d+\.\d+)", tag)
    if match:
        return match.group(0)
    else:
        logger.info(f"Unexpected version format found: '{tag}'")
        return None


def compare_versions(latest_version, current_version):
    return parse(latest_version) > parse(current_version)


def get_download_url_for_platform(assets: list) -> Tuple[Optional[str], int]:
    """Returns (download_url, file_size) for the current platform's asset."""
    system = platform.system()
    pattern = ASSET_PATTERNS.get(system)

    if not pattern:
        return None, 0

    if system == "Darwin":
        machine = platform.machine()
        # Intel macs get intel build, ARM macs get universal build
        preferred_suffix = "intel" if machine == "x86_64" else "universal"

        for asset in assets:
            name = asset.get("name", "")
            if pattern in name and preferred_suffix in name:
                return asset.get("browser_download_url"), asset.get("size", 0)

        # Fallback: any macOS build
        for asset in assets:
            name = asset.get("name", "")
            if pattern in name:
                return asset.get("browser_download_url"), asset.get("size", 0)
    else:
        for asset in assets:
            name = asset.get("name", "")
            if name == pattern or name.startswith(pattern):
                return asset.get("browser_download_url"), asset.get("size", 0)

    return None, 0


def get_downloads_folder() -> Path:
    """Get the user's Downloads folder path."""
    return Path.home() / "Downloads"


class UpdateDownloader(QObject):
    """Downloads update file in background thread with progress reporting."""

    progress = Signal(int, int)  # bytes_downloaded, total_bytes
    finished = Signal(str)  # path to downloaded file
    error = Signal(str)

    def __init__(self, url: str, expected_size: int):
        super().__init__()
        self.url = url
        self.expected_size = expected_size
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            # Extract filename from URL
            filename = self.url.split("/")[-1]
            download_path = get_downloads_folder() / filename

            # Use longer read timeout for large files
            response = requests.get(self.url, stream=True, timeout=(30, 120))
            response.raise_for_status()

            total_size = int(response.headers.get("content-length", self.expected_size))
            downloaded = 0
            last_progress_update = 0
            chunk_size = 65536  # 64KB chunks for better performance

            with open(download_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if self._cancelled:
                        f.close()
                        download_path.unlink(missing_ok=True)
                        self.error.emit("Download cancelled")
                        return

                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        # Throttle progress updates to every 500KB to avoid overwhelming Qt event loop
                        if (
                            downloaded - last_progress_update >= 512 * 1024
                            or downloaded >= total_size
                        ):
                            self.progress.emit(downloaded, total_size)
                            last_progress_update = downloaded

            # Verify download size
            if total_size > 0 and downloaded < total_size * 0.9:
                download_path.unlink(missing_ok=True)
                self.error.emit(f"Download incomplete: {downloaded}/{total_size} bytes")
                return

            self.finished.emit(str(download_path))

        except requests.RequestException as e:
            self.error.emit(f"Download failed: {e}")
        except OSError as e:
            self.error.emit(f"Failed to save file: {e}")


def show_update_dialog(update_info: dict):
    """Show update dialog with download option if available."""
    version = update_info["version"]
    release_url = update_info["url"]
    download_url = update_info.get("download_url")
    file_size = update_info.get("file_size", 0)

    # Check if we're running as a frozen executable
    is_frozen = getattr(sys, "frozen", False)

    msg = QMessageBox()
    msg.setIcon(QMessageBox.Information)
    msg.setText(f"A new version ({version}) is available!")
    msg.setWindowTitle("Update Available")

    if download_url and is_frozen:
        msg.setInformativeText("Would you like to download and install the update?")
        download_btn = msg.addButton("Download && Install", QMessageBox.AcceptRole)
        browser_btn = msg.addButton("Open Release Page", QMessageBox.ActionRole)
        msg.addButton(QMessageBox.Cancel)

        msg.exec_()
        clicked = msg.clickedButton()

        if clicked == download_btn:
            start_download(download_url, file_size, release_url)
        elif clicked == browser_btn:
            import webbrowser

            webbrowser.open(release_url)
    else:
        # Fallback: running from source or no download URL available
        msg.setInformativeText("Would you like to open the release page?")
        msg.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        if msg.exec_() == QMessageBox.Yes:
            import webbrowser

            webbrowser.open(release_url)


# Keep reference to download jobs to avoid GC
_download_jobs: List[Tuple[QThread, UpdateDownloader, QProgressDialog]] = []


def start_download(download_url: str, file_size: int, release_url: str):
    """Start downloading the update with a progress dialog."""
    progress = QProgressDialog("Downloading update...", "Cancel", 0, 100)
    progress.setWindowTitle("Downloading Update")
    progress.setMinimumDuration(0)
    progress.setValue(0)

    thread = QThread()
    downloader = UpdateDownloader(download_url, file_size)
    downloader.moveToThread(thread)

    # QTimer.singleShot(0, app, fn) ensures fn runs on the main/GUI thread.
    # Signals from a blocking worker delivered via QueuedConnection to Python
    # closures lack thread affinity and may run on the worker thread, which
    # crashes macOS (NSWindow operations must be on the Main Thread).
    app = QApplication.instance()

    def on_progress(downloaded: int, total: int):
        def _update():
            if total > 0:
                percent = int(downloaded * 100 / total)
                progress.setValue(percent)
                mb_downloaded = downloaded / (1024 * 1024)
                mb_total = total / (1024 * 1024)
                progress.setLabelText(
                    f"Downloading update... {mb_downloaded:.1f} / {mb_total:.1f} MB"
                )

        if app:
            QTimer.singleShot(0, app, _update)

    def on_finished(path: str):
        def _handle():
            thread.quit()
            progress.close()
            perform_update(path, release_url)
            _cleanup()

        if app:
            QTimer.singleShot(0, app, _handle)

    def on_error(error_msg: str):
        def _handle():
            thread.quit()
            progress.close()
            _cleanup()
            if "cancelled" not in error_msg.lower():
                _show_download_error(error_msg, release_url)

        if app:
            QTimer.singleShot(0, app, _handle)

    def on_cancelled():
        downloader.cancel()

    def _cleanup():
        try:
            _download_jobs.remove((thread, downloader, progress))
        except ValueError:
            pass

    thread.started.connect(downloader.run)
    downloader.progress.connect(on_progress)
    downloader.finished.connect(on_finished)
    downloader.error.connect(on_error)
    progress.canceled.connect(on_cancelled)

    _download_jobs.append((thread, downloader, progress))
    thread.start()
    progress.show()


def _show_download_error(error_msg: str, release_url: str):
    """Show download error with option to open release page."""
    import webbrowser

    error_dialog = QMessageBox()
    error_dialog.setIcon(QMessageBox.Warning)
    error_dialog.setWindowTitle("Download Failed")
    error_dialog.setText("Failed to download update.")
    error_dialog.setInformativeText(error_msg)
    open_btn = error_dialog.addButton("Open Release Page", QMessageBox.ActionRole)
    error_dialog.addButton(QMessageBox.Close)

    error_dialog.exec_()
    if error_dialog.clickedButton() == open_btn:
        webbrowser.open(release_url)


def perform_update(downloaded_path: str, release_url: str):
    """Execute platform-specific update process."""
    system = platform.system()

    if system == "Windows":
        _perform_windows_update(downloaded_path, release_url)
    else:
        _perform_unix_update(downloaded_path)


def _perform_windows_update(downloaded_path: str, release_url: str):
    """Windows: launch new exe with --replace flag, then quit."""
    import webbrowser

    # Get the path to the original executable (not the temp extraction folder)
    # For PyInstaller, sys.executable points to the original .exe file
    original_exe = sys.executable

    try:
        # Launch the downloaded exe with --replace flag
        subprocess.Popen(
            [downloaded_path, "--replace", original_exe],
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,  # type: ignore[attr-defined]
        )

        # Quit the current application
        app = QApplication.instance()
        if app:
            app.quit()

    except OSError as e:
        logger.error(f"Failed to launch update: {e}")
        error_dialog = QMessageBox()
        error_dialog.setIcon(QMessageBox.Warning)
        error_dialog.setWindowTitle("Update Failed")
        error_dialog.setText("Failed to launch the update.")
        error_dialog.setInformativeText(f"The update was downloaded to:\n{downloaded_path}")
        open_btn = error_dialog.addButton("Open Release Page", QMessageBox.ActionRole)
        error_dialog.addButton(QMessageBox.Close)

        error_dialog.exec_()
        if error_dialog.clickedButton() == open_btn:
            webbrowser.open(release_url)


def _perform_unix_update(downloaded_path: str):
    """macOS/Linux: open Downloads folder for manual replacement."""
    downloads_folder = get_downloads_folder()

    msg = QMessageBox()
    msg.setIcon(QMessageBox.Information)
    msg.setWindowTitle("Update Downloaded")
    msg.setText("Update downloaded successfully!")
    msg.setInformativeText(
        f"The new version has been saved to:\n{downloaded_path}\n\n"
        "Please replace the current application with the downloaded file."
    )
    msg.exec_()

    # Open the Downloads folder
    try:
        if platform.system() == "Darwin":
            subprocess.run(["open", str(downloads_folder)], check=False)
        else:
            subprocess.run(["xdg-open", str(downloads_folder)], check=False)
    except OSError:
        pass  # Folder opening is best-effort
