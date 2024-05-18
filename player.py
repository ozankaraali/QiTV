import json
import requests
import sys
from PyQt5.QtCore import pyqtSignal, Qt
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QAction, QFileDialog, QVBoxLayout, QWidget, QPushButton, QFrame,
    QListWidget, QHBoxLayout, QListWidgetItem, QLineEdit, QGridLayout, QFormLayout, QLabel, QComboBox,
)
import vlc
from dialog import OptionsDialog
from threading import Thread
from time import sleep

class VideoPlayer(QMainWindow):
    channels_loaded = pyqtSignal(list)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Not PiTV")
        self.setGeometry(100, 100, 1200, 800)

        self.instance = vlc.Instance('--no-xlib', '--vout=gl')
        self.player = self.instance.media_player_new()

        container_widget = QWidget(self)
        self.setCentralWidget(container_widget)
        grid_layout = QGridLayout(container_widget)

        # Top right buttons for Open and Options
        self.buttons_widget = QWidget(container_widget)
        buttons_layout = QHBoxLayout(self.buttons_widget)
        self.open_button = QPushButton("Open", self)
        self.open_button.clicked.connect(self.open_file)
        buttons_layout.addWidget(self.open_button)

        self.options_button = QPushButton("Options", self)
        self.options_button.clicked.connect(self.options_dialog)
        buttons_layout.addWidget(self.options_button)

        grid_layout.addWidget(self.buttons_widget, 0, 1, 1, 1)

        # Left panel for channel list and search box
        self.left_panel = QWidget(container_widget)
        left_layout = QVBoxLayout(self.left_panel)
        self.search_box = QLineEdit(self.left_panel)
        self.search_box.setPlaceholderText("Search channels...")
        self.search_box.textChanged.connect(self.filter_channels)
        left_layout.addWidget(self.search_box)
        self.channel_list = QListWidget(self.left_panel)
        self.channel_list.itemClicked.connect(self.channel_selected)
        left_layout.addWidget(self.channel_list)
        grid_layout.addWidget(self.left_panel, 1, 0, 1, 1)
        grid_layout.setColumnStretch(0, 1)

        # Video playback frame
        self.video_frame = QFrame(container_widget)
        grid_layout.addWidget(self.video_frame, 1, 1, 1, 2)
        grid_layout.setColumnStretch(1, 4)

        self.init_ui()
        self.load_config()
        self.load_channels()

        # Start proxy server
        self.proxy_thread = Thread(target=self.run_server)
        self.proxy_thread.daemon = True
        self.proxy_thread.start()

    def closeEvent(self, event):
        self.proxy_thread.join(timeout=1)
        event.accept()

    def run_server(self):
        from server import run_server
        run_server()

    def init_ui(self):
        menubar = self.menuBar()
        file_menu = menubar.addMenu('&File')

        open_action = QAction('Open', self)
        open_action.triggered.connect(self.open_file)
        file_menu.addAction(open_action)

        options_action = QAction('Options', self)
        options_action.triggered.connect(self.options_dialog)
        file_menu.addAction(options_action)

    def open_file(self):
        file_dialog = QFileDialog(self)
        file_path, _ = file_dialog.getOpenFileName()
        if file_path != '':
            self.play_video(file_path)

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

    def load_config(self):
        try:
            with open('config.json', 'r') as f:
                self.config = json.load(f)
            if self.config is None:
                self.config = self.default_config()
        except (FileNotFoundError, json.JSONDecodeError):
            self.config = self.default_config()
            self.save_config()

    def default_config(self):
        return {
            "selected": 0,
            "data": [
                {
                    "type": "M3UPLAYLIST",
                    "url": "https://iptv-org.github.io/iptv/index.m3u"
                }
            ]
        }

    def save_config(self):
        with open('config.json', 'w') as f:
            json.dump(self.config, f)

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
                proxy_url = f"http://localhost:8000/proxy?url={requests.utils.quote(url)}"  # proxy link
                self.play_video(proxy_url)
            else:
                print("Failed to create link.")
        else:
            proxy_url = f"http://localhost:8000/proxy?url={requests.utils.quote(cmd)}"  # proxy link
            self.play_video(proxy_url)

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
            'headers': {
                'User-Agent': 'Mozilla/5.0 (QtEmbedded; U; Linux; C) AppleWebKit/533.3 (KHTML, like Gecko) MAG200 stbapp ver: 2 rev: 250 Safari/533.3',
                'Accept-Charset': 'UTF-8,*;q=0.8',
                'X-User-Agent': 'Model: MAG200; Link: Ethernet',
                'Content-Type': 'application/json',
                'Host': url.split("//")[1].split("/")[0],
                'Range': 'bytes=0-',
                'Accept': '*/*',
                'Referer': f"{url}/c/",
                'Cookie': f'mac={mac}; stb_lang=en; timezone=Europe/Kiev; PHPSESSID=null;',
                'Authorization': f'Bearer {token}'
            }
        }
        return options

    def generate_headers(self):
        selected_provider = self.config["data"][self.config["selected"]]
        return selected_provider["options"]["headers"]

    def verify_url(self, url):
        try:
            response = requests.get(url)
            return response.status_code == 200
        except Exception as e:
            print("Error verifying URL:", e)
            return False

if __name__ == '__main__':
    app = QApplication(sys.argv)
    player = VideoPlayer()
    player.show()
    sys.exit(app.exec_())
