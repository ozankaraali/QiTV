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
from services.i18n import N_, translate

logger = logging.getLogger(__name__)

RECORDING_PREFIX = "recording:"  # a recording's id (and resume key): the prefix and its file name
_NAMED = re.compile(r"^(?P<channel>.+) (?P<when>\d{4}-\d{2}-\d{2} \d{2}\.\d{2})(?: \(\d+\))?$")
# i18n: a recording's line in the Library's details: when it started and the file's size
DESCRIPTION = N_("Recorded {when} · {size}")
# i18n: the Library's genre line for a recording (what QiTV recorded from a channel)
GENRE = N_("Recording")


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
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._writing = writing or (lambda: set())
        self._items: list[dict[str, Any]] = []
        self._signature: Optional[tuple] = None
        self._reading = False
        self._again = False
        self.version = 0
        self._scanned.connect(self._on_scanned)
        self._deleted.connect(self.refresh)

    def items(self) -> list[dict[str, Any]]:
        return list(self._items)

    def delete(self, path: str) -> None:
        """Delete the recording at `path` (on a thread of its own), then read the folder again.
        Only a file of the Recordings folder that this list shows goes."""
        if not any(item.get("path") == path for item in self._items):
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
            self._items = items
            self.version += 1
            self.changed.emit()
        if self._again:
            self._again = False
            self.refresh()
