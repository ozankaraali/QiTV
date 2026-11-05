"""The recordings index (2.9.7): what each recording's file holds, kept by its path.

A recording's file name says only its channel and its start ("<channel> <YYYY-MM-DD HH.MM>.ts",
services.recording). When a recording starts, QiTV notes here the programme's title (the
schedule's, or the guide's programme on now for one started with R), the channel's name, its
provider, its id and its logo's address, and the programme's start and end (None: not known).
The Recordings tab names a recording's tile and finds its logo from it; a file without an
entry shows its channel. A file the helper saved under another name (" (2)") keeps its entry
(`moved`), a deleted one loses it (`forget`).

The file sits in QiTV's settings folder (never beside the recordings), is written off the GUI
thread (services.recording_schedule.write_later) and keeps the newest `KEPT` entries. An older
QiTV ignores it; one of another version is read as empty.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable, Optional
import unicodedata

from services import recording_schedule as rs

logger = logging.getLogger(__name__)

FILE_NAME = "recordings_index.json"
VERSION = 1
KEPT = 2000  # entries kept, the newest by start
_FIELDS = ("title", "channel", "provider", "channel_id", "logo")


class RecordingsIndex:
    def __init__(self, path: str, write: Callable[[str, dict], None] = rs.write_later) -> None:
        self._path = path
        self._write = write
        self._entries: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as source:
                data = json.load(source)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            logger.warning("The recordings index could not be read: %s", type(e).__name__)
            return
        if not isinstance(data, dict) or data.get("version") != VERSION:
            return
        entries = data.get("entries")
        if isinstance(entries, dict):
            self._entries = {
                _key(str(path)): entry for path, entry in entries.items() if isinstance(entry, dict)
            }

    def _save(self) -> None:
        if len(self._entries) > KEPT:
            newest = sorted(
                self._entries.items(), key=lambda pair: _number(pair[1].get("start")), reverse=True
            )
            self._entries = dict(newest[:KEPT])
        data = {"version": VERSION, "entries": self._entries}
        self._write(self._path, json.loads(json.dumps(data)))  # a copy for the writing thread

    def note(
        self,
        path: str,
        *,
        title: str,
        channel: str,
        provider: str,
        channel_id: str,
        logo: str,
        start: float,
        stop: Optional[float],
    ) -> None:
        """A recording to `path` started: what it holds."""
        if not path:
            return
        path = _key(path)
        entry: dict[str, Any] = {
            key: str(value or "")
            for key, value in zip(_FIELDS, (title, channel, provider, channel_id, logo))
        }
        entry["start"] = float(start)
        entry["stop"] = float(stop) if stop is not None else None
        self._entries[path] = entry
        self._save()

    def moved(self, old: str, new: str) -> None:
        """The recording meant for `old` was saved as `new` (the helper's " (2)")."""
        entry = self._entries.pop(_key(old), None)
        if entry is not None and new:
            self._entries[_key(new)] = entry
            self._save()

    def forget(self, path: str) -> None:
        if self._entries.pop(_key(path), None) is not None:
            self._save()

    def get(self, path: str) -> Optional[dict[str, Any]]:
        entry = self._entries.get(_key(path))
        return dict(entry) if entry is not None else None


def _number(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _key(path: str) -> str:
    """One key per file, however its path is written: the system's separators and case
    (Windows), no "." parts, and composed letters (a volume may list "é" decomposed)."""
    return os.path.normcase(os.path.normpath(unicodedata.normalize("NFC", path)))
