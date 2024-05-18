import json
import requests
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
from PyQt5.QtCore import pyqtSignal, Qt, QUrl
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QAction, QFileDialog, QVBoxLayout, QWidget, QPushButton, QFrame,
    QListWidget, QHBoxLayout, QListWidgetItem, QLineEdit, QDialog, QLabel, QFormLayout, QGridLayout,
    QRadioButton, QButtonGroup, QComboBox, QMessageBox, QSlider, QStyle, QToolTip
)
from PyQt5.QtMultimedia import QMediaPlayer, QMediaContent
from PyQt5.QtMultimediaWidgets import QVideoWidget

# Attempt to import vlc, set to None if unavailable
try:
    import vlc

    VLC_AVAILABLE = True
except ImportError:
    VLC_AVAILABLE = False

class ProxyHTTPRequestHandler(BaseHTTPRequestHandler):
    parent_app = None  # Reference to VideoPlayer instance
    active_request = False
    lock = threading.Lock()

    def do_GET(self):
        parsed_path = urlparse(self.path)
        query = parse_qs(parsed_path.query)
        stream_url = query.get('url', [None])[0]

        if stream_url:
            headers = ProxyHTTPRequestHandler.parent_app.generate_headers()
            try:
                with ProxyHTTPRequestHandler.lock:
                    if ProxyHTTPRequestHandler.active_request:
                        ProxyHTTPRequestHandler.active_request = False

                    ProxyHTTPRequestHandler.active_request = True

                r = requests.get(stream_url, headers=headers, stream=True)
                self.send_response(r.status_code)
                for key, value in r.headers.items():
                    self.send_header(key, value)
                self.end_headers()

                for chunk in r.iter_content(chunk_size=128):
                    if not ProxyHTTPRequestHandler.active_request:
                        break
                    try:
                        self.wfile.write(chunk)
                    except BrokenPipeError:
                        break

            except requests.RequestException as e:
                self.send_error(500, str(e))

        else:
            self.send_error(400, "Bad request")

    def finish(self):
        if ProxyHTTPRequestHandler.active_request:
            with ProxyHTTPRequestHandler.lock:
                ProxyHTTPRequestHandler.active_request = False
        return super().finish()

class ProxyServerThread(threading.Thread):
    def __init__(self, host, port, handler):
        super().__init__()
        self.server = HTTPServer((host, port), handler)
        self.daemon = True

    def run(self):
        self.server.serve_forever()

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()

class OptionsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Options")
        self.layout = QFormLayout(self)
        self.config = parent.config
        self.selected_provider_index = 0

        self.create_options_ui()
        self.load_providers()

    def create_options_ui(self):
        self.provider_label = QLabel("Select Provider:", self)
        self.provider_combo = QComboBox(self)
        self.provider_combo.currentIndexChanged.connect(self.load_provider_settings)
        self.layout.addRow(self.provider_label, self.provider_combo)

        self.add_provider_button = QPushButton("Add Provider", self)
        self.add_provider_button.clicked.connect(self.add_new_provider)
        self.layout.addWidget(self.add_provider_button)

        self.remove_provider_button = QPushButton("Remove Provider", self)
        self.remove_provider_button.clicked.connect(self.remove_provider)
        self.layout.addWidget(self.remove_provider_button)

        self.create_stream_type_ui()
        self.url_label = QLabel("Server URL:", self)
        self.url_input = QLineEdit(self)
        self.layout.addRow(self.url_label, self.url_input)

        self.mac_label = QLabel("MAC Address (STB only):", self)
        self.mac_input = QLineEdit(self)
        self.layout.addRow(self.mac_label, self.mac_input)

        self.file_button = QPushButton("Load File", self)
        self.file_button.clicked.connect(self.load_file)
        self.layout.addWidget(self.file_button)

        self.verify_button = QPushButton("Verify Provider", self)
        self.verify_button.clicked.connect(self.verify_provider)
        self.layout.addWidget(self.verify_button)
        self.verify_result = QLabel("", self)
        self.layout.addWidget(self.verify_result)
        self.save_button = QPushButton("Save", self)
        self.save_button.clicked.connect(self.save_settings)
        self.layout.addWidget(self.save_button)

    def create_stream_type_ui(self):
        self.type_label = QLabel("Stream Type:", self)
        self.type_group = QButtonGroup(self)
        self.type_STB = QRadioButton("STB", self)
        self.type_M3UPLAYLIST = QRadioButton("M3U Playlist", self)
        self.type_M3USTREAM = QRadioButton("M3U Stream", self)
        self.type_group.addButton(self.type_STB)
        self.type_group.addButton(self.type_M3UPLAYLIST)
        self.type_group.addButton(self.type_M3USTREAM)

        self.type_STB.toggled.connect(self.update_inputs)
        self.type_M3UPLAYLIST.toggled.connect(self.update_inputs)
        self.type_M3USTREAM.toggled.connect(self.update_inputs)

        self.layout.addRow(self.type_label)
        self.layout.addRow(self.type_STB)
        self.layout.addRow(self.type_M3UPLAYLIST)
        self.layout.addRow(self.type_M3USTREAM)

    def load_providers(self):
        self.provider_combo.blockSignals(True)
        self.provider_combo.clear()
        for i, provider in enumerate(self.config["data"]):
            self.provider_combo.addItem(f"Provider {i + 1}", userData=provider)
        self.provider_combo.blockSignals(False)
        self.load_provider_settings(self.selected_provider_index)

    def load_provider_settings(self, index):
        if index == -1 or index >= len(self.config["data"]):
            return
        self.selected_provider_index = index
        self.selected_provider = self.config["data"][index]
        self.url_input.setText(self.selected_provider.get("url", ""))
        self.mac_input.setText(self.selected_provider.get("mac", ""))
        self.update_inputs()

    def update_inputs(self):
        self.type_STB.setChecked(self.selected_provider["type"] == "STB")
        self.type_M3UPLAYLIST.setChecked(self.selected_provider["type"] == "M3UPLAYLIST")
        self.type_M3USTREAM.setChecked(self.selected_provider["type"] == "M3USTREAM")

        self.mac_label.setVisible(self.type_STB.isChecked())
        self.mac_input.setVisible(self.type_STB.isChecked())
        self.file_button.setVisible(self.type_M3UPLAYLIST.isChecked() or self.type_M3USTREAM.isChecked())
        self.url_input.setEnabled(True)

    def add_new_provider(self):
        new_provider = {"type": "STB", "url": "", "mac": ""}
        self.config["data"].append(new_provider)
        self.selected_provider_index = len(self.config["data"]) - 1
        self.load_providers()

    def remove_provider(self):
        if self.provider_combo.currentIndex() == -1:
            return
        del self.config["data"][self.provider_combo.currentIndex()]
        self.selected_provider_index = max(0, self.provider_combo.currentIndex() - 1)
        self.load_providers()

    def save_settings(self):
        if self.selected_provider:
            self.selected_provider["url"] = self.url_input.text()
            self.selected_provider["mac"] = self.mac_input.text() if self.type_STB.isChecked() else ""
            self.selected_provider["type"] = (
                "STB" if self.type_STB.isChecked() else
                "M3UPLAYLIST" if self.type_M3UPLAYLIST.isChecked() else
                "M3USTREAM"
            )
            self.parent().save_config()
            self.parent().load_channels()
            self.accept()

    def load_file(self):
        file_dialog = QFileDialog(self)
        file_path, _ = file_dialog.getOpenFileName()
        if file_path:
            self.url_input.setText(file_path)
            self.file_button.setVisible(False)

    def verify_provider(self):
        self.verify_result.setText("Verifying...")
        self.verify_result.repaint()
        result = False
        if self.type_STB.isChecked():
            result = self.parent().do_handshake(self.url_input.text(), self.mac_input.text(), max_retries=3)
        elif self.type_M3UPLAYLIST.isChecked() or self.type_M3USTREAM.isChecked():
            result = self.parent().verify_url(self.url_input.text())
        self.verify_result.setText("Provider verified successfully." if result else "Failed to verify provider.")
        self.verify_result.setStyleSheet("color: green;" if result else "color: red;")


class VideoPlayer(QMainWindow):
    channels_loaded = pyqtSignal(list)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Not PiTV")
        self.setGeometry(100, 100, 1200, 800)

        self.vlc_available = VLC_AVAILABLE

        self.container_widget = QWidget(self)
        self.setCentralWidget(self.container_widget)
        self.grid_layout = QGridLayout(self.container_widget)

        self.create_ui()
        self.load_config()
        self.load_channels()

        ProxyHTTPRequestHandler.parent_app = self
        self.proxy_server = ProxyServerThread('localhost', 8081, ProxyHTTPRequestHandler)
        self.proxy_server.start()

    def closeEvent(self, event):
        self.proxy_server.stop_server()
        event.accept()

    def create_ui(self):
        self.create_menu()
        self.create_buttons()
        self.create_video_area()
        self.create_left_panel()
        self.create_media_controls()

    def create_menu(self):
        menubar = self.menuBar()
        file_menu = menubar.addMenu('&File')
        open_action = QAction('Open', self)
        open_action.triggered.connect(self.open_file)
        file_menu.addAction(open_action)

        options_action = QAction('Options', self)
        options_action.triggered.connect(self.options_dialog)
        file_menu.addAction(options_action)

    def create_buttons(self):
        self.buttons_widget = QWidget(self.container_widget)
        buttons_layout = QHBoxLayout(self.buttons_widget)
        self.open_button = QPushButton("Open", self)
        self.open_button.clicked.connect(self.open_file)
        buttons_layout.addWidget(self.open_button)
        self.options_button = QPushButton("Options", self)
        self.options_button.clicked.connect(self.options_dialog)
        buttons_layout.addWidget(self.options_button)
        self.grid_layout.addWidget(self.buttons_widget, 0, 1, 1, 1)

    def create_video_area(self):
        if self.vlc_available:
            self.instance = vlc.Instance('--no-xlib', '--vout=gl')
            self.player = self.instance.media_player_new()
        else:
            self.player = QMediaPlayer(None, QMediaPlayer.VideoSurface)
            self.video_widget = QVideoWidget()
            self.player.setVideoOutput(self.video_widget)
            self.grid_layout.addWidget(self.video_widget, 1, 1, 1, 2)
        self.video_frame = QFrame(self.container_widget)
        self.grid_layout.addWidget(self.video_frame, 1, 1, 1, 2)
        self.grid_layout.setColumnStretch(1, 4)

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
        self.grid_layout.addWidget(self.left_panel, 1, 0, 1, 1)
        self.grid_layout.setColumnStretch(0, 1)

    def create_media_controls(self):
        self.media_controls = QWidget(self.container_widget)
        control_layout = QHBoxLayout(self.media_controls)

        self.play_button = QPushButton()
        self.play_button.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        self.play_button.clicked.connect(self.play_pause)
        control_layout.addWidget(self.play_button)

        self.stop_button = QPushButton()
        self.stop_button.setIcon(self.style().standardIcon(QStyle.SP_MediaStop))
        self.stop_button.clicked.connect(self.stop)
        control_layout.addWidget(self.stop_button)

        self.fullscreen_button = QPushButton("Fullscreen", self)
        self.fullscreen_button.clicked.connect(self.toggle_fullscreen)
        control_layout.addWidget(self.fullscreen_button)

        self.position_slider = QSlider(Qt.Horizontal)
        self.position_slider.setRange(0, 0)
        self.position_slider.sliderMoved.connect(self.set_position)
        control_layout.addWidget(self.position_slider)

        self.grid_layout.addWidget(self.media_controls, 2, 0, 1, 3)

        if not self.vlc_available:
            self.player.positionChanged.connect(self.position_changed)
            self.player.durationChanged.connect(self.duration_changed)

    def set_position(self, position):
        self.player.setPosition(position)

    def position_changed(self, position):
        self.position_slider.setValue(position)

    def duration_changed(self, duration):
        self.position_slider.setRange(0, duration)

    def play_pause(self):
        if self.vlc_available:
            state = self.player.get_state()
            if state == vlc.State.Playing:
                self.player.pause()
                self.play_button.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
            else:
                self.player.play()
                self.play_button.setIcon(self.style().standardIcon(QStyle.SP_MediaPause))
        else:
            state = self.player.state()
            if state == QMediaPlayer.PlayingState:
                self.player.pause()
                self.play_button.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
            else:
                self.player.play()
                self.play_button.setIcon(self.style().standardIcon(QStyle.SP_MediaPause))

    def stop(self):
        if self.vlc_available:
            self.player.stop()
        else:
            self.player.stop()
        self.play_button.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))

    def toggle_fullscreen(self):
        if self.vlc_available:
            self.toggle_fullscreen_custom(self.video_frame)
        else:
            self.toggle_fullscreen_custom(self.video_widget)

    def toggle_fullscreen_custom(self, widget):
        if widget.isFullScreen():
            widget.setWindowFlags(Qt.Widget)
            widget.showNormal()
        else:
            widget.setWindowFlags(Qt.Window)
            widget.showFullScreen()

    def open_file(self):
        file_dialog = QFileDialog(self)
        file_path, _ = file_dialog.getOpenFileName()
        if file_path != '':
            self.play_video(file_path)

    def play_video(self, file_path):
        if self.vlc_available:
            self.play_with_vlc(file_path)
        else:
            self.play_with_qt(file_path)

    def play_with_vlc(self, file_path):
        if sys.platform.startswith('linux'):
            self.player.set_xwindow(int(self.video_frame.winId()))
        elif sys.platform == "win32":
            self.player.set_hwnd(int(self.video_frame.winId()))
        elif sys.platform == "darwin":
            self.player.set_nsobject(int(self.video_frame.winId()))
        media = self.instance.media_new(file_path)
        self.player.set_media(media)
        self.player.play()
        self.play_button.setIcon(self.style().standardIcon(QStyle.SP_MediaPause))

    def play_with_qt(self, file_path):
        self.player.setMedia(QMediaContent(QUrl.fromLocalFile(file_path)))
        self.player.play()
        self.play_button.setIcon(self.style().standardIcon(QStyle.SP_MediaPause))

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
                proxy_url = f"http://localhost:8081/?url={requests.utils.quote(url)}"
                self.play_video(proxy_url)
            else:
                print("Failed to create link.")
        else:
            proxy_url = f"http://localhost:8081/?url={requests.utils.quote(cmd)}"
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
