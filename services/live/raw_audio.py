"""A radio station's raw sound as MPEG-TS, for the time-shift pipeline (2.14).

Most internet radio stations (Icecast and SHOUTcast servers) send an endless HTTP body of bare
MPEG audio frames (MP3, MP2) or ADTS AAC (AAC, HE-AAC), not MPEG-TS, which the pipeline reads
throughout: until 2.14 a radio list's stations played directly in VLC, without pause, rewind
or AI captions. `SoundSource` (`services.live.source`) reads such a stream through what is here
and yields one program's TS, so the ring, the timeline, kept buffers, stay-live, recordings and
the captions' side output take it as any radio channel's TS:

- `IcyReader` takes the station's inline metadata out of the sound (`icy-metaint`: every so
  many bytes, a length byte times 16 and that much text), and reads the song it names
  (`stream_title`: its StreamTitle as UTF-8; text that is no UTF-8 is kept as its bytes, a
  `Song` marked `legacy`, which `decoded` reads in the station's code page where it shows).
- `sniff` tells, from the first bytes, sound QiTV reads (a chain of `_CHAIN` frames, after an
  ID3 tag), more to read, or a format it cannot (Ogg, FLAC, ASF/WMA, MP4, a page or a list).
- `AudioFramer` cuts the sound into whole frames, finding its sync anew where a connection
  starts mid-frame, the data is damaged, or an ID3 tag comes between frames.
- `RepeatCut` leaves out, after a reconnect, the frames the station's burst at connect sends
  again (Icecast starts each listener a few seconds back): their bytes are the same, so the
  first `_REPEAT_RUN` frames' digests are looked up, as a run, in those sent last (a run, not
  one frame: digital silence repeats itself), and what repeats is dropped until one is new.
- `AudioMuxer` writes the frames as TS (`services.live.ts_mux`): PAT and PMT every
  `_TABLES_TICKS` of sound, each frame one PES on `AUDIO_PID` (stream type 0x03 MPEG-1 audio,
  0x04 MPEG-2 audio, 0x0F ADTS AAC), its PTS run on by the frames' own durations, and a PCR
  `PCR_LEAD` before it on the same PID. The clock and the counters run on across the station's
  reconnects, so the stream has no break: a reconnect only leaves out what the station
  missed meanwhile. A packed HLS segment's frames (`packed_start`: the PTS its ID3 tag dates
  them with) are written the same way at their own time.

Free-format MP3 and Layer I-III's reserved values are not read. Pure logic: no I/O.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable
import re
import time
from typing import NamedTuple, cast

from services.live.ts_mux import (
    PCR_LEAD,
    PTS_MODULO,
    Counters,
    audio_pes,
    pes_packets,
    psi_packet,
    section,
)

ADTS_RATES = (96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000)
PMT_PID = 0x1000
AUDIO_PID = 0x100
_CLOCK = 90000
_FIRST_PTS = 126000  # 1.4 s: the PCR starts 0.7 s in, as ffmpeg's muxer starts it
_TABLES_TICKS = _CLOCK // 2  # PAT and PMT again after this much sound
_CHAIN = 3  # this many frames, each right after the one before, prove the sync
_SNIFF_LIMIT = 16 * 1024  # sound whose sync is not found this far in is not read
_LOST_LIMIT = 64 * 1024  # this much without a frame: the stream is no sound QiTV reads
_REPEAT_FRAMES = 2048  # the frames whose digests a reconnect's burst is looked up in (~50 s)
_REPEAT_RUN = 8  # a reconnect's first this many frames are looked up as one run ...
# ... among those that came within this long before it: a burst at connect repeats at most the
# station's last this-many seconds (after a longer outage it repeats nothing: the same sound
# again then is the station's own, a looped programme, say)
_BURST_MAX_S = 60.0
_TIMESTAMP_OWNER = b"com.apple.streaming.transportStreamTimestamp\x00"
_MPEG_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}
# kbit/s by bitrate index (1-14), for (MPEG-1, layer bits: 3 = I, 2 = II, 1 = III)
_LOW = (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)
_KBPS = {
    (True, 3): (32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),
    (True, 2): (32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
    (True, 1): (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    (False, 3): (32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256),
    (False, 2): _LOW,
    (False, 1): _LOW,
}
# What no frame of sound starts with: Ogg (Vorbis, Opus), FLAC, ASF (WMA), RIFF (WAV), a PLS
# list; an MP4's "ftyp" sits 4 bytes in.
_UNREADABLE = (b"OggS", b"fLaC", b"\x30\x26\xb2\x75\x8e\x66\xcf\x11", b"RIFF", b"[playlist]")
_TITLE = re.compile(rb"StreamTitle='(.*?)';(?=\s*[A-Za-z]+=|\s*$)", re.DOTALL)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")

SOUND, MORE, UNREADABLE = "sound", "more", "unreadable"


class Frame(NamedTuple):
    """One frame of sound: its bytes (header included), samples, sample rate and stream type."""

    data: bytes
    samples: int
    rate: int
    stream_type: int


class _Header(NamedTuple):
    length: int
    samples: int
    rate: int
    stream_type: int
    form: tuple[int, int, int]  # what each frame of one stream has alike


def adts_header(data: bytes | bytearray, pos: int) -> tuple[int, int, int] | None:
    """(length, samples, sample rate) of the ADTS frame whose header starts at `pos` (7 bytes
    of it there); None when none does (no ADTS sync, a reserved rate, a length under 7)."""
    if pos + 7 > len(data) or data[pos] != 0xFF or data[pos + 1] & 0xF6 != 0xF0:
        return None
    rate_index = (data[pos + 2] >> 2) & 0x0F
    length = ((data[pos + 3] & 0x03) << 11) | (data[pos + 4] << 3) | (data[pos + 5] >> 5)
    if length < 7 or rate_index >= len(ADTS_RATES):
        return None
    return length, 1024 * (1 + (data[pos + 6] & 0x03)), ADTS_RATES[rate_index]


def _header(data: bytes | bytearray, pos: int) -> _Header | None:
    """The frame header at `pos` (ADTS or MPEG audio), None when there is none (or too little
    of it to tell)."""
    adts = adts_header(data, pos)
    if adts is not None:
        length, samples, rate = adts
        return _Header(length, samples, rate, 0x0F, (0x0F, rate, 0))
    if pos + 4 > len(data) or data[pos] != 0xFF or data[pos + 1] & 0xE0 != 0xE0:
        return None
    b1, b2 = data[pos + 1], data[pos + 2]
    version, layer = (b1 >> 3) & 3, (b1 >> 1) & 3
    index, rate_index = b2 >> 4, (b2 >> 2) & 3
    if version == 1 or layer == 0 or index in (0, 15) or rate_index == 3:
        return None  # reserved, free format or a bad bitrate
    mpeg1 = version == 3
    kbps, rate = _KBPS[(mpeg1, layer)][index - 1], _MPEG_RATES[version][rate_index]
    padding = (b2 >> 1) & 1
    if layer == 3:  # Layer I: 4-byte slots
        length, samples = (12000 * kbps // rate + padding) * 4, 384
    elif layer == 2 or mpeg1:  # Layer II, MPEG-1 Layer III
        length, samples = 144000 * kbps // rate + padding, 1152
    else:  # MPEG-2 and 2.5 Layer III
        length, samples = 72000 * kbps // rate + padding, 576
    stream_type = 0x03 if mpeg1 else 0x04
    return _Header(length, samples, rate, stream_type, (stream_type, rate, layer))


def _id3_size(data: bytes | bytearray, pos: int) -> int | None:
    """The length of the ID3v2 tag at `pos` (its header, body and footer); None when there is
    none (or its 10-byte header is not there yet)."""
    if data[pos : pos + 3] != b"ID3" or pos + 10 > len(data):
        return None
    size = data[pos + 6] << 21 | data[pos + 7] << 14 | data[pos + 8] << 7 | data[pos + 9]
    return 10 + size + (10 if data[pos + 5] & 0x10 else 0)


def _chain(data: bytes | bytearray, pos: int) -> bool | None:
    """Whether `_CHAIN` frames of one form follow each other from `pos`; None when the data
    ends before that is known."""
    first = _header(data, pos)
    if first is None:
        return False if pos + 7 <= len(data) else None
    at, header = pos, first
    for _ in range(_CHAIN - 1):
        at += header.length
        if at + 7 > len(data):
            return None
        found = _header(data, at)
        if found is None or found.form != first.form:
            return False
        header = found
    return True


def find_sync(data: bytes | bytearray, start: int = 0) -> tuple[int, bool]:
    """Where frames begin at or after `start`: (that position, True); else (where to look again
    once more data came, False), past the data's end when an ID3 tag goes on beyond it. ID3
    tags are stepped over."""
    pos, found, _tags = _find_sync(data, start)
    return pos, found


def _find_sync(data: bytes | bytearray, start: int) -> tuple[int, bool, int]:
    """`find_sync`, and how many bytes of the data were ID3 tags it stepped over."""
    pos, end, tags = start, len(data), 0
    while pos < end:
        tag = _id3_size(data, pos)
        if tag is not None:
            tags += min(tag, end - pos)
            pos += tag
            continue
        if data[pos] == 0xFF:
            chained = _chain(data, pos)
            if chained:
                return pos, True, tags
            if chained is None:
                return pos, False, tags
        elif data[pos : pos + 3] == b"ID3"[: end - pos]:
            return pos, False, tags  # perhaps a tag, its header not all here yet
        pos += 1
    return pos, False, tags


def sniff(data: bytes) -> str:
    """What a station's first bytes (its sound, the metadata taken out) are: `SOUND` (frames
    QiTV reads begin within `_SNIFF_LIMIT` of the ID3 tags it may start with), `MORE` (too
    little to tell yet) or `UNREADABLE`."""
    pos = 0
    while (tag := _id3_size(data, pos)) is not None:
        pos += tag
    if pos >= len(data):
        return MORE
    head = data[pos:].lstrip(b"\r\n\t ")
    if head.startswith(_UNREADABLE) or head[:1] == b"<" or data[pos + 4 : pos + 8] == b"ftyp":
        return UNREADABLE
    at, found = find_sync(data, pos)
    if found:
        return SOUND
    return MORE if min(at, len(data)) - pos < _SNIFF_LIMIT else UNREADABLE


class AudioFramer:
    """Cuts raw MPEG audio or ADTS bytes into whole frames (see the module)."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._form: tuple[int, int, int] | None = None  # None: out of sync
        self._skip = 0  # an ID3 tag's bytes still to come and be left out
        self.lost = 0  # bytes since the last whole frame (held or left out), ID3 tags' not

    def reset(self) -> None:
        """A new connection: a frame cut short is left out, and the sync is found anew."""
        self._buf.clear()
        self._form, self._skip = None, 0

    def feed(self, data: bytes) -> list[Frame]:
        if self._skip:
            dropped = min(self._skip, len(data))
            self._skip -= dropped
            data = data[dropped:]
        buf = self._buf
        buf += data
        total = len(buf)
        frames: list[Frame] = []
        pos, last_end = 0, None
        tags = 0  # bytes of ID3 tags stepped over since the last frame (none lost)
        while True:
            if self._form is None:
                pos, found, stepped = _find_sync(buf, pos)
                tags += stepped
                if not found:
                    break
                self._form = cast(_Header, _header(buf, pos)).form
            header = _header(buf, pos)
            if header is None or header.form != self._form:
                tag = _id3_size(buf, pos)
                if tag is not None:  # a tag between frames: stepped over, in sync
                    tags += min(tag, len(buf) - pos)
                    pos += tag
                    continue
                if pos + 7 > len(buf):
                    break  # too little to tell
                self._form = None  # lost: found again further on
                pos += 1
                continue
            if pos + header.length > len(buf):
                break
            end = pos + header.length
            frames.append(
                Frame(bytes(buf[pos:end]), header.samples, header.rate, header.stream_type)
            )
            pos = last_end = end
            tags = 0
        if pos > len(buf):  # inside an ID3 tag that goes on: the rest of it is left out too
            self._skip = pos - len(buf)
            pos = len(buf)
        del buf[:pos]
        if last_end is not None:  # what came after the last one, held or left out
            self.lost = max(0, total - last_end - tags)
        else:
            self.lost += max(0, len(data) - tags)
        if self._form is None and len(buf) > _LOST_LIMIT:
            del buf[:-8]  # no sync this far: only a header's worth is kept
        return frames

    @property
    def unreadable(self) -> bool:
        """So long without a whole frame that the stream is no sound QiTV reads."""
        return self.lost > _LOST_LIMIT


class Song(NamedTuple):
    """The song a station names: its text (UTF-8; "" for none), or, `legacy`, text that is no
    UTF-8 as its bytes read one to one (Latin-1), for `decoded` to read in a code page."""

    text: str
    legacy: bool = False


def clean(text: str) -> str:
    """A song's text as shown: control characters as spaces, none at its ends."""
    return _CONTROL.sub(" ", text).strip()


def stream_title(meta: bytes) -> Song | None:
    """The song a metadata block names (its StreamTitle), None when it names nothing."""
    found = _TITLE.search(meta.rstrip(b"\x00"))
    if found is None:
        return None
    title = found.group(1)
    try:
        return Song(clean(title.decode("utf-8")))
    except UnicodeDecodeError:
        return Song(title.decode("latin-1"), legacy=True)


def decoded(text: str, legacy: bool, codepage: str) -> str:
    """A song's text to show: as it is, or a `legacy` one's bytes in `codepage` (the
    station's language's code page, `services.free_radio.codepage`; cp1252 when unknown)."""
    if not legacy:
        return text
    try:
        return clean(text.encode("latin-1").decode(codepage or "cp1252", errors="replace"))
    except LookupError, UnicodeEncodeError:
        return clean(text)


class IcyReader:
    """A station's body without its inline metadata (`icy-metaint` bytes of sound, then a
    length byte times 16 and that much metadata, again and again; 0: none comes): `feed`
    returns the sound and, where a block names one, the song (`stream_title`, a `Song`) in
    order."""

    def __init__(self, every: int) -> None:
        self._every = every
        self._left = every  # sound bytes before the next length byte
        self._meta = bytearray()
        self._meta_left = 0  # metadata bytes still to come (0: in the sound)
        self._length = False  # the next byte is a length byte

    def feed(self, data: bytes) -> list[bytes | Song]:
        if self._every <= 0:
            return [data] if data else []
        out: list[bytes | Song] = []
        pos, end = 0, len(data)
        while pos < end:
            if self._length:
                self._length = False
                self._meta_left = data[pos] * 16
                pos += 1
                self._meta.clear()
                if not self._meta_left:
                    self._left = self._every
                continue
            if self._meta_left:
                take = min(self._meta_left, end - pos)
                self._meta += data[pos : pos + take]
                self._meta_left -= take
                pos += take
                if not self._meta_left:
                    self._left = self._every
                    title = stream_title(bytes(self._meta))
                    if title is not None:
                        out.append(title)
                continue
            take = min(self._left, end - pos)
            out.append(data[pos : pos + take])
            self._left -= take
            pos += take
            if not self._left:
                self._length = True
        return out


def metaint(headers: object) -> int:
    """The `icy-metaint` an answer's headers name (its sound bytes between two metadata
    blocks); 0 when they name none, or nonsense."""
    get = getattr(headers, "get", None)
    value = get("icy-metaint") if callable(get) else None
    try:
        every = int(str(value).strip()) if value is not None else 0
    except ValueError:
        return 0
    return every if 0 < every <= 1 << 20 else 0


class RepeatCut:
    """After a reconnect, the frames the station sends again (see the module)."""

    def __init__(
        self,
        keep: int = _REPEAT_FRAMES,
        run: int = _REPEAT_RUN,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sent: deque[int] = deque(maxlen=keep)  # the digests of the frames passed on ...
        self._at_times: deque[float] = deque(maxlen=keep)  # ... and when each came
        self._clock = clock
        self._run = run
        # A reconnect's first frames (digest, frame), held until a run of them is looked up.
        self._pending: list[tuple[int, Frame]] | None = None
        self._at: int | None = None  # where in `_sent` the repeat has got to
        self.dropped = 0  # frames left out so far

    def reconnect(self) -> list[Frame]:
        """A new connection: its first frames are looked up as a run. What the last one's
        are still held for goes on now (a connection that brought fewer)."""
        held = self._settle()
        self._pending, self._at = [], None
        return held

    def flush(self) -> list[Frame]:
        """The input ends: what a reconnect's run still holds goes on."""
        return self._settle()

    def feed(self, frames: Iterable[Frame]) -> list[Frame]:
        out: list[Frame] = []
        sent = self._sent
        for frame in frames:
            digest = hash(frame.data)
            if self._pending is not None:
                self._pending.append((digest, frame))
                if len(self._pending) >= self._run:
                    out.extend(self._settle())
                continue
            if self._at is not None:
                if self._at < len(sent) and sent[self._at] == digest:
                    self._at += 1
                    self.dropped += 1
                    continue
                self._at = None  # new sound: from here on as it comes
            self._pass(digest)
            out.append(frame)
        return out

    def _pass(self, digest: int) -> None:
        self._sent.append(digest)
        self._at_times.append(self._clock())

    def _settle(self) -> list[Frame]:
        """The frames held after a reconnect, less those that repeat what was sent: the run is
        aligned at the earliest place in `_sent` where all of it that `_sent` reaches matches
        (a run of silence matches anywhere in a silence: the run's later frames decide)."""
        pending, self._pending = self._pending, None
        if not pending:
            return []
        sent, digests = self._sent, [digest for digest, _frame in pending]
        since = self._clock() - _BURST_MAX_S
        first = next((k for k, at in enumerate(self._at_times) if at >= since), len(sent))
        start = next(
            (
                index
                for index in range(first, len(sent))
                if all(
                    sent[index + k] == digests[k]
                    for k in range(min(len(digests), len(sent) - index))
                )
            ),
            None,
        )
        matched = 0 if start is None else min(len(digests), len(sent) - start)
        self.dropped += matched
        if start is not None and start + matched < len(sent):
            self._at = start + matched  # the repeat goes on past the run
        out = []
        for digest, frame in pending[matched:]:
            self._pass(digest)
            out.append(frame)
        return out


def packed_start(data: bytes) -> tuple[int | None, int]:
    """A packed HLS audio segment's start: the PTS its leading ID3 tags date its first frame
    with (the PRIV com.apple.streaming.transportStreamTimestamp; None without one) and where
    the frames begin after them."""
    pos, pts = 0, None
    while data[pos : pos + 3] == b"ID3" and pos + 10 <= len(data):
        size = _id3_size(data, pos)
        assert size is not None
        tag = data[pos + 10 : pos + size]
        at = tag.find(_TIMESTAMP_OWNER)
        if at >= 0 and at + len(_TIMESTAMP_OWNER) + 8 <= len(tag):
            raw = tag[at + len(_TIMESTAMP_OWNER) : at + len(_TIMESTAMP_OWNER) + 8]
            pts = int.from_bytes(raw, "big") % PTS_MODULO
        pos += size
    return pts, pos


class AudioMuxer:
    """Frames of sound as one program's MPEG-TS (see the module)."""

    def __init__(self, first_pts: int | None = None) -> None:
        # The next frame's PTS (not wrapped); `started` once given or a frame is written.
        self._next = float(_FIRST_PTS if first_pts is None else first_pts)
        self.started = first_pts is not None
        self._counters = Counters()
        self._stream_type: int | None = None
        self._version = 0
        self._tables_at: float | None = None  # where the tables are due next

    @property
    def next_pts(self) -> int:
        """The PTS the next frame gets (where the sound written so far ends)."""
        return round(self._next) % PTS_MODULO

    def mux(self, frames: Iterable[Frame], pts: int | None = None) -> bytes:
        """`frames` as TS, the first at `pts` when given (a packed HLS segment's own time),
        else where the sound written so far ends."""
        if pts is not None:
            self._next = float(pts)
            self._tables_at = None
        out: list[bytes] = []
        for frame in frames:
            if frame.stream_type != self._stream_type:
                if self._stream_type is not None:
                    self._version = (self._version + 1) & 0x1F
                self._stream_type = frame.stream_type
                self._tables_at = None
            at = round(self._next)
            if self._tables_at is None or at >= self._tables_at:
                out.append(psi_packet(self._counters, 0, self._pat()))
                out.append(psi_packet(self._counters, PMT_PID, self._pmt()))
                self._tables_at = at + _TABLES_TICKS
            pes = audio_pes(0xC0, at, frame.data)
            out.extend(pes_packets(self._counters, AUDIO_PID, pes, pcr=at - PCR_LEAD))
            self._next += frame.samples * _CLOCK / frame.rate
            self.started = True
        return b"".join(out)

    def _pat(self) -> bytes:
        return section(0x00, 1, self._version, b"\x00\x01" + (0xE000 | PMT_PID).to_bytes(2, "big"))

    def _pmt(self) -> bytes:
        assert self._stream_type is not None
        body = (0xE000 | AUDIO_PID).to_bytes(2, "big") + b"\xf0\x00"  # the PCR's PID, no info
        body += bytes([self._stream_type]) + (0xE000 | AUDIO_PID).to_bytes(2, "big") + b"\xf0\x00"
        return section(0x02, 1, self._version, body)
