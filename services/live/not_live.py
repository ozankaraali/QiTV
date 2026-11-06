"""A connection answered with a recording, not the channel (2.8.10; 2.8.11: the tune too).

A provider allowing one stream closed the channel's connection, and the reconnect a second
later was answered with a file: 600 s of near-empty pictures (450 bytes each, 30 a second)
and silent sound, sent in 2.4 s, then nothing (likely its "connection limit" clip, the old
connection still counted). The pipeline took it as 600 s of the channel: stay-live read
490 s behind, opened at its end, and the picture stayed black. The same file answered tunes
too, zapping on that provider (2.8.11): "573 s behind" before a picture ever played. Live
media comes at about real time; a connection's backlog (the provider's buffer, up to some 30 s
in the recordings) comes faster, but only once and briefly. A source's clock may also run
ahead of the wall clock over a long connection (one provider re-sends seconds at each restart:
220 s ahead within an hour), so what tells a file is its speed, right after a connection opened.

`NotLiveGuard` holds each connection's bytes (`connected()`, the tune's as each reconnect's)
and measures their media: per PID, how far its highest PTS moved on, in steps of at most
`_STEP_MAX_S` (a jump or a file's loop adds nothing; a picture shown before one already
counted, as B pictures are, adds nothing either); the PID that moved furthest is the media.
No program is needed, so a tune's bytes are measured before anything read its PMT. They pass
on, in order, once the arrival settled (in the last `_SETTLE_S` of a `_HOLD_MIN_S` hold at
least, at most `_SETTLE_RATE` times real time came: a live source after its backlog; the rule
stay-live's first play waits for, so a tune's start trusts it: `settled`), or after
`_HOLD_MAX_S`, or once `_HOLD_BYTES` are held. More than `_NOT_LIVE_S` of media ahead of the
time since the connection opened, before that, is no live channel when it is as light as such a
clip (under `_CLIP_BYTES_PER_S`; 2.9.7: a full-HD channel answered each connection with its
backlog of 91 s, in 1.4 s, and was never played): everything held is dropped,
and so is the rest of that connection (`not_live` says so; the caller has the source connect
again later). Pure logic: the clock is injected.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from typing import Optional

from services.live.ts import PACKET, _pes_start, pts_delta

_CLOCK = 90000
_NOT_LIVE_S = 90.0  # media this far ahead of the time since the connection is no live channel
# ... when it is this light: a channel's pictures weigh more (2.9.7: a full-HD channel's
# backlog of 91 s was dropped). Bytes per second of media, some 380 kbit/s.
_CLIP_BYTES_PER_S = 48_000
_HOLD_MIN_S = 1.0  # a connection's bytes are held at least this long ...
_SETTLE_S = 1.0  # ... until the last this-long brought at most ...
_SETTLE_RATE = 2.0  # ... this many times as much media (its backlog landed) ...
_HOLD_MAX_S = 4.0  # ... or this long (VLC plays from its cushion meanwhile) ...
_HOLD_BYTES = 128 * 1024 * 1024  # ... or this much (40 s of 25 Mbit/s)
_STEP_MAX_S = 2.0  # a PTS step longer than this (a jump, a loop) counts as no media


class NotLiveGuard:
    """Holds a connection's bytes until they prove live (module docstring)."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._held: Optional[list[bytes]] = None  # None: bytes pass on as they come
        self._dropping = False
        self.not_live = False  # the connection that is being dropped is no live channel
        # (media, seconds, bytes) of the last connection dropped
        self.last: Optional[tuple[float, float, int]] = None
        # How long the last connection let go was held, when it was let go as its arrival
        # settled (0.0: it was not, or let go for another reason).
        self.settled = 0.0
        self._start = 0.0
        self._bytes = 0
        self._carry = b""
        self._pids: dict[int, list[float]] = {}  # pid: [highest PTS, media it moved on]
        self._media = 0.0
        self._marks: deque[tuple[float, float]] = deque()  # (since the connection, media)

    @property
    def holding(self) -> bool:
        return self._held is not None

    def connected(self) -> None:
        """A connection begins: its bytes are held until they prove live."""
        self._held, self._dropping, self.not_live = [], False, False
        self.settled = 0.0
        self._start, self._bytes, self._carry = self._clock(), 0, b""
        self._pids, self._media = {}, 0.0
        self._marks.clear()

    def feed(self, data: bytes) -> list[bytes]:
        """What passes on of `data` (and of what was held before it)."""
        if self._dropping:
            return []
        if self._held is None:
            return [data]
        self._held.append(data)
        self._bytes += len(data)
        self._scan(data)
        return self._judge()

    def tick(self) -> list[bytes]:
        """While bytes are held and none come: what passes on now."""
        return self._judge() if self._held is not None else []

    def finish(self) -> list[bytes]:
        """The input ends: what is held passes on (unless it is being dropped)."""
        held, self._held = self._held, None
        return held or []

    def _scan(self, data: bytes) -> None:
        buf = self._carry + data
        end = len(buf) - len(buf) % PACKET
        self._carry = buf[end:]
        pids = self._pids
        longest = _STEP_MAX_S * _CLOCK
        for off in range(0, end, PACKET):
            if buf[off] != 0x47 or not buf[off + 1] & 0x40:
                continue
            pkt = buf[off : off + PACKET]
            _, pts, _ = _pes_start(pkt)
            if pts is None:
                continue
            pid = (pkt[1] & 0x1F) << 8 | pkt[2]
            seen = pids.get(pid)
            if seen is None:
                pids[pid] = [pts, 0.0]
                continue
            step = pts_delta(int(seen[0]), pts)
            if 0 < step <= longest:
                seen[0] = pts
                seen[1] += step / _CLOCK
                if seen[1] > self._media:
                    self._media = seen[1]
            elif abs(step) > longest:
                seen[0] = pts  # a jump, either way: measured on from there
            # A small step back: a picture shown before one already counted.

    def _judge(self) -> list[bytes]:
        held = self._held
        assert held is not None
        since = self._clock() - self._start
        media = self._media
        marks = self._marks
        marks.append((since, media))
        while len(marks) > 1 and marks[1][0] <= since - _SETTLE_S:
            marks.popleft()
        if media - since > _NOT_LIVE_S and self._bytes < _CLIP_BYTES_PER_S * media:
            self._held, self._dropping, self.not_live = None, True, True
            self.last = (media, since, self._bytes)
            return []
        earlier = marks[0]
        settled = (
            since >= _HOLD_MIN_S
            and earlier[0] <= since - _SETTLE_S + 1e-9
            and media - earlier[1] <= _SETTLE_RATE * (since - earlier[0])
        )
        if settled or since >= _HOLD_MAX_S or self._bytes >= _HOLD_BYTES:
            self._held = None
            self.settled = since if settled else 0.0
            return held
        return []
