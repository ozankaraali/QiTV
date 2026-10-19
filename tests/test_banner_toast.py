from datetime import datetime, timedelta, timezone
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from timer_fixtures import timeline, wait_until

from services.epg_view import Programme
from ui.overlays.toast import Toast
from ui.overlays.zap_banner import BannerInfo, ZapBanner, banner_texts

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
    def test_content_top_bounds_what_it_paints(self):
        # A bottom-corner PiP rises above content_top() while the banner shows (F1): nothing
        # but the scrim (one colour per row) may be painted above it.
        b = ZapBanner(self.host)
        for epg in (True, False):
            with self.subTest(epg=epg):
                b.show_info(info(epg))
                image = b.grab().toImage()
                top = b.content_top() - b.y()
                self.assertGreater(top, 0)

                def plain(y):
                    first = image.pixel(0, y)
                    return all(image.pixel(x, y) == first for x in range(image.width()))

                self.assertTrue(all(plain(y) for y in range(top)))
                first_painted = next(y for y in range(top, image.height()) if not plain(y))
                if epg:  # the tallest row: it starts right at content_top()
                    self.assertLess(first_painted - top, round(1280 * 0.02))

    def test_show_info_makes_visible(self):
        b = ZapBanner(self.host)
        b.show_info(info())
        self.assertTrue(b.isVisible())
        self.assertEqual(b.info.name, "Nature One HD")
        self.assertEqual(b.height(), round(720 * 0.48))
        self.assertEqual(b.width(), 1280)

    def test_programme_times_are_local_for_a_guide_in_another_zone(self):
        # The banner showed the guide's own zone ("17:15 – 18:15" at 20:03 local).
        local_now = datetime(2026, 10, 15, 20, 3).astimezone()
        guide_zone = timezone(local_now.utcoffset() + timedelta(hours=3))

        def guide(title, start_min, length=60):
            start = (local_now + timedelta(minutes=start_min)).astimezone(guide_zone)
            return Programme(title=title, start=start, stop=start + timedelta(minutes=length))

        texts = banner_texts(
            BannerInfo(
                number="7",
                name="News",
                logo=None,
                now=guide("Headlines", -48),
                next=guide("Weather", 12, 15),
                upcoming=[guide("Sport", 27)],
                clock=local_now.astimezone(guide_zone),
            )
        )
        self.assertEqual(texts.clock, "20:03")
        self.assertEqual(texts.now_span, " · 19:15 – 20:15")
        self.assertEqual(texts.next_line, "Next  20:15 Weather  ·  20:30 Sport")

    def test_hides_after_timer(self):
        b = ZapBanner(self.host, visible_ms=60)
        b.show_info(info())
        self.assertTrue(b.isVisible())
        self.assertTrue(wait_until(b.isHidden))  # 60 ms, then a 250 ms fade

    def test_digits_keep_visible(self):
        b = ZapBanner(self.host, visible_ms=60)
        b.show_info(info())
        b.show_digits("4")
        twin = ZapBanner(self.host, visible_ms=60)  # shown after b, without digits
        twin.show_info(info())
        self.assertTrue(wait_until(twin.isHidden))
        self.assertTrue(b.isVisible())

    def test_empty_digits_restart_timer(self):
        b = ZapBanner(self.host, visible_ms=60)
        b.show_info(info())
        b.show_digits("4")
        b.show_digits("")
        self.assertTrue(wait_until(b.isHidden))

    def test_grab_with_and_without_epg(self):
        for epg in (True, False):
            b = ZapBanner(self.host)
            b.show_info(info(epg))
            self.assertFalse(b.grab().isNull())

    def test_default_clock_is_aware_and_grabs_with_epg(self):
        b = ZapBanner(self.host)
        now = datetime.now().astimezone()
        epg = BannerInfo(
            number="1",
            name="X",
            logo=None,
            now=Programme("A", now - timedelta(minutes=10), now + timedelta(minutes=50)),
            next=None,
        )
        self.assertIsNotNone(epg.clock.tzinfo)
        b.show_info(epg)
        self.assertFalse(b.grab().isNull())

    def test_empty_digits_without_info_does_nothing(self):
        b = ZapBanner(self.host)
        b.show_digits("")
        self.assertFalse(b.isVisible())
        b.show_info(info())
        b.dismiss()
        b.show_digits("")
        self.assertFalse(b.isVisible())

    def test_many_digits_grab(self):
        b = ZapBanner(self.host)
        b.show_info(info())
        b.show_digits("10428")
        self.assertFalse(b.grab().isNull())

    def test_resizes_with_parent(self):
        b = ZapBanner(self.host)
        b.show_info(info())
        self.host.resize(800, 400)
        QTest.qWait(10)
        self.assertEqual((b.width(), b.height()), (800, round(400 * 0.48)))


class HiddenHostTest(Base):
    """An overlay shown while its host is hidden lays out again when the host resizes."""

    def hidden_host(self):
        host = QWidget()
        host.resize(400, 300)  # a stale size: the real one arrives before the host shows
        self.addCleanup(host.close)
        return host

    def test_toast_relayouts_when_the_hidden_host_is_resized(self):
        host = self.hidden_host()
        t = Toast(host)
        t.show_message("Add a provider in Settings to start watching")
        host.resize(1280, 720)  # pending until shown, then sent before the host is visible
        host.show()
        self.assertAlmostEqual(t.geometry().center().x(), 640, delta=2)

    def test_banner_relayouts_when_the_hidden_host_is_resized(self):
        host = self.hidden_host()
        b = ZapBanner(host)
        b.show_info(info())
        host.resize(1280, 720)
        host.show()
        self.assertEqual((b.width(), b.height()), (1280, round(720 * 0.48)))


class ToastTest(Base):
    def test_hides(self):
        t = Toast(self.host)
        t.show_message("x", ms=60)
        self.assertTrue(t.isVisible())
        self.assertTrue(wait_until(t.isHidden))

    def test_new_message_restarts_timer(self):
        t = Toast(self.host)
        t.show_message("one", ms=150)
        _, visible = timeline(
            (100, lambda: t.show_message("two", ms=300)),  # before the first one's 150 ms
            (250, t.isVisible),  # past the first one's 150 ms, before the second one's 400
        )
        self.assertTrue(visible)
        self.assertTrue(wait_until(t.isHidden))

    def test_long_message_fits_host(self):
        t = Toast(self.host)
        t.show_message("w" * 500)
        self.assertGreaterEqual(t.x(), 0)
        self.assertLessEqual(t.width(), self.host.width())

    def test_stays_on_top(self):
        self.assertTrue(Toast.stays_on_top)

    def test_width_grows_with_text(self):
        t = Toast(self.host)
        t.show_message("hi")
        short = t.width()
        t.show_message("a much longer message here", kind="error")
        self.assertGreater(t.width(), short)
        self.assertFalse(t.grab().isNull())


if __name__ == "__main__":
    unittest.main()
