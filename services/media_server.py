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
(`choose_source`: no 4K version below a 4K screen unless chosen). Converting on the server
(`converted_url`, HLS) happens only when Settings caps the bitrate and the file exceeds it.
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

import requests

from services.catalog import ALL_GROUP_ID
from services.i18n import N_

logger = logging.getLogger(__name__)

JELLYFIN, EMBY = "JELLYFIN", "EMBY"
TYPES = (JELLYFIN, EMBY)
TICKS_PER_MS = 10_000
PAGE = 2_000  # titles a request (a library of 50,000 comes in 25)
TIMEOUT_S = 30
# The Library's genre of the titles the user has started (where the server says they stopped).
CONTINUE_ID = "@server-continue"
# i18n: a Library genre of a Jellyfin or Emby server: the titles you started watching there
CONTINUE_TITLE = N_("Continue watching")
FIELDS = "Overview,Genres,ProductionYear,CommunityRating,RunTimeTicks,ProviderIds,DateCreated"
OVERVIEW_CHARS = 400  # a description in the list (the details strip shows a few lines)
_LIBRARY_TYPES = {"movies", "tvshows", "mixed", "homevideos", ""}
# 4K versions (Settings): only on a 4K screen (default), always, never.
FOUR_K_SCREEN, FOUR_K_ALWAYS, FOUR_K_NEVER = "screen", "always", "never"
FOUR_K_CHOICES = (FOUR_K_SCREEN, FOUR_K_ALWAYS, FOUR_K_NEVER)
ONLY_4K = "only_4k"  # choose_source's reason: the title has only 4K versions
NO_SOURCE = "none"
# i18n: a title of a Jellyfin or Emby server has only a 4K version, and Settings plays 4K only
# on a 4K screen
ONLY_4K_TEXT = N_(
    "This title is only in 4K. To play it here, choose Always for 4K versions in Settings."
)
# i18n: a Jellyfin or Emby server refused QiTV's access (its sign-in ended)
SIGN_IN_AGAIN = N_("The server asks to sign in again: Settings → Providers → Verify.")


def is_media_server(provider_type: Any) -> bool:
    return str(provider_type or "").upper() in TYPES


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

    def items(self, item_type: str, parent_id: str, page: int = PAGE) -> list[dict]:
        """Every `item_type` ("Movie", "Series") title of the library `parent_id`."""
        routes = self._user_routes("/Items", f"/Users/{self.user_id}/Items")
        found: list[dict] = []
        start = 0
        while True:
            params = {
                "UserId": self.user_id,  # one spelling: Emby fails on both (Jellyfin takes either)
                "ParentId": parent_id,
                "IncludeItemTypes": item_type,
                "Recursive": "true",
                "Fields": FIELDS,
                "EnableImageTypes": "Primary",
                "ImageTypeLimit": 1,
                "SortBy": "SortName",
                "StartIndex": start,
                "Limit": page,
            }
            answer = self._request("GET", routes, params=params) or {}
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
    items: list[dict] = []
    sorted_channels: dict[str, list[int]] = {}
    seen: dict[str, int] = {}
    for library in libraries:
        for raw in titles.get(library["id"]) or []:
            item_id = str(raw.get("Id") or "")
            if not item_id:
                continue
            index = seen.get(item_id)
            if index is None:
                index = seen[item_id] = len(items)
                items.append(title_item(raw, base, library["id"]))
                if items[index].get("server_position_ms"):
                    sorted_channels.setdefault(CONTINUE_ID, []).append(index)
            sorted_channels.setdefault(library["id"], []).append(index)
    categories = [{"id": ALL_GROUP_ID, "title": N_("All")}]
    if CONTINUE_ID in sorted_channels:
        categories.append({"id": CONTINUE_ID, "title": CONTINUE_TITLE})
    categories += [
        {"id": lib["id"], "title": lib["name"]} for lib in libraries if lib["id"] in sorted_channels
    ]
    return {"categories": categories, "contents": items, "sorted_channels": sorted_channels}


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
    out = []
    for raw in seasons:
        season_id = str(raw.get("Id") or "")
        listed = sorted(by_season.get(season_id, []), key=lambda e: int(e.get("IndexNumber") or 0))
        number = raw.get("IndexNumber")
        out.append(
            {
                "id": season_id,
                "name": str(raw.get("Name") or ""),
                "season_number": str(number) if number is not None else "",
                "cover": _image(raw, base),
                "overview": str(raw.get("Overview") or ""),
                "episode_count": str(len(listed)),
                "episodes": [episode_item(e, base) for e in listed],
            }
        )
    for item in (e for s in out for e in s["episodes"]):
        item["season"] = item["season"] or next(
            (s["season_number"] for s in out if s["id"] == item["season_id"]), ""
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
    wants_4k = setting == FOUR_K_ALWAYS or (setting == FOUR_K_SCREEN and screen_4k)
    pool = (large or small) if wants_4k else small
    if not pool:
        return None, ONLY_4K
    best = max(
        pool,
        key=lambda s: (int(_video(s).get("Height") or 0), int(s.get("Bitrate") or 0)),
    )
    return best, None


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
) -> str:
    """The server's own conversion (HLS, H.264 and AAC), capped at `max_bitrate`."""
    query = urlencode(
        {
            "MediaSourceId": source.get("Id") or "",
            "PlaySessionId": play_session,
            "DeviceId": device_id,
            "VideoCodec": "h264",
            "AudioCodec": "aac",
            "MaxStreamingBitrate": int(max_bitrate),
            "SegmentContainer": "ts",
            "api_key": token,
        }
    )
    return f"{base}/Videos/{item_id}/master.m3u8?{query}"


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
