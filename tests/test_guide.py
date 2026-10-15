from datetime import datetime, timedelta, timezone
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QWidget

from services.epg_view import Programme
from ui.overlays.guide import Guide, box_span, wrap_lines

NOW = datetime(2026, 10, 15, 19, 42, tzinfo=timezone.utc)
HALF_HOUR = timedelta(minutes=30)


def channels(n):
    return [
        {"id": str(i), "name": f"Channel {i}", "number": str(100 + i), "logo": f"http://x/{i}.png"}
        for i in range(n)
    ]


class Lookups:
    """A fake `programmes(channel, window_start, limit)` that records its calls."""

    def __init__(self, result=None):
        self.calls = []
        self.result = result

    def __call__(self, channel, start, limit):
        self.calls.append((channel["id"], start, limit))
        if self.result is not None:
            return self.result(channel, start)
        return [Programme("Show", start - HALF_HOUR, start + HALF_HOUR, "About the show.")]


class FakeLogos(QObject):
    logoReady = Signal(str)

    def __init__(self):
        super().__init__()
        self.requested = []

    def request(self, urls):
        self.requested.extend(urls)

    def pixmap(self, url):
        return None


class GuideTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.host = QWidget()
        self.host.resize(1280, 720)
        self.host.show()
        self.addCleanup(self.host.deleteLater)
        self.addCleanup(self.host.close)
        self.lookups = Lookups()
        self.logos = FakeLogos()
        self.guide = Guide(self.logos, self.lookups, lambda: NOW, self.host)
        self.tuned: list[dict] = []
        self.closed: list[bool] = []
        self.guide.tuneRequested.connect(self.tuned.append)
        self.guide.closed.connect(lambda: self.closed.append(True))

    def test_open_sets_current_row_and_move_by_changes_channel(self):
        chans = channels(5)
        self.guide.open_guide(chans, 2)
        self.assertTrue(self.guide.isVisible())
        self.assertEqual(self.guide.geometry(), self.host.rect())
        self.assertIs(self.guide.current_channel(), chans[2])
        self.guide.move_by(1)
        self.assertIs(self.guide.current_channel(), chans[3])
        self.guide.move_by(10)
        self.assertIs(self.guide.current_channel(), chans[4])
        self.guide.move_by(-10)
        self.assertIs(self.guide.current_channel(), chans[0])

    def test_open_without_channels_has_no_current_channel(self):
        self.guide.open_guide([], -1)
        self.guide.move_by(1)
        self.guide.activate()
        self.assertIsNone(self.guide.current_channel())
        self.assertEqual(self.tuned, [])
        self.assertFalse(self.guide.grab().isNull())

    def test_page_moves_by_the_visible_rows(self):
        chans = channels(100)
        self.guide.open_guide(chans, 0)
        self.guide.page(1)
        moved = chans.index(self.guide.current_channel())
        self.assertGreater(moved, 1)
        self.guide.page(-1)
        self.assertIs(self.guide.current_channel(), chans[0])

    def test_activate_emits_tune_requested_with_current_channel(self):
        chans = channels(5)
        self.guide.open_guide(chans, 1)
        self.guide.move_by(1)
        self.guide.activate()
        self.assertEqual(self.tuned, [chans[2]])

    def test_window_starts_at_the_current_half_hour(self):
        self.guide.open_guide(channels(3), 0)
        start = self.guide.window_start()
        self.assertIsNotNone(start.tzinfo)
        self.assertLessEqual(start, NOW)
        self.assertLess(NOW, start + HALF_HOUR)
        self.assertEqual((start.second, start.microsecond), (0, 0))
        self.assertIn(start.astimezone().minute, (0, 30))

    def test_shift_is_clamped_to_now_slot_and_a_day_ahead(self):
        self.guide.open_guide(channels(3), 0)
        start = self.guide.window_start()
        self.guide.shift(-1)
        self.assertEqual(self.guide.window_start(), start)
        self.guide.shift(1)
        self.assertEqual(self.guide.window_start(), start + HALF_HOUR)
        self.guide.shift(1000)
        self.assertLessEqual(self.guide.window_start(), NOW + timedelta(hours=24))
        self.assertEqual(self.guide.window_start(), start + timedelta(hours=24))
        self.guide.shift(1)
        self.assertEqual(self.guide.window_start(), start + timedelta(hours=24))
        self.guide.shift(-1000)
        self.assertEqual(self.guide.window_start(), start)

    def test_grab_queries_only_the_visible_rows(self):
        chans = channels(1000)
        self.guide.open_guide(chans, 500)
        self.assertEqual(self.lookups.calls, [])  # nothing is looked up before painting
        self.assertFalse(self.guide.grab().isNull())
        calls = self.lookups.calls
        self.assertGreater(len(calls), 0)
        self.assertLessEqual(len(calls), 20)
        self.assertIn("500", {key for key, _, _ in calls})
        self.assertEqual(
            {(start, limit) for _, start, limit in calls}, {(self.guide.window_start(), 12)}
        )
        self.assertLessEqual(len(self.logos.requested), 20)

    def test_lookups_are_cached_until_shift_or_reopen(self):
        chans = channels(1000)
        self.guide.open_guide(chans, 500)
        self.guide.grab()
        first = len(self.lookups.calls)
        self.guide.grab()
        self.assertEqual(len(self.lookups.calls), first)
        self.guide.shift(1)
        self.guide.grab()
        self.assertEqual(len(self.lookups.calls), 2 * first)
        self.assertEqual(self.lookups.calls[-1][1], self.guide.window_start())
        self.guide.open_guide(chans, 500)
        self.guide.grab()
        self.assertEqual(len(self.lookups.calls), 3 * first)

    def test_moving_far_queries_the_new_current_row(self):
        chans = channels(1000)
        self.guide.open_guide(chans, 0)
        self.guide.grab()
        self.guide.move_by(600)
        self.guide.grab()
        self.assertIn("600", {key for key, _, _ in self.lookups.calls})
        self.assertLessEqual(len(self.lookups.calls), 40)

    def test_rows_without_programmes_and_failing_lookups_still_paint(self):
        def result(channel, start):
            if channel["id"] == "1":
                raise RuntimeError("EPG reloading")
            if channel["id"] == "2":  # entirely after the window
                return [Programme("Later", start + timedelta(hours=5), start + timedelta(hours=6))]
            return []

        self.guide = Guide(None, Lookups(result), lambda: NOW, self.host)
        self.guide.open_guide(channels(4), 0)
        with self.assertLogs("ui.overlays.guide", "ERROR"):
            self.assertFalse(self.guide.grab().isNull())

    def test_escape_closes_and_emits_closed(self):
        self.guide.open_guide(channels(3), 0)
        self.assertTrue(self.guide.escape())
        self.assertFalse(self.guide.isVisible())
        self.assertEqual(self.closed, [True])
        self.guide.close_guide()
        self.assertEqual(self.closed, [True])  # already closed: no second signal

    def test_follows_host_resize(self):
        self.guide.open_guide(channels(3), 0)
        self.host.resize(960, 540)
        self.assertEqual(self.guide.geometry(), self.host.rect())


class WrapLinesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_long_text_is_cut_to_two_lines_with_an_ellipsis(self):
        font = QFont()
        font.setPixelSize(12)
        lines = wrap_lines("word " * 200, font, 200.0, 2)
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[1].endswith("…"))
        self.assertEqual(wrap_lines("Short\nand  sweet", font, 200.0, 2), ["Short and sweet"])
        self.assertEqual(wrap_lines("", font, 200.0, 2), [])


class BoxSpanTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 10, 15, 19, 30, tzinfo=timezone.utc)

    def test_a_half_hour_programme_is_one_fifth_of_the_time_area(self):
        span = box_span(self.start, self.start + HALF_HOUR, self.start, 100.0, 500.0)
        self.assertEqual(span, (100.0, 100.0))
        later = self.start + HALF_HOUR
        span = box_span(later, later + HALF_HOUR, self.start, 100.0, 500.0)
        self.assertEqual(span, (200.0, 100.0))

    def test_programmes_are_clipped_to_the_window(self):
        span = box_span(
            self.start - timedelta(minutes=15), self.start + HALF_HOUR, self.start, 0.0, 500.0
        )
        self.assertEqual(span, (0.0, 100.0))
        end = self.start + timedelta(hours=2)
        span = box_span(end, end + timedelta(hours=2), self.start, 0.0, 500.0)
        self.assertEqual(span, (400.0, 100.0))

    def test_programmes_outside_the_window_have_no_box(self):
        self.assertIsNone(box_span(self.start - HALF_HOUR, self.start, self.start, 0.0, 500.0))
        after = self.start + timedelta(hours=2, minutes=30)
        self.assertIsNone(box_span(after, after + HALF_HOUR, self.start, 0.0, 500.0))


if __name__ == "__main__":
    unittest.main()
