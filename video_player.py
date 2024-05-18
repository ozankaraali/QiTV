import sys
import json
import vlc
from PyQt5.QtWidgets import QMainWindow, QWidget, QFrame, QGridLayout


class VideoPlayer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("QiTV Player")

        self.container_widget = QWidget(self)
        self.setCentralWidget(self.container_widget)
        self.grid_layout = QGridLayout(self.container_widget)

        self.instance = vlc.Instance('--no-xlib', '--vout=gl')
        self.player = self.instance.media_player_new()
        self.create_video_area()

        self.load_config()
        self.apply_window_settings()
        self.proxy_server = None

    def closeEvent(self, event):
        self.save_window_settings()
        self.save_config()
        event.accept()

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
