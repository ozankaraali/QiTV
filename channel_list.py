import asyncio
import os
import platform
import random
import re
import shutil
import string
import subprocess
import time
from collections import OrderedDict
from urllib.parse import urlparse

import aiohttp
import orjson
import requests
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLineEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)
from urlobject import URLObject

from options import OptionsDialog

class CategoryTreeWidgetItem(QTreeWidgetItem):
    # sort to always have value "All" first and "Unknown Category" last
    def __lt__( self, other ):
        if ( not isinstance(other, CategoryTreeWidgetItem) ):
            return super(CategoryTreeWidgetItem, self).__lt__(other)

        sort_column = self.treeWidget().sortColumn()
        t1 = self.text(sort_column)
        t2 = other.text(sort_column)
        if t1 == "All":
            return True
        if t2 == "All":
            return False
        if t1 == "Unknown Category":
            return False
        if t2 == "Unknown Category":
            return True
        return t1 < t2

class NumberedTreeWidgetItem(QTreeWidgetItem):
    # Modify the sorting by # to used integer and not string (1 < 10, but "1" may not be < "10")
    def __lt__( self, other ):
        if ( not isinstance(other, NumberedTreeWidgetItem) ):
            return super(NumberedTreeWidgetItem, self).__lt__(other)

        sort_column = self.treeWidget().sortColumn()
        sort_header = self.treeWidget().headerItem().text(sort_column)
        if sort_header == "#":
            return int(self.text(sort_column)) < int(other.text(sort_column))
        return self.text(sort_column) < other.text(sort_column)

class ContentLoader(QThread):
    content_loaded = Signal(dict)
    progress_updated = Signal(int, int)

    def __init__(
        self,
        url,
        headers,
        content_type,
        category_id=None,
        parent_id=None,
        movie_id=None,
        season_id=None,
        action="get_ordered_list",
        sortby="name",
    ):
        super().__init__()
        self.url = url
        self.headers = headers
        self.content_type = content_type
        self.category_id = category_id
        self.parent_id = parent_id
        self.movie_id = movie_id
        self.season_id = season_id
        self.action = action
        self.sortby = sortby
        self.items = []

    async def fetch_page(self, session, page, max_retries=3):
        for attempt in range(max_retries):
            try:
                params = self.get_params(page)
                async with session.get(
                    self.url, headers=self.headers, params=params, timeout=30
                ) as response:
                    content = await response.read()
                    if response.status == 503 or not content:
                        wait_time = (2**attempt) + random.uniform(0, 1)
                        print(
                            f"Received error or empty response. Retrying in {wait_time:.2f} seconds..."
                        )
                        await asyncio.sleep(wait_time)
                        continue
                    result = orjson.loads(content)
                    return (
                        result["js"]["data"],
                        int(result["js"]["total_items"]),
                        int(result["js"]["max_page_items"]),
                    )
            except (
                aiohttp.ClientError,
                orjson.JSONDecodeError,
                asyncio.TimeoutError,
            ) as e:
                print(f"Error fetching page {page}: {e}")
                if attempt == max_retries - 1:
                    raise
                wait_time = (2**attempt) + random.uniform(0, 1)
                print(f"Retrying in {wait_time:.2f} seconds...")
                await asyncio.sleep(wait_time)
        return [], 0, 0

    def get_params(self, page):
        params = {
            "type": self.content_type,
            "action": self.action,
            "p": str(page),
            "JsHttpRequest": "1-xml",
        }
        if self.content_type == "itv":
            params.update(
                {
                    "genre": self.category_id if self.category_id else "*",
                    "force_ch_link_check": "",
                    "fav": "0",
                    "sortby": self.sortby,
                    "hd": "0",
                }
            )
        elif self.content_type == "vod":
            params.update(
                {
                    "category": self.category_id if self.category_id else "*",
                    "sortby": self.sortby,
                }
            )
        elif self.content_type == "series":
            params.update(
                {
                    "category": self.category_id if self.category_id else "*",
                    "movie_id": self.movie_id if self.movie_id else "0",
                    "season_id": self.season_id if self.season_id else "0",
                    "episode_id": "0",
                    "sortby": self.sortby,
                }
            )
        return params

    async def load_content(self):
        async with aiohttp.ClientSession() as session:
            # Fetch initial data to get total items and max page items
            page = 1
            page_items, total_items, max_page_items = await self.fetch_page(
                session, page
            )
            self.items.extend(page_items)

            pages = (total_items + max_page_items - 1) // max_page_items
            self.progress_updated.emit(1, pages)

            tasks = []
            for page_num in range(2, pages + 1):
                tasks.append(self.fetch_page(session, page_num))

            for i, task in enumerate(asyncio.as_completed(tasks), 2):
                page_items, _, _ = await task
                self.items.extend(page_items)
                self.progress_updated.emit(i, pages)

            # Emit all items once done
            self.content_loaded.emit(
                {
                    "category_id": self.category_id,
                    "items": self.items,
                    "parent_id": self.parent_id,
                    "movie_id": self.movie_id,
                    "season_id": self.season_id,
                }
            )

    def run(self):
        try:
            asyncio.run(self.load_content())
        except Exception as e:
            print(f"Error in content loading: {e}")


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

        self.content_type = "itv"  # Default to channels (STB type)

        self.create_upper_panel()
        self.create_left_panel()
        self.create_media_controls()
        self.link = None
        self.current_category = None  # For back navigation
        self.current_series = None
        self.current_season = None
        self.navigation_stack = []  # To keep track of navigation for back button
        self.load_content()

    def closeEvent(self, event):
        self.app.quit()
        self.player.close()
        self.config_manager.save_window_settings(self.geometry(), "channel_list")
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

        self.back_button = QPushButton("Back")
        self.back_button.clicked.connect(self.go_back)
        self.back_button.setVisible(False)
        ctl_layout.addWidget(self.back_button)

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

        self.content_list = QTreeWidget(self.left_panel)
        self.content_list.setIndentation(0)
        self.content_list.itemClicked.connect(self.item_selected)

        left_layout.addWidget(self.content_list)

        self.grid_layout.addWidget(self.left_panel, 1, 0)
        self.grid_layout.setColumnStretch(0, 1)

        # Add favorite button and action
        self.favorite_button = QPushButton("Favorite/Unfavorite")
        self.favorite_button.clicked.connect(self.toggle_favorite)
        left_layout.addWidget(self.favorite_button)

        # Add checkbox to show only favorites
        self.favorites_only_checkbox = QCheckBox("Show only favorites")
        self.favorites_only_checkbox.stateChanged.connect(
            lambda: self.filter_content(self.search_box.text())
        )
        left_layout.addWidget(self.favorites_only_checkbox)

        # Add content type selection
        self.content_switch_group = QWidget(self.left_panel)
        content_switch_layout = QHBoxLayout(self.content_switch_group)

        self.channels_radio = QRadioButton("Channels")
        self.movies_radio = QRadioButton("Movies")
        self.series_radio = QRadioButton("Series")

        content_switch_layout.addWidget(self.channels_radio)
        content_switch_layout.addWidget(self.movies_radio)
        content_switch_layout.addWidget(self.series_radio)

        self.channels_radio.setChecked(True)

        self.channels_radio.toggled.connect(self.toggle_content_type)
        self.movies_radio.toggled.connect(self.toggle_content_type)
        self.series_radio.toggled.connect(self.toggle_content_type)

        left_layout.addWidget(self.content_switch_group)

        self.progress_bar = QProgressBar(self)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)
        left_layout.addWidget(self.progress_bar)

        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.cancel_content_loading)
        self.cancel_button.setVisible(False)
        left_layout.addWidget(self.cancel_button)

    def toggle_favorite(self):
        selected_item = self.content_list.currentItem()
        if selected_item:
            item_type = self.get_item_type(selected_item)
            item_name = self.get_item_name(selected_item, item_type)
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
        # get the radio button that send the signal
        rb = self.sender()
        if not rb.isChecked():
            return # ignore if not checked to avoid double toggling

        if self.channels_radio.isChecked():
            self.content_type = "itv"
        elif self.movies_radio.isChecked():
            self.content_type = "vod"
        elif self.series_radio.isChecked():
            self.content_type = "series"
        self.current_category = None
        self.current_series = None
        self.current_season = None
        self.navigation_stack.clear()
        self.load_content()

        # Clear search box after changing content type and force re-filtering if needed
        self.search_box.clear()
        if not self.search_box.isModified():
            self.filter_content(self.search_box.text())

    def display_categories(self, categories):
        self.content_list.clear()
        self.content_list.setSortingEnabled(False)
        self.content_list.setColumnCount(1)
        if self.content_type == "itv":
            self.content_list.setHeaderLabels(["Channel Categories"])
        elif self.content_type == "vod":
            self.content_list.setHeaderLabels(["Movie Categories"])
        elif self.content_type == "series":
            self.content_list.setHeaderLabels(["Serie Categories"])

        self.favorite_button.setHidden(False)

        for category in categories:
            item = CategoryTreeWidgetItem(self.content_list)
            item.setText(0, category.get("title", "Unknown Category"))
            item.setData(0, Qt.UserRole, {"type": "category", "data": category})
            # Highlight favorite items
            if self.check_if_favorite(category.get("title", "")):
                item.setBackground(0, QColor(0, 0, 255, 20))

        self.content_list.sortItems(0, Qt.AscendingOrder)
        self.content_list.setSortingEnabled(True)
        self.back_button.setVisible(False)

    def display_content(self, items, content_type="content"):
        self.content_list.clear()
        self.content_list.setSortingEnabled(False)

        # Define headers for different content types
        category_hdr = self.current_category.get('title', '') if self.current_category else ''
        serie_hdr = self.current_series.get('name', '') if self.current_series else ''
        season_hdr = self.current_season.get('name', '') if self.current_season else ''
        header_info = {
            "serie": {
               "headers": [self.shorten_header(f"{category_hdr} > Series"), "Added"],
               "keys": ["name", "added"] },
            "movie": {
               "headers": [self.shorten_header(f"{category_hdr} > Movies"), "Added"],
               "keys": ["name", "added"] },
            "season": { 
                "headers": [self.shorten_header(f"{category_hdr} > {serie_hdr} > Seasons"), "Added"],
                "keys": ["name", "added"] },
            "episode": { 
                "headers": ["#", self.shorten_header(f"{category_hdr} > {serie_hdr} > {season_hdr} > Episodes")],
                "keys": ["number", "name"] },
            "channel": { 
                "headers": ["#", self.shorten_header(f"{category_hdr} > Channels")],
                "keys": ["number", "name"] },
            "content": {
                "headers": ["Name"] }
        }
        self.content_list.setColumnCount(len(header_info[content_type]["headers"]))
        self.content_list.setHeaderLabels(header_info[content_type]["headers"])

        # no need to check favorites or allow to add favorites on seasons or episodes folders
        check_fav = content_type in ["channel", "movie", "serie", "content"]
        self.favorite_button.setHidden(not check_fav)

        for item_data in items:
            list_item = NumberedTreeWidgetItem(self.content_list)
            item_name = item_data.get("name") or item_data.get("title")
            if content_type == "content":
                list_item.setText(0, item_name)
            else:
                for i, key in enumerate(header_info[content_type]["keys"]):
                    list_item.setText(i, item_data.get(key, "N/A"))
            list_item.setData(0, Qt.UserRole, {"type": content_type, "data": item_data})
            # Highlight favorite items
            if check_fav and self.check_if_favorite(item_name):
                list_item.setBackground(0, QColor(0, 0, 255, 20))

        for i in range(len(header_info[content_type]["headers"])):
            self.content_list.resizeColumnToContents(i)

        self.content_list.sortItems(0, Qt.AscendingOrder)
        self.content_list.setSortingEnabled(True)
        self.back_button.setVisible(content_type!="content")

    def filter_content(self, text=""):
        show_favorites = self.favorites_only_checkbox.isChecked()
        search_text = text.lower() if isinstance(text, str) else ""

        # retrieve items type first
        if self.content_list.topLevelItemCount() > 0:
            item = self.content_list.topLevelItem(0)
            item_type = self.get_item_type(item)

        for i in range(self.content_list.topLevelItemCount()):
            item = self.content_list.topLevelItem(i)
            item_name = self.get_item_name(item, item_type)
            matches_search = search_text in item_name.lower()
            if item_type in ["category", "channel", "movie", "serie", "content"]:
                # For category, channel, movie, serie and generic content, filter by search text and favorite
                is_favorite = self.check_if_favorite(item_name)
                if show_favorites and not is_favorite:
                    item.setHidden(True)
                else:
                    item.setHidden(not matches_search)
            else:
                # For season, episode, only filter by search text
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
            content_data = provider.get(self.content_type, {})
            base_url = provider.get("url", "")
            config_type = provider.get("type", "")
            mac = provider.get("mac", "")

            if config_type == "STB":
                # Extract all content items from categories
                all_items = []
                for items in content_data.get("contents", {}).values():
                    all_items.extend(items)
                self.save_stb_content(base_url, all_items, mac, file_path)
            elif config_type in ["M3UPLAYLIST", "M3USTREAM", "XTREAM"]:
                content_items = provider.get(self.content_type, [])
                self.save_m3u_content(content_items, file_path)
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
            # If we have categories cached, display them
            if config_type == "STB":
                self.display_categories(content.get("categories", []))
            else:
                # For non-STB, display content directly
                self.display_content(content)
        else:
            self.update_content()

    def update_content(self):
        selected_provider = self.config["data"][self.config["selected"]]
        config_type = selected_provider.get("type", "")
        if config_type == "M3UPLAYLIST":
            self.load_m3u_playlist(selected_provider["url"])
        elif config_type == "XTREAM":
            urlobject = URLObject(selected_provider["url"])
            if urlobject.scheme == "":
                urlobject = URLObject(f"http://{selected_provider['url']}")
            if self.content_type == "itv":
                url = (
                    f"{urlobject.scheme}://{urlobject.netloc}/get.php?"
                    f"username={selected_provider['username']}&password={selected_provider['password']}&type=m3u"
                )
            else:
                url = (
                    f"{urlobject.scheme}://{urlobject.netloc}/get.php?"
                    f"username={selected_provider['username']}&password={selected_provider['password']}&type=m3u&"
                    "contentType=vod"
                )
            self.load_m3u_playlist(url)
        elif config_type == "STB":
            self.do_handshake(
                selected_provider["url"], selected_provider["mac"], load=True
            )
        elif config_type == "M3USTREAM":
            self.load_stream(selected_provider["url"])

    def load_m3u_playlist(self, url):
        try:
            response = requests.get(url)
            if response.status_code == 200:
                content = self.parse_m3u(response.text)
                self.display_content(content)
                # Update the content in the config
                self.config["data"][self.config["selected"]][
                    self.content_type
                ] = content
                self.save_config()
        except requests.RequestException as e:
            print(f"Error loading M3U Playlist: {e}")

    def load_stream(self, url):
        item = {"id": 1, "name": "Stream", "cmd": url}
        self.display_content([item])
        # Update the content in the config
        self.config["data"][self.config["selected"]][self.content_type] = [item]
        self.save_config()

    def item_selected(self, item):
        data = item.data(0, Qt.UserRole)
        if data and "type" in data:
            nav_len = len(self.navigation_stack)
            if data["type"] == "category":
                self.navigation_stack.append(("root", self.current_category))
                self.current_category = data["data"]
                self.load_content_in_category(data["data"])
            elif data["type"] == "serie":
                if self.content_type == "series":
                    # For series, load seasons
                    self.navigation_stack.append(("category", self.current_category))
                    self.current_series = data["data"]
                    self.load_series_seasons(data["data"])
                else:
                    self.play_item(data["data"])
            elif data["type"] == "season":
                # Load episodes for the selected season
                self.navigation_stack.append(("series", self.current_series))
                self.current_season = data["data"]
                self.load_season_episodes(data["data"])
            elif data["type"] in ["content", "channel", "movie"]:
                self.play_item(data["data"])
            elif data["type"] == "episode":
                # Play the selected episode
                self.play_item(data["data"], is_episode=True)
            else:
                print("Unknown item type selected.")
            
            # Clear search box after navigating and force re-filtering if needed
            if len(self.navigation_stack) != nav_len:
                self.search_box.clear()
                if not self.search_box.isModified():
                    self.filter_content(self.search_box.text())
        else:
            print("Item with no type selected.")

    def go_back(self):
        if self.navigation_stack:
            nav_type, previous_data = self.navigation_stack.pop()
            if nav_type == "root":
                # Display root categories
                content = self.config["data"][self.config["selected"]].get(
                    self.content_type, {}
                )
                categories = content.get("categories", [])
                self.display_categories(categories)
                self.current_category = None
            elif nav_type == "category":
                # Go back to category content
                self.current_category = previous_data
                self.load_content_in_category(self.current_category)
                self.current_series = None
            elif nav_type == "series":
                # Go back to series seasons
                self.current_series = previous_data
                self.load_series_seasons(self.current_series)
                self.current_season = None

            # Clear search box after navigating backward and force re-filtering if needed
            self.search_box.clear()
            if not self.search_box.isModified():
                self.filter_content(self.search_box.text())
        else:
            # Already at the root level
            pass

    def options_dialog(self):
        options = OptionsDialog(self)
        options.exec_()

    @staticmethod
    def parse_m3u(data):
        lines = data.split("\n")
        result = []
        item = {}
        id_counter = 0
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

                id_counter += 1
                item = {
                    "id": id_counter,
                    "name": item_name,
                    "logo": tvg_logo,
                }

            elif line.startswith("http"):
                urlobject = urlparse(line)
                item["cmd"] = urlobject.geturl()
                result.append(item)
        return result

    def do_handshake(self, url, mac, serverload="/server/load.php", load=True):
        token = (
            self.config.get("token")
            if self.config.get("token")
            else self.random_token()
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
                self.load_stb_categories(url, options)
            return True
        except Exception as e:
            if serverload != "/portal.php":
                serverload = "/portal.php"
                return self.do_handshake(url, mac, serverload)
            print("Error in handshake:", e)
            return False

    def load_stb_categories(self, url, options):
        url = URLObject(url)
        url = f"{url.scheme}://{url.netloc}"
        try:
            fetchurl = (
                f"{url}/server/load.php?{self.get_categories_params(self.content_type)}"
            )
            response = requests.get(fetchurl, headers=options["headers"])
            result = response.json()
            categories = result["js"]
            if not categories:
                print("No categories found.")
                return
            # Save categories in config
            self.config["data"][self.config["selected"]][self.content_type] = {
                "categories": categories,
                "contents": {},
            }
            self.save_config()
            self.display_categories(categories)
        except Exception as e:
            print(f"Error loading STB categories: {e}")

    @staticmethod
    def get_categories_params(_type):
        params = {}
        params["type"] = _type
        params["action"] = "get_genres" if _type == "itv" else "get_categories"
        params["JsHttpRequest"] = str(int(time.time() * 1000)) + "-xml"
        return "&".join(f"{k}={v}" for k, v in params.items())

    def load_content_in_category(self, category):
        selected_provider = self.config["data"][self.config["selected"]]
        content_data = selected_provider.get(self.content_type, {})
        category_id = category.get("id", "*")

        # Check if we have cached content for this category
        if category_id in content_data.get("contents", {}):
            items = content_data["contents"][category_id]
            if self.content_type == "itv":
                self.display_content(items, content_type="channel")
            elif self.content_type == "series":
                self.display_content(items, content_type="serie")
            elif self.content_type == "vod":
                self.display_content(items, content_type="movie")
        else:
            # Fetch content for the category
            self.fetch_content_in_category(category_id)

    def fetch_content_in_category(self, category_id):
        selected_provider = self.config["data"][self.config["selected"]]
        options = selected_provider.get("options", {})
        url = selected_provider.get("url", "")
        url = URLObject(url)
        url = f"{url.scheme}://{url.netloc}/server/load.php"

        self.content_loader = ContentLoader(
            url, options["headers"], self.content_type, category_id=category_id
        )
        self.content_loader.content_loaded.connect(self.update_content_list)
        self.content_loader.progress_updated.connect(self.update_progress)
        self.content_loader.finished.connect(self.content_loader_finished)
        self.content_loader.start()
        self.progress_bar.setVisible(True)
        self.cancel_button.setVisible(True)

    def load_series_seasons(self, series_item):
        selected_provider = self.config["data"][self.config["selected"]]
        options = selected_provider.get("options", {})
        url = selected_provider.get("url", "")
        url = URLObject(url)
        url = f"{url.scheme}://{url.netloc}/server/load.php"

        self.current_series = series_item  # Store current series

        self.content_loader = ContentLoader(
            url=url,
            headers=options["headers"],
            content_type="series",
            category_id=series_item["category_id"],
            movie_id=series_item["id"],  # series ID
            season_id=0,
            action="get_ordered_list",
            sortby="name",
        )
        self.content_loader.content_loaded.connect(self.update_seasons_list)
        self.content_loader.progress_updated.connect(self.update_progress)
        self.content_loader.finished.connect(self.content_loader_finished)
        self.content_loader.start()
        self.progress_bar.setVisible(True)
        self.cancel_button.setVisible(True)

    def load_season_episodes(self, season_item):
        selected_provider = self.config["data"][self.config["selected"]]
        options = selected_provider.get("options", {})
        url = selected_provider.get("url", "")
        url = URLObject(url)
        url = f"{url.scheme}://{url.netloc}/server/load.php"

        self.current_season = season_item  # Store current season

        self.content_loader = ContentLoader(
            url=url,
            headers=options["headers"],
            content_type="series",
            category_id=self.current_category["id"],  # Category ID
            movie_id=self.current_series["id"],  # Series ID
            season_id=season_item["id"],  # Season ID
            action="get_ordered_list",
            sortby="added",
        )
        self.content_loader.content_loaded.connect(self.update_episodes_list)
        self.content_loader.progress_updated.connect(self.update_progress)
        self.content_loader.finished.connect(self.content_loader_finished)
        self.content_loader.start()
        self.progress_bar.setVisible(True)
        self.cancel_button.setVisible(True)

    def display_episodes(self, season_item):
        episodes = season_item.get("series", [])
        episode_items = []
        for episode_num in episodes:
            episode_item = {
                "number": f"{episode_num}",
                "name": f"Episode {episode_num}",
                "cmd": season_item.get("cmd"),
                "series": episode_num,
            }
            episode_items.append(episode_item)
        self.display_content(episode_items, content_type="episode")

    @staticmethod
    def get_channel_or_series_params(
        typ, category, sortby, page_number, movie_id, series_id
    ):
        params = {
            "type": typ,
            "action": "get_ordered_list",
            "genre": category,
            "force_ch_link_check": "",
            "fav": "0",
            "sortby": sortby,  # name, number, added
            "hd": "0",
            "p": str(page_number),
            "JsHttpRequest": str(int(time.time() * 1000)) + "-xml",
        }
        if typ == "series":
            params.update(
                {
                    "movie_id": movie_id if movie_id else "0",
                    "category": category,
                    "season_id": series_id if series_id else "0",
                    "episode_id": "0",
                }
            )
        return "&".join(f"{k}={v}" for k, v in params.items())

    def play_item(self, item_data, is_episode=False):
        if self.config["data"][self.config["selected"]]["type"] == "STB":
            url = self.create_link(item_data, is_episode=is_episode)
            if url:
                self.link = url
                self.player.play_video(url)
            else:
                print("Failed to create link.")
        else:
            cmd = item_data.get("cmd")
            self.link = cmd
            self.player.play_video(cmd)

    def cancel_content_loading(self):
        if hasattr(self, "content_loader") and self.content_loader.isRunning():
            self.content_loader.terminate()
            self.content_loader.wait()
            self.content_loader_finished()
            QMessageBox.information(
                self, "Cancelled", "Content loading has been cancelled."
            )

    def content_loader_finished(self):
        self.progress_bar.setVisible(False)
        self.cancel_button.setVisible(False)
        if hasattr(self, "content_loader"):
            self.content_loader.deleteLater()
            del self.content_loader

    def update_content_list(self, data):
        category_id = data.get("category_id")
        items = data.get("items")

        # Cache the items in config
        selected_provider = self.config["data"][self.config["selected"]]
        content_data = selected_provider.setdefault(self.content_type, {})
        contents = content_data.setdefault("contents", {})
        contents[category_id] = items
        self.save_config()

        if self.content_type == "series":
            self.display_content(items, content_type="serie")
        elif self.content_type == "vod":
            self.display_content(items, content_type="movie")
        elif self.content_type == "itv":
            self.display_content(items, content_type="channel")

    def update_seasons_list(self, data):
        items = data.get("items")
        self.display_content(items, content_type="season")

    def update_episodes_list(self, data):
        items = data.get("items")
        selected_season = None
        for item in items:
            if item.get("id") == data.get("season_id"):
                selected_season = item
                break

        if selected_season:
            episodes = selected_season.get("series", [])
            episode_items = []
            for episode_num in episodes:
                episode_item = {
                    "number": f"{episode_num}",
                    "name": f"Episode {episode_num}",
                    "cmd": selected_season.get("cmd"),
                    "series": episode_num,
                }
                episode_items.append(episode_item)
            self.display_content(episode_items, content_type="episode")
        else:
            print("Season not found in data.")

    def update_progress(self, current, total):
        progress_percentage = int((current / total) * 100)
        self.progress_bar.setValue(progress_percentage)
        if progress_percentage == 100:
            self.progress_bar.setVisible(False)
        else:
            self.progress_bar.setVisible(True)

    def create_link(self, item, is_episode=False):
        try:
            selected_provider = self.config["data"][self.config["selected"]]
            url = selected_provider["url"]
            url = URLObject(url)
            url = f"{url.scheme}://{url.netloc}"
            options = selected_provider["options"]
            cmd = item.get("cmd")
            if is_episode:
                # For episodes, we need to pass 'series' parameter
                series_param = item.get("series")  # This should be the episode number
                fetchurl = (
                    f"{url}/server/load.php?type={'vod' if self.content_type == 'series' else self.content_type}&action=create_link"
                    f"&cmd={requests.utils.quote(cmd)}&series={series_param}&JsHttpRequest=1-xml"
                )
            else:
                fetchurl = (
                    f"{url}/server/load.php?type={self.content_type}&action=create_link"
                    f"&cmd={requests.utils.quote(cmd)}&JsHttpRequest=1-xml"
                )
            response = requests.get(fetchurl, headers=options["headers"])
            if response.status_code != 200 or not response.content:
                print(
                    f"Error creating link: status code {response.status_code}, response content empty"
                )
                return None
            result = response.json()
            link = result["js"]["cmd"].split(" ")[-1]
            link = self.sanitize_url(link)
            self.link = link
            return link
        except Exception as e:
            print(f"Error creating link: {e}")
            return None

    @staticmethod
    def sanitize_url(url):
        # Remove any whitespace characters
        url = url.strip()
        return url

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
    def verify_url(url):
        try:
            response = requests.head(url, timeout=5)
            return True
        except requests.RequestException as e:
            print(f"Error verifying URL: {e}")
            return False

    @staticmethod
    def shorten_header(s):
        return s[:20] + "..." + s[-25:] if len(s) > 45 else s

    @staticmethod
    def get_item_type(item):
        item_type = None
        data = item.data(0, Qt.UserRole)
        if data:
            item_type = data.get("type", None)
        return item_type

    @staticmethod
    def get_item_name_col(item_type):
        column_with_name_by_item_type = {
            "channel": 1 # Channel names are in second column
            }
        return column_with_name_by_item_type.get(item_type, 0)

    @staticmethod
    def get_item_name(item, item_type):
        return item.text(ChannelList.get_item_name_col(item_type))

