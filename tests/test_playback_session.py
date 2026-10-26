from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QApplication
from timer_fixtures import elapse, wait_until

from services.playback import (
    _AUDIO_POLL_MS,
    _AUDIO_RESTORE_MS,
    NO_VLC_MESSAGE,
    VLC_LOG_ENV,
    NullSession,
    PlaybackSession,
    PlaybackState,
    create_instance,
    default_args,
    vlc,
)
from services.vlc_profile import PlayKind

FIXTURE = Path(__file__).parent / "fixtures" / "smoke.mp4"
INSTANCE = create_instance(["--vout=dummy", "--aout=dummy", "--no-video-title-show", "--quiet"])
# A stop job not joined by then is hung: the TV window gives up on a worker after as long
# (CLOSE_THREAD_WAIT_MS). It is no speed budget. The first stops in a process can take
# seconds while libVLC loads its plugins (CI run 38696639927, Windows: about 6 s, where it
# usually takes under half a second); those are reported on stderr, not failed.
STOP_HANG_MS = 30_000
SLOW_STOP_S = 2.0


def wait_for(predicate, timeout_ms=8000):
    # Short steps (timer_fixtures): one long QTest.qWait holds the GIL, and the workers that
    # stop libVLC barely run during it. At least timer_fixtures' 10 s: a deadline in real time
    # (the old loop counted its steps, so a stalled runner got longer).
    return wait_until(predicate, max(timeout_ms, 10_000))


def release_and_wait(test, session):
    """Release, then wait until the session's worker that stops libVLC has been joined.

    Only this session's jobs count (its runner forgets a job once its thread is joined), so
    a thread that another test left running cannot fail this one.
    """
    session.release()
    started = time.monotonic()
    joined = wait_until(lambda: session._runner.pending() == 0, STOP_HANG_MS)
    took = time.monotonic() - started
    test.assertTrue(joined, f"libVLC's stop job hung: still running after {took:.1f} s")
    if took > SLOW_STOP_S:
        print(f"\n{test.id()}: waited {took:.1f} s for libVLC's stop job", file=sys.stderr)


@unittest.skipIf(INSTANCE is None, "libVLC is not available")
class PlaybackSessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.session = PlaybackSession(INSTANCE, poll_ms=50, open_timeout_ms=3000)
        self.states = []
        self.session.stateChanged.connect(self.states.append)
        self.addCleanup(release_and_wait, self, self.session)

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
        elapse(200)

    def test_the_sound_delay_reaches_each_media_libvlc_opens(self):
        # Measured with 3.0.20: audio_set_delay without an input is dropped (-1), and a new
        # media starts at zero; set right after play(), the new input keeps it.
        self.session.set_audio_delay(150)
        self.session.play(FIXTURE.as_uri(), kind=PlayKind.VOD_FILE)
        self.assertTrue(wait_for(lambda: self.session.state() is PlaybackState.PLAYING))
        self.assertEqual(self.session._player.audio_get_delay(), 150_000)
        self.session.play(FIXTURE.as_uri(), kind=PlayKind.VOD_FILE)  # opened again
        self.assertTrue(wait_for(lambda: self.session.state() is PlaybackState.PLAYING))
        self.assertEqual(self.session._player.audio_get_delay(), 150_000)
        self.session.set_audio_delay(-100)
        self.assertEqual(self.session._player.audio_get_delay(), -100_000)

    def test_a_subtitle_file_shows_named_after_itself_and_comes_back_on_a_reopen(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        srt = Path(directory.name) / "Lighthouse.en.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:30,000\nHello\n", encoding="utf-8")
        session = self.session
        session.play(FIXTURE.as_uri(), kind=PlayKind.VOD_FILE)
        self.assertTrue(wait_for(lambda: session.state() is PlaybackState.PLAYING))
        self.assertTrue(session.add_subtitle_file(str(srt)))

        def shown():
            names = dict(session.subtitle_tracks())
            return names.get(session.subtitle_track()) == "Lighthouse.en.srt"

        self.assertTrue(wait_for(shown))
        session.set_subtitle_delay(-200)
        self.assertEqual(session._player.video_get_spu_delay(), -200_000)
        session.play(FIXTURE.as_uri(), kind=PlayKind.VOD_FILE)  # opened again
        self.assertTrue(wait_for(lambda: session.state() is PlaybackState.PLAYING))
        self.assertTrue(wait_for(shown))
        self.assertEqual(session._player.video_get_spu_delay(), -200_000)

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
        self.addCleanup(release_and_wait, self, session)
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
        with (
            mock.patch.object(session, "time_ms", return_value=500),
            self.assertLogs("services.playback", "WARNING"),
        ):
            for _ in range(3):
                session._watch_stall(PlaybackState.PLAYING)
        self.assertGreaterEqual(session._stalls, 1)

    def test_stall_budget_resets_after_healthy_playback(self):
        # After a recovery the budget never refilled, so a later stall went
        # straight to the last resort.
        session = self._stalling_session()
        with (
            mock.patch.object(session, "time_ms", return_value=500),
            self.assertLogs("services.playback", "WARNING"),
        ):
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
            elapse(150)
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
            with (
                mock.patch.object(session, "time_ms", return_value=500),
                self.assertLogs("services.playback", "WARNING"),
            ):
                for _ in range(2):
                    session._watch_stall(PlaybackState.PLAYING)
            self.assertEqual(set_track.call_args_list, [mock.call(-1)])
            session.stop()
            session.play(FIXTURE.as_uri(), kind=PlayKind.VOD_FILE)
            elapse(800)
            self.assertEqual(set_track.call_args_list, [mock.call(-1)])

    def test_audio_restore_after_release_is_harmless(self):
        session = self._stalling_session()
        session.play(FIXTURE.as_uri(), kind=PlayKind.LIVE)
        with (
            mock.patch.object(session, "time_ms", return_value=500),
            self.assertLogs("services.playback", "WARNING"),
        ):
            for _ in range(3):
                session._watch_stall(PlaybackState.PLAYING)
        session.release()
        with mock.patch.object(session._player, "audio_set_track") as set_track:
            elapse(_AUDIO_RESTORE_MS + 100)  # the cancelled restore would have come due
        set_track.assert_not_called()

    def test_stall_restart_is_cancelled_by_a_user_zap(self):
        session = self._stalling_session()
        session.play(FIXTURE.as_uri(), kind=PlayKind.LIVE)
        session._stalls = 2
        with (
            mock.patch.object(session, "time_ms", return_value=500),
            self.assertLogs("services.playback", "WARNING"),
        ):
            for _ in range(3):
                session._watch_stall(PlaybackState.PLAYING)
        other = (FIXTURE.parent / "other.mp4").as_uri()
        session.play(other, kind=PlayKind.VOD_FILE)
        with mock.patch.object(session, "_start_media") as start:
            elapse(600)
        start.assert_not_called()
        self.assertEqual(session.url, other)

    def test_bad_stream_reports_error(self):
        errors = []
        self.session.errorOccurred.connect(errors.append)
        self.session.play("http://127.0.0.1:9/missing.ts", kind=PlayKind.LIVE)
        self.assertTrue(wait_for(lambda: self.session.state() is PlaybackState.ERROR, 6000))
        self.assertTrue(errors)

    def test_open_timeout_stops_the_player(self):
        # The session reported a timeout but libVLC kept opening, then played
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
        self.addCleanup(release_and_wait, self, session)
        errors = []
        session.errorOccurred.connect(errors.append)
        session.play(f"http://127.0.0.1:{server.server_address[1]}/live.ts", kind=PlayKind.LIVE)
        self.assertTrue(wait_for(lambda: session.state() is PlaybackState.ERROR, 4000))
        self.assertEqual(errors, ["Timed out opening the stream"])
        # libVLC is stopped on a worker. Wait in short steps: one long QTest.qWait holds the
        # GIL throughout, so neither that worker nor the slow server's thread could run.
        self.assertTrue(wait_for(lambda: session._job is None, 6000))
        elapse(2500)  # the slow answer has arrived by now
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

    def audio_set_delay(self, us):
        # libVLC sets it on the input: none, and it is dropped (-1), as with 3.0.20.
        self._call("audio_set_delay", us)
        return 0 if self.has_input else -1

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

    def test_a_channels_agent_and_referrer_are_options_of_its_media(self):
        medias = []
        media_new = _FakeInstance.media_new

        def capture(instance, url):
            medias.append(media_new(instance, url))
            return medias[-1]

        with mock.patch.object(_FakeInstance, "media_new", capture):
            self.session.play(A, kind=PlayKind.LIVE, user_agent="UA/1", referrer="https://s.test/")
        self.assertIn(":http-user-agent=UA/1", medias[-1].options)
        self.assertIn(":http-referrer=https://s.test/", medias[-1].options)

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
        self.assertIn(("audio_set_volume", 40), self.player.calls)
        self.assertEqual(session.volume(), 40)  # what was asked for
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

    def test_input_ended_follows_the_stop_once_the_worker_let_go(self):
        ended = []
        self.session.inputEnded.connect(lambda: ended.append(True))
        self.assertFalse(self.session.holds_input())
        self.play(A)
        self.assertTrue(self.session.holds_input())  # its provider connection may be open
        self.session.stop()
        self.assertTrue(self.session.holds_input())
        self.assertEqual(ended, [])  # the worker still ends the input
        self.runner.finish()
        self.assertFalse(self.session.holds_input())
        self.assertEqual(ended, [True])

    def test_input_ended_waits_for_a_stop_owed_after_a_zap(self):
        ended = []
        self.session.inputEnded.connect(lambda: ended.append(True))
        self.play(A)
        self.play(B)
        self.session.stop()
        self.runner.finish()  # the zap's join: the stop is still owed
        self.assertEqual(ended, [])
        self.assertTrue(self.session.holds_input())
        self.runner.finish()
        self.assertEqual(ended, [True])

    def test_a_zap_that_opens_the_next_input_does_not_report_an_end(self):
        ended = []
        self.session.inputEnded.connect(lambda: ended.append(True))
        self.play(A)
        self.play(B)
        self.runner.finish()
        self.assertEqual(ended, [])
        self.assertTrue(self.session.holds_input())

    def test_release_during_a_pending_join_stops_then_releases_off_the_gui_thread(self):
        self.play(A)
        self.play(B)
        self.session.release()
        self.assertNotIn(("release",), self.player.calls)  # the worker still uses the player
        states = list(self.states)
        self.runner.finish()  # the zap's DETACH kept the outputs: a STOP closes them first
        self.assertNotIn(("release",), self.player.calls)
        self.assertEqual(len(self.runner.jobs), 1)
        self.runner.finish()
        self.assertEqual(self.player.calls[-3:], [("stop",), ("set_media", None), ("release",)])
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

    def test_set_muted_is_safe_to_call_while_a_job_runs(self):
        self.play(A)
        self.play(B)  # a worker joins A's input: mute takes only the audio output's locks
        self.session.set_muted(True)
        self.session.set_muted(False)
        mutes = [call for call in self.player.calls if call[0] == "audio_set_mute"]
        self.assertEqual(mutes, [("audio_set_mute", True), ("audio_set_mute", False)])

    def test_set_muted_after_release_leaves_libvlc_alone(self):
        self.session.release()
        self.player.calls.clear()
        self.session.set_muted(True)
        self.assertEqual(self.player.calls, [])

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
        with (
            mock.patch.object(self.session, "time_ms", return_value=500),
            self.assertLogs("services.playback", "WARNING"),
        ):
            self.session._last_time = 500
            self.session._stall_ms = 0
            self.session._watch_stall(PlaybackState.PLAYING)
        self.assertEqual(len(self.runner.jobs), 1)  # the stop runs on the worker
        self.session._restart_stream()  # the restart timer fires first
        self.assertEqual(self.opened(), [A])
        self.runner.finish()
        self.assertEqual(self.opened(), [A, A])


class SoundDelayTests(unittest.TestCase):
    """The sound's sync (2.2) lives on libVLC's input, which each media starts at zero: the
    session sets it after each play() and keeps it for the next media."""

    setUpClass = PlayerJobTests.setUpClass
    setUp = PlayerJobTests.setUp
    play = PlayerJobTests.play
    opened = PlayerJobTests.opened

    def delays(self):
        return [call for call in self.player.calls if call[0] == "audio_set_delay"]

    def test_a_delay_set_before_anything_plays_comes_with_the_first_media(self):
        self.session.set_audio_delay(150)
        self.assertEqual(self.player.calls, [])  # no input: libVLC would drop it
        self.play(A)
        self.assertEqual(
            self.player.calls, [("set_media", A), ("play",), ("audio_set_delay", 150_000)]
        )
        self.assertEqual(self.session.audio_delay(), 150)

    def test_a_delay_set_while_it_plays_applies_at_once(self):
        self.play(A)
        self.session.set_audio_delay(-200)
        self.assertEqual(self.delays(), [("audio_set_delay", -200_000)])

    def test_every_media_the_player_opens_after_it_takes_it_again(self):
        self.session.set_audio_delay(100)
        self.play(A)
        self.play(B)  # a zap, a reopen of the same channel (a seek, a reconnect) alike
        self.session.set_audio_delay(250)  # the old input is being joined: nothing now
        self.runner.finish()
        self.assertEqual(
            self.delays(), [("audio_set_delay", 100_000), ("audio_set_delay", 250_000)]
        )
        self.assertEqual(self.opened(), [A, B])

    def test_in_sync_sets_nothing_on_a_new_media(self):
        self.play(A)
        self.assertEqual(self.delays(), [])
        self.assertEqual(self.session.audio_delay(), 0)

    def test_after_release_nothing_reaches_libvlc(self):
        self.session.release()
        self.session.set_audio_delay(50)
        self.assertEqual((self.delays(), self.session.audio_delay()), ([], 0))


class _SubtitlePlayer(_FakePlayer):
    """libVLC's subtitles as measured with 3.0.20: a slave added to the input shows as a new
    track named "Track N" once the input has read it (`read`), chosen when it was added so;
    a new media starts with the media's own tracks only, and without a delay."""

    def __init__(self, own=()):
        super().__init__()
        self.own = [(-1, b"Disable"), *own]
        self.spus = list(self.own)
        self.spu = -1
        self.spu_delay = 0
        self.slaves = []  # (uri, select) as added, waiting to be read

    def set_media(self, media):
        super().set_media(media)
        self.spus, self.spu, self.spu_delay = list(self.own), -1, 0

    def get_state(self):
        self._call("get_state")
        return vlc.State.Playing

    def video_get_spu_description(self):
        self._call("video_get_spu_description")
        return list(self.spus)

    def video_get_spu(self):
        self._call("video_get_spu")
        return self.spu

    def video_set_spu(self, track):
        self._call("video_set_spu", track)
        self.spu = track

    def video_set_spu_delay(self, us):
        self._call("video_set_spu_delay", us)
        if not self.has_input:
            return -1
        self.spu_delay = us
        return 0

    def add_slave(self, kind, uri, select):
        self._call("add_slave", uri, select)
        self.slaves.append((uri, select))
        return 0

    def read(self, track):
        """The input read the oldest slave added: its track shows."""
        _uri, select = self.slaves.pop(0)
        self.spus.append((track, f"Track {len(self.spus)}".encode()))
        if select:
            self.spu = track


@unittest.skipIf(vlc is None, "python-vlc's states are needed to poll the player")
class SubtitleFileTests(unittest.TestCase):
    """A subtitle file shows with what plays (2.2), named after the file, and stays with the
    playback through each reopen; its delay works as the sound's."""

    setUpClass = PlayerJobTests.setUpClass

    def setUp(self):
        self.player = _SubtitlePlayer(own=[(3, b"English")])
        self.runner = _HeldRunner(self.player)
        self.session = PlaybackSession(
            _FakeInstance(self.player), runner=self.runner, poll_ms=60_000
        )
        self.addCleanup(self.session.release)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.film = Path(directory.name) / "Lighthouse.en.srt"
        self.film.write_text("1\n00:00:01,000 --> 00:00:04,000\nHello\n", encoding="utf-8")

    def play(self):
        self.session.play(A, kind=PlayKind.VOD_FILE)

    def reopen(self):
        """The same media again (a time-shift seek, a reconnect), then it plays."""
        self.play()
        self.runner.finish()
        self.session._tick()

    def slaves(self):
        return [call[1:] for call in self.player.calls if call[0] == "add_slave"]

    def test_nothing_playing_takes_no_file(self):
        self.assertFalse(self.session.add_subtitle_file(str(self.film)))
        self.assertEqual(self.slaves(), [])

    def test_the_files_track_takes_its_name_and_is_chosen(self):
        self.play()
        self.assertTrue(self.session.add_subtitle_file(str(self.film)))
        self.assertEqual(self.slaves(), [(self.film.as_uri(), True)])
        self.assertEqual(self.session.subtitle_tracks(), [(-1, "Disable"), (3, "English")])
        self.player.read(4)
        self.session._tick()
        self.assertEqual(
            self.session.subtitle_tracks(),
            [(-1, "Disable"), (3, "English"), (4, "Lighthouse.en.srt")],
        )
        self.assertEqual(self.session.subtitle_track(), 4)

    def test_a_reopen_adds_the_files_again_once_it_plays_with_the_delay(self):
        self.play()
        self.session.add_subtitle_file(str(self.film))
        self.player.read(4)
        self.session.set_subtitle_delay(-200)
        self.assertEqual(self.player.spu_delay, -200_000)
        self.reopen()
        self.assertEqual(self.player.spu_delay, -200_000)  # right after play()
        self.assertEqual(self.slaves()[-1], (self.film.as_uri(), True))
        self.player.read(7)
        self.assertEqual(self.session.subtitle_tracks()[-1], (7, "Lighthouse.en.srt"))
        self.assertEqual(self.session.subtitle_track(), 7)

    def test_a_file_not_chosen_comes_back_not_chosen(self):
        self.play()
        self.session.add_subtitle_file(str(self.film))
        self.player.read(4)
        self.session.subtitle_tracks()
        self.session.set_subtitle_track(3)  # the media's own
        self.reopen()
        self.assertEqual(self.slaves()[-1], (self.film.as_uri(), False))

    def test_another_title_starts_without_them(self):
        self.play()
        self.session.add_subtitle_file(str(self.film))
        self.session.set_subtitle_delay(300)
        self.session.clear_subtitles()
        self.reopen()
        self.assertEqual(len(self.slaves()), 1)
        self.assertEqual((self.session.subtitle_delay(), self.player.spu_delay), (0, 0))

    def test_a_media_track_that_shows_late_is_never_taken_for_the_file(self):
        # A live stream's embedded subtitles may show after the file was added, before the
        # file's own track, which libVLC chooses as it comes.
        self.play()
        self.session.add_subtitle_file(str(self.film))
        self.player.spus.append((5, b"Track 2 - [Deutsch]"))
        self.session._tick()
        self.player.read(6)
        self.session._tick()
        self.assertEqual(
            self.session.subtitle_tracks(),
            [(-1, "Disable"), (3, "English"), (5, "Track 2 - [Deutsch]"), (6, "Lighthouse.en.srt")],
        )

    def test_files_added_unchosen_are_named_in_order_once_all_came(self):
        other = self.film.with_name("Lighthouse.fr.srt")
        other.write_text("1\n00:00:01,000 --> 00:00:04,000\nBonjour\n", encoding="utf-8")
        self.play()
        self.session.add_subtitle_file(str(self.film))
        self.session.add_subtitle_file(str(other))
        self.player.read(4)
        self.player.read(5)
        self.session.subtitle_tracks()
        self.session.set_subtitle_track(3)  # the media's own: the files come back unchosen
        self.reopen()
        self.player.read(7)
        self.session._tick()  # one of two came: which one is not known yet
        self.assertEqual(self.session.subtitle_tracks()[-1], (7, "Track 2"))
        self.player.read(8)
        self.assertEqual(
            self.session.subtitle_tracks()[-2:],
            [(7, "Lighthouse.en.srt"), (8, "Lighthouse.fr.srt")],
        )

    def test_a_track_that_never_comes_keeps_libvlcs_name(self):
        self.play()
        self.session.add_subtitle_file(str(self.film))
        with mock.patch("services.playback._SUBTITLE_NAME_WAIT_MS", -1):
            self.session._tick()
        self.player.read(9)  # far too late: maybe the media's own
        self.assertEqual(self.session.subtitle_tracks()[-1], (9, "Track 2"))

    def test_a_file_that_cannot_be_read_is_refused(self):
        self.play()
        self.assertFalse(self.session.add_subtitle_file(str(self.film.with_name("gone.srt"))))
        self.assertFalse(self.session.add_subtitle_file(str(self.film.parent)))  # a folder
        self.assertEqual(self.slaves(), [])

    def test_a_file_added_while_a_reopen_waits_to_play_is_added_once(self):
        self.play()
        self.session.add_subtitle_file(str(self.film))
        self.player.read(4)
        other = self.film.with_name("Lighthouse.fr.srt")
        other.write_text("1\n00:00:01,000 --> 00:00:04,000\nBonjour\n", encoding="utf-8")
        self.play()  # reopened: the files go to it once it plays
        self.runner.finish()
        self.assertTrue(self.session.add_subtitle_file(str(other)))  # before it plays
        self.session._tick()
        self.assertEqual(
            self.slaves(),
            [(self.film.as_uri(), True), (other.as_uri(), True), (self.film.as_uri(), False)],
        )

    def test_a_file_this_media_has_is_chosen_again_not_added_twice(self):
        self.play()
        self.session.add_subtitle_file(str(self.film))
        self.player.read(4)
        self.session.subtitle_tracks()
        self.session.set_subtitle_track(-1)
        self.assertTrue(self.session.add_subtitle_file(str(self.film)))
        self.assertEqual(len(self.slaves()), 1)
        self.assertEqual(self.session.subtitle_track(), 4)

    def test_a_file_gone_before_a_reopen_is_forgotten(self):
        self.play()
        self.session.add_subtitle_file(str(self.film))
        self.film.unlink()
        with self.assertLogs("services.playback", "INFO"):
            self.reopen()
        self.assertEqual(len(self.slaves()), 1)
        self.reopen()  # nothing to add any more
        self.assertEqual(len(self.slaves()), 1)

    def test_the_gui_never_waits_on_the_player_for_them(self):
        self.play()
        self.play()  # a job joins the old input
        self.assertFalse(self.session.add_subtitle_file(str(self.film)))
        self.session.set_subtitle_delay(100)  # kept for the media that opens next
        self.assertEqual(self.session.subtitle_track(), -1)
        self.runner.finish()
        self.assertEqual(self.player.spu_delay, 100_000)


class _AudioPlayer(_FakePlayer):
    """libVLC's audio side, as measured with libVLC 3.0.20 on macOS.

    Mute and volume live on the player's audio output. A stop closes it, and the next media
    opens a new one only once its audio decodes, which can come after the Playing state.
    While there is none, libVLC drops audio_set_mute and audio_set_volume and reports -1; a
    new output comes up with the state the last one had.
    """

    def __init__(self):
        super().__init__()
        self.state = vlc.State.Opening
        self.output = True  # libVLC opens one with the player
        self.mute, self.level = False, 100
        self.probes = 0  # audio_get_mute calls: how the session looks for the output

    def get_state(self):
        self._call("get_state")
        return self.state

    def stop(self):
        super().stop()
        self.output = False

    def open_output(self):
        self.output = True

    def audio_get_mute(self):
        self.probes += 1
        return int(self.mute) if self.output else -1

    def audio_set_mute(self, muted):
        self.calls.append(("audio_set_mute", muted))
        if self.output:
            self.mute = bool(muted)

    def audio_get_volume(self):
        return self.level if self.output else -1

    def audio_set_volume(self, value):
        self.calls.append(("audio_set_volume", value))
        if not self.output:
            return -1
        self.level = value
        return 0


@unittest.skipIf(vlc is None, "python-vlc's states are needed to poll the player")
class AudioSettingsTests(unittest.TestCase):
    """A player keeps the mute and volume it was asked for across stops and new media."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.player = _AudioPlayer()
        self.runner = _HeldRunner(self.player)
        self.session = PlaybackSession(
            _FakeInstance(self.player), runner=self.runner, poll_ms=60_000
        )
        self.addCleanup(self.session.release)

    def stopped(self):
        """A stream played and was stopped: libVLC closed the audio output."""
        self.session.play(A, kind=PlayKind.LIVE)
        self.session.stop()
        self.runner.finish()
        self.assertFalse(self.player.output)

    def playing(self, url, *, output):
        self.session.play(url, kind=PlayKind.LIVE)
        if self.runner.jobs:
            self.runner.finish()
        self.player.state = vlc.State.Playing
        if output:
            self.player.open_output()
        self.session._tick()

    def audio_calls(self):
        return [c for c in self.player.calls if c[0] in ("audio_set_mute", "audio_set_volume")]

    def test_mute_and_volume_asked_for_without_an_audio_output_apply_once_one_opens(self):
        self.stopped()
        self.session.set_muted(True)
        self.session.set_volume(30)
        self.assertEqual((self.player.mute, self.player.level), (False, 100))  # dropped
        self.playing(B, output=False)  # Playing before its audio output opened
        self.assertEqual((self.player.mute, self.player.level), (False, 100))
        self.player.open_output()
        self.session._tick()
        self.assertEqual((self.player.mute, self.player.level), (True, 30))

    def test_they_are_applied_once_for_each_media(self):
        self.stopped()
        self.session.set_muted(True)
        self.playing(B, output=True)
        self.assertTrue(self.player.mute)
        self.player.calls.clear()
        self.session._tick()
        self.assertEqual(self.audio_calls(), [])
        self.playing(C, output=True)  # the next channel: applied again
        self.assertEqual(self.audio_calls(), [("audio_set_mute", True)])

    def test_toggle_mute_follows_what_was_asked_without_an_audio_output(self):
        self.stopped()
        self.assertTrue(self.session.toggle_mute())
        self.assertFalse(self.session.toggle_mute())
        self.assertTrue(self.session.toggle_mute())
        self.playing(B, output=True)
        self.assertTrue(self.player.mute)

    def test_volume_reports_what_was_asked_without_an_audio_output(self):
        self.stopped()
        self.session.set_volume(30)
        self.assertEqual(self.session.volume(), 30)

    def test_volume_before_any_request_is_libvlcs_default_without_an_output(self):
        self.stopped()
        self.assertEqual(self.session.volume(), 100)
        self.session.set_volume(self.session.volume() - 5)  # Vol- while the zap loads
        self.playing(B, output=True)
        self.assertEqual(self.player.level, 95)

    def test_volume_remembers_the_level_read_from_an_open_output_without_asking(self):
        self.player.level = 60
        self.assertEqual(self.session.volume(), 60)
        self.stopped()
        self.assertEqual(self.session.volume(), 60)
        self.playing(B, output=True)
        self.assertEqual(self.audio_calls(), [])  # reading it asked for nothing

    def start_waiting_for_output(self):
        self.stopped()
        self.session.set_muted(True)
        self.session.play(B, kind=PlayKind.LIVE)
        self.player.state = vlc.State.Playing

    def test_the_wanted_audio_lands_soon_after_the_output_opens(self):
        self.start_waiting_for_output()
        self.player.open_output()
        elapse(_AUDIO_POLL_MS * 2)  # no regular poll comes meanwhile (poll_ms is 60 s here)
        self.assertTrue(self.player.mute)

    def test_the_fast_watch_ends_once_applied(self):
        self.start_waiting_for_output()
        self.player.open_output()
        elapse(_AUDIO_POLL_MS * 2)
        probes = self.player.probes
        elapse(_AUDIO_POLL_MS * 4)
        self.assertEqual(self.player.probes, probes)

    def test_the_fast_watch_gives_up_on_media_without_audio(self):
        with mock.patch("services.playback._AUDIO_WATCH_MS", _AUDIO_POLL_MS * 3):
            self.start_waiting_for_output()
            elapse(_AUDIO_POLL_MS * 6)
            probes = self.player.probes
            self.assertGreater(probes, 0)
            elapse(_AUDIO_POLL_MS * 4)
        self.assertEqual(self.player.probes, probes)  # the regular poll still tries

    def test_the_fast_watch_ends_with_a_stop(self):
        self.start_waiting_for_output()
        self.session.stop()
        self.runner.finish()
        probes = self.player.probes
        elapse(_AUDIO_POLL_MS * 4)
        self.assertEqual(self.player.probes, probes)

    def test_a_player_never_asked_is_left_alone(self):
        self.stopped()
        self.playing(B, output=True)
        self.assertEqual(self.audio_calls(), [])


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
        self.addCleanup(release_and_wait, self, session)
        session.play(A, kind=PlayKind.LIVE)
        session.play(B, kind=PlayKind.LIVE)
        self.assertTrue(wait_for(lambda: ("set_media", B) in player.calls, 4000))
        session.stop()
        # This session's jobs only (a thread another test left running must not count), with
        # the time a slow first libVLC use can take (see STOP_HANG_MS).
        self.assertTrue(wait_for(lambda: session._runner.pending() == 0, STOP_HANG_MS))
        self.assertIn(("stop",), player.calls)
        self.assertFalse(player.deadlocked)


def _on(system):
    return mock.patch("services.playback.platform.system", return_value=system)


class _CastPlayer(_FakePlayer):
    def set_renderer(self, renderer):
        self._call("set_renderer", renderer)
        return 0


class CastTests(unittest.TestCase):
    """Casting (2.8): libVLC sends what plays to a renderer given before the play."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.player = _CastPlayer()
        self.runner = _HeldRunner(self.player)
        self.session = PlaybackSession(
            _FakeInstance(self.player), runner=self.runner, poll_ms=60_000
        )
        self.addCleanup(self.session.release)
        held = mock.patch("services.casting.hold", side_effect=lambda handle: handle)
        self.hold = held.start()
        self.addCleanup(held.stop)
        released = mock.patch("services.casting.release")
        self.release = released.start()
        self.addCleanup(released.stop)

    def calls(self, name):
        return [call for call in self.player.calls if call[0] == name]

    def test_the_session_holds_the_device_it_casts_to(self):  # 2.8 review I9
        # A device found again has a new item and its old one is released: the session's own
        # hold keeps the one it casts to valid until it casts elsewhere or closes.
        self.session.cast_to("tv")
        self.hold.assert_called_once_with("tv")
        self.session.cast_to("den")
        self.release.assert_called_once_with("tv")
        self.session.cast_to(None)
        self.assertEqual(self.release.call_args.args, ("den",))
        self.session.cast_to("tv")
        self.session.release()
        self.assertEqual(self.release.call_args.args, ("tv",))

    def test_the_renderer_is_set_before_each_play_while_casting(self):
        self.session.cast_to("tv")
        self.session.play(A, kind=PlayKind.VOD_FILE)
        names = [call[0] for call in self.player.calls if call[0] in ("set_renderer", "play")]
        self.assertEqual(names, ["set_renderer", "play"])
        self.assertEqual(self.calls("set_renderer")[-1], ("set_renderer", "tv"))
        self.assertTrue(self.session.casting())

    def test_stopping_the_cast_gives_the_picture_back_on_the_next_play(self):
        self.session.cast_to("tv")
        self.session.play(A, kind=PlayKind.VOD_FILE)
        self.runner.jobs.clear()
        self.player.joining = False
        self.session.cast_to(None)
        self.assertFalse(self.session.casting())
        self.session.play(B, kind=PlayKind.VOD_FILE)
        if self.runner.jobs:  # the old input ends first
            self.runner.finish()
        self.assertEqual(self.calls("set_renderer")[-1], ("set_renderer", None))

    def test_never_casting_never_touches_the_renderer(self):
        self.session.play(A, kind=PlayKind.VOD_FILE)
        self.assertEqual(self.calls("set_renderer"), [])

    def test_a_reopen_plays_the_same_media_again_from_a_point(self):
        self.session.play(A, kind=PlayKind.VOD_FILE, user_agent="QiTV-test")
        self.runner.jobs.clear()
        self.player.joining = False
        self.session.reopen(42_000)
        if self.runner.jobs:
            self.runner.finish()
        opened = [call[1] for call in self.player.calls if call[0] == "set_media" and call[1]]
        self.assertEqual(opened, [A, A])
        self.assertEqual(self.session._start_ms, 42_000)


class DefaultArgsTests(unittest.TestCase):
    def test_quiet_unless_a_log_file_is_named(self):
        with mock.patch.dict(os.environ, {VLC_LOG_ENV: ""}), _on("Darwin"):
            self.assertEqual(
                default_args(hardware_decoding=True),  # decoding: tests/test_video_decoding.py
                ["--no-video-title-show", "--no-volume-save", "--quiet"],
            )

    def test_windows_plays_audio_through_directsound(self):
        # mmdevice sets volume and mute on the process's audio session: main and PiP would
        # mute and level each other. DirectSound sets them on each player's own buffer.
        with mock.patch.dict(os.environ, {VLC_LOG_ENV: ""}), _on("Windows"):
            quiet = default_args(hardware_decoding=True)
        self.assertEqual(
            quiet, ["--no-video-title-show", "--no-volume-save", "--aout=directsound", "--quiet"]
        )
        with (
            mock.patch.dict(os.environ, {VLC_LOG_ENV: "/tmp/smoke/libvlc.log"}),
            _on("Windows"),
            self.assertLogs("services.playback", "WARNING"),
        ):
            self.assertIn("--aout=directsound", default_args())

    def test_no_player_inherits_the_last_level_another_one_set(self):
        # VLC 3's volume-save keeps the last level set on any audio output in the
        # process's config, and every output opened later starts there. The PiP's 30 became
        # the large picture's (verified with a real output on macOS; the dummy output keeps
        # no volume, so only the argument can be asserted here).
        for system in ("Darwin", "Linux", "Windows"):
            with mock.patch.dict(os.environ, {VLC_LOG_ENV: ""}), _on(system):
                self.assertIn("--no-volume-save", default_args())

    def test_other_platforms_keep_libvlcs_audio_output(self):
        for system in ("Darwin", "Linux"):
            with mock.patch.dict(os.environ, {VLC_LOG_ENV: ""}), _on(system):
                self.assertFalse([arg for arg in default_args() if arg.startswith("--aout")])

    def test_arguments_given_to_create_instance_are_used_as_they_are(self):
        with mock.patch("services.playback.vlc") as fake, _on("Windows"):
            create_instance(["--aout=dummy", "--quiet"])
        fake.Instance.assert_called_once_with(["--aout=dummy", "--quiet"])

    def test_a_named_log_file_gets_the_verbose_log(self):
        with (
            mock.patch.dict(os.environ, {VLC_LOG_ENV: "/tmp/smoke/libvlc.log"}),
            self.assertLogs("services.playback", "WARNING"),
        ):
            args = default_args()
        self.assertIn("--logfile=/tmp/smoke/libvlc.log", args)
        self.assertIn("--file-logging", args)
        self.assertIn("--verbose=2", args)
        self.assertNotIn("--quiet", args)


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
