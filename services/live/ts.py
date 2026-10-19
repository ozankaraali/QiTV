"""MPEG-TS scanner and keyframe segmenter for the live pipeline (no remuxing).

The segmenter cuts a raw 188-byte transport stream into keyframe-aligned segments. Packets pass
through byte for byte, but for two repairs: a packet cut short is dropped whole, and a PCR that
steps back without the discontinuity_indicator gets it (see `Segmenter._scan`). Each segment
is announced with copies of the latest PAT and PMT packets, which the consumer prepends so that
VLC can start playback at any segment. Its close says how cleanly playback starts there
(`Closed.start_quality`): at an IDR picture, at an intra picture whose leading pictures may
reach behind it (open GOP), or without a keyframe (a forced cut).

Pure logic: no Qt and no I/O, and the wall clock is injected.

Tests: `uv run python -m unittest discover -s tests -p test_ts_segmenter.py` (the module-path
form cannot import `ts_fixtures`).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
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
# A PCR never steps back on its PID: one more than this behind the last starts a new clock,
# which VLC takes only with the discontinuity_indicator (more than any PCR jitter).
_PCR_BACK_TICKS = _CLOCK // 10
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
# RepeatTrimmer (a restart that re-sends media inside one connection):
_TRIM_BURST_RATE = 1.25  # a burst has settled once a second brings at most this much media
_TRIM_HOLD_S = 4.0  # a new run is held at most this long: VLC plays from its cushion meanwhile
_TRIM_HOLD_BYTES = 48 * 1024 * 1024  # ... and at most this much: a 15 s burst of 25 Mbit/s 4K
_TRIM_MARGIN_S = 0.25  # R is taken this much short: a frame or two repeated beats new ones lost
_TRIM_MIN_S = 2.0  # a smaller R is a backlog that made up its silence (a control's 0.6 s)
_TRIM_RUNS = 2  # a repeat reaches this many runs back: one upstream's crossed another's 9 s run
_TRIM_NO_BURST_S = 0.5  # a run held this long that brought no burst is let go (a splice at 1x)
_TRIMS_KEPT = 64  # the latest trims kept for a look (a memory bound)
# ClockJoiner: a junction's packets wait at most this many for its first picture (a few
# hundred milliseconds at 25 Mbit/s); then they pass on as they are.
_CLOCK_PENDING_PACKETS = 4096
# ClockJoiner's forward jumps (a source whose clock leaps ahead inside one connection or one run
# of HLS segments: measured on a public news channel, every PID 13 s ahead between two pictures
# of one 10 s segment, which VLC 3 sat out as a 15 s still, its clock gap limit being 60 s):
# - a picture whose DTS (its PTS without one) steps more than `JUMP_S` past the last picture's,
#   and more than twice the step before it (a slideshow's stills seconds apart are its cadence),
#   jumped when arrival leaves more than `JUMP_S` of the step unexplained: the pictures' media
#   less the arrival (raw TS: the wall clock; HLS: each segment's EXTINF as it begins) is
#   measured against its highest within this much arrival (a raw TS burst of 10 s every 10 s,
#   or a whole HLS segment's EXTINF, raised to 1.5 times it for longer segments);
_JUMP_WINDOW_S = 15.0
# - no step is judged before the pictures went on this much since the window began (the start,
#   a junction, a discontinuity, a step back): their cadence is known by then, and the window
#   holds a whole 10 s HLS segment or raw TS burst to measure arrival against (a still every
#   5 s without sound, in one burst or in 6 s HLS segments, read as a 5 s jump at its first);
_JUMP_HISTORY_TICKS = 10 * _CLOCK
# - any other timestamp that lands more than `JUMP_S` past where its own last one leads (a PCR,
#   sound, a picture's PTS ahead of its DTS) holds what follows for a picture that jumps, while
#   the pictures advance at most this much (the capture's PTS leapt three pictures, 0.08 s,
#   before its DTS);
_JUMP_WAIT_TICKS = 2 * _CLOCK
# - for this much of the pictures' media after the jump, each timestamp takes the old offset or
#   the new, whichever lands nearer its own PID's last (pictures decoded before the jump but
#   shown after it; sound muxed in blocks of 7 s that crossed 2.3 s after the pictures), and
#   then the new offset alone.
_JUMP_SETTLE_TICKS = 10 * _CLOCK
# A longer step is a clock of its own, which VLC 3 takes by itself (its clock gap limit) as the
# segmenter's timeline does: it passes as it came.
_JUMP_MAX_TICKS = 60 * _CLOCK
_PCR, _PTS, _DTS = 0, 1, 2  # a timestamp's kind: each PID's PCRs, PTSs and DTSs run on apart
_PASS, _LEAP, _JUMP, _REWRITE = 0, 1, 2, 3  # what `ClockJoiner._watched` makes of a packet
# ReconnectTrimmer
_REJOIN_TAIL_BYTES = 8 * 1024 * 1024  # output kept to read the last PTS and pictures from
_REJOIN_WINDOW_TICKS = 60 * _CLOCK  # a new connection's PTS this near the old's: one timebase
_REJOIN_KEY_WAIT_TICKS = 2 * _CLOCK  # past the old connection's end, a keyframe is waited for
_REJOIN_WAIT_PACKETS = 4 * _CLOCK_PENDING_PACKETS  # ... with this many packets held at most
# LoopTrimmer (a provider that replays its last seconds while its own source is down):
_LOOP_MIN_S = 2.0  # a replay this long, of pictures at least this far back, is a loop ...
_LOOP_PICTURES = 10  # ... of at least this many distinct pictures (a still repeated is none)
_LOOP_WINDOW_S = 60.0  # the pictures a replay's copy is looked for in ...
_LOOP_WINDOW_PICTURES = 4096  # ... at most this many (60 s at 60 fps fit)
_LOOP_WINDOW_SOUND = 8192  # the latest sound PES (every other PES) kept to tell repeated sound
_LOOP_SOUND_SHARE = 0.9  # a replay's sound repeats too: at least this share of its PES, ...
_LOOP_SOUND_NEW = 4  # ... and no replay once this many of its PES are new
_LOOP_EARLY_TICKS = _CLOCK  # sound this far before a loop's first picture still passes
_LOOP_HOLD_BYTES = 16 * 1024 * 1024  # a run judged is held at most this much (2 s of 64 Mbit/s)
_LOOP_SOUND_WAIT = 64  # other PES that begin while a picture is read: past it, the video stalled
# A loop dropped this long plays again (from a keyframe): a channel that is itself a short loop
# (an off-air card, a promo) must not freeze for good, while the provider outages measured lasted
# 12 s and 90 s.
_LOOP_GIVE_UP_S = 180.0


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
        self._flushed: list[Event] = []  # the old input's last packet, after `discontinuity()`
        self._pcrs: dict[int, int] = {}  # the latest PCR base by PID
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
    def video_type(self) -> int | None:
        """The video PID's stream type from the PMT (H.264 0x1B, HEVC 0x24, ...)."""
        return self._video_type

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

        The input starts afresh: the old input's last whole packet is taken as it came (its
        events come with the next `feed` or `finish`), a partial packet it left is dropped, and
        the next packet start must be proven again, so no packet joins the two inputs.
        """
        self._flushed += self._flush()
        self._latch()
        self._media_ref = None  # measure the new content's age from its first PTS
        self._look = None  # the rest of the open segment's first PES never comes
        self._pos += len(self._buf)
        self._buf = b""
        self._searching = True

    def feed(self, chunk: bytes) -> list[Event]:
        """Consume input bytes; raises `Unsupported` for input that cannot be segmented.

        A whole packet at the end of the input waits for the next input's first byte: only a
        sync byte there proves it was not cut short (see `_scan`)."""
        events, self._flushed = self._flushed, []
        if not chunk:
            return events
        if type(chunk) is not bytes:
            chunk = bytes(chunk)
        wall = self._tick()
        if self._probe is not None:
            self._check_probe(chunk)
        data = self._buf + chunk if self._buf else chunk
        if len(data) < PACKET:
            self._buf = data
            return events
        self._scan(data, wall, events)
        return events

    def finish(self) -> list[Event]:
        """Close the open segment at the end of the input: the last packet goes first (it
        waited for a next input), then the close."""
        events, self._flushed = self._flushed + self._flush(), []
        if not self._open:
            return events
        self._open = False
        pts = self._on_segment_axis(self._latest_pts)
        events.append(Closed(self._seq, pts, self._tick(), self._seg_quality))
        self._seq += 1
        return events

    def _flush(self) -> list[Event]:
        """The input ends: scan the packet held for the next input's sync byte as its last."""
        events: list[Event] = []
        if len(self._buf) >= PACKET and not self._searching:
            self._scan(self._buf, self._wall, events, last=True)
        return events

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

    def _scan(self, data: bytes, wall: float, events: list[Event], last: bool = False) -> None:
        """Scan whole packets. A packet passes once the next one's sync byte follows it, so
        the input's last packet waits for the next input (`last`: the input ended, it passes
        as it is). A packet the next one does not follow, while a proven packet start (three
        sync bytes 188 apart) lies within its 188 bytes, was cut short (a provider splicing
        two streams): it is dropped whole, never passed on with the next packet's first bytes
        as its tail. A PCR that steps back by more than `_PCR_BACK_TICKS` without the
        discontinuity_indicator gets it, so VLC takes the new clock (ContinuityJoiner does the
        same at an HLS discontinuity). The segmenter rewrites no timestamp (for raw TS,
        `ClockJoiner` re-bases a restarted run's before it)."""
        if self._start_wall is None:
            self._start_wall = wall
        # The cut PID went quiet while other packets flow: cut at the next PES start of any PID.
        silent = self._open and wall - self._pusi_wall >= self._forced_s
        n = len(data)
        off = 0
        run = 0 if self._open else -1  # start of the packets not yet emitted as Data
        searching = self._searching
        scrambled = self._scrambled_run

        def emit(start: int, stop: int) -> None:
            if 0 <= start < stop:  # (`data` as it is then: a flagged PCR replaces it)
                events.append(Data(data[start:stop]))

        # A reading of the open segment's first PES that the previous input's end interrupted.
        # (No scan starts out of sync while one goes on: `discontinuity()` ends it, and a sync
        # lost within an input ends it as the reading reaches that packet first.)
        if self._look is not None:
            self._read_ahead(data, 0, last)
        cut_pid = self._cut_pid
        pmt_pid = self._pmt_pid
        while off + PACKET <= n:
            if searching or data[off] != SYNC:
                emit(run, off)
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
            if end == n and not last:
                break  # the next input's first byte tells whether this packet is whole
            if end < n and data[end] != SYNC:
                # Within the packet's bytes, three sync bytes 188 apart from where a candidate
                # can still be checked; until then the packet waits for more input.
                found = _find_sync(data, off + 1)
                if found < 0 and n - 2 * PACKET < end and not last:
                    break
                if 0 <= found < end:
                    logger.debug("dropped a packet cut short after %d bytes", found - off)
                    emit(run, off)
                    off = found
                    run = off if self._open else -1
                    continue
                # A whole packet with bytes out of sync after it: those are skipped next.
            b1 = data[off + 1]
            pid = ((b1 & 0x1F) << 8) | data[off + 2]
            b3 = data[off + 3]
            if b3 & 0x20 and data[off + 4] >= 7 and data[off + 5] & 0x10 and not b1 & 0x80:
                pcr = _pcr_base(data, off + 6)
                previous = self._pcrs.get(pid)
                self._pcrs[pid] = pcr
                if (
                    previous is not None
                    and pts_delta(previous, pcr) < -_PCR_BACK_TICKS
                    and not data[off + 5] & 0x80
                ):
                    logger.debug("flagged a PCR %.2f s back", -pts_delta(previous, pcr) / _CLOCK)
                    # Once per new clock: a copy with that one bit set.
                    data = data[: off + 5] + bytes([data[off + 5] | 0x80]) + data[off + 6 :]
            if pid == cut_pid:
                if not b1 & 0x80:  # with the transport error flag the header is untrusted
                    if b3 & 0xC0:
                        scrambled += 1
                        self._scrambled_total += 1
                        self._check_scrambled(scrambled, wall)
                    else:
                        scrambled = 0
                        if b1 & 0x40:
                            silent = False
                            cut = self._on_pusi(data[off:end], wall)
                            if cut:
                                emit(run, off)
                                events.extend(cut)
                                run = off
                                if self._look is not None:
                                    self._read_ahead(data, end, last)
            else:
                if (
                    (b1 & 0xC0) == 0x40  # a PES or section start without a transport error
                    and self._open
                    and (silent or self._media_quiet(pid, data[off:end]))
                ):
                    silent = False
                    logger.debug("PID %#x went quiet; cutting at PID %#x", cut_pid, pid)
                    emit(run, off)
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
        emit(run, off)
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

    def _read_ahead(self, data: bytes, off: int, last: bool = False) -> None:
        """Read the open segment's first PES on in `data` from the packet at `off`, until its
        picture is known or `_LOOK_AHEAD` ES bytes were read. The packets are only peeked at
        (the scan forwards them as usual), and when the input ends first the reading goes on
        with the next input: the scan's loop over every packet pays nothing. As in the scan, a
        packet counts once the next one's sync byte follows it (the input's last waits for the
        next input, unless it is the `last`).

        The reading ends, keeping the first packet's fallback, wherever its bytes could belong
        to another picture: at the next PES start, at a lost packet sync or a packet the next
        one does not follow, and at damage on the cut PID (a transport error, scrambling bits,
        a continuity counter that skips, or a repeated counter on a packet that is no true
        duplicate).
        """
        look = self._look
        if look is None:
            return
        cut_pid = self._cut_pid
        n = len(data)
        while off + PACKET < n or (last and off + PACKET == n):
            if data[off] != SYNC or (off + PACKET < n and data[off + PACKET] != SYNC):
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
    is held), then `end()` (the partial packet held, as it came). `counters` are each PID's
    last counter already passed on before the first segment (a join in mid-stream).
    """

    def __init__(self, counters: dict[int, int] | None = None) -> None:
        self._last: dict[int, int] = dict(counters or {})  # PID -> its last counter passed on
        self._shift: dict[int, int] = {}  # PID -> what this segment adds to its counters
        self._held = b""
        self._lost = False  # out of packet sync: the rest of the segment passes through
        self._flag_pcr = False

    @property
    def counters(self) -> dict[int, int]:
        """Each PID's last counter passed on (a copy)."""
        return dict(self._last)

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


@dataclass(frozen=True, slots=True)
class Rejoin:
    """A reconnect `ReconnectTrimmer` found in the source's own timebase, and what it dropped.

    `offset` is the input offset where the new connection began; `step_s` how far its first
    picture lies from the furthest one before it (negative: it re-sent that much); `dropped`
    the input ranges left out ((offset, bytes)).
    """

    offset: int
    step_s: float
    dropped: tuple[tuple[int, int], ...]

    @property
    def dropped_bytes(self) -> int:
        return sum(size for _, size in self.dropped)


class ReconnectTrimmer:
    """Drops what a raw TS source sends again after a reconnect, in the timebase it had.

    One provider stalls for 9-14 s every 30-60 s; the helper's stall timeout reconnects, and
    the new connection starts at the provider's last keyframe 2-4 s back with the old
    connection's timestamps, so the ring held that stretch twice and it played twice. After
    `reconnect()` the new connection's packets wait for its first video PES with a PTS
    (`_CLOCK_PENDING_PACKETS` at most). It re-sends when that PTS lies at most
    `_REJOIN_WINDOW_TICKS` before the furthest video PTS passed on (modulo 2**33) and its first
    packet's picture bytes are those of the picture passed on with that PTS (a restart's new
    clock may land there too, its picture never matches). Then the picture is dropped up to
    the furthest PTS; past it, it goes on at a keyframe that comes within
    `_REJOIN_KEY_WAIT_TICKS` (the pictures before it dropped too), else at the first picture
    past it (a long GOP or intra refresh: the decoder goes on from the old connection's
    pictures, the same ones), the packets after that picture held meanwhile. Every other PES
    PID (the sound) goes on at its first PES after that PID's furthest PTS, a PCR on a PID of
    its own only once past that PID's furthest one, and the tables pass. What a cut
    connection passes goes through `ContinuityJoiner` until the next reconnect, so the
    counters run on (about a microsecond a packet). A new connection that resumes after the
    old one, one in another timebase (a reset: `RepeatTrimmer`'s and the segmenter's, as
    before), one that re-sends further back than the tail holds, and a program without a
    picture pass untouched, as does everything before the first reconnect: the PTS, PCRs,
    pictures and counters before a reconnect are read from the last `_REJOIN_TAIL_BYTES`
    passed on, so bytes pass as they come, at no cost per packet. The program's video PID and
    type are the caller's (the segmenter's).
    """

    def __init__(self, *, program: Callable[[], tuple[int | None, int | None]]) -> None:
        self._program = program
        self._tail: deque[bytes] = deque()  # the latest output, `_REJOIN_TAIL_BYTES` or more
        self._tail_size = 0
        self._joiner: ContinuityJoiner | None = None  # for a cut connection
        self._pos = 0  # the input offset of the next byte fed
        self._armed = False  # a reconnect came: its connection is judged
        self._buf = b""  # while armed: bytes not yet judged (a partial packet)
        self._searching = False
        self._pending: list[tuple[int, bytes]] | None = None  # (offset, packet) before a picture
        self._video: int | None = None
        self._video_type: int | None = None
        self._top: dict[int, int] = {}  # each PES PID's furthest PTS before the reconnect
        self._counters: dict[int, int] = {}  # each PID's last counter before it
        self._pcr_top: dict[int, int] = {}  # each PID's furthest PCR before it
        self._pictures: dict[int, bytes] = {}  # the picture bytes of each video PES start, by PTS
        self._keep: dict[int, bool] = {}  # PES PIDs: whether the PES being read passes
        self._resumed: set[int] = set()  # PIDs that pass for good
        # From the first picture past the furthest PTS (its PTS: `_past`) while a keyframe is
        # waited for: (offset, packet, passes; None: a video packet, decided by the wait).
        self._waiting: list[tuple[int, bytes, bool | None]] | None = None
        self._past = 0
        self._after = 0  # packets judged since the picture resumed
        self._at = 0  # where the connection began
        self._step = 0.0
        self._dropped: list[tuple[int, int]] = []
        self.rejoins: deque[Rejoin] = deque(maxlen=_TRIMS_KEPT)

    def feed(self, chunk: bytes) -> bytes:
        """Take input bytes; return what passes on."""
        if not self._armed:
            self._pos += len(chunk)
            return self._out(chunk)
        base = self._pos - len(self._buf)
        self._pos += len(chunk)
        data = self._buf + chunk if self._buf else chunk
        kept: list[bytes] = []
        n = len(data)
        off = 0
        searching = self._searching
        while off + PACKET <= n and self._armed:
            if searching or data[off] != SYNC:
                found = _find_sync(data, off)
                if found < 0:
                    junk = max(off, n - 2 * PACKET)  # a later sync may still start in the tail
                    self._drop(base + off, junk - off)
                    off, searching = junk, True
                    break
                self._drop(base + off, found - off)
                off, searching = found, False
                continue
            self._judge(data[off : off + PACKET], base + off, kept)
            off += PACKET
        if self._armed:
            self._buf, self._searching = data[off:], searching
        else:
            kept.append(data[off:])
            self._buf = b""
        return self._out(b"".join(kept))

    def reconnect(self) -> bytes:
        """A reconnect comes next: what is held passes on as it came (the segmenter drops a
        partial packet), and the next connection is judged against what passed on."""
        out = self._release()
        self._arm()
        return out

    def finish(self) -> bytes:
        """The input ended: what is held passes on as it came."""
        return self._release()

    def _release(self) -> bytes:
        kept: list[bytes] = []
        if self._waiting is not None:
            self._end_wait(True, kept)
        held = b"".join(kept) + b"".join(p for _, p in self._pending or ()) + self._buf
        self._pending, self._buf = None, b""
        if self._armed:
            self._close()
        out = self._out(held)
        if self._joiner is not None:  # the next connection's counters are its own again
            out += self._joiner.end()
            self._joiner = None
        return out

    def _out(self, data: bytes) -> bytes:
        """Pass `data` on (through the joiner once one runs), keeping the tail."""
        if self._joiner is not None and data:
            data = self._joiner.feed(data)
        if data:
            self._tail.append(data)
            self._tail_size += len(data)
            while self._tail_size - len(self._tail[0]) >= _REJOIN_TAIL_BYTES:
                self._tail_size -= len(self._tail.popleft())
        return data

    def _arm(self) -> None:
        video, video_type = self._program()
        times = _last_times(b"".join(self._tail), video)
        self._top, self._counters, self._pictures, self._pcr_top = times
        self._armed = video is not None and video in self._top
        self._video, self._video_type = video, video_type
        self._pending = [] if self._armed else None
        self._searching = True
        self._keep, self._resumed, self._waiting = {}, set(), None
        self._after = 0
        self._at, self._step, self._dropped = self._pos, 0.0, []

    def _judge(self, pkt: bytes, at: int, kept: list[bytes]) -> None:
        """Hold a packet until the first picture's PTS tells the timebase, then pass or drop
        it (and those held)."""
        pending = self._pending
        if pending is not None:
            pending.append((at, pkt))
            if (pkt[1] & 0xC0) == 0x40 and ((pkt[1] & 0x1F) << 8 | pkt[2]) == self._video:
                pts = _pes_times(pkt, 0)[0]
                if pts is not None:
                    self._decide(pts, pending, kept)
            elif len(pending) >= _CLOCK_PENDING_PACKETS:  # no picture in reach: as it came
                self._pending = None
                self._armed = False
                kept.extend(packet for _, packet in pending)
            return
        self._cut(pkt, at, kept)
        if self._video in self._resumed:
            self._after += 1
            dropping = any(not keep for pid, keep in self._keep.items() if pid not in self._resumed)
            if not dropping or self._after >= _CLOCK_PENDING_PACKETS:
                self._close()

    def _decide(self, pts: int, pending: list[tuple[int, bytes]], kept: list[bytes]) -> None:
        assert self._video is not None
        self._pending = None
        step = pts_delta(self._top[self._video], pts)
        self._step = step / _CLOCK
        first = pending[-1][1]
        if (
            abs(step) > _REJOIN_WINDOW_TICKS
            or step <= 0
            and self._pictures.get(pts) != first[_pes_start(first)[2] :]
        ):  # another timebase (or a re-send beyond the tail): not this stage's
            self._armed = False
            kept.extend(packet for _, packet in pending)
            return
        if step > 0:
            self._resumed.add(self._video)  # the picture goes on after the old one's
            judged = [self._passes(packet) for _, packet in pending]
            if all(judged):
                self._armed = False  # it resumes where the old connection ended: untouched
                kept.extend(packet for _, packet in pending)
                return
            self._joiner = ContinuityJoiner(self._counters)
            for (at, packet), keep in zip(pending, judged):
                self._place(packet, at, keep, kept)
            return
        self._joiner = ContinuityJoiner(self._counters)
        for at, packet in pending:
            self._cut(packet, at, kept)

    def _cut(self, pkt: bytes, at: int, kept: list[bytes]) -> None:
        """Pass or drop a packet of a connection that re-sends (or hold it: `_wait`)."""
        if self._waiting is not None:
            self._wait(pkt, at, kept)
            return
        keep = self._passes(pkt)
        if self._waiting is not None:  # this picture, the first past the furthest, begins it
            self._waiting.append((at, pkt, None))
        else:
            self._place(pkt, at, keep, kept)

    def _passes(self, pkt: bytes) -> bool:
        """Whether a packet of the new connection passes: other PES from their first after
        their PID's furthest PTS, a PCR of its own PID past that PID's furthest, everything
        else (the tables, null packets); the picture from a keyframe after its furthest PTS
        (the first picture after it begins the wait for one: `_wait`)."""
        b1 = pkt[1]
        pid = ((b1 & 0x1F) << 8) | pkt[2]
        if pid in self._resumed or b1 & 0x80 or pid == 0x1FFF:
            return True
        if b1 & 0x40:
            if not _starts_pes(pkt, 0):
                return True  # a section: the tables pass
            pts = _pes_times(pkt, 0)[0]
            top = self._top.get(pid)
            after = pts is not None and top is not None and pts_delta(top, pts) > 0
            if pid == self._video:
                if after:
                    assert pts is not None
                    rai, _, es = _pes_start(pkt)
                    if not _trim_key(pkt, rai, es, self._video_type):
                        self._waiting, self._past = [], pts  # `_judge` holds it from here
                        return False
            elif top is None:
                after = True  # a PID without a PTS before: nothing to repeat
            elif pts is None:
                after = self._keep.get(pid, False)
            if after:
                self._resumed.add(pid)
                return True
            self._keep[pid] = False
            return False
        if pid not in self._top and pkt[3] & 0x20 and pkt[4] >= 7 and pkt[5] & 0x10:
            top = self._pcr_top.get(pid)  # a PCR of its own: the old connection's clock again?
            if top is not None and self._video not in self._resumed:
                return pts_delta(top, _pcr_base(pkt, 6)) > 0
        return self._keep.get(pid, pid not in self._top)  # as its PES (a section passes)

    def _wait(self, pkt: bytes, at: int, kept: list[bytes]) -> None:
        """Past the furthest PTS: hold the packets until a keyframe comes in time (the
        pictures before it go) or none did (from the first picture past it on, they pass)."""
        waiting = self._waiting
        assert waiting is not None and self._video is not None
        pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
        if pid != self._video:
            waiting.append((at, pkt, self._passes(pkt)))
        else:
            pts = _pes_times(pkt, 0)[0] if (pkt[1] & 0xC0) == 0x40 else None
            if pts is not None:
                rai, _, es = _pes_start(pkt)
                after = pts_delta(self._top[self._video], pts) > 0
                if after and _trim_key(pkt, rai, es, self._video_type):
                    self._end_wait(False, kept)  # a keyframe in time: the pictures before go
                    kept.append(pkt)
                    return
                if pts_delta(self._past, pts) > _REJOIN_KEY_WAIT_TICKS:
                    self._end_wait(True, kept)  # none in time: from the first past it on
                    kept.append(pkt)
                    return
            waiting.append((at, pkt, None))
        if len(waiting) >= _REJOIN_WAIT_PACKETS:
            self._end_wait(True, kept)

    def _end_wait(self, pictures: bool, kept: list[bytes]) -> None:
        """The wait ends: the held packets pass or go as judged, the pictures as `pictures`
        says; the picture passes from here."""
        waiting, self._waiting = self._waiting or [], None
        assert self._video is not None
        self._resumed.add(self._video)
        for at, packet, keep in waiting:
            self._place(packet, at, pictures if keep is None else keep, kept)

    def _place(self, pkt: bytes, at: int, keep: bool, kept: list[bytes]) -> None:
        if keep:
            kept.append(pkt)
        else:
            self._drop(at, PACKET)

    def _drop(self, at: int, size: int) -> None:
        if size <= 0:
            return
        dropped = self._dropped
        if dropped and dropped[-1][0] + dropped[-1][1] == at:
            dropped[-1] = (dropped[-1][0], dropped[-1][1] + size)
        else:
            dropped.append((at, size))

    def _close(self) -> None:
        """The connection is judged: from here its bytes pass as they come."""
        self._armed = False
        if self._joiner is None and not self._dropped:
            return
        rejoin = Rejoin(self._at, self._step, tuple(self._dropped))
        self.rejoins.append(rejoin)
        logger.info(
            "A reconnect resumed %.1f s from where the old connection was: %d bytes dropped.",
            rejoin.step_s,
            rejoin.dropped_bytes,
        )


def _last_times(
    data: bytes, video: int | None
) -> tuple[dict[int, int], dict[int, int], dict[int, bytes], dict[int, int]]:
    """Each PES PID's furthest PTS (of its latest run: a step back over `_REORDER_TICKS`
    starts it over), each PID's last continuity counter, the picture bytes in the first packet
    of each `video` PES by PTS, and each PID's furthest PCR (as the PTS), in `data` from its
    first sync."""
    top: dict[int, int] = {}
    counters: dict[int, int] = {}
    pictures: dict[int, bytes] = {}
    pcrs: dict[int, int] = {}
    n = len(data)
    off = _find_sync(data, 0)
    while 0 <= off and off + PACKET <= n:
        if data[off] != SYNC:
            off = _find_sync(data, off)
            continue
        b1, b3 = data[off + 1], data[off + 3]
        if not b1 & 0x80:
            pid = ((b1 & 0x1F) << 8) | data[off + 2]
            if b3 & 0x10 and pid != 0x1FFF:
                counters[pid] = b3 & 0x0F
            if b3 & 0x20 and data[off + 4] >= 7 and data[off + 5] & 0x10:
                pcr = _pcr_base(data, off + 6)
                last = pcrs.get(pid)
                if last is None or not -_REORDER_TICKS <= pts_delta(last, pcr) <= 0:
                    pcrs[pid] = pcr
            if b1 & 0x40:
                pts = _pes_times(data, off)[0]
                if pts is not None:
                    last = top.get(pid)
                    if last is None or not -_REORDER_TICKS <= pts_delta(last, pts) <= 0:
                        top[pid] = pts
                    if pid == video:
                        pkt = data[off : off + PACKET]
                        pictures[pts] = pkt[_pes_start(pkt)[2] :]
        off += PACKET
    return top, counters, pictures, pcrs


@dataclass(frozen=True, slots=True)
class Trim:
    """A new run `RepeatTrimmer` held after a timestamp restart, and what became of it.

    `offset` is the input offset where the run began (its first PCR or PES that stepped back,
    or the flagged packets right before it); `held_at` and `released_at` the clock then.
    `excess_s` is R (None without media before the restart to measure from). `dropped_s` is
    the media left out from the run's start (to a keyframe), `dropped` the input ranges left
    out ((offset, bytes)), both empty when it went on untouched. `reason`: "dropped", "small"
    (R below `_TRIM_MIN_S`), "no keyframe" (none in reach), "no burst" (none came: released
    early), "first" (no media before it to measure from), "reconnect" or "end" (released
    before its burst settled, untouched).
    """

    offset: int
    held_at: float
    released_at: float
    excess_s: float | None
    dropped_s: float
    dropped: tuple[tuple[int, int], ...]
    reason: str

    @property
    def dropped_bytes(self) -> int:
        return sum(size for _, size in self.dropped)


class RepeatTrimmer:
    """Drops what a raw TS source sends twice when it restarts its clock in one connection.

    One provider's restream restarts its muxer every 15-60 s without closing the connection:
    PTS and PCR start over, and after a silence of 1-3 s the new run arrives in a burst that
    re-sends seconds already sent (byte for byte, or the same scenes from another encoder), so
    the live edge runs ahead of the wall clock by up to 25 s a minute.

    A new run may begin where a PCR steps back by more than `_PCR_BACK_TICKS` on its PID, or a
    PES of any PID steps back by more than `_REORDER_TICKS` (with the flagged packets right
    before it, its first tables): from there it is held. It is a restart once both the PCR and
    the video PTS stepped back from the clock before it; if either goes on instead, it was
    none and the bytes pass on as they came. A restart is held
    until its burst settled (a second brought at most `_TRIM_BURST_RATE` s of media),
    `_TRIM_HOLD_S` passed or `_TRIM_HOLD_BYTES` are held, all judged as input arrives, and
    while a source sends nothing on the caller's `tick()`. R, how far the
    restart moved the media ahead of the wall clock, is media minus clock where the burst
    settled (else at the release), less media minus clock where the old run last brought media
    (before the silence: a burst that only makes up its silence is R 0), less `_TRIM_MARGIN_S`.
    A run without media before it to measure from is released at once, and so is one whose
    first `_TRIM_NO_BURST_S` brought no burst (a splice at real time). When R is at least
    `_TRIM_MIN_S`, the run is dropped up to its last keyframe at or before R, and no further
    than the media of the last `_TRIM_RUNS` runs (a repeat reaches no further back): a short
    repeat stays rather than new media go, so a run with a keyframe every 10 s may keep most of
    it. From that keyframe the picture goes on, the sound and every other PES from its PES at
    or after the keyframe's time (wherever it lies in the bytes), and the tables from their
    next section.

    Every restart held passes on as a piece of its own: a new run starts between two pieces
    (`ClockJoiner` re-bases it there, the segmenter cuts it a segment). With `join_counters`
    (when no `ClockJoiner` follows) what passes on after a drop goes through `ContinuityJoiner`:
    counters run on and the first PCR gets the discontinuity_indicator. A reconnect
    (`discontinuity()`), a stall and backlog without a restart, and a forward jump pass
    untouched. Whole packets are judged as the segmenter does (a read's last packet waits for
    the next read), and a packet cut short is dropped here already (`cut_short`). Pure logic:
    the clock and the program's video PID and type (the segmenter's) are the caller's.
    """

    def __init__(
        self,
        *,
        program: Callable[[], tuple[int | None, int | None]],
        clock: Callable[[], float] = time.monotonic,
        join_counters: bool = True,
    ) -> None:
        self._program = program
        self._clock = clock
        self._join_counters = join_counters
        self._buf = b""
        self._pos = 0  # the input offset of _buf[0]
        self._searching = True  # the next packet start must be proven by three sync bytes
        self._pcrs: dict[int, int] = {}  # each PID's latest PCR base
        self._pts: dict[int, int] = {}  # each PES PID's latest PTS
        self._frame = _CLOCK // 25  # the shortest forward PTS step seen: a frame
        # The run being read, passed on or held: its first and furthest video PTS, after
        # `_done` seconds of the runs before it.
        self._first: int | None = None
        self._top: int | None = None
        self._done = 0.0
        # Media minus clock where the run passed on last brought media (its furthest PTS).
        self._ahead: float | None = None
        self._noted: int | None = None  # the furthest PTS `_ahead` was taken at
        self._counters: dict[int, int] = {}  # each PID's last counter passed on
        self._joiner: ContinuityJoiner | None = None  # with join_counters, from the first drop
        # The held run: its bytes from where it began, where that was in the input, and when.
        self._held: bytearray | None = None
        self._held_pos = 0
        self._held_at = 0.0
        self._held_counters: dict[int, int] = {}
        # Until it is a restart: the clock before it (each PID's PCR, the video PTS) and what
        # stepped back so far.
        self._confirmed = False
        self._ref_pcrs: dict[int, int] = {}
        self._ref_video: int | None = None
        self._pcr_back = False
        self._video_back: int | None = None  # the new run's first video PTS, once it stepped back
        self._release_now: str | None = None  # released at the end of this read, untouched
        self._keys: list[tuple[int, int]] = []  # (offset in the held bytes, PTS) of keyframes
        self._samples: list[tuple[float, float]] = []  # (clock, media held)
        self._before: float | None = None  # media minus clock before the silence
        self._settle: tuple[float, float] | None = None  # (clock, media held) where it settled
        self._runs: deque[float] = deque(maxlen=_TRIM_RUNS)  # the media of the latest runs
        self.trims: deque[Trim] = deque(maxlen=_TRIMS_KEPT)
        self.cut_short: deque[tuple[int, int]] = deque(maxlen=_TRIMS_KEPT)  # (offset, bytes)

    @property
    def holding(self) -> bool:
        return self._held is not None

    def feed(self, chunk: bytes) -> list[bytes]:
        """Take input bytes; return what passes on (see the class docstring for the pieces)."""
        now = self._clock()
        pieces = [b""]
        video_pid, video_type = self._program()
        data = self._buf + chunk if self._buf else bytes(chunk)
        n = len(data)
        off = start = 0  # the next packet to read; the first byte not yet passed on or held
        searching = self._searching
        flags: list[tuple[int, int, int | None]] = []  # flagged packets in a row (_run_begins)
        while off + PACKET <= n:
            if searching or data[off] != SYNC:
                found = _find_sync(data, off)
                if found < 0:
                    off, searching = max(off, n - 2 * PACKET), True
                    break
                off, searching = found, False
                continue
            end = off + PACKET
            if end == n:
                break  # the next input's first byte tells whether this packet is whole
            if data[end] != SYNC:
                found = _find_sync(data, off + 1)
                if found < 0 and n - 2 * PACKET < end:
                    break
                if 0 <= found < end:  # cut short: dropped whole, as the segmenter would
                    self._put(data[start:off], pieces)
                    self.cut_short.append((self._pos + off, found - off))
                    off = start = found
                    continue
            b1, b3 = data[off + 1], data[off + 3]
            if b1 & 0x80:  # with the transport error flag the header is untrusted
                off = end
                continue
            pid = ((b1 & 0x1F) << 8) | data[off + 2]
            pcr = None
            if b3 & 0x20 and data[off + 4]:
                if data[off + 5] & 0x80:  # discontinuity_indicator
                    if flags and flags[-1][0] + PACKET != off:
                        flags.clear()
                    counters = self._counters if self._held is None else self._held_counters
                    flags.append((off, pid, counters.get(pid)))
                if data[off + 4] >= 7 and data[off + 5] & 0x10:
                    pcr = _pcr_base(data, off + 6)
            pts = _pes_times(data, off)[0] if b1 & 0x40 else None
            if pcr is not None or pts is not None:
                start = self._timed(now, data, off, start, pieces, pid, pcr, pts, video_pid, flags)
                if pid == video_pid and pts is not None and self._held is not None:
                    if self._confirmed:
                        self._advance(pts)
                        rai, _, es = _pes_start(data[off:end])
                        if _trim_key(data[off:end], rai, es, video_type):
                            self._keys.append((len(self._held) + off - start, pts))
                if pid == video_pid and pts is not None and self._held is None:
                    self._advance(pts)
                if pcr is not None:
                    self._pcrs[pid] = pcr
                if pts is not None:
                    self._pts[pid] = pts
            if b3 & 0x10 and pid != 0x1FFF:  # after _timed: the packet may begin a held run
                counters = self._counters if self._held is None else self._held_counters
                counters[pid] = b3 & 0x0F
            off = end
        self._put(data[start:off], pieces)
        self._buf = data[off:]
        self._pos += off
        self._searching = searching
        if self._held is not None and self._confirmed:
            self._samples.append((now, self._run_media()))
            reason = self._release_now or self._due(now)
            if reason is not None:
                self._release(now, pieces, reason or None)
        elif self._held is None and self._first is not None:
            self._note(now)
        return pieces

    def _timed(
        self,
        now: float,
        data: bytes,
        off: int,
        start: int,
        pieces: list[bytes],
        pid: int,
        pcr: int | None,
        pts: int | None,
        video_pid: int | None,
        flags: list[tuple[int, int, int | None]],
    ) -> int:
        """A packet carrying a PCR or starting a PES: where a run may begin, be judged, or a
        held one meet the next restart. Returns where the bytes not yet placed start."""
        last_pcr = self._pcrs.get(pid)
        last_pts = self._pts.get(pid)
        back = (
            pcr is not None and last_pcr is not None and pts_delta(last_pcr, pcr) < -_PCR_BACK_TICKS
        ) or (
            pts is not None and last_pts is not None and pts_delta(last_pts, pts) < -_REORDER_TICKS
        )
        if back and (self._held is None or self._confirmed):
            begin = self._run_begins(off, start, flags)
            self._put(data[start:begin], pieces)
            start = begin
            if self._held is not None:  # the next restart while one is held
                self._samples.append((now, self._run_media()))
                self._release(now, pieces, self._release_now)
            self._suspect(now, self._pos + begin, video_pid)
            for at in range(begin, off, PACKET):
                if data[at + 3] & 0x10:
                    self._held_counters[((data[at + 1] & 0x1F) << 8) | data[at + 2]] = (
                        data[at + 3] & 0x0F
                    )
        if self._held is not None and not self._confirmed:
            if pcr is not None and pid in self._ref_pcrs:
                if pts_delta(self._ref_pcrs[pid], pcr) < -_PCR_BACK_TICKS:
                    self._pcr_back = True
                else:  # the PCR goes on: no restart after all
                    return self._abandon(data, off, start, pieces)
            if pid == video_pid and pts is not None and self._video_back is None:
                ref = self._ref_video
                if ref is None or pts_delta(ref, pts) >= -_REORDER_TICKS:
                    return self._abandon(data, off, start, pieces)
                self._video_back = pts
            if self._video_back is not None and (self._pcr_back or not self._ref_pcrs):
                self._confirm(now)
            elif len(self._held) + off - start >= _CLOCK_PENDING_PACKETS * PACKET:
                return self._abandon(data, off, start, pieces)
        return start

    def _run_begins(self, off: int, start: int, flags: list[tuple[int, int, int | None]]) -> int:
        """Where a run whose clock steps back at `off` begins: at the flagged packets in a row
        right before it, when this read still has them (a restarted muxer flags its first
        tables, then its first PCR), so they go with it; their counters are taken back."""
        begin = off
        counters = self._counters if self._held is None else self._held_counters
        for at, pid, previous in reversed(flags):
            if at == begin - PACKET and at >= start:
                begin = at
                if previous is None:
                    counters.pop(pid, None)
                else:
                    counters[pid] = previous
        return begin

    def _suspect(self, now: float, offset: int, video_pid: int | None) -> None:
        """A PCR or a PES stepped back: a new run may begin here; hold it until judged."""
        self._held = bytearray()
        self._held_pos, self._held_at = offset, now
        self._held_counters = {}
        self._confirmed = False
        self._ref_pcrs = dict(self._pcrs)
        self._ref_video = self._pts.get(video_pid) if video_pid is not None else None
        # From here each PID's steps are the new run's own (its first PES steps back from the
        # old run's, as expected).
        self._pcrs.clear()
        self._pts.clear()
        self._pcr_back = False
        self._video_back = None
        self._release_now = None
        self._keys = []

    def _abandon(self, data: bytes, off: int, start: int, pieces: list[bytes]) -> int:
        """No restart after all: what was held passes on as it came."""
        held, self._held = self._held, None
        assert held is not None
        self._put(bytes(held) + data[start:off], pieces)
        self._counters.update(self._held_counters)
        return off

    def _confirm(self, now: float) -> None:
        """A restart: the old run ends where the new one began."""
        assert self._video_back is not None
        self._confirmed = True
        self._before = self._ahead  # as noted at an earlier read's end: before the silence
        self._runs.append(self._run_media())
        self._done += self._runs[-1]
        self._first = self._top = self._video_back
        self._samples = [(self._held_at, 0.0)]
        self._settle = None
        if self._before is None:
            self._release_now = "first"  # nothing to measure it from

    def tick(self) -> list[bytes]:
        """Time passed without input (a source that pauses): a held restart is judged as a
        read would judge it, so one whose hold is due (settled: its media stood still, or the
        cap) passes on now, not with the next read or the reconnect after the pause."""
        pieces = [b""]
        if self._held is None or not self._confirmed:
            return pieces
        now = self._clock()
        self._samples.append((now, self._run_media()))
        reason = self._release_now or self._due(now)
        if reason is not None:
            self._release(now, pieces, reason or None)
        return pieces

    def discontinuity(self) -> list[bytes]:
        """A reconnect comes next: what is held passes on untouched, and so does the partial
        packet left (the segmenter drops it); no restart is looked for across it."""
        pieces = self._end("reconnect")
        self._searching = True
        self._pcrs.clear()
        self._pts.clear()
        self._runs.append(self._run_media())
        self._done += self._runs[-1]
        self._first = self._top = None
        if self._joiner is not None:
            self._joiner.segment()  # the counters run on across it, too
        return pieces

    def finish(self) -> list[bytes]:
        """The input ended: what is held passes on untouched."""
        return self._end("end")

    def _end(self, reason: str) -> list[bytes]:
        pieces = [b""]
        if self._held is not None:
            if self._confirmed:
                self._release(self._clock(), pieces, reason)
            else:
                self._abandon(b"", 0, 0, pieces)
        self._put(self._buf, pieces)
        self._pos += len(self._buf)
        self._buf = b""
        if self._joiner is not None:
            pieces[-1] += self._joiner.end()
        return pieces

    def _advance(self, pts: int) -> None:
        if self._first is None or self._top is None:
            self._first = self._top = pts
            return
        step = pts_delta(self._top, pts)
        if step > 0:
            self._frame = min(self._frame, step) if step < _CLOCK else self._frame
            self._top = pts

    def _run_media(self) -> float:
        """Seconds of the current run from its first PTS to its furthest, plus a frame."""
        if self._first is None or self._top is None:
            return 0.0
        return (pts_delta(self._first, self._top) + self._frame) / _CLOCK

    def _note(self, now: float) -> None:
        """Media passed on minus clock, taken when the run's media went further (before a
        silence it is where the old run last brought media)."""
        if self._top is not None and self._top != self._noted:
            self._noted = self._top
            self._ahead = self._done + self._run_media() - now

    def _due(self, now: float) -> str | None:
        """Why to release now: "" for a settled burst or a cap (R decides), "no burst" when
        the first `_TRIM_NO_BURST_S` brought none; None to hold on."""
        assert self._held is not None
        held_for = now - self._held_at
        if held_for >= _TRIM_HOLD_S or len(self._held) >= _TRIM_HOLD_BYTES:
            return ""
        media = self._run_media()
        if held_for >= _TRIM_NO_BURST_S and self._before is not None:
            earlier = [m for at, m in self._samples if at <= now - _TRIM_NO_BURST_S]
            gained = media - earlier[-1] if earlier else media
            excess = self._done + media - now - self._before - _TRIM_MARGIN_S
            if excess < _TRIM_MIN_S and gained <= _TRIM_NO_BURST_S * _TRIM_BURST_RATE:
                return "no burst"
        earlier_samples = [sample for sample in self._samples if sample[0] <= now - 1.0]
        if earlier_samples and media - earlier_samples[-1][1] <= _TRIM_BURST_RATE:
            self._settle = earlier_samples[-1]
            return ""
        return None

    def _put(self, data: bytes, pieces: list[bytes]) -> None:
        """Hold `data`, or pass it on (through the joiner once one runs)."""
        if not data:
            return
        if self._held is not None:
            self._held += data
        else:
            pieces[-1] += self._joiner.feed(data) if self._joiner is not None else data

    def _release(self, now: float, pieces: list[bytes], reason: str | None = None) -> None:
        """Pass the held run on as a piece of its own: from its last keyframe in reach when R
        allows, else whole."""
        held, first = self._held, self._first
        assert held is not None and first is not None
        self._held = None
        excess = None
        if self._before is not None:
            settled, media = self._settle or (now, self._run_media())
            excess = self._done + media - settled - self._before - _TRIM_MARGIN_S
        drop: tuple[int, int] | None = None  # (offset in the held bytes, PTS) of the keyframe
        if reason is None:
            reach = min(excess or 0.0, sum(self._runs))
            keys = [
                (at, pts) for at, pts in self._keys if 0 < pts_delta(first, pts) <= reach * _CLOCK
            ]
            if excess is None or excess < _TRIM_MIN_S:
                reason = "small"
            elif not keys:
                reason = "no keyframe"
            else:
                reason, drop = "dropped", keys[-1]
        ranges: list[tuple[int, int]] = []
        if drop is None:
            kept = bytes(held)
            self._counters.update(self._held_counters)
        else:
            at, pts = drop
            kept = _kept_from(bytes(held), at, pts, self._program()[0], self._held_pos, ranges)
            self._first = pts  # what passed on of the run starts here
        joiner = self._joiner
        if self._join_counters and drop is not None:
            if joiner is None:
                joiner = self._joiner = ContinuityJoiner(self._counters)
            else:
                pieces[-1] += joiner.end()
            joiner.segment()
            joiner.discontinuity()
        if joiner is not None:
            kept = joiner.feed(kept)
        pieces.append(kept)  # a new run starts here
        dropped_s = pts_delta(first, drop[1]) / _CLOCK if drop is not None else 0.0
        # Where the run last brought media, as passed on: the baseline for the next restart.
        final = self._samples[-1][1] if self._samples else 0.0
        arrived = next((at for at, media in self._samples if media >= final), now)
        self._ahead, self._noted = self._done + final - dropped_s - arrived, self._top
        trim = Trim(self._held_pos, self._held_at, now, excess, dropped_s, tuple(ranges), reason)
        self.trims.append(trim)
        logger.info(
            "A stream restart was held %.1f s: R %s s, %.1f s dropped (%s).",
            now - self._held_at,
            "-" if excess is None else f"{excess:.1f}",
            dropped_s,
            reason,
        )


def _trim_key(pkt: bytes, rai: bool, es: int, video_type: int | None) -> bool:
    """Whether a video PES start is a picture VLC can start decoding at: an IDR or an open
    intra picture, or with RAI one whose kind its first packet cannot tell."""
    kind = _classify(pkt, es, video_type) if es < PACKET else _UNDECIDED
    return kind in (_IDR, _OPEN) or (rai and kind in (_INTRA, _PARAM, _UNDECIDED))


def _kept_from(
    data: bytes,
    at: int,
    key_pts: int,
    video_pid: int | None,
    offset: int,
    ranges: list[tuple[int, int]],
) -> bytes:
    """What of a held run (`data`) goes on from its keyframe (its first packet at `at`, its
    PTS `key_pts`): the picture from the keyframe's packet on (pictures shown before it stay:
    they may carry parameter sets the next ones need); every other PES (the sound) from its
    PES at or after the keyframe's time, wherever it lies in the bytes; the tables and other
    sections from their next start after the keyframe. Each packet left out is added to
    `ranges` at its input offset (`data` begins at `offset`), adjacent ones as one range."""
    kept: list[bytes] = []
    keep_pes: dict[int, bool] = {}  # PES PIDs: whether the PES being read stays
    started: set[int] = set()  # section PIDs past their first start after the keyframe
    n = len(data)
    pos = 0
    while pos + PACKET <= n and data[pos] == SYNC:
        b1, b3 = data[pos + 1], data[pos + 3]
        pid = ((b1 & 0x1F) << 8) | data[pos + 2]
        keep = pos >= at
        if b3 & 0x10 and not b1 & 0x80 and pid != 0x1FFF:
            if b1 & 0x40:
                pts = _pes_times(data, pos)[0]
                if pts is not None or _starts_pes(data, pos):
                    if pts is None or pid == video_pid:  # the picture: from the keyframe on
                        decision = pos >= at
                    else:
                        decision = pts_delta(key_pts, pts) >= 0
                    keep_pes[pid] = decision
                    keep = decision
                elif pos >= at:
                    started.add(pid)
            elif pid in keep_pes:
                keep = keep_pes[pid]
            elif pid not in started:
                keep = False  # a section's continuation whose start went with the dropped part
        if keep:
            kept.append(data[pos : pos + PACKET])
        elif ranges and ranges[-1][0] + ranges[-1][1] == offset + pos:
            ranges[-1] = (ranges[-1][0], ranges[-1][1] + PACKET)
        else:
            ranges.append((offset + pos, PACKET))
        pos += PACKET
    kept.append(data[pos:])
    return b"".join(kept)


def _starts_pes(data: bytes | bytearray, off: int) -> bool:
    """Whether the payload of the unit-start packet at `off` begins a PES."""
    afc = (data[off + 3] >> 4) & 3
    start = off + 4 + (1 + data[off + 4] if afc & 2 else 0)
    return start + 4 <= off + PACKET and data[start : start + 3] == _START_CODE


@dataclass(frozen=True, slots=True)
class Loop:
    """A replay `LoopTrimmer` dropped, from its first picture (`offset`, an input offset) until
    new content came.

    `back_s` is how far back the copy it replayed lay, `found_at` and `ended_at` the clock when
    it was recognised and when it ended, `dropped_s` the pictures' media left out and `dropped`
    the input ranges left out ((offset, bytes)). `ended_by`: "keyframe" (a new picture to start
    at), "pictures" (new pictures without a keyframe for `_LOOP_MIN_S`), "sound" (new sound for
    `_LOOP_MIN_S` while the pictures repeated), "stall" (the pictures stopped while the sound
    went on: `_LOOP_SOUND_WAIT`), "given up" (dropped `_LOOP_GIVE_UP_S`: it plays again),
    "end" (the input ended).
    """

    offset: int
    back_s: float
    found_at: float
    ended_at: float
    dropped_s: float
    dropped: tuple[tuple[int, int], ...]
    ended_by: str

    @property
    def dropped_bytes(self) -> int:
        return sum(size for _, size in self.dropped)


@dataclass(slots=True)
class _Unit:
    """A PES `LoopTrimmer` reads: where its first packet is (an input offset), its PTS, whether
    it is a keyframe (a picture), and its ES bytes so far."""

    at: int
    pts: int | None
    key: bool
    parts: list[bytes | bytearray]


_PASSING, _HOLDING, _DROPPING = 0, 1, 2  # LoopTrimmer's states


def _just_before(step: int) -> bool:
    """Whether a PES `step` ticks from a run's first picture is sound that belongs before it
    (muxed after it): up to `_LOOP_EARLY_TICKS` earlier."""
    return -_LOOP_EARLY_TICKS <= step < 0


class LoopTrimmer:
    """Drops what a raw TS source replays while its own source is down, until new content comes.

    One provider, when its upstream dies, keeps the connection open by replaying its last
    seconds over and over under new, continuous timestamps (measured: copies 7-17 s back, the
    same 9.5 s three times in 37 s), and VLC played the loop. Here the pictures (each video
    PES's ES bytes, hashed) of the last `_LOOP_WINDOW_S` and the latest `_LOOP_WINDOW_SOUND`
    other PES (the sound) are remembered. A picture whose copy lies at least `_LOOP_MIN_S`
    back begins a run: from its first packet everything is held while the pictures after it
    repeat the copy's, in order. The run is a loop once it holds `_LOOP_MIN_S` of pictures,
    `_LOOP_PICTURES` of them distinct, with its sound repeated too (`_LOOP_SOUND_SHARE` of the
    PES that began in it, when the program has sound; the sound muxed after the run's first
    picture but timed before it is the content's before). A copy that far back means the run
    replays a sequence that once went on to other pictures: a still picture an encoder
    repeats, or a short pattern, matches the pictures right before it and begins no run.
    Short of a loop, what was held passes on as it came (a run goes once `_LOOP_SOUND_NEW` of
    its sound PES are new), and a run judged no loop is not held again while it goes on.

    A loop is dropped from its first picture: every packet until new content comes, the tables
    too (alone they would let the segmenter time a segment by the wall clock), but the sound
    that still belongs before it (each sound PID's PES, not repeated, timed up to
    `_LOOP_EARLY_TICKS` before the first picture, until one is not). It ends at a new picture
    (none in the window) that is a keyframe, at a new picture once `_LOOP_MIN_S` of new
    pictures came without one (a long GOP or intra refresh: the ones before it go), or at a
    repeated picture once a sound PID brought `_LOOP_MIN_S` of new sound while the pictures
    repeated (a still picture with live sound). From that picture everything passes on but the
    sound timed before it (each PID's, until a PES at or after it, `_CLOCK_PENDING_PACKETS` at
    most), as a new run (a piece of its own) that `ClockJoiner` re-bases onto the last picture
    passed on: the live edge stood still while the loop went (the player says "Waiting for the
    channel…", as for a stalled source) and goes on with no timestamp jump. A loop dropped for
    `_LOOP_GIVE_UP_S` plays again the same way, from a keyframe (a channel that is itself a
    short loop must not freeze for good), and its pictures begin no run any more; another loop
    is judged as before. From the first loop's end on, what passes on goes through `ContinuityJoiner`, so the counters run on
    across loops and reconnects (about a microsecond a packet).

    A picture is judged once the next begins: its bytes wait that long (a picture's delay), or
    until `_LOOP_SOUND_WAIT` other PES began (the video stalled: what is held passes on, and a
    loop ends there, as it does when that many begin while no picture is read). `feed` takes and returns pieces as `RepeatTrimmer` passes them on (each after the first
    starts a new run): a run being judged passes on as it came at a new run, and a loop goes on
    across it (its new run starts where the loop ends). `reconnect()` (a reconnect comes next)
    and `finish()` pass on what is held as it came, or drop it during a loop, which goes on
    across a reconnect (the provider's next connection may replay on; `ClockJoiner` re-bases
    the loop's end onto the last picture before the reconnect). Per packet the cost is reading
    its header and keeping its payload for its PES's digest; memory is the two windows'
    digests and at most `_LOOP_HOLD_BYTES` held. Pure logic: the clock (for `loops`) and the
    program's video PID and type (the segmenter's) are the caller's.
    """

    def __init__(
        self,
        *,
        program: Callable[[], tuple[int | None, int | None]],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._program = program
        self._clock = clock
        # The bytes not yet passed on or dropped, from input offset `_base` on: from the picture
        # being read (passing or dropping), or from a run's first picture (holding).
        self._held = bytearray()
        self._base = 0
        self._scan = 0  # the input offset of the next packet to read
        self._searching = True  # the next packet start must be proven by three sync bytes
        self._picture: _Unit | None = None  # the picture being read
        self._reading: dict[int, _Unit] = {}  # the sound PES being read, by PID
        self._pes_pids: set[int] = set()  # the PIDs that carried a PES other than the picture
        self._since = 0  # their PES begun since the picture being read began
        self._last_pts: int | None = None  # the latest picture's PTS
        self._frame: int | None = None  # the shortest forward PTS step seen: a picture
        # The windows: digests by number in a ring, and each digest's latest number.
        self._pictures: list[int | None] = [None] * _LOOP_WINDOW_PICTURES
        self._picture_at: dict[int, int] = {}
        self._count = 0  # pictures read: the next one's number
        self._sounds: list[int | None] = [None] * _LOOP_WINDOW_SOUND
        self._sound_at: dict[int, int] = {}
        self._sound_count = 0
        self._state = _PASSING
        # The run (holding) or the loop (dropping): its first picture's input offset and PTS,
        # how many pictures back its copy lies, its pictures, their digests, its sound PES and
        # how many of them repeated.
        self._run_at = 0
        self._run_pts: int | None = None
        self._run_back = 0
        self._run_pictures = 0
        self._run_digests: set[int] = set()
        self._run_sound = [0, 0]
        self._repeated: set[int] = set()  # where its sound PES that repeat begin (input offsets)
        self._rejected: tuple[int, int] | None = None  # a run judged no loop: (back, next number)
        # The pictures of the latest loop given up (`_LOOP_GIVE_UP_S`): none of them begins a run
        self._given_up: frozenset[int] = frozenset()
        # Dropping:
        self._found_at = 0.0
        self._reached: set[int] = set()  # sound PIDs past the sound that still passes
        self._new = 0  # new pictures in a row
        self._new_since = 0  # pictures since the loop was given up, while a keyframe is waited for
        self._live: dict[int, int] = {}  # the PTS where new sound began, by PID
        self._live_now = False  # the next picture ends the loop (the sound is new)
        self._dropped: list[tuple[int, int]] = []
        self._ending: Loop | None = None  # the loop that ended, recorded once the sound after
        # it was judged (`_gate`)
        # Passing after a loop: the sound before the new content's first PTS goes.
        self._gate: int | None = None
        self._gated: set[int] = set()  # sound PIDs past it
        self._gate_left = 0  # packets it judges at most
        self._keep: dict[int, bool] = {}  # sound PIDs: whether the PES being filtered passes
        # Each PID's last counter passed on before the first loop (from its first packet
        # dropped), and from where that loop ended on, the joiner that runs them on.
        self._counters: dict[int, int] = {}
        self._counted = False  # those were read (from the run's bytes)
        self._joiner: ContinuityJoiner | None = None
        self.loops: deque[Loop] = deque(maxlen=_TRIMS_KEPT)

    def feed(self, pieces: list[bytes]) -> list[bytes]:
        """Take pieces (each after the first starts a new run); return what passes on, the same
        way (a loop's end starts a new run)."""
        out: list[list[bytes]] = [[]]
        for k, piece in enumerate(pieces):
            if k:
                self._new_run(out)
            if piece:
                self._read(piece, out)
        return [b"".join(part) for part in out]

    def reconnect(self) -> list[bytes]:
        """A reconnect comes next: what is held passes on as it came (dropped during a loop,
        which goes on); nothing read so far is joined to the next connection's bytes."""
        out: list[list[bytes]] = [[]]
        self._end_input(out)
        return [b"".join(out[0])]

    def finish(self) -> list[bytes]:
        """The input ended: what is held passes on as it came (dropped during a loop)."""
        out: list[list[bytes]] = [[]]
        self._end_input(out)
        if self._state == _DROPPING:
            self._state = _PASSING
            self._ended("end")
        self._close()
        return [b"".join(out[0])]

    # -- reading ---------------------------------------------------------------------------

    def _read(self, chunk: bytes, out: list[list[bytes]]) -> None:
        held = self._held
        held += chunk
        video, video_type = self._program()
        reading = self._reading
        off = self._scan - self._base
        while off + PACKET <= len(held):
            if self._searching or held[off] != SYNC:
                found = _find_sync(held, off)
                if found < 0:
                    off = max(off, len(held) - 2 * PACKET)
                    self._searching = True
                    break
                off, self._searching = found, False
                continue
            b1 = held[off + 1]
            if not b1 & 0x80:  # with the transport error flag the header is untrusted
                pid = ((b1 & 0x1F) << 8) | held[off + 2]
                if b1 & 0x40:
                    if pid == video:
                        at = self._base + off
                        self._picture_starts(off, video_type, out)
                        off = at - self._base  # what was settled left `held`
                    elif _starts_pes(held, off):
                        self._sound_starts(pid, off)
                else:
                    unit = self._picture if pid == video else reading.get(pid)
                    b3 = held[off + 3]
                    if unit is not None and b3 & 0x10:
                        start = off + 5 + held[off + 4] if b3 & 0x20 else off + 4
                        if start < off + PACKET:
                            unit.parts.append(held[start : off + PACKET])
            off += PACKET
        self._scan = self._base + off
        if self._picture is not None and self._since > _LOOP_SOUND_WAIT:
            self._stalled(self._picture, out)
        elif self._picture is None and self._state == _DROPPING:
            if self._since > _LOOP_SOUND_WAIT:  # other PES and no picture (another PID's?)
                self._resume(self._scan, None, self._scan, "stall", out)
            else:
                self._settle(self._scan, out, drop=True)  # no picture is read: nothing waits
        if self._state == _PASSING and self._picture is None:
            self._settle(self._scan, out, drop=False)  # no picture is read: nothing waits

    def _stalled(self, picture: _Unit, out: list[list[bytes]]) -> None:
        """No picture followed `picture` while `_LOOP_SOUND_WAIT` other PES began (the video
        stalled): it is not judged, and what is held passes on as it came. A loop ends there
        (what comes is no replay of pictures)."""
        self._picture = None
        if self._state == _DROPPING:
            self._resume(picture.at, picture.pts, self._scan, "stall", out)
            return
        if self._state == _HOLDING:
            self._rejected = (self._run_back, self._count)
            self._release(self._scan, out)

    def _picture_starts(self, off: int, video_type: int | None, out: list[list[bytes]]) -> None:
        """A picture begins at `off` in `held`: the one before it was read whole."""
        at = self._base + off
        pkt = bytes(self._held[off : off + PACKET])
        rai, pts, es = _pes_start(pkt)
        previous = self._picture
        self._picture = _Unit(
            at, pts, _trim_key(pkt, rai, es, video_type), [pkt[es:]] if es < PACKET else []
        )
        self._since = 0
        if pts is not None:
            if self._last_pts is not None:
                step = pts_delta(self._last_pts, pts)
                if 0 < step < _CLOCK and (self._frame is None or step < self._frame):
                    self._frame = step
            self._last_pts = pts
        if previous is not None:
            self._judge(previous, hash(b"".join(previous.parts)), at, out)

    def _sound_starts(self, pid: int, off: int) -> None:
        """A PES of another PID begins at `off` in `held`: the one before it was read whole."""
        self._pes_pids.add(pid)
        pkt = bytes(self._held[off : off + PACKET])
        _, pts, es = _pes_start(pkt)
        previous = self._reading.get(pid)
        self._reading[pid] = _Unit(self._base + off, pts, False, [pkt[es:]] if es < PACKET else [])
        self._since += 1
        if previous is None:
            return
        digest = hash(b"".join(previous.parts))
        seen = self._sound_at.get(digest) is not None
        number = self._sound_count
        slot = number % _LOOP_WINDOW_SOUND
        old = self._sounds[slot]
        if old is not None and self._sound_at.get(old) == number - _LOOP_WINDOW_SOUND:
            del self._sound_at[old]
        self._sounds[slot] = digest
        self._sound_at[digest] = number
        self._sound_count = number + 1
        if seen and self._state == _HOLDING:
            self._repeated.add(previous.at)
        pts, first = previous.pts, self._run_pts
        if previous.at < self._run_at or (
            pts is not None and first is not None and _just_before(pts_delta(first, pts))
        ):
            return  # the sound of what came before the run (or the loop): it tells nothing
        if self._state == _HOLDING:
            self._run_sound[0] += 1
            self._run_sound[1] += seen
        elif self._state == _DROPPING:
            if seen:
                self._live.pop(pid, None)
            elif pts is not None:  # new sound for `_LOOP_MIN_S` while the pictures repeat
                since = self._live.setdefault(pid, pts)
                self._live_now |= pts_delta(since, pts) >= _LOOP_MIN_S * _CLOCK

    # -- judging ---------------------------------------------------------------------------

    def _judge(self, picture: _Unit, digest: int, end: int, out: list[list[bytes]]) -> None:
        """A picture was read whole: its bytes end at input offset `end`, where the next one's
        begin."""
        number = self._count
        latest = self._latest(digest)
        self._remember(digest)
        if self._state == _DROPPING:
            how = None
            if latest is None:  # new content: from a keyframe (its sound is new too)
                self._new += 1
                self._live.clear()
                self._live_now = False
                if picture.key:
                    how = "keyframe"
                elif self._new * self._ticks() >= _LOOP_MIN_S * _CLOCK:
                    how = "pictures"
            else:
                self._new = 0
                if self._live_now:  # a still picture repeated, its sound live
                    how = "sound"
                elif self._clock() - self._found_at >= _LOOP_GIVE_UP_S:
                    self._new_since += 1  # (pictures since: a keyframe is waited for)
                    if picture.key or self._new_since * self._ticks() >= _LOOP_MIN_S * _CLOCK:
                        how = "given up"
                        self._given_up = frozenset(self._run_digests)
            if how is None:
                self._run_pictures += 1
                if len(self._run_digests) < _LOOP_WINDOW_PICTURES:
                    self._run_digests.add(digest)
                self._settle(end, out, drop=True)
            else:
                self._resume(picture.at, picture.pts, end, how, out)
            return
        if self._state == _HOLDING:
            back = self._run_back
            if self._at(number - back) == digest:
                self._run_pictures += 1
                self._run_digests.add(digest)
                verdict = self._verdict()
                if verdict:
                    self._drop_run(end, out)
                elif verdict is not None or len(self._held) >= _LOOP_HOLD_BYTES:
                    self._rejected = (back, number + 1)
                    self._release(end, out)
                return
            self._release(picture.at, out)  # the run broke here: this picture is judged afresh
        rejected = self._rejected
        if rejected is not None:
            back, expected = rejected
            if expected == number and self._at(number - back) == digest:
                self._rejected = (back, number + 1)  # the run judged no loop goes on
                latest = None
            else:
                self._rejected = None
        if latest is not None and digest in self._given_up:
            latest = None  # a picture of a loop given up: it plays
        if latest is not None and (number - latest) * self._ticks() >= _LOOP_MIN_S * _CLOCK:
            self._settle(picture.at, out, drop=False)
            self._state = _HOLDING
            self._run_at, self._run_pts, self._run_back = picture.at, picture.pts, number - latest
            self._run_pictures, self._run_digests, self._run_sound = 1, {digest}, [0, 0]
            self._repeated = set()
        else:
            self._settle(end, out, drop=False)

    def _verdict(self) -> bool | None:
        """Whether the run held is a loop: None while that is not known yet."""
        came, repeated = self._run_sound
        if came - repeated >= _LOOP_SOUND_NEW:
            return False  # its sound is new
        if self._run_pictures * self._ticks() < _LOOP_MIN_S * _CLOCK:
            return None
        if len(self._run_digests) < _LOOP_PICTURES:
            return False
        return repeated >= _LOOP_SOUND_SHARE * came

    def _release(self, end: int, out: list[list[bytes]]) -> None:
        """The run held is no loop: it passes on as it came, up to `end`."""
        logger.debug("a replay of %d pictures was let go", self._run_pictures)
        self._state = _PASSING
        self._settle(end, out, drop=False)

    def _drop_run(self, end: int, out: list[list[bytes]]) -> None:
        """The run held is a loop: it goes from its first picture on."""
        self._state = _DROPPING
        self._found_at = self._clock()
        self._close()  # the loop before it, should its sound still have been judged
        self._gate = None
        self._reached, self._keep, self._live = set(), {}, {}
        self._new, self._new_since, self._live_now = 0, 0, False
        self._counters, self._counted = {}, False
        self._dropped = []
        back = self._run_back * self._ticks() / _CLOCK
        logger.info("The source replays what it sent %.1f s before: dropped until it is new.", back)
        self._settle(end, out, drop=True)

    def _resume(self, at: int, pts: int | None, end: int, how: str, out: list[list[bytes]]) -> None:
        """The loop ends: new content passes on from input offset `at` (a picture with `pts`)
        as a new run."""
        self._settle(at, out, drop=True)
        self._state = _PASSING
        self._ended(how)
        out.append([])  # the new content's clock is re-based onto what passed on (ClockJoiner)
        if self._joiner is None:
            self._joiner = ContinuityJoiner(self._counters)
        else:
            out[-2].append(self._joiner.end())
        self._joiner.segment()  # the new content's counters run on from what passed on
        self._rejected = None
        self._keep = {}
        self._gate, self._gated, self._gate_left = pts, set(), _CLOCK_PENDING_PACKETS
        if self._gate is None:
            self._close()  # no sound to judge after it
        self._settle(end, out, drop=False)

    def _ended(self, how: str) -> None:
        """The loop ends (`how`): its record waits for the sound after it (`_close`)."""
        ticks = self._ticks()
        dropped_s = self._run_pictures * ticks / _CLOCK
        back_s = self._run_back * ticks / _CLOCK
        found_at = self._found_at
        self._ending = Loop(self._run_at, back_s, found_at, self._clock(), dropped_s, (), how)
        logger.info("A replayed stretch ended (%s): %.1f s dropped.", how, dropped_s)

    def _close(self) -> None:
        """Record the loop that ended, with what was dropped after it too."""
        if self._ending is not None:
            self.loops.append(replace(self._ending, dropped=tuple(self._dropped)))
            self._ending = None

    def _new_run(self, out: list[list[bytes]]) -> None:
        """A new run starts with the next piece: a run held passes on as it came first; a loop
        goes on (the new run starts where it ends)."""
        dropping = self._state == _DROPPING
        self._end_input(out)
        if not dropping:
            out.append([])

    def _end_input(self, out: list[list[bytes]]) -> None:
        """No byte read so far joins the next: what is held passes on as it came, or goes
        during a loop (its sound that still passed ends here). The counters run on across."""
        end = self._base + len(self._held)
        if self._state == _DROPPING:
            self._run_pictures += self._picture is not None  # the picture cut short goes too
            self._settle(end, out, drop=True)
            self._reached.update(self._pes_pids)
        else:
            if self._state == _HOLDING:
                self._release(end, out)
            self._settle(end, out, drop=False)
        if self._joiner is not None:
            out[-1].append(self._joiner.end())
            self._joiner.segment()
        self._picture = None
        self._since = 0
        self._reading.clear()
        self._scan = self._base
        self._searching = True
        self._last_pts = None
        self._rejected = None
        if self._gate is not None:
            self._gate = None
            self._close()

    # -- the windows -----------------------------------------------------------------------

    def _ticks(self) -> int:
        """A picture's duration (the shortest forward PTS step seen; 25 fps before one)."""
        return self._frame or _CLOCK // 25

    def _window(self) -> int:
        """The pictures of the last `_LOOP_WINDOW_S` (at most the ring)."""
        return min(_LOOP_WINDOW_PICTURES, int(_LOOP_WINDOW_S * _CLOCK / self._ticks()))

    def _latest(self, digest: int) -> int | None:
        """The number of the latest picture in the window with this digest."""
        number = self._picture_at.get(digest)
        if number is None or self._count - number > self._window():
            return None
        return number

    def _at(self, number: int) -> int | None:
        """The digest of picture `number`, while it is in the window."""
        if number < 0 or self._count - number > self._window():
            return None
        return self._pictures[number % _LOOP_WINDOW_PICTURES]

    def _remember(self, digest: int) -> None:
        number = self._count
        slot = number % _LOOP_WINDOW_PICTURES
        old = self._pictures[slot]
        if old is not None and self._picture_at.get(old) == number - _LOOP_WINDOW_PICTURES:
            del self._picture_at[old]
        self._pictures[slot] = digest
        self._picture_at[digest] = number
        self._count = number + 1

    # -- passing on and dropping -----------------------------------------------------------

    def _settle(self, upto: int, out: list[list[bytes]], *, drop: bool) -> None:
        """Pass on or drop the held bytes before input offset `upto` (through `_filter` when
        dropping, or passing while the sound after a loop is judged)."""
        n = upto - self._base
        if n <= 0:
            return
        if drop or self._gate is not None:
            self._filter(n, out, drop)
        else:
            self._emit(bytes(self._held[:n]), out)
        del self._held[:n]
        self._base = upto

    def _emit(self, data: bytes, out: list[list[bytes]]) -> None:
        """Pass `data` on (through the joiner once a loop ended)."""
        if data:
            out[-1].append(data if self._joiner is None else self._joiner.feed(data))

    def _filter(self, n: int, out: list[list[bytes]], drop: bool) -> None:
        """The first `n` held bytes packet by packet: dropping, every packet goes but the sound
        before the loop's first picture; passing, every packet passes but the sound before the
        new content's first picture (`_gate`)."""
        held, base = self._held, self._base
        video = self._program()[0]
        sound = self._pes_pids - {video}
        if drop and sound <= self._reached and self._counted:
            self._drop(base, n)  # nothing of it passes
            return
        self._counted |= drop
        kept: list[bytes] = []
        counters = self._counters
        at = 0
        while at < n:
            if at + PACKET > n or held[at] != SYNC:  # out of packet sync, or a partial packet
                found = held.find(_SYNC_BYTE, at + 1, n)
                stop = n if found < 0 else found
                if drop:
                    self._drop(base + at, stop - at)
                else:
                    kept.append(bytes(held[at:stop]))
                at = stop
                continue
            keep = self._passes(at, video, drop)
            if keep:
                kept.append(bytes(held[at : at + PACKET]))
            else:
                self._drop(base + at, PACKET)
            b1, b3 = held[at + 1], held[at + 3]
            if drop and b3 & 0x10 and not b1 & 0x80:  # the counters passed on before the loop
                pid = ((b1 & 0x1F) << 8) | held[at + 2]
                if keep or pid not in counters:
                    counters[pid] = b3 & 0x0F if keep else (b3 - 1) & 0x0F
            at += PACKET
        self._emit(b"".join(kept), out)
        if not drop and self._gate is not None:
            self._gate_left -= n // PACKET
            if self._gate_left <= 0 or sound <= self._gated:
                self._gate = None
                self._close()

    def _passes(self, at: int, video: int | None, drop: bool) -> bool:
        """Whether the held packet at `at` passes (see `_filter`)."""
        held = self._held
        b1 = held[at + 1]
        pid = ((b1 & 0x1F) << 8) | held[at + 2]
        if pid == video or pid not in self._pes_pids or b1 & 0x80:
            return not drop  # the picture, the tables, PCRs and null packets: as the bytes go
        if b1 & 0x40 and _starts_pes(held, at):
            pts = _pes_times(held, at)[0]
            if drop:
                first = self._run_pts
                keep = (
                    pid not in self._reached
                    and pts is not None
                    and first is not None
                    and _just_before(pts_delta(first, pts))
                    and self._base + at not in self._repeated  # (the loop's own goes)
                )
                if not keep:
                    self._reached.add(pid)
            else:
                gate = self._gate
                keep = (
                    pid in self._gated or pts is None or gate is None or pts_delta(gate, pts) >= 0
                )
                if keep:
                    self._gated.add(pid)
            self._keep[pid] = keep
            return keep
        # The rest of a PES: as its start went (one begun before the bytes judged here: before
        # a loop it passes, before new content it goes).
        return self._keep.get(pid, drop and pid not in self._reached)

    def _drop(self, at: int, size: int) -> None:
        if size <= 0:
            return
        dropped = self._dropped
        if dropped and dropped[-1][0] + dropped[-1][1] == at:
            dropped[-1] = (dropped[-1][0], dropped[-1][1] + size)
        else:
            dropped.append((at, size))


@dataclass(slots=True)
class _Watch:
    """What `ClockJoiner` keeps to tell a forward jump (copied while a leap is held)."""

    # Each (PID, kind)'s last timestamp passed on, and the pictures' reference then.
    stamps: dict[tuple[int, int], tuple[int, int | None]] = field(default_factory=dict)
    progress: int = 0  # ticks the pictures advanced since the window began
    # (arrival, lead) of the latest pictures, their leads falling: the first is the highest.
    leads: deque[tuple[float, float]] = field(default_factory=deque)
    cadence: int | None = None  # the pictures' latest forward step
    # How far the other timestamps went past the last picture since it came (sound beyond
    # its PTS, a PCR beyond its DTS): a gap in the pictures that they fill is no jump.
    covered: int = 0

    def copy(self) -> _Watch:
        return _Watch(
            dict(self.stamps), self.progress, deque(self.leads), self.cadence, self.covered
        )


class ClockJoiner:
    """Re-bases each run after a restart inside one connection, so its clock runs on (raw TS),
    and a clock that leaps ahead where arrival does not explain it (raw TS and HLS).

    A restarted muxer starts PTS, DTS and PCR over (its PCR 0.7 s below zero, wrapping 0.7 s
    in). VLC 3 takes a long step back well, but one onto timestamps it has just played (a few
    seconds back, as after a trim) makes it drop the new pictures as late, and the picture
    stands still that long (measured: a 2 s step stood 1.6 s, an 8 s one 5-8 s). At each
    junction `RepeatTrimmer` reports (a new run starts with the bytes fed with `junction`),
    every PTS and DTS (the PES headers of every PID) and every PCR base from then on is moved
    by one offset, modulo 2**33, chosen so that the new run's first picture follows the old
    run's last (its furthest PTS, and its last DTS) by a frame: the new run keeps its own audio
    to video alignment, and the offsets add up over restarts. The junction's bytes wait for its
    first picture (`_CLOCK_PENDING_PACKETS` at most), so the tables, PCRs and sound before it
    move with it. An offset that moves the clock by two frames or less leaves the junction be
    (the clock runs on already). Otherwise it leaves no trace: each PID's first
    discontinuity_indicator after it is cleared, and the counters run on
    (`ContinuityJoiner`). The ring then holds rewritten bytes after a restart (or a jump,
    below): the original-bytes rule gives way there.

    A clock can also leap ahead within a run: a public news channel's encoder lost 13 s of its
    source, and every PID's timestamps stepped 13 s on between two pictures inside one HLS
    segment, no discontinuity tag near. VLC 3 resets its clock only past a 60 s gap, so it
    waited for the timestamps: a 15 s still. With an arrival clock (`clock`, raw TS) or once
    HLS segments `arrive`, such a jump is looked for (the `_JUMP_*` constants say how), once
    the pictures went on 10 s: a picture whose DTS steps on by more than `JUMP_S` that arrival
    does not explain (raw TS: by the wall clock, as after a stalled source whose timestamps
    went on in real time; HLS: by the EXTINF of the segments begun) is re-based like a
    junction, onto the last picture by a frame, by its DTS (a picture's PTS may sit seconds
    ahead of it). Where a timestamp leaps
    before that picture (a PCR, sound, the PTS of pictures decoded before the jump and shown
    after it), what follows is held until a picture jumps (or the pictures moved on
    `_JUMP_WAIT_TICKS` without one: then it passes on as it came). For a while after the jump
    (`_JUMP_SETTLE_TICKS`) each timestamp takes the old offset or the new, whichever keeps its
    own PID running on; then the new one alone. A jump splits no piece (the segmenter sees one
    clock running on, and the timeline leaves nothing out); the flags after it are cleared and
    the counters run on as after a junction. Each is logged once and kept in `jumps`.

    `feed` returns what passes on in pieces: a junction re-based lies between two (the
    segmenter cuts the new run a segment of its own and learns afresh whether it flags its
    keyframes). Until the first junction or jump nothing is rewritten: each packet's header is
    read (its counter, a video PES's timestamps; while jumps are looked for, every PES header
    and PCR). `discontinuity()` (a reconnect, an HLS discontinuity) ends the re-basing of
    restarts: what follows passes on as it comes, as before, but for a junction that comes
    before the new connection passed a picture (where a loop `LoopTrimmer` dropped across the
    reconnect ends): that is re-based onto the last picture before the reconnect. What jumps
    added stays when the first picture after it is within `_REJOIN_WINDOW_TICKS` of the last
    before it as they came (one clock: the bytes in between wait for that picture), so a
    source that goes on with its clock does not leap ahead again there. Pure logic: the
    program's video PID is the caller's, and the clock is injected.
    """

    def __init__(
        self,
        *,
        program: Callable[[], tuple[int | None, int | None]],
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._program = program
        self._clock = clock
        self._video_pid: int | None = None
        self._buf = b""
        self._searching = True  # the next packet start must be proven by three sync bytes
        self._offset = 0  # added to every PTS, DTS and PCR base once active (modulo 2**33)
        self._active = False
        self._last_pts: int | None = None  # the latest video PTS as it came
        self._top: int | None = None  # the furthest video PTS passed on (re-based)
        # The latest video DTS passed on (re-based; its PTS without one): the pictures' clock.
        self._last_dts: int | None = None
        # (`_top`, `_last_dts`, `_last_pts`) before a reconnect, until a picture passed after it
        self._before: tuple[int, int | None, int | None] | None = None
        self._frame = _CLOCK // 25  # the shortest forward PTS step seen: a frame
        self._counters: dict[int, int] = {}  # each PID's last counter passed on, until active
        self._joiner: ContinuityJoiner | None = None  # once active: the counters run on
        self._pending: list[bytes] | None = None  # a junction's packets until its first picture
        self._unflagged: set[int] = set()  # PIDs whose first flag after the junction went
        self._unflag_left = 0  # packets after a junction in which those are looked for
        # (step it came with, step it goes on with), in seconds, per junction re-based
        self.junctions: deque[tuple[float, float]] = deque(maxlen=_TRIMS_KEPT)
        # Forward jumps: looked for with an arrival clock, or once HLS segments `arrive`.
        self._watching = clock is not None
        self._listed = False  # HLS: arrival is the EXTINF of the segments begun, not the clock
        self._credit = 0.0  # arrival so far, in seconds
        self._window = _JUMP_WINDOW_S  # arrival over which the highest lead is taken
        self._watch = _Watch()
        # While a leap is held (`_pending` then holds what came since): the watch and the
        # pictures' clock before it, and the pictures' clock when it came.
        self._held: tuple[_Watch, tuple[int | None, ...]] | None = None
        self._held_at: int | None = None
        # The picture that jumped: (the new offset, its step, of it what arrival did not
        # explain, the pictures' clock before it).
        self._leap: tuple[int, int, float, int] = (0, 0, 0.0, 0)
        # After a jump: (the offset before it, the pictures' clock before it) while it settles.
        self._settle: tuple[int, int] | None = None
        self._jumped = 0  # what jumps added to `_offset` (modulo 2**33): a reconnect keeps it
        # After a discontinuity, until its first picture: (what jumps added, the pictures'
        # clock before it as it came).
        self._marked: tuple[int, int] | None = None
        # (step it came with, of it what arrival did not explain), in seconds, per jump
        self.jumps: deque[tuple[float, float]] = deque(maxlen=_TRIMS_KEPT)

    @property
    def offset_s(self) -> float:
        """What the re-basing adds to every timestamp now, in seconds."""
        return pts_delta(0, self._offset) / _CLOCK

    def arrive(self, seconds: float) -> None:
        """HLS: a segment of `seconds` (its EXTINF) begins with the next bytes. From the first,
        forward jumps are looked for, with the EXTINF as the arrival (see the class
        docstring)."""
        self._watching = self._listed = True
        self._credit += max(0.0, seconds)
        self._window = max(_JUMP_WINDOW_S, 1.5 * seconds)

    def feed(self, data: bytes, junction: bool = False) -> list[bytes]:
        """Take input bytes (`junction`: a new run starts with them); return what passes on,
        in pieces (see the class docstring)."""
        video_pid = self._video_pid = self._program()[0]
        if self._clock is not None and not self._listed:
            self._credit = self._clock()
        out: list[bytes | None] = []  # None: a junction
        if junction:
            self._flush_pending(out)  # one that never met its picture passes on as it came
            self._settle = None  # the junction re-bases what follows on its own
            self._pending = []
        data = self._buf + data if self._buf else data
        n = len(data)
        off = run = 0  # the next packet; the first byte not yet passed on or pending
        while off + PACKET <= n:
            if self._searching or data[off] != SYNC:
                found = _find_sync(data, off)
                if found < 0:
                    off, self._searching = max(off, n - 2 * PACKET), True
                    break
                off, self._searching = found, False
                continue
            end = off + PACKET
            b1 = data[off + 1]
            pid = ((b1 & 0x1F) << 8) | data[off + 2]
            video = bool(pid == video_pid and b1 & 0x40)
            timed = self._watching and (
                b1 & 0x40 or (data[off + 3] & 0x20 and data[off + 4] >= 7 and data[off + 5] & 0x10)
            )
            if self._pending is None:
                if timed:
                    if run < off:  # passed on before a jump may change the offset
                        self._place(data[run:off], out)
                        run = off
                    verdict, packet = self._watched(data, off, pid, pid == video_pid, held=False)
                    if verdict != _PASS:
                        if verdict == _LEAP:  # held from here until a picture jumps
                            self._pending = [data[off:end]]
                        else:  # _REWRITE: after a jump, each timestamp on its own
                            self._put(packet, out)
                        off = run = end
                        continue
                if not self._active:
                    self._count(data, off)
                if video and not timed:
                    self._follow(data, off)
                off = end
                continue
            if run < off:
                self._place(data[run:off], out)
            self._pending.append(data[off:end])
            if self._held is not None:  # a leap waits for a picture that jumps
                verdict = _PASS
                if timed:
                    verdict = self._watched(data, off, pid, pid == video_pid, held=True)[0]
                if verdict == _JUMP:
                    self._confirm(out)
                elif len(self._pending) >= _CLOCK_PENDING_PACKETS or self._waited():
                    self._release(out)
            elif video and not b1 & 0x80 and _pes_times(data, off)[0] is not None:
                if self._marked is not None:
                    self._resume(out)
                else:
                    self._decide(out)
            elif len(self._pending) >= _CLOCK_PENDING_PACKETS:
                self._flush_pending(out)
            off = run = end
        if run < off:
            self._place(data[run:off], out)
        self._buf = data[off:]
        pieces: list[bytes] = []
        piece: list[bytes] = []
        for item in out:
            if item is None:
                pieces.append(b"".join(piece))
                piece = []
            else:
                piece.append(item)
        pieces.append(b"".join(piece))
        return pieces

    def discontinuity(self) -> bytes:
        """A reconnect (or an HLS discontinuity) comes next: what waits passes on as it is, and
        the re-basing of restarts ends. A junction that comes before the next connection passed
        a picture (where a loop `LoopTrimmer` dropped across the reconnect ends) is re-based
        onto the last picture before the reconnect, as if that had been a pause. What jumps
        added goes on if the clock does (see the class docstring)."""
        marked = self._marked  # a discontinuity before, whose first picture never came
        out = self._end()
        if self._joiner is not None:
            self._counters = self._joiner.counters  # what passed on: the next joiner's start
        if self._top is not None:
            self._before = (self._top, self._last_dts, self._last_pts)
        if marked is None and self._jumped and self._last_dts is not None:
            marked = (self._jumped, (self._last_dts - self._offset) % _PTS_MODULO)
        self._active, self._offset, self._joiner = False, 0, None
        self._unflag_left = 0
        self._last_pts = self._top = self._last_dts = None
        self._settle = None
        self._jumped = 0
        self._watch = _Watch()  # the step across it is no jump; the arrival goes on
        if marked is not None:
            self._marked = marked
            self._pending = []  # what follows waits for its first picture (`_resume`)
        self._searching = True
        return out

    def finish(self) -> bytes:
        """The input ended: what waits passes on as it is."""
        return self._end()

    def _end(self) -> bytes:
        out: list[bytes | None] = []
        self._flush_pending(out)
        self._place(self._buf, out)
        self._buf = b""
        if self._joiner is not None:
            out.append(self._joiner.end())
        return b"".join(item for item in out if item is not None)

    def _count(self, data: bytes, off: int) -> None:
        """Until active: note a packet's counter as it passes on (the joiner's start)."""
        b1, b3 = data[off + 1], data[off + 3]
        pid = ((b1 & 0x1F) << 8) | data[off + 2]
        if b3 & 0x10 and pid != 0x1FFF and not b1 & 0x80:
            self._counters[pid] = b3 & 0x0F

    def _decide(self, out: list[bytes | None]) -> None:
        """The junction's first picture came: re-base from the junction on (or leave it be)."""
        pending = self._pending
        assert pending is not None
        self._pending = None
        first = pending[-1]
        pts, dts = _pes_times(first, 0)
        top, last_dts = self._top, self._last_dts
        if top is None and self._before is not None:
            top, last_dts, self._last_pts = self._before  # none passed on since a reconnect
        self._watch = _Watch()  # the step across a junction is no jump
        if pts is None or top is None:
            self._pass_pending(pending, out)
            return
        # The new run's first picture follows the old run's last by a frame, in PTS and DTS.
        last_dts = top if last_dts is None else last_dts
        by_pts = (top + self._frame - pts) % _PTS_MODULO
        by_dts = (last_dts + self._frame - (pts if dts is None else dts)) % _PTS_MODULO
        offset = by_pts if pts_delta(by_dts, by_pts) >= 0 else by_dts
        moved = pts_delta(self._offset, offset)
        if abs(moved) <= 2 * self._frame:  # the clock runs on already: no junction to hide
            self._pass_pending(pending, out)
            return
        step = pts_delta(self._last_pts, pts) if self._last_pts is not None else 0
        self._offset = offset
        if self._joiner is None:
            self._joiner = ContinuityJoiner(self._counters)
        else:
            out.append(self._joiner.end())
        out.append(None)  # the junction: a new run starts
        self._active = True
        self._joiner.segment()  # the counters run on across the junction
        self._unflagged = set()
        self._unflag_left = _CLOCK_PENDING_PACKETS
        self.junctions.append((step / _CLOCK, (step + moved) / _CLOCK))
        logger.debug("a restart's clock re-based by %.2f s", moved / _CLOCK)
        for pkt in pending:
            self._place(pkt, out)
            self._note_packet(pkt)

    def _resume(self, out: list[bytes | None]) -> None:
        """The first picture after a discontinuity came while jumps had moved the clock: within
        `_REJOIN_WINDOW_TICKS` of the last picture before it (as both came), the source's clock
        goes on and so does what the jumps added; else what follows passes on as it comes."""
        pending, marked = self._pending, self._marked
        assert pending is not None and marked is not None
        self._pending = self._marked = None
        jumped, before = marked
        pts, dts = _pes_times(pending[-1], 0)
        now = pts if dts is None else dts
        if now is not None and abs(pts_delta(before, now)) <= _REJOIN_WINDOW_TICKS:
            self._offset = self._jumped = jumped
            self._joiner = ContinuityJoiner(self._counters)  # the counters run on across it
            self._active = True
            logger.debug("a jump's re-basing goes on across a discontinuity")
        for pkt in pending:
            if not self._active:
                self._count(pkt, 0)
            self._place(pkt, out)
            self._note_packet(pkt)

    def _pass_pending(self, pending: list[bytes], out: list[bytes | None]) -> None:
        """No junction to hide: the pending packets pass on as any would."""
        for pkt in pending:
            if not self._active:
                self._count(pkt, 0)
            self._place(pkt, out)
            self._note_packet(pkt)

    def _flush_pending(self, out: list[bytes | None]) -> None:
        """No picture came after a junction (or a discontinuity), or a leap was held when the
        input ends or a junction comes: its packets pass on as they are."""
        if self._held is not None:
            self._release(out)
            return
        pending, self._pending = self._pending, None
        self._marked = None
        for pkt in pending or ():
            if not self._active:
                self._count(pkt, 0)
            self._place(pkt, out)
            self._note_packet(pkt)

    def _follow(self, data: bytes, off: int) -> None:
        """A video PES start (as it came) passes on: its PTS, and re-based the furthest PTS
        and the DTS."""
        pts, dts = _pes_times(data, off)
        if pts is None:
            return
        offset = self._offset
        dts_out = None if dts is None else (dts + offset) % _PTS_MODULO
        self._followed(pts, (pts + offset) % _PTS_MODULO, dts_out)

    def _followed(self, pts: int, shifted: int, dts: int | None) -> None:
        """A picture passed on: its PTS as it came and passed on, and its DTS passed on."""
        if self._last_pts is not None and 0 < pts_delta(self._last_pts, pts) < self._frame:
            self._frame = pts_delta(self._last_pts, pts)
        self._last_pts = pts
        self._before = None
        if self._top is None or pts_delta(self._top, shifted) > 0:
            self._top = shifted
        self._last_dts = shifted if dts is None else dts

    # -- forward jumps -----------------------------------------------------------------------

    def _watched(
        self, data: bytes, off: int, pid: int, video: bool, held: bool
    ) -> tuple[int, bytes]:
        """A packet that carries a PCR or starts a PES, while jumps are looked for: `_PASS` (it
        passes on with the offset, noted), `_LEAP` (a timestamp leapt: hold from it, noted),
        `_JUMP` (`held`: this picture jumped) or `_REWRITE` with the packet re-based on its own
        (after a jump, or at it)."""
        places = _stamps(data, off)
        if not places:
            return _PASS, b""
        ref = _reference(places) if video else None
        if self._settle is not None and not held:
            return _REWRITE, self._settled(data[off : off + PACKET], pid, places, ref)
        offset = self._offset
        last = self._last_dts
        if ref is not None and last is not None:
            step = pts_delta(last, (ref[1] + offset) % _PTS_MODULO)
            unexplained = self._unexplained(step)
            if unexplained is not None:
                # The new offset: this picture follows the last by a frame, by its DTS.
                new = (last + self._frame - ref[1]) % _PTS_MODULO
                self._leap = (new, step, unexplained, last)
                if held:
                    return _JUMP, b""
                self._begin()
                return _REWRITE, self._settled(data[off : off + PACKET], pid, places, ref)
        leapt = not held and self._seasoned() and self._leaps(pid, places, ref, offset)
        if leapt:
            self._held = (self._watch.copy(), self._follow_state())
            self._held_at = last
        self._note(pid, places, ref, offset, cover=not held and not leapt)
        return (_LEAP if leapt else _PASS), b""

    def _unexplained(self, step: int) -> float | None:
        """Of a picture's step, in seconds, what arrival does not explain, when that makes it a
        jump (see `_JUMP_WINDOW_S`); else None."""
        watch = self._watch
        if not self._seasoned():
            return None
        if step <= max(_JUMP_TICKS, 2 * (watch.cadence or 0)) or step > _JUMP_MAX_TICKS:
            return None
        if step - watch.covered <= _JUMP_TICKS:  # the sound (or the PCR) went on through it
            return None
        lead = (watch.progress + step) / _CLOCK - self._credit
        leads = watch.leads
        horizon = self._credit - self._window
        while len(leads) > 1 and leads[0][0] < horizon:
            leads.popleft()
        highest = leads[0][1] if leads else watch.progress / _CLOCK - self._credit
        unexplained = lead - highest
        return unexplained if unexplained > JUMP_S else None

    def _seasoned(self) -> bool:
        """Whether the pictures went on long enough for a step to be judged (see
        `_JUMP_HISTORY_TICKS`)."""
        return self._watch.progress >= _JUMP_HISTORY_TICKS

    def _leaps(
        self, pid: int, places: list[tuple[int, int, int]], ref: tuple[int, int] | None, offset: int
    ) -> bool:
        """Whether a timestamp other than a picture's own reference lands more than `JUMP_S`
        (and at most `_JUMP_MAX_TICKS`) past where its last leads (that one plus how far the
        pictures went on since)."""
        stamps = self._watch.stamps
        now = self._last_dts if ref is None else (ref[1] + offset) % _PTS_MODULO
        for kind, _, value in places:
            if ref is not None and kind == ref[0]:
                continue
            prior = stamps.get((pid, kind))
            if prior is None:
                continue
            last, then = prior
            ahead = 0
            if then is not None and now is not None:
                ahead = max(0, pts_delta(then, now))
            leap = pts_delta(last, (value + offset) % _PTS_MODULO) - ahead
            if _JUMP_TICKS < leap <= _JUMP_MAX_TICKS:
                return True
        return False

    def _note(
        self,
        pid: int,
        places: list[tuple[int, int, int]],
        ref: tuple[int, int] | None,
        offset: int,
        cover: bool = True,
    ) -> None:
        """A packet's timestamps pass on with `offset`: the watch learns them (`cover`: how
        far they go past the last picture too; not while a leap is held)."""
        values = {kind: (value + offset) % _PTS_MODULO for kind, _, value in places}
        if ref is not None:
            self._advance(values[ref[0]])
            pts = next(value for kind, _, value in places if kind == _PTS)
            self._followed(pts, values[_PTS], values.get(_DTS))
        self._record(pid, values, cover and ref is None)

    def _record(self, pid: int, values: dict[int, int], cover: bool) -> None:
        """The timestamps passed on (`values` by kind) are each kind's last on `pid`; with
        `cover`, those of a packet that is no picture also say how far past the last picture
        the other timestamps went (a PCR beyond its DTS, a PTS beyond its furthest PTS)."""
        watch = self._watch
        stamps = watch.stamps
        for kind, value in values.items():
            stamps[(pid, kind)] = (value, self._last_dts)
            if not cover:
                continue
            mark = self._last_dts if kind == _PCR else self._top
            if mark is not None:
                watch.covered = max(watch.covered, pts_delta(mark, value))

    def _note_packet(self, pkt: bytes) -> None:
        """A packet passed on with the offset, out of the scan's order (pending): the watch
        learns its timestamps, and a picture is followed."""
        pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
        video = pid == self._video_pid and bool(pkt[1] & 0x40)
        if not self._watching:
            if video:
                self._follow(pkt, 0)
            return
        places = _stamps(pkt, 0)
        if places:
            self._note(pid, places, _reference(places) if video else None, self._offset)

    def _advance(self, now: int) -> None:
        """The pictures' clock moves to `now` (passed on): their progress against arrival."""
        watch = self._watch
        if self._last_dts is not None:
            step = pts_delta(self._last_dts, now)
            if step < -_REORDER_TICKS:  # a restart no junction reported: measured afresh
                watch.progress, watch.cadence = 0, None
                watch.leads.clear()
            else:
                watch.progress += step
                if step > 0:
                    watch.cadence = step
        watch.covered = 0  # a picture: what the others fill is measured from it
        lead = watch.progress / _CLOCK - self._credit
        leads = watch.leads
        while leads and leads[-1][1] <= lead:
            leads.pop()
        leads.append((self._credit, lead))
        horizon = self._credit - self._window
        while len(leads) > 1 and leads[0][0] < horizon:
            leads.popleft()

    def _begin(self) -> None:
        """A picture jumped (`_leap`): from here on the new offset, settling first."""
        offset, step, unexplained, before = self._leap
        moved = pts_delta(self._offset, offset)
        self._settle = (self._offset, before)
        self._offset = offset
        self._jumped = (self._jumped + moved) % _PTS_MODULO
        if self._joiner is None:
            self._joiner = ContinuityJoiner(self._counters)
        self._active = True
        self._unflagged = set()
        self._unflag_left = _CLOCK_PENDING_PACKETS
        self.jumps.append((step / _CLOCK, unexplained))
        logger.info(
            "The channel's clock leapt %.1f s ahead (%.1f s more than arrived); it runs on.",
            step / _CLOCK,
            unexplained,
        )

    def _settled(
        self,
        packet: bytes,
        pid: int,
        places: list[tuple[int, int, int]],
        ref: tuple[int, int] | None,
    ) -> bytes:
        """After a jump: the packet with each timestamp moved by the old offset or the new,
        whichever lands nearer its own last (and where that leads); the picture's reference
        first, which moves the pictures' clock the others are judged by."""
        assert self._settle is not None
        old, start = self._settle
        new = self._offset
        buf = bytearray(packet)
        chosen: dict[int, int] = {}
        first = ref[0] if ref is not None else -1
        for kind, at, value in sorted(places, key=lambda place: place[0] != first):
            moved = (value + new) % _PTS_MODULO
            value_out = self._nearer((pid, kind), (value + old) % _PTS_MODULO, moved)
            chosen[kind] = value_out
            if kind == _PCR:
                _write_pcr(buf, at, value_out)
            else:
                _write_timestamp(buf, at, value_out)
            if kind == first:
                self._advance(value_out)
                self._last_dts = value_out
        if ref is not None:
            pts = next(value for kind, _, value in places if kind == _PTS)
            self._followed(pts, chosen[_PTS], chosen.get(_DTS))
        self._record(pid, chosen, ref is None)
        last = self._last_dts
        if last is not None and abs(pts_delta(start, last)) >= _JUMP_SETTLE_TICKS:
            self._settle = None  # settled: the new offset alone from here
        return bytes(buf)

    def _nearer(self, key: tuple[int, int], kept: int, moved: int) -> int:
        """`kept` (the old offset) or `moved` (the new): whichever lands nearer the last of its
        kind on its PID, or where that leads now; `moved` for a first."""
        prior = self._watch.stamps.get(key)
        if prior is None:
            return moved
        last, then = prior
        ahead = 0
        if then is not None and self._last_dts is not None:
            ahead = max(0, pts_delta(then, self._last_dts))
        leads = (last + ahead) % _PTS_MODULO

        def miss(value: int) -> int:
            return min(abs(pts_delta(last, value)), abs(pts_delta(leads, value)))

        return kept if miss(kept) < miss(moved) else moved

    def _confirm(self, out: list[bytes | None]) -> None:
        """A held picture jumped: the held packets pass on settled from the leap on, as if the
        jump had been known there."""
        pending, held = self._pending, self._held
        assert pending is not None and held is not None
        self._pending = self._held = None
        self._held_at = None
        self._watch = held[0]
        self._restore_follow(held[1])
        self._begin()
        video_pid = self._video_pid
        for pkt in pending:
            places = _stamps(pkt, 0)
            if not places:
                self._place(pkt, out)
                continue
            pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
            video = pid == video_pid and bool(pkt[1] & 0x40)
            ref = _reference(places) if video else None
            if self._settle is None:  # (settled within what was held)
                self._place(pkt, out)
                self._note(pid, places, ref, self._offset)
            else:
                self._put(self._settled(pkt, pid, places, ref), out)

    def _release(self, out: list[bytes | None]) -> None:
        """No picture jumped after a leap: what was held passes on as it came (noted)."""
        pending, self._pending = self._pending, None
        self._held = None
        self._held_at = None
        for pkt in pending or ():
            if not self._active:
                self._count(pkt, 0)
        self._place(b"".join(pending or ()), out)

    def _waited(self) -> bool:
        """Whether the pictures moved on `_JUMP_WAIT_TICKS` since a leap without a jump."""
        held_at, last = self._held_at, self._last_dts
        return (
            held_at is not None
            and last is not None
            and (abs(pts_delta(held_at, last)) > _JUMP_WAIT_TICKS)
        )

    def _follow_state(self) -> tuple[int | None, ...]:
        return (self._last_pts, self._top, self._last_dts, self._frame) + (
            self._before if self._before is not None else (None, None, None)
        )

    def _restore_follow(self, state: tuple[int | None, ...]) -> None:
        self._last_pts, self._top, self._last_dts, frame, top, last_dts, last_pts = state
        self._frame = frame if frame is not None else self._frame
        self._before = None if top is None else (top, last_dts, last_pts)

    # -- passing on ------------------------------------------------------------------------

    def _put(self, packet: bytes, out: list[bytes | None]) -> None:
        """A packet rewritten on its own passes on (with the counters, unflagged)."""
        if self._active:
            self._join(packet, out)
        else:
            out.append(packet)

    def _place(self, data: bytes, out: list[bytes | None]) -> None:
        """Pass bytes on: as they are, or once active re-based, unflagged after a junction,
        with the counters running on."""
        if not data:
            return
        if not self._active:
            out.append(data)
            return
        buf = bytearray(data)
        at, n, start = 0, len(buf), 0
        while at < n:
            if at + PACKET <= n and buf[at] == SYNC:
                if buf[at + 1] & 0x40 or buf[at + 3] & 0x20:
                    _shift_packet(buf, at, self._offset)
                at += PACKET
                continue
            # Out of packet sync (a desync inside the run, or the input's partial tail): the
            # packets before it go through the joiner, these bytes as they are (the segmenter
            # skips them), and the packets after it are re-based too.
            self._join(bytes(buf[start:at]), out)
            resync = at + 1
            while resync < n and not (
                buf[resync] == SYNC
                and resync + PACKET <= n
                and (resync + PACKET == n or buf[resync + PACKET] == SYNC)
            ):
                resync += 1
            out.append(bytes(buf[at:resync]))
            start = at = resync
        self._join(bytes(buf[start:at]), out)

    def _join(self, data: bytes, out: list[bytes | None]) -> None:
        """Whole re-based packets through the joiner (counters on), unflagged after a junction."""
        if not data:
            return
        assert self._joiner is not None
        last = dict(self._joiner.counters) if self._unflag_left > 0 else None
        joined = self._joiner.feed(data)
        out.append(joined if last is None else self._unflag(joined, last))

    def _unflag(self, data: bytes, last: dict[int, int]) -> bytes:
        """After a junction: each PID's first discontinuity_indicator goes when it announces
        nothing any more (its counter runs on, and its clock does); one over a counter that
        still jumps (the next restart's tables, say) stays."""
        buf = bytearray(data)
        at, n = 0, len(buf)
        while at + PACKET <= n and buf[at] == SYNC and self._unflag_left > 0:
            self._unflag_left -= 1
            b3 = buf[at + 3]
            pid = ((buf[at + 1] & 0x1F) << 8) | buf[at + 2]
            if b3 & 0x20 and buf[at + 4] and buf[at + 5] & 0x80 and pid not in self._unflagged:
                self._unflagged.add(pid)
                previous = last.get(pid)
                cc = b3 & 0x0F
                if previous is None or cc == ((previous + 1) & 0x0F if b3 & 0x10 else previous):
                    buf[at + 5] &= 0x7F
            if b3 & 0x10:
                last[pid] = b3 & 0x0F
            at += PACKET
        return bytes(buf)


def _stamps(data: bytes | bytearray, off: int) -> list[tuple[int, int, int]]:
    """The timestamps a re-basing moves in the packet at `off` (as `_shift_packet` does): its
    PCR base, its PES header's PTS and DTS, each as (kind, its place from the packet's start,
    its value); none with the transport error flag."""
    b1, b3 = data[off + 1], data[off + 3]
    if b1 & 0x80:
        return []
    found: list[tuple[int, int, int]] = []
    start = 4
    if b3 & 0x20:
        length = data[off + 4]
        start = 5 + length
        if length >= 7 and data[off + 5] & 0x10:
            found.append((_PCR, 6, _pcr_base(data, off + 6)))
    if not b1 & 0x40 or not b3 & 0x10 or start + 14 > PACKET:
        return found
    at = off + start
    if data[at : at + 3] != _START_CODE or data[at + 3] in _NO_PES_HEADER:
        return found
    if data[at + 6] & 0xC0 != 0x80:
        return found
    flags = data[at + 7] >> 6
    if flags & 2:
        found.append((_PTS, start + 9, _timestamp(data, at + 9)))
        if flags == 3 and start + 19 <= PACKET:
            found.append((_DTS, start + 14, _timestamp(data, at + 14)))
    return found


def _reference(places: list[tuple[int, int, int]]) -> tuple[int, int] | None:
    """A picture's reference on the pictures' clock: (kind, value) of its DTS, else its PTS."""
    pts = None
    for kind, _, value in places:
        if kind == _DTS:
            return kind, value
        if kind == _PTS:
            pts = (kind, value)
    return pts


# PES stream_ids without the optional header (no PTS or DTS): program stream map, padding,
# private stream 2, ECM, EMM, DSM-CC, H.222.1 type E, program stream directory.
_NO_PES_HEADER = frozenset((0xBC, 0xBE, 0xBF, 0xF0, 0xF1, 0xF2, 0xF8, 0xFF))


def _pes_times(pkt: bytes | bytearray, off: int) -> tuple[int | None, int | None]:
    """(PTS, DTS) of the PES starting in the packet at `off`, None where it has none."""
    afc = (pkt[off + 3] >> 4) & 3
    start = off + 4
    if afc & 2:
        start = off + 5 + pkt[off + 4]
    end = off + PACKET
    if not afc & 1 or start + 14 > end or pkt[start : start + 3] != _START_CODE:
        return None, None
    if pkt[start + 3] in _NO_PES_HEADER or pkt[start + 6] & 0xC0 != 0x80:  # no optional header
        return None, None
    flags = pkt[start + 7] >> 6
    pts = dts = None
    if flags & 2:
        pts = _timestamp(pkt, start + 9)
        if flags == 3 and start + 19 <= end:
            dts = _timestamp(pkt, start + 14)
    return pts, dts


def _timestamp(data: bytes | bytearray, at: int) -> int:
    b = data[at : at + 5]
    return ((b[0] >> 1) & 7) << 30 | b[1] << 22 | (b[2] >> 1) << 15 | b[3] << 7 | b[4] >> 1


def _write_timestamp(buf: bytearray, at: int, value: int) -> None:
    """Write a 33-bit PTS or DTS at `at`, keeping its 4-bit prefix and the marker bits."""
    buf[at] = (buf[at] & 0xF0) | ((value >> 29) & 0x0E) | 1
    buf[at + 1] = (value >> 22) & 0xFF
    buf[at + 2] = ((value >> 14) & 0xFE) | 1
    buf[at + 3] = (value >> 7) & 0xFF
    buf[at + 4] = ((value << 1) & 0xFE) | 1


def _write_pcr(buf: bytearray, at: int, base: int) -> None:
    """Write a 33-bit PCR base into the six PCR bytes at `at`, keeping their extension."""
    buf[at] = base >> 25
    buf[at + 1] = (base >> 17) & 0xFF
    buf[at + 2] = (base >> 9) & 0xFF
    buf[at + 3] = (base >> 1) & 0xFF
    buf[at + 4] = ((base & 1) << 7) | (buf[at + 4] & 0x7F)


def _shift_packet(buf: bytearray, at: int, offset: int) -> None:
    """Move the PCR base and the PES header's PTS and DTS of the packet at `at` by `offset`."""
    b1, b3 = buf[at + 1], buf[at + 3]
    if b1 & 0x80:  # the transport error flag: the header is untrusted
        return
    start = at + 4
    if b3 & 0x20:
        length = buf[at + 4]
        start = at + 5 + length
        if length >= 7 and buf[at + 5] & 0x10:
            _write_pcr(buf, at + 6, (_pcr_base(buf, at + 6) + offset) % _PTS_MODULO)
    end = at + PACKET
    if not b1 & 0x40 or not b3 & 0x10 or start + 14 > end:
        return
    if buf[start : start + 3] != _START_CODE or buf[start + 3] in _NO_PES_HEADER:
        return
    if buf[start + 6] & 0xC0 != 0x80:
        return
    flags = buf[start + 7] >> 6
    if flags & 2:
        pts = (_timestamp(bytes(buf[start + 9 : start + 14]), 0) + offset) % _PTS_MODULO
        _write_timestamp(buf, start + 9, pts)
        if flags == 3 and start + 19 <= end:
            dts = (_timestamp(bytes(buf[start + 14 : start + 19]), 0) + offset) % _PTS_MODULO
            _write_timestamp(buf, start + 14, dts)


_RELABEL_PES = 3  # audio PES starts in a row of a codec the PMT misnames before it is renamed
_RELABEL_HOLD_PACKETS = 64  # a PMT section spanning packets is held this long at most
_CODEC_FAMILY = {0x03: "mpeg", 0x04: "mpeg", 0x0F: "aac", 0x11: "latm", 0x81: "ac3", 0x87: "ac3"}


def _sniffed_type(es: bytes) -> int | None:
    """The stream type the first bytes of an audio PES's payload show: 0x0F for ADTS AAC (a
    12-bit sync, layer 00), 0x03 or 0x04 for MPEG-1 or -2 audio (an 11-bit sync and a layer),
    0x81 for AC-3 (0x0B77); None for anything else."""
    if len(es) < 2:
        return None
    if es[0] == 0x0B:
        return 0x81 if es[1] == 0x77 else None
    if es[0] != 0xFF:
        return None
    if es[1] & 0xF6 == 0xF0:
        return 0x0F
    if es[1] & 0xE0 == 0xE0 and es[1] & 0x06:
        return 0x03 if es[1] & 0x18 == 0x18 else 0x04
    return None


class AudioRelabeler:
    """Renames an audio stream's codec in the PMT when its PES carry another one.

    One provider alternates sources behind a channel and sometimes keeps the old source's PMT:
    it declares MPEG audio (stream_type 0x03) while the PES carry ADTS AAC, and VLC decodes
    them with the MPEG audio decoder: silence (ffprobe too). At each PES start of an audio PID
    the PMT lists (MPEG audio, AAC, LATM, AC-3), the payload's first bytes tell the codec
    (`_sniffed_type`; anything else counts for nothing). After `_RELABEL_PES` PES in a row of a
    codec the PMT does not declare, every following PMT names that one (0x0F, 0x03 or 0x04,
    0x81) with its CRC32 recomputed; the same way back once the PES carry what the provider
    declares again. A renaming is forgotten when the provider's PMT declares another type for
    that PID, names another PMT PID, or a reconnect comes (`reconnect()`): its PMT is trusted
    again until its PES disagree anew. From the first renaming on, the version_number is this
    stage's: the last one written plus one whenever the section written differs from the last
    one (so VLC re-creates the stream with the right decoder, and a seek back to a segment
    whose header PMT names the stream otherwise sees another version), else the same. A PMT
    section spanning packets is held with the packets after it until it ends
    (`_RELABEL_HOLD_PACKETS` at most, then it passes as it came), and only a section whose CRC
    holds is changed. While the PES agree with the PMT nothing changes. It reads the bytes on
    their way to the segmenter (raw TS and HLS), whose segment headers then carry the renamed
    PMT; `flush()` before a discontinuity or the end passes on what it holds.
    """

    def __init__(self) -> None:
        self._buf = b""  # a partial packet, or a PMT section that spans packets and the rest
        self._pmt_pid: int | None = None
        self._declared: dict[int, int] = {}  # audio PID -> the stream type its PMT declares
        self._renamed: dict[int, int] = {}  # audio PID -> the stream type written instead
        self._streak: dict[int, tuple[int, int]] = {}  # PID -> (sniffed type, PES in a row)
        self._managed = False  # a renaming came: the version_number is this stage's
        self._version: int | None = None  # the last version_number written
        self._written = b""  # the last section written, but for its version and CRC
        self.renamings: deque[tuple[int, int, int]] = deque(maxlen=_TRIMS_KEPT)  # (PID, from, to)

    def feed(self, chunk: bytes) -> bytes:
        """Take bytes; return what passes on (a partial packet waits for the next bytes)."""
        data = self._buf + chunk if self._buf else bytes(chunk)
        out: bytearray | None = None
        n = len(data)
        off = 0
        while off + PACKET <= n:
            if data[off] != SYNC:
                found = data.find(_SYNC_BYTE, off + 1)
                off = n if found < 0 else found
                continue
            b1 = data[off + 1]
            if b1 & 0x40 and not b1 & 0x80:
                pid = ((b1 & 0x1F) << 8) | data[off + 2]
                if pid in self._declared:
                    self._sniff(pid, data, off)
                elif pid == 0:
                    self._on_pat(data, off)
                elif pid == self._pmt_pid:
                    done = self._on_pmt(data, off)
                    if done is None and n - off <= _RELABEL_HOLD_PACKETS * PACKET:
                        self._buf = data[off:]  # it ends in a later packet: held from here
                        return bytes(out[:off]) if out is not None else data[:off]
                    if done:  # (None: it never ended within the bound; as it came)
                        if out is None:
                            out = bytearray(data)
                        for at, value in done:
                            out[at] = value
            off += PACKET
        self._buf = data[off:]
        return bytes(out[:off]) if out is not None else data[:off]

    def flush(self) -> bytes:
        """A discontinuity or the end comes next: what is held passes on as it came."""
        held, self._buf = self._buf, b""
        return held

    def reconnect(self) -> None:
        """A new connection (or an HLS discontinuity) comes next: its PMT is trusted again."""
        self._renamed.clear()
        self._streak.clear()

    def _on_pat(self, data: bytes, off: int) -> None:
        section = _section_at(data, off)
        if section is None or section[0] != 0x00 or _crc32(section):
            return
        for i in range(8, len(section) - 4 - 3, 4):
            if (section[i] << 8) | section[i + 1]:  # program 0 is the network table
                pmt_pid = ((section[i + 2] & 0x1F) << 8) | section[i + 3]
                if pmt_pid != self._pmt_pid:
                    self._pmt_pid = pmt_pid
                    self._declared.clear()
                    self._renamed.clear()
                    self._streak.clear()
                return

    def _on_pmt(self, data: bytes, off: int) -> list[tuple[int, int]] | None:
        """Read the PMT section starting at `off`: its audio streams' declared types, and the
        bytes to change ((offset in `data`, value)); None while it ends beyond `data`."""
        places = _section_places(data, off, self._pmt_pid)
        if places is None:
            return None
        section = bytearray(data[at] for at in places)
        if len(section) < 16 or section[0] != 0x02 or _crc32(bytes(section)):
            return []
        end = len(section) - 4
        i = 12 + (((section[10] & 0x0F) << 8) | section[11])
        declared: dict[int, int] = {}
        while i + 5 <= end:
            stream_type = section[i]
            pid = ((section[i + 1] & 0x1F) << 8) | section[i + 2]
            if stream_type in _CODEC_FAMILY:
                declared[pid] = stream_type
                if self._declared.get(pid, stream_type) != stream_type:
                    self._renamed.pop(pid, None)  # the provider's PMT says otherwise now
                    self._streak.pop(pid, None)
                renamed = self._renamed.get(pid)
                if renamed is not None:
                    section[i] = renamed
            i += 5 + (((section[i + 3] & 0x0F) << 8) | section[i + 4])
        for pid in set(self._renamed) - set(declared):
            del self._renamed[pid]  # no longer listed
        self._declared = declared
        written = bytes(section[:5]) + bytes([section[5] & 0xC1]) + bytes(section[6:-4])
        version = (section[5] >> 1) & 0x1F
        if self._managed:
            if written != self._written and self._version is not None:
                version = (self._version + 1) & 0x1F
            elif self._version is not None:
                version = self._version
        self._version, self._written = version, written
        if not self._managed:
            return []
        section[5] = (section[5] & 0xC1) | (version << 1)
        section[-4:] = _crc32(bytes(section[:-4])).to_bytes(4, "big")
        return [(at, section[k]) for k, at in enumerate(places) if section[k] != data[at]]

    def _sniff(self, pid: int, data: bytes, off: int) -> None:
        pkt = data[off : off + PACKET]
        found = _sniffed_type(pkt[_pes_start(pkt)[2] :])
        if found is None:
            return
        declared = self._declared[pid]
        written = self._renamed.get(pid, declared)
        if _CODEC_FAMILY[found] == _CODEC_FAMILY[written]:
            self._streak.pop(pid, None)
            return
        last, count = self._streak.get(pid, (found, 0))
        count = count + 1 if _CODEC_FAMILY[last] == _CODEC_FAMILY[found] else 1
        if count < _RELABEL_PES:
            self._streak[pid] = (found, count)
            return
        del self._streak[pid]
        if _CODEC_FAMILY[found] == _CODEC_FAMILY[declared]:
            del self._renamed[pid]  # what the provider's PMT declares again
        else:
            self._renamed[pid] = found
        self._managed = True
        to = self._renamed.get(pid, declared)
        self.renamings.append((pid, written, to))
        logger.info(
            "Audio PID %#x carries stream type %#x, not %#x: its PMT is renamed.", pid, to, written
        )


def _section_at(data: bytes, off: int) -> bytes | None:
    """The section starting in the PUSI packet at `off` when it ends in that packet."""
    afc = (data[off + 3] >> 4) & 3
    start = off + (5 + data[off + 4] if afc & 2 else 4)
    if not afc & 1 or start >= off + PACKET:
        return None
    begin = start + 1 + data[start]
    if begin + 3 > off + PACKET:
        return None
    end = begin + 3 + (((data[begin + 1] & 0x0F) << 8) | data[begin + 2])
    return data[begin:end] if end <= off + PACKET and end - begin >= 16 else None


def _section_places(data: bytes, off: int, pid: int | None) -> list[int] | None:
    """Where the bytes of the section starting in the PUSI packet at `off` lie in `data`, on
    across that PID's next packets; None when it ends beyond `data`; [] when unreadable."""
    afc = (data[off + 3] >> 4) & 3
    start = off + (5 + data[off + 4] if afc & 2 else 4)
    if not afc & 1 or start >= off + PACKET:
        return []
    begin = start + 1 + data[start]
    if begin >= off + PACKET:
        return []
    places = list(range(begin, off + PACKET))
    at = off + PACKET
    while len(places) < 3 or len(places) < 3 + (((data[places[1]] & 0x0F) << 8) | data[places[2]]):
        while at + PACKET <= len(data) and (
            data[at] != SYNC or ((data[at + 1] & 0x1F) << 8) | data[at + 2] != pid
        ):
            at += PACKET
        if at + PACKET > len(data):
            return None
        if data[at + 1] & 0xC0 or not data[at + 3] & 0x10:  # a new start, or damaged
            return []
        afc = (data[at + 3] >> 4) & 3
        places.extend(range(at + (5 + data[at + 4] if afc & 2 else 4), at + PACKET))
        at += PACKET
    if len(places) < 16:
        return []
    return places[: 3 + (((data[places[1]] & 0x0F) << 8) | data[places[2]])]


def program_has_audio(data: bytes) -> bool | None:
    """Whether the first program's PMT in `data` (packet-aligned, as an HLS segment starts)
    lists an audio stream: one of the audio stream types, or a private PES (0x06) with a DVB
    AC-3, E-AC-3 or AAC descriptor. None until its PAT and PMT came (single-packet sections,
    as segment packagers write them) or once the packet sync is lost.

    2.1: an HLS master may name a separate default audio playlist while the variant muxes that
    audio too; only the variant's own program tables tell.
    """
    pmt_pid: int | None = None
    for at in range(0, len(data) - PACKET + 1, PACKET):
        if data[at] != SYNC:
            return None
        b1 = data[at + 1]
        pid = ((b1 & 0x1F) << 8) | data[at + 2]
        if not b1 & 0x40 or b1 & 0x80 or (pid != 0 and pid != pmt_pid):
            continue  # not a section start, damaged, or another PID
        afc = (data[at + 3] >> 4) & 3
        start = at + (5 + data[at + 4] if afc & 2 else 4)
        if not afc & 1 or start >= at + PACKET:
            continue
        begin = start + 1 + data[start]  # past the pointer field
        if begin + 3 > at + PACKET:
            continue
        end = begin + 3 + (((data[begin + 1] & 0x0F) << 8) | data[begin + 2])
        if end > at + PACKET or end - begin < 16:  # spans packets, or too short
            continue
        section = data[begin:end]
        if pid == 0 and section[0] == 0x00:
            for i in range(8, len(section) - 4 - 3, 4):
                if (section[i] << 8) | section[i + 1]:  # program 0 is the network table
                    pmt_pid = ((section[i + 2] & 0x1F) << 8) | section[i + 3]
                    break
        elif pid == pmt_pid and section[0] == 0x02:
            return _lists_audio(section)
    return None


_DVB_AUDIO = (0x6A, 0x7A, 0x7C)  # AC-3, enhanced AC-3, AAC descriptors


def _lists_audio(section: bytes) -> bool:
    """Whether a PMT section lists an audio stream (see `program_has_audio`)."""
    end = len(section) - 4
    i = 12 + (((section[10] & 0x0F) << 8) | section[11])
    while i + 5 <= end:
        stream_type = section[i]
        info_end = i + 5 + (((section[i + 3] & 0x0F) << 8) | section[i + 4])
        if stream_type in _AUDIO_TYPES:
            return True
        if stream_type == 0x06:
            j = i + 5
            while j + 2 <= min(info_end, end):
                if section[j] in _DVB_AUDIO:
                    return True
                j += 2 + section[j + 1]
        i = info_end
    return False


def _find_sync(data: bytes | bytearray, start: int) -> int:
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


def _pcr_base(data: bytes | bytearray, at: int) -> int:
    """The 33-bit base of the PCR whose six bytes start at `at` (90 kHz ticks)."""
    return (
        data[at] << 25
        | data[at + 1] << 17
        | data[at + 2] << 9
        | data[at + 3] << 1
        | data[at + 4] >> 7
    )


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
