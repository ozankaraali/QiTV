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
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QObject, Signal
from PySide6.QtWidgets import QApplication

from services import provider_api, recording_schedule as rs
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
        self.seqs = [5, 6]

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

    def spans(self, sid):  # the stream's own seconds; the live edge is the last one's end
        return [SimpleNamespace(seq=n, start=(n - 5) * 10.0, end=(n - 4) * 10.0) for n in self.seqs]

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
        self.items = []
        self.max = 1

    def request_link(self, item, *, section):
        self.asked.append((item["name"], section))
        self.items.append(dict(item))
        return len(self.asked)

    items: list = []  # each item asked for (its address)

    def provider(self, name):
        return self.entry

    def max_connections(self, name):
        return self.max

    def programmes(self, channel, now, limit=8):
        return []


def rs_name(scheduler, title):
    """The REC sign's name of the scheduled recording of `title`."""
    return next("sched:" + run.job.key for run in scheduler._runs if run.job.title == title)


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="qitv-scheduler-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.now = (T0 + timedelta(minutes=29)).timestamp()
        self.schedule = rs.Schedule(
            str(self.folder / rs.FILE_NAME), write=rs.write_now, clock=lambda: self.now
        )
        self.engine, self.catalogs = FakeEngine(), FakeCatalogs()
        self.budget = ConnectionBudget()
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
        self.assertEqual(max_s, 24 * 3600)  # the scheduler stops it at its window's end
        self.assertTrue(any("The News" in text for text, _ in self.toasts))

    def test_a_portal_that_answered_only_over_http_records_over_http(self):  # 2.8.1
        memory = mock.patch.object(provider_api, "_http_only", set())
        memory.start()
        self.addCleanup(memory.stop)
        self.catalogs.entry = {"name": "P", "type": "STB", "url": "http://portal.test/c/"}
        provider_api.remember_http_only("http://portal.test/c/")  # its sign-in found no HTTPS
        self.scheduled_now()
        self.assertFalse(self.engine.opened[0][2]["https_first"])

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

    def test_quitting_while_it_records_notes_the_file_and_the_rest_may_follow(self):
        # 2.8 review M1: the helper's end may come after QiTV is gone: the file is noted now
        # (the Library names it), and QiTV opened again while it airs records the rest.
        self.scheduled_now()
        sid = self.engine.opened[0][0]
        self.engine.sessionReady.emit(sid)
        _rid, _sid, path, *_ = self.engine.recordings[0]
        self.scheduler.shutdown()
        (outcome,) = self.schedule.outcomes()
        self.assertEqual((outcome["state"], outcome["path"]), (rs.RECORDED, path))
        self.assertEqual(len(self.schedule.items()), 1)  # still to record while it airs
        self.assertEqual(len(self.schedule.due(self.now, lambda c, since: [])), 1)

    def test_a_link_asked_uses_the_providers_account_now(self):  # 2.8 review M15
        self.catalogs.entry = dict(self.catalogs.entry, username="u", password="p")
        account = {"username": "u", "password": "p"}
        self.schedule.add_once(CHANNEL, programme("The News", 30), account=account)
        self.scheduler.tick()
        self.assertEqual(self.catalogs.items[-1]["cmd"], CHANNEL["cmd"])

    def test_its_own_time_limit_is_a_normal_end(self):
        self.scheduled_now()
        sid = self.engine.opened[0][0]
        self.engine.sessionReady.emit(sid)
        rid, _sid, path, *_ = self.engine.recordings[0]
        self.engine.recordingEnded.emit(rid, LIMIT, path, 5_000)
        self.assertEqual(self.schedule.outcomes()[0]["state"], rs.RECORDED)

    def at(self, minutes, seconds=0):
        self.now = (T0 + timedelta(minutes=minutes, seconds=seconds)).timestamp()
        self.scheduler.tick()
        self.pump()

    def test_no_free_stream_asks_no_link_and_is_missed_only_at_its_end(self):  # review I3, I4
        held = self.budget.request("P", 1, purpose="main")  # the channel watched holds it
        self.schedule.add_once(CHANNEL, programme("The News", 30))
        self.scheduler.tick()
        self.assertEqual(self.catalogs.asked, [])  # an STB portal mints a link per request
        self.assertEqual(self.schedule.outcomes(), [])  # tried again while it airs
        self.at(63, 1)
        (outcome,) = self.schedule.outcomes()
        self.assertEqual((outcome["state"], outcome["why"]), (rs.MISSED, "stream"))
        self.assertEqual(sum(kind == "error" for _text, kind in self.toasts), 1)
        self.budget.release(held)

    def test_a_stream_freed_meanwhile_records_the_rest(self):  # review I4
        held = self.budget.request("P", 1, purpose="main")
        self.schedule.add_once(CHANNEL, programme("The News", 30))
        self.scheduler.tick()
        self.budget.release(held)
        self.at(29, 30)  # too soon to try again
        self.assertEqual(self.catalogs.asked, [])
        self.at(30, 10)
        self.assertEqual(self.catalogs.asked, [("News One", "live")])
        self.catalogs.linkReady.emit(1, CHANNEL["cmd"])
        self.assertEqual(len(self.engine.opened), 1)

    def test_a_link_that_fails_is_asked_again_and_gives_the_stream_back(self):  # review I4
        self.schedule.add_once(CHANNEL, programme("The News", 30))
        self.scheduler.tick()
        self.catalogs.linkFailed.emit(1, "Portal down")
        self.assertEqual(self.budget.holders("P"), [])
        self.assertEqual(self.schedule.outcomes(), [])
        self.at(30, 10)
        self.assertEqual(len(self.catalogs.asked), 2)
        self.catalogs.linkFailed.emit(2, "Portal down")
        self.at(63, 1)
        self.assertEqual(self.schedule.outcomes()[0]["state"], rs.FAILED)
        self.assertEqual(self.schedule.items(), [])

    def test_a_stream_that_fails_before_recording_gives_the_stream_back_and_tries_again(self):
        self.scheduled_now()
        sid = self.engine.opened[0][0]
        self.engine.sessionFailed.emit(sid, "http", "HTTP 502")
        self.assertEqual(self.engine.closed, [sid])
        self.engine.sessionClosed.emit(sid)
        self.assertEqual(self.budget.holders("P"), [])
        self.at(30, 10)
        self.assertEqual(len(self.catalogs.asked), 2)

    def test_a_recording_cut_short_goes_on_in_another_file(self):  # review I4
        self.scheduled_now()
        sid = self.engine.opened[0][0]
        self.engine.sessionReady.emit(sid)
        rid, _sid, path, *_ = self.engine.recordings[0]
        self.now = (T0 + timedelta(minutes=40)).timestamp()
        self.engine.sessionFailed.emit(sid, "http", "HTTP 502")  # the helper gave up
        self.assertEqual(self.engine.stops, [sid])
        self.engine.recordingEnded.emit(rid, "stopped", path, 9_000)
        self.engine.sessionClosed.emit(sid)
        (outcome,) = self.schedule.outcomes()
        self.assertEqual((outcome["state"], outcome["path"]), (rs.FAILED, path))  # kept
        self.assertEqual(len(self.schedule.items()), 1)  # its programme isn't over
        self.at(41, 10)
        self.assertEqual(len(self.catalogs.asked), 2)

    def test_back_to_back_programmes_hand_the_stream_over(self):  # review I1
        self.schedule.add_once(CHANNEL, programme("Ep 1", 30))
        self.schedule.add_once(CHANNEL, programme("Ep 2", 60))
        self.scheduler.tick()
        self.catalogs.linkReady.emit(1, CHANNEL["cmd"])
        sid = self.engine.opened[0][0]
        self.engine.sessionReady.emit(sid)
        rid, _sid, path, *_ = self.engine.recordings[0]
        self.at(59, 0)  # Ep 2's minute early: the stream Ep 1 records is the one it needs
        self.assertEqual(len(self.catalogs.asked), 1)
        self.assertEqual(len(self.budget.holders("P")), 1)
        self.at(60, 30)  # half a minute into Ep 2, Ep 1's file ends
        self.assertEqual(self.engine.stops, [sid])
        self.engine.seqs = list(range(5, 25))  # 200 s held: Ep 2 starts 90 s back
        self.engine.recordingEnded.emit(rid, "stopped", path, 1_000_000)
        self.pump()
        self.assertEqual(self.engine.closed, [])  # the session goes on
        rid2, sid2, path2, seq2, _max = self.engine.recordings[1]
        self.assertEqual(sid2, sid)
        self.assertEqual(seq2, 16)  # live edge 200 s; Ep 2 from a minute early: 90 s back, 110 s
        self.assertEqual([o["title"] for o in self.schedule.outcomes()], ["Ep 1"])
        self.at(93, 1)
        self.engine.recordingEnded.emit(rid2, "stopped", path2, 1_000_000)
        self.engine.sessionClosed.emit(sid)
        self.assertEqual(self.engine.closed, [sid])
        self.assertEqual(self.budget.holders("P"), [])
        self.assertEqual(self.schedule.items(), [])

    def recording(self):
        """The first link answered and the session ready: (sid, rid, path)."""
        self.catalogs.linkReady.emit(1, CHANNEL["cmd"])
        sid = self.engine.opened[0][0]
        self.engine.sessionReady.emit(sid)
        rid, _sid, path, *_ = self.engine.recordings[0]
        return sid, rid, path

    def test_a_programme_inside_a_recording_on_its_channel_is_in_that_file(self):
        # 2.8 final review 1: an evening's recording and a programme within it on the same
        # channel: the evening's goes on (no hand-over to the shorter one), the programme is
        # noted in its file, the file named after the evening's.
        self.schedule.add_once(CHANNEL, programme("Evening", 30, 120))  # 20:30–22:30
        self.schedule.add_once(CHANNEL, programme("Quiz", 60, 30))  # 21:00–21:30
        self.scheduler.tick()
        sid, rid, path = self.recording()
        self.at(59, 30)
        self.at(60, 40)
        self.at(94, 0)  # after the Quiz's end
        self.assertEqual(self.engine.stops, [])
        self.assertEqual(len(self.catalogs.asked), 1)
        self.assertEqual(len(self.engine.recordings), 1)
        self.at(153, 1)  # the evening's end
        self.assertEqual(self.engine.stops, [sid])
        self.engine.recordingEnded.emit(rid, "stopped", path, 1_000_000)
        outcomes = {o["title"]: (o["state"], o["path"]) for o in self.schedule.outcomes()}
        self.assertEqual(outcomes, {"Quiz": (rs.RECORDED, path), "Evening": (rs.RECORDED, path)})
        self.assertEqual(self.schedule.title_of(path), "Evening")
        self.assertEqual(self.schedule.items(), [])

    def test_a_longer_one_at_the_same_start_records_and_holds_the_shorter(self):
        # The guide's half hour and a two-hour timer at the same minute: the timer records,
        # the half hour is noted in its file (not the other way round).
        start = (T0 + timedelta(minutes=60)).astimezone()
        self.schedule.add_once(CHANNEL, programme("Show", 60, 30))
        timer = self.schedule.add_timer(
            CHANNEL, begin=start.hour * 60 + start.minute, minutes=120, days=range(7)
        )
        self.at(59, 30)
        self.assertEqual(len(self.catalogs.asked), 1)
        (run,) = self.scheduler._runs
        self.assertEqual(run.job.item_id, timer["id"])
        sid, rid, path = self.recording()
        self.at(94, 0)
        self.assertEqual(self.engine.stops, [])
        self.at(183, 1)
        self.engine.recordingEnded.emit(rid, "stopped", path, 1_000_000)
        titles = [o["title"] for o in self.schedule.outcomes()]
        self.assertEqual(sorted(titles), sorted(["Show", run.job.title]))
        self.assertEqual(self.schedule.title_of(path), run.job.title)

    def test_a_programme_held_by_a_recording_stopped_early_records_on_its_own(self):
        self.schedule.add_once(CHANNEL, programme("Evening", 30, 120))
        self.schedule.add_once(CHANNEL, programme("Quiz", 60, 30))
        self.scheduler.tick()
        sid, rid, path = self.recording()
        self.at(60, 0)  # the Quiz is held by the evening's recording
        self.scheduler.stop_now(rs_name(self.scheduler, "Evening"))  # the REC sign's Stop
        self.engine.recordingEnded.emit(rid, "stopped", path, 1_000_000)
        self.engine.sessionClosed.emit(sid)
        self.at(61, 0)
        self.assertEqual(len(self.catalogs.asked), 2)  # the Quiz asks a stream of its own
        self.assertNotIn("Quiz", [o["title"] for o in self.schedule.outcomes()])

    def test_a_recording_made_longer_while_it_records_goes_on(self):  # review 2
        news = programme("The News", 30)
        self.schedule.add_once(CHANNEL, news)
        self.scheduler.tick()
        sid, rid, path = self.recording()
        _rid, _sid, _path, _seq, max_s = self.engine.recordings[0]
        self.assertGreaterEqual(max_s, 24 * 3600)  # the scheduler stops it, at its end
        later = Programme("20:30–21:30", news.start, news.stop + timedelta(minutes=30))
        self.schedule.add_once(CHANNEL, later)
        self.at(64, 0)  # past the first end
        self.assertEqual(self.engine.stops, [])
        self.at(93, 1)
        self.assertEqual(self.engine.stops, [sid])

    def test_one_airing_scheduled_twice_records_once(self):  # review I1
        self.schedule.add_once(CHANNEL, programme("The News", 30))
        self.schedule.add_series(CHANNEL, "The News")
        self.catalogs.programmes = lambda channel, now, limit=8: [programme("The News", 30)]
        self.scheduler.tick()
        self.assertEqual(len(self.catalogs.asked), 1)

    def test_its_normal_end_is_not_said_as_a_24_hour_limit(self):  # review I2
        self.scheduled_now()
        sid = self.engine.opened[0][0]
        self.engine.sessionReady.emit(sid)
        rid, _sid, path, *_ = self.engine.recordings[0]
        self.engine.recordingEnded.emit(rid, LIMIT, path, 5_000)
        text, kind = self.toasts[-1]
        self.assertNotIn("24 hours", text)
        self.assertEqual(kind, "info")

    def test_what_records_shows_and_stops_for_this_airing_only(self):  # review I5
        self.schedule.add_series(CHANNEL, "The News")
        self.catalogs.programmes = lambda channel, now, limit=8: [programme("The News", 30)]
        self.scheduler.tick()
        self.catalogs.linkReady.emit(1, CHANNEL["cmd"])
        sid = self.engine.opened[0][0]
        self.engine.sessionReady.emit(sid)
        ((name, channel, _since, item),) = self.scheduler.recording_now()
        self.assertTrue(name.startswith("sched:"))
        self.assertEqual((channel, item["name"]), ("News One", "News One"))
        self.scheduler.stop_now(name)
        self.assertEqual(self.engine.stops, [sid])
        rid, _sid, path, *_ = self.engine.recordings[0]
        self.engine.recordingEnded.emit(rid, "stopped", path, 9_000)
        self.at(30, 30)
        self.assertEqual(len(self.catalogs.asked), 1)  # this airing is done
        self.assertEqual(len(self.schedule.items()), 1)  # the series goes on

    def test_the_helper_gone_gives_the_streams_back_and_a_new_one_is_used(self):  # review I10
        self.scheduled_now()
        sid = self.engine.opened[0][0]
        self.engine.sessionReady.emit(sid)
        self.scheduler.engine_gone()
        self.assertEqual(self.budget.holders("P"), [])
        fresh = FakeEngine()
        self.scheduler.use_engine(fresh)
        self.at(30, 10)
        self.catalogs.linkReady.emit(2, CHANNEL["cmd"])
        self.assertEqual(len(fresh.opened), 1)

    def test_a_job_is_started_once(self):
        self.scheduled_now()
        self.scheduler.tick()
        self.assertEqual(len(self.catalogs.asked), 1)

    def test_what_was_missed_while_closed_is_said_at_start(self):
        now, self.now = self.now, (T0 - timedelta(hours=6)).timestamp()  # scheduled back then
        self.schedule.add_once(CHANNEL, programme("Old News", -300))
        self.now = now
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
