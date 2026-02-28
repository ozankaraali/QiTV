"""Export and save channel list methods."""

import logging
from pathlib import Path
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog, QMessageBox, QProgressDialog, QTreeWidget
import requests
from urlobject import URLObject

from services.export import save_m3u_content, save_stb_content

if TYPE_CHECKING:
    from config_manager import ConfigManager
    from provider_manager import ProviderManager

logger = logging.getLogger(__name__)


class ExportMixin:
    """Mixin providing export and save functionality."""

    # Provided by ChannelList at runtime
    provider_manager: "ProviderManager"
    config_manager: "ConfigManager"
    content_list: QTreeWidget
    content_type: str

    def export_all_live_channels(self):
        provider = self.provider_manager.current_provider
        if provider.get("type") != "STB":
            QMessageBox.warning(
                self,
                "Export Error",
                "This feature is only available for STB providers.",
            )
            return

        default_dir = str(Path.home() / "Downloads")
        file_path, _ = QFileDialog.getSaveFileName(
            self, "Export All Live Channels", default_dir, "M3U files (*.m3u)"
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

    def export_shown_channels(self):
        """Export the channels currently displayed in the list."""
        items = []
        for i in range(self.content_list.topLevelItemCount()):
            tree_item = self.content_list.topLevelItem(i)
            if tree_item:
                user_data = tree_item.data(0, Qt.UserRole)
                if user_data and "data" in user_data:
                    items.append(user_data["data"])

        if not items:
            QMessageBox.warning(self, "Export", "No channels to export.")
            return

        default_dir = str(Path.home() / "Downloads")
        file_path, _ = QFileDialog.getSaveFileName(
            self, "Export Shown Channels", default_dir, "M3U files (*.m3u)"
        )
        if file_path:
            provider = self.provider_manager.current_provider
            config_type = provider.get("type", "")
            if config_type == "STB":
                base_url = provider.get("url", "")
                mac = provider.get("mac", "")
                save_stb_content(base_url, items, mac, file_path)
            else:
                save_m3u_content(items, file_path)

    def export_content_cached(self):
        """Export only the cached/browsed content that has already been loaded."""
        default_dir = str(Path.home() / "Downloads")
        file_path, _ = QFileDialog.getSaveFileName(
            self, "Export Cached Content", default_dir, "M3U files (*.m3u)"
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

        default_dir = str(Path.home() / "Downloads")
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Complete Content (Fetch All)",
            default_dir,
            "M3U files (*.m3u)",
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
        scheme = url.scheme
        if self.config_manager.prefer_https and scheme == "http":
            scheme = "https"
        base_url = f"{scheme}://{url.netloc}/server/load.php"

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
            verify_ssl = selected_provider.get("ssl_verify", self.config_manager.ssl_verify)
            response = requests.get(
                base_url, headers=headers, params=params, timeout=10, verify=verify_ssl
            )
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
        scheme = url.scheme
        if self.config_manager.prefer_https and scheme == "http":
            scheme = "https"
        base_url = f"{scheme}://{url.netloc}/server/load.php"

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
            verify_ssl = selected_provider.get("ssl_verify", self.config_manager.ssl_verify)
            response = requests.get(
                base_url, headers=headers, params=params, timeout=10, verify=verify_ssl
            )
            if response.status_code == 200:
                return response.json().get("js", {})
        except Exception as e:
            logger.warning(
                f"Error fetching episodes for {series_item.get('name')} - {season_item.get('name')}: {e}"
            )

        return None
