"""Provider workers: catalog loads, provider setup, EPG and STB links, for CatalogService."""

from functools import partial
import logging
import re
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple

from PySide6.QtCore import QObject, Qt, Signal

from services.catchup import (
    Answer,
    mark_stb_archives,
    overall,
    probe,
    xtream_archive_fields,
    xtream_zone_fields,
)
from services.failure_reasons import (
    DEVICE_MESSAGE,
    UNEXPECTED_ANSWER_MESSAGE,
    UNREACHABLE_MESSAGE,
)
from services.i18n import N_
from services.lazy_import import lazy_module
from services.m3u import decode_playlist, parse_m3u
from services.provider_api import (
    CLIENT_HEADERS,
    https_fallback_candidates,
    remember_http_only,
    remember_xtream_root,
    stb_archive_link_url,
    stb_create_link_url,
    stb_link_shape,
    stb_portal_base,
    stb_request_url,
    xtream_base,
    xtream_choose_resolved_base,
    xtream_elsewhere,
    xtream_https_bases,
    xtream_player_api_url,
    xtream_sign_in_bases,
    xtream_stream_url,
)
from services.provider_manager import HANDSHAKE_UNEXPECTED, HANDSHAKE_UNREACHABLE
from services.redact import safe_exc, safe_traceback

logger = logging.getLogger(__name__)
requests = lazy_module("requests")

# Sent from the worker's thread in English (a catalog message), shown with translate().
NO_PORTAL_DATA_MESSAGE = N_("The portal returned no data")
STB_DEVICE_REJECTED_MESSAGE = DEVICE_MESSAGE


def _resolve_xtream_base(server_info: dict, base_url: str) -> str:
    """Pick the panel-advertised base, falling back to the user's URL when there is none."""
    resolved_base = xtream_choose_resolved_base(server_info, base_url)
    return resolved_base or xtream_base(base_url)


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


# The Xtream workers' helpers take the worker for its account: base_url, username, password,
# verify_ssl and prefer_https. `get` is requests' or a session's, sending CLIENT_HEADERS.


# This run's resolved HTTP bases of Xtream panels, each with the HTTPS base its panel answered
# its sign-in at ("": none did).
_https_bases: Dict[str, str] = {}
_HTTPS_CONNECT_S = 5.0  # an HTTPS address that never answers does not hold a list up long


def _xtream_sign_in(get: Callable[..., Any], worker: Any) -> Tuple[Any, str, Any]:
    """Sign in to an Xtream panel (the Player API without an action): the reply's user_info,
    the base its API and streams are at, and its server_info (its time zone: catch-up). An
    address with a path whose panel is not there (a 404 or a page, not the API's JSON) is asked
    once more at its server's root, which is used from then on when it answers (a stray path
    2.2.0 and older cut off). With HTTPS preferred, an HTTP base becomes the HTTPS one its
    panel answers at (`_xtream_https_base`)."""
    user_info, base, server_info = _xtream_sign_in_at(get, worker)
    if worker.prefer_https and base.startswith("http://"):
        base = _xtream_https_base(get, worker, base, server_info) or base
    return user_info, base, server_info


def _xtream_https_base(get: Callable[..., Any], worker: Any, base: str, server_info: Any) -> str:
    """The HTTPS base the panel signed in at the HTTP `base` answers its sign-in at
    (`xtream_https_bases`), asked once a run; "" when it answers at none, and its host is
    asked over HTTP from then on (the streams too)."""
    if base in _https_bases:
        return _https_bases[base]
    secure = ""
    for candidate in xtream_https_bases(base, server_info):
        url = xtream_player_api_url(candidate, worker.username, worker.password)
        try:
            response = get(url, timeout=(_HTTPS_CONNECT_S, 10), verify=worker.verify_ssl)
            try:
                body = response.json() if response.ok and response.content else None
            finally:
                response.close()
        except requests.RequestException, ValueError:
            continue
        if isinstance(body, dict) and isinstance(body.get("user_info"), dict):
            secure = candidate
            break
    _https_bases[base] = secure
    if secure:
        logger.info("The Xtream panel answers over HTTPS: its lists and streams come over HTTPS.")
    else:
        remember_http_only(base)
        logger.info("The Xtream panel does not answer over HTTPS: it is asked over HTTP.")
    return secure


class _XtreamAccount(NamedTuple):
    """An Xtream account as `_xtream_sign_in` reads it off a worker."""

    base_url: str
    username: str
    password: str
    verify_ssl: bool
    prefer_https: bool = True


def xtream_answers_https(base_url: str, username: str, password: str, verify_ssl: bool) -> bool:
    """Whether the Xtream panel at `base_url` gives its lists and streams over HTTPS: its
    address is HTTPS, or the panel answers its sign-in over HTTPS (Settings' Verify Provider
    says when it does not, 2.7.1). Blocking: run it on a worker."""
    account = _XtreamAccount(base_url, username, password, verify_ssl)
    with requests.Session() as session:
        session.headers.update(CLIENT_HEADERS)
        return _xtream_sign_in(session.get, account)[1].startswith("https://")


def _xtream_sign_in_at(get: Callable[..., Any], worker: Any) -> Tuple[Any, str, Any]:
    """`_xtream_sign_in` at the address as saved."""
    bases = xtream_sign_in_bases(worker.base_url)
    for at, server in enumerate(bases):
        url = xtream_player_api_url(server, worker.username, worker.password)
        response = get(url, timeout=10, verify=worker.verify_ssl)
        if at + 1 < len(bases) and xtream_elsewhere(response):
            continue  # the next is the root: no further one
        response.raise_for_status()
        body = response.json() if response.content else {}
        if not isinstance(body, dict):
            body = {}
        if at:
            remember_xtream_root(worker.base_url, server)
        server_info = body.get("server_info", {})
        resolved = _resolve_xtream_base(server_info, server)
        return body.get("user_info", {}), resolved, server_info
    raise AssertionError("unreachable: the last base always answers or raises")


def _xtream_categories(
    get: Callable[..., Any], worker: Any, base: str, action: str
) -> List[Dict[str, str]]:
    """A catalog's categories, "All" first: only it when the panel lists none or fails."""
    # The loaders' labels stay English in the lists (catalog.shown_label shows them).
    categories = [{"id": "*", "title": N_("All")}]
    url = xtream_player_api_url(base, worker.username, worker.password, action=action)
    try:
        response = get(url, timeout=10, verify=worker.verify_ssl)
        if response.ok and response.content:
            for c in response.json() or []:
                name = c.get("category_name") or N_("Unknown")
                categories.append({"id": str(c.get("category_id")), "title": name})
    except Exception as e:
        logger.warning("Failed to fetch categories: %s", safe_exc(e))
    return categories


def _xtream_item(
    stream: Dict[str, Any], content_type: str, address: Callable[[Any, str], str], ext: str, n: int
) -> Optional[Dict[str, Any]]:
    """A catalog item of a Player API stream (number `n` when it has none); None without an
    id. `address(id, ext)` is its stream's: a channel plays as `ext`, a film as its own
    container; a series has none (its episodes do, once its info is fetched)."""
    stream_id = stream.get("stream_id") or stream.get("series_id")
    if not stream_id:
        return None
    category_ids = stream.get("category_ids")
    if isinstance(category_ids, list) and category_ids:
        cid = str(category_ids[0])
    else:
        cid = str(stream.get("category_id") or "None")
    item = {
        "id": stream_id,
        "number": str(stream.get("num") or n),
        "name": stream.get("name") or f"Stream {stream_id}",
        "logo": stream.get("stream_icon") or stream.get("cover") or "",
        "tv_genre_id": cid,  # STB's field name
        "cmd": "",
    }
    if content_type == "itv":
        item["cmd"] = address(stream_id, ext)
        item["xmltv_id"] = stream.get("epg_channel_id") or ""
        item.update(xtream_archive_fields(stream))  # only on a channel with an archive
    elif content_type == "vod":
        ext = stream.get("container_extension") or ext
        item["cmd"] = address(stream_id, ext)
        item["director"] = stream.get("director") or ""
        item["description"] = stream.get("plot") or ""
        item["rating"] = stream.get("rating") or ""
        item["year"] = stream.get("releasedate") or ""
        item["container_extension"] = ext
    elif content_type == "series":
        item["plot"] = stream.get("plot") or ""
        item["rating"] = stream.get("rating") or ""
        item["year"] = stream.get("year") or ""
    if content_type != "itv":
        tmdb = _tmdb_of(stream)  # finds its subtitles on OpenSubtitles
        if tmdb:
            item["tmdb"] = tmdb
        # When the panel added it (a series: last changed), a unix time: the Library's New
        # shelf and its "Recently added" order, as STB portals' titles have them (2.8).
        added = str(stream.get("added" if content_type == "vod" else "last_modified") or "")
        if added.strip().isdigit() and int(added) > 0:
            item["added"] = added.strip()
    return item


def _tmdb_of(info: Any) -> str:
    """A film's or a series' TMDB id as an Xtream panel lists it ("tmdb", "tmdb_id"; "" when
    it lists none)."""
    if not isinstance(info, dict):
        return ""
    for key in ("tmdb", "tmdb_id"):
        value = str(info.get(key) or "").strip()
        if value.isdigit() and int(value) > 0:
            return value
    return ""


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
            user_info, resolved_base, server_info = _xtream_sign_in(session.get, self)
            # Select from provider metadata only. Opening a media URL here consumes
            # a playback connection before the user has selected anything.
            allowed_formats = user_info.get("allowed_output_formats", []) or []
            if not allowed_formats or "ts" in allowed_formats:
                stream_ext = "ts"
            elif "m3u8" in allowed_formats:
                stream_ext = "m3u8"
            else:
                stream_ext = allowed_formats[0]

            actions = _XTREAM_CATALOG_ACTIONS.get(self.content_type)
            if actions is None:
                raise ValueError(f"Unknown content type: {self.content_type}")
            cat_action, streams_action, kind = actions
            categories = _xtream_categories(session.get, self, resolved_base, cat_action)

            streams_url = xtream_player_api_url(
                resolved_base, self.username, self.password, action=streams_action
            )
            streams_resp = session.get(streams_url, timeout=15, verify=self.verify_ssl)
            streams_resp.raise_for_status()
            streams = streams_resp.json() or []

            address = partial(xtream_stream_url, resolved_base, kind, self.username, self.password)
            items: List[Dict] = []
            sorted_channels: Dict[str, List[int]] = {}
            for s in streams:
                try:
                    item = _xtream_item(s, self.content_type, address, stream_ext, len(items) + 1)
                except Exception as e:
                    logger.warning("Failed to process stream: %s", e)
                    continue
                if item is not None:
                    items.append(item)
                    sorted_channels.setdefault(item["tv_genre_id"], []).append(len(items) - 1)

            # Sort channels within each category
            for cat_id in sorted_channels:
                sorted_channels[cat_id].sort(
                    key=lambda x: int(items[x]["number"]) if items[x]["number"].isdigit() else 0
                )

            # Add "None" category if there are uncategorized items
            if "None" in sorted_channels:
                categories.append({"id": "None", "title": N_("Unknown Category")})

            result = {
                "categories": categories,
                "contents": items,
                "sorted_channels": sorted_channels,
                "resolved_base": resolved_base,
                "stream_base": resolved_base,
                "stream_ext": stream_ext,
                "max_connections": _max_connections(user_info),
                **xtream_zone_fields(server_info),  # the panel's time zone (catch-up)
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
                get = partial(requests.get, headers=CLIENT_HEADERS)
                _user_info, self.resolved_base, _server = _xtream_sign_in(get, self)

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
            # The series' TMDB id on each season: its episodes' subtitles are found by it.
            series_tmdb = _tmdb_of(series_info)
            series_ids = {"series_tmdb": series_tmdb} if series_tmdb else {}
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
                        **series_ids,
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
            base = stb_portal_base(self.base_url, self.prefer_https)

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
                all_channels = self._all_channels(fetchurl)
                mark_stb_archives(all_channels)  # catch-up: the channels marked archived
                sorted_channels: Dict[str, List[int]] = {}
                for index, channel in enumerate(all_channels):
                    category_id = str(channel.get("tv_genre_id"))
                    sorted_channels.setdefault(category_id, []).append(index)
                if "None" in sorted_channels:
                    categories.append({"id": "None", "title": N_("Unknown Category")})
                result["contents"] = all_channels
                result["sorted_channels"] = sorted_channels
            self.finished.emit(result)
        except Exception as e:
            self.error.emit(str(e))

    def _all_channels(self, fetchurl: str) -> list:
        """The portal's whole live list: one answer that can be large (tens of thousands of
        channels), so it gets `ALL_CHANNELS_TIMEOUT_S` and, after a timeout, a second try
        (2.8: a slow portal's list failed at 10 s)."""
        for attempt in (1, 2):
            try:
                resp = requests.get(
                    fetchurl,
                    headers=self.headers,
                    timeout=ALL_CHANNELS_TIMEOUT_S,
                    verify=self.verify_ssl,
                )
            except requests.Timeout:
                if attempt == 2:
                    raise
                logger.info("The portal's live list timed out; asking again.")
                continue
            resp.raise_for_status()
            return list(resp.json()["js"]["data"])
        return []


ALL_CHANNELS_TIMEOUT_S = 30

# The `js.error` a portal answers a tv_archive create_link with when the programme has no
# recording; any other ("limit", "link_fault", …) is the portal's own trouble.
_NO_RECORDING_ERRORS = frozenset({"nothing_to_play"})
# Raised (and so the worker's error) when the portal answered a recording's link with neither
# a link nor "no recording": it says nothing of the recording.
ARCHIVE_LINK_ERROR = "The portal answered the archive link with an error"


def stb_archive_link(answer: Any) -> str:
    """The link in an STB portal's answer to a tv_archive create_link: the last word of its
    `js.cmd` ("ffrt http://…", or the address alone), as a channel's; "" when the portal says
    the programme has no recording (an empty `cmd`, alone or with the `error`
    "nothing_to_play"). Any other answer (another `error`, such as "limit"; no `js` dict, as
    a portal without an archive to ask answers; a `cmd` that is no address, the
    "/media/….mpg" asked for, unmade) raises ValueError: it says nothing of the recording."""
    js = answer.get("js") if isinstance(answer, dict) else None
    if not isinstance(js, dict):
        raise ValueError(ARCHIVE_LINK_ERROR)
    error = js.get("error")
    error = error.strip() if isinstance(error, str) else ""
    cmd = js.get("cmd")
    cmd = cmd.strip() if isinstance(cmd, str) else ""
    if error and error not in _NO_RECORDING_ERRORS:
        logger.info("The portal made no archive link: %s", error[:40])
        raise ValueError(ARCHIVE_LINK_ERROR)
    if error or not cmd:
        return ""  # no recording
    link = cmd.split(" ")[-1]
    if "://" not in link:
        raise ValueError(ARCHIVE_LINK_ERROR)
    return link


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
        programme: str = "",
    ):
        """`programme`: the id of a programme in the portal's guide: the link made is its
        recording's in the portal's archive (catch-up; `content_type` and `cmd` unused)."""
        super().__init__()
        self.base_url = base_url
        self.headers = headers
        self.content_type = content_type
        self.cmd = cmd
        self.is_episode = is_episode
        self.series_param = series_param
        self.verify_ssl = verify_ssl
        self.prefer_https = prefer_https
        self.programme = programme

    def run(self):
        try:
            base = stb_portal_base(self.base_url, self.prefer_https)
            if self.programme:
                fetchurl = stb_archive_link_url(base, self.programme)
            else:
                fetchurl = stb_create_link_url(
                    base,
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
            if self.programme:
                link = stb_archive_link(result)  # "": no recording (EMPTY_LINK_MESSAGE)
            else:
                link = result["js"]["cmd"].split(" ")[-1]
            if link:  # 2.8.1: what kind of link a portal gives, should its streams fail
                logger.info("The portal's link: %s.", stb_link_shape(link, self.base_url))
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
    shown = Signal()  # an outdated saved guide is up while the fresh one downloads

    def __init__(self, epg_manager, force_refresh: bool = False):
        super().__init__()
        self.epg_manager = epg_manager
        self.force_refresh = force_refresh

    def run(self):
        try:
            if self.force_refresh:
                self.epg_manager.force_refresh_current_epg()
            else:
                self.epg_manager.set_current_epg(on_shown=self.shown.emit)
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


class ArchiveProbeWorker(QObject):
    """Probe a channel's archive addresses in turn (catch-up, 2.2): `finished` carries
    (the index of the first that plays, or -1; the forms' answer together, `catchup.overall`).

    A refusal stops there (it may be the provider's connection limit: asking the next form
    would only be refused too); a "nothing there" or no clear answer tries the next form, and
    "nothing there" is the answer only when every form said so.
    """

    finished = Signal(object)
    error = Signal(str)

    def __init__(self, urls: List[str], headers: Dict[str, str], verify_ssl: bool = True):
        super().__init__()
        self.urls = list(urls)
        self.headers = dict(headers)
        self.verify_ssl = verify_ssl

    def run(self):
        answers: List[Answer] = []
        for index, url in enumerate(self.urls):
            answer = probe(url, headers=self.headers, verify=self.verify_ssl)
            if answer is Answer.PLAYS:
                self.finished.emit((index, answer.value))
                return
            answers.append(answer)
            if answer is Answer.REFUSED:
                break
        self.finished.emit((-1, overall(answers).value))
