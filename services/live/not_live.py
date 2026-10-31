"""A reconnect answered with a recording, not the channel (2.8.10).

A provider allowing one stream closed the channel's connection, and the reconnect a second
later was answered with a file: 600 s of near-empty pictures (450 bytes each, 30 a second)
and silent sound, sent in 2.4 s, then nothing (likely its "connection limit" clip, the old
connection still counted). The pipeline took it as 600 s of the channel: stay-live read
490 s behind, opened at its end, and the picture stayed black. Live media comes at about real
time; a reconnect's backlog (the provider's buffer, up to some 30 s in the recordings) comes
faster, but only once and briefly. A source's clock may also run ahead of the wall clock over
a long connection (one provider re-sends seconds at each restart: 220 s ahead within an
hour), so what tells a file is its speed, right after a reconnect.

`NotLiveGuard` holds each new connection's bytes (`reconnect()`) and measures their media:
the steps of the video PID's PTS, each of at most `_STEP_MAX_S` (a jump or a file's loop adds
nothing). They pass on, in order, once the arrival settled (in the last `_SETTLE_S` of a
`_HOLD_MIN_S` hold at least, at most `_SETTLE_RATE` times real time came: a live source after
its backlog), or after `_HOLD_MAX_S`, or once `_HOLD_BYTES` are held. More than
`_NOT_LIVE_S` of media ahead of the time since the reconnect, before that, is no live
channel: everything held is dropped, and so is the rest of that connection (`not_live` says
so; the caller has the source connect again later). The first connection (the tune) is not
held. Pure logic: the video PID is the caller's, and the clock is injected.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from typing import Optional

from services.live.ts import PACKET, _pes_start, pts_delta

_CLOCK = 90000
_NOT_LIVE_S = 90.0  # media this far ahead of the time since the reconnect is no live channel
_HOLD_MIN_S = 0.5  # a reconnect's bytes are held at least this long ...
_SETTLE_S = 0.5  # ... until the last this-long brought at most ...
_SETTLE_RATE = 1.5  # ... this many times as much media (its backlog landed) ...
_HOLD_MAX_S = 4.0  # ... or this long (VLC plays from its cushion meanwhile) ...
_HOLD_BYTES = 128 * 1024 * 1024  # ... or this much (40 s of 25 Mbit/s)
_STEP_MAX_S = 2.0  # a PTS step longer than this (a jump, a loop) counts as no media


class NotLiveGuard:
    """Holds a reconnect's bytes until they prove live (module docstring)."""

    def __init__(
        self, program: Callable[[], tuple[Optional[int], Optional[int]]], clock: Callable[[], float]
    ) -> None:
        self._program = program
        self._clock = clock
        self._held: Optional[list[bytes]] = None  # None: bytes pass on as they come
        self._dropping = False
        self.not_live = False  # the connection that is being dropped is no live channel
        self.last: Optional[tuple[float, float]] = None  # (media, seconds) of the last dropped
        self._start = 0.0
        self._bytes = 0
        self._carry = b""
        self._pts: Optional[int] = None
        self._media = 0.0
        self._marks: deque[tuple[float, float]] = deque()  # (since the reconnect, media)

    @property
    def holding(self) -> bool:
        return self._held is not None

    def reconnect(self) -> None:
        """A new connection begins: its bytes are held until they prove live."""
        self._held, self._dropping, self.not_live = [], False, False
        self._start, self._bytes, self._carry = self._clock(), 0, b""
        self._pts, self._media = None, 0.0
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
        video, _ = self._program()
        buf = self._carry + data
        end = len(buf) - len(buf) % PACKET
        self._carry = buf[end:]
        if video is None:
            return
        for off in range(0, end, PACKET):
            pkt = buf[off : off + PACKET]
            if pkt[0] != 0x47 or not pkt[1] & 0x40 or ((pkt[1] & 0x1F) << 8 | pkt[2]) != video:
                continue
            _, pts, _ = _pes_start(pkt)
            if pts is None:
                continue
            if self._pts is not None:
                step = pts_delta(self._pts, pts)
                if 0 < step <= _STEP_MAX_S * _CLOCK:
                    self._media += step / _CLOCK
            self._pts = pts

    def _judge(self) -> list[bytes]:
        held = self._held
        assert held is not None
        since = self._clock() - self._start
        media = self._media
        marks = self._marks
        marks.append((since, media))
        while len(marks) > 1 and marks[1][0] <= since - _SETTLE_S:
            marks.popleft()
        if media - since > _NOT_LIVE_S:
            self._held, self._dropping, self.not_live = None, True, True
            self.last = (media, since)
            return []
        earlier = marks[0]
        settled = (
            since >= _HOLD_MIN_S
            and earlier[0] <= since - _SETTLE_S + 1e-9
            and media - earlier[1] <= _SETTLE_RATE * (since - earlier[0])
        )
        if settled or since >= _HOLD_MAX_S or self._bytes >= _HOLD_BYTES:
            self._held = None
            return held
        return []
