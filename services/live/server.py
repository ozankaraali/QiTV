"""Token-protected loopback HTTP server streaming ring segments progressively.

`GET /<token>/<session>/stream/<seq>.ts` answers with one close-delimited MPEG-TS
stream: segment `seq` from its PAT/PMT header, then every later segment without its
header (so the bytes stay the original stream), following the open segment as it grows.

A paused player stops reading and the response write then blocks indefinitely, so chunks
are copied out with the lease-free `Ring.read_at`: nothing is held across socket I/O and
no segment is pinned against eviction, so the writer never waits for a reader. A reader
that falls behind eviction has its response ended. Handler errors are silent because
request paths carry the session token.

2.15, one stream across a join: a session added with `joins` (a site's recording of the running
broadcast, before the live buffer) may be told to go on into another session's ring (`join`:
the live buffer's segment where the recording's input stopped). Its viewer's response then goes
on there once the recording's ring ends, with that segment's tables, its clock re-based onto
the recording's last picture (`ts.ClockJoiner`: the player sees one stream whose clock runs
on, so it neither stops nor opens anything), and it reads as the live session's viewer from
then on. A join whose streams' video differs (another PID or stream type) is not made: the
response ends there, as before (the player's end opens the buffer there).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import errno
from http.server import BaseHTTPRequestHandler, HTTPServer
import logging
import re
import secrets
import socket
from socketserver import TCPServer, ThreadingMixIn
import sys
import threading
import time
from typing import Any

from services.live.ring import Ring, RingError
from services.live.ts import ClockJoiner

_LOG = logging.getLogger(__name__)
_CHUNK = 64 * 1024
_HEADER_TIMEOUT = 2.0
_WAIT = 0.5
# 2.15: a joining session's ring that ended waits this long for its join (the session asks it
# once it heard of the end: a moment later) before its response ends
_JOIN_WAIT_S = 3.0
_SESSION_ID = r"[A-Za-z0-9_-]{1,64}"
# `side`: the same stream for a reader that is not the viewer (2.9: AI captions read ahead of
# the picture); it never moves `reading()`, which follows the viewer's VLC.
_ROUTE = re.compile(rf"/([^/]+)/({_SESSION_ID})/(stream|side)/([0-9]{{1,20}})\.ts")
# Windows reports a vanished client as a reset or an abort where Unix reports EOF.
_QUIET = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError)


class LoopbackHTTPServer(HTTPServer):
    """Internal HTTP listener whose startup never needs reverse DNS."""

    def server_bind(self) -> None:
        TCPServer.server_bind(self)
        self.server_name = str(self.server_address[0])
        self.server_port = self.server_address[1]


class ThreadingLoopbackHTTPServer(ThreadingMixIn, LoopbackHTTPServer):
    daemon_threads = True
    block_on_close = False


@dataclass
class _Session:
    ring: Any  # a Ring, or what reads like one (recorder.RecordedReads)
    is_ended: Callable[[], bool]
    removed: bool = False
    clients: set[socket.socket] = field(default_factory=set)
    # 2.7: the segment the newest client streams now (a server part is paced by it), and
    # that client's turn (an older client still open after a reopen no longer moves it).
    reading: int | None = None
    reader: int = 0
    # 2.15: its stream's video (PID, stream type), and whether its viewer's response may go on
    # into another session's ring (`StreamServer.join`): where, once its ring ends.
    program: Callable[[], tuple[int | None, int | None]] = lambda: (None, None)
    joins: bool = False
    join: tuple[str, int] | None = None


def _wake(ring: Ring) -> None:
    with ring.changed:
        ring.changed.notify_all()


_Snapshot = tuple[int, int | None, int | None]


def _snapshot(ring: Ring) -> _Snapshot:
    """Differs after every append (more bytes, or an eviction), completion or new segment.

    Call it holding `changed` for a consistent view; only read-only properties are read
    (the ring's documented rule).
    """
    return ring.bytes, ring.oldest_seq, ring.open_seq


def _wait(session: _Session, before: _Snapshot) -> None:
    """Wait up to `_WAIT` for the ring to change from `before`, close, or lose the session.

    `Ring.wait_for` and `Ring.wait_for_seq` re-check only their own predicate after a
    notify, so a removal could not end them early.
    """
    ring = session.ring
    with ring.changed:
        ring.changed.wait_for(
            lambda: session.removed or ring.closed or _snapshot(ring) != before, _WAIT
        )


def _shutdown(client: socket.socket) -> None:
    try:
        client.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


class StreamServer:
    """One loopback server per helper; sessions are added and removed while it runs."""

    def __init__(self) -> None:
        self.token = secrets.token_hex(16)
        self._lock = threading.Lock()
        self._sessions: dict[str, _Session] = {}
        self._clients: set[socket.socket] = set()
        self._stopping = False
        self._base_url = ""
        self._server: _HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> str:
        if self._server is None:
            self._server = _HTTPServer(self)
            self._base_url = f"http://127.0.0.1:{self._server.server_port}/{self.token}"
            self._thread = threading.Thread(
                target=self._server.serve_forever,
                kwargs={"poll_interval": 0.1},
                name="qitv-live-http",
                daemon=True,
            )
            self._thread.start()
        return self._base_url

    def add_session(
        self,
        session_id: str,
        ring: Any,
        is_ended: Callable[[], bool],
        *,
        program: Callable[[], tuple[int | None, int | None]] | None = None,
        joins: bool = False,
    ) -> None:
        """`ring`: a `Ring`, or what reads like one (2.6: `recorder.RecordedReads`).
        `program()`: its stream's video PID and stream type; `joins` (2.15): its viewer's
        response may go on into another session's ring (`join`)."""
        if not re.fullmatch(_SESSION_ID, session_id):
            raise ValueError("A stream session id must be 1-64 URL-safe characters.")
        with self._lock:
            previous = self._sessions.get(session_id)
            session = _Session(ring, is_ended, joins=joins)
            if program is not None:
                session.program = program
            if previous is not None and previous.join is not None:
                session.join = previous.join  # (a resumed run's ring: the join stays)
            self._sessions[session_id] = session
        if previous is not None:
            self._end(previous)

    def join(self, session_id: str, other_id: str, seq: int) -> None:
        """2.15: the viewer's response of session `session_id` (added with `joins`) goes on,
        once its ring ends, into session `other_id`'s from segment `seq`; `other_id` "": it
        joins none (its response ends at its ring's end at once, no join waited for)."""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or not session.joins:
                return
            session.join = (other_id, int(seq))
        _wake(session.ring)

    def _joined(self, session: _Session, client: socket.socket) -> tuple[_Session, int] | None:
        """The session a joining response goes on in, and its segment, the client moved to
        it; None when there is none (no join, its session went, or its video differs)."""
        with self._lock:
            join = session.join
            other = self._sessions.get(join[0]) if join is not None else None
            if join is None or other is None or other.removed or self._stopping:
                return None
            ours, theirs = session.program(), other.program()
            if ours[1] != theirs[1] or ours[0] is None or theirs[0] is None:
                _LOG.info("A recording's stream does not join the buffer: its video differs.")
                return None  # (another kind of video; its PIDs alone are mapped: `_PidMap`)
            session.clients.discard(client)
            other.clients.add(client)
            return other, join[1]

    def _move_back(self, other: _Session, session: _Session, client: socket.socket) -> None:
        """A join that is not made after all: the client is the session's again."""
        with self._lock:
            other.clients.discard(client)
            session.clients.add(client)

    def remove_session(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is not None:
            self._end(session)

    def reading(self, session_id: str) -> int | None:
        """The segment the newest client of the session streams now (2.7); None before one
        did, or for an unknown session."""
        with self._lock:
            session = self._sessions.get(session_id)
        return session.reading if session is not None else None

    def stream_url(self, session_id: str, seq: int, *, side: bool = False) -> str:
        """The stream of a session from segment `seq`; `side`: for a reader that is not the
        viewer (it never moves `reading()`)."""
        if not self._base_url:
            raise RuntimeError("The stream server is not running.")
        return f"{self._base_url}/{session_id}/{'side' if side else 'stream'}/{seq}.ts"

    def stop(self) -> None:
        with self._lock:
            self._stopping = True
            sessions = list(self._sessions.values())
            self._sessions.clear()
            # Interrupt partial request headers and blocked writes before joining.
            for client in self._clients:
                _shutdown(client)
        for session in sessions:
            self._end(session)
        server, thread = self._server, self._thread
        if server is None or thread is None:
            return
        if thread.is_alive():
            server.shutdown()
            thread.join()
        server.server_close()

    def _end(self, session: _Session) -> None:
        with self._lock:
            session.removed = True
            for client in session.clients:
                _shutdown(client)
        _wake(session.ring)

    # ---- called from the HTTP server and its handler threads --------------------------
    def _track(self, client: socket.socket) -> bool:
        with self._lock:
            if self._stopping:
                return False
            self._clients.add(client)
            return True

    def _untrack(self, client: socket.socket) -> None:
        with self._lock:
            self._clients.discard(client)

    def _attach(self, path: str, client: socket.socket) -> tuple[_Session, int, bool] | None:
        """The session, start seq and whether it is a side reader, for a valid stream path,
        registered to `client`."""
        match = _ROUTE.fullmatch(path)
        if match is None:
            return None
        token, session_id, kind, seq_text = match.groups()
        if not secrets.compare_digest(token.encode("latin-1"), self.token.encode("ascii")):
            return None
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or self._stopping:
                return None
            session.clients.add(client)
        return session, int(seq_text), kind == "side"

    def _detach(self, session: _Session, client: socket.socket) -> None:
        with self._lock:
            session.clients.discard(client)


_PACKET = 188
# Picture and sound stream types a join maps (H.264, HEVC, MPEG-2 video; AAC, MP3, AC-3, E-AC-3,
# LATM): a stream of any other (a site's timed metadata) is left out of the joined stream
_AV_TYPES = frozenset({0x1B, 0x24, 0x02, 0x10, 0x0F, 0x11, 0x03, 0x04, 0x81, 0x87})


def _program_tables(data: bytes) -> tuple[int, int, list[tuple[int, int]]] | None:
    """2.15: a stream's PMT PID, PCR PID and its streams ((stream type, PID) in order), read
    from its first bytes (a segment's tables); None when they are not there whole."""
    pmt_pid: int | None = None
    for at in range(0, len(data) - _PACKET + 1, _PACKET):
        packet = data[at : at + _PACKET]
        if packet[0] != 0x47 or not packet[1] & 0x40:
            continue
        pid = ((packet[1] & 0x1F) << 8) | packet[2]
        start = 4 + (1 + packet[4] if packet[3] & 0x20 else 0)
        if start >= _PACKET:
            continue
        section = packet[start + 1 + packet[start] :]
        if pid == 0 and pmt_pid is None and len(section) >= 12 and section[0] == 0x00:
            length = ((section[1] & 0x0F) << 8) | section[2]
            for i in range(8, min(3 + length - 4, len(section) - 3), 4):
                if (section[i] << 8) | section[i + 1]:
                    pmt_pid = ((section[i + 2] & 0x1F) << 8) | section[i + 3]
                    break
        elif pid == pmt_pid and len(section) >= 12 and section[0] == 0x02:
            length = ((section[1] & 0x0F) << 8) | section[2]
            end = min(3 + length - 4, len(section))
            pcr = ((section[8] & 0x1F) << 8) | section[9]
            i = 12 + (((section[10] & 0x0F) << 8) | section[11])
            streams = []
            while i + 5 <= end:
                streams.append((section[i], ((section[i + 1] & 0x1F) << 8) | section[i + 2]))
                i += 5 + (((section[i + 3] & 0x0F) << 8) | section[i + 4])
            return pmt_pid, pcr, streams
    return None


class _PidMap:
    """2.15: the live buffer's packets under the recording's PIDs (Twitch's recording is
    remuxed from fMP4, a TS live stream keeps its own PIDs): each stream to the recording's
    stream of its place and type, its PAT and PMT the recording's own; packets passed whole
    (a chunk may end inside one). `of(ours, theirs)`: None when the two programs' streams do
    not match one to one, or need no mapping."""

    @classmethod
    def of(cls, ours: bytes, theirs: bytes) -> "_PidMap | None | bool":
        """A map of `theirs`' PIDs onto `ours` (each a stream's first bytes); True: the same
        PIDs already; None: no map can be made."""
        mine, other = _program_tables(ours), _program_tables(theirs)
        if mine is None or other is None:
            return None
        if mine == other:
            return True
        ours_av = [(t, pid) for t, pid in mine[2] if t in _AV_TYPES]
        theirs_av = [(t, pid) for t, pid in other[2] if t in _AV_TYPES]
        if not ours_av or [t for t, _ in ours_av] != [t for t, _ in theirs_av]:
            return None
        pids = {pid: to for (_, pid), (_, to) in zip(theirs_av, ours_av)}
        pids.setdefault(other[1], mine[1])
        # (their other streams' packets are left out: our tables do not name them)
        dropped = {pid for t, pid in other[2] if t not in _AV_TYPES} - set(pids)
        tables: dict[int, bytes] = {}
        for at in range(0, len(ours) - _PACKET + 1, _PACKET):
            pid = ((ours[at + 1] & 0x1F) << 8) | ours[at + 2]
            if pid in (0, mine[0]) and ours[at + 1] & 0x40 and pid not in tables:
                tables[0 if pid == 0 else other[0]] = ours[at : at + _PACKET]
        if len(tables) < 2:
            return None
        return cls(pids, tables, dropped)

    def __init__(self, pids: dict[int, int], tables: dict[int, bytes], dropped: set[int]) -> None:
        self._pids, self._tables, self._dropped = pids, tables, dropped
        self._rest = b""

    def feed(self, data: bytes) -> bytes:
        data = self._rest + data
        whole = len(data) - len(data) % _PACKET
        self._rest = data[whole:]
        out = bytearray()
        for at in range(0, whole, _PACKET):
            packet = data[at : at + _PACKET]
            pid = ((packet[1] & 0x1F) << 8) | packet[2]
            table = self._tables.get(pid)
            if table is not None:
                if packet[1] & 0x40:  # a table's first packet: ours, under our PID
                    out += table
                continue
            if pid in self._dropped:
                continue
            to = self._pids.get(pid)
            if to is None:
                out += packet
                continue
            out += packet[:1] + bytes([(packet[1] & 0xE0) | (to >> 8), to & 0xFF]) + packet[3:]
        return bytes(out)


class _HTTPServer(ThreadingLoopbackHTTPServer):
    allow_reuse_address = False

    def __init__(self, owner: StreamServer) -> None:
        self.owner = owner
        super().__init__(("127.0.0.1", 0), _Handler)

    def get_request(self) -> tuple[socket.socket, Any]:
        client, address = super().get_request()
        client.settimeout(_HEADER_TIMEOUT)
        if not self.owner._track(client):
            client.close()
            raise OSError("server stopping")
        return client, address

    def close_request(self, request: Any) -> None:
        self.owner._untrack(request)
        super().close_request(request)

    def handle_error(self, request: Any, client_address: Any) -> None:
        # Request paths carry the session token: log only the exception type.
        _LOG.debug("A stream request failed: %s", type(sys.exception()).__name__)


class _Handler(BaseHTTPRequestHandler):
    server: _HTTPServer
    _serving: _Session

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def handle(self) -> None:
        try:
            super().handle()
        except _QUIET:
            pass
        except OSError as error:
            # macOS reports a send racing the peer's reset as EPROTOTYPE.
            if error.errno != errno.EPROTOTYPE:
                raise

    def do_GET(self) -> None:
        owner = self.server.owner
        attached = owner._attach(self.path, self.connection)
        if attached is None:
            self._empty(404)
            return
        session, seq, side = attached
        self._serving = session  # (2.15: the session it reads from now, after a join too)
        try:
            status = self._stream(session, seq, side)
            if status:
                self._empty(status)
        finally:
            owner._detach(self._serving, self.connection)

    def _empty(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()

    def _start(self) -> None:
        # Body writes block without a timeout: a paused player resumes later.
        self.connection.settimeout(None)
        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()

    def _stream(self, session: _Session, seq: int, side: bool = False) -> int:
        """Stream from segment `seq`; returns the status to send when nothing was sent. A
        side reader leaves `reading` to the viewer's. 2.15: a joining session's viewer goes on
        into the session it joins once its ring ends (`StreamServer.join`)."""
        ring = session.ring
        number, offset, started = seq, 0, False
        turn = 0
        if not side:
            session.reader += 1
            turn = session.reader
            session.reading = number
        joiner: ClockJoiner | None = None
        junction = False
        current = [session]
        first = b""  # (2.15: its first bytes, its tables: a join maps the buffer's PIDs to them)
        mapped: _PidMap | None = None
        if session.joins and not side:
            joiner = ClockJoiner(program=session.program)  # (its PIDs stay on: `_PidMap`)
        while not session.removed:
            # Before the read, so any later append, completion or eviction ends the wait.
            with ring.changed:
                before = _snapshot(ring)
            # Read before the data: an ended session never appends after this.
            ended = session.is_ended()
            try:
                result = ring.read_at(number, offset, _CHUNK)
            except RingError:
                _LOG.debug("A stream response ended because the buffer could not be read.")
                return 0 if started else 503
            if result is None:
                # Evicted (under a slow reader, or before the first byte), not opened yet,
                # or the ring closed.
                return 0 if started else 404
            data, complete, _ = result
            if not started:
                started = True
                self._start()
            if data:
                # Nothing is held here: this write blocks while the player is paused.
                if joiner is None:
                    self.wfile.write(data)
                else:
                    if len(first) < 4 * 1024:
                        first += data[: 4 * 1024 - len(first)]
                    read = len(data)  # (the ring's bytes: a map's output differs in length)
                    sent = mapped.feed(data) if mapped is not None else data
                    for piece in joiner.feed(sent, junction=junction):
                        self.wfile.write(piece)
                    junction = False
                    offset += read
                    continue
                offset += len(data)
                continue
            if not complete:
                if ended:
                    if joiner is not None:
                        self._await_join(session)
                    if joiner is not None and session.join is not None:
                        went = self._go_on(session, joiner, side, first)
                        if went is not None:
                            session, number, turn, way = went
                            mapped = way if isinstance(way, _PidMap) else mapped
                            current[0], ring, offset, junction = session, session.ring, 0, True
                            continue
                    return 0
                _wait(session, before)
                continue
            status = ring.wait_for_seq(number + 1, 0.0)
            if status == "open":
                following = ring.read_at(number + 1, 0, 0)  # header length; reads no storage
                if following is None:
                    return 0  # evicted since
                # A new run's tables (2.6) come with it; else the header is skipped.
                number = number + 1
                offset = 0 if ring.junction(number) else following[2]
                if not side and session.reader == turn:
                    session.reading = number
            elif status != "timeout" or ended:
                if ended and status == "timeout" and joiner is not None:
                    self._await_join(session)
                if ended and status == "timeout" and joiner is not None and session.join:
                    went = self._go_on(session, joiner, side, first)
                    if went is not None:
                        session, number, turn, way = went
                        mapped = way if isinstance(way, _PidMap) else mapped
                        current[0], ring, offset, junction = session, session.ring, 0, True
                        continue
                return 0  # the next segment was evicted, the ring closed, or the session ended
            else:
                _wait(session, before)
        return 0 if started else 404

    @staticmethod
    def _await_join(session: _Session) -> None:
        """2.15: a joining session's ring ended: its join, asked once the session heard of the
        end, is waited for up to `_JOIN_WAIT_S` (the player plays what it holds meanwhile)."""
        deadline = time.monotonic() + _JOIN_WAIT_S
        ring = session.ring
        while session.join is None and not session.removed and time.monotonic() < deadline:
            with ring.changed:
                ring.changed.wait(min(0.05, max(0.0, deadline - time.monotonic())))

    def _go_on(
        self, session: _Session, joiner: ClockJoiner, side: bool, first: bytes
    ) -> tuple[_Session, int, int, Any] | None:
        """2.15: a joining session's ring ended: the session it joins and its segment (with
        that segment's tables: `offset` 0), the response reading as its viewer from now on,
        and how its PIDs map onto this stream's (`first`: its first bytes; True: the same);
        None: it ends here (no join, or streams that do not match one to one)."""
        joined = self.server.owner._joined(session, self.connection)
        if joined is None:
            return None
        other, number = joined
        try:
            head = other.ring.read_at(number, 0, 4 * 1024)
        except RingError:
            head = None
        way = _PidMap.of(first, head[0]) if head is not None else None
        if way is None:
            _LOG.info("A recording's stream does not join the buffer: its streams differ.")
            self.server.owner._move_back(other, session, self.connection)
            return None
        if isinstance(way, _PidMap):
            _LOG.info("A recording's stream goes on into the buffer under its own PIDs.")
        self._serving = other
        turn = 0
        if not side:
            other.reader += 1
            turn = other.reader
            other.reading = number
        _LOG.info("A recording's stream goes on into the buffer.")
        return other, number, turn, way
