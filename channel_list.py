import base64
from datetime import datetime
import html
import logging
import os
import platform
import re
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional
from urllib.parse import quote as url_quote

from PySide6.QtCore import QBuffer, QObject, QRect, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QProgressDialog,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
import requests
from urlobject import URLObject

logger = logging.getLogger(__name__)

from content_loader import ContentLoader
from image_loader import ImageLoader
from options import OptionsDialog
from services.export import save_m3u_content, save_stb_content
from services.m3u import parse_m3u
from widgets.delegates import ChannelItemDelegate, HtmlItemDelegate


class M3ULoaderWorker(QObject):
    finished = Signal(object)
    error = Signal(str)

    def __init__(self, url: str):
        super().__init__()
        self.url = url

    def run(self):
        try:
            response = requests.get(self.url, timeout=10)
            response.raise_for_status()
            self.finished.emit({"content": response.text})
        except requests.RequestException as e:
            self.error.emit(str(e))


class STBCategoriesWorker(QObject):
    finished = Signal(object)
    error = Signal(str)

    def __init__(self, base_url: str, headers: dict, content_type: str = "itv"):
        super().__init__()
        self.base_url = base_url
        self.headers = headers
        self.content_type = content_type

    def run(self):
        try:
            url = URLObject(self.base_url)
            base = f"{url.scheme}://{url.netloc}"

            # Use correct action based on content type
            action = "get_genres" if self.content_type == "itv" else "get_categories"
            fetchurl = f"{base}/server/load.php?type={self.content_type}&action={action}&JsHttpRequest=1-xml"
            resp = requests.get(fetchurl, headers=self.headers, timeout=10)
            resp.raise_for_status()
            categories = resp.json()["js"]

            # Only fetch all channels for itv type
            all_channels = []
            if self.content_type == "itv":
                fetchurl = (
                    f"{base}/server/load.php?type=itv&action=get_all_channels&JsHttpRequest=1-xml"
                )
                resp = requests.get(fetchurl, headers=self.headers, timeout=10)
                resp.raise_for_status()
                all_channels = resp.json()["js"]["data"]

            self.finished.emit({"categories": categories, "all_channels": all_channels})
        except Exception as e:
            self.error.emit(str(e))


class LinkCreatorWorker(QObject):
    finished = Signal(object)
    error = Signal(str)

    def __init__(
        self,
        base_url: str,
        headers: dict,
        content_type: str,
        cmd: str,
        is_episode: bool = False,
        series_param: Optional[str] = None,
    ):
        super().__init__()
        self.base_url = base_url
        self.headers = headers
        self.content_type = content_type
        self.cmd = cmd
        self.is_episode = is_episode
        self.series_param = series_param

    def run(self):
        try:
            url = URLObject(self.base_url)
            base = f"{url.scheme}://{url.netloc}"
            if self.is_episode:
                fetchurl = (
                    f"{base}/server/load.php?type={'vod' if self.content_type == 'series' else self.content_type}&action=create_link"
                    f"&cmd={url_quote(self.cmd)}&series={self.series_param}&JsHttpRequest=1-xml"
                )
            else:
                fetchurl = (
                    f"{base}/server/load.php?type={self.content_type}&action=create_link"
                    f"&cmd={url_quote(self.cmd)}&JsHttpRequest=1-xml"
                )
            response = requests.get(fetchurl, headers=self.headers, timeout=10)
            response.raise_for_status()
            result = response.json()
            link = result["js"]["cmd"].split(" ")[-1]
            self.finished.emit({"link": link})
        except Exception as e:
            self.error.emit(str(e))


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


class ChannelTreeWidgetItem(QTreeWidgetItem):
    # Modify the sorting by Channel Number to used integer and not string (1 < 10, but "1" may not be < "10")
    # Modify the sorting by Program Progress to read the progress in item data
    def __lt__(self, other):
        if not isinstance(other, ChannelTreeWidgetItem):
            return super(ChannelTreeWidgetItem, self).__lt__(other)

        sort_column = self.treeWidget().sortColumn()
        if sort_column == 0:  # Channel number
            return int(self.text(sort_column)) < int(other.text(sort_column))
        elif sort_column == 2:  # EPG Program progress
            p1 = self.data(sort_column, Qt.UserRole)
            if p1 is None:
                return False
            p2 = other.data(sort_column, Qt.UserRole)
            if p2 is None:
                return True
            return self.data(sort_column, Qt.UserRole) < other.data(sort_column, Qt.UserRole)
        elif sort_column == 3:  # EPG Program name
            return self.data(sort_column, Qt.UserRole) < other.data(sort_column, Qt.UserRole)

        return self.text(sort_column) < other.text(sort_column)


class NumberedTreeWidgetItem(QTreeWidgetItem):
    # Modify the sorting by Number to used integer and not string (1 < 10, but "1" may not be < "10")
    def __lt__(self, other):
        if not isinstance(other, NumberedTreeWidgetItem):
            return super(NumberedTreeWidgetItem, self).__lt__(other)

        sort_column = self.treeWidget().sortColumn()
        if sort_column == 0:  # Channel number
            return int(self.text(sort_column)) < int(other.text(sort_column))
        return self.text(sort_column) < other.text(sort_column)


## Delegates moved to widgets/delegates.py


class SetProviderThread(QThread):
    progress = Signal(str)

    def __init__(self, provider_manager, epg_manager):
        super().__init__()
        self.provider_manager = provider_manager
        self.epg_manager = epg_manager

    def run(self):
        try:
            self.provider_manager.set_current_provider(self.progress)
            self.epg_manager.set_current_epg()
        except Exception as e:
            logger.warning(f"Error in initializing provider: {e}")


class ChannelList(QMainWindow):

    def __init__(self, app, player, config_manager, provider_manager, image_manager, epg_manager):
        super().__init__()
        self.app = app
        self.player = player
        self.config_manager = config_manager
        self.provider_manager = provider_manager
        self.image_manager = image_manager
        self.epg_manager = epg_manager
        self.splitter_ratio = 0.75
        self.splitter_content_info_ratio = 0.33
        self.config_manager.apply_window_settings("channel_list", self)

        self.setWindowTitle("QiTV Content List")

        self.container_widget = QWidget(self)
        self.setCentralWidget(self.container_widget)

        self.content_type = "itv"  # Default to channels (STB type)
        self.current_list_content: Optional[str] = None
        self.content_info_shown: Optional[str] = None
        self.image_loader: Optional[ImageLoader] = None
        self.content_loader: Optional[ContentLoader] = None
        self._provider_combo_connected = False  # Track if signal is connected

        self.create_upper_panel()
        self.create_list_panel()
        self.create_content_info_panel()
        self.create_media_controls()

        self.main_layout = QVBoxLayout()
        self.main_layout.addWidget(self.upper_layout)
        self.main_layout.addWidget(self.list_panel)

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

        self.link: Optional[str] = None
        self.current_category: Optional[Dict[str, Any]] = None  # For back navigation
        self.current_series: Optional[Dict[str, Any]] = None
        self.current_season: Optional[Dict[str, Any]] = None
        self.navigation_stack = []  # To keep track of navigation for back button

        # Connect player signals to show/hide media controls
        self.player.playing.connect(self.show_media_controls)
        self.player.stopped.connect(self.hide_media_controls)

        self.splitter.splitterMoved.connect(self.update_splitter_ratio)
        self.channels_radio.toggled.connect(self.toggle_content_type)
        self.movies_radio.toggled.connect(self.toggle_content_type)
        self.series_radio.toggled.connect(self.toggle_content_type)

        # Create a timer to update "On Air" status
        self.refresh_on_air_timer = QTimer(self)
        self.refresh_on_air_timer.timeout.connect(self.refresh_on_air)

        self.update_layout()

        self.set_provider()

        # Keep references to background jobs (threads/workers)
        self._bg_jobs = []

    def closeEvent(self, event):
        # Stop and delete timer
        if self.refresh_on_air_timer.isActive():
            self.refresh_on_air_timer.stop()
        self.refresh_on_air_timer.deleteLater()

        self.app.quit()
        self.player.close()
        self.image_manager.save_index()
        self.epg_manager.save_index()
        self.config_manager.save_window_settings(self, "channel_list")
        event.accept()

    def refresh_on_air(self):
        epg_source = self.config_manager.epg_source
        for i in range(self.content_list.topLevelItemCount()):
            item = self.content_list.topLevelItem(i)
            item_data = item.data(0, Qt.UserRole)
            content_type = item_data.get("type")

            if self.config_manager.channel_epg and self.can_show_epg(content_type):
                epg_data = self.epg_manager.get_programs_for_channel(item_data["data"], None, 1)
                if epg_data:
                    epg_item = epg_data[0]
                    if epg_source == "STB":
                        start_time = datetime.strptime(epg_item["time"], "%Y-%m-%d %H:%M:%S")
                        end_time = datetime.strptime(epg_item["time_to"], "%Y-%m-%d %H:%M:%S")
                    else:
                        start_time = datetime.strptime(epg_item["@start"], "%Y%m%d%H%M%S %z")
                        end_time = datetime.strptime(epg_item["@stop"], "%Y%m%d%H%M%S %z")
                    now = datetime.now(start_time.tzinfo)
                    if end_time != start_time:
                        progress = (
                            100
                            * (now - start_time).total_seconds()
                            / (end_time - start_time).total_seconds()
                        )
                    else:
                        progress = 0 if now < start_time else 100
                    progress = max(0, min(100, progress))
                    if epg_source == "STB":
                        epg_text = str(epg_item.get("name") or "")
                    else:
                        title = epg_item.get("title", {})
                        text = title.get("__text") if isinstance(title, dict) else ""
                        epg_text = str(text or "")
                    item.setData(2, Qt.UserRole, progress)
                    item.setData(3, Qt.UserRole, epg_text)
                else:
                    # Avoid passing None to Qt (causes _pythonToCppCopy warnings)
                    item.setData(2, Qt.UserRole, 0)
                    item.setData(3, Qt.UserRole, "")

        self.content_list.viewport().update()

    def set_provider(self, force_update=False):
        self.lock_ui_before_loading()
        self.progress_bar.setRange(0, 0)  # busy indicator

        if force_update:
            self.provider_manager.clear_current_provider_cache()

        # Remember if this call was a forced update so we can use it in the
        # UI-thread handler safely.
        self._set_provider_force_update = force_update

        self.set_provider_thread = SetProviderThread(self.provider_manager, self.epg_manager)
        self.set_provider_thread.progress.connect(self.update_busy_progress)
        # Ensure the finished handler runs on the GUI thread (no lambda)
        self.set_provider_thread.finished.connect(
            self._on_set_provider_thread_finished, Qt.QueuedConnection
        )
        self.set_provider_thread.start()

    def set_provider_finished(self, force_update=False):
        self.progress_bar.setRange(0, 100)  # Stop busy indicator
        if hasattr(self, "set_provider_thread"):
            self.set_provider_thread.deleteLater()
            del self.set_provider_thread
        self.unlock_ui_after_loading()

        # Connect provider combo signal after first initialization (deferred to main thread)
        if not self._provider_combo_connected:
            QTimer.singleShot(0, lambda: self._connect_provider_combo_signal())
            self._provider_combo_connected = True

        # No need to switch content type if not STB
        selected_provider = self.provider_manager.current_provider
        config_type = selected_provider.get("type", "")
        self.content_switch_group.setVisible(config_type == "STB")

        if force_update:
            self.update_content()
        else:
            self.load_content()

        # If a resume operation was requested while switching provider, handle it now
        pending = getattr(self, "_pending_resume", None)
        if pending:
            try:
                item_data = pending.get("item_data")
                item_type = pending.get("item_type")
                is_episode = item_type == "episode"

                # Ensure content_type matches the item being resumed
                if item_type == "channel":
                    self.content_type = "itv"
                elif item_type == "movie":
                    self.content_type = "vod"
                elif item_type == "episode":
                    self.content_type = "series"

                current_provider_type = self.provider_manager.current_provider.get("type", "")
                if current_provider_type == "STB":
                    self.play_item(item_data, is_episode=is_episode, item_type=item_type)
                elif pending.get("link"):
                    self.link = pending["link"]
                    self.player.play_video(self.link)
                else:
                    # Fallback: recreate the link
                    self.play_item(item_data, is_episode=is_episode, item_type=item_type)
            finally:
                self._pending_resume = None

    def _connect_provider_combo_signal(self):
        """Connect provider combo signal (called after initialization)."""
        # Avoid disconnecting when not connected (causes warnings); connect once.
        self.provider_combo.currentTextChanged.connect(self.on_provider_changed)

    def _on_set_provider_thread_finished(self):
        # Called in the GUI thread after provider setup completes in background
        force_update = getattr(self, "_set_provider_force_update", False)
        self.set_provider_finished(force_update)

    def update_splitter_ratio(self, pos, index):
        sizes = self.splitter.sizes()
        total_size = sizes[0] + sizes[1]
        if total_size:
            self.splitter_ratio = sizes[0] / total_size

    def update_splitter_content_info_ratio(self, pos, index):
        sizes = self.splitter_content_info.sizes()
        total_size = sizes[0] + sizes[1]
        if total_size:
            self.splitter_content_info_ratio = sizes[0] / total_size

    def create_upper_panel(self):
        self.upper_layout = QWidget(self.container_widget)
        main_layout = QVBoxLayout(self.upper_layout)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(8)  # Space between toolbar sections

        # Modern toolbar layout - single row with logical sections
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setSpacing(12)  # Space between sections

        # Section 1: Provider Selection
        provider_section = QHBoxLayout()
        provider_section.setSpacing(6)

        provider_label = QLabel("Provider:")
        provider_section.addWidget(provider_label)

        self.provider_combo = QComboBox()
        self.provider_combo.setMinimumWidth(150)
        self.provider_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        # Signal connection happens in populate_provider_combo after initial setup
        provider_section.addWidget(self.provider_combo)

        self.options_button = QPushButton("⚙")  # Settings icon
        self.options_button.setToolTip("Settings")
        self.options_button.setFixedWidth(30)
        self.options_button.clicked.connect(self.options_dialog)
        provider_section.addWidget(self.options_button)

        toolbar.addLayout(provider_section)

        # Separator
        toolbar.addSpacing(6)

        # Section 2: File Operations
        file_section = QHBoxLayout()
        file_section.setSpacing(6)

        self.open_button = QPushButton("Open File")
        self.open_button.clicked.connect(self.open_file)
        file_section.addWidget(self.open_button)

        toolbar.addLayout(file_section)

        # Separator
        toolbar.addSpacing(6)

        # Section 3: Content Navigation
        nav_section = QHBoxLayout()
        nav_section.setSpacing(6)

        self.back_button = QPushButton("Back")
        self.back_button.clicked.connect(self.go_back)
        self.back_button.setVisible(False)
        nav_section.addWidget(self.back_button)

        self.update_button = QPushButton("Update")
        self.update_button.setToolTip("Update Content")
        self.update_button.clicked.connect(lambda: self.set_provider(force_update=True))
        nav_section.addWidget(self.update_button)

        toolbar.addLayout(nav_section)

        # Separator
        toolbar.addSpacing(6)

        # Section 4: Content Actions
        actions_section = QHBoxLayout()
        actions_section.setSpacing(6)

        self.resume_button = QPushButton("Resume")
        self.resume_button.setToolTip("Resume Last Watched")
        self.resume_button.clicked.connect(self.resume_last_watched)
        actions_section.addWidget(self.resume_button)

        # Export button with dropdown menu
        self.export_button = QPushButton("Export")

        # Create export menu
        export_menu = QMenu(self)

        export_cached_action = export_menu.addAction("Export Cached Content")
        export_cached_action.setToolTip("Quickly export only browsed/cached content")
        export_cached_action.triggered.connect(self.export_content_cached)

        export_complete_action = export_menu.addAction("Export Complete (Fetch All)")
        export_complete_action.setToolTip(
            "For STB series: Fetch all seasons/episodes before exporting"
        )
        export_complete_action.triggered.connect(self.export_content_complete)

        export_menu.addSeparator()

        export_all_live_action = export_menu.addAction("Export All Live Channels")
        export_all_live_action.setToolTip("For STB: Export all live TV channels from cache")
        export_all_live_action.triggered.connect(self.export_all_live_channels)

        # Use a clean label; Qt will add a dropdown arrow automatically
        self.export_button.setMenu(export_menu)
        actions_section.addWidget(self.export_button)

        self.rescanlogo_button = QPushButton("Rescan Logos")
        self.rescanlogo_button.setToolTip("Rescan Channel Logos")
        self.rescanlogo_button.clicked.connect(self.rescan_logos)
        self.rescanlogo_button.setVisible(False)
        actions_section.addWidget(self.rescanlogo_button)

        toolbar.addLayout(actions_section)

        # Push everything to the left
        toolbar.addStretch()

        main_layout.addLayout(toolbar)

        # Populate provider combo box
        self.populate_provider_combo()

    def populate_provider_combo(self):
        """Populate the provider dropdown with available providers."""
        # Block signals to prevent triggering change during population
        was_blocked = self.provider_combo.blockSignals(True)

        try:
            self.provider_combo.clear()

            for provider in self.provider_manager.providers:
                self.provider_combo.addItem(provider["name"])

            # Set current provider
            current_name = self.config_manager.selected_provider_name
            index = self.provider_combo.findText(current_name)
            if index >= 0:
                self.provider_combo.setCurrentIndex(index)
        finally:
            # Restore previous signal blocking state
            self.provider_combo.blockSignals(was_blocked)

    def on_provider_changed(self, provider_name):
        """Handle provider selection change from combo box."""
        if not provider_name:
            return

        # Check if this is actually a change
        if provider_name == self.config_manager.selected_provider_name:
            return

        # Update config
        self.config_manager.selected_provider_name = provider_name
        self.config_manager.save_config()

        # Reload provider (use QTimer to ensure we're in the main thread)
        QTimer.singleShot(0, lambda: self.set_provider())

    def create_list_panel(self):
        self.list_panel = QWidget(self.container_widget)
        list_layout = QVBoxLayout(self.list_panel)
        list_layout.setContentsMargins(0, 0, 0, 0)  # Set margins to zero

        # Add content type selection
        self.content_switch_group = QWidget(self.list_panel)
        content_switch_layout = QHBoxLayout(self.content_switch_group)
        content_switch_layout.setContentsMargins(0, 0, 0, 0)
        content_switch_layout.setSpacing(6)  # Add consistent spacing

        self.channels_radio = QRadioButton("Channels")
        self.movies_radio = QRadioButton("Movies")
        self.series_radio = QRadioButton("Series")

        content_switch_layout.addWidget(self.channels_radio)
        content_switch_layout.addWidget(self.movies_radio)
        content_switch_layout.addWidget(self.series_radio)
        content_switch_layout.addStretch()  # Push radio buttons to the left

        self.channels_radio.setChecked(True)

        list_layout.addWidget(self.content_switch_group)

        self.search_box = QLineEdit(self.list_panel)
        self.search_box.setPlaceholderText("Search content...")
        self.search_box.textChanged.connect(lambda: self.filter_content(self.search_box.text()))
        list_layout.addWidget(self.search_box)

        self.content_list = QTreeWidget(self.list_panel)
        self.content_list.setSelectionMode(QTreeWidget.SingleSelection)
        self.content_list.setIndentation(0)
        self.content_list.setAlternatingRowColors(True)
        self.content_list.itemSelectionChanged.connect(self.item_selected)
        self.content_list.itemActivated.connect(self.item_activated)
        self.refresh_content_list_size()

        list_layout.addWidget(self.content_list, 1)

        # Create a horizontal layout for the favorite button and checkbox
        self.favorite_layout = QHBoxLayout()
        self.favorite_layout.setSpacing(6)  # Add consistent spacing

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

        # Add checkbox to show EPG
        self.epg_checkbox = QCheckBox("Show EPG")
        self.epg_checkbox.setChecked(self.config_manager.channel_epg)
        self.epg_checkbox.stateChanged.connect(self.show_epg)
        self.favorite_layout.addWidget(self.epg_checkbox)

        # Add checkbox to show vod/tvshow content info
        self.vodinfo_checkbox = QCheckBox("Show VOD Info")
        self.vodinfo_checkbox.setChecked(self.config_manager.show_stb_content_info)
        self.vodinfo_checkbox.stateChanged.connect(self.show_vodinfo)
        self.favorite_layout.addWidget(self.vodinfo_checkbox)

        # Add stretch to prevent excessive spacing
        self.favorite_layout.addStretch()

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

    def show_vodinfo(self):
        self.config_manager.show_stb_content_info = self.vodinfo_checkbox.isChecked()
        self.save_config()
        self.item_selected()

    def show_epg(self):
        self.config_manager.channel_epg = self.epg_checkbox.isChecked()
        self.save_config()

        # Refresh the EPG data
        self.epg_manager.set_current_epg()
        self.refresh_channels()

    def refresh_channels(self):
        # No refresh for content other than itv
        if self.content_type != "itv":
            return
        # No refresh from itv list of categories
        selected_provider = self.provider_manager.current_provider
        config_type = selected_provider.get("type", "")
        if config_type == "STB" and not self.current_category:
            return

        # Get the index of the selected item in the content list
        selected_item = self.content_list.selectedItems()
        if selected_item:
            selected_row = self.content_list.indexOfTopLevelItem(selected_item[0])

        # Store how was sorted the content list
        sort_column = self.content_list.sortColumn()

        # Update the content list
        if config_type != "STB":
            # For non-STB, display content directly
            content = self.provider_manager.current_provider_content.setdefault(
                self.content_type, {}
            )
            self.display_content(content)
        else:
            # Reload the current category
            self.load_content_in_category(self.current_category)

        # Restore the sorting
        self.content_list.sortItems(sort_column, self.content_list.header().sortIndicatorOrder())

        # Restore the selected item
        if selected_item:
            item = self.content_list.topLevelItem(selected_row)
            self.content_list.setCurrentItem(item)
            self.item_selected()

    def can_show_content_info(self, item_type):
        return (
            item_type in ["movie", "serie", "season", "episode"]
            and self.provider_manager.current_provider["type"] == "STB"
        )

    def can_show_epg(self, item_type):
        if item_type in ["channel", "m3ucontent"]:
            if self.config_manager.epg_source == "No Source":
                return False
            if (
                self.config_manager.epg_source == "STB"
                and self.provider_manager.current_provider["type"] != "STB"
            ):
                return False
            return True
        return False

    def create_content_info_panel(self):
        self.content_info_panel = QWidget(self.container_widget)
        self.content_info_layout = QVBoxLayout(self.content_info_panel)
        self.content_info_panel.setVisible(False)

    def setup_movie_tvshow_content_info(self):
        self.clear_content_info_panel()
        self.content_info_text = QLabel(self.content_info_panel)
        self.content_info_text.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Ignored
        )  # Allow to reduce splitter below label minimum size
        self.content_info_text.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.content_info_text.setWordWrap(True)
        self.content_info_layout.addWidget(self.content_info_text, 1)
        self.content_info_shown = "movie_tvshow"

    def setup_channel_program_content_info(self):
        self.clear_content_info_panel()
        self.splitter_content_info = QSplitter(Qt.Horizontal)
        self.program_list = QListWidget()
        self.program_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.program_list.setItemDelegate(HtmlItemDelegate())
        self.splitter_content_info.addWidget(self.program_list)
        self.content_info_text = QLabel()
        self.content_info_text.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.content_info_text.setWordWrap(True)
        self.splitter_content_info.addWidget(self.content_info_text)
        self.content_info_layout.addWidget(self.splitter_content_info)
        self.splitter_content_info.setSizes(
            [
                int(self.content_info_panel.width() * self.splitter_content_info_ratio),
                int(self.content_info_panel.width() * (1 - self.splitter_content_info_ratio)),
            ]
        )
        self.content_info_shown = "channel"

        self.program_list.itemSelectionChanged.connect(self.update_channel_program)
        self.splitter_content_info.splitterMoved.connect(self.update_splitter_content_info_ratio)

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

        self.content_info_shown = None
        self.update_layout()

    def update_layout(self):
        if self.content_info_panel.isVisible():
            self.main_layout.setContentsMargins(8, 8, 8, 4)
            if self.media_controls.isVisible():
                self.content_info_layout.setContentsMargins(8, 4, 8, 0)
            else:
                self.content_info_layout.setContentsMargins(8, 4, 8, 8)
        else:
            if self.media_controls.isVisible():
                self.main_layout.setContentsMargins(8, 8, 8, 0)
            else:
                self.main_layout.setContentsMargins(8, 8, 8, 8)

    @staticmethod
    def clear_layout(layout):
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                ChannelList.clear_layout(item.layout())
        layout.deleteLater()

    def switch_content_info_panel(self, item_type):
        if item_type in ["channel", "m3ucontent"]:
            if self.content_info_shown == "channel":
                return
            self.setup_channel_program_content_info()
        else:
            if self.content_info_shown == "movie_tvshow":
                return
            self.setup_movie_tvshow_content_info()

        if not self.content_info_panel.isVisible():
            self.content_info_panel.setVisible(True)
            self.splitter.setSizes(
                [
                    int(self.container_widget.height() * self.splitter_ratio),
                    int(self.container_widget.height() * (1 - self.splitter_ratio)),
                ]
            )

    def populate_channel_programs_content_info(self, item_data):
        try:
            self.program_list.itemSelectionChanged.disconnect()
        except (TypeError, RuntimeError):
            pass
        self.program_list.clear()
        self.program_list.itemSelectionChanged.connect(self.update_channel_program)

        # Show EPG data for the selected channel
        epg_data = self.epg_manager.get_programs_for_channel(item_data)
        if epg_data:
            # Fill the program list
            for epg_item in epg_data:
                if self.config_manager.epg_source == "STB":
                    epg_text = f"<b>{epg_item.get('t_time', 'start')}-{epg_item.get('t_time_to' ,'end')}</b>&nbsp;&nbsp;{epg_item['name']}"
                else:
                    epg_text = f"<b>{datetime.strptime(epg_item.get('@start'), '%Y%m%d%H%M%S %z').strftime('%H:%M')}-{datetime.strptime(epg_item.get('@stop'), '%Y%m%d%H%M%S %z').strftime('%H:%M')}</b>&nbsp;&nbsp;{epg_item['title'].get('__text')}"
                item = QListWidgetItem(f"{epg_text}")
                item.setData(Qt.UserRole, epg_item)
                self.program_list.addItem(item)
            self.program_list.setCurrentRow(0)
        else:
            item = QListWidgetItem("Program not available")
            self.program_list.addItem(item)
            xmltv_id = item_data.get("xmltv_id", "")
            if xmltv_id:
                self.content_info_text.setText(f'No EPG found for channel id "{xmltv_id}"')
            else:
                self.content_info_text.setText(f"Channel without id")

    def update_channel_program(self):
        selected_items = self.program_list.selectedItems()
        if not selected_items:
            self.content_info_text.setText("No program selected")
            return
        selected_item = selected_items[0]
        item_data = selected_item.data(Qt.UserRole)
        if item_data:
            if self.config_manager.epg_source == "STB":
                # Extract information from item_data
                title = item_data.get("name", {})
                desc = item_data.get("descr")
                desc = desc.replace("\r\n", "<br>") if desc else ""
                director = item_data.get("director")
                actor = item_data.get("actor")
                category = item_data.get("category")

                # Format the content information
                info = ""
                if title:
                    info += f"<b>Title:</b> {title}<br>"
                if category:
                    info += f"<b>Category:</b> {category}<br>"
                if desc:
                    info += f"<b>Description:</b> {desc}<br>"
                if director:
                    info += f"<b>Director:</b> {director}<br>"
                if actor:
                    info += f"<b>Actor:</b> {actor}<br>"

                self.content_info_text.setText(info if info else "No data available")

            else:
                # Extract information from item_data
                title = item_data.get("title", {})
                sub_title = item_data.get("sub-title")
                desc = item_data.get("desc")
                credits = item_data.get("credits", {})
                director = credits.get("director")
                actor = credits.get("actor")
                writer = credits.get("writer")
                presenter = credits.get("presenter")
                adapter = credits.get("adapter")
                producer = credits.get("producer")
                composer = credits.get("composer")
                editor = credits.get("editor")
                guest = credits.get("guest")
                category = item_data.get("category")
                country = item_data.get("country")
                episode_num = item_data.get("episode-num")
                rating = item_data.get("rating", {}).get("value")

                # Format the content information
                info = ""
                if title:
                    info += f"<b>Title:</b> {title.get('__text')}<br>"
                if sub_title:
                    info += f"<b>Sub-title:</b> {sub_title.get('__text')}<br>"
                if episode_num:
                    info += f"<b>Episode Number:</b> {episode_num.get('__text')}<br>"
                if category:
                    if isinstance(category, dict):
                        info += f"<b>Category:</b> {category.get('__text')}<br>"
                    elif isinstance(category, list):
                        info += (
                            f"<b>Category:</b> {', '.join([c.get('__text') for c in category])}<br>"
                        )
                if rating:
                    info += f"<b>Rating:</b> {rating.get('__text')}<br>"
                if desc:
                    info += f"<b>Description:</b> {desc.get('__text')}<br>"
                if credits:
                    if director:
                        if isinstance(director, dict):
                            info += f"<b>Director:</b> {director.get('__text')}<br>"
                        elif isinstance(director, list):
                            info += f"<b>Director:</b> {', '.join([c.get('__text') for c in director])}<br>"
                    if actor:
                        if isinstance(actor, dict):
                            info += f"<b>Actor:</b> {actor.get('__text')}<br>"
                        elif isinstance(actor, list):
                            info += (
                                f"<b>Actor:</b> {', '.join([c.get('__text') for c in actor])}<br>"
                            )
                    if guest:
                        if isinstance(guest, dict):
                            info += f"<b>Guest:</b> {guest.get('__text')}<br>"
                        elif isinstance(guest, list):
                            info += (
                                f"<b>Guest:</b> {', '.join([c.get('__text') for c in guest])}<br>"
                            )
                    if writer:
                        if isinstance(writer, dict):
                            info += f"<b>Writer:</b> {writer.get('__text')}<br>"
                        elif isinstance(writer, list):
                            info += (
                                f"<b>Writer:</b> {', '.join([c.get('__text') for c in writer])}<br>"
                            )
                    if presenter:
                        if isinstance(presenter, dict):
                            info += f"<b>Presenter:</b> {presenter.get('__text')}<br>"
                        elif isinstance(presenter, list):
                            info += f"<b>Presenter:</b> {', '.join([c.get('__text') for c in presenter])}<br>"
                    if adapter:
                        if isinstance(adapter, dict):
                            info += f"<b>Adapter:</b> {adapter.get('__text')}<br>"
                        elif isinstance(adapter, list):
                            info += f"<b>Adapter:</b> {', '.join([c.get('__text') for c in adapter])}<br>"
                    if producer:
                        if isinstance(producer, dict):
                            info += f"<b>Producer:</b> {producer.get('__text')}<br>"
                        elif isinstance(producer, list):
                            info += f"<b>Producer:</b> {', '.join([c.get('__text') for c in producer])}<br>"
                    if composer:
                        if isinstance(composer, dict):
                            info += f"<b>Composer:</b> {composer.get('__text')}<br>"
                        elif isinstance(composer, list):
                            info += f"<b>Composer:</b> {', '.join([c.get('__text') for c in composer])}<br>"
                    if editor:
                        if isinstance(editor, dict):
                            info += f"<b>Editor:</b> {editor.get('__text')}<br>"
                        elif isinstance(editor, list):
                            info += (
                                f"<b>Editor:</b> {', '.join([c.get('__text') for c in editor])}<br>"
                            )
                if country:
                    info += f"<b>Country:</b> {country.get('__text')}<br>"

                self.content_info_text.setText(info if info else "No data available")

                # Load poster image if available
                icon_url = item_data.get("icon", {}).get("@src")
                if icon_url:
                    self.lock_ui_before_loading()
                    if self.image_loader and self.image_loader.isRunning():
                        self.image_loader.wait()
                    self.image_loader = ImageLoader(
                        [
                            icon_url,
                        ],
                        self.image_manager,
                        iconified=False,
                    )
                    self.image_loader.progress_updated.connect(self.update_poster)
                    self.image_loader.finished.connect(self.image_loader_finished)
                    self.image_loader.start()
                    self.cancel_button.setText("Cancel fetching poster...")
        else:
            self.content_info_text.setText("No data available")

    def populate_movie_tvshow_content_info(self, item_data):
        content_info_label = {
            "name": "Title",
            "rating_imdb": "Rating",
            "year": "Year",
            "genres_str": "Genre",
            "length": "Length",
            "director": "Director",
            "actors": "Actors",
            "description": "Summary",
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

        # Load poster image if available
        poster_url = item_data.get("screenshot_uri", "")
        if poster_url:
            self.lock_ui_before_loading()
            if self.image_loader and self.image_loader.isRunning():
                self.image_loader.wait()
            self.image_loader = ImageLoader(
                [
                    poster_url,
                ],
                self.image_manager,
                iconified=False,
            )
            self.image_loader.progress_updated.connect(self.update_poster)
            self.image_loader.finished.connect(self.image_loader_finished)
            self.image_loader.start()
            self.cancel_button.setText("Cancel fetching poster...")

    def refresh_content_list_size(self):
        font_size = 12
        icon_size = font_size + 4
        self.content_list.setIconSize(QSize(icon_size, icon_size))
        self.content_list.setStyleSheet(
            f"""
        QTreeWidget {{ font-size: {font_size}px; }}
        """
        )

        font = QFont()
        font.setPointSize(font_size)
        self.content_list.setFont(font)

        # Set header font
        header_font = QFont()
        header_font.setPointSize(font_size)
        header_font.setBold(True)
        self.content_list.header().setFont(header_font)

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

    def rescan_logos(self):
        # Loop on content_list items to get logos and delete them from image_manager
        logo_urls = []
        for i in range(self.content_list.topLevelItemCount()):
            item = self.content_list.topLevelItem(i)
            url_logo = item.data(0, Qt.UserRole)["data"].get("logo", "")
            logo_urls.append(url_logo)
            if url_logo:
                self.image_manager.remove_icon_from_cache(url_logo)

        self.lock_ui_before_loading()
        if self.image_loader and self.image_loader.isRunning():
            self.image_loader.wait()
        self.image_loader = ImageLoader(logo_urls, self.image_manager, iconified=True)
        self.image_loader.progress_updated.connect(self.update_channel_logos)
        self.image_loader.finished.connect(self.image_loader_finished)
        self.image_loader.start()
        self.cancel_button.setText("Cancel fetching channel logos...")

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

    def display_categories(self, categories, select_first=True):
        # Unregister the content_list selection change event
        try:
            self.content_list.itemSelectionChanged.disconnect(self.item_selected)
        except (TypeError, RuntimeError):
            pass
        self.content_list.clear()
        # Re-register the content_list selection change event
        self.content_list.itemSelectionChanged.connect(self.item_selected)

        # Stop refreshing content list
        self.refresh_on_air_timer.stop()

        self.current_list_content = "category"

        self.content_list.setSortingEnabled(False)
        self.content_list.setColumnCount(1)
        if self.content_type == "itv":
            self.content_list.setHeaderLabels([f"Channel Categories ({len(categories)})"])
        elif self.content_type == "vod":
            self.content_list.setHeaderLabels([f"Movie Categories ({len(categories)})"])
        elif self.content_type == "series":
            self.content_list.setHeaderLabels([f"Serie Categories ({len(categories)})"])

        self.show_favorite_layout(True)
        self.rescanlogo_button.setVisible(False)
        self.epg_checkbox.setVisible(False)
        self.vodinfo_checkbox.setVisible(False)

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

        # Select an item in the list (first or a previously selected)
        if select_first:
            if select_first == True:
                if self.content_list.topLevelItemCount() > 0:
                    self.content_list.setCurrentItem(self.content_list.topLevelItem(0))
            else:
                previous_selected_id = select_first
                previous_selected = self.content_list.findItems(
                    previous_selected_id, Qt.MatchExactly, 0
                )
                if previous_selected:
                    self.content_list.setCurrentItem(previous_selected[0])
                    self.content_list.scrollToItem(previous_selected[0], QTreeWidget.PositionAtTop)

    def display_content(self, items, content="m3ucontent", select_first=True):
        # Unregister the selection change event
        try:
            self.content_list.itemSelectionChanged.disconnect(self.item_selected)
        except (TypeError, RuntimeError):
            pass
        self.content_list.clear()
        self.content_list.setSortingEnabled(False)
        # Re-register the selection change event
        self.content_list.itemSelectionChanged.connect(self.item_selected)

        # Stop refreshing On Air content
        self.refresh_on_air_timer.stop()

        self.current_list_content = content
        need_logos = content in ["channel", "m3ucontent"] and self.config_manager.channel_logos
        logo_urls = []
        use_epg = self.can_show_epg(content) and self.config_manager.channel_epg

        # Define headers for different content types
        category_header = self.current_category.get("title", "") if self.current_category else ""
        serie_header = self.current_series.get("name", "") if self.current_series else ""
        season_header = self.current_season.get("name", "") if self.current_season else ""
        header_info = {
            "serie": {
                "headers": [
                    self.shorten_header(f"{category_header} > Series ({len(items)})"),
                    "Genre",
                    "Added",
                ],
                "keys": ["name", "genres_str", "added"],
            },
            "movie": {
                "headers": [
                    self.shorten_header(f"{category_header} > Movies ({len(items)})"),
                    "Genre",
                    "Added",
                ],
                "keys": ["name", "genres_str", "added"],
            },
            "season": {
                "headers": [
                    "#",
                    self.shorten_header(f"{category_header} > {serie_header} > Seasons"),
                    "Added",
                ],
                "keys": ["number", "o_name", "added"],
            },
            "episode": {
                "headers": [
                    "#",
                    self.shorten_header(
                        f"{category_header} > {serie_header} > {season_header} > Episodes"
                    ),
                ],
                "keys": ["number", "ename"],
            },
            "channel": {
                "headers": [
                    "#",
                    self.shorten_header(f"{category_header} > Channels ({len(items)})"),
                ]
                + (["", "On Air"] if use_epg else []),
                "keys": ["number", "name"],
            },
            "m3ucontent": {
                "headers": [f"Name ({len(items)})", "Group"] + (["", "On Air"] if use_epg else []),
                "keys": ["name", "group"],
            },
        }
        self.content_list.setColumnCount(len(header_info[content]["headers"]))
        self.content_list.setHeaderLabels(header_info[content]["headers"])

        # no favorites on seasons or episodes genre_sfolders
        check_fav = content in ["channel", "movie", "serie", "m3ucontent"]
        self.show_favorite_layout(check_fav)

        for item_data in items:
            if content == "channel":
                list_item = ChannelTreeWidgetItem(self.content_list)
            elif content in ["season", "episode"]:
                list_item = NumberedTreeWidgetItem(self.content_list)
            else:
                list_item = QTreeWidgetItem(self.content_list)

            for i, key in enumerate(header_info[content]["keys"]):
                raw_value = item_data.get(key)
                if key == "added":
                    # Show only date part if present
                    text_value = str(raw_value).split()[0] if raw_value else ""
                else:
                    text_value = html.unescape(str(raw_value)) if raw_value is not None else ""
                list_item.setText(i, text_value)

            list_item.setData(0, Qt.UserRole, {"type": content, "data": item_data})

            # If content type is channel, collect the logo urls from the image_manager
            if need_logos:
                logo_urls.append(item_data.get("logo", ""))

            # Highlight favorite items
            item_name = item_data.get("name") or item_data.get("title")
            if check_fav and self.check_if_favorite(item_name):
                list_item.setBackground(0, QColor(0, 0, 255, 20))

        for i in range(len(header_info[content]["headers"])):
            if i != 2:  # Don't auto-resize the progress column
                self.content_list.resizeColumnToContents(i)

        self.content_list.sortItems(0, Qt.AscendingOrder)
        self.content_list.setSortingEnabled(True)
        self.back_button.setVisible(content != "m3ucontent")
        self.epg_checkbox.setVisible(self.can_show_epg(content))
        self.vodinfo_checkbox.setVisible(self.can_show_content_info(content))

        if use_epg:
            self.content_list.setItemDelegate(ChannelItemDelegate())
            # Set a fixed width for the progress column
            self.content_list.setColumnWidth(
                2, 100
            )  # Force column 2 (progress) to be 100 pixels wide
            # Prevent user from resizing the progress column too small
            self.content_list.header().setMinimumSectionSize(100)
            # Start refreshing content list (currently aired program)
            self.refresh_on_air()
            self.refresh_on_air_timer.start(30000)

        # Select an item in the list (first or a previously selected)
        if select_first:
            if select_first == True:
                if self.content_list.topLevelItemCount() > 0:
                    self.content_list.setCurrentItem(self.content_list.topLevelItem(0))
            else:
                previous_selected_id = select_first
                previous_selected = self.content_list.findItems(
                    previous_selected_id, Qt.MatchExactly, 0
                )
                if previous_selected:
                    self.content_list.setCurrentItem(previous_selected[0])
                    self.content_list.scrollToItem(previous_selected[0], QTreeWidget.PositionAtTop)

        # Load channel logos if needed
        self.rescanlogo_button.setVisible(need_logos)
        if need_logos:
            self.lock_ui_before_loading()
            if self.image_loader and self.image_loader.isRunning():
                self.image_loader.wait()
            self.image_loader = ImageLoader(logo_urls, self.image_manager, iconified=True)
            self.image_loader.progress_updated.connect(self.update_channel_logos)
            self.image_loader.finished.connect(self.image_loader_finished)
            self.image_loader.start()
            self.cancel_button.setText("Cancel fetching channel logos...")

    def update_channel_logos(self, current, total, data):
        self.update_progress(current, total)
        if data:
            # Prefer using cache_path to construct GUI objects in the main thread
            logo_column = ChannelList.get_logo_column(self.current_list_content)
            rank = data.get("rank", 0)
            item = (
                self.content_list.topLevelItem(rank)
                if rank < self.content_list.topLevelItemCount()
                else None
            )
            if not item:
                return
            cache_path = data.get("cache_path")
            if cache_path:
                pix = QPixmap(cache_path)
                if not pix.isNull():
                    item.setIcon(logo_column, QIcon(pix))
            else:
                # Backward compatibility: if an icon was provided (older worker behavior)
                qicon = data.get("icon", None)
                if qicon:
                    item.setIcon(logo_column, qicon)

    def update_poster(self, current, total, data):
        self.update_progress(current, total)
        if data:
            cache_path = data.get("cache_path")
            pixmap = None
            if cache_path:
                pixmap = QPixmap(cache_path)
            if pixmap and not pixmap.isNull():
                scaled_pixmap = pixmap.scaled(200, 300, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                buffer = QBuffer()
                buffer.open(QBuffer.ReadWrite)
                scaled_pixmap.save(buffer, "PNG")
                buffer.close()
                base64_data = base64.b64encode(buffer.data()).decode("utf-8")
                img_tag = f'<img src="data:image/png;base64,{base64_data}" alt="Poster Image" style="float:right; margin: 0 0 10px 10px;">'
                self.content_info_text.setText(img_tag + self.content_info_text.text())

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
            if item_type in ["category", "channel", "movie", "serie", "m3ucontent"]:
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
        self.update_layout()

    def hide_media_controls(self):
        self.media_controls.setVisible(False)
        self.update_layout()

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
                        program_files = os.environ.get("ProgramFiles", "C:\\Program Files")
                        vlc_path = os.path.join(program_files, "VideoLAN", "VLC", "vlc.exe")
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
                    if not vlc_path:
                        raise FileNotFoundError("VLC not found")
                    subprocess.Popen([vlc_path, self.link])
                else:  # Assuming Linux or other Unix-like OS
                    vlc_path = shutil.which("vlc")  # Try to find VLC in PATH
                    if not vlc_path:
                        raise FileNotFoundError("VLC not found")
                    subprocess.Popen([vlc_path, self.link])
                # when VLC opens, stop running video on self.player
                self.player.stop_video()
            except FileNotFoundError as fnf_error:
                logger.warning("VLC not found: %s", fnf_error)
            except Exception as e:
                logger.warning(f"Error opening VLC: {e}")

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
            provider_itv_content = self.provider_manager.current_provider_content.setdefault(
                "itv", {}
            )
            categories_list = provider_itv_content.setdefault("categories", [])
            categories = {
                c.get("id", "None"): c.get("title", "Unknown Category") for c in categories_list
            }
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
                            cmd_url = (
                                f"{base_url}/play/live.php?mac={mac}&stream={ch_id}&extension=m3u8"
                            )

                    channel_str = f'#EXTINF:-1  tvg-id="{xmltv_id}" tvg-logo="{logo}" group-title="{group}" ,{name}\n{cmd_url}\n'
                    count += 1
                    file.write(channel_str)
                logger.info(f"Channels = {count}")
                logger.info(f"Channel list has been dumped to {file_path}")
        except IOError as e:
            logger.warning(f"Error saving channel list: {e}")

    def export_content_cached(self):
        """Export only the cached/browsed content that has already been loaded."""
        file_dialog = QFileDialog(self)
        file_dialog.setAcceptMode(QFileDialog.AcceptSave)
        file_dialog.setDefaultSuffix("m3u")
        file_path, _ = file_dialog.getSaveFileName(
            self, "Export Cached Content", "", "M3U files (*.m3u)"
        )
        if file_path:
            provider = self.provider_manager.current_provider
            # Get the content data from the provider manager on content type
            provider_content = self.provider_manager.current_provider_content.setdefault(
                self.content_type, {}
            )

            base_url = provider.get("url", "")
            config_type = provider.get("type", "")
            mac = provider.get("mac", "")

            if config_type == "STB":
                # Extract all content items from categories
                all_items = []
                for items in provider_content.get("contents", {}).values():
                    all_items.extend(items)
                save_stb_content(base_url, all_items, mac, file_path)
            elif config_type in ["M3UPLAYLIST", "M3USTREAM", "XTREAM"]:
                content_items = provider_content if provider_content else []
                save_m3u_content(content_items, file_path)
            else:
                logger.info(f"Unknown provider type: {config_type}")

    def export_content_complete(self):
        """Export all content by fetching all seasons/episodes for series (STB only)."""
        provider = self.provider_manager.current_provider
        config_type = provider.get("type", "")

        # Check if this is appropriate content type
        if config_type != "STB":
            QMessageBox.information(
                self,
                "Export Complete",
                "Complete export is only available for STB providers.\n\n"
                "For other provider types, use 'Export Cached Content'.",
            )
            return

        if self.content_type != "series":
            if self.content_type == "itv":
                QMessageBox.information(
                    self,
                    "Export Complete",
                    "For live channels, please use 'Export All Live Channels' instead.\n\n"
                    "Export Complete is designed for series with multiple seasons/episodes.",
                )
            else:
                QMessageBox.information(
                    self,
                    "Export Complete",
                    "Export Complete is only available for series content.\n\n"
                    f"Current content type: {self.content_type}\n"
                    "For movies or other content, use 'Export Cached Content'.",
                )
            return

        file_dialog = QFileDialog(self)
        file_dialog.setAcceptMode(QFileDialog.AcceptSave)
        file_dialog.setDefaultSuffix("m3u")
        file_path, _ = file_dialog.getSaveFileName(
            self, "Export Complete Content (Fetch All)", "", "M3U files (*.m3u)"
        )
        if file_path:
            self.fetch_and_export_all_series(file_path)

    def fetch_and_export_all_series(self, file_path):
        """Fetch all series, seasons, and episodes, then export to M3U."""
        selected_provider = self.provider_manager.current_provider
        url = selected_provider.get("url", "")
        url = URLObject(url)
        base_url = f"{url.scheme}://{url.netloc}"
        mac = selected_provider.get("mac", "")

        # Get the current content (series in categories)
        provider_content = self.provider_manager.current_provider_content.get(self.content_type, {})
        categories = provider_content.get("categories", [])

        if not categories:
            QMessageBox.warning(
                self,
                "Export Error",
                "No series categories found. Please load content first.",
            )
            return

        # Show progress dialog
        progress = QProgressDialog("Fetching all series data...", "Cancel", 0, 100, self)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)

        all_episodes = []
        total_series = 0

        # Count total series across all categories
        for cat_items in provider_content.get("contents", {}).values():
            total_series += len(cat_items)

        if total_series == 0:
            progress.close()
            QMessageBox.warning(
                self,
                "Export Error",
                "No series found in loaded content.",
            )
            return

        processed_series = 0

        try:
            # For each category
            for category in categories:
                category_id = category.get("id")
                series_list = provider_content.get("contents", {}).get(category_id, [])

                for series_item in series_list:
                    if progress.wasCanceled():
                        progress.close()
                        return

                    series_name = series_item.get("name", "Unknown")
                    progress.setLabelText(f"Fetching: {series_name}")

                    # Fetch seasons for this series
                    seasons_data = self.fetch_seasons_sync(series_item)

                    if seasons_data:
                        seasons = seasons_data.get("data", [])
                        for season in seasons:
                            if progress.wasCanceled():
                                progress.close()
                                return

                            # Fetch episodes for this season
                            episodes_data = self.fetch_episodes_sync(series_item, season)

                            if episodes_data:
                                episodes = episodes_data.get("data", [])
                                # Add series and season name to each episode for better identification
                                for episode in episodes:
                                    episode["series_name"] = series_name
                                    episode["season_name"] = season.get("name", "")
                                all_episodes.extend(episodes)

                    processed_series += 1
                    progress.setValue(int((processed_series / total_series) * 100))

            progress.setValue(100)
            progress.close()

            # Now export all episodes
            if all_episodes:
                save_stb_content(base_url, all_episodes, mac, file_path)
                QMessageBox.information(
                    self,
                    "Export Complete",
                    f"Successfully exported {len(all_episodes)} episodes to {file_path}",
                )
            else:
                QMessageBox.warning(
                    self,
                    "Export Warning",
                    "No episodes found to export.",
                )

        except Exception as e:
            progress.close()
            logger.error(f"Error during complete export: {e}")
            QMessageBox.critical(
                self,
                "Export Error",
                f"An error occurred during export: {str(e)}",
            )

    def fetch_seasons_sync(self, series_item):
        """Synchronously fetch seasons for a series."""
        selected_provider = self.provider_manager.current_provider
        headers = self.provider_manager.headers
        url = selected_provider.get("url", "")
        url = URLObject(url)
        base_url = f"{url.scheme}://{url.netloc}/server/load.php"

        params = {
            "type": "series",
            "action": "get_ordered_list",
            "category_id": series_item.get("category_id"),
            "movie_id": series_item.get("id"),
            "season_id": 0,
            "sortby": "name",
            "JsHttpRequest": "1-xml",
        }

        try:
            response = requests.get(base_url, headers=headers, params=params, timeout=10)
            if response.status_code == 200:
                return response.json().get("js", {})
        except Exception as e:
            logger.warning(f"Error fetching seasons for {series_item.get('name')}: {e}")

        return None

    def fetch_episodes_sync(self, series_item, season_item):
        """Synchronously fetch episodes for a season."""
        selected_provider = self.provider_manager.current_provider
        headers = self.provider_manager.headers
        url = selected_provider.get("url", "")
        url = URLObject(url)
        base_url = f"{url.scheme}://{url.netloc}/server/load.php"

        params = {
            "type": "series",
            "action": "get_ordered_list",
            "category_id": series_item.get("category_id"),
            "movie_id": series_item.get("id"),
            "season_id": season_item.get("id"),
            "sortby": "added",
            "JsHttpRequest": "1-xml",
        }

        try:
            response = requests.get(base_url, headers=headers, params=params, timeout=10)
            if response.status_code == 200:
                return response.json().get("js", {})
        except Exception as e:
            logger.warning(
                f"Error fetching episodes for {series_item.get('name')} - {season_item.get('name')}: {e}"
            )

        return None

    # save_m3u_content and save_stb_content moved to services/export.py

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
                # Run network download in a worker thread
                self.lock_ui_before_loading()
                thread = QThread()
                worker = M3ULoaderWorker(url)
                worker.moveToThread(thread)

                def on_started():
                    worker.run()

                def handle_finished_ui(payload):
                    try:
                        content = payload.get("content", "")
                        parsed_content = parse_m3u(content)
                        self.display_content(parsed_content)
                        self.provider_manager.current_provider_content[self.content_type] = (
                            parsed_content
                        )
                        self.save_provider()
                    finally:
                        thread.quit()
                        self.unlock_ui_after_loading()

                def on_finished(payload):
                    # Ensure UI updates run in the main thread
                    QTimer.singleShot(0, self, lambda: handle_finished_ui(payload))

                def on_error(msg):
                    def handle_error_ui():
                        logger.warning(f"Error loading M3U Playlist: {msg}")
                        thread.quit()
                        self.unlock_ui_after_loading()

                    QTimer.singleShot(0, self, handle_error_ui)

                def _cleanup():
                    try:
                        self._bg_jobs.remove((thread, worker))
                    except ValueError:
                        pass

                thread.started.connect(on_started)
                worker.finished.connect(on_finished, Qt.QueuedConnection)
                worker.error.connect(on_error, Qt.QueuedConnection)
                thread.finished.connect(_cleanup)
                thread.start()
                self._bg_jobs.append((thread, worker))
            else:
                with open(url, "r", encoding="utf-8") as file:
                    content = file.read()
                parsed_content = parse_m3u(content)
                self.display_content(parsed_content)
                self.provider_manager.current_provider_content[self.content_type] = parsed_content
                self.save_provider()
        except (requests.RequestException, IOError) as e:
            logger.warning(f"Error loading M3U Playlist: {e}")

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

                if (
                    self.can_show_content_info(item_type)
                    and self.config_manager.show_stb_content_info
                ):
                    self.switch_content_info_panel(item_type)
                    self.populate_movie_tvshow_content_info(item_data)
                elif self.can_show_epg(item_type) and self.config_manager.channel_epg:
                    self.switch_content_info_panel(item_type)
                    self.populate_channel_programs_content_info(item_data)
                else:
                    self.clear_content_info_panel()
                self.update_layout()

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
            elif item_type == "season":
                # Load episodes for the selected season
                self.navigation_stack.append(("series", self.current_series, item.text(0)))
                self.current_season = item_data
                self.load_season_episodes(item_data)
            elif item_type in ["m3ucontent", "channel", "movie"]:
                self.play_item(item_data, item_type=item_type)
            elif item_type == "episode":
                # Play the selected episode
                self.play_item(item_data, is_episode=True, item_type=item_type)
            else:
                logger.info("Unknown item type selected.")

            # Clear search box after navigating and force re-filtering if needed
            if len(self.navigation_stack) != nav_len:
                self.search_box.clear()
                if not self.search_box.isModified():
                    self.filter_content(self.search_box.text())
        else:
            logger.info("Item with no type selected.")

    def go_back(self):
        if self.navigation_stack:
            nav_type, previous_data, previous_selected_id = self.navigation_stack.pop()
            if nav_type == "root":
                # Display root categories
                content = self.provider_manager.current_provider_content.setdefault(
                    self.content_type, {}
                )
                categories = content.get("categories", [])
                self.display_categories(categories, select_first=previous_selected_id)
                self.current_category = None
            elif nav_type == "category":
                # Go back to category content
                self.current_category = previous_data
                self.load_content_in_category(
                    self.current_category, select_first=previous_selected_id
                )
                self.current_series = None
            elif nav_type == "series":
                # Go back to series seasons
                self.current_series = previous_data
                self.load_series_seasons(self.current_series, select_first=previous_selected_id)
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
        # Refresh provider combo in case providers were added/removed/renamed
        self.populate_provider_combo()

    # parse_m3u moved to services/m3u.py

    def load_stb_categories(self, url: str, headers: Optional[dict] = None):
        if headers is None:
            headers = self.provider_manager.headers
        # Run network calls in a worker thread
        self.lock_ui_before_loading()
        thread = QThread()
        worker = STBCategoriesWorker(url, headers, self.content_type)
        worker.moveToThread(thread)

        def on_started():
            worker.run()

        def handle_finished_ui(payload):
            try:
                categories = payload.get("categories", [])
                if not categories:
                    logger.info("No categories found.")
                    return
                provider_content = self.provider_manager.current_provider_content.setdefault(
                    self.content_type, {}
                )
                provider_content["categories"] = categories
                provider_content["contents"] = {}

                if self.content_type == "itv":
                    provider_content["contents"] = payload.get("all_channels", [])

                    sorted_channels: Dict[str, List[int]] = {}
                    for i in range(len(provider_content["contents"])):
                        genre_id = provider_content["contents"][i]["tv_genre_id"]
                        category_id = str(genre_id)
                        if category_id not in sorted_channels:
                            sorted_channels[category_id] = []
                        sorted_channels[category_id].append(i)

                    for cat in sorted_channels:
                        sorted_channels[cat].sort(
                            key=lambda x: int(provider_content["contents"][x]["number"])
                        )

                    if "None" in sorted_channels:
                        categories.append({"id": "None", "title": "Unknown Category"})

                    provider_content["sorted_channels"] = sorted_channels

                self.save_provider()
                self.display_categories(categories)
            finally:
                thread.quit()
                self.unlock_ui_after_loading()

        def on_finished(payload):
            QTimer.singleShot(0, self, lambda: handle_finished_ui(payload))

        def on_error(msg):
            def handle_error_ui():
                logger.warning(f"Error loading STB categories: {msg}")
                thread.quit()
                self.unlock_ui_after_loading()

            QTimer.singleShot(0, self, handle_error_ui)

        def _cleanup():
            try:
                self._bg_jobs.remove((thread, worker))
            except ValueError:
                pass

        thread.started.connect(on_started)
        worker.finished.connect(on_finished, Qt.QueuedConnection)
        worker.error.connect(on_error, Qt.QueuedConnection)
        thread.finished.connect(_cleanup)
        thread.start()
        self._bg_jobs.append((thread, worker))

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

    def load_content_in_category(self, category, select_first=True):
        content_data = self.provider_manager.current_provider_content.setdefault(
            self.content_type, {}
        )
        category_id = category.get("id", "*")

        if self.content_type == "itv":
            # Show only channels for the selected category
            if category_id == "*":
                items = content_data["contents"]
            else:
                items = [
                    content_data["contents"][i]
                    for i in content_data["sorted_channels"].get(category_id, [])
                ]
            self.display_content(items, content="channel")
        else:
            # Check if we have cached content for this category
            if category_id in content_data.get("contents", {}):
                items = content_data["contents"][category_id]
                if self.content_type == "itv":
                    self.display_content(items, content="channel", select_first=select_first)
                elif self.content_type == "series":
                    self.display_content(items, content="serie", select_first=select_first)
                elif self.content_type == "vod":
                    self.display_content(items, content="movie", select_first=select_first)
            else:
                # Fetch content for the category
                self.fetch_content_in_category(category_id, select_first=select_first)

    def fetch_content_in_category(self, category_id, select_first=True):

        # Ask confirmation if the user wants to load all content
        if category_id == "*":
            reply = QMessageBox.question(
                self,
                "Load All Content",
                "This will load all content in this category. Continue?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply == QMessageBox.No:
                return

        selected_provider = self.provider_manager.current_provider
        headers = self.provider_manager.headers
        url = selected_provider.get("url", "")
        url = URLObject(url)
        url = f"{url.scheme}://{url.netloc}/server/load.php"

        self.lock_ui_before_loading()
        if self.content_loader and self.content_loader.isRunning():
            self.content_loader.wait()
        self.content_loader = ContentLoader(
            url, headers, self.content_type, category_id=category_id
        )
        self.content_loader.content_loaded.connect(
            lambda data: self.update_content_list(data, select_first)
        )
        self.content_loader.progress_updated.connect(self.update_progress)
        self.content_loader.finished.connect(self.content_loader_finished)
        self.content_loader.start()
        self.cancel_button.setText("Cancel loading content in category")

    def load_series_seasons(self, series_item, select_first=True):
        selected_provider = self.provider_manager.current_provider
        headers = self.provider_manager.headers
        url = selected_provider.get("url", "")
        url = URLObject(url)
        url = f"{url.scheme}://{url.netloc}/server/load.php"

        self.current_series = series_item  # Store current series

        self.lock_ui_before_loading()
        if self.content_loader and self.content_loader.isRunning():
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

        self.content_loader.content_loaded.connect(
            lambda data: self.update_seasons_list(data, select_first)
        )
        self.content_loader.progress_updated.connect(self.update_progress)
        self.content_loader.finished.connect(self.content_loader_finished)
        self.content_loader.start()
        self.cancel_button.setText("Cancel loading seasons")

    def load_season_episodes(self, season_item, select_first=True):
        selected_provider = self.provider_manager.current_provider
        headers = self.provider_manager.headers
        url = selected_provider.get("url", "")
        url = URLObject(url)
        url = f"{url.scheme}://{url.netloc}/server/load.php"

        self.current_season = season_item  # Store current season

        if not self.current_category or not self.current_series:
            logger.warning("Current category/series not set when loading season episodes")
            return

        self.lock_ui_before_loading()
        if self.content_loader and self.content_loader.isRunning():
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
        self.content_loader.content_loaded.connect(
            lambda data: self.update_episodes_list(data, select_first)
        )
        self.content_loader.progress_updated.connect(self.update_progress)
        self.content_loader.finished.connect(self.content_loader_finished)
        self.content_loader.start()
        self.cancel_button.setText("Cancel loading episodes")

    def play_item(self, item_data, is_episode=False, item_type=None):
        if self.provider_manager.current_provider["type"] == "STB":
            # Create link in a worker thread, then play
            selected_provider = self.provider_manager.current_provider
            headers = self.provider_manager.headers
            base_url = selected_provider.get("url", "")
            cmd = item_data.get("cmd")
            series_param = item_data.get("series") if is_episode else None

            self.lock_ui_before_loading()
            thread = QThread()
            worker = LinkCreatorWorker(
                base_url=base_url,
                headers=headers,
                content_type=self.content_type,
                cmd=cmd,
                is_episode=is_episode,
                series_param=series_param,
            )
            worker.moveToThread(thread)

            def on_started():
                worker.run()

            def handle_finished_ui(payload):
                try:
                    link = self.sanitize_url(payload.get("link", ""))
                    if link:
                        self.link = link
                        self.player.play_video(link)
                        # Save last watched
                        self.save_last_watched(item_data, item_type or "channel", link)
                    else:
                        logger.warning("Failed to create link.")
                finally:
                    thread.quit()
                    self.unlock_ui_after_loading()

            def on_finished(payload):
                QTimer.singleShot(0, self, lambda: handle_finished_ui(payload))

            def on_error(msg):
                def handle_error_ui():
                    logger.warning(f"Error creating link: {msg}")
                    thread.quit()
                    self.unlock_ui_after_loading()

                QTimer.singleShot(0, self, handle_error_ui)

            def _cleanup():
                try:
                    self._bg_jobs.remove((thread, worker))
                except ValueError:
                    pass

            thread.started.connect(on_started)
            worker.finished.connect(on_finished, Qt.QueuedConnection)
            worker.error.connect(on_error, Qt.QueuedConnection)
            thread.finished.connect(_cleanup)
            thread.start()
            self._bg_jobs.append((thread, worker))
        else:
            cmd = item_data.get("cmd")
            self.link = cmd
            self.player.play_video(cmd)
            # Save last watched
            self.save_last_watched(item_data, item_type or "m3ucontent", cmd)

    def save_last_watched(self, item_data, item_type, link):
        """Save the last watched item to config"""
        self.config_manager.last_watched = {
            "item_data": item_data,
            "item_type": item_type,
            "link": link,
            "timestamp": datetime.now().isoformat(),
            "provider_name": self.provider_manager.current_provider.get("name", ""),
        }
        self.config_manager.save_config()

    def resume_last_watched(self):
        """Resume playing the last watched item"""
        last_watched = self.config_manager.last_watched
        if not last_watched:
            QMessageBox.information(self, "No History", "No previously watched content found.")
            return

        # Check if the provider matches
        current_provider_name = self.provider_manager.current_provider.get("name", "")
        if last_watched.get("provider_name") != current_provider_name:
            reply = QMessageBox.question(
                self,
                "Different Provider",
                f"Last watched content was from provider '{last_watched.get('provider_name')}'. Continue anyway?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply == QMessageBox.No:
                return
            # Switch provider automatically and resume after switching
            target_name = last_watched.get("provider_name", "")
            if target_name:
                # If provider exists, switch to it
                names = [p.get("name", "") for p in self.provider_manager.providers]
                if target_name in names:
                    self._pending_resume = last_watched
                    self.config_manager.selected_provider_name = target_name
                    self.config_manager.save_config()
                    # Trigger provider switch; playback continues in set_provider_finished
                    QTimer.singleShot(0, lambda: self.set_provider())
                    return

        # Play the last watched item
        item_data = last_watched.get("item_data")
        item_type = last_watched.get("item_type")
        is_episode = item_type == "episode"

        # For STB providers, always recreate the link (tokens expire)
        # For M3U/stream providers, can use stored link directly
        current_provider_type = self.provider_manager.current_provider.get("type", "")
        if current_provider_type == "STB":
            # Recreate link with fresh token
            self.play_item(item_data, is_episode=is_episode, item_type=item_type)
        elif last_watched.get("link"):
            # Use stored link for non-STB providers
            self.link = last_watched["link"]
            self.player.play_video(self.link)
        else:
            # Fallback: recreate the link
            self.play_item(item_data, is_episode=is_episode, item_type=item_type)

    def cancel_loading(self):
        if self.content_loader and self.content_loader.isRunning():
            self.content_loader.terminate()
            self.content_loader.wait()
            self.content_loader_finished()
            QMessageBox.information(self, "Cancelled", "Content loading has been cancelled.")
        elif self.image_loader and self.image_loader.isRunning():
            self.image_loader.terminate()
            self.image_loader.wait()
            self.image_loader_finished()
            self.image_manager.save_index()
            QMessageBox.information(self, "Cancelled", "Image loading has been cancelled.")

    def lock_ui_before_loading(self):
        self.update_ui_on_loading(loading=True)

    def unlock_ui_after_loading(self):
        self.update_ui_on_loading(loading=False)

    def update_ui_on_loading(self, loading):
        self.open_button.setEnabled(not loading)
        self.options_button.setEnabled(not loading)
        self.export_button.setEnabled(not loading)
        self.update_button.setEnabled(not loading)
        self.back_button.setEnabled(not loading)
        self.progress_bar.setVisible(loading)
        self.cancel_button.setVisible(loading)
        self.content_switch_group.setEnabled(not loading)
        if loading:
            self.content_list.setSelectionMode(QListWidget.NoSelection)
        else:
            self.content_list.setSelectionMode(QListWidget.SingleSelection)

    def content_loader_finished(self):
        if self.content_loader:
            self.content_loader.deleteLater()
            self.content_loader = None
        self.unlock_ui_after_loading()

    def image_loader_finished(self):
        if self.image_loader:
            self.image_loader.deleteLater()
            self.image_loader = None
        self.unlock_ui_after_loading()

    def update_content_list(self, data, select_first=True):
        category_id = data.get("category_id")
        items = data.get("items")

        # Cache the items in config
        selected_provider = self.provider_manager.current_provider_content
        content_data = selected_provider.setdefault(self.content_type, {})
        contents = content_data.setdefault("contents", {})
        contents[category_id] = items
        self.save_provider()

        if self.content_type == "series":
            self.display_content(items, content="serie", select_first=select_first)
        elif self.content_type == "vod":
            self.display_content(items, content="movie", select_first=select_first)
        elif self.content_type == "itv":
            self.display_content(items, content="channel", select_first=select_first)

    def update_seasons_list(self, data, select_first=True):
        if not self.current_series:
            logger.warning("Current series not set when updating seasons list")
            return
        items = data.get("items")
        for item in items:
            item["number"] = item["name"].split(" ")[-1]
            item["name"] = f'{self.current_series["name"]}.{item["name"]}'
        self.display_content(items, content="season", select_first=select_first)

    def update_episodes_list(self, data, select_first=True):
        if not self.current_series:
            logger.warning("Current series not set when updating episodes list")
            return
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
                # merge episode data with series data
                episode_item = self.current_series.copy()
                episode_item["number"] = f"{episode_num}"
                episode_item["ename"] = f"Episode {episode_num}"
                episode_item["cmd"] = selected_season.get("cmd")
                episode_item["series"] = episode_num
                episode_items.append(episode_item)
            self.display_content(episode_items, content="episode", select_first=select_first)
        else:
            logger.info("Season not found in data.")

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
                    f"&cmd={url_quote(cmd)}&series={series_param}&JsHttpRequest=1-xml"
                )
            else:
                fetchurl = (
                    f"{url}/server/load.php?type={self.content_type}&action=create_link"
                    f"&cmd={url_quote(cmd)}&JsHttpRequest=1-xml"
                )
            response = requests.get(fetchurl, headers=headers, timeout=5)
            if response.status_code != 200 or not response.content:
                logger.warning(
                    f"Error creating link: status code {response.status_code}, response content empty"
                )
                return None
            result = response.json()
            link = result["js"]["cmd"].split(" ")[-1]
            link = self.sanitize_url(link)
            self.link = link
            return link
        except Exception as e:
            logger.warning(f"Error creating link: {e}")
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
        return item.text(1 if item_type == "channel" else 0)

    @staticmethod
    def get_logo_column(item_type):
        return 0 if item_type == "m3ucontent" else 1
