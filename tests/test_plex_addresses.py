"""Plex (2.14, review I2, I3, M4): where each server is reached now, and its pictures kept as
references (no token, no address) that become the server's resizer only when fetched."""

import asyncio
import http.server
import os
import tempfile
import threading
from types import SimpleNamespace
import unittest
from urllib.parse import parse_qs, urlsplit

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from http_fixtures import ThreadingLoopbackHTTPServer

from services import plex, plex_addresses as pa
from services.image_manager import ImageManager
from tests.fixtures.local_provider import PROVIDER_NAME
from tests.test_plex import fixture
from tests.test_tv_window import WindowCase

HOME = "https://10-0-0-5.example.plex.direct:32400"
AWAY = "https://203-0-113-7.example.plex.direct:32400"
PROVIDER = {
    "type": "PLEX",
    "name": "Home Media",
    "url": HOME,
    "token": "server-token",
    "server_id": "machine-home",
    "device_id": "client-1",
}


class AddressesTest(unittest.TestCase):
    def setUp(self):
        pa.forget()
        self.addCleanup(pa.forget)

    def test_a_server_is_reached_where_it_was_last_found(self):
        self.assertEqual(pa.base_of(PROVIDER), HOME)  # not known yet: as stored
        pa.known([PROVIDER, {"type": "JELLYFIN", "url": "http://x.test", "server_id": "j"}])
        pa.moved("machine-home", AWAY + "/")
        self.assertEqual(pa.base_of(PROVIDER), AWAY)
        pa.known([PROVIDER])  # listed again as it was: it stays where it was found
        self.assertEqual(pa.base_of(PROVIDER), AWAY)
        signed_in_again = {**PROVIDER, "url": "http://10.0.0.9:32400"}
        pa.known([signed_in_again])
        self.assertEqual(pa.base_of(signed_in_again), "http://10.0.0.9:32400")
        pa.known([])  # removed: forgotten
        self.assertIsNone(pa.resolve_image("plex-image://machine-home/library/metadata/1/thumb/2"))

    def test_a_picture_is_a_reference_that_becomes_the_resizer_when_fetched(self):
        ref = pa.image_ref("machine-home", "/library/metadata/101/thumb/1795376703")
        self.assertEqual(ref, "plex-image://machine-home/library/metadata/101/thumb/1795376703")
        self.assertNotIn("token", ref)
        self.assertEqual(pa.image_ref("machine-home", ""), "")
        self.assertEqual(pa.image_ref("", "/x"), "")
        self.assertIsNone(pa.resolve_image(ref))  # its server not known yet
        pa.known([PROVIDER])
        pa.moved("machine-home", AWAY)
        url = pa.resolve_image(ref)
        parts = urlsplit(url)
        self.assertEqual(
            f"{parts.scheme}://{parts.netloc}{parts.path}", AWAY + "/photo/:/transcode"
        )
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        self.assertEqual(
            query,
            {
                "width": "240",
                "height": "360",  # as Jellyfin's posters
                "minSize": "1",
                "upscale": "1",
                "url": "/library/metadata/101/thumb/1795376703",
                "X-Plex-Token": "server-token",
            },
        )

    def test_titles_keep_references_never_the_token(self):
        films = fixture("movies_all.json")["MediaContainer"]["Metadata"]
        item = plex.title_item(films[0], HOME, "server-token", "1", server_id="machine-home")
        self.assertEqual(
            item["logo"], "plex-image://machine-home/library/metadata/101/thumb/1795376703"
        )
        payload = plex.seasons_payload(
            fixture("show_children.json")["MediaContainer"]["Metadata"],
            fixture("show_allLeaves.json")["MediaContainer"]["Metadata"],
            HOME,
            "server-token",
            server_id="machine-home",
        )
        self.assertNotIn("server-token", repr(payload))
        self.assertTrue(payload["seasons"][0]["cover"].startswith("plex-image://machine-home/"))
        section = plex.section_payload(
            [{"id": "1", "name": "Films", "type": "movie"}],
            {"1": films},
            HOME,
            "server-token",
            server_id="machine-home",
        )
        self.assertNotIn("server-token", repr(section))


class PictureFetchTest(unittest.TestCase):
    """A reference is fetched from the resizer at the server's address now, and kept in the
    image cache under the reference (a new address or token downloads nothing again)."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtCore import QBuffer, QIODevice
        from PySide6.QtGui import QImage
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])  # (the window's case needs one)
        image = QImage(8, 6, QImage.Format.Format_ARGB32)
        image.fill(0xFF336699)
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, "PNG")
        cls.png = bytes(buffer.data())

    def setUp(self):
        pa.forget()
        self.addCleanup(pa.forget)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        config = SimpleNamespace(get_config_dir=lambda: self.directory.name)
        self.manager = ImageManager(config, max_cache_size=1_000_000)
        self.asked: list[str] = []
        case = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                case.asked.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(case.png)))
                self.end_headers()
                self.wfile.write(case.png)

            def log_message(self, *args):
                pass

        self.server = ThreadingLoopbackHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def load(self, urls):
        from services.image_loader import ImageLoader

        loader = ImageLoader(urls, self.manager, iconified=False, decode_side=64)
        results = []
        loader.progress_updated.connect(lambda current, total, data: results.append(data))
        asyncio.run(loader.load_images())
        return results

    def test_a_reference_is_fetched_from_the_resizer_and_cached_under_itself(self):
        ref = "plex-image://machine-home/library/metadata/101/thumb/1"
        self.assertEqual(self.load([ref]), [{}])  # its server not known: nothing asked
        self.assertEqual(self.asked, [])
        pa.known([{**PROVIDER, "url": self.base}])
        results = self.load([ref])
        self.assertFalse(results[0]["image"].isNull())
        self.assertEqual(len(self.asked), 1)
        path, _, query = self.asked[0].partition("?")
        self.assertEqual(path, "/photo/:/transcode")
        self.assertEqual(parse_qs(query)["url"], ["/library/metadata/101/thumb/1"])
        pa.known([{**PROVIDER, "url": self.base, "token": "new-token"}])  # signed in again
        self.load([ref])
        self.assertEqual(len(self.asked), 1)  # the cache kept it under its reference


class WindowTellsTest(WindowCase):
    """The TV window tells where each Plex server is, as its provider list is and changes."""

    def provider_list(self):
        local = {"type": "M3UPLAYLIST", "name": PROVIDER_NAME, "url": "http://127.0.0.1:9/a.m3u"}
        return [local, dict(PROVIDER)]

    def setUp(self):
        pa.forget()
        self.addCleanup(pa.forget)
        super().setUp()

    def test_the_window_tells_the_plex_providers(self):
        ref = "plex-image://machine-home/library/metadata/1/thumb/2"
        self.assertIsNotNone(pa.resolve_image(ref))
        providers = self.window.provider_manager.providers
        providers[:] = [p for p in providers if p.get("type") != "PLEX"]
        self.window.catalogs.reload()
        self.assertIsNone(pa.resolve_image(ref))  # removed: its pictures go with it


if __name__ == "__main__":
    unittest.main()
