"""Recording from the guide (2.8): the footer's Record and Record series for the programme on
now or one to come (clicked on), R to record it or cancel, and a dot on scheduled ones."""

from datetime import datetime, timedelta, timezone
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from services.epg_view import Programme
from ui.guide_controller import GuideController
from ui.keymap import Action
from ui.overlays.guide import WINDOW, Guide, slot_start
from ui.theme import PALETTE

NOW = datetime(2026, 10, 15, 19, 42, tzinfo=timezone.utc)
CHANNELS = [{"id": "a", "name": "Harbour News", "number": "1"}]


def half_hours(channel, start):
    first = slot_start(NOW) - timedelta(hours=1)
    shows = [
        Programme(
            f"Show {i}", first + i * timedelta(minutes=30), first + (i + 1) * timedelta(minutes=30)
        )
        for i in range(12)
    ]
    return [p for p in shows if p.stop > start][:12]


class GuideRecordingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.host = QWidget()
        self.host.resize(1280, 720)
        self.host.show()
        self.addCleanup(self.host.deleteLater)
        self.addCleanup(self.host.close)
        self.scheduled = set()
        self.guide = Guide(
            None,
            lambda channel, start, limit: half_hours(channel, start),
            lambda: NOW,
            self.host,
            scheduled=lambda channel, programme: programme.title in self.scheduled,
        )
        self.asked, self.cancelled = [], []
        self.guide.recordRequested.connect(
            lambda c, p, series: self.asked.append((p.title, series))
        )
        self.guide.recordCancelled.connect(lambda c, p: self.cancelled.append(p.title))
        self.timers = []
        self.guide.timerRequested.connect(
            lambda c, p: self.timers.append((c["name"], p.title if p else None))
        )
        self.guide.open_guide(CHANNELS, 0)

    def point(self, moment):
        layout = self.guide._layout()
        offset = (moment - self.guide.window_start()).total_seconds() / WINDOW.total_seconds()
        x = layout.times.left() + offset * layout.times.width()
        return QPoint(round(x), round(layout.rows.top() + 0.5 * layout.row_h))

    def click(self, rect):
        QTest.mouseClick(self.guide, Qt.MouseButton.LeftButton, pos=rect.center().toPoint())

    def test_the_programme_on_now_can_be_recorded_from_the_footer(self):
        buttons = self.guide.footer_buttons()
        self.assertEqual(list(buttons), ["live", "record", "series", "timer"])
        self.click(buttons["record"])
        self.assertEqual(self.asked, [("Show 2", False)])  # 19:30–20:00, on at 19:42

    def test_a_programme_to_come_clicked_on_is_the_footers(self):
        later = slot_start(NOW) + timedelta(minutes=45)
        QTest.mouseClick(self.guide, Qt.MouseButton.LeftButton, pos=self.point(later))
        self.assertEqual(self.guide.record_target().title, "Show 3")
        self.click(self.guide.footer_buttons()["series"])
        self.assertEqual(self.asked, [("Show 3", True)])

    def test_a_scheduled_programme_offers_its_cancel_only(self):
        self.scheduled.add("Show 2")
        buttons = self.guide.footer_buttons()
        self.assertEqual(list(buttons), ["live", "cancel", "timer"])
        self.click(buttons["cancel"])
        self.assertEqual(self.cancelled, ["Show 2"])

    def test_a_scheduled_series_airing_offers_skip(self):  # 2.9.7
        self.scheduled.add("Show 2")
        self.guide._repeating = lambda channel, programme: True
        self.assertEqual(self.guide._button_text("cancel", self.guide.record_target()), "Skip")
        self.guide._repeating = lambda channel, programme: False
        self.assertEqual(
            self.guide._button_text("cancel", self.guide.record_target()), "Cancel recording"
        )

    def test_r_records_the_footers_programme_or_cancels_it(self):
        controller = GuideController(self.guide)
        self.assertTrue(controller.handle(Action.RECORD))
        self.assertEqual(self.asked, [("Show 2", False)])
        self.scheduled.add("Show 2")
        self.assertTrue(controller.handle(Action.RECORD))
        self.assertEqual(self.cancelled, ["Show 2"])

    def test_without_recordings_the_footer_offers_none(self):
        guide = Guide(None, lambda c, s, _hours: half_hours(c, s), lambda: NOW, self.host)
        guide.open_guide(CHANNELS, 0)
        self.assertEqual(list(guide.footer_buttons()), ["live"])  # ▶ Watch only
        self.assertFalse(guide.record_key())

    def test_record_at_a_time_opens_on_the_footers_programme(self):  # 2.8 (owner)
        later = slot_start(NOW) + timedelta(minutes=45)
        QTest.mouseClick(self.guide, Qt.MouseButton.LeftButton, pos=self.point(later))
        self.click(self.guide.footer_buttons()["timer"])
        self.assertEqual(self.timers, [("Harbour News", "Show 3")])
        self.assertEqual(
            self.guide.tooltip_at(self.guide.footer_buttons()["timer"].center()),
            "Record at a time",
        )

    def test_a_channel_without_a_guide_can_be_recorded_at_a_time(self):
        guide = Guide(
            None, lambda c, s, _hours: [], lambda: NOW, self.host, scheduled=lambda c, p: False
        )
        timers = []
        guide.timerRequested.connect(lambda c, p: timers.append((c["name"], p)))
        guide.open_guide(CHANNELS, 0)
        buttons = guide.footer_buttons()
        self.assertEqual(list(buttons), ["live", "timer"])
        self.click_on(guide, buttons["timer"])
        self.assertEqual(timers, [("Harbour News", None)])

    def click_on(self, guide, rect):
        QTest.mouseClick(guide, Qt.MouseButton.LeftButton, pos=rect.center().toPoint())

    def test_watch_tunes_the_highlighted_channel_live(self):
        tuned = []
        self.guide.tuneRequested.connect(lambda c: tuned.append(c["name"]))
        button = self.guide.footer_buttons()["live"]
        self.assertTrue(self.guide.tooltip_at(button.center()).startswith("Watch ("))  # its key
        self.click(button)
        self.assertEqual(tuned, ["Harbour News"])

    def test_watch_is_accented_and_record_outlined_with_a_red_dot(self):
        image = self.guide.grab().toImage()
        buttons = self.guide.footer_buttons()
        accent, live = QColor(PALETTE.accent), QColor(PALETTE.live)

        def near(color, wanted):
            return all(abs(a - b) < 12 for a, b in zip(color.getRgb()[:3], wanted.getRgb()[:3]))

        watch = buttons["live"]
        self.assertTrue(
            near(image.pixelColor(round(watch.left()) + 6, round(watch.center().y())), accent)
        )
        record = buttons["record"]
        inside = [
            image.pixelColor(x, round(record.center().y()))
            for x in range(round(record.left()) + 4, round(record.right()) - 4)
        ]
        self.assertFalse(
            near(image.pixelColor(round(record.left()) + 6, round(record.center().y())), live)
        )
        self.assertTrue(any(near(c, live) for c in inside))  # the dot

    def test_the_record_buttons_name_their_key(self):
        buttons = self.guide.footer_buttons()
        self.assertIn("(R)", self.guide.tooltip_at(buttons["record"].center()))


if __name__ == "__main__":
    unittest.main()
