from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

from services.catalog import format_xtream_episodes
from services.provider_api import xtream_stream_url
from services.workers import (
    _XTREAM_CATALOG_ACTIONS,
    XtreamLoaderWorker,
    _resolve_xtream_base,
    _xtream_item,
)


class ResolvedBaseTests(unittest.TestCase):
    def test_without_an_advertised_base_the_users_url_gives_scheme_and_host(self):
        for url, base in (
            ("http://panel.example:8080/", "http://panel.example:8080"),
            ("https://panel.example/player_api.php", "https://panel.example"),
            ("panel.example", "http://panel.example"),
            ("panel.example:8080", "panel.example://panel.example:8080"),  # as ever
            ("", "http:"),
        ):
            with self.subTest(url=url):
                self.assertEqual(_resolve_xtream_base({}, url, prefer_https=False), base)


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


class XtreamItemTests(unittest.TestCase):
    """A Player API stream becomes a catalog item of its kind."""

    def item(self, stream, kind):
        prefix = _XTREAM_CATALOG_ACTIONS[kind][2]  # as the loader binds it
        address = partial(xtream_stream_url, "http://p:8", prefix, "u", "pw")
        return _xtream_item(stream, kind, address, "ts", 4)

    def test_each_kind(self):
        live = {"stream_id": 7, "name": "News", "category_id": "3", "epg_channel_id": "news.test"}
        self.assertEqual(
            self.item(live, "itv"),
            {
                "id": 7,
                "number": "4",  # its place, without a number of its own
                "name": "News",
                "logo": "",
                "tv_genre_id": "3",
                "cmd": "http://p:8/live/u/pw/7.ts",
                "xmltv_id": "news.test",
            },
        )
        film = {"stream_id": 8, "num": 2, "container_extension": "mkv", "category_ids": [5, 6]}
        film.update(stream_icon="i.png", plot="Plot", rating="7", releasedate="2026")
        item = self.item(film, "vod")
        self.assertEqual((item["number"], item["name"], item["logo"]), ("2", "Stream 8", "i.png"))
        self.assertEqual((item["tv_genre_id"], item["cmd"]), ("5", "http://p:8/movie/u/pw/8.mkv"))
        self.assertEqual(
            [item[k] for k in ("description", "rating", "year", "container_extension")],
            ["Plot", "7", "2026", "mkv"],
        )
        show = {"series_id": 9, "name": "Show", "cover": "c.png", "plot": "P", "year": "2025"}
        item = self.item(show, "series")
        self.assertEqual((item["cmd"], item["logo"], item["tv_genre_id"]), ("", "c.png", "None"))
        self.assertEqual((item["plot"], item["rating"], item["year"]), ("P", "", "2025"))
        self.assertIsNone(self.item({"name": "No id"}, "itv"))

    def test_an_episode_plays_from_the_series_path(self):
        season = {"episodes": [{"id": 31, "title": "Pilot", "container_extension": "mp4"}]}
        [episode] = format_xtream_episodes(season, "http://p:8", "ts", "u", "pw")
        self.assertEqual(episode["cmd"], "http://p:8/series/u/pw/31.mp4")


if __name__ == "__main__":
    unittest.main()
