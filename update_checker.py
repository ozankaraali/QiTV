import logging
from typing import List, Tuple

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import QApplication, QMessageBox
from packaging.version import parse
import requests

from config_manager import get_app_version

logger = logging.getLogger(__name__)

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
                self.finished.emit(
                    {
                        "version": latest_version,
                        "url": latest_release.get("html_url", ""),
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
                QTimer.singleShot(
                    0, app, lambda: show_update_dialog(result["version"], result["url"])
                )

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


def show_update_dialog(latest_version, release_url):
    msg = QMessageBox()
    msg.setIcon(QMessageBox.Information)
    msg.setText(f"A new version ({latest_version}) is available!")
    msg.setInformativeText("Would you like to open the release page to download the update?")
    msg.setWindowTitle("Update Available")
    msg.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
    if msg.exec_() == QMessageBox.Yes:
        import webbrowser

        webbrowser.open(release_url)
