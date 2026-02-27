"""Content loading, worker callbacks, navigation, and progress methods."""

import logging
import re
import time
from typing import Any, Dict, List, Optional

import requests
from PySide6.QtCore import QThread, Qt
from PySide6.QtWidgets import QListWidget, QMessageBox
from urlobject import URLObject

from content_loader import ContentLoader
from services.m3u import parse_m3u
from workers import (
    M3ULoaderWorker,
    STBCategoriesWorker,
    XtreamLoaderWorker,
    XtreamSeriesInfoWorker,
    find_roman_token,
)

logger = logging.getLogger(__name__)


class ContentLoadingMixin:
    """Mixin providing content loading, worker callbacks, and progress functionality."""

    def load_content(self):
        selected_provider = self.provider_manager.current_provider
        config_type = selected_provider.get("type", "")
        content = self.provider_manager.current_provider_content.setdefault(self.content_type, {})
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
        try:
            if url.startswith(("http://", "https://")):
                # Run network download in a worker thread
                self.lock_ui_before_loading()
                thread = QThread()
                provider = self.provider_manager.current_provider
                prefer_https = provider.get("prefer_https", self.config_manager.prefer_https)
                verify_ssl = provider.get("ssl_verify", self.config_manager.ssl_verify)
                worker = M3ULoaderWorker(url, verify_ssl=verify_ssl, prefer_https=prefer_https)
                worker.moveToThread(thread)

                def _cleanup():
                    try:
                        self._bg_jobs.remove((thread, worker))
                    except ValueError:
                        pass

                # Start worker when thread starts
                thread.started.connect(worker.run)
                # Route results to UI thread
                worker.finished.connect(self._on_m3u_loaded, Qt.QueuedConnection)
                worker.error.connect(self._on_m3u_error, Qt.QueuedConnection)
                # Ensure proper lifecycle
                worker.finished.connect(thread.quit)
                worker.error.connect(thread.quit)
                thread.finished.connect(worker.deleteLater)
                thread.finished.connect(thread.deleteLater)
                thread.finished.connect(_cleanup)
                thread.start()
                self._bg_jobs.append((thread, worker))
            else:
                with open(url, "r", encoding="utf-8") as file:
                    content = file.read()
                # Parse with categorization enabled
                parsed_content = parse_m3u(content, categorize=True)

                # Check if we have categories (dict structure)
                if isinstance(parsed_content, dict) and "categories" in parsed_content:
                    # Store categorized content
                    self.provider_manager.current_provider_content[self.content_type] = (
                        parsed_content
                    )
                    self.save_provider()
                    # Display categories
                    self.display_categories(parsed_content.get("categories", []))
                else:
                    # Fallback to flat list
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

    def load_xtream_content(self, base_url: str, username: str, password: str, content_type: str):
        """Load Xtream content (Live/VOD/Series) using Player API v2 with categories."""
        self.lock_ui_before_loading()
        thread = QThread()
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
        worker.moveToThread(thread)

        def _cleanup():
            try:
                self._bg_jobs.remove((thread, worker))
            except ValueError:
                pass

        thread.started.connect(worker.run)
        worker.finished.connect(self._on_xtream_content_loaded, Qt.QueuedConnection)
        worker.error.connect(self._on_xtream_content_error, Qt.QueuedConnection)
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(_cleanup)
        thread.start()
        self._bg_jobs.append((thread, worker))

    def load_stb_categories(self, url: str, headers: Optional[dict] = None):
        if headers is None:
            headers = self.provider_manager.headers
        # Run network calls in a worker thread
        self.lock_ui_before_loading()
        thread = QThread()
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
        worker.moveToThread(thread)

        def _cleanup():
            try:
                self._bg_jobs.remove((thread, worker))
            except ValueError:
                pass

        thread.started.connect(worker.run)
        worker.finished.connect(self._on_stb_categories_loaded, Qt.QueuedConnection)
        worker.error.connect(self._on_stb_categories_error, Qt.QueuedConnection)
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
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
        scheme = url.scheme
        if (
            selected_provider.get("prefer_https", self.config_manager.prefer_https)
            and scheme == "http"
        ):
            scheme = "https"
        url = f"{scheme}://{url.netloc}/server/load.php"

        self.lock_ui_before_loading()
        if self.content_loader and self.content_loader.isRunning():
            self.content_loader.wait()
        verify_ssl = selected_provider.get("ssl_verify", self.config_manager.ssl_verify)
        self.content_loader = ContentLoader(
            url,
            headers,
            self.content_type,
            category_id=category_id,
            verify_ssl=verify_ssl,
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
        config_type = selected_provider.get("type", "")

        self.current_series = series_item  # Store current series

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

            self.lock_ui_before_loading()
            if self.content_loader and self.content_loader.isRunning():
                self.content_loader.wait()
            verify_ssl = selected_provider.get("ssl_verify", self.config_manager.ssl_verify)
            self.content_loader = ContentLoader(
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

            self.content_loader.content_loaded.connect(
                lambda data: self.update_seasons_list(data, select_first)
            )
            self.content_loader.progress_updated.connect(self.update_progress)
            self.content_loader.finished.connect(self.content_loader_finished)
            self.content_loader.start()
            self.cancel_button.setText("Cancel loading seasons")

    def load_xtream_series_info(self, series_item, select_first=True):
        """Load Xtream series info (seasons and episodes) using Player API v2."""
        selected_provider = self.provider_manager.current_provider
        content_data = self.provider_manager.current_provider_content.get(self.content_type, {})
        resolved_base = content_data.get("resolved_base", "")

        # Clean up any running background jobs before starting new one
        for old_thread, old_worker in self._bg_jobs[
            :
        ]:  # Copy list to avoid modification during iteration
            try:
                if old_thread.isRunning():
                    logger.info("Stopping previous worker thread")
                    old_thread.quit()
                    old_thread.wait(1000)  # Wait up to 1 second
                    old_worker.deleteLater()
                    old_thread.deleteLater()
            except RuntimeError:
                # Thread already deleted by Qt
                pass
            try:
                self._bg_jobs.remove((old_thread, old_worker))
            except ValueError:
                # Already removed
                pass

        self.lock_ui_before_loading()
        thread = QThread(self)
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
        worker.moveToThread(thread)

        # Remember selection behavior for UI handler
        self._pending_series_select_first = select_first

        # No nested UI handler; connect directly to a bound method

        def on_error(msg):
            # Route to UI handler
            self._on_xtream_series_error(msg)

        def _cleanup():
            try:
                self._bg_jobs.remove((thread, worker))
            except ValueError:
                pass

        # Start worker when thread starts
        thread.started.connect(worker.run)

        # Route worker results to UI thread and ensure cleanup
        worker.finished.connect(self._on_xtream_series_finished, Qt.QueuedConnection)
        worker.error.connect(on_error, Qt.QueuedConnection)

        # Ensure proper thread/worker lifecycle
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)
        # Delete worker and thread objects in UI thread after finish
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(_cleanup)
        thread.start()
        self._bg_jobs.append((thread, worker))

    def load_season_episodes(self, season_item, select_first=True):
        selected_provider = self.provider_manager.current_provider
        config_type = selected_provider.get("type", "")

        self.current_season = season_item  # Store current season

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

            self.lock_ui_before_loading()
            if self.content_loader and self.content_loader.isRunning():
                self.content_loader.wait()
            verify_ssl = selected_provider.get("ssl_verify", self.config_manager.ssl_verify)
            self.content_loader = ContentLoader(
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
            self.content_loader.content_loaded.connect(
                lambda data: self.update_episodes_list(data, select_first)
            )
            self.content_loader.progress_updated.connect(self.update_progress)
            self.content_loader.finished.connect(self.content_loader_finished)
            self.content_loader.start()
            self.cancel_button.setText("Cancel loading episodes")

    # --- Worker callbacks ---

    def _on_xtream_series_finished(self, payload):
        """UI-thread handler for Xtream series info results."""
        try:
            if payload and isinstance(payload, dict):
                seasons = payload.get("seasons", [])
                if seasons:
                    # Cache seasons for auto-play navigation
                    self._current_seasons_list = seasons

                    # Also cache in provider content for persistence
                    if self.current_series:
                        series_id = str(self.current_series.get("id", ""))
                        content_data = self.provider_manager.current_provider_content.setdefault(
                            "series", {}
                        )
                        seasons_cache = content_data.setdefault("seasons", {})
                        seasons_cache[series_id] = seasons

                        # Store resolved_base for episode URL construction
                        content_data["resolved_base"] = payload.get("resolved_base", "")

                    select_first = getattr(self, "_pending_series_select_first", True)
                    self.update_seasons_list({"items": seasons}, select_first)
            else:
                logger.warning("Invalid payload from series worker")
        except Exception as e:
            logger.error(f"Series finished handler exception: {e}", exc_info=True)
        finally:
            # Ensure UI is unlocked even if cleanup signals race
            self.unlock_ui_after_loading()

    def _on_xtream_series_error(self, msg: str):
        try:
            logger.warning(f"Error loading Xtream series info: {msg}")
        finally:
            self.unlock_ui_after_loading()

    def _on_m3u_loaded(self, payload):
        try:
            content = payload.get("content", "")
            # Parse with categorization enabled
            parsed_content = parse_m3u(content, categorize=True)

            if isinstance(parsed_content, dict) and "categories" in parsed_content:
                self.provider_manager.current_provider_content[self.content_type] = parsed_content
                self.save_provider()
                self.display_categories(parsed_content.get("categories", []))
            else:
                self.display_content(parsed_content)
                self.provider_manager.current_provider_content[self.content_type] = parsed_content
                self.save_provider()
        finally:
            self.unlock_ui_after_loading()

    def _on_m3u_error(self, msg: str):
        try:
            logger.warning(f"Error loading M3U Playlist: {msg}")
        finally:
            self.unlock_ui_after_loading()

    def _on_xtream_content_loaded(self, payload):
        try:
            categories = payload.get("categories", [])
            contents = payload.get("contents", [])
            sorted_channels = payload.get("sorted_channels", {})

            if not categories and not contents:
                logger.info("No content found.")
                return

            provider_content = self.provider_manager.current_provider_content.setdefault(
                self.content_type, {}
            )
            provider_content["categories"] = categories
            provider_content["contents"] = contents
            provider_content["sorted_channels"] = sorted_channels

            # Store stream settings for later use
            provider_content["stream_base"] = payload.get("stream_base", "")
            provider_content["stream_ext"] = payload.get("stream_ext", "")
            provider_content["resolved_base"] = payload.get("resolved_base", "")

            self.save_provider()
            self.display_categories(categories)
        finally:
            self.unlock_ui_after_loading()

    def _on_xtream_content_error(self, msg: str):
        try:
            logger.warning(f"Error loading Xtream content: {msg}")
        finally:
            self.unlock_ui_after_loading()

    def _on_stb_categories_loaded(self, payload):
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
            self.unlock_ui_after_loading()

    def _on_stb_categories_error(self, msg: str):
        try:
            logger.warning(f"Error loading STB categories: {msg}")
        finally:
            self.unlock_ui_after_loading()

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
        self.top_bar.back_button.setEnabled(not loading)
        self.progress_bar.setVisible(loading)
        self.cancel_button.setVisible(loading)
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
        if not items:
            logger.warning("No items in data when updating seasons list")
            return

        for i, item in enumerate(items):
            try:
                # Store original name before modification
                original_name = item.get("name", f"Season {i + 1}")
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
