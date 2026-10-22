"""2.6, Record as a PVR: a recording keeps its player when its channel is left."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from test_multiview import MultiviewCase
from test_tv_controller import CHANNELS, FakeLiveSession, FakeSession, play_context
from test_tv_pip import MOVIE, PipCase
from timer_fixtures import wait_until

from services.sessions import Slot
from ui.live_tuner import LiveTuner


class RecordingAsideTests(PipCase):
    def setUp(self):
        super().setUp()

        def make_slot(name):
            session = FakeSession()
            live = FakeLiveSession(session)
            tuner = LiveTuner(session, self.config, live, self.budget, purpose=name)
            slot = Slot(name, session, live, tuner=tuner)
            self.sessions.add_slot(slot)
            self.controller.slot_added(slot)
            self.addCleanup(live.released.emit)
            return slot

        self.sessions.set_slot_factory(make_slot)

    def record_large(self, index=0):
        self.start_large(index)
        recording = self.controller.live
        self.assertTrue(recording.start_recording("/tmp/x.ts"))
        return recording

    def leave_for(self, index):
        """Tune channel `index` while the large picture records: another player opens it."""
        self.play(index)
        stand_in = self.controller.live
        self.assertTrue(wait_until(lambda: len(stand_in.named("start")) == 1))
        stand_in.play_pipeline()
        return stand_in

    def since_recording(self, live):
        return live.calls[live.calls.index(("start_recording", "/tmp/x.ts")) :]

    def test_a_zap_away_from_a_recording_keeps_it(self):
        recording = self.record_large(0)
        self.leave_for(1)
        self.assertNotIn(("stop",), self.since_recording(recording))
        self.assertIn(("set_aside",), recording.calls)
        self.assertEqual([s.name for s in self.sessions.background()], ["main"])
        self.assertEqual(self.sessions.large().name, "b1")
        self.assertIn(("set_roles", "b1", "pip"), self.frame.calls)
        self.assertEqual(self.controller.live.named("start")[-1][0], "http://stream/2")
        self.assertEqual(sorted(self.budget.holders("Home")), ["b1", "main"])

    def test_tuning_the_recording_channel_brings_it_back(self):
        recording = self.record_large(0)
        stand_in = self.leave_for(1)
        self.controller.tune(CHANNELS[0], group_key="10", channels=CHANNELS)
        self.assertIs(self.controller.live, recording)
        self.assertEqual(recording.calls[-1], ("bring_back",))
        self.assertEqual(self.sessions.background(), [])
        self.assertIn(("stop",), stand_in.calls)  # the channel it showed is left
        self.assertEqual(self.controller._channel, CHANNELS[0])

    def test_a_film_while_recording_keeps_it(self):
        recording = self.record_large(0)
        self.controller.play_vod(MOVIE, "movie", play_context())
        self.ready("http://vod/heat.mkv")
        self.assertNotIn(("stop",), self.since_recording(recording))
        self.assertEqual([s.name for s in self.sessions.background()], ["main"])
        stand_in = self.sessions.large()
        self.assertEqual(stand_in.name, "b1")
        self.assertEqual([url for url, _ in stand_in.playback.plays], ["http://vod/heat.mkv"])

    def test_the_library_keeps_a_recording(self):
        recording = self.record_large(0)
        self.controller.stop_for_library()
        self.assertNotIn(("stop",), self.since_recording(recording))
        self.assertEqual([s.name for s in self.sessions.background()], ["main"])

    def test_closing_a_recording_pip_keeps_it(self):
        self.start_large(0)
        small = self.open_small(CHANNELS[1])
        self.assertTrue(small.live.start_recording("/tmp/x.ts"))
        self.controller._pip.close()
        self.assertFalse(self.sessions.pip_open())
        self.assertNotIn(("stop",), self.since_recording(small.live))
        self.assertEqual([s.name for s in self.sessions.background()], ["pip"])
        self.assertEqual(small.live.calls[-1], ("set_aside",))
        self.assertIn(("set_roles", "main", "b1"), self.frame.calls)

    def test_the_recording_channel_opens_small_without_a_stream(self):
        recording = self.record_large(0)
        self.leave_for(1)
        links = len(self.catalog.links)
        self.controller.open_pip(CHANNELS[0], group_key="10", channels=CHANNELS)
        self.assertEqual(len(self.catalog.links), links)
        self.assertEqual(self.sessions.small().name, "main")
        self.assertEqual(recording.calls[-1], ("bring_back",))
        self.assertIn(("set_roles", "b1", "main"), self.frame.calls)
        self.assertEqual(self.sessions.background(), [])

    def test_quitting_stops_the_recordings_in_the_background(self):
        recording = self.record_large(0)
        self.leave_for(1)
        self.controller.stop_live()
        self.assertIn(("stop",), self.since_recording(recording))
        self.assertEqual(self.sessions.background(), [])


class RecordingTileTests(MultiviewCase):
    def setUp(self):
        super().setUp()
        self.sessions.set_slot_factory(self.make_slot)

    def test_leaving_the_grid_keeps_a_recording_tile(self):
        self.catalog.current_provider = lambda: {"name": "Home", "type": "STB", "max_streams": 4}
        self.start_large(0)
        self.v()
        t2 = self.pick_in("t2", CHANNELS[2], "http://stream/3")
        t3 = self.pick_in("t3", CHANNELS[3], "http://stream/4")
        self.assertTrue(t2.live.start_recording("/tmp/x.ts"))
        self.grid_frame.focusRequested.emit("main")
        self.v()
        self.assertIs(self.sessions.small(), t3)  # focused just before
        self.assertNotIn(
            ("stop",), t2.live.calls[t2.live.calls.index(("start_recording", "/tmp/x.ts")) :]
        )
        self.assertEqual([s.name for s in self.sessions.background()], ["t2"])
        self.assertTrue(self.sessions.muted("t2"))

    def test_a_background_player_is_no_tile(self):
        self.start_large(0)
        self.assertTrue(self.controller.live.start_recording("/tmp/x.ts"))
        self.play(1)
        stand_in = self.controller.live
        self.assertTrue(wait_until(lambda: len(stand_in.named("start")) == 1))
        stand_in.play_pipeline()
        self.v()
        self.assertEqual(self.grid.names(), ["b1", "pip", "t2", "t3"])


if __name__ == "__main__":
    unittest.main()
