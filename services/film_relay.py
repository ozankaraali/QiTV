"""A provider's film for two readers over one stream (2.9, AI captions).

AI captions read a film's sound a few seconds ahead of its picture (services.captions.film), a
second reader of the same file. Many providers allow one stream, and a closed connection can stay
counted there for minutes, so while AI captions are on a provider's film plays through this relay:
it holds the one upstream HTTP(S) connection and serves the film on two loopback addresses with
byte ranges, the picture's (the leader: the window's libVLC) and the captions' (the follower: the
captions process's libVLC).

- One upstream reader fills a cache of blocks. It reads up to `ahead` bytes past where the
  picture's libVLC reads, then holds the connection without reading (TCP backpressure). Only the
  picture gates it: a slow or held captions reader never holds the picture back. What lies up to
  `behind` bytes before the picture's read is kept, which is where the captions read: a few
  seconds past the picture, behind libVLC's own read-ahead (about 16 MiB, measured with 3.0.20).
  In all it keeps `keep` bytes and the pinned head and tail at most (80 MiB as set). With no
  reader connected for `idle_s` it reads nothing and lets the connection go.
- The film's head and tail (`pin_head`, `pin_tail`) stay once read: an MP4's moov, an MKV's
  SeekHead and Cues, which libVLC reads when it opens the film, so both readers share them.
- A read in the cache is served from it. One just past what the upstream has read waits for it.
- The picture moves the upstream: its read where nothing is cached (a seek, also back over what
  was let go) closes the connection, then opens one there (what libVLC's own seek does).
- The captions never move it, except for a short detour (`detour` bytes) to read the head or the
  tail the picture did not keep, and only while the picture has `detour_lead` bytes cached ahead
  and is not waiting; the picture's read resumes right after. A detour that fails fails only that
  captions read, and its range is not asked again. A captions read the upstream is not heading for
  fails after `follower_wait_s`: the captions reader ends, and AI captions start again near the
  picture.
- Never two upstream connections: one thread makes them, one at a time, and closes each before it
  makes the next. Redirects are followed (an Xtream VOD's 302 to a tokenized address); the address
  they end at is asked again while it answers, and the provider's own after a 401, 403, 404 or
  410. Cookies a response sets go with the next requests (libVLC's http-forward-cookies). The
  requests carry the user agent and referrer the picture's play sends, worded as libVLC words them.
- A failed connection, or an answer that brings no byte, is tried again a few times with a wait
  that doubles, then the readers are told; a new ask of the picture tries again (libVLC's own
  reconnect). A 416 past the film's end teaches its real length. Every wait of the picture's
  reader is bounded.
- Until the picture has its film, anything that goes wrong (no answer, a refusal, a certificate
  not trusted, a playlist or manifest, a file without byte ranges or a length) leaves the film to
  libVLC: the upstream connection closed first, the picture is redirected to the provider's address
  (libVLC then plays it as it would without captions), and the captions are refused (`declined`).

Every socket is non-blocking and waits in slices (`_SLICE`, through poll() where the system has
it: no descriptor limit), so `stop()` never blocks and no thread touches another's socket;
`closed()` says when the upstream connection is closed (the budget slot goes back then:
PlaybackSession.holds_input). No Qt: plain threads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import errno
import logging
import secrets
import select
import socket
import ssl
import threading
import time
from typing import Optional
from urllib.parse import urljoin, urlsplit

logger = logging.getLogger(__name__)

LEADER, FOLLOWER = "leader", "follower"
_SLICE = 0.1  # every socket wait and every reader's wait, at most this long at once
_CHUNK = 64 * 1024
_MAX_HEAD = 64 * 1024  # an HTTP head (the request's, the provider's answer's) no longer than this
_REDIRECTS = 5
_EXPIRED = (401, 403, 404, 410)  # the address a redirect ended at answers so: ask the provider's
_REDIRECT = (301, 302, 303, 307, 308)
_IN_PROGRESS = {errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY, 10035}  # 10035: Winsock's
_CLOSING_S = 2.0  # how long a redirect to the provider waits for the upstream socket to close
# The containers a provider's film comes in: the relay's addresses end as the provider's does, so
# libVLC picks its demuxer as it would.
_EXTENSIONS = (".mkv", ".mp4", ".m4v", ".mov", ".avi", ".webm", ".ts", ".mpg", ".mpeg", ".wmv")
_PLAYLISTS = (".m3u8", ".m3u", ".mpd")
_MANIFEST_TYPES = ("mpegurl", "dash+xml")


@dataclass(frozen=True)
class Limits:
    """How much the relay keeps and how long its readers wait (the tests make them small)."""

    block: int = 256 * 1024  # the cache's unit
    ahead: int = 24 * 1024 * 1024  # the upstream reads no further past the picture's read
    behind: int = 20 * 1024 * 1024  # kept before the picture's read (where the captions read)
    keep: int = 56 * 1024 * 1024  # unpinned bytes kept at most (the oldest used go first)
    # Kept once read: the head and the tail (an MP4's moov at the end: some MiB for a long film).
    pin_head: int = 8 * 1024 * 1024
    pin_tail: int = 16 * 1024 * 1024
    near: int = 2 * 1024 * 1024  # a picture's read this far past the upstream waits for it
    detour: int = 2 * 1024 * 1024  # a captions detour reads this much at most
    detour_lead: int = 2 * 1024 * 1024  # cached past the picture's read for a detour to go
    detour_calm_s: float = 1.0  # and the picture's read has not waited for this long
    open_wait_s: float = 25.0  # the provider's first answer (then libVLC asks it itself)
    leader_wait_s: float = 30.0  # the picture's read waits no longer without the upstream moving
    follower_wait_s: float = 15.0  # a captions read the upstream is not heading for
    idle_s: float = 10.0  # no reader connected this long: the upstream reads nothing
    connect_s: float = 10.0
    head_s: float = 15.0  # the request out and the answer's head in
    read_s: float = 20.0  # no byte of the answer for this long: the connection failed
    retries: int = 3  # failed connections in a row before the readers are told
    retry_s: float = 0.5  # the first wait before trying again (it doubles each time)


def relayable(url: str) -> bool:
    """`url` can play through the relay: an HTTP(S) file, no playlist or manifest, and no user
    and password in it (libVLC would answer the provider's sign-in question itself)."""
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return False
    if parts.username is not None or parts.password is not None:
        return False
    path, query = parts.path.lower(), parts.query.lower()
    return not path.endswith(_PLAYLISTS) and "m3u8" not in query


def _manifest(data: bytes) -> bool:
    """The first bytes of an HLS playlist or a DASH manifest, whatever the address said."""
    head = data[:1024].lstrip(b"\xef\xbb\xbf \t\r\n")
    if head.startswith(b"#EXTM3U"):
        return True
    return head.startswith((b"<?xml", b"<MPD")) and b"<MPD" in data[:4096]


def _ready(sock: socket.socket, readable: bool, timeout: float) -> bool:
    """`sock` can be read (or written; a failed connect counts) within `timeout`. poll() where
    the system has it (select() refuses a descriptor past FD_SETSIZE), select() on Windows."""
    if hasattr(select, "poll"):
        poller = select.poll()
        poller.register(sock, select.POLLIN | select.POLLPRI if readable else select.POLLOUT)
        return bool(poller.poll(max(0, int(timeout * 1000))))
    if readable:
        return bool(select.select([sock], [], [], timeout)[0])
    _, writable, failed = select.select([], [sock], [sock], timeout)
    return bool(writable or failed)


class _Stopped(Exception):
    """The relay stops (or leaves the film to libVLC): the upstream lets go."""


class _Retry(Exception):
    """The connection failed or the provider answered 5xx: try again."""


class _Refused(Exception):
    """The provider refused the film (a 4xx, a certificate not trusted)."""

    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


class _Decline(Exception):
    """An answer the relay cannot carry: libVLC plays the provider's address itself."""


@dataclass
class _Span:
    """A run of the film the upstream reads in order: from `start`, now at `pos`, to `end`."""

    start: int
    pos: int
    end: Optional[int] = None  # exclusive; None: to the file's end
    detour: bool = False

    def done(self, size: Optional[int]) -> bool:
        end = self.end if self.end is not None else size
        return end is not None and self.pos >= end


@dataclass
class _Head:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    cookies: list[str] = field(default_factory=list)


class _Wire:
    """One non-blocking socket (TLS or not), written and read in slices."""

    def __init__(self, sock: socket.socket, halt: threading.Event) -> None:
        self.sock = sock
        self._halt = halt
        self._pending = b""

    def wait(self, readable: bool, deadline: float) -> None:
        while True:
            if self._halt.is_set():
                raise _Stopped()
            left = deadline - time.monotonic()
            if left <= 0:
                raise _Retry("timed out")
            try:
                if _ready(self.sock, readable, min(_SLICE, left)):
                    return
            except (OSError, ValueError) as error:
                raise _Retry(type(error).__name__) from None

    def send_all(self, data: bytes, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        view = memoryview(data)
        while view:
            if self._halt.is_set():
                raise _Stopped()
            try:
                sent = self.sock.send(view)
            except BlockingIOError, ssl.SSLWantWriteError:
                self.wait(False, deadline)
                continue
            except ssl.SSLWantReadError:
                self.wait(True, deadline)
                continue
            except OSError as error:
                raise _Retry(type(error).__name__) from None
            view = view[sent:]

    def recv(self, size: int, timeout: float) -> bytes:
        """Up to `size` bytes; b"" at the end of the answer."""
        if self._pending:
            data, self._pending = self._pending[:size], self._pending[size:]
            return data
        deadline = time.monotonic() + timeout
        while True:
            if self._halt.is_set():
                raise _Stopped()
            try:
                return self.sock.recv(size)  # a TLS socket's decrypted bytes first
            except BlockingIOError, ssl.SSLWantReadError:
                self.wait(True, deadline)
            except ssl.SSLWantWriteError:
                self.wait(False, deadline)
            except ssl.SSLZeroReturnError:
                return b""  # the provider closed TLS: the answer's end
            except OSError as error:
                raise _Retry(type(error).__name__) from None

    def unread(self, data: bytes) -> None:
        self._pending = data + self._pending

    def line(self, timeout: float) -> bytes:
        """One line without its CRLF (a chunk's size)."""
        found = b""
        while b"\r\n" not in found:
            if len(found) > _MAX_HEAD:
                raise _Retry("a line too long")
            data = self.recv(4096, timeout)
            if not data:
                raise _Retry("the answer ended in a line")
            found += data
        line, rest = found.split(b"\r\n", 1)
        self.unread(rest)
        return line

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class _Body:
    """An answer's body as its head frames it: a length, chunks, or until the connection ends."""

    def __init__(self, wire: _Wire, head: _Head, read_s: float) -> None:
        self._wire = wire
        self._read_s = read_s
        self._chunked = "chunked" in head.headers.get("transfer-encoding", "").lower()
        length = head.headers.get("content-length", "")
        self._left: Optional[int] = None
        if not self._chunked and length.strip().isdigit():
            self._left = int(length)
        self._chunk = 0
        self.finished = False

    def read(self, size: int) -> bytes:
        """Up to `size` bytes; b"" once the body has ended as framed. An end before that is a
        failed connection (_Retry)."""
        if self.finished:
            return b""
        if self._chunked:
            return self._read_chunked(size)
        if self._left is not None:
            if self._left <= 0:
                self.finished = True
                return b""
            size = min(size, self._left)
        data = self._wire.recv(size, self._read_s)
        if not data:
            if self._left is not None:
                raise _Retry("the answer ended early")
            self.finished = True
            return b""
        if self._left is not None:
            self._left -= len(data)
        return data

    def _read_chunked(self, size: int) -> bytes:
        if self._chunk == 0:
            line = self._wire.line(self._read_s)
            try:
                self._chunk = int(line.split(b";", 1)[0].strip() or b"0", 16)
            except ValueError:
                raise _Retry("a chunk's size is no number") from None
            if self._chunk == 0:
                while self._wire.line(self._read_s):
                    pass  # trailers
                self.finished = True
                return b""
        data = self._wire.recv(min(size, self._chunk), self._read_s)
        if not data:
            raise _Retry("the answer ended in a chunk")
        self._chunk -= len(data)
        if self._chunk == 0:
            self._wire.line(self._read_s)  # the chunk's CRLF
        return data


@dataclass
class _Reading:
    """The upstream connection open now, the span it reads into, and whether it brought bytes."""

    wire: _Wire
    body: _Body
    span: _Span
    advanced: bool = False


def _content_range(value: str) -> tuple[Optional[int], Optional[int], Optional[int]]:
    """ "bytes 10-99/1000": (10, 100, 1000); "bytes */1000": (None, None, 1000)."""
    value = value.strip().lower()
    if not value.startswith("bytes"):
        return None, None, None
    span, _, total = value[5:].strip().partition("/")
    size = int(total) if total.strip().isdigit() else None
    first, _, last = span.partition("-")
    if first.strip().isdigit() and last.strip().isdigit():
        return int(first), int(last) + 1, size
    return None, None, size


def _ranges(value: Optional[str]) -> Optional[tuple[Optional[int], Optional[int]]]:
    """A request's Range: (first, last + 1 or None), or (None, a suffix's length); None: none."""
    if not value:
        return None
    value = value.strip().lower()
    if not value.startswith("bytes="):
        return None
    first, _, last = value[6:].split(",", 1)[0].strip().partition("-")
    try:
        if not first.strip():
            return None, int(last)
        return int(first), (int(last) + 1 if last.strip() else None)
    except ValueError:
        return None


class FilmRelay:
    """One provider film on two loopback addresses over one upstream connection (module doc)."""

    def __init__(
        self,
        source: str,
        *,
        user_agent: str,
        referrer: str = "",
        verify_ssl: bool = True,
        ssl_context: Optional[ssl.SSLContext] = None,
        limits: Limits = Limits(),
    ) -> None:
        self.source = source
        self._user_agent = user_agent
        self._referrer = referrer
        self._verify = verify_ssl
        self._ssl_context = ssl_context
        self.limits = limits
        self._token = secrets.token_urlsafe(16)
        path = urlsplit(source).path.lower()
        self._extension = next((e for e in _EXTENSIONS if path.endswith(e)), "")
        self.leader_url = ""
        self.follower_url = ""
        self._paths: dict[str, str] = {}
        self._listener: Optional[socket.socket] = None
        self._cond = threading.Condition()
        self._stopping = threading.Event()  # stop(): everything ends
        self._halt = threading.Event()  # the upstream lets go (stopped, or left to libVLC)
        self._closed = threading.Event()
        self._sock_open = False  # an upstream socket exists (from its connect to its close)
        self._wire: Optional[_Wire] = None  # that socket's wire
        # What the provider said (under the lock):
        self._size: Optional[int] = None  # known once the picture's first answer is taken
        self._type = "application/octet-stream"
        self._final: Optional[str] = None  # where a redirect ended (asked again while it answers)
        self._cookies: dict[str, str] = {}
        self._declined = False  # the film is libVLC's own: the picture is redirected to it
        self._failed = False  # the tries ran out; the picture's next ask tries again
        self._failures = 0
        self._broken = False  # the upstream reader ended on a fault: every ask fails from here
        # The cache and the readers:
        self._blocks: dict[int, bytearray] = {}
        self._used: dict[int, float] = {}
        self._unpinned = 0  # bytes in unpinned blocks
        self._armed = False  # the picture asked: the upstream may open (never before it)
        self._leader_pos = 0
        self._leader_waits = 0.0  # when the picture's read last waited for the upstream
        self._newest = {LEADER: 0, FOLLOWER: 0}
        self._asks = 0
        self._serving = 0  # readers' connections being served now
        self._idle_since = time.monotonic()  # since none is
        self._lead: Optional[_Span] = None  # the picture's run
        self._detour: Optional[_Span] = None
        self._dead: list[tuple[int, int]] = []  # detour ranges that failed: not asked again
        self._reading: Optional[_Span] = None
        self._progress_at = time.monotonic()
        # Counted for the log and the tests: upstream connections made (redirects' too), the
        # picture's moves of the upstream, captions detours, bytes read, failed answers.
        self.stats = {"connections": 0, "moves": 0, "detours": 0, "bytes": 0, "failures": 0}

    # ---- its life (the GUI thread) ------------------------------------------------------------
    def start(self) -> None:
        """Listen on the loopback and start the upstream reader (it opens once the picture asks)."""
        listener = socket.create_server(("127.0.0.1", 0))
        listener.setblocking(False)
        self._listener = listener
        port = listener.getsockname()[1]
        for role in (LEADER, FOLLOWER):
            path = f"/{self._token}/{role}{self._extension}"
            self._paths[path] = role
            url = f"http://127.0.0.1:{port}{path}"
            if role == LEADER:
                self.leader_url = url
            else:
                self.follower_url = url
        threading.Thread(target=self._accept, name="qitv-film-relay", daemon=True).start()
        threading.Thread(target=self._upstream, name="qitv-film-relay-up", daemon=True).start()

    def stop(self) -> None:
        """Stop serving and close the upstream connection; never blocks (`closed()` says when)."""
        with self._cond:
            if self._stopping.is_set():
                return
            self._stopping.set()
            self._halt.set()
            if not self._sock_open:
                self._closed.set()
            self._cond.notify_all()

    def closed(self) -> bool:
        """Stopped, and no upstream connection is open."""
        return self._closed.is_set()

    def wait_closed(self, timeout: float) -> bool:
        return self._closed.wait(timeout)

    def declined(self) -> bool:
        """The film is left to libVLC (the relay could not carry it): the picture plays it
        itself, and the captions can't read it. (Read without the lock: the GUI asks often.)"""
        return self._declined

    def followable(self) -> bool:
        """The captions may read the film here."""
        return not (self._declined or self._stopping.is_set() or self._broken)

    # ---- the cache (under the lock) -----------------------------------------------------------
    def _pinned(self, index: int) -> bool:
        block = self.limits.block
        if index * block < self.limits.pin_head:
            return True
        size = self._size
        return size is not None and (index + 1) * block > size - self.limits.pin_tail

    def _cached(self, pos: int, size: int) -> bytes:
        index, offset = divmod(pos, self.limits.block)
        found = self._blocks.get(index)
        if found is None or offset >= len(found):
            return b""
        self._used[index] = time.monotonic()
        return bytes(found[offset : min(len(found), offset + size)])

    def _store(self, pos: int, data: memoryview) -> None:
        block = self.limits.block
        now = time.monotonic()
        while data:
            index, offset = divmod(pos, block)
            take = min(len(data), block - offset)
            found = self._blocks.get(index)
            if found is None and offset == 0:
                found = self._blocks[index] = bytearray()
            if found is not None and offset <= len(found):
                before = len(found)
                found[offset : offset + take] = data[:take]
                if not self._pinned(index):
                    self._unpinned += len(found) - before
                self._used[index] = now
            # else a block gone from under the run (or one the run began past): left missing
            pos += take
            data = data[take:]

    def _first_missing(self, pos: int, stop: Optional[int] = None) -> int:
        """The first byte at or after `pos` not cached (a block's start, or a partial one's end);
        `stop` when everything up to it is."""
        block = self.limits.block
        if stop is not None:
            steps = max(0, stop - pos) // block + 2
        else:
            steps = (self.limits.ahead + self.limits.behind) // block + 2
        for _ in range(steps):
            if stop is not None and pos >= stop:
                return stop
            if self._size is not None and pos >= self._size:
                return self._size
            index = pos // block
            found = self._blocks.get(index)
            if found is None:
                return index * block
            end = index * block + len(found)
            if pos < end:
                pos = end
            if len(found) < block:
                return end
            pos = max(pos, (index + 1) * block)
        return pos

    def _evict(self) -> None:
        limits = self.limits
        if self._unpinned <= limits.keep:
            return
        low = self._leader_pos - limits.behind
        high = self._leader_pos + limits.ahead + limits.near
        reading = self._reading.pos // limits.block if self._reading is not None else -1
        candidates = [
            index
            for index in self._blocks
            if not self._pinned(index)
            and index != reading
            and not low <= index * limits.block < high
        ]
        candidates.sort(key=lambda index: self._used.get(index, 0.0))
        for index in candidates:
            if self._unpinned <= limits.keep * 9 // 10:
                break
            self._unpinned -= len(self._blocks.pop(index))
            self._used.pop(index, None)

    # ---- where the upstream reads (under the lock) ------------------------------------------
    def _frontier(self) -> int:
        """Where the picture's run reads next: the lead span's place while it covers the
        picture's read with everything between them cached, else the first byte past the
        picture's read not cached (where a new run opens: a seek, also one back over what was
        let go)."""
        lead, at = self._lead, self._leader_pos
        if lead is not None and lead.start <= at <= lead.pos + self.limits.near:
            missing = self._first_missing(at, stop=lead.pos)
            return lead.pos if missing >= lead.pos else missing
        return self._first_missing(at)

    def _idle(self) -> bool:
        """No reader has been connected for `idle_s`: nobody needs the upstream's bytes."""
        return self._serving == 0 and time.monotonic() - self._idle_since >= self.limits.idle_s

    def _want(self) -> Optional[_Span]:
        """The span the upstream should read now; None: nothing (yet, or no more)."""
        if not self._armed or self._failed or self._declined or self._halt.is_set():
            return None
        if self._idle():
            return None
        size = self._size
        detour = self._detour
        if detour is not None:
            if detour.done(size):
                self._detour = None
                self._cond.notify_all()
            elif self._reading is detour or self._detour_allowed():
                return detour
        lead = self._lead
        frontier = self._frontier()
        if lead is None or frontier != lead.pos:
            if lead is not None:
                self.stats["moves"] += 1
            lead = self._lead = _Span(frontier, frontier)
        if size is not None and lead.pos >= size:
            return None  # read to the end
        if lead.pos - self._leader_pos >= self.limits.ahead:
            return None  # far enough past the picture
        return lead

    def _holding(self, span: _Span) -> bool:
        """The upstream keeps `span`'s connection without reading (far enough ahead)."""
        if span is not self._lead or self._failed or self._halt.is_set() or self._idle():
            return False
        if self._size is not None and span.pos >= self._size:
            return False
        return span.pos - self._leader_pos >= self.limits.ahead

    def _detour_allowed(self) -> bool:
        calm = time.monotonic() - self._leader_waits >= self.limits.detour_calm_s
        if self._size is None or not calm:
            return False  # the picture's read waits for the upstream now
        frontier = self._frontier()
        return frontier >= self._size or frontier - self._leader_pos >= self.limits.detour_lead

    def _reachable(self, pos: int) -> bool:
        """The picture's run is heading for `pos` (a captions read past it waits for it)."""
        if not self._armed or self._size is None:
            return True  # the first answer has not come: wait for it
        return self._frontier() <= pos <= self._leader_pos + 2 * self.limits.ahead

    def _metadata(self, pos: int) -> bool:
        size = self._size
        return pos < self.limits.pin_head or (
            size is not None and pos >= size - self.limits.pin_tail
        )

    def _dead_at(self, pos: int) -> bool:
        return any(start <= pos < end for start, end in self._dead)

    def _ask_detour(self, pos: int) -> None:
        if self._detour is not None or self._size is None or self._dead_at(pos):
            return
        limits = self.limits
        start = pos - pos % limits.block
        end = min(self._size, start + limits.detour)
        if self._size - start <= limits.detour * 3 // 2:
            end = self._size  # the rest of the tail at once
        self._detour = _Span(start, start, end, detour=True)
        self.stats["detours"] += 1
        self._cond.notify_all()

    # ---- the upstream (its thread) --------------------------------------------------------------
    def _upstream(self) -> None:
        current: Optional[_Reading] = None
        try:
            while True:
                moves = self.stats["moves"]
                with self._cond:
                    while True:
                        if self._halt.is_set():
                            raise _Stopped()
                        span = self._want()
                        if span is not None:
                            break
                        if current is not None and not self._holding(current.span):
                            break  # read to its end, or no longer wanted: closed below
                        self._cond.wait(_SLICE)
                    self._reading = span
                if self.stats["moves"] != moves and span is not None:
                    logger.debug("Film relay: the picture moved the upstream to %d", span.pos)
                if current is not None and current.span is not span:
                    self._drop(current.wire)
                    current = None
                if span is None:
                    continue
                if current is None:
                    current = self._open_for(span)
                    continue  # (the first answer's first bytes are in; ask `_want` again)
                try:
                    more = self._read_into(current)
                except _Retry as error:
                    self._drop(current.wire)  # closed before the wait to try again
                    current = None
                    self._failed_span(span, str(error))
                    continue
                if not more:
                    advanced = current.advanced
                    self._drop(current.wire)
                    current = None
                    if not advanced:
                        self._failed_span(span, "an answer without a byte")
        except _Stopped:
            pass
        except Exception as error:  # a fault: every reader is told, and no ask tries again
            logger.warning("Film relay: the upstream reader failed (%s)", type(error).__name__)
            with self._cond:
                self._broken = self._failed = True
                if self._size is None:  # the picture has nothing yet: libVLC asks itself
                    self._declined = True
                self._cond.notify_all()
        finally:
            wire = self._wire
            if wire is not None:  # every way out closes the connection (and says so)
                self._drop(wire)
            logger.debug("Film relay: closed (%s)", self.stats)

    def _open_for(self, span: _Span) -> Optional[_Reading]:
        """The connection for `span` (its place), or None: the readers were told why, or a try
        failed (the next `_want` decides again). The picture's first answer is checked by its
        first bytes before the readers hear the film's length."""
        first = self._size is None
        try:
            wire, head, size, kind = self._ask(span.pos, span.end, first)
        except _Retry as error:
            self._failed_span(span, str(error))
            return None
        except (_Refused, _Decline) as error:
            why = (
                f"the provider answered {error.status}"
                if isinstance(error, _Refused)
                else str(error)
            )
            if first:
                self._decline(why)
            elif span.detour:
                self._failed_span(span, why)
            else:
                with self._cond:
                    self._failed = True
                    self._cond.notify_all()
                logger.info("Film relay: %s", why)
            return None
        if head.status == 416:
            self._drop(wire)
            self._ended_at(span, size, first)
            return None
        reading = _Reading(wire, _Body(wire, head, self.limits.read_s), span)
        if not first:
            return reading
        try:
            data = reading.body.read(_CHUNK)
        except _Retry as error:
            self._drop(wire)
            self._failed_span(span, str(error))
            return None
        if span.pos == 0 and _manifest(data):
            self._drop(wire)
            self._decline("a streaming manifest")
            return None
        if not data:
            self._drop(wire)
            self._failed_span(span, "an answer without a byte")
            return None
        with self._cond:
            self._size = size
            if kind:
                self._type = kind
            self._take_bytes(reading, data)
        return reading

    def _read_into(self, current: _Reading) -> bool:
        """One chunk into the cache; False: the answer has ended. Raises _Retry when the
        connection failed (closed by the caller before it waits to try again)."""
        data = current.body.read(_CHUNK)
        with self._cond:
            if not data:  # the answer ended as framed: the span's end, the file's, or a range
                self._cond.notify_all()  # the provider capped (the next ask goes on from there)
                return False
            self._take_bytes(current, data)
        return True

    def _take_bytes(self, current: _Reading, data: bytes) -> None:
        """(Under the lock.) Bytes the connection brought, into the cache."""
        span = current.span
        self._store(span.pos, memoryview(data))
        span.pos += len(data)
        current.advanced = True
        self.stats["bytes"] += len(data)
        self._progress_at = time.monotonic()
        self._failures = 0
        self._evict()
        self._cond.notify_all()

    def _ended_at(self, span: _Span, total: Optional[int], first: bool) -> None:
        """A 416 for `span`: nothing there. `total` (its Content-Range: bytes */N) is the film's
        real length when it ends at or before `span`; else the answer brought nothing."""
        if first:
            self._decline("nothing at the film's start")
            return
        if span.detour:
            self._failed_span(span, "a range past the film's end")
            return
        with self._cond:
            known = self._size
            learned = total is not None and total <= span.pos and (known is None or total <= known)
            if learned and total is not None:
                self._size = total  # the readers past it get the film's end
                span.end = span.pos
                self._unpinned = sum(len(b) for i, b in self._blocks.items() if not self._pinned(i))
                self._cond.notify_all()
        if learned:
            logger.info("Film relay: the film ends at %s, not at %s", total, known)
            return
        self._failed_span(span, "a range past the film's end")

    def _failed_span(self, span: _Span, why: str) -> None:
        """A connection for `span` failed or brought nothing. A detour fails its captions read
        alone (its range is not asked again); the picture's run is tried again after a wait
        that doubles, and after `retries` the readers are told (before the picture has its
        film, libVLC is given it instead)."""
        self.stats["failures"] += 1
        if span.detour:
            with self._cond:
                self._dead.append((span.start, span.end if span.end is not None else span.pos))
                if self._detour is span:
                    self._detour = None
                self._cond.notify_all()
            logger.info("Film relay: a detour for the captions failed (%s)", why)
            return
        with self._cond:
            self._failures += 1
            failures = self._failures
            given = self._size is not None
            if failures > self.limits.retries and given:
                self._failed = True
                self._cond.notify_all()
        if failures > self.limits.retries:
            if given:
                logger.warning("Film relay: the provider's film did not come (%s)", why)
            else:
                self._decline(f"the provider's film did not come ({why})")
            return
        logger.info("Film relay: the upstream connection failed (%s); trying again", why)
        self._pause(self.limits.retry_s * 2 ** (failures - 1))

    def _decline(self, why: str) -> None:
        """Leave the film to libVLC: the upstream lets go, the picture's ask is redirected to the
        provider's address once its connection is closed, and the captions are refused."""
        with self._cond:
            if self._declined:
                return
            self._declined = True
            self._halt.set()
            self._cond.notify_all()
        logger.info("Film relay: %s; the picture plays the film itself", why)

    def _pause(self, seconds: float) -> None:
        if self._halt.wait(seconds):
            raise _Stopped()

    # ---- HTTP to the provider (the upstream's thread) ---------------------------------------
    def _ask(
        self, start: int, end: Optional[int], first: bool
    ) -> tuple[_Wire, _Head, Optional[int], str]:
        """The provider's answer for bytes from `start` (to `end`), its head read, and what it
        says of the film (its length, its type); redirects followed; the address they ended at
        kept while it answers."""
        final = self._final
        try:
            wire, head, landed = self._exchange(final or self.source, start, end)
        except _Retry:
            if final is None:
                raise
            self._final = None  # the provider's own address from here
            wire, head, landed = self._exchange(self.source, start, end)
        if final is not None and head.status in _EXPIRED:
            self._drop(wire)  # the tokenized address expired: from the provider's again
            self._final = None
            wire, head, landed = self._exchange(self.source, start, end)
        try:
            size, kind = self._check(head, start, first)
        except BaseException:
            self._drop(wire)
            raise
        self._final = landed if landed != self.source else None
        return wire, head, size, kind

    @staticmethod
    def _check(head: _Head, start: int, first: bool) -> tuple[Optional[int], str]:
        """What the answer says of the film (its length, its type); raises for one not to read."""
        status = head.status
        if status >= 500:
            raise _Retry(f"the provider answered {status}")
        kind = head.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if status == 416:
            return _content_range(head.headers.get("content-range", ""))[2], kind
        if status not in (200, 206):
            raise _Refused(status)
        if first and any(t in kind for t in _MANIFEST_TYPES):
            raise _Decline("a streaming manifest")
        if status == 206:
            begins, _, size = _content_range(head.headers.get("content-range", ""))
            if begins != start:
                raise _Retry("the provider answered another range")
        else:  # the whole file: from the start only, and the first time with ranges said
            length = head.headers.get("content-length", "")
            size = int(length) if length.strip().isdigit() else None
            ranges = "bytes" in head.headers.get("accept-ranges", "").lower()
            if start > 0 or (first and not ranges):
                if first:
                    raise _Decline("no byte ranges")
                raise _Retry("the provider ignored the range")
        if first and size is None:
            raise _Decline("no length")
        return size, kind

    def _exchange(self, url: str, start: int, end: Optional[int]) -> tuple[_Wire, _Head, str]:
        for _ in range(_REDIRECTS + 1):
            wire = self._connect(url)
            try:
                wire.send_all(self._request(url, start, end), self.limits.head_s)
                head = self._read_head(wire)
            except BaseException:
                self._drop(wire)
                raise
            self._keep_cookies(head)
            location = head.headers.get("location", "")
            if head.status in _REDIRECT and location:
                self._drop(wire)
                url = urljoin(url, location.strip())
                continue
            return wire, head, url
        raise _Retry("too many redirects")

    def _request(self, url: str, start: int, end: Optional[int]) -> bytes:
        parts = urlsplit(url)
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        host = parts.netloc.rsplit("@", 1)[-1]
        last = str(end - 1) if end is not None else ""
        lines = [
            f"GET {target} HTTP/1.1",
            f"Host: {host}",
            "Accept: */*",
            "Accept-Language: en_US",
            f"User-Agent: {self._user_agent}",
            f"Range: bytes={start}-{last}",
        ]
        if self._referrer:
            lines.append(f"Referer: {self._referrer}")
        if self._cookies:
            lines.append("Cookie: " + "; ".join(f"{k}={v}" for k, v in self._cookies.items()))
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1", "replace")

    def _read_head(self, wire: _Wire) -> _Head:
        found = b""
        deadline = time.monotonic() + self.limits.head_s
        while b"\r\n\r\n" not in found:
            if len(found) > _MAX_HEAD:
                raise _Retry("the answer's head is too long")
            data = wire.recv(16384, max(0.01, deadline - time.monotonic()))
            if not data:
                raise _Retry("the connection closed before an answer")
            found += data
        raw, rest = found.split(b"\r\n\r\n", 1)
        wire.unread(rest)
        lines = raw.decode("latin-1").split("\r\n")
        try:
            status = int(lines[0].split()[1])
        except IndexError, ValueError:
            raise _Retry("no HTTP answer") from None
        head = _Head(status)
        for line in lines[1:]:
            name, _, value = line.partition(":")
            name = name.strip().lower()
            if name == "set-cookie":
                head.cookies.append(value.strip())
            elif name:
                head.headers[name] = value.strip()
        return head

    def _keep_cookies(self, head: _Head) -> None:
        for cookie in head.cookies:
            pair = cookie.split(";", 1)[0]
            name, sep, value = pair.partition("=")
            if sep and name.strip():
                self._cookies[name.strip()] = value.strip()

    def _connect(self, url: str) -> _Wire:
        parts = urlsplit(url)
        https = parts.scheme.lower() == "https"
        host = parts.hostname or ""
        port = parts.port or (443 if https else 80)
        try:
            found = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)  # no connection yet
        except OSError as unresolved:
            raise _Retry(f"the address did not resolve ({type(unresolved).__name__})") from None
        last: Optional[BaseException] = None
        for family, kind, proto, _, address in found:
            with self._cond:
                if self._halt.is_set():
                    raise _Stopped()
                if self._sock_open:  # never two: a bug's, refused before the provider sees it
                    raise RuntimeError("a second upstream connection")
                sock = socket.socket(family, kind, proto)
                wire = self._wire = _Wire(sock, self._halt)
                self._sock_open = True
                self.stats["connections"] += 1
            try:
                self._connect_socket(wire, address)
                if https:
                    wire.sock = self._tls(wire, host)
                return wire
            except _Stopped:
                self._drop(wire)
                raise
            except ssl.SSLCertVerificationError:
                self._drop(wire)
                logger.warning("Film relay: the provider's certificate could not be verified")
                raise _Refused(502) from None
            except Exception as failed:  # OSError, a TLS error, select's ValueError: closed
                self._drop(wire)
                last = failed
        raise _Retry(f"no connection ({type(last).__name__})")

    def _connect_socket(self, wire: _Wire, address: tuple) -> None:
        sock = wire.sock
        sock.setblocking(False)
        code = sock.connect_ex(address)
        if code == 0:
            return
        if code not in _IN_PROGRESS:
            raise OSError(code, "connect failed")
        deadline = time.monotonic() + self.limits.connect_s
        while True:
            if self._halt.is_set():
                raise _Stopped()
            left = deadline - time.monotonic()
            if left <= 0:
                raise _Retry("the connection timed out")
            if _ready(sock, False, min(_SLICE, left)):
                code = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                if code:
                    raise OSError(code, "connect failed")
                return

    def _tls(self, wire: _Wire, host: str) -> socket.socket:
        context = self._ssl_context
        if context is None:
            if self._verify:
                import certifi

                context = ssl.create_default_context(cafile=certifi.where())
            else:
                context = ssl.create_default_context()
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
        tls = context.wrap_socket(wire.sock, server_hostname=host, do_handshake_on_connect=False)
        wire.sock = tls  # closed with the wire from here
        deadline = time.monotonic() + self.limits.connect_s
        while True:
            try:
                tls.do_handshake()
                return tls
            except ssl.SSLWantReadError:
                wire.wait(True, deadline)
            except ssl.SSLWantWriteError:
                wire.wait(False, deadline)

    def _drop(self, wire: _Wire) -> None:
        """Close an upstream connection; the relay's close is told once none is left."""
        wire.close()
        with self._cond:
            if self._wire is wire:
                self._wire = None
            self._sock_open = self._wire is not None
            if self._stopping.is_set() and not self._sock_open:
                self._closed.set()
            self._cond.notify_all()

    # ---- the loopback side (a thread per libVLC connection) -------------------------------------
    def _accept(self) -> None:
        listener = self._listener
        assert listener is not None
        try:
            while not self._stopping.is_set():
                if not _ready(listener, True, _SLICE):
                    continue
                try:
                    client, _ = listener.accept()
                except BlockingIOError, InterruptedError:
                    continue
                threading.Thread(
                    target=self._client, args=(client,), name="qitv-film-relay-out", daemon=True
                ).start()
        except (OSError, ValueError) as error:
            logger.debug("Film relay: listening ended (%s)", type(error).__name__)
        finally:
            listener.close()

    def _client(self, client: socket.socket) -> None:
        client.setblocking(False)
        try:
            request = self._read_request(client)
            if request is None:
                return
            method, path, range_header = request
            role = self._paths.get(path.split("?", 1)[0])
            if role is None or method not in ("GET", "HEAD"):
                self._reply(client, 404, [])
                return
            with self._cond:
                self._serving += 1
            try:
                self._serve(client, role, method == "HEAD", range_header)
            finally:
                with self._cond:
                    self._serving -= 1
                    if self._serving == 0:
                        self._idle_since = time.monotonic()
                    self._cond.notify_all()
        except _Stopped, OSError, ValueError:
            pass
        except Exception as error:
            logger.warning("Film relay: a reader's request failed (%s)", type(error).__name__)
        finally:
            try:
                client.close()
            except OSError:
                pass

    def _read_request(self, client: socket.socket) -> Optional[tuple[str, str, Optional[str]]]:
        found = b""
        deadline = time.monotonic() + self.limits.head_s
        while b"\r\n\r\n" not in found:
            if len(found) > _MAX_HEAD or time.monotonic() > deadline or self._stopping.is_set():
                return None
            if not _ready(client, True, _SLICE):
                continue
            try:
                data = client.recv(16384)
            except BlockingIOError:
                continue
            if not data:
                return None
            found += data
        lines = found.split(b"\r\n\r\n", 1)[0].decode("latin-1").split("\r\n")
        words = lines[0].split()
        if len(words) < 2:
            return None
        range_header = None
        for line in lines[1:]:
            name, _, value = line.partition(":")
            if name.strip().lower() == "range":
                range_header = value.strip()
        return words[0].upper(), words[1], range_header

    def _serve(
        self, client: socket.socket, role: str, head_only: bool, range_header: Optional[str]
    ) -> None:
        asked = _ranges(range_header)
        with self._cond:
            self._asks += 1
            seq = self._newest[role] = self._asks
            if role == LEADER:
                self._armed = True
                if self._failed and not self._broken:  # a new ask: the upstream tries again
                    self._failed, self._failures = False, 0
                if asked is not None and asked[0] is not None:
                    self._leader_pos = asked[0]
                elif asked is None:
                    self._leader_pos = 0
                self._cond.notify_all()
        if not self._wait_answer(client, role):
            if role != LEADER or self._stopping.is_set() or self._size is not None:
                return
            self._decline("no answer in time")  # libVLC asks the provider itself
        with self._cond:
            size, kind = self._size, self._type
            declined, failed = self._declined, self._failed
        if declined:
            if role == LEADER and self._wait_upstream_closed():
                self._reply(client, 302, [("Location", self.source)])
            else:
                self._reply(client, 503, [])
            return
        if failed or size is None:
            self._reply(client, 502, [])
            return
        if asked is None:
            start, end = 0, size
        elif asked[0] is None:
            start, end = max(0, size - int(asked[1] or 0)), size
        else:
            start, end = asked[0], min(size, asked[1] if asked[1] is not None else size)
        if start >= size or start >= end:
            self._reply(client, 416, [("Content-Range", f"bytes */{size}")])
            return
        if role == LEADER:
            with self._cond:
                if self._newest[LEADER] == seq:
                    self._leader_pos = start
                    self._cond.notify_all()
        headers = [("Content-Type", kind), ("Accept-Ranges", "bytes")]
        headers.append(("Content-Length", str(end - start)))
        if asked is not None:
            headers.append(("Content-Range", f"bytes {start}-{end - 1}/{size}"))
        if not self._reply(client, 206 if asked is not None else 200, headers):
            return
        if head_only:
            return
        pos = start
        while pos < end:
            data = self._take(client, role, seq, pos, min(_CHUNK, end - pos))
            if not data or not self._send(client, data):
                return
            pos += len(data)

    def _wait_upstream_closed(self) -> bool:
        """Before libVLC asks the provider itself: the relay's connection is closed."""
        deadline = time.monotonic() + _CLOSING_S
        with self._cond:
            while self._sock_open:
                left = deadline - time.monotonic()
                if left <= 0:
                    logger.warning("Film relay: its connection did not close in time")
                    return False
                self._cond.wait(min(_SLICE, left))
        return True

    def _wait_answer(self, client: socket.socket, role: str) -> bool:
        """Wait for the provider's first answer (the picture's ask makes it); False: none came."""
        wait = self.limits.open_wait_s
        if role == FOLLOWER:
            wait += self.limits.follower_wait_s  # the picture may not have asked yet
        deadline = time.monotonic() + wait
        while True:
            with self._cond:
                if self._stopping.is_set():
                    return False
                if self._size is not None or self._declined or (self._failed and self._armed):
                    return True
                if time.monotonic() > deadline:
                    return False
                self._cond.wait(_SLICE)
            if _gone(client):
                return False

    def _take(self, client: socket.socket, role: str, seq: int, pos: int, size: int) -> bytes:
        """The next bytes for a reader at `pos`, once there; b"": none will come (the film's end,
        or the reader's connection ends: libVLC's reconnect asks again, or the captions start
        again)."""
        limits = self.limits
        waited_from: Optional[float] = None
        deadline: Optional[float] = None
        while True:
            late, detours = "", self.stats["detours"]
            with self._cond:
                if self._stopping.is_set():
                    return b""
                newest = self._newest[role] == seq
                picture = role == LEADER and newest
                if picture and pos != self._leader_pos:
                    self._leader_pos = pos
                    self._cond.notify_all()
                data = self._cached(pos, size)
                if data:
                    return data
                if self._size is not None and pos >= self._size:
                    return b""  # the film's real end (a 416 taught it)
                if self._failed or self._declined:
                    return b""
                if role == FOLLOWER and self._dead_at(pos):
                    return b""  # a detour there failed: the captions start again elsewhere
                now = time.monotonic()
                if picture:
                    self._leader_waits = now
                    if waited_from is None:
                        waited_from = now
                    if now - max(waited_from, self._progress_at) > limits.leader_wait_s:
                        late = "the picture's read waited too long"
                elif role == FOLLOWER and newest and self._reachable(pos):
                    deadline = None  # the picture's run reads there (it waits for the picture)
                else:
                    if deadline is None:
                        deadline = now + limits.follower_wait_s
                    elif now > deadline:
                        late = f"a read at {pos} was not served in time"
                    if role == FOLLOWER and newest and self._metadata(pos):
                        self._ask_detour(pos)  # the head or the tail: once the picture can wait
                if not late:
                    self._cond.wait(_SLICE)
            if late:  # logged out of the lock: no reader waits on a log's disk
                logger.info("Film relay: %s; that connection ends", late)
                return b""
            if self.stats["detours"] != detours:
                logger.debug("Film relay: the captions read %d past the cache: a detour", pos)
            if _gone(client):
                return b""

    def _reply(self, client: socket.socket, status: int, headers: list[tuple[str, str]]) -> bool:
        reason = {
            200: "OK",
            206: "Partial Content",
            302: "Found",
            404: "Not Found",
            416: "Range Not Satisfiable",
            502: "Bad Gateway",
            503: "Service Unavailable",
        }.get(status, "Error")
        lines = [f"HTTP/1.1 {status} {reason}", *(f"{k}: {v}" for k, v in headers)]
        if status not in (200, 206):
            lines.append("Content-Length: 0")
        lines.append("Connection: close")
        return self._send(client, ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1", "replace"))

    def _send(self, client: socket.socket, data: bytes) -> bool:
        """All of `data` to a reader (it reads at its pace: TCP backpressure); False: it left."""
        view = memoryview(data)
        while view:
            if self._stopping.is_set():
                return False
            try:
                view = view[client.send(view) :]
                continue
            except BlockingIOError:
                pass
            except OSError:
                return False
            try:
                _ready(client, False, _SLICE)
            except OSError, ValueError:
                return False
        return True


def _gone(client: socket.socket) -> bool:
    """The reader closed its connection (libVLC stopped, or moved to another range)."""
    try:
        if not _ready(client, True, 0):
            return False
        return client.recv(1, socket.MSG_PEEK) == b""
    except BlockingIOError:
        return False
    except OSError, ValueError:
        return True
