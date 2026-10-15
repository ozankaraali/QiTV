import asyncio
import base64
import json
import os
import tempfile
import threading
from types import SimpleNamespace
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from image_manager import ImageManager


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
