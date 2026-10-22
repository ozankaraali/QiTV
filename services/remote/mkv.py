"""Reading Matroska (MKV) films for a phone's player: their layout, cues and frames.

Safari plays no Matroska, yet most of a provider's films are MKV. QiTV's home door hands a
phone such a film as HLS (`services.remote.films`), reading only the parts it needs over HTTP
ranges. This module reads the bytes it is given, nothing else:

- `read_head` reads a file's first bytes: the EBML header, where the Segment's data starts,
  and what of its SeekHead, Info and Tracks they hold (most files keep all three there).
  `read_seek_head`, `read_info` and `read_tracks` read one of those elements on its own (one
  the SeekHead points to further on), `read_cues` the Cues: where each keyframe's Cluster is.
- `ClusterReader` reads Clusters as their bytes arrive: each block's frames, laced or not, a
  header stripping's bytes put back and zlib undone. A piece is read until it has what it
  needs (`services.remote.films`), so nothing waits for a whole Cluster.

Times are in the file's timestamp units (`Info.scale_ns` nanoseconds each, 1 ms in most).
"""

from __future__ import annotations

from dataclasses import dataclass
import struct
from typing import Mapping, NamedTuple, Optional
import zlib


class MkvError(Exception):
    """Bytes that are no Matroska, or a damaged one: `str(error)` says why."""


NOT_MKV = "The film is not a Matroska file."
DAMAGED = "The film's Matroska is damaged."

EBML = 0x1A45DFA3
DOC_TYPE = 0x4282
SEGMENT = 0x18538067
SEEK_HEAD = 0x114D9B74
SEEK = 0x4DBB
SEEK_ID = 0x53AB
SEEK_POSITION = 0x53AC
INFO = 0x1549A966
TIMESTAMP_SCALE = 0x2AD7B1
DURATION = 0x4489
TRACKS = 0x1654AE6B
TRACK_ENTRY = 0xAE
TRACK_NUMBER = 0xD7
TRACK_TYPE = 0x83
CODEC_ID = 0x86
CODEC_PRIVATE = 0x63A2
DEFAULT_DURATION = 0x23E383
LANGUAGE = 0x22B59C
FLAG_DEFAULT = 0x88
FLAG_ENABLED = 0xB9
VIDEO = 0xE0
PIXEL_WIDTH = 0xB0
PIXEL_HEIGHT = 0xBA
AUDIO = 0xE1
SAMPLING_FREQUENCY = 0xB5
CHANNELS = 0x9F
CONTENT_ENCODINGS = 0x6D80
CONTENT_ENCODING = 0x6240
CONTENT_ENCODING_TYPE = 0x5033
CONTENT_COMPRESSION = 0x5034
CONTENT_COMP_ALGO = 0x4254
CONTENT_COMP_SETTINGS = 0x4255
CUES = 0x1C53BB6B
CUE_POINT = 0xBB
CUE_TIME = 0xB3
CUE_TRACK_POSITIONS = 0xB7
CUE_TRACK = 0xF7
CUE_CLUSTER_POSITION = 0xF1
CLUSTER = 0x1F43B675
TIMESTAMP = 0xE7
SIMPLE_BLOCK = 0xA3
BLOCK_GROUP = 0xA0
BLOCK = 0xA1
REFERENCE_BLOCK = 0xFB
VOID = 0xEC
CRC32 = 0xBF

# The Segment's children: met inside a Cluster of unknown size, one ends it.
_TOP = {SEEK_HEAD, INFO, TRACKS, CUES, CLUSTER, 0x1941A469, 0x1043A770, 0x1254C367}
_KINDS = {1: "video", 2: "audio"}
_ZLIB, _STRIPPING = 0, 3  # ContentCompAlgo
_COMPRESSION, _ENCRYPTION = 0, 1  # ContentEncodingType


class Truncated(MkvError):
    """The bytes end inside what is being read (more may come)."""


def _vint(data: bytes | bytearray | memoryview, at: int) -> tuple[int, int]:
    """An EBML variable-size integer at `at`: its value (marker taken off) and its length."""
    if at >= len(data):
        raise Truncated
    first = data[at]
    if not first:
        raise MkvError(DAMAGED)
    length = 9 - first.bit_length()
    if at + length > len(data):
        raise Truncated
    value = first & (0xFF >> length)
    for byte in data[at + 1 : at + length]:
        value = value << 8 | byte
    return value, length


def element_header(
    data: bytes | bytearray | memoryview, at: int = 0
) -> tuple[int, Optional[int], int]:
    """An element's ID, data size (None: unknown) and header length, at `at`. Raises MkvError
    (damaged), or Truncated when the bytes end inside the header."""
    if at >= len(data):
        raise Truncated
    first = data[at]
    if not first or first.bit_length() < 5:
        raise MkvError(DAMAGED)  # IDs are one to four bytes long
    id_length = 9 - first.bit_length()
    if at + id_length > len(data):
        raise Truncated
    element_id = int.from_bytes(data[at : at + id_length], "big")
    size, size_length = _vint(data, at + id_length)
    unknown = size == (1 << (7 * size_length)) - 1
    return element_id, None if unknown else size, id_length + size_length


def _children(data: memoryview) -> list[tuple[int, memoryview]]:
    """A master element's children (its body `data`): ID and body each."""
    out = []
    at = 0
    try:
        while at < len(data):
            element_id, size, header = element_header(data, at)
            if size is None or at + header + size > len(data):
                raise MkvError(DAMAGED)
            out.append((element_id, data[at + header : at + header + size]))
            at += header + size
    except Truncated as short:
        raise MkvError(DAMAGED) from short
    return out


def _uint(data: memoryview) -> int:
    return int.from_bytes(data, "big") if len(data) <= 8 else 0


def _float(data: memoryview) -> float:
    if len(data) == 4:
        return float(struct.unpack(">f", data)[0])
    if len(data) == 8:
        return float(struct.unpack(">d", data)[0])
    return 0.0


def _text(data: memoryview) -> str:
    return bytes(data).split(b"\x00", 1)[0].decode("utf-8", "replace")


@dataclass(frozen=True)
class Info:
    scale_ns: int = 1_000_000  # a timestamp unit, in nanoseconds
    duration: float = 0.0  # in timestamp units (0: not given)


@dataclass(frozen=True)
class MkvTrack:
    number: int
    kind: str  # "video", "audio" or "other"
    codec: str  # the CodecID, e.g. "V_MPEG4/ISO/AVC", "A_AAC", "A_AC3"
    private: bytes = b""
    default_duration_ns: int = 0
    language: str = "und"
    default: bool = True
    enabled: bool = True
    width: int = 0
    height: int = 0
    rate: float = 0.0  # audio's sampling frequency
    channels: int = 0
    strip: bytes = b""  # header stripping: the bytes each frame left out
    zlib: bool = False  # each frame zlib-compressed
    encrypted: bool = False


class Cue(NamedTuple):
    time: int  # timestamp units
    track: int
    cluster: int  # the Cluster's position in the file


@dataclass(frozen=True)
class Head:
    """What a file's first bytes say (`read_head`)."""

    segment: int  # where the Segment's data starts in the file (positions count from there)
    seeks: Mapping[int, int]  # element ID -> its position in the file (the SeekHead's)
    info: Optional[Info]  # None: not within the bytes read
    tracks: Optional[tuple[MkvTrack, ...]]
    first_cluster: int  # the first Cluster's position in the file (0: not within them)


def read_head(data: bytes) -> Head:
    """The EBML header, the Segment and the elements before its first Cluster that `data` (a
    file's first bytes) holds whole. Raises MkvError: no Matroska, or damaged."""
    view = memoryview(data)
    try:
        element_id, size, header = element_header(view, 0)
        if element_id != EBML or size is None:
            raise MkvError(NOT_MKV)
        doc = dict(_children(view[header : header + size])).get(DOC_TYPE)
        if doc is not None and _text(doc) not in ("matroska", "webm"):
            raise MkvError(NOT_MKV)
        at = header + size
        element_id, _size, header = element_header(view, at)
    except Truncated as short:
        raise MkvError(NOT_MKV) from short
    if element_id != SEGMENT:
        raise MkvError(NOT_MKV)
    segment = at + header
    at = segment
    seeks: dict[int, int] = {}
    info: Optional[Info] = None
    tracks: Optional[tuple[MkvTrack, ...]] = None
    first_cluster = 0
    while at < len(view):
        try:
            element_id, size, header = element_header(view, at)
        except Truncated:
            break
        if element_id == CLUSTER:
            first_cluster = at
            break
        if size is None:
            raise MkvError(DAMAGED)
        end = at + header + size
        if end > len(view):
            break
        body = view[at + header : end]
        if element_id == SEEK_HEAD:
            for kind, position in read_seek_head(body, segment).items():
                seeks.setdefault(kind, position)
        elif element_id == INFO:
            info = read_info(body)
        elif element_id == TRACKS:
            tracks = read_tracks(body)
        at = end
    return Head(segment, seeks, info, tracks, first_cluster)


def read_seek_head(body: bytes | memoryview, segment: int) -> dict[int, int]:
    """A SeekHead's entries: element ID -> position in the file (its first of each)."""
    seeks: dict[int, int] = {}
    for kind, seek in _children(memoryview(body)):
        if kind != SEEK:
            continue
        fields = dict(_children(seek))
        if SEEK_ID in fields and SEEK_POSITION in fields:
            target = int.from_bytes(fields[SEEK_ID], "big")
            seeks.setdefault(target, segment + _uint(fields[SEEK_POSITION]))
    return seeks


def read_info(body: bytes | memoryview) -> Info:
    fields = dict(_children(memoryview(body)))
    scale = _uint(fields[TIMESTAMP_SCALE]) if TIMESTAMP_SCALE in fields else 1_000_000
    duration = _float(fields[DURATION]) if DURATION in fields else 0.0
    return Info(scale_ns=scale or 1_000_000, duration=max(duration, 0.0))


def read_tracks(body: bytes | memoryview) -> tuple[MkvTrack, ...]:
    return tuple(
        _track(entry) for kind, entry in _children(memoryview(body)) if kind == TRACK_ENTRY
    )


def _track(entry: memoryview) -> MkvTrack:
    fields = dict(_children(entry))
    width = height = channels = 0
    rate = 0.0
    if VIDEO in fields:
        video = dict(_children(fields[VIDEO]))
        width = _uint(video[PIXEL_WIDTH]) if PIXEL_WIDTH in video else 0
        height = _uint(video[PIXEL_HEIGHT]) if PIXEL_HEIGHT in video else 0
    if AUDIO in fields:
        audio = dict(_children(fields[AUDIO]))
        rate = _float(audio[SAMPLING_FREQUENCY]) if SAMPLING_FREQUENCY in audio else 8000.0
        channels = _uint(audio[CHANNELS]) if CHANNELS in audio else 1
    strip, compressed, encrypted = b"", False, False
    if CONTENT_ENCODINGS in fields:
        for kind, encoding in _children(fields[CONTENT_ENCODINGS]):
            if kind != CONTENT_ENCODING:
                continue
            parts = dict(_children(encoding))
            kind_of = _uint(parts[CONTENT_ENCODING_TYPE]) if CONTENT_ENCODING_TYPE in parts else 0
            if kind_of == _ENCRYPTION:
                encrypted = True
            elif kind_of == _COMPRESSION:
                compression = dict(_children(parts.get(CONTENT_COMPRESSION, memoryview(b""))))
                algo = _ZLIB
                if CONTENT_COMP_ALGO in compression:
                    algo = _uint(compression[CONTENT_COMP_ALGO])
                if algo == _STRIPPING:
                    strip = bytes(compression.get(CONTENT_COMP_SETTINGS, b""))
                elif algo == _ZLIB:
                    compressed = True
                else:
                    encrypted = True  # bzlib, lzo: frames read as nothing playable
    track_type = _uint(fields[TRACK_TYPE]) if TRACK_TYPE in fields else 0
    return MkvTrack(
        number=_uint(fields[TRACK_NUMBER]) if TRACK_NUMBER in fields else 0,
        kind=_KINDS.get(track_type, "other"),
        codec=_text(fields[CODEC_ID]) if CODEC_ID in fields else "",
        private=bytes(fields.get(CODEC_PRIVATE, b"")),
        default_duration_ns=_uint(fields[DEFAULT_DURATION]) if DEFAULT_DURATION in fields else 0,
        language=_text(fields[LANGUAGE]) if LANGUAGE in fields else "eng",
        default=bool(_uint(fields[FLAG_DEFAULT])) if FLAG_DEFAULT in fields else True,
        enabled=bool(_uint(fields[FLAG_ENABLED])) if FLAG_ENABLED in fields else True,
        width=width,
        height=height,
        rate=rate,
        channels=channels,
        strip=strip,
        zlib=compressed,
        encrypted=encrypted,
    )


def read_cues(body: bytes | memoryview, segment: int) -> list[Cue]:
    """The Cues' points, each track's position of each: by time, then track."""
    cues = []
    for kind, point in _children(memoryview(body)):
        if kind != CUE_POINT:
            continue
        fields = _children(point)
        time = next((_uint(data) for kind_of, data in fields if kind_of == CUE_TIME), None)
        if time is None:
            continue
        for kind_of, data in fields:
            if kind_of != CUE_TRACK_POSITIONS:
                continue
            position = dict(_children(data))
            if CUE_TRACK in position and CUE_CLUSTER_POSITION in position:
                cluster = segment + _uint(position[CUE_CLUSTER_POSITION])
                cues.append(Cue(time, _uint(position[CUE_TRACK]), cluster))
    cues.sort()
    return cues


class Frame(NamedTuple):
    track: int
    time: int  # the block's time, in timestamp units: each of its laced frames shares it
    key: bool
    data: bytes
    lace: int  # its place among its block's laced frames (0: the first, or not laced)


class ClusterReader:
    """Clusters' frames as their bytes arrive (`feed`), read from a Cluster's first byte at
    `start` in the file; only `tracks`' frames are kept.

    `done` tells the Clusters are over (Cues, Tags or the like came next); `position` is how far
    in the file the reading got."""

    _KEEP = 1 << 20  # bytes read are dropped once this many pile up

    def __init__(self, tracks: Mapping[int, MkvTrack], start: int) -> None:
        self._tracks = dict(tracks)
        self._data = bytearray()
        self._base = start  # the file position of _data[0]
        self._at = 0
        self._skip = 0  # bytes of an element passed over still to come
        self._cluster = False  # inside a Cluster
        self._end: Optional[int] = None  # the Cluster's end in the file (None: unknown size)
        self._time = 0  # the Cluster's timestamp
        self.done = False

    @property
    def position(self) -> int:
        return self._base + self._at + self._skip

    def feed(self, chunk: bytes) -> list[Frame]:
        self._data += chunk
        frames: list[Frame] = []
        while not self.done and self._step(frames):
            pass
        if self._at >= self._KEEP:
            del self._data[: self._at]
            self._base += self._at
            self._at = 0
        return frames

    def _step(self, frames: list[Frame]) -> bool:
        """Read one element (or pass over what is left of one); False: more bytes are due."""
        data = self._data
        if self._skip:
            passed = min(self._skip, len(data) - self._at)
            self._at += passed
            self._skip -= passed
            return not self._skip
        here = self._base + self._at
        if self._cluster and self._end is not None and here >= self._end:
            self._cluster = False
        try:
            element_id, size, header = element_header(data, self._at)
        except Truncated:
            return False
        if element_id == CLUSTER:
            self._cluster, self._time = True, 0
            self._end = None if size is None else here + header + size
            self._at += header
            return True
        if not self._cluster or (self._end is None and element_id in _TOP):
            self._cluster = False
            if element_id in (VOID, CRC32) and size is not None:
                self._at += header
                self._skip = size
                return True
            self.done = True  # Cues, Tags, Chapters...: no more Clusters
            return False
        if size is None:
            raise MkvError(DAMAGED)
        if element_id not in (TIMESTAMP, SIMPLE_BLOCK, BLOCK_GROUP):
            self._at += header
            self._skip = size
            return True
        if self._at + header + size > len(data):
            return False
        body = memoryview(bytes(data[self._at + header : self._at + header + size]))
        if element_id == TIMESTAMP:
            self._time = _uint(body)
        elif element_id == SIMPLE_BLOCK:
            frames.extend(self._block(body, None))
        else:
            fields = _children(body)
            block = next((data for kind, data in fields if kind == BLOCK), None)
            if block is not None:
                key = all(kind != REFERENCE_BLOCK for kind, _data in fields)
                frames.extend(self._block(block, key))
        self._at += header + size
        return True

    def _block(self, body: memoryview, key: Optional[bool]) -> list[Frame]:
        """A block's frames, if its track is one kept. `key`: a BlockGroup's (no reference
        block); None: a SimpleBlock's flags say."""
        try:
            number, length = _vint(body, 0)
        except Truncated as short:
            raise MkvError(DAMAGED) from short
        track = self._tracks.get(number)
        if track is None:
            return []
        if len(body) < length + 3:
            raise MkvError(DAMAGED)
        relative = int.from_bytes(body[length : length + 2], "big", signed=True)
        flags = body[length + 2]
        laced = _laces(body, length + 3, (flags >> 1) & 0x03)
        time = self._time + relative
        is_key = bool(flags & 0x80) if key is None else key
        out = []
        for lace, frame in enumerate(laced):
            data = bytes(frame)
            if track.zlib:
                try:
                    data = zlib.decompress(data)
                except zlib.error as error:
                    raise MkvError(DAMAGED) from error
            out.append(Frame(number, time, is_key, track.strip + data, lace))
        return out


def _laces(body: memoryview, at: int, lacing: int) -> list[memoryview]:
    """A block's frames after its header (`at`): one, or its laced ones (Xiph, fixed, EBML)."""
    if not lacing:
        return [body[at:]]
    try:
        count = body[at] + 1
        at += 1
        sizes: list[int] = []
        if lacing == 1:  # Xiph: each size as bytes summed, the last below 255
            for _ in range(count - 1):
                size = 0
                while True:
                    byte = body[at]
                    at += 1
                    size += byte
                    if byte != 255:
                        break
                sizes.append(size)
        elif lacing == 3:  # EBML: the first size, then signed differences
            size, length = _vint(body, at)
            at += length
            sizes.append(size)
            for _ in range(count - 2):
                value, length = _vint(body, at)
                at += length
                size += value - ((1 << (7 * length - 1)) - 1)
                sizes.append(size)
        else:  # fixed: all alike
            whole = len(body) - at
            if whole % count:
                raise MkvError(DAMAGED)
            sizes = [whole // count] * (count - 1)
    except (IndexError, Truncated) as error:
        raise MkvError(DAMAGED) from error
    last = len(body) - at - sum(sizes)
    if last < 0 or any(size < 0 for size in sizes):
        raise MkvError(DAMAGED)
    frames = []
    for size in [*sizes, last]:
        frames.append(body[at : at + size])
        at += size
    return frames
