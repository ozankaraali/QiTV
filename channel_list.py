import asyncio
import os
import json
import platform
import random
import re
import shutil
import string
import subprocess
import time
from urllib.parse import urlparse
from collections import OrderedDict

import aiohttp
import requests
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QRadioButton,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from urlobject import URLObject

from options import OptionsDialog


class AsyncWorker(QThread):
    finished = Signal(object)

    def __init__(self, coro):
        super().__init__()
        self.coro = coro

    def run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(self.coro)
            self.finished.emit(result)
        finally:
            loop.close()


class ChannelList(QMainWindow):
    content_loaded = Signal(list)

    def __init__(self, app, player, config_manager):
        super().__init__()
        self.app = app
        self.player = player
        self.config_manager = config_manager
        self.config = self.config_manager.config
        self.config_manager.apply_window_settings("channel_list", self)

        self.setWindowTitle("QiTV Content List")

        self.container_widget = QWidget(self)
        self.setCentralWidget(self.container_widget)
        self.grid_layout = QGridLayout(self.container_widget)

        self.content_type = "channels"  # Default to channels

        self.create_upper_panel()
        self.create_left_panel()
        self.create_media_controls()
        self.link = None
        self.workers = []
        self.load_content()

    def closeEvent(self, event):
        self.app.quit()
        self.player.close()
        self.config_manager.save_window_settings(self.geometry(), "channel_list")

        # Clean up workers
        for worker in self.workers:
            worker.quit()
            worker.wait()

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

        self.export_button = QPushButton("Export Content")
        self.export_button.clicked.connect(self.export_content)
        ctl_layout.addWidget(self.export_button)

        self.update_button = QPushButton("Update Content")
        self.update_button.clicked.connect(self.update_content)
        ctl_layout.addWidget(self.update_button)

        self.grid_layout.addWidget(self.upper_layout, 0, 0)

    def create_left_panel(self):
        self.left_panel = QWidget(self.container_widget)
        left_layout = QVBoxLayout(self.left_panel)

        self.search_box = QLineEdit(self.left_panel)
        self.search_box.setPlaceholderText("Search content...")
        self.search_box.textChanged.connect(
            lambda: self.filter_content(self.search_box.text())
        )
        left_layout.addWidget(self.search_box)

        self.content_list = QListWidget(self.left_panel)
        self.content_list.itemClicked.connect(self.item_selected)
        left_layout.addWidget(self.content_list)

        self.grid_layout.addWidget(self.left_panel, 1, 0)
        self.grid_layout.setColumnStretch(0, 1)

        # Add a row with Previous page and Next page buttons below the content_list (for STB navigation in page)
        self.stb_page_controls = QWidget(self.container_widget)
        stb_page_layout = QHBoxLayout(self.stb_page_controls)
        
        self.stb_first_page_button = QPushButton("\u27EA")
        self.stb_first_page_button.clicked.connect(self.stb_first_page)
        stb_page_layout.addWidget(self.stb_first_page_button)

        self.stb_prev_page_button = QPushButton("\u27E8")
        self.stb_prev_page_button.clicked.connect(self.stb_prev_page)
        stb_page_layout.addWidget(self.stb_prev_page_button)
        
        self.stb_current_page_button = QPushButton("Page x/x")
        self.stb_current_page_button.clicked.connect(self.stb_next_page)
        stb_page_layout.addWidget(self.stb_current_page_button)

        self.stb_next_page_button = QPushButton("\u27E9")
        self.stb_next_page_button.clicked.connect(self.stb_next_page)
        stb_page_layout.addWidget(self.stb_next_page_button)
        
        self.stb_last_page_button = QPushButton("\u27EB")
        self.stb_last_page_button.clicked.connect(self.stb_last_page)
        stb_page_layout.addWidget(self.stb_last_page_button)
        
        left_layout.addWidget(self.stb_page_controls)

        # Disable the stb_current_page_button
        self.stb_current_page_button.setEnabled(False)

        # Hide the STB page controls by default
        self.stb_page_controls.hide()

        # Add a row with a back to category (STB only) and favorite button
        self.page_back_fav_controls = QWidget(self.container_widget)
        page_back_fav_layout = QHBoxLayout(self.page_back_fav_controls)

        self.stb_page_back_button = QPushButton("Back")
        self.stb_page_back_button.clicked.connect(self.back_content)
        page_back_fav_layout.addWidget(self.stb_page_back_button)

        self.favorite_button = QPushButton("Favorite/Unfavorite")
        self.favorite_button.clicked.connect(self.toggle_favorite)
        page_back_fav_layout.addWidget(self.favorite_button)

        left_layout.addWidget(self.page_back_fav_controls)

        # Hide the Back/Favorite controls by default
        self.page_back_fav_controls.hide()

        # Add checkbox to show only favorites
        self.favorites_only_checkbox = QCheckBox("Show only favorites")
        self.favorites_only_checkbox.stateChanged.connect(
            lambda: self.filter_content(self.search_box.text())
        )
        left_layout.addWidget(self.favorites_only_checkbox)

        # Add radio buttons to switch between Channels / Movies / Series
        self.content_switch_controls = QWidget(self.container_widget)
        content_switch_layout = QHBoxLayout(self.content_switch_controls)

        rb_channels = QRadioButton('Channels', self.content_switch_controls)
        rb_channels.setChecked(True)
        rb_channels.toggled.connect(self.toggle_content_type)

        rb_movies = QRadioButton('Movies', self.content_switch_controls)
        rb_movies.toggled.connect(self.toggle_content_type)

        rb_series = QRadioButton('Series', self.content_switch_controls)
        rb_series.toggled.connect(self.toggle_content_type)

        content_switch_layout.addWidget(rb_channels)
        content_switch_layout.addWidget(rb_movies)
        content_switch_layout.addWidget(rb_series)

        left_layout.addWidget(self.content_switch_controls)

    def stb_first_page(self):
        category = self.stb_navigation["category"]
        page = self.stb_navigation["page"]
        if page > 1:
            self.load_stb_content_by_category(category, 1)

    def stb_prev_page(self):
        category = self.stb_navigation["category"]
        page = self.stb_navigation["page"]
        if page > 1:
            self.load_stb_content_by_category(category, page - 1)

    def stb_next_page(self):
        category = self.stb_navigation["category"]
        page = self.stb_navigation["page"]
        page_count = self.stb_navigation["page_count"]
        if page < page_count:
            self.load_stb_content_by_category(category, page + 1)

    def stb_last_page(self):
        category = self.stb_navigation["category"]
        page = self.stb_navigation["page"]
        page_count = self.stb_navigation["page_count"]
        if page < page_count:
            self.load_stb_content_by_category(category, page_count)

    def toggle_favorite(self):
        selected_item = self.content_list.currentItem()
        if selected_item and selected_item.data(30) == "content":
            item_name = selected_item.text()
            is_favorite = self.check_if_favorite(item_name)
            if is_favorite:
                self.remove_from_favorites(item_name)
            else:
                self.add_to_favorites(item_name)
            self.filter_content(self.search_box.text())

    def add_to_favorites(self, item_name):
        if item_name not in self.config["favorites"]:
            self.config["favorites"].append(item_name)
            self.save_config()

    def remove_from_favorites(self, item_name):
        if item_name in self.config["favorites"]:
            self.config["favorites"].remove(item_name)
            self.save_config()

    def check_if_favorite(self, item_name):
        return item_name in self.config["favorites"]

    def toggle_content_type(self):
        state = self.sender().isChecked()
        if state:
            if self.sender().text() == "Channels":
                self.content_type = "channels"
            elif self.sender().text() == "Movies":
                self.content_type = "movies"
            elif self.sender().text() == "Series":
                self.content_type = "series"
            self.load_content()

    def show_back_fav(self, show):
        # Hide/Show the Back/Favorite controls
        if show:
            self.page_back_fav_controls.show()
        else:
            self.page_back_fav_controls.hide()

        # Show the Back to Category button if provider is STB
        if self.config["data"][self.config["selected"]]["type"] == "STB":
            self.stb_page_back_button.show()
        else:
            self.stb_page_back_button.hide()

    def show_pagination(self, show, page = 1, page_count = 1):
        # Show/Hide the STB page controls
        if show:
            self.stb_page_controls.show()
        else:
            self.stb_page_controls.hide()

        # Update the STB navigation buttons
        if show:
            self.stb_current_page_button.setText(f"Page {page}/{page_count}")
            self.stb_prev_page_button.setEnabled(page > 1)
            self.stb_next_page_button.setEnabled(page < page_count)
            self.stb_first_page_button.setEnabled(page > 1)
            self.stb_last_page_button.setEnabled(page < page_count)

    def display_content(self, items):
        self.show_pagination(False)
        self.show_back_fav(True)

        self.content_list.clear()
        for item in items:
            list_item = QListWidgetItem(item["name"])
            list_item.setData(30, 'content')
            list_item.setData(31, item)
            self.content_list.addItem(list_item)
            if self.check_if_favorite(item["name"]):
                list_item.setBackground(QColor(0, 0, 255, 20))

    def display_paginated_content(self, items, page, page_count):
        self.display_content(items)
        self.show_pagination(True, page, page_count)
        
    def display_categories(self, items):
        self.show_pagination(False)
        self.show_back_fav(False)

        self.content_list.clear()
        for item in items:
            list_item = QListWidgetItem(item["title"])
            list_item.setData(30, 'category')
            list_item.setData(31, item)
            self.content_list.addItem(list_item)

    def display_paginated_series(self, items, page, page_count):
        self.show_pagination(True, page, page_count)
        self.show_back_fav(True)

        self.content_list.clear()
        for item in items:
            list_item = QListWidgetItem(item["name"])
            list_item.setData(30, 'serie')
            list_item.setData(31, item)
            self.content_list.addItem(list_item)

    def display_seasons(self, items):
        self.show_pagination(False)
        self.show_back_fav(True)

        self.content_list.clear()
        for item in items:
            list_item = QListWidgetItem(item["name"])
            list_item.setData(30, 'season')
            list_item.setData(31, item)
            self.content_list.addItem(list_item)

    def display_episodes(self, item):
        self.show_pagination(False)
        self.show_back_fav(True)

        self.content_list.clear()
        for episode in item.get("series", []):
            list_item = QListWidgetItem(f"Episode {episode}")
            list_item.setData(30, 'episode')
            list_item.setData(31, item)
            list_item.setData(32, episode)
            self.content_list.addItem(list_item)

    def filter_content(self, text=""):
        show_favorites = self.favorites_only_checkbox.isChecked()
        search_text = text.lower() if isinstance(text, str) else ""

        # Check if the current content is a list of folder by checking the first item
        if self.content_list.count() > 0:
            first_item = self.content_list.item(0)
            is_folder = first_item.data(30) in ["category", "serie", "season"]

        for i in range(self.content_list.count()):
            item = self.content_list.item(i)
            item_name = item.text().lower()

            matches_search = search_text in item_name
            if show_favorites and not is_folder:
                is_favorite = self.check_if_favorite(item.text())

            if show_favorites and not is_folder and not is_favorite:
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

    def export_content(self):
        file_dialog = QFileDialog(self)
        file_dialog.setAcceptMode(QFileDialog.AcceptSave)
        file_dialog.setDefaultSuffix("m3u")
        file_path, _ = file_dialog.getSaveFileName(
            self, "Export Content", "", "M3U files (*.m3u)"
        )
        if file_path:
            provider = self.config["data"][self.config["selected"]]
            base_url = provider.get("url", "")
            config_type = provider.get("type", "")
            mac = provider.get("mac", "")

            if config_type == "STB":
                content_data = provider.get(self.content_type, {}).get("contents", [])
                self.save_stb_content(base_url, content_data, mac, file_path)
            elif config_type in ["M3UPLAYLIST", "M3USTREAM", "XTREAM"]:
                content_data = provider.get(self.content_type, [])
                self.save_m3u_content(content_data, file_path)
            else:
                print(f"Unknown provider type: {config_type}")

    def save_m3u_content(self, content_data, file_path):
        try:
            with open(file_path, "w", encoding="utf-8") as file:
                file.write("#EXTM3U\n")
                count = 0
                for item in content_data:
                    name = item.get("name", "Unknown")
                    logo = item.get("logo", "")
                    cmd_url = item.get("cmd")

                    if cmd_url:
                        item_str = f'#EXTINF:-1 tvg-logo="{logo}" ,{name}\n{cmd_url}\n'
                        count += 1
                        file.write(item_str)
                print(f"Items exported: {count}")
                print(f"\nContent list has been saved to {file_path}")
        except IOError as e:
            print(f"Error saving content list: {e}")

    def save_stb_content(self, base_url, content_data, mac, file_path):
        try:
            with open(file_path, "w", encoding="utf-8") as file:
                file.write("#EXTM3U\n")
                count = 0
                for item in content_data:
                    name = item.get("name", "Unknown")
                    logo = item.get("logo", "")
                    cmd_url = item.get("cmd", "").replace("ffmpeg ", "")

                    # Generalized URL construction
                    if "localhost" in cmd_url:
                        id_match = re.search(r"/(ch|vod)/(\d+)_", cmd_url)
                        if id_match:
                            content_type = id_match.group(1)
                            content_id = id_match.group(2)
                            if content_type == "ch":
                                cmd_url = f"{base_url}/play/live.php?mac={mac}&stream={content_id}&extension=m3u8"
                            elif content_type == "vod":
                                cmd_url = f"{base_url}/play/vod.php?mac={mac}&stream={content_id}&extension=m3u8"

                    item_str = f'#EXTINF:-1 tvg-logo="{logo}" ,{name}\n{cmd_url}\n'
                    count += 1
                    file.write(item_str)
                print(f"Items exported: {count}")
                print(f"\nContent list has been saved to {file_path}")
        except IOError as e:
            print(f"Error saving content list: {e}")

    def save_config(self):
        self.config_manager.save_config()

    def load_content(self):
        selected_provider = self.config["data"][self.config["selected"]]
        config_type = selected_provider.get("type", "")
        content = selected_provider.get(self.content_type, {})
        if content:
            if config_type == "STB":
                worker = AsyncWorker(self.do_handshake(selected_provider["url"], selected_provider["mac"]))
                worker.finished.connect(lambda result, load=False: self.on_handshake_complete(result, load))
                worker.start()
                self.workers.append(worker)
                self.display_categories(content["categories"])
            else:
                self.display_channels(content)
        else:
            self.update_content()

    def back_content(self):
        if self.content_type in ["channels", "movies"]:
            self.load_content()
        else:
            selected_provider = self.config["data"][self.config["selected"]]
            folder_type = self.stb_navigation["folder_type"]
            if folder_type == "series":
                self.load_content()
            elif folder_type == "seasons":
                category = self.stb_navigation["category"]
                page = self.stb_navigation["page"]
                self.load_stb_content_by_category(category, page)
            elif folder_type == "episodes":
                serie = self.stb_navigation["serie"]
                self.load_stb_seasons_by_serie(serie)

    def update_content(self):
        selected_provider = self.config["data"][self.config["selected"]]
        config_type = selected_provider.get("type", "")
        if config_type == "M3UPLAYLIST":
            self.load_m3u_playlist(selected_provider["url"])
        elif config_type == "XTREAM":
            urlobject = URLObject(selected_provider["url"])
            if urlobject.scheme == "":
                urlobject = URLObject(f"http://{selected_provider['url']}")
            if self.content_type == "channels":
                url = (
                    f"{urlobject.scheme}://{urlobject.netloc}/get.php?"
                    f"username={selected_provider['username']}&password={selected_provider['password']}&type=m3u"
                )
            elif self.content_type == "movies":
                url = (
                    f"{urlobject.scheme}://{urlobject.netloc}/get.php?"
                    f"username={selected_provider['username']}&password={selected_provider['password']}&type=m3u&"
                    "contentType=vod"
                )
            self.load_m3u_playlist(url)
        elif config_type == "STB":
            worker = AsyncWorker(self.do_handshake(selected_provider["url"], selected_provider["mac"]))
            worker.finished.connect(lambda result, load=True: self.on_handshake_complete(result, load))
            worker.start()
            self.workers.append(worker)
        elif config_type == "M3USTREAM":
            self.load_stream(selected_provider["url"])

    def load_m3u_playlist(self, url):
        async def fetch_m3u():
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    if response.status == 200:
                        content = await response.text()
                        return self.parse_m3u(content)
                    else:
                        return []

        worker = AsyncWorker(fetch_m3u())
        worker.finished.connect(self.on_m3u_loaded)
        worker.start()
        self.workers.append(worker)  # Keep a reference to the worker

    def on_m3u_loaded(self, content):
        self.display_content(content)
        self.config["data"][self.config["selected"]][self.content_type] = content
        self.save_config()

    def load_stream(self, url):
        item = {"id": 1, "name": "Stream", "cmd": url}
        self.display_content([item])
        # Update the content in the config
        self.config["data"][self.config["selected"]][self.content_type] = [item]
        self.save_config()

    def item_selected(self, item):
        typ = item.data(30)
        if typ == "content":
            if self.config["data"][self.config["selected"]]["type"] == "STB":
                self.create_link(item.data(31))
            else:
                cmd = item.data(31)
                self.link = cmd
                self.player.play_video(cmd)
        elif typ == "category":
            self.load_stb_content_by_category(item.data(31))
        elif typ == "serie":
            self.load_stb_seasons_by_serie(item.data(31))
        elif typ == "season":
            self.load_stb_episodes_by_season(item.data(31))
        elif typ == "episode":
            season = item.data(31)
            episode = item.data(32)
            self.create_link_episode(season, episode)

    def options_dialog(self):
        options = OptionsDialog(self)
        options.exec_()

    def ContentTypeToSTB(self):
        # Convert content type to STB type (itv, vod, series)
        if self.content_type == "channels":
            return "itv"
        elif self.content_type == "movies":
            return "vod"
        elif self.content_type == "series":
            return "series"
        else:
            return None

    @staticmethod
    def parse_m3u(data):
        lines = data.split("\n")
        result = []
        item = {}
        id = 0
        for line in lines:
            if line.startswith("#EXTINF"):
                tvg_id_match = re.search(r'tvg-id="([^"]+)"', line)
                tvg_logo_match = re.search(r'tvg-logo="([^"]+)"', line)
                group_title_match = re.search(r'group-title="([^"]+)"', line)
                item_name_match = re.search(r",(.+)", line)

                tvg_id = tvg_id_match.group(1) if tvg_id_match else None
                tvg_logo = tvg_logo_match.group(1) if tvg_logo_match else None
                group_title = group_title_match.group(1) if group_title_match else None
                item_name = item_name_match.group(1) if item_name_match else None

                id += 1
                item = {
                    "id": id,
                    "name": item_name,
                    "logo": tvg_logo,
                }

            elif line.startswith("http"):
                urlobject = urlparse(line)
                item["cmd"] = urlobject.geturl()
                result.append(item)
        return result

    async def do_handshake(self, url, mac, serverload="/server/load.php"):
        token = self.config.get("token") or self.random_token()
        options = self.create_options(url, mac, token)
        fetchurl = f"{url}{serverload}?{self.getHandshakeParams(token)}"

        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(fetchurl, headers=options["headers"]) as response:
                    if response.status == 200:
                        try:
                            body = await response.json()
                        except aiohttp.ContentTypeError:
                            body = await response.text()
                            body = json.loads(body)
                        token = body["js"]["token"]
                        options["headers"]["Authorization"] = f"Bearer {token}"
                        self.config["data"][self.config["selected"]]["options"] = options
                        return True
                    else:
                        print(f"Handshake failed with status code: {response.status}")
                        return False
            except aiohttp.ClientError as e:
                print(f"Error in handshake: {e}")
                if serverload != "/portal.php":
                    return await self.do_handshake(url, mac, "/portal.php")
                return False

    def on_handshake_complete(self, success, load):
        if success:
            selected_provider = self.config["data"][self.config["selected"]]
            options = selected_provider["options"]
            if load:
                self.load_stb_categories(selected_provider["url"], options)
        else:
            print("Handshake failed")

    def load_stb_categories(self, url, options):
        url = URLObject(url)
        url = f"{url.scheme}://{url.netloc}"
        async def fetch_categories():
            async with aiohttp.ClientSession() as session:
                try:
                    fetchurl = f"{url}/server/load.php?{self.getCategoriesParams(self.ContentTypeToSTB())}"
                    async with session.get(fetchurl, headers=options["headers"]) as response:
                        try:
                            result = await response.json()
                        except aiohttp.ContentTypeError:
                            result = await response.text()
                            result = json.loads(result)
                        return result["js"]
                except aiohttp.ClientError as e:
                    print(f"Error fetching categories: {e}")
                    return None

        worker = AsyncWorker(fetch_categories())
        worker.finished.connect(self.on_stb_categories_loaded)
        worker.start()
        self.workers.append(worker)  # Keep a reference to the worker

    def on_stb_categories_loaded(self, categories):
        if categories is None:
            print("Error loading categories")
            return
        selected_provider = self.config["data"][self.config["selected"]]
        url = URLObject(selected_provider["url"])
        url = f"{url.scheme}://{url.netloc}"

        selected_provider[self.content_type] = {}
        selected_provider[self.content_type]["categories"] = categories

        async def fetch_allchannels():
            async with aiohttp.ClientSession() as session:
                try:
                    fetchurl = f"{url}/server/load.php?{self.getAllChannelsParams()}"
                    async with session.get(fetchurl, headers=self.generate_headers()) as response:
                        try:
                            result = await response.json()
                        except aiohttp.ContentTypeError:
                            result = await response.text()
                            result = json.loads(result)
                        return result["js"]["data"]
                except aiohttp.ClientError as e:
                    print(f"Error fetching channels: {e}")
                    return None

        # Fetching all channels
        if self.content_type == "channels":
            worker = AsyncWorker(fetch_allchannels())
            worker.finished.connect(self.on_stb_allchannels_loaded)
            worker.start()
            self.workers.append(worker)  # Keep a reference to the worker
        else:
            self.display_categories(categories)

    def on_stb_allchannels_loaded(self, items):
        if items is None:
            print("Error loading channels")
            return

        # Sorting all channels by category
        content = self.config["data"][self.config["selected"]][self.content_type]
        content["contents"] = items

        # Split channels by category, and sort them number-wise
        sorted_channels = {}

        for i in range(len(content["contents"])):
            genre_id = content["contents"][i]["tv_genre_id"]
            category = str(genre_id)
            if category not in sorted_channels:
                sorted_channels[category] = []
            sorted_channels[category].append(i)

        for category in sorted_channels:
            sorted_channels[category].sort(key=lambda x: int(content["contents"][x]["number"]))

        # Prepend a specific category for null genre_id before
        if "None" in sorted_channels:
            content["categories"].insert(0, {
                "id": "None",
                "title": "No Category"
                })

        content["sorted_channels"] = sorted_channels
        self.display_categories(content["categories"])

    def load_stb_content_by_category(self, category, page=0):
        category_id = category["id"]

        async def fetch_content(category_id, page):
            async with aiohttp.ClientSession() as session:
                try:
                    selected_provider = self.config["data"][self.config["selected"]]
                    url = URLObject(selected_provider["url"])
                    url = f"{url.scheme}://{url.netloc}"
                    options = selected_provider["options"]
                    if not page:
                        fetchurl = f"{url}/server/load.php?{self.getChannelOrSeriesParams(self.ContentTypeToSTB(), category_id, 'name', 1, 0, 0)}"
                        async with session.get(fetchurl, headers=options["headers"]) as response:
                            try:
                                result = await response.json()
                            except aiohttp.ContentTypeError:
                                result = await response.text()
                                result = json.loads(result)
                            total_items = int(result["js"]["total_items"])
                            max_page_items = int(result["js"]["max_page_items"])
                            page_count = (total_items + max_page_items - 1) // max_page_items
                            page = 1
                            if page_count and page > page_count:
                                return None
                            items = result["js"]["data"]
                    else:
                        # Load content for the category and the page
                        page_count = self.stb_navigation["page_count"]
                        if page <= page_count:
                            fetchurl = f"{url}/server/load.php?{self.getChannelOrSeriesParams(self.ContentTypeToSTB(), category_id, 'name', page, 0, 0)}"
                            async with session.get(fetchurl, headers=options["headers"]) as response:
                                try:
                                    result = await response.json()
                                except aiohttp.ContentTypeError:
                                    result = await response.text()
                                    result = json.loads(result)
                                items = result["js"]["data"]
                    return (page, page_count, items)

                except aiohttp.ClientError as e:
                    print(f"Error fetching content by category: {e}")
                    return None
        try:
            if self.content_type == "channels":
                selected_provider = self.config["data"][self.config["selected"]]
                content = selected_provider[self.content_type]
                # Show only channels for the selected category
                if category_id == "*":
                    items = content["contents"]
                    # Sort channels by number
                    items.sort(key=lambda x: int(x["number"]))
                else:
                    items = [content["contents"][i] for i in content["sorted_channels"].get(category_id, [])]
                self.display_content(items)
            else:
                worker = AsyncWorker(fetch_content(category_id, page))
                worker.finished.connect(lambda result, cat=category: self.on_stb_content_by_category_loaded(result, cat))
                worker.start()
                self.workers.append(worker)  # Keep a reference to the worker
        except Exception as e:
            print(f"Error loading STB content by category: {e}")

    def on_stb_content_by_category_loaded(self, data, category):
        if data is None:
            print("Error loading STB content by category")
            return

        page, page_count, items = data

        if self.content_type == "movies":
            self.display_paginated_content(items, page, page_count)
        elif self.content_type == "series":
            self.display_paginated_series(items, page, page_count)

        # Update the config with the navigation data
        self.stb_navigation = {
            "folder_type": self.content_type,
            "category": category,
            "page": page,
            "page_count": page_count
            }

    def load_stb_seasons_by_serie(self, serie):
        selected_provider = self.config["data"][self.config["selected"]]
        url = selected_provider["url"]
        url = URLObject(url)
        url = f"{url.scheme}://{url.netloc}"
        options = selected_provider["options"]

        async def fetch_content(serie):
            async with aiohttp.ClientSession() as session:
                try:
                    selected_provider = self.config["data"][self.config["selected"]]
                    url = URLObject(selected_provider["url"])
                    url = f"{url.scheme}://{url.netloc}"
                    options = selected_provider["options"]
                    fetchurl = f"{url}/server/load.php?{self.getChannelOrSeriesParams(self.ContentTypeToSTB(), serie['category_id'], 'added', 1, serie['id'], 0)}"

                    async with session.get(fetchurl, headers=options["headers"]) as response:
                        try:
                            result = await response.json()
                        except aiohttp.ContentTypeError:
                            result = await response.text()
                            result = json.loads(result)
                        total_items = int(result["js"]["total_items"])
                        max_page_items = int(result["js"]["max_page_items"])
                        pages = (total_items + max_page_items - 1) // max_page_items

                        tasks = []
                        for page in range(pages):
                            page_url = f"{url}/server/load.php?{self.getChannelOrSeriesParams(self.ContentTypeToSTB(), serie['category_id'], 'added', page+1, serie['id'], 0)}"
                            tasks.append(session.get(page_url, headers=options["headers"]))

                        responses = await asyncio.gather(*tasks)
                        items = []
                        for response in responses:
                            try:
                                result = await response.json()
                            except aiohttp.ContentTypeError:
                                result = await response.text()
                                result = json.loads(result)
                            items.extend(result["js"]["data"])
                        return items
                except aiohttp.ClientError as e:
                    print(f"Error fetching seasons by serie: {e}")
                    return None

        worker = AsyncWorker(fetch_content(serie))
        worker.finished.connect(lambda result, serie=serie: self.on_stb_seasons_by_serie_loaded(result, serie))
        worker.start()
        self.workers.append(worker)  # Keep a reference to the worker

    def on_stb_seasons_by_serie_loaded(self, items, serie):
        if items is None:
            print("Error loading STB seasons by serie")
            return

        self.display_seasons(items)

        # Update the config with the navigation data
        self.stb_navigation["folder_type"] = "seasons"
        self.stb_navigation["serie"] = serie

    def load_stb_episodes_by_season(self, season):
        try:
            self.display_episodes(season)

            # Update the config with the navigation data
            self.stb_navigation["folder_type"] = "episodes"
            self.stb_navigation["season"] = season

        except Exception as e:
            print(f"Error loading STB episode by season: {e}")

    def create_link(self, item):
        async def fetch_link():
            try:
                selected_provider = self.config["data"][self.config["selected"]]
                url = URLObject(selected_provider["url"])
                url = f"{url.scheme}://{url.netloc}"
                fetchurl = f"{url}/server/load.php?{self.getLinkParams(self.ContentTypeToSTB(), requests.utils.quote(item['cmd']), 0)}"
                async with aiohttp.ClientSession() as session:
                    async with session.get(fetchurl, headers=self.generate_headers()) as response:
                        if response.status == 200:
                            try:
                                result = await response.json()
                            except aiohttp.ContentTypeError:
                                result = await response.text()
                                result = json.loads(result)
                            link = result["js"]["cmd"].split(" ")[-1]
                            return link
                        else:
                            print(f"Error creating link. Status code: {response.status}")
                            return None
            except Exception as e:
                print(f"Error creating link: {e}")
                return None

        worker = AsyncWorker(fetch_link())
        worker.finished.connect(self.on_link_created)
        worker.start()
        self.workers.append(worker)  # Keep a reference to the worker

    def on_link_created(self, link):
        if link:
            self.link = link
            self.player.play_video(link)
        else:
            print("Failed to create link.")

    def create_link_episode(self, season, episode):
        async def fetch_link():
            try:
                selected_provider = self.config["data"][self.config["selected"]]
                url = URLObject(selected_provider["url"])
                url = f"{url.scheme}://{url.netloc}"
                fetchurl = f"{url}/server/load.php?{self.getLinkParams(self.ContentTypeToSTB(), requests.utils.quote(season['cmd']), episode)}"
                async with aiohttp.ClientSession() as session:
                    async with session.get(fetchurl, headers=self.generate_headers()) as response:
                        if response.status == 200:
                            try:
                                result = await response.json()
                            except aiohttp.ContentTypeError:
                                result = await response.text()
                                result = json.loads(result)
                            link = result["js"]["cmd"].split(" ")[-1]
                            return link
                        else:
                            print(f"Error creating link for episode. Status code: {response.status}")
                            return None
            except Exception as e:
                print(f"Error creating link for episode: {e}")
                return None
        worker = AsyncWorker(fetch_link())
        worker.finished.connect(self.on_link_episode_created)
        worker.start()
        self.workers.append(worker)  # Keep a reference to the worker

    def on_link_episode_created(self, link):
        if link:
            self.link = link
            self.player.play_video(link)
        else:
            print("Failed to create link.")

    @staticmethod
    def getHandshakeParams(token):
        params = OrderedDict()
        params["type"] = "stb"
        params["action"] = "handshake"
        params["prehash"] = "0"
        params["token"] = token
        params["JsHttpRequest"] = str(int(time.time() * 1000)) + "-xml"
        return "&".join(f"{k}={v}" for k, v in params.items())

    @staticmethod
    def getCategoriesParams(typ):
        params = OrderedDict()
        params["type"] = typ
        params["action"] = "get_genres" if typ == "itv" else "get_categories"
        params["JsHttpRequest"] = str(int(time.time() * 1000)) + "-xml"
        return "&".join(f"{k}={v}" for k, v in params.items())

    @staticmethod
    def getAllChannelsParams():
        params = OrderedDict()
        params["type"] = "itv"
        params["action"] = "get_all_channels"
        params["JsHttpRequest"] = str(int(time.time() * 1000)) + "-xml"
        return "&".join(f"{k}={v}" for k, v in params.items())

    @staticmethod
    def getChannelOrSeriesParams(typ, category, sortby, pageNumber, movieId, seriesId):
        params = OrderedDict()
        params["type"] = typ
        params["action"] = "get_ordered_list"
        params["genre"] = category
        params["force_ch_link_check"] = ""
        params["fav"] = "0"
        params["sortby"] = sortby # name, number, added
        if typ == "series":
            params["movie_id"] = movieId if movieId else "0"
            params["category"] = category
            params["season_id"] = seriesId if seriesId else "0"
            params["episode_id"] = "0"
        params["hd"] = "0"
        params["p"] = str(pageNumber)
        params["JsHttpRequest"] = str(int(time.time() * 1000)) + "-xml"
        return "&".join(f"{k}={v}" for k, v in params.items())

    @staticmethod
    def getLinkParams(typ, cmd, episode):
        params = OrderedDict()
        params["type"] = "vod" if typ == "series" else typ
        params["action"] = "create_link"
        params["cmd"] = cmd
        params["series"] = episode if typ == "series" else ""
        params["hd"] = "0"
        params["forced_storage"] = "0"
        params["disable_ad"] = "0"
        params["download"] = "0"
        params["JsHttpRequest"] = str(int(time.time() * 1000)) + "-xml"
        return "&".join(f"{k}={v}" for k, v in params.items())

    @staticmethod
    def random_token():
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
    async def verify_url(url):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    return response.status != 0
        except aiohttp.ClientError as e:
            print(f"Error verifying URL: {e}")
            return False

    # To use this method, you'll need to create an AsyncWorker:
    def check_url(self, url):
        worker = AsyncWorker(self.verify_url(url))
        worker.finished.connect(self.on_url_verified)
        worker.start()
        self.workers.append(worker)  # Keep a reference to the worker

    def on_url_verified(self, is_valid):
        if is_valid:
            print("URL is valid")
        else:
            print("URL is invalid")
