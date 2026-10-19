"""One provider link, read for the add-provider form (QiTV 2.2; no Qt).

Providers hand out a single link more often than the form's fields:
- an Xtream panel's playlist or API link, `…/get.php?username=U&password=P&type=m3u_plus…` or
  `…/player_api.php?username=U&password=P` (also `xmltv.php`, or a stream's
  `/live/U/P/123.ts`): an Xtream provider, its server (scheme, host and port), username and
  password;
- a playlist's address, ending in .m3u or .m3u8: an M3U playlist;
- an STB portal's address, with `/c/` or `/stalker_portal/` in it: an STB provider, its portal
  address up to that part (and its MAC when the link names one).

The text may come from the clipboard or a drop: it is never logged, and a `ProviderLink`'s
repr hides the password.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Optional
from urllib.parse import parse_qs, urlsplit, urlunsplit

XTREAM, M3U, STB = "XTREAM", "M3UPLAYLIST", "STB"
_XTREAM_FILES = ("get.php", "player_api.php", "xmltv.php", "panel_api.php")
_XTREAM_STREAMS = ("live", "movie", "series", "timeshift")
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


def _first_line(text: str) -> str:
    for line in str(text or "").splitlines():
        line = line.strip().strip("<>\"'")
        if line:
            return line
    return ""


def _query(query: str) -> dict[str, str]:
    return {key.lower(): values[0] for key, values in parse_qs(query).items() if values}


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
        return ProviderLink(XTREAM, server, query["username"], query["password"])
    if len(segments) == 4 and segments[0].lower() in _XTREAM_STREAMS:
        return ProviderLink(XTREAM, server, segments[1], segments[2])  # /live/U/P/123.ts
    portal = _portal(path)
    if portal is not None:
        mac = query.get("mac", "")
        return ProviderLink(STB, f"{server}{portal}", mac=mac.upper() if _MAC.match(mac) else "")
    playlist = last == "get.php" and "m3u" in query.get("type", "")
    if lower.endswith(_PLAYLIST_SUFFIXES) or playlist:
        return ProviderLink(M3U, urlunsplit(parts._replace(fragment="")))
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
