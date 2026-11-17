"""A playlist file opened in QiTV (.m3u, .m3u8), read to tell what it is (spec §1, §11).

- Any network link (http, https, rtmp, rtsp, udp…) on a line of its own: a provider's list,
  added as a dropped playlist is.
- An HLS tag (`#EXT-X-…`) anywhere: one stream, played as one film.
- Otherwise its lines name local files: file URLs, absolute paths or paths relative to the
  playlist's folder (`\\` taken as a separator too, as a list written on Windows has it). The
  media files among them that are there play as a list, in its order; the others are counted
  as missing.

Pure logic: no Qt. `read_playlist` touches the disk, so call it only on a worker.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import ntpath
import os
import re
import stat
import sys
from typing import Optional
from urllib.parse import unquote, urlsplit

from services.open_paths import extended_path, is_music, is_video

logger = logging.getLogger(__name__)

PROVIDER, HLS, LOCAL, EMPTY = "provider", "hls", "local", "empty"

# A link's scheme (RFC 3986). One letter is a Windows drive ("C://x"), never a link.
_LINK = re.compile(r"([A-Za-z][A-Za-z0-9+.-]+)://")
# A list this large is a provider's (a local one names a few thousand files at most): it is
# not read, and becomes a provider as before.
MAX_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class PlaylistRead:
    """What a playlist is: `kind` (PROVIDER, HLS, LOCAL or EMPTY), its `name` (the file's stem),
    the absolute paths of its local media files that are there (`entries`, in its order) with
    their `sizes` (None where it couldn't be read), and how many of its lines name nothing that
    plays (`missing`)."""

    kind: str
    name: str
    entries: tuple[str, ...] = ()
    missing: int = 0
    sizes: tuple[Optional[int], ...] = ()


def read_playlist(path: str) -> PlaylistRead:
    """What the playlist at `path` is. Never raises: one that can't be read is EMPTY. Blocking
    disk I/O: call on a worker."""
    name = os.path.splitext(os.path.basename(path))[0]
    try:
        with open(path, "rb") as file:
            data = file.read(MAX_BYTES + 1)
    except Exception as error:
        logger.info("A playlist opened in QiTV could not be read (%s)", type(error).__name__)
        return PlaylistRead(EMPTY, name)
    if len(data) > MAX_BYTES:
        return PlaylistRead(PROVIDER, name)
    lines = [line.strip() for line in _decode(data).splitlines()]
    if any(line.startswith("#EXT-X-") for line in lines):
        return PlaylistRead(HLS, name)
    named = [line for line in lines if line and not line.startswith("#")]
    for line in named:
        link = _LINK.match(line)
        if link is not None and link.group(1).lower() != "file":
            return PlaylistRead(PROVIDER, name)
    folder = os.path.dirname(os.path.abspath(path))
    entries: list[str] = []
    sizes: list[Optional[int]] = []
    for line in named:
        try:
            entry = _local_path(line, folder)
            size = _media_size(entry) if entry is not None else None
        except Exception as error:  # an odd line never ends the read
            logger.debug("A playlist line could not be looked at (%s)", type(error).__name__)
            entry, size = None, None
        if entry is not None and size is not None:
            entries.append(entry)
            sizes.append(size)
    missing = len(named) - len(entries)
    if not entries:
        return PlaylistRead(EMPTY, name, missing=missing)
    return PlaylistRead(LOCAL, name, tuple(entries), missing, tuple(sizes))


def _decode(data: bytes) -> str:
    """UTF-8 (with or without its BOM), else Windows' Western code page, as older players wrote
    .m3u files; Latin-1 never fails."""
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _local_path(line: str, folder: str) -> Optional[str]:
    """The absolute path a playlist line names, or None when it names none."""
    if _LINK.match(line):
        return _file_url_path(line)
    if sys.platform == "win32":
        return os.path.abspath(os.path.join(folder, line))
    if ntpath.splitdrive(line)[0]:
        return None  # a Windows drive's ("C:\\…"), on another system
    return os.path.abspath(os.path.join(folder, line.replace("\\", "/")))


def _file_url_path(url: str) -> Optional[str]:
    """The path of a file URL (percent-encoded): file:///home/a.mp3, file://localhost/…, and on
    Windows file:///C:/a.mp3 or a share's file://server/share/a.mp3."""
    parts = urlsplit(url)
    path = unquote(parts.path)
    host = parts.netloc
    if sys.platform == "win32":
        if host and host.lower() != "localhost":
            return ntpath.normpath(f"\\\\{host}{path}")
        if re.match(r"/[A-Za-z]:", path):
            path = path[1:]
        return ntpath.normpath(path) if path else None
    if host and host.lower() != "localhost":
        return None  # another computer's file
    return os.path.normpath(path) if path.startswith("/") else None


def _media_size(path: str) -> Optional[int]:
    """The size of the media file at `path`; None when it is no media file or isn't there
    (gone, a folder, a broken link). On Windows a path past 260 characters may only be found
    in its extended spelling, as TVWindow's file check finds it."""
    if not (is_video(path) or is_music(path)):
        return None
    size = _file_size(path)
    if size is None and sys.platform == "win32":
        extended = extended_path(path)
        if extended != path:
            size = _file_size(extended)
    return size


def _file_size(path: str) -> Optional[int]:
    try:
        info = os.stat(path)
    except OSError, ValueError:
        return None
    return info.st_size if stat.S_ISREG(info.st_mode) else None
