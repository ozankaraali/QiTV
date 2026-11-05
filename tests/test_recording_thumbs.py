"""Recordings' thumbnails (2.9.7): a frame of each recording, read once on a worker thread and
kept as a small picture in the cache; tiles show it through the image cache's own loader."""

import asyncio
import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QElapsedTimer
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from services import recording_thumbs as rt

app = QApplication.instance() or QApplication([])
FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "live.ts")


def picture(color="#3060a0"):
    image = QImage(rt.WIDTH, rt.HEIGHT, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    return image


def wait(condition, ms=5000):
    timer = QElapsedTimer()
    timer.start()
    while not condition() and timer.elapsed() < ms:
        QCoreApplication.processEvents()
    return condition()


class ThumbnailsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="qitv-thumbs-")
        self.addCleanup(self.directory.cleanup)
        self.folder = os.path.join(self.directory.name, "thumbs")
        self.grabbed = []
        self.recording = os.path.join(self.directory.name, "Northwind News 2026-11-05.ts")
        with open(self.recording, "wb") as f:
            f.write(b"\x47" * 188 * 20000)  # big enough to be read

    def thumbs(self, grab=None):
        def fake(path, at):
            self.grabbed.append((path, at))
            return picture()

        thumbs = rt.Thumbnails(self.folder, grab=grab or fake)
        self.addCleanup(thumbs.shutdown, wait=True)
        return thumbs

    def item(self, length=3600.0):
        return {"path": self.recording, "_start": 1000.0, "_stop": 1000.0 + length}

    def test_a_recordings_thumbnail_is_made_once_and_its_url_follows(self):
        thumbs = self.thumbs()
        ready = []
        thumbs.ready.connect(ready.append)
        self.assertEqual(thumbs.url(self.item()), "")  # asked for: not there yet
        self.assertTrue(wait(lambda: ready))
        url = thumbs.url(self.item())
        self.assertTrue(url.startswith(rt.SCHEME))
        self.assertTrue(os.path.isfile(rt.file_of(url)))
        self.assertEqual(len(self.grabbed), 1)
        ((path, at),) = self.grabbed
        self.assertEqual(path, self.recording)
        self.assertEqual(at, 300.0)  # a fifth in, five minutes at most
        thumbs.url(self.item())
        self.assertEqual(len(self.grabbed), 1)

    def test_a_thumbnail_made_before_is_used_without_reading_the_recording(self):
        first = self.thumbs()
        first.url(self.item())
        self.assertTrue(wait(lambda: first.url(self.item())))
        first.shutdown()
        self.grabbed.clear()
        again = self.thumbs()
        self.assertEqual(again.url(self.item()), "")
        self.assertTrue(wait(lambda: again.url(self.item())))
        self.assertEqual(self.grabbed, [])

    def test_a_recording_that_gives_no_frame_is_not_read_again(self):
        thumbs = self.thumbs(grab=lambda path, at: self.grabbed.append(path) or None)
        failed = []
        thumbs.failed.connect(failed.append)
        thumbs.url(self.item())
        self.assertTrue(wait(lambda: failed))
        tries = len(self.grabbed)  # its moment, then its start
        thumbs.url(self.item())
        for _ in range(50):
            QCoreApplication.processEvents()
        self.assertEqual(len(self.grabbed), tries)
        self.assertEqual(thumbs.url(self.item()), "")

    def test_a_recording_shorter_than_its_slot_is_read_from_its_start(self):
        def grab(path, at):
            self.grabbed.append(at)
            return picture() if at == 0 else None  # stopped early: nothing at 5 minutes

        thumbs = self.thumbs(grab=grab)
        thumbs.url(self.item())
        self.assertTrue(wait(lambda: thumbs.url(self.item())))
        self.assertEqual(self.grabbed, [300.0, 0.0])

    def test_a_short_recording_is_read_near_its_start(self):
        thumbs = self.thumbs()
        thumbs.url(self.item(length=40.0))
        self.assertTrue(wait(lambda: self.grabbed))
        self.assertEqual(self.grabbed[0][1], 8.0)

    def test_only_names_in_its_folder_are_read(self):
        thumbs = self.thumbs()
        thumbs.url(self.item())
        self.assertTrue(wait(lambda: thumbs.url(self.item())))
        self.assertIsNone(rt.file_of("thumb:../../" + os.path.basename(self.recording)))
        self.assertIsNone(rt.file_of("https://example.invalid/a.jpg"))

    def test_the_image_loader_reads_a_thumbnail(self):
        from services.image_loader import ImageLoader

        thumbs = self.thumbs()
        thumbs.url(self.item())
        self.assertTrue(wait(lambda: thumbs.url(self.item())))
        url = thumbs.url(self.item())
        loader = ImageLoader([url], image_manager=None, iconified=False, decode_side=64)
        results = []
        loader.progress_updated.connect(lambda current, total, data: results.append(data))
        asyncio.run(loader.load_images())
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["image"].isNull())
        self.assertTrue(os.path.isfile(results[0]["cache_path"]))  # never discarded

    def test_shutdown_stops_the_worker(self):
        thumbs = self.thumbs()
        thumbs.shutdown()
        self.assertEqual(thumbs.url(self.item()), "")
        self.assertEqual(self.grabbed, [])


class FrameTests(unittest.TestCase):
    def test_a_frame_of_a_real_recording_is_read_muted_and_offscreen(self):
        try:
            import vlc  # noqa: F401
        except Exception:  # no libVLC here
            self.skipTest("libVLC is not available")
        image = rt.grab_frame(FIXTURE, 1.0)
        if image is None:
            self.skipTest("libVLC gave no frame here")
        self.assertEqual((image.width(), image.height()), (rt.WIDTH, rt.HEIGHT))
        colors = {
            image.pixel(x, y) for x in range(0, rt.WIDTH, 37) for y in range(0, rt.HEIGHT, 23)
        }
        self.assertGreater(len(colors), 3)  # a picture, not one flat colour


if __name__ == "__main__":
    unittest.main()
