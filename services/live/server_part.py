"""A server part (2.6.2): a channel's own server window, past QiTV's buffer.

A live HLS playlist may keep hours of its channel. When the user goes back past what QiTV
buffered, the session opens a second helper session of the channel, the server part
(`LiveEngine.open_session(window=...)`): the same playlist from that far back, its input ending
before the media sequence the live buffer begins with (it never fetches what the buffer
holds), paced to a minute ahead of the player. Its segments are placed on the live session's
own timeline, before the buffer's start: flush against the buffer's segment that begins its
stop (the helper says how much media lies before it, `part_media_s`), else by its first
segment's time of day (`part_wall` on the live timeline, `services.live.parts.t_at`, which
goes on backwards before the oldest part; a few seconds early, measured). Position, the bar
and seeks so keep one coordinate system. Pure functions and data; no Qt.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional, Sequence

from services.live.parts import Part, t_at, wall_at
from services.live.timeline import Span


@dataclass
class ServerPart:
    sid: str  # its helper session
    asked: float  # the live timeline second the seek that opened it asked for
    until: Optional[int]  # the media sequence its input ends before (the buffer's first)
    offset: Optional[float] = None  # the live timeline second of its own second 0
    ended: bool = False  # its input ended (at its stop, or the window's end)

    def place(
        self,
        part_wall: float,
        parts: Sequence[Part],
        *,
        live_edge: float,
        now: float,
        media_before: Optional[float] = None,
        live: Sequence[Span] = (),
    ) -> None:
        """Place it once the helper's state says where: flush against the buffer's segment
        that begins its stop, `media_before` (its media up to the stop) before it; without
        that segment, by its first segment's time of day (which runs a few seconds early:
        a live segment arrives after its time in the playlist). A later state leaves it."""
        if self.offset is not None:
            return
        stop = next((s.start for s in live if s.media_seq == self.until), None)
        if media_before is not None and stop is not None and self.until is not None:
            self.offset = stop - media_before
            return
        t = t_at(parts, part_wall, live_edge=live_edge, now=now)
        self.offset = self.asked if t is None else t  # None: a gap of the buffer's

    def spans(self, own: Sequence[Span]) -> list[Span]:
        """Its spans on the live timeline; [] until it is placed."""
        offset = self.offset
        if offset is None:
            return []
        return [replace(s, start=s.start + offset, end=s.end + offset) for s in own]

    def holds(self, own: Sequence[Span], t: float) -> bool:
        """It has live timeline second `t` buffered."""
        placed = self.spans(own)
        return bool(placed) and placed[0].start <= t < placed[-1].end


def back_for(t: float, parts: Sequence[Part], *, live_edge: float, now: float) -> float:
    """How far back from now live timeline second `t` lies (a part opens that far back)."""
    return now - wall_at(parts, t, live_edge=live_edge, now=now)


def buffer_first_media(live: Sequence[Span]) -> Optional[int]:
    """The media sequence of the first HLS segment the live buffer holds from its start."""
    return next((span.media_seq for span in live if span.media_seq is not None), None)


def hop_seq(live: Sequence[Span], until: Optional[int]) -> Optional[int]:
    """Where playback goes on in the live buffer once a part ends: the segment beginning the
    part's stop, else (evicted since, or no stop) the buffer's oldest; None when it is empty."""
    if not live:
        return None
    if until is not None:
        for span in live:
            if span.media_seq == until:
                return span.seq
    return live[0].seq
