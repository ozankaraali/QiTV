"""MPEG-TS scanner and keyframe segmenter for the live pipeline (no remuxing).

The segmenter cuts a raw 188-byte transport stream into keyframe-aligned segments. Packets pass
through byte for byte. Each segment is announced with copies of the latest PAT and PMT packets,
which the consumer prepends so that VLC can start playback at any segment. Its close says how
cleanly playback starts there (`Closed.start_quality`): at an IDR picture, at an intra picture
whose leading pictures may reach behind it (open GOP), or without a keyframe (a forced cut).

Pure logic: no Qt and no I/O, and the wall clock is injected.

Tests: `uv run python -m unittest discover -s tests -p test_ts_segmenter.py` (the module-path
form cannot import `ts_fixtures`).
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import time
from typing import Callable

from services.live.timeline import START_KEY, START_NONE, START_OPEN, StartQuality

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
_CRC_REPEATS = 3  # identical bad-CRC sections accepted while no valid one has been seen
# Backward PTS steps up to this size are B-frame reordering, not a reset: with RAI on every
# PES a reordered B-frame must not cut a one-frame "discontinuity" segment.
_REORDER_TICKS = _CLOCK
# A longer forward step between two PES is a timestamp jump, unless the stream's own cadence
# (its previous forward step) is at least half as long: a slideshow's stills seconds apart.
# A jump is left out of the timeline (`Segmenter.dropped`); a shorter step stays media.
JUMP_S = 3.0
_JUMP_TICKS = round(JUMP_S * _CLOCK)
_SCRAMBLED_PACKETS = 8  # scrambled cut-PID packets that prove scrambling ...
_SCRAMBLED_WINDOW_S = 2.0  # ... in total within this first wall time, later in a row
_STALL_S = 5.0  # a longer pause between feeds is a stalled source: removed from the clock
# A segment's PTS span may pass the forced cut by this much: the cut lands on the first frame at
# or past it (10.01 s at 29.97 fps).
_FORCED_MARGIN = _CLOCK

_VIDEO_TYPES = (0x1B, 0x24, 0x02, 0x01)  # H.264, HEVC, MPEG-2, MPEG-1
_AUDIO_TYPES = (0x03, 0x04, 0x0F, 0x11, 0x81, 0x87)  # MPEG audio, AAC, LATM, AC-3, E-AC-3
_H264 = 0x1B
_HEVC = 0x24
# Results of `_classify`; the positive ones are keyframes to the cut decision.
_IDR = 4
_OPEN = 3
_INTRA = 2
_PARAM = 1
_UNDECIDED = 0
_NOT_KEY = -1
# A segment start whose picture begins beyond the PES's first packet (behind large SEI, say)
# is read on in the PES's next packets, up to this many ES bytes (16 packets).
_LOOK_AHEAD = 16 * 184


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
    """A segment starts. `header` is the latest PAT packet followed by the PMT packets.

    `wall` (here and in `Closed`) is on the segmenter's stall-free clock (`Segmenter.wall`).
    """

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
    the input. Across a PTS jump it is the segment's own first PTS plus the media it holds:
    the old content up to its last frame's end, then the new content up to the cut. It is None
    when that is unknown (no PTS), or beyond the forced cut plus a margin, so that the segment
    is sized by the stall-free wall clock instead.

    `start_quality` is how cleanly playback starts at this segment (the timeline's START_*
    values). It comes with the close because the first picture may begin in a later packet of
    the segment's first PES.
    """

    seq: int
    next_first_pts: int | None
    wall: float
    start_quality: StartQuality = START_KEY


Event = Opened | Data | Closed


@dataclass(slots=True)
class _Look:
    """Reading the open segment's first PES on while its picture is unknown."""

    es: bytearray  # its ES bytes so far
    cc: int  # the continuity counter of the last packet read
    last: bytes  # that packet: a true duplicate repeats it byte for byte
    rai: bool  # the PES's random access indicator


class Segmenter:
    """Cuts a 188-byte MPEG-TS byte stream into keyframe-aligned segments."""

    def __init__(
        self,
        *,
        target_s: float = 2.0,
        forced_s: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._target = round(target_s * _CLOCK)
        self._target_s = target_s
        self._forced = round(forced_s * _CLOCK)
        self._forced_s = forced_s
        self._clock = clock
        # Input framing.
        self._buf = b""
        self._pos = 0  # absolute input offset of _buf[0]
        self._searching = True  # the next packet start must be proven by three sync bytes
        self._probe: bytearray | None = bytearray()
        self._start_wall: float | None = None
        self._feed_wall: float | None = None  # the injected clock at the latest input
        self._stalled = 0.0  # stalls so far: removed from every wall time used and reported
        self._wall = 0.0  # the stall-free time of the latest input
        # Program tables.
        self._sections: dict[int, tuple[list[bytes], bytearray]] = {}  # partial PSI by PID
        self._accepted: dict[int, bytes] = {}  # last parsed section by PID
        self._crc_valid: set[int] = set()  # PIDs that delivered a section with a valid CRC
        self._bad_crc: dict[int, tuple[bytes, int]] = {}  # repeated bad-CRC section by PID
        self._bad_crc_logged = False
        self._program: int | None = None
        self._pmt_pid: int | None = None
        self._pat = b""
        self._pmt = b""
        self._video_pid: int | None = None
        self._video_type: int | None = None
        self._cut_pid: int | None = None
        # The cut PID.
        self._rai_seen = False
        self._last_pts: int | None = None
        self._latest_pts: int | None = None
        # The cut PID's current content (since the last PTS jump): its furthest PTS and its
        # shortest forward PTS step (one frame), which give where it ends at a jump.
        self._top: int | None = None
        self._frame: int | None = None
        self._cadence: int | None = None  # its latest forward step; None right after a jump
        self._pusi_wall = 0.0  # wall time of the last cut-PID PES start (or cut)
        # Other PIDs' (first PTS, last PTS) since then: media time while the cut PID is quiet.
        self._quiet: dict[int, tuple[int, int]] = {}
        self._scrambled_total = 0
        self._scrambled_run = 0
        # The open segment.
        self._seq = 0
        self._open = False
        self._seg_quality: StartQuality = START_NONE  # how cleanly the open segment starts
        self._look: _Look | None = None  # its first PES read on while its picture is unknown
        self._seg_pts: int | None = None
        self._seg_wall = 0.0
        self._pending = False
        self._jumped = False  # the pending discontinuity came with a PTS jump
        # The segment's media age is measured from this PTS (moved back by the media carried
        # across a jump).
        self._media_ref: int | None = None
        # Across a jump the segment holds parts of two contents: the current content's part
        # starts at `_part_pts`, after `_carried` ticks of earlier media (None when unknown).
        self._part_pts: int | None = None
        self._carried: int | None = 0
        # Forward PTS jumps left out of the timeline, for the caller to take: (the open
        # segment's seq, seconds of its media before the jump or None, seconds left out).
        self.dropped: list[tuple[int, float | None, float]] = []

    @property
    def video_pid(self) -> int | None:
        return self._video_pid

    @property
    def latest_pts(self) -> int | None:
        return self._latest_pts

    @property
    def open_quality(self) -> StartQuality | None:
        """How cleanly playback starts at the open segment, as known so far (None: none is
        open); its `Closed.start_quality` once it closes (2.1 stage 3: a tune prefers a key)."""
        return self._seg_quality if self._open else None

    @property
    def progress_pts(self) -> int | None:
        """The latest cut-PID PTS on the open segment's own axis, for its live progress: across
        a PTS jump, the segment's first PTS plus the media since. None when unknown."""
        return self._on_segment_axis(self._latest_pts)

    @property
    def wall(self) -> float:
        """The stall-free time of the latest input (0.0 before any): the clock of `Opened.wall`
        and `Closed.wall`. A pause between two inputs longer than `_STALL_S` is a stalled source
        and is removed whole, so it never counts as media time, silence or segment age."""
        return self._wall

    def discontinuity(self) -> None:
        """Mark the next cut as a discontinuity (the source restarted its timestamps).

        The input starts afresh: a partial packet the old input left is dropped, and the next
        packet start must be proven again, so no packet joins the two inputs.
        """
        self._latch()
        self._media_ref = None  # measure the new content's age from its first PTS
        self._look = None  # the rest of the open segment's first PES never comes
        self._pos += len(self._buf)
        self._buf = b""
        self._searching = True

    def feed(self, chunk: bytes) -> list[Event]:
        """Consume input bytes; raises `Unsupported` for input that cannot be segmented."""
        if not chunk:
            return []
        if type(chunk) is not bytes:
            chunk = bytes(chunk)
        wall = self._tick()
        if self._probe is not None:
            self._check_probe(chunk)
        data = self._buf + chunk if self._buf else chunk
        if len(data) < PACKET:
            self._buf = data
            return []
        events: list[Event] = []
        self._scan(data, wall, events)
        return events

    def finish(self) -> list[Closed]:
        """Close the open segment at the end of the input."""
        if not self._open:
            return []
        self._open = False
        pts = self._on_segment_axis(self._latest_pts)
        closed = Closed(self._seq, pts, self._tick(), self._seg_quality)
        self._seq += 1
        return [closed]

    def _tick(self) -> float:
        """The stall-free wall time now (see `wall`)."""
        now = self._clock()
        if self._feed_wall is not None and now - self._feed_wall > _STALL_S:
            self._stalled += now - self._feed_wall
        self._feed_wall = now
        self._wall = now - self._stalled
        return self._wall

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
        if self._start_wall is None:
            self._start_wall = wall
        # The cut PID went quiet while other packets flow: cut at the next PES start of any PID.
        silent = self._open and wall - self._pusi_wall >= self._forced_s
        n = len(data)
        off = 0
        run = 0 if self._open else -1  # start of the packets not yet emitted as Data
        searching = self._searching
        scrambled = self._scrambled_run
        # A reading of the open segment's first PES that the previous input's end interrupted.
        # (No scan starts out of sync while one goes on: `discontinuity()` ends it, and a sync
        # lost within an input ends it as the reading reaches that packet first.)
        if self._look is not None:
            self._read_ahead(data, 0)
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
                        scrambled += 1
                        self._scrambled_total += 1
                        self._check_scrambled(scrambled, wall)
                    else:
                        scrambled = 0
                        if b1 & 0x40:
                            silent = False
                            cut = self._on_pusi(data[off:end], wall)
                            if cut:
                                if 0 <= run < off:
                                    events.append(Data(data[run:off]))
                                events.extend(cut)
                                run = off
                                if self._look is not None:
                                    self._read_ahead(data, end)
            else:
                if (
                    (b1 & 0xC0) == 0x40  # a PES or section start without a transport error
                    and self._open
                    and (silent or self._media_quiet(pid, data[off:end]))
                ):
                    silent = False
                    logger.debug("PID %#x went quiet; cutting at PID %#x", cut_pid, pid)
                    if 0 <= run < off:
                        events.append(Data(data[run:off]))
                    events.extend(self._cut(None, wall, True))
                    run = off
                if pid == 0 or pid == pmt_pid:
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
        self._scrambled_run = scrambled

    def _media_quiet(self, pid: int, pkt: bytes) -> bool:
        """Whether another PID's PTS advanced `forced_s` since the cut PID's last PES start.

        Media time also notices a silent cut PID when the input arrives in bursts. Each PID is
        measured on its own clock, which may be offset, sparse or jump (then it starts over).
        """
        pts = _pes_start(pkt)[1]
        if pts is None:
            return False
        span = self._quiet.get(pid)
        if span is None:
            self._quiet[pid] = (pts, pts)
            return False
        first, last = span
        step = pts_delta(last, pts)
        if step < -_REORDER_TICKS or step > _JUMP_TICKS:
            self._quiet[pid] = (pts, pts)
            return False
        self._quiet[pid] = (first, pts)
        return pts_delta(first, pts) >= self._forced

    def _check_scrambled(self, run: int, wall: float) -> None:
        """Isolated scrambling bits are corruption; many mean the stream is encrypted."""
        start = wall if self._start_wall is None else self._start_wall
        early = wall - start < _SCRAMBLED_WINDOW_S
        if run >= _SCRAMBLED_PACKETS or (early and self._scrambled_total >= _SCRAMBLED_PACKETS):
            raise Unsupported("scrambled")

    # -- keyframes and cuts ----------------------------------------------------------------

    def _on_pusi(self, pkt: bytes, wall: float) -> list[Event] | None:
        rai, pts, es = _pes_start(pkt)
        if es >= PACKET:
            logger.debug("PES header not readable from its first packet")
        self._pusi_wall = wall
        self._quiet.clear()
        if pts is not None:
            last = self._last_pts
            if last is not None:
                step = pts_delta(last, pts)
                cadence = self._cadence
                if step < -_REORDER_TICKS or (
                    cadence is not None and step > max(_JUMP_TICKS, 2 * cadence)
                ):
                    logger.debug("PTS jumped by %.2f s", step / _CLOCK)
                    self._latch()
                    self._carry()
                    if step > 0 and self._open:
                        self._report_jump(last, pts)
                    self._jumped = True
                    self._part_pts = pts
                    # The segment ages by all of its media, not only the new content's.
                    carried = self._carried
                    self._media_ref = pts if carried is None else (pts - carried) % _PTS_MODULO
                    self._top = self._frame = self._cadence = None
                elif step > 0:
                    self._cadence = step
                    if self._frame is None or step < self._frame:
                        self._frame = step
            self._last_pts = pts
            self._latest_pts = pts
            if self._top is None or pts_delta(self._top, pts) > 0:
                self._top = pts
            if self._media_ref is None:
                self._media_ref = pts
        if rai:
            self._rai_seen = True
        if not self._open:  # the first segment, or the first after finish(), which left a gap
            if self._is_key(rai, pts, pkt, es):
                return self._cut_pes(pkt, rai, es, pts, wall, self._seq > 0)
            return None
        first = self._seg_pts
        if not self._pending and pts is not None and first is not None:
            delta = pts_delta(first, pts)
            if delta >= -_REORDER_TICKS:
                if delta >= self._target and self._is_key(rai, pts, pkt, es):
                    return self._cut_pes(pkt, rai, es, pts, wall, False)
                if delta >= self._forced:
                    return self._cut_pes(pkt, rai, es, pts, wall, False)
                return None
        # A pending discontinuity, no PTS, or PTS drifted backwards: time the segment by the
        # wall clock, and bound it by media time where PTS exist (feed stalls do not age it).
        elapsed = wall - self._seg_wall
        due = self._pending or elapsed >= self._target_s
        ref = self._media_ref
        aged = elapsed >= self._forced_s or (
            pts is not None and ref is not None and pts_delta(ref, pts) >= self._forced
        )
        if (due and self._is_key(rai, pts, pkt, es)) or aged:
            return self._cut_pes(pkt, rai, es, pts, wall, True)
        return None

    def _latch(self) -> None:
        """Make the next cut a discontinuity. The new content may lack RAI: until it shows
        one, its keyframes are found by their NAL units."""
        self._pending = True
        self._rai_seen = False

    def _carry(self) -> None:
        """At a PTS jump: add the old content's part, up to its last frame's end, to the media
        of the open segment."""
        part_pts, top, frame = self._part_pts, self._top, self._frame
        if self._carried is None or part_pts is None or top is None or frame is None:
            self._carried = None
            return
        part = pts_delta(part_pts, top + frame)
        self._carried = self._carried + part if part >= 0 else None

    def _report_jump(self, last: int, pts: int) -> None:
        """A forward jump the timeline leaves out: from the old content's end (its furthest PTS
        plus a frame; else the last PTS) to `pts`, after the open segment's media so far."""
        top, frame = self._top, self._frame
        end = (top + frame) % _PTS_MODULO if top is not None and frame is not None else last
        carried = self._carried
        offset = carried / _CLOCK if carried is not None else None
        self.dropped.append((self._seq, offset, max(pts_delta(end, pts), 0) / _CLOCK))

    def _on_segment_axis(self, pts: int | None) -> int | None:
        """`pts` of the current content as a PTS on the open segment's own axis: unchanged
        unless a PTS jump is pending, then the segment's first PTS plus its media up to `pts`."""
        if not self._jumped:
            return pts
        first, part_pts, carried = self._seg_pts, self._part_pts, self._carried
        if pts is None or first is None or part_pts is None or carried is None:
            return None
        return (first + carried + max(pts_delta(part_pts, pts), 0)) % _PTS_MODULO

    def _is_key(self, rai: bool, pts: int | None, pkt: bytes, es: int) -> bool:
        if self._video_pid is None:  # audio only: every audio PES with a PTS is a cut point
            return pts is not None
        kind = _classify(pkt, es, self._video_type)
        if rai:  # trust the flag unless the picture is visibly not a keyframe
            return kind != _NOT_KEY
        return not self._rai_seen and kind > _UNDECIDED

    def _cut_pes(
        self, pkt: bytes, rai: bool, es: int, pts: int | None, wall: float, discontinuity: bool
    ) -> list[Event]:
        """Cut at this cut-PID PES start; its picture tells how cleanly the segment starts."""
        events = self._cut(pts, wall, discontinuity)
        if self._video_pid is None:  # audio only: no keyframes (START_NONE)
            return events
        kind = _classify(pkt, es, self._video_type)
        quality = _quality(kind, rai)
        if quality is not None:
            self._seg_quality = quality
            return events
        # The picture starts beyond this packet: read on in the PES's next packets (the scan
        # calls `_read_ahead`). Until it is found, a random access point (RAI) or parameter
        # sets count as an open start.
        self._seg_quality = START_OPEN if rai or kind == _PARAM else START_NONE
        if es < PACKET:
            self._look = _Look(bytearray(pkt[es:]), pkt[3] & 0x0F, pkt, rai)
        return events

    def _read_ahead(self, data: bytes, off: int) -> None:
        """Read the open segment's first PES on in `data` from the packet at `off`, until its
        picture is known or `_LOOK_AHEAD` ES bytes were read. The packets are only peeked at
        (the scan forwards them as usual), and when the input ends first the reading goes on
        with the next input: the scan's loop over every packet pays nothing.

        The reading ends, keeping the first packet's fallback, wherever its bytes could belong
        to another picture: at the next PES start, at a lost packet sync, and at damage on the
        cut PID (a transport error, scrambling bits, a continuity counter that skips, or a
        repeated counter on a packet that is no true duplicate).
        """
        look = self._look
        if look is None:
            return
        cut_pid = self._cut_pid
        n = len(data)
        while off + PACKET <= n:
            if data[off] != SYNC:
                break
            b1 = data[off + 1]
            if ((b1 & 0x1F) << 8) | data[off + 2] == cut_pid:
                b3 = data[off + 3]
                if b1 & 0x80 or b3 & 0xC0:  # a transport error, or scrambled
                    break
                if b3 & 0x10:  # a payload (adaptation-only packets keep the counter)
                    end = off + PACKET
                    cc = b3 & 0x0F
                    if cc == look.cc:  # a true duplicate is skipped (ISO 13818-1, 2.4.3.3)
                        if data[off:end] != look.last:
                            break
                    elif cc != (look.cc + 1) & 0x0F or b1 & 0x40:  # a lost packet, or the next PES
                        break
                    else:
                        look.cc, look.last = cc, data[off:end]
                        start = off + (5 + data[off + 4] if b3 & 0x20 else 4)
                        if start < end:
                            look.es += data[start:end]
                            kind = _classify(look.es, 0, self._video_type)
                            quality = _quality(kind, look.rai)
                            if quality is not None:
                                self._seg_quality = quality
                                self._look = None
                                return
                            if len(look.es) >= _LOOK_AHEAD:
                                break
            off += PACKET
        else:
            return  # the input ended first
        self._look = None  # the fallback stays

    def _cut(self, pts: int | None, wall: float, discontinuity: bool) -> list[Event]:
        events: list[Event] = []
        if self._open:
            end = pts
            if discontinuity:
                # Across a PTS jump the next PTS says nothing about this segment: its end is
                # its first PTS plus the media it holds. Without PTS, or beyond the forced cut,
                # it is sized by wall time.
                end = self._on_segment_axis(pts)
                first = self._seg_pts
                if (
                    end is None
                    or first is None
                    or not 0 < pts_delta(first, end) <= self._forced + _FORCED_MARGIN
                ):
                    end = None
            events.append(Closed(self._seq, end, wall, self._seg_quality))
            self._seq += 1
        events.append(Opened(self._seq, pts, self._pat + self._pmt, wall, discontinuity))
        self._open = True
        self._seg_quality = START_NONE  # a cut without a cut-PID PES (`_cut_pes` learns more)
        self._look = None
        self._seg_pts = pts
        self._seg_wall = wall
        self._pusi_wall = wall
        self._quiet.clear()
        self._pending = False
        self._jumped = False
        self._media_ref = pts
        self._part_pts = pts
        self._carried = 0
        if discontinuity:  # the content may change here: learn again whether it uses RAI
            self._rai_seen = False
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
        crc_ok = _crc32(section) == 0
        if not crc_ok and not self._tolerate_bad_crc(pid, section):
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
            if crc_ok:
                self._crc_valid.add(pid)

    def _tolerate_bad_crc(self, pid: int, section: bytes) -> bool:
        """Some muxers write wrong CRCs: until a valid section arrives on this PID, accept a
        section repeated identically `_CRC_REPEATS` times (as FFmpeg tolerates bad CRCs)."""
        if pid in self._crc_valid:
            logger.debug("skipped a PSI section with a bad CRC on PID %#x", pid)
            return False
        last, count = self._bad_crc.get(pid, (b"", 0))
        count = count + 1 if section == last else 1
        self._bad_crc[pid] = (section, count)
        if count < _CRC_REPEATS:
            logger.debug("PSI section with a bad CRC on PID %#x (seen %d times)", pid, count)
            return False
        if not self._bad_crc_logged:
            self._bad_crc_logged = True
            logger.info("Accepting program tables with a bad CRC repeated %d times", count)
        return True

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
            self._look = None
        return True


class ContinuityJoiner:
    """Joins separately packaged TS segments (HLS) so each PID's continuity counter runs on.

    Some CDNs package every HLS segment on its own, each PID's counter (PAT's and PMT's too)
    restarting at 0. Joined as they are, VLC sees a counter break on every PID at every
    segment, drops its reference clock and discards late pictures (measured on a public
    channel, 2.0.2). Here the counters continue each PID's last one (ISO 13818-1, 2.4.3.3):
    an adaptation-only packet takes the previous value; the first packet with a payload of a
    PID in a segment takes the next one, and the PID's later payload packets in the segment
    the same shift. A well-formed segment then runs on exactly, and a loss or a repeated
    packet inside one stays as it came. A PID's first counter ever stays; null packets and packets with the transport error flag
    are left alone. Only the counter nibble changes, but for one flag: after
    `discontinuity()` (a real HLS discontinuity, whose counters run on too), the first packet
    carrying a PCR gets the adaptation field's discontinuity_indicator, so VLC takes the new
    time base. A segment out of packet sync passes through from where it lost sync.

    Per segment: `segment()`, then `feed()` its bytes (whole packets come back; a partial one
    is held), then `end()` (the partial packet held, as it came).
    """

    def __init__(self) -> None:
        self._last: dict[int, int] = {}  # PID -> its last counter passed on
        self._shift: dict[int, int] = {}  # PID -> what this segment adds to its counters
        self._held = b""
        self._lost = False  # out of packet sync: the rest of the segment passes through
        self._flag_pcr = False

    def segment(self) -> None:
        """A segment starts: each PID's shift is found afresh from its first packet."""
        self._shift.clear()
        self._held = b""
        self._lost = False

    def discontinuity(self) -> None:
        """A real discontinuity comes before the next bytes: flag the first PCR after it."""
        self._flag_pcr = True

    def feed(self, data: bytes) -> bytes:
        if self._lost:
            return data
        data = self._held + data if self._held else data
        whole = len(data) - len(data) % PACKET
        self._held = data[whole:]
        out = bytearray(data[:whole])
        for at in range(0, whole, PACKET):
            if out[at] != SYNC:
                self._lost = True
                rest, self._held = bytes(data[at:]), b""
                return bytes(out[:at]) + rest
            self._join(out, at)
        return bytes(out)

    def end(self) -> bytes:
        """The segment ended: the partial packet held, as it came."""
        held, self._held = self._held, b""
        return held

    def _join(self, out: bytearray, at: int) -> None:
        b1, b3 = out[at + 1], out[at + 3]
        pid = ((b1 & 0x1F) << 8) | out[at + 2]
        afc = (b3 >> 4) & 3
        if b1 & 0x80 or pid == 0x1FFF or afc == 0:  # errored, null or reserved: no counter
            return
        cc = b3 & 0x0F
        last = self._last.get(pid)
        if not afc & 1:  # adaptation only: the previous value
            cc = cc if last is None else last  # the PID's first counter stays
        else:
            shift = self._shift.get(pid)
            if shift is None:  # the first payload in the segment continues the PID's last
                shift = 0 if last is None else (last + 1 - cc) & 0x0F
                self._shift[pid] = shift
            cc = (cc + shift) & 0x0F
        if cc != b3 & 0x0F:
            out[at + 3] = (b3 & 0xF0) | cc
        self._last[pid] = cc
        # The first PCR after a discontinuity: adaptation field, length >= 1, PCR_flag set.
        if self._flag_pcr and afc & 2 and out[at + 4] and out[at + 5] & 0x10:
            out[at + 5] |= 0x80
            self._flag_pcr = False


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


def _quality(kind: int, rai: bool) -> StartQuality | None:
    """The start quality a `_classify` result gives (with the PES's random access indicator);
    None while the picture is still unknown."""
    if kind == _IDR:
        return START_KEY
    if kind == _OPEN or (kind == _INTRA and rai):
        return START_OPEN
    if kind == _INTRA or kind == _NOT_KEY:
        return START_NONE
    return None


def _classify(pkt: bytes | bytearray, start: int, video_type: int | None) -> int:
    """What the picture starting in these ES bytes (from `start` to the end) is.

    `_IDR`: an IDR picture (H.264 5, HEVC 19-20) or an MPEG-1/2 I picture in a closed GOP.
    `_OPEN`: another intra picture, whose leading pictures may reference the previous GOP (an
    H.264 non-IDR picture of I or SI slices only, slice_type 7 or 9, which open-GOP encoders
    use; HEVC CRA 21 or BLA 16-18; an MPEG-1/2 I picture otherwise). `_INTRA`: an H.264
    non-IDR picture whose first slice is I or SI (slice_type 2 or 4): its other slices may be
    P. `_PARAM`: a parameter set (H.264 SPS 7, HEVC VPS 32, MPEG-1/2 sequence header) without
    a picture in these bytes. `_NOT_KEY`: the first picture is not intra (H.264 1-4 with P or
    B slices, HEVC 0-9, MPEG-1/2 P or B). `_UNDECIDED`: neither, or the first slice header is
    cut off by the end of the bytes.
    """
    size = len(pkt)
    parameter_set = closed_gop = False
    i = pkt.find(_START_CODE, start)
    while 0 <= i < size - 3:
        code = pkt[i + 3]
        if video_type == _H264:
            nal_type = code & 0x1F
            if nal_type == 5:
                return _IDR
            if nal_type == 1:  # the first slice decides
                slice_type = _h264_slice_type(pkt, i + 4)
                if slice_type is None:
                    return _UNDECIDED
                if slice_type % 5 not in (2, 4):
                    return _NOT_KEY
                return _OPEN if slice_type >= 5 else _INTRA
            if 2 <= nal_type <= 4:  # data partitions: never intra-only
                return _NOT_KEY
            if nal_type == 7:
                parameter_set = True
        elif video_type == _HEVC:
            nal_type = (code >> 1) & 0x3F
            if 19 <= nal_type <= 20:
                return _IDR
            if 16 <= nal_type <= 21:
                return _OPEN
            if nal_type <= 9:
                return _NOT_KEY
            if nal_type == 32:
                parameter_set = True
        elif code == 0x00:  # MPEG-1/2 picture header: the first picture decides
            if i + 6 > size:
                break
            coding_type = (pkt[i + 5] >> 3) & 7  # after the 10-bit temporal_reference
            if coding_type == 1:
                return _IDR if closed_gop else _OPEN
            if coding_type in (2, 3):
                return _NOT_KEY
            return _UNDECIDED
        elif code == 0xB8:  # MPEG-1/2 GOP header: closed_gop follows the 25-bit time code
            closed_gop = i + 8 <= size and bool(pkt[i + 7] & 0x40)
        elif code == 0xB3:  # MPEG-1/2 sequence header
            parameter_set = True
        i = pkt.find(_START_CODE, i + 3)
    return _PARAM if parameter_set else _UNDECIDED


def _h264_slice_type(pkt: bytes | bytearray, at: int) -> int | None:
    """The slice_type of the H.264 slice header at `at` (0 P, 1 B, 2 I, 3 SP, 4 SI; plus 5
    when every slice of the picture has that type); None when undecidable.

    Reads first_mb_in_slice and slice_type, two Exp-Golomb codes, from up to 6 bytes: enough
    for any legal first_mb_in_slice (level 6.2 allows 139264 macroblocks). Such codes never
    hold the 22 zero bits an emulation prevention byte needs. None when the bytes end first,
    or for a value that is no slice type.
    """
    head = pkt[at : at + 6]
    bits = int.from_bytes(head, "big")
    left = 8 * len(head)  # bits not read yet
    value = 0
    for _ in range(2):
        zeros = 0
        while left and not bits >> (left - 1) & 1:
            zeros += 1
            left -= 1
        if left <= zeros:  # truncated
            return None
        left -= 1 + zeros
        value = (1 << zeros) - 1 + (bits >> left & ((1 << zeros) - 1))
    return value if value <= 9 else None


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
