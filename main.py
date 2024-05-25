import sys
import qdarktheme

from PyQt5.QtWidgets import QApplication

from video_player import VideoPlayer
from channel_list import ChannelList

if __name__ == "__main__":
    app = QApplication(sys.argv)
    player = VideoPlayer()
    channel_list = ChannelList(app, player)
    qdarktheme.setup_theme("auto")
    player.show()
    channel_list.show()
    sys.exit(app.exec_())
