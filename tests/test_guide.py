from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QPoint, QPointF, Qt, Signal
from PySide6.QtGui import QFont, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMainWindow, QWidget
from shiboken6 import isValid
from timer_fixtures import elapse

from services.epg_view import Programme
from ui.keymap import key_hint
from ui.overlay_host import OverlayHost
from ui.overlays.guide import (
    CHANNEL_U,
    HEADER_U,
    REPAINT_MS,
    ROW_U,
    TOP_U,
    Guide,
    box_span,
    footer_programme,
    programme_spans,
    wrap_lines,
)
from ui.video_surface import VideoSurface

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
        self.calls.append((channel.get("id"), start, limit))
        if self.result is not None:
            return self.result(channel, start)
        return [Programme("Show", start - HALF_HOUR, start + HALF_HOUR, "About the show.")]


def wheel(widget, dy, dx=0, modifiers=Qt.KeyboardModifier.NoModifier):
    event = QWheelEvent(
        QPointF(5, 5),
        QPointF(5, 5),
        QPoint(0, 0),
        QPoint(dx, dy),
        Qt.MouseButton.NoButton,
        modifiers,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(widget, event)


def close_window(window):
    if isValid(window):
        window.close()
        window.deleteLater()


def prog(title, start, minutes):
    return Programme(title, start, start + timedelta(minutes=minutes))


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

    def test_visible_channels_are_the_rows_on_screen(self):
        chans = channels(100)
        self.guide.open_guide(chans, 0)
        shown = self.guide.visible_channels()
        self.assertGreater(len(shown), 1)
        self.assertLess(len(shown), len(chans))
        self.assertEqual(shown, chans[: len(shown)])

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

    def test_failed_lookups_are_retried_and_logged_once(self):
        def result(channel, start):
            if channel["id"] == "1":
                raise RuntimeError("EPG reloading")
            return []

        lookups = Lookups(result)
        self.guide = Guide(None, lookups, lambda: NOW, self.host)
        self.guide.open_guide(channels(3), 0)
        with self.assertLogs("ui.overlays.guide", "DEBUG") as logs:
            self.guide.grab()
            self.guide.grab()
        self.assertEqual([key for key, _, _ in lookups.calls].count("1"), 2)
        self.assertEqual([key for key, _, _ in lookups.calls].count("0"), 1)
        self.assertEqual([r.levelname for r in logs.records].count("ERROR"), 1)

    def test_invalidate_looks_the_visible_rows_up_again(self):
        self.guide.open_guide(channels(1000), 500)
        self.guide.grab()
        first = len(self.lookups.calls)
        self.guide.invalidate()
        self.guide.grab()
        self.assertEqual(len(self.lookups.calls), 2 * first)

    def test_channels_without_a_key_are_cached_per_row(self):
        def result(channel, start):
            return [prog("News " + channel["number"], start, 30)]

        lookups = Lookups(result)
        self.guide = Guide(None, lookups, lambda: NOW, self.host)
        self.guide.open_guide([{"number": "1"}, {"number": "2"}], 0)
        self.guide.grab()
        self.guide.grab()
        self.assertEqual(len(lookups.calls), 2)

    def test_a_naive_clock_is_treated_as_local_time(self):
        naive = datetime(2026, 10, 15, 19, 42)
        self.guide = Guide(None, self.lookups, lambda: naive, self.host)
        self.guide.open_guide(channels(3), 0)
        start = self.guide.window_start()
        self.assertIsNotNone(start.tzinfo)
        self.assertEqual(start.astimezone().replace(tzinfo=None), datetime(2026, 10, 15, 19, 30))
        self.assertFalse(self.guide.grab().isNull())
        self.guide.shift(1)
        self.assertEqual(self.guide.window_start(), start + HALF_HOUR)
        self.assertFalse(self.guide.grab().isNull())

    def test_repaint_timer_runs_only_while_open_and_keeps_the_window(self):
        self.guide.open_guide(channels(3), 0)
        start = self.guide.window_start()
        self.assertTrue(self.guide._repaint.isActive())
        self.assertEqual(self.guide._repaint.interval(), REPAINT_MS)
        self.guide._repaint.timeout.emit()
        self.assertEqual(self.guide.window_start(), start)
        self.guide.close_guide()
        self.assertFalse(self.guide._repaint.isActive())

    def test_escape_closes_and_emits_closed(self):
        self.guide.open_guide(channels(3), 0)
        self.assertTrue(self.guide.escape())
        self.assertFalse(self.guide.isVisible())
        self.assertEqual(self.closed, [True])
        self.guide.close_guide()
        self.assertEqual(self.closed, [True])  # already closed: no second signal

    def painted(self):
        """Process events; the rows and footers the guide painted meanwhile."""
        rows, footers = [], []
        paint_row, paint_footer = self.guide._paint_row, self.guide._paint_footer

        def row_spy(p, layout, row, rect, now):
            rows.append(row)
            return paint_row(p, layout, row, rect, now)

        def footer_spy(p, layout, now):
            footers.append(True)
            return paint_footer(p, layout, now)

        self.guide._paint_row, self.guide._paint_footer = row_spy, footer_spy
        try:
            elapse(10)
            QApplication.processEvents()
        finally:
            del self.guide._paint_row, self.guide._paint_footer
        return rows, footers

    def test_a_move_on_screen_repaints_only_the_two_rows_and_the_footer(self):
        logos = []
        self.logos.request = lambda urls: logos.extend(urls)
        self.guide.open_guide(channels(100), 50)
        rows, _ = self.painted()
        visible = sorted(set(rows))
        self.assertIn(50, visible)
        layout = self.guide._layout()
        # The row the footer cuts shares pixels with it: it repaints with the footer.
        cut = {self.guide._top + layout.full_rows()}
        self.guide.move_by(1)
        rows, footers = self.painted()
        self.assertEqual(sorted(set(rows) - cut), [50, 51])
        self.assertTrue(footers)
        self.guide.move_by(-1)
        self.assertEqual(sorted(set(self.painted()[0]) - cut), [50, 51])
        self.guide.move_by(40)  # off screen: the rows scroll, all of them repaint
        rows, _ = self.painted()
        self.assertEqual(len(rows), len(visible))
        self.assertIn(90, rows)

    def test_a_logo_repaints_only_its_rows(self):
        chans = channels(100)
        self.guide.open_guide(chans, 50)
        self.painted()
        self.logos.logoReady.emit(chans[52]["logo"])
        rows, footers = self.painted()
        self.assertEqual(rows, [52])
        self.assertFalse(footers)
        self.logos.logoReady.emit("http://elsewhere/logo.png")
        self.assertEqual(self.painted(), ([], []))

    def test_follows_host_resize(self):
        self.guide.open_guide(channels(3), 0)
        self.host.resize(960, 540)
        self.assertEqual(self.guide.geometry(), self.host.rect())


class GuideMouseTests(unittest.TestCase):
    """The guide consumes the mouse: nothing reaches the overlay host (drawer, zapping)."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = QMainWindow()
        self.surface = VideoSurface()
        self.window.setCentralWidget(self.surface)
        self.window.setGeometry(100, 100, 800, 450)
        self.window.show()
        elapse(20)
        self.host = OverlayHost(self.window, self.surface)
        self.addCleanup(close_window, self.window)
        self.guide = Guide(None, Lookups(), lambda: NOW, self.host)
        self.host.add_overlay(self.guide)
        self.chans = channels(30)
        self.guide.open_guide(self.chans, 0)
        elapse(20)
        self.host_events: list[object] = []
        self.host.clicked.connect(lambda: self.host_events.append("clicked"))
        self.host.doubleClicked.connect(lambda: self.host_events.append("doubleClicked"))
        self.host.wheelSteps.connect(lambda n, m: self.host_events.append(("wheel", n)))
        self.tuned: list[dict] = []
        self.guide.tuneRequested.connect(self.tuned.append)

    def row_point(self, row):
        u = self.guide.width() / 100
        y = (TOP_U + HEADER_U + ROW_U * (row + 0.5)) * u
        return QPoint(self.guide.width() // 2, round(y))

    def wait_past_double_click(self):
        elapse(QApplication.doubleClickInterval() + 100)

    def test_the_guide_fills_the_host(self):
        self.assertTrue(self.guide.isVisible())
        self.assertEqual(self.guide.geometry(), self.host.rect())

    def test_click_selects_the_row_under_the_pointer(self):
        left = Qt.MouseButton.LeftButton
        QTest.mouseClick(self.guide, left, Qt.KeyboardModifier.NoModifier, self.row_point(3))
        self.assertIs(self.guide.current_channel(), self.chans[3])
        QTest.mouseClick(self.guide, left, Qt.KeyboardModifier.NoModifier, QPoint(10, 2))
        self.assertIs(self.guide.current_channel(), self.chans[3])  # the header: no row
        self.wait_past_double_click()
        self.assertEqual(self.host_events, [])
        self.assertEqual(self.tuned, [])

    def test_double_click_tunes_the_row(self):
        QTest.mouseDClick(
            self.guide, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, self.row_point(5)
        )
        self.assertEqual(self.tuned, [self.chans[5]])
        self.wait_past_double_click()
        self.assertEqual(self.host_events, [])

    def test_wheel_moves_rows_and_shift_or_sideways_moves_time(self):
        start = self.guide.window_start()
        wheel(self.guide, -120)
        self.assertIs(self.guide.current_channel(), self.chans[1])
        wheel(self.guide, 120)
        self.assertIs(self.guide.current_channel(), self.chans[0])
        wheel(self.guide, -120, modifiers=Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(self.guide.window_start(), start + HALF_HOUR)
        wheel(self.guide, 0, dx=-120)
        self.assertEqual(self.guide.window_start(), start + 2 * HALF_HOUR)
        self.assertIs(self.guide.current_channel(), self.chans[0])
        self.assertEqual(self.host_events, [])


class GuideHeaderTests(unittest.TestCase):
    """The owner's mouse-only rule (2.0.1): the guide's keys have buttons in its header."""

    BUTTONS = ("earlier", "later", "search", "fullscreen", "close")

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.host = QWidget()
        self.host.resize(1280, 720)
        self.host.show()
        self.addCleanup(self.host.deleteLater)
        self.addCleanup(self.host.close)
        self.guide = Guide(None, Lookups(), lambda: NOW, self.host)
        self.chans = channels(30)
        self.guide.open_guide(self.chans, 3)
        self.tuned: list[dict] = []
        self.guide.tuneRequested.connect(self.tuned.append)

    def click(self, name, double=False):
        point = self.guide.header_buttons()[name].center().toPoint()
        left, none = Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier
        if double:  # as a mouse does it: press, release, then a press that is a double-click
            host = self.host.windowHandle()
            QTest.mouseDClick(host, left, none, self.guide.mapTo(self.host, point))
        else:
            QTest.mouseClick(self.guide, left, none, point)

    def test_the_header_has_its_buttons_each_naming_its_key(self):
        buttons = self.guide.header_buttons()
        self.assertEqual(list(buttons), list(self.BUTTONS))
        u = self.guide.width() / 100
        header_bottom = (TOP_U + HEADER_U) * u
        for name, (what, keys) in zip(
            self.BUTTONS,
            (
                ("Earlier", "Left"),
                ("Later", "Right"),
                ("Search", "Ctrl+F"),
                ("Fullscreen", "F"),
                ("Close the guide", "G"),
            ),
        ):
            with self.subTest(name=name):
                rect = buttons[name]
                self.assertGreaterEqual(min(rect.width(), rect.height()), 28)
                self.assertLessEqual(rect.bottom(), header_bottom)
                self.assertEqual(self.guide.tooltip_at(rect.center()), key_hint(what, keys))
        # ‹ › beside the day, in the channels' column; ⌕ ⛶ × at the right end, in that order.
        self.assertLessEqual(buttons["later"].right(), CHANNEL_U * u)
        self.assertLess(buttons["earlier"].right(), buttons["later"].left())
        self.assertLess(buttons["search"].right(), buttons["fullscreen"].left())
        self.assertLess(buttons["fullscreen"].right(), buttons["close"].left())
        self.assertLessEqual(buttons["close"].right(), self.guide.width())

    def test_earlier_and_later_move_the_time_window(self):
        start = self.guide.window_start()
        self.click("earlier")  # already the current half hour
        self.assertEqual(self.guide.window_start(), start)
        self.click("later")
        self.assertEqual(self.guide.window_start(), start + HALF_HOUR)
        self.click("earlier")
        self.assertEqual(self.guide.window_start(), start)
        self.assertIs(self.guide.current_channel(), self.chans[3])  # the rows stay
        self.assertEqual(self.tuned, [])

    def test_a_quick_second_click_on_later_counts(self):
        start = self.guide.window_start()
        self.click("later", double=True)
        self.assertEqual(self.guide.window_start(), start + 2 * HALF_HOUR)

    def test_earlier_is_dimmed_when_nothing_is_earlier(self):
        earlier = self.guide.header_buttons()["earlier"].toAlignedRect()
        at_start = self.guide.grab().toImage().copy(earlier)
        self.click("later")
        self.assertNotEqual(self.guide.grab().toImage().copy(earlier), at_start)

    def test_a_quick_second_click_on_fullscreen_asks_once(self):
        fullscreens = []
        self.guide.fullscreenRequested.connect(lambda: fullscreens.append(True))
        self.click("fullscreen", double=True)
        self.assertEqual(fullscreens, [True])  # not in and straight out again

    def test_search_and_fullscreen_ask_the_shell(self):
        searches, fullscreens = [], []
        self.guide.searchRequested.connect(lambda: searches.append(True))
        self.guide.fullscreenRequested.connect(lambda: fullscreens.append(True))
        self.click("search")
        self.click("fullscreen")
        self.assertEqual((searches, fullscreens), ([True], [True]))
        self.assertTrue(self.guide.isVisible())  # the shell decides what follows
        self.assertEqual(self.tuned, [])

    def test_close_closes_the_guide(self):
        closed = []
        self.guide.closed.connect(lambda: closed.append(True))
        self.click("close")
        self.assertFalse(self.guide.isVisible())
        self.assertEqual(closed, [True])
        self.assertEqual(self.tuned, [])

    def test_the_buttons_fit_a_small_guide(self):
        for width in (360, 640, 960):
            self.host.resize(width, round(width * 9 / 16))
            with self.subTest(width=width):
                buttons = list(self.guide.header_buttons().values())
                for i, a in enumerate(buttons):
                    self.assertGreaterEqual(a.left(), 0)
                    self.assertLessEqual(a.right(), width)
                    for b in buttons[i + 1 :]:
                        self.assertFalse(a.intersects(b))
                self.assertFalse(self.guide.grab().isNull())


class ProgrammeSpansTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 10, 15, 19, 30, tzinfo=timezone.utc)

    def spans(self, programmes, min_width=10.0):
        result = programme_spans(programmes, self.start, 0.0, 500.0, min_width)
        return [(s.programme.title, s.x, s.width) for s in result]

    def test_adjacent_programmes_sit_side_by_side(self):
        a, b = prog("A", self.start, 30), prog("B", self.start + HALF_HOUR, 30)
        self.assertEqual(self.spans([b, a]), [("A", 0.0, 100.0), ("B", 100.0, 100.0)])

    def test_an_overlap_ends_where_the_next_programme_starts(self):
        a, b = prog("A", self.start, 60), prog("B", self.start + HALF_HOUR, 60)
        result = programme_spans([a, b], self.start, 0.0, 500.0, 10.0)
        self.assertEqual(self.spans([a, b]), [("A", 0.0, 100.0), ("B", 100.0, 200.0)])
        self.assertEqual(result[0].stop, self.start + HALF_HOUR)

    def test_equal_starts_draw_one_box(self):
        a, b = prog("A", self.start, 30), prog("B", self.start, 60)
        self.assertEqual(self.spans([a, b]), [("B", 0.0, 200.0)])

    def test_slivers_narrower_than_the_minimum_are_dropped(self):
        sliver = prog("Sliver", self.start - timedelta(minutes=29), 30)  # 1 min in the window
        rest = prog("Rest", self.start + timedelta(minutes=1), 29)
        self.assertEqual([t for t, _, _ in self.spans([sliver, rest])], ["Rest"])


class FooterProgrammeTests(unittest.TestCase):
    def test_on_now_wins(self):
        on_now = prog("On now", NOW - timedelta(minutes=12), 30)
        later = prog("Later", on_now.stop, 30)
        self.assertIs(footer_programme([on_now, later], NOW), on_now)

    def test_first_upcoming_when_nothing_is_on_now(self):
        ended = prog("Ended", NOW - timedelta(minutes=42), 30)
        upcoming = prog("Upcoming", NOW + timedelta(minutes=18), 60)
        later = prog("Later", upcoming.stop, 60)
        self.assertIs(footer_programme([ended, upcoming, later], NOW), upcoming)
        self.assertIsNone(footer_programme([ended], NOW))
        self.assertIsNone(footer_programme([], NOW))


_LOCAL_TIME_SCRIPT = """
import json
from datetime import datetime, timedelta, timezone
from services.epg_view import Programme
from ui.overlays.guide import day_label, fmt_span, slot_start
now = datetime(2026, 10, 15, 19, 42, tzinfo=timezone.utc)
slot = slot_start(now)
print(json.dumps({
    "slot_utc": slot.astimezone(timezone.utc).strftime("%H:%M"),
    "span": fmt_span(Programme("Show", slot, slot + timedelta(hours=1))),
    "days": [day_label(slot + timedelta(hours=h), now) for h in (0, 12, 13, 24)],
}))
"""


@unittest.skipIf(sys.platform == "win32", "POSIX TZ strings")
class LocalTimeTests(unittest.TestCase):
    """Slots, times and day labels follow the local zone, not UTC (run in a child process)."""

    def run_in_zone(self, tz):
        env = dict(os.environ, TZ=tz, QT_QPA_PLATFORM="offscreen")
        done = subprocess.run(
            [sys.executable, "-c", _LOCAL_TIME_SCRIPT],
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout.strip().splitlines()[-1])

    def test_quarter_hour_zone_ahead_of_utc(self):
        # UTC+5:45: 19:42 UTC is 01:27 on the 16th, so the slot is 01:00 local (19:15 UTC).
        self.assertEqual(
            self.run_in_zone("NPT-05:45"),
            {
                "slot_utc": "19:15",
                "span": "01:00 – 02:00",
                "days": ["Today", "Today", "Today", "Tomorrow"],
            },
        )

    def test_zone_behind_utc(self):
        # UTC-8: 19:42 UTC is 11:42 on the 15th; 12 h later is 23:30 local, still today.
        self.assertEqual(
            self.run_in_zone("PST8"),
            {
                "slot_utc": "19:30",
                "span": "11:30 – 12:30",
                "days": ["Today", "Today", "Tomorrow", "Tomorrow"],
            },
        )


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
