"""MPEG-TS scanner and keyframe segmenter for the live pipeline (no remuxing).

The segmenter cuts a raw 188-byte transport stream into keyframe-aligned segments. Packets pass
through byte for byte. Each segment is announced with copies of the latest PAT and PMT packets,
which the consumer prepends so that VLC can start playback at any segment.

Pure logic: no Qt and no I/O, and the wall clock is injected.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import time
from typing import Callable

logger = logging.getLogger(__name__)

PACKET = 188
SYNC = 0x47

_CLOCK = 90000  # PTS ticks per second
_PTS_MODULO = 1 << 33
_SYNC_BYTE = b"\x47"
_START_CODE = b"\x00\x00\x01"
_PROBE_BYTES = 64 * 1024  # leading input inspected to tell TS from other content
_PROBE_RUN = 5  # consecutive sync bytes at one stride that prove a packet size
_TABLE_LIMIT = 3 * 1024 * 1024  # input allowed before PAT and PMT must be known
_MAX_SECTION = 1024  # PAT and PMT sections are at most 1024 bytes long
# Backward PTS steps up to this size are B-frame reordering, not a reset: with RAI on every
# PES a reordered B-frame must not cut a one-frame "discontinuity" segment.
_REORDER_TICKS = _CLOCK

_VIDEO_TYPES = (0x1B, 0x24, 0x02, 0x01)  # H.264, HEVC, MPEG-2, MPEG-1
_AUDIO_TYPES = (0x03, 0x04, 0x0F, 0x11, 0x81, 0x87)  # MPEG audio, AAC, LATM, AC-3, E-AC-3
_H264 = 0x1B
_HEVC = 0x24


class Unsupported(Exception):
    """The input cannot be time-shifted.

    `reason` is one of "not MPEG-TS", "unsupported packet size", "no program tables" and
    "scrambled".
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def pts_delta(a: int, b: int) -> int:
    """Wrap-aware `b - a` in 90 kHz ticks: the shortest signed difference modulo 2**33."""
    d = (b - a) % _PTS_MODULO
    return d - _PTS_MODULO if d >= _PTS_MODULO >> 1 else d


@dataclass(frozen=True, slots=True)
class Opened:
    """A segment starts. `header` is the latest PAT packet followed by the PMT packets."""

    seq: int
    first_pts: int | None
    header: bytes
    wall: float
    discontinuity: bool


@dataclass(frozen=True, slots=True)
class Data:
    """Packets of the open segment, in input order."""

    payload: bytes


@dataclass(frozen=True, slots=True)
class Closed:
    """A segment ends.

    `next_first_pts` is the PTS where the next segment starts, or the latest PTS at the end of
    the input.
    """

    seq: int
    next_first_pts: int | None
    wall: float


Event = Opened | Data | Closed


class Segmenter:
    """Cuts a 188-byte MPEG-TS byte stream into keyframe-aligned segments."""

    def __init__(
        self,
        *,
        target_s: float = 2.0,
        forced_s: float = 10.0,
        rai_fallback_s: float = 6.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._target = round(target_s * _CLOCK)
        self._jump = 5 * self._target  # a longer forward step is a discontinuity
        self._forced = round(forced_s * _CLOCK)
        self._forced_s = forced_s
        self._fallback_s = rai_fallback_s
        self._clock = clock
        # Input framing.
        self._buf = b""
        self._pos = 0  # absolute input offset of _buf[0]
        self._searching = True  # the next packet start must be proven by three sync bytes
        self._probe: bytearray | None = bytearray()
        # Program tables.
        self._sections: dict[int, tuple[list[bytes], bytearray]] = {}  # partial PSI by PID
        self._accepted: dict[int, bytes] = {}  # last parsed section by PID
        self._program: int | None = None
        self._pmt_pid: int | None = None
        self._pat = b""
        self._pmt = b""
        self._video_pid: int | None = None
        self._video_type: int | None = None
        self._cut_pid: int | None = None
        # Keyframe detection on the cut PID.
        self._rai_seen = False
        self._nal_scan = False
        self._ref_pts: int | None = None
        self._ref_wall: float | None = None
        self._latest_pts: int | None = None
        # The open segment.
        self._seq = 0
        self._open = False
        self._seg_pts: int | None = None
        self._seg_wall = 0.0
        self._pending = False

    @property
    def video_pid(self) -> int | None:
        return self._video_pid

    @property
    def latest_pts(self) -> int | None:
        return self._latest_pts

    def discontinuity(self) -> None:
        """Mark the next cut as a discontinuity (the source restarted its timestamps)."""
        self._pending = True

    def feed(self, chunk: bytes) -> list[Event]:
        """Consume input bytes; raises `Unsupported` for input that cannot be segmented."""
        if not chunk:
            return []
        if type(chunk) is not bytes:
            chunk = bytes(chunk)
        if self._probe is not None:
            self._check_probe(chunk)
        data = self._buf + chunk if self._buf else chunk
        if len(data) < PACKET:
            self._buf = data
            return []
        events: list[Event] = []
        self._scan(data, self._clock(), events)
        return events

    def finish(self) -> list[Closed]:
        """Close the open segment at the end of the input."""
        if not self._open:
            return []
        self._open = False
        closed = Closed(self._seq, self._latest_pts, self._clock())
        self._seq += 1
        return [closed]

    # -- framing ---------------------------------------------------------------------------

    def _check_probe(self, chunk: bytes) -> None:
        probe = self._probe
        if probe is None:
            return
        probe += chunk[: _PROBE_BYTES - len(probe)]
        if len(probe) < _PROBE_BYTES:
            return
        self._probe = None
        window = bytes(probe)
        if _has_stride(window, PACKET):
            return
        if _has_stride(window, 192) or _has_stride(window, 204):
            raise Unsupported("unsupported packet size")
        raise Unsupported("not MPEG-TS")

    def _scan(self, data: bytes, wall: float, events: list[Event]) -> None:
        n = len(data)
        off = 0
        run = 0 if self._open else -1  # start of the packets not yet emitted as Data
        searching = self._searching
        cut_pid = self._cut_pid
        pmt_pid = self._pmt_pid
        while off + PACKET <= n:
            if searching or data[off] != SYNC:
                if 0 <= run < off:
                    events.append(Data(data[run:off]))
                run = -1
                found = _find_sync(data, off)
                if found < 0:
                    keep = max(off, n - 2 * PACKET)  # a later sync may still start in the tail
                    if keep > off:
                        logger.debug("no packet sync; skipped %d bytes", keep - off)
                    off = keep
                    searching = True
                    if cut_pid is None and self._pos + off >= _TABLE_LIMIT:
                        raise Unsupported("no program tables")
                    break
                if found > off:
                    logger.debug("packet sync regained after %d bytes", found - off)
                off = found
                searching = False
                if self._open:
                    run = off
                continue
            end = off + PACKET
            b1 = data[off + 1]
            pid = ((b1 & 0x1F) << 8) | data[off + 2]
            if pid == cut_pid:
                if not b1 & 0x80:  # with the transport error flag the header is untrusted
                    if data[off + 3] & 0xC0:
                        raise Unsupported("scrambled")
                    if b1 & 0x40:
                        cut = self._on_pusi(data[off:end], wall)
                        if cut:
                            if 0 <= run < off:
                                events.append(Data(data[run:off]))
                            events.extend(cut)
                            run = off
            elif pid == 0 or pid == pmt_pid:
                if not b1 & 0x80:
                    self._on_psi(pid, data[off:end])
                    cut_pid = self._cut_pid
                    pmt_pid = self._pmt_pid
            elif cut_pid is None and self._pos + off >= _TABLE_LIMIT:
                raise Unsupported("no program tables")
            off = end
        if 0 <= run < off:
            events.append(Data(data[run:off]))
        self._buf = data[off:]
        self._pos += off
        self._searching = searching

    # -- keyframes and cuts ----------------------------------------------------------------

    def _on_pusi(self, pkt: bytes, wall: float) -> list[Event] | None:
        rai, pts, es = _pes_start(pkt)
        if es >= PACKET:
            logger.debug("PES header not readable from its first packet")
        if pts is not None:
            self._latest_pts = pts
            if self._ref_pts is None:
                self._ref_pts = pts
        if self._ref_wall is None:
            self._ref_wall = wall
        if rai:
            self._rai_seen = True
        if not self._open:  # the first segment, or the first after finish(), which left a gap
            if self._is_key(rai, pts, pkt, es, wall):
                return self._cut(pts, wall, self._seq > 0)
            return None
        first = self._seg_pts
        delta = None if pts is None or first is None else pts_delta(first, pts)
        if delta is None or delta < -_REORDER_TICKS:
            # PTS missing or reset backwards: cut at the next keyframe, time out by wall clock.
            if self._is_key(rai, pts, pkt, es, wall) or wall - self._seg_wall >= self._forced_s:
                return self._cut(pts, wall, True)
            return None
        discontinuity = self._pending or delta > self._jump
        if (discontinuity or delta >= self._target) and self._is_key(rai, pts, pkt, es, wall):
            return self._cut(pts, wall, discontinuity)
        if delta >= self._forced:
            return self._cut(pts, wall, discontinuity)
        return None

    def _is_key(self, rai: bool, pts: int | None, pkt: bytes, es: int, wall: float) -> bool:
        if self._video_pid is None:  # audio only: every audio PES with a PTS is a cut point
            return pts is not None
        if rai:
            return True
        if self._rai_seen:
            return False
        if not self._nal_scan:
            ref_wall = wall if self._ref_wall is None else self._ref_wall
            elapsed = wall - ref_wall
            if pts is not None and self._ref_pts is not None:
                elapsed = max(elapsed, pts_delta(self._ref_pts, pts) / _CLOCK)
            if elapsed < self._fallback_s:
                return False
            self._nal_scan = True
            logger.debug("no random access indicator after %.1f s; scanning NAL units", elapsed)
        return _nal_key(pkt, es, self._video_type)

    def _cut(self, pts: int | None, wall: float, discontinuity: bool) -> list[Event]:
        events: list[Event] = []
        if self._open:
            events.append(Closed(self._seq, pts, wall))
            self._seq += 1
        events.append(Opened(self._seq, pts, self._pat + self._pmt, wall, discontinuity))
        self._open = True
        self._seg_pts = pts
        self._seg_wall = wall
        self._pending = False
        return events

    # -- program tables --------------------------------------------------------------------

    def _on_psi(self, pid: int, pkt: bytes) -> None:
        afc = (pkt[3] >> 4) & 3
        if not afc & 1:
            return
        start = 5 + pkt[4] if afc & 2 else 4
        if start >= PACKET:
            logger.debug("skipped a PSI packet with an oversized adaptation field")
            return
        if pkt[1] & 0x40:
            pointer = pkt[start]
            begin = start + 1 + pointer
            pending = self._sections.pop(pid, None)
            if pending is not None and pointer:  # the pointer bytes end the previous section
                pending[0].append(pkt)
                pending[1].extend(pkt[start + 1 : min(begin, PACKET)])
                self._collect(pid, *pending)
                self._sections.pop(pid, None)
            if begin >= PACKET or pkt[begin] == 0xFF:
                if begin >= PACKET:
                    logger.debug("skipped a PSI packet whose pointer field overruns it")
                return
            self._collect(pid, [pkt], bytearray(pkt[begin:]))
            return
        pending = self._sections.get(pid)
        if pending is not None:
            pending[0].append(pkt)
            pending[1].extend(pkt[start:])
            self._collect(pid, *pending)

    def _collect(self, pid: int, packets: list[bytes], body: bytearray) -> None:
        if len(body) >= 3:
            total = 3 + (((body[1] & 0x0F) << 8) | body[2])
            if total > _MAX_SECTION:
                logger.debug("skipped a PSI section of %d bytes on PID %#x", total, pid)
                self._sections.pop(pid, None)
                return
            if len(body) >= total:
                self._sections.pop(pid, None)
                self._on_section(pid, bytes(body[:total]), packets)
                return
        self._sections[pid] = (packets, body)

    def _on_section(self, pid: int, section: bytes, packets: list[bytes]) -> None:
        if section == self._accepted.get(pid):  # a repeat: only refresh the packet copies
            if pid == 0:
                self._pat = b"".join(packets)
            else:
                self._pmt = b"".join(packets)
            return
        if _crc32(section) != 0:
            logger.debug("skipped a PSI section with a bad CRC on PID %#x", pid)
            return
        if len(section) < 12 or not section[5] & 1:  # truncated, or not yet applicable
            return
        if pid == 0 and section[0] == 0x00:
            accepted = self._on_pat(section, packets)
        elif pid == self._pmt_pid and section[0] == 0x02:
            accepted = self._on_pmt(section, packets)
        else:
            return
        if accepted:
            self._accepted[pid] = section

    def _on_pat(self, section: bytes, packets: list[bytes]) -> bool:
        end = len(section) - 4
        for i in range(8, end - 3, 4):
            program = (section[i] << 8) | section[i + 1]
            if program:  # program 0 points at the network table
                pmt_pid = ((section[i + 2] & 0x1F) << 8) | section[i + 3]
                break
        else:
            logger.debug("skipped a PAT without programs")
            return False
        if pmt_pid in (0, 0x1FFF):
            logger.debug("skipped a PAT with an invalid PMT PID")
            return False
        self._pat = b"".join(packets)
        if (program, pmt_pid) != (self._program, self._pmt_pid):
            if self._pmt_pid is not None:
                logger.debug("program map moved to PID %#x", pmt_pid)
                self._sections.pop(self._pmt_pid, None)
                self._accepted.pop(self._pmt_pid, None)
            self._program = program
            self._pmt_pid = pmt_pid
        return True

    def _on_pmt(self, section: bytes, packets: list[bytes]) -> bool:
        if ((section[3] << 8) | section[4]) != self._program:
            return False
        end = len(section) - 4
        i = 12 + (((section[10] & 0x0F) << 8) | section[11])
        video: tuple[int, int] | None = None
        audio: int | None = None
        while i + 5 <= end:
            stream_type = section[i]
            es_pid = ((section[i + 1] & 0x1F) << 8) | section[i + 2]
            if video is None and stream_type in _VIDEO_TYPES:
                video = (es_pid, stream_type)
            elif audio is None and stream_type in _AUDIO_TYPES:
                audio = es_pid
            i += 5 + (((section[i + 3] & 0x0F) << 8) | section[i + 4])
        if video is not None:
            self._video_pid, self._video_type = video
            cut_pid: int | None = video[0]
        elif audio is not None:
            self._video_pid = self._video_type = None
            cut_pid = audio
        else:
            logger.debug("skipped a PMT without supported video or audio")
            return False
        self._pmt = b"".join(packets)
        if cut_pid != self._cut_pid:
            if self._cut_pid is not None:
                logger.debug("cut PID moved from %#x to %#x", self._cut_pid, cut_pid)
            self._cut_pid = cut_pid
            self._rai_seen = False
            self._nal_scan = False
            self._ref_pts = None
            self._ref_wall = None
        return True


def _find_sync(data: bytes, start: int) -> int:
    """First i >= start with sync bytes at i, i+188 and i+376; -1 if none is decidable yet."""
    stop = len(data) - 2 * PACKET  # candidates must have i + 376 inside data
    if stop <= start:  # (a negative stop would count from the end)
        return -1
    i = data.find(_SYNC_BYTE, start, stop)
    while i >= 0:
        if data[i + PACKET] == SYNC and data[i + 2 * PACKET] == SYNC:
            return i
        i = data.find(_SYNC_BYTE, i + 1, stop)
    return -1


def _has_stride(window: bytes, stride: int) -> bool:
    """Whether `window` holds `_PROBE_RUN` consecutive sync bytes `stride` bytes apart."""
    run = _SYNC_BYTE * _PROBE_RUN
    return any(run in window[phase::stride] for phase in range(stride))


def _pes_start(pkt: bytes) -> tuple[bool, int | None, int]:
    """(random access indicator, PTS, offset of the elementary stream bytes) of a PUSI packet.

    The offset is PACKET when the PES header cannot be read from this packet.
    """
    afc = (pkt[3] >> 4) & 3
    rai = False
    start = 4
    if afc & 2:
        length = pkt[4]
        if length > PACKET - 5:
            return False, None, PACKET
        rai = length > 0 and bool(pkt[5] & 0x40)
        start = 5 + length
    if not afc & 1 or start + 9 > PACKET or pkt[start : start + 3] != _START_CODE:
        return rai, None, PACKET
    header_length = pkt[start + 8]
    pts = None
    if (
        (pkt[start + 6] & 0xC0) == 0x80  # an optional PES header is present
        and pkt[start + 7] & 0x80
        and header_length >= 5
        and start + 14 <= PACKET
    ):
        b = pkt[start + 9 : start + 14]
        pts = ((b[0] >> 1) & 7) << 30 | b[1] << 22 | (b[2] >> 1) << 15 | b[3] << 7 | b[4] >> 1
    return rai, pts, start + 9 + header_length


def _nal_key(pkt: bytes, start: int, video_type: int | None) -> bool:
    """Whether the elementary stream bytes of this packet begin a keyframe."""
    i = pkt.find(_START_CODE, start)
    while 0 <= i < PACKET - 3:
        code = pkt[i + 3]
        if video_type == _H264:
            if (code & 0x1F) in (5, 7):  # IDR slice, SPS
                return True
        elif video_type == _HEVC:
            nal_type = (code >> 1) & 0x3F
            if 16 <= nal_type <= 21 or nal_type == 32:  # IRAP slices, VPS
                return True
        elif code == 0xB3:  # MPEG-1/2 sequence header
            return True
        i = pkt.find(_START_CODE, i + 3)
    return False


def _crc_table() -> tuple[int, ...]:
    table = []
    for i in range(256):
        crc = i << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) if crc & 0x80000000 else crc << 1
            crc &= 0xFFFFFFFF
        table.append(crc)
    return tuple(table)


_CRC_TABLE = _crc_table()


def _crc32(data: bytes) -> int:
    """MPEG-2 CRC-32; a section including its CRC field yields 0 when intact."""
    crc = 0xFFFFFFFF
    for byte in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC_TABLE[(crc >> 24) ^ byte]
    return crc
