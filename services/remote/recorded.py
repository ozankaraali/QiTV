"""A recording as HLS for a phone's player, read from the disk as the phone plays it (2.8).

A recording is an MPEG-TS file (services/live/recorder.py), and Safari plays MPEG-TS in HLS, so
nothing is re-encoded or repackaged:

- `Recorded.open` reads the file's program tables (`parse_tables`): its H.264 picture and the
  first sound a phone plays (AAC, AC-3, E-AC-3), and finds its keyframes (`_Scan`: only the
  picture's PES starts are read, found a packet column at a time). Pieces of about `TARGET_S`
  go from one keyframe to another (`plan`); a timestamp jump (the stream reconnected) starts a
  piece after a discontinuity. A keyframe further than `SOFT_S` away starts a piece at a picture
  in between, so a growing playlist's target duration holds every piece; a piece over
  `MAX_PIECE_BYTES` (its keyframes lost) is left out, and the playlist goes on after a break.
- A recording read whole within `OPEN_PREFER_S` plays as a whole (a VOD playlist). A larger one
  answers once read past where the phone starts (or after `OPEN_WAIT_S`), as an EVENT playlist
  that grows as the rest is read behind (a thread of its own, stopped by `close`), until it
  ends (`#EXT-X-ENDLIST`).
- `Recorded.piece` reads a piece from the disk and keeps the picture's, the sound's and the
  clock's packets, behind tables naming only them (another sound, teletext and the stream's own
  tables are left out: a phone's player takes the first sound it is told of).
- A recording still written (as the TV says, `writing`; else its file changed within
  `GROWING_S`) is an EVENT playlist: the phone asks again, the file is read on from where it
  was, and pieces are added once complete, until the recording ends (`#EXT-X-ENDLIST`).

The pieces' reads and the scans run on the event loop's executor: the home door's loop never
waits for the disk. A recording a phone can't play is refused (`FilmRefused`) as a film is:
"format" (HEVC, MPEG-2, High 10, no keyframe), "sound" (only MPEG audio and the like). Nothing
here logs a path.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import math
import os
import threading
import time
from typing import Any, Callable, Optional

from services.live.ts import _crc32, _pes_start, pts_delta
from services.remote.films import _H264_PROFILES, FORMAT, SOUND, FilmRefused

__all__ = ["FilmRefused", "Recorded", "Tables", "add_routes", "parse_tables", "plan"]

PACKET = 188
TARGET_S = 6.0  # a piece's length, at the least (keyframes permitting)
SOFT_S = 10.0  # a picture this long after the last keyframe starts a piece (a long GOP)
GROWING_S = 30.0  # a file changed this recently is still being recorded (the TV not saying)
# A growing playlist's target duration (it may not change as it grows): pieces are shorter
# than TARGET_S + SOFT_S.
EVENT_TARGET_S = 17
HEAD_BYTES = 2 * 1024 * 1024  # where the program tables are looked for
CHUNK = PACKET * 22310  # about 4 MiB, whole packets
BATCH = 16 * CHUNK  # read between two looks at the pieces (and at `close`)
MAX_PIECE_BYTES = 64 * 1024 * 1024  # a piece is read into memory whole: none larger
OPEN_PREFER_S = 8.0  # read whole within this: a VOD playlist
OPEN_WAIT_S = 20.0  # else it answers once read past where the phone starts, or after this
_CLOCK = 90000
_BACK = _CLOCK  # a picture this far behind the last is a new timeline (B-frames step less)
_JUMP = 10 * _CLOCK  # ... and this far ahead
_FRAME = 3600  # one picture (25 a second) until the file shows its own
_H264 = 0x1B
_SOUNDS = {0x0F: "aac", 0x81: "ac3", 0x87: "eac3"}
_DVB_SOUNDS = {0x6A: "ac3", 0x7A: "eac3"}  # descriptors naming a private stream (0x06)
_PICTURES = (0x01, 0x02, 0x1B, 0x24, 0x10, 0x42, 0xEA)  # MPEG-1/2, H.264, HEVC, MPEG-4 and others
_SOUND_TYPES = (0x03, 0x04, 0x0F, 0x11, 0x81, 0x87)


@dataclass(frozen=True)
class Tables:
    """A recording's program: what a phone plays of it, and the tables naming only that."""

    program: int
    pmt_pid: int
    pcr_pid: int
    video_pid: int
    audio_pid: Optional[int]
    pmt: bytes  # the PMT section made for the phone (its CRC included)

    def keeps(self) -> frozenset[int]:
        kept = {self.video_pid, self.pcr_pid}
        if self.audio_pid is not None:
            kept.add(self.audio_pid)
        return frozenset(kept - {0, self.pmt_pid, 0x1FFF})  # (0x1FFF: no clock PID)

    def header(self, counter: int) -> bytes:
        """The PAT and the PMT a piece starts with (`counter`: their continuity counter)."""
        pat = _section(
            0x00, 1, bytes([self.program >> 8, self.program & 0xFF]) + _pid(self.pmt_pid)
        )
        return _psi_packets(0, pat, counter) + _psi_packets(self.pmt_pid, self.pmt, counter)


@dataclass(frozen=True)
class Piece:
    start: int  # the file position of its first keyframe's packet
    end: int  # of the next piece's, or the file's end
    seconds: float
    jump: bool  # a new timeline starts with it (a discontinuity before it)


@dataclass
class _Key:
    offset: int
    pts: int
    end: int  # the latest picture's PTS since it, on its timeline
    jump: bool


# ---- tables ---------------------------------------------------------------------------------


def _pid(pid: int) -> bytes:
    return bytes([0xE0 | (pid >> 8), pid & 0xFF])


def _section(table_id: int, extension: int, body: bytes) -> bytes:
    length = 5 + len(body) + 4
    head = bytes([table_id, 0xB0 | (length >> 8), length & 0xFF, extension >> 8, extension & 0xFF])
    data = head + b"\xc1\x00\x00" + body
    return data + _crc32(data).to_bytes(4, "big")


def _psi_packets(pid: int, section: bytes, counter: int) -> bytes:
    payload = b"\x00" + section  # the pointer field
    out = bytearray()
    first = True
    while payload:
        part, payload = payload[:184], payload[184:]
        out += bytes([0x47, (0x40 if first else 0) | (pid >> 8), pid & 0xFF, 0x10 | counter])
        out += part + b"\xff" * (184 - len(part))
        first, counter = False, (counter + 1) & 0x0F
    return bytes(out)


def _sections(data: bytes, pid: int) -> Optional[bytes]:
    """The first whole PSI section on `pid` in `data` (packets from its start)."""
    gathered = bytearray()
    for off in range(0, len(data) - PACKET + 1, PACKET):
        if data[off] != 0x47 or ((data[off + 1] & 0x1F) << 8 | data[off + 2]) != pid:
            continue
        start = data[off + 1] & 0x40
        if not gathered and not start:
            continue
        body = 4
        if data[off + 3] & 0x20:  # an adaptation field
            body += 1 + data[off + 4]
        if not data[off + 3] & 0x10 or body >= PACKET:
            continue
        payload = data[off + body : off + PACKET]
        if start and not gathered:
            payload = payload[1 + payload[0] :]  # the pointer field
        gathered += payload
        if len(gathered) >= 3:
            total = 3 + (((gathered[1] & 0x0F) << 8) | gathered[2])
            if len(gathered) >= total:
                section = bytes(gathered[:total])
                if _crc32(section) == 0:
                    return section
                gathered.clear()  # a damaged one: the next
    return None


def _descriptors(data: bytes) -> list[int]:
    tags, i = [], 0
    while i + 2 <= len(data):
        tags.append(data[i])
        i += 2 + data[i + 1]
    return tags


def parse_tables(head: bytes) -> Tables:
    """The program of a recording's first `HEAD_BYTES`; FilmRefused when a phone can't play it."""
    pat = _sections(head, 0)
    if pat is None:
        raise FilmRefused(FORMAT, "no program tables")
    end = 3 + (((pat[1] & 0x0F) << 8) | pat[2]) - 4
    programs = [
        (pat[i] << 8 | pat[i + 1], (pat[i + 2] & 0x1F) << 8 | pat[i + 3])
        for i in range(8, end - 3, 4)
        if pat[i] << 8 | pat[i + 1]
    ]
    if not programs:
        raise FilmRefused(FORMAT, "no program")
    program, pmt_pid = programs[0]
    pmt = _sections(head, pmt_pid)
    if pmt is None:
        raise FilmRefused(FORMAT, "no program map")
    end = 3 + (((pmt[1] & 0x0F) << 8) | pmt[2]) - 4
    pcr_pid = (pmt[8] & 0x1F) << 8 | pmt[9]
    i = 12 + (((pmt[10] & 0x0F) << 8) | pmt[11])
    video: Optional[tuple[int, int, bytes]] = None
    audio: Optional[tuple[int, int, bytes]] = None
    sounds = 0
    while i + 5 <= end:
        kind, es_pid = pmt[i], (pmt[i + 1] & 0x1F) << 8 | pmt[i + 2]
        size = ((pmt[i + 3] & 0x0F) << 8) | pmt[i + 4]
        descriptors = bytes(pmt[i + 5 : i + 5 + size])
        entry = (kind, es_pid, descriptors)
        if video is None and kind in _PICTURES:
            video = entry
        tags = _descriptors(descriptors) if kind == 0x06 else []
        if kind in _SOUND_TYPES or any(tag in _DVB_SOUNDS for tag in tags):
            sounds += 1
            if audio is None and (kind in _SOUNDS or any(tag in _DVB_SOUNDS for tag in tags)):
                audio = entry
        i += 5 + size
    if video is None or video[0] != _H264:
        raise FilmRefused(FORMAT, "picture 0x%02x" % (video[0] if video else 0))
    if sounds and audio is None:
        raise FilmRefused(SOUND, "no sound a phone plays")
    body = _pid(pcr_pid) + b"\xf0\x00"
    for kind, es_pid, descriptors in (e for e in (video, audio) if e is not None):
        size = len(descriptors)
        body += bytes([kind]) + _pid(es_pid) + bytes([0xF0 | (size >> 8), size & 0xFF])
        body += descriptors
    return Tables(
        program=program,
        pmt_pid=pmt_pid,
        pcr_pid=pcr_pid,
        video_pid=video[1],
        audio_pid=audio[1] if audio else None,
        pmt=_section(0x02, program, body),
    )


# ---- keyframes ------------------------------------------------------------------------------


def _h264(es: bytes) -> tuple[bool, Optional[int]]:
    """Whether a picture's first bytes start a keyframe (an IDR, or an SPS before an I-frame
    with its recovery point: broadcast's open GOPs), and its SPS's profile, if there."""
    key, profile = False, None
    i = es.find(b"\x00\x00\x01")
    while 0 <= i < len(es) - 3:
        kind = es[i + 3] & 0x1F
        if kind in (5, 7):
            key = True
        if kind == 7 and i + 4 < len(es) and profile is None:
            profile = es[i + 4]
        i = es.find(b"\x00\x00\x01", i + 3)
    return key, profile


class _Scan:
    """A recording's keyframes, read on as the file grows."""

    def __init__(self, tables: Tables) -> None:
        self.tables = tables
        self.keys: list[_Key] = []
        self.offset = 0  # where the next read starts (a packet's start)
        self.frame = 0  # the shortest step forward between pictures: one picture
        self.profile: Optional[int] = None
        self._last: Optional[int] = None
        self._jumped = False
        pid = tables.video_pid
        self._lo = pid & 0xFF
        self._his = (0x40 | (pid >> 8), 0x60 | (pid >> 8))  # a PES start, either priority

    def read(self, file: Any, size: int) -> bool:
        """Read the file from where the last read stopped to `size` (`BATCH` at most);
        whether it got there."""
        stop = min(size, self.offset + BATCH)
        while self.offset + PACKET <= stop:
            file.seek(self.offset)
            chunk = file.read(min(CHUNK, stop - self.offset))
            whole = len(chunk) // PACKET * PACKET
            if not whole:
                return True  # the file is shorter than it was (cut): nothing more now
            chunk = chunk[:whole]
            synced = self._synced(chunk)
            self._pictures(chunk[:synced])
            self.offset += synced
            if synced < whole:
                self.offset += self._resync(chunk[synced:])
        return self.offset + PACKET > size

    @staticmethod
    def _synced(chunk: bytes) -> int:
        """How far `chunk` keeps the packets' rhythm (all of it, but in a damaged file)."""
        count = len(chunk) // PACKET
        if chunk[::PACKET].count(0x47) == count:
            return len(chunk)
        return next(n for n in range(count) if chunk[n * PACKET] != 0x47) * PACKET

    @staticmethod
    def _resync(rest: bytes) -> int:
        """Where the rhythm comes back in `rest` (three sync bytes a packet apart)."""
        i = rest.find(b"\x47", 1)
        while 0 <= i < len(rest) - 2 * PACKET:
            if rest[i + PACKET] == 0x47 and rest[i + 2 * PACKET] == 0x47:
                return i
            i = rest.find(b"\x47", i + 1)
        return max(1, len(rest) - 2 * PACKET)

    def _pictures(self, chunk: bytes) -> None:
        firsts, seconds = chunk[1::PACKET], chunk[2::PACKET]
        found: list[int] = []
        for value in self._his:
            n = firsts.find(value)
            while n >= 0:
                if seconds[n] == self._lo:
                    found.append(n)
                n = firsts.find(value, n + 1)
        for n in sorted(found):
            pkt = chunk[n * PACKET : (n + 1) * PACKET]
            rai, pts, es = _pes_start(pkt)
            if pts is not None:
                self._picture(self.offset + n * PACKET, pts, rai, pkt[es:])

    def _picture(self, offset: int, pts: int, rai: bool, es: bytes) -> None:
        nal_key, profile = _h264(es)
        if profile is not None and self.profile is None:
            self.profile = profile
        if self._last is not None:
            step = pts_delta(self._last, pts)
            if step < -_BACK or step > _JUMP:
                self._jumped = True
            elif step > 0:
                self.frame = min(self.frame, step) if self.frame else step
        self._last = pts
        if rai or nal_key:
            self.keys.append(_Key(offset, pts, pts, self._jumped and bool(self.keys)))
            self._jumped = False
        elif (
            self.keys and not self._jumped and pts_delta(self.keys[-1].pts, pts) >= SOFT_S * _CLOCK
        ):
            self.keys.append(_Key(offset, pts, pts, False))  # a long GOP: a piece from here
        elif self.keys and not self._jumped and pts_delta(self.keys[-1].end, pts) > 0:
            self.keys[-1].end = pts


def plan(keys: list[_Key], frame: int, size: Optional[int]) -> list[Piece]:
    """Pieces of at least `TARGET_S` from keyframe to keyframe; the last runs to the file's
    `size`, or is left out while the file grows (None): pieces listed never change. One over
    `MAX_PIECE_BYTES` is left out (the next starts after a break)."""
    return plan_from(keys, frame, size, 0, False)[0]


def plan_from(
    keys: list[_Key], frame: int, size: Optional[int], i: int, broken: bool
) -> tuple[list[Piece], int, bool]:
    """`plan` from key `i` on (`broken`: a piece before was left out): the pieces, the key the
    next ones start at, and whether the last was left out."""
    frame = frame or _FRAME
    pieces: list[Piece] = []
    while i < len(keys):
        first = keys[i]
        j = i + 1
        while (
            j < len(keys)
            and not keys[j].jump
            and pts_delta(first.pts, keys[j].pts) < TARGET_S * _CLOCK
        ):
            j += 1
        if j < len(keys) and not keys[j].jump:
            end, ticks = keys[j].offset, pts_delta(first.pts, keys[j].pts)
        elif j < len(keys) or size is not None:
            end = keys[j].offset if j < len(keys) else int(size or 0)
            last = max(pts_delta(first.pts, k.end) for k in keys[i:j])
            ticks = last + frame
        else:
            break  # the piece still written
        if end - first.offset > MAX_PIECE_BYTES:
            broken = True
        else:
            pieces.append(
                Piece(first.offset, end, max(ticks, frame) / _CLOCK, first.jump or broken)
            )
            broken = False
        i = j
    return pieces, i, broken


def playlist(pieces: list[Piece], *, event: bool, ended: bool, start_s: float) -> str:
    longest = max((math.ceil(p.seconds) for p in pieces), default=1)
    target = max(longest, EVENT_TARGET_S) if event else longest
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{target}",
        "#EXT-X-MEDIA-SEQUENCE:0",
        f"#EXT-X-PLAYLIST-TYPE:{'EVENT' if event else 'VOD'}",
    ]
    if event or start_s > 0:  # a growing playlist plays from its start, not its end
        lines.append(f"#EXT-X-START:TIME-OFFSET={start_s:.3f},PRECISE=YES")
    for n, piece in enumerate(pieces):
        if piece.jump and n:
            lines.append("#EXT-X-DISCONTINUITY")
        lines += [f"#EXTINF:{piece.seconds:.3f},", f"{n}.ts"]
    if ended:
        lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def read_piece(path: str, piece: Piece, tables: Tables, counter: int) -> bytes:
    """A piece as the phone gets it: the tables, then the kept packets of its bytes."""
    with open(path, "rb") as file:
        file.seek(piece.start)
        data = file.read(piece.end - piece.start)
    keeps = tables.keeps()
    view = memoryview(data)
    out: list[bytes | memoryview] = [tables.header(counter & 0x0F)]
    for off in range(0, len(data) - PACKET + 1, PACKET):
        if data[off] == 0x47 and ((data[off + 1] & 0x1F) << 8 | data[off + 2]) in keeps:
            out.append(view[off : off + PACKET])
    return b"".join(out)


@dataclass
class Recorded:
    """A recording a phone plays: its file, its pieces and its playlist."""

    path: str
    start_s: float = 0.0  # where the phone starts (where it stopped before)
    # Whether a recording still writes the file, as the TV knows (None: its file's time says).
    writing: Optional[Callable[[], bool]] = None
    _tables: Optional[Tables] = field(default=None, init=False)
    _scan: Optional[_Scan] = field(default=None, init=False)
    _pieces: list[Piece] = field(default_factory=list, init=False)
    _next: int = field(default=0, init=False)  # the key the next pieces start at
    _broken: bool = field(default=False, init=False)  # the last piece was left out
    _event: bool = field(default=False, init=False)
    _ended: bool = field(default=False, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _closed: threading.Event = field(default_factory=threading.Event, init=False)
    _behind: Optional[threading.Thread] = field(default=None, init=False)

    async def open(self) -> None:
        await asyncio.get_running_loop().run_in_executor(None, self._open)

    def _open(self) -> None:
        if self._closed.is_set():
            raise OSError("closed")
        with open(self.path, "rb") as file:
            head = file.read(HEAD_BYTES)
        self._tables = parse_tables(head)
        self._scan = _Scan(self._tables)
        began = time.monotonic()
        growing = self._growing()
        size = os.path.getsize(self.path)
        whole = self._read(size, lambda: time.monotonic() - began >= OPEN_PREFER_S)
        if not whole:  # a large one: on to where the phone starts, then read behind

            def far_enough() -> bool:
                read = sum(piece.seconds for piece in self._pieces)
                return time.monotonic() - began >= OPEN_WAIT_S or (
                    bool(self._pieces) and read > self.start_s
                )

            whole = self._read(size, far_enough)
        if self._closed.is_set():
            raise OSError("closed")
        if not self._scan.keys:
            raise FilmRefused(FORMAT, "no keyframe")
        if self._scan.profile is not None and self._scan.profile not in _H264_PROFILES:
            raise FilmRefused(FORMAT, f"H.264 profile {self._scan.profile}")
        self._event = growing or not whole
        if whole and not growing:
            self._end(size)
        elif not whole:
            self._behind = threading.Thread(
                target=self._read_behind, name="recording-read", daemon=True
            )
            self._behind.start()

    def _growing(self) -> bool:
        if self.writing is not None:
            return bool(self.writing())
        return time.time() - os.stat(self.path).st_mtime < GROWING_S

    def _read(self, size: int, enough: Callable[[], bool]) -> bool:
        """Read the file on to `size`, a batch at a time, listing the pieces read whole after
        each, until `enough()` or `close`; whether it got to `size`."""
        scan = self._scan
        assert scan is not None
        while not self._closed.is_set():
            with self._lock:
                with open(self.path, "rb") as file:  # none held between batches
                    done = scan.read(file, size)
                found, self._next, self._broken = plan_from(
                    scan.keys, scan.frame, None, self._next, self._broken
                )
                self._pieces = self._pieces + found  # a new list: a reader's stays whole
            if done:
                return True
            if enough():
                return False
        return False

    def _end(self, size: int) -> None:
        """Read to its end, not written any more: the last pieces, and the playlist ends."""
        scan = self._scan
        assert scan is not None
        with self._lock:
            found, self._next, self._broken = plan_from(
                scan.keys, scan.frame, size, self._next, self._broken
            )
            self._pieces = self._pieces + found
            self._ended = True

    def _read_behind(self) -> None:
        """The rest of a large recording, behind the playlist (to its end, or to where a
        recording still writes it: the phone's asks read on from there)."""
        try:
            growing = self._growing()
            size = os.path.getsize(self.path)
            if self._read(size, lambda: False) and not growing:
                self._end(size)
        except OSError:
            pass  # the file went: the phone's next piece says so

    def _update(self) -> None:
        if self._scan is None or self._ended or self._closed.is_set():
            return
        growing = self._growing()
        size = os.path.getsize(self.path)
        if self._read(size, lambda: False) and not growing:
            self._end(size)

    async def fresh_playlist(self) -> str:
        """The playlist, the file read on first while it grows (and nothing reads behind)."""
        behind = self._behind
        if self._event and not self._ended and (behind is None or not behind.is_alive()):
            await asyncio.get_running_loop().run_in_executor(None, self._update)
        return self.playlist()

    def playlist(self) -> str:
        pieces = self._pieces
        start = self.start_s
        if self._ended and start > 0:
            start = min(start, max(0.0, sum(p.seconds for p in pieces) - 1.0))
        return playlist(pieces, event=self._event, ended=self._ended, start_s=start)

    async def piece(self, index: int) -> bytes:
        pieces, tables = self._pieces, self._tables
        if tables is None or not 0 <= index < len(pieces):
            raise IndexError(index)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, read_piece, self.path, pieces[index], tables, index)

    def close(self) -> None:
        """No more reading (the shelf's films stop reading here): an opening or the reading
        behind stops at its next batch. Nothing is held between reads."""
        self._closed.set()


def add_routes(app: Any, shelf: Any) -> None:
    """The recordings' routes on the home door's aiohttp app (before its page's catch-all):
    ``/rec/<token>/index.m3u8`` and ``<n>.ts``, from the films' shelf (one per phone)."""
    from aiohttp import web

    headers = {"Cache-Control": "no-cache"}

    def recorded_of(request: web.Request) -> Recorded:
        recorded = shelf.get(request.match_info["token"])
        if not isinstance(recorded, Recorded):
            raise web.HTTPNotFound(text="404: Not Found")
        return recorded

    async def index(request: web.Request) -> web.StreamResponse:
        recorded = recorded_of(request)
        try:
            text = await recorded.fresh_playlist()
        except OSError:  # the file went (deleted, its disk gone)
            raise web.HTTPNotFound(text="404: Not Found")
        return web.Response(
            text=text, content_type="application/vnd.apple.mpegurl", headers=headers
        )

    async def piece(request: web.Request) -> web.StreamResponse:
        recorded = recorded_of(request)
        try:
            data = await recorded.piece(int(request.match_info["n"]))
        except IndexError, ValueError, OSError:
            raise web.HTTPNotFound(text="404: Not Found")
        return web.Response(body=data, content_type="video/mp2t", headers=headers)

    app.router.add_get("/rec/{token}/index.m3u8", index)
    app.router.add_get(r"/rec/{token}/{n:\d+}.ts", piece)
