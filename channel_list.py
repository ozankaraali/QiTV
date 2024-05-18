import json
import sys
import requests
from PyQt5.QtCore import pyqtSignal, Qt
from PyQt5.QtWidgets import (
    QMainWindow, QAction, QFileDialog, QVBoxLayout, QWidget, QPushButton, QListWidget,
    QHBoxLayout, QListWidgetItem, QLineEdit, QGridLayout, QApplication
)
from proxy_server import ProxyHTTPRequestHandler, ProxyServerThread
from options import OptionsDialog


class ChannelList(QMainWindow):
    channels_loaded = pyqtSignal(list)

    def __init__(self, player):
        super().__init__()
        self.player = player
        self.setWindowTitle("QiTV Channel List")

        self.container_widget = QWidget(self)
        self.setCentralWidget(self.container_widget)
        self.grid_layout = QGridLayout(self.container_widget)

        self.create_menu()
        self.create_left_panel()
        self.create_media_controls()

        self.load_config()
        self.apply_window_settings()
        self.load_channels()

        self.proxy_server = ProxyServerThread('localhost', 8081, ProxyHTTPRequestHandler)
        ProxyHTTPRequestHandler.parent_app = self
        self.proxy_server.start()

    def closeEvent(self, event):
        self.proxy_server.stop_server()
        self.save_window_settings()
        self.save_config()
        event.accept()

    def create_menu(self):
        menubar = self.menuBar()
        file_menu = menubar.addMenu('&File')
        open_action = QAction('Open', self)
        open_action.triggered.connect(self.open_file)
        file_menu.addAction(open_action)

        options_action = QAction('Options', self)
        options_action.triggered.connect(self.options_dialog)
        file_menu.addAction(options_action)

    def create_left_panel(self):
        self.left_panel = QWidget(self.container_widget)
        left_layout = QVBoxLayout(self.left_panel)

        self.open_button = QPushButton("Open")
        self.open_button.clicked.connect(self.open_file)
        left_layout.addWidget(self.open_button)

        self.options_button = QPushButton("Options")
        self.options_button.clicked.connect(self.options_dialog)
        left_layout.addWidget(self.options_button)

        self.search_box = QLineEdit(self.left_panel)
        self.search_box.setPlaceholderText("Search channels...")
        self.search_box.textChanged.connect(self.filter_channels)
        left_layout.addWidget(self.search_box)

        self.channel_list = QListWidget(self.left_panel)
        self.channel_list.itemClicked.connect(self.channel_selected)
        left_layout.addWidget(self.channel_list)

        self.grid_layout.addWidget(self.left_panel, 0, 0)
        self.grid_layout.setColumnStretch(0, 1)

    def create_media_controls(self):
        self.media_controls = QWidget(self.container_widget)
        control_layout = QHBoxLayout(self.media_controls)

        self.play_button = QPushButton("Play/Pause")
        self.play_button.clicked.connect(self.player.toggle_play_pause)
        control_layout.addWidget(self.play_button)

        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(self.player.stop_video)
        control_layout.addWidget(self.stop_button)

        self.grid_layout.addWidget(self.media_controls, 1, 0)

    def open_file(self):
        file_dialog = QFileDialog(self)
        file_path, _ = file_dialog.getOpenFileName()
        if file_path:
            self.player.play_video(file_path)

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
        window_positions["channel_list"] = {
            "x": pos.x(),
            "y": pos.y(),
            "width": pos.width(),
            "height": pos.height()
        }
        self.config["window_positions"] = window_positions
        self.save_config()

    def apply_window_settings(self):
        window_positions = self.config.get("window_positions", {})
        channel_list_pos = window_positions.get("channel_list", {})
        self.setGeometry(
            channel_list_pos.get("x", 1250),
            channel_list_pos.get("y", 100),
            channel_list_pos.get("width", 400),
            channel_list_pos.get("height", 800)
        )

    def load_channels(self):
        selected_provider = self.config["data"][self.config["selected"]]
        config_type = selected_provider.get("type", "")
        if config_type == "M3UPLAYLIST":
            self.load_m3u_playlist(selected_provider["url"])
        elif config_type == "STB":
            self.do_handshake(selected_provider["url"], selected_provider["mac"])
        elif config_type == "M3USTREAM":
            self.load_stream(selected_provider["url"])

    def load_m3u_playlist(self, url):
        try:
            response = requests.get(url)
            if response.status_code == 200:
                channels = self.parse_m3u(response.text)
                self.display_channels(channels)
        except requests.RequestException as e:
            print(f"Error loading M3U Playlist: {e}")

    def load_stream(self, url):
        channel = {"id": 1, "name": "Stream", "cmd": url}
        self.display_channels([channel])

    def display_channels(self, channels):
        self.channel_list.clear()
        for channel in channels:
            item = QListWidgetItem(channel["name"])
            item.setData(1, channel["cmd"])
            self.channel_list.addItem(item)

    def filter_channels(self, text):
        for i in range(self.channel_list.count()):
            item = self.channel_list.item(i)
            item.setHidden(text.lower() not in item.text().lower())

    def channel_selected(self, item):
        cmd = item.data(1)
        if self.config["data"][self.config["selected"]]["type"] == "STB":
            url = self.create_link(cmd)
            if url:
                proxy_url = f"http://localhost:8081/?url={requests.utils.quote(url)}"
                self.player.play_video(proxy_url)
            else:
                print("Failed to create link.")
        else:
            proxy_url = f"http://localhost:8081/?url={requests.utils.quote(cmd)}"
            self.player.play_video(proxy_url)

    def options_dialog(self):
        options = OptionsDialog(self)
        options.exec_()

    @staticmethod
    def parse_m3u(data):
        lines = data.split('\n')
        result = []
        channel = {}
        for line in lines:
            if line.startswith('#EXTINF'):
                info = line.split(',')
                channel = {'id': len(result) + 1, 'name': info[1] if len(info) > 1 else 'Unnamed Channel'}
            elif line.startswith('http'):
                channel['cmd'] = line
                result.append(channel)
        return result

    def do_handshake(self, url, mac, retries=0, max_retries=3):
        if retries > max_retries:
            return False
        token = self.config.get("token")
        serverload = "/server/load.php"
        options = self.create_options(url, mac, token)
        try:
            fetchurl = f"{url}{serverload}?type=stb&action=handshake&prehash=0&token={token}&JsHttpRequest=1-xml"
            handshake = requests.get(fetchurl, headers=options["headers"])
            body = handshake.json()
            token = body["js"]["token"]
            options["headers"]["Authorization"] = f"Bearer {token}"
            self.config["data"][self.config["selected"]]["options"] = options
            self.save_config()
            self.load_stb_channels(url, options)
            return True
        except Exception as e:
            if retries < max_retries:
                return self.do_handshake(url, mac, retries + 1, max_retries)
            print("Error in handshake:", e)
            return False

    def load_stb_channels(self, url, options):
        try:
            fetchurl = f"{url}/server/load.php?type=itv&action=get_all_channels"
            response = requests.get(fetchurl, headers=options["headers"])
            result = response.json()
            channels = result["js"]["data"]
            self.display_channels(channels)
        except Exception as e:
            print(f"Error loading STB channels: {e}")

    def create_link(self, cmd):
        try:
            selected_provider = self.config["data"][self.config["selected"]]
            url = selected_provider["url"]
            options = selected_provider["options"]
            fetchurl = f"{url}/server/load.php?type=itv&action=create_link&type=itv&cmd={requests.utils.quote(cmd)}&JsHttpRequest=1-xml"
            response = requests.get(fetchurl, headers=options["headers"])
            result = response.json()
            link = result["js"]["cmd"].split(' ')[-1]
            return link
        except Exception as e:
            print(f"Error creating link: {e}")
            return None

    @staticmethod
    def create_options(url, mac, token):
        options = {
            "headers": {
                "User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C) AppleWebKit/533.3 (KHTML, like Gecko) MAG200 stbapp ver: 2 rev: 250 Safari/533.3",
                "Accept-Charset": "UTF-8,*;q=0.8",
                "X-User-Agent": "Model: MAG200; Link: Ethernet",
                "Content-Type": "application/json",
                "Host": url.split("//")[1].split("/")[0],
                "Range": "bytes=0-",
                "Accept": "*/*",
                "Referer": f"{url}/c/",
                "Cookie": f"mac={mac}; stb_lang=en; timezone=Europe/Kiev; PHPSESSID=null;",
                "Authorization": f"Bearer {token}",
            }
        }
        return options

    def generate_headers(self):
        selected_provider = self.config["data"][self.config["selected"]]
        return selected_provider["options"]["headers"]

    @staticmethod
    def verify_url(url):
        try:
            response = requests.get(url)
            return response.status_code == 200
        except Exception as e:
            print("Error verifying URL:", e)
            return False
