"""The links that played, kept as "Network streams" (no Qt).

A stream link opened with File > Open Link is kept in its own file, network_streams.json, so
it can be played again, renamed or removed. It is not a provider: the providers list never
reads this file.

An entry's id comes from where the link points, not from its token: a link handed out with a
fresh `?token=` each day is still the same entry, and keeping it again updates its address.
The file is versioned ({"v": 1, "entries": [...]}); a missing or unreadable one reads as
empty. `save_bytes` and `load` keep the file I/O apart, so the UI can write on a worker.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit

from services.atomic_write import atomic_write

logger = logging.getLogger(__name__)

LIVE, FILM = "LIVE", "FILM"
_VERSION = 1
_DEFAULT_PORTS = {"http": 80, "https": 443, "rtmp": 1935, "rtsp": 554}
_SCRIPTS = (".php", ".aspx", ".jsp")
_GENERIC = {"index", "playlist", "master", "main", "manifest", "chunklist", "stream", "live"}


@dataclass
class Entry:
    id: str
    name: str
    url: str
    kind: str  # LIVE or FILM
    opened: float = 0.0  # when it last played (0: never)
    failed: str = ""  # why the last try failed, until it plays again


def stream_id(url: str) -> str:
    """A link's id: 16 hex characters of a hash of where it points. http and https are one
    scheme here (a provider that serves both is one stream), host is lower-cased, a default
    port and the fragment are dropped. The query counts only when the last path segment has
    no extension or is a script (.php, .aspx, .jsp), where it names the stream; elsewhere it
    is a token that changes."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    try:
        port = parts.port
    except ValueError:
        port = None
    if port == _DEFAULT_PORTS.get(scheme) or (scheme in ("http", "https") and port in (80, 443)):
        port = None
    if scheme in ("http", "https"):
        scheme = "http"
    host = (parts.hostname or "").lower()
    path = parts.path or "/"
    last = path.rstrip("/").rsplit("/", 1)[-1].lower()
    ext = os.path.splitext(last)[1]
    query = ""
    if not ext or ext in _SCRIPTS:
        query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    key = f"{scheme}://{host}{'' if port is None else f':{port}'}{path}"
    if query:
        key += f"?{query}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def default_name(url: str) -> str:
    """A name for a link: its host and the last meaningful path segment ("cdn.example —
    channel"; an extension and generic names such as index or playlist are skipped)."""
    parts = urlsplit(url.strip())
    host = parts.hostname or url.strip()
    segments = [s for s in parts.path.split("/") if s]
    for segment in reversed(segments):
        stem = os.path.splitext(segment)[0]
        if stem and stem.lower() not in _GENERIC:
            return f"{host} — {stem}"
    return host


def write(path: Path, data: bytes) -> None:
    """Write `data` to `path` whole or not at all (the folder is made when missing)."""
    os.makedirs(os.path.dirname(str(path)) or ".", exist_ok=True)
    with atomic_write(str(path)) as f:
        f.write(data)


class NetworkStreams:
    """The kept links, read from `path` when made. Changes stay in memory: write
    `save_bytes()` to the file (on a worker) to keep them."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._entries: dict[str, Entry] = {}
        try:
            data = self.path.read_bytes()
        except OSError:
            return
        self.load(data)

    def load(self, data: bytes) -> None:
        """Replace the entries with those in `data`; broken data reads as none."""
        self._entries = {}
        try:
            doc = json.loads(data)
            rows = doc["entries"]
            if not isinstance(rows, list):
                return
            loaded: dict[str, Entry] = {}
            for row in rows:
                entry = Entry(
                    id=str(row["id"]),
                    name=str(row.get("name", "")),
                    url=str(row["url"]),
                    kind=FILM if row.get("kind") == FILM else LIVE,
                    opened=float(row.get("opened", 0.0)),
                    failed=str(row.get("failed", "")),
                )
                loaded[entry.id] = entry
        except ValueError, TypeError, KeyError, AttributeError:
            logger.warning("Network streams file unreadable: starting empty")
            return
        self._entries = loaded

    def save_bytes(self) -> bytes:
        rows = [asdict(e) for e in self._entries.values()]
        return json.dumps({"v": _VERSION, "entries": rows}, indent=1).encode("utf-8")

    def entries(self) -> list[Entry]:
        """Newest opened first; links never opened follow, the latest kept first."""
        kept = list(reversed(list(self._entries.values())))
        return sorted(kept, key=lambda e: e.opened, reverse=True)

    def get(self, id: str) -> Optional[Entry]:
        return self._entries.get(id)

    def keep(self, url: str, name: str, kind: str) -> Entry:
        """Keep a link that played: a new entry, or the same stream's entry with its address
        and kind updated and its name kept."""
        id = stream_id(url)
        entry = self._entries.get(id)
        if entry is None:
            entry = Entry(id, name or default_name(url), url, kind)
            self._entries[id] = entry
        else:
            entry.url, entry.kind = url, kind
        return entry

    def rename(self, id: str, name: str) -> None:
        if id in self._entries:
            self._entries[id].name = name

    def set_url(self, id: str, url: str) -> None:
        if id in self._entries:
            self._entries[id].url = url

    def remove(self, id: str) -> None:
        self._entries.pop(id, None)

    def opened(self, id: str, when: float) -> None:
        """It played at `when`: the last failure is forgotten."""
        if id in self._entries:
            self._entries[id].opened = when
            self._entries[id].failed = ""

    def failed(self, id: str, why: str) -> None:
        if id in self._entries:
            self._entries[id].failed = why
