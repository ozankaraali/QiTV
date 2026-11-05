"""Recordings in the Library (2.8): the recordings folder's files as Library titles.

Each `.ts` file of the Recordings folder (`services.recording.folder`) is a title: its channel
and start from its name ("<channel> <YYYY-MM-DD HH.MM>[ (n)].ts", as QiTV names them), else
the file's own name and time. They are listed newest first. The folder is read on a thread of
its own (`RecordingsList.refresh`); the list changes, and says so, only when the files did.

2.9.7: the Recordings page adds places, other folders listed too (`services.recording.places`);
each read notes their disks' free space and whether they could be reached (`places`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
import os
from pathlib import Path
import re
import shutil
import threading
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, Signal

from services import recording
from services.i18n import N_, app_locale, translate
from services.recording_schedule import slot_text

logger = logging.getLogger(__name__)

RECORDING_PREFIX = "recording:"  # a recording's id (and resume key): the prefix and its file name
_NAMED = re.compile(r"^(?P<channel>.+) (?P<when>\d{4}-\d{2}-\d{2} \d{2}\.\d{2})(?: \(\d+\))?$")
# i18n: a recording's line in the Library's details: when it started and the file's size
DESCRIPTION = N_("Recorded {when} · {size}")
# i18n: the Library's genre line for a recording (what QiTV recorded from a channel)
GENRE = N_("Recording")
# i18n: a scheduled recording's genre line in the Library (it is still to come)
SCHEDULED_GENRE = N_("Scheduled")
# i18n: a scheduled recording's details in the Library ({when}: its time, {channel}: its channel)
SCHEDULED_ONCE = N_("Records {when} on {channel}")
# i18n: a series recording's details in the Library: every airing of it on the channel
SCHEDULED_SERIES = N_("Records every airing on {channel}")
# i18n: a timer recording's details in the Library: the channel at a time ({time}: 21:00–22:30)
TIMER_DAILY = N_("Records {channel} {time}, every day")
# i18n: a timer recording's details in the Library, on working days
TIMER_WEEKDAYS = N_("Records {channel} {time}, Monday to Friday")
# i18n: a timer recording's details in the Library, at weekends
TIMER_WEEKENDS = N_("Records {channel} {time}, Saturday and Sunday")
# i18n: a timer recording's details in the Library ({days}: short day names, as "Mon, Wed")
TIMER_DAYS = N_("Records {channel} {time}, {days}")
SCHEDULED_PREFIX = "scheduled:"
# i18n: a toast: a recording was not deleted, as another program (a player) has its file open
DELETE_IN_USE = N_("“{name}” is open in another program: close it there, then delete it again.")
# i18n: a toast: a recording's file could not be deleted ({error}: the system's reason)
DELETE_FAILED = N_("“{name}” could not be deleted: {error}")
# Windows refuses to delete a file another program has open: a sharing or a lock violation.
_IN_USE_WINERRORS = (32, 33)


def day_names(days: list[int]) -> str:
    """Weekdays (Monday 0) by their short names in the app's language, as "Mon, Wed"."""
    from PySide6.QtCore import QLocale

    locale = app_locale()
    return ", ".join(locale.dayName(day + 1, QLocale.FormatType.ShortFormat) for day in days)


def timer_text(entry: dict) -> str:
    """What a timer records, as the Library and its toast say it."""
    channel = str((entry.get("channel") or {}).get("name") or "")
    when = slot_text(int(entry.get("begin") or 0), int(entry.get("minutes") or 0))
    days = sorted(entry.get("days") or [])
    if days == list(range(7)):
        return translate(TIMER_DAILY, channel=channel, time=when)
    if days == list(range(5)):
        return translate(TIMER_WEEKDAYS, channel=channel, time=when)
    if days == [5, 6]:
        return translate(TIMER_WEEKENDS, channel=channel, time=when)
    return translate(TIMER_DAYS, channel=channel, time=when, days=day_names(days))


def short_when(start: float) -> str:
    """A moment as the Library's strip shows a scheduled recording's ("Sun 1 Nov 21:30", in
    the app's language); "" without one."""
    if not start:
        return ""
    from PySide6.QtCore import QDateTime

    moment = QDateTime.fromSecsSinceEpoch(int(start))
    return str(app_locale().toString(moment, "ddd d MMM HH:mm"))


def scheduled_item(entry: dict) -> dict[str, Any]:
    """A scheduled recording (services.recording_schedule's item) as a Library title: shown
    first, not playable (its strip's Cancel or its right-click cancels it). `_when` is the
    time the strip shows where Play is (2.8.12): a programme's start, a timer's hours; none
    for a series (every airing)."""
    channel = str((entry.get("channel") or {}).get("name") or "")
    start = float(entry.get("start") or entry.get("created") or 0)
    name = str(entry.get("title") or "")
    shown = ""
    if entry.get("kind") == "series":
        description = translate(SCHEDULED_SERIES, channel=channel)
    elif entry.get("kind") == "timer":
        description = timer_text(entry)
        shown = slot_text(int(entry.get("begin") or 0), int(entry.get("minutes") or 0))
        name = name or shown
    else:
        when = datetime.fromtimestamp(start).strftime("%Y-%m-%d %H:%M") if start else ""
        description = translate(SCHEDULED_ONCE, when=when, channel=channel)
        shown = short_when(float(entry.get("start") or 0))
    return {
        "id": SCHEDULED_PREFIX + str(entry.get("id") or ""),
        "name": name,
        "added": str(int(start)),
        "genre": translate(SCHEDULED_GENRE),
        "description": description,
        "_scheduled": True,
        "_item_id": str(entry.get("id") or ""),
        "_channel": channel,
        "_when": shown,
    }


def parse_name(file_name: str) -> tuple[str, Optional[datetime]]:
    """The channel and the start a recording's file name gives; another name is its own title
    with no start."""
    stem = (
        file_name[: -len(recording.SUFFIX)] if file_name.endswith(recording.SUFFIX) else file_name
    )
    match = _NAMED.match(stem)
    if match is None:
        return stem, None
    try:
        when = datetime.strptime(match.group("when"), "%Y-%m-%d %H.%M")
    except ValueError:
        return stem, None
    return match.group("channel"), when


def size_text(size: int) -> str:
    """A file's size as people read it: "1.5 GB", "300 MB"."""
    if size >= 1_000_000_000:
        return f"{size / 1_000_000_000:.1f} GB"
    return f"{max(1, round(size / 1_000_000))} MB"


def recording_item(path: Path, size: int, mtime: float, writing: bool = False) -> dict[str, Any]:
    """The Library title of the recording at `path`."""
    channel, when = parse_name(path.name)
    started = when.timestamp() if when is not None else mtime
    shown = (when or datetime.fromtimestamp(mtime)).strftime("%Y-%m-%d %H:%M")
    return {
        "id": RECORDING_PREFIX + path.name,
        "name": channel,
        "cmd": path.absolute().as_uri(),
        "path": str(path),
        "added": str(int(started)),
        "genre": translate(GENRE),
        "description": translate(DESCRIPTION, when=shown, size=size_text(size)),
        "_recording": True,
        "_writing": writing,
        "_channel": channel,  # the name may become the programme's (titles)
        "_size": size,  # 2.9.7: the Recordings page adds up each place's
    }


def _entries(folder: str) -> list[tuple[Path, int, float, int]]:
    """(path, size, mtime, mtime_ns) of the folder's recordings (no hidden file, no folder)."""
    found = []
    try:
        with os.scandir(folder) as listing:
            for entry in listing:
                name = entry.name
                if name.startswith(".") or not name.endswith(recording.SUFFIX):
                    continue
                try:
                    if not entry.is_file():
                        continue
                    stat = entry.stat()
                except OSError:
                    continue
                found.append((Path(entry.path), stat.st_size, stat.st_mtime, stat.st_mtime_ns))
    except OSError:
        return []
    return found


def scan(folder: str, writing: frozenset[str] = frozenset()) -> list[dict[str, Any]]:
    """The recordings in `folder`, newest first (none when it cannot be read)."""
    return _listed(_entries(folder), writing)


def _listed(
    entries: list[tuple[Path, int, float, int]], writing: frozenset[str]
) -> list[dict[str, Any]]:
    items = [
        recording_item(path, size, mtime, str(path) in writing)
        for path, size, mtime, _ns in entries
    ]
    items.sort(key=lambda item: int(item["added"]), reverse=True)
    return items


@dataclass(frozen=True)
class PlaceState:
    """2.9.7: a folder recordings are listed from, as last read: whether it could be, and its
    disk's free bytes (None: not known)."""

    path: str
    reachable: bool
    free: Optional[int]


def _place_state(folder: str, default: bool) -> PlaceState:
    """`folder`'s state. Where recordings go may not exist yet (the first recording makes
    it): its disk counts then, from the nearest folder above it that exists."""
    if os.path.isdir(folder):
        probe: Optional[str] = folder
    elif default:
        probe = os.path.dirname(os.path.abspath(folder))
        while probe and not os.path.isdir(probe) and os.path.dirname(probe) != probe:
            probe = os.path.dirname(probe)
    else:
        return PlaceState(folder, False, None)
    try:
        free = shutil.disk_usage(probe).free if probe else None
    except OSError:
        free = None
    return PlaceState(folder, True, free)


class RecordingsList(QObject):
    """The Recordings folder's titles, read again on `refresh()` (a thread of its own).
    `writing()` names the files recordings are writing now (marked `_writing`)."""

    changed = Signal()
    places_changed = Signal()  # 2.9.7: a place's free space or reachability (files unchanged)
    delete_failed = Signal(str)  # why a recording's file stayed, in the active language
    # (signature, (items, places)), from the reading thread
    _scanned = Signal(object, object)
    # (its path, why it stayed: None when it went), from the deleting thread
    _deleted = Signal(str, object)

    def __init__(
        self,
        config: Any,
        writing: Optional[Callable[[], set[str]]] = None,
        parent: Optional[QObject] = None,
        titles: Optional[Callable[[str], str]] = None,
        index: Any = None,
    ) -> None:
        """`titles(path)` (2.8): the programme a scheduled recording's file holds ("": none).
        `index` (2.9.7, services.recordings_index): what each file holds, its programme,
        channel, provider, logo and times first."""
        super().__init__(parent)
        self._config = config
        self._writing = writing or (lambda: set())
        self._titles = titles or (lambda path: "")
        self._index = index
        self._scheduled: list[dict[str, Any]] = []  # 2.8: still to come, listed first
        self._items: list[dict[str, Any]] = []
        self._places: list[PlaceState] = []  # 2.9.7: as last read
        self._signature: Optional[tuple] = None
        self._reading = False
        self._again = False
        self.version = 0
        self._scanned.connect(self._on_scanned)
        self._deleted.connect(self._on_deleted)

    def items(self) -> list[dict[str, Any]]:
        return self._scheduled + self._items

    def files(self) -> list[dict[str, Any]]:
        """2.9.7: the recordings' files alone (newest first)."""
        return list(self._items)

    def places(self) -> list[PlaceState]:
        """2.9.7: the folders listed from, as last read (where recordings go first)."""
        return list(self._places)

    def set_scheduled(self, entries: list[dict]) -> None:
        """The schedule's items (2.8), listed before the recordings, soonest first."""
        ordered = sorted(
            entries,
            key=lambda e: (
                e.get("kind") != "once",  # those that repeat after the ones to come once
                float(e.get("start") or e.get("created") or 0),
            ),
        )
        self._scheduled = [scheduled_item(entry) for entry in ordered]
        self.version += 1
        self.changed.emit()

    def is_writing(self, path: str) -> bool:
        """Whether a recording writes the file at `path` now."""
        return path in self._writing()

    def delete(self, path: str) -> None:
        """Delete the recording at `path` (on a thread of its own), then read the folder again.
        Only a file of the Recordings folder that this list shows goes, and never one a
        recording still writes."""
        if not any(item.get("path") == path for item in self._items):
            return
        if self.is_writing(path):
            return
        threading.Thread(
            target=self._remove, args=(path,), name="recording-delete", daemon=True
        ).start()

    def _remove(self, path: str) -> None:
        failure: Optional[OSError] = None
        try:
            os.remove(path)
        except OSError as e:
            logger.warning("A recording could not be deleted: %s", type(e).__name__)
            failure = e
        self._deleted.emit(path, failure)

    def _on_deleted(self, path: str, failure: Optional[OSError]) -> None:
        """A file went, or could not (the user is told why); either way the folder is read
        again."""
        name = os.path.basename(path)
        if failure is None and self._index is not None:  # 2.9.7: its entry goes with it
            self._index.forget(path)
        if failure is not None:
            if getattr(failure, "winerror", None) in _IN_USE_WINERRORS:
                self.delete_failed.emit(translate(DELETE_IN_USE, name=name))
            else:
                reason = failure.strerror or type(failure).__name__
                self.delete_failed.emit(translate(DELETE_FAILED, name=name, error=reason))
        self.refresh()

    def _noted(self, item: dict[str, Any]) -> dict[str, Any]:
        """2.9.7: what the index says of `item`'s file, set on the item (`_channel`,
        `_provider`, `_logo`, `_start`, `_stop`; the name's channel and start without one)."""
        entry = self._index.get(item.get("path", "")) if self._index is not None else None
        entry = entry or {}
        if entry.get("channel"):
            item["_channel"] = str(entry["channel"])
        item["_provider"] = str(entry.get("provider") or "")
        item["_logo"] = str(entry.get("logo") or "")
        start = entry.get("start")
        item["_start"] = float(start) if isinstance(start, (int, float)) else float(item["added"])
        stop = entry.get("stop")
        item["_stop"] = float(stop) if isinstance(stop, (int, float)) else None
        return entry

    def refresh(self) -> None:
        """Read the folder again (once more after a reading under way, when asked meanwhile)."""
        if self._reading:
            self._again = True
            return
        self._reading = True
        folders = tuple(recording.places(self._config))  # 2.9.7: where they go, and the others
        try:
            writing = frozenset(self._writing())
        except Exception as e:  # the list never fails for it
            logger.debug("Recordings being written unknown: %s", type(e).__name__)
            writing = frozenset()
        threading.Thread(
            target=self._read, args=(folders, writing), name="recordings-list", daemon=True
        ).start()

    def _read(self, folders: tuple[str, ...], writing: frozenset[str]) -> None:
        places: Optional[list[PlaceState]] = None
        try:
            entries = [entry for folder in folders for entry in _entries(folder)]
            signature = (
                folders,
                tuple(sorted((str(p), s, ns) for p, s, _m, ns in entries)),
                writing,
            )
            items = _listed(entries, writing)
            places = [_place_state(folder, k == 0) for k, folder in enumerate(folders)]
        except Exception as e:  # pragma: no cover - the list stays as it was
            logger.warning("The recordings could not be listed: %s", type(e).__name__)
            signature, items = None, None
        self._scanned.emit(signature, (items, places))

    def _on_scanned(self, signature: Optional[tuple], read: tuple) -> None:
        self._reading = False
        items, places = read
        if places is not None and places != self._places:
            self._places = places
            if signature == self._signature:  # the files did not change: the places did
                self.places_changed.emit()
        if items is not None and signature != self._signature:
            self._signature = signature
            for item in items:  # 2.8: a scheduled recording is named by its programme
                entry = self._noted(item)
                title = str(entry.get("title") or "") or self._titles(item.get("path", ""))
                item["_title"] = title
                if title and title != item["name"]:
                    item["description"] = f"{item['name']} · {item['description']}"
                    item["name"] = title
            self._items = items
            self.version += 1
            self.changed.emit()
        if self._again:
            self._again = False
            self.refresh()
