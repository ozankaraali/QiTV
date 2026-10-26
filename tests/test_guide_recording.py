"""Recording from the guide (2.8): the footer's Record and Record series for the programme on
now or one to come (clicked on), R to record it or cancel, and a dot on scheduled ones."""

from datetime import datetime, timedelta, timezone
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from services.epg_view import Programme
from ui.guide_controller import GuideController
from ui.keymap import Action
from ui.overlays.guide import WINDOW, Guide, slot_start

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
        self.assertEqual(list(buttons), ["record", "series"])
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
        self.assertEqual(list(buttons), ["cancel"])
        self.click(buttons["cancel"])
        self.assertEqual(self.cancelled, ["Show 2"])

    def test_r_records_the_footers_programme_or_cancels_it(self):
        controller = GuideController(self.guide)
        self.assertTrue(controller.handle(Action.RECORD))
        self.assertEqual(self.asked, [("Show 2", False)])
        self.scheduled.add("Show 2")
        self.assertTrue(controller.handle(Action.RECORD))
        self.assertEqual(self.cancelled, ["Show 2"])

    def test_without_recordings_the_footer_offers_none(self):
        guide = Guide(None, lambda c, s, l: half_hours(c, s), lambda: NOW, self.host)
        guide.open_guide(CHANNELS, 0)
        self.assertEqual(guide.footer_buttons(), {})
        self.assertFalse(guide.record_key())

    def test_the_record_buttons_name_their_key(self):
        buttons = self.guide.footer_buttons()
        self.assertIn("(R)", self.guide.tooltip_at(buttons["record"].center()))


if __name__ == "__main__":
    unittest.main()
