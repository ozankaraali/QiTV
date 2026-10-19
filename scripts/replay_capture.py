"""Replay a captured raw MPEG-TS channel through the live helper's ingest, on a simulated clock.

    uv run python scripts/replay_capture.py <capture> [...] [--json F] [--decode] [--dump DIR]
                                            [--stages none|trim|all]

A capture is `<name>.ts` (the bytes as the connection delivered them) and `<name>.times` (one
"<cumulative bytes> <seconds since connect>" line per read; a third word "reconnect" makes the
next read a new connection's first); `<capture>` names either file or their common prefix.
An HLS capture whose separate audio was woven in has `<name>.audio.ts` and `.audio.times`
beside it: each video segment is woven with its audio again (`hls_audio.weave_capture`) and
read whole when its audio had come, as the helper read it, so the replay has sound.
Captures hold provider streams: keep them out of the repository.

The bytes go through the helper's own session code (`services.live.worker._Session._ingest`:
reconnect trimmer, repeat trimmer, clock joiner, segmenter, RAM ring, timeline, burst meter and
the state it sends the GUI; --stages trim: without the `ClockJoiner`, none: without the
`ReconnectTrimmer` and the `RepeatTrimmer` either), read by read, with
the clock set to each read's arrival time, so a three-minute capture replays in seconds. The
seams this needs, which the product does not offer: `_Session` builds its `Segmenter()` on the
real clock and calls `time.monotonic()` in `_measure` and `_notify`, so the replay swaps in a
segmenter on the simulated clock and patches the worker module's `time`; `_ingest` imports
`open_source` when it runs, so the replay patches `services.live.source.open_source` to hand it
the capture. Nothing else is stood in for, and no product file changes.

Per capture it prints (and with --json writes):
- each timestamp restart in the input (a video PTS step back of more than a second, or forward
  by `ts.JUMP_S` or more): the provider's discontinuity_indicator on the new run's first PCR, a
  packet cut short at the junction, the silence before it (since the old run last brought
  media), the burst after it (until a second brings at most `_BURST_RATE` s of media), and R:
  how far the restart moved the media ahead of the wall clock (media minus wall once the burst
  settled, its median over `_SETTLE_S`, less media minus wall before the silence; also at the
  burst's end), on the input's video and on the pipeline's own live edge; the new run's first
  keyframes and D, the last at or before R; the byte-exact repeat of earlier video frames at
  the start of the new run;
- how the pipeline took it: what the trimmer did (why, R as it measured it, what it dropped and
  how long it held the run), a segmenter discontinuity (its cut, how far into the new run, how
  cleanly that segment starts), a span only the timeline marked, or nothing; and its junction
  in the ring: a packet cut short passed on as a whole packet (its missing bytes taken from the
  next one), the indicator reaching VLC, continuity counter breaks no indicator announces;
- the live edge the GUI hears, minus the wall clock, each second, with the burst and backlog it
  hears and how far it trails the pipeline's own edge (`_notify` throttles); its drift once the
  connect backlog landed, and that drift without D, and without all of R, at each restart;
- the video frames in the ring that repeat an earlier frame byte for byte (VLC plays them
  twice);
- with --decode, the video decoder's error lines from ffmpeg on the ring's segments at each
  junction and on a few away from them (ffmpeg is a lab tool here; the product never uses it).
With --dump, the stream VLC reads from the oldest segment is written out as well (it holds the
provider's stream too).
"""

from __future__ import annotations

import argparse
import bisect
from dataclasses import dataclass, field
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from typing import Any, Iterator, Optional
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.live import source as source_module, worker  # noqa: E402
from services.live.capture import AUDIO_PAIR, captured_segments  # noqa: E402
from services.live.hls_audio import weave_capture  # noqa: E402
from services.live.timeline import START_KEY, START_OPEN, Span  # noqa: E402
from services.live.ts import (  # noqa: E402  (the segmenter's own picture reading)
    JUMP_S,
    Data,
    Event,
    Opened,
    Rejoin,
    Segmenter,
    Trim,
    _classify,
    _quality,
    pts_delta,
)
from services.vlc_profile import DEFAULT_USER_AGENT  # noqa: E402

PACKET = 188
SYNC = 0x47
CLOCK = 90000
_VIDEO_TYPES = (0x1B, 0x24, 0x02, 0x01)
_AUDIO_TYPES = (0x03, 0x04, 0x0F, 0x11, 0x81, 0x87)
_REORDER_S = 1.0  # a PTS step back by more is a restart (B-frame reordering is less)
_HEAD_BYTES = 2048  # ES bytes of a PES kept to tell its picture (`ts._classify`)
_FLAG_WINDOW = 64  # packets before a restart's first PES where its run may begin
_STEP_S = 0.25  # the resolution of the burst and settle searches
_BURST_RATE = 1.25  # a burst ends once a second brings no more than this much media ...
_BURST_MAX_S = 20.0  # ... or this long after the junction
_SETTLE_S = 5.0  # R takes the median media minus wall over this long after the burst
_REPEAT_SEARCH_S = 2.0  # a byte-exact repeat counts when it starts this early in the run
_BACKLOG_S = 10.0  # the drift is measured from this long after connect
_DECODE_BASELINE = 3  # segment pairs away from junctions decoded for comparison
_DECODERS = ("[h264 @", "[hevc @", "[mpeg2video @", "[mpeg1video @")


# ---- the capture ---------------------------------------------------------------------------


@dataclass
class Capture:
    name: str
    data: bytes
    reads: list[tuple[int, float]]  # (cumulative bytes, seconds since connect) per read
    reconnects: list[int] = field(default_factory=list)  # where a new connection's bytes begin
    _ends: list[int] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        self._ends = [end for end, _ in self.reads]

    @property
    def seconds(self) -> float:
        return self.reads[-1][1] if self.reads else 0.0

    def arrival(self, offset: int) -> float:
        """When the byte at `offset` arrived: the time of the read that delivered it."""
        index = bisect.bisect_right(self._ends, offset)
        return self.reads[min(index, len(self.reads) - 1)][1]


def load_capture(path: str | Path) -> Capture:
    path = str(path)
    for suffix in (".ts", ".times"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
    if Path(path + AUDIO_PAIR + ".ts").exists():
        return _load_woven(path)
    data = Path(path + ".ts").read_bytes()
    reads: list[tuple[int, float]] = []
    reconnects: list[int] = []
    for line in Path(path + ".times").read_text().splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2] == "reconnect":
            reconnects.append(int(parts[0]))
        elif len(parts) != 2:
            continue
        end, at = int(parts[0]), float(parts[1])
        if reads and (end < reads[-1][0] or at < reads[-1][1]):
            raise ValueError(f"{path}.times is not monotone at {end} bytes")
        reads.append((min(end, len(data)), at))
    if not reads or reads[-1][0] < len(data):
        reads.append((len(data), reads[-1][1] if reads else 0.0))
    return Capture(Path(path).name, data, reads, reconnects)


def _load_woven(path: str) -> Capture:
    """An HLS capture with its separate audio's pair: each video segment woven with its audio
    again (`hls_audio.weave_capture`), read whole once its audio had come, as the helper read
    it then."""
    video = captured_segments(path)
    audio = {segment.sequence: segment for segment in captured_segments(path + AUDIO_PAIR)}
    woven = weave_capture(
        [(segment.sequence, segment.data) for segment in video],
        {sequence: segment.data for sequence, segment in audio.items()},
    )
    reads: list[tuple[int, float]] = []
    end = 0
    for segment, data in zip(video, woven):
        end += len(data)
        heard = audio.get(segment.sequence)
        reads.append((end, max(segment.at, heard.at) if heard is not None else segment.at))
    return Capture(Path(path).name, b"".join(woven), reads or [(0, 0.0)])


# ---- MPEG-TS reading -----------------------------------------------------------------------


@dataclass(slots=True)
class Pes:
    """One PES: where its first packet is, its timestamps and its ES bytes' digest."""

    offset: int
    pts: Optional[int]
    start: str  # how cleanly a picture starts here: the timeline's START_* ("none" for audio)
    digest: bytes


@dataclass
class Scan:
    video_pid: Optional[int] = None
    audio_pid: Optional[int] = None
    video_type: Optional[int] = None
    video: list[Pes] = field(default_factory=list)
    audio: list[Pes] = field(default_factory=list)
    pcrs: list[tuple[int, int, bool]] = field(default_factory=list)  # (offset, base, indicator)
    flagged: list[int] = field(default_factory=list)  # packets with a discontinuity_indicator
    cut_short: list[tuple[int, int]] = field(default_factory=list)  # (offset, bytes)
    skipped: list[tuple[int, int]] = field(default_factory=list)  # (offset, bytes) out of sync
    cc_breaks: list[tuple[int, int]] = field(default_factory=list)  # (offset, pid) unannounced


def _find_sync(data: bytes, start: int) -> int:
    """The first offset >= start with sync bytes there and 188 and 376 bytes on; -1 if none."""
    stop = len(data) - 2 * PACKET
    i = data.find(b"\x47", start, max(start, stop))
    while i >= 0:
        if data[i + PACKET] == SYNC and data[i + 2 * PACKET] == SYNC:
            return i
        i = data.find(b"\x47", i + 1, stop)
    return -1


def _section(data: bytes, at: int, end: int) -> Optional[bytes]:
    """The PSI section starting in the packet ending at `end` (its pointer field at `at`)."""
    begin = at + 1 + data[at]
    if begin + 3 > end:
        return None
    return data[begin : min(begin + 3 + (((data[begin + 1] & 0x0F) << 8) | data[begin + 2]), end)]


def _timestamp(data: bytes, at: int) -> int:
    b = data[at : at + 5]
    return ((b[0] >> 1) & 7) << 30 | b[1] << 22 | (b[2] >> 1) << 15 | b[3] << 7 | b[4] >> 1


def _pcr(data: bytes, at: int) -> int:
    b = data[at : at + 5]
    return (b[0] << 25) | (b[1] << 17) | (b[2] << 9) | (b[3] << 1) | (b[4] >> 7)


class _Assembly:
    """The PES being read on one PID."""

    __slots__ = ("offset", "pts", "rai", "hash", "head")

    def __init__(self, offset: int, pts: Optional[int], rai: bool) -> None:
        self.offset, self.pts, self.rai = offset, pts, rai
        self.hash = hashlib.blake2b(digest_size=12)
        self.head = bytearray()

    def add(self, chunk: memoryview) -> None:
        self.hash.update(chunk)
        if len(self.head) < _HEAD_BYTES:
            self.head += chunk[: _HEAD_BYTES - len(self.head)]


def scan_ts(data: bytes, header_len: int = 0) -> Scan:
    """Read a transport stream: its first program's video and audio PES, its PCRs, every
    discontinuity_indicator, packets cut short (three sync bytes 188 apart begin less than 188
    bytes after one), bytes out of sync, and counter breaks no indicator announces (counting
    afresh after `header_len` bytes: the copies of the tables a ring segment starts with)."""
    scan = Scan()
    view = memoryview(data)
    n = len(data)
    pmt_pid: Optional[int] = None
    counters: dict[int, int] = {}
    reading: dict[int, _Assembly] = {}
    forget = header_len > 0

    def flush(pid: int) -> None:
        pes = reading.pop(pid, None)
        if pes is None:
            return
        if pid == scan.video_pid:
            start = _quality(_classify(bytes(pes.head), 0, scan.video_type), pes.rai)
            scan.video.append(Pes(pes.offset, pes.pts, start or "none", pes.hash.digest()))
        else:
            scan.audio.append(Pes(pes.offset, pes.pts, "none", pes.hash.digest()))

    i = _find_sync(data, 0)
    while 0 <= i and i + PACKET <= n:
        if data[i] != SYNC or (i + PACKET < n and data[i + PACKET] != SYNC):
            j = _find_sync(data, i + 1)
            if j < 0:
                break
            if data[i] == SYNC and j - i < PACKET:
                scan.cut_short.append((i, j - i))
            else:
                scan.skipped.append((i, j - i))
            i = j
            continue
        end = i + PACKET
        b1, b3 = data[i + 1], data[i + 3]
        pid = ((b1 & 0x1F) << 8) | data[i + 2]
        afc = (b3 >> 4) & 3
        start = i + 4
        flag = rai = False
        if afc & 2:
            length = data[i + 4]
            start = i + 5 + length
            if length:
                flags = data[i + 5]
                flag, rai = bool(flags & 0x80), bool(flags & 0x40)
                if flag:
                    scan.flagged.append(i)
                if flags & 0x10 and length >= 7:
                    scan.pcrs.append((i, _pcr(data, i + 6), flag))
        if b1 & 0x80 or not afc & 1 or start >= end:  # errored, or no payload
            i = end
            continue
        if forget and i >= header_len:
            forget = False
            counters.clear()
        if pid != 0x1FFF:
            last, cc = counters.get(pid), b3 & 0x0F
            if last is not None and cc != last and cc != (last + 1) & 0x0F and not flag:
                scan.cc_breaks.append((i, pid))
            counters[pid] = cc
        pusi = b1 & 0x40
        if pid == 0 and pusi:
            section = _section(data, start, end)
            if section and len(section) >= 16 and section[0] == 0x00:
                for k in range(8, len(section) - 7, 4):
                    if (section[k] << 8) | section[k + 1]:  # program 0 is the network table
                        pmt_pid = ((section[k + 2] & 0x1F) << 8) | section[k + 3]
                        break
        elif pid == pmt_pid and pusi and scan.video_pid is None and scan.audio_pid is None:
            section = _section(data, start, end)
            if section and len(section) >= 16 and section[0] == 0x02:
                k = 12 + (((section[10] & 0x0F) << 8) | section[11])
                while k + 5 <= len(section) - 4:
                    kind, es_pid = section[k], ((section[k + 1] & 0x1F) << 8) | section[k + 2]
                    if scan.video_pid is None and kind in _VIDEO_TYPES:
                        scan.video_pid, scan.video_type = es_pid, kind
                    elif scan.audio_pid is None and kind in _AUDIO_TYPES:
                        scan.audio_pid = es_pid
                    k += 5 + (((section[k + 3] & 0x0F) << 8) | section[k + 4])
        elif pid == scan.video_pid or pid == scan.audio_pid:
            if pusi:
                flush(pid)
                if data[start : start + 3] == b"\x00\x00\x01" and start + 9 <= end:
                    has_pts = data[start + 7] & 0x80 and start + 14 <= end
                    pes = reading[pid] = _Assembly(
                        i, _timestamp(data, start + 9) if has_pts else None, rai
                    )
                    payload = start + 9 + data[start + 8]
                    if payload < end:
                        pes.add(view[payload:end])
            elif (pes_read := reading.get(pid)) is not None:
                pes_read.add(view[start:end])
        i = end
    for pid in list(reading):
        flush(pid)
    return scan


@dataclass
class Restart:
    """A video timestamp restart: the scan's video PES `first` starts a new run."""

    first: int
    junction: int  # where the new run begins: its first flagged packet, else that PES
    before_s: float  # the last PTS before it, in seconds
    after_s: float  # the new run's first PTS


def find_restarts(scan: Scan) -> list[Restart]:
    """Video PTS steps back by more than `_REORDER_S`, or forward by `JUMP_S` or more."""
    restarts = []
    previous: Optional[int] = None
    for index, pes in enumerate(scan.video):
        if pes.pts is None:
            continue
        if previous is not None:
            step = pts_delta(previous, pes.pts) / CLOCK
            if step < -_REORDER_S or step >= JUMP_S:
                floor = max(scan.video[index - 1].offset, pes.offset - _FLAG_WINDOW * PACKET)
                k = bisect.bisect_right(scan.flagged, floor)
                junction = scan.flagged[k] if k < len(scan.flagged) else pes.offset
                restarts.append(
                    Restart(index, min(junction, pes.offset), previous / CLOCK, pes.pts / CLOCK)
                )
        previous = pes.pts
    return restarts


def frame_seconds(scan: Scan) -> float:
    """The video's frame duration: the median small forward PTS step (0.04 s without one)."""
    steps = []
    previous = None
    for pes in scan.video[:4000]:
        if pes.pts is None:
            continue
        if previous is not None and 0 < (step := pts_delta(previous, pes.pts) / CLOCK) < 0.2:
            steps.append(step)
        previous = pes.pts
    return statistics.median(steps) if steps else 0.04


class Media:
    """The video media an input brought over time: per video PES, the seconds received by
    then (each run's furthest PTS less its first, after the runs before it, each a frame longer
    than that) and when its first byte arrived."""

    def __init__(self, capture: Capture, scan: Scan, restarts: list[Restart], frame: float):
        starts = {restart.first for restart in restarts}
        self.positions: list[float] = []
        done = 0.0
        first = top = None
        for index, pes in enumerate(scan.video):
            if index in starts and first is not None and top is not None:
                done += pts_delta(first, top) / CLOCK + frame
                first = top = None
            if pes.pts is not None:
                if first is None or top is None:
                    first = top = pes.pts
                elif pts_delta(top, pes.pts) > 0:
                    top = pes.pts
            span = pts_delta(first, top) / CLOCK if first is not None and top is not None else 0.0
            self.positions.append(done + span)
        self.arrivals = [capture.arrival(pes.offset) for pes in scan.video]

    def at(self, t: float) -> Optional[float]:
        """The media received by time `t` (both lists only grow, in input order)."""
        k = bisect.bisect_right(self.arrivals, t)
        return self.positions[k - 1] if k else None

    def ahead(self, t: float) -> Optional[float]:
        """Media received minus wall time."""
        media = self.at(t)
        return None if media is None else media - t


# ---- the replay ----------------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _ReplaySource:
    """`services.live.source.Source` over a capture: each read's bytes at its arrival time."""

    hls = False
    burst_s = 0.0

    def __init__(self, capture: Capture, clock: _Clock) -> None:
        self.user_agent = DEFAULT_USER_AGENT
        self._capture = capture
        self._clock = clock

    def chunks(self) -> Iterator[bytes | source_module.Discontinuity]:
        data, done = self._capture.data, 0
        reconnects = set(self._capture.reconnects)
        for end, at in self._capture.reads:
            self._clock.now = at
            if end > done:
                yield data[done:end]
                done = end
            if end in reconnects:
                reconnects.discard(end)
                yield source_module.Discontinuity()

    def close(self) -> None:
        pass

    def wait_closed(self, timeout: float) -> bool:
        return True


class _ObservedSegmenter(Segmenter):
    """The helper's segmenter, noting where each cut and each passed-on byte came from (its
    input offsets: the trimmer's output)."""

    def __init__(self, clock: _Clock) -> None:
        super().__init__(clock=clock)
        self.passed = 0  # bytes passed on so far: the stream VLC reads, without headers
        # (stream offset, input offset, length) of each run of bytes passed on
        self.slices: list[tuple[int, int, int]] = []
        self.cuts: list[tuple[int, bool, int]] = []  # (seq, discontinuity, stream offset)
        self._old: Optional[tuple[bytes, int]] = None  # what a discontinuity flushed from

    def discontinuity(self) -> None:
        self._old = (self._buf, self._pos)  # its last packet comes with the next events
        super().discontinuity()

    def feed(self, chunk: bytes) -> list[Event]:
        held = (self._buf + chunk if self._buf else chunk, self._pos)
        return self._note(super().feed(chunk), held)

    def finish(self) -> list[Event]:
        held = (self._buf, self._pos)
        return self._note(super().finish(), held)

    def _note(self, events: list[Event], held: tuple[bytes, int]) -> list[Event]:
        """Each Data is a slice of the bytes held before (then flushed) or of these ones: find
        where. One with a PCR flagged here is not found byte for byte: taken as following on."""
        sources = [held] if self._old is None else [self._old, held]
        self._old = None
        source, cursor = 0, 0
        for event in events:
            if isinstance(event, Data):
                size = len(event.payload)
                at = sources[source][0].find(event.payload, cursor)
                while at < 0 and source + 1 < len(sources):
                    source, cursor = source + 1, 0
                    at = sources[source][0].find(event.payload, cursor)
                if at < 0:
                    at = cursor
                self.slices.append((self.passed, sources[source][1] + at, size))
                self.passed += size
                cursor = at + size
            elif isinstance(event, Opened):
                self.cuts.append((event.seq, event.discontinuity, self.passed))
        return events

    def to_input(self, stream_offset: int) -> Optional[int]:
        """The input offset of a byte passed on (stream offsets exclude the headers)."""
        k = bisect.bisect_right(self.slices, (stream_offset, 1 << 62, 0)) - 1
        if k < 0:
            return None
        start, origin, length = self.slices[k]
        return origin + stream_offset - start if stream_offset < start + length else None

    def to_stream(self, input_offset: int) -> Optional[int]:
        """The stream offset of the first byte passed on at or after `input_offset`."""
        for start, origin, length in self.slices:
            if origin + length > input_offset:
                return start + max(0, input_offset - origin)
        return None

    def passed_whole(self, input_offset: int, length: int) -> bool:
        """Whether input bytes [offset, offset + length) were passed on in one piece."""
        for _, origin, size in self.slices:
            if origin <= input_offset and input_offset + length <= origin + size:
                return True
            if origin > input_offset:
                return False
        return False


class Positions:
    """Where the stream VLC reads came from in the capture: through the reconnect trimmer
    (which left out `Rejoin.dropped` ranges), the repeat trimmer (`Trim.dropped` ranges and the
    packets cut short, in the reconnect trimmer's output) and the segmenter
    (`_ObservedSegmenter.slices`)."""

    def __init__(
        self,
        segmenter: _ObservedSegmenter,
        trims: list[Trim],
        cut: list[tuple[int, int]],
        rejoined: Optional[list[tuple[int, int]]] = None,
    ) -> None:
        self._seg = segmenter
        self._dropped = sorted([item for trim in trims for item in trim.dropped] + cut)
        self._rejoined = sorted(rejoined or [])

    @staticmethod
    def _up(out: int, dropped: list[tuple[int, int]]) -> int:
        for start, size in dropped:
            if start > out:
                break
            out += size
        return out

    @staticmethod
    def _down(offset: int, dropped: list[tuple[int, int]]) -> Optional[int]:
        shift = 0
        for start, size in dropped:
            if start + size <= offset:
                shift += size
            elif start <= offset:
                return None
            else:
                break
        return offset - shift

    def _capture(self, out: int) -> int:
        """The capture offset of the trimmer's output byte `out`."""
        return self._up(self._up(out, self._dropped), self._rejoined)

    def _out(self, offset: int) -> Optional[int]:
        """The trimmer's output offset of capture byte `offset` (None: it was dropped)."""
        mid = self._down(offset, self._rejoined)
        return None if mid is None else self._down(mid, self._dropped)

    def dropped(self, offset: int, length: int) -> bool:
        if any(s < offset + length and offset < s + n for s, n in self._rejoined):
            return True
        mid = self._down(offset, self._rejoined)
        return mid is not None and any(s < mid + length and mid < s + n for s, n in self._dropped)

    def to_input(self, stream_offset: int) -> Optional[int]:
        """The capture offset of a byte VLC reads (stream offsets exclude the headers)."""
        out = self._seg.to_input(stream_offset)
        return None if out is None else self._capture(out)

    def to_stream(self, offset: int) -> Optional[int]:
        """The stream offset of the first byte passed on at or after capture byte `offset`."""
        while self._out(offset) is None:
            offset = self._next_kept(offset)
        out = self._out(offset)
        return None if out is None else self._seg.to_stream(out)

    def _next_kept(self, offset: int) -> int:
        """Past the dropped range that holds capture byte `offset`."""
        for start, size in self._rejoined:
            if start <= offset < start + size:
                return start + size
        mid = self._down(offset, self._rejoined)
        for start, size in self._dropped:
            if mid is not None and start <= mid < start + size:
                return offset + start + size - mid
        return offset + 1

    def passed_whole(self, offset: int, length: int) -> bool:
        """Whether capture bytes [offset, offset + length) reached VLC in one piece."""
        out = self._out(offset)
        if out is None or self.dropped(offset, length):
            return False
        return self._seg.passed_whole(out, length)


class _Unrejoined:
    """`ReconnectTrimmer` as if there were none (--stages none): everything passes untouched."""

    def __init__(self, **_: Any) -> None:
        self.rejoins: list[Rejoin] = []

    def feed(self, chunk: bytes) -> bytes:
        return chunk

    def reconnect(self) -> bytes:
        return b""

    def finish(self) -> bytes:
        return b""


class _Untrimmed:
    """`RepeatTrimmer` as if there were none (--stages none): everything passes on untouched."""

    def __init__(self, **_: Any) -> None:
        self.trims: list[Trim] = []
        self.cut_short: list[tuple[int, int]] = []

    def feed(self, chunk: bytes) -> list[bytes]:
        return [chunk]

    def discontinuity(self) -> list[bytes]:
        return [b""]

    def finish(self) -> list[bytes]:
        return [b""]


class _RecordingMeter(worker.BurstMeter):
    """The session's burst meter, keeping what it is fed after every read: the time and the
    live edge (the pipeline's own, before `_notify` throttles what the GUI hears)."""

    def __init__(self) -> None:
        super().__init__()
        self.edges: list[tuple[float, float]] = []

    def add(self, wall: float, media: float) -> None:
        self.edges.append((wall, media))
        super().add(wall, media)


class _Server:
    def __init__(self) -> None:
        self.ring: Any = None

    def add_session(self, sid: str, ring: Any, is_ended: Any) -> None:
        self.ring = ring

    def remove_session(self, sid: str) -> None:
        pass


class _Helper:
    """What `_Session` needs of `worker._Helper`: a root, a server and a send (recorded)."""

    def __init__(self, clock: _Clock, root: str) -> None:
        self.root = root
        self.server = _Server()
        self.messages: list[tuple[float, tuple[Any, ...]]] = []
        self._clock = clock

    def send(self, message: tuple[Any, ...], session: Any = None) -> None:
        self.messages.append((self._clock.now, message))


@dataclass
class Replay:
    segmenter: _ObservedSegmenter
    positions: Positions
    trims: list[Trim]
    spans: list[Span]
    stream: bytes  # what VLC reads from the oldest segment: its header, then every body
    header_len: int
    messages: list[tuple[float, tuple[Any, ...]]]  # (sim time, message) the GUI heard
    edges: list[tuple[float, float]]  # (sim time, live edge) after every read
    junctions: list[tuple[float, float]]  # the clock joiner's (step before, step after) in s
    seconds: float  # how long the replay took
    rejoins: list[Rejoin] = field(default_factory=list)  # reconnects found in the old timebase


def replay(capture: Capture, stages: str = "all") -> Replay:
    """Run the capture through `_Session._ingest` on a simulated clock (`stages` "trim": as if
    there were no `ClockJoiner`, "none": no `ReconnectTrimmer` and `RepeatTrimmer` either)."""
    clock = _Clock()
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="qitv-replay-") as root:
        helper = _Helper(clock, root)
        ring = {"kind": "ram", "max_bytes": 2 * len(capture.data) + (16 << 20)}  # no eviction
        session = worker._Session(
            helper, "replay", "replay://capture", {}, True, ring, 6.0  # type: ignore[arg-type]
        )
        segmenter = session._segmenter = _ObservedSegmenter(clock)
        meter = session._bursts = _RecordingMeter()
        trimmer = worker.RepeatTrimmer if stages != "none" else _Untrimmed
        rejoiner = worker.ReconnectTrimmer if stages != "none" else _Unrejoined
        clock_joiner = worker.ClockJoiner if stages == "all" else (lambda **_: None)
        with (
            mock.patch.object(worker, "time", SimpleNamespace(monotonic=clock)),
            mock.patch.object(worker, "RepeatTrimmer", trimmer),
            mock.patch.object(worker, "ReconnectTrimmer", rejoiner),
            mock.patch.object(worker, "ClockJoiner", clock_joiner),
            mock.patch.object(
                source_module, "open_source", lambda *_, **__: _ReplaySource(capture, clock)
            ),
        ):
            session._ingest()
        trimmer_used = session._trimmer
        joined = session._clock_joiner
        junctions = list(joined.junctions) if joined is not None else []
        trims = list(trimmer_used.trims) if trimmer_used is not None else []
        rejoiner_used = session._rejoiner
        rejoins = list(rejoiner_used.rejoins) if rejoiner_used is not None else []
        cut = list(trimmer_used.cut_short) if trimmer_used is not None else []
        kept = helper.server.ring
        parts: list[bytes] = []
        header_len = 0
        if kept is not None and kept.oldest_seq is not None:
            for seq in range(kept.oldest_seq, kept.newest_seq + 1):
                read = kept.read_at(seq, 0, 1 << 40)
                if read is not None:
                    body, _, header = read
                    header_len = header_len or header
                    parts.append(body if not parts else body[header:])
            kept.close()
    return Replay(
        segmenter,
        Positions(segmenter, trims, cut, [item for r in rejoins for item in r.dropped]),
        trims,
        session._timeline.spans(),
        b"".join(parts),
        header_len,
        helper.messages,
        meter.edges,
        junctions,
        time.monotonic() - started,
        rejoins,
    )


# ---- metrics -------------------------------------------------------------------------------


class Heard:
    """The states the GUI heard, by time."""

    def __init__(self, messages: list[tuple[float, tuple[Any, ...]]]) -> None:
        self.states = [(at, m[2]) for at, m in messages if m[0] == "state"]
        self._stamps = [at for at, _ in self.states]

    def at(self, t: float) -> Optional[dict[str, Any]]:
        k = bisect.bisect_right(self._stamps, t) - 1
        if k < 0:
            return None
        state = self.states[k][1]
        playable = state.get("oldest") is not None
        return state if playable and isinstance(state.get("live_edge"), (int, float)) else None

    def ahead(self, t: float) -> Optional[float]:
        """The live edge minus wall time, as last heard at `t`."""
        state = self.at(t)
        return None if state is None else float(state["live_edge"]) - t


class Own:
    """The pipeline's own live edge after every read (`Replay.edges`), by time."""

    def __init__(self, edges: list[tuple[float, float]]) -> None:
        self._stamps = [at for at, _ in edges]
        self._edges = [edge for _, edge in edges]

    def at(self, t: float) -> Optional[float]:
        k = bisect.bisect_right(self._stamps, t) - 1
        return self._edges[k] if k >= 0 else None

    def ahead(self, t: float) -> Optional[float]:
        edge = self.at(t)
        return None if edge is None or edge <= 0.0 else edge - t


def edge_curve(capture: Capture, heard: Heard, own: Own) -> list[dict[str, Any]]:
    """Each second since connect: what the latest state the GUI heard by then said (and the
    pipeline's own edge then)."""
    samples = []
    for second in range(int(capture.seconds) + 1):
        state = heard.at(float(second))
        if state is None:
            continue
        samples.append(
            {
                "t": second,
                "live_edge": round(state["live_edge"], 3),
                "edge_minus_wall": round(state["live_edge"] - second, 3),
                "own_edge": _round(own.at(float(second)), 3),
                "burst_s": round(float(state.get("burst_s") or 0.0), 3),
                "backlog_s": state.get("backlog_s"),
                "breaks": len(state.get("breaks") or ()),
            }
        )
    return samples


def _slope_per_min(points: list[tuple[float, float]]) -> Optional[float]:
    if len(points) < 3:
        return None
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    return 60.0 * sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den if den else None


def _settled(ahead: Any, start: float, stop: float) -> Optional[float]:
    """The median of `ahead` over [start, stop]: past the dip of a pause after a burst and the
    catching up after it."""
    values = [ahead(start + k * _STEP_S) for k in range(int((stop - start) / _STEP_S) + 1)]
    known = [v for v in values if v is not None]
    return statistics.median(known) if known else None


def _pcr_steps(scan: Scan) -> list[float]:
    """PCR steps on the first PCR PID back by more than 0.1 s or on by more than 1 s."""
    steps = []
    pcrs = scan.pcrs
    for (_, a, _), (_, b, _) in zip(pcrs, pcrs[1:]):
        step = pts_delta(a, b) / CLOCK
        if step < -0.1 or step > 1.0:
            steps.append(round(step, 2))
    return steps


def _round(value: Optional[float], digits: int = 2) -> Optional[float]:
    return None if value is None else round(value, digits)


def _exact_repeat(scan: Scan, media: Media, first: int, end: int, frame: float) -> dict:
    """The byte-exact repeat at the start of the run [first, end): the first frame within
    `_REPEAT_SEARCH_S` that repeats an earlier one, and how many follow it in the same order."""
    video = scan.video
    earliest: dict[bytes, int] = {}
    for index in range(first):
        earliest.setdefault(video[index].digest, index)
    total = sum(1 for pes in video[first:end] if pes.digest in earliest)
    result: dict[str, Any] = {"lead_s": 0.0, "run_repeated_s": round(total * frame, 2)}
    for m in range(first, min(end, first + int(_REPEAT_SEARCH_S / frame) + 1)):
        j = earliest.get(video[m].digest)
        if j is None:
            continue
        n = 0
        while m + n < end and j + n < first and video[m + n].digest == video[j + n].digest:
            n += 1
        result.update(
            lead_s=round(n * frame, 2),
            starts_after_s=round((m - first) * frame, 2),
            sent_earlier_s=round(media.arrivals[m] - media.arrivals[j], 1),
            media_back_s=round(media.positions[m] - media.positions[j], 1),
        )
        break
    return result


def _restart_row(
    capture: Capture,
    source: Scan,
    media: Media,
    own: Own,
    rep: Replay,
    stream_scan: Scan,
    restarts: list[Restart],
    number: int,
    frame: float,
) -> dict[str, Any]:
    restart = restarts[number]
    following = restarts[number + 1] if number + 1 < len(restarts) else None
    end = following.first if following else len(source.video)
    at = capture.arrival(restart.junction)
    limit = capture.arrival(following.junction) if following else capture.seconds
    # Before the silence: where the old run last brought media.
    k = restart.first - 1
    while k > 0 and media.positions[k - 1] >= media.positions[restart.first - 1]:
        k -= 1
    quiet_from = media.arrivals[k] if k >= 0 else at
    before = media.ahead(quiet_from)
    # The burst: until a second brings at most _BURST_RATE seconds of media.
    t = at + _STEP_S
    while t + 1.0 <= min(limit, at + _BURST_MAX_S):
        now, later = media.at(t), media.at(t + 1.0)
        if now is not None and later is not None and later - now <= _BURST_RATE:
            break
        t += _STEP_S
    settle = (t, max(t, min(t + _SETTLE_S, limit - _STEP_S)))
    burst_ahead, settled = media.ahead(t), _settled(media.ahead, *settle)
    r = None if before is None or settled is None else settled - before
    own_before, own_settled = own.ahead(quiet_from), _settled(own.ahead, *settle)
    first_pts = source.video[restart.first].pts
    keys: list[float] = []
    for pes in source.video[restart.first : end]:
        if pes.start in (START_KEY, START_OPEN) and pes.pts is not None and first_pts is not None:
            offset_s = pts_delta(first_pts, pes.pts) / CLOCK
            if offset_s > _BURST_MAX_S:
                break
            keys.append(round(offset_s, 2))
    d = max([key for key in keys if r is not None and key <= r] or [0.0])
    pcr = bisect.bisect_left(source.pcrs, (restart.junction, -1, False))
    # A packet cut short since the old run's last PES began: where the provider cut it off.
    old_last = source.video[restart.first - 1].offset
    cut_short = [(o, b) for o, b in source.cut_short if old_last <= o < restart.junction]
    return {
        "wall_s": round(at, 2),
        "pts": f"{restart.before_s:.2f}->{restart.after_s:.2f}",
        "provider_indicator": source.pcrs[pcr][2] if pcr < len(source.pcrs) else None,
        "cut_short_bytes": [b for _, b in cut_short],
        "silence_s": round(at - quiet_from, 2),
        "ahead_before": _round(before),
        "burst_s": round(t - at, 2),
        "ahead_after_burst": _round(burst_ahead),
        "r_burst_s": _round(
            None if before is None or burst_ahead is None else burst_ahead - before
        ),
        "r_s": _round(r),
        "r_edge_s": _round(
            None if own_before is None or own_settled is None else own_settled - own_before
        ),
        "new_run_keys_s": keys[:8],
        "d_s": round(d, 2),
        "exact_repeat": _exact_repeat(source, media, restart.first, end, frame),
        "pipeline": _taken(rep, source, restart, following),
        "ring": _junction(rep, stream_scan, restart, cut_short),
        "trim": _trim_of(rep, source.video[restart.first].offset),
    }


def _trim_of(rep: Replay, offset: int) -> Optional[dict[str, Any]]:
    """What the trimmer did with the run starting at input `offset` (its first video PES)."""
    near = [trim for trim in rep.trims if abs(trim.offset - offset) <= _FLAG_WINDOW * PACKET]
    if not near:
        return None
    trim = near[0]
    return {
        "reason": trim.reason,
        "held_s": round(trim.released_at - trim.held_at, 2),
        "excess_s": _round(trim.excess_s),
        "dropped_s": round(trim.dropped_s, 2),
        "dropped_bytes": trim.dropped_bytes,
    }


def _taken(rep: Replay, source: Scan, restart: Restart, following: Optional[Restart]) -> dict:
    """How the pipeline took a restart: the first discontinuity cut in the new run."""
    seg = rep.segmenter
    spans = {span.seq: span for span in rep.spans}
    stop = following.junction if following else 1 << 62
    for seq, discontinuity, offset in seg.cuts:
        origin = rep.positions.to_input(offset)
        if origin is None or not restart.junction <= origin < stop:
            continue
        span = spans.get(seq)
        if discontinuity or (span is not None and span.discontinuity):
            k = bisect.bisect_left(source.video, origin, key=lambda pes: pes.offset)
            pts = source.video[min(k, len(source.video) - 1)].pts
            first_pts = source.video[restart.first].pts
            into = None
            if pts is not None and first_pts is not None:
                into = round(pts_delta(first_pts, pts) / CLOCK, 2)
            return {
                "how": "segmenter discontinuity" if discontinuity else "timeline only",
                "seq": seq,
                "cut_into_run_s": into,
                "start": span.start_quality if span else None,
            }
    return {"how": "nothing"}


def _junction(rep: Replay, stream_scan: Scan, restart: Restart, cut_short: list) -> dict:
    """The junction as VLC reads it from the ring."""
    seg = rep.segmenter
    at = rep.positions.to_stream(restart.junction)
    result: dict[str, Any] = {
        "seq": None,
        "cut_short_passed_on": [b for o, b in cut_short if rep.positions.passed_whole(o, PACKET)],
        "indicator_reaches_vlc": None,
        "cc_breaks_unannounced": 0,
    }
    if at is None:
        return result
    k = bisect.bisect_right([offset for _, _, offset in seg.cuts], at) - 1
    result["seq"] = seg.cuts[k][0] if k >= 0 else None
    at += rep.header_len
    pcr = bisect.bisect_left(stream_scan.pcrs, (at, -1, False))
    if pcr < len(stream_scan.pcrs):
        result["indicator_reaches_vlc"] = stream_scan.pcrs[pcr][2]
    reach = _FLAG_WINDOW * PACKET
    result["cc_breaks_unannounced"] = sum(
        1 for o, _ in stream_scan.cc_breaks if at - reach <= o <= at + reach
    )
    return result


def _ffmpeg() -> Optional[str]:
    found = shutil.which("ffmpeg")
    return found or ("/opt/local/bin/ffmpeg" if os.path.exists("/opt/local/bin/ffmpeg") else None)


def _decode_errors(ffmpeg: str, data: bytes) -> int:
    with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as handle:
        handle.write(data)
        name = handle.name
    try:
        proc = subprocess.run(
            [ffmpeg, "-v", "error", "-nostdin", "-i", name, "-map", "0:v:0", "-f", "null", "-"],
            capture_output=True,
            text=True,
            timeout=300,
        )
        # The video decoder's own lines; not the null muxer's complaints about the timestamps
        # going back (every restart) nor the audio probe's.
        return sum(1 for line in proc.stderr.splitlines() if line.startswith(_DECODERS))
    finally:
        os.unlink(name)


def _decode(rep: Replay, junctions: set[int]) -> dict[str, Any]:
    """ffmpeg's error lines on the ring's segments at each junction (the segment holding it and
    the next, behind the first segment's header) and on a few pairs away from them."""
    ffmpeg = _ffmpeg()
    if ffmpeg is None:
        return {"error": "ffmpeg not found"}
    seqs = [seq for seq, _, _ in rep.segmenter.cuts]
    starts = [offset + rep.header_len for _, _, offset in rep.segmenter.cuts]

    def pair(seq: int) -> bytes:
        k = seqs.index(seq)
        stop = starts[k + 2] if k + 2 < len(starts) else len(rep.stream)
        return rep.stream[: rep.header_len] + rep.stream[starts[k] : stop]

    away = [s for s in seqs[1:-2] if all(abs(s - j) > 2 for j in junctions)]
    baseline = away[:: max(1, len(away) // _DECODE_BASELINE)][:_DECODE_BASELINE]
    return {
        "junctions": {seq: _decode_errors(ffmpeg, pair(seq)) for seq in sorted(junctions)},
        "baseline": {seq: _decode_errors(ffmpeg, pair(seq)) for seq in baseline},
    }


def measure(
    capture: Capture, decode: bool = False, dump: Optional[Path] = None, stages: str = "all"
) -> dict[str, Any]:
    """Replay one capture and measure it (see the module docstring); `dump` receives the
    stream VLC reads from the oldest segment; `stages` as for `replay`."""
    rep = replay(capture, stages)
    if dump is not None:
        dump.write_bytes(rep.stream)
    source = scan_ts(capture.data)
    frame = frame_seconds(source)
    restarts = find_restarts(source)
    media = Media(capture, source, restarts, frame)
    own = Own(rep.edges)
    stream_scan = scan_ts(rep.stream, rep.header_len)
    rows = [
        _restart_row(capture, source, media, own, rep, stream_scan, restarts, n, frame)
        for n in range(len(restarts))
    ]
    curve = edge_curve(capture, Heard(rep.messages), own)
    # What the GUI heard trails the pipeline's own edge between two throttled states.
    behind = [s["own_edge"] - s["live_edge"] for s in curve if s["own_edge"] is not None]
    late = [(s["t"], s["edge_minus_wall"]) for s in curve if s["t"] >= _BACKLOG_S]
    drops = [(row["wall_s"], row["d_s"]) for row in rows]
    without_d = [(t, e - sum(d for w, d in drops if w <= t)) for t, e in late]
    whole = [(row["wall_s"], max(row["r_s"] or 0.0, 0.0)) for row in rows]
    without_r = [(t, e - sum(r for w, r in whole if w <= t)) for t, e in late]
    bursts: list[tuple[int, float]] = []
    for sample in curve:
        if not bursts or bursts[-1][1] != sample["burst_s"]:
            bursts.append((sample["t"], sample["burst_s"]))
    seen: set[bytes] = set()
    repeated = 0
    for pes in stream_scan.video:
        repeated += pes.digest in seen
        seen.add(pes.digest)
    seen.clear()
    input_repeated = 0
    for pes in source.video:
        input_repeated += pes.digest in seen
        seen.add(pes.digest)
    slices = rep.segmenter.slices
    result: dict[str, Any] = {
        "capture": capture.name,
        "wall_s": round(capture.seconds, 2),
        "bytes": len(capture.data),
        "replay_s": round(rep.seconds, 2),
        "video_frames": len(source.video),
        "frame_s": round(frame, 4),
        "input": {
            "media_s": round(media.positions[-1] + frame, 1) if media.positions else 0.0,
            "restarts": len(restarts),
            "cut_short": len(source.cut_short),
            "bytes_out_of_sync": sum(b for _, b in source.skipped),
            "frames_repeated": input_repeated,
        },
        "pipeline": {
            "ready_s": next((round(at, 2) for at, m in rep.messages if m[0] == "ready"), None),
            "segments": len(rep.spans),
            "discontinuities": sum(1 for span in rep.spans if span.discontinuity),
            "timeline_s": round(rep.spans[-1].end - rep.spans[0].start, 1) if rep.spans else 0.0,
            "errors": [list(m[2:4]) for _, m in rep.messages if m[0] == "error"],
        },
        "ring": {
            "bytes": len(rep.stream),
            "frames_repeated": repeated,
            "repeated_s": round(repeated * frame, 1),
            "input_bytes_left_out": sum(
                b - (a + n) for (_, a, n), (_, b, _) in zip(slices, slices[1:]) if b > a + n
            ),
            "cc_breaks_unannounced": len(stream_scan.cc_breaks),
            "cc_break_pids": sorted({f"{pid:#x}" for _, pid in stream_scan.cc_breaks}),
            "pcrs_flagged": sum(1 for _, _, flag in stream_scan.pcrs if flag),
            # The clock as VLC reads it: video timestamp restarts, and PCR steps back or
            # forward by more than a second.
            "restarts": len(find_restarts(stream_scan)),
            "pcr_steps": _pcr_steps(stream_scan),
            "junctions_rebased": [(round(a, 2), round(b, 2)) for a, b in rep.junctions],
        },
        "edge": {
            "ahead_at_backlog": next((e for t, e in late[:1]), None),
            "ahead_at_end": curve[-1]["edge_minus_wall"] if curve else None,
            "drift_s_per_min": _round(_slope_per_min(late)),
            "drift_without_d_s_per_min": _round(_slope_per_min(without_d)),
            "drift_without_r_s_per_min": _round(_slope_per_min(without_r)),
            "without_d_at_end": _round(without_d[-1][1]) if without_d else None,
            "heard_behind_max_s": _round(max(behind, default=0.0)),
            "heard_behind_1s_samples": sum(1 for b in behind if b > 1.0),
            "burst_s_heard": bursts,
            "backlog_s_heard": sorted({str(s["backlog_s"]) for s in curve}),
            "samples": curve,
        },
        "trims": {
            "held": len(rep.trims),
            "dropped": sum(1 for trim in rep.trims if trim.reason == "dropped"),
            "dropped_s": round(sum(trim.dropped_s for trim in rep.trims), 1),
            "dropped_bytes": sum(trim.dropped_bytes for trim in rep.trims),
            "held_s": [round(trim.released_at - trim.held_at, 2) for trim in rep.trims],
            "reasons": [trim.reason for trim in rep.trims],
        },
        "reconnects": {
            "count": len(capture.reconnects),
            "rejoined": [
                {"step_s": round(r.step_s, 2), "dropped_bytes": r.dropped_bytes}
                for r in rep.rejoins
            ],
        },
        "restarts": rows,
    }
    if decode:
        junctions = {row["ring"]["seq"] for row in rows if row["ring"]["seq"] is not None}
        result["decode"] = _decode(rep, junctions)
    return result


# ---- the report ----------------------------------------------------------------------------


def _fmt(value: Any, spec: str = ".1f") -> str:
    return "-" if value is None else format(value, spec)


def _row_line(row: dict[str, Any]) -> str:
    repeat, taken, ring = row["exact_repeat"], row["pipeline"], row["ring"]
    exact = "-"
    if repeat["lead_s"]:
        exact = f"{repeat['lead_s']:.1f} (+{repeat['starts_after_s']:.1f})"
        exact += f" back {_fmt(repeat['media_back_s'])}"
    how = taken["how"]
    if "seq" in taken:
        how = "disc." if how.startswith("seg") else "tl"
        how += f" seg {taken['seq']} +{_fmt(taken['cut_into_run_s'], '.2f')} {taken['start']}"
    junction = "short pkt passed" if ring["cut_short_passed_on"] else "in sync"
    junction += ", DI to VLC" if ring["indicator_reaches_vlc"] else ", no DI"
    if ring["cc_breaks_unannounced"]:
        junction += f", {ring['cc_breaks_unannounced']} cc"
    keys = ",".join(f"{k:g}" for k in row["new_run_keys_s"][:4])
    short = ",".join(map(str, row["cut_short_bytes"])) or "-"
    flag = {True: "yes", False: "no", None: "-"}[row["provider_indicator"]]
    trim = row["trim"]
    trimmed = (
        "-"
        if trim is None
        else (
            f"{trim['reason']} R {_fmt(trim['excess_s'])}, {trim['dropped_s']:.1f} s dropped,"
            f" held {trim['held_s']:.2f} s"
        )
    )
    return (
        f"  {row['wall_s']:6.1f} {row['pts']:<13} {flag:<3} {short:<5} {row['silence_s']:5.2f}"
        f" {row['burst_s']:5.2f} {_fmt(row['r_burst_s']):>5} {_fmt(row['r_s']):>5}"
        f" {_fmt(row['r_edge_s']):>5}  {keys:<16} {row['d_s']:4.1f}  {exact:<20} {how:<22}"
        f" {junction:<22} {trimmed}"
    )  # fmt: skip


def report(result: dict[str, Any]) -> str:
    edge, pipe, ring, source = result["edge"], result["pipeline"], result["ring"], result["input"]
    lines = [
        f"== {result['capture']}: {result['wall_s']:.1f} s, {result['bytes'] / 1e6:.1f} MB, "
        f"{result['video_frames']} video frames of {result['frame_s'] * 1000:.0f} ms, "
        f"replayed in {result['replay_s']:.1f} s",
        f"input: {source['media_s']:.1f} s of video in {result['wall_s']:.1f} s; "
        f"{source['restarts']} timestamp restarts; {source['cut_short']} packets cut short; "
        f"{source['frames_repeated']} frames repeat an earlier one byte for byte",
        f"pipeline: ready at {_fmt(pipe['ready_s'], '.2f')} s; {pipe['segments']} segments, "
        f"{pipe['discontinuities']} marked discontinuous; timeline {pipe['timeline_s']:.1f} s"
        + (f"; errors {pipe['errors']}" if pipe["errors"] else ""),
        "live edge minus wall, every 10 s: "
        + " ".join(f"{s['edge_minus_wall']:.1f}" for s in edge["samples"] if s["t"] % 10 == 0),
        f"  {_fmt(edge['ahead_at_backlog'])} at {_BACKLOG_S:.0f} s, {_fmt(edge['ahead_at_end'])} "
        f"at the end: drift {_fmt(edge['drift_s_per_min'], '+.2f')} s/min; without D at each "
        f"restart {_fmt(edge['drift_without_d_s_per_min'], '+.2f')} s/min "
        f"({_fmt(edge['without_d_at_end'])} at the end), without all of R "
        f"{_fmt(edge['drift_without_r_s_per_min'], '+.2f')} s/min",
        f"  heard edge behind the pipeline's by up to {_fmt(edge['heard_behind_max_s'])} s "
        f"({edge['heard_behind_1s_samples']} seconds over 1 s); burst_s heard: "
        + ", ".join(f"{b:.1f} from {t} s" for t, b in edge["burst_s_heard"])
        + f"; backlog_s heard: {', '.join(edge['backlog_s_heard'])}",
        f"ring: {ring['frames_repeated']} video frames ({ring['repeated_s']:.1f} s) repeat an "
        f"earlier one byte for byte; {ring['input_bytes_left_out']} input bytes left out; "
        f"{ring['cc_breaks_unannounced']} counter breaks without an indicator "
        f"{ring['cc_break_pids'] or ''}; {ring['pcrs_flagged']} PCRs flagged discontinuous",
        f"  clock in the ring: {ring['restarts']} timestamp restarts, PCR steps "
        f"{ring['pcr_steps'] or 'none'}; re-based junctions (step before, after): "
        f"{ring['junctions_rebased'] or 'none'}",
    ]
    trims = result["trims"]
    if trims["held"]:
        lines.append(
            f"trimmer: held {trims['held']} new runs ({', '.join(map(str, trims['held_s']))} s),"
            f" dropped {trims['dropped']}: {trims['dropped_s']:.1f} s, "
            f"{trims['dropped_bytes']} bytes"
        )
    reconnects = result["reconnects"]
    if reconnects["count"]:
        cuts = ", ".join(
            f"{r['step_s']:+.2f} s ({r['dropped_bytes']} bytes)" for r in reconnects["rejoined"]
        )
        lines.append(
            f"reconnects: {reconnects['count']}; in the old timebase, cut: {cuts or 'none'}"
        )
    if result["restarts"]:
        lines.append(
            "  wall   pts           DI  short silen burst R@bst     R R@edg  keys of new run  "
            "   D  exact repeat         pipeline               ring junction          trimmer"
        )
        lines.extend(_row_line(row) for row in result["restarts"])
    decode = result.get("decode")
    if decode:
        if "error" in decode:
            lines.append(f"decode: {decode['error']}")
        else:
            lines.append(
                "decode errors (ffmpeg lines), junction segments: "
                + (", ".join(f"seg {k}: {v}" for k, v in decode["junctions"].items()) or "none")
                + "; away from them: "
                + (", ".join(f"seg {k}: {v}" for k, v in decode["baseline"].items()) or "none")
            )
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("captures", nargs="+", help="<name>.ts, <name>.times or <name>")
    parser.add_argument("--json", help="write every capture's metrics to this file")
    parser.add_argument(
        "--decode", action="store_true", help="count ffmpeg's error lines around each junction"
    )
    parser.add_argument(
        "--dump",
        metavar="DIR",
        help="write <DIR>/<name>.ring.ts, the stream VLC reads from the oldest segment (it holds "
        "the provider's stream: keep it out of the repository)",
    )
    parser.add_argument(
        "--stages",
        choices=("none", "trim", "all"),
        default="all",
        help="all (the default), trim (no ClockJoiner), none (no RepeatTrimmer either)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    results = []
    for path in args.captures:
        capture = load_capture(path)
        dump = Path(args.dump) / f"{capture.name}.ring.ts" if args.dump else None
        result = measure(capture, decode=args.decode, dump=dump, stages=args.stages)
        results.append(result)
        print(report(result) + "\n", flush=True)
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
