import ctypes
import logging
import multiprocessing
import os
import platform
import shutil
import sys
import time
from typing import Mapping, Optional
import warnings

from PySide6 import QtGui
from PySide6.QtCore import QLoggingCategory, qInstallMessageHandler
from PySide6.QtWidgets import QApplication
import qdarktheme

from config_manager import ConfigManager, get_app_version
from epg_manager import EpgManager
from image_manager import ImageManager
from provider_manager import ProviderManager
from sleep_manager import allow_sleep, prevent_sleep
from ui.tv_window import TVWindow
from update_checker import check_for_updates


def qt_platform_override(system: str, environ: Mapping[str, str]) -> Optional[str]:
    """The QT_QPA_PLATFORM to set before Qt starts, or None to leave Qt's choice.

    libVLC embeds its video and the overlay host stays glued only on X11. Qt 6 picks
    Wayland in Wayland sessions, so Linux prefers XWayland when an X display exists.
    """
    if system == "Linux" and not environ.get("QT_QPA_PLATFORM") and environ.get("DISPLAY"):
        return "xcb;wayland"
    return None


def handle_replace_flag():
    """Handle --replace flag for Windows auto-update.

    When launched with --replace <old_exe_path>, this function:
    1. Waits for the old process to exit
    2. Copies this executable to the old location
    3. Optionally cleans up the downloaded file
    """
    if "--replace" not in sys.argv:
        return

    try:
        replace_index = sys.argv.index("--replace")
        if replace_index + 1 >= len(sys.argv):
            return

        old_exe_path = sys.argv[replace_index + 1]
        current_exe = sys.executable

        # Wait for old process to exit
        time.sleep(1.5)

        # Copy current executable to old location
        shutil.copy2(current_exe, old_exe_path)
        logging.info(f"Updated {old_exe_path} successfully")

        # Note: We can't delete the currently running exe on Windows
        # The downloaded file in Downloads will remain - user can delete manually

    except Exception as e:
        logging.error(f"Failed to perform update replacement: {e}")


def main() -> int:
    # Frozen builds re-run this entry point in worker processes (Phase 2's live worker).
    multiprocessing.freeze_support()

    # Basic logging configuration (tweak level as needed)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Handle Windows auto-update replacement if launched with --replace flag
    if platform.system() == "Windows":
        handle_replace_flag()

    # Suppress noisy third-party warnings
    warnings.filterwarnings("ignore", category=SyntaxWarning, module="vlc")
    # Reduce Qt info logs that are not actionable
    QLoggingCategory.setFilterRules("qt.accessibility.*=false\nqt.qpa.fonts.*=false")
    qt_platform = qt_platform_override(platform.system(), os.environ)
    if qt_platform:
        os.environ["QT_QPA_PLATFORM"] = qt_platform
    app = QApplication(sys.argv)

    # Optional: capture Qt warnings to help debug thread/timer issues
    def _qt_msg_handler(mode, context, message):
        # Focus on timer/thread-related warnings only when enabled. Print directly to stderr to avoid recursion.
        if "QBasicTimer::start" in message or "QObject::startTimer" in message:
            try:
                sys.stderr.write(f"Qt: {message}\n")
                sys.stderr.flush()
            except Exception:
                pass

    if os.environ.get("QITV_DEBUG_QT", "0") == "1":
        qInstallMessageHandler(_qt_msg_handler)

    icon_path = "assets/qitv.png"

    # Resolve icon path for PyInstaller bundles
    if hasattr(sys, "_MEIPASS"):
        if platform.system() == "Windows":
            icon_path = os.path.join(sys._MEIPASS, "assets", "qitv.ico")
        else:
            icon_path = os.path.join(sys._MEIPASS, "assets", "qitv.png")

    config_manager = ConfigManager()
    image_manager = ImageManager(config_manager, config_manager.max_cache_image_size * 1024 * 1024)
    provider_manager = ProviderManager(config_manager)
    epg_manager = EpgManager(config_manager, provider_manager)

    if platform.system() == "Windows":
        myappid = f"com.ozankaraali.qitv.{get_app_version()}"
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)  # type: ignore

    app.setWindowIcon(QtGui.QIcon(icon_path))
    qdarktheme.setup_theme("dark", custom_colors={"primary": "#C96B43"})

    prevent_sleep()
    try:
        window = TVWindow(app, config_manager, provider_manager, image_manager, epg_manager)
        window.show()

        if config_manager.check_updates:
            check_for_updates(config_manager=config_manager)

        return app.exec()
    finally:
        allow_sleep()


if __name__ == "__main__":
    sys.exit(main())
