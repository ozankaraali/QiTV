"""The Folders shelf (Plan 4, Task 12): the folders the viewer opened as folders, for the
Library's Folders tab, newest first.

A light shelf, not a media library to manage: it only remembers the folders opened. No drive is
scanned, nothing is fetched, nothing is set up. A folder goes on it once it plays as a folder (a
drop, File → Open Folder…, the Add folder tile, a folder handed to QiTV by the system); a single
file, several files dropped together or a playlist never add theirs. Opened again, it moves to
the front; past `MAX_ENTRIES` the one opened longest ago goes. Each entry is {path, name, kind,
opened_at, last_entry}: the folder's path as it was opened (its trailing separator dropped), its
name, what it plays as (`SERIES`, `ALBUM` or `VIDEOS`, `folder_kind`: the tile's line until its
folder is read again), when it was opened last (seconds since the epoch) and the name of its
entry that opened last. Paths stay on this computer: the file is never sent anywhere (the phone
remote's page has no shelf).

The file is `folder_shelf.json` beside the config, kept as `services.channel_store` keeps its
files (TrackMemory's way): read once, written on a background thread. Version 1; a later build
adds only optional fields, so this one keeps reading it. A broken file counts as empty; one of
a newer version is left as it is (the shelf of that run is kept in memory only), and builds
before the shelf never read it.

Pure logic: no Qt. Its listeners (`subscribe`) are called on the thread that changed it, the
GUI thread's in QiTV.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
import logging
import os
from typing import Any, Optional

from services.channel_store import ChannelStore
from services.local_playlist import one_show
from services.open_paths import is_song, is_video

_LOG = logging.getLogger(__name__)

FILE_NAME = "folder_shelf.json"
MAX_ENTRIES = 30
SERIES, ALBUM, VIDEOS = "series", "album", "videos"
KINDS = (SERIES, ALBUM, VIDEOS)
_VERSION = 1
_TEXT_MAX = 1024  # a name or an entry's, kept at most this long; a longer path isn't kept


def _trimmed(path: str) -> str:
    """`path` without the separator macOS's Finder ends a dropped folder with; a root keeps
    its own ("/", and a drive's "E:\\": "E:" alone is that drive's current folder)."""
    trimmed = path.rstrip("/\\")
    if not trimmed:
        return path[:1]  # the root
    if len(trimmed) == 2 and trimmed[1] == ":" and trimmed[0].isalpha() and len(path) > 2:
        return path[:3]  # a drive's root, with its separator
    return trimmed


def shelf_key(path: str) -> str:
    """The shelf's key of the folder at `path`, however it was spelled ("…/", "x/../…"; on
    Windows in any case), without a look at the disk."""
    return os.path.normcase(os.path.normpath(_trimmed(path)))


def folder_kind(entries: Sequence[str]) -> str:
    """What a folder whose list holds `entries` plays as: an album when every one is a song
    (as TVController.play_list tells one), a series when its videos are episodes of one show
    (they play by season and episode), else videos."""
    if entries and all(is_song(entry) for entry in entries):
        return ALBUM
    videos = [entry for entry in entries if is_video(entry)]
    if videos and one_show(videos):
        return SERIES
    return VIDEOS if videos else ALBUM


def _text(value: Any) -> Optional[str]:
    return value[:_TEXT_MAX] if isinstance(value, str) else None


def _entry(value: Any) -> Optional[dict[str, Any]]:
    """A stored entry as `add()` writes it; None when it is not one."""
    if not isinstance(value, dict):
        return None
    path, name, last = value.get("path"), _text(value.get("name")), value.get("last_entry")
    when, kind = value.get("opened_at"), value.get("kind")
    if not isinstance(path, str) or not path or len(path) > _TEXT_MAX:
        return None  # (a path cut short would name another folder)
    if name is None or kind not in KINDS:
        return None
    if not isinstance(when, (int, float)) or isinstance(when, bool):
        return None
    last_entry = _text(last) if last is not None else ""
    if last_entry is None:
        return None
    return {"path": path, "name": name, "kind": kind, "opened_at": when, "last_entry": last_entry}


class FolderShelf(ChannelStore):
    """`FolderShelf(path)`; `path` None keeps the shelf for this run only. Its entries are kept
    in the order they were opened (the newest last), so the front is the last opened."""

    WHAT = "folder shelf"
    VERSION = _VERSION
    FIELD = "folders"
    MAX_ENTRIES = MAX_ENTRIES
    LOG = _LOG
    _entry = staticmethod(_entry)

    def __init__(self, path: Optional[str], **how: Any) -> None:
        self._listeners: list[Callable[[], None]] = []
        super().__init__(path, **how)

    def _load(self) -> dict[str, dict[str, Any]]:
        """As the base reads it, keyed anew by path, in the order the folders were opened."""
        entries = super()._load()
        ordered = sorted(entries.values(), key=lambda entry: entry["opened_at"])
        return {shelf_key(entry["path"]): entry for entry in ordered}

    def subscribe(self, listener: Callable[[], None]) -> None:
        """`listener()` is called after each change (an add, a remove, a clear)."""
        self._listeners.append(listener)

    def folders(self) -> list[dict[str, Any]]:
        """The shelf's folders, the one opened last first (copies of their entries)."""
        return [dict(entry) for entry in reversed(self._entries.values())]

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, path: object) -> bool:
        return isinstance(path, str) and shelf_key(path) in self._entries

    def add(self, path: str, name: str, kind: str, last_entry: str = "") -> None:
        """The folder at `path` was opened as a folder and plays: at the front (once)."""
        if not path or kind not in KINDS:
            return
        if len(_trimmed(path)) > _TEXT_MAX:
            _LOG.info("A folder whose path is too long to keep is left off the shelf")
            return  # never cut: that would name another folder
        key = shelf_key(path)
        self._entries.pop(key, None)
        self._entries[key] = {
            "path": _trimmed(path),
            "name": str(name or "")[:_TEXT_MAX],
            "kind": kind,
            "opened_at": self._now(),
            "last_entry": str(last_entry or "")[:_TEXT_MAX],
        }
        self._trim()
        self._changed()

    def played(self, path: str, entry_name: str) -> None:
        """The entry named `entry_name` of the folder at `path` opened (its list goes on): kept
        as its last entry; the folder keeps its place."""
        key = shelf_key(path) if path else ""
        entry = self._entries.get(key)
        name = str(entry_name or "")[:_TEXT_MAX]
        if entry is None or entry.get("last_entry") == name:
            return
        self._entries[key] = {**entry, "last_entry": name}  # a write queued keeps its copy
        self._save()

    def remove(self, path: str) -> bool:
        """The folder at `path` off the shelf (its files stay where they are); False when it
        was not on it."""
        if self._entries.pop(shelf_key(path), None) is None:
            return False
        self._changed()
        return True

    def clear(self) -> None:
        """Every folder off the shelf."""
        if not self._entries:
            return
        self._entries.clear()
        self._changed()

    def _trim(self) -> None:
        """Beyond the cap, the folders opened longest ago go first."""
        while len(self._entries) > self._max:
            del self._entries[next(iter(self._entries))]

    def _changed(self) -> None:
        self._save()
        for listener in list(self._listeners):
            listener()

    def _save(self) -> None:
        if isinstance(self._other_version, int) and self._other_version > self.VERSION:
            return  # a newer build's file: left as it is (this run's shelf stays in memory)
        super()._save()
