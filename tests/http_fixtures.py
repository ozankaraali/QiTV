"""Local HTTP(S) fixture servers for the live pipeline tests (not a test module).

``LoopbackHTTPServer`` and ``ThreadingLoopbackHTTPServer`` are an early copy of main's
``services/timeshift_source.py`` (numeric bind, no reverse DNS); the stream server task
replaces them with an import from ``services/live/server.py``. ``leaf`` and ``LocalSource``
are ported verbatim from main's ``tests/test_timeshift_source.py``.
"""

from collections import Counter
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import TCPServer, ThreadingMixIn
import ssl
import threading


class LoopbackHTTPServer(HTTPServer):
    """Internal HTTP listener whose startup never needs reverse DNS."""

    def server_bind(self) -> None:
        TCPServer.server_bind(self)
        self.server_name = str(self.server_address[0])
        self.server_port = self.server_address[1]


class ThreadingLoopbackHTTPServer(ThreadingMixIn, LoopbackHTTPServer):
    daemon_threads = True


def leaf(*segments, ended=True, extra=""):
    text = "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:4\n"
    text += "#EXT-X-MEDIA-SEQUENCE:0\n" + extra
    text += "".join(f"#EXTINF:4,\n{segment}\n" for segment in segments)
    if ended:
        text += "#EXT-X-ENDLIST\n"
    return text.encode()


class LocalSource:
    def __init__(self, routes, tls=None):
        self.routes = routes
        self.requests = []
        self.counts = Counter()
        self.stop = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

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
