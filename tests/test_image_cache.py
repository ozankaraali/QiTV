import asyncio
import base64
import json
import os
import tempfile
import threading
from types import SimpleNamespace
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from services.image_manager import ImageManager


def _data_url(payload: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(payload).decode("ascii")


class ImageCacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.config = SimpleNamespace(get_config_dir=lambda: self.directory.name)
        self.cache_dir = os.path.join(self.directory.name, "cache", "image")

    def tearDown(self):
        self.directory.cleanup()

    def _store(self, manager, payload, iconified):
        return asyncio.run(manager.cache_image_from_base64(_data_url(payload), iconified))

    def test_new_images_are_counted(self):
        manager = ImageManager(self.config, max_cache_size=10_000)
        self._store(manager, b"a" * 100, True)
        self._store(manager, b"b" * 200, False)
        self.assertEqual(manager.current_cache_size, 300)

    def test_touching_an_existing_image_does_not_double_count(self):
        manager = ImageManager(self.config, max_cache_size=10_000)
        self._store(manager, b"a" * 100, True)
        self._store(manager, b"a" * 100, True)
        self.assertEqual(manager.current_cache_size, 100)

    def test_oldest_images_are_evicted_with_their_real_extension(self):
        manager = ImageManager(self.config, max_cache_size=250)
        first = self._store(manager, b"a" * 100, False)  # jpg
        second = self._store(manager, b"b" * 100, True)  # png
        third = self._store(manager, b"c" * 100, True)
        self.assertFalse(os.path.exists(first))
        self.assertTrue(os.path.exists(second))
        self.assertTrue(os.path.exists(third))
        self.assertEqual(manager.current_cache_size, 200)

    def test_a_single_oversized_image_is_kept(self):
        manager = ImageManager(self.config, max_cache_size=50)
        path = self._store(manager, b"a" * 100, True)
        self.assertTrue(os.path.exists(path))
        self.assertEqual(len(manager.cache), 1)

    def test_legacy_index_with_null_and_extensionless_entries(self):
        os.makedirs(self.cache_dir, exist_ok=True)
        legacy_hash = "0" * 64
        with open(os.path.join(self.cache_dir, f"{legacy_hash}.jpg"), "wb") as f:
            f.write(b"x" * 120)
        index = {
            "f" * 64: None,
            legacy_hash: {"size": 120, "last_access": "2026-01-01 00:00:00"},
        }
        with open(os.path.join(self.cache_dir, "index.json"), "w", encoding="utf-8") as f:
            json.dump(index, f)

        manager = ImageManager(self.config, max_cache_size=150)
        self.assertEqual(manager.current_cache_size, 120)
        newest = self._store(manager, b"n" * 100, True)

        self.assertFalse(os.path.exists(os.path.join(self.cache_dir, f"{legacy_hash}.jpg")))
        self.assertTrue(os.path.exists(newest))
        self.assertEqual(manager.current_cache_size, 100)

    def test_unindexed_files_are_indexed_without_gui_objects(self):
        os.makedirs(self.cache_dir, exist_ok=True)
        with open(os.path.join(self.cache_dir, f"{'1' * 64}.png"), "wb") as f:
            f.write(b"p" * 10)
        manager = ImageManager(self.config)
        entry = manager.cache["1" * 64]
        self.assertEqual(entry["ext"], "png")
        self.assertEqual(entry["size"], 10)
        self.assertNotIn("qicon", entry)

    def test_concurrent_stores_and_eviction_keep_accounting_consistent(self):
        manager = ImageManager(self.config, max_cache_size=2_000)
        errors = []

        def work(worker):
            try:
                for i in range(200):
                    # Some payloads are shared between threads to force same-key touches.
                    payload = f"{worker if i % 3 else 0}-{i}".encode() * 20
                    asyncio.run(manager.cache_image_from_base64(_data_url(payload), True))
                    manager.save_index()
            except Exception as exc:  # noqa: BLE001 - surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(manager.current_cache_size, sum(e["size"] for e in manager.cache.values()))
        self.assertLessEqual(manager.current_cache_size, 2_000 + 400)

    def test_touching_an_evicted_entry_does_not_raise(self):
        manager = ImageManager(self.config, max_cache_size=10_000)
        path = self._store(manager, b"a" * 100, True)
        key = os.path.splitext(os.path.basename(path))[0]
        manager.cache.pop(key)
        manager.current_cache_size = 0
        os.remove(path)
        self.assertFalse(manager._touch_cache_entry(key, path))
        self.assertEqual(manager.current_cache_size, 0)

    def test_the_entry_just_stored_is_never_evicted(self):
        manager = ImageManager(self.config, max_cache_size=50)
        self._store(manager, b"a" * 100, True)
        path = self._store(manager, b"b" * 100, True)
        self.assertTrue(os.path.exists(path))
        self.assertEqual(len(manager.cache), 1)


if __name__ == "__main__":
    unittest.main()


class ImageDownloadTests(unittest.TestCase):
    """Images over HTTP. One public image host answers a library's default User-Agent with a
    403 and a line of text; that text was cached as the logo, never decoded, and never asked
    for again."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtCore import QBuffer, QIODevice
        from PySide6.QtGui import QGuiApplication, QImage

        cls.app = QGuiApplication.instance() or QGuiApplication([])
        image = QImage(8, 6, QImage.Format.Format_ARGB32)
        image.fill(0xFF336699)
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, "PNG")
        cls.png = bytes(buffer.data())

    def setUp(self):
        import http.server

        self.directory = tempfile.TemporaryDirectory()
        self.config = SimpleNamespace(get_config_dir=lambda: self.directory.name)
        self.manager = ImageManager(self.config, max_cache_size=1_000_000)
        self.agents: list[str] = []
        case = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                case.agents.append(self.headers.get("User-Agent", ""))
                refused = self.path == "/refused.png"
                body = (
                    b"Please set a user-agent and respect our robot policy" if refused else case.png
                )
                self.send_response(403 if refused else 200)
                self.send_header("Content-Type", "text/plain" if refused else "image/png")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.directory.cleanup()

    def load(self, urls):
        from services.image_loader import ImageLoader

        loader = ImageLoader(urls, self.manager, iconified=True, decode_side=64)
        results = []
        loader.progress_updated.connect(lambda current, total, data: results.append(data))
        asyncio.run(loader.load_images())
        return results

    def test_a_refusal_is_never_cached_as_the_image(self):
        results = self.load([f"{self.base}/refused.png"])
        self.assertEqual(results, [{}])
        self.assertEqual(self.manager.current_cache_size, 0)
        self.assertEqual([n for n in os.listdir(self.manager.cache_dir) if n.endswith(".png")], [])

    def test_images_are_asked_for_as_providers_are(self):
        from services.provider_api import CLIENT_HEADERS

        results = self.load([f"{self.base}/logo.png"])
        self.assertFalse(results[0]["image"].isNull())
        self.assertEqual(self.agents, [CLIENT_HEADERS["User-Agent"]])

    def test_a_cached_file_that_does_not_decode_is_asked_for_again(self):
        # What an older QiTV cached from a refusal: the next load drops it, the one after
        # that downloads the image.
        url = f"{self.base}/logo.png"
        stale = os.path.join(
            self.manager.cache_dir, self.manager._hash_string(url + "png") + ".png"
        )
        with open(stale, "wb") as f:
            f.write(b"Please set a user-agent and respect our robot policy")
        self.assertIsNone(self.load([url])[0]["image"])
        self.assertFalse(os.path.exists(stale))
        self.assertFalse(self.load([url])[0]["image"].isNull())
        self.assertEqual(len(self.agents), 1)
