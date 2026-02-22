import ctypes
import logging
import os
import platform
import shutil
import sys
import time
import warnings

from PySide6 import QtGui
from PySide6.QtCore import QLoggingCategory, QTimer, qInstallMessageHandler
from PySide6.QtWidgets import QApplication
import qdarktheme

from channel_list import ChannelList
from config_manager import ConfigManager, get_app_version
from epg_manager import EpgManager
from image_manager import ImageManager
from provider_manager import ProviderManager
from sleep_manager import allow_sleep, prevent_sleep
from update_checker import check_for_updates
from video_player import VideoPlayer


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


if __name__ == "__main__":
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

    config_manager = ConfigManager()
    image_manager = ImageManager(config_manager, config_manager.max_cache_image_size * 1024 * 1024)
    provider_manager = ProviderManager(config_manager)
    epg_manager = EpgManager(config_manager, provider_manager)

    if platform.system() == "Windows":
        myappid = f"com.ozankaraali.qitv.{get_app_version()}"
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)  # type: ignore
        if hasattr(sys, "_MEIPASS"):
            icon_path = sys._MEIPASS + "\\assets\\qitv.ico"

    app.setWindowIcon(QtGui.QIcon(icon_path))

    prevent_sleep()
    try:
        player = VideoPlayer(config_manager)
        channel_list = ChannelList(
            app, player, config_manager, provider_manager, image_manager, epg_manager
        )
        qdarktheme.setup_theme("auto", custom_colors={"primary": "#C96B43"})
        # Skip showing embedded player if external player (VLC/MPV) is enabled
        if not config_manager.play_in_vlc and not config_manager.play_in_mpv:
            player.show()
        channel_list.show()

        def _bring_main_window_to_front():
            try:
                if channel_list.isMinimized():
                    channel_list.showNormal()
                channel_list.raise_()
                channel_list.activateWindow()
                app.setActiveWindow(channel_list)
            except Exception:
                pass

        # Bring the app to foreground on startup. A second delayed attempt
        # helps on desktops where theme/app init races with activation.
        QTimer.singleShot(0, _bring_main_window_to_front)
        QTimer.singleShot(250, _bring_main_window_to_front)

        if config_manager.check_updates:
            check_for_updates()

        sys.exit(app.exec())
    finally:
        allow_sleep()
