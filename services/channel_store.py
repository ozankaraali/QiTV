"""A small versioned file of what QiTV keeps per live channel (no Qt): the base of
`services.live.tuning.LiveTuning` and `services.sound_sync.SoundSync`.

The file is {"version": VERSION, "channels": {key: entry}}. It is read once, and the entries
are kept in memory; each change writes the file on one background thread, the newest state
only, through a temporary file swapped in. A missing, unreadable or corrupt file, or one of
another version, counts as empty, and an entry the subclass's `_entry` does not read is left
out. A write that fails is logged (without its path).
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import logging
import os
import tempfile
import threading
import time
from typing import Any, Callable, Optional

import orjson as json

from services.redact import safe_message


class ChannelStore:
    """A subclass names what it keeps (`WHAT`, in its log lines and its writer thread's name),
    its file's `VERSION`, its logger (`LOG`) and `MAX_ENTRIES`, and gives `_entry` (a stored
    entry as it reads it, or None) and `_trim` (keeping at most `self._max` entries)."""

    WHAT = ""
    VERSION = 1
    MAX_ENTRIES = 0
    LOG = logging.getLogger(__name__)

    def __init__(
        self,
        path: Optional[str],
        *,
        max_entries: Optional[int] = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        """`path` None keeps the entries for this run only."""
        self._path = path
        self._max = self.MAX_ENTRIES if max_entries is None else max_entries
        self._now = now
        self._lock = threading.Lock()  # the newest state's number, read by the writer
        self._generation = 0
        self._writer: Optional[ThreadPoolExecutor] = None
        self._written: Optional[Future] = None
        self._entries: dict[str, dict[str, Any]] = self._load()
        self._trim()

    @staticmethod
    def _entry(value: Any) -> Optional[dict[str, Any]]:
        raise NotImplementedError

    def _trim(self) -> None:
        raise NotImplementedError

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
            self.LOG.info("Ignoring the saved %s: %s", self.WHAT, safe_message(e))
            return {}
        channels = data.get("channels") if isinstance(data, dict) else None
        if not isinstance(channels, dict):
            self.LOG.info("Ignoring the saved %s: it is not QiTV's", self.WHAT)
            return {}
        if data.get("version") != self.VERSION:
            self.LOG.info("Ignoring the saved %s: it is of another version", self.WHAT)
            return {}
        entries = {}
        for key, value in channels.items():
            entry = self._entry(value)
            if isinstance(key, str) and key and entry is not None:
                entries[key] = entry
        return entries

    def _save(self) -> None:
        if self._path is None:
            return
        snapshot = {"version": self.VERSION, "channels": dict(self._entries)}
        with self._lock:
            self._generation += 1
            generation = self._generation
        if self._writer is None:
            name = self.WHAT.replace(" ", "-")
            self._writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix=name)
        self._written = self._writer.submit(self._write, generation, snapshot)

    def _write(self, generation: int, snapshot: dict[str, Any]) -> None:
        """On the writer thread: write `snapshot` unless a newer one is queued."""
        with self._lock:
            if generation != self._generation:
                return
        path = self._path
        assert path is not None
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(
                dir=os.path.dirname(path) or ".",
                prefix="." + self.WHAT.replace(" ", "_") + "-",
                suffix=".tmp",
            )
            with os.fdopen(fd, "wb") as f:
                f.write(json.dumps(snapshot))
            os.replace(temporary, path)
            temporary = None
        except Exception as e:  # anything else would stay in the Future, unseen
            self.LOG.warning("Couldn't save the %s: %s", self.WHAT, safe_message(e))
        finally:
            if temporary is not None:
                try:
                    os.remove(temporary)
                except OSError:
                    pass
