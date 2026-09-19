from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

from workers import XtreamLoaderWorker


class XtreamRequestTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.allowed_formats = ["m3u8", "ts"]
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                url = urlsplit(self.path)
                query = parse_qs(url.query)
                action = query.get("action", [None])[0]
                fixture.requests.append((url.path, action))
                if url.path != "/player_api.php":
                    self.send_error(403, "Media must not be opened during catalog loading")
                    return
                if action is None:
                    payload = {
                        "server_info": {
                            "url": "127.0.0.1",
                            "server_protocol": "https",
                            "port": "1",
                            "https_port": "443",
                        },
                        "user_info": {"allowed_output_formats": fixture.allowed_formats},
                    }
                elif action in ("get_live_categories", "get_vod_categories"):
                    payload = [{"category_id": "10", "category_name": "Fixture"}]
                elif action == "get_live_streams":
                    payload = [{"stream_id": 7, "name": "Live", "category_id": "10"}]
                elif action == "get_vod_streams":
                    payload = [
                        {
                            "stream_id": 8,
                            "name": "Movie",
                            "category_id": "10",
                            "container_extension": "mkv",
                        }
                    ]
                else:
                    self.send_error(400, "Unexpected API action")
                    return
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_port}"

    def load_catalog(self, content_type):
        worker = XtreamLoaderWorker(self.base, "user", "password", content_type)
        results = []
        errors = []
        worker.finished.connect(results.append)
        worker.error.connect(errors.append)
        worker.run()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 1)
        return results[0]

    def test_live_catalog_only_fetches_api_and_preserves_explicit_scheme_and_port(self):
        catalog = self.load_catalog("itv")
        self.assertEqual(
            self.requests,
            [
                ("/player_api.php", None),
                ("/player_api.php", "get_live_categories"),
                ("/player_api.php", "get_live_streams"),
            ],
        )
        self.assertEqual(catalog["contents"][0]["cmd"], f"{self.base}/live/user/password/7.ts")

    def test_hls_only_provider_does_not_need_a_stream_probe(self):
        self.allowed_formats = ["m3u8"]
        catalog = self.load_catalog("itv")
        self.assertEqual(catalog["contents"][0]["cmd"], f"{self.base}/live/user/password/7.m3u8")
        self.assertTrue(all(path == "/player_api.php" for path, _ in self.requests))

    def test_movie_catalog_uses_container_metadata_without_opening_media(self):
        catalog = self.load_catalog("vod")
        self.assertEqual(
            self.requests,
            [
                ("/player_api.php", None),
                ("/player_api.php", "get_vod_categories"),
                ("/player_api.php", "get_vod_streams"),
            ],
        )
        self.assertEqual(catalog["contents"][0]["cmd"], f"{self.base}/movie/user/password/8.mkv")


if __name__ == "__main__":
    unittest.main()
