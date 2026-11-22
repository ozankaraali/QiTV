"""Plex Media Server as a provider (2.14): the account's sign-in at plex.tv and the server's API.

A Plex server is a media server provider (services.provider_types.PLEX) beside Jellyfin and
Emby, with an API of its own (developer.plex.tv/pms). Its films and series come as theirs do,
in the same Library payloads (services.media_server), so everything above them is the same.

Signing in is Plex's PIN flow: plex.tv gives a 4-character code (`PlexAccount.create_pin`),
the viewer enters it at plex.tv/link, QiTV looks until the code is claimed (`check_pin`: the
account's token), then lists the account's servers (`servers`), each with its own access token
and its connections, local first, then remote, then relayed (`ordered`). The provider keeps the
server's address, its access token and its id, never the account's token.

QiTV says only who it is: X-Plex-Product "QiTV", its version and a random client identifier
(the provider's `device_id`), never the computer's or the viewer's name. A
token travels in a header to plex.tv and to the server's API, and in the query of the addresses
VLC opens (a stream, a poster), as Plex documents; nothing here logs an address, a token or an
answer's body. Blocking: worker threads only.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import logging
import threading
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlencode, urlsplit
import uuid

from services import media_server as ms
from services.lazy_import import lazy_module
from services.media_server import MediaServerError, SignInNeeded, normalized_base
from services.surrogates import response_json

logger = logging.getLogger(__name__)
requests = lazy_module("requests")  # the window shows before the network libraries load

PRODUCT = "QiTV"
PLEX_TV = "https://plex.tv"
LINK_PAGE = "plex.tv/link"  # where the viewer enters the code (shown as it is)
TIMEOUT_S = 30
PROBE_TIMEOUT_S = 4  # a connection that answers its identity in this is the one used
PIN_LIFETIME_S = 900  # plex.tv's 4-character codes last 15 minutes (it says so in each)


def new_client_id() -> str:
    """A client identifier of QiTV's own for one sign-in (random: never the computer's)."""
    return uuid.uuid4().hex


def client_headers(client_id: str, version: str, token: str = "") -> dict[str, str]:
    """What QiTV tells plex.tv and a server about itself: its name, its version and its random
    client id, nothing else (and its token, when it has one)."""
    headers = {
        "Accept": "application/json",
        "X-Plex-Product": PRODUCT,
        "X-Plex-Version": version,
        "X-Plex-Client-Identifier": client_id,
    }
    if token:
        headers["X-Plex-Token"] = token
    return headers


# ---- the account at plex.tv -----------------------------------------------------------------


@dataclass(frozen=True)
class Pin:
    """A sign-in code: its id at plex.tv, the code the viewer enters, how long it lasts."""

    id: int
    code: str
    expires_in: int = PIN_LIFETIME_S


@dataclass(frozen=True)
class Connection:
    """One way to reach a server: its address (`uri`), whether it is at home (`local`) or
    through Plex's relay (`relay`)."""

    uri: str
    local: bool = False
    relay: bool = False


@dataclass(frozen=True)
class Server:
    """A server of the account: its name, its id (`machine_id`, Plex's clientIdentifier), the
    token it takes from this account (`access_token`) and its connections, best first."""

    name: str
    machine_id: str
    access_token: str = field(repr=False)
    owned: bool = False
    connections: tuple[Connection, ...] = ()


def ordered(connections: list[Connection]) -> list[Connection]:
    """A server's connections as QiTV tries them: at home first, then remote, then relayed
    (slow: Plex limits its speed); HTTPS before HTTP within each."""

    def rank(connection: Connection) -> tuple[int, int]:
        where = 2 if connection.relay else (0 if connection.local else 1)
        return where, 0 if connection.uri.startswith("https://") else 1

    return sorted(connections, key=rank)


def _connections(raw: Any) -> tuple[Connection, ...]:
    found: list[Connection] = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        uri = normalized_base(str(entry.get("uri") or ""))
        if not uri.startswith(("http://", "https://")):
            continue
        local, relay = bool(entry.get("local")), bool(entry.get("relay"))
        found.append(Connection(uri, local, relay))
        # A home address by its IP over HTTP too, after its plex.direct one: a router that
        # refuses plex.direct's names (DNS rebinding protection) still lets QiTV in.
        address, port = str(entry.get("address") or ""), entry.get("port")
        protocol = str(entry.get("protocol") or "")
        if local and not relay and address and port and protocol == "https":
            host = f"[{address}]" if ":" in address else address
            found.append(Connection(f"http://{host}:{int(port)}", True, False))
    unique: dict[str, Connection] = {}
    for connection in ordered(found):
        unique.setdefault(connection.uri, connection)
    return tuple(unique.values())


def servers_of(resources: Any) -> list[Server]:
    """The Plex Media Servers among an account's resources (plex.tv's /api/v2/resources), each
    with its connections best first; players and other devices are left out."""
    out: list[Server] = []
    for raw in resources if isinstance(resources, list) else []:
        if not isinstance(raw, dict):
            continue
        provides = str(raw.get("provides") or "").split(",")
        machine = str(raw.get("clientIdentifier") or "")
        if "server" not in provides or not machine:
            continue
        out.append(
            Server(
                name=str(raw.get("name") or ""),
                machine_id=machine,
                access_token=str(raw.get("accessToken") or ""),
                owned=bool(raw.get("owned")),
                connections=_connections(raw.get("connections")),
            )
        )
    return out


def must_verify(url: str, verify: bool) -> bool:
    """Whether a server's certificate is checked at `url`: always at a plex.direct address
    (Plex's own certificates, public: what proves the server the token goes to), else as
    the setting says."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        host = ""
    return verify or host.endswith(".plex.direct")


def answers(uri: str, server: Server, client_id: str, version: str, session: Any = None) -> bool:
    """Whether `server` answers at `uri`: its identity, asked without its token, is its own
    (review I1: a stranger's machine at a private address that is home's elsewhere never
    gets it). Only an HTTPS address whose certificate proved the server is asked again with
    the token, when its identity wants one."""
    http = session if session is not None else requests.Session()
    secure = uri.startswith("https://")

    def identity(token: str) -> Any:
        return http.request(
            "GET",
            uri + "/identity",
            headers=client_headers(client_id, version, token),
            timeout=PROBE_TIMEOUT_S,
            verify=True,
        )

    try:
        response = identity("")
        if response.status_code == 401 and secure and server.access_token:
            response = identity(server.access_token)
        if response.status_code != 200 or not response.content:
            return False
        container = (response_json(response) or {}).get("MediaContainer") or {}
        return bool(container.get("machineIdentifier") == server.machine_id)
    except Exception as error:  # unreachable, a certificate refused, not Plex: not this way
        logger.debug("A Plex connection did not answer: %s", type(error).__name__)
        return False
    finally:
        if session is None:
            http.close()


def reachable(
    servers: list[Server], probe: Callable[[str, Server], bool]
) -> list[tuple[Server, str]]:
    """Each server that answers, with the best of its connections that does (`probe(uri,
    server)`; every connection is asked at once, so a slow one delays none of the others)."""
    asks = [(server, c.uri) for server in servers for c in server.connections]
    if not asks:
        return []

    def ask(job: tuple[Server, str]) -> bool:
        try:
            return bool(probe(job[1], job[0]))
        except Exception:
            return False

    with ThreadPoolExecutor(max_workers=min(8, len(asks)), thread_name_prefix="plex-probe") as pool:
        answered = dict(zip(asks, pool.map(ask, asks)))
    found = []
    for server in servers:
        uri = next((c.uri for c in server.connections if answered.get((server, c.uri))), "")
        if uri:
            found.append((server, uri))
    return found


class PlexAccount:
    """plex.tv for one sign-in: the code, the token it gives, the account's servers."""

    def __init__(
        self,
        client_id: str,
        version: str,
        *,
        session: Any = None,
        timeout: float = TIMEOUT_S,
        base: str = PLEX_TV,
    ) -> None:
        self.client_id = client_id
        self.version = version
        self.timeout = timeout
        self.base = base.rstrip("/")
        self._http = session if session is not None else requests.Session()

    def close(self) -> None:
        self._http.close()

    def _ask(self, method: str, path: str, token: str = "", params: Optional[dict] = None) -> Any:
        response = self._http.request(
            method,
            self.base + path,
            params=params,
            headers=client_headers(self.client_id, self.version, token),
            timeout=self.timeout,
        )
        if response.status_code == 401:
            raise SignInNeeded("401")
        if response.status_code >= 400:  # never the address or the body
            raise MediaServerError(f"plex.tv answered {response.status_code}")
        return response_json(response) if response.content else None

    def create_pin(self) -> Pin:
        """A new 4-character code (the viewer enters it at plex.tv/link)."""
        answer = self._ask("POST", "/api/v2/pins") or {}
        pin_id, code = answer.get("id"), str(answer.get("code") or "")
        if not isinstance(pin_id, int) or not code:
            raise MediaServerError("plex.tv gave no code")
        lasts = answer.get("expiresIn")
        return Pin(pin_id, code, int(lasts) if isinstance(lasts, int) and lasts > 0 else 900)

    def check_pin(self, pin_id: int) -> str:
        """The account's token once the code was entered, else "" (not yet). A code gone
        (expired) raises SignInNeeded."""
        try:
            answer = self._ask("GET", f"/api/v2/pins/{int(pin_id)}") or {}
        except MediaServerError as error:
            if str(error).endswith(" 404"):
                raise SignInNeeded("404") from None  # the code expired
            raise
        return str(answer.get("authToken") or "")

    def servers(self, token: str) -> list[Server]:
        """The account's servers (its own and those shared with it)."""
        answer = self._ask(
            "GET",
            "/api/v2/resources",
            token,
            params={"includeHttps": 1, "includeRelay": 1, "includeIPv6": 1},
        )
        return servers_of(answer)


# ---- the server -----------------------------------------------------------------------------

MOVIE, SHOW = "movie", "show"  # the libraries QiTV lists (music and photos are not)
_TYPE_NUMBERS = {MOVIE: 1, SHOW: 2}  # Plex's metadata types
PAGE = 2_000  # titles a request (a library of 50,000 comes in 25)
_TIMELINE_STATES = {"start": "playing", "progress": "playing", "stopped": "stopped"}
_DURATIONS: dict[tuple[str, str], int] = {}  # (server, title) -> its length, for the timeline
_DURATIONS_KEPT = 64
_DURATIONS_LOCK = threading.Lock()


def forget_durations() -> None:
    """The titles' lengths the reports remembered (tests)."""
    with _DURATIONS_LOCK:
        _DURATIONS.clear()


class PlexClient:
    """One server, as signed in: its address, its own token for this account, and the client
    id QiTV signed in as. Blocking: worker threads only."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str,
        client_id: str,
        version: str,
        verify: bool = True,
        session: Any = None,
        timeout: float = TIMEOUT_S,
    ) -> None:
        self.base = normalized_base(base_url)
        self.token = token
        self.client_id = client_id
        self.version = version
        self.verify = verify
        self.timeout = timeout
        self._http = session if session is not None else requests.Session()

    def close(self) -> None:
        self._http.close()

    def _ask(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        headers: Optional[dict] = None,
    ) -> dict:
        """The answer's MediaContainer ({} for none); 401 raises SignInNeeded. The token goes
        in a header, never in the address (an error's text never names either)."""
        response = self._http.request(
            method,
            self.base + path,
            params=params,
            headers={**client_headers(self.client_id, self.version, self.token), **(headers or {})},
            timeout=self.timeout,
            verify=self.verify,
        )
        if response.status_code == 401:
            raise SignInNeeded("401")
        if response.status_code >= 400:
            raise MediaServerError(f"The server answered {response.status_code}")
        if not response.content:
            return {}
        answer = response_json(response)
        container = answer.get("MediaContainer") if isinstance(answer, dict) else None
        return container if isinstance(container, dict) else {}

    def identity(self) -> dict:
        return self._ask("GET", "/identity")

    def libraries(self) -> list[dict]:
        """The server's libraries of films and of series: {id, name, type}."""
        out = []
        for raw in self._ask("GET", "/library/sections/all").get("Directory") or []:
            kind, key = str(raw.get("type") or ""), str(raw.get("key") or "")
            if kind in (MOVIE, SHOW) and key:
                out.append({"id": key, "name": str(raw.get("title") or ""), "type": kind})
        return out

    def _paged(self, path: str, params: Optional[dict] = None, page: int = PAGE) -> list[dict]:
        """Every item of a list, in pages (a server may cut one it was not asked to page)."""
        found: list[dict] = []
        while True:
            paging = {"X-Plex-Container-Start": str(len(found)), "X-Plex-Container-Size": str(page)}
            answer = self._ask("GET", path, params, paging)
            batch = [m for m in answer.get("Metadata") or [] if isinstance(m, dict)]
            found.extend(batch)
            total = answer.get("totalSize")
            if not batch or (isinstance(total, int) and len(found) >= total):
                return found
            if not isinstance(total, int) and len(batch) < page:
                return found

    def items(self, library_id: str, kind: str, page: int = PAGE) -> list[dict]:
        """Every film (`kind` MOVIE) or series (SHOW) of the library, in pages."""
        params = {"type": _TYPE_NUMBERS[kind], "includeGuids": 1}
        return self._paged(f"/library/sections/{library_id}/all", params, page)

    def seasons_and_episodes(self, show_id: str) -> tuple[list[dict], list[dict]]:
        """A series' seasons (its children) and every episode of it (its leaves)."""
        seasons = self._paged(f"/library/metadata/{show_id}/children")
        episodes = self._paged(f"/library/metadata/{show_id}/allLeaves", {"includeGuids": 1})
        return seasons, episodes

    def media(self, item_id: str) -> list[dict]:
        """A title's versions (its Media, each with its files)."""
        found = self._ask("GET", f"/library/metadata/{item_id}").get("Metadata") or [{}]
        first = found[0] if isinstance(found[0], dict) else {}
        with _DURATIONS_LOCK:
            _remember(self.base, str(item_id), first.get("duration"))
        return [m for m in first.get("Media") or [] if isinstance(m, dict)]

    # ---- where the user is ------------------------------------------------------------------

    def _duration(self, item_id: str) -> int:
        with _DURATIONS_LOCK:
            known = _DURATIONS.get((self.base, item_id))
        if known is None:
            try:
                self.media(item_id)
            except Exception as error:  # unknown: told without it
                logger.info("A Plex title's length is unknown: %s", type(error).__name__)
                return 0
            with _DURATIONS_LOCK:
                known = _DURATIONS.get((self.base, item_id))
        return int(known or 0)

    def report(self, event: str, ref: ms.StreamRef, position_ms: int, paused: bool = False) -> None:
        """Tell the server a play started ("start"), where it is ("progress"), or that it
        stopped ("stopped"), through its timeline, with the title's length: a stop near its
        end is marked watched by the server itself, by its own setting (no scrobble of QiTV's:
        checked on a 1.43 server)."""
        state = "paused" if paused and event == "progress" else _TIMELINE_STATES[event]
        position = max(0, int(position_ms))
        duration = self._duration(ref.item_id)
        params: dict[str, Any] = {
            "key": f"/library/metadata/{ref.item_id}",
            "ratingKey": ref.item_id,
            "state": state,
            "time": position,
        }
        if duration > 0:
            params["duration"] = duration
        self._ask("POST", "/:/timeline", params, {"X-Plex-Session-Identifier": ref.play_session})


def _remember(base: str, item_id: str, duration: Any) -> None:
    if not isinstance(duration, int) or duration <= 0:
        return
    _DURATIONS[(base, item_id)] = duration
    while len(_DURATIONS) > _DURATIONS_KEPT:
        _DURATIONS.pop(next(iter(_DURATIONS)))


# ---- titles ---------------------------------------------------------------------------------


def image_url(base: str, path: Any, token: str) -> str:
    """A picture of the server (a poster, a season's cover) as VLC and the image cache fetch
    it: with the token (Plex asks for it), never resized by the server."""
    path = str(path or "")
    if not path.startswith("/"):
        return ""
    return f"{base}{path}?{urlencode({'X-Plex-Token': token})}"


def _tmdb(raw: dict) -> str:
    for guid in raw.get("Guid") or []:
        value = str(guid.get("id") or "") if isinstance(guid, dict) else ""
        if value.startswith("tmdb://") and value[7:].isdigit():
            return value[7:]
    return ""


def _number(value: Any) -> str:
    return str(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else ""


def title_item(raw: dict, base: str, token: str, library_id: str) -> dict:
    """A film or a series of the server as a Library title (Jellyfin's and Emby's keys:
    services.media_server.title_item)."""
    overview = ms.shortened(str(raw.get("summary") or ""))
    rating = raw.get("audienceRating")
    if not isinstance(rating, (int, float)):
        rating = raw.get("rating")
    item: dict[str, Any] = {
        "id": str(raw.get("ratingKey") or ""),
        "name": str(raw.get("title") or ""),
        "logo": image_url(base, raw.get("thumb"), token),
        "tv_genre_id": library_id,
        "cmd": "",
        "description": overview,
        "plot": overview,
        "year": _number(raw.get("year")),
        "rating": _number(rating),
        "genre": ", ".join(
            str(g.get("tag"))
            for g in raw.get("Genre") or []
            if isinstance(g, dict) and g.get("tag")
        ),
    }
    duration = raw.get("duration")
    if isinstance(duration, int) and duration > 0:
        item["duration_secs"] = duration // 1000
    tmdb = _tmdb(raw)
    if tmdb:
        item["tmdb"] = tmdb
    added = raw.get("addedAt")
    if isinstance(added, int) and added > 0:
        item["added"] = str(added)
    position = raw.get("viewOffset")
    if isinstance(position, int) and position > 0:
        item["server_position_ms"] = position
    media = next((m for m in raw.get("Media") or [] if isinstance(m, dict)), {})
    container = str(media.get("container") or "").split(",")[0]
    if container:
        item["container_extension"] = container
    return item


def section_payload(
    libraries: list[dict], titles: dict[str, list[dict]], base: str, token: str
) -> dict:
    """The Library's section of a server: All, Continue watching (titles started there), then
    each library (services.media_server.ordered_payload). Loaded whole each time: Plex answers
    a library at once."""
    ordered: dict[str, list[dict]] = {}
    made: dict[str, dict] = {}
    for library in libraries:
        listed = ordered.setdefault(library["id"], [])
        for raw in titles.get(library["id"]) or []:
            item_id = str(raw.get("ratingKey") or "")
            if item_id:
                if item_id not in made:
                    made[item_id] = title_item(raw, base, token, library["id"])
                listed.append(made[item_id])
    return ms.ordered_payload(libraries, ordered)


def episode_item(raw: dict, base: str, token: str) -> dict:
    number = _number(raw.get("index"))
    name = str(raw.get("title") or (f"Episode {number}" if number else ""))
    item = title_item(raw, base, token, "")
    item.update(
        {
            "number": number,
            "episode_num": number,
            "ename": name,
            "name": name,
            "season": _number(raw.get("parentIndex")),
            "series_id": str(raw.get("grandparentRatingKey") or ""),
            "season_id": str(raw.get("parentRatingKey") or ""),
        }
    )
    return item


def seasons_payload(seasons: list[dict], episodes: list[dict], base: str, token: str) -> dict:
    """A series' seasons, each with its episodes in order (as Jellyfin's come)."""
    by_season: dict[str, list[dict]] = {}
    for raw in episodes:
        by_season.setdefault(str(raw.get("parentRatingKey") or ""), []).append(raw)
    out: list[dict[str, Any]] = []
    for raw in seasons:
        season_id = str(raw.get("ratingKey") or "")
        listed = sorted(by_season.get(season_id, []), key=lambda e: int(e.get("index") or 0))
        season_number = _number(raw.get("index"))
        made = [episode_item(e, base, token) for e in listed]
        for episode in made:  # an episode without its own season's number has its season's
            episode["season"] = episode["season"] or season_number
        out.append(
            {
                "id": season_id,
                "name": str(raw.get("title") or ""),
                "season_number": season_number,
                "cover": image_url(base, raw.get("thumb"), token),
                "overview": str(raw.get("summary") or ""),
                "episode_count": str(len(listed)),
                "episodes": made,
            }
        )
    return {"seasons": out}


# ---- versions and streams -------------------------------------------------------------------


def sources(media: list[dict]) -> list[dict]:
    """A title's versions in the shape services.media_server's rules read (`choose_source`,
    `version_of`, `within`): each version's id, its picture's size, its rate (bits a second)
    and its first file (`part`, the address it plays from)."""
    out = []
    for raw in media:
        part = next(
            (p for p in raw.get("Part") or [] if isinstance(p, dict) and p.get("key")), None
        )
        if part is None or raw.get("id") is None:
            continue
        rate = raw.get("bitrate")
        out.append(
            {
                "Id": str(raw["id"]),
                "Bitrate": int(rate) * 1000 if isinstance(rate, int) else 0,
                "Container": str(part.get("container") or raw.get("container") or ""),
                "MediaStreams": [
                    {
                        "Type": "Video",
                        "Width": int(raw.get("width") or 0),
                        "Height": int(raw.get("height") or 0),
                    }
                ],
                "part": str(part["key"]),
            }
        )
    return out


def stream_url(base: str, source: dict, token: str, client_id: str, item_id: str) -> str:
    """A version's file itself, as it is (Plex's direct play: never /transcode). The address
    names QiTV and a play session of its own, `<title>-<random>`, which tells the reporter
    which title plays (a file's address names only the file)."""
    query = urlencode(
        {
            "X-Plex-Token": token,
            "X-Plex-Client-Identifier": client_id,
            "X-Plex-Product": PRODUCT,
            "X-Plex-Session-Identifier": f"{item_id}-{uuid.uuid4().hex}",
        }
    )
    return f"{base}{source['part']}?{query}"


def parse_stream_url(url: str, bases: Any) -> Optional[ms.StreamRef]:
    """What a stream address of one of `bases` (the Plex servers' addresses) plays, else None
    (an address QiTV did not make: no play session of its own)."""
    for base in bases:
        base = normalized_base(str(base or ""))
        if not base or not url.startswith(base + "/library/parts/"):
            continue
        parts = urlsplit(url)
        segments = parts.path[len(urlsplit(base).path) :].strip("/").split("/")
        query = parse_qs(parts.query)
        session = (query.get("X-Plex-Session-Identifier") or [""])[0]
        item_id = session.split("-", 1)[0]
        if len(segments) < 3 or not item_id.isdigit():
            return None
        return ms.StreamRef(
            base=base,
            item_id=item_id,
            source_id=segments[2],
            token=(query.get("X-Plex-Token") or [""])[0],
            play_session=session,
            converted=False,
        )
    return None
