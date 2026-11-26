"""Local HTTP(S) fixture servers for the live pipeline tests (not a test module).

The servers are the product's ``LoopbackHTTPServer`` and ``ThreadingLoopbackHTTPServer``
from ``services/live/server.py`` (numeric bind, no reverse DNS). ``leaf`` and
``LocalSource`` are ported from main's ``tests/test_timeshift_source.py``; ``LocalSource``
gained a keep-alive mode (no ``Connection: close``) and counts the TCP connections it
accepted.
"""

from collections import Counter
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest

from services.live.server import LoopbackHTTPServer, ThreadingLoopbackHTTPServer

__all__ = [
    "LocalSource",
    "LoopbackHTTPServer",
    "ThreadingLoopbackHTTPServer",
    "leaf",
    "live_body",
    "tls_server_context",
]

# make_stream's media comes at about 32 kB a second. A live channel's connection brings its
# backlog at once and then about real time; the helper's not-live guard (2.8.11) holds each
# connection's bytes until their arrival settles (at most twice real time over a second), 4 s
# at most, so a body faster than that throughout is held the whole 4 s at every connection.
LIVE_BACKLOG = 188 * 1400  # some 8 s of make_stream's media
LIVE_BLOCK = 188 * 5  # every 20 ms: some 1.5 times real time


class _QuietServer(ThreadingLoopbackHTTPServer):
    """A fixture server that prints no traceback when its client went away: QiTV asks a
    provider over HTTPS first and hangs up on a server that speaks HTTP only (its "bad
    request" answer meets a closed socket). Other errors print as before."""

    def handle_error(self, request, client_address) -> None:
        if isinstance(sys.exception(), (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


@contextmanager
def tls_server_context():
    """A server TLS context with a fresh self-signed certificate for localhost.

    Raises ``unittest.SkipTest`` without an ``openssl`` binary to generate it.
    """
    openssl = shutil.which("openssl")
    if not openssl:
        raise unittest.SkipTest("openssl is required to generate a local test certificate")
    with tempfile.TemporaryDirectory(prefix="qitv-source-tls-") as directory:
        key = Path(directory) / "key.pem"
        certificate = Path(directory) / "cert.pem"
        subprocess.run(
            [
                openssl,
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-subj",
                "/CN=localhost",
                "-keyout",
                str(key),
                "-out",
                str(certificate),
            ],
            check=True,
            capture_output=True,
        )
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate, key)
        yield context


def leaf(*segments, ended=True, extra=""):
    text = "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:4\n"
    text += "#EXT-X-MEDIA-SEQUENCE:0\n" + extra
    text += "".join(f"#EXTINF:4,\n{segment}\n" for segment in segments)
    if ended:
        text += "#EXT-X-ENDLIST\n"
    return text.encode()


def live_body(payload, stop, hung_up=None, backlog=LIVE_BACKLOG):
    """A close-delimited body as a live channel sends it: `backlog` bytes of `payload` at
    once, then the rest at some 1.5 times real time, until `stop` is set or the client hangs
    up (`hung_up` is set then)."""

    def body(request):
        try:
            request.wfile.write(payload[:backlog])
            for offset in range(backlog, len(payload), LIVE_BLOCK):
                if stop.wait(0.02):
                    return
                request.wfile.write(payload[offset : offset + LIVE_BLOCK])
        except OSError:
            if hung_up is not None:
                hung_up.set()

    return body


class LocalSource:
    def __init__(self, routes, tls=None, keep_alive=False):
        self.routes = routes
        self.requests = []
        self.bodies = []
        self.counts = Counter()
        self.connections = 0
        self.keep_alive = keep_alive
        self.stop = threading.Event()
        lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def setup(self):
                super().setup()
                with lock:
                    owner.connections += 1

            def do_GET(self):
                owner.requests.append((self.path, dict(self.headers)))
                owner.counts[self.path] += 1
                route = owner.routes.get(self.path, (404, {}, b""))
                if callable(route):
                    route = route(self, owner.counts[self.path])
                status, headers, body = route
                self.send_response(status)
                if not callable(body):
                    self.send_header("Content-Length", str(len(body)))
                if not owner.keep_alive:
                    self.send_header("Connection", "close")
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                try:
                    if callable(body):
                        body(self)
                    else:
                        self.wfile.write(body)
                except ConnectionError, ssl.SSLError:
                    pass

            def do_POST(self):
                # The body is read first (the client may not hang up on an unread one) and
                # kept on the handler and in ``owner.bodies`` for a route to look at.
                self.body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                owner.bodies.append(self.body)
                self.do_GET()

        self.server = _QuietServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        if tls:
            self.server.socket = tls.wrap_socket(self.server.socket, server_side=True)
        self.url = f"{'https' if tls else 'http'}://127.0.0.1:{self.server.server_port}"
        # A daemon: a test run interrupted before close(), or a probe that crashed, must
        # still exit instead of waiting for this thread at interpreter shutdown forever.
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self.thread.start()

    def close(self):
        self.stop.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
