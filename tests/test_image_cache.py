import asyncio
import base64
import json
import os
import tempfile
from types import SimpleNamespace
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from image_manager import ImageManager

# The pre-fix ImageManager builds QPixmaps while indexing; keep that from aborting the run.
APP = QApplication.instance() or QApplication([])


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


if __name__ == "__main__":
    unittest.main()
