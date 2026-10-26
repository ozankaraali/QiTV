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
