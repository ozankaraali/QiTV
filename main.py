import sys
import qdarktheme

from PyQt5.QtWidgets import QApplication

from video_player import VideoPlayer
from channel_list import ChannelList
from config_manager import ConfigManager

if __name__ == "__main__":
    app = QApplication(sys.argv)
    config_manager = ConfigManager()
    player = VideoPlayer(config_manager)
    channel_list = ChannelList(app, player, config_manager)
    qdarktheme.setup_theme("auto")
    player.show()
    channel_list.show()
    sys.exit(app.exec_())
