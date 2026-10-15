import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest
from unittest import mock

from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtWidgets import QApplication

from ui.logos import LogoCache
from ui.overlays import painting
from ui.overlays.painting import (
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
    "live_dot",
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
        from datetime import datetime

        self.assertEqual(fmt_clock(datetime(2026, 1, 2, 9, 5)), "09:05")

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
        path = os.path.join(os.environ.get("TMPDIR", "/tmp"), "qitv_logo_test.png")
        pix.save(path)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
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
            cache._on_progress(instance, 1, 1, {"rank": 0, "cache_path": path})
        self.assertEqual(ready, ["http://x/a.png"])
        self.assertIsNotNone(cache.pixmap("http://x/a.png"))
        # unseen request urls are not re-requested
        with mock.patch("ui.logos.ImageLoader") as loader, mock.patch("ui.logos.ThreadCleanup"):
            cache._on_cleanup(None)
            self.assertEqual(loader.call_args.args[0], ["http://x/b.png"])


if __name__ == "__main__":
    unittest.main()
