"""Provider catch-up in the player (2.3): rewinding past the buffer opens the provider's
archive, the connection budget orders the streams, Go live comes back, failures are learned.

A fake LiveSession and player (tests/test_tv_controller.py's) with a real connection budget;
probes run through a fake runner the tests answer. Channels and addresses are fictional.
"""

from datetime import timedelta
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from test_tv_controller import NOW, PipelineCase
from timer_fixtures import wait_until

from services.catchup_memory import CatchupMemory
from services.epg_view import Programme
from services.live.tuning import channel_key
from services.vlc_profile import PlayKind
from ui.catchup_controls import (
    ARCHIVE_TEXT,
    NO_ANSWER_TEXT,
    NO_RECORDING_TEXT,
    NOT_AVAILABLE_TEXT,
)
from ui.keymap import Action

# An Xtream panel's channel with three days of archive, and one without.
ARCHIVED = {
    "id": 1,
    "name": "Harbour News",
    "number": "1",
    "cmd": "http://panel.test/live/viewer/secret/1.ts",
    "archive_days": 3,
}
PLAIN = {
    "id": 2,
    "name": "Quiet Pier",
    "number": "2",
    "cmd": "http://panel.test/live/viewer/secret/2.ts",
}
# An M3U channel whose playlist appends its catch-up query.
APPENDED = {
    "id": 3,
    "name": "Lighthouse Sport",
    "number": "3",
    "cmd": "http://m3u.test/3.ts",
    "archive_days": 2,
    "catchup": "append",
    "catchup_source": "?utc={utc}&lutc={lutc}",
}
CHANNELS = [ARCHIVED, PLAIN, APPENDED]
# The buffer holds the last 10 minutes; it plays 9:55 behind live.
STATE = {
    "start": 3000.0,
    "end": 3600.0,
    "position": 3005.0,
    "live_edge": 3600.0,
    "lag": 595.0,
    "mode": "shifted",
    "rate": 1.0,
    "paused": False,
    "target": 2.5,
}


class FakeRunner:
    """Keeps each probe; the test answers it."""

    def __init__(self):
        self.jobs = []

    def run_worker(self, worker, on_finished, on_error=None, context=None):
        self.jobs.append((worker, on_finished, on_error, context))
        return worker

    def answer(self, index, answer):
        worker, on_finished, _on_error, context = self.jobs.pop(0)
        on_finished(context, (index, answer))
        return worker


class CatchupCase(PipelineCase):
    def setUp(self):
        super().setUp()
        self.config.ssl_verify = True
        self.catalog.live = {"10": list(CHANNELS)}
        self.catalog.ptype = "XTREAM"  # 1 stream (FakeCatalog.max_connections)
        self.memory = CatchupMemory(None)
        self.runner = FakeRunner()
        catchup = self.controller._live_controls.catchup
        catchup.memory = self.memory
        catchup._runner = self.runner
        self.catchup = catchup
        self.session.time_ms = lambda: self.played_ms
        self.played_ms = 0

    def tune_channel(self, channel):
        self.controller.tune(channel, group_key="10", channels=CHANNELS)
        self.catalog.linkReady.emit(self.catalog.links[-1][0], channel["cmd"])

    def start_archived(self, channel=ARCHIVED):
        count = len(self.starts())
        self.tune_channel(channel)
        self.assertTrue(wait_until(lambda: len(self.starts()) == count + 1))
        self.live.play_pipeline()
        self.live.timelineChanged.emit(dict(STATE))

    def archive_plays(self):
        return [
            (url, kw) for url, kw in self.session.plays if kw.get("kind") is PlayKind.VOD_STREAM
        ]

    def in_use(self):
        return self.budget.in_use("Home")


class RewindTests(CatchupCase):
    def test_a_step_back_past_the_buffer_opens_the_archive_after_live_lets_go(self):
        self.start_archived()
        self.controller.handle(Action.SEEK_BACK)  # 10 s back from 5 s after the buffer's start
        self.assertEqual(self.live.named("seek_relative"), [])
        self.assertEqual(self.live.calls[-1], ("stop",))  # live closes first ...
        self.assertEqual(self.archive_plays(), [])  # ... and nothing else opens meanwhile
        self.assertLessEqual(self.in_use(), 1)
        self.live.released.emit()  # its connection is closed
        [(url, kwargs)] = self.archive_plays()
        # 9:55 + 10 s before 20:30:00 is 20:19:55: the minute's address, 55 s in.
        self.assertEqual(url, "http://panel.test/timeshift/viewer/secret/11/2026-10-15:20-19/1.ts")
        self.assertEqual(kwargs["start_ms"], 55_000)
        self.assertEqual(self.budget.holders("Home"), ["main"])  # one stream, never two
        self.assertIn((ARCHIVE_TEXT, "info"), self.toast.messages())
        self.assertEqual(self.runner.jobs, [])  # a 1-stream account is never probed

    def test_the_archive_plays_on_its_own_bar_and_go_live_tunes_the_channel_again(self):
        self.start_archived()
        self.controller.mouse_moved()
        self.controller.handle(Action.SEEK_BACK)
        self.live.released.emit()
        self.session.stateChanged.emit("playing")
        self.played_ms = 65_000  # a minute and 5 s into the minute's address
        self.session.positionChanged.emit(65_000, 0)
        state, _ticks = self.transport.named("update_timeline")[-1]
        self.assertTrue(state["archive"])
        self.assertEqual(state["position"], NOW.timestamp() - 660 + 65)
        self.assertEqual(state["live_edge"], NOW.timestamp())
        self.assertEqual(state["mode"], "shifted")
        self.assertEqual(self.memory.answered(channel_key("Home", ARCHIVED)), 0)  # it answered
        starts = len(self.starts())
        links = len(self.catalog.links)
        self.controller.handle(Action.GO_LIVE)
        self.assertFalse(self.catchup.active())
        self.assertEqual(len(self.catalog.links), links + 1)  # the channel's link again
        self.catalog.linkReady.emit(self.catalog.links[-1][0], ARCHIVED["cmd"])
        self.assertTrue(wait_until(lambda: len(self.starts()) == starts + 1))
        self.assertEqual(self.starts()[-1][0], ARCHIVED["cmd"])  # live through the buffer
        self.assertEqual(self.budget.holders("Home"), ["main"])

    def test_a_drag_into_the_dashed_part_and_programme_start_open_the_archive(self):
        # (The fake guide lists from its first programme, whatever the time asked.)
        self.catalog.epg[1] = [
            Programme("Noon", NOW - timedelta(minutes=20), NOW + timedelta(minutes=40))
        ]
        self.start_archived()
        self.controller.mouse_moved()
        state, _ticks = self.transport.named("update_timeline")[-1]
        self.assertEqual(state["buffer_start"], 3000.0)  # the buffer's start ...
        self.assertEqual(state["start"], 3000.0 - 1800.0)  # ... and 30 min of archive before
        self.transport.seekToRequested.emit(1500.0)  # 35 min ago: the archive's part
        self.live.released.emit()
        url = self.archive_plays()[-1][0]
        self.assertIn("/2026-10-15:19-55/1.ts", url)
        self.controller.handle(Action.GO_LIVE)
        self.catalog.linkReady.emit(self.catalog.links[-1][0], ARCHIVED["cmd"])
        self.session.stateChanged.emit("idle")
        self.assertTrue(wait_until(lambda: self.live.running))
        self.live.play_pipeline()
        self.live.timelineChanged.emit(dict(STATE, position=3100.0))  # 20:21:40: Noon
        self.transport.programmeStartRequested.emit()  # Noon began 20:10, before the buffer
        self.live.released.emit()
        self.assertIn("/2026-10-15:20-10/1.ts", self.archive_plays()[-1][0])

    def test_a_channel_without_an_archive_rewinds_as_before(self):
        self.start_archived(PLAIN)
        self.controller.mouse_moved()
        state, _ticks = self.transport.named("update_timeline")[-1]
        self.assertNotIn("buffer_start", state)
        self.controller.handle(Action.SEEK_BACK)
        self.assertEqual(self.live.named("seek_relative"), [(-10.0,)])
        self.assertEqual(self.archive_plays(), [])

    def test_a_step_back_within_the_buffer_stays_in_it(self):
        self.start_archived()
        self.live.timelineChanged.emit(dict(STATE, position=3300.0))
        self.controller.handle(Action.SEEK_BACK)
        self.assertEqual(self.live.named("seek_relative"), [(-10.0,)])
        self.assertFalse(self.catchup.active())

    def test_an_external_player_or_the_multiview_never_opens_an_archive(self):
        self.start_archived()
        self.config.play_in_vlc = True
        self.controller.handle(Action.SEEK_BACK)
        self.assertEqual(self.live.named("seek_relative"), [(-10.0,)])
        self.assertFalse(self.catchup.active())


class ArchiveModeTests(CatchupCase):
    def setUp(self):
        super().setUp()
        self.start_archived()
        self.controller.mouse_moved()
        self.controller.handle(Action.SEEK_BACK)
        self.live.released.emit()
        self.session.stateChanged.emit("playing")

    def test_steps_open_the_archive_anew_where_vlc_cannot_seek(self):
        self.played_ms = 55_000
        self.controller.handle(Action.SEEK_BACK)  # 20:19:45
        self.assertEqual(self.archive_plays()[-1][1]["start_ms"], 45_000)
        self.assertIn("/2026-10-15:20-19/1.ts", self.archive_plays()[-1][0])
        self.assertEqual(self.budget.holders("Home"), ["main"])

    def test_vlc_seeks_within_an_address_it_can_seek(self):
        self.session.seekable = True
        self.played_ms = 55_000
        plays = len(self.session.plays)
        self.transport.seekRelativeRequested.emit(60_000)
        self.assertEqual(self.session.seeks, [115_000])
        self.assertEqual(len(self.session.plays), plays)

    def test_a_seek_near_now_goes_live(self):
        links = len(self.catalog.links)
        self.transport.seekToRequested.emit(NOW.timestamp() - 5)
        self.assertFalse(self.catchup.active())
        self.assertEqual(len(self.catalog.links), links + 1)

    def test_the_stretch_ending_opens_the_next_one(self):
        self.played_ms = 11 * 60_000
        self.session.ended.emit()  # 20:30:00 is now: live
        self.assertFalse(self.catchup.active())
        self.assertNotIn(("The stream ended", "info"), self.toast.messages())

    def test_a_stretch_that_ends_at_once_goes_live(self):
        self.played_ms = 5_000  # far from now, but only 5 s played
        links = len(self.catalog.links)
        self.session.ended.emit()
        self.assertFalse(self.catchup.active())
        self.assertIn((NO_ANSWER_TEXT, "info"), self.toast.messages())
        self.assertEqual(len(self.catalog.links), links + 1)

    def test_record_says_it_cannot(self):
        self.controller.handle(Action.RECORD)
        self.assertIn((NO_RECORDING_TEXT, "info"), self.toast.messages())

    def test_a_zap_leaves_the_archive(self):
        self.tune_channel(PLAIN)
        self.assertFalse(self.catchup.active())


class FailureTests(CatchupCase):
    def test_an_unprobed_archive_failing_at_once_is_remembered_and_live_comes_back(self):
        self.start_archived()
        self.live.timelineChanged.emit(dict(STATE, position=3000.0 - 0.0, start=3000.0))
        self.transport.seekToRequested.emit(1000.0)  # 43 min ago
        self.live.released.emit()
        links = len(self.catalog.links)
        self.session.errorOccurred.emit("The stream could not be opened")
        self.assertIn((NO_ANSWER_TEXT, "info"), self.toast.messages())
        self.assertNotIn(("The stream could not be opened", "error"), self.toast.messages())
        self.assertTrue(self.memory.failed(channel_key("Home", ARCHIVED)))
        self.assertEqual(len(self.catalog.links), links + 1)  # the channel live again
        self.catalog.linkReady.emit(self.catalog.links[-1][0], ARCHIVED["cmd"])
        self.session.stateChanged.emit("idle")
        self.assertTrue(wait_until(lambda: self.live.running))
        self.live.play_pipeline()
        self.live.timelineChanged.emit(dict(STATE))
        self.controller.mouse_moved()
        state, _ticks = self.transport.named("update_timeline")[-1]
        self.assertNotIn("buffer_start", state)  # no archive on the bar now
        self.controller.handle(Action.SEEK_BACK)
        self.assertIn((NOT_AVAILABLE_TEXT, "info"), self.toast.messages())
        self.assertEqual(self.live.named("seek_relative")[-1], (-10.0,))  # the buffer only


class ProbeTests(CatchupCase):
    """An M3U playlist names no limit: the archive is probed while the channel plays on."""

    def setUp(self):
        super().setUp()
        self.catalog.ptype = "M3UPLAYLIST"
        self.start_archived(APPENDED)

    def test_a_probe_that_plays_opens_the_archive(self):
        self.controller.handle(Action.SEEK_BACK)
        self.assertEqual(sorted(self.budget.holders("Home")), ["main", "probe"])
        self.assertNotIn(("stop",), self.live.calls[-1:])  # the channel plays on meanwhile
        [(worker, *_rest)] = self.runner.jobs
        moment = int(NOW.timestamp()) - 605
        self.assertEqual(
            worker.urls, [f"http://m3u.test/3.ts?utc={moment}&lutc={int(NOW.timestamp())}"]
        )
        self.runner.answer(0, "plays")
        self.assertEqual(self.budget.holders("Home"), ["main"])  # the probe's slot is back
        self.assertEqual(self.live.calls[-1], ("stop",))
        self.live.released.emit()
        self.assertEqual(self.archive_plays()[-1][0], worker.urls[0])
        self.assertEqual(self.memory.answered(channel_key("Home", APPENDED)), 0)

    def test_nothing_there_is_said_remembered_and_the_channel_plays_on(self):
        self.live.timelineChanged.emit(dict(STATE))
        self.transport.seekToRequested.emit(1000.0)  # 43 min ago
        self.runner.answer(-1, "none")
        self.assertEqual(self.budget.holders("Home"), ["main"])
        self.assertIn((NO_ANSWER_TEXT, "info"), self.toast.messages())
        self.assertTrue(self.memory.failed(channel_key("Home", APPENDED)))
        self.assertTrue(self.live.running)
        self.assertEqual(self.archive_plays(), [])

    def test_a_refusal_teaches_nothing(self):
        self.controller.handle(Action.SEEK_BACK)
        self.runner.answer(-1, "refused")
        self.assertFalse(self.memory.failed(channel_key("Home", APPENDED)))
        self.assertTrue(self.live.running)

    def test_steps_while_it_probes_ask_once_and_play_the_newest_moment(self):
        self.live.timelineChanged.emit(dict(STATE))
        self.transport.seekToRequested.emit(1000.0)  # 43:20 ago
        self.transport.seekToRequested.emit(900.0)  # then 45:00 ago, while it probes
        self.assertEqual(len(self.runner.jobs), 1)
        self.runner.answer(0, "plays")
        self.live.released.emit()
        moment = int(NOW.timestamp()) - 2700
        self.assertIn(f"utc={moment}&", self.archive_plays()[-1][0])
        self.assertEqual(self.budget.holders("Home"), ["main"])

    def test_a_probe_answered_after_a_zap_is_dropped(self):
        self.controller.handle(Action.SEEK_BACK)
        self.tune_channel(PLAIN)
        self.runner.answer(0, "plays")
        self.assertEqual(self.archive_plays(), [])
        self.assertNotIn("probe", self.budget.holders("Home"))


if __name__ == "__main__":
    unittest.main()
