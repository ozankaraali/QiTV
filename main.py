import platform
import sys
import qdarktheme
from PySide6.QtWidgets import QApplication
from PySide6 import QtGui

from video_player import VideoPlayer
from channel_list import ChannelList
from config_manager import ConfigManager
from update_checker import check_for_updates

import ctypes

if __name__ == "__main__":
    app = QApplication(sys.argv)

    icon_path = "assets/qitv.png"
    config_manager = ConfigManager()
    if platform.system() == 'Windows':
        myappid = f'com.ozankaraali.qitv.{config_manager.CURRENT_VERSION}'
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
        if hasattr(sys, '_MEIPASS'):
            icon_path = sys._MEIPASS + "\\assets\\qitv.ico"

    app.setWindowIcon(QtGui.QIcon(icon_path))
    player = VideoPlayer(config_manager)
    channel_list = ChannelList(app, player, config_manager)
    qdarktheme.setup_theme("auto")
    player.show()
    channel_list.show()
    check_for_updates()
    sys.exit(app.exec())
