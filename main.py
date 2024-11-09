import ctypes
import platform
import sys

import qdarktheme
from PySide6 import QtGui
from PySide6.QtWidgets import QApplication

from channel_list import ChannelList
from config_manager import ConfigManager
from sleep_manager import allow_sleep, prevent_sleep
from update_checker import check_for_updates
from video_player import VideoPlayer
from provider_manager import ProviderManager, ProviderContext

if __name__ == "__main__":
    app = QApplication(sys.argv)

    icon_path = "assets/qitv.png"
    config_manager = ConfigManager()
    provider_context = ProviderContext()
    provider_manager = ProviderManager(config_manager, provider_context)
    if platform.system() == "Windows":
        myappid = f"com.ozankaraali.qitv.{config_manager.CURRENT_VERSION}"
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)  # type: ignore
        if hasattr(sys, "_MEIPASS"):
            icon_path = sys._MEIPASS + "\\assets\\qitv.ico"

    app.setWindowIcon(QtGui.QIcon(icon_path))

    prevent_sleep()
    try:
        player = VideoPlayer(config_manager)
        channel_list = ChannelList(app, player, config_manager, provider_manager)
        qdarktheme.setup_theme("auto")
        player.show()
        channel_list.show()

        if config_manager.check_updates:
            check_for_updates()

        sys.exit(app.exec())
    finally:
        allow_sleep()
