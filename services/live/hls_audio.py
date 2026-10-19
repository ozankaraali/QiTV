"""Weaving a separate HLS audio rendition into the video variant's MPEG-TS (2.2).

About one public HLS channel in eight names its audio in a playlist of its own, its video
variant's TS carrying none. The time-shift helper used to fetch only the variant and leave such
a channel to direct play (no rewind). `HlsSource` now fetches both and hands each video segment
with the audio segment of the same media sequence number to `AudioWeaver.weave`, which returns
one TS: the video's packets as they came, the audio's PES on a PID of their own listed in the
video's PMT, each woven in once the video's clock (its PCR) is within `_LEAD_TICKS` of the
audio's PTS, so VLC always holds the sound before it is due. Both kinds of audio segment the
public channels use are read: MPEG-TS (its audio stream's packets, under its own stream type
and descriptors) and packed ADTS AAC (raw frames after an ID3 tag whose PRIV
com.apple.streaming.transportStreamTimestamp dates the first one; each frame becomes a PES).
The renditions share the video's timestamps (HLS requires it), so nothing is re-timed. fMP4
(CMAF) audio comes here remuxed to TS (`HlsSource`, `services.live.fmp4`). Anything
unreadable leaves the video segment as it came. A capture (``QITV_CAPTURE_DIR``) keeps the
audio segments in a pair of their own; `weave_capture` weaves them in again, so it replays
with sound.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import logging

from services.live.ts import (
    PACKET,
    SYNC,
    _crc32,
    _pcr_base,
    _pes_times,
    _section_at,
    pts_delta,
)

logger = logging.getLogger(__name__)

_LEAD_TICKS = 90000  # audio is woven in up to a second before the video's clock reaches it
_AUDIO_TYPES = (0x03, 0x04, 0x0F, 0x11, 0x81, 0x87)  # MPEG audio, AAC, LATM, AC-3, E-AC-3
_DVB_AUDIO = (0x6A, 0x7A, 0x7C)  # private PES (0x06) with an AC-3, E-AC-3 or AAC descriptor
_ADTS_RATES = (96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000)
_TIMESTAMP_OWNER = b"com.apple.streaming.transportStreamTimestamp\x00"
_PTS_MODULO = 1 << 33

Unit = tuple[int, list[bytes]]  # (PTS, the TS packets of one audio PES, on the audio's own PID)


class AudioWeaver:
    """Weaves audio segments into video segments, one pair at a time (see the module)."""

    def __init__(self) -> None:
        self._pid: int | None = None  # the audio's PID in the woven TS, kept once chosen
        self._type = 0  # its stream type ...
        self._info = b""  # ... and ES descriptors in the PMT
        self._cc = 0  # the counter of the packets built from packed audio
        self._next_pts: int | None = None  # packed audio: where its last segment ended
        self.woven = 0  # segments woven so far

    def weave(self, video: bytes, audio: bytes) -> bytes:
        """`video` with `audio`'s sound in it; `video` as it came when either is unreadable."""
        tables = _program(video)
        if tables is None:
            return video
        pmt_pid, used = tables
        read = _ts_audio(audio) if audio[:1] == bytes([SYNC]) else self._packed(audio)
        if read is None:
            return video
        units, stream_type, info, own_pid = read
        if not units:
            return video
        if self._pid is None:
            self._pid = own_pid if own_pid not in used else _free_pid(used)
            self._type, self._info = stream_type, info
        pid = self._pid
        packets = [video[at : at + PACKET] for at in range(0, len(video) - PACKET + 1, PACKET)]
        out: list[bytes] = []
        clock: int | None = None
        k = 0
        for pkt in packets:
            if pkt[0] == SYNC:
                b3 = pkt[3]
                if b3 & 0x20 and pkt[4] >= 7 and pkt[5] & 0x10:
                    clock = _pcr_base(pkt, 6)
                elif clock is None and pkt[1] & 0x40:
                    clock = _pes_times(pkt, 0)[0]  # no PCR yet: the first picture's time
                if pkt[1] & 0x40 and ((pkt[1] & 0x1F) << 8 | pkt[2]) == pmt_pid:
                    listed = self._listed(pkt, pmt_pid)
                    if listed is None:
                        return video  # its PMT cannot take the audio: as it came
                    pkt = listed
                while clock is not None and k < len(units):
                    if pts_delta((clock + _LEAD_TICKS) % _PTS_MODULO, units[k][0]) > 0:
                        break
                    out.extend(_on_pid(p, pid) for p in units[k][1])
                    k += 1
            out.append(pkt)
        for _, unit in units[k:]:
            out.extend(_on_pid(p, pid) for p in unit)
        self.woven += 1
        return b"".join(out) + video[len(packets) * PACKET :]

    def _listed(self, pkt: bytes, pmt_pid: int) -> bytes | None:
        """A PMT packet that lists the audio (as it came when it already does); None when its
        section does not fit one packet with it."""
        section = _section_at(pkt, 0)
        if section is None or section[0] != 0x02 or _crc32(section):
            return pkt
        assert self._pid is not None
        end = len(section) - 4
        i = 12 + (((section[10] & 0x0F) << 8) | section[11])
        while i + 5 <= end:
            if ((section[i + 1] & 0x1F) << 8) | section[i + 2] == self._pid:
                return pkt
            i += 5 + (((section[i + 3] & 0x0F) << 8) | section[i + 4])
        entry = bytes([self._type, 0xE0 | self._pid >> 8, self._pid & 0xFF])
        entry += bytes([0xF0 | len(self._info) >> 8, len(self._info) & 0xFF]) + self._info
        body = bytearray(section[:end] + entry)
        length = len(body) - 3 + 4
        body[1] = (body[1] & 0xF0) | (length >> 8)
        body[2] = length & 0xFF
        body += _crc32(bytes(body)).to_bytes(4, "big")
        afc = (pkt[3] >> 4) & 3
        start = 5 + pkt[4] if afc & 2 else 4
        head = pkt[: start + 1 + pkt[start]]  # through the pointer field and what it skips
        if len(head) + len(body) > PACKET and afc & 2 and pkt[4] and not pkt[5]:
            # An adaptation field that only stuffs (one packager fills its PMT packet so): left
            # out, the section's room is the packet's.
            head = bytes([pkt[0], pkt[1], pkt[2], (pkt[3] & 0xCF) | 0x10]) + pkt[start:]
            head = head[: 5 + head[4]]
        if len(head) + len(body) > PACKET:
            logger.info("The HLS video's PMT cannot take its audio; it plays without.")
            return None
        return head + bytes(body) + b"\xff" * (PACKET - len(head) - len(body))

    def _packed(self, data: bytes) -> tuple[list[Unit], int, bytes, int] | None:
        """Packed ADTS audio as PES units (one per frame), on a PID this weaver numbers."""
        pos, pts = 0, None
        while data[pos : pos + 3] == b"ID3" and pos + 10 <= len(data):
            size = (
                (data[pos + 6] << 21) | (data[pos + 7] << 14) | (data[pos + 8] << 7) | data[pos + 9]
            )
            tag = data[pos + 10 : pos + 10 + size]
            at = tag.find(_TIMESTAMP_OWNER)
            if at >= 0 and at + len(_TIMESTAMP_OWNER) + 8 <= len(tag):
                raw = tag[at + len(_TIMESTAMP_OWNER) : at + len(_TIMESTAMP_OWNER) + 8]
                pts = int.from_bytes(raw, "big") % _PTS_MODULO
            pos += 10 + size + (10 if data[pos + 5] & 0x10 else 0)
        if pts is None:
            pts = self._next_pts
        if pts is None:
            return None  # undated, and nothing before it to follow on from
        units: list[Unit] = []
        elapsed = 0.0
        while pos + 7 <= len(data):
            if data[pos] != 0xFF or data[pos + 1] & 0xF6 != 0xF0:
                break  # not ADTS (or damaged): what came before it is kept
            length = ((data[pos + 3] & 0x03) << 11) | (data[pos + 4] << 3) | (data[pos + 5] >> 5)
            rate_index = (data[pos + 2] >> 2) & 0x0F
            if length < 7 or rate_index >= len(_ADTS_RATES) or pos + length > len(data):
                break
            at = (pts + round(elapsed)) % _PTS_MODULO
            units.append((at, self._pes(data[pos : pos + length], at)))
            elapsed += 1024 * 90000 / _ADTS_RATES[rate_index]
            pos += length
        self._next_pts = (pts + round(elapsed)) % _PTS_MODULO
        return units, 0x0F, b"", 0

    def _pes(self, frame: bytes, pts: int) -> list[bytes]:
        """One audio frame as a PES (stream 0xC0, its PTS) in TS packets on PID 0 (`_on_pid`
        moves them); the last packet stuffed by its adaptation field."""
        stamp = bytes(
            [
                0x21 | ((pts >> 29) & 0x0E),
                (pts >> 22) & 0xFF,
                ((pts >> 14) & 0xFE) | 1,
                (pts >> 7) & 0xFF,
                ((pts << 1) & 0xFE) | 1,
            ]
        )
        length = 3 + 5 + len(frame)
        pes = b"\x00\x00\x01\xc0" + length.to_bytes(2, "big") + b"\x80\x80\x05" + stamp + frame
        packets = []
        for at in range(0, len(pes), 184):
            chunk = pes[at : at + 184]
            head = bytes([SYNC, 0x40 if at == 0 else 0x00, 0x00])
            if len(chunk) < 184:  # stuffing: an adaptation field fills the rest
                room = 184 - len(chunk)
                field = bytes([room - 1]) + (b"\x00" + b"\xff" * (room - 2) if room > 1 else b"")
                packets.append(head + bytes([0x30 | self._cc]) + field + chunk)
            else:
                packets.append(head + bytes([0x10 | self._cc]) + chunk)
            self._cc = (self._cc + 1) & 0x0F
        return packets


def weave_capture(video: Iterable[tuple[int, bytes]], audio: Mapping[int, bytes]) -> list[bytes]:
    """A capture's video segments, (sequence, bytes), woven again as `HlsSource` wove them:
    each with the captured audio segment of its sequence when there is one (else silent), by
    one weaver for them all (`services.live.capture` saves the audio in a pair of its own)."""
    weaver = AudioWeaver()
    return [
        weaver.weave(segment, audio[sequence]) if audio.get(sequence) else segment
        for sequence, segment in video
    ]


def _program(data: bytes) -> tuple[int, set[int]] | None:
    """The first program's PMT PID and every PID it uses (PAT, PMT, PCR, streams), from the
    first PAT and PMT in `data`; None without them (single-packet sections)."""
    pmt_pid: int | None = None
    for at in range(0, len(data) - PACKET + 1, PACKET):
        if data[at] != SYNC or not data[at + 1] & 0x40:
            continue
        pid = ((data[at + 1] & 0x1F) << 8) | data[at + 2]
        if pid != 0 and pid != pmt_pid:
            continue
        section = _section_at(data, at)
        if section is None or _crc32(section):
            continue
        if pid == 0 and section[0] == 0x00:
            for i in range(8, len(section) - 4 - 3, 4):
                if (section[i] << 8) | section[i + 1]:
                    pmt_pid = ((section[i + 2] & 0x1F) << 8) | section[i + 3]
                    break
        elif pid == pmt_pid and section[0] == 0x02:
            used = {0, pmt_pid, ((section[8] & 0x1F) << 8) | section[9]}
            end = len(section) - 4
            i = 12 + (((section[10] & 0x0F) << 8) | section[11])
            while i + 5 <= end:
                used.add(((section[i + 1] & 0x1F) << 8) | section[i + 2])
                i += 5 + (((section[i + 3] & 0x0F) << 8) | section[i + 4])
            return pmt_pid, used
    return None


def _ts_audio(data: bytes) -> tuple[list[Unit], int, bytes, int] | None:
    """An audio TS segment's first audio stream: its PES as units, stream type, descriptors
    and PID; None without one."""
    tables = _program(data)
    if tables is None:
        return None
    pmt_pid, _ = tables
    stream: tuple[int, int, bytes] | None = None
    for at in range(0, len(data) - PACKET + 1, PACKET):
        if (
            data[at] == SYNC
            and data[at + 1] & 0x40
            and (data[at + 1] & 0x1F) << 8 | data[at + 2] == pmt_pid
        ):
            section = _section_at(data, at)
            if section is not None:
                stream = _first_audio(section)
                break
    if stream is None:
        return None
    pid, stream_type, info = stream
    units: list[Unit] = []
    for at in range(0, len(data) - PACKET + 1, PACKET):
        pkt = data[at : at + PACKET]
        if pkt[0] != SYNC or ((pkt[1] & 0x1F) << 8 | pkt[2]) != pid or not pkt[3] & 0x10:
            continue
        if pkt[1] & 0x40:
            pts = _pes_times(pkt, 0)[0]
            if pts is None:
                pts = units[-1][0] if units else None
            if pts is not None:
                units.append((pts, [pkt]))
        elif units:
            units[-1][1].append(pkt)
    return units, stream_type, info, pid


def _first_audio(section: bytes) -> tuple[int, int, bytes] | None:
    """(PID, stream type, descriptors) of a PMT section's first audio stream."""
    end = len(section) - 4
    i = 12 + (((section[10] & 0x0F) << 8) | section[11])
    while i + 5 <= end:
        stream_type = section[i]
        pid = ((section[i + 1] & 0x1F) << 8) | section[i + 2]
        info_end = i + 5 + (((section[i + 3] & 0x0F) << 8) | section[i + 4])
        info = section[i + 5 : min(info_end, end)]
        dvb, j = False, 0
        while stream_type == 0x06 and j + 2 <= len(info):
            dvb = dvb or info[j] in _DVB_AUDIO
            j += 2 + info[j + 1]
        if stream_type in _AUDIO_TYPES or dvb:
            return pid, stream_type, bytes(info)
        i = info_end
    return None


def _free_pid(used: set[int]) -> int:
    """The first PID from 0x101 the video's program does not use."""
    pid = 0x101
    while pid in used:
        pid += 1
    return pid


def _on_pid(pkt: bytes, pid: int) -> bytes:
    return bytes([pkt[0], (pkt[1] & 0xE0) | pid >> 8, pid & 0xFF]) + pkt[3:]
