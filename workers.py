"""Worker classes, tree widget items, and helper functions for channel_list."""

import logging
import re
import time
from typing import Dict, List, Optional

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtWidgets import QTreeWidgetItem
import requests
from urlobject import URLObject

from services.m3u import parse_m3u
from services.provider_api import (
    base_from_url,
    stb_request_url,
    xtream_choose_resolved_base,
    xtream_player_api_url,
)

logger = logging.getLogger(__name__)


# --- Roman numeral helpers (conservative matching) ---
_ROMAN_RE = re.compile(r"^M{0,3}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})$")


def roman_to_int(token: str) -> Optional[int]:
    if not token or token != token.upper():
        return None
    if not _ROMAN_RE.match(token):
        return None
    values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    total = 0
    i = 0
    while i < len(token):
        if i + 1 < len(token) and values[token[i]] < values[token[i + 1]]:
            total += values[token[i + 1]] - values[token[i]]
            i += 2
        else:
            total += values[token[i]]
            i += 1
    return total if 1 <= total <= 3999 else None


def find_roman_token(text: str) -> Optional[int]:
    if not isinstance(text, str) or not text:
        return None
    # 1) Context keywords (Season/Episode/Part) followed by Roman numerals
    m = re.search(r"(?i)\b(season|episode|part)\s+([MDCLXVI]+)\b", text)
    if m:
        token = m.group(2)
        val = roman_to_int(token)
        if val is not None:
            return val
    # 2) Trailing Roman numerals at end (e.g., "Rocky II")
    m = re.search(r"\b([MDCLXVI]+)\)?\s*$", text)
    if m:
        token = m.group(1)
        val = roman_to_int(token)
        # Be conservative: avoid accepting single 'I' at end (too ambiguous)
        if val is not None and val >= 2:
            return val
    return None


class M3ULoaderWorker(QObject):
    finished = Signal(object)
    error = Signal(str)

    def __init__(self, url: str, verify_ssl: bool = True, prefer_https: bool = False):
        super().__init__()
        self.url = url
        self.verify_ssl = verify_ssl
        self.prefer_https = prefer_https

    def run(self):
        try:
            if not self.url.startswith(("http://", "https://")):
                with open(self.url, encoding="utf-8") as file:
                    self.finished.emit(parse_m3u(file.read(), categorize=True))
                return
            candidate_urls = []
            if self.prefer_https and self.url.startswith("http://"):
                candidate_urls.append("https://" + self.url[len("http://") :])
            candidate_urls.append(self.url)

            last_exc = None
            for u in candidate_urls:
                try:
                    response = requests.get(u, timeout=10, verify=self.verify_ssl)
                    response.raise_for_status()
                    self.finished.emit(parse_m3u(response.text, categorize=True))
                    return
                except requests.RequestException as e:
                    last_exc = e
                    continue
            # If we got here, all attempts failed
            raise last_exc or Exception("Failed to load M3U")
        except (requests.RequestException, OSError, ValueError) as e:
            self.error.emit(str(e))


class XtreamAuthWorker(QObject):
    finished = Signal(object)
    error = Signal(str)

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        verify_ssl: bool = True,
        prefer_https: bool = False,
    ):
        super().__init__()
        self.base_url = base_url
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl
        self.prefer_https = prefer_https

    def run(self):
        try:
            # First authenticate to resolve the correct domain/ports
            auth_url = xtream_player_api_url(self.base_url, self.username, self.password)
            resp = requests.get(auth_url, timeout=10, verify=self.verify_ssl)
            resp.raise_for_status()
            body = resp.json()
            server_info = body.get("server_info", {}) if isinstance(body, dict) else {}
            allowed: List[str] = []
            user_info = body.get("user_info", {}) if isinstance(body, dict) else {}
            try:
                allowed = user_info.get("allowed_output_formats", []) or []
            except Exception:
                allowed = []

            resolved_base = xtream_choose_resolved_base(
                server_info, self.base_url, prefer_https=self.prefer_https
            )
            if not resolved_base:
                # Fall back to given base
                url_obj = URLObject(self.base_url)
                resolved_base = f"{url_obj.scheme or 'http'}://{url_obj.netloc or url_obj}".rstrip(
                    "/"
                )

            self.finished.emit(
                {
                    "resolved_base": resolved_base,
                    "allowed_output_formats": allowed,
                    "server_info": server_info,
                }
            )
        except Exception as e:
            self.error.emit(str(e))


class XtreamLoaderWorker(QObject):
    """Worker for loading Xtream content (Live/VOD/Series) using Player API v2."""

    finished = Signal(object)
    error = Signal(str)

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        content_type: str = "itv",
        verify_ssl: bool = True,
        prefer_https: bool = False,
    ):
        super().__init__()
        self.base_url = base_url
        self.username = username
        self.password = password
        self.content_type = content_type  # "itv", "vod", or "series"
        self.verify_ssl = verify_ssl
        self.prefer_https = prefer_https

    def run(self):
        # Use a single Session so connections are reused and properly
        # closed when the worker finishes (avoids leaking connections
        # against providers with strict connection limits).
        session = requests.Session()
        try:
            # Step 1: Authenticate and resolve proper base + formats
            auth_url = xtream_player_api_url(self.base_url, self.username, self.password)
            auth_resp = session.get(auth_url, timeout=10, verify=self.verify_ssl)
            auth_resp.raise_for_status()
            auth_body = auth_resp.json() if auth_resp.content else {}
            server_info = auth_body.get("server_info", {}) if isinstance(auth_body, dict) else {}
            user_info = auth_body.get("user_info", {}) if isinstance(auth_body, dict) else {}
            allowed_formats = user_info.get("allowed_output_formats", []) or []

            resolved_base = xtream_choose_resolved_base(
                server_info, self.base_url, prefer_https=self.prefer_https
            )
            if not resolved_base:
                url_obj = URLObject(self.base_url)
                resolved_base = f"{url_obj.scheme or 'http'}://{url_obj.netloc or url_obj}".rstrip(
                    "/"
                )

            # Select from provider metadata only. Opening a media URL here consumes
            # a playback connection before the user has selected anything.
            if not allowed_formats or "ts" in allowed_formats:
                stream_ext = "ts"
            elif "m3u8" in allowed_formats:
                stream_ext = "m3u8"
            else:
                stream_ext = allowed_formats[0]

            # Step 2: Determine API actions based on content type
            if self.content_type == "itv":
                cat_action = "get_live_categories"
                streams_action = "get_live_streams"
                url_prefix = "live"
            elif self.content_type == "vod":
                cat_action = "get_vod_categories"
                streams_action = "get_vod_streams"
                url_prefix = "movie"
            elif self.content_type == "series":
                cat_action = "get_series_categories"
                streams_action = "get_series"
                url_prefix = "series"
            else:
                raise ValueError(f"Unknown content type: {self.content_type}")

            # Step 3: Fetch categories
            cat_url = xtream_player_api_url(
                resolved_base, self.username, self.password, action=cat_action
            )
            categories = []
            categories_map = {}
            try:
                cat_resp = session.get(cat_url, timeout=10, verify=self.verify_ssl)
                if cat_resp.ok and cat_resp.content:
                    cats_data = cat_resp.json() or []
                    for c in cats_data:
                        cid = str(c.get("category_id"))
                        cname = c.get("category_name") or "Unknown"
                        categories_map[cid] = cname
                        categories.append({"id": cid, "title": cname})
            except Exception as e:
                logger.warning(f"Failed to fetch categories: {e}")

            # Add "All" category
            categories.insert(0, {"id": "*", "title": "All"})

            # Step 4: Fetch all content
            streams_url = xtream_player_api_url(
                resolved_base, self.username, self.password, action=streams_action
            )
            streams_resp = session.get(streams_url, timeout=15, verify=self.verify_ssl)
            streams_resp.raise_for_status()
            streams = streams_resp.json() or []

            # Step 5: Build items list with category mapping
            items: List[Dict] = []
            sorted_channels: Dict[str, List[int]] = {}

            for s in streams:
                try:
                    stream_id = s.get("stream_id") or s.get("series_id")
                    if not stream_id:
                        continue

                    num = s.get("num") or len(items) + 1
                    name = s.get("name") or f"Stream {stream_id}"
                    logo = s.get("stream_icon") or s.get("cover") or ""

                    # Get category ID
                    if isinstance(s.get("category_ids"), list) and s.get("category_ids"):
                        cid = str(s.get("category_ids")[0])
                    else:
                        cid = str(s.get("category_id") or "None")

                    # Build URL based on content type
                    if self.content_type == "series":
                        # Series don't have direct playback URLs; store series_id for later fetching
                        cmd = ""  # Will be populated when episodes are fetched
                    elif self.content_type == "vod":
                        # For VOD, use container_extension from stream data
                        container_ext = s.get("container_extension") or stream_ext
                        cmd = f"{resolved_base}/{url_prefix}/{self.username}/{self.password}/{stream_id}.{container_ext}"
                    else:
                        # For live streams, prefer TS when the provider supports it.
                        cmd = f"{resolved_base}/{url_prefix}/{self.username}/{self.password}/{stream_id}.{stream_ext}"

                    item = {
                        "id": stream_id,
                        "number": str(num),
                        "name": name,
                        "logo": logo,
                        "tv_genre_id": cid,  # Use STB-compatible field name
                        "cmd": cmd,
                    }

                    if self.content_type == "itv":
                        item["xmltv_id"] = s.get("epg_channel_id") or ""
                    elif self.content_type == "vod":
                        item["director"] = s.get("director") or ""
                        item["description"] = s.get("plot") or ""
                        item["rating"] = s.get("rating") or ""
                        item["year"] = s.get("releasedate") or ""
                        # Store container extension for reference
                        item["container_extension"] = s.get("container_extension") or stream_ext
                    elif self.content_type == "series":
                        item["plot"] = s.get("plot") or ""
                        item["rating"] = s.get("rating") or ""
                        item["year"] = s.get("year") or ""

                    items.append(item)

                    # Build sorted_channels mapping
                    if cid not in sorted_channels:
                        sorted_channels[cid] = []
                    sorted_channels[cid].append(len(items) - 1)

                except Exception as e:
                    logger.warning(f"Failed to process stream: {e}")
                    continue

            # Sort channels within each category
            for cat_id in sorted_channels:
                sorted_channels[cat_id].sort(
                    key=lambda x: int(items[x]["number"]) if items[x]["number"].isdigit() else 0
                )

            # Add "None" category if there are uncategorized items
            if "None" in sorted_channels:
                categories.append({"id": "None", "title": "Unknown Category"})

            result = {
                "categories": categories,
                "contents": items,
                "sorted_channels": sorted_channels,
                "resolved_base": resolved_base,
                "stream_base": resolved_base,
                "stream_ext": stream_ext,
            }

            self.finished.emit(result)

        except Exception as e:
            self.error.emit(str(e))
        finally:
            session.close()


class XtreamSeriesInfoWorker(QObject):
    """Worker for loading Xtream series info (seasons and episodes) using Player API v2."""

    finished = Signal(object)
    error = Signal(str)

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        series_id: str,
        resolved_base: str = "",
        verify_ssl: bool = True,
        prefer_https: bool = False,
    ):
        super().__init__()
        self.base_url = base_url
        self.username = username
        self.password = password
        self.series_id = series_id
        self.resolved_base = resolved_base
        self.verify_ssl = verify_ssl
        self.prefer_https = prefer_https

    def run(self):
        try:
            # Use resolved_base if provided, otherwise determine from base_url
            if not self.resolved_base:
                auth_url = xtream_player_api_url(self.base_url, self.username, self.password)
                auth_resp = requests.get(auth_url, timeout=10, verify=self.verify_ssl)
                auth_resp.raise_for_status()
                auth_body = auth_resp.json() if auth_resp.content else {}
                server_info = (
                    auth_body.get("server_info", {}) if isinstance(auth_body, dict) else {}
                )
                self.resolved_base = xtream_choose_resolved_base(
                    server_info, self.base_url, prefer_https=self.prefer_https
                )

            if not self.resolved_base:
                url_obj = URLObject(self.base_url)
                self.resolved_base = (
                    f"{url_obj.scheme or 'http'}://{url_obj.netloc or url_obj}".rstrip("/")
                )

            # Fetch series info
            info_url = xtream_player_api_url(
                self.resolved_base,
                self.username,
                self.password,
                action="get_series_info",
                extra={"series_id": self.series_id},
            )
            info_resp = requests.get(info_url, timeout=15, verify=self.verify_ssl)
            info_resp.raise_for_status()
            series_data = info_resp.json() or {}

            # Extract seasons and episodes with safety checks
            if not isinstance(series_data, dict):
                raise ValueError(f"Invalid series_data type: {type(series_data)}")

            episodes_dict = series_data.get("episodes", {})
            if not isinstance(episodes_dict, dict):
                episodes_dict = {}
            seasons_info_raw = series_data.get("seasons", [])
            series_info = series_data.get("info", {})

            # Convert seasons list to dict for easy lookup (some providers use list, some use dict)
            seasons_info = {}
            if isinstance(seasons_info_raw, list):
                # List format: convert to dict by season number
                for season_item in seasons_info_raw:
                    if isinstance(season_item, dict):
                        season_num = str(
                            season_item.get("season_number", season_item.get("id", ""))
                        )
                        if season_num:
                            seasons_info[season_num] = season_item
            elif isinstance(seasons_info_raw, dict):
                # Already in dict format
                seasons_info = seasons_info_raw

            # Build seasons list
            seasons = []
            if episodes_dict:
                for season_num in sorted(
                    episodes_dict.keys(), key=lambda x: int(x) if x.isdigit() else 0
                ):
                    season_episodes = episodes_dict.get(season_num, [])
                    if not isinstance(season_episodes, list):
                        continue

                    season_data = seasons_info.get(season_num, {})
                    if not isinstance(season_data, dict):
                        season_data = {}

                    season_item = {
                        "id": season_num,
                        "name": season_data.get("name") or f"Season {season_num}",
                        "cover": season_data.get("cover_big") or season_data.get("cover") or "",
                        "overview": season_data.get("overview") or "",
                        "air_date": season_data.get("air_date") or "",
                        "episode_count": str(len(season_episodes)),
                        "episodes": season_episodes,  # Store episodes with season
                    }
                    seasons.append(season_item)

            # Explicitly convert to plain Python types
            result = {
                "seasons": list(seasons),  # Ensure it's a plain list
                "series_info": dict(series_info) if series_info else {},
                "resolved_base": str(self.resolved_base) if self.resolved_base else "",
            }

            # Emit result to UI
            self.finished.emit(result)

        except Exception as e:
            logger.error(f"Worker error: {e}")
            try:
                self.error.emit(str(e))
            except Exception as emit_err:
                logger.error(f"Error emitting error: {emit_err}")


class STBCategoriesWorker(QObject):
    finished = Signal(object)
    error = Signal(str)

    def __init__(
        self,
        base_url: str,
        headers: dict,
        content_type: str = "itv",
        verify_ssl: bool = True,
        prefer_https: bool = False,
    ):
        super().__init__()
        self.base_url = base_url
        self.headers = headers
        self.content_type = content_type
        self.verify_ssl = verify_ssl
        self.prefer_https = prefer_https

    def run(self):
        try:
            base = base_from_url(self.base_url)
            if self.prefer_https and base.startswith("http://"):
                base = "https://" + base[len("http://") :]

            # Use correct action based on content type
            action = "get_genres" if self.content_type == "itv" else "get_categories"
            fetchurl = stb_request_url(base, self.content_type, action)
            resp = requests.get(fetchurl, headers=self.headers, timeout=10, verify=self.verify_ssl)
            resp.raise_for_status()
            categories = resp.json()["js"]

            # Only fetch all channels for itv type
            all_channels = []
            if self.content_type == "itv":
                fetchurl = stb_request_url(base, "itv", "get_all_channels")
                resp = requests.get(
                    fetchurl, headers=self.headers, timeout=10, verify=self.verify_ssl
                )
                resp.raise_for_status()
                all_channels = resp.json()["js"]["data"]

            result = {"categories": categories, "contents": {}}
            if self.content_type == "itv":
                sorted_channels: Dict[str, List[int]] = {}
                for index, channel in enumerate(all_channels):
                    category_id = str(channel.get("tv_genre_id"))
                    sorted_channels.setdefault(category_id, []).append(index)
                if "None" in sorted_channels:
                    categories.append({"id": "None", "title": "Unknown Category"})
                result["contents"] = all_channels
                result["sorted_channels"] = sorted_channels
            self.finished.emit(result)
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
        verify_ssl: bool = True,
        prefer_https: bool = False,
    ):
        super().__init__()
        self.base_url = base_url
        self.headers = headers
        self.content_type = content_type
        self.cmd = cmd
        self.is_episode = is_episode
        self.series_param = series_param
        self.verify_ssl = verify_ssl
        self.prefer_https = prefer_https

    def run(self):
        try:
            from urllib.parse import quote as url_quote

            url = URLObject(self.base_url)
            scheme = url.scheme
            if self.prefer_https and scheme == "http":
                scheme = "https"
            base = f"{scheme}://{url.netloc}"
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
            response = requests.get(
                fetchurl, headers=self.headers, timeout=10, verify=self.verify_ssl
            )
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


class NumberedTreeWidgetItem(QTreeWidgetItem):
    """Keep numeric sort values in Python instead of parsing Qt text per comparison."""

    _number_key: tuple[int, int | str] = (1, "")
    _epg_columns = False

    def __init__(self, texts):
        super().__init__(texts)
        try:
            self._number_key = (0, int(texts[0]))
        except ValueError:
            self._number_key = (1, texts[0])

    def __lt__(self, other):
        widget = self.treeWidget()
        if widget is None or not isinstance(other, NumberedTreeWidgetItem):
            return self.text(0) < other.text(0)
        column = widget.sortColumn()
        if column == 0:
            return self._number_key < other._number_key
        if self._epg_columns and other._epg_columns:
            if column == 2:
                left = self.data(column, Qt.UserRole)
                right = other.data(column, Qt.UserRole)
                return (left is None, left or 0) < (right is None, right or 0)
            if column == 3:
                return (self.data(column, Qt.UserRole) or "") < (
                    other.data(column, Qt.UserRole) or ""
                )
        return self.text(column) < other.text(column)


class ChannelTreeWidgetItem(NumberedTreeWidgetItem):
    _epg_columns = True


class SetProviderThread(QThread):
    progress = Signal(str)

    def __init__(self, provider_manager, epg_manager, force_epg_refresh: bool = False):
        super().__init__()
        self.provider_manager = provider_manager
        self.epg_manager = epg_manager
        self.force_epg_refresh = force_epg_refresh

    def run(self):
        try:
            self.provider_manager.set_current_provider(self.progress)
            if self.force_epg_refresh:
                self.provider_manager.clear_current_provider_cache()
            if self.force_epg_refresh:
                try:
                    self.progress.emit("Refreshing EPG…")
                except Exception:
                    pass
                self.epg_manager.force_refresh_current_epg()
            else:
                self.epg_manager.set_current_epg()
        except Exception as e:
            logger.warning(f"Error in initializing provider: {e}")
