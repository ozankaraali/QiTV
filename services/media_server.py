"""Jellyfin and Emby servers as providers (2.8): their films and series in the Library.

A media server is a provider of type JELLYFIN or EMBY: its address and user, and the access
token its sign-in gave (`token`, `user_id`, and the `device_id` QiTV signed in as; the password
is not kept). Its libraries of films, series and both are the Library's genres; the titles come
as the other providers' do (`title_item`), with the server's pictures, details, TMDB ids, the
date each was added and where the user stopped in it (`server_position_ms`).

Both servers speak much the same API. Jellyfin 10.9 and later moved some routes (`/Items`,
`/UserViews`); older ones and Emby answer the classic routes (`/Users/{id}/Items`,
`/Users/{id}/Views`): Jellyfin is asked the new routes first and the classic ones when a route
is missing, Emby the classic ones.

A title plays as its file is (`stream_url`: never converted), in the version the setting picks
(`choose_source`: no 4K version below a 4K screen while there is another, unless chosen).
Converting on the server (`converted_url`, HLS) happens only when Settings caps the bitrate and
the file exceeds it.
Where the user is in it is told back (`report`), so the server's own "continue watching" and
watched marks follow. The client names QiTV and the system, never the computer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import platform
from typing import Any, Iterable, Optional
from urllib.parse import parse_qs, urlencode, urlsplit
import uuid

from services.catalog import ALL_GROUP_ID
from services.i18n import N_
from services.lazy_import import lazy_module
from services.provider_types import EMBY, JELLYFIN, MEDIA_SERVERS, is_media_server  # noqa: F401

logger = logging.getLogger(__name__)
requests = lazy_module("requests")  # the window shows before the network libraries load

TYPES = MEDIA_SERVERS
TICKS_PER_MS = 10_000
PAGE = 2_000  # titles a request (a library of 50,000 comes in 25)
ID_PAGE = 10_000  # ids a request (no details: a library's order and members)
# A later look asks what was saved since the last one, from this much earlier (the server's
# clock and this computer's may differ), and loads every title again after a week (what a
# clock far off missed).
SINCE_MARGIN_S = 600
WHOLE_AFTER_S = 7 * 24 * 3600
IDS_A_REQUEST = 100  # titles asked by their ids at once
TIMEOUT_S = 30
# The Library's genre of the titles the user has started (where the server says they stopped).
CONTINUE_ID = "@server-continue"
# i18n: a Library genre of a Jellyfin or Emby server: the titles you started watching there
CONTINUE_TITLE = N_("Continue watching")
FIELDS = "Overview,Genres,ProductionYear,CommunityRating,RunTimeTicks,ProviderIds,DateCreated"
OVERVIEW_CHARS = 400  # a description in the list (the details strip shows a few lines)
_LIBRARY_TYPES = {"movies", "tvshows", "mixed", "homevideos", ""}
# 4K versions (Settings): on a 4K screen or when there is no other (default), always, never.
# A 4K version plays as it is below a 4K screen too: the computer scales it down.
FOUR_K_SCREEN, FOUR_K_ALWAYS, FOUR_K_NEVER = "screen", "always", "never"
FOUR_K_CHOICES = (FOUR_K_SCREEN, FOUR_K_ALWAYS, FOUR_K_NEVER)
ONLY_4K = "only_4k"  # choose_source's reason: the title has only 4K versions, and Never
NO_SOURCE = "none"
# i18n: a title of a Jellyfin or Emby server has only a 4K version, and Settings plays no 4K
# version (Never)
ONLY_4K_TEXT = N_(
    "This title is only in 4K. To play it here, choose another 4K versions setting than Never."
)
# i18n: a Jellyfin or Emby server refused QiTV's access (its sign-in ended)
SIGN_IN_AGAIN = N_("The server asks to sign in again: Settings → Providers → Verify Provider.")


def new_device_id() -> str:
    return uuid.uuid4().hex


def _system(system: Optional[str] = None) -> str:
    name = system or platform.system()
    return {"Darwin": "macOS", "Windows": "Windows"}.get(name, "Linux")


def auth_header(device_id: str, version: str, token: str = "", system: Optional[str] = None) -> str:
    """The server's client header: QiTV and the system, never the computer's name."""
    header = (
        f'MediaBrowser Client="QiTV", Device="QiTV ({_system(system)})", '
        f'DeviceId="{device_id}", Version="{version}"'
    )
    return header + (f', Token="{token}"' if token else "")


def normalized_base(url: str) -> str:
    """A server's address without its trailing slash (http:// when it names no scheme)."""
    url = str(url or "").strip().rstrip("/")
    if url and "://" not in url:
        url = "http://" + url
    return url


class MediaServerError(Exception):
    pass


class SignInNeeded(MediaServerError):
    """The server refused the token or the password (401)."""


class MediaServerClient:
    """One server, one user. Blocking: used on worker threads only."""

    def __init__(
        self,
        base_url: str,
        kind: str,
        *,
        device_id: str,
        version: str,
        token: str = "",
        user_id: str = "",
        verify: bool = True,
        session: Any = None,
        timeout: float = TIMEOUT_S,
    ) -> None:
        self.base = normalized_base(base_url)
        self.kind = str(kind or JELLYFIN).upper()
        self.device_id = device_id
        self.version = version
        self.token = token
        self.user_id = user_id
        self.verify = verify
        self.timeout = timeout
        self._http = session if session is not None else requests.Session()
        self._set_auth()

    def _set_auth(self) -> None:
        header = auth_header(self.device_id, self.version, self.token)
        self._http.headers.update(
            {"Authorization": header, "X-Emby-Authorization": header, "Accept": "application/json"}
        )
        if self.token:
            self._http.headers["X-Emby-Token"] = self.token

    def close(self) -> None:
        self._http.close()

    # ---- requests ---------------------------------------------------------------------------

    def _request(
        self,
        method: str,
        paths: Iterable[str],
        params: Optional[dict] = None,
        body: Optional[dict] = None,
    ) -> Any:
        """Ask the first of `paths` that exists (404: the next); 401 raises SignInNeeded."""
        paths = list(paths)
        for index, path in enumerate(paths):
            response = self._http.request(
                method,
                self.base + path,
                params=params,
                json=body,
                timeout=self.timeout,
                verify=self.verify,
            )
            if response.status_code == 404 and index < len(paths) - 1:
                continue
            if response.status_code == 401:
                raise SignInNeeded("401")
            if response.status_code >= 400:  # never the address: it may carry the token
                raise MediaServerError(f"The server answered {response.status_code}")
            return response.json() if response.content else None
        return None

    def _user_routes(self, modern: str, classic: str) -> list[str]:
        if self.kind == JELLYFIN:
            return [modern, classic]
        return [classic]

    # ---- the user ---------------------------------------------------------------------------

    def sign_in(self, username: str, password: str) -> tuple[str, str]:
        """(token, user id) of a sign-in; the client uses them from then on."""
        answer = self._request(
            "POST", ["/Users/AuthenticateByName"], body={"Username": username, "Pw": password}
        )
        token = str((answer or {}).get("AccessToken") or "")
        user_id = str(((answer or {}).get("User") or {}).get("Id") or "")
        if not token or not user_id:
            raise MediaServerError("The server gave no access")
        self.token, self.user_id = token, user_id
        self._set_auth()
        return token, user_id

    def sign_out(self) -> None:
        self._request("POST", ["/Sessions/Logout"])

    # ---- the libraries ----------------------------------------------------------------------

    def libraries(self) -> list[dict]:
        """The user's libraries that hold films or series: {id, name, type}."""
        answer = self._request(
            "GET",
            self._user_routes("/UserViews", f"/Users/{self.user_id}/Views"),
            params={"UserId": self.user_id},
        )
        out = []
        for view in (answer or {}).get("Items") or []:
            kind = str(view.get("CollectionType") or "")
            if kind in _LIBRARY_TYPES and view.get("Id"):
                out.append(
                    {"id": str(view["Id"]), "name": str(view.get("Name") or ""), "type": kind}
                )
        return out

    def items(
        self,
        item_type: str,
        parent_id: str,
        page: int = PAGE,
        *,
        since: str = "",
        for_user: bool = False,
        ids: Iterable[str] = (),
    ) -> list[dict]:
        """Every `item_type` ("Movie", "Series") title of the library `parent_id`; with
        `since` (2.8, a refresh), only those saved since (`for_user`: whose user data, a
        position or a watched mark, was); with `ids`, only those."""
        params: dict[str, Any] = {
            "Fields": FIELDS,
            "EnableImageTypes": "Primary",
            "ImageTypeLimit": 1,
        }
        if since:
            params["MinDateLastSavedForUser" if for_user else "MinDateLastSaved"] = since
        wanted = ",".join(ids)
        if wanted:
            params["Ids"] = wanted
        return self._paged(item_type, parent_id, params, page)

    def count(self, item_type: str, parent_id: str) -> int:
        """How many `item_type` titles the library holds (2.8: a refresh compares it)."""
        answer = self._request(
            "GET", self._items_routes(), params=self._query(item_type, parent_id, Limit=0)
        )
        return int((answer or {}).get("TotalRecordCount") or 0)

    def item_ids(self, item_type: str, parent_id: str, page: int = ID_PAGE) -> list[str]:
        """The library's `item_type` titles' ids in its order, without their details (2.8:
        a refresh learns which came and which went)."""
        params = {"EnableImages": "false", "EnableUserData": "false", "Fields": ""}
        found = self._paged(item_type, parent_id, params, page)
        return [str(raw.get("Id")) for raw in found if raw.get("Id")]

    def _items_routes(self) -> list[str]:
        return self._user_routes("/Items", f"/Users/{self.user_id}/Items")

    def _query(self, item_type: str, parent_id: str, **more: Any) -> dict[str, Any]:
        return {
            "UserId": self.user_id,  # one spelling: Emby fails on both (Jellyfin takes either)
            "ParentId": parent_id,
            "IncludeItemTypes": item_type,
            "Recursive": "true",
            **more,
        }

    def _paged(
        self, item_type: str, parent_id: str, params: dict[str, Any], page: int
    ) -> list[dict]:
        routes = self._items_routes()
        found: list[dict] = []
        start = 0
        while True:
            asked = self._query(
                item_type, parent_id, SortBy="SortName", StartIndex=start, Limit=page, **params
            )
            answer = self._request("GET", routes, params=asked) or {}
            batch = answer.get("Items") or []
            found.extend(batch)
            start += len(batch)
            total = int(answer.get("TotalRecordCount") or 0)
            if not batch or start >= total:
                return found

    def seasons_and_episodes(self, series_id: str) -> tuple[list[dict], list[dict]]:
        params = {"UserId": self.user_id, "Fields": FIELDS}
        seasons = self._request("GET", [f"/Shows/{series_id}/Seasons"], params=params) or {}
        episodes = self._request("GET", [f"/Shows/{series_id}/Episodes"], params=params) or {}
        return list(seasons.get("Items") or []), list(episodes.get("Items") or [])

    def media_sources(self, item_id: str) -> list[dict]:
        params = {"UserId": self.user_id}
        answer = self._request("GET", [f"/Items/{item_id}/PlaybackInfo"], params=params) or {}
        return list(answer.get("MediaSources") or [])

    # ---- where the user is ------------------------------------------------------------------

    def report(self, event: str, ref: "StreamRef", position_ms: int, paused: bool = False) -> None:
        """Tell the server a play started ("start"), where it is ("progress"), or that it
        stopped ("stopped")."""
        path = {
            "start": "/Sessions/Playing",
            "progress": "/Sessions/Playing/Progress",
            "stopped": "/Sessions/Playing/Stopped",
        }[event]
        body = {
            "ItemId": ref.item_id,
            "MediaSourceId": ref.source_id,
            "PlaySessionId": ref.play_session,
            "PositionTicks": max(0, int(position_ms)) * TICKS_PER_MS,
            "IsPaused": paused,
            "PlayMethod": "Transcode" if ref.converted else "DirectStream",
            "CanSeek": True,
        }
        self._request("POST", [path], body=body)


# ---- titles ---------------------------------------------------------------------------------


def image_url(base: str, item_id: str, tag: str, kind: str = "Primary") -> str:
    if not item_id or not tag:
        return ""
    return f"{base}/Items/{item_id}/Images/{kind}?maxHeight=360&tag={tag}"


def _unix(date: Any) -> Optional[str]:
    """A server's date ("2026-10-20T08:00:00.0000000Z", UTC) as a unix time, else None."""
    text = str(date or "").strip()
    try:
        when = datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    seconds = int(when.timestamp())
    return str(seconds) if seconds > 0 else None


def _shortened(text: str) -> str:
    """A description as the Library keeps it: OVERVIEW_CHARS at most (a server's 50,000
    titles stay a list of their size), cut at a word."""
    if len(text) <= OVERVIEW_CHARS:
        return text
    return text[:OVERVIEW_CHARS].rsplit(" ", 1)[0].rstrip(" ,.;:") + "…"


def _image(raw: dict, base: str) -> str:
    tags = raw.get("ImageTags") or {}
    return image_url(base, str(raw.get("Id") or ""), str(tags.get("Primary") or ""))


def title_item(raw: dict, base: str, library_id: str) -> dict:
    """A film or a series of the server as a Library title."""
    overview = _shortened(str(raw.get("Overview") or ""))
    item: dict[str, Any] = {
        "id": str(raw.get("Id") or ""),
        "name": str(raw.get("Name") or ""),
        "logo": _image(raw, base),
        "tv_genre_id": library_id,
        "cmd": "",
        "description": overview,
        "plot": overview,
        "year": str(raw.get("ProductionYear") or ""),
        "rating": str(raw.get("CommunityRating") or ""),
        "genre": ", ".join(str(g) for g in raw.get("Genres") or []),
    }
    ticks = int(raw.get("RunTimeTicks") or 0)
    if ticks > 0:
        item["duration_secs"] = ticks // (TICKS_PER_MS * 1000)
    tmdb = str((raw.get("ProviderIds") or {}).get("Tmdb") or "")
    if tmdb.isdigit():
        item["tmdb"] = tmdb
    added = _unix(raw.get("DateCreated"))
    if added:
        item["added"] = added
    position = int((raw.get("UserData") or {}).get("PlaybackPositionTicks") or 0)
    if position > 0:
        item["server_position_ms"] = position // TICKS_PER_MS
    container = str(raw.get("Container") or "").split(",")[0]
    if container:
        item["container_extension"] = container
    return item


def section_payload(libraries: list[dict], titles: dict[str, list[dict]], base: str) -> dict:
    """The Library's section of a server: All, Continue watching (titles started there), then
    each library, every title once (a title in two libraries is listed in both)."""
    ordered: dict[str, list[dict]] = {}
    made: dict[str, dict] = {}
    for library in libraries:
        listed = ordered.setdefault(library["id"], [])
        for raw in titles.get(library["id"]) or []:
            item_id = str(raw.get("Id") or "")
            if item_id:
                if item_id not in made:
                    made[item_id] = title_item(raw, base, library["id"])
                listed.append(made[item_id])
    return _payload(libraries, ordered)


def _payload(libraries: list[dict], ordered: dict[str, list[dict]]) -> dict:
    """`section_payload` of Library titles already made, in each library's order."""
    items: list[dict] = []
    sorted_channels: dict[str, list[int]] = {}
    seen: dict[str, int] = {}
    for library in libraries:
        for item in ordered.get(library["id"]) or []:
            item_id = str(item.get("id") or "")
            if not item_id:
                continue
            index = seen.get(item_id)
            if index is None:
                index = seen[item_id] = len(items)
                items.append(item)
                if item.get("server_position_ms"):
                    sorted_channels.setdefault(CONTINUE_ID, []).append(index)
            sorted_channels.setdefault(library["id"], []).append(index)
    categories = [{"id": ALL_GROUP_ID, "title": N_("All")}]
    if CONTINUE_ID in sorted_channels:
        categories.append({"id": CONTINUE_ID, "title": CONTINUE_TITLE})
    categories += [
        {"id": lib["id"], "title": lib["name"]} for lib in libraries if lib["id"] in sorted_channels
    ]
    return {"categories": categories, "contents": items, "sorted_channels": sorted_channels}


def server_time(when: float) -> str:
    """A moment as a server's date filters take it (UTC)."""
    return datetime.fromtimestamp(when, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def saved_summary(payload: Optional[dict]) -> Optional[dict]:
    """What a refresh needs of a saved section (2.8): when it was last looked at (`since`, the
    server's date; `full`, when it was loaded whole) and each library's titles' ids in order.
    None: it was never looked at so (an older list): it loads whole."""
    if not isinstance(payload, dict):
        return None
    since, full = payload.get("server_since"), payload.get("server_full")
    if not isinstance(since, str) or not isinstance(full, (int, float)):
        return None
    contents = payload.get("contents") or []
    members: dict[str, list[str]] = {}
    for key, indices in (payload.get("sorted_channels") or {}).items():
        if key in (ALL_GROUP_ID, CONTINUE_ID):
            continue
        members[key] = [str(contents[i].get("id")) for i in indices if 0 <= i < len(contents)]
    return {"since": since, "full": float(full), "members": members}


def merged_payload(saved: Optional[dict], delta: dict) -> Optional[dict]:
    """A refresh's answer (`delta`: the libraries, each one's titles' ids in order, the titles
    that changed or came, and the new look's date) merged into the saved section, by the
    server's own ids (never by name: two titles alike stay two). None without a saved list."""
    if not isinstance(saved, dict):
        return None
    made = {str(item.get("id")): item for item in saved.get("contents") or []}
    made.update(delta.get("changed") or {})
    ordered = {
        library: [made[i] for i in ids if i in made]
        for library, ids in (delta.get("order") or {}).items()
    }
    payload = _payload(list(delta.get("libraries") or []), ordered)
    payload["server_since"] = delta.get("server_since")
    payload["server_full"] = saved.get("server_full")
    return payload


def episode_item(raw: dict, base: str) -> dict:
    number = raw.get("IndexNumber")
    season = raw.get("ParentIndexNumber")
    name = str(raw.get("Name") or (f"Episode {number}" if number is not None else ""))
    item = title_item(raw, base, "")
    item.update(
        {
            "number": str(number) if number is not None else "",
            "episode_num": str(number) if number is not None else "",
            "ename": name,
            "name": name,
            "season": str(season) if season is not None else "",
            "series_id": str(raw.get("SeriesId") or ""),
            "season_id": str(raw.get("SeasonId") or ""),
        }
    )
    return item


def seasons_payload(seasons: list[dict], episodes: list[dict], base: str) -> dict:
    """A series' seasons, each with its episodes in order (as Xtream's come)."""
    by_season: dict[str, list[dict]] = {}
    for raw in episodes:
        by_season.setdefault(str(raw.get("SeasonId") or ""), []).append(raw)
    out: list[dict[str, Any]] = []
    for raw in seasons:
        season_id = str(raw.get("Id") or "")
        listed = sorted(by_season.get(season_id, []), key=lambda e: int(e.get("IndexNumber") or 0))
        number = raw.get("IndexNumber")
        season_number = str(number) if number is not None else ""
        episodes = [episode_item(e, base) for e in listed]
        for episode in episodes:  # an episode without its own number has its season's
            episode["season"] = episode["season"] or season_number
        out.append(
            {
                "id": season_id,
                "name": str(raw.get("Name") or ""),
                "season_number": season_number,
                "cover": _image(raw, base),
                "overview": str(raw.get("Overview") or ""),
                "episode_count": str(len(listed)),
                "episodes": episodes,
            }
        )
    return {"seasons": out}


# ---- versions and streams -----------------------------------------------------------------


def _video(source: dict) -> dict:
    return next((s for s in source.get("MediaStreams") or [] if s.get("Type") == "Video"), {})


def is_4k(source: dict) -> bool:
    video = _video(source)
    return int(video.get("Width") or 0) >= 3200 or int(video.get("Height") or 0) >= 2000


def choose_source(
    sources: list[dict], *, screen_4k: bool, setting: str
) -> tuple[Optional[dict], Optional[str]]:
    """The version to play, or None and why (ONLY_4K, NO_SOURCE)."""
    playable = [s for s in sources if s.get("Id")]
    if not playable:
        return None, NO_SOURCE
    small = [s for s in playable if not is_4k(s)]
    large = [s for s in playable if is_4k(s)]
    if setting == FOUR_K_NEVER:
        pool = small
    elif setting == FOUR_K_ALWAYS or screen_4k:
        pool = large or small
    else:  # below a 4K screen: the other version, or the 4K one as it is when it's the only one
        pool = small or large
    if not pool:
        return None, ONLY_4K
    best = max(
        pool,
        key=lambda s: (int(_video(s).get("Height") or 0), int(s.get("Bitrate") or 0)),
    )
    return best, None


@dataclass(frozen=True)
class Quality:
    """A choice of the player's quality menu (2.8): a version as its file is (`source`, its id;
    "" for the one the settings pick), or the server's conversion of it to at most `height`
    lines and `bitrate` bits a second."""

    source: str = ""
    height: int = 0
    bitrate: int = 0

    @property
    def converted(self) -> bool:
        return self.height > 0 or self.bitrate > 0

    def same_as(self, other: "Quality") -> bool:
        """The same choice: a conversion by its size and rate (of whatever version), a version
        as it is by its id."""
        if self.converted or other.converted:
            return (self.converted, self.height, self.bitrate) == (
                other.converted,
                other.height,
                other.bitrate,
            )
        return self.source == other.source


# The conversions the quality menu offers, best first (the server converts: its processor).
CONVERSIONS = (
    Quality(height=1080, bitrate=8_000_000),
    Quality(height=720, bitrate=4_000_000),
    Quality(height=480, bitrate=2_000_000),
)


def within(source: dict, quality: Quality) -> bool:
    """A version already as small as a conversion would make it (2.8: one carried to a
    server's next titles): its size known and no taller, and its rate no higher (when both
    are known). Converting it would use the server's processor for nothing."""
    height = int(_video(source).get("Height") or 0)
    if height <= 0 or (quality.height and height > quality.height):
        return False
    rate = int(source.get("Bitrate") or 0)
    return not (quality.bitrate and rate and rate > quality.bitrate)


def version_of(source: dict) -> dict:
    """A version as the quality menu lists it: its id, height, bitrate, whether it is 4K, and
    the server's name for it."""
    return {
        "id": str(source.get("Id") or ""),
        "height": int(_video(source).get("Height") or 0),
        "bitrate": int(source.get("Bitrate") or 0),
        "uhd": is_4k(source),
        "name": str(source.get("Name") or "") if source.get("Name") else "",
    }


def _container(source: dict) -> str:
    return str(source.get("Container") or "").split(",")[0].strip()


def stream_url(base: str, item_id: str, source: dict, token: str, play_session: str) -> str:
    """The file itself, as it is (`static`): never converted."""
    container = _container(source)
    path = f"/Videos/{item_id}/stream" + (f".{container}" if container else "")
    query = urlencode(
        {
            "static": "true",
            "MediaSourceId": source.get("Id") or "",
            "PlaySessionId": play_session,
            "api_key": token,
        }
    )
    return f"{base}{path}?{query}"


def converted_url(
    base: str,
    item_id: str,
    source: dict,
    token: str,
    play_session: str,
    max_bitrate: int,
    device_id: str,
    max_height: int = 0,
) -> str:
    """The server's own conversion (HLS, H.264 and AAC), capped at `max_bitrate` (and at
    `max_height` lines when given: 2.8, the quality menu)."""
    params: dict[str, object] = {
        "MediaSourceId": source.get("Id") or "",
        "PlaySessionId": play_session,
        "DeviceId": device_id,
        "VideoCodec": "h264",
        "AudioCodec": "aac",
        "MaxStreamingBitrate": int(max_bitrate),
        "SegmentContainer": "ts",
    }
    if max_height > 0:
        params["MaxHeight"] = int(max_height)
    params["api_key"] = token
    query = urlencode(params)
    return f"{base}/Videos/{item_id}/master.m3u8?{query}"


def server_resume(item: dict) -> Optional[int]:
    """Where a media server says the user stopped in `item` (ms; None: nowhere)."""
    try:
        position = int(item.get("server_position_ms") or 0)
    except TypeError, ValueError:
        return None
    return position if position > 0 else None


_LOOPBACK = ("localhost", "127.0.0.1", "::1", "[::1]")


def phone_reachable(url: str, home: Optional[str]) -> str:
    """`url` as another device at home reaches it: a server on this computer ("localhost")
    by this computer's home address `home` (None: as it is)."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not home or not (host in _LOOPBACK or host.startswith("127.")):
        return url
    netloc = home + (f":{parts.port}" if parts.port else "")
    return parts._replace(netloc=netloc).geturl()


@dataclass(frozen=True)
class StreamRef:
    base: str
    item_id: str
    source_id: str
    token: str
    play_session: str
    converted: bool


def parse_stream_url(url: str, bases: Iterable[str]) -> Optional[StreamRef]:
    """What a stream address of one of `bases` (the servers' addresses) plays, else None."""
    for base in bases:
        base = normalized_base(base)
        if not base or not url.startswith(base + "/Videos/"):
            continue
        parts = urlsplit(url)
        segments = parts.path[len(urlsplit(base).path) :].strip("/").split("/")
        if len(segments) < 3:
            return None
        query = parse_qs(parts.query)

        def first(name: str) -> str:
            return (query.get(name) or [""])[0]

        return StreamRef(
            base=base,
            item_id=segments[1],
            source_id=first("MediaSourceId"),
            token=first("api_key"),
            play_session=first("PlaySessionId"),
            converted=segments[2].startswith("master"),
        )
    return None
