"""Matroska files made in the tests: what ffmpeg doesn't write (laced blocks, header stripping,
zlib, BlockGroups, unknown-size Clusters, codecs a phone doesn't play)."""

from __future__ import annotations

import struct
from typing import Optional, Sequence
import zlib

from services.remote import mkv


def _id(element_id: int) -> bytes:
    return element_id.to_bytes((element_id.bit_length() + 7) // 8, "big")


def vint(value: int, length: int = 0) -> bytes:
    length = length or next(n for n in range(1, 9) if value < (1 << (7 * n)) - 1)
    return ((1 << (7 * length)) | value).to_bytes(length, "big")


def element(element_id: int, body: bytes, unknown: bool = False) -> bytes:
    size = b"\x01\xff\xff\xff\xff\xff\xff\xff" if unknown else vint(len(body), 8)
    return _id(element_id) + size + body


def uint(element_id: int, value: int) -> bytes:
    return element(element_id, value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big"))


def text(element_id: int, value: str) -> bytes:
    return element(element_id, value.encode())


def number(element_id: int, value: float) -> bytes:
    return element(element_id, struct.pack(">d", value))


def track(
    number_: int,
    kind: int,
    codec: str,
    private: bytes = b"",
    *,
    strip: bytes = b"",
    compressed: bool = False,
    default: bool = True,
    rate: float = 48000.0,
    channels: int = 2,
) -> bytes:
    body = uint(mkv.TRACK_NUMBER, number_) + uint(mkv.TRACK_TYPE, kind) + text(mkv.CODEC_ID, codec)
    body += uint(mkv.FLAG_DEFAULT, int(default))
    if private:
        body += element(mkv.CODEC_PRIVATE, private)
    if kind == 1:
        body += element(mkv.VIDEO, uint(mkv.PIXEL_WIDTH, 160) + uint(mkv.PIXEL_HEIGHT, 90))
    if kind == 2:
        body += element(
            mkv.AUDIO, number(mkv.SAMPLING_FREQUENCY, rate) + uint(mkv.CHANNELS, channels)
        )
    if strip or compressed:
        compression = uint(mkv.CONTENT_COMP_ALGO, 3 if strip else 0)
        if strip:
            compression += element(mkv.CONTENT_COMP_SETTINGS, strip)
        encoding = uint(mkv.CONTENT_ENCODING_TYPE, 0)
        encoding += element(mkv.CONTENT_COMPRESSION, compression)
        body += element(mkv.CONTENT_ENCODINGS, element(mkv.CONTENT_ENCODING, encoding))
    return element(mkv.TRACK_ENTRY, body)


def block_body(
    number_: int, relative: int, frames: Sequence[bytes], key: bool, lacing: str = ""
) -> bytes:
    """A block's body: its frames laced ("xiph", "fixed", "ebml") or a single one."""
    flags = 0x80 if key else 0
    head = vint(number_) + struct.pack(">h", relative)
    if not lacing:
        return head + bytes([flags]) + frames[0]
    code = {"xiph": 1, "fixed": 2, "ebml": 3}[lacing]
    sizes = b""
    if lacing == "xiph":
        for frame in frames[:-1]:
            size = len(frame)
            sizes += b"\xff" * (size // 255) + bytes([size % 255])
    elif lacing == "ebml":
        sizes = vint(len(frames[0]))
        for before, frame in zip(frames, frames[1:-1]):
            difference = len(frame) - len(before)
            sizes += vint(difference + (1 << 13) - 1, 2)  # signed, two bytes
    return head + bytes([flags | code << 1, len(frames) - 1]) + sizes + b"".join(frames)


def simple_block(number_: int, relative: int, frames: Sequence[bytes], key: bool, lacing=""):
    return element(mkv.SIMPLE_BLOCK, block_body(number_, relative, frames, key, lacing))


def block_group(number_: int, relative: int, frame: bytes, reference: Optional[int]) -> bytes:
    body = element(mkv.BLOCK, block_body(number_, relative, [frame], False))
    if reference is not None:
        body += element(mkv.REFERENCE_BLOCK, struct.pack(">b", reference))
    return element(mkv.BLOCK_GROUP, body)


def cluster(timestamp: int, *blocks: bytes, unknown: bool = False) -> bytes:
    return element(mkv.CLUSTER, uint(mkv.TIMESTAMP, timestamp) + b"".join(blocks), unknown)


def film(
    tracks: Sequence[bytes],
    clusters: Sequence[bytes],
    duration_ms: float,
    cue_track: int = 1,
    cues: bool = True,
) -> bytes:
    """A whole file: header, Segment (SeekHead, Info, Tracks, the Clusters, Cues). Each
    Cluster gets a cue at its timestamp for `cue_track`."""
    head = element(mkv.EBML, text(mkv.DOC_TYPE, "matroska"))
    info = element(
        mkv.INFO,
        uint(mkv.TIMESTAMP_SCALE, 1_000_000) + number(mkv.DURATION, duration_ms),
    )
    tracks_element = element(mkv.TRACKS, b"".join(tracks))

    # The SeekHead's size is fixed (8-byte positions), so the layout is known before it.
    def seek_head(info_at: int, tracks_at: int, cues_at: int) -> bytes:
        body = element(mkv.SEEK, element(mkv.SEEK_ID, _id(mkv.INFO)) + _pos(info_at))
        body += element(mkv.SEEK, element(mkv.SEEK_ID, _id(mkv.TRACKS)) + _pos(tracks_at))
        if cues:
            body += element(mkv.SEEK, element(mkv.SEEK_ID, _id(mkv.CUES)) + _pos(cues_at))
        return element(mkv.SEEK_HEAD, body)

    size = len(seek_head(0, 0, 0))
    info_at = size
    tracks_at = info_at + len(info)
    first = tracks_at + len(tracks_element)
    points = b""
    at = first
    for made in clusters:
        stamp = _cluster_time(made)
        position = element(
            mkv.CUE_TRACK_POSITIONS,
            uint(mkv.CUE_TRACK, cue_track) + uint(mkv.CUE_CLUSTER_POSITION, at),
        )
        points += element(mkv.CUE_POINT, uint(mkv.CUE_TIME, stamp) + position)
        at += len(made)
    body = seek_head(info_at, tracks_at, at) + info + tracks_element + b"".join(clusters)
    if cues:
        body += element(mkv.CUES, points)
    return head + element(mkv.SEGMENT, body)


def _pos(position: int) -> bytes:
    return element(mkv.SEEK_POSITION, position.to_bytes(8, "big"))


def _cluster_time(made: bytes) -> int:
    _id_, _size, header = mkv.element_header(made)
    stamp_id, stamp_size, stamp_header = mkv.element_header(made, header)
    assert stamp_id == mkv.TIMESTAMP and stamp_size is not None
    return int.from_bytes(made[header + stamp_header : header + stamp_header + stamp_size], "big")


def zlibbed(data: bytes) -> bytes:
    return zlib.compress(data)
