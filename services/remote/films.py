"""A Matroska film as HLS for a phone's player, read from its provider as the phone plays it.

Safari plays no MKV, the container of most of a provider's films. The home door hands a phone
such a film as an HLS playlist of fragmented MP4 instead (nothing is re-encoded):

- `Film.open` reads the film's head, Tracks and Cues over HTTP ranges (`HttpFetcher`), picks
  its video track and the first audio track a phone plays, and plans its pieces: each starts
  at a keyframe the Cues name, about `TARGET_S` long, so a seek lands anywhere at once.
- `Film.piece` reads a piece's Clusters from its provider as they come (`ClusterReader`) and
  stops once it holds the piece's frames: the video's from its keyframe to the next piece's,
  the sound's of its span. They go out as one `moof`/`mdat` (`services.remote.fmp4_writer`).
- `FilmShelf` keeps the films phones play (one per phone), `add_routes` serves them:
  ``/film/<token>/index.m3u8``, ``init.mp4`` and ``<n>.m4s``.

A film a phone cannot play this way is refused (`FilmRefused`) with a reason the page words:
"format" (another video codec, no Cues, encryption), "sound" (DTS, TrueHD, MP3 and the like,
with no other track), "provider" (it answered no HTTP range).
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import logging
import secrets
import time
from typing import Any, AsyncIterator, Optional, Protocol, Sequence
from urllib.parse import parse_qsl, urlsplit

from services.remote import fmp4_writer as mp4
from services.remote.mkv import (
    CLUSTER,
    CUES,
    INFO,
    SEEK_HEAD,
    TRACKS,
    ClusterReader,
    Cue,
    Frame,
    MkvError,
    MkvTrack,
    Truncated,
    element_header,
    read_cues,
    read_head,
    read_info,
    read_seek_head,
    read_tracks,
)

logger = logging.getLogger(__name__)

TARGET_S = 6.0  # a piece's length, at the least (keyframes permitting)
HEAD_BYTES = 256 * 1024
MAX_ELEMENT = 32 * 1024 * 1024  # Tracks or Cues larger than this are no film's
MAX_PIECE = 128 * 1024 * 1024  # a piece read no further (a damaged file's)
FIRST_FRAME_BYTES = 8 * 1024 * 1024  # how far the sound's first frame is looked for
OPEN_S = 30.0
SHELF = 4  # films kept at once (one per phone)
_CACHED = 1  # pieces kept per film (a phone asks again for the one it got)
_VIDEO_SCALE = 90000

FORMAT, SOUND, PROVIDER = "format", "sound", "provider"

_AAC_RATES = (
    96000,
    88200,
    64000,
    48000,
    44100,
    32000,
    24000,
    22050,
    16000,
    12000,
    11025,
    8000,
    7350,
)
_AAC_OBJECTS = {"MAIN": 1, "LC": 2, "SSR": 3, "LTP": 4, "SBR": 2, "LC/SBR": 2}
_AC3_RATES = (48000, 44100, 32000)
_AC3_CODECS = ("A_AC3", "A_AC3/BSID9", "A_AC3/BSID10")
_H264_PROFILES = (66, 77, 88, 100)  # 8-bit 4:2:0 (High 10 and above a phone can't decode)


class FilmRefused(Exception):
    """A film a phone can't play as HLS: `reason` is FORMAT, SOUND or PROVIDER."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason


class Fetcher(Protocol):
    async def read(self, start: int, end: int) -> bytes:
        """The file's bytes [start, end), fewer at its end."""

    def stream(self, start: int, end: Optional[int]) -> AsyncIterator[bytes]:
        """The file's bytes from `start` as they arrive, to `end` (None: its end)."""


@dataclass(frozen=True)
class Piece:
    start: int  # its first keyframe's time (timestamp units)
    end: int  # the next piece's start, or the film's end
    position: int  # the file position of the Cluster its keyframe is in
    bound: Optional[int]  # read no further (None: to the file's end)


@dataclass(frozen=True)
class Sound:
    entry: mp4.AudioEntry
    frame: int  # samples per frame


# ---- codecs -------------------------------------------------------------------------------


def is_matroska(item: dict, url: str) -> bool:
    """Whether the film `item` at `url` is an MKV (its container, or its link's file name)."""
    if str(item.get("container_extension") or "").lower().lstrip(".") == "mkv":
        return True
    parts = urlsplit(url)
    return parts.path.lower().endswith(".mkv") or any(
        value.lower().endswith(".mkv") for _key, value in parse_qsl(parts.query)
    )


def video_entry(track: MkvTrack) -> mp4.VideoEntry:
    """`track` as an avc1 or hvc1 entry; FilmRefused(FORMAT) for what a phone doesn't play."""
    if track.codec == "V_MPEG4/ISO/AVC":
        if len(track.private) < 7 or track.private[1] not in _H264_PROFILES:
            raise FilmRefused(FORMAT, f"H.264 profile {track.private[1:2].hex() or '?'}")
        codec = "h264"
    elif track.codec == "V_MPEGH/ISO/HEVC" and len(track.private) >= 23:
        codec = "hevc"
    else:
        raise FilmRefused(FORMAT, f"video {track.codec or '?'}")
    if track.encrypted or track.zlib:
        raise FilmRefused(FORMAT, "encoded frames")
    return mp4.VideoEntry(codec, track.private, track.width, track.height, _VIDEO_SCALE)


def _bits(data: bytes, at: int, count: int) -> int:
    """`count` bits of `data` from bit `at` (most significant first)."""
    value = int.from_bytes(data, "big")
    return (value >> (len(data) * 8 - at - count)) & ((1 << count) - 1)


def _aac_config(track: MkvTrack) -> bytes:
    """An AAC track's AudioSpecificConfig: its own, or one made for an older CodecID's."""
    if track.private:
        return track.private
    profile = track.codec.split("/", 2)[-1] if track.codec.count("/") >= 2 else "LC"
    rate = round(track.rate)
    index = _AAC_RATES.index(rate) if rate in _AAC_RATES else 3
    objects = _AAC_OBJECTS.get(profile, 2)
    channels = min(max(track.channels, 1), 7)
    return (objects << 11 | index << 7 | channels << 3).to_bytes(2, "big")


def _aac(track: MkvTrack) -> Sound:
    config = _aac_config(track)
    if len(config) < 2:
        raise FilmRefused(SOUND, "AAC config")
    index = _bits(config, 5, 4)
    rate = _bits(config, 9, 24) if index == 15 and len(config) >= 5 else 0
    rate = rate or (_AAC_RATES[index] if index < len(_AAC_RATES) else round(track.rate))
    entry = mp4.AudioEntry("aac", config, rate, track.channels, _lang(track))
    return Sound(entry, 1024)


def _ac3(track: MkvTrack, frame: bytes) -> Sound:
    if len(frame) < 8 or frame[:2] != b"\x0b\x77":
        raise FilmRefused(SOUND, "AC-3 frame")
    fscod, frmsizecod = frame[4] >> 6, frame[4] & 0x3F
    bsid, bsmod, acmod = frame[5] >> 3, frame[5] & 0x07, frame[6] >> 5
    at = 51  # the bits after acmod
    if acmod & 1 and acmod != 1:
        at += 2  # cmixlev
    if acmod & 4:
        at += 2  # surmixlev
    if acmod == 2:
        at += 2  # dsurmod
    lfeon = _bits(frame[:8], at, 1)
    if fscod > 2:
        raise FilmRefused(SOUND, "AC-3 rate")
    config = fscod << 22 | bsid << 17 | bsmod << 14 | acmod << 11 | lfeon << 10
    config |= (frmsizecod >> 1) << 5
    entry = mp4.AudioEntry("ac3", config.to_bytes(3, "big"), _AC3_RATES[fscod], track.channels)
    return Sound(entry, 1536)


def _eac3(track: MkvTrack, frame: bytes) -> Sound:
    if len(frame) < 7 or frame[:2] != b"\x0b\x77":
        raise FilmRefused(SOUND, "E-AC-3 frame")
    head = frame[:7]
    frmsiz, fscod = _bits(head, 21, 11), _bits(head, 32, 2)
    if fscod == 3:
        rate, blocks = _AC3_RATES[_bits(head, 34, 2)] // 2, 6
    else:
        rate, blocks = _AC3_RATES[fscod], (1, 2, 3, 6)[_bits(head, 34, 2)]
    acmod, lfeon, bsid = _bits(head, 36, 3), _bits(head, 39, 1), _bits(head, 40, 5)
    samples = blocks * 256
    data_rate = (frmsiz + 1) * 2 * 8 * rate // samples // 1000
    config = data_rate << 27  # num_ind_sub 0: one independent substream
    config |= (fscod & 3) << 22 | bsid << 17 | acmod << 9 | lfeon << 8
    entry = mp4.AudioEntry("eac3", config.to_bytes(5, "big"), rate, track.channels, _lang(track))
    return Sound(entry, samples * _syncframes(frame))


def _syncframes(frame: bytes) -> int:
    """How many independent substream 0 syncframes an E-AC-3 frame holds (its samples are as
    many times one's)."""
    count, at = 0, 0
    while at + 5 <= len(frame) and frame[at : at + 2] == b"\x0b\x77":
        head = frame[at : at + 5]
        if _bits(head, 16, 2) != 1 and _bits(head, 18, 3) == 0:
            count += 1
        at += (_bits(head, 21, 11) + 1) * 2
    return max(count, 1)


def _lang(track: MkvTrack) -> str:
    return track.language if len(track.language) == 3 else "und"


def plays(track: MkvTrack) -> bool:
    """Whether a phone plays `track`'s sound (as far as its CodecID tells)."""
    codec = track.codec
    return not track.encrypted and (
        codec.startswith("A_AAC") or codec in _AC3_CODECS or codec == "A_EAC3"
    )


def sound(track: MkvTrack, first: bytes = b"") -> Sound:
    """`track`'s entry; `first`: its first frame (AC-3 and E-AC-3 read theirs from it)."""
    codec = track.codec
    if not plays(track):
        raise FilmRefused(SOUND, f"sound {codec or '?'}")
    if codec.startswith("A_AAC"):
        return _aac(track)
    if codec in _AC3_CODECS:
        return _ac3(track, first)
    return _eac3(track, first)


# ---- the plan -----------------------------------------------------------------------------


def plan(
    cues: Sequence[Cue], video: int, duration: int, first_cluster: int, units_per_s: float
) -> list[Piece]:
    """The film's pieces: from each keyframe the Cues name for track `video`, at least
    TARGET_S on to the next (the first from 0, at the first Cluster)."""
    points: list[Cue] = []
    for cue in cues:
        if cue.track == video and (not points or cue.time > points[-1].time):
            points.append(cue)
    if not points:
        raise FilmRefused(FORMAT, "no video cues")
    clusters = sorted({cue.cluster for cue in points})
    if points[0].time > 0 and first_cluster and first_cluster < points[0].cluster:
        points.insert(0, Cue(0, video, first_cluster))
    least = TARGET_S * units_per_s
    starts = [points[0]]
    for cue in points[1:]:
        if cue.time - starts[-1].time >= least and cue.cluster >= starts[-1].cluster:
            starts.append(cue)
    end = max(duration, starts[-1].time + 1)
    pieces = []
    for at, cue in enumerate(starts):
        following = starts[at + 1] if at + 1 < len(starts) else None
        bound = None
        if following is not None:
            later = [c for c in clusters if c > following.cluster]
            bound = later[0] if later else None
        pieces.append(
            Piece(cue.time if at else 0, following.time if following else end, cue.cluster, bound)
        )
    return pieces


def playlist(pieces: Sequence[Piece], units_per_s: float) -> str:
    lengths = [(piece.end - piece.start) / units_per_s for piece in pieces]
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:7",
        f"#EXT-X-TARGETDURATION:{max(1, round(max(lengths) + 0.5))}",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        "#EXT-X-INDEPENDENT-SEGMENTS",
        '#EXT-X-MAP:URI="init.mp4"',
    ]
    for index, length in enumerate(lengths):
        lines += [f"#EXTINF:{length:.3f},", f"{index}.m4s"]
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


# ---- a piece ------------------------------------------------------------------------------


class Collector:
    """A piece's frames picked from what its reading brings (`add`), until `done`; `fragment`
    makes them a media segment.

    The video's run from the keyframe at the piece's start, in decode order, to the keyframe
    at its end (left for the next piece). The sound's frames go by slot (its frame length's
    grid from time 0): those starting within the piece's span, so that a frame belongs to one
    piece alone and the runs follow on from piece to piece."""

    def __init__(
        self,
        piece: Piece,
        last: bool,
        video: MkvTrack,
        audio: Optional[MkvTrack],
        sound_of: Optional[Sound],
        scale_ns: int,
    ) -> None:
        self._piece, self._last = piece, last
        self._video, self._audio, self._sound = video, audio, sound_of
        self._scale = scale_ns
        self._frames: list[Frame] = []
        self._sounds: list[tuple[int, bytes]] = []
        self._started = self._stopped = False
        self._past = audio is None or sound_of is None  # a sound frame came past the span
        if sound_of is not None:
            self._first_slot = self._slot_at(piece.start, ceil=True)
            self._end_slot = self._slot_at(piece.end, ceil=True)

    @property
    def done(self) -> bool:
        return not self._last and self._stopped and self._past

    def _slot_at(self, units: int, ceil: bool = False) -> int:
        """The sound's slot at time `units`: the nearest (or the first not before it)."""
        assert self._sound is not None
        scale = 1_000_000_000 * self._sound.frame
        samples = units * self._scale * self._sound.entry.rate
        return (samples + scale - 1) // scale if ceil else (2 * samples + scale) // (2 * scale)

    def add(self, frames: Sequence[Frame]) -> None:
        for frame in frames:
            if frame.track == self._video.number:
                self._add_video(frame)
            elif self._audio is not None and frame.track == self._audio.number:
                self._add_sound(frame)

    def _add_video(self, frame: Frame) -> None:
        if self._stopped:
            return
        if not self._started:
            if not frame.key or frame.time < self._piece.start:
                return
            self._started = True
        elif frame.key and not self._last and frame.time >= self._piece.end:
            self._stopped = True
            return
        self._frames.append(frame)

    def _add_sound(self, frame: Frame) -> None:
        slot = self._slot_at(frame.time) + frame.lace
        if slot >= self._end_slot and not self._last:
            self._past = True
        elif slot >= self._first_slot:
            self._sounds.append((slot, frame.data))

    def fragment(self, sequence: int) -> bytes:
        runs = [self._video_run()]
        if self._sound is not None and self._sounds:
            runs.append(self._sound_run())
        if not self._frames:
            logger.warning("Film piece %s: no video frames found", sequence - 1)
        return mp4.fragment(sequence, runs)

    def _ticks(self, units: int) -> int:
        return units * self._scale * _VIDEO_SCALE // 1_000_000_000

    def _video_run(self) -> mp4.Run:
        frames = self._frames
        pts = [self._ticks(frame.time) for frame in frames]
        dts: list[int] = []
        for value in sorted(pts):  # decode times: the presentation times in decode order
            dts.append(max(value, dts[-1] + 1) if dts else value)
        steps = [b - a for a, b in zip(dts, dts[1:])]
        default = self._video.default_duration_ns * _VIDEO_SCALE // 1_000_000_000
        default = default or (sorted(steps)[len(steps) // 2] if steps else 3750)
        samples = [
            mp4.Sample(
                duration=steps[k] if k < len(steps) else default,
                size=len(frame.data),
                flags=mp4.KEY_FLAGS if frame.key else mp4.DELTA_FLAGS,
                offset=pts[k] - dts[k],
            )
            for k, frame in enumerate(frames)
        ]
        data = b"".join(frame.data for frame in frames)
        return mp4.Run(
            mp4.VIDEO_ID, dts[0] if dts else self._ticks(self._piece.start), samples, data
        )

    def _sound_run(self) -> mp4.Run:
        assert self._sound is not None
        frame = self._sound.frame
        slots: list[int] = []
        for slot, _data in self._sounds:
            slots.append(max(slot, slots[-1] + 1) if slots else slot)
        samples = [
            mp4.Sample(
                duration=((slots[k + 1] - slot) if k + 1 < len(slots) else 1) * frame,
                size=len(data),
                flags=mp4.KEY_FLAGS,
            )
            for k, (slot, (_slot, data)) in enumerate(zip(slots, self._sounds))
        ]
        data = b"".join(data for _slot, data in self._sounds)
        return mp4.Run(mp4.AUDIO_ID, slots[0] * frame, samples, data)


# ---- a film -------------------------------------------------------------------------------


class Film:
    """One film as HLS (see the module): `open` once, then `playlist`, `init` and `piece`."""

    def __init__(self, fetcher: Fetcher) -> None:
        self._fetcher = fetcher
        self._lock = asyncio.Lock()
        self._cache: OrderedDict[int, bytes] = OrderedDict()
        self._pieces: list[Piece] = []
        self._init = b""
        self._playlist = ""
        self._video: Optional[MkvTrack] = None
        self._audio: Optional[MkvTrack] = None
        self._sound: Optional[Sound] = None
        self._scale = 1_000_000

    @property
    def pieces(self) -> list[Piece]:
        return list(self._pieces)

    async def open(self) -> None:
        """Read the film's layout and plan its pieces. Raises FilmRefused."""
        try:
            await self._open()
        except MkvError as error:
            raise FilmRefused(FORMAT, str(error)) from error

    async def _open(self) -> None:
        head = read_head(await self._fetcher.read(0, HEAD_BYTES))
        seeks = dict(head.seeks)
        if CUES not in seeks and SEEK_HEAD in seeks:  # a second SeekHead, at the end
            for kind, position in read_seek_head(
                await self._element(seeks[SEEK_HEAD], SEEK_HEAD), head.segment
            ).items():
                seeks.setdefault(kind, position)
        info = head.info
        if info is None and INFO in seeks:
            info = read_info(await self._element(seeks[INFO], INFO))
        tracks = head.tracks
        if tracks is None and TRACKS in seeks:
            tracks = read_tracks(await self._element(seeks[TRACKS], TRACKS))
        if info is None or tracks is None:
            raise FilmRefused(FORMAT, "no Info or Tracks")
        if info.duration <= 0:
            raise FilmRefused(FORMAT, "no duration")
        if CUES not in seeks:
            raise FilmRefused(FORMAT, "no Cues")
        self._scale = info.scale_ns
        videos = [track for track in tracks if track.kind == "video" and track.enabled]
        if not videos:
            raise FilmRefused(FORMAT, "no video")
        video = sorted(videos, key=lambda track: not track.default)[0]
        entry = video_entry(video)
        sounds = [track for track in tracks if track.kind == "audio" and track.enabled]
        playable = [track for track in sorted(sounds, key=lambda t: not t.default) if plays(track)]
        if sounds and not playable:
            raise FilmRefused(SOUND, f"sound {sounds[0].codec or '?'}")
        audio = playable[0] if playable else None
        cues = read_cues(await self._element(seeks[CUES], CUES), head.segment)
        first_cluster = head.first_cluster or seeks.get(CLUSTER, 0)
        units_per_s = 1_000_000_000 / info.scale_ns
        self._pieces = plan(cues, video.number, round(info.duration), first_cluster, units_per_s)
        sound_of = None
        if audio is not None:
            first = b""
            if not audio.codec.startswith("A_AAC"):
                first = await self._first_frame(audio, first_cluster or self._pieces[0].position)
            sound_of = sound(audio, first)
        self._video, self._audio, self._sound = video, audio, sound_of
        self._init = mp4.init(entry, None if sound_of is None else sound_of.entry)
        self._playlist = playlist(self._pieces, units_per_s)

    async def _element(self, position: int, expected: int) -> bytes:
        """The body of the element at `position` (one of ID `expected`)."""
        head = await self._fetcher.read(position, position + 12)
        try:
            element_id, size, header = element_header(head)
        except Truncated as short:
            raise FilmRefused(FORMAT, "element cut short") from short
        if element_id != expected or size is None or size > MAX_ELEMENT:
            raise FilmRefused(FORMAT, f"no element {expected:x} where the SeekHead says")
        body = await self._fetcher.read(position + header, position + header + size)
        if len(body) < size:
            raise FilmRefused(FORMAT, "element cut short")
        return body

    async def _first_frame(self, audio: MkvTrack, position: int) -> bytes:
        reader = ClusterReader({audio.number: audio}, position)
        async for chunk in self._fetcher.stream(position, position + FIRST_FRAME_BYTES):
            for frame in reader.feed(chunk):
                return frame.data
            if reader.done:
                break
        raise FilmRefused(SOUND, "no sound frame")

    def playlist(self) -> str:
        return self._playlist

    def init(self) -> bytes:
        return self._init

    async def piece(self, index: int) -> bytes:
        """Piece `index` as a media segment (IndexError: none such). Raises FilmRefused,
        MkvError, or what the fetcher raises."""
        if not 0 <= index < len(self._pieces):
            raise IndexError(index)
        async with self._lock:  # the provider is read once at a time
            cached = self._cache.get(index)
            if cached is not None:
                self._cache.move_to_end(index)
                return cached
            try:
                data = await self._read_piece(index)
            except MkvError:
                raise  # the file's: reading it again changes nothing
            except Exception as error:  # the provider's (refused, cut off, slow): once more
                logger.info("Film piece %s: reading again after %s", index, error)
                data = await self._read_piece(index)
            self._cache[index] = data
            while len(self._cache) > _CACHED:
                self._cache.popitem(last=False)
            return data

    async def _read_piece(self, index: int) -> bytes:
        assert self._video is not None
        piece = self._pieces[index]
        last = index == len(self._pieces) - 1
        tracks = {self._video.number: self._video}
        if self._audio is not None:
            tracks[self._audio.number] = self._audio
        collect = Collector(piece, last, self._video, self._audio, self._sound, self._scale)
        reader = ClusterReader(tracks, piece.position)
        async for chunk in self._fetcher.stream(piece.position, piece.bound):
            collect.add(reader.feed(chunk))
            if collect.done or reader.done or reader.position - piece.position > MAX_PIECE:
                break
        return collect.fragment(index + 1)


# ---- over HTTP ----------------------------------------------------------------------------


class HttpFetcher:
    """A film's bytes from its provider in HTTP ranges (aiohttp, on the home door's loop)."""

    CHUNK = 256 * 1024

    def __init__(self, session: Any, url: str, headers: dict, verify: bool = True) -> None:
        self._session = session
        self._url = url
        self._headers = dict(headers)
        self._ssl = None if verify else False

    def _get(self, start: int, end: Optional[int]) -> Any:
        last = "" if end is None else str(end - 1)
        headers = {**self._headers, "Range": f"bytes={start}-{last}"}
        return self._session.get(self._url, headers=headers, ssl=self._ssl)

    @staticmethod
    def _check(response: Any, start: int, end: Optional[int]) -> None:
        if response.status != 206:
            asked = f"{start}-{'' if end is None else end - 1}"
            raise FilmRefused(PROVIDER, f"HTTP {response.status} to the range {asked}")

    async def read(self, start: int, end: int) -> bytes:
        data = bytearray()
        async for chunk in self.stream(start, end):
            data += chunk
            if len(data) >= end - start:
                break
        return bytes(data[: end - start])

    async def stream(self, start: int, end: Optional[int]) -> AsyncIterator[bytes]:
        async with self._get(start, end) as response:
            self._check(response, start, end)
            async for chunk in response.content.iter_chunked(self.CHUNK):
                yield chunk


@dataclass
class _Shelved:
    film: Film
    device: str
    used: float


class FilmShelf:
    """The films phones play now, by token (one per phone: its next replaces it)."""

    def __init__(self) -> None:
        self._films: OrderedDict[str, _Shelved] = OrderedDict()

    def put(self, device: str, film: Film) -> str:
        for token in [t for t, kept in self._films.items() if kept.device == device]:
            del self._films[token]
        token = secrets.token_urlsafe(16)
        self._films[token] = _Shelved(film, device, time.monotonic())
        while len(self._films) > SHELF:
            self._films.popitem(last=False)
        return token

    def get(self, token: str) -> Optional[Film]:
        kept = self._films.get(token)
        if kept is None:
            return None
        kept.used = time.monotonic()
        return kept.film

    def clear(self) -> None:
        self._films.clear()

    def __len__(self) -> int:
        return len(self._films)


def add_routes(app: Any, shelf: FilmShelf) -> None:
    """The films' routes on the home door's aiohttp app (before its page's catch-all)."""
    from aiohttp import web

    headers = {"Cache-Control": "no-cache"}

    def film_of(request: web.Request) -> Film:
        film = shelf.get(request.match_info["token"])
        if film is None:
            raise web.HTTPNotFound(text="404: Not Found")
        return film

    async def index(request: web.Request) -> web.StreamResponse:
        return web.Response(
            text=film_of(request).playlist(),
            content_type="application/vnd.apple.mpegurl",
            headers=headers,
        )

    async def init(request: web.Request) -> web.StreamResponse:
        return web.Response(body=film_of(request).init(), content_type="video/mp4", headers=headers)

    async def piece(request: web.Request) -> web.StreamResponse:
        film = film_of(request)
        try:
            data = await film.piece(int(request.match_info["n"]))
        except IndexError, ValueError:
            raise web.HTTPNotFound(text="404: Not Found")
        except Exception as error:  # the provider's or the file's: this piece fails alone
            logger.warning("Film piece %s failed: %s", request.match_info["n"], error)
            raise web.HTTPBadGateway(text="502: Bad Gateway")
        return web.Response(body=data, content_type="video/mp4", headers=headers)

    app.router.add_get("/film/{token}/index.m3u8", index)
    app.router.add_get("/film/{token}/init.mp4", init)
    app.router.add_get(r"/film/{token}/{n:\d+}.m4s", piece)
