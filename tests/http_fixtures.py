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
import tempfile
import threading
import unittest

from services.live.server import LoopbackHTTPServer, ThreadingLoopbackHTTPServer

__all__ = [
    "LocalSource",
    "LoopbackHTTPServer",
    "ThreadingLoopbackHTTPServer",
    "leaf",
    "tls_server_context",
]


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


class LocalSource:
    def __init__(self, routes, tls=None, keep_alive=False):
        self.routes = routes
        self.requests = []
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

        self.server = ThreadingLoopbackHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        if tls:
            self.server.socket = tls.wrap_socket(self.server.socket, server_side=True)
        self.url = f"{'https' if tls else 'http'}://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}
        )
        self.thread.start()

    def close(self):
        self.stop.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
