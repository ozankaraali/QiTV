from datetime import datetime, timedelta, timezone
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import unittest
from unittest import mock

from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtWidgets import QApplication

from ui.logos import LogoCache
from ui.overlays import painting
from ui.overlays.painting import (
    MIN_FONT_PX,
    Units,
    badge_color,
    draw_icon,
    draw_logo,
    draw_progress,
    elided,
    fmt_clock,
    fmt_duration,
    initials,
)

ICONS = [
    "play",
    "pause",
    "back10",
    "fwd10",
    "prev",
    "next",
    "volume",
    "mute",
    "fullscreen",
    "subtitles",
    "audio",
    "search",
    "star",
    "recent",
    "clock",
    "sparkle",
    "live_dot",
    "gear",
    "plus",
]


def blank() -> QImage:
    image = QImage(64, 64, QImage.Format_ARGB32_Premultiplied)
    image.fill(0)
    return image


def painted(image: QImage) -> bool:
    return any(image.pixelColor(x, y).alpha() > 0 for x in range(64) for y in range(64))


class PaintingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_units(self):
        units = Units(1280)
        self.assertEqual(units.px(2.6), 33)
        self.assertEqual(units.px(0.0), 1)
        self.assertEqual(units.font(2.6).pixelSize(), 33)

    def test_fonts_never_go_below_a_readable_pixel_size(self):
        mini = Units(360)  # the mini player: u = 3.6 px
        self.assertEqual(mini.font(1.4).pixelSize(), MIN_FONT_PX)  # was 5 px
        self.assertGreaterEqual(MIN_FONT_PX, 11)
        self.assertEqual(mini.px(1.4), 5)  # layout sizes still follow u
        self.assertEqual(Units(1280).font(1.4).pixelSize(), 18)  # large enough: unchanged

    def test_initials(self):
        self.assertEqual(initials("Nature One HD"), "NO")
        self.assertEqual(initials("bbc"), "BB")
        self.assertEqual(initials(""), "?")

    def test_badge_color_stable(self):
        self.assertEqual(badge_color("A"), badge_color("A"))
        self.assertIn(badge_color("A").name(), painting._BADGE_COLORS)

    def test_formats(self):
        self.assertEqual(fmt_duration(37_000), "0:37")
        self.assertEqual(fmt_duration(3_725_000), "1:02:05")
        self.assertEqual(fmt_clock(datetime(2026, 1, 2, 9, 5)), "09:05")

    def test_the_clock_shows_local_time_for_a_guide_in_another_zone(self):
        # A UTC guide at 20:03 local read "17:15 – 18:15" on the banner (H16).
        local = datetime(2026, 10, 15, 20, 3).astimezone()
        guide_zone = timezone(local.utcoffset() + timedelta(hours=3))  # never the local zone
        start = local.astimezone(guide_zone)
        self.assertNotEqual(start.strftime("%H:%M"), "20:03")
        self.assertEqual(fmt_clock(start), "20:03")
        self.assertEqual(fmt_clock(datetime(2026, 1, 2, 9, 5)), "09:05")  # naive: as given

    def test_every_icon_paints(self):
        for name in ICONS:
            with self.subTest(icon=name):
                image = blank()
                p = QPainter(image)
                draw_icon(p, name, QRectF(0, 0, 64, 64), QColor("#ffffff"))
                p.end()
                self.assertTrue(painted(image))

    def test_unknown_icon_raises(self):
        image = blank()
        p = QPainter(image)
        with self.assertRaises(KeyError):
            draw_icon(p, "nope", QRectF(0, 0, 64, 64), QColor("#fff"))
        p.end()

    def test_logo_badge_without_pixmap(self):
        image = blank()
        p = QPainter(image)
        draw_logo(p, QRectF(0, 0, 64, 64), "Nature One", None, 8)
        p.end()
        self.assertTrue(painted(image))

    def test_logo_with_pixmap(self):
        pix = QPixmap(10, 10)
        pix.fill(QColor("#ff0000"))
        image = blank()
        p = QPainter(image)
        draw_logo(p, QRectF(0, 0, 64, 64), "X", pix, 8)
        p.end()
        self.assertTrue(painted(image))

    def test_progress_and_elide(self):
        image = blank()
        p = QPainter(image)
        draw_progress(p, QRectF(0, 28, 64, 8), 0.5)
        self.assertEqual(elided(p, "short", 1000), "short")
        self.assertNotEqual(
            elided(p, "a very long string indeed" * 5, 40), "a very long string indeed" * 5
        )
        p.end()
        self.assertTrue(painted(image))


class LogoCacheTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make(self, logos=True):
        config = mock.Mock(channel_logos=logos, ssl_verify=True)
        return LogoCache(mock.Mock(), config)

    def png(self, side):
        pix = QPixmap(side, side)
        pix.fill(QColor("red"))
        handle, path = tempfile.mkstemp(suffix=".png")
        os.close(handle)
        self.addCleanup(os.remove, path)
        pix.save(path)
        return path

    def load(self, cache, url, path):
        cache._batch = [url]
        cache._seen.add(url)
        cache._on_progress(1, 1, {"rank": 0, "cache_path": path})

    def test_large_logo_is_bounded(self):
        cache = self.make()
        self.load(cache, "u", self.png(1000))
        stored = cache.pixmap("u")
        self.assertLessEqual(
            max(stored.width(), stored.height()), 192 * max(1, round(self.app.devicePixelRatio()))
        )

    def test_lru_eviction_and_touch(self):
        cache = self.make()
        path = self.png(4)
        with mock.patch.object(LogoCache, "MAX_BYTES", 3 * 4 * 4 * 4):  # three 4×4 logos
            for name in "abc":
                self.load(cache, name, path)
            cache.pixmap("a")  # touch: b is now least recent
            self.load(cache, "d", path)
        self.assertIsNone(cache.pixmap("b"))
        self.assertIsNotNone(cache.pixmap("a"))
        self.assertIsNotNone(cache.pixmap("d"))

    def test_shutdown_cancels_and_ignores_late_results(self):
        cache = self.make()
        loader = mock.Mock()
        cache._loader = loader
        cache._batch = ["u"]
        cache._queue = ["q"]
        cache.shutdown()
        loader.cancel.assert_called_once()
        cache._on_progress(1, 1, {"rank": 0, "cache_path": self.png(4)})
        self.assertIsNone(cache.pixmap("u"))
        with mock.patch("ui.logos.ImageLoader") as made:
            cache.request(["z"])
        made.assert_not_called()

    def test_failed_url_is_retried_at_most_twice(self):
        cache = self.make()
        for attempt in range(4):
            cache._seen.add("bad")
            cache._batch = ["bad"]
            with mock.patch("ui.logos.ImageLoader"), mock.patch("ui.logos.ThreadCleanup"):
                cache._on_cleanup(None)
            self.assertEqual("bad" in cache._seen, attempt >= 2)

    def test_pixmap_none_before_load(self):
        self.assertIsNone(self.make().pixmap("http://x/a.png"))

    def test_request_noop_when_disabled(self):
        cache = self.make(logos=False)
        with mock.patch("ui.logos.ImageLoader") as loader:
            cache.request(["http://x/a.png"])
        loader.assert_not_called()

    def test_request_batches_and_delivers(self):
        cache = self.make()
        pix = QPixmap(4, 4)
        pix.fill(QColor("red"))
        path = self.png(4)
        ready = []
        cache.logoReady.connect(ready.append)
        with mock.patch("ui.logos.ImageLoader") as loader, mock.patch("ui.logos.ThreadCleanup"):
            cache.request(["http://x/a.png", "", "http://x/a.png"])
            loader.assert_called_once()
            self.assertEqual(loader.call_args.args[0], ["http://x/a.png"])
            self.assertTrue(loader.call_args.kwargs["iconified"])
            # a second request while busy is queued, not started
            cache.request(["http://x/b.png"])
            loader.assert_called_once()
            instance = loader.return_value
            instance.start.assert_called_once()
            cache._on_progress(1, 1, {"rank": 0, "cache_path": path})
        self.assertEqual(ready, ["http://x/a.png"])
        self.assertIsNotNone(cache.pixmap("http://x/a.png"))
        # unseen request urls are not re-requested
        with mock.patch("ui.logos.ImageLoader") as loader, mock.patch("ui.logos.ThreadCleanup"):
            cache._on_cleanup(None)
            self.assertEqual(loader.call_args.args[0], ["http://x/b.png"])


if __name__ == "__main__":
    unittest.main()
