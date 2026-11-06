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
from services.recordings_index import RecordingsIndex
from ui.recording_scheduler import RecordingScheduler

app = QApplication.instance() or QApplication([])
T0 = datetime(2026, 10, 26, 20, 0, tzinfo=timezone.utc)
CHANNEL = {
    "id": "7",
    "name": "News One",
    "cmd": "http://panel.test/live/u/p/7.ts",
    "_provider": "P",
}

ACCOUNT = {"username": "u", "password": "p"}


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
        self.index = RecordingsIndex(str(self.folder / "index.json"), write=rs.write_now)
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
            index=self.index,
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
        # Its stream's purpose is its REC sign name: the stream question counts it (audit).
        self.assertEqual(self.budget.holders("P"), [rs_name(self.scheduler, "The News")])
        self.engine.sessionReady.emit(sid)
        rid, rec_sid, path, seq, max_s = self.engine.recordings[0]
        self.assertEqual((rec_sid, seq), (sid, 5))  # from the first segment the helper has
        self.assertTrue(path.startswith(str(self.folder)))
        self.assertTrue(os.path.basename(path).startswith("News One "))
        self.assertEqual(max_s, 24 * 3600)  # the scheduler stops it at its window's end
        self.assertTrue(any("The News" in text for text, _ in self.toasts))

    def test_a_scheduled_recording_notes_its_title(self):  # 2.9.7
        self.scheduled_now()
        sid = self.engine.opened[0][0]
        self.engine.sessionReady.emit(sid)
        _rid, _sid, path, *_ = self.engine.recordings[0]
        entry = self.index.get(path)
        self.assertEqual(entry["title"], "The News")
        self.assertEqual((entry["channel"], entry["provider"]), ("News One", "P"))
        self.assertEqual(entry["channel_id"], "7")
        self.assertEqual(entry["start"], (T0 + timedelta(minutes=30)).timestamp())
        self.assertEqual(entry["stop"], (T0 + timedelta(minutes=60)).timestamp())

    def test_a_recording_saved_under_another_name_moves_its_entry(self):  # 2.9.7
        self.scheduled_now()
        sid = self.engine.opened[0][0]
        self.engine.sessionReady.emit(sid)
        rid, _sid, path, *_ = self.engine.recordings[0]
        saved = path[: -len(".ts")] + " (2).ts"
        self.now = (T0 + timedelta(minutes=63)).timestamp()
        self.scheduler.tick()
        self.engine.recordingEnded.emit(rid, ENDED, saved, 1_000)
        self.assertEqual(self.index.get(saved)["title"], "The News")
        self.assertIsNone(self.index.get(path))

    def test_a_recording_cut_short_keeps_where_its_file_ends(self):  # final review 5
        self.scheduled_now()
        sid = self.engine.opened[0][0]
        self.engine.sessionReady.emit(sid)
        rid, _sid, path, *_ = self.engine.recordings[0]
        self.now = (T0 + timedelta(minutes=34)).timestamp()  # four minutes into it
        self.engine.recordingEnded.emit(rid, ENDED, path, 1_000)
        self.assertEqual(self.index.get(path)["stop"], self.now)

    def test_quitting_mid_recording_notes_where_its_file_ends(self):  # release review
        self.scheduled_now()
        sid = self.engine.opened[0][0]
        self.engine.sessionReady.emit(sid)
        rid, _sid, path, *_ = self.engine.recordings[0]
        self.now = (T0 + timedelta(minutes=50)).timestamp()  # twenty minutes in
        self.scheduler.shutdown()  # its end (recordingEnded) may come after QiTV is gone
        self.assertEqual(self.index.get(path)["stop"], self.now)

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
        said = [text for text, kind in self.toasts if kind == "error"]
        self.assertEqual(len(said), 2)  # at once (audit 1c), and at its end
        self.assertIn("was not recorded", said[-1])
        self.budget.release(held)

    def test_no_free_stream_is_said_at_once_and_once(self):  # audit 1c
        held = self.budget.request("P", 1, purpose="main")  # the channel watched holds it
        self.schedule.add_once(CHANNEL, programme("The News", 30))
        self.scheduler.tick()
        self.assertEqual(
            self.toasts,
            [
                (
                    "“The News” can't start recording: no stream of News One's provider is "
                    "free. QiTV tries again every minute while it airs.",
                    "error",
                )
            ],
        )
        self.at(30, 10)  # tried again: nothing more said
        self.at(31, 20)
        self.assertEqual(len(self.toasts), 1)
        self.budget.release(held)
        self.at(32, 30)  # freed: it records the rest
        self.catalogs.linkReady.emit(1, CHANNEL["cmd"])
        self.assertEqual(len(self.engine.opened), 1)

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
        self.assertEqual(self.budget.holders("P"), [rs_name(self.scheduler, "Ep 2")])
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

    def test_one_slept_through_while_qitv_ran_is_said_missed_then(self):  # audit 3
        self.schedule.add_once(CHANNEL, programme("The News", 30))
        self.now = (T0 + timedelta(minutes=10)).timestamp()
        self.scheduler.start()  # before its window
        self.assertEqual(self.toasts, [])
        self.at(90)  # the computer slept through its window; the clock ticks again
        self.assertEqual(self.toasts, [("Missed while the computer was asleep: The News", "error")])
        self.assertEqual(self.schedule.items(), [])
        self.assertEqual(self.schedule.outcomes()[0]["why"], "asleep")
        self.scheduler.shutdown()

    def test_running_recordings_are_told_and_stopped_at_quit(self):
        self.scheduled_now()
        sid = self.engine.opened[0][0]
        self.engine.sessionReady.emit(sid)
        self.assertEqual([title for title, _channel in self.scheduler.running()], ["The News"])
        self.scheduler.shutdown()
        self.assertEqual(self.engine.stops, [sid])

    # ---- 2.9.7: the Recordings page ----------------------------------------------------------

    def test_a_recording_waiting_for_a_stream_is_listed_and_tried_now_when_asked(self):
        held = self.budget.request("P", 1, purpose="main")
        self.schedule.add_once(CHANNEL, programme("The News", 30))
        self.scheduler.tick()
        ((job, why),) = self.scheduler.waiting()
        self.assertEqual((job.title, why), ("The News", "stream"))
        self.budget.release(held)
        self.scheduler.try_now(job.key)  # no minute's wait
        self.assertEqual(self.scheduler.waiting(), [])
        self.assertEqual(self.catalogs.asked, [("News One", "live")])

    def test_skip_drops_this_airing_a_once_one_leaves_the_schedule(self):
        held = self.budget.request("P", 1, purpose="main")
        self.schedule.add_once(CHANNEL, programme("The News", 30))
        self.scheduler.tick()
        ((job, _why),) = self.scheduler.waiting()
        self.scheduler.skip(job.key)
        self.assertEqual((self.scheduler.waiting(), self.schedule.items()), ([], []))
        self.at(63, 1)
        self.assertEqual(self.schedule.outcomes(), [])  # the user's choice: nothing noted
        self.budget.release(held)

    def test_skip_of_a_timer_s_airing_keeps_the_timer_for_its_next(self):
        held = self.budget.request("P", 1, purpose="main")
        when = datetime.fromtimestamp(self.now).astimezone()
        begin = when.hour * 60 + when.minute
        item = self.schedule.add_timer(CHANNEL, begin=begin, minutes=30, days=range(7))
        self.scheduler.tick()
        ((job, _why),) = self.scheduler.waiting()
        self.scheduler.skip(job.key)
        self.assertEqual(self.schedule.item(item["id"])["kind"], rs.TIMER)
        # final review: skipped as the guide's Skip skips it (its mark and its row go)
        self.assertTrue(self.schedule.is_skipped(item["id"], job.start))
        self.scheduler.tick()
        self.assertEqual(self.scheduler.waiting(), [])  # this airing is never due again
        self.budget.release(held)

    def test_an_edit_drops_a_try_and_is_due_at_its_new_time(self):
        held = self.budget.request("P", 1, purpose="main")
        item = self.schedule.add_once(CHANNEL, programme("The News", 30))
        self.scheduler.tick()
        later = programme("The News", 120)
        edited = self.scheduler.edit(
            item["id"], title="Later", start=later.start.timestamp(), stop=later.stop.timestamp()
        )
        self.assertEqual(edited["title"], "Later")
        self.assertEqual(self.scheduler.waiting(), [])
        self.budget.release(held)

    def test_what_records_is_detailed_for_the_page(self):
        self.scheduled_now()
        sid = self.engine.opened[0][0]
        self.engine.sessionReady.emit(sid)
        ((name, title, channel, _since, until, item_id),) = self.scheduler.recording_details()
        self.assertEqual((title, channel), ("The News", "News One"))
        self.assertTrue(name.startswith("sched:"))
        self.assertEqual(until, (T0 + timedelta(minutes=60)).timestamp() + rs.POST_S)
        self.assertEqual(item_id, self.schedule.items()[0]["id"])
        self.scheduler.shutdown()

    # ---- 2.9.7: one airing skipped (Stage 1 review) -----------------------------------------------

    def series_now(self):
        """A series whose airing at 20:30 is in its window now (it airs on the guide)."""
        item = self.schedule.add_series(CHANNEL, "The News")
        show = programme("The News", 30)
        self.catalogs.programmes = lambda channel, now, limit=8: [show]
        return item, show.start.timestamp()

    def test_skipping_an_airing_waiting_for_a_stream_ends_its_wait_unsaid(self):
        held = self.budget.request("P", 1, purpose="main")
        item, start = self.series_now()
        self.scheduler.tick()
        key = f"{item['id']}@{int(start)}"
        self.assertEqual([job.key for job, _why in self.scheduler.waiting()], [key])
        self.assertEqual(self.scheduler.skip_airing(item["id"], start), "skipped")
        self.assertEqual(self.scheduler.waiting(), [])
        self.assertTrue(self.schedule.is_skipped(item["id"], start))
        said = len(self.toasts)
        self.at(63, 1)  # its window ends: nothing said, nothing noted
        self.assertEqual(len(self.toasts), said)
        self.assertEqual(self.schedule.outcomes(), [])
        self.budget.release(held)

    def test_skipping_an_airing_whose_stream_is_on_its_way_lets_it_go(self):
        item, start = self.series_now()
        self.scheduler.tick()  # its link asked for
        self.assertEqual(self.scheduler.skip_airing(item["id"], start), "skipped")
        self.catalogs.linkReady.emit(1, CHANNEL["cmd"])
        self.assertEqual(self.engine.opened, [])  # nothing opens for it
        self.assertEqual(self.budget.holders("P"), [])

    def test_skipping_an_airing_that_records_stops_it_and_the_series_goes_on(self):
        item, start = self.series_now()
        self.scheduler.tick()
        self.catalogs.linkReady.emit(1, CHANNEL["cmd"])
        sid = self.engine.opened[0][0]
        self.engine.sessionReady.emit(sid)
        self.assertEqual(self.scheduler.skip_airing(item["id"], start), "stopped")
        self.assertEqual(self.engine.stops, [sid])  # its file is kept
        self.assertEqual([i["id"] for i in self.schedule.items()], [item["id"]])

    # ---- 2.9.7: watching what records, with no second stream ------------------------------------

    LISTED = {"id": "7", "name": "News One", "cmd": "http://panel.test/live/u/p/7.ts"}

    def lent(self):
        self.scheduled_now()
        sid = self.engine.opened[0][0]
        self.engine.sessionReady.emit(sid)
        rid, _sid, path, *_ = self.engine.recordings[0]
        return sid, rid, path

    def test_a_recording_lends_its_session_to_a_player_of_its_channel(self):
        self.assertIsNone(self.scheduler.lend(self.LISTED, "P", "pip"))  # nothing records
        sid, _rid, _path = self.lent()
        self.assertTrue(self.scheduler.lendable(self.LISTED, "P"))
        self.assertFalse(self.scheduler.lendable(self.LISTED, "Q"))  # another provider's
        other = dict(self.LISTED, id="8", cmd="http://panel.test/live/u/p/8.ts")
        self.assertFalse(self.scheduler.lendable(other, "P"))
        borrowed = self.scheduler.lend(self.LISTED, "P", "pip")
        self.assertEqual(borrowed.sid, sid)
        self.assertTrue(borrowed.recording())
        self.assertEqual(len(self.engine.opened), 1)  # no second session, no second stream
        self.assertEqual(len(self.budget.holders("P")), 1)
        borrowed.give_back()
        borrowed.give_back()  # once is all it counts
        self.assertEqual(self.engine.closed, [])  # it records on

    def test_a_recording_that_ends_while_watched_keeps_its_session_for_the_viewer(self):
        sid, rid, path = self.lent()
        borrowed = self.scheduler.lend(self.LISTED, "P", "main")
        self.at(63, 1)  # its window ends
        self.engine.recordingEnded.emit(rid, "stopped", path, 1_000_000)
        self.pump()
        self.assertEqual(self.engine.closed, [])  # the viewer still watches it
        self.assertEqual(self.budget.holders("P"), ["main"])  # the viewer's stream now
        self.assertFalse(borrowed.recording())
        self.assertEqual(self.scheduler.recording_details(), [])  # nothing records
        self.assertTrue(self.scheduler.lendable(self.LISTED, "P"))  # still watchable
        borrowed.give_back()
        self.assertEqual(self.engine.closed, [sid])
        self.engine.sessionClosed.emit(sid)
        self.assertEqual(self.budget.holders("P"), [])

    def test_a_recording_that_breaks_while_watched_records_the_rest_meanwhile(self):  # release
        sid, rid, path = self.lent()
        borrowed = self.scheduler.lend(self.LISTED, "P", "main")  # watched behind live
        self.now = (T0 + timedelta(minutes=40)).timestamp()
        self.engine.sessionFailed.emit(sid, "http", "HTTP 502")  # the provider's stream broke
        self.engine.recordingEnded.emit(rid, "stopped", path, 9_000)
        self.pump()
        self.assertEqual(self.engine.closed, [])  # the viewer keeps what is buffered
        self.assertEqual(self.budget.holders("P"), [])  # no stream of it is open any more
        self.assertFalse(self.scheduler.lendable(self.LISTED, "P"))  # nobody borrows it now
        self.at(41, 10)  # without waiting for the viewer to catch up or leave
        self.assertEqual(len(self.catalogs.asked), 2)  # the rest records on a stream of its own
        borrowed.give_back()
        self.assertEqual(self.engine.closed, [sid])
        self.engine.sessionClosed.emit(sid)
        self.scheduler.shutdown()

    def test_the_next_programme_records_on_a_session_kept_for_a_viewer(self):
        sid, rid, path = self.lent()
        borrowed = self.scheduler.lend(self.LISTED, "P", "main")
        self.at(63, 1)
        self.engine.recordingEnded.emit(rid, "stopped", path, 1_000_000)
        self.pump()
        self.schedule.add_once(CHANNEL, programme("Late News", 66))
        self.at(65, 30)  # its minute early
        self.assertEqual(len(self.engine.opened), 1)  # no new session: the kept one records
        self.assertEqual(len(self.catalogs.asked), 1)
        rid2, sid2, *_ = self.engine.recordings[1]
        self.assertEqual(sid2, sid)
        self.assertEqual(self.budget.holders("P"), [rs_name(self.scheduler, "Late News")])
        self.assertTrue(borrowed.recording())
        borrowed.give_back()
        self.assertEqual(self.engine.closed, [])  # the recording keeps it
        self.scheduler.shutdown()

    def test_a_kept_session_goes_on_under_a_viewer_still_watching(self):
        sid, rid, path = self.lent()
        main = self.scheduler.lend(self.LISTED, "P", "main")
        pip = self.scheduler.lend(self.LISTED, "P", "pip")
        pip.give_back()  # the small picture closed: the big one still watches
        self.at(63, 1)
        self.engine.recordingEnded.emit(rid, "stopped", path, 1_000_000)
        self.pump()
        self.assertEqual(self.budget.holders("P"), ["main"])  # not the closed picture's name
        main.give_back()
        self.assertEqual(self.engine.closed, [sid])
        # Closing: neither lent again nor recorded on, until the helper says it closed.
        self.assertIsNone(self.scheduler.lend(self.LISTED, "P", "main"))
        self.schedule.add_once(CHANNEL, programme("Late News", 66))
        self.at(65, 30)
        self.assertEqual(len(self.engine.recordings), 1)
        self.engine.sessionClosed.emit(sid)

    def test_a_listed_channel_is_found_by_its_address_without_the_account(self):
        kept = dict(CHANNEL, cmd="https://panel.test/live/{username}/{password}/7.ts")
        self.assertTrue(rs.is_listed(kept, self.LISTED))  # http or https alike
        self.assertFalse(
            rs.is_listed(kept, dict(self.LISTED, cmd="http://panel.test/live/u/p/8.ts"))
        )
        # A playlist's channels: ids are list places and a name may repeat in another group.
        a = {"id": "3", "name": "News", "cmd": "http://cdn.test/news-hd.m3u8"}
        b = {"id": "3", "name": "News", "cmd": "http://cdn.test/news-sd.m3u8"}
        self.assertFalse(rs.is_listed(a, b))
        self.assertTrue(rs.is_listed({"id": "3", "name": "News"}, b))  # no address: its id


class FakePlayer:
    """A player's LiveSession as the scheduler sees it (2.9.7): it records on its own session."""

    def __init__(self, engine, sid="main-sid"):
        self.engine, self.sid = engine, sid
        self.playing = True  # from its buffer
        self.rid = None
        self.started = []  # (path, since)

    def recordable(self):
        return self.playing

    def recording(self):
        return self.rid is not None

    def recording_id(self):
        return self.rid

    def start_recording(self, path, *, since=None):
        if not self.recordable() or self.rid is not None:
            return False
        self.started.append((path, since))
        self.rid = self.engine.start_recording(self.sid, path, 0, max_s=0, min_free=0)
        return True

    def stop_recording(self):
        self.rid = None
        self.engine.stop_recording(self.sid)


class RecordOnPlayerTests(unittest.TestCase):
    """2.9.7: a programme due on a channel a player shows records on that player: no stream
    of its own (a one-stream provider records the channel watched)."""

    # The list's channel: the provider's account in its address, which the schedule keeps
    # without it (its copy's "{username}/{password}").
    LISTED = dict(CHANNEL, cmd="http://panel.test/live/u/p/7.ts")

    setUp = SchedulerTests.setUp
    pump = SchedulerTests.pump
    at = SchedulerTests.at

    def watch(self, channel=None, provider="P"):
        self.held = self.budget.request("P", 1, purpose="main")  # the channel watched holds it
        self.player = FakePlayer(self.engine)
        self.scheduler.players = lambda: [("main", provider, channel or self.LISTED, self.player)]

    def test_a_programme_on_the_channel_watched_records_on_its_player(self):
        self.watch()
        self.schedule.add_once(CHANNEL, programme("The News", 30), account=ACCOUNT)
        self.scheduler.tick()
        self.assertEqual(self.catalogs.asked, [])  # no link, no stream of its own
        self.assertEqual(self.engine.opened, [])
        self.assertEqual(self.budget.holders("P"), ["main"])
        ((path, since),) = self.player.started
        self.assertLessEqual(since, (T0 + timedelta(minutes=30)).timestamp())  # its start
        self.assertTrue(os.path.basename(path).startswith("News One "))
        self.assertEqual(self.toasts[-1][1], "info")  # it records: no "no stream" said
        self.assertEqual(self.scheduler.recording_now(), [])  # the player's REC counts it
        self.assertEqual(self.scheduler.hosted_players(), {"main"})
        self.assertEqual(len(self.scheduler.recording_details()), 1)  # the Recordings page
        ((name, *_rest),) = self.scheduler.recording_details()
        self.assertEqual(self.scheduler.host_of(name), "main")  # its Watch: that player

    def test_a_stop_on_its_background_player_is_the_users_not_a_break(self):  # review 1
        """The REC sign's Stop on a player in the background (its slot then leaves the
        channel before the helper's end arrives): this airing is done, not failed, and no
        stream of its own records the rest."""
        self.catalogs.max = 2  # a second stream would be free
        self.watch()
        self.schedule.add_once(CHANNEL, programme("The News", 30), account=ACCOUNT)
        self.scheduler.tick()
        rid, _sid, path, *_ = self.engine.recordings[0]
        self.at(40)
        self.scheduler.stopped_by_user(self.player)
        self.player.stop_recording()
        self.player.playing = False  # its slot left the channel
        self.engine.recordingEnded.emit(rid, "stopped", path, 50_000)
        (outcome,) = self.schedule.outcomes()
        self.assertEqual(outcome["state"], rs.RECORDED)
        self.at(41, 30)
        self.assertEqual(self.catalogs.asked, [])  # the rest isn't recorded on a stream

    def test_back_to_back_on_its_player_the_player_stays_for_the_next(self):  # review 2
        """A then B on the channel a player shows: as A stops for B, the player is still
        wanted (a background one stays on its channel), and B records on it: no stream."""
        self.watch()
        self.schedule.add_once(CHANNEL, programme("A", 30), account=ACCOUNT)
        self.schedule.add_once(CHANNEL, programme("B", 60), account=ACCOUNT)
        self.scheduler.tick()
        asked = []
        stop = self.player.stop_recording

        def stopping():  # the player's slot asks as its recording stops
            asked.append(self.scheduler.wants(self.player))
            stop()

        self.player.stop_recording = stopping
        self.at(59, 30)
        self.at(60, 31)  # B's handover: A stops
        rid, _sid, path, *_ = self.engine.recordings[0]
        self.engine.recordingEnded.emit(rid, "stopped", path, 1_000_000)
        self.pump()
        self.assertEqual(asked, [True])
        self.assertEqual(len(self.player.started), 2)  # B on the same player
        self.assertEqual(self.catalogs.asked, [])
        self.assertFalse(self.scheduler.wants(self.player))  # nothing follows B

    def test_its_window_end_stops_the_players_recording_and_the_player_plays_on(self):
        self.watch()
        self.schedule.add_once(CHANNEL, programme("The News", 30), account=ACCOUNT)
        self.scheduler.tick()
        rid, sid, path, *_ = self.engine.recordings[0]
        said = len(self.toasts)
        self.at(63, 1)
        self.assertEqual(self.engine.stops, [sid])
        self.assertFalse(self.player.recording())
        self.engine.recordingEnded.emit(rid, ENDED, path, 1_000_000)
        self.assertEqual(self.engine.closed, [])  # the player's session stays the player's
        (outcome,) = self.schedule.outcomes()
        self.assertEqual((outcome["state"], outcome["path"]), (rs.RECORDED, path))
        self.assertEqual(len(self.toasts), said)  # the player says how it ended
        self.assertEqual(self.scheduler.hosted_players(), set())
        self.assertEqual(self.budget.holders("P"), ["main"])

    def test_one_that_found_no_stream_records_once_the_channel_is_watched(self):
        held = self.budget.request("P", 1, purpose="main")  # another channel holds it
        self.schedule.add_once(CHANNEL, programme("The News", 30), account=ACCOUNT)
        self.scheduler.tick()
        self.assertIn("no stream", self.toasts[-1][0])
        self.player = FakePlayer(self.engine)
        self.scheduler.players = lambda: [("main", "P", self.LISTED, self.player)]
        self.at(30, 10)  # tried again: the viewer tuned its channel meanwhile
        self.assertEqual(len(self.player.started), 1)
        self.assertEqual(self.engine.opened, [])
        self.assertEqual(self.scheduler.waiting(), [])
        self.budget.release(held)

    def test_a_player_already_recording_or_another_providers_is_not_used(self):
        self.catalogs.max = 2
        self.watch(provider="Q")  # the same channel at another provider
        self.schedule.add_once(CHANNEL, programme("The News", 30), account=ACCOUNT)
        self.scheduler.tick()
        self.assertEqual(self.player.started, [])
        self.assertEqual(len(self.catalogs.asked), 1)  # a stream of its own

    def test_a_recording_cut_while_it_airs_goes_on_from_where_it_broke(self):
        self.watch()
        self.schedule.add_once(CHANNEL, programme("The News", 30), account=ACCOUNT)
        self.scheduler.tick()
        rid, _sid, path, *_ = self.engine.recordings[0]
        self.at(40)
        self.player.rid = None  # its stream failed: the player's recording ends
        self.engine.recordingEnded.emit(rid, "failed", path, 50_000)
        broke = self.now
        self.at(41, 30)
        self.assertEqual(len(self.player.started), 2)
        self.assertEqual(self.player.started[1][1], broke)  # the rest, not again from its start

    def test_a_players_stream_that_ended_while_it_airs_records_the_rest(self):
        # The provider closed the stream, or the player fell back to direct play (its
        # recording "stopped" with no buffer to play): not a finished recording.
        for reason, buffered in (("ended", True), ("stopped", False)):
            with self.subTest(reason=reason):
                self.setUp()
                self.watch()
                self.schedule.add_once(CHANNEL, programme("The News", 30), account=ACCOUNT)
                self.scheduler.tick()
                rid, _sid, path, *_ = self.engine.recordings[0]
                self.at(40)
                self.player.rid, self.player.playing = None, buffered
                self.engine.recordingEnded.emit(rid, reason, path, 50_000)
                (outcome,) = self.schedule.outcomes()
                self.assertEqual(outcome["state"], rs.FAILED)  # its part kept, noted
                self.player.playing = True  # the channel plays from its buffer again
                self.at(41, 30)
                self.assertEqual(len(self.player.started), 2)
                self.budget.release(self.held)

    def test_a_recording_stopped_on_its_player_is_done_for_this_airing(self):
        self.watch()
        self.schedule.add_once(CHANNEL, programme("The News", 30), account=ACCOUNT)
        self.scheduler.tick()
        rid, _sid, path, *_ = self.engine.recordings[0]
        self.at(40)
        self.player.stop_recording()  # the viewer pressed R; the player plays on
        self.engine.recordingEnded.emit(rid, "stopped", path, 50_000)
        self.at(41, 30)
        self.assertEqual(len(self.player.started), 1)
        (outcome,) = self.schedule.outcomes()
        self.assertEqual(outcome["state"], rs.RECORDED)


if __name__ == "__main__":
    unittest.main()
