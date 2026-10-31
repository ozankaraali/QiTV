"""Which addresses a player may be handed: libVLC, an external VLC or mpv, an exported playlist.

A provider's link is the provider's to choose, and a hostile portal could name a share (on
Windows, opening file://server/share/… makes the system sign in to that server with the user's
password hash; smb:// and the like reach out too), a capture device (dshow://, v4l2://,
screen://) or another of VLC's own inputs. Only the schemes streams come in pass: http and
https, and the streaming ones IPTV lists use (RTMP, RTSP, RTP, UDP, MMS, SRT), and file:// URIs
of a path on this computer. A provider's local file plays on the user's own screen and reveals
nothing; a file URI naming another computer, a UNC path or a device is refused, but for QiTV's
own files (a recording, a file opened from the disk: the caller says `local`), wherever the
user keeps them.
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

from services.i18n import N_

# A player's refusal, as a provider's empty link: the service's own words, shown translated.
NO_STREAM_MESSAGE = N_("This item has no stream address")

STREAM_SCHEMES = frozenset(
    {
        "http",
        "https",
        "rtmp",
        "rtmps",
        "rtmpe",  # RTMP's encrypted and tunnelled variants VLC plays
        "rtmpt",
        "rtmpte",
        "rtmpts",
        "rtsp",
        "rtsps",
        "rtp",
        "udp",
        "mms",
        "mmsh",
        "mmst",
        "mmsu",
        "srt",
    }
)
# The scheme as libVLC reads it: from the address's first character, up to "://".
_SCHEME = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]{0,31})://")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")  # no address carries one; a line break would split it
_DEVICES = ("/dev/", "/proc/", "/sys/")  # a camera's or the system's, not a file to play


def scheme(url: object) -> str:
    """The address's scheme in lower case, "" when it starts with none."""
    match = _SCHEME.match(url) if isinstance(url, str) else None
    return match.group(1).lower() if match else ""


def _local_file(url: str) -> bool:
    """A file:// URI of a path on this computer: no host but this one's, no UNC path (two
    slashes, any backslash, as `Path.as_uri()` never writes one), no device."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.netloc.lower() not in ("", "localhost"):
        return False
    path = unquote(parts.path)
    return (
        path.startswith("/")
        and not path.startswith("//")
        and "\\" not in path
        and not (path.startswith(_DEVICES))
    )


def playable(url: object, *, local: bool = False) -> bool:
    """Whether `url` may be handed to a player: a stream's address in one of STREAM_SCHEMES,
    or a file:// URI of a path on this computer (with `local`, QiTV's own file's: any file://
    URI). A path without a scheme never is (libVLC would open it as a file, a UNC path too),
    nor an address with a control character."""
    if not isinstance(url, str) or _CONTROL.search(url):
        return False
    found = scheme(url)
    return found in STREAM_SCHEMES or (found == "file" and (local or _local_file(url)))


def shape(url: object) -> str:
    """What a log may say of a refused address: its scheme alone ("smb", "no scheme")."""
    return scheme(url) or "no scheme"
