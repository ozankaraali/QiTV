from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import threading
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from services.playback import PlaybackSession, PlaybackState, create_instance, vlc
from services.vlc_profile import PlayKind

FIXTURE = Path(__file__).parent / "fixtures" / "smoke.mp4"
INSTANCE = create_instance(["--vout=dummy", "--aout=dummy", "--no-video-title-show", "--quiet"])


def wait_for(predicate, timeout_ms=8000):
    waited = 0
    while not predicate() and waited < timeout_ms:
        QTest.qWait(50)
        waited += 50
    return predicate()


@unittest.skipIf(INSTANCE is None, "libVLC is not available")
class PlaybackSessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.session = PlaybackSession(INSTANCE, poll_ms=50, open_timeout_ms=3000)
        self.states = []
        self.session.stateChanged.connect(self.states.append)
        self.addCleanup(self.session.release)

    def test_stop_is_safe_before_play(self):
        self.session.stop()
        self.assertIs(self.session.state(), PlaybackState.IDLE)

    def test_plays_pauses_seeks_and_stops(self):
        self.session.play(FIXTURE.as_uri(), kind=PlayKind.VOD_FILE)
        self.assertTrue(wait_for(lambda: self.session.state() is PlaybackState.PLAYING))
        self.assertTrue(wait_for(lambda: self.session.length_ms() > 0))
        self.session.set_paused(True)
        self.assertTrue(wait_for(lambda: self.session.state() is PlaybackState.PAUSED))
        self.session.set_paused(False)
        self.assertTrue(wait_for(lambda: self.session.state() is PlaybackState.PLAYING))
        self.session.stop()
        self.assertIs(self.session.state(), PlaybackState.IDLE)
        self.assertIn("playing", self.states)

    def test_resume_position(self):
        self.session.play(FIXTURE.as_uri(), kind=PlayKind.VOD_FILE, start_ms=4000)
        self.assertTrue(wait_for(lambda: self.session.state() is PlaybackState.PLAYING))
        positions = []
        self.session.positionChanged.connect(lambda t, _l: positions.append(t))
        self.assertTrue(wait_for(lambda: any(t > 0 for t in positions), 4000))
        self.assertGreaterEqual(next(t for t in positions if t > 0), 3500)

    def test_release_twice_and_calls_after_release_are_safe(self):
        self.session.play(FIXTURE.as_uri(), kind=PlayKind.VOD_FILE)
        self.assertTrue(wait_for(lambda: self.session.state() is PlaybackState.PLAYING))
        self.session.release()
        self.session.release()
        self.assertEqual(self.session.time_ms(), 0)
        self.assertEqual(self.session.length_ms(), 0)
        self.assertFalse(self.session.is_seekable())
        self.assertEqual(self.session.audio_tracks(), [])
        self.assertEqual(self.session.volume(), 0)
        self.session.stop()
        self.session.toggle_pause()
        self.session.set_paused(True)
        self.session.seek(100)
        self.session.play(FIXTURE.as_uri(), kind=PlayKind.VOD_FILE)
        self.session._tick()
        QTest.qWait(200)

    def test_seekable_false_when_media_is_replaced(self):
        flags = []
        self.session.seekableChanged.connect(flags.append)
        self.session.play(FIXTURE.as_uri(), kind=PlayKind.VOD_FILE)
        self.assertTrue(wait_for(lambda: True in flags))
        self.session.play(FIXTURE.as_uri(), kind=PlayKind.VOD_FILE)
        self.assertEqual(flags[-1], False)

    def test_seekable_false_on_stop(self):
        flags = []
        self.session.seekableChanged.connect(flags.append)
        self.session.play(FIXTURE.as_uri(), kind=PlayKind.VOD_FILE)
        self.assertTrue(wait_for(lambda: True in flags))
        self.session.stop()
        self.assertEqual(flags[-1], False)

    def _stalling_session(self):
        session = PlaybackSession(INSTANCE, poll_ms=50, stall_ms=0)
        self.addCleanup(session.release)
        session._kind = PlayKind.LIVE
        session._stall_clock.start()
        return session

    def test_no_stall_counted_while_time_stays_zero(self):
        session = self._stalling_session()
        with mock.patch.object(session, "time_ms", return_value=0):
            for _ in range(6):
                session._watch_stall(PlaybackState.PLAYING)
        self.assertEqual(session._stalls, 0)

    def test_stall_counted_when_time_stops_advancing(self):
        session = self._stalling_session()
        with mock.patch.object(session, "time_ms", return_value=500):
            for _ in range(3):
                session._watch_stall(PlaybackState.PLAYING)
        self.assertGreaterEqual(session._stalls, 1)

    def test_audio_restore_does_not_touch_a_replaced_media(self):
        session = self._stalling_session()
        session.play(FIXTURE.as_uri(), kind=PlayKind.LIVE)
        player = session._player
        with (
            mock.patch.object(player, "audio_get_track", return_value=7),
            mock.patch.object(player, "audio_set_track") as set_track,
        ):
            with mock.patch.object(session, "time_ms", return_value=500):
                for _ in range(2):
                    session._watch_stall(PlaybackState.PLAYING)
            self.assertEqual(set_track.call_args_list, [mock.call(-1)])
            session.stop()
            session.play(FIXTURE.as_uri(), kind=PlayKind.VOD_FILE)
            QTest.qWait(800)
            self.assertEqual(set_track.call_args_list, [mock.call(-1)])

    def test_audio_restore_after_release_is_harmless(self):
        session = self._stalling_session()
        session.play(FIXTURE.as_uri(), kind=PlayKind.LIVE)
        with mock.patch.object(session, "time_ms", return_value=500):
            for _ in range(3):
                session._watch_stall(PlaybackState.PLAYING)
        session.release()
        QTest.qWait(800)

    def test_stall_restart_is_cancelled_by_a_user_zap(self):
        session = self._stalling_session()
        session.play(FIXTURE.as_uri(), kind=PlayKind.LIVE)
        session._stalls = 2
        with mock.patch.object(session, "time_ms", return_value=500):
            for _ in range(3):
                session._watch_stall(PlaybackState.PLAYING)
        other = (FIXTURE.parent / "other.mp4").as_uri()
        session.play(other, kind=PlayKind.VOD_FILE)
        with mock.patch.object(session, "_start_media") as start:
            QTest.qWait(600)
        start.assert_not_called()
        self.assertEqual(session.url, other)

    def test_bad_stream_reports_error(self):
        errors = []
        self.session.errorOccurred.connect(errors.append)
        self.session.play("http://127.0.0.1:9/missing.ts", kind=PlayKind.LIVE)
        self.assertTrue(wait_for(lambda: self.session.state() is PlaybackState.ERROR, 6000))
        self.assertTrue(errors)

    def test_open_timeout_stops_the_player(self):
        # Finding I1: the session reported a timeout but libVLC kept opening, then played
        # behind the error and held the provider connection.
        data = FIXTURE.read_bytes()

        class SlowProvider(BaseHTTPRequestHandler):
            def do_GET(self):
                time.sleep(1.5)  # the first byte arrives after the open timeout
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), SlowProvider)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        session = PlaybackSession(INSTANCE, poll_ms=50, open_timeout_ms=500)
        self.addCleanup(session.release)
        errors = []
        session.errorOccurred.connect(errors.append)
        session.play(f"http://127.0.0.1:{server.server_address[1]}/live.ts", kind=PlayKind.LIVE)
        self.assertTrue(wait_for(lambda: session.state() is PlaybackState.ERROR, 4000))
        self.assertEqual(errors, ["Timed out opening the stream"])
        QTest.qWait(2500)  # the slow answer has arrived by now
        self.assertIsNone(session._player.get_media())
        self.assertNotIn(
            session._player.get_state(), (vlc.State.Opening, vlc.State.Playing, vlc.State.Buffering)
        )
        self.assertEqual(session.time_ms(), 0)
        self.assertIs(session.state(), PlaybackState.ERROR)

    def test_ended_before_playing_is_an_error(self):
        errors = []
        self.session.errorOccurred.connect(errors.append)
        missing = (FIXTURE.parent / "does-not-exist.ts").as_uri()
        self.session.play(missing, kind=PlayKind.LIVE)
        self.assertTrue(wait_for(lambda: self.session.state() is PlaybackState.ERROR, 6000))
        self.assertTrue(errors)


if __name__ == "__main__":
    unittest.main()
