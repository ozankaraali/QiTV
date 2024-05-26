import json
import platform
import sys

import vlc
from PyQt5.QtCore import Qt, QEvent
from PyQt5.QtWidgets import QMainWindow, QFrame, QHBoxLayout
from PyQt5.QtGui import QIcon


class VideoPlayer(QMainWindow):
    def __init__(self, config_manager, *args, **kwargs):
        super(VideoPlayer, self).__init__(*args, **kwargs)
        self.config_manager = config_manager
        self.config = self.config_manager.config

        self.config_manager.apply_window_settings("video_player", self)

        self.mainFrame = QFrame()
        self.setCentralWidget(self.mainFrame)
        self.setWindowTitle("QiTV Player")
        self.setWindowIcon(QIcon("assets/qitv.png"))
        t_lay_parent = QHBoxLayout()
        t_lay_parent.setContentsMargins(0, 0, 0, 0)

        self.video_frame = QFrame()
        self.video_frame.mouseDoubleClickEvent = self.mouseDoubleClickEvent
        self.video_frame.installEventFilter(self)
        t_lay_parent.addWidget(self.video_frame)
        self.instance = vlc.Instance(["--video-on-top"])
        self.media_player = self.instance.media_player_new()
        self.media_player.video_set_mouse_input(False)
        self.media_player.video_set_key_input(False)

        if sys.platform.startswith("linux"):
            self.media_player.set_xwindow(self.video_frame.winId())
        elif sys.platform == "win32":
            self.media_player.set_hwnd(self.video_frame.winId())
        elif sys.platform == "darwin":
            self.media_player.set_nsobject(int(self.video_frame.winId()))

        self.mainFrame.setLayout(t_lay_parent)
        self.show()

    def eventFilter(self, obj, event):
        if obj == self.video_frame:
            if event.type() == QEvent.Wheel:
                self.wheelEvent(event)
                return True
            elif event.type() == QEvent.KeyPress:
                self.keyPressEvent(event)
                return True
        return False

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if platform.system() == "Darwin":
            delta = -delta
        if delta > 0:
            self.change_volume(10)  # Increase volume
        else:
            self.change_volume(-10)  # Decrease volume

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Up:
            self.change_volume(10)  # Increase volume
        elif event.key() == Qt.Key_Down:
            self.change_volume(-10)  # Decrease volume
        elif event.key() == Qt.Key_Space:
            self.toggle_play_pause()  # Toggle Play/Pause
        elif event.key() == Qt.Key_M:
            self.toogle_mute()  # Toggle Mute
        elif event.key() == Qt.Key_Escape:
            self.setWindowState(Qt.WindowNoState)
        elif event.key() == Qt.Key_F:
            if self.windowState() == Qt.WindowNoState:
                self.video_frame.show()
                self.setWindowState(Qt.WindowFullScreen)
            else:
                self.setWindowState(Qt.WindowNoState)
        super().keyPressEvent(event)

    def change_volume(self, step):
        current_volume = self.media_player.audio_get_volume()
        new_volume = max(0, min(100, current_volume + step))
        self.media_player.audio_set_volume(new_volume)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            if self.windowState() == Qt.WindowNoState:
                self.video_frame.show()
                self.setWindowState(Qt.WindowFullScreen)
            else:
                self.setWindowState(Qt.WindowNoState)

    def closeEvent(self, event):
        if self.media_player.is_playing():
            self.media_player.stop()
        self.config_manager.save_window_settings(self.geometry(), "video_player")
        self.hide()
        event.ignore()

    def play_video(self, video_url):
        if platform.system() == "Linux":
            self.media_player.set_xwindow(self.video_frame.winId())
        elif platform.system() == "Windows":
            self.media_player.set_hwnd(self.video_frame.winId())
        elif platform.system() == "Darwin":
            self.media_player.set_nsobject(int(self.video_frame.winId()))

        self.media = self.instance.media_new(video_url)
        self.media_player.set_media(self.media)
        self.media_player.play()
        self.show()

    def stop_video(self):
        self.media_player.stop()

    def toogle_mute(self):
        state = self.media_player.audio_get_mute()
        self.media_player.audio_set_mute(not state)

    def toggle_play_pause(self):
        state = self.media_player.get_state()
        if state == vlc.State.Playing:
            self.media_player.pause()
        else:
            self.media_player.play()
