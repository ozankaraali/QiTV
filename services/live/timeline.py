"""Continuous playback timeline for live segments.

Pure logic: segment first-PTS values (90 kHz, 33-bit, wrap-aware) and injected wall
times become contiguous seconds. A missing, backward, zero or oversized PTS delta
(clock reset, B-frame/equal stamps, encoder restart) falls back to wall time, so the
timeline stays monotone and nothing is aborted. Oversized means beyond the forced cut plus
a margin: a forced cut lands on the first frame at or past 10 s. The wall times are the
segmenter's stall-free clock (`Segmenter.wall`), so a stalled source adds no time.

Each span also says how cleanly playback can start at it (`Span.start_quality`, from the
segmenter's `Closed.start_quality`), so a jump can prefer real keyframes.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Final, List, Literal, Optional, Tuple

PTS_TICKS_PER_SECOND = 90_000
_PTS_MODULUS = 1 << 33
_MIN_WALL_DURATION = 0.04
_FORCED_CUT = 10.0
_FORCED_MARGIN = 1.0
_LIMIT = _FORCED_CUT + _FORCED_MARGIN  # the longest segment a PTS delta may size

log = logging.getLogger(__name__)

# How cleanly playback starts at a segment, best first:
StartQuality = Literal["key", "open", "none"]
# An IDR picture (H.264 NAL 5, HEVC 19/20, an MPEG-1/2 I picture in a closed GOP): every
# picture from here decodes.
START_KEY: Final = "key"
# An intra picture that later pictures may reach behind (an H.264 non-IDR intra slice, HEVC
# CRA/BLA, an MPEG-1/2 I picture in an open GOP), or a random access point whose picture
# could not be read: the pictures shown before it (its leading pictures) may come out broken.
START_OPEN: Final = "open"
# No keyframe: a forced cut (10 s without one), a cut while the video was quiet, or an
# audio-only stream's cut.
START_NONE: Final = "none"
START_QUALITIES: Final[Tuple[StartQuality, ...]] = (START_KEY, START_OPEN, START_NONE)


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
    # Spans made without one (an older helper's 4-field additions) count as keyframe starts,
    # as every segment start did before.
    start_quality: StartQuality = START_KEY


@dataclass
class _Open:
    seq: int
    first_pts: Optional[int]
    wall: float
    discontinuity: bool


class Timeline:
    def __init__(self) -> None:
        self._spans: List[Span] = []
        self._end = 0.0
        self._open: Optional[_Open] = None
        self._next_seq = 0
        self._max_edge = 0.0

    # -- segment lifecycle -------------------------------------------------
    def on_opened(
        self, seq: int, first_pts: Optional[int], wall: float, discontinuity: bool
    ) -> None:
        if self._open is not None:
            log.warning("segment %r opened while %r was still open", seq, self._open.seq)
            # Its start quality came with its Closed, which never came: no jump lands there.
            self._close(self._open, None, wall, START_NONE, force_discontinuity=True)
        self._open = _Open(seq, first_pts, wall, discontinuity)

    def on_closed(
        self,
        seq: int,
        next_first_pts: Optional[int],
        wall: float,
        start_quality: StartQuality = START_KEY,
    ) -> Span:
        """Close the open segment ``seq``; raises ValueError if it is not the open one.

        ``start_quality`` is how cleanly the segment starts (``Closed.start_quality``).
        """
        opened = self._open
        if opened is None or opened.seq != seq:
            raise ValueError("segment %r is not open" % seq)
        return self._close(opened, next_first_pts, wall, start_quality)

    def _close(
        self,
        opened: _Open,
        next_first_pts: Optional[int],
        wall: float,
        start_quality: StartQuality,
        force_discontinuity=False,
    ) -> Span:
        duration = self._pts_seconds(opened.first_pts, next_first_pts)
        discontinuity = opened.discontinuity or force_discontinuity
        if duration is None or duration < 0 or duration > _LIMIT:
            discontinuity = True
            duration = None
        if duration is None or duration == 0:
            # Equal stamps are not a reset, but carry no duration: use wall time.
            duration = min(max(wall - opened.wall, _MIN_WALL_DURATION), _LIMIT)
        span = Span(opened.seq, self._end, self._end + duration, discontinuity, start_quality)
        self._spans.append(span)
        self._end = span.end
        self._next_seq = opened.seq + 1
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
        """Buffered end plus open-segment progress; never decreases between calls.

        ``latest_pts`` is on the open segment's own axis (``Segmenter.progress_pts``) and
        ``now_wall`` on the segmenter's stall-free clock, so neither a stall nor a PTS jump
        inflates the progress. The edge is held only when a segment closes shorter than
        the wall-clock progress reported while it was open (a segment whose PTS end comes
        out earlier than its arrival time); real progress then passes it again.
        """
        edge = self._end
        opened = self._open
        if opened is not None:
            progress = self._pts_seconds(opened.first_pts, latest_pts)
            if progress is None or progress < 0 or progress > _LIMIT:
                progress = now_wall - opened.wall
            edge += min(max(progress, 0.0), _LIMIT)
        self._max_edge = max(self._max_edge, edge)
        return self._max_edge

    def locate(self, t: float) -> Optional[Tuple[int, float]]:
        """Segment seq and in-segment offset for ``t``, clamped to the closed spans.

        None when nothing is closed yet. The open segment is not addressable, so
        ``locate(live_edge(...))`` lands on the last closed span's end.
        """
        if not self._spans:
            return None
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
        return self._next_seq

    @staticmethod
    def _pts_seconds(a: Optional[int], b: Optional[int]) -> Optional[float]:
        if a is None or b is None:
            return None
        return _pts_delta(a, b) / PTS_TICKS_PER_SECOND
