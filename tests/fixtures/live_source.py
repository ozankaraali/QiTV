"""A local live channel for the smoke test: tests/fixtures/live.ts, endless and in real time.

`LiveSource` serves the clip at `url` over plain HTTP on 127.0.0.1 as one close-delimited
MPEG-TS stream that never ends, the way a provider's live channel arrives: 64-packet chunks,
each sent when the wall clock reaches the media time of its last PCR. Every pass after the
first moves PTS, DTS and PCR one clip length on and continues the continuity counters, so
the loop looks like one continuous broadcast (no timestamp jump, no discontinuity).

The clip (12 s, 320×180, 25 fps H.264 baseline with a 1 s GOP, plus AAC) is generated test
content, made once with ffmpeg as a developer tool:

    ffmpeg -f lavfi -i testsrc2=s=320x180:r=25 -f lavfi -i sine=frequency=440 -t 12 \
        -c:v libx264 -profile:v baseline -g 25 -keyint_min 25 -sc_threshold 0 -b:v 400k \
        -c:a aac -b:a 64k -f mpegts tests/fixtures/live.ts

With `burst_s`, the channel arrives in bursts, as some providers' restreams send it: every
`burst_s` seconds, everything due before the next burst at once.

`connections` counts the requests; `events` records when each connection opened (+1) and
closed (-1), and `peak` is the most open at once. A client going away is seen at once (the
server watches its socket between chunks), so the counts follow the client's own closes.

No credentials, no network: the server binds 127.0.0.1 only.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
import itertools
import math
from pathlib import Path
import select
import statistics
import threading
import time
from typing import Optional

from services.live.server import ThreadingLoopbackHTTPServer

FIXTURE_DIR = Path(__file__).resolve().parent
LIVE_TS = FIXTURE_DIR / "live.ts"
PATH = "/live/1.ts"
PACKET = 188
CHUNK_PACKETS = 64
CLOCK = 90_000
_WRAP = 1 << 33
_NO_PES_HEADER = {0xBC, 0xBE, 0xBF, 0xF0, 0xF1, 0xF2, 0xF8, 0xFF}  # no PTS field


@dataclass(frozen=True)
class _Packet:
    pid: int
    payload: bool
    pcr: Optional[int] = None  # offset of the 6-byte PCR
    pts: Optional[int] = None  # offset of the 5-byte PTS
    dts: Optional[int] = None  # offset of the 5-byte DTS


def _read_stamp(data: bytes | bytearray, at: int) -> int:
    b = data[at : at + 5]
    return ((b[0] >> 1) & 7) << 30 | b[1] << 22 | (b[2] >> 1) << 15 | b[3] << 7 | b[4] >> 1


def _write_stamp(data: bytearray, at: int, value: int) -> None:
    value %= _WRAP
    data[at] = (data[at] & 0xF0) | ((value >> 30) & 7) << 1 | 1
    data[at + 1] = (value >> 22) & 0xFF
    data[at + 2] = ((value >> 15) & 0x7F) << 1 | 1
    data[at + 3] = (value >> 7) & 0xFF
    data[at + 4] = (value & 0x7F) << 1 | 1


def _read_pcr(data: bytes | bytearray, at: int) -> int:
    b = data[at : at + 5]
    return (b[0] << 25) | (b[1] << 17) | (b[2] << 9) | (b[3] << 1) | (b[4] >> 7)


def _write_pcr(data: bytearray, at: int, base: int) -> None:
    base %= _WRAP
    data[at] = (base >> 25) & 0xFF
    data[at + 1] = (base >> 17) & 0xFF
    data[at + 2] = (base >> 9) & 0xFF
    data[at + 3] = (base >> 1) & 0xFF
    data[at + 4] = ((base & 1) << 7) | (data[at + 4] & 0x7F)  # reserved bits and extension


def _parse(packet: bytes) -> _Packet:
    if packet[0] != 0x47:
        raise ValueError("not an MPEG-TS packet")
    pid = ((packet[1] & 0x1F) << 8) | packet[2]
    control = (packet[3] >> 4) & 3
    offset, pcr = 4, None
    if control & 2:
        length = packet[4]
        if length and packet[5] & 0x10:
            pcr = 6
        offset = 5 + length
    if not control & 1:
        return _Packet(pid, False, pcr)
    pts = dts = None
    pes = packet[offset:]
    if packet[1] & 0x40 and pes[:3] == b"\0\0\1" and pes[3] not in _NO_PES_HEADER:
        flags = pes[7] >> 6
        if flags & 2:
            pts = offset + 9
        if flags == 3:
            dts = offset + 14
    return _Packet(pid, True, pcr, pts, dts)


class LoopedStream:
    """An MPEG-TS clip played as an endless live stream (see the module docstring)."""

    def __init__(self, data: bytes, chunk_packets: int = CHUNK_PACKETS) -> None:
        if not data or len(data) % PACKET:
            raise ValueError("the clip must be whole 188-byte packets")
        self._packets = [data[i : i + PACKET] for i in range(0, len(data), PACKET)]
        self._parsed = [_parse(packet) for packet in self._packets]
        # Each pass continues every PID's counters from where the last pass left them.
        self._cc_step = Counter(p.pid for p in self._parsed if p.payload)
        pcrs = [
            (i, _read_pcr(self._packets[i], p.pcr))
            for i, p in enumerate(self._parsed)
            if p.pcr is not None
        ]
        if not pcrs:
            raise ValueError("the clip carries no PCR")
        clock_pid = self._parsed[pcrs[0][0]].pid
        stamps = [
            _read_stamp(self._packets[i], p.pts)
            for i, p in enumerate(self._parsed)
            if p.pid == clock_pid and p.pts is not None
        ]
        if len(stamps) < 2:
            raise ValueError("the clip is too short to loop")
        # One pass lasts the PCR stream's frames: the last one's PTS plus one frame. The
        # other streams start one period later too, so they never overlap themselves.
        frame = statistics.median(b - a for a, b in itertools.pairwise(stamps))
        self._ticks = stamps[-1] - stamps[0] + int(frame)
        self.period = self._ticks / CLOCK
        first_pcr = pcrs[0][1]
        latest, due = first_pcr, []
        pcr_at = dict(pcrs)
        for i in range(len(self._packets)):
            latest = pcr_at.get(i, latest)
            due.append((latest - first_pcr) / CLOCK)
        self._chunks = [
            (start, min(start + chunk_packets, len(self._packets)))
            for start in range(0, len(self._packets), chunk_packets)
        ]
        self._due = [due[end - 1] for _, end in self._chunks]

    def chunks(self) -> Iterator[tuple[float, bytes]]:
        """(seconds after the stream started when it is due, chunk), for ever."""
        for loop in itertools.count():
            for (start, end), due in zip(self._chunks, self._due):
                yield loop * self.period + due, self._render(loop, start, end)

    def _render(self, loop: int, start: int, end: int) -> bytes:
        if loop == 0:
            return b"".join(self._packets[start:end])
        shift = loop * self._ticks
        out = bytearray()
        for packet, parsed in zip(self._packets[start:end], self._parsed[start:end]):
            data = bytearray(packet)
            data[3] = (data[3] & 0xF0) | ((data[3] + loop * self._cc_step[parsed.pid]) & 0x0F)
            if parsed.pcr is not None:
                _write_pcr(data, parsed.pcr, _read_pcr(data, parsed.pcr) + shift)
            for at in (parsed.pts, parsed.dts):
                if at is not None:
                    _write_stamp(data, at, _read_stamp(data, at) + shift)
            out += data
        return bytes(out)


def peak_of(
    events: list[tuple[float, int]], *, start: Optional[float] = None, end: Optional[float] = None
) -> int:
    """The most connections open at once in `events` ((when, +1 or -1), any order).

    With `start` and/or `end`, only from `start` (counting those already open then) until
    before `end`.
    """
    peak = current = 0
    started = start is None
    for at, change in sorted(events):  # at the same moment, a close counts before an open
        if end is not None and at >= end:
            break
        if not started and start is not None and at >= start:
            started, peak = True, current  # what was open when it began
        current += change
        if started:
            peak = max(peak, current)
    return peak if started else current  # it began after the last event before `end`


def opens_in(events: list[tuple[float, int]], start: float, end: float) -> int:
    """How many connections opened in [start, end) in `events` ((when, +1 or -1))."""
    return sum(1 for at, change in events if change > 0 and start <= at < end)


class LiveSource:
    """Serves `LoopedStream(path)` at `url` to every client until `close()`; in bursts of
    `burst_s` seconds when that is set."""

    def __init__(self, path: Path = LIVE_TS, *, burst_s: float = 0.0) -> None:
        self.stream = LoopedStream(Path(path).read_bytes())
        self.burst_s = burst_s
        self.connections = 0
        self.events: list[tuple[float, int]] = []  # (time.monotonic(), +1 open / -1 close)
        self._stop = threading.Event()
        lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:
                pass

            def do_GET(self) -> None:
                if self.path != PATH:
                    self.send_error(404)
                    return
                with lock:
                    owner.connections += 1
                    owner.events.append((time.monotonic(), 1))
                try:
                    self._serve()
                finally:
                    with lock:
                        owner.events.append((time.monotonic(), -1))

            def _serve(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "video/mp2t")
                self.send_header("Connection", "close")
                self.end_headers()
                started = time.monotonic()
                for due, chunk in owner.stream.chunks():
                    if owner.burst_s > 0:  # sent with the burst it falls in
                        due = math.floor(due / owner.burst_s) * owner.burst_s
                    if not self._wait(started + due):
                        return
                    try:
                        self.wfile.write(chunk)
                    except OSError:  # the client went away
                        return

            def _wait(self, until: float) -> bool:
                """Wait until `until`; False once the source closes or the client went away."""
                while not owner._stop.is_set():
                    left = until - time.monotonic()
                    if left <= 0:
                        return True
                    readable, _, _ = select.select([self.connection], [], [], min(left, 0.05))
                    if not readable:
                        continue
                    try:  # the client sends nothing after its request: this is its close
                        if not self.connection.recv(4096):
                            return False
                    except OSError:
                        return False
                return False

        self._server = ThreadingLoopbackHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_port}{PATH}"
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self._thread.start()

    @property
    def peak(self) -> int:
        """The most connections that were open at once."""
        return peak_of(list(self.events))

    def close(self) -> None:
        self._stop.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(3)
