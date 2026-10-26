"""Scheduled recordings (2.8): a due programme is recorded by the time-shift helper with no
player: its link, a stream of the provider's budget, the helper's session, the recording, and
everything given back at its end."""

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QObject, Signal
from PySide6.QtWidgets import QApplication

from services import recording_schedule as rs
from services.connection_budget import ConnectionBudget
from services.epg_view import Programme
from services.live.recorder import ENDED, LIMIT
from ui.recording_scheduler import RecordingScheduler

app = QApplication.instance() or QApplication([])
T0 = datetime(2026, 10, 26, 20, 0, tzinfo=timezone.utc)
CHANNEL = {
    "id": "7",
    "name": "News One",
    "cmd": "http://panel.test/live/u/p/7.ts",
    "_provider": "P",
}


def programme(title, start_min, length_min=30):
    start = T0 + timedelta(minutes=start_min)
    return Programme(title, start, start + timedelta(minutes=length_min), "", None)


class FakeEngine(QObject):
    available = Signal(bool)
    sessionReady = Signal(str)
    sessionFailed = Signal(str, str, str)
    sessionEnded = Signal(str)
    sessionClosed = Signal(str)
    recordingEnded = Signal(str, str, str, int)

    def __init__(self):
        super().__init__()
        self.opened, self.closed, self.recordings, self.stops = [], [], [], []
        self.up = True

    def is_available(self):
        return self.up

    def start(self):
        pass

    def open_session(self, url, **kwargs):
        sid = f"s{len(self.opened) + 1}"
        self.opened.append((sid, url, kwargs))
        return sid

    def close_session(self, sid):
        self.closed.append(sid)

    def spans(self, sid):
        return [SimpleNamespace(seq=5), SimpleNamespace(seq=6)]

    def start_recording(self, sid, path, seq, *, max_s, min_free):
        rid = f"r{len(self.recordings) + 1}"
        self.recordings.append((rid, sid, path, seq, max_s))
        return rid

    def stop_recording(self, sid):
        self.stops.append(sid)


class FakeCatalogs(QObject):
    linkReady = Signal(int, str)
    linkFailed = Signal(int, str)

    def __init__(self, entry=None):
        super().__init__()
        self.asked = []
        self.entry = entry or {"name": "P", "type": "XTREAM", "url": "http://panel.test"}
        self.max = 1

    def request_link(self, item, *, section):
        self.asked.append((item["name"], section))
        return len(self.asked)

    def provider(self, name):
        return self.entry

    def max_connections(self, name):
        return self.max

    def programmes(self, channel, now, limit=8):
        return []


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="qitv-scheduler-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.schedule = rs.Schedule(str(self.folder / rs.FILE_NAME), write=rs.write_now)
        self.engine, self.catalogs = FakeEngine(), FakeCatalogs()
        self.budget = ConnectionBudget()
        self.now = (T0 + timedelta(minutes=29)).timestamp()
        self.toasts = []
        config = SimpleNamespace(recordings_dir=str(self.folder), ssl_verify=True)
        self.scheduler = RecordingScheduler(
            schedule=self.schedule,
            engine=self.engine,
            catalogs=self.catalogs,
            budget=self.budget,
            config=config,
            toast=lambda text, kind="info", ms=0: self.toasts.append((text, kind)),
            clock=lambda: self.now,
            parent=None,
        )

    def pump(self):
        QCoreApplication.processEvents()

    def scheduled_now(self):
        self.schedule.add_once(CHANNEL, programme("The News", 30))
        self.scheduler.tick()
        self.catalogs.linkReady.emit(1, CHANNEL["cmd"])
        self.pump()

    def test_a_due_programme_is_recorded_by_the_helper_with_no_player(self):
        self.scheduled_now()
        self.assertEqual(self.catalogs.asked, [("News One", "live")])
        sid, url, kwargs = self.engine.opened[0]
        self.assertEqual(url, CHANNEL["cmd"])
        self.assertFalse(kwargs["disk"])
        self.assertEqual(self.budget.holders("P"), ["rec:" + self.schedule.items()[0]["id"]])
        self.engine.sessionReady.emit(sid)
        rid, rec_sid, path, seq, max_s = self.engine.recordings[0]
        self.assertEqual((rec_sid, seq), (sid, 5))  # from the first segment the helper has
        self.assertTrue(path.startswith(str(self.folder)))
        self.assertTrue(os.path.basename(path).startswith("News One "))
        self.assertAlmostEqual(max_s, (T0 + timedelta(minutes=63)).timestamp() - self.now, delta=1)
        self.assertTrue(any("The News" in text for text, _ in self.toasts))

    def test_at_its_end_it_stops_saves_and_gives_everything_back(self):
        self.scheduled_now()
        sid = self.engine.opened[0][0]
        self.engine.sessionReady.emit(sid)
        rid, _sid, path, *_ = self.engine.recordings[0]
        self.now = (T0 + timedelta(minutes=63)).timestamp()
        self.scheduler.tick()
        self.assertEqual(self.engine.stops, [sid])
        self.engine.recordingEnded.emit(rid, ENDED, path, 1_000_000)
        self.assertEqual(self.engine.closed, [sid])
        self.engine.sessionClosed.emit(sid)
        self.assertEqual(self.budget.holders("P"), [])
        (outcome,) = self.schedule.outcomes()
        self.assertEqual((outcome["state"], outcome["path"]), (rs.RECORDED, path))
        self.assertEqual(self.schedule.items(), [])

    def test_its_own_time_limit_is_a_normal_end(self):
        self.scheduled_now()
        sid = self.engine.opened[0][0]
        self.engine.sessionReady.emit(sid)
        rid, _sid, path, *_ = self.engine.recordings[0]
        self.engine.recordingEnded.emit(rid, LIMIT, path, 5_000)
        self.assertEqual(self.schedule.outcomes()[0]["state"], rs.RECORDED)

    def test_no_free_stream_misses_it_and_says_why(self):
        self.budget.request("P", 1, purpose="main")  # the channel watched holds the only one
        self.scheduled_now()
        self.assertEqual(self.engine.opened, [])
        (outcome,) = self.schedule.outcomes()
        self.assertEqual((outcome["state"], outcome["why"]), (rs.MISSED, "stream"))
        self.assertTrue(any(kind == "error" for _text, kind in self.toasts))

    def test_a_link_that_fails_fails_the_recording(self):
        self.schedule.add_once(CHANNEL, programme("The News", 30))
        self.scheduler.tick()
        self.catalogs.linkFailed.emit(1, "Portal down")
        self.assertEqual(self.schedule.outcomes()[0]["state"], rs.FAILED)
        self.assertEqual(self.budget.holders("P"), [])

    def test_a_stream_that_fails_for_good_ends_it_and_gives_the_stream_back(self):
        self.scheduled_now()
        sid = self.engine.opened[0][0]
        self.engine.sessionFailed.emit(sid, "http", "HTTP 502")
        self.assertEqual(self.engine.closed, [sid])
        self.engine.sessionClosed.emit(sid)
        self.assertEqual(self.budget.holders("P"), [])
        self.assertEqual(self.schedule.outcomes()[0]["state"], rs.FAILED)

    def test_a_job_is_started_once(self):
        self.scheduled_now()
        self.scheduler.tick()
        self.assertEqual(len(self.catalogs.asked), 1)

    def test_what_was_missed_while_closed_is_said_at_start(self):
        self.schedule.add_once(CHANNEL, programme("Old News", -300))
        self.scheduler.start()
        self.assertTrue(any("Old News" in text for text, _ in self.toasts))

    def test_running_recordings_are_told_and_stopped_at_quit(self):
        self.scheduled_now()
        sid = self.engine.opened[0][0]
        self.engine.sessionReady.emit(sid)
        self.assertEqual([title for title, _channel in self.scheduler.running()], ["The News"])
        self.scheduler.shutdown()
        self.assertEqual(self.engine.stops, [sid])


if __name__ == "__main__":
    unittest.main()
