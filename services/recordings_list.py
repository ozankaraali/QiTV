"""Recordings in the Library (2.8): the recordings folder's files as Library titles.

Each `.ts` file of the Recordings folder (`services.recording.folder`) is a title: its channel
and start from its name ("<channel> <YYYY-MM-DD HH.MM>[ (n)].ts", as QiTV names them), else
the file's own name and time. They are listed newest first. The folder is read on a thread of
its own (`RecordingsList.refresh`); the list changes, and says so, only when the files did.
"""

from __future__ import annotations

from datetime import datetime
import logging
import os
from pathlib import Path
import re
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


def scheduled_item(entry: dict) -> dict[str, Any]:
    """A scheduled recording (services.recording_schedule's item) as a Library title: shown
    first, not playable (its right-click cancels it)."""
    channel = str((entry.get("channel") or {}).get("name") or "")
    start = float(entry.get("start") or entry.get("created") or 0)
    name = str(entry.get("title") or "")
    if entry.get("kind") == "series":
        description = translate(SCHEDULED_SERIES, channel=channel)
    elif entry.get("kind") == "timer":
        description = timer_text(entry)
        name = name or slot_text(int(entry.get("begin") or 0), int(entry.get("minutes") or 0))
    else:
        when = datetime.fromtimestamp(start).strftime("%Y-%m-%d %H:%M") if start else ""
        description = translate(SCHEDULED_ONCE, when=when, channel=channel)
    return {
        "id": SCHEDULED_PREFIX + str(entry.get("id") or ""),
        "name": name,
        "added": str(int(start)),
        "genre": translate(SCHEDULED_GENRE),
        "description": description,
        "_scheduled": True,
        "_item_id": str(entry.get("id") or ""),
        "_channel": channel,
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
    items = [
        recording_item(path, size, mtime, str(path) in writing)
        for path, size, mtime, _ns in _entries(folder)
    ]
    items.sort(key=lambda item: int(item["added"]), reverse=True)
    return items


class RecordingsList(QObject):
    """The Recordings folder's titles, read again on `refresh()` (a thread of its own).
    `writing()` names the files recordings are writing now (marked `_writing`)."""

    changed = Signal()
    _scanned = Signal(object, object)  # (signature, items), from the reading thread
    _deleted = Signal()  # a file went (or could not), from the deleting thread

    def __init__(
        self,
        config: Any,
        writing: Optional[Callable[[], set[str]]] = None,
        parent: Optional[QObject] = None,
        titles: Optional[Callable[[str], str]] = None,
    ) -> None:
        """`titles(path)` (2.8): the programme a scheduled recording's file holds ("": none)."""
        super().__init__(parent)
        self._config = config
        self._writing = writing or (lambda: set())
        self._titles = titles or (lambda path: "")
        self._scheduled: list[dict[str, Any]] = []  # 2.8: still to come, listed first
        self._items: list[dict[str, Any]] = []
        self._signature: Optional[tuple] = None
        self._reading = False
        self._again = False
        self.version = 0
        self._scanned.connect(self._on_scanned)
        self._deleted.connect(self.refresh)

    def items(self) -> list[dict[str, Any]]:
        return self._scheduled + self._items

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

    def delete(self, path: str) -> None:
        """Delete the recording at `path` (on a thread of its own), then read the folder again.
        Only a file of the Recordings folder that this list shows goes, and never one a
        recording still writes."""
        if not any(item.get("path") == path for item in self._items):
            return
        if path in self._writing():
            return
        threading.Thread(
            target=self._remove, args=(path,), name="recording-delete", daemon=True
        ).start()

    def _remove(self, path: str) -> None:
        try:
            os.remove(path)
        except OSError as e:
            logger.warning("A recording could not be deleted: %s", type(e).__name__)
        self._deleted.emit()

    def refresh(self) -> None:
        """Read the folder again (once more after a reading under way, when asked meanwhile)."""
        if self._reading:
            self._again = True
            return
        self._reading = True
        folder = recording.folder(self._config)
        try:
            writing = frozenset(self._writing())
        except Exception as e:  # the list never fails for it
            logger.debug("Recordings being written unknown: %s", type(e).__name__)
            writing = frozenset()
        threading.Thread(
            target=self._read, args=(folder, writing), name="recordings-list", daemon=True
        ).start()

    def _read(self, folder: str, writing: frozenset[str]) -> None:
        try:
            entries = _entries(folder)
            signature = (
                folder,
                tuple(sorted((str(p), s, ns) for p, s, _m, ns in entries)),
                writing,
            )
            items = scan(folder, writing)
        except Exception as e:  # pragma: no cover - the list stays as it was
            logger.warning("The recordings could not be listed: %s", type(e).__name__)
            signature, items = None, None
        self._scanned.emit(signature, items)

    def _on_scanned(self, signature: Optional[tuple], items: Optional[list]) -> None:
        self._reading = False
        if items is not None and signature != self._signature:
            self._signature = signature
            for item in items:  # 2.8: a scheduled recording is named by its programme
                title = self._titles(item.get("path", ""))
                if title and title != item["name"]:
                    item["description"] = f"{item['name']} · {item['description']}"
                    item["name"] = title
            self._items = items
            self.version += 1
            self.changed.emit()
        if self._again:
            self._again = False
            self.refresh()
