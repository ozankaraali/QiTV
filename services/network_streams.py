"""The links that played, kept as "Network streams" (no Qt).

A stream link opened with File > Open Link is kept in its own file, network_streams.json, so
it can be played again, renamed or removed. It is not a provider: the providers list never
reads this file.

An entry's id comes from where the link points, not from its token: a link handed out with a
fresh `?token=` each day is still the same entry, and keeping it again updates its address.
The file is versioned ({"v": 1, "entries": [...]}; an older build skips what it does not
know). A missing file reads as empty, and so does an unreadable one, which is `broken`: it is
set aside before the first write over it (`save`). A row without an id or an address is left
out, the others are read. Making a store reads nothing: `read_bytes` and `save` are the file's
(blocking: a worker's), `load`, `merge` and `save_bytes` the store's, so the UI never waits
for the disk.

A link that failed stays, with why (`failure_reason`: an English text, shown with
translate()). A Kick or Twitch channel's page is kept by its page (never the address with the
token the site answers as it plays), named by its channel ("examplechannel · Kick"); why the
site gave no stream (`site_failure`) names the channel too, kept in English with it and shown
in the active language by `shown_reason`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit

from services.atomic_write import atomic_write, set_aside as set_file_aside
from services.i18n import N_, translate
from services.live.refusals import STREAM_LIMIT, STREAM_REFUSED
from services.live_sites import SITE_NAMES, SitePage, page_name, site_page
from services.provider_ids import NETWORK_PROVIDER, provider_of

logger = logging.getLogger(__name__)

LIVE, FILM = "LIVE", "FILM"
FILE_NAME = "network_streams.json"  # beside config.json
_VERSION = 1
_DEFAULT_PORTS = {"http": 80, "https": 443, "rtmp": 1935, "rtsp": 554}
_SCRIPTS = (".php", ".aspx", ".jsp")
_GENERIC = {"index", "playlist", "master", "main", "manifest", "chunklist", "stream", "live"}

# Why a link failed, as Network streams keeps it (English: shown with translate()).
# i18n: why a link opened with Open Link did not play: its server answered 401 (its token ended)
ACCESS_ENDED = N_("401: the link's access ran out")
# i18n: why a link opened with Open Link did not play: its server answered 403
REFUSED = N_("403: the link was refused")
# i18n: why a link opened with Open Link did not play: its server answered 404 (or 410)
GONE = N_("404: nothing is at this address")
# i18n: why a link opened with Open Link did not play: its server never answered
TIMED_OUT = N_("The link didn't answer in time")
# i18n: why a link opened with Open Link did not play: its server's stream limit is reached
BUSY = N_("The link's server allows no more streams now")
# i18n: why a link opened with Open Link stopped: it played, then went down and stayed down
DOWN = N_("The link stopped answering")
# i18n: why a link opened with Open Link did not play, when nothing more is known
FAILED = N_("The link could not be opened")
# Why a Kick or Twitch channel opened by its page did not play: the site's own answer (kept
# in English with its fields, as the others: `shown_reason` shows one).
# i18n: why a channel opened by its page (Kick, Twitch) did not play: it isn't streaming now
SITE_OFFLINE = N_("{channel} isn't live now")
# i18n: why a channel opened by its page did not play: its site (Kick, Twitch) has none so named
SITE_UNKNOWN = N_("No channel named {channel} on {site}")
# i18n: a site (Kick, Twitch) refused to give a channel's stream; its own link may be pasted
SITE_REFUSED = N_("{site} refused QiTV's request: try again later, or paste the stream link")
_SITE_TEXTS = {"offline": SITE_OFFLINE, "unknown": SITE_UNKNOWN, "refused": SITE_REFUSED}
_SITE_FIELDS = {
    "channel": r"(?P<channel>[a-z0-9_-]+)",  # (as live_sites reads a channel)
    "site": "(?P<site>" + "|".join(re.escape(name) for name in SITE_NAMES.values()) + ")",
}
_LIMIT_STATUS = (429, 458, 509)
_REFUSED_STATUS = (403, 407)
_GONE_STATUS = (404, 410)


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
    channel"; an extension and generic names such as index or playlist are skipped); a Kick
    or Twitch channel's page, its channel's ("examplechannel · Kick")."""
    page = site_page(url)
    if page is not None:
        return page_name(page)
    parts = urlsplit(url.strip())
    host = parts.hostname or url.strip()
    segments = [s for s in parts.path.split("/") if s]
    for segment in reversed(segments):
        stem = os.path.splitext(segment)[0]
        if stem and stem.lower() not in _GENERIC:
            return f"{host} — {stem}"
    return host


def is_link(item: dict) -> bool:
    """Whether a playing item is a link of Network streams: stamped as theirs, or its id is
    its own address's (a link of a configured account, stamped with that account)."""
    if provider_of(item) == NETWORK_PROVIDER:
        return True
    cmd = str(item.get("cmd") or "").strip()
    key = item.get("id")
    return bool(cmd) and isinstance(key, str) and key == stream_id(cmd)


def failure_reason(message: str, kind: str = "", status: Optional[int] = None) -> str:
    """Why a link did not play, in plain words (an English text), from what the player
    reported: its message, and how its buffer's input failed when it had one (`kind`, as the
    time-shift helper names it, and the HTTP `status`)."""
    if status == 401:
        return ACCESS_ENDED
    if message == STREAM_LIMIT or status in _LIMIT_STATUS:
        return BUSY
    if message == STREAM_REFUSED or status in _REFUSED_STATUS:
        return REFUSED
    if status in _GONE_STATUS:
        return GONE
    if kind == "timeout":
        return TIMED_OUT
    return FAILED


def site_failure(reason: str, page: SitePage) -> str:
    """Why the site gave no stream for the channel `page` (live_sites.SiteError's `reason`:
    "offline", "unknown", "refused"; "timeout", a site that never answered in time; "network"
    and anything else), in English, a site's own answer with the channel and the site in it
    (`shown_reason` shows it in the active language)."""
    if reason == "timeout":
        return TIMED_OUT
    text = _SITE_TEXTS.get(reason)
    if text is None:
        return FAILED  # not reached (no address, no connection, no TLS), and nothing more known
    return text.format(channel=page.channel, site=SITE_NAMES[page.site])


def is_site_failure(why: str) -> bool:
    """Whether `why` is one `site_failure` gives (a site's answer, or why it was not had)."""
    return why in (TIMED_OUT, FAILED) or names_its_channel(why)


def _site_reason(why: str) -> Optional[tuple[str, dict[str, str]]]:
    """The site's text `why` was made of (`site_failure`) and its fields; None for another."""
    for text in _SITE_TEXTS.values():
        found = _site_pattern(text).fullmatch(why)
        if found is not None:
            return text, found.groupdict()
    return None


def _site_pattern(text: str) -> re.Pattern[str]:
    pattern = re.escape(text)
    for field, group in _SITE_FIELDS.items():
        pattern = pattern.replace(re.escape("{" + field + "}"), group)
    return re.compile(pattern)


def names_its_channel(why: str) -> bool:
    """Whether why a link failed names its channel itself (a site's answer): said alone."""
    return _site_reason(why) is not None


def shown_reason(why: str) -> str:
    """Why a link failed (as it is kept: English) in the active language, a site's with the
    channel and the site it names."""
    found = _site_reason(why)
    if found is None:
        return translate(why)
    text, fields = found
    return translate(text, **fields)


def write(path: Path, data: bytes) -> None:
    """Write `data` to `path` whole or not at all (the folder is made when missing)."""
    os.makedirs(os.path.dirname(str(path)) or ".", exist_ok=True)
    with atomic_write(str(path)) as f:
        f.write(data)


def read_bytes(path: Path) -> Optional[bytes]:
    """The file's bytes (blocking: a worker's); None when there is none. One that cannot be
    read raises OSError."""
    try:
        return Path(path).read_bytes()
    except FileNotFoundError:
        return None


def save(path: Path, data: bytes, *, set_aside: bool = False) -> None:
    """Write the store's `data` to `path` (blocking: a worker's). `set_aside`: the file there
    could not be read; it is moved beside it first ("<name>.broken-<time>"), so nothing in it
    is lost. One that cannot be moved is never written over: OSError."""
    if set_aside and os.path.exists(path) and set_file_aside(str(path)) is None:
        raise OSError("the unreadable network streams file could not be set aside")
    write(path, data)


def _entry(row: Any) -> Optional[Entry]:
    """A stored row as an entry; None for one without an id or an address."""
    if not isinstance(row, dict):
        return None
    key, url = row.get("id"), row.get("url")
    if isinstance(key, bool) or not isinstance(key, (str, int)) or not str(key):
        return None
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        opened = float(row.get("opened") or 0.0)
    except TypeError, ValueError, OverflowError:  # (an integer too large for a time)
        opened = 0.0
    name, failed = row.get("name"), row.get("failed")
    return Entry(
        id=str(key),
        name=name if isinstance(name, str) else "",
        url=url,
        kind=FILM if row.get("kind") == FILM else LIVE,
        opened=opened if math.isfinite(opened) else 0.0,
        failed=failed if isinstance(failed, str) else "",
    )


class NetworkStreams:
    """The kept links of the file at `path`. Making one reads nothing: `load` (or `merge`)
    the file's bytes, read on a worker, and write `save_bytes()` there (on a worker too).
    `broken`: the last data loaded was not this file's (set it aside before writing)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._entries: dict[str, Entry] = {}
        self.broken = False

    @classmethod
    def read(cls, path: Path) -> NetworkStreams:
        """The store as the file at `path` holds it (blocking: a worker's, or a test's)."""
        store = cls(path)
        try:
            data = read_bytes(store.path)
        except OSError:
            store.broken = True
            return store
        if data is not None:
            store.load(data)
        return store

    def load(self, data: bytes) -> bool:
        """Replace the entries with those in `data`; False (and `broken`) when it is not this
        file's, read as none. A row without an id or an address is left out."""
        self._entries = {}
        try:
            doc = json.loads(data)
            rows = doc["entries"]
        except ValueError, TypeError, KeyError, RecursionError:
            rows = None
        if not isinstance(rows, list):
            logger.warning("Network streams file unreadable: starting empty")
            self.broken = True
            return False
        self.broken = False
        for row in rows:
            try:
                entry = _entry(row)
            except Exception as e:  # a row of another kind: left out, the others read
                logger.info("A network stream row could not be read (%s)", type(e).__name__)
                entry = None
            if entry is not None:
                self._entries[entry.id] = entry
        if len(self._entries) < len(rows):
            logger.info("Left out %d unreadable network stream(s)", len(rows) - len(self._entries))
        return True

    def merge(self, data: bytes) -> bool:
        """`load` the file's `data`, read after links played: what was kept meanwhile stays
        (its address, kind and times), under the name the file gave it."""
        since = self._entries
        loaded = False
        try:
            loaded = self.load(data)
        finally:  # what played meanwhile is never lost, whatever the file held
            for key, entry in since.items():
                stored = self._entries.get(key)
                if stored is not None:
                    entry.name = stored.name or entry.name
                self._entries[key] = entry
        return loaded

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
        and kind updated and its name kept. Only FILM is a film; any other kind is LIVE."""
        kind = FILM if kind == FILM else LIVE
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
        """It played at `when` (Unix seconds; a time that is no number counts as never): the
        last failure is forgotten."""
        if id in self._entries:
            self._entries[id].opened = when if math.isfinite(when) else 0.0
            self._entries[id].failed = ""

    def failed(self, id: str, why: str) -> None:
        if id in self._entries:
            self._entries[id].failed = why
