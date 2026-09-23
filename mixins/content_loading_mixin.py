"""Content loading, worker callbacks, navigation, and progress methods."""

import logging
import re
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, cast

from PySide6.QtCore import Qt, QThread
from PySide6.QtWidgets import QMessageBox, QProgressBar, QPushButton, QTreeWidget, QWidget
from urlobject import URLObject

from content_loader import ContentLoader
from image_loader import ImageLoader
from services.thread_cleanup import ThreadCleanup
from workers import (
    M3ULoaderWorker,
    STBCategoriesWorker,
    XtreamLoaderWorker,
    XtreamSeriesInfoWorker,
    find_roman_token,
)

if TYPE_CHECKING:
    from config_manager import ConfigManager
    from image_manager import ImageManager
    from provider_manager import ProviderManager
    from widgets.top_bar import TopBar

logger = logging.getLogger(__name__)


class ContentLoadingMixin:
    """Mixin providing content loading, worker callbacks, and progress functionality."""

    # Provided by ChannelList at runtime
    provider_manager: "ProviderManager"
    config_manager: "ConfigManager"
    image_manager: "ImageManager"
    content_type: str
    content_list: QTreeWidget
    progress_bar: QProgressBar
    cancel_button: QPushButton
    top_bar: "TopBar"
    image_loader: Optional[ImageLoader]
    content_loader: Optional[ContentLoader]
    current_category: Optional[Dict[str, Any]]
    current_series: Optional[Dict[str, Any]]
    current_season: Optional[Dict[str, Any]]
    _bg_jobs: List[Any]
    _current_seasons_list: List[Dict[str, Any]]
    _pending_link_ctx: Optional[Dict[str, Any]]
    _pending_series_select_first: bool
    link: Optional[str]

    def cancel_content_loading(self):
        """Detach obsolete results without waiting for network threads on the GUI."""
        self.cancel_content_render()
        worker = getattr(self, "_active_content_worker", None)
        self._active_content_worker = None
        if isinstance(worker, QThread):
            worker.requestInterruption()
        self.content_loader = None
        self.unlock_ui_after_loading()

    def _start_content_worker(self, worker, callback, select_first=None):
        self.cancel_content_loading()
        self.lock_ui_before_loading()
        self._active_content_worker = worker
        self._content_callback = callback
        self._content_select_first = select_first
        thread: QThread
        if isinstance(worker, ContentLoader):
            thread = worker
            self.content_loader = worker
            worker.content_loaded.connect(self._content_worker_result, Qt.QueuedConnection)
            worker.progress_updated.connect(self._content_worker_progress, Qt.QueuedConnection)
        else:
            thread = QThread()
            worker.moveToThread(thread)
            thread.started.connect(worker.run)
            worker.finished.connect(self._content_worker_result, Qt.QueuedConnection)
            worker.finished.connect(thread.quit)
            worker.error.connect(thread.quit)
        worker.error.connect(self._content_worker_error, Qt.QueuedConnection)
        ThreadCleanup(thread, worker).finished.connect(self._content_job_finished)
        self._bg_jobs.append((thread, worker))
        thread.start()

    def _content_worker_result(self, payload):
        if self.sender() is not getattr(self, "_active_content_worker", None):
            return
        self._active_content_worker = None
        self.content_loader = None
        callback = self._content_callback
        select_first = self._content_select_first
        self.unlock_ui_after_loading()
        if select_first is None:
            callback(payload)
        else:
            callback(payload, select_first)

    def _content_worker_error(self, message):
        if self.sender() is not getattr(self, "_active_content_worker", None):
            return
        self.cancel_content_loading()
        logger.warning("Content loading failed: %s", message)
        self.statusBar().showMessage("Content refresh failed; previous content retained.", 10000)

    def _content_worker_progress(self, current, total):
        if self.sender() is getattr(self, "_active_content_worker", None):
            self.update_progress(current, total)

    def _content_job_finished(self, thread):
        for job in self._bg_jobs:
            if job[0] is thread:
                self._bg_jobs.remove(job)
                if job[1] is getattr(self, "_active_content_worker", None):
                    self.cancel_content_loading()
                break

    def _display_loaded_root(self):
        """Refresh an open category in place, or display the refreshed root."""
        content = self.provider_manager.current_provider_content[self.content_type]
        if not isinstance(content, dict):
            self.display_content(content)
            return
        categories = content.get("categories", [])
        if self.current_category is not None:
            category_id = str(self.current_category.get("id", "*"))
            category = next(
                (cat for cat in categories if str(cat.get("id", "*")) == category_id), None
            )
            if category is not None:
                self.current_category = category
                self.load_content_in_category(category)
                return
        self.current_category = None
        self.current_series = None
        self.current_season = None
        self.navigation_stack.clear()
        self.forward_stack.clear()
        self.display_categories(categories)
        self.filter_content(self.top_bar.search_text())

    def load_content(self):
        self.cancel_content_loading()
        selected_provider = self.provider_manager.current_provider
        config_type = selected_provider.get("type", "")
        content = self.provider_manager.current_provider_content.setdefault(self.content_type, {})
        if self.provider_manager.is_content_stale(self.content_type):
            self.update_content()
            return
        if content:
            # Check if content is a dict with categories (categorized format)
            if isinstance(content, dict) and "categories" in content:
                # Display categories for STB, XTREAM, and categorized M3U
                self.display_categories(content.get("categories", []))
            elif config_type in ("STB", "XTREAM"):
                # Old cached format for STB/XTREAM - force update
                self.update_content()
            else:
                # For flat M3U lists and other types, display content directly
                self.display_content(content)
        else:
            self.update_content()

    def update_content(self):
        selected_provider = self.provider_manager.current_provider
        config_type = selected_provider.get("type", "")
        if config_type == "M3UPLAYLIST":
            self.load_m3u_playlist(selected_provider["url"])
        elif config_type == "XTREAM":
            # Use Xtream Player API v2 with categories
            self.load_xtream_content(
                base_url=selected_provider.get("url", ""),
                username=selected_provider.get("username", ""),
                password=selected_provider.get("password", ""),
                content_type=self.content_type,
            )
        elif config_type == "STB":
            self.load_stb_categories(selected_provider["url"], self.provider_manager.headers)
        elif config_type == "M3USTREAM":
            self.load_stream(selected_provider["url"])

    def load_m3u_playlist(self, url):
        provider = self.provider_manager.current_provider
        worker = M3ULoaderWorker(
            url,
            verify_ssl=provider.get("ssl_verify", self.config_manager.ssl_verify),
            prefer_https=provider.get("prefer_https", self.config_manager.prefer_https),
        )
        self._start_content_worker(worker, self._on_catalog_loaded)

    def load_stream(self, url):
        item = {"id": 1, "name": "Stream", "cmd": url}
        self.display_content([item])
        # Update the content in the config
        self.provider_manager.current_provider_content[self.content_type] = [item]
        self.provider_manager.mark_content_fresh(self.content_type)
        self.save_provider()

    def load_xtream_content(self, base_url: str, username: str, password: str, content_type: str):
        """Load Xtream content (Live/VOD/Series) using Player API v2 with categories."""
        provider = self.provider_manager.current_provider
        prefer_https = provider.get("prefer_https", self.config_manager.prefer_https)
        verify_ssl = provider.get("ssl_verify", self.config_manager.ssl_verify)
        worker = XtreamLoaderWorker(
            base_url,
            username,
            password,
            content_type,
            verify_ssl=verify_ssl,
            prefer_https=prefer_https,
        )
        self._start_content_worker(worker, self._on_catalog_loaded)

    def load_stb_categories(self, url: str, headers: Optional[dict] = None):
        if headers is None:
            headers = self.provider_manager.headers
        provider = self.provider_manager.current_provider
        prefer_https = provider.get("prefer_https", self.config_manager.prefer_https)
        verify_ssl = provider.get("ssl_verify", self.config_manager.ssl_verify)
        worker = STBCategoriesWorker(
            url,
            headers,
            self.content_type,
            verify_ssl=verify_ssl,
            prefer_https=prefer_https,
        )
        self._start_content_worker(worker, self._on_catalog_loaded)

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
        self.cancel_content_loading()
        if category is None:
            self.load_content()
            return
        content_data = self.provider_manager.current_provider_content.setdefault(
            self.content_type, {}
        )
        category_id = str(category.get("id", "*"))
        selected_provider = self.provider_manager.current_provider
        config_type = selected_provider.get("type", "")

        # For XTREAM and STB providers with sorted_channels structure
        if "sorted_channels" in content_data:
            contents = content_data.get("contents", [])
            if category_id == "*":
                items = contents if isinstance(contents, list) else []
            else:
                sorted_map = content_data.get("sorted_channels", {})
                indices = sorted_map.get(category_id, []) if isinstance(sorted_map, dict) else []
                # Guard missing/invalid state
                items = [contents[i] for i in indices] if isinstance(contents, list) else []

            # Display with appropriate content type
            if self.content_type == "itv":
                self.display_content(items, content="channel", select_first=select_first)
            elif self.content_type == "series":
                self.display_content(items, content="serie", select_first=select_first)
            elif self.content_type == "vod":
                self.display_content(items, content="movie", select_first=select_first)
        else:
            # For STB providers with per-category fetching
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
                # Fetch content for the category (STB only)
                if config_type == "STB":
                    self.fetch_content_in_category(category_id, select_first=select_first)

    def fetch_content_in_category(self, category_id, select_first=True):
        # Ask confirmation if the user wants to load all content
        if category_id == "*":
            reply = QMessageBox.question(
                cast(QWidget, self),
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
        scheme = url.scheme
        if (
            selected_provider.get("prefer_https", self.config_manager.prefer_https)
            and scheme == "http"
        ):
            scheme = "https"
        url = f"{scheme}://{url.netloc}/server/load.php"

        verify_ssl = selected_provider.get("ssl_verify", self.config_manager.ssl_verify)
        loader = ContentLoader(
            url,
            headers,
            self.content_type,
            category_id=category_id,
            verify_ssl=verify_ssl,
        )
        self._start_content_worker(loader, self.update_content_list, select_first)
        self.cancel_button.setText("Cancel loading content in category")

    def load_series_seasons(self, series_item, select_first=True):
        self.cancel_content_loading()
        selected_provider = self.provider_manager.current_provider
        config_type = selected_provider.get("type", "")

        self.current_series = series_item  # Store current series
        content_data = self.provider_manager.current_provider_content.get("series", {})
        cached = content_data.get("seasons", {}).get(str(series_item["id"]))
        if cached is not None:
            self._current_seasons_list = cached
            self.display_content(cached, content="season", select_first=select_first)
            return

        if config_type == "XTREAM":
            # Use Xtream API v2 to get series info
            self.load_xtream_series_info(series_item, select_first)
        elif config_type == "STB":
            # Use STB API
            headers = self.provider_manager.headers
            url = selected_provider.get("url", "")
            url = URLObject(url)
            scheme = url.scheme
            if (
                selected_provider.get("prefer_https", self.config_manager.prefer_https)
                and scheme == "http"
            ):
                scheme = "https"
            url = f"{scheme}://{url.netloc}/server/load.php"

            verify_ssl = selected_provider.get("ssl_verify", self.config_manager.ssl_verify)
            loader = ContentLoader(
                url=url,
                headers=headers,
                content_type="series",
                category_id=series_item.get("category_id") or series_item.get("tv_genre_id", ""),
                movie_id=series_item["id"],  # series ID
                season_id=0,
                action="get_ordered_list",
                sortby="name",
                verify_ssl=verify_ssl,
            )

            self._start_content_worker(loader, self.update_seasons_list, select_first)
            self.cancel_button.setText("Cancel loading seasons")

    def load_xtream_series_info(self, series_item, select_first=True):
        """Load Xtream series info (seasons and episodes) using Player API v2."""
        selected_provider = self.provider_manager.current_provider
        content_data = self.provider_manager.current_provider_content.get(self.content_type, {})
        resolved_base = content_data.get("resolved_base", "")

        prefer_https = selected_provider.get("prefer_https", self.config_manager.prefer_https)
        verify_ssl = selected_provider.get("ssl_verify", self.config_manager.ssl_verify)
        worker = XtreamSeriesInfoWorker(
            base_url=selected_provider.get("url", ""),
            username=selected_provider.get("username", ""),
            password=selected_provider.get("password", ""),
            series_id=str(series_item["id"]),
            resolved_base=resolved_base,
            verify_ssl=verify_ssl,
            prefer_https=prefer_https,
        )
        self._pending_series_select_first = select_first
        self._start_content_worker(worker, self._on_xtream_series_finished)

    def load_season_episodes(self, season_item, select_first=True):
        self.cancel_content_loading()
        selected_provider = self.provider_manager.current_provider
        config_type = selected_provider.get("type", "")

        self.current_season = season_item  # Store current season
        if config_type == "STB" and self.current_series:
            content_data = self.provider_manager.current_provider_content.get("series", {})
            cached = (
                content_data.get("episodes", {})
                .get(str(self.current_series["id"]), {})
                .get(str(season_item["id"]))
            )
            if cached is not None:
                self.display_content(cached, content="episode", select_first=select_first)
                return

        if config_type == "XTREAM":
            # Episodes are already loaded with the season data
            episodes = season_item.get("episodes", [])
            if episodes:
                # Format episodes for Xtream
                content_data = self.provider_manager.current_provider_content.get(
                    self.content_type, {}
                )
                stream_base = content_data.get("stream_base", "")
                stream_ext = content_data.get("stream_ext", "ts")
                username = selected_provider.get("username", "")
                password = selected_provider.get("password", "")

                formatted_episodes = []
                for idx, ep in enumerate(episodes, start=1):
                    episode_id = ep.get("id")
                    # Use container_extension from episode data, fallback to stream_ext
                    container_ext = ep.get("container_extension") or stream_ext
                    cmd = f"{stream_base}/series/{username}/{password}/{episode_id}.{container_ext}"
                    # Robust episode number extraction
                    number_str = None
                    for key in ("episode_num", "episode", "num"):
                        val = ep.get(key)
                        if val is not None:
                            s = str(val)
                            if s.isdigit():
                                number_str = str(int(s))
                                break
                    if not number_str:
                        title_text = ep.get("title") or ep.get("name") or ""
                        m = re.search(r"\d+", str(title_text))
                        if m:
                            number_str = str(int(m.group(0)))
                    if not number_str:
                        # Try Roman numerals in title/name with conservative rules
                        val = find_roman_token(title_text)
                        if val is not None:
                            number_str = str(val)
                    if not number_str and episode_id is not None:
                        s = str(episode_id)
                        if s.isdigit():
                            number_str = str(int(s))
                    if not number_str:
                        number_str = str(idx)

                    formatted_ep = {
                        "id": episode_id,
                        "number": number_str,
                        "ename": ep.get("title") or f"Episode {number_str}",
                        "name": ep.get("title") or f"Episode {number_str}",
                        "description": ep.get("info") or "",
                        "cmd": cmd,
                        "logo": (
                            ep.get("info", {}).get("movie_image")
                            if isinstance(ep.get("info"), dict)
                            else ""
                        ),
                        "season": ep.get("season"),
                        "episode_num": number_str,
                        "container_extension": container_ext,
                    }
                    formatted_episodes.append(formatted_ep)

                # Display episodes directly for Xtream
                self.display_content(
                    formatted_episodes, content="episode", select_first=select_first
                )
            else:
                logger.info("No episodes found for this season.")
        elif config_type == "STB":
            # Use STB API
            if not self.current_category or not self.current_series:
                logger.warning("Current category/series not set when loading season episodes")
                return

            headers = self.provider_manager.headers
            url = selected_provider.get("url", "")
            url = URLObject(url)
            url = f"{url.scheme}://{url.netloc}/server/load.php"

            verify_ssl = selected_provider.get("ssl_verify", self.config_manager.ssl_verify)
            loader = ContentLoader(
                url=url,
                headers=headers,
                content_type="series",
                category_id=self.current_category["id"],  # Category ID
                movie_id=self.current_series["id"],  # Series ID
                season_id=season_item["id"],  # Season ID
                action="get_ordered_list",
                sortby="added",
                verify_ssl=verify_ssl,
            )
            self._start_content_worker(loader, self.update_episodes_list, select_first)
            self.cancel_button.setText("Cancel loading episodes")

    # --- Worker callbacks ---

    def _on_xtream_series_finished(self, payload):
        content_data = self.provider_manager.current_provider_content.setdefault("series", {})
        content_data["resolved_base"] = payload.get("resolved_base", "")
        self.update_seasons_list(
            {"items": payload.get("seasons", [])}, self._pending_series_select_first
        )

    def _on_catalog_loaded(self, payload):
        self.statusBar().clearMessage()
        self.provider_manager.current_provider_content[self.content_type] = payload
        self.provider_manager.mark_content_fresh(self.content_type)
        self.save_provider()
        self._display_loaded_root()

    def _on_link_created(self, payload):
        try:
            ctx = getattr(self, "_pending_link_ctx", None)
            link = self.sanitize_url(payload.get("link", ""))
            if link:
                self.link = link
                self._play_content(link)
                if ctx:
                    self.save_last_watched(ctx["item_data"], ctx["item_type"], link)
            else:
                logger.warning("Failed to create link.")
        finally:
            self.unlock_ui_after_loading()
            self._pending_link_ctx = None

    def _on_link_error(self, msg: str):
        try:
            logger.warning(f"Error creating link: {msg}")
        finally:
            self.unlock_ui_after_loading()

    # --- UI state for loading ---

    def cancel_loading(self):
        self.cancel_content_loading()
        self.stop_image_loading()

    def lock_ui_before_loading(self):
        self.update_ui_on_loading(loading=True)

    def unlock_ui_after_loading(self):
        self.update_ui_on_loading(loading=False)

    def update_ui_on_loading(self, loading):
        # Navigation stays usable; obsolete worker results are detached on Back.
        self.progress_bar.setVisible(loading)
        self.cancel_button.setVisible(loading)

    def update_content_list(self, data, select_first=True):
        category_id = str(data.get("category_id"))
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
        items = data.get("items", [])

        for i, item in enumerate(items):
            try:
                # Store original name before modification
                original_name = item.get("o_name") or item.get("name", f"Season {i + 1}")
                item["o_name"] = original_name

                # Derive a numeric season index for sorting/display
                number_str = None
                # Prefer explicit id/season_number if numeric
                sid = str(item.get("id", ""))
                if sid.isdigit():
                    number_str = str(int(sid))
                else:
                    s_num = str(item.get("season_number", ""))
                    if s_num.isdigit():
                        number_str = str(int(s_num))
                # Fallback: first integer found in the name (e.g., "Season 1", "1. Sezon", "S02")
                if not number_str:
                    m = re.search(r"\d+", original_name)
                    if m:
                        number_str = str(int(m.group(0)))
                # Fallback: Roman numeral detection (e.g., "Season IV", "Rocky II")
                if not number_str:
                    val = find_roman_token(original_name)
                    if val is not None:
                        number_str = str(val)
                # Final fallback: position index
                if not number_str:
                    number_str = str(i + 1)
                item["number"] = number_str

                # Create combined name
                series_name = self.current_series.get("name", "Unknown Series")
                item["name"] = f"{series_name}.{original_name}"

                # Add "added" field if not present (use air_date or empty)
                if "added" not in item:
                    item["added"] = item.get("air_date", "")

            except Exception as e:
                logger.error(f"Error processing season {i}: {e}", exc_info=True)
                # Set defaults if processing fails
                item["o_name"] = item.get("name", f"Season {i + 1}")
                item["number"] = str(i + 1)
                item["name"] = f"{self.current_series.get('name', 'Unknown')}.Season {i + 1}"
                item["added"] = ""
        self._current_seasons_list = items
        content_data = self.provider_manager.current_provider_content.setdefault("series", {})
        content_data.setdefault("seasons", {})[str(self.current_series["id"])] = items
        self.save_provider()
        self.display_content(items, content="season", select_first=select_first)

    def update_episodes_list(self, data, select_first=True):
        if not self.current_series:
            logger.warning("Current series not set when updating episodes list")
            return
        items = data.get("items")
        selected_season = None
        for item in items:
            if str(item.get("id")) == str(data.get("season_id")):
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
            content_data = self.provider_manager.current_provider_content.setdefault("series", {})
            series_cache = content_data.setdefault("episodes", {}).setdefault(
                str(self.current_series["id"]), {}
            )
            series_cache[str(data["season_id"])] = episode_items
            self.save_provider()
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
