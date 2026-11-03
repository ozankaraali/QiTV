"""A stand-in provider for the film relay's tests (tests/test_film_relay.py, test_film_relay_vlc).

It serves one film over HTTP with byte ranges behind a redirect to a tokenized address, as an
Xtream panel serves its films, and notes each time a connection opens while another of its
connections is still open (`overlaps`): the relay must never hold two. A connection counts as
open until its client has closed it (its FIN or reset is seen, with a peek, when the next one
opens: on the loopback the client's close arrives before its next connect).
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import select
import socket
import threading
import time
from typing import Any, Optional

FILM_PATH = "/movie/viewer/secret/7"


def _peer_closed(sock: socket.socket) -> bool:
    try:
        ready, _, _ = select.select([sock], [], [], 0)
        if not ready:
            return False
        # The socket's own bytes (a TLS socket's too: the relay closes without a TLS goodbye).
        return socket.socket.recv(sock, 1, socket.MSG_PEEK) == b""
    except OSError:
        return True


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, provider: "FilmProvider") -> None:
        self.provider = provider
        super().__init__(("127.0.0.1", 0), _Handler)

    def process_request(self, request: Any, client_address: Any) -> None:
        self.provider._opened(request)
        super().process_request(request, client_address)

    def shutdown_request(self, request: Any) -> None:
        self.provider._closed(request)
        super().shutdown_request(request)

    def handle_error(self, request: Any, client_address: Any) -> None:
        pass  # a client that let go mid-answer (a seek) is no error here


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _Server

    def log_message(self, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        self.server.provider._serve(self)


class FilmProvider:
    def __init__(
        self,
        data: bytes,
        *,
        extension: str = ".mkv",
        content_type: str = "video/x-matroska",
        rate: Optional[float] = None,
    ) -> None:
        self.data = data
        self.extension = extension
        self.content_type = content_type
        self.rate = rate  # bytes a second per connection (None: as fast as the socket takes)
        self.requests: list[dict] = []  # each ask: path, first, last, user agent, referrer, cookie
        self.overlaps: list[str] = []
        self.answers: list[int] = []  # statuses the next film asks get instead of the film
        self.cut: Optional[int] = None  # the next film answer ends after this many bytes
        self.ranges = True  # False: the film is answered whole, without ranges
        self._token = 1
        self._lock = threading.Lock()
        self._open: dict[int, socket.socket] = {}
        self._sent = 0
        self._server = _Server(self)
        threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        ).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}{FILM_PATH}{self.extension}"

    def expire(self) -> None:
        """The tokenized address answers 403 from now; the next redirect gives a new one."""
        with self._lock:
            self._token += 1

    def sent(self) -> int:
        with self._lock:
            return self._sent

    def open_now(self) -> int:
        with self._lock:
            return sum(1 for sock in self._open.values() if not _peer_closed(sock))

    def film_asks(self) -> list[dict]:
        with self._lock:
            return [r for r in self.requests if r["path"].startswith("/stream/")]

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    # ---- the server's side -------------------------------------------------------------------
    def _opened(self, sock: socket.socket) -> None:
        with self._lock:
            still = [s for s in self._open.values() if not _peer_closed(s)]
            if still:
                self.overlaps.append(f"{len(still)} open at {time.monotonic():.3f}")
            self._open[id(sock)] = sock

    def _closed(self, sock: socket.socket) -> None:
        with self._lock:
            self._open.pop(id(sock), None)

    def _serve(self, handler: _Handler) -> None:
        path = handler.path
        first, last = None, None
        value = handler.headers.get("Range")
        if value and value.startswith("bytes="):
            a, _, b = value[6:].partition("-")
            first = int(a) if a else None
            last = int(b) if b else None
        with self._lock:
            self.requests.append(
                {
                    "path": path,
                    "first": first,
                    "last": last,
                    "agent": handler.headers.get("User-Agent"),
                    "referrer": handler.headers.get("Referer"),
                    "cookie": handler.headers.get("Cookie"),
                    "at": time.monotonic(),
                }
            )
            token = self._token
            answer = self.answers.pop(0) if self.answers and path.startswith("/stream/") else None
            cut, self.cut = (self.cut, None) if path.startswith("/stream/") else (None, self.cut)
        if path == FILM_PATH + self.extension:
            handler.send_response(302)
            handler.send_header("Location", f"/stream/{token}/7{self.extension}")
            handler.send_header("Set-Cookie", "session=abc; Path=/")
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return
        if not path.startswith(f"/stream/{token}/"):
            self._status(handler, 403)
            return
        if answer is not None:
            self._status(handler, answer)
            return
        size = len(self.data)
        if first is None or not self.ranges:
            start, end, status = 0, size, 200
        else:
            start, end, status = first, size if last is None else min(size, last + 1), 206
        if start >= size:
            handler.send_response(416)
            handler.send_header("Content-Range", f"bytes */{size}")
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return
        handler.send_response(status)
        handler.send_header("Content-Type", self.content_type)
        if self.ranges:
            handler.send_header("Accept-Ranges", "bytes")
        if status == 206:
            handler.send_header("Content-Range", f"bytes {start}-{end - 1}/{size}")
        handler.send_header("Content-Length", str(end - start))
        handler.end_headers()
        began, pos = time.monotonic(), start
        try:
            while pos < end:
                piece = self.data[pos : min(end, pos + 16384)]
                if cut is not None and pos - start + len(piece) > cut:
                    piece = piece[: max(0, cut - (pos - start))]
                    handler.wfile.write(piece)
                    with self._lock:
                        self._sent += len(piece)
                    handler.close_connection = True
                    handler.connection.shutdown(socket.SHUT_RDWR)
                    return
                handler.wfile.write(piece)
                pos += len(piece)
                with self._lock:
                    self._sent += len(piece)
                if self.rate:
                    wait = (pos - start) / self.rate - (time.monotonic() - began)
                    if wait > 0:
                        time.sleep(wait)
        except OSError:
            pass  # the client let go

    @staticmethod
    def _status(handler: _Handler, status: int) -> None:
        handler.send_response(status)
        handler.send_header("Content-Length", "0")
        handler.end_headers()
