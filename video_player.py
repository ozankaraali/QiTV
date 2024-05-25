import json
import platform
import sys

import vlc
from PyQt5.QtCore import Qt, QEvent
from PyQt5.QtWidgets import QMainWindow, QFrame, QHBoxLayout


class VideoPlayer(QMainWindow):
    def __init__(self, *args, **kwargs):
        super(VideoPlayer, self).__init__(*args, **kwargs)
        self.load_config()
        self.apply_window_settings()
        self.config = None
        self.media = None
        self.setGeometry(100, 100, 800, 600)
        self.mainFrame = QFrame()
        self.setCentralWidget(self.mainFrame)
        self.setWindowTitle("QiTV Player")
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
        self.save_window_settings()
        self.save_config()
        if self.media_player.is_playing():
            self.media_player.stop()
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

    def toggle_play_pause(self):
        state = self.media_player.get_state()
        if state == vlc.State.Playing:
            self.media_player.pause()
        else:
            self.media_player.play()

    def load_config(self):
        try:
            with open("config.json", "r") as f:
                self.config = json.load(f)
            if self.config is None:
                self.config = self.default_config()
        except (FileNotFoundError, json.JSONDecodeError):
            self.config = self.default_config()
            self.save_config()

    @staticmethod
    def default_config():
        return {
            "selected": 0,
            "data": [
                {
                    "type": "M3UPLAYLIST",
                    "url": "https://iptv-org.github.io/iptv/index.m3u",
                }
            ],
            "window_positions": {
                "channel_list": {"x": 1250, "y": 100, "width": 400, "height": 800},
                "video_player": {"x": 50, "y": 100, "width": 1200, "height": 800},
            },
        }

    def save_config(self):
        with open("config.json", "w") as f:
            json.dump(self.config, f)

    def save_window_settings(self):
        pos = self.geometry()
        window_positions = self.config.get("window_positions", {})
        window_positions["video_player"] = {
            "x": pos.x(),
            "y": pos.y(),
            "width": pos.width(),
            "height": pos.height(),
        }
        self.config["window_positions"] = window_positions
        self.save_config()

    def apply_window_settings(self):
        window_positions = self.config.get("window_positions", {})
        video_player_pos = window_positions.get("video_player", {})
        self.setGeometry(
            video_player_pos.get("x", 50),
            video_player_pos.get("y", 100),
            video_player_pos.get("width", 1200),
            video_player_pos.get("height", 800),
        )
