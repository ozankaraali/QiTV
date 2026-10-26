"""LiveSession's recording (2.2): where it starts, its stops, and the ends it reports."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from services.live.recorder import LIMIT, MAX_SECONDS, MIN_FREE_BYTES, STOPPED
from services.playback import PlaybackState
from tests.test_live_session import LiveSessionCase, segments

PATH = "/Movies/QiTV/One 2026-10-19 21.30.ts"


class RecordingTests(LiveSessionCase):
    def setUp(self):
        super().setUp()
        self.changes, self.ends = [], []
        self.live.recordingChanged.connect(self.changes.append)
        self.live.recordingEnded.connect(lambda *end: self.ends.append(end))
        closing = self.engine.close_session

        def close(sid):
            self.engine.events.append(("close", sid))
            closing(sid)

        self.engine.close_session = close

    def playing(self):
        sid = self.ready(segments(0, 10), live_edge=20.5)
        self.playback.set_state(PlaybackState.PLAYING)
        return sid

    def test_it_records_only_while_it_plays_from_the_buffer(self):
        self.assertFalse(self.live.recordable())
        self.assertFalse(self.live.start_recording(PATH))
        sid = self.start()
        self.assertFalse(self.live.start_recording(PATH))  # before ready: nothing plays
        self.engine.buffer(sid, segments(0, 10), live_edge=20.5)
        self.engine.sessionReady.emit(sid)
        self.assertTrue(self.live.recordable())
        self.assertEqual(self.engine.recordings, [])

    def test_a_cast_opens_vlc_again_from_the_buffer_where_it_played(self):
        self.assertFalse(self.live.reopen_player())  # nothing plays from the buffer yet
        self.playing()
        self.step(1.0)
        plays = len(self.playback.plays)
        self.assertTrue(self.live.reopen_player())
        self.assertEqual(len(self.playback.plays), plays + 1)
        self.assertIn("/stream/", self.playback.plays[-1][0])  # the loopback, not the provider

    def test_a_recording_starts_where_it_plays_with_the_limits(self):
        sid = self.playing()
        self.step(1.0)
        position = self.live.position()
        self.clock.now = 500.0
        self.assertTrue(self.live.start_recording(PATH))
        [(rid, recorded_sid, path, seq, kwargs)] = self.engine.recordings
        self.assertEqual((recorded_sid, path), (sid, PATH))
        span = segments(0, 10)[seq]
        self.assertLessEqual(span.start, position)  # the clean start at or before it
        self.assertLess(position, span.end)
        self.assertEqual(kwargs, {"max_s": MAX_SECONDS, "min_free": MIN_FREE_BYTES})
        self.assertEqual(self.changes, [True])
        self.assertTrue(self.live.recording())
        self.assertEqual(self.live.recording_since(), 500.0)
        self.assertFalse(self.live.start_recording(PATH))  # one at a time

    def test_a_rewound_recording_starts_where_it_was_rewound_to(self):
        sid = self.playing()
        self.live.seek_to(5.0)  # opens segment 2 (4-6 s)
        self.playback.set_state(PlaybackState.PLAYING)
        self.live.start_recording(PATH)
        self.assertEqual(self.engine.recordings[-1][1:4], (sid, PATH, 2))

    def test_at_the_live_edge_it_starts_at_the_open_segment(self):
        sid = self.ready([], open_seq=4, live_edge=3.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.live.start_recording(PATH)
        self.assertEqual(self.engine.recordings[-1][1:4], (sid, PATH, 4))

    def test_a_stop_ends_it_and_its_end_is_told(self):
        sid = self.playing()
        self.live.start_recording(PATH)
        rid = self.engine.recordings[-1][0]
        self.live.stop_recording()
        self.assertEqual(self.engine.record_stops, [sid])
        self.assertEqual(self.changes, [True, False])
        self.assertFalse(self.live.recording())
        self.assertIsNone(self.live.recording_since())
        self.engine.recordingEnded.emit("other", STOPPED, "/x.ts", 1)  # another player's
        self.engine.recordingEnded.emit(rid, STOPPED, PATH, 4096)
        self.assertEqual(self.ends, [(STOPPED, PATH, 4096)])
        self.live.stop_recording()  # nothing more
        self.assertEqual(self.engine.record_stops, [sid])

    def test_a_channel_change_stops_it_before_the_close_and_keeps_its_end(self):
        sid = self.playing()
        self.live.start_recording(PATH)
        rid = self.engine.recordings[-1][0]
        self.start()  # the next channel
        events = self.engine.events
        self.assertLess(events.index(("record_stop", sid)), events.index(("close", sid)))
        self.assertEqual(self.changes, [True, False])
        self.engine.answer()
        self.engine.recordingEnded.emit(rid, STOPPED, PATH, 4096)  # after the close
        self.assertEqual(self.ends, [(STOPPED, PATH, 4096)])

    def test_a_recording_that_ends_by_itself_says_so(self):
        self.playing()
        self.live.start_recording(PATH)
        rid = self.engine.recordings[-1][0]
        self.engine.recordingEnded.emit(rid, LIMIT, PATH, 10**9)
        self.assertEqual(self.changes, [True, False])
        self.assertFalse(self.live.recording())
        self.assertEqual(self.ends, [(LIMIT, PATH, 10**9)])

    def test_a_fallback_to_direct_play_stops_it(self):
        sid = self.playing()
        self.live.start_recording(PATH)
        self.engine.sessionFailed.emit(sid, "network", "The connection was lost.")
        self.assertEqual(self.engine.record_stops, [sid])
        self.assertEqual(self.changes, [True, False])
        self.assertFalse(self.live.recordable())

    def test_an_engine_shut_down_ends_it_unannounced(self):
        self.playing()
        self.live.start_recording(PATH)
        self.live.engine_shut_down()
        self.assertEqual(self.changes, [True, False])
        self.assertEqual(self.ends, [])


if __name__ == "__main__":
    unittest.main()
