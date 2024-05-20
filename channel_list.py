import json
import string
import random
import re
from urlobject import URLObject

import requests
from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import (
    QMainWindow, QFileDialog, QVBoxLayout, QWidget, QPushButton, QListWidget,
    QHBoxLayout, QListWidgetItem, QLineEdit, QGridLayout
)

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

        # self.create_menu()
        self.create_upper_panel()
        self.create_left_panel()
        self.create_media_controls()

        self.load_config()
        self.apply_window_settings()
        self.load_channels()

    def closeEvent(self, event):
        self.save_window_settings()
        self.save_config()
        event.accept()

    def create_upper_panel(self):
        self.upper_layout = QWidget(self.container_widget)
        ctl_layout = QHBoxLayout(self.upper_layout)

        self.open_button = QPushButton("Open File")
        self.open_button.clicked.connect(self.open_file)
        ctl_layout.addWidget(self.open_button)

        self.options_button = QPushButton("Options")
        self.options_button.clicked.connect(self.options_dialog)
        ctl_layout.addWidget(self.options_button)
        self.grid_layout.addWidget(self.upper_layout, 0, 0)


    def create_left_panel(self):
        self.left_panel = QWidget(self.container_widget)
        left_layout = QVBoxLayout(self.left_panel)

        self.search_box = QLineEdit(self.left_panel)
        self.search_box.setPlaceholderText("Search channels...")
        self.search_box.textChanged.connect(self.filter_channels)
        left_layout.addWidget(self.search_box)

        self.channel_list = QListWidget(self.left_panel)
        self.channel_list.itemClicked.connect(self.channel_selected)
        left_layout.addWidget(self.channel_list)

        self.grid_layout.addWidget(self.left_panel, 1, 0)
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

        self.grid_layout.addWidget(self.media_controls, 2, 0)

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

        selected_config = self.config['data'][self.config['selected']]
        if 'options' in selected_config:
            self.options = selected_config['options']
            self.token = self.options['headers']['Authorization'].split(" ")[1]
        else:
            self.options = {
                'headers': {
                    'User-Agent': 'Mozilla/5.0 (QtEmbedded; U; Linux; C) AppleWebKit/533.3 (KHTML, like Gecko) MAG200 stbapp ver: 2 rev: 250 Safari/533.3',
                    'Accept-Charset': 'UTF-8,*;q=0.8',
                    'X-User-Agent': 'Model: MAG200; Link: Ethernet',
                    'Content-Type': 'application/json'
                }
            }

        self.url = selected_config.get('url')
        self.mac = selected_config.get('mac')

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
                self.player.play_video(url)
            else:
                print("Failed to create link.")
        else:
            self.player.play_video(cmd)

    def options_dialog(self):
        options = OptionsDialog(self)
        options.exec_()

    @staticmethod
    def parse_m3u(data):
        lines = data.split("\n")
        result = []
        channel = {}
        id = 0
        for line in lines:
            if line.startswith("#EXTINF"):
                tvg_id_match = re.search(r'tvg-id="([^"]+)"', line)
                tvg_logo_match = re.search(r'tvg-logo="([^"]+)"', line)
                group_title_match = re.search(r'group-title="([^"]+)"', line)
                channel_name_match = re.search(r",(.+)", line)

                tvg_id = tvg_id_match.group(1) if tvg_id_match else None
                tvg_logo = tvg_logo_match.group(1) if tvg_logo_match else None
                group_title = group_title_match.group(1) if group_title_match else None
                channel_name = channel_name_match.group(1) if channel_name_match else None

                id += 1
                channel = {"id": id, "name": channel_name}
            elif line.startswith("http"):
                channel["cmd"] = line
                result.append(channel)
        return result

    def do_handshake(self, url, mac, serverload="/server/load.php", load=True):
        token = self.config.get("token") if self.config.get("token") else self.random_token(self)
        options = self.create_options(url, mac, token)
        try:
            fetchurl = f"{url}{serverload}?type=stb&action=handshake&prehash=0&token={token}&JsHttpRequest=1-xml"
            handshake = requests.get(fetchurl, headers=options["headers"])
            body = handshake.json()
            token = body["js"]["token"]
            options["headers"]["Authorization"] = f"Bearer {token}"
            self.config["data"][self.config["selected"]]["options"] = options
            self.save_config()
            if load:
                self.load_stb_channels(url, options)
            return True
        except Exception as e:
            if serverload != "/portal.php":
                serverload = "/portal.php"
                return self.do_handshake(url, mac, serverload)
            print("Error in handshake:", e)
            return False

    def load_stb_channels(self, url, options):
        url = URLObject(url)
        url = f"{url.scheme}://{url.netloc}"
        try:
            fetchurl = f"{url}/server/load.php?type=itv&action=get_all_channels"
            response = requests.get(fetchurl, headers=options["headers"])
            result = response.json()
            channels = result["js"]["data"]
            self.display_channels(channels)
            self.config["data"][self.config["selected"]]["options"] = options
            self.config["data"][self.config["selected"]]["channels"] = channels
            self.save_config()
        except Exception as e:
            print(f"Error loading STB channels: {e}")

    def create_link(self, cmd):
        try:
            selected_provider = self.config["data"][self.config["selected"]]
            url = selected_provider["url"]
            url = URLObject(url)
            url = f"{url.scheme}://{url.netloc}"
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
    def random_token(self):
        return ''.join(random.choices(string.ascii_letters + string.digits, k=32))

    @staticmethod
    def create_options(url, mac, token):
        url = URLObject(url)
        options = {
            "headers": {
                "User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C) AppleWebKit/533.3 (KHTML, like Gecko) MAG200 stbapp ver: 2 rev: 250 Safari/533.3",
                "Accept-Charset": "UTF-8,*;q=0.8",
                "X-User-Agent": "Model: MAG200; Link: Ethernet",
                # "Content-Type": "application/json",
                "Host": f"{url.netloc}",
                "Range": "bytes=0-",
                "Accept": "*/*",
                "Referer": f"{url}/c/" if not url.path else f"{url}/",
                "Cookie": f"mac={mac}; stb_lang=en; timezone=Europe/Kiev; PHPSESSID=null;",
                "Authorization": f"Bearer {token}"
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
