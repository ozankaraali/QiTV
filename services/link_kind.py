"""What a link is, from its address or its first bytes (no Qt, no network).

File > Open Link takes any link a person pastes. Most say what they are by their address: a
film (a video file type QiTV opens from the disk, services.open_paths: it ends, so it seeks
and resumes), an Xtream live path, an account's playlist or API link, a portal. A `.m3u8` (or
any other address over http) may be one stream or a list of channels, and a song (.mp3,
.aac…) or a `.ts` or `.flv` may be a file or a stream that never ends (internet radio, a
live channel), so `classify` answers PROBE and the caller fetches the first 64 KB for
`probe_text`, with what the answer's headers say of it (a size: a file that ends).

A kind is FILM, LIVE, PROBE, NONE (not a link QiTV opens), or a `Provider` holding what
`parse_provider_link` read, for the "Add provider" offer. An Xtream stream path
(/live/U/P/1.ts) is one stream, not a provider, though `xtream_account` can read the whole
account out of it for an "Add the whole account" offer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Union
from urllib.parse import unquote, urlsplit

from services.open_paths import (
    EXTRA_MUSIC_EXTENSIONS,
    EXTRA_VIDEO_EXTENSIONS,
    MEDIA_EXTENSIONS,
)
from services.provider_link import (
    _STREAM_ID,
    M3U,
    XTREAM,
    ProviderLink,
    _first_line,
    _stream_login,
    parse_provider_link,
)

FILM, LIVE, PROBE, NONE = "FILM", "LIVE", "PROBE", "NONE"
# The audio internet radio is sent in, and the containers live channels come in: a file or a
# stream that never ends, as its answer says (final review I2, the coordinator's ruling).
_EITHER_EXTENSIONS = tuple(
    f".{ext}" for ext in ("mp3", "aac", "ogg", "opus", "m4a", "flac", "wav", "flv", "ts")
)
# Every other video and music file type QiTV opens from the disk is a file that ends (final
# review I2: a song played as a live channel replayed from its start for ever).
_FILM_EXTENSIONS = tuple(
    f".{ext}"
    for ext in sorted(MEDIA_EXTENSIONS | EXTRA_VIDEO_EXTENSIONS | EXTRA_MUSIC_EXTENSIONS)
    if f".{ext}" not in _EITHER_EXTENSIONS
)
_LIVE_SCHEMES = ("rtmp", "rtmps", "rtsp", "udp", "rtp")
PROBE_BYTES = 64 * 1024  # what a caller reads of a PROBE link for probe_text, at most
_WEB_PORTS = {"http": "80", "https": "443"}  # each scheme's own port: the host without it


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
    """A web address's host and port, as a link's is compared with a provider's (and as a
    provider's stream hosts are kept, services.catalog): case-folded, without a login and
    without its scheme's own port, as `stream_id` reads it ("panel.example:8080"; "" for
    none). `panel.example:80` over http is `panel.example`, as is `panel.example:443` over
    https, and an address at a host over http is at that host over https too. One typed
    without its scheme ("panel.example:8080/get.php") is read as http."""
    text = address(str(url or ""))
    if text and "://" not in text:
        text = f"http://{text}"
    try:
        parts = urlsplit(text)
    except ValueError:
        return ""
    host = parts.netloc.rpartition("@")[2].casefold()
    name, colon, port = host.rpartition(":")
    if colon and port == _WEB_PORTS.get(parts.scheme.lower()):
        return name  # its scheme's own port, written out ("[::1]:80" is "[::1]")
    return host


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
    if path.endswith((".m3u8",) + _EITHER_EXTENSIONS):
        return PROBE
    if path.endswith(_FILM_EXTENSIONS):
        return FILM
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


def probe_text(
    text: str, url: str = "", content_type: str = "", length: Optional[int] = None
) -> Kind:
    """What a fetched link is, from its first 64 KB and its answer's `content_type` and
    `length` (its Content-Length; None: not given): FILM when it has an end (a finished
    playlist), LIVE for any other HLS playlist (a master too), a Provider (an M3U playlist at
    `url`) for a list of channels with their own http(s) links and no HLS tags. Any other
    answer with a size is a file that ends, a FILM (a song or a film whose address has no
    extension), unless it is text or a playlist. Anything else is LIVE (a stream that never
    ends: internet radio, a live channel): VLC decides, and so is a list read without its
    address (no provider to offer)."""
    lines = [line.strip() for line in text[:PROBE_BYTES].lstrip("\ufeff").splitlines()]
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
                    return Provider(ProviderLink(M3U, url)) if url.strip() else LIVE
                break
    playlist = any(line.startswith(("#EXTM3U", "#EXTINF")) for line in lines)
    return FILM if not playlist and _ends(content_type, length) else LIVE


def _ends(content_type: str, length: Optional[int]) -> bool:
    """Whether an answer is a file that ends: its size is given (a stream that never ends has
    none), and it is neither text (a page saying why) nor a playlist."""
    if length is None or length <= 0:
        return False
    kind = content_type.partition(";")[0].strip().lower()
    return not (kind.startswith("text/") or "mpegurl" in kind)


def _login_user(url: str) -> Optional[str]:
    """The user name an Xtream stream link carries: /live|movie|series/U/P/<id>, or the short
    /U/P/<id> Xtream also serves live channels at; None for any other link."""
    parts = _split(url)
    if parts is None or parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    segments = [s for s in parts.path.split("/") if s]
    login = _stream_login(segments)
    if login is not None:
        return login[1]
    if len(segments) >= 3 and _STREAM_ID.match(segments[-1]):
        return unquote(segments[-3])
    return None


def owner_of(url: str, providers: Iterable[tuple[dict, set[str]]]) -> str:
    """The name of the configured Xtream account whose stream `url` is ("" for none): a
    provider of `providers` ((its settings, the hosts its address and streams are at, as
    `host_of` gives them)) whose host the link's is and whose user name it carries (any case).
    Only against a configured account is the short form /U/P/<id> told from another path."""
    user = _login_user(url)
    host = host_of(url)
    if user is None or not host:
        return ""
    for provider, hosts in providers:
        if str(provider.get("type") or "").upper() != XTREAM or host not in hosts:
            continue
        name = str(provider.get("username") or "").strip()
        if name and name.casefold() == user.casefold():
            return str(provider.get("name") or "")
    return ""
