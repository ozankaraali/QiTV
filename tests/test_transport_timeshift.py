from datetime import datetime, timezone
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QWidget

from ui.overlays.transport import Transport, fmt_behind
from ui.theme import PALETTE

# 19:42 local time, as an aware datetime: the buffer below starts 58 minutes earlier (18:44).
NOW = datetime(2026, 10, 15, 19, 42).astimezone()
PROGRAMME = [(960.0, "19:00 Wild Coasts")]


def timeline(**changes):
    state = {
        "start": 0.0,
        "end": 3480.0,
        "position": 3442.0,
        "live_edge": 3480.0,
        "lag": 38.0,
        "mode": "shifted",
        "rate": 1.0,
        "paused": False,
        "ended": False,
    }
    state.update(changes)
    return state


def at_edge(lag=0.9, rate=1.0):
    return timeline(position=3480.0 - lag, lag=lag, mode="live", rate=rate)


class Recorder(QWidget):
    """Stands in for the OverlayHost: records every press that falls through an overlay."""

    def __init__(self):
        super().__init__()
        self.presses = 0

    def mousePressEvent(self, event):
        self.presses += 1


class TimeShiftCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make(self, idle_ms=3000, now=lambda: NOW, parent=None):
        t = Transport(parent, idle_ms=idle_ms, now=now)
        t.setGeometry(0, 0, 1280, 720)
        t.show()
        t.grab()  # warm up font loading so timer tests are not skewed by first paint
        self.addCleanup(t.close)
        return t

    def shifted(self, **kw):
        t = self.make(**kw)
        t.show_timeshift("104 · Nature One HD", "Wild Coasts")
        t.update_timeline(timeline(), PROGRAMME)
        return t

    def click(self, t, point):
        QTest.mouseClick(t, Qt.LeftButton, Qt.NoModifier, point)

    # ---- texts --------------------------------------------------------------------------------

    def test_behind_live_format(self):
        self.assertEqual(fmt_behind(38), "−00:38")
        self.assertEqual(fmt_behind(38.9), "−00:38")
        self.assertEqual(fmt_behind(0), "−00:00")
        self.assertEqual(fmt_behind(-2.5), "−00:00")
        self.assertEqual(fmt_behind(605), "−10:05")
        self.assertEqual(fmt_behind(3725), "−1:02:05")

    def test_shifted_texts(self):
        t = self.shifted()
        self.assertEqual(t.texts(), ("Buffer from 18:44", "−00:38 behind live"))

    def test_buffer_start_is_local_wall_clock(self):
        for now in (NOW.astimezone(timezone.utc), datetime(2026, 10, 15, 19, 42)):
            t = self.make(now=lambda now=now: now)
            t.show_timeshift("104", "Nature")
            t.update_timeline(timeline(), [])
            self.assertEqual(t.texts()[0], "Buffer from 18:44", now)

    # ---- seeking ------------------------------------------------------------------------------

    def test_click_at_buffer_midpoint_seeks_there(self):
        t = self.make()
        t.show_timeshift("104", "Nature")
        t.update_timeline(
            timeline(start=100.0, end=400.0, live_edge=400.0, position=350.0, lag=50.0), []
        )
        to = QSignalSpy(t.seekToRequested)
        ms = QSignalSpy(t.seekRequested)
        r = t.seek_rect()
        self.click(t, QPoint(round(r.center().x()), round(r.center().y())))
        self.assertEqual(to.count(), 1)
        self.assertAlmostEqual(to.at(0)[0], 250.0, delta=1.0)
        self.assertEqual(ms.count(), 0)

    def test_drag_previews_lag_and_survives_idle(self):
        t = self.shifted(idle_ms=60)
        spy = QSignalSpy(t.seekToRequested)
        r = t.seek_rect()
        y = round(r.center().y())
        QTest.mousePress(t, Qt.LeftButton, Qt.NoModifier, QPoint(round(r.right() - 2), y))
        QTest.mouseMove(t, QPoint(round(r.center().x()), y))
        self.assertEqual(t.texts()[1], "−29:00 behind live")
        QTest.qWait(250)
        self.assertTrue(t.isVisible())
        t.update_timeline(at_edge(), PROGRAMME)  # caught up mid-drag: the bar stays until release
        self.assertIn("seek", t._layout())
        QTest.mouseRelease(t, Qt.LeftButton, Qt.NoModifier, QPoint(round(r.center().x()), y))
        self.assertEqual(spy.count(), 1)
        self.assertAlmostEqual(spy.at(0)[0], 1740.0, delta=6.0)
        self.assertTrue(t.isVisible())
        QTest.qWait(250)
        self.assertFalse(t.isVisible())

    def test_drag_anchors_on_the_bar_while_the_buffer_slides(self):
        t = self.shifted()
        spy = QSignalSpy(t.seekToRequested)
        r = t.seek_rect()
        x, y = round(r.center().x()), round(r.center().y())
        frac = (x - r.x()) / r.width()
        QTest.mousePress(t, Qt.LeftButton, Qt.NoModifier, QPoint(x, y))
        # The ring evicts and grows by 120 s while the button is held.
        t.update_timeline(
            timeline(start=120.0, end=3600.0, live_edge=3600.0, position=3562.0), PROGRAMME
        )
        target = 120.0 + frac * 3480.0  # the same spot on the bar, not the same moment
        self.assertEqual(t.texts()[1], f"{fmt_behind(3600.0 - target)} behind live")
        QTest.mouseRelease(t, Qt.LeftButton, Qt.NoModifier, QPoint(x, y))
        self.assertEqual(spy.count(), 1)
        self.assertAlmostEqual(spy.at(0)[0], target, places=6)

    # ---- buttons ------------------------------------------------------------------------------

    def test_go_live_chip_only_when_behind(self):
        t = self.shifted()
        spy = QSignalSpy(t.goLiveRequested)
        self.click(t, t.button_rect("golive").center().toPoint())
        self.assertEqual(spy.count(), 1)
        self.assertGreater(t.button_rect("subtitles").x(), t.button_rect("golive").right())
        # Shifted or catching up (a chosen speed) always shows the chip, close to live or not.
        t.update_timeline(timeline(position=3477.0, lag=3.0), PROGRAMME)
        self.assertIn("seek", t._layout())
        self.assertIn("golive", t._layout())
        t.update_timeline(timeline(position=3479.0, lag=1.0, mode="catching_up"), PROGRAMME)
        self.assertIn("golive", t._layout())
        # Stay-live far behind the target is still live: the edge row, no bar, no chip.
        t.update_timeline(at_edge(lag=5.0, rate=1.10), PROGRAMME)
        self.assertNotIn("seek", t._layout())
        self.assertNotIn("golive", t._layout())
        t.update_timeline(at_edge(), PROGRAMME)
        self.assertNotIn("golive", t._layout())
        self.assertNotIn("seek", t._layout())

    def test_timeshift_buttons_emit(self):
        t = self.shifted()
        start = QSignalSpy(t.programmeStartRequested)
        self.click(t, t.button_rect("start").center().toPoint())
        self.assertEqual(start.count(), 1)
        rel = QSignalSpy(t.seekRelativeRequested)
        for name in ("back10", "fwd10", "fwd60"):
            self.click(t, t.button_rect(name).center().toPoint())
        self.assertEqual([rel.at(i)[0] for i in range(3)], [-10_000, 10_000, 60_000])
        speed = QSignalSpy(t.speedRequested)
        self.click(t, t.button_rect("speed").center().toPoint())
        self.assertEqual(speed.at(0)[0], 1.25)
        play = QSignalSpy(t.playPauseRequested)
        self.click(t, t.button_rect("play").center().toPoint())
        self.assertEqual(play.count(), 1)
        left_to_right = ["start", "back10", "play", "fwd10", "fwd60", "speed"]
        xs = [t.button_rect(name).x() for name in left_to_right]
        self.assertEqual(xs, sorted(xs))

    def test_speed_chip_follows_the_session_rate(self):
        t = self.shifted()
        t.update_timeline(timeline(mode="catching_up", rate=1.5), PROGRAMME)
        speed = QSignalSpy(t.speedRequested)
        self.click(t, t.button_rect("speed").center().toPoint())
        self.assertEqual(speed.at(0)[0], 2.0)
        t.update_timeline(at_edge(lag=4.0, rate=1.05), PROGRAMME)  # stay-live's nudge, not a choice
        self.assertNotIn("speed", t._layout())  # the edge row has no speed chip
        t.update_timeline(timeline(position=3476.0, lag=4.0), PROGRAMME)  # the nudge is not kept
        self.click(t, t.button_rect("speed").center().toPoint())
        self.assertEqual(speed.at(1)[0], 1.25)

    def test_clicks_on_the_bar_and_chips_do_not_fall_through(self):
        host = Recorder()
        host.resize(1280, 720)
        host.show()
        self.addCleanup(host.close)
        t = self.make(parent=host)
        t.show_timeshift("104", "Nature")
        t.update_timeline(timeline(), PROGRAMME)
        for point in (
            t.seek_rect().center().toPoint(),
            t.button_rect("golive").center().toPoint(),
            t.button_rect("speed").center().toPoint(),
            t.button_rect("start").center().toPoint(),
        ):
            self.click(t, point)
        self.assertEqual(host.presses, 0)

    # ---- live edge ----------------------------------------------------------------------------

    def test_live_edge_shows_the_indicator_instead_of_the_bar(self):
        t = self.make()
        t.show_timeshift("104", "Nature")
        t.update_timeline(at_edge(), PROGRAMME)
        self.assertEqual(t.texts(), ("LIVE", "Stay live ● 0.9 s behind source"))
        layout = t._layout()
        self.assertNotIn("seek", layout)
        self.assertNotIn("golive", layout)
        self.assertLess(layout["indicator"].right(), layout["live"].x())
        play = QSignalSpy(t.playPauseRequested)
        self.click(t, t.button_rect("play").center().toPoint())
        self.assertEqual(play.count(), 1)

    def test_indicator_shows_catching_up_while_rate_above_one(self):
        t = self.make()
        t.show_timeshift("104", "Nature")
        t.update_timeline(at_edge(lag=3.2, rate=1.05), PROGRAMME)
        self.assertEqual(t.texts()[1], "Stay live ● 3.2 s behind source · catching up 1.05×")
        t.update_timeline(at_edge(lag=2.6, rate=1.0), PROGRAMME)
        self.assertEqual(t.texts()[1], "Stay live ● 2.6 s behind source")
        t.show_live_edge(3.0, True)
        self.assertIn("catching up", t.texts()[1])
        t.show_live_edge(0.9, False)
        self.assertEqual(t.texts(), ("LIVE", "Stay live ● 0.9 s behind source"))

    def dot_colors(self, t):
        """Colours along the middle of the indicator pill, as painted."""
        box = t._layout()["indicator"]
        image = t.grab().toImage()
        y = round(box.center().y() * image.devicePixelRatio())
        xs = range(
            round(box.left() * image.devicePixelRatio()),
            round(box.right() * image.devicePixelRatio()),
        )
        return {image.pixelColor(x, y).name() for x in xs}

    def test_a_stall_at_the_edge_reads_reconnecting_without_the_on_target_dot(self):
        t = self.make()
        t.show_timeshift("104", "Nature")
        t.update_timeline(at_edge(lag=2.6), PROGRAMME)
        self.assertIn(PALETTE.ok.lower(), self.dot_colors(t))
        t.update_timeline(dict(at_edge(lag=2.6, rate=1.05), stalled=True), PROGRAMME)
        self.assertEqual(t.texts(), ("LIVE", "Stay live ● Reconnecting…"))
        colors = self.dot_colors(t)
        self.assertNotIn(PALETTE.ok.lower(), colors)
        self.assertIn(PALETTE.warn.lower(), colors)
        t.update_timeline(dict(at_edge(lag=2.6), stalled=False), PROGRAMME)
        self.assertEqual(t.texts()[1], "Stay live ● 2.6 s behind source")

    def test_a_stall_while_shifted_keeps_the_bar(self):
        t = self.shifted()
        t.update_timeline(timeline(stalled=True), PROGRAMME)
        self.assertEqual(t.texts(), ("Buffer from 18:44", "−00:38 behind live"))

    def test_show_live_edge_drops_the_bar(self):
        t = self.shifted()
        t.show_live_edge(1.2, False)
        self.assertNotIn("seek", t._layout())
        self.assertEqual(t.texts(), ("LIVE", "Stay live ● 1.2 s behind source"))

    def test_live_mode_shows_the_edge_row_whatever_the_lag(self):
        # After Go live on a long-GOP source the lag sits above the target while stay-live closes it.
        t = self.shifted()
        t.update_timeline(at_edge(lag=20.0, rate=1.10), PROGRAMME)
        layout = t._layout()
        self.assertNotIn("seek", layout)
        self.assertNotIn("golive", layout)
        self.assertEqual(t.texts(), ("LIVE", "Stay live ● 20.0 s behind source · catching up 1.1×"))
        t.update_timeline(at_edge(lag=20.0), PROGRAMME)
        self.assertNotIn("seek", t._layout())
        self.assertNotIn("catching up", t.texts()[1])

    def test_shifted_mode_shows_the_bar_even_when_close_to_live(self):
        t = self.shifted()
        for mode in ("shifted", "catching_up"):
            t.update_timeline(timeline(position=3479.0, lag=1.0, mode=mode), PROGRAMME)
            self.assertIn("seek", t._layout(), mode)
            self.assertIn("golive", t._layout(), mode)

    def test_drag_keeps_the_bar_in_live_mode(self):
        t = self.make()
        t.show_timeshift("104", "Nature")
        t.update_timeline(at_edge(lag=1.0), PROGRAMME)
        t._drag = 0.5
        t.update_timeline(at_edge(lag=1.0), PROGRAMME)
        self.assertIn("seek", t._layout())
        t._drag = None
        t.update_timeline(at_edge(lag=1.0), PROGRAMME)
        self.assertNotIn("seek", t._layout())

    def test_non_finite_or_negative_lag_reads_as_zero(self):
        for bad in (float("inf"), float("-inf"), float("nan"), -4.0):
            self.assertEqual(fmt_behind(bad), "−00:00", bad)
        t = self.shifted()
        for bad in (float("inf"), float("nan"), -4.0):
            t.update_timeline(at_edge(lag=bad), PROGRAMME)
            self.assertEqual(t.texts(), ("LIVE", "Stay live ● 0.0 s behind source"), bad)
            self.assertFalse(t.grab().isNull())
            t.update_timeline(timeline(lag=bad), PROGRAMME)
            self.assertEqual(t.texts()[1], "−00:00 behind live", bad)
            self.assertFalse(t.grab().isNull())
        t.show_live_edge(float("inf"), False)
        self.assertEqual(t.texts(), ("LIVE", "Stay live ● 0.0 s behind source"))

    def test_tick_labels_elide_and_never_overlap(self):
        t = self.make()
        t.show_timeshift("104", "Nature")
        long_title = "19:00 " + "A Very Long Documentary About Coastlines " * 4
        ticks = [
            (960.0, long_title),
            (1000.0, "19:01 Short Break"),  # 40 s later: a few pixels to the right
            (2760.0, "19:30 News"),
        ]
        t.update_timeline(timeline(), ticks)
        marks = t.tick_marks()
        self.assertEqual(len(marks), 3)  # every programme keeps its tick
        (x0, text0, box0), (x1, text1, box1), (x2, text2, box2) = marks
        self.assertLess(x0, x1)
        self.assertTrue(text0.startswith("19:00 A Very") and text0.endswith("…"), text0)
        self.assertLessEqual(box0.width(), round(1280 * 0.25) + 1)
        self.assertIsNone(box1)  # the later of two overlapping labels is dropped
        self.assertEqual(text1, "")
        self.assertEqual(text2, "19:30 News")
        self.assertGreater(box2.x(), box0.right())
        self.assertFalse(t.grab().isNull())

    def test_new_channel_forgets_the_old_timeline(self):
        t = self.shifted()
        t.show_timeshift("104 · Nature One HD", "Coasts at night")
        self.assertEqual(t.texts()[0], "Buffer from 18:44")  # same channel, new programme
        t.show_timeshift("105 · Science", "Stars")
        self.assertEqual(t.texts(), ("LIVE", ""))  # nothing measured yet on this channel
        self.assertNotIn("indicator", t._layout())

    def test_plain_modes_leave_timeshift(self):
        t = self.shifted()
        t.show_live("104", "Nature")
        self.assertEqual(t.texts(), ("LIVE", ""))
        self.assertNotIn("start", t._layout())
        self.assertNotIn("indicator", t._layout())
        t.show_vod("A", "b")
        t.update_position(30_000, 120_000)
        self.assertEqual(t.texts(), ("0:30", "-1:30"))
        self.assertNotIn("golive", t._layout())

    def test_grab_renders_both_states(self):
        t = self.shifted()
        self.assertFalse(t.grab().isNull())
        t.update_timeline(at_edge(), PROGRAMME)
        self.assertFalse(t.grab().isNull())
        t.update_timeline(at_edge(lag=3.2, rate=1.05), PROGRAMME)
        self.assertFalse(t.grab().isNull())
        t.update_timeline(timeline(start=0.0, end=0.0, live_edge=0.0, position=0.0, lag=0.0), [])
        self.assertFalse(t.grab().isNull())  # nothing buffered yet


if __name__ == "__main__":
    unittest.main()
