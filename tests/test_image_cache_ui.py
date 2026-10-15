import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QElapsedTimer, QThread, Signal
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

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

    def test_keeps_most_recent_entries(self):
        path = self.image(8, 8)
        names = ["a", "b", "c", "d", "e"]
        FakeLoader.paths = {n: path for n in names}
        cache = self.make()
        with mock.patch.object(PosterCache, "MAX_ENTRIES", 3):
            for name in names:
                cache.request([name])
                self.settle(cache)
        self.assertEqual(list(cache._pixmaps), ["c", "d", "e"])

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


if __name__ == "__main__":
    unittest.main()
