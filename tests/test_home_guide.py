"""The Recordings home's guide (2.9.7): rows of channels, a NOW line, marks, timer bands, a card
to record from, and only what shows looked up."""

from datetime import datetime, timedelta
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from services.epg_view import Programme
from ui.library.home_guide import HomeGuide

app = QApplication.instance() or QApplication([])
NOW = datetime(2026, 11, 5, 19, 47).astimezone()
CHANNELS = [{"id": str(n), "name": f"Channel {n}"} for n in range(6)]


def half_hours(channel, start, limit):
    """Half-hour programmes named "<channel> <hh:mm>" from the half hour holding `start`."""
    first = start.replace(minute=start.minute - start.minute % 30, second=0, microsecond=0)
    return [
        Programme(
            f"{channel['name']} {(first + timedelta(minutes=30 * k)):%H:%M}",
            first + timedelta(minutes=30 * k),
            first + timedelta(minutes=30 * (k + 1)),
            "",
            None,
        )
        for k in range(min(limit, 16))
    ]


class HomeGuideTests(unittest.TestCase):
    def setUp(self):
        self.asked = []

        def programmes(channel, start, limit):
            self.asked.append(channel["id"])
            return half_hours(channel, start, limit)

        self.guide = HomeGuide(programmes, lambda: NOW)
        self.addCleanup(self.guide.deleteLater)
        self.guide.resize(1100, 420)
        self.guide.set_channels(CHANNELS)
        self.events = []
        self.guide.recordRequested.connect(
            lambda c, p, s: self.events.append(("record", c["id"], p.title, s))
        )
        self.guide.skipRequested.connect(
            lambda c, p: self.events.append(("skip", c["id"], p.title))
        )
        self.guide.cancelRequested.connect(
            lambda c, p: self.events.append(("cancel", c["id"], p.title))
        )
        self.guide.cancelSeriesRequested.connect(
            lambda c, p: self.events.append(("cancel_series", c["id"], p.title))
        )

    def test_its_window_starts_half_an_hour_before_now(self):
        self.assertEqual(self.guide.window_start(), NOW.replace(minute=0, second=0, microsecond=0))

    def test_only_visible_rows_ask_for_programmes(self):
        many = [{"id": str(n), "name": f"Channel {n}"} for n in range(5000)]
        self.guide.set_channels(many)
        self.asked.clear()
        self.guide.grab()
        self.assertTrue(self.asked)
        self.assertLess(len(set(self.asked)), 20)

    def test_no_guide_data_says_so_in_one_line(self):
        guide = HomeGuide(lambda c, s, n: [], lambda: NOW)
        self.addCleanup(guide.deleteLater)
        guide.resize(1100, 420)
        guide.set_channels(CHANNELS)
        self.assertTrue(guide.empty_text())
        self.assertFalse(guide.grab().isNull())
        guide.set_channels([])
        self.assertTrue(guide.empty_text())

    def test_the_now_line_sits_at_now(self):
        area = self.guide.time_area()
        expected = area.left() + area.width() * 47 / 240  # 19:47 in a 19:00–23:00 window
        self.assertAlmostEqual(self.guide.now_x(), expected, delta=1.0)

    def test_marks_and_bands_paint_where_their_times_are(self):
        self.guide.set_mark_provider(lambda c, p: "series" if p.title == "Channel 1 21:00" else "")
        start = NOW.replace(hour=21, minute=0, second=0, microsecond=0)
        self.guide.set_band_provider(
            lambda c: (
                [(start.timestamp(), (start + timedelta(minutes=90)).timestamp(), "Late film")]
                if c["id"] == "2"
                else []
            )
        )
        box = self.guide.box_rect(1, "Channel 1 21:00")
        area = self.guide.time_area()
        self.assertAlmostEqual(box.left(), area.left() + area.width() * 120 / 240, delta=3.0)
        (band,) = self.guide.band_rects(2)
        self.assertAlmostEqual(band.width(), area.width() * 90 / 240, delta=3.0)
        self.assertEqual(self.guide.mark_of(1, "Channel 1 21:00"), "series")
        self.assertFalse(self.guide.grab().isNull())

    def test_a_click_opens_the_card_and_record_emits(self):
        box = self.guide.box_rect(0, "Channel 0 21:00")
        QTest.mouseClick(self.guide, Qt.LeftButton, Qt.NoModifier, box.center().toPoint())
        self.assertEqual(self.guide.card_buttons(), ["record", "series"])
        self.assertFalse(self.guide.grab().isNull())
        rect = self.guide.card_button_rect("record")
        QTest.mouseClick(self.guide, Qt.LeftButton, Qt.NoModifier, rect.center().toPoint())
        self.assertEqual(self.events, [("record", "0", "Channel 0 21:00", False)])
        self.assertEqual(self.guide.card_buttons(), [])  # it closes

    def test_a_past_programme_opens_no_card(self):
        box = self.guide.box_rect(0, "Channel 0 19:00")
        QTest.mouseClick(self.guide, Qt.LeftButton, Qt.NoModifier, box.center().toPoint())
        self.assertEqual(self.guide.card_buttons(), [])

    def test_a_scheduled_series_airing_offers_skip_and_cancel_series(self):
        self.guide.set_mark_provider(lambda c, p: "series" if p.title == "Channel 0 21:00" else "")
        box = self.guide.box_rect(0, "Channel 0 21:00")
        QTest.mouseClick(self.guide, Qt.LeftButton, Qt.NoModifier, box.center().toPoint())
        self.assertEqual(self.guide.card_buttons(), ["skip", "cancel_series"])
        rect = self.guide.card_button_rect("skip")
        QTest.mouseClick(self.guide, Qt.LeftButton, Qt.NoModifier, rect.center().toPoint())
        self.assertEqual(self.events, [("skip", "0", "Channel 0 21:00")])

    def test_a_one_time_recording_offers_its_cancel(self):
        self.guide.set_mark_provider(lambda c, p: "once" if p.title == "Channel 0 21:00" else "")
        box = self.guide.box_rect(0, "Channel 0 21:00")
        QTest.mouseClick(self.guide, Qt.LeftButton, Qt.NoModifier, box.center().toPoint())
        self.assertEqual(self.guide.card_buttons(), ["cancel"])

    def test_keys_walk_programmes_and_r_records(self):
        self.assertEqual(self.guide.current_title(), "Channel 0 19:30")  # the one on now
        self.guide.move_by(1, 0)
        self.assertEqual(self.guide.current_title(), "Channel 0 20:00")
        self.guide.move_by(0, 1)
        self.assertEqual(self.guide.current_title(), "Channel 1 20:00")
        self.assertTrue(self.guide.record_key())
        self.assertEqual(self.events, [("record", "1", "Channel 1 20:00", False)])
        self.guide.activate()  # Enter opens its card; Esc closes it
        self.assertEqual(self.guide.card_buttons(), ["record", "series"])
        self.assertTrue(self.guide.escape())
        self.assertEqual(self.guide.card_buttons(), [])
        self.assertFalse(self.guide.escape())

    def test_days_step_and_the_source_chip_asks(self):
        asked = []
        self.guide.sourceRequested.connect(asked.append)
        self.guide.set_source_label("★ Favorites")
        labels = [label for label, _day in self.guide.day_choices()]
        self.assertEqual(labels[:2], ["Today", "Tomorrow"])
        self.guide.set_day(1)
        self.assertEqual(self.guide.window_start().date(), (NOW + timedelta(days=1)).date())
        chip = self.guide.source_rect()
        QTest.mouseClick(self.guide, Qt.LeftButton, Qt.NoModifier, chip.center().toPoint())
        self.assertEqual(len(asked), 1)
        self.assertIsInstance(asked[0], QPoint)
        self.guide.set_day(0)
        self.assertEqual(self.guide.window_start(), NOW.replace(minute=0, second=0, microsecond=0))


def wheel(widget, dy, dx=0):
    """A wheel event over `widget` (a trackpad sends small deltas; a mouse 120 a notch)."""
    centre = QPointF(widget.width() / 2, widget.height() / 2)
    event = QWheelEvent(
        centre,
        widget.mapToGlobal(centre),
        QPoint(0, 0),
        QPoint(dx, dy),
        Qt.NoButton,
        Qt.NoModifier,
        Qt.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(widget, event)


class HomeGuideReviewTests(unittest.TestCase):
    """What the Stage 3–4 review found: a window that follows now, a guide arriving late, two
    providers' channels with one id, no rows without a guide, keys to every control, the card
    beside its own airing, a trackpad's scroll."""

    def setUp(self):
        self.clock = [NOW]
        self.guide = HomeGuide(half_hours, lambda: self.clock[0])
        self.addCleanup(self.guide.deleteLater)
        self.guide.resize(1100, 420)
        self.guide.set_channels(CHANNELS)
        self.events = []
        self.guide.recordRequested.connect(
            lambda c, p, s: self.events.append(("record", c["id"], p.title, s))
        )

    def test_the_window_follows_now_until_the_viewer_moves_it(self):
        self.clock[0] = NOW + timedelta(hours=10)  # 05:47 the next morning
        self.guide.follow_now()
        later = (NOW + timedelta(hours=10)).replace(minute=0, second=0, microsecond=0)
        self.assertEqual(
            self.guide.window_start(), later + timedelta(minutes=30) - timedelta(minutes=30)
        )
        self.assertEqual(self.guide.current_title(), "Channel 0 05:30")  # the one on now
        self.guide.shift(1)
        moved = self.guide.window_start()
        self.clock[0] += timedelta(hours=1)
        self.guide.follow_now()
        self.assertEqual(self.guide.window_start(), moved)  # the viewer's window stays
        self.guide.set_day(0)
        self.assertEqual(self.guide.window_start(), later + timedelta(hours=1))

    def test_a_shown_guide_starts_at_now(self):
        self.guide.show()
        self.guide.hide()
        self.clock[0] = NOW + timedelta(hours=3)
        self.guide.show()
        self.addCleanup(self.guide.hide)
        start = (NOW + timedelta(hours=3)).replace(minute=0, second=0, microsecond=0)
        self.assertEqual(self.guide.window_start(), start)

    def test_a_guide_that_arrives_later_shows_without_a_refresh(self):
        ready = [False]
        guide = HomeGuide(lambda c, s, n: half_hours(c, s, n) if ready[0] else [], lambda: NOW)
        self.addCleanup(guide.deleteLater)
        guide.resize(1100, 420)
        guide.set_channels(CHANNELS)
        self.assertTrue(guide.empty_text())
        ready[0] = True
        self.assertEqual(guide.empty_text(), "")

    def test_two_providers_channels_with_one_id_keep_their_own_programmes(self):
        twins = [
            {"id": "12", "name": "North", "_provider": "One"},
            {"id": "12", "name": "South", "_provider": "Two"},
        ]
        self.guide.set_channels(twins)
        self.guide.grab()
        self.assertFalse(self.guide.box_rect(1, "South 20:00").isEmpty())
        self.assertTrue(self.guide.box_rect(1, "North 20:00").isEmpty())

    def test_without_a_guide_no_row_takes_a_click_or_a_key(self):
        guide = HomeGuide(lambda c, s, n: [], lambda: NOW)
        self.addCleanup(guide.deleteLater)
        guide.resize(1100, 420)
        guide.set_channels(CHANNELS)
        tuned = []
        guide.tuneRequested.connect(tuned.append)
        area = guide.time_area()
        QTest.mouseClick(guide, Qt.LeftButton, Qt.NoModifier, QPoint(20, int(area.top() + 10)))
        self.assertEqual(tuned, [])
        self.assertFalse(guide.has_rows())

    def test_up_from_the_rows_reaches_the_days_the_arrows_and_the_source(self):
        asked, ups = [], []
        self.guide.sourceRequested.connect(asked.append)
        self.guide.upEdge.connect(lambda: ups.append(True))
        self.guide.move_by(0, -1)
        self.assertEqual(self.guide.bar_focus(), "day0")  # the day shown
        self.assertEqual(ups, [])
        self.guide.move_by(1, 0)
        self.assertEqual(self.guide.bar_focus(), "day1")
        self.guide.activate()
        self.assertEqual(self.guide.window_start().date(), (NOW + timedelta(days=1)).date())
        for _ in range(10):
            self.guide.move_by(1, 0)
        self.assertEqual(self.guide.bar_focus(), "source")  # the last: it stops there
        self.guide.activate()
        self.assertEqual(len(asked), 1)
        self.guide.move_by(-1, 0)
        self.assertEqual(self.guide.bar_focus(), "forward")
        start = self.guide.window_start()
        self.guide.activate()
        self.assertEqual(self.guide.window_start(), start + timedelta(minutes=30))
        self.guide.move_by(0, 1)  # back to the rows
        self.assertEqual(self.guide.bar_focus(), "")
        self.assertTrue(self.guide.current_title())
        self.guide.move_by(0, -1)
        self.guide.move_by(0, -1)  # above the bar: the page's
        self.assertEqual(ups, [True])

    def test_arrows_move_between_the_card_buttons(self):
        self.guide.move_by(1, 0)  # 20:00, still to come
        self.guide.activate()
        self.assertEqual(self.guide.card_focus(), "record")
        self.guide.move_by(1, 0)
        self.assertEqual(self.guide.card_focus(), "series")
        self.guide.move_by(1, 0)  # the last stays
        self.assertEqual(self.guide.card_focus(), "series")
        self.guide.activate()
        self.assertEqual(self.events, [("record", "0", "Channel 0 20:00", True)])

    def test_page_moves_a_screen_of_rows(self):
        many = [{"id": str(n), "name": f"Channel {n}"} for n in range(40)]
        self.guide.set_channels(many)
        fit = self.guide.rows_fit()
        self.guide.page(1)
        self.assertTrue(self.guide.current_title().startswith(f"Channel {fit} "))
        self.guide.page(-1)
        self.assertTrue(self.guide.current_title().startswith("Channel 0 "))

    def test_the_card_sits_beside_its_own_airing_when_titles_repeat(self):
        def news(channel, start, limit):
            return [
                Programme("News", p.start, p.stop, "", None)
                for p in half_hours(channel, start, limit)
            ]

        guide = HomeGuide(news, lambda: NOW)
        self.addCleanup(guide.deleteLater)
        guide.resize(1100, 420)
        guide.set_channels(CHANNELS)
        area = guide.time_area()
        x = area.left() + area.width() * 135 / 240  # 21:15, the 21:00 airing
        y = guide.box_rect(0, "News").center().y()
        QTest.mouseClick(guide, Qt.LeftButton, Qt.NoModifier, QPointF(x, y).toPoint())
        card = guide.card_rect()
        self.assertGreater(card.left(), area.left() + area.width() * 110 / 240)

    def test_a_trackpad_scroll_moves_by_its_distance(self):
        many = [{"id": str(n), "name": f"Channel {n}"} for n in range(40)]
        self.guide.set_channels(many)
        for _ in range(3):
            wheel(self.guide, -30)  # a quarter notch each: not a row yet
        self.assertEqual(self.guide.top_row(), 0)
        wheel(self.guide, -30)
        self.assertEqual(self.guide.top_row(), 1)
        wheel(self.guide, 0)  # a gesture's start or end
        wheel(self.guide, 0, dx=-240)  # sideways: the window, not the rows
        self.assertEqual(self.guide.top_row(), 1)
        self.assertGreater(
            self.guide.window_start(), NOW.replace(minute=0, second=0, microsecond=0)
        )

    def test_just_after_midnight_today_stays_today_and_the_window_ends_with_the_days(self):
        self.clock[0] = NOW.replace(hour=0, minute=10)
        self.guide.set_day(0)
        self.guide.shift(1)
        self.assertEqual(self.guide.shown_day(), 0)  # Today, though the window began yesterday
        for _ in range(500):
            self.guide.shift(1)
        last = self.guide.window_start() + timedelta(hours=4)
        self.assertLessEqual(last, self.clock[0].replace(hour=0, minute=0) + timedelta(days=4))
        self.assertEqual(self.guide.shown_day(), 3)

    def test_a_long_source_name_leaves_the_days_alone(self):
        self.guide.resize(700, 420)
        self.guide.set_source_label("★ " + "A very long group name " * 4)
        chips = self.guide.chip_rects()
        self.assertGreaterEqual(chips["source"].left(), chips["forward"].right())


if __name__ == "__main__":
    unittest.main()
