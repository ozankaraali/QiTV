import json
import platform

import vlc
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QPalette, QColor
from PyQt5.QtWidgets import QMainWindow, QVBoxLayout, QWidget


class VideoPlayer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("QiTV Player")

        self.setGeometry(100, 100, 800, 600)
        palette = self.palette()
        palette.setColor(QPalette.Window, QColor(0, 0, 0))
        self.setPalette(palette)

        try:
            self.instance = vlc.Instance()
        except Exception as e:
            print(f"Exception occurred while creating VLC instance: {e}")
            raise

        try:
            self.media_player = self.instance.media_player_new()
            if not self.media_player:
                raise Exception("Failed to create VLC media player")
        except Exception as e:
            print(f"Exception occurred while creating VLC media player: {e}")
            raise

        self.fullscreen = False

        # Main widget and layout
        self.widget = QWidget(self)
        self.setCentralWidget(self.widget)

        self.layout = QVBoxLayout()
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(0)
        self.widget.setLayout(self.layout)

        self.create_video_area()
        self.load_config()
        self.apply_window_settings()

    def toggle_fullscreen(self):
        if self.fullscreen:
            self.showNormal()
            self.setWindowFlags(self.windowFlags() & ~Qt.FramelessWindowHint)
            self.show()
        else:
            self.showFullScreen()
            self.setWindowFlags(self.windowFlags() | Qt.FramelessWindowHint)
            self.show()

    def closeEvent(self, event):
        self.save_window_settings()
        self.save_config()
        if self.media_player.is_playing():
            self.media_player.stop()
        event.accept()

    def create_video_area(self):
        self.videoframe = VideoFrame(self)
        self.videoframe.setAutoFillBackground(True)
        videoframe_palette = self.videoframe.palette()
        videoframe_palette.setColor(QPalette.Window, Qt.black)
        self.videoframe.setPalette(videoframe_palette)
        self.layout.addWidget(self.videoframe)

    def play_video(self, video_url):
        if platform.system() == "Linux":
            self.media_player.set_xwindow(self.videoframe.winId())
        elif platform.system() == "Windows":
            self.media_player.set_hwnd(self.videoframe.winId())
        elif platform.system() == "Darwin":
            self.media_player.set_nsobject(int(self.videoframe.winId()))

        self.media = self.instance.media_new(video_url)
        self.media_player.set_media(self.media)
        self.media_player.play()

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


class VideoFrame(QWidget):
    def __init__(self, parent=None):
        super(VideoFrame, self).__init__(parent)
        self.player = parent  # Store the VideoPlayer instance

    def mouseDoubleClickEvent(self, event):
        self.player.fullscreen = not self.player.fullscreen
        self.player.toggle_fullscreen()  # Call the method on VideoPlayer instance
