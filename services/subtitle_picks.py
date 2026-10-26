"""The subtitles the viewer downloaded for each film and episode (2.2), for next time.

When a title plays again, the Subtitles panel offers the file picked for it last ("Name.srt
(OpenSubtitles)") to load again from the subtitles folder, without a new download. Each
title is keyed as `pick_key()` makes it: its provider with the film's id, or with the series,
season and episode numbers, hashed (as `live_tuning.json` keys channels), so the file never
names a title. The picks live in a small file of their own, `subtitle_picks.json` beside the
config; beyond `MAX_ENTRIES` the oldest go first.

`SubtitlePicks` reads the file once and keeps the picks in memory; each change writes it on
one background thread, the newest state only, through a temporary file swapped in. A missing
file counts as empty; a corrupt one, or one of another version, too, and it is kept as
`subtitle_picks.json.bak` rather than written over. A bad entry is left out. Version 1: a
later build adds only optional fields, so this one keeps reading it.
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import logging
import os
import threading
import time
from typing import Any, Callable, Mapping, Optional

import orjson as json

from services.atomic_write import atomic_write
from services.next_episode import season_and_episode
from services.redact import safe_message

_LOG = logging.getLogger(__name__)

FILE_NAME = "subtitle_picks.json"
MAX_ENTRIES = 2000
_VERSION = 1


@dataclass(frozen=True)
class Pick:
    file_id: int  # OpenSubtitles' file
    name: str  # the file's name, as the Subtitles panel shows it
    path: str  # where it is, under the subtitles folder
    language: str = ""


def _identity(item: Mapping[str, Any]) -> str:
    for field in ("stream_id", "id", "series_id", "cmd", "name"):
        value = item.get(field)
        if value not in (None, ""):
            return f"{field}:{value}"
    return ""


def pick_key(
    provider: str, item: Mapping[str, Any], *, episode: bool, context: Optional[dict] = None
) -> str:
    """A film's or an episode's key under `provider` ("" when the item names nothing): an
    episode's is its series' with its season and episode numbers; without both numbers, its
    own (never one every episode of the series would share)."""
    if not episode:
        identity = _identity(item)
        return _hashed(provider, f"film:{identity}") if identity else ""
    context = context or {}
    series = context.get("series") if isinstance(context.get("series"), dict) else None
    season = context.get("season") if isinstance(context.get("season"), dict) else None
    number, position = season_and_episode(season, dict(item))
    owner = _identity(series) if series else ""
    if owner and number not in (None, "") and position not in (None, ""):
        return _hashed(provider, f"episode:{owner}:{number}:{position}")
    own = _identity(item)
    if not own:
        return ""
    return _hashed(provider, f"episode-item:{own}:{number or ''}:{position or ''}")


def _hashed(provider: str, identity: str) -> str:
    return hashlib.sha256(f"{provider}\0{identity}".encode("utf-8")).hexdigest()[:32]


def _entry(value: Any) -> Optional[dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    file_id, name, path, when = (value.get(k) for k in ("file_id", "name", "path", "set"))
    if not isinstance(file_id, int) or isinstance(file_id, bool):
        return None
    if not isinstance(name, str) or not name or not isinstance(path, str) or not path:
        return None
    if os.path.isabs(path) or ".." in path.replace("\\", "/").split("/"):
        return None  # always under the subtitles folder
    if not isinstance(when, (int, float)) or isinstance(when, bool):
        return None
    language = value.get("language")
    return {
        "file_id": file_id,
        "name": name,
        "path": path,
        "language": language if isinstance(language, str) else "",
        "set": when,
    }


class SubtitlePicks:
    def __init__(
        self,
        path: Optional[str],
        folder: str,
        *,
        max_entries: int = MAX_ENTRIES,
        now: Callable[[], float] = time.time,
    ) -> None:
        """`path` None keeps the picks for this run only; `folder` holds the downloads."""
        self._path = path
        self.folder = folder
        self._max = max_entries
        self._now = now
        self._lock = threading.Lock()
        self._generation = 0
        self._writer: Optional[ThreadPoolExecutor] = None
        self._written: Optional[Future] = None
        self._entries: dict[str, dict[str, Any]] = self._load()
        self._trim()

    def get(self, key: str) -> Optional[Pick]:
        """The title's pick, its path made whole; None when it has none."""
        entry = self._entries.get(key) if key else None
        if entry is None:
            return None
        path = os.path.join(self.folder, entry["path"])
        return Pick(entry["file_id"], entry["name"], path, entry["language"])

    def set(self, key: str, pick: Pick) -> None:
        """Keep `pick` for the title (its path under the folder); write it soon."""
        if not key:
            return
        relative = os.path.relpath(pick.path, self.folder)
        if relative.startswith(".."):
            return  # not one of ours
        entry = {
            "file_id": int(pick.file_id),
            "name": pick.name,
            "path": relative.replace(os.sep, "/"),
            "language": pick.language,
            "set": self._now(),
        }
        self._entries[key] = entry
        self._trim()
        self._save()

    def forget(self, key: str) -> None:
        if key and self._entries.pop(key, None) is not None:
            self._save()

    def flush(self, timeout: float = 5.0) -> None:
        """Wait for the writes asked for so far (tests; at exit the pool waits by itself)."""
        written = self._written
        if written is not None:
            written.result(timeout=timeout)

    # ---- the file ------------------------------------------------------------------------
    def _load(self) -> dict[str, dict[str, Any]]:
        if self._path is None:
            return {}
        try:
            with open(self._path, "rb") as f:
                data = json.loads(f.read())
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as e:
            _LOG.info("Setting the saved subtitle picks aside: %s", safe_message(e))
            self._set_aside()
            return {}
        titles = data.get("titles") if isinstance(data, dict) else None
        if not isinstance(titles, dict) or data.get("version") != _VERSION:
            _LOG.info("Setting the saved subtitle picks aside: not this version's")
            self._set_aside()
            return {}
        entries = {}
        for key, value in titles.items():
            entry = _entry(value)
            if isinstance(key, str) and key and entry is not None:
                entries[key] = entry
        return entries

    def _set_aside(self) -> None:
        """A file this build cannot read (another version's, or damaged) is kept as .bak,
        never written over."""
        path = self._path
        assert path is not None
        try:
            os.replace(path, path + ".bak")
        except OSError as e:
            _LOG.info("Couldn't set the saved subtitle picks aside: %s", safe_message(e))
            self._path = None  # this run keeps its picks in memory: the file stays as it is

    def _trim(self) -> None:
        excess = len(self._entries) - self._max
        if excess > 0:
            for key in sorted(self._entries, key=lambda k: self._entries[k]["set"])[:excess]:
                del self._entries[key]

    def _save(self) -> None:
        if self._path is None:
            return
        snapshot = {"version": _VERSION, "titles": dict(self._entries)}
        with self._lock:
            self._generation += 1
            generation = self._generation
        if self._writer is None:
            self._writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="subtitle-picks")
        self._written = self._writer.submit(self._write, generation, snapshot)

    def _write(self, generation: int, snapshot: dict[str, Any]) -> None:
        """On the writer thread: write `snapshot` unless a newer one is queued."""
        with self._lock:
            if generation != self._generation:
                return
        path = self._path
        assert path is not None
        try:
            with atomic_write(path) as f:
                f.write(json.dumps(snapshot))
        except Exception as e:  # anything else would stay in the Future, unseen
            _LOG.warning("Couldn't save the subtitle picks: %s", safe_message(e))
