import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QElapsedTimer, Qt, QThread, Signal
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from services import image_loader
from services.thread_cleanup import has_pending_threads
from ui import logos
from ui.logos import LogoCache, PosterCache


class FakeLoader(QThread):
    """Stands in for ImageLoader: emits progress for ranks the test maps to paths."""

    progress_updated = Signal(int, int, dict)
    paths: dict[str, str] = {}
    batches: list[list[str]] = []

    def __init__(self, urls, image_manager, iconified=False, verify_ssl=True, **_):
        super().__init__()
        self.urls = list(urls)
        self.iconified = iconified
        FakeLoader.batches.append(self.urls)

    def run(self):
        hits = [(r, u) for r, u in enumerate(self.urls) if u in FakeLoader.paths]
        for i, (rank, url) in enumerate(hits, 1):
            self.progress_updated.emit(
                i, len(hits), {"rank": rank, "cache_path": FakeLoader.paths[url]}
            )


class ImageCacheTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        FakeLoader.paths = {}
        FakeLoader.batches = []
        patcher = mock.patch.object(logos, "ImageLoader", FakeLoader)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.assert_no_pending_threads)

    def assert_no_pending_threads(self):
        # Runs before the loader patch is undone and after cache.shutdown (LIFO order).
        QTest.qWait(1)
        deadline = QElapsedTimer()
        deadline.start()
        while has_pending_threads() and deadline.elapsed() < 2000:
            QTest.qWait(10)
        self.assertFalse(has_pending_threads(), "a test leaked a pending thread cleanup")

    def image(self, width, height):
        img = QImage(width, height, QImage.Format_RGB32)
        img.fill(QColor("blue"))
        handle, path = tempfile.mkstemp(suffix=".png")
        os.close(handle)
        self.addCleanup(os.remove, path)
        img.save(path)
        return path

    def make(self, cls=PosterCache, logos_on=True):
        cache = cls(mock.Mock(), mock.Mock(channel_logos=logos_on, ssl_verify=True))
        self.addCleanup(cache.shutdown)
        return cache

    def settle(self, cache, timeout=5000):
        timer = QElapsedTimer()
        timer.start()
        while (cache._loader is not None or cache._queue) and timer.elapsed() < timeout:
            QCoreApplication.processEvents()
            QThread.msleep(5)
        self.assertLess(timer.elapsed(), timeout, "cache did not settle")

    def test_poster_is_bounded_and_announced(self):
        FakeLoader.paths = {"p": self.image(1000, 1500)}
        cache = self.make()
        ready = []
        cache.logoReady.connect(ready.append)
        cache.request(["p"])
        self.settle(cache)
        stored = cache.pixmap("p")
        dpr = max(1, round(self.app.devicePixelRatio()))
        self.assertEqual(max(stored.width(), stored.height()), 360 * dpr)
        self.assertEqual(ready, ["p"])

    def test_poster_loads_full_size_not_iconified(self):
        cache = self.make()
        with (
            mock.patch.object(logos, "ImageLoader") as loader,
            mock.patch.object(logos, "ThreadCleanup"),
        ):
            cache.request(["p"])
        self.assertFalse(loader.call_args.kwargs["iconified"])

    def test_poster_ignores_logo_setting_but_logo_cache_obeys(self):
        FakeLoader.paths = {"p": self.image(10, 10)}
        poster = self.make(PosterCache, logos_on=False)
        poster.request(["p"])
        self.settle(poster)
        self.assertIsNotNone(poster.pixmap("p"))
        logo = self.make(LogoCache, logos_on=False)
        logo.request(["l"])
        self.assertEqual(FakeLoader.batches, [["p"]])
        self.assertIsNone(logo._loader)

    def test_keeps_most_recent_entries_within_the_byte_budget(self):
        path = self.image(8, 8)
        names = ["a", "b", "c", "d", "e"]
        FakeLoader.paths = {n: path for n in names}
        cache = self.make()
        with mock.patch.object(PosterCache, "MAX_BYTES", 3 * 8 * 8 * 4):  # three 8×8 images
            for name in names:
                cache.request([name])
                self.settle(cache)
        self.assertEqual(list(cache._pixmaps), ["c", "d", "e"])
        self.assertEqual(cache.size_bytes(), 3 * 8 * 8 * 4)

    def test_the_caches_are_bounded_by_bytes(self):
        self.assertEqual(LogoCache.MAX_BYTES, 48 * 1024 * 1024)
        self.assertGreater(PosterCache.MAX_BYTES, 0)
        self.assertFalse(hasattr(LogoCache, "MAX_ENTRIES"))  # a byte budget, not a count

    def test_an_image_larger_than_the_budget_is_kept_alone(self):
        FakeLoader.paths = {"a": self.image(8, 8), "big": self.image(40, 40)}
        cache = self.make()
        with mock.patch.object(PosterCache, "MAX_BYTES", 1000):
            for name in ("a", "big"):
                cache.request([name])
                self.settle(cache)
        self.assertEqual(list(cache._pixmaps), ["big"])
        self.assertEqual(cache.size_bytes(), 40 * 40 * 4)

    def test_logos_are_stored_at_the_size_they_are_painted(self):
        # About 8u of a ~1600 px host: the banner's logo; rows (4u) scale down from it.
        FakeLoader.paths = {"l": self.image(1000, 500)}
        cache = self.make(LogoCache)
        cache.request(["l"])
        self.settle(cache)
        dpr = max(1, round(self.app.devicePixelRatio()))
        stored = cache.pixmap("l")
        self.assertEqual((stored.width(), stored.height()), (128 * dpr, 64 * dpr))
        self.assertEqual(cache.size_bytes(), 128 * dpr * 64 * dpr * 4)

    def test_the_loader_decodes_at_the_stored_size(self):
        cache = self.make(LogoCache)
        with (
            mock.patch.object(logos, "ImageLoader") as loader,
            mock.patch.object(logos, "ThreadCleanup"),
        ):
            cache.request(["l"])
        dpr = self.app.devicePixelRatio()
        self.assertEqual(loader.call_args.kwargs["decode_side"], round(LogoCache.MAX_SIDE * dpr))

    def test_an_image_decoded_by_the_loader_is_used_as_is(self):
        cache = self.make()
        cache._batch = ["u"]
        image = QImage(30, 20, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor("green"))
        cache._on_progress(1, 1, {"rank": 0, "cache_path": "/nonexistent.png", "image": image})
        stored = cache.pixmap("u")
        self.assertEqual((stored.width(), stored.height()), (30, 20))

    def test_an_image_the_loader_could_not_decode_is_not_read_again(self):
        cache = self.make()
        cache._batch = ["u"]
        path = self.image(10, 10)
        with (
            mock.patch.object(logos, "read_scaled") as read,
            self.assertLogs("ui.logos", "INFO"),
        ):
            cache._on_progress(1, 1, {"rank": 0, "cache_path": path, "image": None})
        read.assert_not_called()
        self.assertIsNone(cache.pixmap("u"))

    def test_failing_url_is_requested_three_times_in_total(self):
        cache = self.make()
        for _ in range(6):
            cache.request(["bad"])
            self.settle(cache)
        self.assertEqual(FakeLoader.batches, [["bad"]] * 3)  # first try + 2 retries

    def test_queued_urls_accumulate_by_default(self):
        cache = self.make()
        cache.request(["a"])  # in flight until its cleanup runs on the event loop
        cache.request(["b"])
        cache.request(["c"])
        self.settle(cache)
        self.assertEqual(FakeLoader.batches, [["a"], ["b", "c"]])

    def test_replace_pending_drops_queued_urls_but_not_the_batch_in_flight(self):
        cache = self.make()
        cache.request(["a"])  # in flight
        cache.request(["b", "c"])  # queued behind it
        cache.request(["d", "b"], replace_pending=True)
        self.assertEqual(cache._queue, ["d", "b"])
        self.settle(cache)
        self.assertEqual(FakeLoader.batches, [["a"], ["d", "b"]])
        cache.request(["c"])  # dropped before it loaded, so it may be requested again
        self.settle(cache)
        self.assertEqual(FakeLoader.batches, [["a"], ["d", "b"], ["c"]])

    def test_late_progress_after_shutdown_is_ignored(self):
        cache = self.make()
        cache._batch = ["u"]
        cache.shutdown()
        cache._on_progress(1, 1, {"rank": 0, "cache_path": self.image(4, 4)})
        self.assertIsNone(cache.pixmap("u"))

    def test_superseded_generation_is_ignored(self):
        cache = self.make()
        cache._batch = ["u"]
        cache._generation = 2
        data = {"rank": 0, "cache_path": self.image(4, 4)}
        cache._on_progress(1, 1, data, generation=1)
        self.assertIsNone(cache.pixmap("u"))
        cache._on_progress(1, 1, data, generation=2)
        self.assertIsNotNone(cache.pixmap("u"))

    def test_shutdown_mid_batch_still_releases_the_loader(self):
        class SlowLoader(FakeLoader):
            def run(self):
                while not self.isInterruptionRequested():
                    QThread.msleep(5)

            def cancel(self):
                self.requestInterruption()

        cache = self.make()
        with mock.patch.object(logos, "ImageLoader", SlowLoader):
            cache.request(["slow"])
            self.assertIsNotNone(cache._loader)
            cache.shutdown()
            self.assertTrue(has_pending_threads())
            self.assertEqual(cache._queue, [])
            deadline = QElapsedTimer()
            deadline.start()
            while has_pending_threads() and deadline.elapsed() < 3000:
                QTest.qWait(10)
        self.assertFalse(has_pending_threads())

    def test_missing_poster_file_leaves_no_entry(self):
        cache = self.make()
        cache._batch = ["u"]
        cache._on_progress(1, 1, {"rank": 0, "cache_path": "/nonexistent/poster.png"})
        self.assertIsNone(cache.pixmap("u"))


class LoaderDecodeTests(unittest.TestCase):
    """ImageLoader decodes and scales into a QImage on its own thread (never a QPixmap)."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_decodes_and_scales_off_the_gui_thread(self):
        img = QImage(1000, 500, QImage.Format_RGB32)
        img.fill(QColor("blue"))
        handle, path = tempfile.mkstemp(suffix=".png")
        os.close(handle)
        self.addCleanup(os.remove, path)
        img.save(path)

        class Manager:
            async def cache_image_from_base64(self, image_str, iconified):
                return path

        threads = []
        read = image_loader.read_scaled

        def recording(*args):
            threads.append(QThread.currentThread())
            return read(*args)

        results = []
        loader = image_loader.ImageLoader(
            ["data:image/png;base64,AAAA"], Manager(), iconified=True, decode_side=64
        )
        loader.progress_updated.connect(
            lambda _c, _t, data: results.append(data), Qt.ConnectionType.QueuedConnection
        )
        with mock.patch.object(image_loader, "read_scaled", side_effect=recording):
            loader.start()
            self.assertTrue(loader.wait(5000))
        QCoreApplication.processEvents()
        self.assertEqual(len(results), 1)
        image = results[0]["image"]
        self.assertIsInstance(image, QImage)
        self.assertEqual((image.width(), image.height()), (64, 32))
        self.assertEqual(len(threads), 1)
        self.assertIsNot(threads[0], self.app.thread())

    def test_read_scaled_keeps_small_images_and_rejects_unreadable_files(self):
        img = QImage(20, 10, QImage.Format_RGB32)
        img.fill(QColor("red"))
        handle, path = tempfile.mkstemp(suffix=".png")
        os.close(handle)
        self.addCleanup(os.remove, path)
        img.save(path)
        self.assertEqual(image_loader.read_scaled(path, 64).size().toTuple(), (20, 10))
        self.assertIsNone(image_loader.read_scaled("/nonexistent/x.png", 64))


if __name__ == "__main__":
    unittest.main()
