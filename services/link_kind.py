"""What a link is, from its address or its first bytes (no Qt, no network).

File > Open Link takes any link a person pastes. Most say what they are by their address: a
film file, a live `.ts`, an account's playlist or API link, a portal. A `.m3u8` (or any other
address over http) may be one stream or a list of channels, so `classify` answers PROBE and
the caller fetches the first 64 KB for `probe_text`.

A kind is FILM, LIVE, PROBE, NONE (not a link QiTV opens), or a `Provider` holding what
`parse_provider_link` read, for the "Add provider" offer. An Xtream stream path
(/live/U/P/1.ts) is one stream, not a provider, though `xtream_account` can read the whole
account out of it for an "Add the whole account" offer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Union
from urllib.parse import urlsplit

from services.provider_link import (
    M3U,
    ProviderLink,
    _first_line,
    _stream_login,
    parse_provider_link,
)

FILM, LIVE, PROBE, NONE = "FILM", "LIVE", "PROBE", "NONE"
_FILM_EXTENSIONS = (".mp4", ".mkv", ".avi", ".webm", ".mov", ".m4v")
_LIVE_SCHEMES = ("rtmp", "rtmps", "rtsp", "udp", "rtp")
PROBE_BYTES = 64 * 1024  # what a caller reads of a PROBE link for probe_text, at most


@dataclass(frozen=True)
class Provider:
    """A link that is a whole provider (an account, a playlist or a portal)."""

    link: ProviderLink


Kind = Union[str, Provider]


def address(text: str) -> str:
    """The link `classify` reads in `text`, as it plays: its first line, without the quotes or
    angle brackets around it."""
    return _first_line(text)


def host_of(url: object) -> str:
    """A web address's host and port, as a link's is compared with a provider's: lower case,
    without a login ("panel.example:8080"; "" for none). One typed without its scheme
    ("panel.example:8080/get.php") is read as http."""
    text = address(str(url or ""))
    if text and "://" not in text:
        text = f"http://{text}"
    try:
        netloc = urlsplit(text).netloc
    except ValueError:
        return ""
    return netloc.rpartition("@")[2].lower()


def _split(text: str):
    """The link's parts, or None for text that is not one address."""
    line = address(text)
    if not line or any(c.isspace() for c in line):
        return None
    try:
        parts = urlsplit(line)
        parts.port  # noqa: B018 - raises for a port that is not a number
    except ValueError:
        return None
    return parts


def _stream_kind(parts) -> Optional[tuple[str, ProviderLink]]:
    """(FILM or LIVE, the account) for an Xtream stream path, or None."""
    segments = [s for s in parts.path.split("/") if s]
    login = _stream_login(segments)
    if login is None:
        return None
    link = parse_provider_link(parts.geturl())
    if link is None:
        return None
    return (FILM if segments[login[0]].lower() in ("movie", "series") else LIVE), link


def classify(url: str) -> Kind:
    """What `url` is, from its address alone."""
    parts = _split(url)
    if parts is None or not parts.netloc:
        return NONE
    scheme = parts.scheme.lower()
    if scheme in _LIVE_SCHEMES:
        return LIVE
    if scheme not in ("http", "https") or not parts.hostname:
        return NONE
    stream = _stream_kind(parts)
    if stream is not None:
        return stream[0]
    path = parts.path.lower()
    if path.endswith(".m3u8"):
        return PROBE
    if path.endswith(_FILM_EXTENSIONS):
        return FILM
    if path.endswith(".ts"):
        return LIVE
    link = parse_provider_link(parts.geturl())
    if link is not None:
        return Provider(link)
    return PROBE


def xtream_account(url: str) -> Optional[ProviderLink]:
    """The whole account behind an Xtream stream link (/live|movie|series/U/P/<id>), or None."""
    parts = _split(url)
    if parts is None or parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    stream = _stream_kind(parts)
    return stream[1] if stream is not None else None


def probe_text(text: str, url: str = "") -> Kind:
    """What a fetched playlist is, from its first 64 KB: FILM when it has an end (a finished
    playlist), LIVE for any other HLS playlist (a master too), a Provider (an M3U playlist at
    `url`) for a list of channels with their own http(s) links and no HLS tags. Anything
    else is LIVE: VLC decides."""
    lines = [line.strip() for line in text[:PROBE_BYTES].lstrip("﻿").splitlines()]
    if any(line.startswith("#EXT-X-") for line in lines):
        vod = any(
            line.startswith("#EXT-X-ENDLIST") or line.upper() == "#EXT-X-PLAYLIST-TYPE:VOD"
            for line in lines
        )
        return FILM if vod else LIVE
    for at, line in enumerate(lines):
        if line.startswith("#EXTINF"):
            for nxt in lines[at + 1 :]:
                if not nxt or nxt.startswith("#"):
                    continue
                if nxt.lower().startswith(("http://", "https://")):
                    return Provider(ProviderLink(M3U, url))
                break
    return LIVE
