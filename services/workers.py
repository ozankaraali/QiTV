"""Provider workers: catalog loads, provider setup, EPG and STB links, for CatalogService."""

import logging
import re
from typing import Dict, List, Optional
from urllib.parse import urlsplit

from PySide6.QtCore import QObject, Qt, Signal

from services.failure_reasons import (
    DEVICE_MESSAGE,
    UNEXPECTED_ANSWER_MESSAGE,
    UNREACHABLE_MESSAGE,
)
from services.lazy_import import lazy_module
from services.m3u import decode_playlist, parse_m3u
from services.provider_api import (
    CLIENT_HEADERS,
    base_from_url,
    https_fallback_candidates,
    stb_create_link_url,
    stb_portal_base,
    stb_request_url,
    xtream_choose_resolved_base,
    xtream_player_api_url,
)
from services.provider_manager import HANDSHAKE_UNEXPECTED, HANDSHAKE_UNREACHABLE
from services.redact import safe_exc, safe_traceback

logger = logging.getLogger(__name__)
requests = lazy_module("requests")

NO_PORTAL_DATA_MESSAGE = "The portal returned no data"
STB_DEVICE_REJECTED_MESSAGE = DEVICE_MESSAGE


def _resolve_xtream_base(server_info: dict, base_url: str, prefer_https: bool) -> str:
    """Pick the panel-advertised base, falling back to the user's URL when there is none."""
    resolved_base = xtream_choose_resolved_base(server_info, base_url, prefer_https=prefer_https)
    if resolved_base:
        return resolved_base
    parts = urlsplit(base_url)
    return f"{parts.scheme or 'http'}://{parts.netloc or base_url}".rstrip("/")


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
                with open(self.url, encoding="utf-8-sig", errors="replace") as file:
                    self.finished.emit(parse_m3u(file.read(), categorize=True))
                return
            last_exc = None
            for u in https_fallback_candidates(self.url, self.prefer_https):
                try:
                    response = requests.get(
                        u, timeout=10, headers=CLIENT_HEADERS, verify=self.verify_ssl
                    )
                    response.raise_for_status()
                    self.finished.emit(parse_m3u(decode_playlist(response), categorize=True))
                    return
                except requests.RequestException as e:
                    last_exc = e
            # If we got here, all attempts failed
            raise last_exc or Exception("Failed to load M3U")
        except (requests.RequestException, OSError, ValueError) as e:
            self.error.emit(str(e))


# Player API actions (categories, streams) and stream URL prefix per content type.
_XTREAM_CATALOG_ACTIONS = {
    "itv": ("get_live_categories", "get_live_streams", "live"),
    "vod": ("get_vod_categories", "get_vod_streams", "movie"),
    "series": ("get_series_categories", "get_series", "series"),
}


def _max_connections(user_info) -> Optional[int]:
    """Xtream reports the account's concurrent stream limit as a string or int."""
    try:
        value = int(str(user_info.get("max_connections", "")).strip())
    except AttributeError, ValueError:
        return None
    return value if value > 0 else None


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
        session.headers.update(CLIENT_HEADERS)
        try:
            # Step 1: Authenticate and resolve proper base + formats
            auth_url = xtream_player_api_url(self.base_url, self.username, self.password)
            auth_resp = session.get(auth_url, timeout=10, verify=self.verify_ssl)
            auth_resp.raise_for_status()
            auth_body = auth_resp.json() if auth_resp.content else {}
            server_info = auth_body.get("server_info", {}) if isinstance(auth_body, dict) else {}
            user_info = auth_body.get("user_info", {}) if isinstance(auth_body, dict) else {}
            allowed_formats = user_info.get("allowed_output_formats", []) or []
            resolved_base = _resolve_xtream_base(server_info, self.base_url, self.prefer_https)

            # Select from provider metadata only. Opening a media URL here consumes
            # a playback connection before the user has selected anything.
            if not allowed_formats or "ts" in allowed_formats:
                stream_ext = "ts"
            elif "m3u8" in allowed_formats:
                stream_ext = "m3u8"
            else:
                stream_ext = allowed_formats[0]

            # Step 2: Determine API actions based on content type
            actions = _XTREAM_CATALOG_ACTIONS.get(self.content_type)
            if actions is None:
                raise ValueError(f"Unknown content type: {self.content_type}")
            cat_action, streams_action, url_prefix = actions

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
                logger.warning("Failed to fetch categories: %s", safe_exc(e))

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
                    sorted_channels.setdefault(cid, []).append(len(items) - 1)

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
                "max_connections": _max_connections(user_info),
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
                auth_resp = requests.get(
                    auth_url, timeout=10, headers=CLIENT_HEADERS, verify=self.verify_ssl
                )
                auth_resp.raise_for_status()
                auth_body = auth_resp.json() if auth_resp.content else {}
                server_info = (
                    auth_body.get("server_info", {}) if isinstance(auth_body, dict) else {}
                )
                self.resolved_base = _resolve_xtream_base(
                    server_info, self.base_url, self.prefer_https
                )

            # Fetch series info
            info_url = xtream_player_api_url(
                self.resolved_base,
                self.username,
                self.password,
                action="get_series_info",
                extra={"series_id": self.series_id},
            )
            info_resp = requests.get(
                info_url, timeout=15, headers=CLIENT_HEADERS, verify=self.verify_ssl
            )
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

            self.finished.emit(
                {
                    "seasons": seasons,
                    "series_info": dict(series_info) if series_info else {},
                    "resolved_base": self.resolved_base,
                }
            )

        except Exception as e:
            logger.error("Xtream series info failed: %s", safe_exc(e))
            self.error.emit(str(e))


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

            result = {"categories": categories, "contents": {}}
            # Live channels come as one list; VOD/series are fetched per category later.
            if self.content_type == "itv":
                fetchurl = stb_request_url(base, "itv", "get_all_channels")
                resp = requests.get(
                    fetchurl, headers=self.headers, timeout=10, verify=self.verify_ssl
                )
                resp.raise_for_status()
                all_channels = resp.json()["js"]["data"]
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
            fetchurl = stb_create_link_url(
                stb_portal_base(self.base_url, self.prefer_https),
                self.content_type,
                self.cmd,
                is_episode=self.is_episode,
                series=self.series_param,
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


def _handshake_failure_message(failure: Optional[str]) -> str:
    """The user's text for a failed STB handshake (provider_manager.HANDSHAKE_*)."""
    if failure == HANDSHAKE_UNREACHABLE:
        return UNREACHABLE_MESSAGE
    if failure == HANDSHAKE_UNEXPECTED:
        return UNEXPECTED_ANSWER_MESSAGE
    return STB_DEVICE_REJECTED_MESSAGE


class ProviderSetupWorker(QObject):
    """Select the configured provider (STB handshake, disk cache) without loading EPG."""

    finished = Signal(object)
    error = Signal(str)
    progress = Signal(str)

    def __init__(self, provider_manager, force_refresh: bool = False):
        super().__init__()
        self.provider_manager = provider_manager
        self.force_refresh = force_refresh

    def run(self):
        try:
            if self.provider_manager.set_current_provider(self.progress) is False:
                self.error.emit(
                    _handshake_failure_message(
                        getattr(self.provider_manager, "handshake_failure", None)
                    )
                )
                return
            if self.force_refresh:
                self.provider_manager.clear_current_provider_cache()
            self.finished.emit({"name": self.provider_manager.current_provider.get("name", "")})
        except Exception as e:
            # The text can carry portal URLs and credentials: log only the type at warning.
            logger.warning("Provider setup failed (%s)", type(e).__name__)
            logger.debug("Provider setup failure:\n%s", safe_traceback(e))
            self.error.emit(str(e))


class EpgLoadWorker(QObject):
    finished = Signal(object)
    error = Signal(str)

    def __init__(self, epg_manager, force_refresh: bool = False):
        super().__init__()
        self.epg_manager = epg_manager
        self.force_refresh = force_refresh

    def run(self):
        try:
            if self.force_refresh:
                self.epg_manager.force_refresh_current_epg()
            else:
                self.epg_manager.set_current_epg()
            self.finished.emit({})
        except Exception as e:
            logger.warning("EPG load failed (%s)", type(e).__name__)
            logger.debug("EPG load failure:\n%s", safe_traceback(e))
            self.error.emit(str(e))


class StbListWorker(QObject):
    """Run one STB ContentLoader listing synchronously inside this worker's thread."""

    finished = Signal(object)
    error = Signal(str)

    def __init__(self, **loader_kwargs):
        super().__init__()
        self.loader_kwargs = loader_kwargs

    def run(self):
        from services.content_loader import ContentLoader

        results: list = []
        errors: list = []
        loader = ContentLoader(**self.loader_kwargs)
        loader.content_loaded.connect(results.append, Qt.DirectConnection)
        loader.error.connect(errors.append, Qt.DirectConnection)
        try:
            loader.run()
        except Exception as e:
            self.error.emit(str(e))
            return
        if results:
            self.finished.emit(results[-1])
        else:
            self.error.emit(errors[-1] if errors else NO_PORTAL_DATA_MESSAGE)
