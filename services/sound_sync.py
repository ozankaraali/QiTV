"""How far the viewer moved each live channel's sound against its picture (2.2).

On some channels the sound drifts out of step with the picture over time (other players do
the same: the stream's own clocks are clean). The viewer nudges it in steps of `STEP_MS`, up
to `LIMIT_MS` either way (J and K, or the player's Sound panel), and QiTV keeps it for the
channel's next tune: by `services.live.tuning.channel_key()`, the provider and the channel
hashed as `live_tuning.json` keys them, in a small file of its own, `sound_sync.json` beside
the config. `live_tuning.json` forgets what a channel taught once 500 others taught something
since; a sync stays until the viewer sets it back to zero. Only beyond `MAX_ENTRIES` channels
do the oldest go first. Films and episodes are not kept here (the player keeps theirs while
they play).

`SoundSync` keeps the entries in memory and in the file, read once. Each change writes it on
one background thread, the newest state only, through a temporary file swapped in, as
`LiveTuning` does. A missing, unreadable or corrupt file, or one of another version, counts
as empty, and a bad entry is left out. Version 1: a later build adds only optional fields,
so this one keeps reading the file. A write that fails is logged (without its path).
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

_LOG = logging.getLogger(__name__)

FILE_NAME = "sound_sync.json"
STEP_MS = 50  # one nudge (J, K, the panel's − and +)
LIMIT_MS = 5000  # the most either way
MAX_ENTRIES = 5000
_VERSION = 1


def clamp(ms: int) -> int:
    """`ms` within ±LIMIT_MS."""
    return max(-LIMIT_MS, min(LIMIT_MS, int(ms)))


def _entry(value: Any) -> Optional[dict[str, Any]]:
    """A stored entry as `set_audio_ms()` writes it; None when it is not one."""
    if not isinstance(value, dict):
        return None
    ms, when = value.get("audio_ms"), value.get("set")
    if not isinstance(ms, int) or isinstance(ms, bool) or not ms or abs(ms) > LIMIT_MS:
        return None
    if not isinstance(when, (int, float)) or isinstance(when, bool):
        return None
    return {"audio_ms": ms, "set": when}


class SoundSync:
    def __init__(
        self,
        path: Optional[str],
        *,
        max_entries: int = MAX_ENTRIES,
        now: Callable[[], float] = time.time,
    ) -> None:
        """`path` None keeps the syncs for this run only."""
        self._path = path
        self._max = max_entries
        self._now = now
        self._lock = threading.Lock()  # the newest state's number, read by the writer
        self._generation = 0
        self._writer: Optional[ThreadPoolExecutor] = None
        self._written: Optional[Future] = None
        self._entries: dict[str, dict[str, Any]] = self._load()
        self._trim()

    def audio_ms(self, key: str) -> int:
        """The channel's sound delay in milliseconds (later when positive); 0: in sync."""
        entry = self._entries.get(key) if key else None
        return int(entry["audio_ms"]) if entry is not None else 0

    def set_audio_ms(self, key: str, ms: int) -> None:
        """Keep the channel's sound delay (0 forgets it); write it soon."""
        if not key:
            return
        ms = clamp(ms)
        if not ms:
            if self._entries.pop(key, None) is None:
                return
        else:
            if self.audio_ms(key) == ms:
                return
            self._entries[key] = {"audio_ms": ms, "set": self._now()}
            self._trim()
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
            _LOG.info("Ignoring the saved sound sync: %s", safe_message(e))
            return {}
        channels = data.get("channels") if isinstance(data, dict) else None
        if not isinstance(channels, dict):
            _LOG.info("Ignoring the saved sound sync: it is not QiTV's")
            return {}
        if data.get("version") != _VERSION:
            _LOG.info("Ignoring the saved sound sync: it is of another version")
            return {}
        entries = {}
        for key, value in channels.items():
            entry = _entry(value)
            if isinstance(key, str) and key and entry is not None:
                entries[key] = entry
        return entries

    def _trim(self) -> None:
        """Beyond the cap, the syncs set longest ago go first."""
        excess = len(self._entries) - self._max
        if excess > 0:
            for key in sorted(self._entries, key=lambda k: self._entries[k]["set"])[:excess]:
                del self._entries[key]

    def _save(self) -> None:
        if self._path is None:
            return
        snapshot = {"version": _VERSION, "channels": dict(self._entries)}
        with self._lock:
            self._generation += 1
            generation = self._generation
        if self._writer is None:
            self._writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sound-sync")
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
                dir=os.path.dirname(path) or ".", prefix=".sound_sync-", suffix=".tmp"
            )
            with os.fdopen(fd, "wb") as f:
                f.write(json.dumps(snapshot))
            os.replace(temporary, path)
            temporary = None
        except Exception as e:  # anything else would stay in the Future, unseen
            _LOG.warning("Couldn't save the sound sync: %s", safe_message(e))
        finally:
            if temporary is not None:
                try:
                    os.remove(temporary)
                except OSError:
                    pass
