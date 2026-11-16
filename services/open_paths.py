"""Paths the app is asked to open (launch arguments, dropped files) and their kinds."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import re

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = frozenset(
    {
        "mp4", "m4v", "mkv", "avi", "mov", "webm", "ts", "m2ts", "mts",
        "mpg", "mpeg", "flv", "wmv", "ogv",
    }
)  # fmt: skip
MUSIC_EXTENSIONS = frozenset({"mp3", "m4a", "aac", "flac", "ogg", "opus", "wav"})
MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | MUSIC_EXTENSIONS
SUBTITLE_EXTENSIONS = (".srt", ".ass", ".ssa", ".vtt", ".sub")
PLAYLIST_EXTENSIONS = (".m3u", ".m3u8")

_DIGITS = re.compile(r"(\d+)")


@dataclass(frozen=True)
class Sorted:
    """Opened paths by kind: media in natural order, subtitles, playlists."""

    media: tuple[str, ...]
    subtitles: tuple[str, ...]
    playlists: tuple[str, ...]


def launch_paths(argv: Sequence[str]) -> list[str]:
    """The absolute paths of the existing files among `argv[1:]`.

    Anything starting with `-` is skipped (macOS's `-psn_...` too), and so is the value after
    `--replace`.
    """
    found: list[str] = []
    skip = False
    for arg in argv[1:]:
        if skip:
            skip = False
            continue
        if arg == "--replace":
            skip = True
            continue
        if not arg or arg.startswith("-"):
            continue
        try:
            path = Path(arg).resolve()
            if path.is_file():
                found.append(str(path))
        except OSError, ValueError:
            logger.debug("launch argument is not a readable path: %r", arg)
    return found


def _extension(path: str) -> str:
    return os.path.splitext(path)[1].lower()


def natural_key(path: str) -> tuple:
    """A natural, case-insensitive sort key on the file name (`Ep 2` before `Ep 10`)."""
    name = re.split(r"[\\/]", path)[-1].casefold()
    return tuple(
        (0, int(part), "") if part.isdigit() else (1, 0, part)
        for part in _DIGITS.split(name)
        if part
    )


def is_video(path: str) -> bool:
    return _extension(path)[1:] in VIDEO_EXTENSIONS


def is_music(path: str) -> bool:
    return _extension(path)[1:] in MUSIC_EXTENSIONS


def sort_paths(paths: Iterable[str]) -> Sorted:
    """Split `paths` by kind; playlists are checked first, anything else is dropped."""
    media: list[str] = []
    subtitles: list[str] = []
    playlists: list[str] = []
    for path in paths:
        if not path:
            continue
        ext = _extension(path)
        if ext in PLAYLIST_EXTENSIONS:
            playlists.append(path)
        elif ext in SUBTITLE_EXTENSIONS:
            subtitles.append(path)
        elif ext[1:] in MEDIA_EXTENSIONS:
            media.append(path)
    media.sort(key=natural_key)
    return Sorted(tuple(media), tuple(subtitles), tuple(playlists))
