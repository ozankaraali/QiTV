"""Plex Media Server as a provider (2.14): the account's sign-in at plex.tv and the server's API.

A Plex server is a media server provider (services.provider_types.PLEX) beside Jellyfin and
Emby, with an API of its own (developer.plex.tv/pms). Its films and series come as theirs do,
in the same Library payloads (services.media_server), so everything above them is the same.

Signing in is Plex's PIN flow: plex.tv gives a 4-character code (`PlexAccount.create_pin`),
the viewer enters it at plex.tv/link, QiTV looks until the code is claimed (`check_pin`: the
account's token), then lists the account's servers (`servers`), each with its own access token
and its connections, local first, then remote, then relayed (`ordered`). The provider keeps the
server's address, its access token and its id, never the account's token.

QiTV says only who it is: X-Plex-Product "QiTV", its version, the system's kind and a random
client identifier (the provider's `device_id`), never the computer's or the viewer's name. A
token travels in a header to plex.tv and to the server's API, and in the query of the addresses
VLC opens (a stream, a poster), as Plex documents; nothing here logs an address, a token or an
answer's body. Blocking: worker threads only.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import logging
import platform
from typing import Any, Callable, Optional
import uuid

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


def _platform(system: Optional[str] = None) -> str:
    name = system or platform.system()
    return {"Darwin": "macOS", "Windows": "Windows"}.get(name, "Linux")


def client_headers(client_id: str, version: str, token: str = "") -> dict[str, str]:
    """What QiTV tells plex.tv and a server about itself (and its token, when it has one)."""
    headers = {
        "Accept": "application/json",
        "X-Plex-Product": PRODUCT,
        "X-Plex-Version": version,
        "X-Plex-Client-Identifier": client_id,
        "X-Plex-Platform": _platform(),
        "X-Plex-Device-Name": f"{PRODUCT} ({_platform()})",
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


def answers(
    uri: str, server: Server, client_id: str, version: str, verify: bool, session: Any = None
) -> bool:
    """Whether `server` answers at `uri`: its identity, asked with its token, is its own."""
    http = session if session is not None else requests.Session()
    try:
        response = http.request(
            "GET",
            uri + "/identity",
            headers=client_headers(client_id, version, server.access_token),
            timeout=PROBE_TIMEOUT_S,
            verify=verify,
        )
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
