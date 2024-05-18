import sys

from PyQt5.QtWidgets import (
    QApplication
)

from gui import VideoPlayer, ChannelListWindow

if __name__ == "__main__":
    app = QApplication(sys.argv)
    player = VideoPlayer()
    channel_list = ChannelListWindow(player)
    player.show()
    channel_list.show()
    sys.exit(app.exec_())
