"""A playlist file opened in QiTV (.m3u, .m3u8), read to tell what it is (spec §1, §11).

- Any network link (http, https, rtmp, rtsp, udp…) on a line of its own: a provider's list,
  added as a dropped playlist is.
- An HLS tag (`#EXT-X-…`) anywhere: one stream, played as one film.
- Otherwise its lines name local files: file URLs, absolute paths or paths relative to the
  playlist's folder (`\\` taken as a separator too, as a list written on Windows has it). The
  media files among them that are there play as a list, in its order; the others are counted
  as missing.

A folder opened in QiTV (`read_folder`, Task 11) is read as such a list too, and so are several
media files dropped together (`read_files`), in one order for the whole list:
- when every video is an episode of one show (Plan 3's parse and its same-show sense: a bare
  name takes its folder's show), the videos go by season and episode, in the places natural
  order gives the videos, and the songs among them stay where natural order puts them;
- a list of songs only goes by its tags (disc, then track) when every song was read, has a
  track number and they name one album at most; otherwise, and for anything else, by name
  (`natural_key`: numbered names in their numbers' order).
Only the folder's own files are read, never its subfolders, and no hidden one (".…"). The
folder is always listed whole (as album_listing.music_files lists a song's): only the songs'
tags have a time (READ_BUDGET_S), past which they go by name.

Pure logic: no Qt. Each read touches the disk (the songs' tags are read by libVLC), so call
it only on a worker; `subtitles_for` alone is pure.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import logging
import ntpath
import os
import re
import stat
import sys
import time
from typing import Any, Optional
from urllib.parse import unquote, urlsplit

from services.album_listing import LIST_CAP, read_tracks
from services.local_episodes import folder_show, parse_episode, same_show
from services.open_paths import extended_path, folder_spelling, is_music, is_video, natural_key
from services.song_facts import album_key, track_number

logger = logging.getLogger(__name__)

PROVIDER, HLS, LOCAL, EMPTY = "provider", "hls", "local", "empty"
UNREADABLE = "unreadable"  # gone, or refused: the playlist itself couldn't be read
OTHER = "other"  # opened as a folder, but a file QiTV won't play (read_folder)

# A list's songs' tags (after its listing, which is never cut short: a share waking up says no
# folder is empty) are read for this long at most, then they go by name. It comes before the
# first sound (the album's listing comes after it, with album_listing.LIST_BUDGET_S), so it
# is shorter.
READ_BUDGET_S = 3.0
TAGS_CAP = LIST_CAP  # more songs than this: by name, no tag read

# A link's scheme (RFC 3986). One letter is a Windows drive ("C://x"), never a link.
_LINK = re.compile(r"([A-Za-z][A-Za-z0-9+.-]+)://")
# A list this large is a provider's (a local one names a few thousand files at most): it is
# not read, and becomes a provider as before.
MAX_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class PlaylistRead:
    """What a playlist is: `kind` (PROVIDER, HLS, LOCAL, EMPTY or UNREADABLE), its `name` (the
    file's stem), the absolute paths of its local media files that are there (`entries`, in
    its order) with their `sizes` (None where it couldn't be read), and how many of its lines
    name nothing that plays (`missing`). A folder's (read_folder) is named after the folder
    and is LOCAL, EMPTY, UNREADABLE or OTHER; several files' (read_files) after the first's
    folder."""

    kind: str
    name: str
    entries: tuple[str, ...] = ()
    missing: int = 0
    sizes: tuple[Optional[int], ...] = ()


def read_playlist(path: str) -> PlaylistRead:
    """What the playlist at `path` is. Never raises: one that can't be read (gone, refused) is
    UNREADABLE. Blocking disk I/O: call on a worker."""
    name = os.path.splitext(os.path.basename(path))[0]
    try:
        with open(path, "rb") as file:
            data = file.read(MAX_BYTES + 1)
    except Exception as error:
        logger.info("A playlist opened in QiTV could not be read (%s)", type(error).__name__)
        return PlaylistRead(UNREADABLE, name)
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


def read_folder(path: str, *, budget_s: float = READ_BUDGET_S, cap: int = TAGS_CAP) -> PlaylistRead:
    """The folder at `path` as a list (the module docstring), named after it: LOCAL, EMPTY when
    none of its own files plays, UNREADABLE when it is gone or refused, OTHER when `path` is a
    file (one QiTV won't play: the router opens media files itself). It is listed whole; its
    songs' tags are read for `budget_s` from then on, `cap` songs at most (0: none, by name).
    Never raises; blocking disk I/O: call on a worker."""
    folder = path.rstrip("/\\") or path  # macOS's Finder ends a dropped folder with "/"
    name = os.path.basename(folder) or folder
    try:
        spelling = folder_spelling(path)
        if spelling is None:
            return PlaylistRead(OTHER if os.path.exists(path) else UNREADABLE, name)
        found = _listing(spelling, path)
    except Exception as error:  # gone meanwhile, or refused: it says it can't be opened
        logger.info("A folder opened in QiTV could not be read (%s)", type(error).__name__)
        return PlaylistRead(UNREADABLE, name)
    if not found:
        return PlaylistRead(EMPTY, name)
    return PlaylistRead(LOCAL, name, *_columns(_ordered(found, budget_s, cap)))


def read_files(
    paths: Sequence[str], *, budget_s: float = READ_BUDGET_S, cap: int = TAGS_CAP
) -> PlaylistRead:
    """Several media files opened together (a drop) as one list in the module docstring's
    order, named after its first one's folder: LOCAL with those not there counted as
    `missing`, or UNREADABLE when none is. Its songs' tags are read as read_folder reads a
    folder's. Never raises; blocking: call on a worker."""
    found: list[tuple[str, Optional[int]]] = []
    for path in paths:
        try:
            size = _media_size(path)
        except Exception:  # an odd path never ends the read
            size = None
        if size is not None:
            found.append((path, size))
    missing = len(paths) - len(found)
    if not found:
        return PlaylistRead(UNREADABLE, _folder_name(paths[0] if paths else ""), missing=missing)
    entries, _missing, sizes = _columns(_ordered(found, budget_s, cap))
    return PlaylistRead(LOCAL, _folder_name(entries[0]), entries, missing, sizes)


def _ordered(
    files: Sequence[tuple[str, Optional[int]]], budget_s: float, cap: int
) -> list[tuple[str, Optional[int]]]:
    """`files` in a list's one order (_in_order), its songs' tags read for `budget_s` from now:
    by name, and said in the log, should the ordering itself fail."""
    try:
        return _in_order(files, time.monotonic() + budget_s, cap)
    except Exception as error:  # never raises: by name, then
        logger.warning("A list's files could not be put in order (%s)", type(error).__name__)
        return sorted(files, key=_by_name)


def _listing(spelling: str, path: str) -> list[tuple[str, Optional[int]]]:
    """The media files directly in the folder (read at `spelling`, named in `path`) as
    (path, size or None when it can't be read), all of them: never a subfolder (nor a folder
    named like a film) nor a hidden file (".…", macOS's "._" companions). Raises OSError when
    the folder can't be listed."""
    found: list[tuple[str, Optional[int]]] = []
    with os.scandir(spelling) as entries:
        for entry in entries:
            name = entry.name
            if name.startswith(".") or not (is_video(name) or is_music(name)):
                continue
            try:
                if not entry.is_file():  # a link to a file counts
                    continue
            except OSError:
                continue  # neither a file nor a folder it can tell
            try:
                size: Optional[int] = entry.stat().st_size
            except OSError:
                size = None  # it plays all the same, from its start (no resume key)
            found.append((os.path.join(path, name), size))
    return found


def _folder_name(path: str) -> str:
    folder = os.path.dirname(path)
    return os.path.basename(folder) or folder


_File = tuple[str, Optional[int]]  # a list's file: its path, and its size (None: unknown)


def _columns(files: Sequence[_File]) -> tuple[tuple[str, ...], int, tuple[Optional[int], ...]]:
    """(entries, missing, sizes) of `files`: what a PlaylistRead holds after its name."""
    return tuple(path for path, _size in files), 0, tuple(size for _path, size in files)


def _by_name(file: tuple[str, Any]) -> tuple:
    return natural_key(file[0]), file[0]  # names natural order can't tell apart: as written


def _in_order(files: Sequence[_File], deadline: float, cap: int) -> list[_File]:
    """`files` ((path, size)) in a list's one order (the module docstring)."""
    by_name = sorted(files, key=_by_name)
    videos = [file for file in by_name if is_video(file[0])]
    if not videos:
        return _by_tags(by_name, deadline, cap)
    episodes = _by_episode(videos)
    if episodes is None:
        return by_name
    following = iter(episodes)  # in the places natural order gives the videos
    return [next(following) if is_video(file[0]) else file for file in by_name]


def _by_episode(videos: Sequence[_File]) -> Optional[list[_File]]:
    """`videos` by season and episode, when every one is an episode of one show (Plan 3's
    parse; a bare name is its folder's show's); else None. A double episode (S01E03E04) goes
    by its first; the same episode twice (two releases) both play, by name."""
    keyed: list[tuple[str, Any, str, Optional[int]]] = []
    for path, size in videos:
        episode = parse_episode(os.path.basename(path))
        if episode is None or not (episode.show or episode.sxxeyy):
            return None  # an extra, or a bare 1x02 (no show to tell it by)
        keyed.append((episode.show or folder_show(os.path.dirname(path)), episode, path, size))
    shows = {show for show, _episode, _path, _size in keyed}
    if not all(same_show(a, b) for a in shows for b in shows):
        return None  # two shows
    keyed.sort(key=lambda k: (k[1].season, k[1].first, k[1].last, *_by_name((k[2], k[3]))))
    return [(path, size) for _show, _episode, path, size in keyed]


def one_show(videos: Sequence[str]) -> bool:
    """Whether the `videos` (paths) are every one an episode of one show, as a folder's list
    tells a series (it plays by season and episode). Pure."""
    return bool(videos) and _by_episode([(path, None) for path in videos]) is not None


def start_index(
    entries: Sequence[str],
    sizes: Sequence[Optional[int]],
    *,
    part_way: Callable[[str, int], bool],
    finished: Callable[[str, int], bool],
) -> Optional[int]:
    """Where a folder's list (or several files dropped together) starts, and what the Folders
    shelf says is next (Task 12, ruling 3): the first entry the viewer stopped part-way into
    (`part_way(path, size)`: it resumes there), else the one after the last entry seen to its
    end (`finished(path, size)`), else None (as any list starts: its first entry, or a song of
    its own picking when shuffled). An entry whose size is unknown has no resume key: it
    never counts. Pure (the callables read the watch state)."""
    known: list[tuple[int, str, int]] = [
        (index, path, size)
        for index, (path, size) in enumerate(zip(entries, sizes))
        if size is not None
    ]
    for index, path, size in known:
        if part_way(path, size):
            return index
    seen = [index for index, path, size in known if finished(path, size)]
    if seen and seen[-1] + 1 < len(entries):
        return seen[-1] + 1
    return None


def _by_tags(songs: list[_File], deadline: float, cap: int) -> list[_File]:
    """`songs` (in natural order) by disc and track (no disc: the first), when libVLC read each
    one (album_listing.read_tracks, within what is left until `deadline`) with a track number
    and they name one album at most; else as they are. More than `cap`: as they are, unread."""
    left = deadline - time.monotonic()
    if len(songs) < 2 or len(songs) > cap or left <= 0:
        return songs
    tracks = read_tracks(songs, budget_s=left, cap=cap)
    if len(tracks) != len(songs):
        return songs  # its time ran out first (or no libVLC)
    numbers: list[tuple[int, int]] = []
    for (path, _size), track in zip(songs, tracks):
        number = track_number(track.get("track"))
        if track.get("path") != path or track.get("parsed") is False or number is None:
            return songs
        numbers.append((track_number(track.get("disc")) or 1, number))
    if len({album_key(track.get("album")) for track in tracks} - {""}) > 1:
        return songs  # several albums: track 1s side by side would make no sense
    order = sorted(range(len(songs)), key=lambda i: (numbers[i], *_by_name(songs[i])))
    return [songs[i] for i in order]


def subtitles_for(
    entries: Sequence[str], subtitles: Sequence[str], start: int = 0
) -> dict[str, list[str]]:
    """The subtitle files dropped with a list's `entries`, by the entry each is for: the one
    whose name it bears ("Ep 1.srt", "Ep 1.de.srt" for "Ep 1.mkv"; the longest such name),
    else the only one of its episode (S01E02 in both names), else the entry the list starts
    at (`start`), as one video dropped with subtitles takes them all. Pure."""
    if not entries:
        return {}
    stems = [os.path.splitext(os.path.basename(entry))[0].casefold() for entry in entries]
    episodes = [parse_episode(os.path.basename(entry)) for entry in entries]
    found: dict[str, list[str]] = {}
    for subtitle in subtitles:
        name = os.path.basename(subtitle)
        stem = os.path.splitext(name)[0].casefold()
        named = [i for i, own in enumerate(stems) if stem == own or stem.startswith(own + ".")]
        if named:
            index = max(named, key=lambda i: len(stems[i]))
        else:
            episode = parse_episode(name)
            same = [
                i
                for i, own in enumerate(episodes)
                if episode is not None
                and own is not None
                and (own.season, own.first) == (episode.season, episode.first)
            ]
            index = same[0] if len(same) == 1 else min(max(start, 0), len(entries) - 1)
        found.setdefault(entries[index], []).append(subtitle)
    return found
