"""Paths the app is asked to open (launch arguments, dropped files) and their kinds."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
import logging
import ntpath
import os
import re
import sys
from typing import Optional

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = frozenset(
    {
        "mp4", "m4v", "mkv", "avi", "mov", "webm", "ts", "m2ts", "mts",
        "mpg", "mpeg", "flv", "wmv", "ogv",
    }
)  # fmt: skip
MUSIC_EXTENSIONS = frozenset(
    {"mp3", "m4a", "aac", "flac", "ogg", "opus", "wav", "aif", "aiff", "aifc"}
)
# 2.11.1: the other audio formats libVLC 3 plays that people keep music in, each checked with
# a test tone (Windows Media Audio, WavPack, Matroska audio, Core Audio, Musepack, True Audio,
# Sun audio, AMR, AC-3, DTS). QiTV registers none of them with the system: they play when they
# are handed over, dropped, opened with Open File… or in a folder or a playlist, and they are
# songs as the registered ones are. (Monkey's Audio's .ape is left out: none could be made to
# check it.)
OTHER_MUSIC_EXTENSIONS = frozenset(
    {"wma", "wv", "mka", "caf", "mpc", "tta", "au", "snd", "amr", "ac3", "dts"}
)
# Open File's filter
MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | MUSIC_EXTENSIONS | OTHER_MUSIC_EXTENSIONS
# The other extensions a type QiTV registers for covers (services/file_types.py registers
# VIDEO_EXTENSIONS and MUSIC_EXTENSIONS): on macOS 13 the QuickTime movie is .mov and .qt,
# MPEG-4 .mp4 and .mpg4, AVI .avi and .vfw, MPEG .mpe, .m75 and .m15 too, FLV .f4v, .f4p, .f4a
# and .f4b too, and an Ogg video may be .ogm; MP3 is .mpga too, AAC .adts, a wave .wave and
# .bwf, and Ogg audio .oga (AIFF is .aif and .aiff, AIFF-C .aifc: nothing more). Linux's MIME
# types (shared-mime-info's globs) bring: video/mpeg .vob, .mpe and .mp2 (MPEG audio layer 2:
# music), video/vnd.avi .avf and .divx, video/quicktime .qt, .moov and .qtvr, video/mp4 .f4v
# and .lrv, video/mp2t (Open With) .m2t, audio/mpeg .mpga, audio/aac .adts, the Ogg audio
# types .oga and Speex's .spx, audio/x-aifc .aiffc, and an audiobook's .m4b (audio/x-m4b, a
# kind of audio/mp4: Open With offers QiTV for it). The system may hand them over: they play
# too. (On macOS 13 an .m4b is protected MPEG-4 audio, and Windows lists the extensions QiTV
# registers: there only a drop or "Other…" opens one.)
EXTRA_VIDEO_EXTENSIONS = frozenset(
    {
        "qt", "mpg4", "vfw", "mpe", "m75", "m15", "f4v", "f4p", "ogm",
        "vob", "avf", "divx", "moov", "qtvr", "lrv", "m2t",
    }
)  # fmt: skip
EXTRA_MUSIC_EXTENSIONS = (
    frozenset({"f4a", "f4b", "mp2", "mpga", "adts", "wave", "bwf", "oga", "spx", "aiffc", "m4b"})
    | OTHER_MUSIC_EXTENSIONS
)
_VIDEO = VIDEO_EXTENSIONS | EXTRA_VIDEO_EXTENSIONS
_MUSIC = MUSIC_EXTENSIONS | EXTRA_MUSIC_EXTENSIONS
_SONGS = MUSIC_EXTENSIONS | OTHER_MUSIC_EXTENSIONS
SUBTITLE_EXTENSIONS = (".srt", ".ass", ".ssa", ".vtt", ".sub")
PLAYLIST_EXTENSIONS = (".m3u", ".m3u8")

_DIGITS = re.compile(r"(\d+)")
_EXTENDED = "\\\\?\\"  # Windows' extended-length prefix, \\?\
_DEVICE = "\\\\.\\"


@dataclass(frozen=True)
class Sorted:
    """Opened paths by kind: media in natural order, subtitles, playlists, and the others
    (files QiTV won't play, or folders: only the disk tells them apart), as given."""

    media: tuple[str, ...]
    subtitles: tuple[str, ...]
    playlists: tuple[str, ...]
    others: tuple[str, ...] = ()


def launch_paths(argv: Sequence[str]) -> list[str]:
    """The absolute paths of the existing files and folders among `argv[1:]` (a folder plays
    as a list: TVWindow.open_paths), spelled as they were given
    (a relative one made absolute): a link stays the link, not the file it points to, and a
    mapped drive stays its letter (`Z:\\ep.mkv`, never `\\\\server\\share\\ep.mkv`), so a
    launch names a file as a hand-over or DDE does.

    Anything starting with `-` is skipped (macOS's `-psn_...` too), and so is the value after
    `--replace`. On Windows a path past 260 characters may only be found in its extended
    spelling (a QiTV that isn't long-path aware): that is looked at too, and the plain
    spelling kept.
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
            path = os.path.abspath(arg)
            if _is_file(path) or folder_spelling(path) is not None:
                found.append(path)
        except OSError, ValueError:
            logger.debug("launch argument is not a readable path: %r", arg)
    return found


def _is_file(path: str) -> bool:
    if os.path.isfile(path):
        return True
    if sys.platform != "win32":
        return False
    extended = extended_path(path)
    return extended != path and os.path.isfile(extended)


def folder_spelling(path: str) -> Optional[str]:
    """`path` as the disk reads it as a folder, or None when it is no folder: on Windows a
    path past 260 characters may only be read in its extended spelling. It touches the disk:
    never on the GUI thread but at a start (launch_paths)."""
    if os.path.isdir(path):
        return path
    if sys.platform != "win32":
        return None
    extended = extended_path(path)
    return extended if extended != path and os.path.isdir(extended) else None


def extended_path(path: str) -> str:
    """Windows path `path` with the extended-length prefix: `\\\\?\\C:\\…`, or
    `\\\\?\\UNC\\server\\share\\…` for a share. Windows' file calls take it past 260
    characters in a process that isn't long-path aware. The prefix turns Windows' own clean-up
    off, so the path is first made absolute, with backslashes and no "." or "..". A path that
    is prefixed already (or a device's) stays; so does one without a drive or a share.
    """
    if path.startswith((_EXTENDED, _DEVICE)):
        return path
    full = ntpath.normpath(ntpath.abspath(path))
    if full.startswith((_EXTENDED, _DEVICE)):
        return full  # one spelled with forward slashes
    drive, _rest = ntpath.splitdrive(full)
    if drive.startswith("\\\\"):
        return f"{_EXTENDED}UNC\\{full[2:]}"
    return f"{_EXTENDED}{full}" if drive else path


def _extension(path: str) -> str:
    return os.path.splitext(path)[1].lower()


def natural_key(path: str) -> tuple:
    """A natural, case-insensitive sort key on the file name (`Ep 2` before `Ep 10`). A run of
    decimal digits counts as its number, in any script (`١٢`, `１２`); a superscript or circled
    digit (`²`, `①`) is a digit to `str.isdigit` but no decimal one: it sorts as text."""
    name = re.split(r"[\\/]", path)[-1].casefold()
    return tuple(
        (0, int(part), "") if part.isdecimal() else (1, 0, part)
        for part in _DIGITS.split(name)
        if part
    )


def is_video(path: str) -> bool:
    return _extension(path)[1:] in _VIDEO


def is_music(path: str) -> bool:
    return _extension(path)[1:] in _MUSIC


def is_song(path: str) -> bool:
    """A song by its extension (MUSIC_EXTENSIONS and OTHER_MUSIC_EXTENSIONS, in any case): a
    playlist of these only is an album (TVController.play_list). is_music also takes the kinds
    a type QiTV registers brings (.f4a, .f4b, .mp2)."""
    return _extension(path)[1:] in _SONGS


def sort_paths(paths: Iterable[str]) -> Sorted:
    """Split `paths` by kind; playlists are checked first, and what is none of the kinds is
    one of the others."""
    media: list[str] = []
    subtitles: list[str] = []
    playlists: list[str] = []
    others: list[str] = []
    for path in paths:
        if not path:
            continue
        ext = _extension(path)
        if ext in PLAYLIST_EXTENSIONS:
            playlists.append(path)
        elif ext in SUBTITLE_EXTENSIONS:
            subtitles.append(path)
        elif is_video(path) or is_music(path):
            media.append(path)
        else:
            others.append(path)
    media.sort(key=natural_key)
    return Sorted(tuple(media), tuple(subtitles), tuple(playlists), tuple(others))


def holds_the_start(paths: Iterable[str]) -> bool:
    """Whether a start with `paths` holds what would open over a film (the last channel, the
    drawer, the welcome card): it carries a media file, or a file QiTV won't play, whose
    message comes instead, or a folder (one of the others too), which plays as a list or says
    it has nothing to play (Ruling P4-3: such a read always holds the start as a film's).
    Subtitle and playlist files alone start as any start does."""
    kinds = sort_paths(paths)
    return bool(kinds.media or kinds.others)


def reads_the_start(paths: Iterable[str]) -> bool:
    """Whether a start with `paths` waits for its playlists to be read before it starts: it
    carries one and no media file (a media file plays instead, and its playlists only add
    their providers). One of local files then holds the start as a film does; a provider's
    lets it start as any start does (TVWindow._on_playlists_read)."""
    kinds = sort_paths(paths)
    return bool(kinds.playlists) and not kinds.media
