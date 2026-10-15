from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import threading
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from services.playback import (
    NO_VLC_MESSAGE,
    NullSession,
    PlaybackSession,
    PlaybackState,
    create_instance,
    vlc,
)
from services.thread_cleanup import has_pending_threads
from services.vlc_profile import PlayKind

FIXTURE = Path(__file__).parent / "fixtures" / "smoke.mp4"
INSTANCE = create_instance(["--vout=dummy", "--aout=dummy", "--no-video-title-show", "--quiet"])


def wait_for(predicate, timeout_ms=8000):
    waited = 0
    while not predicate() and waited < timeout_ms:
        QTest.qWait(50)
        waited += 50
    return predicate()


def release_and_wait(session):
    """Release, then let the worker that stops libVLC finish before the next test."""
    session.release()
    wait_for(lambda: not has_pending_threads(), 5000)


@unittest.skipIf(INSTANCE is None, "libVLC is not available")
class PlaybackSessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.session = PlaybackSession(INSTANCE, poll_ms=50, open_timeout_ms=3000)
        self.states = []
        self.session.stateChanged.connect(self.states.append)
        self.addCleanup(release_and_wait, self.session)

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
        self.addCleanup(release_and_wait, session)
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

    def test_stall_budget_resets_after_healthy_playback(self):
        # Finding m3: after a recovery the budget never refilled, so a later stall went
        # straight to the last resort.
        session = self._stalling_session()
        with mock.patch.object(session, "time_ms", return_value=500):
            for _ in range(3):
                session._watch_stall(PlaybackState.PLAYING)
        self.assertEqual(session._stalls, 2)
        clock = iter(range(1000, 100000, 250))
        with (
            mock.patch("services.playback._STALL_BUDGET_RESET_MS", 100),
            mock.patch.object(session, "time_ms", side_effect=lambda: next(clock)),
        ):
            session._watch_stall(PlaybackState.PLAYING)
            self.assertEqual(session._stalls, 2)  # not healthy for long enough yet
            QTest.qWait(150)
            session._watch_stall(PlaybackState.PLAYING)
        self.assertEqual((session._stalls, session._restarted), (0, False))

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
        self.addCleanup(release_and_wait, session)
        errors = []
        session.errorOccurred.connect(errors.append)
        session.play(f"http://127.0.0.1:{server.server_address[1]}/live.ts", kind=PlayKind.LIVE)
        self.assertTrue(wait_for(lambda: session.state() is PlaybackState.ERROR, 4000))
        self.assertEqual(errors, ["Timed out opening the stream"])
        # libVLC is stopped on a worker. Wait in short steps: one long QTest.qWait holds the
        # GIL throughout, so neither that worker nor the slow server's thread could run.
        self.assertTrue(wait_for(lambda: session._job is None, 6000))
        wait_for(lambda: False, 2500)  # the slow answer has arrived by now
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


class _FakeMedia:
    def __init__(self, url):
        self.url = url
        self.options = []

    def add_option(self, option):
        self.options.append(option)

    def release(self):
        pass


class _FakePlayer:
    """libVLC's media player as the session drives it, with its input lock made visible.

    libvlc_media_player_set_media and _stop hold the player's input lock while they join
    the old input. While a job does that (``joining``), a GUI-thread call that takes the
    same lock would wait for it; the fake fails the test instead of hanging. Volume and
    mute take only the audio output's locks and stay allowed.
    """

    def __init__(self):
        self.calls = []
        self.joining = False
        self.has_input = False
        self.play_result = 0
        self.fail_stop = False

    def _call(self, *call):
        if self.joining:
            raise AssertionError(f"{call[0]} on the GUI thread while libVLC joins the input")
        self.calls.append(call)

    def video_set_mouse_input(self, on):
        pass

    def video_set_key_input(self, on):
        pass

    def set_nsobject(self, handle):
        self.calls.append(("window", handle))

    set_hwnd = set_xwindow = set_nsobject

    def set_media(self, media):
        self._call("set_media", media.url if media is not None else None)
        self.has_input = False

    def play(self):
        self._call("play")
        self.has_input = self.play_result == 0
        return self.play_result

    def stop(self):
        self._call("stop")
        if self.fail_stop:
            raise OSError("libVLC failed")
        self.has_input = False

    def release(self):
        self._call("release")

    def get_state(self):
        self._call("get_state")
        return vlc.State.Opening if vlc is not None else 1

    def get_time(self):
        self._call("get_time")
        return 0

    def get_length(self):
        self._call("get_length")
        return 0

    def is_seekable(self):
        self._call("is_seekable")
        return True

    def set_time(self, ms):
        self._call("set_time", ms)

    def pause(self):
        self._call("pause")

    def set_pause(self, paused):
        self._call("set_pause", paused)

    def set_rate(self, rate):
        self._call("set_rate", rate)

    def audio_get_track_description(self):
        self._call("audio_get_track_description")
        return [(1, b"Main")]

    def audio_set_track(self, track):
        self._call("audio_set_track", track)

    def audio_get_track(self):
        self._call("audio_get_track")
        return 1

    def video_get_spu_description(self):
        self._call("video_get_spu_description")
        return [(-1, b"Disable")]

    def video_set_spu(self, track):
        self._call("video_set_spu", track)

    def audio_get_volume(self):
        self.calls.append(("audio_get_volume",))
        return 70

    def audio_set_volume(self, value):
        self.calls.append(("audio_set_volume", value))

    def audio_get_mute(self):
        return False

    def audio_set_mute(self, muted):
        self.calls.append(("audio_set_mute", muted))


class _FakeInstance:
    def __init__(self, player):
        self.player = player

    def media_player_new(self):
        return self.player

    def media_new(self, url):
        return _FakeMedia(url)


class _HeldRunner:
    """A JobRunner whose worker runs only when the test finishes the job.

    From submission until then the worker owns the player's input lock (``joining``).
    """

    def __init__(self, player):
        self.player = player
        self.jobs = []

    def run_worker(self, worker, on_finished, on_error=None, context=None):
        self.jobs.append((worker, on_finished, on_error, context))
        self.player.joining = True
        return worker

    def finish(self):
        worker, on_finished, on_error, context = self.jobs.pop(0)
        self.player.joining = False
        try:
            worker.run()
        except Exception:
            on_error(context, "The background task failed")
        else:
            on_finished(context, None)


A, B, C = "http://provider.test/a.ts", "http://provider.test/b.ts", "http://provider.test/c.ts"


class PlayerJobTests(unittest.TestCase):
    """CI run 38675064690 (macOS): a zap hung in libvlc_media_player_set_media.

    Ending an input joins its threads; on macOS the join can wait for the main thread
    (the video output's Open dispatch_syncs onto it while the first picture is still on
    its way). Those calls must run on a worker, and the GUI thread must not wait on the
    player meanwhile.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.player = _FakePlayer()
        self.runner = _HeldRunner(self.player)
        self.session = PlaybackSession(
            _FakeInstance(self.player), runner=self.runner, poll_ms=60_000
        )
        self.states, self.errors = [], []
        self.session.stateChanged.connect(self.states.append)
        self.session.errorOccurred.connect(self.errors.append)
        self.addCleanup(self.session.release)  # stops its timers; held jobs never run

    def play(self, url):
        self.session.play(url, kind=PlayKind.LIVE)

    def opened(self):
        return [call[1] for call in self.player.calls if call[0] == "set_media" and call[1]]

    def test_the_first_play_opens_on_the_gui_thread(self):
        self.play(A)
        self.assertEqual(self.runner.jobs, [])
        self.assertEqual(self.player.calls, [("set_media", A), ("play",)])
        self.assertIs(self.session.state(), PlaybackState.OPENING)

    def test_a_zap_ends_the_old_input_off_the_gui_thread(self):
        self.play(A)
        self.player.calls.clear()
        self.play(B)
        self.assertEqual(self.player.calls, [])
        self.assertEqual(len(self.runner.jobs), 1)
        self.assertEqual((self.session.url, self.session.state()), (B, PlaybackState.OPENING))
        self.runner.finish()
        self.assertEqual(self.player.calls, [("set_media", None), ("set_media", B), ("play",)])

    def test_the_gui_never_waits_on_the_player_while_its_input_is_joined(self):
        self.play(A)
        self.play(B)
        session = self.session
        self.assertEqual((session.time_ms(), session.length_ms()), (0, 0))
        self.assertFalse(session.is_seekable())
        self.assertEqual((session.audio_tracks(), session.subtitle_tracks()), ([], []))
        session.toggle_pause()
        session.set_paused(True)
        session.seek(1000)
        session.seek_relative(5000)
        session.set_audio_track(2)
        session.set_subtitle_track(3)
        session.set_rate(1.5)
        session._tick()
        session._restore_audio()
        session.set_window(42)  # a variable on the player: no input lock
        session.set_volume(40)  # the audio output has its own locks
        self.assertEqual(session.volume(), 70)
        self.runner.finish()
        # The rate asked for meanwhile belongs to the new media: set before it plays.
        tail = self.player.calls[-3:]
        self.assertEqual(tail, [("set_media", B), ("set_rate", 1.5), ("play",)])

    def test_a_burst_of_zaps_joins_once_and_opens_only_the_newest(self):
        self.play(A)
        self.play(B)
        self.play(C)
        self.assertEqual(len(self.runner.jobs), 1)
        self.runner.finish()
        self.assertEqual(self.opened(), [A, C])
        self.assertEqual(self.runner.jobs, [])

    def test_a_late_stop_completion_after_a_newer_play_opens_the_new_media(self):
        self.play(A)
        self.session.stop()
        self.assertIs(self.session.state(), PlaybackState.IDLE)
        self.play(B)
        self.runner.finish()  # the stop's join ends after the newer play
        self.assertEqual(
            self.player.calls[-4:], [("stop",), ("set_media", None), ("set_media", B), ("play",)]
        )
        self.assertIs(self.session.state(), PlaybackState.OPENING)
        self.assertEqual(self.states[-2:], ["idle", "opening"])
        self.assertEqual(self.runner.jobs, [])

    def test_a_stop_during_a_zap_still_stops_libvlc(self):
        self.play(A)
        self.play(B)
        self.session.stop()
        self.runner.finish()  # the zap's join
        self.assertEqual(len(self.runner.jobs), 1)  # the stop is still owed
        self.runner.finish()
        self.assertIn(("stop",), self.player.calls)
        self.assertEqual(self.opened(), [A])
        self.assertIs(self.session.state(), PlaybackState.IDLE)

    def test_release_during_a_pending_join_releases_once_it_has_finished(self):
        self.play(A)
        self.play(B)
        self.session.release()
        self.assertNotIn(("release",), self.player.calls)  # the worker still uses the player
        states = list(self.states)
        self.runner.finish()
        self.assertEqual(self.player.calls[-1], ("release",))
        self.assertEqual(self.opened(), [A])
        self.assertEqual(self.states, states)  # nothing is reported after a release
        self.play(C)
        self.session.stop()
        self.session.release()
        self.assertEqual((self.runner.jobs, self.player.calls.count(("release",))), ([], 1))

    def test_release_with_a_live_input_stops_it_off_the_gui_thread(self):
        self.play(A)
        self.session.release()
        self.assertEqual(self.player.calls[-1], ("play",))
        self.runner.finish()
        self.assertEqual(self.player.calls[-3:], [("stop",), ("set_media", None), ("release",)])

    def test_stale_or_repeated_completions_are_ignored(self):
        self.play(A)
        self.play(B)
        token = self.session._job_token
        self.session._job_done(token - 1)  # not the running job's
        self.assertEqual((self.opened(), len(self.runner.jobs)), ([A], 1))
        self.runner.finish()
        calls = list(self.player.calls)
        self.session._job_done(token)  # delivered twice
        self.assertEqual(self.player.calls, calls)

    def test_release_before_any_play_is_immediate(self):
        self.session.release()
        self.assertEqual(self.player.calls, [("release",)])
        self.assertEqual(self.runner.jobs, [])

    def test_a_failure_stops_libvlc_off_the_gui_thread(self):
        self.play(A)
        self.session._fail("Timed out opening the stream")
        self.assertEqual(self.errors, ["Timed out opening the stream"])
        self.assertIs(self.session.state(), PlaybackState.ERROR)
        self.assertEqual(self.player.calls[-1], ("play",))
        self.runner.finish()
        self.assertEqual(self.player.calls[-2:], [("stop",), ("set_media", None)])
        self.assertIs(self.session.state(), PlaybackState.ERROR)

    def test_a_play_that_libvlc_refuses_is_an_error(self):
        self.player.play_result = -1
        self.play(A)
        self.assertEqual(self.errors, ["VLC could not start playback"])
        self.assertIs(self.session.state(), PlaybackState.ERROR)
        self.assertEqual(len(self.runner.jobs), 1)  # closes what set_media prepared
        self.runner.finish()
        self.player.play_result = 0
        self.play(B)
        self.assertEqual(self.player.calls[-2:], [("set_media", B), ("play",)])

    def test_a_failed_job_still_starts_the_newest_media(self):
        self.play(A)
        self.session.stop()
        self.play(B)
        self.player.fail_stop = True
        with self.assertLogs("services.playback", "WARNING"):
            self.runner.finish()
        self.assertEqual(self.player.calls[-2:], [("set_media", B), ("play",)])

    def test_a_stall_restart_waits_for_its_stop(self):
        self.play(A)
        self.session._kind = PlayKind.LIVE
        self.session._stalls = 2
        self.session._stall_clock.start()
        with mock.patch.object(self.session, "time_ms", return_value=500):
            self.session._last_time = 500
            self.session._stall_ms = 0
            self.session._watch_stall(PlaybackState.PLAYING)
        self.assertEqual(len(self.runner.jobs), 1)  # the stop runs on the worker
        self.session._restart_stream()  # the restart timer fires first
        self.assertEqual(self.opened(), [A])
        self.runner.finish()
        self.assertEqual(self.opened(), [A, A])


class _GuiQueue(QObject):
    """Runs callables on the GUI thread, queued, like a block sent to the main queue."""

    request = Signal(object)

    def __init__(self):
        super().__init__()
        self.request.connect(lambda run: run(), Qt.ConnectionType.QueuedConnection)


class _MacPlayer(_FakePlayer):
    """Ending an input waits for the GUI thread, as libVLC 3 on macOS can.

    caopengllayer's Open runs dispatch_sync(main queue) while the decoder waits for it,
    and the input thread waits for the decoder: set_media/stop then wait for the main
    thread to run a block. Thread-safe; ``deadlocked`` records a join that never ended.
    """

    def __init__(self):
        super().__init__()
        self.queue = _GuiQueue()
        self.deadlocked = False
        self._lock = threading.Lock()

    def _call(self, *call):
        with self._lock:
            self.calls.append(call)

    def _join(self):
        if not self.has_input:
            return
        done = threading.Event()
        self.queue.request.emit(done.set)
        if not done.wait(2.0):
            self.deadlocked = True

    def set_media(self, media):
        self._join()
        super().set_media(media)

    def stop(self):
        self._join()
        super().stop()


class MainThreadJoinTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_zap_and_stop_finish_when_the_join_needs_the_gui_thread(self):
        player = _MacPlayer()
        session = PlaybackSession(_FakeInstance(player), poll_ms=60_000)  # a real JobRunner
        self.addCleanup(release_and_wait, session)
        session.play(A, kind=PlayKind.LIVE)
        session.play(B, kind=PlayKind.LIVE)
        self.assertTrue(wait_for(lambda: ("set_media", B) in player.calls, 4000))
        session.stop()
        self.assertTrue(wait_for(lambda: not has_pending_threads(), 4000))
        self.assertIn(("stop",), player.calls)
        self.assertFalse(player.deadlocked)


def _public(cls) -> set[str]:
    return {name for name in vars(cls) if not name.startswith("_")}


class NullSessionTests(unittest.TestCase):
    """Runs without libVLC."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_shares_the_playback_session_api(self):
        self.assertLessEqual(_public(PlaybackSession), _public(NullSession))

    def test_play_explains_that_vlc_is_needed(self):
        session = NullSession()
        errors = []
        session.errorOccurred.connect(errors.append)
        session.play("file:///clip.mp4", kind=PlayKind.VOD_FILE)
        self.assertEqual(errors, [NO_VLC_MESSAGE])
        self.assertIs(session.state(), PlaybackState.IDLE)
        self.assertIsNone(session.url)
        self.assertEqual(
            (session.volume(), session.toggle_mute(), session.audio_tracks()), (0, False, [])
        )
        session.stop()
        session.release()
        session.release()


if __name__ == "__main__":
    unittest.main()
