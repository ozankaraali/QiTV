"""Record at a time (2.8, owner): a channel from one time to another, once on a day or on chosen
weekdays, with no guide needed."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from datetime import date, datetime, timedelta
import unittest

from PySide6.QtCore import QDate, QTime
from PySide6.QtWidgets import QApplication

from ui.record_timer_dialog import DAILY, DAYS, ONCE, WEEKDAYS, RecordTimerDialog

NOW = datetime(2026, 10, 26, 20, 7)  # a Monday, this computer's clock


class RecordTimerDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def dialog(self, **kw):
        dialog = RecordTimerDialog("News One", now=lambda: NOW, **kw)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def set_times(self, dialog, begin, end):
        dialog.begin.setTime(QTime(*begin))
        dialog.end.setTime(QTime(*end))

    def test_it_opens_on_the_next_hour_to_come(self):
        dialog = self.dialog()
        self.assertEqual(dialog.windowTitle(), "Record at a time")
        self.assertEqual(dialog.begin.time(), QTime(20, 10))  # the next five minutes
        self.assertEqual(dialog.end.time(), QTime(21, 10))
        self.assertEqual(dialog.day.date(), QDate(2026, 10, 26))
        self.assertEqual(dialog.repeat.currentData(), ONCE)

    def test_it_opens_on_a_programme_s_time(self):
        dialog = self.dialog(
            start=datetime(2026, 10, 27, 21, 0), stop=datetime(2026, 10, 27, 22, 30)
        )
        self.assertEqual((dialog.begin.time(), dialog.end.time()), (QTime(21, 0), QTime(22, 30)))
        self.assertEqual(dialog.day.date(), QDate(2026, 10, 27))

    def test_once_on_a_day(self):
        dialog = self.dialog()
        dialog.name.setText("  Late film ")
        self.set_times(dialog, (23, 30), (1, 0))  # past midnight: ends the next day
        choice = dialog.choice()
        self.assertEqual(choice.title, "Late film")
        self.assertEqual((choice.begin, choice.minutes, choice.days), (23 * 60 + 30, 90, ()))
        self.assertEqual(choice.start(), datetime(2026, 10, 26, 23, 30).astimezone())
        self.assertEqual(choice.stop(), datetime(2026, 10, 27, 1, 0).astimezone())
        self.assertTrue(dialog.hint.text())  # says it ends the next day

    def test_repeats(self):
        dialog = self.dialog()
        self.set_times(dialog, (21, 0), (22, 0))
        for kind, days in ((DAILY, tuple(range(7))), (WEEKDAYS, (0, 1, 2, 3, 4))):
            dialog.repeat.setCurrentIndex(dialog.repeat.findData(kind))
            self.assertEqual(dialog.choice().days, days)
            self.assertFalse(dialog.day.isEnabled())  # a day is for once only
            self.assertFalse(dialog.day.isVisibleTo(dialog))
        dialog.repeat.setCurrentIndex(dialog.repeat.findData(DAYS))
        self.assertIsNone(dialog.choice())  # no day chosen yet
        self.assertFalse(dialog.schedule_button.isEnabled())
        dialog.day_buttons[2].setChecked(True)
        dialog.day_buttons[5].setChecked(True)
        self.assertEqual(dialog.choice().days, (2, 5))
        self.assertTrue(dialog.schedule_button.isEnabled())

    def test_a_time_gone_by_or_of_no_length_cannot_be_scheduled(self):
        dialog = self.dialog()
        self.set_times(dialog, (19, 0), (20, 0))  # today, over
        self.assertIsNone(dialog.choice())
        self.assertFalse(dialog.schedule_button.isEnabled())
        self.assertTrue(dialog.hint.text())
        dialog.day.setDate(QDate(2026, 10, 27))  # tomorrow: fine
        self.assertIsNotNone(dialog.choice())
        self.set_times(dialog, (19, 0), (19, 0))
        self.assertIsNone(dialog.choice())
        dialog.repeat.setCurrentIndex(dialog.repeat.findData(DAILY))
        self.set_times(dialog, (19, 0), (20, 0))  # every day: today's being over is fine
        self.assertIsNotNone(dialog.choice())

    def test_one_on_now_can_be_scheduled(self):
        dialog = self.dialog()
        self.set_times(dialog, (20, 0), (21, 0))  # began 7 minutes ago
        self.assertIsNotNone(dialog.choice())

    def test_the_day_cannot_be_before_today(self):
        dialog = self.dialog()
        dialog.day.setDate(QDate(2026, 10, 20))
        self.assertEqual(dialog.day.date().toPython(), date(2026, 10, 26))

    def test_schedule_accepts(self):
        dialog = self.dialog()
        accepted = []
        dialog.accepted.connect(lambda: accepted.append(dialog.choice()))
        dialog.schedule_button.click()
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0].start(), (NOW + timedelta(minutes=3)).astimezone())


if __name__ == "__main__":
    unittest.main()
