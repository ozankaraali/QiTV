import base64
import html
import logging
import os
import platform
import re
import shutil
import subprocess
import time
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

import requests
from PySide6.QtCore import QBuffer, QObject, QRect, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QTextCursor,
    QTextDocument,
    QTextOption,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionProgressBar,
    QStyleOptionViewItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from urlobject import URLObject

logger = logging.getLogger(__name__)

from content_loader import ContentLoader
from image_loader import ImageLoader
from options import OptionsDialog


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

    def __init__(self, base_url: str, headers: dict):
        super().__init__()
        self.base_url = base_url
        self.headers = headers

    def run(self):
        try:
            url = URLObject(self.base_url)
            base = f"{url.scheme}://{url.netloc}"
            fetchurl = (
                f"{base}/server/load.php?type=itv&action=get_genres&JsHttpRequest=1-xml"
            )
            resp = requests.get(fetchurl, headers=self.headers, timeout=10)
            resp.raise_for_status()
            categories = resp.json()["js"]

            fetchurl = f"{base}/server/load.php?type=itv&action=get_all_channels&JsHttpRequest=1-xml"
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
                    f"&cmd={requests.utils.quote(self.cmd)}&series={self.series_param}&JsHttpRequest=1-xml"
                )
            else:
                fetchurl = (
                    f"{base}/server/load.php?type={self.content_type}&action=create_link"
                    f"&cmd={requests.utils.quote(self.cmd)}&JsHttpRequest=1-xml"
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
            return self.data(sort_column, Qt.UserRole) < other.data(
                sort_column, Qt.UserRole
            )
        elif sort_column == 3:  # EPG Program name
            return self.data(sort_column, Qt.UserRole) < other.data(
                sort_column, Qt.UserRole
            )

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


class HtmlItemDelegate(QStyledItemDelegate):
    elidedPostfix = "..."
    doc = QTextDocument()
    doc.setDocumentMargin(1)

    def __init__(self):
        super().__init__()

    def paint(self, painter, inOption, index):
        options = QStyleOptionViewItem(inOption)
        self.initStyleOption(options, index)
        if not options.text:
            return super().paint(painter, inOption, index)
        style = options.widget.style() if options.widget else QApplication.style()

        textOption = QTextOption()
        textOption.setWrapMode(
            QTextOption.WordWrap
            if options.features & QStyleOptionViewItem.WrapText
            else QTextOption.ManualWrap
        )
        textOption.setTextDirection(options.direction)

        self.doc.setDefaultTextOption(textOption)
        self.doc.setHtml(options.text)
        self.doc.setDefaultFont(options.font)
        self.doc.setTextWidth(options.rect.width())
        self.doc.adjustSize()

        if self.doc.size().width() > options.rect.width():
            # Elide text
            cursor = QTextCursor(self.doc)
            cursor.movePosition(QTextCursor.End)
            metric = QFontMetrics(options.font)
            postfixWidth = metric.horizontalAdvance(self.elidedPostfix)
            while self.doc.size().width() > options.rect.width() - postfixWidth:
                cursor.deletePreviousChar()
                self.doc.adjustSize()
            cursor.insertText(self.elidedPostfix)

        # Painting item without text (this takes care of painting e.g. the highlighted for selected
        # or hovered over items in an ItemView)
        options.text = ""
        style.drawControl(QStyle.CE_ItemViewItem, options, painter, inOption.widget)

        # Figure out where to render the text in order to follow the requested alignment
        textRect = style.subElementRect(QStyle.SE_ItemViewItemText, options)
        documentSize = QSize(
            self.doc.size().width(), self.doc.size().height()
        )  # Convert QSizeF to QSize
        layoutRect = QRect(
            QStyle.alignedRect(
                Qt.LayoutDirectionAuto, options.displayAlignment, documentSize, textRect
            )
        )

        painter.save()

        # Translate the painter to the origin of the layout rectangle in order for the text to be
        # rendered at the correct position
        painter.translate(layoutRect.topLeft())
        self.doc.drawContents(painter, textRect.translated(-textRect.topLeft()))

        painter.restore()

    def sizeHint(self, inOption, index):
        options = QStyleOptionViewItem(inOption)
        self.initStyleOption(options, index)
        if not options.text:
            return super().sizeHint(inOption, index)
        self.doc.setHtml(options.text)
        self.doc.setTextWidth(options.rect.width())
        return QSize(self.doc.idealWidth(), self.doc.size().height())


class ChannelItemDelegate(QStyledItemDelegate):
    def __init__(self):
        super().__init__()
        # Create a default font to avoid font family issues
        self.default_font = QFont()
        self.default_font.setPointSize(12)

    def paint(self, painter, inOption, index):
        col = index.column()
        if col == 2:  # EPG program progress
            progress = index.data(Qt.UserRole)
            if progress is not None:
                options = QStyleOptionViewItem(inOption)
                self.initStyleOption(options, index)

                # Draw selection background first
                style = (
                    options.widget.style() if options.widget else QApplication.style()
                )
                style.drawPrimitive(
                    QStyle.PE_PanelItemViewItem, options, painter, options.widget
                )

                # Save painter state
                painter.save()

                # Calculate progress bar dimensions with padding
                padding = 4
                rect = options.rect.adjusted(padding, padding, -padding, -padding)

                # Draw background (gray rectangle)
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(200, 200, 200))
                painter.drawRect(rect)

                # Draw progress (blue rectangle)
                if progress > 0:
                    progress_width = int((rect.width() * progress) / 100)
                    progress_rect = QRect(
                        rect.x(), rect.y(), progress_width, rect.height()
                    )
                    painter.setBrush(QColor(0, 120, 215))  # Windows 10 style blue
                    painter.drawRect(progress_rect)

                # Restore painter state
                painter.restore()
            else:
                super().paint(painter, inOption, index)
        elif col == 3:  # EPG program name
            epg_text = index.data(Qt.UserRole)
            if epg_text:
                options = QStyleOptionViewItem(inOption)
                self.initStyleOption(options, index)
                style = (
                    options.widget.style() if options.widget else QApplication.style()
                )
                options.text = epg_text
                style.drawControl(
                    QStyle.CE_ItemViewItem, options, painter, inOption.widget
                )
            else:
                super().paint(painter, inOption, index)
        else:
            super().paint(painter, inOption, index)

    def sizeHint(self, option, index):
        col = index.column()
        if col == 2:  # EPG program progress
            # Set a minimum width of 100 pixels and height of 24 pixels for the progress bar column
            return QSize(100, 24)
        elif col == 3:  # EPG program name
            options = QStyleOptionViewItem(option)
            self.initStyleOption(options, index)
            style = options.widget.style() if options.widget else QApplication.style()
            text = index.data(Qt.UserRole)
            font = options.font
            if not font:
                font = style.font(QStyle.CE_ItemViewItem, options, index)
            metrics = QFontMetrics(font)
            return QSize(metrics.boundingRect(text).width(), metrics.height())
        return super().sizeHint(option, index)


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

    def __init__(
        self, app, player, config_manager, provider_manager, image_manager, epg_manager
    ):
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
        self.current_list_content = None
        self.content_info_show = None

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
                epg_data = self.epg_manager.get_programs_for_channel(
                    item_data["data"], None, 1
                )
                if epg_data:
                    epg_item = epg_data[0]
                    if epg_source == "STB":
                        start_time = datetime.strptime(
                            epg_item["time"], "%Y-%m-%d %H:%M:%S"
                        )
                        end_time = datetime.strptime(
                            epg_item["time_to"], "%Y-%m-%d %H:%M:%S"
                        )
                    else:
                        start_time = datetime.strptime(
                            epg_item["@start"], "%Y%m%d%H%M%S %z"
                        )
                        end_time = datetime.strptime(
                            epg_item["@stop"], "%Y%m%d%H%M%S %z"
                        )
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
                        epg_text = f"{epg_item['name']}"
                    else:
                        epg_text = f"{epg_item['title'].get('__text')}"
                    item.setData(2, Qt.UserRole, progress)
                    item.setData(3, Qt.UserRole, epg_text)
                else:
                    item.setData(2, Qt.UserRole, None)
                    item.setData(3, Qt.UserRole, "")

        self.content_list.viewport().update()

    def set_provider(self, force_update=False):
        self.lock_ui_before_loading()
        self.progress_bar.setRange(0, 0)  # busy indicator

        if force_update:
            self.provider_manager.clear_current_provider_cache()

        self.set_provider_thread = SetProviderThread(
            self.provider_manager, self.epg_manager
        )
        self.set_provider_thread.progress.connect(self.update_busy_progress)
        self.set_provider_thread.finished.connect(
            lambda: self.set_provider_finished(force_update)
        )
        self.set_provider_thread.start()

    def set_provider_finished(self, force_update=False):
        self.progress_bar.setRange(0, 100)  # Stop busy indicator
        if hasattr(self, "set_provider_thread"):
            self.set_provider_thread.deleteLater()
            del self.set_provider_thread
        self.unlock_ui_after_loading()

        # No need to switch content type if not STB
        selected_provider = self.provider_manager.current_provider
        config_type = selected_provider.get("type", "")
        self.content_switch_group.setVisible(config_type == "STB")

        if force_update:
            self.update_content()
        else:
            self.load_content()

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
        self.update_button.clicked.connect(lambda: self.set_provider(force_update=True))
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

        self.rescanlogo_button = QPushButton("Rescan Channel Logos")
        self.rescanlogo_button.clicked.connect(self.rescan_logos)
        self.rescanlogo_button.setVisible(False)
        bottom_layout.addWidget(self.rescanlogo_button)

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
        self.refresh_content_list_size()

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
        self.content_list.sortItems(
            sort_column, self.content_list.header().sortIndicatorOrder()
        )

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
                int(
                    self.content_info_panel.width()
                    * (1 - self.splitter_content_info_ratio)
                ),
            ]
        )
        self.content_info_shown = "channel"

        self.program_list.itemSelectionChanged.connect(self.update_channel_program)
        self.splitter_content_info.splitterMoved.connect(
            self.update_splitter_content_info_ratio
        )

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
        self.program_list.itemSelectionChanged.disconnect()
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
                self.content_info_text.setText(
                    f'No EPG found for channel id "{xmltv_id}"'
                )
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
                        info += f"<b>Category:</b> {', '.join([c.get('__text') for c in category])}<br>"
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
                            info += f"<b>Actor:</b> {', '.join([c.get('__text') for c in actor])}<br>"
                    if guest:
                        if isinstance(guest, dict):
                            info += f"<b>Guest:</b> {guest.get('__text')}<br>"
                        elif isinstance(guest, list):
                            info += f"<b>Guest:</b> {', '.join([c.get('__text') for c in guest])}<br>"
                    if writer:
                        if isinstance(writer, dict):
                            info += f"<b>Writer:</b> {writer.get('__text')}<br>"
                        elif isinstance(writer, list):
                            info += f"<b>Writer:</b> {', '.join([c.get('__text') for c in writer])}<br>"
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
                            info += f"<b>Editor:</b> {', '.join([c.get('__text') for c in editor])}<br>"
                if country:
                    info += f"<b>Country:</b> {country.get('__text')}<br>"

                self.content_info_text.setText(info if info else "No data available")

                # Load poster image if available
                icon_url = item_data.get("icon", {}).get("@src")
                if icon_url:
                    self.lock_ui_before_loading()
                    if hasattr(self, "image_loader") and self.image_loader.isRunning():
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
            if hasattr(self, "image_loader") and self.image_loader.isRunning():
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
        if hasattr(self, "image_loader") and self.image_loader.isRunning():
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
        self.content_list.itemSelectionChanged.disconnect(self.item_selected)
        self.content_list.clear()
        # Re-egister the content_list selection change event
        self.content_list.itemSelectionChanged.connect(self.item_selected)

        # Stop refreshing content list
        self.refresh_on_air_timer.stop()

        self.current_list_content = "category"

        self.content_list.setSortingEnabled(False)
        self.content_list.setColumnCount(1)
        if self.content_type == "itv":
            self.content_list.setHeaderLabels(
                [f"Channel Categories ({len(categories)})"]
            )
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
                    self.content_list.scrollToItem(
                        previous_selected[0], QTreeWidget.PositionAtTop
                    )

    def display_content(self, items, content="m3ucontent", select_first=True):
        # Unregister the selection change event
        self.content_list.itemSelectionChanged.disconnect(self.item_selected)
        self.content_list.clear()
        self.content_list.setSortingEnabled(False)
        # Re-register the selection change event
        self.content_list.itemSelectionChanged.connect(self.item_selected)

        # Stop refreshing On Air content
        self.refresh_on_air_timer.stop()

        self.current_list_content = content
        need_logos = (
            content in ["channel", "m3ucontent"] and self.config_manager.channel_logos
        )
        logo_urls = []
        use_epg = self.can_show_epg(content) and self.config_manager.channel_epg

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
                    self.shorten_header(
                        f"{category_header} > {serie_header} > Seasons"
                    ),
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
                "headers": [f"Name ({len(items)})", "Group"]
                + (["", "On Air"] if use_epg else []),
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
                if key == "added":
                    # Change a date time from "YYYY-MM-DD HH:MM:SS" to "YYYY-MM-DD" only
                    list_item.setText(
                        i, html.unescape(item_data.get(key, "")).split()[0]
                    )
                else:
                    list_item.setText(i, html.unescape(item_data.get(key, "")))

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
                    self.content_list.scrollToItem(
                        previous_selected[0], QTreeWidget.PositionAtTop
                    )

        # Load channel logos if needed
        self.rescanlogo_button.setVisible(need_logos)
        if need_logos:
            self.lock_ui_before_loading()
            if hasattr(self, "image_loader") and self.image_loader.isRunning():
                self.image_loader.wait()
            self.image_loader = ImageLoader(
                logo_urls, self.image_manager, iconified=True
            )
            self.image_loader.progress_updated.connect(self.update_channel_logos)
            self.image_loader.finished.connect(self.image_loader_finished)
            self.image_loader.start()
            self.cancel_button.setText("Cancel fetching channel logos...")

    def update_channel_logos(self, current, total, data):
        self.update_progress(current, total)
        if data:
            qicon = data.get("icon", None)
            if qicon:
                logo_column = ChannelList.get_logo_column(self.current_list_content)
                rank = data["rank"]
                item = self.content_list.topLevelItem(rank)
                item.setIcon(logo_column, qicon)

    def update_poster(self, current, total, data):
        self.update_progress(current, total)
        if data:
            pixmap = data.get("pixmap", None)
            if pixmap:
                scaled_pixmap = pixmap.scaled(
                    200, 300, Qt.KeepAspectRatio, Qt.SmoothTransformation
                )
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
            provider_itv_content = (
                self.provider_manager.current_provider_content.setdefault("itv", {})
            )
            categories_list = provider_itv_content.setdefault("categories", [])
            categories = {
                c.get("id", "None"): c.get("title", "Unknown Category")
                for c in categories_list
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

    def save_channel_list(
        self, base_url, channels_data, categories, mac, file_path
    ) -> None:
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
                logger.info(f"Channels = {count}")
                logger.info(f"Channel list has been dumped to {file_path}")
        except IOError as e:
            logger.warning(f"Error saving channel list: {e}")

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
            provider_content = (
                self.provider_manager.current_provider_content.setdefault(
                    self.content_type, {}
                )
            )

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
                logger.info(f"Unknown provider type: {config_type}")

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
                logger.info(f"Items exported: {count}")
                logger.info(f"Content list has been saved to {file_path}")
        except IOError as e:
            logger.warning(f"Error saving content list: {e}")

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
                logger.info(f"Items exported: {count}")
                logger.info(f"Content list has been saved to {file_path}")
        except IOError as e:
            logger.warning(f"Error saving content list: {e}")

    def save_config(self):
        self.config_manager.save_config()

    def save_provider(self):
        self.provider_manager.save_provider()

    def load_content(self):
        selected_provider = self.provider_manager.current_provider
        config_type = selected_provider.get("type", "")
        content = self.provider_manager.current_provider_content.setdefault(
            self.content_type, {}
        )
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
            self.load_stb_categories(
                selected_provider["url"], self.provider_manager.headers
            )
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

                def on_finished(payload):
                    try:
                        content = payload.get("content", "")
                        parsed_content = self.parse_m3u(content)
                        self.display_content(parsed_content)
                        self.provider_manager.current_provider_content[
                            self.content_type
                        ] = parsed_content
                        self.save_provider()
                    finally:
                        thread.quit()
                        thread.wait()
                        try:
                            self._bg_jobs.remove((thread, worker))
                        except ValueError:
                            pass
                        self.unlock_ui_after_loading()

                def on_error(msg):
                    logger.warning(f"Error loading M3U Playlist: {msg}")
                    thread.quit()
                    thread.wait()
                    try:
                        self._bg_jobs.remove((thread, worker))
                    except ValueError:
                        pass
                    self.unlock_ui_after_loading()

                thread.started.connect(on_started)
                worker.finished.connect(on_finished)
                worker.error.connect(on_error)
                thread.start()
                self._bg_jobs.append((thread, worker))
            else:
                with open(url, "r", encoding="utf-8") as file:
                    content = file.read()
                parsed_content = self.parse_m3u(content)
                self.display_content(parsed_content)
                self.provider_manager.current_provider_content[self.content_type] = (
                    parsed_content
                )
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
                self.navigation_stack.append(
                    ("root", self.current_category, item.text(0))
                )
                self.current_category = item_data
                self.load_content_in_category(item_data)
            elif item_type == "serie":
                if self.content_type == "series":
                    # For series, load seasons
                    self.navigation_stack.append(
                        ("category", self.current_category, item.text(0))
                    )
                    self.current_series = item_data
                    self.load_series_seasons(item_data)
            elif item_type == "season":
                # Load episodes for the selected season
                self.navigation_stack.append(
                    ("series", self.current_series, item.text(0))
                )
                self.current_season = item_data
                self.load_season_episodes(item_data)
            elif item_type in ["m3ucontent", "channel", "movie"]:
                self.play_item(item_data)
            elif item_type == "episode":
                # Play the selected episode
                self.play_item(item_data, is_episode=True)
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
                self.load_series_seasons(
                    self.current_series, select_first=previous_selected_id
                )
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
                user_agent_match = re.search(r'user-agent="([^"]+)"', line)
                item_name_match = re.search(r",([^,]+)$", line)

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
        # Run network calls in a worker thread
        self.lock_ui_before_loading()
        thread = QThread()
        worker = STBCategoriesWorker(url, headers)
        worker.moveToThread(thread)

        def on_started():
            worker.run()

        def on_finished(payload):
            try:
                categories = payload.get("categories", [])
                if not categories:
                    logger.info("No categories found.")
                    return
                provider_content = (
                    self.provider_manager.current_provider_content.setdefault(
                        self.content_type, {}
                    )
                )
                provider_content["categories"] = categories
                provider_content["contents"] = {}

                if self.content_type == "itv":
                    provider_content["contents"] = payload.get("all_channels", [])

                    sorted_channels = {}
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
                thread.wait()
                try:
                    self._bg_jobs.remove((thread, worker))
                except ValueError:
                    pass
                self.unlock_ui_after_loading()

        def on_error(msg):
            logger.warning(f"Error loading STB categories: {msg}")
            thread.quit()
            thread.wait()
            try:
                self._bg_jobs.remove((thread, worker))
            except ValueError:
                pass
            self.unlock_ui_after_loading()

        thread.started.connect(on_started)
        worker.finished.connect(on_finished)
        worker.error.connect(on_error)
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
                    self.display_content(
                        items, content="channel", select_first=select_first
                    )
                elif self.content_type == "series":
                    self.display_content(
                        items, content="serie", select_first=select_first
                    )
                elif self.content_type == "vod":
                    self.display_content(
                        items, content="movie", select_first=select_first
                    )
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
        if hasattr(self, "content_loader") and self.content_loader.isRunning():
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

        self.lock_ui_before_loading()
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
        self.content_loader.content_loaded.connect(
            lambda data: self.update_episodes_list(data, select_first)
        )
        self.content_loader.progress_updated.connect(self.update_progress)
        self.content_loader.finished.connect(self.content_loader_finished)
        self.content_loader.start()
        self.cancel_button.setText("Cancel loading episodes")

    def play_item(self, item_data, is_episode=False):
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

            def on_finished(payload):
                try:
                    link = self.sanitize_url(payload.get("link", ""))
                    if link:
                        self.link = link
                        self.player.play_video(link)
                    else:
                        logger.warning("Failed to create link.")
                finally:
                    thread.quit()
                    thread.wait()
                    try:
                        self._bg_jobs.remove((thread, worker))
                    except ValueError:
                        pass
                    self.unlock_ui_after_loading()

            def on_error(msg):
                logger.warning(f"Error creating link: {msg}")
                thread.quit()
                thread.wait()
                try:
                    self._bg_jobs.remove((thread, worker))
                except ValueError:
                    pass
                self.unlock_ui_after_loading()

            thread.started.connect(on_started)
            worker.finished.connect(on_finished)
            worker.error.connect(on_error)
            thread.start()
            self._bg_jobs.append((thread, worker))
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
        elif hasattr(self, "image_loader") and self.image_loader.isRunning():
            self.image_loader.terminate()
            if hasattr(self, "image_loader"):
                self.image_loader.wait()
            self.image_loader_finished()
            self.image_manager.save_index()
            QMessageBox.information(
                self, "Cancelled", "Image loading has been cancelled."
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
        if loading:
            self.content_list.setSelectionMode(QListWidget.NoSelection)
        else:
            self.content_list.setSelectionMode(QListWidget.SingleSelection)

    def content_loader_finished(self):
        if hasattr(self, "content_loader"):
            self.content_loader.deleteLater()
            del self.content_loader
        self.unlock_ui_after_loading()

    def image_loader_finished(self):
        if hasattr(self, "image_loader"):
            self.image_loader.deleteLater()
            del self.image_loader
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
        items = data.get("items")
        for item in items:
            item["number"] = item["name"].split(" ")[-1]
            item["name"] = f'{self.current_series["name"]}.{item["name"]}'
        self.display_content(items, content="season", select_first=select_first)

    def update_episodes_list(self, data, select_first=True):
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
            self.display_content(
                episode_items, content="episode", select_first=select_first
            )
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
                    f"&cmd={requests.utils.quote(cmd)}&series={series_param}&JsHttpRequest=1-xml"
                )
            else:
                fetchurl = (
                    f"{url}/server/load.php?type={self.content_type}&action=create_link"
                    f"&cmd={requests.utils.quote(cmd)}&JsHttpRequest=1-xml"
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
