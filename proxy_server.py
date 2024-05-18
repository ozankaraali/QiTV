import requests
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

import requests


# Attempt to import vlc, set to None if unavailable

class ProxyHTTPRequestHandler(BaseHTTPRequestHandler):
    parent_app = None  # Reference to VideoPlayer instance
    active_request = False
    lock = threading.Lock()

    def do_GET(self):
        parsed_path = urlparse(self.path)
        query = parse_qs(parsed_path.query)
        stream_url = query.get('url', [None])[0]

        if stream_url:
            headers = ProxyHTTPRequestHandler.parent_app.generate_headers()
            try:
                with ProxyHTTPRequestHandler.lock:
                    if ProxyHTTPRequestHandler.active_request:
                        ProxyHTTPRequestHandler.active_request = False

                    ProxyHTTPRequestHandler.active_request = True

                r = requests.get(stream_url, headers=headers, stream=True)
                self.send_response(r.status_code)
                for key, value in r.headers.items():
                    self.send_header(key, value)
                self.end_headers()

                for chunk in r.iter_content(chunk_size=128):
                    if not ProxyHTTPRequestHandler.active_request:
                        break
                    try:
                        self.wfile.write(chunk)
                    except BrokenPipeError:
                        break

            except requests.RequestException as e:
                self.send_error(500, str(e))

        else:
            self.send_error(400, "Bad request")

    def finish(self):
        if ProxyHTTPRequestHandler.active_request:
            with ProxyHTTPRequestHandler.lock:
                ProxyHTTPRequestHandler.active_request = False
        return super().finish()


class ProxyServerThread(threading.Thread):
    def __init__(self, host, port, handler):
        super().__init__()
        self.server = HTTPServer((host, port), handler)
        self.daemon = True

    def run(self):
        self.server.serve_forever()

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()