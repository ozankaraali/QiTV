"""One provider link, read for the add-provider form (QiTV 2.2; no Qt).

Providers hand out a single link more often than the form's fields:
- an Xtream panel's playlist or API link, `…/get.php?username=U&password=P&type=m3u_plus…` or
  `…/player_api.php?username=U&password=P` (also `xmltv.php`, or a stream's
  `/live/U/P/123.ts`): an Xtream provider, its server (scheme, host, port and the path the
  panel is served under), username and password;
- a playlist's address, ending in .m3u or .m3u8: an M3U playlist (also under a `/c/`);
- an STB portal's address, with `/c/` or `/stalker_portal/` in it: an STB provider, its portal
  address up to that part (and its MAC when the link names one).

Values are percent-decoded only (a "+" in a password stays one).

The text may come from the clipboard or a drop: it is never logged, and a `ProviderLink`'s
repr hides the password.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Optional
from urllib.parse import unquote, urlsplit, urlunsplit

XTREAM, M3U, STB = "XTREAM", "M3UPLAYLIST", "STB"
_XTREAM_FILES = ("get.php", "player_api.php", "xmltv.php", "panel_api.php")
_XTREAM_STREAMS = ("live", "movie", "series", "timeshift")
_STREAM_ID = re.compile(r"^\d+(?:\.\w+)?$")  # a stream's last part: 123, 123.ts, 123.m3u8
_PLAYLIST_SUFFIXES = (".m3u", ".m3u8")
_MAC = re.compile(r"^[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}$")
_SCHEMES = ("http", "https")


@dataclass(frozen=True)
class ProviderLink:
    type: str  # XTREAM, M3U or STB, as the providers list names them
    url: str  # the server (Xtream), the playlist (M3U) or the portal (STB)
    username: str = ""
    password: str = field(default="", repr=False)
    mac: str = ""

    @property
    def name(self) -> str:
        """A name for the provider: its host (the user can change it)."""
        return urlsplit(self.url).hostname or ""


_WRAPPERS = {"<": ">", '"': '"', "'": "'"}


def _first_line(text: str) -> str:
    """The text's first non-empty line; quotes or angle brackets around the whole of it go
    (a password may hold such characters: nothing inside is touched)."""
    for line in str(text or "").splitlines():
        line = line.strip()
        if len(line) >= 2 and _WRAPPERS.get(line[0]) == line[-1]:
            line = line[1:-1].strip()
        if line:
            return line
    return ""


def _query(query: str) -> dict[str, str]:
    """The query's values by lower-case name (the first of each), percent-decoded only: a "+"
    in a password stays a "+"."""
    values: dict[str, str] = {}
    for part in query.split("&"):
        key, _, value = part.partition("=")
        if key:
            values.setdefault(unquote(key).lower(), unquote(value))
    return values


def _stream_login(segments: list[str]) -> Optional[tuple[int, str, str]]:
    """An Xtream stream's path, `[prefix/]live/U/P/123.ts` (movie, series, timeshift/U/P/…):
    (where its kind is, its username, its password), or None."""
    for at, segment in enumerate(segments):
        if segment.lower() in _XTREAM_STREAMS and len(segments) - at >= 4:
            if _STREAM_ID.match(segments[-1]):
                return at, unquote(segments[at + 1]), unquote(segments[at + 2])
            return None
    return None


def parse_provider_link(text: str) -> Optional[ProviderLink]:
    """The provider `text` (a pasted or dropped link) describes, or None."""
    line = _first_line(text)
    if not line or any(c.isspace() for c in line):
        return None
    if "://" not in line:
        line = f"http://{line}"  # "panel.example:8080/get.php?…" as typed
    try:
        parts = urlsplit(line)
        port = parts.port  # raises for a port that is not a number
    except ValueError:
        return None
    if parts.scheme.lower() not in _SCHEMES or not parts.hostname:
        return None
    netloc = parts.hostname if port is None else f"{parts.hostname}:{port}"
    if ":" in parts.hostname:  # an IPv6 literal keeps its brackets
        netloc = f"[{parts.hostname}]" if port is None else f"[{parts.hostname}]:{port}"
    server = urlunsplit((parts.scheme.lower(), netloc, "", "", ""))
    path = parts.path or ""
    lower = path.lower()
    query = _query(parts.query)
    segments = [s for s in path.split("/") if s]
    last = segments[-1].lower() if segments else ""
    if last in _XTREAM_FILES and query.get("username") and query.get("password"):
        prefix = "/".join(segments[:-1])  # a panel served under a path keeps it
        base = f"{server}/{prefix}" if prefix else server
        return ProviderLink(XTREAM, base, query["username"], query["password"])
    login = _stream_login(segments)
    if login is not None:
        at, username, password = login
        base = f"{server}/{'/'.join(segments[:at])}" if at else server
        return ProviderLink(XTREAM, base, username, password)
    playlist = last == "get.php" and "m3u" in query.get("type", "")
    if lower.endswith(_PLAYLIST_SUFFIXES) or playlist:  # before a portal: …/c/news.m3u
        return ProviderLink(M3U, urlunsplit(parts._replace(fragment="")))
    portal = _portal(path)
    if portal is not None:
        mac = query.get("mac", "")
        return ProviderLink(STB, f"{server}{portal}", mac=mac.upper() if _MAC.match(mac) else "")
    return None


def _portal(path: str) -> Optional[str]:
    """An STB portal's address part of `path`: up to and with its "/c/" ("/c" at the end gets
    its slash), or up to "/stalker_portal/" when it has no "/c"; None for no portal."""
    lower = path.lower()
    found = lower.find("/c/")
    if found >= 0:
        return path[: found + len("/c/")]
    if lower.endswith("/c"):
        return f"{path}/"
    found = lower.find("/stalker_portal")
    if found >= 0:
        return f"{path[: found + len('/stalker_portal')]}/"
    return None
