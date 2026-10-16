"""What QiTV learned about each live channel (2.0.2), for that channel's next tune.

Per (provider, channel): Auto latency's target (`AutoTarget`) and whether VLC can play the
channel at a rate other than 1x (`RateHealth`). `channel_key()` tells channels apart as the
zap list does (the stream address, else the id, else the name) and hashes that with the
provider's name: a stream address may carry credentials, and none reach the file.

`LiveTuning` keeps the entries in memory and in `live_tuning.json` beside the config, at
most `MAX_ENTRIES` (the least recently learned go first). The file is read once; each change
writes it on one background thread, the newest state only, through a temporary file swapped
in. A missing, unreadable or corrupt file counts as empty, and a bad entry is left out.
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import logging
import math
import os
import tempfile
import threading
import time
from typing import Any, Callable, Mapping, Optional

import orjson as json

from services.redact import safe_message

_LOG = logging.getLogger(__name__)

FILE_NAME = "live_tuning.json"
MAX_ENTRIES = 500
_VERSION = 1


@dataclass(frozen=True)
class Tuning:
    """What one channel taught: nothing, by default."""

    target: Optional[float] = None  # Auto latency's target, when not where Auto starts
    rate_locked: bool = False  # VLC cannot play the channel at a rate other than 1x


def channel_key(provider: str, item: Mapping[str, Any]) -> str:
    """The channel's key under `provider`; "" when the item has no address, id or name."""
    cmd = str(item.get("cmd") or "").strip()
    if cmd:
        identity = f"cmd:{cmd}"
    elif item.get("id") is not None:
        identity = f"id:{item['id']}"
    elif item.get("name"):
        identity = f"name:{item['name']}"
    else:
        return ""
    return hashlib.sha256(f"{provider}\0{identity}".encode("utf-8")).hexdigest()[:32]


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _entry(value: Any) -> Optional[dict[str, Any]]:
    """A stored entry as written by `remember()`; None when it is not one."""
    if not isinstance(value, dict):
        return None
    target, locked, used = value.get("target"), value.get("rate_locked", False), value.get("used")
    if target is not None and not (_number(target) and math.isfinite(target)):
        return None
    if not isinstance(locked, bool) or not _number(used):
        return None
    if target is None and not locked:
        return None
    return {"target": target, "rate_locked": locked, "used": used}


class LiveTuning:
    def __init__(
        self,
        path: Optional[str],
        *,
        max_entries: int = MAX_ENTRIES,
        now: Callable[[], float] = time.time,
    ) -> None:
        """`path` None keeps what is learned for this run only."""
        self._path = path
        self._max = max_entries
        self._now = now
        self._lock = threading.Lock()  # the newest state's number, read by the writer
        self._generation = 0
        self._writer: Optional[ThreadPoolExecutor] = None
        self._written: Optional[Future] = None
        self._entries: dict[str, dict[str, Any]] = self._load()
        self._trim()

    def get(self, key: str) -> Tuning:
        entry = self._entries.get(key) if key else None
        if entry is None:
            return Tuning()
        return Tuning(entry["target"], entry["rate_locked"])

    def remember(self, key: str, tuning: Tuning) -> None:
        """Keep `tuning` for the channel `key` (nothing learned: forget it); write it soon."""
        if not key:
            return
        if tuning == Tuning():
            if self._entries.pop(key, None) is None:
                return
        else:
            self._entries[key] = {
                "target": tuning.target,
                "rate_locked": tuning.rate_locked,
                "used": self._now(),
            }
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
            _LOG.info("Ignoring the saved live tuning: %s", safe_message(e))
            return {}
        channels = data.get("channels") if isinstance(data, dict) else None
        if not isinstance(channels, dict):
            _LOG.info("Ignoring the saved live tuning: it is not QiTV's")
            return {}
        entries = {}
        for key, value in channels.items():
            entry = _entry(value)
            if isinstance(key, str) and key and entry is not None:
                entries[key] = entry
        return entries

    def _trim(self) -> None:
        """Drop the least recently learned entries beyond the cap."""
        excess = len(self._entries) - self._max
        if excess > 0:
            oldest = sorted(self._entries, key=lambda key: self._entries[key]["used"])
            for key in oldest[:excess]:
                del self._entries[key]

    def _save(self) -> None:
        if self._path is None:
            return
        snapshot = {"version": _VERSION, "channels": dict(self._entries)}
        with self._lock:
            self._generation += 1
            generation = self._generation
        if self._writer is None:
            self._writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="live-tuning")
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
                dir=os.path.dirname(path) or ".", prefix=".live_tuning-", suffix=".tmp"
            )
            with os.fdopen(fd, "wb") as f:
                f.write(json.dumps(snapshot))
            os.replace(temporary, path)
            temporary = None
        except (OSError, TypeError) as e:
            _LOG.warning("Couldn't save the live tuning: %s", safe_message(e))
        finally:
            if temporary is not None:
                try:
                    os.remove(temporary)
                except OSError:
                    pass
