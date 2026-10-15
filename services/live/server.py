"""Token-protected loopback HTTP server streaming ring segments progressively.

`GET /<token>/<session>/stream/<seq>.ts` answers with one close-delimited MPEG-TS
stream: segment `seq` from its PAT/PMT header, then every later segment without its
header (so the bytes stay the original stream), following the open segment as it grows.

A paused player stops reading and the response write then blocks indefinitely, so chunks
are copied out with the lease-free `Ring.read_at`: nothing is held across socket I/O and
no segment is pinned against eviction, so the writer never waits for a reader. A reader
that falls behind eviction has its response ended. Handler errors are silent because
request paths carry the session token.
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
from typing import Any

from services.live.ring import Ring, RingError

_LOG = logging.getLogger(__name__)
_CHUNK = 64 * 1024
_HEADER_TIMEOUT = 2.0
_WAIT = 0.5
_SESSION_ID = r"[A-Za-z0-9_-]{1,64}"
_ROUTE = re.compile(rf"/([^/]+)/({_SESSION_ID})/stream/([0-9]{{1,20}})\.ts")
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
    ring: Ring
    is_ended: Callable[[], bool]
    removed: bool = False
    clients: set[socket.socket] = field(default_factory=set)


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

    def add_session(self, session_id: str, ring: Ring, is_ended: Callable[[], bool]) -> None:
        if not re.fullmatch(_SESSION_ID, session_id):
            raise ValueError("A stream session id must be 1-64 URL-safe characters.")
        with self._lock:
            previous = self._sessions.get(session_id)
            self._sessions[session_id] = _Session(ring, is_ended)
        if previous is not None:
            self._end(previous)

    def remove_session(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is not None:
            self._end(session)

    def stream_url(self, session_id: str, seq: int) -> str:
        if not self._base_url:
            raise RuntimeError("The stream server is not running.")
        return f"{self._base_url}/{session_id}/stream/{seq}.ts"

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

    def _attach(self, path: str, client: socket.socket) -> tuple[_Session, int] | None:
        """The session and start seq for a valid stream path, registered to `client`."""
        match = _ROUTE.fullmatch(path)
        if match is None:
            return None
        token, session_id, seq_text = match.groups()
        if not secrets.compare_digest(token.encode("latin-1"), self.token.encode("ascii")):
            return None
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or self._stopping:
                return None
            session.clients.add(client)
        return session, int(seq_text)

    def _detach(self, session: _Session, client: socket.socket) -> None:
        with self._lock:
            session.clients.discard(client)


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
        session, seq = attached
        try:
            status = self._stream(session, seq)
            if status:
                self._empty(status)
        finally:
            owner._detach(session, self.connection)

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

    def _stream(self, session: _Session, seq: int) -> int:
        """Stream from segment `seq`; returns the status to send when nothing was sent."""
        ring = session.ring
        number, offset, started = seq, 0, False
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
                self.wfile.write(data)
                offset += len(data)
                continue
            if not complete:
                if ended:
                    return 0
                _wait(session, before)
                continue
            status = ring.wait_for_seq(number + 1, 0.0)
            if status == "open":
                following = ring.read_at(number + 1, 0, 0)  # header length; reads no storage
                if following is None:
                    return 0  # evicted since
                number, offset = number + 1, following[2]
            elif status != "timeout" or ended:
                return 0  # the next segment was evicted, the ring closed, or the session ended
            else:
                _wait(session, before)
        return 0 if started else 404
