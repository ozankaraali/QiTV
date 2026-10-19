"""Fragmented MP4 (CMAF) HLS segments as MPEG-TS.

HLS channels whose playlists name an init segment (EXT-X-MAP) send fragmented MP4, which the
time-shift helper's pipeline (MPEG-TS throughout) cannot read: they played directly, without
rewind. `Fmp4Remuxer` turns them into TS as they come, one segment at a time:

- `Fmp4Remuxer.init` reads an init segment (`read_init`): each track's codec, timescale, edit
  list and trex defaults; its first video and first audio track are kept.
- `Fmp4Remuxer.remux` reads a media segment's `moof`/`mdat` pairs into samples (`read_segment`:
  DTS, PTS, duration, keyframe) and writes them as one TS: PAT and PMT first, then one PES per
  sample in DTS order. H.264 and HEVC become Annex B with an access unit delimiter and the
  init's parameter sets before each keyframe that has none (`annex_b`); AAC gets ADTS headers;
  AC-3, E-AC-3 and MPEG audio go as they are. The PCR comes with each video PES (each audio
  PES without video), `_PCR_LEAD` before its DTS, as ffmpeg's muxer writes it.

Timestamps are each track's media time less its edit list's shift, at 90 kHz modulo 2^33:
HLS renditions share one timeline, so a separate audio rendition remuxed on its own lines up
with its video. Encrypted tracks (CENC, or SAMPLE-AES inside the samples) and other codecs
raise `Fmp4Error`: those channels keep playing directly.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
import logging
import struct
from typing import NamedTuple

from services.live.ts import PACKET, SYNC, _crc32

logger = logging.getLogger(__name__)

ENCRYPTED = "The HLS channel's fragmented MP4 is encrypted."
UNSUPPORTED = "The HLS channel's fragmented MP4 uses an unsupported codec."
DAMAGED = "The HLS channel's fragmented MP4 is damaged."
NO_INIT = "The HLS channel's fragmented MP4 has no init segment."

_CLOCK = 90000
_PTS_MODULO = 1 << 33
_PCR_LEAD = 63000  # the PCR runs 0.7 s before the DTS it comes with (ffmpeg's mux delay)
_PMT_PID = 0x1000
_VIDEO_PID = 0x100
_AUDIO_PID = 0x101
_PAYLOAD = PACKET - 4

_VIDEO_ENTRIES = {b"avc1": "h264", b"avc3": "h264", b"hvc1": "hevc", b"hev1": "hevc"}
_AUDIO_ENTRIES = {b"mp4a": "mp4a", b"ac-3": "ac3", b"ec-3": "eac3", b".mp3": "mp3"}
_ENCRYPTED_ENTRIES = (b"encv", b"enca")
_ENCRYPTION_BOXES = (b"senc", b"saiz", b"saio")  # a track fragment's per-sample encryption
_STREAM_TYPES = {"h264": 0x1B, "hevc": 0x24, "aac": 0x0F, "ac3": 0x81, "eac3": 0x87}
_STREAM_IDS = {"aac": 0xC0, "mp3": 0xC0, "ac3": 0xBD, "eac3": 0xBD}
_VIDEO_STREAM = 0xE0
_AAC_OBJECTS = (0x40, 0x66, 0x67, 0x68)  # MPEG-4 audio; MPEG-2 AAC main, LC, SSR
_MPEG_AUDIO = {0x6B: 0x03, 0x69: 0x04}  # MPEG-1, MPEG-2 audio (layers 1-3)
_SBR, _PS = 5, 29  # HE-AAC's object types: ADTS names the core (AAC LC) beneath them
_NON_SYNC = 0x10000  # sample_is_non_sync_sample in a sample's flags
# An edit list's media time past this (seconds) is no B-frame offset or AAC priming: left out.
_EDIT_LIMIT_S = 10.0

_H264_AUD = b"\x00\x00\x00\x01\x09\xf0"
_HEVC_AUD = b"\x00\x00\x00\x01\x46\x01\x50"
_START = b"\x00\x00\x00\x01"
_H264_SETS = (7, 8)  # SPS, PPS
_HEVC_SETS = (32, 33, 34)  # VPS, SPS, PPS
_HEVC_IRAP = range(16, 22)  # BLA, IDR, CRA pictures
_H264_IDR = 5


class Fmp4Error(Exception):
    """An init or media segment that cannot be remuxed: `str(error)` says why."""


@contextmanager
def _reading() -> Iterator[None]:
    """A box whose fields point past its data raises Fmp4Error(DAMAGED), whatever the reading
    of it met first."""
    try:
        yield
    except (struct.error, IndexError, ValueError, KeyError, OverflowError) as error:
        raise Fmp4Error(DAMAGED) from error


@dataclass(frozen=True)
class Track:
    """One track of an init segment, as `Fmp4Remuxer` writes it."""

    track_id: int
    kind: str  # "video" or "audio"
    codec: str  # "h264", "hevc", "aac", "ac3", "eac3" or "mp3"
    timescale: int
    stream_type: int
    shift: int = 0  # the edit list's media time at presentation 0, less an empty edit's delay
    edit_media: int = 0  # ... that media time alone (0: no edit list, or one left out)
    decode_shift: int = 0  # `cslg`'s composition-to-DTS shift: how far DTS goes before a PTS
    nal_size: int = 4  # H.264/HEVC: the bytes of each NAL unit's length ...
    parameter_sets: bytes = b""  # ... and the init's parameter sets, Annex B
    adts: tuple[int, int, int] = (1, 3, 2)  # AAC: ADTS profile, sampling index, channels
    language: str = ""  # ISO 639-2; "" when undetermined
    default_duration: int = 0  # trex's defaults
    default_size: int = 0
    default_flags: int | None = None  # None: trex gives none (zero)


class Sample(NamedTuple):
    """One sample of a media segment, its times in its track's timescale (shift applied)."""

    track: Track
    dts: int
    pts: int
    duration: int
    key: bool
    data: memoryview


class _Entry(NamedTuple):
    codec: str
    stream_type: int
    nal_size: int = 4
    parameter_sets: bytes = b""
    adts: tuple[int, int, int] = (1, 3, 2)


def _boxes_at(
    data: memoryview, start: int = 0, end: int | None = None
) -> Iterator[tuple[bytes, int, int, int]]:
    """(type, start, body start, end) of each box in `data[start:end]`."""
    end = len(data) if end is None else end
    at = start
    while at + 8 <= end:
        size, kind = struct.unpack_from(">I4s", data, at)
        head = 8
        if size == 1:
            if at + 16 > end:
                raise Fmp4Error(DAMAGED)
            size = struct.unpack_from(">Q", data, at + 8)[0]
            head = 16
        elif size == 0:
            size = end - at
        if size < head or at + size > end:
            raise Fmp4Error(DAMAGED)
        yield kind, at, at + head, at + size
        at += size


def _boxes(
    data: memoryview, start: int = 0, end: int | None = None
) -> Iterator[tuple[bytes, int, int]]:
    """(type, body start, end) of each box in `data[start:end]`."""
    for kind, _start, body, stop in _boxes_at(data, start, end):
        yield kind, body, stop


def _child(data: memoryview, start: int, end: int, kind: bytes) -> tuple[int, int] | None:
    for found, body, stop in _boxes(data, start, end):
        if found == kind:
            return body, stop
    return None


def _path(data: memoryview, start: int, end: int, *kinds: bytes) -> tuple[int, int] | None:
    span: tuple[int, int] | None = (start, end)
    for kind in kinds:
        if span is None:
            return None
        span = _child(data, span[0], span[1], kind)
    return span


def _u16(data: memoryview, at: int) -> int:
    return int(struct.unpack_from(">H", data, at)[0])


def _u32(data: memoryview, at: int) -> int:
    return int(struct.unpack_from(">I", data, at)[0])


def _u64(data: memoryview, at: int) -> int:
    return int(struct.unpack_from(">Q", data, at)[0])


def _s32(data: memoryview, at: int) -> int:
    return int(struct.unpack_from(">i", data, at)[0])


def _language(data: memoryview, at: int) -> str:
    code = _u16(data, at)
    text = "".join(chr(((code >> shift) & 0x1F) + 0x60) for shift in (10, 5, 0))
    return "" if text == "und" or not text.isalpha() else text


def _edits(
    data: memoryview, span: tuple[int, int] | None, movie_scale: int, scale: int
) -> tuple[int, int]:
    """An edit list's (media time of its first edit, delay of the empty edits before it), in
    the track's timescale; a media time past `_EDIT_LIMIT_S` (no B-frame offset or AAC
    priming: a packager's leftover) is left out (0)."""
    if span is None:
        return 0, 0
    body, end = span
    version = data[body]
    at, delay = body + 8, 0
    for _ in range(min(_u32(data, body + 4), 64)):
        if at + (20 if version == 1 else 12) > end:
            raise Fmp4Error(DAMAGED)
        if version == 1:
            duration, media = _u64(data, at), int(struct.unpack_from(">q", data, at + 8)[0])
            at += 20
        else:
            duration, media = _u32(data, at), _s32(data, at + 4)
            at += 12
        if media == -1:
            delay += duration * scale // max(1, movie_scale)
            continue
        if abs(media) > _EDIT_LIMIT_S * scale:
            logger.info(
                "A fragmented MP4 edit list's media time is left out (%.1f s).", media / scale
            )
            media = 0
        return media, delay
    return 0, delay


def _decode_shift(data: memoryview, stbl: tuple[int, int] | None) -> int:
    """`cslg`'s compositionToDTSShift (negative composition offsets need DTS that much
    earlier); 0 without one."""
    cslg = _child(data, *stbl, b"cslg") if stbl is not None else None
    if cslg is None or cslg[1] - cslg[0] < 8:
        return 0
    if data[cslg[0]] == 1:
        return max(0, int(struct.unpack_from(">q", data, cslg[0] + 4)[0]))
    return max(0, _s32(data, cslg[0] + 4))


def _parameter_sets(data: memoryview, at: int, end: int, count: int) -> tuple[int, bytes]:
    """`count` NAL units with 16-bit lengths from `at`, as Annex B; and where they end."""
    sets = bytearray()
    for _ in range(count):
        if at + 2 > end or at + 2 + _u16(data, at) > end:
            raise Fmp4Error(DAMAGED)
        length = _u16(data, at)
        sets += _START + data[at + 2 : at + 2 + length]
        at += 2 + length
    return at, bytes(sets)


def _avcc(data: memoryview, body: int, end: int) -> tuple[int, bytes]:
    """(NAL length size, SPS and PPS as Annex B) of an `avcC`."""
    if end - body < 7:
        raise Fmp4Error(DAMAGED)
    at, sps = _parameter_sets(data, body + 6, end, data[body + 5] & 0x1F)
    if at >= end:
        raise Fmp4Error(DAMAGED)
    _at, pps = _parameter_sets(data, at + 1, end, data[at])
    return (data[body + 4] & 3) + 1, sps + pps


def _hvcc(data: memoryview, body: int, end: int) -> tuple[int, bytes]:
    """(NAL length size, its arrays' NAL units (VPS, SPS, PPS, SEI) as Annex B) of an `hvcC`."""
    if end - body < 23:
        raise Fmp4Error(DAMAGED)
    sets = b""
    at = body + 23
    for _ in range(data[body + 22]):
        if at + 3 > end:
            raise Fmp4Error(DAMAGED)
        at, units = _parameter_sets(data, at + 3, end, _u16(data, at + 1))
        sets += units
    return (data[body + 21] & 3) + 1, sets


def _descriptor(data: memoryview, at: int, end: int) -> tuple[int, int, int]:
    """(tag, body start, body end) of the MPEG-4 descriptor at `at` (ISO 14496-1)."""
    if at + 2 > end:
        raise Fmp4Error(DAMAGED)
    tag, length, at = data[at], 0, at + 1
    for _ in range(4):
        byte = data[at]
        at += 1
        length = length << 7 | byte & 0x7F
        if not byte & 0x80 or at >= end:
            break
    return tag, at, min(end, at + length)


class _Bits:
    def __init__(self, data: bytes) -> None:
        self._value, self._left = int.from_bytes(data, "big"), len(data) * 8

    def read(self, count: int) -> int:
        if count > self._left:
            raise Fmp4Error(DAMAGED)
        self._left -= count
        return (self._value >> self._left) & ((1 << count) - 1)


def _audio_config(config: bytes) -> tuple[int, int, int]:
    """(ADTS profile, sampling index, channels) of an AudioSpecificConfig; HE-AAC's core."""
    bits = _Bits(config)
    kind = bits.read(5)
    if kind == 31:
        kind = 32 + bits.read(6)
    rate = bits.read(4)
    if rate == 15:
        raise Fmp4Error(UNSUPPORTED)  # an explicit frequency: ADTS cannot name it
    channels = bits.read(4)
    if kind in (_SBR, _PS):
        if bits.read(4) == 15:  # the extension's sampling index
            bits.read(24)
        kind = bits.read(5)
    if not 1 <= kind <= 4 or not 1 <= channels <= 7:
        raise Fmp4Error(UNSUPPORTED)  # ADTS names AAC main, LC, SSR and LTP; PCE layouts not
    return kind - 1, rate, channels


def _esds(data: memoryview, body: int, end: int) -> _Entry:
    tag, at, stop = _descriptor(data, body + 4, end)  # past the full box's version and flags
    if tag != 0x03 or stop - at < 3:
        raise Fmp4Error(DAMAGED)
    flags = data[at + 2]
    at += 3
    if flags & 0x80:  # streamDependenceFlag
        at += 2
    if flags & 0x40:  # URL_Flag
        at += 1 + data[at]
    if flags & 0x20:  # OCRstreamFlag
        at += 2
    tag, at, stop = _descriptor(data, at, stop)
    if tag != 0x04 or stop - at < 13:
        raise Fmp4Error(DAMAGED)
    kind = data[at]
    if kind in _MPEG_AUDIO:
        return _Entry("mp3", _MPEG_AUDIO[kind])
    if kind not in _AAC_OBJECTS:
        raise Fmp4Error(UNSUPPORTED)
    tag, info, info_end = _descriptor(data, at + 13, stop)
    if tag != 0x05:
        raise Fmp4Error(DAMAGED)
    profile, rate, channels = _audio_config(bytes(data[info:info_end]))
    if kind != 0x40:  # MPEG-2 AAC: its object type is the profile
        profile = kind - 0x66
    return _Entry("aac", _STREAM_TYPES["aac"], adts=(profile, rate, channels))


def _entry(data: memoryview, at: int, stop: int, kind: str) -> _Entry:
    """What `Track` takes from a sample entry (a track's `stsd`'s first)."""
    entry = bytes(data[at + 4 : at + 8])
    body = at + 8
    if entry in _ENCRYPTED_ENTRIES:
        raise Fmp4Error(ENCRYPTED)
    if kind == "video":
        codec = _VIDEO_ENTRIES.get(entry)
        if codec is None:
            raise Fmp4Error(UNSUPPORTED)
        config = _child(data, body + 78, stop, b"avcC" if codec == "h264" else b"hvcC")
        if config is None:
            raise Fmp4Error(DAMAGED)
        size, sets = (_avcc if codec == "h264" else _hvcc)(data, *config)
        return _Entry(codec, _STREAM_TYPES[codec], size, sets)
    audio = _AUDIO_ENTRIES.get(entry)
    if audio is None:
        raise Fmp4Error(UNSUPPORTED)
    if audio in ("ac3", "eac3"):
        return _Entry(audio, _STREAM_TYPES[audio])
    if audio == "mp3":
        return _Entry("mp3", _MPEG_AUDIO[0x6B])
    version = _u16(data, body + 8)  # QuickTime's sound descriptions add fields after v0's
    esds = _child(data, body + 28 + {1: 16, 2: 36}.get(version, 0), stop, b"esds")
    if esds is None:
        raise Fmp4Error(DAMAGED)
    return _esds(data, *esds)


def read_init(data: bytes | memoryview) -> dict[int, Track]:
    """The tracks of an init segment by track ID: its first video and first audio track.

    Raises `Fmp4Error`: no `moov`; encryption (`encv`, `enca`, `pssh`); a video track, or
    every audio track, of a codec not remuxed; damaged boxes."""
    with _reading():
        return _read_init(memoryview(data))


def _read_init(view: memoryview) -> dict[int, Track]:
    moov = _child(view, 0, len(view), b"moov")
    if moov is None:
        raise Fmp4Error(NO_INIT)
    if _child(view, *moov, b"pssh") is not None:
        raise Fmp4Error(ENCRYPTED)
    movie_scale = 1000
    mvhd = _child(view, *moov, b"mvhd")
    if mvhd is not None:
        movie_scale = _u32(view, mvhd[0] + (20 if view[mvhd[0]] == 1 else 12)) or 1000
    defaults: dict[int, tuple[int, int, int]] = {}
    mvex = _child(view, *moov, b"mvex")
    for kind, body, _end in _boxes(view, *mvex) if mvex is not None else ():
        if kind == b"trex":
            defaults[_u32(view, body + 4)] = (
                _u32(view, body + 12),
                _u32(view, body + 16),
                _u32(view, body + 20),
            )
    tracks: dict[int, Track] = {}
    refused: dict[str, Fmp4Error] = {}
    for kind, body, end in _boxes(view, *moov):
        if kind != b"trak":
            continue
        tkhd = _child(view, body, end, b"tkhd")
        mdhd = _path(view, body, end, b"mdia", b"mdhd")
        hdlr = _path(view, body, end, b"mdia", b"hdlr")
        stsd = _path(view, body, end, b"mdia", b"minf", b"stbl", b"stsd")
        if tkhd is None or mdhd is None or hdlr is None or stsd is None:
            continue
        track_kind = {b"vide": "video", b"soun": "audio"}.get(
            bytes(view[hdlr[0] + 8 : hdlr[0] + 12])
        )
        if track_kind is None or any(track.kind == track_kind for track in tracks.values()):
            continue
        track_id = _u32(view, tkhd[0] + (20 if view[tkhd[0]] == 1 else 12))
        long = view[mdhd[0]] == 1
        scale = _u32(view, mdhd[0] + (20 if long else 12))
        entries = list(_boxes_at(view, stsd[0] + 8, stsd[1]))
        if not scale or not entries:
            raise Fmp4Error(DAMAGED)
        try:
            entry = _entry(view, entries[0][1], entries[0][3], track_kind)
        except Fmp4Error as error:
            if str(error) == ENCRYPTED:
                raise
            refused[track_kind] = error
            continue
        duration, size, flags = defaults.get(track_id, (0, 0, 0))
        media, delay = _edits(view, _path(view, body, end, b"edts", b"elst"), movie_scale, scale)
        tracks[track_id] = Track(
            track_id=track_id,
            kind=track_kind,
            codec=entry.codec,
            timescale=scale,
            stream_type=entry.stream_type,
            shift=media - delay,
            edit_media=media,
            decode_shift=_decode_shift(view, _path(view, body, end, b"mdia", b"minf", b"stbl")),
            nal_size=entry.nal_size,
            parameter_sets=entry.parameter_sets,
            adts=entry.adts,
            language=_language(view, mdhd[0] + (32 if long else 20)),
            default_duration=duration,
            default_size=size,
            default_flags=flags or None,
        )
    kinds = {track.kind for track in tracks.values()}
    for track_kind, refusal in refused.items():
        if track_kind not in kinds:
            raise refusal
    if not tracks:
        raise Fmp4Error(UNSUPPORTED)
    return tracks


def _key(track: Track, data: memoryview, flags: int | None) -> bool:
    """Whether a sample is a keyframe: an audio one always; a video one when its flags say it
    syncs, or (with or without flags) when it holds an IDR (H.264) or IRAP (HEVC) picture."""
    if track.kind == "audio" or (flags is not None and not flags & _NON_SYNC):
        return True
    hevc = track.codec == "hevc"
    size, at, end = track.nal_size, 0, len(data)
    while at + size < end:
        length = int.from_bytes(data[at : at + size], "big")
        header = data[at + size]
        if ((header >> 1) & 0x3F in _HEVC_IRAP) if hevc else (header & 0x1F == _H264_IDR):
            return True
        at += size + length
    return False


def read_segment(
    data: bytes | memoryview, tracks: dict[int, Track], ends: dict[int, int] | None = None
) -> list[Sample]:
    """The samples of a media segment's `moof`/`mdat` pairs for `tracks`, each track's in
    order. `ends` keeps each track's end (its next DTS) across segments: a track fragment
    without `tfdt` follows on from it.

    Raises `Fmp4Error`: encrypted samples (`senc`, `saiz`, `saio`), data out of bounds, damaged
    boxes."""
    with _reading():
        return _read_segment(memoryview(data), tracks, {} if ends is None else ends)


def _read_segment(view: memoryview, tracks: dict[int, Track], ends: dict[int, int]) -> list[Sample]:
    samples: list[Sample] = []
    for kind, moof, body, end in _boxes_at(view):
        if kind != b"moof":
            continue
        data_end = moof  # the first track fragment's data counts from the moof by default
        for traf_kind, traf, traf_end in _boxes(view, body, end):
            if traf_kind == b"traf":
                data_end = _fragment(view, traf, traf_end, moof, data_end, tracks, ends, samples)
    return samples


def _fragment(
    view: memoryview,
    traf: int,
    traf_end: int,
    moof: int,
    previous_end: int,
    tracks: dict[int, Track],
    ends: dict[int, int],
    samples: list[Sample],
) -> int:
    """One track fragment's samples onto `samples`; returns where its data ends (the next
    track fragment's default base)."""
    tfhd = _child(view, traf, traf_end, b"tfhd")
    if tfhd is None or tfhd[1] - tfhd[0] < 8:
        raise Fmp4Error(DAMAGED)
    at = tfhd[0]
    flags = _u32(view, at) & 0xFFFFFF
    track = tracks.get(_u32(view, at + 4))
    at += 8
    base = previous_end
    if flags & 0x01:  # base-data-offset
        base = _u64(view, at)
        at += 8
    elif flags & 0x20000:  # default-base-is-moof
        base = moof
    if flags & 0x02:  # sample-description-index
        at += 4
    duration = size = 0
    sample_flags: int | None = None
    if track is not None:
        duration, size = track.default_duration, track.default_size
        sample_flags = track.default_flags
    if flags & 0x08:
        duration = _u32(view, at)
        at += 4
    if flags & 0x10:
        size = _u32(view, at)
        at += 4
    if flags & 0x20:
        sample_flags = _u32(view, at)
    dts: int | None = None
    for kind, body, _stop in _boxes(view, traf, traf_end):
        if kind in _ENCRYPTION_BOXES:
            raise Fmp4Error(ENCRYPTED)
        if kind == b"tfdt" and track is not None:
            dts = _u64(view, body + 4) if view[body] == 1 else _u32(view, body + 4)
    if dts is None and track is not None:
        dts = ends.get(track.track_id, 0)
    data_end = base
    for kind, body, run_end in _boxes(view, traf, traf_end):
        if kind != b"trun":
            continue
        run_flags = _u32(view, body) & 0xFFFFFF
        count = _u32(view, body + 4)
        at = body + 8
        offset = data_end
        if run_flags & 0x01:  # data-offset
            offset = base + _s32(view, at)
            at += 4
        first_flags = sample_flags
        if run_flags & 0x04:  # first-sample-flags
            first_flags = _u32(view, at)
            at += 4
        if at + count * 4 * bin(run_flags & 0xF00).count("1") > run_end:
            raise Fmp4Error(DAMAGED)
        for index in range(count):
            sample_duration, sample_size = duration, size
            flags_now = first_flags if index == 0 else sample_flags
            composition = 0
            if run_flags & 0x100:
                sample_duration = _u32(view, at)
                at += 4
            if run_flags & 0x200:
                sample_size = _u32(view, at)
                at += 4
            if run_flags & 0x400:
                flags_now = _u32(view, at)
                at += 4
            if run_flags & 0x800:
                # Signed in version 1; some packagers write negative ones in version 0 too,
                # where an offset past 2^31 means nothing else (ffmpeg reads both so).
                composition = _s32(view, at)
                at += 4
            if offset < 0 or offset + sample_size > len(view):
                raise Fmp4Error(DAMAGED)
            if track is not None and dts is not None:
                data = view[offset : offset + sample_size]
                start = dts - track.shift
                key = _key(track, data, flags_now)
                samples.append(
                    Sample(track, start, start + composition, sample_duration, key, data)
                )
                dts += sample_duration
            offset += sample_size
        data_end = offset
    if track is not None and dts is not None:
        ends[track.track_id] = dts
    return data_end


def _ticks(value: int, scale: int) -> int:
    """`value` in `scale` units at 90 kHz (not wrapped)."""
    return (value * _CLOCK + scale // 2) // scale


def _stamp(prefix: int, value: int) -> bytes:
    """A PES timestamp field: `prefix` (2: PTS alone, 3: PTS before a DTS, 1: DTS)."""
    value %= _PTS_MODULO
    return bytes(
        [
            prefix << 4 | (value >> 29) & 0x0E | 1,
            (value >> 22) & 0xFF,
            (value >> 14) & 0xFE | 1,
            (value >> 7) & 0xFF,
            (value << 1) & 0xFE | 1,
        ]
    )


def annex_b(track: Track, data: memoryview, key: bool) -> bytes:
    """A video sample as Annex B: an access unit delimiter first (the sample's own, else one
    added), then the init's parameter sets when it is a keyframe without its own, then its
    NAL units."""
    hevc = track.codec == "hevc"
    size, at, end = track.nal_size, 0, len(data)
    units: list[memoryview] = []
    while at < end:
        if at + size > end:
            raise Fmp4Error(DAMAGED)
        length = int.from_bytes(data[at : at + size], "big")
        at += size
        if at + length > end:
            raise Fmp4Error(DAMAGED)
        if length:
            units.append(data[at : at + length])
        at += length
    types = [(unit[0] >> 1) & 0x3F if hevc else unit[0] & 0x1F for unit in units]
    out = bytearray()
    if types and types[0] == (35 if hevc else 9):
        out += _START + units[0]
        units, types = units[1:], types[1:]
    else:
        out += _HEVC_AUD if hevc else _H264_AUD
    if key and not any(kind in (_HEVC_SETS if hevc else _H264_SETS) for kind in types):
        out += track.parameter_sets
    for unit in units:
        out += _START
        out += unit
    return bytes(out)


def _adts(track: Track, data: memoryview) -> bytes:
    """An AAC sample with its ADTS header."""
    profile, rate, channels = track.adts
    length = 7 + len(data)
    if length > 0x1FFF:
        raise Fmp4Error(DAMAGED)
    head = bytes(
        [
            0xFF,
            0xF1,
            profile << 6 | rate << 2 | channels >> 2,
            (channels & 3) << 6 | length >> 11,
            (length >> 3) & 0xFF,
            (length & 7) << 5 | 0x1F,
            0xFC,
        ]
    )
    return head + data


def _section(table: int, number: int, version: int, body: bytes) -> bytes:
    """A PSI section (PAT or PMT) with its CRC."""
    length = 5 + len(body) + 4
    head = bytes([table, 0xB0 | length >> 8, length & 0xFF]) + number.to_bytes(2, "big")
    section = head + bytes([0xC1 | version << 1, 0, 0]) + body
    return section + _crc32(section).to_bytes(4, "big")


class Fmp4Remuxer:
    """An HLS stream's fMP4 segments as MPEG-TS, one segment at a time (see the module): its
    continuity counters, PMT version and each track's end run on from segment to segment."""

    def __init__(self, *, pcr: bool = True) -> None:
        self._pcr = pcr  # False: no PCR (an audio rendition woven into its video's TS)
        self._tracks: dict[int, Track] = {}
        self._pids: dict[int, int] = {}
        self._pcr_pid = 0x1FFF
        self._version = 0
        self._counters: dict[int, int] = {}
        self._ends: dict[int, int] = {}
        # Each track's DTS shift (PTS never before its DTS), set by its first segment and kept:
        # DTS and the PCR never step back. A later frame that needs more has its DTS clamped.
        self._delays: dict[int, int] = {}
        self._clamped = False  # one was (logged once)
        self.segments = 0  # media segments remuxed so far

    @property
    def tracks(self) -> dict[int, Track]:
        return dict(self._tracks)

    @property
    def edit_media_s(self) -> float:
        """The longest edit list media time of its tracks, in seconds (0: none)."""
        return max(
            (abs(track.edit_media) / track.timescale for track in self._tracks.values()),
            default=0.0,
        )

    def drop_edit_media(self, beyond_s: float) -> None:
        """Leave out an edit list's media time longer than `beyond_s` (its empty edits' delay
        stays): a separate rendition whose other side has none to match (`HlsSource`)."""
        for track_id, track in list(self._tracks.items()):
            if abs(track.edit_media) > beyond_s * track.timescale:
                logger.info("A one-sided fragmented MP4 edit list's media time is left out.")
                self._tracks[track_id] = replace(
                    track, shift=track.shift - track.edit_media, edit_media=0
                )

    def init(self, data: bytes | memoryview) -> None:
        """Read an init segment (`read_init`); one unlike the last (codecs, track IDs) bumps
        the PMT's version."""
        tracks = read_init(data)
        if tracks == self._tracks:
            return
        if self._tracks:
            self._version = (self._version + 1) & 0x1F
            logger.info("The fragmented MP4's init segment changed.")
        self._tracks, self._ends, self._delays = tracks, {}, {}
        ordered = sorted(tracks.values(), key=lambda track: track.kind != "video")
        self._pids = {
            track.track_id: _VIDEO_PID if track.kind == "video" else _AUDIO_PID for track in ordered
        }
        self._pcr_pid = self._pids[ordered[0].track_id]

    def remux(self, data: bytes | memoryview) -> bytes:
        """A media segment as TS: PAT, PMT, then its samples' PES in DTS order. A segment that
        carries its own `moov` is read with it. Raises `Fmp4Error`: no init read yet, or as
        `read_init` and `read_segment` do."""
        with _reading():
            return self._remux(memoryview(data))

    def _remux(self, view: memoryview) -> bytes:
        if _child(view, 0, len(view), b"moov") is not None:
            self.init(view)
        if not self._tracks:
            raise Fmp4Error(NO_INIT)
        samples = read_segment(view, self._tracks, self._ends)
        delays = self._delays
        seeding: dict[int, int] = {}
        for sample in samples:
            # Negative composition offsets (trun v1) put a B-frame's PTS before its DTS, which
            # TS forbids: DTS goes that much earlier (the PTS, presentation, stay), by the most
            # a track's first segment needs (or its cslg's), kept from then on.
            track = sample.track
            if track.track_id not in delays:
                need = max(track.decode_shift, sample.dts - sample.pts)
                seeding[track.track_id] = max(seeding.get(track.track_id, 0), need)
        delays.update(seeding)
        timed = [
            (
                _ticks(sample.dts - delays[sample.track.track_id], sample.track.timescale),
                sample.track.kind != "video",
                k,
            )
            for k, sample in enumerate(samples)
        ]
        timed.sort()
        out = [self._psi(0, self._pat()), self._psi(_PMT_PID, self._pmt())]
        for dts, _audio, k in timed:
            out.extend(self._pes(samples[k], dts))
        self.segments += 1
        return b"".join(out)

    def _pat(self) -> bytes:
        return _section(
            0x00, 1, self._version, b"\x00\x01" + (0xE000 | _PMT_PID).to_bytes(2, "big")
        )

    def _pmt(self) -> bytes:
        body = bytearray((0xE000 | self._pcr_pid).to_bytes(2, "big") + b"\xf0\x00")
        for track_id, pid in sorted(self._pids.items(), key=lambda item: item[1]):
            track = self._tracks[track_id]
            info = b""
            if track.language:
                info = b"\x0a\x04" + track.language.encode("ascii") + b"\x00"
            body += bytes([track.stream_type]) + (0xE000 | pid).to_bytes(2, "big")
            body += (0xF000 | len(info)).to_bytes(2, "big") + info
        return _section(0x02, 1, self._version, bytes(body))

    def _psi(self, pid: int, section: bytes) -> bytes:
        """A section in one packet after its pointer field, the rest filled with 0xFF."""
        payload = b"\x00" + section
        return self._head(pid, True, 0x10) + payload + b"\xff" * (_PAYLOAD - len(payload))

    def _head(self, pid: int, start: bool, control: int) -> bytes:
        counter = self._counters.get(pid, 0)
        self._counters[pid] = (counter + 1) & 0x0F
        return bytes([SYNC, (0x40 if start else 0) | pid >> 8, pid & 0xFF, control | counter])

    def _pes(self, sample: Sample, dts: int) -> list[bytes]:
        track = sample.track
        pid = self._pids[track.track_id]
        pts = _ticks(sample.pts, track.timescale)
        video = track.kind == "video"
        clock = dts  # decode order and the PCR keep the DTS before any clamping
        if pts < dts:  # more negative than the first segment's: never before its PTS
            if not self._clamped:
                self._clamped = True
                logger.info("A fragmented MP4 composition offset outreached its first segment's.")
            dts = pts
        if video:
            payload = annex_b(track, sample.data, sample.key)
            stream_id = _VIDEO_STREAM
        else:
            payload = _adts(track, sample.data) if track.codec == "aac" else bytes(sample.data)
            stream_id = _STREAM_IDS[track.codec]
        if video and pts != dts:
            header = b"\x84\xc0\x0a" + _stamp(3, pts) + _stamp(1, dts)
        else:
            header = b"\x84\x80\x05" + _stamp(2, pts)
        length = len(header) + len(payload)
        pes = (
            b"\x00\x00\x01"
            + bytes([stream_id])
            + (length if length <= 0xFFFF else 0).to_bytes(2, "big")
        )
        pcr = (clock - _PCR_LEAD) % _PTS_MODULO if self._pcr and pid == self._pcr_pid else None
        return self._packets(pid, pes + header + payload, pcr, sample.key and video)

    def _packets(self, pid: int, pes: bytes, pcr: int | None, key: bool) -> list[bytes]:
        """A PES in TS packets on `pid`: the first with the PCR and the random access
        indicator in its adaptation field, the last stuffed by its own."""
        packets: list[bytes] = []
        flags = b""
        if pcr is not None or key:
            flags = bytes([(0x40 if key else 0) | (0x10 if pcr is not None else 0)])
            if pcr is not None:
                flags += (pcr << 15 | 0x7E00).to_bytes(6, "big")
        at, total = 0, len(pes)
        while at < total:
            room = _PAYLOAD - (1 + len(flags) if flags else 0)
            chunk = pes[at : at + room]
            start = at == 0
            at += len(chunk)
            if flags or len(chunk) < _PAYLOAD:
                fill = _PAYLOAD - 1 - len(chunk)  # the adaptation field's length
                if fill:
                    field = bytes([fill]) + (flags or b"\x00")
                    field += b"\xff" * (fill + 1 - len(field))
                else:
                    field = b"\x00"
                packets.append(self._head(pid, start, 0x30) + field + chunk)
            else:
                packets.append(self._head(pid, start, 0x10) + chunk)
            flags = b""
        return packets
