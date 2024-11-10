import os
import platform
import re
import shutil
import subprocess
import time
from urllib.parse import urlparse
from content_loader import ContentLoader

import requests
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from urlobject import URLObject

from options import OptionsDialog


class CategoryTreeWidgetItem(QTreeWidgetItem):
    # sort to always have value "All" first and "Unknown Category" last
    def __lt__(self, other):
        if not isinstance(other, CategoryTreeWidgetItem):
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
    def __lt__(self, other):
        if not isinstance(other, NumberedTreeWidgetItem):
            return super(NumberedTreeWidgetItem, self).__lt__(other)

        sort_column = self.treeWidget().sortColumn()
        if sort_column == 0: # Channel number
            return int(self.text(sort_column)) < int(other.text(sort_column))
        return self.text(sort_column) < other.text(sort_column)

class SetProviderThread(QThread):
    progress = Signal(str)

    def __init__(self, provider_manager):
        super().__init__()
        self.provider_manager = provider_manager

    def run(self):
        try:
            self.provider_manager.set_current_provider(self.progress)
        except Exception as e:
            print(f"Error in initializing provider: {e}")

class ChannelList(QMainWindow):

    def __init__(self, app, player, config_manager, provider_manager):
        super().__init__()
        self.app = app
        self.player = player
        self.config_manager = config_manager
        self.provider_manager = provider_manager
        self.splitter_ratio = 0.75
        self.config_manager.apply_window_settings("channel_list", self)

        self.setWindowTitle("QiTV Content List")

        self.container_widget = QWidget(self)
        self.setCentralWidget(self.container_widget)

        self.content_type = "itv"  # Default to channels (STB type)

        self.create_upper_panel()
        self.create_list_panel()
        self.create_content_info_panel()
        self.create_media_controls()

        self.main_layout = QVBoxLayout()
        self.main_layout.addWidget(self.upper_layout)
        self.main_layout.addWidget(self.list_panel)
        self.main_layout.setContentsMargins(8, 8, 8, 8)

        widget_top = QWidget()
        widget_top.setLayout(self.main_layout)

        # Splitter with content info part
        self.splitter = QSplitter(Qt.Vertical)
        self.splitter.addWidget(widget_top)
        self.splitter.addWidget(self.content_info_panel)
        self.splitter.setSizes([1, 0])

        container_layout = QVBoxLayout(self.container_widget)
        container_layout.setContentsMargins(0, 0, 0, 0)  # Set margins to zero
        container_layout.addWidget(self.splitter)
        container_layout.addWidget(self.media_controls)

        self.link = None
        self.current_category = None  # For back navigation
        self.current_series = None
        self.current_season = None
        self.navigation_stack = []  # To keep track of navigation for back button

        # Connect player signals to show/hide media controls
        self.player.playing.connect(self.show_media_controls)
        self.player.stopped.connect(self.hide_media_controls)

        self.splitter.splitterMoved.connect(self.update_splitter_ratio)
        self.channels_radio.toggled.connect(self.toggle_content_type)
        self.movies_radio.toggled.connect(self.toggle_content_type)
        self.series_radio.toggled.connect(self.toggle_content_type)

        self.set_provider()

    def closeEvent(self, event):
        self.app.quit()
        self.player.close()
        self.config_manager.save_window_settings(self, "channel_list")
        event.accept()

    def set_provider(self):
        self.lock_ui_before_loading()
        self.progress_bar.setRange(0, 0)  # busy indicator
        self.content_list.setEnabled(False)

        self.set_provider_thread = SetProviderThread(self.provider_manager)
        self.set_provider_thread.progress.connect(self.update_busy_progress)
        self.set_provider_thread.finished.connect(self.set_provider_finished)
        self.set_provider_thread.start()

    def set_provider_finished(self):
        self.progress_bar.setRange(0, 100)  # Stop busy indicator
        if hasattr(self, "set_provider_thread"):
            self.set_provider_thread.deleteLater()
            del self.set_provider_thread

        self.load_content()
        self.content_list.setEnabled(True)
        self.unlock_ui_after_loading()

    def update_splitter_ratio(self, pos, index):
        sizes = self.splitter.sizes()
        total_size = sizes[0] + sizes[1]
        self.splitter_ratio = sizes[0] / total_size

    def create_upper_panel(self):
        self.upper_layout = QWidget(self.container_widget)
        main_layout = QVBoxLayout(self.upper_layout)
        main_layout.setContentsMargins(0, 0, 0, 0)  # Set margins to zero

        # Top row
        top_layout = QHBoxLayout()
        top_layout.setContentsMargins(0, 0, 0, 0)  # Set margins to zero

        self.open_button = QPushButton("Open File")
        self.open_button.clicked.connect(self.open_file)
        top_layout.addWidget(self.open_button)

        self.options_button = QPushButton("Settings")
        self.options_button.clicked.connect(self.options_dialog)
        top_layout.addWidget(self.options_button)

        self.update_button = QPushButton("Update Content")
        self.update_button.clicked.connect(self.update_content)
        top_layout.addWidget(self.update_button)

        self.back_button = QPushButton("Back")
        self.back_button.clicked.connect(self.go_back)
        self.back_button.setVisible(False)
        top_layout.addWidget(self.back_button)

        main_layout.addLayout(top_layout)

        # Bottom row (export buttons)
        bottom_layout = QHBoxLayout()
        bottom_layout.setContentsMargins(0, 0, 0, 0)  # Set margins to zero

        self.export_button = QPushButton("Export Browsed")
        self.export_button.clicked.connect(self.export_content)
        bottom_layout.addWidget(self.export_button)

        self.export_all_live_button = QPushButton("Export All Live")
        self.export_all_live_button.clicked.connect(self.export_all_live_channels)
        bottom_layout.addWidget(self.export_all_live_button)

        main_layout.addLayout(bottom_layout)

    def create_list_panel(self):
        self.list_panel = QWidget(self.container_widget)
        list_layout = QVBoxLayout(self.list_panel)
        list_layout.setContentsMargins(0, 0, 0, 0)  # Set margins to zero

        # Add content type selection
        self.content_switch_group = QWidget(self.list_panel)
        content_switch_layout = QHBoxLayout(self.content_switch_group)
        content_switch_layout.setContentsMargins(0, 0, 0, 0)  # Set margins to zero

        self.channels_radio = QRadioButton("Channels")
        self.movies_radio = QRadioButton("Movies")
        self.series_radio = QRadioButton("Series")

        content_switch_layout.addWidget(self.channels_radio)
        content_switch_layout.addWidget(self.movies_radio)
        content_switch_layout.addWidget(self.series_radio)

        self.channels_radio.setChecked(True)

        list_layout.addWidget(self.content_switch_group)

        self.search_box = QLineEdit(self.list_panel)
        self.search_box.setPlaceholderText("Search content...")
        self.search_box.textChanged.connect(
            lambda: self.filter_content(self.search_box.text())
        )
        list_layout.addWidget(self.search_box)

        self.content_list = QTreeWidget(self.list_panel)
        self.content_list.setSelectionMode(QTreeWidget.SingleSelection)
        self.content_list.setIndentation(0)
        self.content_list.setAlternatingRowColors(True)
        self.content_list.itemSelectionChanged.connect(self.item_selected)
        self.content_list.itemActivated.connect(self.item_activated)

        list_layout.addWidget(self.content_list, 1)

        # Create a horizontal layout for the favorite button and checkbox
        self.favorite_layout = QHBoxLayout()
    
        # Add favorite button and action
        self.favorite_button = QPushButton("Favorite/Unfavorite")
        self.favorite_button.clicked.connect(self.toggle_favorite)
        self.favorite_layout.addWidget(self.favorite_button)

        # Add checkbox to show only favorites
        self.favorites_only_checkbox = QCheckBox("Show only favorites")
        self.favorites_only_checkbox.stateChanged.connect(
            lambda: self.filter_content(self.search_box.text())
        )
        self.favorite_layout.addWidget(self.favorites_only_checkbox)

        # Add the horizontal layout to the main vertical layout
        list_layout.addLayout(self.favorite_layout)

        self.progress_bar = QProgressBar(self)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)
        list_layout.addWidget(self.progress_bar)

        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.cancel_loading)
        self.cancel_button.setVisible(False)
        list_layout.addWidget(self.cancel_button)

    def can_show_content_info(self, item_type):
        return self.config_manager.show_stb_content_info and item_type in ["movie", "serie"] and self.provider_manager.current_provider["type"] == "STB"

    def create_content_info_panel(self):
        self.content_info_panel = QWidget(self.container_widget)
        self.content_info_layout = QVBoxLayout(self.content_info_panel)
        self.content_info_panel.setVisible(False)

    def setup_movie_tvshow_content_info(self):
        self.clear_content_info_panel()
        self.content_info_layout.setContentsMargins(8, 4, 8, 8)
        self.content_info_text = QLabel(self.content_info_panel)
        self.content_info_text.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Ignored) # Allow to reduce splitter below label minimum size
        self.content_info_text.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.content_info_text.setWordWrap(True)
        self.content_info_layout.addWidget(self.content_info_text, 1)

    def clear_content_info_panel(self):
        # Clear all widgets from the content_info layout
        for i in reversed(range(self.content_info_layout.count())):
            widget = self.content_info_layout.itemAt(i).widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

        # Clear the layout itself
        while self.content_info_layout.count():
            item = self.content_info_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                self.clear_layout(item.layout())

        # Hide the content_info panel if it is visible
        if self.content_info_panel.isVisible():
            self.content_info_panel.setVisible(False)
            self.splitter.setSizes([1, 0])
            self.main_layout.setContentsMargins(8, 8, 8, 8)

    def clear_layout(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                self.clear_layout(item.layout())
        layout.deleteLater()

    def switch_content_info_panel(self, content_type):
        if content_type == "channel":
            pass # for program content info
        else:
            self.setup_movie_tvshow_content_info()

        if not self.content_info_panel.isVisible():
            self.main_layout.setContentsMargins(8, 8, 8, 4)

            # set splitter sizes to show both panels using the splitter_ratio
            self.splitter.setSizes([int(self.container_widget.height() * self.splitter_ratio), int(self.container_widget.height() * (1 - self.splitter_ratio))])
            self.content_info_panel.setVisible(True)

    def populate_movie_tvshow_content_info(self, item_data):
        content_info_label = {
            "name": "Title",
            "rating_imdb": "Rating",
            "age": "Age",
            "country": "Country",
            "year": "Year",
            "genre_str": "Genre",
            "length": "Length",
            "director": "Director",
            "actors": "Actors",
            "description": "Summary"
        }

        info = ""
        for key, label in content_info_label.items():
            if key in item_data:
                value = item_data[key]
                # if string, check is not empty and not "na" or "n/a"
                if value:
                    if isinstance(value, str) and value.lower() in ["na", "n/a"]:
                        continue
                    info += f"<b>{label}:</b> {value}<br>"
        self.content_info_text.setText(info)

    def show_favorite_layout(self, show):
        for i in range(self.favorite_layout.count()):
            item = self.favorite_layout.itemAt(i)
            if item.widget():
                item.widget().setVisible(show)

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
        if item_name not in self.config_manager.favorites:
            self.config_manager.favorites.append(item_name)
            self.save_config()

    def remove_from_favorites(self, item_name):
        if item_name in self.config_manager.favorites:
            self.config_manager.favorites.remove(item_name)
            self.save_config()

    def check_if_favorite(self, item_name):
        return item_name in self.config_manager.favorites

    def toggle_content_type(self):
        # Checking only when receiving event of something checked
        # Ignore when receiving event of something unchecked
        rb = self.sender()
        if not rb.isChecked():
            return

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
        # Unregister the content_list selection change event
        self.content_list.itemSelectionChanged.disconnect(self.item_selected)
        self.content_list.clear()
        # Re-egister the content_list selection change event
        self.content_list.itemSelectionChanged.connect(self.item_selected)

        self.content_list.setSortingEnabled(False)
        self.content_list.setColumnCount(1)
        if self.content_type == "itv":
            self.content_list.setHeaderLabels(["Channel Categories"])
        elif self.content_type == "vod":
            self.content_list.setHeaderLabels(["Movie Categories"])
        elif self.content_type == "series":
            self.content_list.setHeaderLabels(["Serie Categories"])

        self.show_favorite_layout(True)

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

        self.clear_content_info_panel()

    def display_content(self, items, content_type="content"):
        # Unregister the content_list selection change event
        self.content_list.itemSelectionChanged.disconnect(self.item_selected)
        self.content_list.clear()
        # Re-egister the content_list selection change event
        self.content_list.itemSelectionChanged.connect(self.item_selected)
        self.content_list.setSortingEnabled(False)

        # Define headers for different content types
        category_header = (
            self.current_category.get("title", "") if self.current_category else ""
        )
        serie_header = (
            self.current_series.get("name", "") if self.current_series else ""
        )
        season_header = (
            self.current_season.get("name", "") if self.current_season else ""
        )
        header_info = {
            "serie": {
                "headers": [
                    self.shorten_header(f"{category_header} > Series"),
                    "Added",
                ],
                "keys": ["name", "added"],
            },
            "movie": {
                "headers": [
                    self.shorten_header(f"{category_header} > Movies"),
                    "Added",
                ],
                "keys": ["name", "added"],
            },
            "season": {
                "headers": [
                    self.shorten_header(
                        f"{category_header} > {serie_header} > Seasons"
                    ),
                    "Added",
                ],
                "keys": ["name", "added"],
            },
            "episode": {
                "headers": [
                    "#",
                    self.shorten_header(
                        f"{category_header} > {serie_header} > {season_header} > Episodes"
                    ),
                ],
                "keys": ["number", "name"],
            },
            "channel": {
                "headers": ["#", self.shorten_header(f"{category_header} > Channels")],
                "keys": ["number", "name"],
            },
            "content": {
                "headers": ["Group", "Name"],
                "keys": ["group", "name"]
            },
        }
        self.content_list.setColumnCount(len(header_info[content_type]["headers"]))
        self.content_list.setHeaderLabels(header_info[content_type]["headers"])

        # no favorites on seasons or episodes folders
        check_fav = content_type in ["channel", "movie", "serie", "content"]
        self.show_favorite_layout(check_fav)

        for item_data in items:
            if content_type == "channel":
                list_item = NumberedTreeWidgetItem(self.content_list)
            else:
                list_item = QTreeWidgetItem(self.content_list)

            for i, key in enumerate(header_info[content_type]["keys"]):
                list_item.setText(i, item_data.get(key, "N/A"))

            list_item.setData(0, Qt.UserRole, {"type": content_type, "data": item_data})
            # Highlight favorite items
            item_name = item_data.get("name") or item_data.get("title")
            if check_fav and self.check_if_favorite(item_name):
                list_item.setBackground(0, QColor(0, 0, 255, 20))

        for i in range(len(header_info[content_type]["headers"])):
            self.content_list.resizeColumnToContents(i)

        self.content_list.sortItems(0, Qt.AscendingOrder)
        self.content_list.setSortingEnabled(True)
        self.back_button.setVisible(content_type != "content")

        # Select 1st item in the list
        if self.content_list.topLevelItemCount() > 0:
            self.content_list.setCurrentItem(self.content_list.topLevelItem(0))

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
        control_layout.setContentsMargins(8, 0, 8, 8)

        self.play_button = QPushButton("Play/Pause")
        self.play_button.clicked.connect(self.toggle_play_pause)
        control_layout.addWidget(self.play_button)

        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(self.stop_video)
        control_layout.addWidget(self.stop_button)

        self.vlc_button = QPushButton("Open in VLC")
        self.vlc_button.clicked.connect(self.open_in_vlc)
        control_layout.addWidget(self.vlc_button)

        self.media_controls.setVisible(False)  # Initially hidden

    def show_media_controls(self):
        self.media_controls.setVisible(True)

        if not self.content_info_panel.isVisible():
            self.main_layout.setContentsMargins(8, 8, 8, 0)
        else:
            self.content_info_layout.setContentsMargins(8, 8, 8, 0)

    def hide_media_controls(self):
        self.media_controls.setVisible(False)

        if not self.content_info_panel.isVisible():
            self.main_layout.setContentsMargins(8, 8, 8, 8)
        else:
            self.content_info_layout.setContentsMargins(8, 8, 8, 8)

    def toggle_play_pause(self):
        self.player.toggle_play_pause()
        self.show_media_controls()

    def stop_video(self):
        self.player.stop_video()
        self.hide_media_controls()

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

    def export_all_live_channels(self):
        provider = self.provider_manager.current_provider
        if provider.get("type") != "STB":
            QMessageBox.warning(
                self,
                "Export Error",
                "This feature is only available for STB providers.",
            )
            return

        file_dialog = QFileDialog(self)
        file_dialog.setAcceptMode(QFileDialog.AcceptSave)
        file_dialog.setDefaultSuffix("m3u")
        file_path, _ = file_dialog.getSaveFileName(
            self, "Export All Live Channels", "", "M3U files (*.m3u)"
        )
        if file_path:
            self.fetch_and_export_all_live_channels(file_path)

    def fetch_and_export_all_live_channels(self, file_path):
        selected_provider = self.provider_manager.current_provider
        url = selected_provider.get("url", "")
        url = URLObject(url)
        base_url = f"{url.scheme}://{url.netloc}"
        mac = selected_provider.get("mac", "")

        try:
            # Get all channels and categories (in provider cache)
            provider_itv_content = self.provider_manager.current_provider_content.setdefault("itv", {})
            categories_list = provider_itv_content.setdefault("categories", [])
            categories = {c.get("id", "None"): c.get("title", "Unknown Category") for c in categories_list}
            channels = provider_itv_content["contents"]

            self.save_channel_list(base_url, channels, categories, mac, file_path)
            QMessageBox.information(
                self,
                "Export Successful",
                f"All live channels have been exported to {file_path}",
            )
        except Exception as e:
            QMessageBox.critical(
                self,
                "Export Error",
                f"An error occurred while exporting channels: {str(e)}",
            )

    def save_channel_list(self, base_url, channels_data, categories, mac, file_path) -> None:
        try:
            with open(file_path, "w", encoding="utf-8") as file:
                file.write("#EXTM3U\n")
                count = 0
                for channel in channels_data:
                    name = channel.get("name", "Unknown Channel")
                    logo = channel.get("logo", "")
                    category = channel.get("tv_genre_id", "None")
                    xmltv_id = channel.get("xmltv_id", "")
                    group = categories.get(category, "Unknown Group")
                    cmd_url = channel.get("cmd", "").replace("ffmpeg ", "")
                    if "localhost" in cmd_url:
                        ch_id_match = re.search(r"/ch/(\d+)_", cmd_url)
                        if ch_id_match:
                            ch_id = ch_id_match.group(1)
                            cmd_url = f"{base_url}/play/live.php?mac={mac}&stream={ch_id}&extension=m3u8"

                    channel_str = f'#EXTINF:-1  tvg-id="{xmltv_id}" tvg-logo="{logo}" group-title="{group}" ,{name}\n{cmd_url}\n'
                    count += 1
                    file.write(channel_str)
                print(f"Channels = {count}")
                print(f"\nChannel list has been dumped to {file_path}")
        except IOError as e:
            print(f"Error saving channel list: {e}")

    def export_content(self):
        file_dialog = QFileDialog(self)
        file_dialog.setAcceptMode(QFileDialog.AcceptSave)
        file_dialog.setDefaultSuffix("m3u")
        file_path, _ = file_dialog.getSaveFileName(
            self, "Export Content", "", "M3U files (*.m3u)"
        )
        if file_path:
            provider = self.provider_manager.current_provider
            # Get the content data from the provider manager on content type
            provider_content = self.provider_manager.current_provider_content.setdefault(self.content_type, {})
            
            base_url = provider.get("url", "")
            config_type = provider.get("type", "")
            mac = provider.get("mac", "")

            if config_type == "STB":
                # Extract all content items from categories
                all_items = []
                for items in provider_content.get("contents", {}).values():
                    all_items.extend(items)
                self.save_stb_content(base_url, all_items, mac, file_path)
            elif config_type in ["M3UPLAYLIST", "M3USTREAM", "XTREAM"]:
                content_items = provider_content if provider_content else []
                self.save_m3u_content(content_items, file_path)
            else:
                print(f"Unknown provider type: {config_type}")

    @staticmethod
    def save_m3u_content(content_data, file_path):
        try:
            with open(file_path, "w", encoding="utf-8") as file:
                file.write("#EXTM3U\n")
                count = 0
                for item in content_data:
                    name = item.get("name", "Unknown")
                    logo = item.get("logo", "")
                    group = item.get("group", "")
                    xmltv_id = item.get("xmltv_id", "")
                    cmd_url = item.get("cmd")

                    if cmd_url:
                        item_str = f'#EXTINF:-1 tvg-id="{xmltv_id}" tvg-logo="{logo}" group-title="{group}" ,{name}\n{cmd_url}\n'
                        count += 1
                        file.write(item_str)
                print(f"Items exported: {count}")
                print(f"\nContent list has been saved to {file_path}")
        except IOError as e:
            print(f"Error saving content list: {e}")

    @staticmethod
    def save_stb_content(base_url, content_data, mac, file_path):
        try:
            with open(file_path, "w", encoding="utf-8") as file:
                file.write("#EXTM3U\n")
                count = 0
                for item in content_data:
                    name = item.get("name", "Unknown")
                    logo = item.get("logo", "")
                    xmltv_id = item.get("xmltv_id", "")
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

                    item_str = f'#EXTINF:-1 tvg-id="{xmltv_id}" tvg-logo="{logo}" ,{name}\n{cmd_url}\n'
                    count += 1
                    file.write(item_str)
                print(f"Items exported: {count}")
                print(f"\nContent list has been saved to {file_path}")
        except IOError as e:
            print(f"Error saving content list: {e}")

    def save_config(self):
        self.config_manager.save_config()

    def save_provider(self):
        self.provider_manager.save_provider()

    def load_content(self):
        selected_provider = self.provider_manager.current_provider
        config_type = selected_provider.get("type", "")
        content = self.provider_manager.current_provider_content.setdefault(self.content_type, {})
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
        selected_provider = self.provider_manager.current_provider
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
            self.load_stb_categories(selected_provider["url"], self.provider_manager.headers)
        elif config_type == "M3USTREAM":
            self.load_stream(selected_provider["url"])

    def load_m3u_playlist(self, url):
        try:
            if url.startswith(("http://", "https://")):
                response = requests.get(url)
                content = response.text
            else:
                with open(url, "r", encoding="utf-8") as file:
                    content = file.read()

            parsed_content = self.parse_m3u(content)
            self.display_content(parsed_content)
            # Update the content in the config
            self.provider_manager.current_provider_content[
                self.content_type
            ] = parsed_content
            self.save_provider()
        except (requests.RequestException, IOError) as e:
            print(f"Error loading M3U Playlist: {e}")

    def load_stream(self, url):
        item = {"id": 1, "name": "Stream", "cmd": url}
        self.display_content([item])
        # Update the content in the config
        self.provider_manager.current_provider_content[self.content_type] = [item]
        self.save_provider()

    def item_selected(self):
        selected_items = self.content_list.selectedItems()
        if selected_items:
            item = selected_items[0]
            data = item.data(0, Qt.UserRole)
            if data and "type" in data:
                item_data = data["data"]
                item_type = item.data(0, Qt.UserRole)["type"]

                if self.can_show_content_info(item_type):
                    self.switch_content_info_panel("movie_tvshow")
                    self.populate_movie_tvshow_content_info(item_data)
                else:
                    self.clear_content_info_panel()

    def item_activated(self, item):
        data = item.data(0, Qt.UserRole)
        if data and "type" in data:
            item_data = data["data"]
            item_type = item.data(0, Qt.UserRole)["type"]

            nav_len = len(self.navigation_stack)
            if item_type == "category":
                self.navigation_stack.append(("root", self.current_category, item.text(0)))
                self.current_category = item_data
                self.load_content_in_category(item_data)
            elif item_type == "serie":
                if self.content_type == "series":
                    # For series, load seasons
                    self.navigation_stack.append(("category", self.current_category, item.text(0)))
                    self.current_series = item_data
                    self.load_series_seasons(item_data)
                else:
                    self.play_item(item_data)
            elif item_type == "season":
                # Load episodes for the selected season
                self.navigation_stack.append(("series", self.current_series, item.text(0)))
                self.current_season = item_data
                self.load_season_episodes(item_data)
            elif item_type in ["content", "channel", "movie"]:
                self.play_item(item_data)
            elif item_type == "episode":
                # Play the selected episode
                self.play_item(item_data, is_episode=True)
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
            nav_type, previous_data, previous_selected_id = self.navigation_stack.pop()
            if nav_type == "root":
                # Display root categories
                content = self.provider_manager.current_provider_content.setdefault(
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

            # Select previous item
            if previous_selected_id:
                previous_selected = self.content_list.findItems(previous_selected_id, Qt.MatchExactly, 0)
                if previous_selected:
                    self.content_list.setCurrentItem(previous_selected[0])
                    self.content_list.scrollToItem(previous_selected[0], QTreeWidget.PositionAtTop)
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
                user_agent_match = re.search(r'user-agent="([^"]+)"', line)
                item_name_match = re.search(r',([^,]+)$', line)

                tvg_id = tvg_id_match.group(1) if tvg_id_match else None
                tvg_logo = tvg_logo_match.group(1) if tvg_logo_match else None
                group_title = group_title_match.group(1) if group_title_match else None
                user_agent = user_agent_match.group(1) if user_agent_match else None
                item_name = item_name_match.group(1) if item_name_match else None

                id_counter += 1
                item = {
                    "id": id_counter,
                    "group": group_title,
                    "xmltv_id": tvg_id,
                    "name": item_name,
                    "logo": tvg_logo,
                    "user_agent": user_agent,
                }

            elif line.startswith("#EXTVLCOPT:http-user-agent="):
                user_agent = line.split("=", 1)[1]
                item["user_agent"] = user_agent

            elif line.startswith("http"):
                urlobject = urlparse(line)
                item["cmd"] = urlobject.geturl()
                result.append(item)
        return result

    def load_stb_categories(self, url, headers):
        url = URLObject(url)
        url = f"{url.scheme}://{url.netloc}"
        try:
            fetchurl = (
                f"{url}/server/load.php?{self.get_categories_params(self.content_type)}"
            )
            response = requests.get(fetchurl, headers=headers)
            result = response.json()
            categories = result["js"]
            if not categories:
                print("No categories found.")
                return
            # Save categories in config
            provider_content = self.provider_manager.current_provider_content.setdefault(self.content_type, {})
            provider_content["categories"] = categories
            provider_content["contents"] = {}

            # Sorting all channels now by category
            if self.content_type == "itv":
                fetchurl = (
                    f"{url}/server/load.php?{self.get_allchannels_params()}"
                )
                response = requests.get(fetchurl, headers=headers)
                result = response.json()
                provider_content["contents"] = result["js"]["data"]

                # Split channels by category, and sort them number-wise
                sorted_channels = {}

                for i in range(len(provider_content["contents"])):
                    genre_id = provider_content["contents"][i]["tv_genre_id"]
                    category = str(genre_id)
                    if category not in sorted_channels:
                        sorted_channels[category] = []
                    sorted_channels[category].append(i)

                for category in sorted_channels:
                    sorted_channels[category].sort(key=lambda x: int(provider_content["contents"][x]["number"]))

                # Add a specific category for null genre_id
                if "None" in sorted_channels:
                    categories.append({
                        "id": "None",
                        "title": "Unknown Category"
                        })

                provider_content["sorted_channels"] = sorted_channels

            self.save_provider()
            self.display_categories(categories)
        except Exception as e:
            print(f"Error loading STB categories: {e}")

    @staticmethod
    def get_categories_params(_type):
        params = {
            "type": _type,
            "action": "get_genres" if _type == "itv" else "get_categories",
            "JsHttpRequest": str(int(time.time() * 1000)) + "-xml",
        }
        return "&".join(f"{k}={v}" for k, v in params.items())

    @staticmethod
    def get_allchannels_params():
        params = {
            "type": "itv",
            "action": "get_all_channels",
            "JsHttpRequest": str(int(time.time() * 1000)) + "-xml",
        }
        return "&".join(f"{k}={v}" for k, v in params.items())

    def load_content_in_category(self, category):
        content_data = self.provider_manager.current_provider_content.setdefault(self.content_type, {})
        category_id = category.get("id", "*")

        if self.content_type == "itv":
            # Show only channels for the selected category
            if category_id == "*":
                items = content_data["contents"]
            else:
                items = [content_data["contents"][i] for i in content_data["sorted_channels"].get(category_id, [])]
            self.display_content(items, content_type="channel")
        else:
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
        selected_provider = self.provider_manager.current_provider
        headers = self.provider_manager.headers
        url = selected_provider.get("url", "")
        url = URLObject(url)
        url = f"{url.scheme}://{url.netloc}/server/load.php"

        self.lock_ui_before_loading()
        self.content_list.setEnabled(False)
        if hasattr(self, "content_loader") and self.content_loader.isRunning():
            self.content_loader.wait()
        self.content_loader = ContentLoader(
            url, headers, self.content_type, category_id=category_id
        )
        self.content_loader.content_loaded.connect(self.update_content_list)
        self.content_loader.progress_updated.connect(self.update_progress)
        self.content_loader.finished.connect(self.content_loader_finished)
        self.content_loader.start()
        self.cancel_button.setText("Cancel loading content in category")

    def load_series_seasons(self, series_item):
        selected_provider = self.provider_manager.current_provider
        headers = self.provider_manager.headers
        url = selected_provider.get("url", "")
        url = URLObject(url)
        url = f"{url.scheme}://{url.netloc}/server/load.php"

        self.current_series = series_item  # Store current series

        self.lock_ui_before_loading()
        self.content_list.setEnabled(False)
        if hasattr(self, "content_loader") and self.content_loader.isRunning():
            self.content_loader.wait()
        self.content_loader = ContentLoader(
            url=url,
            headers=headers,
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
        self.cancel_button.setText("Cancel loading seasons")

    def load_season_episodes(self, season_item):
        selected_provider = self.provider_manager.current_provider
        headers = self.provider_manager.headers
        url = selected_provider.get("url", "")
        url = URLObject(url)
        url = f"{url.scheme}://{url.netloc}/server/load.php"

        self.current_season = season_item  # Store current season

        self.lock_ui_before_loading()
        self.content_list.setEnabled(False)
        if hasattr(self, "content_loader") and self.content_loader.isRunning():
            self.content_loader.wait()
        self.content_loader = ContentLoader(
            url=url,
            headers=headers,
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
        self.cancel_button.setText("Cancel loading episodes")

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

    def play_item(self, item_data, is_episode=False):
        if self.provider_manager.current_provider["type"] == "STB":
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

    def cancel_loading(self):
        if hasattr(self, "content_loader") and self.content_loader.isRunning():
            self.content_loader.terminate()
            if hasattr(self, "content_loader"):
                self.content_loader.wait()
            self.content_loader_finished()
            QMessageBox.information(
                self, "Cancelled", "Content loading has been cancelled."
            )

    def lock_ui_before_loading(self):
        self.update_ui_on_loading(loading=True)

    def unlock_ui_after_loading(self):
        self.update_ui_on_loading(loading=False)

    def update_ui_on_loading(self, loading):
        self.open_button.setEnabled(not loading)
        self.options_button.setEnabled(not loading)
        self.export_button.setEnabled(not loading)
        self.export_all_live_button.setEnabled(not loading)
        self.update_button.setEnabled(not loading)
        self.back_button.setEnabled(not loading)
        self.progress_bar.setVisible(loading)
        self.cancel_button.setVisible(loading)
        self.content_switch_group.setEnabled(not loading)

    def content_loader_finished(self):
        self.content_list.setEnabled(True)
        if hasattr(self, "content_loader"):
            self.content_loader.deleteLater()
            del self.content_loader
        self.unlock_ui_after_loading()

    def update_content_list(self, data):
        category_id = data.get("category_id")
        items = data.get("items")

        # Cache the items in config
        selected_provider = self.provider_manager.current_provider_content
        content_data = selected_provider.setdefault(self.content_type, {})
        contents = content_data.setdefault("contents", {})
        contents[category_id] = items
        self.save_provider()

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
        if total:
            progress_percentage = int((current / total) * 100)
            self.progress_bar.setValue(progress_percentage)
            if progress_percentage == 100:
                self.progress_bar.setVisible(False)
            else:
                self.progress_bar.setVisible(True)

    def update_busy_progress(self, msg):
        self.cancel_button.setText(msg)

    def create_link(self, item, is_episode=False):
        try:
            selected_provider = self.provider_manager.current_provider
            headers = self.provider_manager.headers
            url = selected_provider.get("url", "")
            url = URLObject(url)
            url = f"{url.scheme}://{url.netloc}"
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
            response = requests.get(fetchurl, headers=headers)
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
    def shorten_header(s):
        return s[:20] + "..." + s[-25:] if len(s) > 45 else s

    @staticmethod
    def get_item_type(item):
        data = item.data(0, Qt.UserRole)
        return data.get("type") if data else None

    @staticmethod
    def get_item_name(item, item_type):
        return item.text(1 if item_type in ["channel", "content"] else 0)
