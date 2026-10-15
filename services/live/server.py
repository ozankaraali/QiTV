"""Token-protected loopback HTTP server streaming ring segments progressively.

`GET /<token>/<session>/stream/<seq>.ts` answers with one close-delimited MPEG-TS
stream: segment `seq` from its PAT/PMT header, then every later segment without its
header (so the bytes stay the original stream), following the open segment as it grows.

A paused player stops reading and the response write then blocks indefinitely, so a
lease is only held while one chunk is copied out of the ring, never across socket I/O:
eviction can always proceed and the writer never waits for a reader. A reader that falls
behind eviction has its response ended. Handler errors are silent because request paths
carry the session token.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
import logging
import re
import secrets
import socket
from socketserver import TCPServer, ThreadingMixIn
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


def _wait(session: _Session, ready: Callable[[], bool]) -> None:
    """Wait up to `_WAIT` for `ready()`, the ring closing, or the session's removal.

    `Ring.wait_for` and `Ring.wait_for_seq` re-check only their own predicate after a
    notify, so a removal could not end them early. Under `changed` only segment fields
    and the ring's read-only properties are read (the ring's documented rule).
    """
    ring = session.ring
    with ring.changed:
        ring.changed.wait_for(lambda: session.removed or ring.closed or ready(), _WAIT)


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
        # Request paths carry the session token; never print them.
        pass


class _Handler(BaseHTTPRequestHandler):
    server: _HTTPServer

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def handle(self) -> None:
        try:
            super().handle()
        except _QUIET:
            pass

    def do_GET(self) -> None:
        owner = self.server.owner
        attached = owner._attach(self.path, self.connection)
        if attached is None:
            self._not_found()
            return
        session, seq = attached
        try:
            segment = session.ring.lease(seq)
            if segment is None:
                # Evicted (older than the oldest) or not opened yet.
                self._not_found()
                return
            session.ring.release(segment)
            # Body writes block without a timeout: a paused player resumes later.
            self.connection.settimeout(None)
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self._stream(session, seq)
        finally:
            owner._detach(session, self.connection)

    def _not_found(self) -> None:
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()

    def _stream(self, session: _Session, seq: int) -> None:
        ring = session.ring
        number, offset, fresh = seq, 0, False  # fresh: skip the next segment's header
        while not session.removed:
            # Read before the data: an ended session never appends after this.
            ended = session.is_ended()
            segment = ring.lease(number)
            if segment is None:
                return  # evicted under a slow reader, or the ring closed
            try:
                if fresh:
                    offset, fresh = segment.header_len, False
                # Read before the data: once complete, an empty read means fully sent.
                complete = segment.complete
                data = ring.read(segment, offset, _CHUNK)
            except RingError:
                _LOG.debug("A stream response ended because the buffer could not be read.")
                return
            finally:
                ring.release(segment)
            if data:
                # No lease is held here: this write blocks while the player is paused.
                self.wfile.write(data)
                offset += len(data)
                continue
            if not complete:
                if ended:
                    return
                sent = offset
                _wait(session, lambda: segment.size > sent or segment.complete)
                continue
            # Captured before the status check, so a segment opened in between still wakes.
            newest = ring.newest_seq
            status = ring.wait_for_seq(number + 1, 0.0)
            if status == "open":
                number, offset, fresh = number + 1, 0, True
            elif status != "timeout" or ended:
                return  # the next segment was evicted, the ring closed, or the session ended
            else:
                _wait(session, lambda: ring.newest_seq != newest)
