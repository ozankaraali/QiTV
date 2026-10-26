"""Scheduled recordings in the window (2.8 review): what records shows on the REC sign, which
stops it or goes back to its channel; the time-shift helper stopped or replaced, the scheduler
gives its streams back and records with the new one."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time
import unittest
from unittest import mock

from PySide6.QtCore import QObject, Signal
from test_tv_window import WindowCase

CHANNEL = {"id": "2", "name": "Channel 2", "_provider": "Local fixture"}


class StandInScheduler(QObject):
    changed = Signal()

    def __init__(self):
        super().__init__()
        self.now = []
        self.stopped, self.gone, self.engines = [], 0, []

    def recording_now(self):
        return list(self.now)

    def tick(self):
        pass

    def stop_now(self, name):
        self.stopped.append(name)

    def providers(self):
        return set()

    def engine_gone(self):
        self.gone += 1

    def use_engine(self, engine):
        self.engines.append(engine)

    def shutdown(self):
        pass


class ScheduleWindowTests(WindowCase):
    def attach(self):
        scheduler = StandInScheduler()
        self.window.attach_scheduler(scheduler)
        return scheduler

    def test_what_records_on_schedule_shows_on_the_rec_sign_and_stops_there(self):
        scheduler = self.attach()
        since = time.monotonic() - 65
        scheduler.now = [("sched:a@1", "Channel 2", since, CHANNEL)]
        scheduler.changed.emit()
        controller = self.window.controller
        self.assertIn(("sched:a@1", "Channel 2", since), controller.rec_list())
        self.assertIn("Channel 2", self.window.overlays.rec_sign.text())
        controller.stop_background_recording("sched:a@1")
        self.assertEqual(scheduler.stopped, ["sched:a@1"])
        with mock.patch.object(controller, "tune_live") as tune:
            controller.go_back_to_recording("sched:a@1")
        self.assertEqual(tune.call_args.args[0]["name"], "Channel 2")

    def test_record_at_a_time_schedules_a_timer_or_one_slot(self):  # 2.8 (owner)
        from datetime import date, timedelta

        from PySide6.QtCore import QTime

        from ui.record_timer_dialog import DAILY, TimerChoice

        self.attach()
        window = self.window
        self.assertIsNotNone(window.controller.record_at)  # the channel's right-click
        window.overlays.guide.timerRequested.emit(CHANNEL, None)  # the guide's footer
        dialog = window._timer_dialog
        self.assertEqual(dialog.channel.text(), "Channel 2")
        dialog.repeat.setCurrentIndex(dialog.repeat.findData(DAILY))
        dialog.begin.setTime(QTime(21, 0))
        dialog.end.setTime(QTime(22, 0))
        with mock.patch.object(window.screens, "notify") as notify:
            dialog.schedule_button.click()
        timers = [i for i in window.recording_schedule.items() if i["kind"] == "timer"]
        self.assertEqual(
            [(i["begin"], i["minutes"], i["days"]) for i in timers], [(1260, 60, list(range(7)))]
        )
        self.assertEqual(notify.call_args[0][0], "Records Channel 2 21:00–22:00, every day")
        tomorrow = date.today() + timedelta(days=1)
        choice = TimerChoice("", tomorrow, 21 * 60, 60, ())
        window.schedule_timer(CHANNEL, choice)
        once = [i for i in window.recording_schedule.items() if i["kind"] == "once"]
        self.assertEqual(
            [(i["title"], i["start"]) for i in once], [("21:00–22:00", choice.start().timestamp())]
        )

    def test_the_recordings_shelf_knows_the_files_being_written(self):  # 2.8 review
        # Its Delete stays greyed out for one a recording still writes (the REC button's or
        # the schedule's), whichever engine runs now.
        shelf = self.window.screens.recordings
        engine = mock.Mock(writing=frozenset({"/rec/a.ts"}))
        with mock.patch.object(self.window, "engine", engine):
            self.assertEqual(set(shelf._writing()), {"/rec/a.ts"})
        with mock.patch.object(self.window, "engine", None):
            self.assertEqual(set(shelf._writing()), set())

    def test_delete_follows_whether_the_file_is_written_now(self):  # 2.8 review
        # The shelf was read while it recorded (or before): its menu asks now.
        from PySide6.QtCore import QPoint
        from PySide6.QtWidgets import QMenu

        pages = self.window.screens
        item = {"path": "/rec/a.ts", "_writing": False}

        def delete_enabled():
            pages._recording_menu(item, QPoint(0, 0))
            menu = self.window.library_page.findChildren(QMenu)[-1]
            action = next(a for a in menu.actions() if a.text() == "Delete Recording…")
            menu.close()
            menu.deleteLater()
            return action.isEnabled()

        with mock.patch.object(self.window, "engine", mock.Mock(writing=frozenset({"/rec/a.ts"}))):
            self.assertFalse(delete_enabled())
        item["_writing"] = True  # read while it recorded; it ended since
        with mock.patch.object(self.window, "engine", mock.Mock(writing=frozenset())):
            self.assertTrue(delete_enabled())

    def test_the_helper_stopped_or_replaced_reaches_the_scheduler(self):
        scheduler = self.attach()
        window = self.window
        window.engine = mock.Mock()
        window.live_session = mock.Mock()
        window.stop_live()
        self.assertEqual(scheduler.gone, 1)
        fresh = mock.Mock()
        with (
            mock.patch("ui.tv_window.LiveEngine", return_value=fresh),
            mock.patch("ui.tv_window.call_before_update"),
            mock.patch.object(window.controller, "resume_stopped"),
        ):
            window.restart_live()
        self.assertEqual(scheduler.engines, [fresh])
        window.engine = None
        window.live_session = None


if __name__ == "__main__":
    unittest.main()
