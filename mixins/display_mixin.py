"""Content display, filtering, info panel, favorites, and logo methods."""

import base64
from collections.abc import Buffer, Generator
from contextlib import contextmanager
from datetime import datetime
import html
import logging
from time import monotonic
from typing import TYPE_CHECKING, Any, Dict, List, Optional, cast

from PySide6.QtCore import QBuffer, QObject, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QWidget,
)
import tzlocal

from image_loader import ImageLoader
from widgets.delegates import ChannelItemDelegate, HtmlItemDelegate
from workers import CategoryTreeWidgetItem, ChannelTreeWidgetItem, NumberedTreeWidgetItem

if TYPE_CHECKING:
    from config_manager import ConfigManager
    from epg_manager import EpgManager
    from image_manager import ImageManager
    from provider_manager import ProviderManager
    from widgets.sidebar import Sidebar
    from widgets.top_bar import TopBar

logger = logging.getLogger(__name__)


class DisplayMixin:
    """Mixin providing display, filtering, info panel, favorites, and logo functionality."""

    # Provided by ChannelList at runtime
    provider_manager: "ProviderManager"
    config_manager: "ConfigManager"
    image_manager: "ImageManager"
    epg_manager: "EpgManager"
    content_type: str
    content_list: QTreeWidget
    content_info_panel: QWidget
    content_info_text: QLabel
    content_info_shown: Optional[str]
    top_bar: "TopBar"
    sidebar: "Sidebar"
    progress_bar: QProgressBar
    cancel_button: QPushButton
    splitter: QSplitter
    container_widget: QWidget
    splitter_ratio: float
    program_list: QListWidget
    refresh_on_air_timer: QTimer
    navigation_stack: List[Any]
    forward_stack: List[Any]
    _suppress_forward_clear: bool
    current_list_content: Optional[str]
    current_category: Optional[Dict[str, Any]]
    current_series: Optional[Dict[str, Any]]
    current_season: Optional[Dict[str, Any]]
    image_loader: Optional[ImageLoader]
    _all_provider_cache_snapshot: List[Any]
    _retired_image_loaders: List[ImageLoader]
    _content_renderer: Optional[Generator[None, None, None]] = None
    _render_generation = 0

    def cancel_content_render(self):
        self._render_generation += 1
        timer = getattr(self, "_content_render_timer", None)
        if timer is not None:
            timer.stop()
        renderer = self._content_renderer
        self._content_renderer = None
        if renderer is not None:
            renderer.close()

    def _continue_content_render(self):
        renderer = self._content_renderer
        if renderer is None:
            return
        generation = self._render_generation
        # A completion callback may navigate again; never close a running generator.
        self._content_renderer = None
        try:
            next(renderer)
        except StopIteration:
            return
        if generation != self._render_generation:
            renderer.close()
            return
        self._content_renderer = renderer
        self._content_render_timer.start(1)

    @contextmanager
    def _batch_content_updates(self, sorting=None):
        """Keep selection callbacks and repaints out of structural tree changes."""
        updates_enabled = self.content_list.updatesEnabled()
        signals_blocked = self.content_list.blockSignals(True)
        # Filtering changes visibility only; do not re-sort the entire catalog.
        self.content_list.setUpdatesEnabled(False)
        if sorting is not None:
            self.content_list.setSortingEnabled(False)
        try:
            yield
        finally:
            if sorting is not None:
                self.content_list.setSortingEnabled(sorting)
            self.content_list.blockSignals(signals_blocked)
            self.content_list.setUpdatesEnabled(updates_enabled)

    def toggle_content_type(self, content_type=None):
        """Switch content type. Called by sidebar or legacy code."""
        if content_type:
            self.content_type = content_type
        self.current_category = None
        self.current_series = None
        self.current_season = None
        self.navigation_stack.clear()
        self.forward_stack.clear()
        self.load_content()
        self.top_bar.clear_search()

    def display_categories(self, categories, select_first=True):
        self.cancel_content_render()
        self.stop_image_loading()
        self.refresh_on_air_timer.stop()
        self.current_list_content = "category"
        favorites = set(self.config_manager.favorites)

        with self._batch_content_updates(sorting=True):
            self.content_list.clear()
            self.content_list.setColumnCount(1)
            if self.content_type == "itv":
                self.content_list.setHeaderLabels([f"Channel Categories ({len(categories)})"])
            elif self.content_type == "vod":
                self.content_list.setHeaderLabels([f"Movie Categories ({len(categories)})"])
            elif self.content_type == "series":
                self.content_list.setHeaderLabels([f"Serie Categories ({len(categories)})"])

            for category in categories:
                item = CategoryTreeWidgetItem(self.content_list)
                item.setText(0, category.get("title", "Unknown Category"))
                item.setData(0, Qt.UserRole, {"type": "category", "data": category})
                if category.get("title", "") in favorites:
                    item.setBackground(0, QColor(201, 107, 67, 24))

            self.content_list.header().setSortIndicator(0, Qt.AscendingOrder)
        self.top_bar.set_back_visible(False)

        self.clear_content_info_panel()

        # Select an item in the list (first or a previously selected)
        if select_first:
            if select_first == True:
                first_item = self.content_list.topLevelItem(0)
                if first_item is not None:
                    self.content_list.setCurrentItem(first_item)
            else:
                previous_selected_id = select_first
                previous_selected = self.content_list.findItems(
                    previous_selected_id, Qt.MatchExactly, 0
                )
                if previous_selected:
                    self.content_list.setCurrentItem(previous_selected[0])
                    self.content_list.scrollToItem(previous_selected[0], QTreeWidget.PositionAtTop)

    def display_content(self, items, content="m3ucontent", select_first=True, on_complete=None):
        self.cancel_content_render()
        self.stop_image_loading()
        self.refresh_on_air_timer.stop()
        self.clear_content_info_panel()

        self.current_list_content = content
        need_logos = content in ["channel", "m3ucontent"] and self.config_manager.channel_logos
        logo_urls = []
        logo_items = []
        use_epg = self.can_show_epg(content) and self.config_manager.channel_epg

        # Define headers for different content types
        category_header = self.current_category.get("title", "") if self.current_category else ""
        serie_header = self.current_series.get("name", "") if self.current_series else ""
        season_header = self.current_season.get("name", "") if self.current_season else ""
        try:
            serie_headers_str = f"{category_header} > Series ({len(items)})"
            serie_headers_shortened = self.shorten_header(serie_headers_str)
        except Exception as e:
            logger.error(f"display_content: Error creating serie headers: {e}", exc_info=True)
            serie_headers_shortened = "Series"

        header_info = {
            "serie": {
                "headers": [
                    serie_headers_shortened,
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
                    (
                        f"{category_header} > {serie_header} > Seasons"[:50]
                        if len(f"{category_header} > {serie_header} > Seasons") > 50
                        else f"{category_header} > {serie_header} > Seasons"
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
                "headers": [f"Name ({len(items)})", "Group"] + (["", "On Air"] if use_epg else []),
                "keys": ["name", "group"],
            },
        }

        headers = header_info[content]["headers"]
        check_fav = content in ["channel", "movie", "serie", "m3ucontent"]
        favorites = set(self.config_manager.favorites) if check_fav else set()

        with self._batch_content_updates(sorting=False):
            self.content_list.clear()
            self.content_list.setColumnCount(len(headers))
            self.content_list.setHeaderLabels(headers)
            self.content_list.header().setSortIndicator(0, Qt.AscendingOrder)
        self.top_bar.set_back_visible(content != "m3ucontent")
        if use_epg:
            self.content_list.setItemDelegate(ChannelItemDelegate())
            self.content_list.setColumnWidth(2, 100)
            self.content_list.header().setMinimumSectionSize(100)

        def populate():
            position = 0
            while position < len(items):
                first = position
                deadline = monotonic() + 0.008
                batch: List[QTreeWidgetItem] = []
                # Build detached items: one insertion notification per batch,
                # rather than model updates for every field on every row.
                while position < len(items) and len(batch) < 1000:
                    item_data = items[position]
                    texts = []
                    for key in header_info[content]["keys"]:
                        raw_value = item_data.get(key)
                        if key == "added":
                            text_value = str(raw_value).split()[0] if raw_value else ""
                        else:
                            text_value = (
                                html.unescape(str(raw_value)) if raw_value is not None else ""
                            )
                        texts.append(text_value)
                    list_item: QTreeWidgetItem
                    if content == "channel":
                        list_item = ChannelTreeWidgetItem(texts)
                    elif content in ["season", "episode"]:
                        list_item = NumberedTreeWidgetItem(texts)
                    else:
                        list_item = QTreeWidgetItem(texts)
                    list_item.setData(0, Qt.UserRole, {"type": content, "data": item_data})
                    if need_logos:
                        logo_urls.append(item_data.get("logo", ""))
                        logo_items.append(list_item)
                    if check_fav and (item_data.get("name") or item_data.get("title")) in favorites:
                        list_item.setBackground(0, QColor(201, 107, 67, 24))
                    batch.append(list_item)
                    position += 1
                    if monotonic() >= deadline:
                        break
                with self._batch_content_updates():
                    self.content_list.addTopLevelItems(batch)
                    if use_epg:
                        self.refresh_on_air(first, position)
                if position < len(items):
                    self.content_list.headerItem().setText(0, f"{headers[0]} (loading...)")
                    yield
            with self._batch_content_updates(sorting=True):
                self.content_list.setHeaderLabels(headers)
            self._finish_content_render(
                headers, select_first, use_epg, logo_urls, logo_items, on_complete
            )

        if not hasattr(self, "_content_render_timer"):
            self._content_render_timer = QTimer(cast(QObject, self))
            self._content_render_timer.setSingleShot(True)
            self._content_render_timer.timeout.connect(self._continue_content_render)
        self._content_renderer = populate()
        self._continue_content_render()

    def _finish_content_render(
        self, headers, select_first, use_epg, logo_urls, logo_items, on_complete
    ):

        # Resize columns AFTER re-enabling updates to avoid Qt timer conflicts
        for i in range(len(headers)):
            if i != 2:  # Don't auto-resize the progress column
                try:
                    self.content_list.resizeColumnToContents(i)
                except Exception as e:
                    logger.error(f"Error resizing column {i}: {e}", exc_info=True)

        if use_epg:
            self.refresh_on_air_timer.start(30000)
        if self.top_bar.search_text() or self.sidebar.favorites_btn.isChecked():
            self.filter_content(self.top_bar.search_text())

        # Select an item in the list (first or a previously selected)
        if select_first and self.content_list.currentItem() is None:
            if select_first == True:
                first_item = self.content_list.topLevelItem(0)
                if first_item is not None:
                    self.content_list.setCurrentItem(first_item)
            else:
                previous_selected_id = select_first
                previous_selected = self.content_list.findItems(
                    previous_selected_id, Qt.MatchExactly, 0
                )
                if previous_selected:
                    self.content_list.setCurrentItem(previous_selected[0])
                    self.content_list.scrollToItem(previous_selected[0], QTreeWidget.PositionAtTop)

        # Load channel logos if needed
        if logo_urls:
            self._start_logo_loading(logo_urls, logo_items)
        if on_complete is not None:
            on_complete()

    def update_channel_logos(self, current, total, data):
        if self.sender() is not self.image_loader or not data:
            return
        rank = data.get("rank")
        if not isinstance(rank, int) or not 0 <= rank < len(self._logo_items):
            return
        # Ranks refer to the request order, not the tree's current sort order.
        item = self._logo_items[rank]
        cache_path = data.get("cache_path")
        if cache_path:
            pixmap = QPixmap(cache_path)
            if not pixmap.isNull():
                item.setIcon(self._logo_column, QIcon(pixmap))

    def update_poster(self, current, total, data):
        if self.sender() is not getattr(self, "_poster_loader", None) or not data:
            return
        cache_path = data.get("cache_path")
        if cache_path:
            pixmap = QPixmap(cache_path)
            if pixmap and not pixmap.isNull():
                scaled_pixmap = pixmap.scaled(200, 300, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                buffer = QBuffer()
                buffer.open(QBuffer.ReadWrite)
                scaled_pixmap.save(buffer, "PNG")
                buffer.close()
                base64_data = base64.b64encode(cast(Buffer, buffer.data())).decode("utf-8")
                img_tag = f'<img src="data:image/png;base64,{base64_data}" alt="Poster Image" style="float:right; margin: 0 0 10px 10px;">'
                self.content_info_text.setText(img_tag + self.content_info_text.text())

    def filter_content(self, text=""):
        # Cross-provider search mode
        if getattr(self, "_all_providers_mode", False):
            self._fusion_search(text)
            return

        show_favorites = self.sidebar.favorites_btn.isChecked()
        search_text = text.lower() if isinstance(text, str) else ""
        favorites = set(self.config_manager.favorites) if show_favorites else set()

        # When favorites is active at category level, switch to flat favorites view
        if show_favorites and self.current_list_content == "category":
            self._show_favorites_flat()
            return

        # retrieve items type first
        item_type = None
        if self.content_list.topLevelItemCount() > 0:
            item = self.content_list.topLevelItem(0)
            item_type = self.get_item_type(item)

        with self._batch_content_updates():
            for i in range(self.content_list.topLevelItemCount()):
                item = self.content_list.topLevelItem(i)
                item_name = self.get_item_name(item, item_type)
                matches_search = search_text in item_name.lower()

                # Optionally include descriptions, group names, and On Air EPG.
                if (
                    not matches_search
                    and hasattr(self, "app_menu")
                    and self.app_menu.search_descriptions_action.isChecked()
                    and item_type in ["channel", "movie", "serie", "m3ucontent"]
                ):
                    try:
                        data = item.data(0, Qt.UserRole) or {}
                        content = data.get("data", {}) if isinstance(data, dict) else {}
                        description = content.get("description") or content.get("plot") or ""
                        group = content.get("group", "")
                        if (
                            isinstance(description, str) and search_text in description.lower()
                        ) or (isinstance(group, str) and search_text in group.lower()):
                            matches_search = True
                        if (
                            not matches_search
                            and item_type in ["channel", "m3ucontent"]
                            and self.config_manager.channel_epg
                            and self.can_show_epg(item_type)
                        ):
                            epg_text = item.data(3, Qt.UserRole) or ""
                            if isinstance(epg_text, str) and search_text in epg_text.lower():
                                matches_search = True
                    except Exception:
                        # Ignore metadata with an unexpected structure.
                        pass

                if item_type == "category" and search_text:
                    matching_items = self._get_matching_items_in_category(item, search_text)
                    if matching_items:
                        matches_search = True
                        self._populate_category_dropdown(item, matching_items)
                        item.setExpanded(True)
                    else:
                        item.takeChildren()
                        if not matches_search:
                            item.setExpanded(False)
                elif item_type == "category":
                    item.takeChildren()
                    item.setExpanded(False)

                if item_type in ["category", "channel", "movie", "serie", "m3ucontent"]:
                    item.setHidden(
                        not matches_search or (show_favorites and item_name not in favorites)
                    )
                else:
                    item.setHidden(not matches_search)

    def _fusion_search(self, text):
        """Search across all providers' cached content."""
        self.cancel_content_render()
        self.stop_image_loading()
        self.clear_content_info_panel()
        self.content_list.clear()

        if not text or len(text) < 3:
            self.content_list.setColumnCount(1)
            self.content_list.setHeaderLabels(["Type to search across all providers..."])
            return

        search_text = text.lower()
        self.content_list.setHeaderLabels([f'Search results for "{text}"'])
        self.content_list.setSortingEnabled(False)
        self.content_list.setColumnCount(1)

        total_results = 0
        type_to_item_type = {"itv": "channel", "vod": "movie", "series": "serie"}
        source_cache = self._all_provider_cache_snapshot or []

        for provider_name, cache in source_cache:
            matches = []
            for content_type in ["itv", "vod", "series"]:
                content_data = cache.get(content_type, {})
                if not isinstance(content_data, dict):
                    continue
                items = content_data.get("contents", content_data)
                if isinstance(items, dict):
                    # Could be category-based
                    for cat_name, cat_items in items.items():
                        if isinstance(cat_items, list):
                            for item in cat_items:
                                name = item.get("name", item.get("title", ""))
                                if isinstance(name, str) and search_text in name.lower():
                                    matches.append((content_type, item))
                elif isinstance(items, list):
                    for item in items:
                        name = item.get("name", item.get("title", ""))
                        if isinstance(name, str) and search_text in name.lower():
                            matches.append((content_type, item))

            if matches:
                # Create provider group header styled as orange bar
                provider_header = CategoryTreeWidgetItem(self.content_list)
                provider_header.setText(0, f"  {provider_name} ({len(matches)} results)")
                provider_header.setExpanded(True)
                font = provider_header.font(0)
                font.setBold(True)
                provider_header.setFont(0, font)
                provider_header.setBackground(0, QColor(201, 107, 67, 180))
                provider_header.setForeground(0, QColor(255, 255, 255))

                for content_type, item_data in matches[:50]:  # Limit per provider
                    child = QTreeWidgetItem(provider_header)
                    name = item_data.get("name", item_data.get("title", "Unknown"))
                    type_prefix = {
                        "itv": "[CH]",
                        "vod": "[MOV]",
                        "series": "[SER]",
                    }.get(content_type, "")
                    child.setText(0, f"{type_prefix} {name}")
                    child.setData(
                        0,
                        Qt.UserRole,
                        {
                            "data": item_data,
                            "type": type_to_item_type.get(content_type, "channel"),
                            "provider": provider_name,
                            "source_content_type": content_type,
                            "cross_provider_result": True,
                        },
                    )
                    total_results += 1

        if total_results == 0:
            self.content_list.setHeaderLabels([f'No results for "{text}"'])

    def _get_matching_items_in_category(self, category_item, search_text):
        """Get items in category that match the search text."""
        try:
            data = category_item.data(0, Qt.UserRole)
            if not data or "data" not in data:
                return []

            category_data = data["data"]
            category_id = category_data.get("id", "*")

            # Get provider content
            content_data = self.provider_manager.current_provider_content.get(self.content_type, {})

            # Check if we have categorized structure
            if not isinstance(content_data, dict):
                return []

            # Get all items in this category
            if category_id == "*":
                # "All" category - check all contents
                items = content_data.get("contents", [])
            else:
                # Specific category - get items by sorted_channels index
                sorted_channels = content_data.get("sorted_channels", {})
                indices = sorted_channels.get(category_id, [])
                contents = content_data.get("contents", [])
                items = [contents[i] for i in indices if i < len(contents)]

            # Find matching items
            search_lower = search_text.lower()
            matching = []
            include_desc = (
                hasattr(self, "app_menu") and self.app_menu.search_descriptions_action.isChecked()
            )
            for item in items:
                # Check name
                name = item.get("name", "")
                matches_name = search_lower in name.lower()

                # Also check description for movies/series when enabled
                description = item.get("description") or item.get("plot") or ""
                matches_desc = include_desc and search_lower in description.lower()

                # Optionally check current EPG program title when in live channels view
                matches_epg = False
                if (
                    include_desc
                    and self.content_type == "itv"
                    and self.config_manager.channel_epg
                    and self.can_show_epg("channel")
                ):
                    try:
                        epg_list = self.epg_manager.get_programs_for_channel(item, None, 1) or []
                        if epg_list:
                            epg_item = epg_list[0]
                            if "title" in epg_item:  # XMLTV style
                                title_val = epg_item.get("title")
                                epg_text = ""
                                if isinstance(title_val, dict):
                                    epg_text = title_val.get("__text") or ""
                                elif isinstance(title_val, list) and title_val:
                                    first = title_val[0]
                                    if isinstance(first, dict):
                                        epg_text = first.get("__text") or ""
                                else:
                                    epg_text = str(title_val or "")
                            else:
                                epg_text = str(epg_item.get("name") or "")
                            matches_epg = search_lower in epg_text.lower()
                    except Exception:
                        matches_epg = False

                if matches_name or matches_desc or matches_epg:
                    matching.append(item)

            return matching

        except Exception as e:
            # If anything goes wrong, return empty list
            return []

    def _populate_category_dropdown(self, category_item, items):
        """Populate category tree item with matching content as children."""
        # Clear existing children
        category_item.takeChildren()

        # Limit number of items shown in dropdown to avoid performance issues
        max_items = 50
        items_to_show = items[:max_items]
        has_more = len(items) > max_items

        # Add matching items as children
        for item_data in items_to_show:
            child_item = QTreeWidgetItem(category_item)
            name = item_data.get("name", "Unknown")

            # Add channel number if available
            number = item_data.get("number", "")
            if number:
                child_item.setText(0, f"{number}. {name}")
            else:
                child_item.setText(0, name)

            # Store item data
            item_type = "channel"
            if self.content_type == "vod":
                item_type = "movie"
            elif self.content_type == "series":
                item_type = "serie"

            child_item.setData(0, Qt.UserRole, {"type": item_type, "data": item_data})

        # Add "... and X more" item if truncated
        if has_more:
            more_item = QTreeWidgetItem(category_item)
            more_item.setText(
                0, f"... and {len(items) - max_items} more (click category to see all)"
            )
            more_item.setDisabled(True)
            # Gray out the text
            font = more_item.font(0)
            font.setItalic(True)
            more_item.setFont(0, font)

    def show_content_context_menu(self, position):
        """Show context menu on right-click in content list."""
        item = self.content_list.itemAt(position)
        if not item:
            return

        item_data = item.data(0, Qt.UserRole)
        if not item_data:
            return

        menu = QMenu(self)

        # Favorite/Unfavorite action
        item_type = self.get_item_type(item)
        item_name = self.get_item_name(item, item_type)
        is_fav = self.check_if_favorite(item_name)
        fav_label = "\u2606 Remove from Favorites" if is_fav else "\u2605 Add to Favorites"
        fav_action = menu.addAction(fav_label)
        fav_action.triggered.connect(self.toggle_favorite)

        # Copy URL action - only for playable content (not folders)
        content_type = item_data.get("type", "")
        if content_type not in ["category", "series"]:
            url = self._get_stream_url_for_item(item_data)
            if url:
                menu.addSeparator()
                copy_url_action = menu.addAction("Copy URL to Clipboard")
                copy_url_action.triggered.connect(
                    lambda checked=False, u=url: self._copy_url_to_clipboard(u)
                )

        menu.exec_(self.content_list.viewport().mapToGlobal(position))

    def _get_stream_url_for_item(self, item_data):
        """Get the stream URL for an item without playing it."""
        # item_data is {"type": "...", "data": actual_item}
        actual_data = item_data.get("data", {})
        item_type = item_data.get("type", "")

        if self.provider_manager.current_provider.get("type") == "STB":
            # STB: need to create link via API
            is_episode = item_type == "episode"
            return self.create_link(actual_data, is_episode=is_episode)
        else:
            # M3U: cmd is the direct URL
            return actual_data.get("cmd") or actual_data.get("url")

    def _copy_url_to_clipboard(self, url):
        """Copy URL to system clipboard."""
        clipboard = QApplication.clipboard()
        clipboard.setText(url)
        logger.info(f"URL copied to clipboard: {url}")

    # --- Favorites ---

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
            self.filter_content(self.top_bar.search_text())

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

    # --- Logo / image loading ---

    def _retire_image_loader(self, loader):
        if loader is None:
            return
        if loader is self.image_loader:
            self.image_loader = None
            self._logo_items = []
        if loader is getattr(self, "_poster_loader", None):
            self._poster_loader = None
        if not hasattr(self, "_retired_image_loaders"):
            self._retired_image_loaders = []
        if loader not in self._retired_image_loaders:
            self._retired_image_loaders.append(loader)
            loader.cancel()

    def stop_image_loading(self):
        """Detach results immediately; retain each thread until its finished signal."""
        self._retire_image_loader(self.image_loader)
        self.stop_poster_loading()

    def stop_poster_loading(self):
        self._retire_image_loader(getattr(self, "_poster_loader", None))

    def image_loader_finished(self):
        loader = self.sender()
        if loader is None:
            return
        if loader is self.image_loader:
            self.image_loader = None
            self._logo_items = []
        elif loader is getattr(self, "_poster_loader", None):
            self._poster_loader = None
        elif loader in getattr(self, "_retired_image_loaders", ()):
            self._retired_image_loaders.remove(loader)
        else:
            return
        # Image downloads do not own navigation, selection, or content progress UI.
        loader.deleteLater()

    def _start_logo_loading(self, logo_urls, logo_items, refresh_cache=False):
        self._retire_image_loader(self.image_loader)
        if not any(logo_urls):
            return
        self._logo_items = logo_items
        self._logo_column = self.get_logo_column(self.current_list_content)
        self.image_loader = ImageLoader(
            logo_urls,
            self.image_manager,
            iconified=True,
            verify_ssl=self.config_manager.ssl_verify,
            refresh_cache=refresh_cache,
        )
        self.image_loader.progress_updated.connect(self.update_channel_logos, Qt.QueuedConnection)
        self.image_loader.finished.connect(self.image_loader_finished, Qt.QueuedConnection)
        self.image_loader.start()

    def _start_poster_loading(self, poster_url):
        self.stop_poster_loading()
        self._poster_loader = ImageLoader(
            [poster_url],
            self.image_manager,
            iconified=False,
            verify_ssl=self.config_manager.ssl_verify,
        )
        self._poster_loader.progress_updated.connect(self.update_poster, Qt.QueuedConnection)
        self._poster_loader.finished.connect(self.image_loader_finished, Qt.QueuedConnection)
        self._poster_loader.start()

    def rescan_logos(self):
        if self.current_list_content not in ("channel", "m3ucontent"):
            return
        self._retire_image_loader(self.image_loader)
        logo_urls = []
        logo_items = []
        for i in range(self.content_list.topLevelItemCount()):
            item = self.content_list.topLevelItem(i)
            url_logo = item.data(0, Qt.UserRole)["data"].get("logo", "")
            logo_urls.append(url_logo)
            logo_items.append(item)
        self._start_logo_loading(logo_urls, logo_items, refresh_cache=True)

    def refresh_content_list_size(self):
        font_size = 12
        icon_size = font_size + 4
        self.content_list.setIconSize(QSize(icon_size, icon_size))
        self.content_list.setStyleSheet(
            f"""
        QTreeWidget {{ border: none; font-size: {font_size}px; }}
        QTreeWidget::item {{ padding: 6px 8px; }}
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

    # --- Info panel ---

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
        # Show full EPG list for the channel (no windowing)
        epg_data = self.epg_manager.get_programs_for_channel(item_data, max_programs=0)
        if epg_data:
            # Fill the program list and try to select the currently playing entry
            now_index = None
            try:
                import tzlocal as _tz

                _local_now = datetime.now(_tz.get_localzone())
            except Exception:
                _local_now = datetime.now()
            for idx, epg_item in enumerate(epg_data):
                # Detect structure and format row text accordingly
                if "@start" in epg_item and "@stop" in epg_item:
                    try:
                        local_tz = tzlocal.get_localzone()
                        start_raw = datetime.strptime(epg_item.get("@start"), "%Y%m%d%H%M%S %z")
                        stop_raw = datetime.strptime(epg_item.get("@stop"), "%Y%m%d%H%M%S %z")
                        start_dt = start_raw.astimezone(local_tz)
                        stop_dt = stop_raw.astimezone(local_tz)
                        start_txt = start_dt.strftime("%H:%M")
                        stop_txt = stop_dt.strftime("%H:%M")
                        if (
                            now_index is None
                            and start_raw <= _local_now.astimezone(start_raw.tzinfo) < stop_raw
                        ):
                            now_index = idx
                    except Exception:
                        start_txt, stop_txt = "", ""
                    title_val = epg_item.get("title")
                    title_txt = ""
                    if isinstance(title_val, dict):
                        title_txt = title_val.get("__text") or ""
                    elif isinstance(title_val, list) and title_val:
                        first = title_val[0]
                        if isinstance(first, dict):
                            title_txt = first.get("__text") or ""
                    epg_text = f"<b>{start_txt}-{stop_txt}</b>&nbsp;&nbsp;{title_txt}"
                else:
                    # STB style: t_time fields may exist; fallback to parsed time/time_to
                    start_txt = epg_item.get("t_time")
                    stop_txt = epg_item.get("t_time_to")
                    if (
                        not (start_txt and stop_txt)
                        and "time" in epg_item
                        and "time_to" in epg_item
                    ):
                        try:
                            start_dt = datetime.strptime(epg_item.get("time"), "%Y-%m-%d %H:%M:%S")
                            stop_dt = datetime.strptime(
                                epg_item.get("time_to"), "%Y-%m-%d %H:%M:%S"
                            )
                            start_txt = start_dt.strftime("%H:%M")
                            stop_txt = stop_dt.strftime("%H:%M")
                            if now_index is None and start_dt <= datetime.now() < stop_dt:
                                now_index = idx
                        except Exception:
                            start_txt, stop_txt = "", ""
                    epg_text = f"<b>{start_txt or ''}-{stop_txt or ''}</b>&nbsp;&nbsp;{epg_item.get('name', '')}"
                item = QListWidgetItem(f"{epg_text}")
                item.setData(Qt.UserRole, epg_item)
                self.program_list.addItem(item)
            # Visually highlight the currently airing program
            if now_index is not None:
                try:
                    now_item = self.program_list.item(now_index)
                    if now_item is not None:
                        # Prefix with a play arrow for visibility
                        try:
                            current_text = now_item.text()
                            if not current_text.lstrip().startswith("\u25b6 Now"):
                                now_item.setText(f"\u25b6 Now  {current_text}")
                        except Exception:
                            pass
                        # Light blue background tint
                        try:
                            now_item.setBackground(QColor(201, 107, 67, 45))
                        except Exception:
                            pass
                except Exception:
                    pass
                self.program_list.setCurrentRow(now_index)
            else:
                self.program_list.setCurrentRow(0)
        else:
            item = QListWidgetItem("Program not available")
            self.program_list.addItem(item)
            xmltv_id = item_data.get("xmltv_id", "")
            ch_name = item_data.get("name", "")
            if xmltv_id:
                self.content_info_text.setText(f'No EPG found for channel id "{xmltv_id}"')
            elif ch_name:
                self.content_info_text.setText(f'No EPG found for channel "{ch_name}"')
            else:
                self.content_info_text.setText("Channel without id")

    def update_channel_program(self):
        self.stop_poster_loading()
        selected_items = self.program_list.selectedItems()
        if not selected_items:
            self.content_info_text.setText("No program selected")
            return
        selected_item = selected_items[0]
        item_data = selected_item.data(Qt.UserRole)
        if item_data:
            # Decide formatting by epg item structure rather than global source
            if "@start" not in item_data:
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
                    self._start_poster_loading(icon_url)
        else:
            self.content_info_text.setText("No data available")

    def populate_movie_tvshow_content_info(self, item_data):
        self.stop_poster_loading()
        provider_type = self.provider_manager.current_provider.get("type", "").upper()

        # Common labels; not all keys will be present for all providers
        stb_labels = {
            "name": "Title",
            "rating_imdb": "Rating",
            "year": "Year",
            "genres_str": "Genre",
            "length": "Length",
            "director": "Director",
            "actors": "Actors",
            "description": "Summary",
        }
        xtream_labels = {
            "name": "Title",
            "rating": "Rating",
            "year": "Year",
            "genre": "Genre",
            "director": "Director",
            "actors": "Actors",
            "description": "Summary",
            "plot": "Summary",
        }

        labels = stb_labels if provider_type == "STB" else xtream_labels

        info = ""
        for key, label in labels.items():
            if key in item_data:
                value = item_data.get(key)
                if not value:
                    continue
                # Normalize dict/list values from some APIs
                if isinstance(value, dict) and "__text" in value:
                    value = value.get("__text")
                if isinstance(value, list):
                    value = ", ".join(str(v) for v in value if v)
                if isinstance(value, str) and value.strip().lower() in {
                    "na",
                    "n/a",
                    "none",
                    "null",
                }:
                    continue
                info += f"<b>{label}:</b> {value}<br>"

        self.content_info_text.setText(info if info else "No data available")

        # Poster/cover image
        poster_url = ""
        if provider_type == "STB":
            poster_url = item_data.get("screenshot_uri", "")
        else:  # XTREAM and others
            poster_url = item_data.get("logo") or item_data.get("cover") or ""

        if poster_url:
            self._start_poster_loading(poster_url)
