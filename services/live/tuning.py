"""What QiTV learned about each live channel (2.0.2), for that channel's next tune.

Per (provider, channel): what 2.0.2's Auto latency added to its target (`extra`, which 2.1
reads as the learned target's prior and drops on its first save), whether VLC can play the
channel at a rate other than 1x (`RateHealth`), and whether it plays directly because VLC
crawled through the pipeline even at 1x (an HLS channel). Since 2.1 also its learned target
(`Learned`: the arrival need and the starvation prior, three small numbers; see
`services.live.learned_target`), which Auto latency steers by. `channel_key()` tells channels apart as the
zap list does (the stream address, else the id, else the name) and hashes that with the
provider's name: a stream address may carry credentials, and none reach the file.

A lock is kept with when it was set: from `RATE_LOCK_S` (a day) later it reads as unlocked,
so the channel's next tune tries other rates again, and rate health locks it again if VLC
still crawls (a new day from then). Playing directly is kept the same way (`DIRECT_S`): a
day later the next tune tries the pipeline again. A later lesson (Auto's extra moving)
keeps their times; a time in the future (the clock was set back) counts as expired.

`LiveTuning` keeps the entries in memory and in `live_tuning.json` beside the config, at
most `MAX_ENTRIES` (beyond it the least recently learned go first, a locked rate or direct
play still in force last). The file is read once; each change writes it on one background
thread, the newest state only, through a temporary file swapped in. A missing, unreadable or
corrupt file,
or one of another version, counts as empty, and a bad entry is left out. The learned target
is an optional field of version 1, so 2.0.x reads 2.1's file (it ignores the field): one
that makes no sense, or would not change the next tune (`Learned.informative`), is left out
on its own, and an entry left with nothing is none. A 2.0.2 `extra` stays as it was:
`LearnedTarget` reads it as a prior. A write that fails is logged (without its path or any
address).
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

from services.live.learned_target import (
    EVENTS_MAX,
    FLOOR_S,
    HOLD_S,
    HOURS_MAX,
    NEED_MAX_S,
    Learned,
)
from services.redact import safe_message

_LOG = logging.getLogger(__name__)

FILE_NAME = "live_tuning.json"
MAX_ENTRIES = 500
RATE_LOCK_S = 24 * 60 * 60  # a lock older than this is tried again
DIRECT_S = 24 * 60 * 60  # so is a channel played directly
_VERSION = 1  # 2.1's learned target is an optional field: 2.0.x still reads the file
_MIN_HOURS = 1e-6  # a learned target's hours of playing are more than nothing


@dataclass(frozen=True)
class Tuning:
    """What one channel taught: nothing, by default."""

    extra: Optional[float] = None  # what Auto latency adds to the target in force (if any)
    rate_locked: bool = False  # VLC cannot play the channel at a rate other than 1x
    direct: bool = False  # it crawled through the pipeline even at 1x: play it directly
    learned: Optional[Learned] = None  # its learned target (2.1), if any


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


def _finite(value: Any, low: float, high: float) -> bool:
    return _number(value) and math.isfinite(value) and low <= value <= high


def _learned(value: Any) -> Optional[dict[str, Any]]:
    """A learned target as `remember()` writes it, when it makes sense and would change the
    next tune (`Learned.informative`, under the floor it was learned with); else None."""
    if not isinstance(value, dict):
        return None
    need, events, hours = value.get("need"), value.get("events"), value.get("hours")
    floor = value.get("floor", FLOOR_S)  # written only when not Normal's ...
    hold = value.get("hold", 0.0)  # ... and only when there is one
    if need is not None and not _finite(need, 0.0, NEED_MAX_S):
        return None
    if events is None or hours is None or not _finite(floor, 0.0, NEED_MAX_S):
        return None
    if not _finite(events, 0.0, EVENTS_MAX) or not _finite(hours, _MIN_HOURS, HOURS_MAX):
        return None
    if not _finite(hold, 0.0, HOLD_S):
        return None
    if not Learned(need, float(events), float(hours), float(floor)).informative():
        return None
    fields = {"need": need, "events": events, "hours": hours}
    if floor != FLOOR_S:
        fields["floor"] = floor
    if hold > 0:
        fields["hold"] = hold
    return fields


def _entry(value: Any) -> Optional[dict[str, Any]]:
    """A stored entry as written by `remember()`; None when it is not one. A learned target
    that makes no sense is left out on its own."""
    if not isinstance(value, dict):
        return None
    extra, locked, used = value.get("extra"), value.get("rate_locked", False), value.get("used")
    direct, learned = value.get("direct", False), _learned(value.get("learned"))
    if extra is not None and not (_number(extra) and math.isfinite(extra)):
        return None
    if not isinstance(locked, bool) or not isinstance(direct, bool) or not _number(used):
        return None
    if extra is None and not locked and not direct and learned is None:
        return None
    entry = {"extra": extra, "rate_locked": locked, "direct": direct, "used": used}
    if learned is not None:
        entry["learned"] = learned
    for flag, at in (("rate_locked", "locked_at"), ("direct", "direct_at")):
        if entry[flag]:
            when = value.get(at, used)  # without its time: when it was learned
            if not _number(when):
                return None
            entry[at] = when
    return entry


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
        """What the channel taught; a lock from `RATE_LOCK_S` ago or longer reads as none,
        and so does playing directly from `DIRECT_S` ago."""
        entry = self._entries.get(key) if key else None
        if entry is None:
            return Tuning()
        now = self._now()
        locked = entry["rate_locked"] and 0 <= now - entry["locked_at"] < RATE_LOCK_S
        direct = entry.get("direct", False) and 0 <= now - entry["direct_at"] < DIRECT_S
        learned = entry.get("learned")
        return Tuning(entry["extra"], locked, direct, Learned(**learned) if learned else None)

    def remember(self, key: str, tuning: Tuning) -> None:
        """Keep `tuning` for the channel `key` (nothing learned: forget it); write it soon.
        A learned target that makes no sense or would change nothing counts as none."""
        if not key:
            return
        learned = None
        if tuning.learned is not None:
            what = tuning.learned
            learned = _learned(
                {
                    "need": what.need,
                    "events": what.events,
                    "hours": what.hours,
                    "floor": what.floor,
                    "hold": what.hold,
                }
            )
        if tuning.extra is None and not tuning.rate_locked and not tuning.direct and not learned:
            if self._entries.pop(key, None) is None:
                return
        else:
            now, old = self._now(), self._entries.get(key)
            entry: dict[str, Any] = {
                "extra": tuning.extra,
                "rate_locked": tuning.rate_locked,
                "direct": tuning.direct,
                "used": now,
            }
            if learned is not None:
                entry["learned"] = learned
            # A lock (or direct play) still in force keeps its time; else it counts from now.
            for flag, at, lasts in (
                ("rate_locked", "locked_at", RATE_LOCK_S),
                ("direct", "direct_at", DIRECT_S),
            ):
                if entry[flag]:
                    entry[at] = now
                    if old is not None and old.get(flag) and 0 <= now - old[at] < lasts:
                        entry[at] = old[at]
            self._entries[key] = entry
            self._trim(now)
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
        if data.get("version") != _VERSION:
            _LOG.info("Ignoring the saved live tuning: it is of another version")
            return {}
        entries = {}
        for key, value in channels.items():
            entry = _entry(value)
            if isinstance(key, str) and key and entry is not None:
                entries[key] = entry
        return entries

    def _trim(self, now: Optional[float] = None) -> None:
        """Drop the entries beyond the cap: the least recently learned first, those with a
        locked rate or direct play still in force last."""
        excess = len(self._entries) - self._max
        if excess > 0:
            when = self._now() if now is None else now

            def worth(key: str) -> tuple[bool, float]:
                entry = self._entries[key]
                locked = entry["rate_locked"] and 0 <= when - entry["locked_at"] < RATE_LOCK_S
                direct = entry["direct"] and 0 <= when - entry["direct_at"] < DIRECT_S
                return bool(locked or direct), entry["used"]

            for key in sorted(self._entries, key=worth)[:excess]:
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
        except Exception as e:  # anything else would stay in the Future, unseen
            _LOG.warning("Couldn't save the live tuning: %s", safe_message(e))
        finally:
            if temporary is not None:
                try:
                    os.remove(temporary)
                except OSError:
                    pass
