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

from PySide6.QtCore import QBuffer, QEvent, QObject, QRect, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QFontMetrics, QIcon, QKeySequence, QPixmap
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
import tzlocal
from urlobject import URLObject

from services.provider_api import (
    base_from_url,
    stb_request_url,
    xtream_choose_resolved_base,
    xtream_choose_stream_base,
    xtream_get_php_url,
    xtream_player_api_url,
)

logger = logging.getLogger(__name__)

from content_loader import ContentLoader
from image_loader import ImageLoader
from options import OptionsDialog
from services.export import save_m3u_content, save_stb_content
from services.m3u import parse_m3u
from widgets.delegates import ChannelItemDelegate, HtmlItemDelegate

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
            candidate_urls = []
            if self.prefer_https and self.url.startswith("http://"):
                candidate_urls.append("https://" + self.url[len("http://") :])
            candidate_urls.append(self.url)

            last_exc = None
            for u in candidate_urls:
                try:
                    response = requests.get(u, timeout=10, verify=self.verify_ssl)
                    response.raise_for_status()
                    self.finished.emit({"content": response.text})
                    return
                except requests.RequestException as e:
                    last_exc = e
                    continue
            # If we got here, all attempts failed
            raise last_exc or Exception("Failed to load M3U")
        except requests.RequestException as e:
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
                    '/'
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
        try:
            # Step 1: Authenticate and resolve proper base + formats
            auth_url = xtream_player_api_url(self.base_url, self.username, self.password)
            auth_resp = requests.get(auth_url, timeout=10, verify=self.verify_ssl)
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
                    '/'
                )

            stream_base = xtream_choose_stream_base(server_info) or resolved_base

            # Build candidate ext/base combinations
            exts_pref = []
            if not allowed_formats:
                exts_pref = ["ts", "m3u8"]
            else:
                # Prefer TS if present, then m3u8
                if "ts" in allowed_formats:
                    exts_pref.append("ts")
                if "m3u8" in allowed_formats:
                    exts_pref.append("m3u8")
                # Ensure at least one
                if not exts_pref:
                    exts_pref = ["ts"]

            bases_pref = []
            # Per API doc: prefer HTTPS when available for API & player requests
            if resolved_base:
                bases_pref.append(resolved_base)
            if stream_base and stream_base not in bases_pref:
                bases_pref.append(stream_base)

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
                cat_resp = requests.get(cat_url, timeout=10, verify=self.verify_ssl)
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
            streams_resp = requests.get(streams_url, timeout=15, verify=self.verify_ssl)
            streams_resp.raise_for_status()
            streams = streams_resp.json() or []

            # Step 5: Probe a working combination for live/vod (not needed for series metadata)
            pick_base = bases_pref[0] if bases_pref else resolved_base
            pick_ext = exts_pref[0]

            if self.content_type in ("itv", "vod"):
                sample_id = None
                for s in streams:
                    sid = s.get("stream_id")
                    if sid:
                        sample_id = sid
                        break

                if sample_id:

                    def looks_like_m3u8(rbytes, ctype):
                        if ctype:
                            ctype = ctype.lower()
                            if (
                                "application/vnd.apple.mpegurl" in ctype
                                or "application/x-mpegurl" in ctype
                            ):
                                return True
                            if "text/plain" in ctype and rbytes.startswith(b"#EXTM3U"):
                                return True
                        return rbytes.startswith(b"#EXTM3U")

                    def looks_like_ts(rbytes, ctype):
                        # First byte of TS packet is 0x47 (sync byte) every 188 bytes
                        if not rbytes:
                            return False
                        if rbytes[0:1] == b"\x47":
                            return True
                        if ctype:
                            ctype = ctype.lower()
                            if "video/" in ctype or "application/octet-stream" in ctype:
                                return True
                        return False

                    for b in bases_pref:
                        worked = False
                        # Prefer ext order depending on scheme (https -> m3u8 first)
                        if b.startswith("https://"):
                            ext_order = [ext for ext in ("m3u8", "ts") if ext in exts_pref]
                        else:
                            ext_order = [ext for ext in ("ts", "m3u8") if ext in exts_pref]
                        for ext in ext_order:
                            test_url = f"{b}/{url_prefix}/{self.username}/{self.password}/{sample_id}.{ext}"
                            try:
                                headers = {
                                    "User-Agent": "VLC/3.0.20",
                                }
                                # Fetch a small range sufficient to identify TS or M3U
                                if ext == "ts":
                                    headers["Range"] = "bytes=0-187"
                                else:
                                    headers["Range"] = "bytes=0-1023"
                                r = requests.get(
                                    test_url,
                                    headers=headers,
                                    timeout=6,
                                    allow_redirects=True,
                                    verify=self.verify_ssl,
                                )
                                # Check status, content exists, and content-length > 0
                                clen = r.headers.get("Content-Length", "1")
                                if (
                                    r.status_code in (200, 206)
                                    and r.content
                                    and len(r.content) > 0
                                    and clen != "0"
                                ):
                                    ctype = r.headers.get("Content-Type", "")
                                    ok = (
                                        looks_like_m3u8(r.content, ctype)
                                        if ext == "m3u8"
                                        else looks_like_ts(r.content, ctype)
                                    )
                                    if ok:
                                        pick_base, pick_ext = b, ext
                                        worked = True
                                        break
                            except Exception:
                                pass
                        if worked:
                            break

                try:
                    logger.info(
                        "Xtream stream probe picked base=%s ext=%s (candidates bases=%s, exts=%s)",
                        pick_base,
                        pick_ext,
                        bases_pref,
                        exts_pref,
                    )
                except Exception:
                    pass

            # Step 6: Build items list with category mapping
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
                        container_ext = s.get("container_extension") or pick_ext
                        cmd = f"{pick_base}/{url_prefix}/{self.username}/{self.password}/{stream_id}.{container_ext}"
                    else:
                        # For live streams (itv), use probed extension
                        cmd = f"{pick_base}/{url_prefix}/{self.username}/{self.password}/{stream_id}.{pick_ext}"

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
                        item["container_extension"] = s.get("container_extension") or pick_ext
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
                "stream_base": pick_base,
                "stream_ext": pick_ext,
            }

            self.finished.emit(result)

        except Exception as e:
            self.error.emit(str(e))


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
    # Modify the sorting by Number to use integer and not string (1 < 10, but "1" may not be < "10")
    def __lt__(self, other):
        if not isinstance(other, NumberedTreeWidgetItem):
            return super(NumberedTreeWidgetItem, self).__lt__(other)

        # Safety check: ensure widget is available
        try:
            widget = self.treeWidget()
            if not widget:
                return super(NumberedTreeWidgetItem, self).__lt__(other)

            sort_column = widget.sortColumn()
            if sort_column == 0:  # Number column (channel/season/episode number)
                # Safely convert to int, fallback to string comparison
                try:
                    return int(self.text(sort_column)) < int(other.text(sort_column))
                except (ValueError, TypeError):
                    return self.text(sort_column) < other.text(sort_column)
            return self.text(sort_column) < other.text(sort_column)
        except (RuntimeError, AttributeError):
            # Widget deleted or in invalid state
            return super(NumberedTreeWidgetItem, self).__lt__(other)


## Delegates moved to widgets/delegates.py


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
                try:
                    self.progress.emit("Refreshing EPG…")
                except Exception:
                    pass
                self.epg_manager.force_refresh_current_epg()
            else:
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
        self.forward_stack = []  # Forward history to undo last Back
        self._suppress_forward_clear = False

        # Connect player signals to show/hide media controls
        self.player.playing.connect(self.show_media_controls)
        self.player.stopped.connect(self.hide_media_controls)

        # Input integration from player: mouse back/forward and remote Up/Down
        try:
            self.player.backRequested.connect(self.go_back)
            self.player.forwardRequested.connect(self.go_forward)
            self.player.channelNextRequested.connect(self.channel_surf_next)
            self.player.channelPrevRequested.connect(self.channel_surf_prev)
        except Exception:
            pass

        self.splitter.splitterMoved.connect(self.update_splitter_ratio)
        self.channels_radio.toggled.connect(self.toggle_content_type)
        self.movies_radio.toggled.connect(self.toggle_content_type)

        # Global shortcuts mirrored on main window
        self._setup_global_shortcuts()
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
        for i in range(self.content_list.topLevelItemCount()):
            item = self.content_list.topLevelItem(i)
            item_data = item.data(0, Qt.UserRole)
            content_type = item_data.get("type")

            if self.config_manager.channel_epg and self.can_show_epg(content_type):
                epg_data = self.epg_manager.get_programs_for_channel(item_data["data"], None, 1)
                if epg_data:
                    epg_item = epg_data[0]
                    # Determine format by keys (robust against mixed sources)
                    if "time" in epg_item and "time_to" in epg_item:
                        start_time = datetime.strptime(epg_item["time"], "%Y-%m-%d %H:%M:%S")
                        end_time = datetime.strptime(epg_item["time_to"], "%Y-%m-%d %H:%M:%S")
                    elif "@start" in epg_item and "@stop" in epg_item:
                        start_time = datetime.strptime(epg_item["@start"], "%Y%m%d%H%M%S %z")
                        end_time = datetime.strptime(epg_item["@stop"], "%Y%m%d%H%M%S %z")
                    else:
                        # Unknown structure; skip gracefully
                        item.setData(2, Qt.UserRole, 0)
                        item.setData(3, Qt.UserRole, "")
                        continue
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
                    if "title" in epg_item:  # XMLTV style
                        title_val = epg_item.get("title")
                        text = ""
                        if isinstance(title_val, dict):
                            text = title_val.get("__text") or ""
                        elif isinstance(title_val, list) and title_val:
                            # take first element's text if present
                            first = title_val[0]
                            if isinstance(first, dict):
                                text = first.get("__text") or ""
                        # Localize displayed times
                        try:
                            local_tz = tzlocal.get_localzone()
                            ls = start_time.astimezone(local_tz).strftime("%H:%M")
                            le = end_time.astimezone(local_tz).strftime("%H:%M")
                            epg_text = f"{ls}-{le}  {str(text)}"
                        except Exception:
                            epg_text = str(text)
                    else:
                        # STB style: treat naive datetimes as local
                        try:
                            ls = start_time.strftime("%H:%M")
                            le = end_time.strftime("%H:%M")
                            name_txt = str(epg_item.get("name") or "")
                            epg_text = f"{ls}-{le}  {name_txt}"
                        except Exception:
                            epg_text = str(epg_item.get("name") or "")
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

        # Reset navigation histories on provider switch
        self.navigation_stack.clear()
        self.forward_stack.clear()

        # Remember if this call was a forced update so we can use it in the
        # UI-thread handler safely.
        self._set_provider_force_update = force_update

        self.set_provider_thread = SetProviderThread(
            self.provider_manager, self.epg_manager, force_epg_refresh=bool(force_update)
        )
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
        # Show content type switches for STB and XTREAM providers
        self.content_switch_group.setVisible(config_type in ("STB", "XTREAM"))

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
                    self._play_content(self.link)
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

    def _setup_global_shortcuts(self):
        def not_in_text_input():
            from PySide6.QtWidgets import QLineEdit, QPlainTextEdit, QTextEdit

            w = QApplication.focusWidget()
            return not isinstance(w, (QLineEdit, QTextEdit, QPlainTextEdit))

        # Playback shortcuts: mirror on ChannelList with Window scope
        # so they work when this window is active, without colliding
        # with VideoPlayer's own shortcuts when it has focus.

        # Fullscreen
        act_full = QAction("Fullscreen", self)
        act_full.setShortcut(QKeySequence(Qt.Key_F))
        act_full.setShortcutContext(Qt.WindowShortcut)
        act_full.triggered.connect(
            lambda: self.player.toggle_fullscreen() if not_in_text_input() else None
        )
        self.addAction(act_full)

        # Mute
        act_mute = QAction("Mute", self)
        act_mute.setShortcut(QKeySequence(Qt.Key_M))
        act_mute.setShortcutContext(Qt.WindowShortcut)
        act_mute.triggered.connect(
            lambda: self.player.toggle_mute() if not_in_text_input() else None
        )
        self.addAction(act_mute)

        # Play/Pause
        act_play = QAction("Play/Pause", self)
        act_play.setShortcut(QKeySequence(Qt.Key_Space))
        act_play.setShortcutContext(Qt.WindowShortcut)
        act_play.triggered.connect(
            lambda: self.player.toggle_play_pause() if not_in_text_input() else None
        )
        self.addAction(act_play)

        # Picture-in-Picture
        act_pip = QAction("PiP", self)
        act_pip.setShortcut(QKeySequence(Qt.ALT | Qt.Key_P))
        act_pip.setShortcutContext(Qt.WindowShortcut)

        def _pip():
            if not_in_text_input():
                if self.player.windowState() == Qt.WindowFullScreen:
                    self.player.setWindowState(Qt.WindowNoState)
                self.player.toggle_pip_mode()

        act_pip.triggered.connect(_pip)
        self.addAction(act_pip)

        # Back navigation via keyboard (Backspace/Back keys)
        act_back = QAction("Back", self)
        act_back.setShortcutContext(Qt.ApplicationShortcut)
        try:
            act_back.setShortcuts([QKeySequence(Qt.Key_Backspace), QKeySequence(Qt.Key_Back)])
        except Exception:
            act_back.setShortcut(QKeySequence(Qt.Key_Backspace))
        act_back.triggered.connect(self.go_back)
        self.addAction(act_back)

        # Forward navigation via keyboard (Forward key / Alt+Right fallback)
        act_forward = QAction("Forward", self)
        act_forward.setShortcutContext(Qt.ApplicationShortcut)
        forward_shortcuts = []
        try:
            forward_shortcuts.append(QKeySequence(Qt.Key_Forward))
        except Exception:
            pass
        try:
            # StandardKey.Forward
            forward_shortcuts.append(QKeySequence(QKeySequence.StandardKey.Forward))
        except Exception:
            pass
        if not forward_shortcuts:
            try:
                forward_shortcuts = [QKeySequence(Qt.ALT | Qt.Key_Right)]
            except Exception:
                forward_shortcuts = []
        if forward_shortcuts:
            act_forward.setShortcuts(forward_shortcuts)
        act_forward.triggered.connect(self.go_forward)
        self.addAction(act_forward)

    def _is_playable_item(self, item: QTreeWidgetItem) -> bool:
        try:
            data = item.data(0, Qt.UserRole)
            t = data.get("type") if isinstance(data, dict) else None
            return t in {"m3ucontent", "channel", "movie", "episode"}
        except Exception:
            return False

    def channel_surf_next(self):
        """Move selection down by one; auto-play only if playable (not folders)."""
        cl = self.content_list
        count = cl.topLevelItemCount()
        if count == 0:
            return
        current = cl.currentItem()
        idx = cl.indexOfTopLevelItem(current) if current else -1
        idx = (idx + 1) % count
        candidate = cl.topLevelItem(idx)
        if candidate is not None:
            cl.setCurrentItem(candidate)
            if self._is_playable_item(candidate):
                self.item_activated(candidate)

    def channel_surf_prev(self):
        """Move selection up by one; auto-play only if playable (not folders)."""
        cl = self.content_list
        count = cl.topLevelItemCount()
        if count == 0:
            return
        current = cl.currentItem()
        idx = cl.indexOfTopLevelItem(current) if current else 0
        idx = (idx - 1) % count
        candidate = cl.topLevelItem(idx)
        if candidate is not None:
            cl.setCurrentItem(candidate)
            if self._is_playable_item(candidate):
                self.item_activated(candidate)

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
        # Enable keyboard surfing on the list when remote mode is on
        self.content_list.installEventFilter(self)
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

        # Add checkbox to play in vlc
        self.play_in_vlc_checkbox = QCheckBox("Play in VLC")
        self.play_in_vlc_checkbox.setChecked(getattr(self.config_manager, 'play_in_vlc', False))
        self.play_in_vlc_checkbox.stateChanged.connect(lambda: self.play_in_vlc())
        self.favorite_layout.addWidget(self.play_in_vlc_checkbox)

        # Add checkbox to show EPG
        self.epg_checkbox = QCheckBox("Show EPG")
        self.epg_checkbox.setChecked(self.config_manager.channel_epg)
        self.epg_checkbox.stateChanged.connect(self.show_epg)
        self.favorite_layout.addWidget(self.epg_checkbox)

        # Add checkbox to include descriptions in search (default unchecked)
        self.search_descriptions_checkbox = QCheckBox("Search descriptions")
        self.search_descriptions_checkbox.setToolTip(
            "Also match description/plot and current On Air text"
        )
        self.search_descriptions_checkbox.setChecked(False)
        self.search_descriptions_checkbox.stateChanged.connect(
            lambda: self.filter_content(self.search_box.text())
        )
        self.favorite_layout.addWidget(self.search_descriptions_checkbox)

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
        config_type = selected_provider.get("type", "").upper()
        if config_type == "STB" and not self.current_category:
            return

        # Get the index of the selected item in the content list
        selected_item = self.content_list.selectedItems()
        selected_row = None
        if selected_item:
            selected_row = self.content_list.indexOfTopLevelItem(selected_item[0])

        # Store how was sorted the content list
        sort_column = self.content_list.sortColumn()

        # Update the content list
        if config_type != "STB":
            # For non-STB (Xtream or M3U), display content directly
            content_data = self.provider_manager.current_provider_content.get(self.content_type, {})
            # Get the items from either 'contents' or the content_data itself
            items = content_data.get("contents", content_data)

            # Determine content type for display
            if config_type == "XTREAM":
                content_type_name = "channel"
            else:
                content_type_name = "m3ucontent"

            self.display_content(items, content=content_type_name, select_first=False)
        else:
            # Reload the current category
            self.load_content_in_category(self.current_category)

        # Restore the sorting
        self.content_list.sortItems(sort_column, self.content_list.header().sortIndicatorOrder())

        # Restore the selected item
        if selected_row is not None:
            item = self.content_list.topLevelItem(selected_row)
            if item:
                self.content_list.setCurrentItem(item)
                self.item_selected()

    def can_show_content_info(self, item_type):
        # Show metadata panel for VOD/Series across STB and Xtream providers
        return item_type in ["movie", "serie", "season", "episode"]

    def can_show_epg(self, item_type):
        if item_type in ["channel", "m3ucontent"]:
            if self.config_manager.epg_source == "No Source":
                return False
            if self.config_manager.epg_source == "STB":
                # STB EPG source works with both STB and Xtream providers
                provider_type = self.provider_manager.current_provider.get("type", "").upper()
                if provider_type not in ["STB", "XTREAM"]:
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
                    epg_text = f"<b>{start_txt or ''}-{stop_txt or ''}</b>&nbsp;&nbsp;{epg_item.get('name','')}"
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
                            if not current_text.lstrip().startswith("▶ Now"):
                                now_item.setText(f"▶ Now  {current_text}")
                        except Exception:
                            pass
                        # Light blue background tint
                        try:
                            now_item.setBackground(QColor(51, 153, 255, 40))
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
                    self.lock_ui_before_loading()
                    if self.image_loader and self.image_loader.isRunning():
                        self.image_loader.wait()
                    self.image_loader = ImageLoader(
                        [
                            icon_url,
                        ],
                        self.image_manager,
                        iconified=False,
                        verify_ssl=self.config_manager.ssl_verify,
                    )
                    self.image_loader.progress_updated.connect(self.update_poster)
                    self.image_loader.finished.connect(self.image_loader_finished)
                    self.image_loader.start()
                    self.cancel_button.setText("Cancel fetching poster...")
        else:
            self.content_info_text.setText("No data available")

    def populate_movie_tvshow_content_info(self, item_data):
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
            self.lock_ui_before_loading()
            if self.image_loader and self.image_loader.isRunning():
                self.image_loader.wait()
            self.image_loader = ImageLoader(
                [poster_url],
                self.image_manager,
                iconified=False,
                verify_ssl=self.config_manager.ssl_verify,
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
        self.image_loader = ImageLoader(
            logo_urls, self.image_manager, iconified=True, verify_ssl=self.config_manager.ssl_verify
        )
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
        self.forward_stack.clear()
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
        # Stop refreshing On Air content BEFORE any structural changes
        try:
            if self.refresh_on_air_timer.isActive():
                self.refresh_on_air_timer.stop()
        except Exception:
            pass

        # Unregister the selection change event during rebuild
        try:
            self.content_list.itemSelectionChanged.disconnect(self.item_selected)
        except (TypeError, RuntimeError):
            pass

        # Disable widget updates during clear (but keep signals enabled to allow Qt internal cleanup)
        self.content_list.setUpdatesEnabled(False)

        try:
            self.content_list.clear()
        except Exception as e:
            logger.error(f"Error clearing content_list: {e}", exc_info=True)

        try:
            self.content_list.setSortingEnabled(False)
        except (RuntimeError, AttributeError):
            pass

        # Defer reconnecting itemSelectionChanged until population completes

        self.current_list_content = content
        need_logos = content in ["channel", "m3ucontent"] and self.config_manager.channel_logos
        logo_urls = []
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

        # Get headers
        headers = header_info[content]["headers"]
        self.content_list.setColumnCount(len(headers))
        try:
            self.content_list.setHeaderLabels(headers)
        except Exception as e:
            logger.error(f"display_content: setHeaderLabels failed: {e}", exc_info=True)
            raise

        # no favorites on seasons or episodes genre_sfolders
        check_fav = content in ["channel", "movie", "serie", "m3ucontent"]

        self.show_favorite_layout(check_fav)

        # Disable updates during population to prevent Qt conflicts
        self.content_list.setUpdatesEnabled(False)

        for item_idx, item_data in enumerate(items):

            # Create tree widget item based on content type
            if content == "channel":
                list_item = ChannelTreeWidgetItem(self.content_list)
            elif content in ["season", "episode"]:
                # Use NumberedTreeWidgetItem for seasons and episodes (numeric sorting)
                list_item = NumberedTreeWidgetItem(self.content_list)
            else:
                # Use plain QTreeWidgetItem for other content
                list_item = QTreeWidgetItem(self.content_list)

            for col_idx, key in enumerate(header_info[content]["keys"]):
                raw_value = item_data.get(key)
                if key == "added":
                    # Show only date part if present
                    text_value = str(raw_value).split()[0] if raw_value else ""
                else:
                    text_value = html.unescape(str(raw_value)) if raw_value is not None else ""
                list_item.setText(col_idx, text_value)

            list_item.setData(0, Qt.UserRole, {"type": content, "data": item_data})

            # If content type is channel, collect the logo urls from the image_manager
            if need_logos:
                logo_urls.append(item_data.get("logo", ""))

            # Highlight favorite items
            item_name = item_data.get("name") or item_data.get("title")
            if check_fav and self.check_if_favorite(item_name):
                list_item.setBackground(0, QColor(0, 0, 255, 20))

        self.content_list.sortItems(0, Qt.AscendingOrder)
        self.content_list.setSortingEnabled(True)

        # Re-enable updates now that population and sorting are complete
        self.content_list.setUpdatesEnabled(True)

        # Resize columns AFTER re-enabling updates to avoid Qt timer conflicts
        for i in range(len(header_info[content]["headers"])):
            if i != 2:  # Don't auto-resize the progress column
                try:
                    self.content_list.resizeColumnToContents(i)
                except Exception as e:
                    logger.error(f"Error resizing column {i}: {e}", exc_info=True)

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

        # Re-register the selection change event after rebuild
        try:
            self.content_list.itemSelectionChanged.connect(self.item_selected)
        except Exception:
            pass

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
            self.image_loader = ImageLoader(
                logo_urls,
                self.image_manager,
                iconified=True,
                verify_ssl=self.config_manager.ssl_verify,
            )
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
        item_type = None
        if self.content_list.topLevelItemCount() > 0:
            item = self.content_list.topLevelItem(0)
            item_type = self.get_item_type(item)

        for i in range(self.content_list.topLevelItemCount()):
            item = self.content_list.topLevelItem(i)
            item_name = self.get_item_name(item, item_type)
            matches_search = search_text in item_name.lower()

            # Optionally include metadata fields (description/plot, group, On Air EPG) in search
            if (
                not matches_search
                and getattr(self, "search_descriptions_checkbox", None)
                and self.search_descriptions_checkbox.isChecked()
                and item_type in ["channel", "movie", "serie", "m3ucontent"]
            ):
                try:
                    data = item.data(0, Qt.UserRole) or {}
                    content = data.get("data", {}) if isinstance(data, dict) else {}
                    description = content.get("description") or content.get("plot") or ""
                    group = content.get("group", "")
                    # Check textual metadata
                    if (isinstance(description, str) and search_text in description.lower()) or (
                        isinstance(group, str) and search_text in group.lower()
                    ):
                        matches_search = True
                    # Check EPG "On Air" column text for channels/m3u when EPG is enabled
                    if (
                        not matches_search
                        and item_type in ["channel", "m3ucontent"]
                        and self.config_manager.channel_epg
                        and self.can_show_epg(item_type)
                    ):
                        try:
                            epg_text = item.data(3, Qt.UserRole) or ""
                            if isinstance(epg_text, str) and search_text in epg_text.lower():
                                matches_search = True
                        except Exception:
                            pass
                except Exception:
                    # Be conservative; ignore metadata if unexpected structure
                    pass

            # For categories, check if any content inside matches and show dropdown
            if item_type == "category" and search_text:
                matching_items = self._get_matching_items_in_category(item, search_text)
                if matching_items:
                    matches_search = True
                    # Populate category with matching items as children
                    self._populate_category_dropdown(item, matching_items)
                    item.setExpanded(True)  # Auto-expand to show matches
                else:
                    # Category name matches but no content inside
                    # Clear any existing children
                    item.takeChildren()
                    if not matches_search:
                        item.setExpanded(False)
            else:
                # Not searching or not a category - clear children
                if item_type == "category":
                    item.takeChildren()
                    item.setExpanded(False)

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

    def _get_matching_items_in_category(self, category_item, search_text):
        """Get items in category that match the search text.

        Returns list of matching items that can be displayed as dropdown children.
        """
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
                getattr(self, "search_descriptions_checkbox", None)
                and self.search_descriptions_checkbox.isChecked()
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
            logger.warning(f"Opening VLC for link: {self.link}")
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
            self._play_content(file_path)

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

            # Clear forward history unless we are performing a programmatic forward
            if not getattr(self, "_suppress_forward_clear", False):
                self.forward_stack.clear()

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
            # Save to forward stack so we can undo this Back
            self.forward_stack.append((nav_type, previous_data, previous_selected_id))
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

    def go_forward(self):
        if not self.forward_stack:
            return
        nav_type, previous_data, previous_selected_id = self.forward_stack.pop()
        # Redo the last navigation by selecting the same item again
        try:
            self._suppress_forward_clear = True
            items = self.content_list.findItems(previous_selected_id or "", Qt.MatchExactly, 0)
            if items:
                self.content_list.setCurrentItem(items[0])
                self.item_activated(items[0])
        finally:
            self._suppress_forward_clear = False

    def options_dialog(self):
        options = OptionsDialog(self)
        options.exec_()
        # Refresh provider combo in case providers were added/removed/renamed
        self.populate_provider_combo()

    # parse_m3u moved to services/m3u.py

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

    def eventFilter(self, obj, event):
        try:
            if obj is self.content_list and event.type() == QEvent.KeyPress:
                # Honor Keyboard/Remote Mode for list as well
                if bool(self.config_manager.keyboard_remote_mode):
                    if event.key() == Qt.Key_Up:
                        self.channel_surf_prev()
                        return True
                    elif event.key() == Qt.Key_Down:
                        self.channel_surf_next()
                        return True
        except Exception:
            pass
        return super().eventFilter(obj, event)

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
            url, headers, self.content_type, category_id=category_id, verify_ssl=verify_ssl
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

    def _on_xtream_series_finished(self, payload):
        """UI-thread handler for Xtream series info results."""
        try:
            if payload and isinstance(payload, dict):
                seasons = payload.get("seasons", [])
                if seasons:
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
            prefer_https = selected_provider.get("prefer_https", self.config_manager.prefer_https)
            verify_ssl = selected_provider.get("ssl_verify", self.config_manager.ssl_verify)
            worker = LinkCreatorWorker(
                base_url=base_url,
                headers=headers,
                content_type=self.content_type,
                cmd=cmd,
                is_episode=is_episode,
                series_param=series_param,
                verify_ssl=verify_ssl,
                prefer_https=prefer_https,
            )
            worker.moveToThread(thread)

            # Store context for UI handler
            self._pending_link_ctx = {
                "item_data": item_data,
                "item_type": item_type or "channel",
                "is_episode": is_episode,
            }

            def _cleanup():
                try:
                    self._bg_jobs.remove((thread, worker))
                except ValueError:
                    pass

            thread.started.connect(worker.run)
            worker.finished.connect(self._on_link_created, Qt.QueuedConnection)
            worker.error.connect(self._on_link_error, Qt.QueuedConnection)
            worker.finished.connect(thread.quit)
            worker.error.connect(thread.quit)
            thread.finished.connect(worker.deleteLater)
            thread.finished.connect(thread.deleteLater)
            thread.finished.connect(_cleanup)
            thread.start()
            self._bg_jobs.append((thread, worker))
        else:
            cmd = item_data.get("cmd")
            self.link = cmd
            self._play_content(cmd)
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
            self._play_content(self.link)
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
        if not items:
            logger.warning("No items in data when updating seasons list")
            return

        for i, item in enumerate(items):
            try:
                # Store original name before modification
                original_name = item.get("name", f"Season {i+1}")
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
                item["name"] = f'{series_name}.{original_name}'

                # Add "added" field if not present (use air_date or empty)
                if "added" not in item:
                    item["added"] = item.get("air_date", "")

            except Exception as e:
                logger.error(f"Error processing season {i}: {e}", exc_info=True)
                # Set defaults if processing fails
                item["o_name"] = item.get("name", f"Season {i+1}")
                item["number"] = str(i + 1)
                item["name"] = f'{self.current_series.get("name", "Unknown")}.Season {i+1}'
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

    def create_link(self, item, is_episode=False):
        try:
            selected_provider = self.provider_manager.current_provider
            headers = self.provider_manager.headers
            url = selected_provider.get("url", "")
            url = URLObject(url)
            scheme = url.scheme
            if self.config_manager.prefer_https and scheme == "http":
                scheme = "https"
            url = f"{scheme}://{url.netloc}"
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
            verify_ssl = selected_provider.get("ssl_verify", self.config_manager.ssl_verify)
            response = requests.get(fetchurl, headers=headers, timeout=5, verify=verify_ssl)
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
        # Keep it minimal and non-invasive; prior working behavior
        return (url or "").strip()

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

    def _play_content(self, url):
        """Play content either in VLC or built-in player based on checkbox state."""
        if self.play_in_vlc_checkbox.isChecked():
            self._launch_vlc(url)
        else:
            self.player.play_video(url)

    def _launch_vlc(self, cmd):
        """Launch VLC with error handling and platform support."""
        vlc_cmd = None

        # Platform-specific VLC detection
        if platform.system() == "Darwin":  # macOS
            # Check common macOS VLC locations
            macos_vlc_path = "/Applications/VLC.app/Contents/MacOS/VLC"
            if os.path.exists(macos_vlc_path):
                vlc_cmd = macos_vlc_path
            else:
                # Try homebrew cask location
                homebrew_vlc = os.path.expanduser("~/Applications/VLC.app/Contents/MacOS/VLC")
                if os.path.exists(homebrew_vlc):
                    vlc_cmd = homebrew_vlc
        else:
            # For Windows and Linux, try to find vlc in PATH
            vlc_cmd = shutil.which('vlc')

        if not vlc_cmd:
            QMessageBox.warning(
                self,
                "VLC Not Found",
                "VLC Media Player is not installed or not found.\n\n"
                "Please install VLC:\n"
                "• macOS: Download from https://www.videolan.org/vlc/\n"
                "• Linux: Use your package manager (apt, yum, etc.)\n"
                "• Windows: Download from https://www.videolan.org/vlc/",
            )
            return False

        try:
            subprocess.Popen([vlc_cmd, cmd])
            return True
        except Exception as e:
            QMessageBox.warning(self, "VLC Launch Failed", f"Failed to launch VLC: {str(e)}")
            return False

    def play_in_vlc(self):
        """Handle VLC checkbox state changes."""
        if self.play_in_vlc_checkbox.isChecked():
            # Only close if player is visible and playing something
            if hasattr(self, 'player') and self.player.isVisible():
                self.player.close()

        # Save preference to config
        self.config_manager.play_in_vlc = self.play_in_vlc_checkbox.isChecked()
