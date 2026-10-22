"""Fragmented MP4 (CMAF) for a phone's HLS player: an init segment and media segments.

`services.remote.films` hands a phone a Matroska film this way: the init segment (`init`)
describes a video track and, if any, an audio track (ID 1 and 2); each media segment
(`fragment`) is one `moof` and its `mdat`, a track run per track. The sample entries a phone
plays: avc1 (H.264) and hvc1 (HEVC) with the film's own decoder configuration; mp4a (AAC, with
its AudioSpecificConfig), ac-3 and ec-3 (with their dac3 and dec3 boxes). (A phone plays no MP3
this way.)

A run's composition offsets are signed (`trun` version 1): a B-frame shows before its decode
time's rank, and no edit list is needed.
"""

from __future__ import annotations

from dataclasses import dataclass
import struct
from typing import Sequence

VIDEO_ID, AUDIO_ID = 1, 2
KEY_FLAGS = 0x02000000  # depends on no other sample
DELTA_FLAGS = 0x01010000  # depends on others; no sync sample
_MATRIX = struct.pack(">9I", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)


@dataclass(frozen=True)
class VideoEntry:
    codec: str  # "h264" or "hevc"
    config: bytes  # its avcC or hvcC record (Matroska's CodecPrivate)
    width: int
    height: int
    timescale: int


@dataclass(frozen=True)
class AudioEntry:
    codec: str  # "aac", "ac3" or "eac3"
    config: bytes  # AAC's AudioSpecificConfig; dac3's or dec3's payload
    rate: int
    channels: int
    language: str = "und"  # ISO 639-2


@dataclass(frozen=True)
class Sample:
    duration: int
    size: int
    flags: int
    offset: int = 0  # composition offset: PTS less DTS


@dataclass(frozen=True)
class Run:
    track_id: int
    decode_time: int  # the first sample's DTS
    samples: Sequence[Sample]
    data: bytes  # the samples' bytes, one after another


def _box(kind: bytes, *parts: bytes) -> bytes:
    body = b"".join(parts)
    return struct.pack(">I", 8 + len(body)) + kind + body


def _full(kind: bytes, version: int, flags: int, *parts: bytes) -> bytes:
    return _box(kind, struct.pack(">I", version << 24 | flags), *parts)


def _language(code: str) -> int:
    letters = code.lower()[:3] if len(code) >= 3 and code[:3].isalpha() else "und"
    value = 0
    for letter in letters.ljust(3, "d"):
        value = value << 5 | ((ord(letter) - 0x60) & 0x1F)
    return value


def _descriptor(tag: int, body: bytes) -> bytes:
    size = len(body)
    length = bytes([0x80 | (size >> 21) & 0x7F, 0x80 | (size >> 14) & 0x7F])
    length += bytes([0x80 | (size >> 7) & 0x7F, size & 0x7F])
    return bytes([tag]) + length + body


def _esds(audio: AudioEntry) -> bytes:
    config = struct.pack(">BB", 0x40, 0x15) + b"\x00\x00\x00" + struct.pack(">II", 0, 0)
    config += _descriptor(0x05, audio.config)
    body = struct.pack(">HB", AUDIO_ID, 0) + _descriptor(0x04, config)
    body += _descriptor(0x06, b"\x02")
    return _full(b"esds", 0, 0, _descriptor(0x03, body))


def _video_entry(video: VideoEntry) -> bytes:
    kind, record = (b"avc1", b"avcC") if video.codec == "h264" else (b"hvc1", b"hvcC")
    body = b"\x00" * 6 + struct.pack(">H", 1) + b"\x00" * 16
    body += struct.pack(">HHIIIH", video.width, video.height, 0x480000, 0x480000, 0, 1)
    body += b"\x00" * 32 + struct.pack(">Hh", 0x18, -1)
    return _box(kind, body, _box(record, video.config))


def _audio_entry(audio: AudioEntry) -> bytes:
    rate = min(audio.rate, 0xFFFF)
    body = b"\x00" * 6 + struct.pack(">H", 1) + b"\x00" * 8
    body += struct.pack(">HHHHI", max(audio.channels, 1), 16, 0, 0, rate << 16)
    if audio.codec == "ac3":
        return _box(b"ac-3", body, _box(b"dac3", audio.config))
    if audio.codec == "eac3":
        return _box(b"ec-3", body, _box(b"dec3", audio.config))
    return _box(b"mp4a", body, _esds(audio))


def _trak(
    track_id: int, entry: bytes, timescale: int, video: VideoEntry | None, lang: str
) -> bytes:
    width, height = (video.width, video.height) if video is not None else (0, 0)
    volume = 0 if video is not None else 0x0100
    tkhd = _full(
        b"tkhd",
        0,
        3,
        struct.pack(">IIIII", 0, 0, track_id, 0, 0),
        b"\x00" * 8,
        struct.pack(">hhhH", 0, 0, volume, 0),
        _MATRIX,
        struct.pack(">II", width << 16, height << 16),
    )
    mdhd = _full(b"mdhd", 0, 0, struct.pack(">IIIIHH", 0, 0, timescale, 0, _language(lang), 0))
    handler, name = (
        (b"vide", b"VideoHandler\x00") if video is not None else (b"soun", b"SoundHandler\x00")
    )
    hdlr = _full(b"hdlr", 0, 0, b"\x00" * 4, handler, b"\x00" * 12, name)
    header = (
        _full(b"vmhd", 0, 1, b"\x00" * 8)
        if video is not None
        else _full(b"smhd", 0, 0, b"\x00" * 4)
    )
    dinf = _box(b"dinf", _full(b"dref", 0, 0, struct.pack(">I", 1), _full(b"url ", 0, 1)))
    stbl = _box(
        b"stbl",
        _full(b"stsd", 0, 0, struct.pack(">I", 1), entry),
        _full(b"stts", 0, 0, b"\x00" * 4),
        _full(b"stsc", 0, 0, b"\x00" * 4),
        _full(b"stsz", 0, 0, b"\x00" * 8),
        _full(b"stco", 0, 0, b"\x00" * 4),
    )
    return _box(b"trak", tkhd, _box(b"mdia", mdhd, hdlr, _box(b"minf", header, dinf, stbl)))


def init(video: VideoEntry, audio: AudioEntry | None) -> bytes:
    """The init segment: `ftyp` and a `moov` of the video track and the audio one."""
    ftyp = _box(b"ftyp", b"iso6", struct.pack(">I", 0), b"iso6cmfcisommp41")
    tracks = [_trak(VIDEO_ID, _video_entry(video), video.timescale, video, "und")]
    trex = [_full(b"trex", 0, 0, struct.pack(">IIIII", VIDEO_ID, 1, 0, 0, 0))]
    if audio is not None:
        tracks.append(_trak(AUDIO_ID, _audio_entry(audio), audio.rate, None, audio.language))
        trex.append(_full(b"trex", 0, 0, struct.pack(">IIIII", AUDIO_ID, 1, 0, 0, 0)))
    next_id = (AUDIO_ID if audio is not None else VIDEO_ID) + 1
    mvhd = _full(
        b"mvhd",
        0,
        0,
        struct.pack(">IIII", 0, 0, 1000, 0),
        struct.pack(">IH", 0x10000, 0x0100),
        b"\x00" * 10,
        _MATRIX,
        b"\x00" * 24,
        struct.pack(">I", next_id),
    )
    return ftyp + _box(b"moov", mvhd, *tracks, _box(b"mvex", *trex))


def _traf(run: Run, data_offset: int) -> bytes:
    tfhd = _full(b"tfhd", 0, 0x020000, struct.pack(">I", run.track_id))  # default-base-is-moof
    tfdt = _full(b"tfdt", 1, 0, struct.pack(">Q", run.decode_time))
    entries = b"".join(
        struct.pack(">IIIi", sample.duration, sample.size, sample.flags, sample.offset)
        for sample in run.samples
    )
    trun = _full(b"trun", 1, 0x000F01, struct.pack(">Ii", len(run.samples), data_offset), entries)
    return _box(b"traf", tfhd, tfdt, trun)


def fragment(sequence: int, runs: Sequence[Run]) -> bytes:
    """A media segment: one `moof` (a track fragment per run) and the `mdat` of their data."""
    runs = [run for run in runs if run.samples]

    def moof(offsets: Sequence[int]) -> bytes:
        mfhd = _full(b"mfhd", 0, 0, struct.pack(">I", sequence))
        return _box(b"moof", mfhd, *(_traf(run, at) for run, at in zip(runs, offsets)))

    size = len(moof([0] * len(runs)))
    offsets, at = [], size + 8
    for run in runs:
        offsets.append(at)
        at += len(run.data)
    data = b"".join(run.data for run in runs)
    return moof(offsets) + _box(b"mdat", data)
