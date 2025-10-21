import ctypes
import logging
import os
import platform
import sys
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

if __name__ == "__main__":
    # Basic logging configuration (tweak level as needed)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

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
        qdarktheme.setup_theme("auto")
        player.show()
        channel_list.show()

        # Do not force focus/activation; let the window manager handle it

        if config_manager.check_updates:
            check_for_updates()

        sys.exit(app.exec())
    finally:
        allow_sleep()
