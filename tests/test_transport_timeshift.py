from datetime import datetime, timezone
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QWidget
from timer_fixtures import elapse

from ui.overlays.transport import GAP_PX, Transport, fmt_behind
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
        elapse(250)
        self.assertTrue(t.isVisible())
        t.update_timeline(at_edge(), PROGRAMME)  # caught up mid-drag: the bar stays until release
        self.assertIn("seek", t._layout())
        QTest.mouseRelease(t, Qt.LeftButton, Qt.NoModifier, QPoint(round(r.center().x()), y))
        self.assertEqual(spy.count(), 1)
        self.assertAlmostEqual(spy.at(0)[0], 1740.0, delta=6.0)
        self.assertTrue(t.isVisible())
        elapse(250)
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
        self.click(t, t.button_rect("faster").center().toPoint())
        self.click(t, t.button_rect("slower").center().toPoint())
        self.assertEqual([speed.at(0)[0], speed.at(1)[0]], [1.25, 1.0])
        play = QSignalSpy(t.playPauseRequested)
        self.click(t, t.button_rect("play").center().toPoint())
        self.assertEqual(play.count(), 1)
        left_to_right = ["start", "back10", "play", "fwd10", "fwd60", "slower", "speed", "faster"]
        xs = [t.button_rect(name).x() for name in left_to_right]
        self.assertEqual(xs, sorted(xs))

    def test_speed_chip_follows_the_session_rate(self):
        t = self.shifted()
        t.update_timeline(timeline(mode="catching_up", rate=1.5), PROGRAMME)
        speed = QSignalSpy(t.speedRequested)
        self.click(t, t.button_rect("faster").center().toPoint())
        self.assertEqual(speed.at(0)[0], 2.0)
        t.update_timeline(at_edge(lag=4.0, rate=1.05), PROGRAMME)  # stay-live's nudge, not a choice
        self.assertNotIn("speed", t._layout())  # the edge row has no speed chip
        t.update_timeline(timeline(position=3476.0, lag=4.0), PROGRAMME)  # the nudge is not kept
        self.click(t, t.button_rect("faster").center().toPoint())
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
            t.button_rect("slower").center().toPoint(),
            t.button_rect("speed").center().toPoint(),
            t.button_rect("faster").center().toPoint(),
            t.button_rect("start").center().toPoint(),
        ):
            self.click(t, point)
        self.assertEqual(host.presses, 0)

    # ---- live edge ----------------------------------------------------------------------------

    def test_live_edge_shows_the_indicator_instead_of_the_bar(self):
        t = self.make()
        t.show_timeshift("104", "Nature")
        t.update_timeline(at_edge(), PROGRAMME)
        self.assertEqual(t.texts(), ("LIVE", "Stay live ● ≈ 1 s behind source"))
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
        self.assertEqual(t.texts()[1], "Stay live ● ≈ 3 s behind source · catching up 1.05×")
        t.update_timeline(at_edge(lag=2.6, rate=1.0), PROGRAMME)
        self.assertEqual(t.texts()[1], "Stay live ● ≈ 2.5 s behind source")

    def test_the_indicator_shows_the_settled_lag_and_the_nudge_not_its_ramp(self):
        # 2.1: the session's "shown" lag (settled, in half seconds) is what the row reads, and
        # a nudge reads as the rate it ramps to while the rate steps there and back.
        t = self.make()
        t.show_timeshift("104", "Nature")
        t.update_timeline(dict(at_edge(lag=7.3), shown=7.0), PROGRAMME)
        self.assertEqual(t.texts()[1], "Stay live ● ≈ 7 s behind source")
        for rate in (1.01, 1.02, 1.03, 1.02, 1.01):
            t.update_timeline(dict(at_edge(lag=8.6, rate=rate), shown=8.5, nudge=1.03), PROGRAMME)
            self.assertEqual(t.texts()[1], "Stay live ● ≈ 8.5 s behind source · catching up 1.03×")
        t.update_timeline(dict(at_edge(lag=3.1, rate=0.98), shown=3.0, nudge=0.95), PROGRAMME)
        self.assertEqual(t.texts()[1], "Stay live ● ≈ 3 s behind source · adding buffer 0.95×")
        t.update_timeline(dict(at_edge(lag=7.3, rate=1.0), shown=7.5, nudge=1.0), PROGRAMME)
        self.assertEqual(t.texts()[1], "Stay live ● ≈ 7.5 s behind source")

    def test_the_dot_is_green_only_at_normal_speed(self):
        # 2.1: amber whenever stay-live's rate is not 1x, a slower one (0.95x) included.
        t = self.make()
        t.show_timeshift("104", "Nature")
        for rate, nudge, colour in (
            (1.0, 1.0, PALETTE.ok),
            (0.95, 0.95, PALETTE.warn),
            (0.99, 0.95, PALETTE.warn),  # ramping out of a nudge: still amber
            (1.03, 1.03, PALETTE.warn),
            (1.0, 1.0, PALETTE.ok),
        ):
            with self.subTest(rate=rate, nudge=nudge):
                t.update_timeline(dict(at_edge(lag=6.0, rate=rate), nudge=nudge), PROGRAMME)
                colors = self.dot_colors(t)
                self.assertIn(colour.lower(), colors)
                other = PALETTE.warn if colour == PALETTE.ok else PALETTE.ok
                self.assertNotIn(other.lower(), colors)

    def test_auto_shows_the_lag_and_says_when_its_target_was_adjusted(self):
        # 2.0.2: under Auto latency the row shows the measured lag, and "adjusted" once the
        # channel's Auto target moved from where Auto starts it.
        t = self.make()
        t.show_timeshift("104", "Nature")
        auto = {"auto": True, "target": 2.5, "adjusted": False}
        t.update_timeline(dict(at_edge(lag=2.6), **auto), PROGRAMME)
        self.assertEqual(t.texts(), ("LIVE", "Stay live ● ≈ 2.5 s behind"))
        auto = {"auto": True, "target": 4.5, "adjusted": True}
        t.update_timeline(dict(at_edge(lag=7.2), **auto), PROGRAMME)
        self.assertEqual(t.texts()[1], "Stay live ● ≈ 7 s behind · adjusted")
        t.update_timeline(dict(at_edge(lag=7.0, rate=1.05), **auto), PROGRAMME)
        self.assertEqual(t.texts()[1], "Stay live ● ≈ 7 s behind · adjusted · catching up 1.05×")
        t.update_timeline(dict(at_edge(lag=4.9), stalled=True, **auto), PROGRAMME)
        self.assertEqual(t.texts()[1], "Stay live ● Waiting for the channel…")
        t.update_timeline(dict(at_edge(lag=4.9), stalled=True, reconnecting=True), PROGRAMME)
        self.assertEqual(t.texts()[1], "Stay live ● Reconnecting…")
        t.update_timeline(dict(at_edge(lag=2.6), target=2.5), PROGRAMME)  # a fixed latency
        self.assertEqual(t.texts()[1], "Stay live ● ≈ 2.5 s behind source")

    def test_a_narrow_transport_shortens_the_auto_text_as_the_others(self):
        t = self.make()
        state = dict(at_edge(lag=4.9), auto=True, target=4.5, adjusted=True)
        t.show_timeshift("104", "Nature")
        t.update_timeline(state, PROGRAMME)
        whole = t._layout()["indicator"].width()
        t.setGeometry(0, 0, 480, 270)
        layout = t._layout()
        box = layout["indicator"]
        self.assertLess(box.width(), whole)  # shortened: the tail is painted elided
        self.assertLessEqual(box.right(), layout["live"].left())
        self.assertGreaterEqual(box.left(), layout["back10"].right())

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

    def test_a_stall_at_the_edge_reads_waiting_without_the_on_target_dot(self):
        t = self.make()
        t.show_timeshift("104", "Nature")
        t.update_timeline(at_edge(lag=2.6), PROGRAMME)
        self.assertIn(PALETTE.ok.lower(), self.dot_colors(t))
        t.update_timeline(dict(at_edge(lag=2.6, rate=1.05), stalled=True), PROGRAMME)
        self.assertEqual(t.texts(), ("LIVE", "Stay live ● Waiting for the channel…"))
        colors = self.dot_colors(t)
        self.assertNotIn(PALETTE.ok.lower(), colors)
        self.assertIn(PALETTE.warn.lower(), colors)
        t.update_timeline(dict(at_edge(lag=2.6), stalled=False), PROGRAMME)
        self.assertEqual(t.texts()[1], "Stay live ● ≈ 2.5 s behind source")

    def test_a_stall_while_shifted_keeps_the_bar(self):
        t = self.shifted()
        t.update_timeline(timeline(stalled=True), PROGRAMME)
        self.assertEqual(t.texts(), ("Buffer from 18:44", "−00:38 behind live"))

    def test_live_mode_shows_the_edge_row_whatever_the_lag(self):
        # After Go live on a long-GOP source the lag sits above the target while stay-live closes it.
        t = self.shifted()
        t.update_timeline(at_edge(lag=20.0, rate=1.10), PROGRAMME)
        layout = t._layout()
        self.assertNotIn("seek", layout)
        self.assertNotIn("golive", layout)
        self.assertEqual(t.texts(), ("LIVE", "Stay live ● ≈ 20 s behind source · catching up 1.1×"))
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
            self.assertEqual(t.texts(), ("LIVE", "Stay live ● ≈ 0 s behind source"), bad)
            self.assertFalse(t.grab().isNull())
            t.update_timeline(timeline(lag=bad), PROGRAMME)
            self.assertEqual(t.texts()[1], "−00:00 behind live", bad)
            self.assertFalse(t.grab().isNull())

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

    # ---- catch-up (2.2) -----------------------------------------------------------------------

    def test_a_bar_into_the_archive_keeps_the_buffers_start_and_dashes_the_archive(self):
        t = self.make()
        t.show_timeshift("104", "Nature")
        # The buffer starts at 1740 (18:44 + 29 min); 29 min of archive before it.
        t.update_timeline(timeline(buffer_start=1740.0), PROGRAMME)
        self.assertEqual(t.texts(), ("Buffer from 19:13", "−00:38 behind live"))
        self.assertAlmostEqual(t.archive_fraction(), 0.5)
        self.assertFalse(t.grab().isNull())
        t.update_timeline(timeline(), PROGRAMME)
        self.assertEqual(t.archive_fraction(), 0.0)

    def test_the_archive_playing_reads_from_when_and_its_bubble_names_another_day(self):
        t = self.make()
        t.show_timeshift("104", "Nature")
        t.update_timeline(timeline(archive=True), PROGRAMME)
        self.assertEqual(t.texts(), ("Archive from 18:44", "−00:38 behind live"))
        self.assertEqual(t.archive_fraction(), 1.0)
        self.assertEqual(t.bubble_text(), "19:41:22")
        self.assertIn("golive", t._layout())  # Go live leads back
        two_days = 2 * 86400.0
        t.update_timeline(
            timeline(
                archive=True,
                start=0.0,
                position=100.0,
                lag=two_days,
                end=two_days + 100,
                live_edge=two_days + 100,
            ),
            [],
        )
        self.assertTrue(t.bubble_text().endswith("19:42:00"))
        self.assertGreater(len(t.bubble_text()), len("19:42:00"))  # the day's name before it
        self.assertFalse(t.grab().isNull())


if __name__ == "__main__":
    unittest.main()


# 2.6: a kept buffer of two parts: 30 s kept at 16:42, then 90 s since 19:40 (now 19:42).
W0 = NOW.timestamp() - 3 * 3600
W1 = NOW.timestamp() - 90
PARTED = timeline(
    start=0.0,
    end=120.0,
    live_edge=120.0,
    position=10.0,
    lag=110.0,
    parts=[[0, 0.0, W0], [15, 30.0, W1]],
    behind=NOW.timestamp() - (W0 + 10.0),
)


class GapTests(TimeShiftCase):
    """2.6: the bar shows where nothing was kept, and the real times on either side."""

    def parted(self):
        t = self.make()
        t.show_timeshift("104", "Nature")
        t.update_timeline(PARTED, [])
        return t

    def test_buffer_from_is_the_oldest_parts_time(self):
        self.assertEqual(self.parted().texts()[0], "Buffer from 16:42")

    def test_behind_live_counts_the_time_away(self):
        self.assertEqual(self.parted().texts()[1], "−2:59:50 behind live")

    def test_the_bubble_shows_the_moment_watched(self):
        self.assertEqual(self.parted().bubble_text(), "16:42:10")

    def test_the_gap_has_its_room_on_the_bar(self):
        t = self.parted()
        width = t.seek_rect().width()
        jump = t.fraction_of(30.0) - t.fraction_of(29.999)
        self.assertAlmostEqual(jump * width, GAP_PX, delta=0.5)
        self.assertAlmostEqual(t.fraction_of(0.0), 0.0)
        self.assertAlmostEqual(t.fraction_of(120.0), 1.0)

    def test_a_click_in_the_gap_seeks_to_the_next_part(self):
        t = self.parted()
        to = QSignalSpy(t.seekToRequested)
        r = t.seek_rect()
        x = r.x() + r.width() * t.fraction_of(30.0) - GAP_PX / 2
        self.click(t, QPoint(round(x), round(r.center().y())))
        self.assertEqual(to.count(), 1)
        self.assertEqual(to.at(0)[0], 30.0)

    def test_a_click_either_side_maps_within_its_part(self):
        t = self.parted()
        to = QSignalSpy(t.seekToRequested)
        r = t.seek_rect()
        x = r.x() + r.width() * t.fraction_of(75.0)
        self.click(t, QPoint(round(x), round(r.center().y())))
        self.assertAlmostEqual(to.at(0)[0], 75.0, delta=0.5)

    def test_one_part_is_the_bar_as_before(self):
        t = self.make()
        t.show_timeshift("104", "Nature")
        t.update_timeline(timeline(parts=[[0, 0.0, W0]], behind=12345.0), [])
        self.assertEqual(t.texts(), ("Buffer from 18:44", "−00:38 behind live"))
        self.assertAlmostEqual(t.fraction_of(1740.0), 0.5)
