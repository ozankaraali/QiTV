"""What QiTV learned about each live channel (2.0.2), for that channel's next tune.

Per (provider, channel): what 2.0.2's Auto latency added to its target (`extra`, which 2.1
reads as the learned target's prior and drops on its first save), whether VLC can play the
channel at a rate other than 1x (`RateHealth`), and whether it plays directly because VLC
crawled through the pipeline even at 1x (an HLS channel). Since 2.1 also its learned target
(`Learned`: the arrival need and the starvation prior, three small numbers; see
`services.live.learned_target`), which Auto latency steers by, and whether it decodes in
software because hardware decoding froze its picture while its sound played
(`services.picture_watch`). `channel_key()` tells channels apart as the zap list does (the
stream address, else the id, else the name) and hashes that with the provider's name: a
stream address may carry credentials, and none reach the file.

A lock is kept with when it was set: from `RATE_LOCK_S` (a day) later it reads as unlocked,
so the channel's next tune tries other rates again, and rate health locks it again if VLC
still crawls (a new day from then). Playing directly is kept the same way (`DIRECT_S`): a
day later the next tune tries the pipeline again, and so is decoding in software
(`SOFTWARE_S`): a day later the next tune tries hardware decoding again. A later lesson
(Auto's extra moving) keeps their times; a time in the future (the clock was set back)
counts as expired.

`LiveTuning` keeps the entries in memory and in `live_tuning.json` beside the config, at
most `MAX_ENTRIES` (beyond it the least recently learned go first, a locked rate, direct
play or software decoding still in force last), as `services.channel_store` keeps them: read
once, written in the background, a bad entry left out. The learned target is an optional
field of version 1, so 2.0.x reads 2.1's file (it ignores the field): one that makes no
sense, or would not change the next tune (`Learned.informative`), is left out on its own,
and an entry left with nothing is none. A 2.0.2 `extra` stays as it was: `LearnedTarget` reads it as
a prior. Software decoding (2.1) is an optional field too, written only when it is so: 2.0.x reads
an entry with it as without it. So is the user's size cap (2.8, the live ⚙: `max_height`, the
lines of the largest HLS variant to play), written only when there is one; a cap is the
user's choice, so it outlasts learned entries when the file is full. A write that fails is
logged (without its path or any address).
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import logging
import math
from typing import Any, Mapping, Optional

from services.channel_store import ChannelStore
from services.live.learned_target import (
    EVENTS_MAX,
    FLOOR_S,
    HOLD_S,
    HOURS_MAX,
    NEED_MAX_S,
    Learned,
)

_LOG = logging.getLogger(__name__)

FILE_NAME = "live_tuning.json"
MAX_ENTRIES = 500
RATE_LOCK_S = 24 * 60 * 60  # a lock older than this is tried again
DIRECT_S = 24 * 60 * 60  # so is a channel played directly
SOFTWARE_S = 24 * 60 * 60  # and one decoded in software
_VERSION = 1  # 2.1's learned target is an optional field: 2.0.x still reads the file
MAX_LINES = 4320  # the largest size cap kept (8K's lines)
_MIN_HOURS = 1e-6  # a learned target's hours of playing are more than nothing
# What lasts a while, with when it was set and for how long: (flag, its time's key, seconds).
_FLAGS = (
    ("rate_locked", "locked_at", RATE_LOCK_S),
    ("direct", "direct_at", DIRECT_S),
    ("software", "software_at", SOFTWARE_S),
)


@dataclass(frozen=True)
class Tuning:
    """What one channel taught: nothing, by default."""

    extra: Optional[float] = None  # what Auto latency adds to the target in force (if any)
    rate_locked: bool = False  # VLC cannot play the channel at a rate other than 1x
    direct: bool = False  # it crawled through the pipeline even at 1x: play it directly
    learned: Optional[Learned] = None  # its learned target (2.1), if any
    software: bool = False  # hardware decoding froze its picture: decode it in software
    max_height: int = 0  # 2.8: the user's size cap for its HLS variants (0: the best)


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


def _lines(value: Any) -> int:
    """A stored size cap: whole lines up to `MAX_LINES`, else 0 (none)."""
    return value if type(value) is int and 0 < value <= MAX_LINES else 0


def _entry(value: Any) -> Optional[dict[str, Any]]:
    """A stored entry as written by `remember()`; None when it is not one. A learned target
    that makes no sense is left out on its own."""
    if not isinstance(value, dict):
        return None
    extra, locked, used = value.get("extra"), value.get("rate_locked", False), value.get("used")
    direct, learned = value.get("direct", False), _learned(value.get("learned"))
    software = value.get("software", False)
    if extra is not None and not (_number(extra) and math.isfinite(extra)):
        return None
    if not isinstance(locked, bool) or not isinstance(direct, bool) or not _number(used):
        return None
    if not isinstance(software, bool):
        return None
    cap = _lines(value.get("max_height"))  # a wrong one is left out on its own
    lessons = (extra is not None, locked, direct, learned is not None, software, cap)
    if not any(lessons):
        return None
    entry = {"extra": extra, "rate_locked": locked, "direct": direct, "used": used}
    if learned is not None:
        entry["learned"] = learned
    if software:
        entry["software"] = True
    if cap:
        entry["max_height"] = cap
    for flag, at, _lasts in _FLAGS:
        if entry.get(flag):
            when = value.get(at, used)  # without its time: when it was learned
            if not _number(when):
                return None
            entry[at] = when
    return entry


def _in_force(entry: Mapping[str, Any], now: float) -> tuple[bool, bool, bool]:
    """(rate locked, played directly, decoded in software) of a stored entry at `now`: each
    for its time from when it was set; a time in the future counts as expired."""
    flags = [bool(entry.get(flag)) and 0 <= now - entry[at] < lasts for flag, at, lasts in _FLAGS]
    return flags[0], flags[1], flags[2]


class LiveTuning(ChannelStore):
    """`LiveTuning(path)`; `path` None keeps what is learned for this run only."""

    WHAT = "live tuning"
    VERSION = _VERSION
    MAX_ENTRIES = MAX_ENTRIES
    LOG = _LOG
    _entry = staticmethod(_entry)

    def get(self, key: str) -> Tuning:
        """What the channel taught; a lock from `RATE_LOCK_S` ago or longer reads as none,
        and so do playing directly from `DIRECT_S` ago and software decoding from
        `SOFTWARE_S` ago."""
        entry = self._entries.get(key) if key else None
        if entry is None:
            return Tuning()
        locked, direct, software = _in_force(entry, self._now())
        learned = entry.get("learned")
        return Tuning(
            entry["extra"],
            locked,
            direct,
            Learned(**learned) if learned else None,
            software=software,
            max_height=entry.get("max_height", 0),
        )

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
        cap = _lines(tuning.max_height)
        lessons = (tuning.extra is not None, tuning.rate_locked, tuning.direct, tuning.software)
        if not any(lessons) and not learned and not cap:
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
            if tuning.software:
                entry["software"] = True  # only when it is so (2.0.x reads it as without)
            if cap:
                entry["max_height"] = cap  # 2.8: only when there is one
            # A lock (or direct play, or software decoding) still in force keeps its time;
            # else it counts from now.
            for flag, at, lasts in _FLAGS:
                if entry.get(flag):
                    entry[at] = now
                    if old is not None and old.get(flag) and 0 <= now - old[at] < lasts:
                        entry[at] = old[at]
            self._entries[key] = entry
            self._trim(now)
        self._save()

    def _trim(self, now: Optional[float] = None) -> None:
        """Drop the entries beyond the cap: the least recently learned first, those with a
        locked rate, direct play or software decoding still in force, or a size cap, last."""
        excess = len(self._entries) - self._max
        if excess > 0:
            when = self._now() if now is None else now

            def worth(key: str) -> tuple[bool, float]:
                entry = self._entries[key]
                kept = any(_in_force(entry, when)) or "max_height" in entry
                return kept, entry["used"]

            for key in sorted(self._entries, key=worth)[:excess]:
                del self._entries[key]
