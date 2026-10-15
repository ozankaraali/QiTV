"""Checking for, downloading and launching a QiTV update; no widgets (they are ui/'s).

`check_for_updates` asks GitHub for the latest release on a QThread, `start_download` fetches
it on another, and `perform_update` launches it: on Windows the new executable replaces this
one once the background work stopped (`call_before_update`); elsewhere the user replaces the
app. What the user sees (the update prompt, progress and errors) is the `UpdateUI` the caller
passes: ui/update_dialogs.py's dialogs.
"""

import logging
from pathlib import Path
import platform
import re
import subprocess
import sys
from typing import Callable, List, Optional, Protocol, Tuple, TypeAlias
import weakref

from PySide6.QtCore import QCoreApplication, QObject, Qt, QThread, QTimer, Signal, Slot
from packaging.version import parse

from services.config_manager import ConfigManager, get_app_version
from services.lazy_import import lazy_module
from services.redact import safe_message
from services.thread_cleanup import ThreadCleanup, has_pending_threads

logger = logging.getLogger(__name__)
requests = lazy_module("requests")

# Platform-specific asset name patterns
ASSET_PATTERNS = {
    "Windows": "qitv-windows.exe",
    "Darwin": "qitv-macos",  # Will match qitv-macos-universal.zip or qitv-macos-intel.zip
    "Linux": "qitv-linux",
}


class UpdateUI(Protocol):
    """What the user sees of an update (ui/update_dialogs.py)."""

    def up_to_date(self) -> None: ...  # a manual check found nothing newer

    def check_failed(self, message: str) -> None: ...  # a manual check failed

    def update_available(self, info: dict, config_manager, manual: bool) -> None: ...

    def launch_failed(self, downloaded_path: str, release_url: str) -> None: ...

    def downloaded(self, downloaded_path: str) -> None: ...  # replace the app by hand


# Keep references to background threads/workers to avoid premature GC
_update_jobs: List[Tuple[QThread, "UpdateWorker"]] = []
# Run before a self-update waits for background threads, and undone when its launch fails
# (weak: a closed window may go).
_Hook: TypeAlias = "weakref.WeakMethod[Callable[[], None]]"
_before_update: List[Tuple[_Hook, Optional[_Hook]]] = []
_undo_update: List[_Hook] = []


def call_before_update(
    method: Callable[[], None], undo: Optional[Callable[[], None]] = None
) -> None:
    """Run the bound `method` before a Windows self-update waits for background threads.

    For work whose thread stays up for the whole run until told to stop: the live
    time-shift engine's supervisor (services/live/engine.py) would make the wait endless.
    `undo` runs if the update then fails to launch and QiTV goes on. Each runs once: an
    owner that goes on registers again.
    """
    _before_update[:] = [hook for hook in _before_update if hook[0]() is not None]
    _before_update.append(
        (weakref.WeakMethod(method), weakref.WeakMethod(undo) if undo is not None else None)
    )


def _call(refs: List[_Hook], what: str) -> None:
    for ref in refs:
        method = ref()
        if method is None:
            continue
        try:
            method()
        except Exception:
            logger.exception("Could not %s", what)


def _run_before_update() -> None:
    hooks, _before_update[:] = list(_before_update), []
    _call([stop for stop, _undo in hooks], "prepare for the update")
    _undo_update.extend(undo for stop, undo in hooks if undo is not None and stop() is not None)


def _undo_before_update() -> None:
    hooks, _undo_update[:] = list(_undo_update), []
    _call(hooks, "go on after the failed update")


class UpdateWorker(QObject):
    finished = Signal(object)
    error = Signal(str)

    @Slot()
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


def check_for_updates(
    config_manager: Optional[ConfigManager] = None,
    manual: bool = False,
    ui: Optional[UpdateUI] = None,
):
    """Check for updates in background.

    Args:
        config_manager: Used to persist skip-version and reminder preferences.
        manual: If True, bypass skipped-version check and show "up to date"
                or error dialogs. Automatic checks (manual=False) are silent
                when no newer version is available and honour the skipped
                version stored in config.
        ui: What the user sees (None: the log only).
    """
    thread = QThread()
    worker = UpdateWorker()
    worker.moveToThread(thread)

    update_result = None
    update_error = None

    def on_finished(result):
        nonlocal update_result
        update_result = result
        thread.quit()

    def on_error(msg):
        nonlocal update_error
        update_error = msg
        thread.quit()

    def _cleanup(_thread):
        _update_jobs.remove((thread, worker))
        app = QCoreApplication.instance()
        if update_error is not None:
            logger.warning(f"Error checking for updates: {update_error}")
            if manual and app and ui is not None:
                QTimer.singleShot(0, app, lambda: ui.check_failed(update_error))
            return
        if app is None or ui is None:
            return
        if update_result:
            version = update_result.get("version", "")
            # For automatic checks, skip if user previously chose to skip this version
            if (
                not manual
                and config_manager
                and version
                and config_manager.skipped_update_version == version
            ):
                logger.debug("Skipping update dialog for version %s (user skipped)", version)
                return
            QTimer.singleShot(
                0, app, lambda: ui.update_available(update_result, config_manager, manual)
            )
        elif manual:
            QTimer.singleShot(0, app, ui.up_to_date)

    thread.started.connect(worker.run)
    worker.finished.connect(on_finished, Qt.DirectConnection)
    worker.error.connect(on_error, Qt.DirectConnection)
    ThreadCleanup(thread, worker).finished.connect(_cleanup)
    _update_jobs.append((thread, worker))
    thread.start()


def save_settings(config_manager: ConfigManager) -> None:
    """Save the update preferences; a locked or read-only config.json is logged, not raised
    (as TVWindow.save_config does): the update flow goes on, the setting applies until quit."""
    try:
        config_manager.save_config()
    except Exception as e:
        logger.warning("Couldn't save the settings: %s", safe_message(e))


def skip_version(config_manager: Optional[ConfigManager], version: str):
    """Record a version to skip in future automatic checks."""
    if config_manager is None:
        logger.warning("Cannot skip version: no config_manager provided")
        return
    config_manager.skipped_update_version = version
    save_settings(config_manager)
    logger.info("Skipping future prompts for version %s", version)


def disable_update_reminders(config_manager: Optional[ConfigManager]):
    """Turn off automatic update checks entirely.

    Re-enable in Settings or via Help > Check for Updates.
    """
    if config_manager is None:
        logger.warning("Cannot disable update reminders: no config_manager provided")
        return
    config_manager.check_updates = False
    save_settings(config_manager)
    logger.info("Automatic update reminders disabled")


def extract_version_from_tag(tag):
    # Handle common tag formats like 'v1.2.3', 'release-1.2.3', or '1.2.3'
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

    @Slot()
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


# Keep reference to download jobs to avoid GC
_download_jobs: List[Tuple[QThread, UpdateDownloader]] = []


def start_download(
    download_url: str,
    file_size: int,
    *,
    progress: Callable[[int, int], None],
    finished: Callable[[str], None],
    failed: Callable[[str], None],
) -> Callable[[], None]:
    """Download the update on a QThread; returns what cancels it.

    The callbacks run on the GUI thread: `progress(downloaded, total)`, then `finished(path)`
    or `failed(message)` (also "Download cancelled").
    """
    thread = QThread()
    downloader = UpdateDownloader(download_url, file_size)
    downloader.moveToThread(thread)

    # QTimer.singleShot(0, app, fn) ensures fn runs on the main/GUI thread.
    # Signals from a blocking worker delivered via QueuedConnection to Python
    # closures lack thread affinity and may run on the worker thread, which
    # crashes macOS (NSWindow operations must be on the Main Thread).
    app = QCoreApplication.instance()
    downloaded_path = None
    download_error = None
    completed = False

    def on_progress(downloaded: int, total: int):
        def _update():
            if not completed:
                progress(downloaded, total)

        if app:
            QTimer.singleShot(0, app, _update)

    def on_finished(path: str):
        nonlocal downloaded_path
        downloaded_path = path
        thread.quit()

    def on_error(error_msg: str):
        nonlocal download_error
        download_error = error_msg
        thread.quit()

    def _cleanup(_thread):
        nonlocal completed
        completed = True
        _download_jobs.remove((thread, downloader))
        if app:
            if downloaded_path is not None:
                path = downloaded_path
                QTimer.singleShot(0, app, lambda: finished(path))
            elif download_error is not None:
                message = download_error
                QTimer.singleShot(0, app, lambda: failed(message))

    thread.started.connect(downloader.run)
    downloader.progress.connect(on_progress, Qt.DirectConnection)
    downloader.finished.connect(on_finished, Qt.DirectConnection)
    downloader.error.connect(on_error, Qt.DirectConnection)
    ThreadCleanup(thread, downloader).finished.connect(_cleanup)

    _download_jobs.append((thread, downloader))
    thread.start()
    return downloader.cancel


def perform_update(downloaded_path: str, release_url: str, ui: UpdateUI):
    """Execute platform-specific update process."""
    system = platform.system()

    if system == "Windows":
        _perform_windows_update(downloaded_path, release_url, ui)
    else:
        ui.downloaded(downloaded_path)  # macOS/Linux: the user replaces the app


def _perform_windows_update(downloaded_path: str, release_url: str, ui: Optional[UpdateUI] = None):
    """Windows: launch new exe with --replace flag, then quit."""
    app = QCoreApplication.instance()
    _run_before_update()  # once: later retries find nothing left to stop
    if app and has_pending_threads():
        QTimer.singleShot(
            25, app, lambda: _perform_windows_update(downloaded_path, release_url, ui)
        )
        return

    # Get the path to the original executable (not the temp extraction folder)
    # For PyInstaller, sys.executable points to the original .exe file
    original_exe = sys.executable

    try:
        # Launch the downloaded exe with --replace flag
        subprocess.Popen(
            [downloaded_path, "--replace", original_exe],
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        )

        # Run the normal window shutdown paths before exiting (a QApplication's).
        close_all = getattr(app, "closeAllWindows", None)
        if close_all is not None:
            close_all()

    except OSError as e:
        logger.error(f"Failed to launch update: {e}")
        _undo_before_update()  # QiTV goes on: restart what the update stopped
        if ui is not None:
            ui.launch_failed(downloaded_path, release_url)


def open_downloads_folder() -> None:
    """Show the Downloads folder (best effort), where the update was saved."""
    downloads_folder = get_downloads_folder()
    try:
        if platform.system() == "Darwin":
            subprocess.run(["open", str(downloads_folder)], check=False)
        else:
            subprocess.run(["xdg-open", str(downloads_folder)], check=False)
    except OSError:
        pass  # Folder opening is best-effort
