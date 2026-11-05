"""The Recordings home's guide (2.9.7): rows of channels, a NOW line, marks, timer bands, a card
to record from, and only what shows looked up."""

from datetime import datetime, timedelta
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
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


if __name__ == "__main__":
    unittest.main()
