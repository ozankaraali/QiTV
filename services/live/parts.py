"""A kept buffer's parts: its runs of segments, each with the time of day it began (2.6).

A channel's buffer is kept when the user leaves the channel; coming back appends to it after
a gap in which nothing was recorded (`services.live.worker`: park and resume). The helper
re-bases the new run's timestamps onto the old run's last picture, so the timeline's seconds
stay contiguous and VLC plays straight across: a gap has no width on the timeline. What
places each part in the day is its `wall`, the Unix time its first segment opened.

The newest part maps as a buffer always has, from the live edge (`now - (live_edge - t)`);
an older part from its own `wall`. The system clock may step back between two parts (an NTP
correction, a laptop asleep): each part's start is then held at or before the next one's,
so a later point never maps to an earlier time. Pure functions, no Qt.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping, Optional, Sequence


@dataclass(frozen=True)
class Part:
    first_seq: int  # its oldest segment still held
    start: float  # timeline seconds where it begins
    wall: float  # Unix seconds when that segment opened


def from_state(value: Any) -> list[Part]:
    """The snapshot's `parts` (`[[first_seq, start, wall], ...]`): entries that are not three
    finite numbers, or that do not follow the one before, are dropped; [] for anything else."""
    if not isinstance(value, (list, tuple)):
        return []
    parts: list[Part] = []
    for entry in value:
        try:
            seq, start, wall = entry
            part = Part(int(seq), float(start), float(wall))
        except TypeError, ValueError:
            continue
        if not (math.isfinite(part.start) and math.isfinite(part.wall)):
            continue
        if parts and (part.first_seq <= parts[-1].first_seq or part.start < parts[-1].start):
            continue
        parts.append(part)
    return parts


def index_of(parts: Sequence[Part], t: float) -> int:
    """The part holding `t`: the last that starts at or before it (0 when none does)."""
    index = 0
    for i, part in enumerate(parts):
        if part.start <= t:
            index = i
    return index


def junctions(parts: Sequence[Part]) -> list[float]:
    """Where each later part begins: the bar draws a gap there."""
    return [part.start for part in parts[1:]]


def newest_first_seq(parts: Sequence[Part]) -> Optional[int]:
    return parts[-1].first_seq if parts else None


def _starts(parts: Sequence[Part], live_edge: float, now: float) -> list[float]:
    """Each part's start in the day, each at or before the next one's."""
    starts = [part.wall for part in parts]
    starts[-1] = now - (live_edge - parts[-1].start)
    for i in range(len(starts) - 2, -1, -1):
        starts[i] = min(starts[i], starts[i + 1])
    return starts


def wall_at(parts: Sequence[Part], t: float, *, live_edge: float, now: float) -> float:
    """The time of day (Unix seconds) of timeline second `t`."""
    if not parts:
        return now - (live_edge - t)
    i = index_of(parts, t)
    if i == len(parts) - 1:
        return now - (live_edge - t)
    starts = _starts(parts, live_edge, now)
    return min(starts[i] + (t - parts[i].start), starts[i + 1])


def t_at(parts: Sequence[Part], wall: float, *, live_edge: float, now: float) -> Optional[float]:
    """The timeline second of time of day `wall`; None in a gap. Before the oldest part it
    goes on backwards from it (the catch-up archive's part of the bar)."""
    if not parts:
        return live_edge - (now - wall)
    starts = _starts(parts, live_edge, now)
    if wall >= starts[-1]:
        return live_edge - (now - wall)
    if wall < starts[0]:
        return parts[0].start - (starts[0] - wall)
    for i in range(len(parts) - 1):
        length = parts[i + 1].start - parts[i].start
        end = min(starts[i] + length, starts[i + 1])
        if starts[i] <= wall <= end:
            return parts[i].start + (wall - starts[i])
    return None


# A `LiveSession.timelineChanged` state: its parts and live edge place its seconds in the day.


def wall_in(state: Mapping[str, Any], t: float, now: float) -> float:
    """`wall_at` for a timeline state."""
    edge = float(state.get("live_edge", 0.0))
    return wall_at(from_state(state.get("parts")), t, live_edge=edge, now=now)


def t_in(state: Mapping[str, Any], wall: float, now: float) -> Optional[float]:
    """`t_at` for a timeline state."""
    edge = float(state.get("live_edge", 0.0))
    return t_at(from_state(state.get("parts")), wall, live_edge=edge, now=now)


def behind_in(state: Mapping[str, Any]) -> Optional[float]:
    """How far behind now a state plays: once its buffer has a gap, the time of day's
    (`behind`, the time away included), else the lag as before; None when it says neither."""
    key = "behind" if len(from_state(state.get("parts"))) > 1 else "lag"
    value = state.get(key)
    return float(value) if isinstance(value, (int, float)) else None
