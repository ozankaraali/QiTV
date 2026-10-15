from datetime import datetime, timedelta, timezone
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from services.epg_view import Programme
from ui.overlays.toast import Toast
from ui.overlays.zap_banner import BannerInfo, ZapBanner

NOW = datetime(2026, 10, 15, 19, 42, tzinfo=timezone.utc)


def prog(title, start_min, length=60):
    start = NOW + timedelta(minutes=start_min)
    return Programme(title=title, start=start, stop=start + timedelta(minutes=length))


def info(epg=True):
    return BannerInfo(
        number="104",
        name="Nature One HD",
        logo=None,
        now=prog("Wild Coasts", -42) if epg else None,
        next=prog("Ocean Giants", 18) if epg else None,
        upcoming=[prog("Night Shift", 78)] if epg else [],
        is_live=True,
        clock=NOW,
    )


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.host = QWidget()
        self.host.resize(1280, 720)
        self.host.show()
        self.addCleanup(self.host.close)


class BannerTest(Base):
    def test_show_info_makes_visible(self):
        b = ZapBanner(self.host)
        b.show_info(info())
        self.assertTrue(b.isVisible())
        self.assertEqual(b.info.name, "Nature One HD")
        self.assertEqual(b.height(), round(720 * 0.48))
        self.assertEqual(b.width(), 1280)

    def test_hides_after_timer(self):
        b = ZapBanner(self.host, visible_ms=60)
        b.show_info(info())
        QTest.qWait(400)
        self.assertFalse(b.isVisible())

    def test_digits_keep_visible(self):
        b = ZapBanner(self.host, visible_ms=60)
        b.show_info(info())
        b.show_digits("4")
        QTest.qWait(400)
        self.assertTrue(b.isVisible())

    def test_empty_digits_restart_timer(self):
        b = ZapBanner(self.host, visible_ms=60)
        b.show_info(info())
        b.show_digits("4")
        b.show_digits("")
        QTest.qWait(400)
        self.assertFalse(b.isVisible())

    def test_grab_with_and_without_epg(self):
        for epg in (True, False):
            b = ZapBanner(self.host)
            b.show_info(info(epg))
            self.assertFalse(b.grab().isNull())

    def test_resizes_with_parent(self):
        b = ZapBanner(self.host)
        b.show_info(info())
        self.host.resize(800, 400)
        QTest.qWait(10)
        self.assertEqual((b.width(), b.height()), (800, round(400 * 0.48)))


class ToastTest(Base):
    def test_hides(self):
        t = Toast(self.host)
        t.show_message("x", ms=60)
        self.assertTrue(t.isVisible())
        QTest.qWait(300)
        self.assertFalse(t.isVisible())

    def test_width_grows_with_text(self):
        t = Toast(self.host)
        t.show_message("hi")
        short = t.width()
        t.show_message("a much longer message here", kind="error")
        self.assertGreater(t.width(), short)
        self.assertFalse(t.grab().isNull())


if __name__ == "__main__":
    unittest.main()
