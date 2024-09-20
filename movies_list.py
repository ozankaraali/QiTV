import os
import platform
import random
import re
import shutil
import string
import subprocess
import json

import requests
from PySide6.QtCore import (
    Signal,
    Qt,
)
from PySide6.QtWidgets import (
    QMainWindow,
    QFileDialog,
    QVBoxLayout,
    QWidget,
    QPushButton,
    QListWidget,
    QHBoxLayout,
    QListWidgetItem,
    QLineEdit,
    QGridLayout,
    QCheckBox,
)
from PySide6.QtGui import (
    QColor,
    QPixmap,
    QIcon,
)
from urlobject import URLObject
from urllib.parse import urlparse

from options import OptionsDialog


class MoviesList(QMainWindow):
    movies_loaded = Signal(list)

    def __init__(self, app, player, config_manager):
        super().__init__()
        self.app = app
        self.player = player
        self.config_manager = config_manager
        self.config = self.config_manager.config
        self.config_manager.apply_window_settings("channel_list", self)

        self.setWindowTitle("QiTV Movies List")
        # self.setWindowIcon(QIcon("qitv.ico"))

        self.container_widget = QWidget(self)
        self.setCentralWidget(self.container_widget)
        self.grid_layout = QGridLayout(self.container_widget)

        self.create_upper_panel()
        self.create_left_panel()
        self.create_media_controls()
        self.link = None
        self.load_movies()

    def closeEvent(self, event):
        self.app.quit()
        self.player.close()
        self.config_manager.save_window_settings(self.geometry(), "movies_list")
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

        self.export_button = QPushButton("Export Movies")
        self.export_button.clicked.connect(self.export_movies)
        ctl_layout.addWidget(self.export_button)

        self.update_button = QPushButton("Update Movies")
        self.update_button.clicked.connect(self.update_movies)
        ctl_layout.addWidget(self.update_button)

        self.grid_layout.addWidget(self.upper_layout, 0, 0)

    def create_left_panel(self):
        self.left_panel = QWidget(self.container_widget)
        left_layout = QVBoxLayout(self.left_panel)

        self.search_box = QLineEdit(self.left_panel)
        self.search_box.setPlaceholderText("Search movies...")
        self.search_box.textChanged.connect(
            lambda: self.filter_movies(self.search_box.text())
        )
        left_layout.addWidget(self.search_box)

        self.movies_list = QListWidget(self.left_panel)
        self.movies_list.itemClicked.connect(self.movie_selected)
        left_layout.addWidget(self.movies_list)

        self.grid_layout.addWidget(self.left_panel, 1, 0)
        self.grid_layout.setColumnStretch(0, 1)

        # Add favorite button and action
        self.favorite_button = QPushButton("Favorite/Unfavorite")
        self.favorite_button.clicked.connect(self.toggle_favorite)
        left_layout.addWidget(self.favorite_button)

        # Add checkbox to show only favorites
        self.favorites_only_checkbox = QCheckBox("Show only favorites")
        self.favorites_only_checkbox.stateChanged.connect(
            lambda: self.filter_movies(self.search_box.text())
        )
        left_layout.addWidget(self.favorites_only_checkbox)

    def toggle_favorite(self):
        selected_item = self.movies_list.currentItem()
        if selected_item:
            movie_name = selected_item.text()
            is_favorite = self.check_if_favorite(movie_name)
            if is_favorite:
                self.remove_from_favorites(movie_name)
            else:
                self.add_to_favorites(movie_name)
            self.filter_movies(self.search_box.text())

    def add_to_favorites(self, movie_name):
        if movie_name not in self.config["favorites"]:
            self.config["favorites"].append(movie_name)
            self.save_config()

    def remove_from_favorites(self, movie_name):
        if movie_name in self.config["favorites"]:
            self.config["favorites"].remove(movie_name)
            self.save_config()

    def check_if_favorite(self, movie_name):
        return movie_name in self.config["favorites"]

    def display_movies(self, movies):
        self.movies_list.clear()
        for movie in movies:
            item = QListWidgetItem(movie["name"])
            item.setData(31, movie["cmd"])

            self.movies_list.addItem(item)

            # Mark favorite movies
            if self.check_if_favorite(movie["name"]):
                item.setBackground(
                    QColor(0, 0, 255, 20)
                )  # Optional: change color for favorite movies

    def filter_movies(self, text=""):
        show_favorites = self.favorites_only_checkbox.isChecked()
        search_text = text.lower() if isinstance(text, str) else ""

        for i in range(self.movies_list.count()):
            item = self.movies_list.item(i)
            movie_name = item.text().lower()
            

            matches_search = search_text in movie_name
            is_favorite = self.check_if_favorite(item.text())

            if show_favorites and not is_favorite:
                item.setHidden(True)
            else:
                item.setHidden(not matches_search)

    def create_media_controls(self):
        self.media_controls = QWidget(self.container_widget)
        control_layout = QHBoxLayout(self.media_controls)

        self.play_button = QPushButton("Play/Pause")
        self.play_button.clicked.connect(self.player.toggle_play_pause)
        control_layout.addWidget(self.play_button)

        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(self.player.stop_video)
        control_layout.addWidget(self.stop_button)

        self.vlc_button = QPushButton("Open in VLC")
        self.vlc_button.clicked.connect(self.open_in_vlc)
        control_layout.addWidget(self.vlc_button)

        self.grid_layout.addWidget(self.media_controls, 2, 0)

    def open_in_vlc(self):
        # Invoke user's VLC player to open the current stream
        if self.link:
            try:
                if platform.system() == "Windows":
                    vlc_path = shutil.which("vlc")  # Try to find VLC in PATH
                    if not vlc_path:
                        program_files = os.environ.get(
                            "ProgramFiles", "C:\\Program Files"
                        )
                        vlc_path = os.path.join(
                            program_files, "VideoLAN", "VLC", "vlc.exe"
                        )
                    subprocess.Popen([vlc_path, self.link])
                elif platform.system() == "Darwin":  # macOS
                    vlc_path = shutil.which("vlc")  # Try to find VLC in PATH
                    if not vlc_path:
                        common_paths = [
                            "/Applications/VLC.app/Contents/MacOS/VLC",
                            "~/Applications/VLC.app/Contents/MacOS/VLC",
                        ]
                        for path in common_paths:
                            expanded_path = os.path.expanduser(path)
                            if os.path.exists(expanded_path):
                                vlc_path = expanded_path
                                break
                    subprocess.Popen([vlc_path, self.link])
                else:  # Assuming Linux or other Unix-like OS
                    vlc_path = shutil.which("vlc")  # Try to find VLC in PATH
                    subprocess.Popen([vlc_path, self.link])
                # when VLC opens, stop running video on self.player
                self.player.stop_video()
            except FileNotFoundError as fnf_error:
                print("VLC not found: ", fnf_error)
            except Exception as e:
                print(f"Error opening VLC: {e}")

    def open_file(self):
        file_dialog = QFileDialog(self)
        file_path, _ = file_dialog.getOpenFileName()
        if file_path:
            self.player.play_video(file_path)

    def export_movies(self):
        file_dialog = QFileDialog(self)
        file_dialog.setAcceptMode(QFileDialog.AcceptSave)
        file_dialog.setDefaultSuffix("m3u")
        file_path, _ = file_dialog.getSaveFileName(
            self, "Export movies", "", "M3U files (*.m3u)"
        )
        if file_path:
            provider = self.config["data"][self.config["selected"]]
            movies_data = provider.get("movies", [])
            base_url = provider.get("url", "")
            config_type = provider.get("type", "")
            mac = provider.get("mac", "")

            if config_type == "STB":
                self.save_movie_list(base_url, movies_data, mac, file_path)
            elif config_type in ["M3UPLAYLIST", "M3USTREAM", "XTREAM"]:
                self.save_m3u_movies(movies_data, file_path)
            else:
                print(f"Unknown provider type: {config_type}")

    def save_m3u_movies(self, movies_data, file_path):
        try:
            with open(file_path, "w", encoding="utf-8") as file:
                file.write("#EXTM3U\n")
                count = 0
                for movie in movies_data:
                    name = movie.get("name", "Unknown Movie")
                    logo = movie.get("logo", "")
                    cmd_url = movie.get("cmd")  # Directly get the 'cmd' field

                    if cmd_url:  # Proceed only if cmd_url exists
                        movie_str = (
                            f'#EXTINF:-1 tvg-logo="{logo}" ,{name}\n{cmd_url}\n'
                        )
                        count += 1
                        file.write(movie_str)
                print(f"Movies = {count}")
                print(f"\nMovie list has been dumped to {file_path}")
        except IOError as e:
            print(f"Error saving movie list: {e}")

    def save_movie_list(self, base_url, movies_data, mac, file_path) -> None:
        try:
            with open(file_path, "w", encoding="utf-8") as file:
                file.write("#EXTM3U\n")
                count = 0
                for movie in movies_data:
                    name = movie.get("name", "Unknown Movie")
                    logo = movie.get("logo", "")
                    cmd_url = movie.get("cmd", "").replace("ffmpeg ", "")
                    if "localhost" in cmd_url:
                        ch_id_match = re.search(r"/ch/(\d+)_", cmd_url)
                        if ch_id_match:
                            ch_id = ch_id_match.group(1)
                            cmd_url = f"{base_url}/play/live.php?mac={mac}&stream={ch_id}&extension=m3u8"

                    movie_str = f'#EXTINF:-1 tvg-logo="{logo}" ,{name}\n{cmd_url}\n'
                    count += 1
                    file.write(movie_str)
                print(f"Movies = {count}")
                print(f"\nMovie list has been dumped to {file_path}")
        except IOError as e:
            print(f"Error saving movie list: {e}")

    def save_config(self):
        self.config_manager.save_config()

    def load_movies(self):
        movies = self.config["data"][self.config["selected"]].get("movies", [])
        if movies:
            self.display_movies(movies)
        else:
            self.update_movies()  # If no movies, attempt to update
    

    # Method to manually update movies
    def update_movies(self):
        selected_provider = self.config["data"][self.config["selected"]]
        config_type = selected_provider.get("type", "")
        if config_type == "M3UPLAYLIST":
            self.load_m3u_playlist(selected_provider["url"])
        elif config_type == "XTREAM":
            urlobject = URLObject(selected_provider["url"])
            if urlobject.scheme == "":
                urlobject = URLObject(f"http://{selected_provider['url']}")
            url = f"{urlobject.scheme}://{urlobject.netloc}/get.php?username={selected_provider['username']}&password={selected_provider['password']}&type=m3u"
            self.load_m3u_playlist(url)
        elif config_type == "STB":
            self.do_handshake(
                selected_provider["url"], selected_provider["mac"], load=True
            )
        elif config_type == "M3USTREAM":
            self.load_stream_movie(selected_provider["url"])

    def load_m3u_playlist(self, url):
        try:
            response = requests.get(url)
            if response.status_code == 200:
                movies = self.parse_m3u(response.text)
                self.display_movies(movies)
                # Update the movies in the config
                self.config["data"][self.config["selected"]]["movies"] = movies
                self.save_config()
        except requests.RequestException as e:
            print(f"Error loading M3U Playlist: {e}")

    def load_stream_movie(self, url):
        movie = {"id": 1, "name": "Stream", "cmd": url}
        self.display_movies([movie])
        # Update the movies in the config
        self.config["data"][self.config["selected"]]["movies"] = [movie]
        self.save_config()

    def movie_selected(self, item):
        cmd = item.data(31)
        if self.config["data"][self.config["selected"]]["type"] == "STB":
            url = self.create_link_movies(cmd)
            if url:
                self.link = url
                self.player.play_video(url)
            else:
                print("Failed to create link.")
        else:
            self.link = cmd
            self.player.play_video(cmd)

    def options_dialog(self):
        options = OptionsDialog(self)
        options.exec_()

    @staticmethod
    def parse_m3u(data):
        lines = data.split("\n")
        result = []
        movie = {}
        id = 0
        for line in lines:
            if line.startswith("#EXTINF"):
                tvg_id_match = re.search(r'tvg-id="([^"]+)"', line)
                tvg_logo_match = re.search(r'tvg-logo="([^"]+)"', line)
                group_title_match = re.search(r'group-title="([^"]+)"', line)
                movie_name_match = re.search(r",(.+)", line)

                tvg_id = tvg_id_match.group(1) if tvg_id_match else None
                tvg_logo = tvg_logo_match.group(1) if tvg_logo_match else None
                group_title = group_title_match.group(1) if group_title_match else None
                movie_name = (
                    movie_name_match.group(1) if movie_name_match else None
                )

                id += 1
                movie = {
                    "id": id,
                    "name": movie_name,
                    "logo": tvg_logo,
                }

            elif line.startswith("http"):
                urlobject = urlparse(line)
                movie["cmd"] = urlobject.geturl()
                result.append(movie)
        return result

    def do_handshake(self, url, mac, serverload="/server/load.php", load=True):
        token = (
            self.config.get("token")
            if self.config.get("token")
            else self.random_token(self)
        )
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
                self.load_stb_movies(url, options)
            return True
        except Exception as e:
            if serverload != "/portal.php":
                serverload = "/portal.php"
                return self.do_handshake(url, mac, serverload)
            print("Error in handshake:", e)
            return False
    
    def load_stb_movies(self, url, options):
        url = URLObject(url)
        url = f"{url.scheme}://{url.netloc}"
        Allmovies = []
        try:
            fetchurl = f"{url}/server/load.php?type=vod&action=get_ordered_list"
            response = requests.get(fetchurl, headers=options["headers"])
            result = response.json()
            length = int(result["js"]["total_items"] / result["js"]["max_page_items"])
            for i in range(length): 
                fetchurl = f"{url}/server/load.php?type=vod&action=get_ordered_list&genre=0&category=*&p={i}&sortby=added"
                response = requests.get(fetchurl, headers=options["headers"])
                result = response.json()
                movies = result["js"]["data"]
                Allmovies += movies        
            self.display_movies(Allmovies)
            self.config["data"][self.config["selected"]]["options"] = options
            self.config["data"][self.config["selected"]]["movies"] = movies
            self.save_config()
        except Exception as e:
            print(f"Error")

    def create_link_movies(self, cmd):
        try:
            selected_provider = self.config["data"][self.config["selected"]]
            url = selected_provider["url"]
            url = URLObject(url)
            url = f"{url.scheme}://{url.netloc}"
            options = selected_provider["options"]
            fetchurl = f"{url}/server/load.php?type=vod&action=create_link&type=vod&cmd={requests.utils.quote(cmd)}&JsHttpRequest=1-xml"
            response = requests.get(fetchurl, headers=options["headers"])
            result = response.json()
            link = result["js"]["cmd"].split(" ")[-1]
            self.link = link
            return link
        except Exception as e:
            print(f"Error creating link: {e}")
            return None
        
    @staticmethod
    def random_token(self):
        return "".join(random.choices(string.ascii_letters + string.digits, k=32))

    @staticmethod
    def create_options(url, mac, token):
        url = URLObject(url)
        options = {
            "headers": {
                "User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C) AppleWebKit/533.3 (KHTML, like Gecko) MAG200 stbapp ver: 2 rev: 250 Safari/533.3",
                "Accept-Charset": "UTF-8,*;q=0.8",
                "X-User-Agent": "Model: MAG200; Link: Ethernet",
                "Host": f"{url.netloc}",
                "Range": "bytes=0-",
                "Accept": "*/*",
                "Referer": f"{url}/c/" if not url.path else f"{url}/",
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
            # return response.status_code == 200
            # basically we check if we can connect
            return True if response.status_code else False
        except Exception as e:
            print("Error verifying URL:", e)
            return False
