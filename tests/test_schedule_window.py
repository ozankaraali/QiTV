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
        self.user_stops = []

    def recording_now(self):
        return list(self.now)

    def stopped_by_user(self, live):  # 2.9.7 review: a user's stop of a hosted run
        self.user_stops.append(live)

    def wants(self, live):
        return False

    def host_of(self, name):
        return None

    def hosted_players(self):
        return set()

    def tick(self):
        pass

    def stop_now(self, name):
        self.stopped.append(name)

    def providers(self):
        return set()

    def lend(self, channel, provider, purpose):  # 2.9.7: nothing records to watch
        return None

    def lendable(self, channel, provider):
        return False

    def recording_details(self):  # 2.9.7: the Recordings page's
        return []

    def waiting(self):
        return []

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

    def test_hosted_runs_hooks_reach_the_scheduler(self):  # 2.9.7 review 1–3
        scheduler = self.attach()
        controller = self.window.controller
        self.assertEqual(controller.recording_user_stop, scheduler.stopped_by_user)
        self.assertEqual(controller.scheduled_host, scheduler.host_of)
        if controller.sessions is not None:
            self.assertEqual(controller.sessions.wanted, scheduler.wants)

    def test_record_at_a_time_schedules_a_timer_or_one_slot(self):  # 2.8 (owner)
        from datetime import date, timedelta

        from ui.keymap import Action
        from ui.record_timer_dialog import DAILY, TimerChoice

        self.attach()
        window = self.window
        self.assertIsNotNone(window.controller.record_at)  # the channel's right-click
        window.overlays.guide.timerRequested.emit(CHANNEL, None)  # the guide's footer
        card = window.record_card  # 2.13.1: a card over the picture, no window
        self.assertFalse(card.isHidden())
        self.assertEqual(card.channel_text(), "Channel 2")
        press = window.controller.handle  # the TV's keys: the card's while it shows

        def step_to(value, minutes):
            for _ in range(400):
                if value() == minutes:
                    return
                ahead = (minutes - value()) % (24 * 60)
                press(Action.NAV_PAGE_UP if ahead >= 60 else Action.NAV_RIGHT)
            self.fail(f"never {minutes}")

        press(Action.NAV_DOWN)  # Name, then Repeat
        press(Action.NAV_RIGHT)  # Every day
        self.assertEqual(card.repeat(), DAILY)
        press(Action.NAV_DOWN)  # From
        step_to(card.begin, 21 * 60)
        press(Action.NAV_DOWN)  # To
        step_to(card.end, 22 * 60)
        press(Action.NAV_DOWN)  # Record
        with mock.patch.object(window.screens, "notify") as notify:
            press(Action.NAV_ACTIVATE)
        self.assertTrue(card.isHidden())
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

        pages = self.window.screens
        item = {"path": "/rec/a.ts", "_writing": False}

        def delete_enabled():
            pages._recording_menu(item, QPoint(0, 0))
            shown = self.window.library_menu  # 2.13.1: painted on the Library's page
            self.assertEqual(
                shown.menu().texts(), ["Show in Folder", "Delete Recording…", "Manage Recordings…"]
            )
            entry = shown.menu().entry("Delete Recording…")
            shown.close_menu()
            return entry.enabled

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
