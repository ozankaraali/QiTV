import sys
import vlc
import json
import platform
from PyQt5.QtWidgets import QApplication, QMainWindow, QVBoxLayout, QFrame, QWidget
from PyQt5.QtGui import QPalette, QColor
from PyQt5.QtCore import Qt


class VideoPlayer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("QiTV Player")

        self.setGeometry(100, 100, 800, 600)
        palette = self.palette()
        palette.setColor(QPalette.Window, QColor(0, 0, 0))
        self.setPalette(palette)

        # VLC instance and media player
        self.instance = vlc.Instance()
        self.mediaplayer = self.instance.media_player_new()
        self.proxy_server = None

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
        if self.isFullScreen():
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
        if self.mediaplayer.is_playing():
            self.mediaplayer.stop()
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
            self.mediaplayer.set_xwindow(self.videoframe.winId())
        elif platform.system() == "Windows":
            self.mediaplayer.set_hwnd(self.videoframe.winId())
        elif platform.system() == "Darwin":
            self.mediaplayer.set_nsobject(int(self.videoframe.winId()))

        self.media = self.instance.media_new(video_url)
        self.mediaplayer.set_media(self.media)
        self.mediaplayer.play()

    def stop_video(self):
        self.mediaplayer.stop()

    def toggle_play_pause(self):
        state = self.mediaplayer.get_state()
        if state == vlc.State.Playing:
            self.mediaplayer.pause()
        else:
            self.mediaplayer.play()

    def load_config(self):
        try:
            with open('config.json', 'r') as f:
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
                    "url": "https://iptv-org.github.io/iptv/index.m3u"
                }
            ],
            "window_positions": {
                "channel_list": {"x": 1250, "y": 100, "width": 400, "height": 800},
                "video_player": {"x": 50, "y": 100, "width": 1200, "height": 800}
            }
        }

    def save_config(self):
        with open('config.json', 'w') as f:
            json.dump(self.config, f)

    def save_window_settings(self):
        pos = self.geometry()
        window_positions = self.config.get("window_positions", {})
        window_positions["video_player"] = {
            "x": pos.x(),
            "y": pos.y(),
            "width": pos.width(),
            "height": pos.height()
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
            video_player_pos.get("height", 800)
        )


class VideoFrame(QWidget):
    def __init__(self, parent=None):
        super(VideoFrame, self).__init__(parent)
        self.player = parent  # Store the VideoPlayer instance

    def mouseDoubleClickEvent(self, event):
        self.player.toggle_fullscreen()  # Call the method on VideoPlayer instance
