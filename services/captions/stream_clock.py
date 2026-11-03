"""Where on the time-shift timeline a moment of the stream's sound is (2.9, AI captions).

The captions process reads a live session's stream from the helper's loopback server from
segment `seq` (its PAT/PMT first, then the original bytes), and libVLC decodes its sound into
blocks stamped on VLC's own clock: continuous while the stream is (measured 22 µs on 30 s),
but not the stream's timestamps. `TsTimes` reads the same bytes as they pass: the programme's
PIDs, each audio track's ISO 639 language (descriptor 0x0a), the first video PTS (what the
helper's timeline counts from: services.live.timeline sizes segments by the video PTS) and
the first PTS of the sound VLC decodes. `StreamClock` ties them together:

    timeline(block) = start(seq) + (first audio PTS - first video PTS) / 90 kHz
                      + (block's VLC time - first block's VLC time)

A video PTS that jumps (`JUMP_S` forward, or back), as at a provider's restart, ends the run:
the helper's timeline leaves such a jump out, so times after it would be off; the reader
starts again where the timeline says (`TsTimes.jumped`). Only the standard library is used.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

PACKET = 188
PTS_HZ = 90_000
_WRAP = 1 << 33
JUMP_S = 3.0  # as services.live.ts.JUMP_S: a forward step this long is a jump, not media
_BACK_S = 1.0  # B-frames reorder by a fraction of a second; more backwards is a reset

_VIDEO_TYPES = frozenset({0x01, 0x02, 0x10, 0x1B, 0x24, 0x42, 0xD1, 0xEA})
_AUDIO_TYPES = frozenset({0x03, 0x04, 0x0F, 0x11, 0x81, 0x87})
_AC3_DESCRIPTORS = frozenset({0x6A, 0x7A, 0x7C, 0x81})  # AC-3, E-AC-3, AAC, AC-3 (ATSC)


def pts_seconds(later: int, earlier: int) -> float:
    """`later - earlier` in seconds, across the 33-bit wrap (the nearer way round)."""
    delta = (later - earlier) % _WRAP
    if delta > _WRAP // 2:
        delta -= _WRAP
    return delta / PTS_HZ


@dataclass
class AudioTrack:
    pid: int
    stream_type: int
    language: str = ""  # ISO 639-2 ("tur", "eng"), "" when the stream names none


@dataclass
class TsTimes:
    """Feed it the stream's bytes in order (`feed`); it keeps what the clock needs."""

    audio_pid: Optional[int] = None  # the track VLC decodes; None: the first one listed
    video_pid: Optional[int] = None
    audio: list[AudioTrack] = field(default_factory=list)
    first_video_pts: Optional[int] = None
    first_audio_pts: Optional[int] = None
    last_video_pts: Optional[int] = None
    jumped: bool = False  # a video PTS jump since the first: the run's times end there
    _pmt_pid: Optional[int] = None
    _rest: bytes = b""

    def feed(self, data: bytes) -> None:
        data = self._rest + data
        at = 0
        end = len(data) - PACKET + 1
        while at < end:
            if data[at] != 0x47:
                at = _resync(data, at + 1)
                if at < 0:
                    self._rest = b""
                    return
                continue
            self._packet(data, at)
            at += PACKET
        self._rest = data[at:]

    # ---- one packet ------------------------------------------------------------------
    def _packet(self, data: bytes, at: int) -> None:
        start = bool(data[at + 1] & 0x40)
        pid = ((data[at + 1] & 0x1F) << 8) | data[at + 2]
        control = (data[at + 3] >> 4) & 0x3
        if not control & 0x1:
            return  # no payload
        off = at + 4
        if control & 0x2:
            off += 1 + data[at + 4]
        stop = at + PACKET
        if off >= stop:
            return
        if pid == 0:
            if start:
                self._pat(data[off:stop])
        elif pid == self._pmt_pid:
            if start:
                self._pmt(data[off:stop])
        elif start and (pid == self.video_pid or pid == self._audio_pid()):
            pts = _pes_pts(data, off, stop)
            if pts is not None:
                self._pts(pid, pts)

    def _audio_pid(self) -> Optional[int]:
        if self.audio_pid is not None:
            return self.audio_pid
        return self.audio[0].pid if self.audio else None

    def _pts(self, pid: int, pts: int) -> None:
        if pid == self.video_pid:
            if self.first_video_pts is None:
                self.first_video_pts = pts
            elif self.last_video_pts is not None:
                step = pts_seconds(pts, self.last_video_pts)
                if step >= JUMP_S or step < -_BACK_S:
                    self.jumped = True
            self.last_video_pts = pts
        elif self.first_audio_pts is None:
            # VLC decodes the sound from its first PES on, before the first picture or not.
            self.first_audio_pts = pts

    # ---- tables ----------------------------------------------------------------------
    def _pat(self, payload: bytes) -> None:
        section = _section(payload)
        if section is None or section[0] != 0x00:
            return
        body = section[8:-4]
        for i in range(0, len(body) - 3, 4):
            program = (body[i] << 8) | body[i + 1]
            if program != 0:
                self._pmt_pid = ((body[i + 2] & 0x1F) << 8) | body[i + 3]
                return

    def _pmt(self, payload: bytes) -> None:
        section = _section(payload)
        if section is None or section[0] != 0x02:
            return
        info_length = ((section[10] & 0x0F) << 8) | section[11]
        i = 12 + info_length
        body_end = len(section) - 4
        video: Optional[int] = None
        audio: list[AudioTrack] = []
        while i + 5 <= body_end:
            stream_type = section[i]
            pid = ((section[i + 1] & 0x1F) << 8) | section[i + 2]
            es_length = ((section[i + 3] & 0x0F) << 8) | section[i + 4]
            descriptors = section[i + 5 : i + 5 + es_length]
            language, ac3 = _descriptors(descriptors)
            if stream_type in _VIDEO_TYPES and video is None:
                video = pid
            elif stream_type in _AUDIO_TYPES or (stream_type == 0x06 and ac3):
                audio.append(AudioTrack(pid, stream_type, language))
            i += 5 + es_length
        if video is not None:
            self.video_pid = video
        if audio:
            self.audio = audio


def _resync(data: bytes, at: int) -> int:
    while True:
        at = data.find(b"\x47", at)
        if at < 0 or at + PACKET >= len(data) or data[at + PACKET] == 0x47:
            return at
        at += 1


def _section(payload: bytes) -> Optional[bytes]:
    if not payload:
        return None
    pointer = payload[0]
    section = payload[1 + pointer :]
    if len(section) < 12:
        return None
    length = ((section[1] & 0x0F) << 8) | section[2]
    if len(section) < 3 + length:
        return None  # a table over several packets: none here are that long
    return section[: 3 + length]


def _descriptors(data: bytes) -> tuple[str, bool]:
    language, ac3 = "", False
    i = 0
    while i + 2 <= len(data):
        tag, length = data[i], data[i + 1]
        value = data[i + 2 : i + 2 + length]
        if tag == 0x0A and len(value) >= 3 and not language:
            language = value[:3].decode("latin-1").strip().lower()
            if not language.isalpha() or language in ("und", "mis", "mul", "zxx"):
                language = ""
        elif tag in _AC3_DESCRIPTORS:
            ac3 = True
        i += 2 + length
    return language, ac3


def _pes_pts(data: bytes, off: int, stop: int) -> Optional[int]:
    if stop - off < 14 or data[off : off + 3] != b"\x00\x00\x01":
        return None
    flags = data[off + 7]
    if not flags & 0x80:
        return None
    p = off + 9
    return (
        ((data[p] >> 1) & 0x07) << 30
        | data[p + 1] << 22
        | (data[p + 2] >> 1) << 15
        | data[p + 3] << 7
        | data[p + 4] >> 1
    )


class StreamClock:
    """VLC's sound blocks on the time-shift timeline, for one run of the reader."""

    def __init__(self, segment_start: float, times: TsTimes) -> None:
        self.segment_start = segment_start
        self.times = times
        self._first_block: Optional[int] = None  # VLC µs
        self._expected: Optional[int] = None
        self.broken = False  # VLC's blocks stepped: a gap or a clock reset inside VLC

    def ready(self) -> bool:
        return self.times.first_video_pts is not None and self.times.first_audio_pts is not None

    def place(self, vlc_us: int, samples: int, rate: int) -> Optional[float]:
        """The timeline second a block that VLC stamped `vlc_us` starts at; None until the
        stream's first stamps are known, and after the run broke."""
        if self.broken or not self.ready():
            return None
        if self._first_block is None:
            self._first_block = vlc_us
        elif self._expected is not None and abs(vlc_us - self._expected) > 500_000:
            self.broken = True
            return None
        self._expected = vlc_us + samples * 1_000_000 // max(1, rate)
        assert self.times.first_audio_pts is not None and self.times.first_video_pts is not None
        offset = pts_seconds(self.times.first_audio_pts, self.times.first_video_pts)
        return self.segment_start + offset + (vlc_us - self._first_block) / 1e6
