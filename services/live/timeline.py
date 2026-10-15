"""Continuous playback timeline for live segments.

Pure logic: segment first-PTS values (90 kHz, 33-bit, wrap-aware) and injected wall
times become contiguous seconds. A missing, backward, zero or oversized PTS delta
(clock reset, B-frame/equal stamps, encoder restart) falls back to wall time, so the
timeline stays monotone and nothing is aborted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

PTS_TICKS_PER_SECOND = 90_000
_PTS_MODULUS = 1 << 33
_MIN_WALL_DURATION = 0.04
_FORCED_CUT = 10.0


def _pts_delta(a: int, b: int) -> int:
    """Signed ticks from ``a`` to ``b`` on the 33-bit PTS circle."""
    delta = (b - a) % _PTS_MODULUS
    if delta >= _PTS_MODULUS // 2:
        delta -= _PTS_MODULUS
    return delta


@dataclass(frozen=True)
class Span:
    seq: int
    start: float
    end: float
    discontinuity: bool


@dataclass
class _Open:
    seq: int
    first_pts: Optional[int]
    wall: float
    discontinuity: bool


class Timeline:
    def __init__(self, target_s: float = 2.0) -> None:
        self._target = target_s
        self._spans: List[Span] = []
        self._end = 0.0
        self._open: Optional[_Open] = None

    # -- segment lifecycle -------------------------------------------------
    def on_opened(
        self, seq: int, first_pts: Optional[int], wall: float, discontinuity: bool
    ) -> None:
        self._open = _Open(seq, first_pts, wall, discontinuity)

    def on_closed(self, seq: int, next_first_pts: Optional[int], wall: float) -> Span:
        opened = self._open
        if opened is None or opened.seq != seq:
            raise ValueError("segment %r is not open" % seq)
        limit = 5 * self._target
        duration = self._pts_seconds(opened.first_pts, next_first_pts)
        discontinuity = opened.discontinuity
        if duration is None or duration <= 0 or duration > limit:
            duration = min(max(wall - opened.wall, _MIN_WALL_DURATION), limit)
            discontinuity = True
        span = Span(seq, self._end, self._end + duration, discontinuity)
        self._spans.append(span)
        self._end = span.end
        self._open = None
        return span

    def on_evicted(self, seq: int) -> None:
        self._spans = [span for span in self._spans if span.seq > seq]

    # -- queries -----------------------------------------------------------
    def spans(self) -> List[Span]:
        return list(self._spans)

    @property
    def start(self) -> float:
        return self._spans[0].start if self._spans else self._end

    @property
    def end(self) -> float:
        return self._end

    @property
    def oldest_seq(self) -> Optional[int]:
        return self._spans[0].seq if self._spans else None

    @property
    def newest_seq(self) -> Optional[int]:
        return self._spans[-1].seq if self._spans else None

    def live_edge(self, latest_pts: Optional[int], now_wall: float) -> float:
        opened = self._open
        if opened is None:
            return self._end
        progress = self._pts_seconds(opened.first_pts, latest_pts)
        if progress is None or progress < 0 or progress > _FORCED_CUT:
            progress = now_wall - opened.wall
        return self._end + min(max(progress, 0.0), _FORCED_CUT)

    def locate(self, t: float) -> Tuple[int, float]:
        """Segment seq and in-segment offset for ``t``, clamped to the buffer."""
        if not self._spans:
            raise LookupError("timeline is empty")
        t = min(max(t, self.start), self._end)
        for span in self._spans:
            if t < span.end:
                return span.seq, t - span.start
        last = self._spans[-1]
        return last.seq, last.end - last.start

    def seq_after(self, t: float) -> int:
        """First buffered segment starting at or after ``t`` (else the next to come)."""
        for span in self._spans:
            if span.start >= t:
                return span.seq
        if self._open is not None:
            return self._open.seq
        return self._spans[-1].seq + 1 if self._spans else 0

    @staticmethod
    def _pts_seconds(a: Optional[int], b: Optional[int]) -> Optional[float]:
        if a is None or b is None:
            return None
        return _pts_delta(a, b) / PTS_TICKS_PER_SECOND
