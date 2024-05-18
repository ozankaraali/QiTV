import sys

import vlc
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QFrame,
    QGridLayout
)


class VideoPlayer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("QiTV Player")
        self.setGeometry(50, 100, 1200, 800)

        self.container_widget = QWidget(self)
        self.setCentralWidget(self.container_widget)
        self.grid_layout = QGridLayout(self.container_widget)

        self.instance = vlc.Instance('--no-xlib', '--vout=gl')
        self.player = self.instance.media_player_new()
        self.create_video_area()

        self.proxy_server = None

    def create_video_area(self):
        self.video_frame = QFrame(self.container_widget)
        self.grid_layout.addWidget(self.video_frame, 0, 0)
        self.grid_layout.setRowStretch(0, 1)
        self.grid_layout.setColumnStretch(0, 1)

    def play_video(self, file_path):
        if sys.platform.startswith('linux'):
            self.player.set_xwindow(int(self.video_frame.winId()))
        elif sys.platform == "win32":
            self.player.set_hwnd(int(self.video_frame.winId()))
        elif sys.platform == "darwin":
            self.player.set_nsobject(int(self.video_frame.winId()))
        media = self.instance.media_new(file_path)
        self.player.set_media(media)
        self.player.play()

    def stop_video(self):
        self.player.stop()

    def toggle_play_pause(self):
        state = self.player.get_state()
        if state == vlc.State.Playing:
            self.player.pause()
        else:
            self.player.play()