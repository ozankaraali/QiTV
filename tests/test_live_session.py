import os
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import services.live.live_session as live_session
from services.live.live_session import LiveSession
from services.live.timeline import Span
from services.playback import PlaybackState
from services.vlc_profile import PlayKind

app = QApplication.instance() or QApplication([])

URL = "http://provider.example/live/user/secret/42.ts"
UNAVAILABLE = "Time-shift isn't available for this channel"
LOST = "Lost the time-shift buffer; playing live"
BUFFERED = "Lost the connection; playing what's buffered"
DISK = "Disk time-shift isn't available; using memory"
REWOUND = "Rewound past the buffer"
DIRECT = (URL, PlayKind.LIVE, "normal")
MiB = 1024 * 1024


def segments(first_seq, count, *, start=0.0, length=2.0):
    """`count` closed spans of `length` seconds from `first_seq`, the first at `start`."""
    return [
        Span(first_seq + i, start + i * length, start + (i + 1) * length, False)
        for i in range(count)
    ]


class FakeEngine(QObject):
    """LiveEngine's GUI-side surface; the test drives its buffer and its signals."""

    available = Signal(bool)
    sessionReady = Signal(str)
    sessionUpdated = Signal(str, dict)
    sessionFailed = Signal(str, str, str)
    sessionEnded = Signal(str)
    sessionClosed = Signal(str)

    def __init__(self):
        super().__init__()
        self.up = True
        self.opened = []  # (sid, url, keyword arguments)
        self.closed = []
        self.events = []  # ("open", sid) as sessions open
        self._answered = set()
        self._spans = {}
        self._snapshots = {}

    @property
    def last_sid(self):
        return self.opened[-1][0]

    def is_available(self):
        return self.up

    def open_session(self, url, **kwargs):
        sid = f"s{len(self.opened) + 1}"
        self.opened.append((sid, url, kwargs))
        self.events.append(("open", sid))
        return sid

    def close_session(self, sid):
        self.closed.append(sid)

    def answer(self):
        """The helper confirms every close so far."""
        for sid in self.closed:
            if sid not in self._answered:
                self._answered.add(sid)
                self.sessionClosed.emit(sid)

    def lose(self):
        """A second helper death: time-shift is off for the run."""
        self.up = False
        self.available.emit(False)

    def spans(self, sid):
        return list(self._spans.get(sid, []))

    def snapshot(self, sid):
        return dict(self._snapshots.get(sid, {}))

    def stream_url(self, sid, seq):
        return f"http://127.0.0.1:9/token/{sid}/stream/{seq}.ts" if self.up else ""

    def buffer(self, sid, spans, *, live_edge=None, open_seq=None, burst=0.0):
        """Closed `spans` plus an optional open segment, as the helper's state reports them."""
        end = spans[-1].end if spans else 0.0
        oldest = spans[0].seq if spans else open_seq
        self._spans[sid] = list(spans)
        self._snapshots[sid] = {
            "oldest": oldest,
            "newest": open_seq if open_seq is not None else (spans[-1].seq if spans else None),
            "open": open_seq,
            "start": spans[0].start if spans else end,
            "end": end,
            "live_edge": end if live_edge is None else live_edge,
            "bytes": 0,
            "ended": False,
            "burst_s": burst,
        }
        self.sessionUpdated.emit(sid, dict(self._snapshots[sid]))

    def reset(self, sid):
        """The helper holding `sid` died: its spans and snapshot are gone."""
        self._spans.pop(sid, None)
        self._snapshots.pop(sid, None)


class FakePlayback(QObject):
    """PlaybackSession's surface: records calls; `ms` and `current` are set by the test."""

    stateChanged = Signal(str)
    positionChanged = Signal(int, int)
    errorOccurred = Signal(str)
    ended = Signal()

    def __init__(self):
        super().__init__()
        self.plays = []  # (url, kind, latency)
        self.stops = 0
        self.paused = []
        self.rates = []
        self.toggles = 0
        self.ms = 0
        self.current = PlaybackState.IDLE
        self.fail_on_play = False

    def play(self, url, *, kind, latency="normal", user_agent=None, start_ms=None):
        self.plays.append((url, kind, latency))
        self.ms = 0
        self.set_state(PlaybackState.OPENING)
        if self.fail_on_play:
            self.set_state(PlaybackState.ERROR)
            self.errorOccurred.emit("VLC could not start playback")

    def set_state(self, state):
        self.current = state
        self.stateChanged.emit(state.value)

    def stop(self):
        self.stops += 1
        self.set_state(PlaybackState.IDLE)

    def state(self):
        return self.current

    def time_ms(self):
        return self.ms

    def set_paused(self, paused):
        self.paused.append(paused)

    def toggle_pause(self):
        self.toggles += 1

    def set_rate(self, rate):
        self.rates.append(rate)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class LiveSessionCase(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.live = self.make()

    def make(self, **kwargs):
        """A LiveSession on fresh fakes: an earlier one stays wired only to its own."""
        self.engine = FakeEngine()
        self.playback = FakePlayback()
        kwargs.setdefault("tick_ms", 3_600_000)  # ticks are driven by the test
        kwargs.setdefault("clock", self.clock)
        live = LiveSession(self.engine, self.playback, **kwargs)
        self.modes, self.notices, self.failures = [], [], []
        self.releases, self.timelines = [], []
        live.modeChanged.connect(self.modes.append)
        live.notice.connect(self.notices.append)
        live.failed.connect(self.failures.append)
        live.released.connect(lambda: self.releases.append(True))
        live.released.connect(lambda: self.engine.events.append(("released",)))
        live.timelineChanged.connect(self.timelines.append)
        self.addCleanup(self.engine.answer)  # runs after live.stop (cleanups are LIFO)
        self.addCleanup(live.stop)
        return live

    def patch(self, name, value):
        self.enterContext(mock.patch.object(live_session, name, value))

    # ---- helpers ----------------------------------------------------------------------
    def start(self, latency="normal", disk=False, max_bytes=128 * MiB):
        self.live.start(
            URL,
            headers={"User-Agent": "QiTV-Test"},
            verify_ssl=True,
            latency=latency,
            user_agent="QiTV-Test",
            disk=disk,
            max_bytes=max_bytes,
        )
        return self.engine.last_sid if self.engine.opened else None

    def ready(self, spans, *, live_edge=None, open_seq=None, burst=0.0, latency="normal"):
        sid = self.start(latency=latency)
        self.engine.buffer(sid, spans, live_edge=live_edge, open_seq=open_seq, burst=burst)
        self.engine.sessionReady.emit(sid)
        return sid

    def stream(self, sid, seq):
        return (self.engine.stream_url(sid, seq), PlayKind.LIVE_PIPELINE, "normal")

    def step(self, seconds, media=None):
        """Let `seconds` of wall time pass while VLC advances by `media` (default: as much)."""
        self.clock.now += seconds
        self.playback.ms += round((seconds if media is None else media) * 1000)
        self.live._tick()

    def wait_until(self, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > deadline:
                self.fail("timed out")
            QTest.qWait(10)

    def active_timers(self):
        return [timer for timer in self.live.findChildren(QTimer) if timer.isActive()]

    def assert_falls_back(self, sid, notice, plays_before):
        """Closed, nothing direct until the close is confirmed, then direct play."""
        self.assertIn(sid, self.engine.closed)
        self.assertEqual(len(self.playback.plays), plays_before)
        self.assertNotIn("direct", self.modes)
        self.engine.answer()
        self.assertEqual(self.playback.plays[-1], DIRECT)
        self.assertEqual(self.notices[-1], notice)
        self.assertEqual(self.modes[-1], "direct")
        self.assertFalse(self.live.is_pipeline())


class StartTests(LiveSessionCase):
    def test_ready_plays_the_oldest_segment_through_the_pipeline(self):
        sid = self.start(disk=True, max_bytes=2048 * MiB)
        _, url, kwargs = self.engine.opened[0]
        self.assertEqual(url, URL)
        self.assertEqual(
            kwargs,
            {
                "headers": {"User-Agent": "QiTV-Test"},
                "verify_ssl": True,
                "disk": True,
                "max_bytes": 2048 * MiB,
                "live_delay": 6.0,
            },
        )
        self.assertEqual(self.playback.plays, [])  # nothing plays before ready

        modes_at_open = []
        self.playback.stateChanged.connect(
            lambda state: modes_at_open.append(self.live.mode) if state == "opening" else None
        )
        self.engine.buffer(sid, [], open_seq=0)  # the first segment is still open
        self.engine.sessionReady.emit(sid)
        self.assertEqual(self.playback.plays, [self.stream(sid, 0)])
        self.assertEqual(modes_at_open, ["pipeline"])  # set before the first play
        self.assertEqual(self.modes, ["pipeline"])
        self.assertTrue(self.live.is_pipeline())
        self.assertEqual(self.live.mode, "pipeline")
        self.assertEqual(self.live.position(), 0.0)
        self.assertEqual(self.notices, [])

    def test_ready_bases_the_position_on_the_oldest_closed_segment(self):
        sid = self.ready(segments(7, 3, start=14.0), live_edge=20.5)
        self.assertEqual(self.playback.plays, [self.stream(sid, 7)])
        self.assertEqual(self.live.position(), 14.0)
        self.assertEqual(self.live.buffered_range(), (14.0, 20.5))

    def test_live_delay_follows_the_latency_setting(self):
        for latency, delay in (("low", 3.0), ("normal", 6.0), ("stable", 12.0)):
            with self.subTest(latency=latency):
                self.start(latency=latency)
                self.assertEqual(self.engine.opened[-1][2]["live_delay"], delay)
                self.live.stop()
                self.engine.answer()

    def test_opens_before_the_helper_says_hello(self):
        self.engine.up = False  # spawning: no hello yet, but no available(False) either
        sid = self.start()
        self.assertEqual([opened[0] for opened in self.engine.opened], [sid])
        self.assertEqual(self.playback.plays, [])
        self.engine.up = True
        self.engine.available.emit(True)
        self.engine.buffer(sid, [], open_seq=0)
        self.engine.sessionReady.emit(sid)
        self.assertEqual(self.playback.plays, [self.stream(sid, 0)])
        self.assertEqual(self.modes, ["pipeline"])

    def test_a_dead_engine_plays_directly_at_once(self):
        self.engine.lose()
        self.start(latency="low")
        self.assertEqual(self.engine.opened, [])
        self.assertEqual(self.playback.plays, [(URL, PlayKind.LIVE, "low")])
        self.assertEqual(self.notices, [UNAVAILABLE])
        self.assertEqual(self.modes, ["direct"])
        self.assertFalse(self.live.is_pipeline())

    def test_an_unsupported_source_falls_back_once_the_session_closed(self):
        sid = self.start()
        self.engine.sessionFailed.emit(sid, "unsupported", "This stream format isn't supported.")
        self.assert_falls_back(sid, UNAVAILABLE, 0)
        self.assertEqual(self.notices, [UNAVAILABLE])
        self.assertEqual(self.modes, ["direct"])

    def test_not_ready_in_time_falls_back(self):
        self.live = self.make(ready_timeout_ms=50)
        sid = self.start()
        self.wait_until(lambda: self.engine.closed)
        self.assert_falls_back(sid, UNAVAILABLE, 0)
        self.engine.buffer(sid, [], open_seq=0)
        self.engine.sessionReady.emit(sid)  # too late
        self.assertEqual(len(self.playback.plays), 1)

    def test_a_fallback_plays_directly_after_the_close_cap(self):
        self.patch("_CLOSE_CAP_MS", 50)
        sid = self.start()
        with self.assertLogs("services.live.live_session", "WARNING"):
            self.engine.sessionFailed.emit(sid, "network", "The stream could not be reached.")
            self.assertEqual(self.playback.plays, [])
            self.wait_until(lambda: self.playback.plays)
        self.assertEqual(self.playback.plays, [DIRECT])
        self.assertEqual(self.notices, [UNAVAILABLE])
        self.engine.answer()  # a late confirmation changes nothing
        self.assertEqual(len(self.playback.plays), 1)

    def test_a_disk_failure_before_ready_keeps_waiting(self):
        sid = self.start(disk=True)
        self.engine.sessionFailed.emit(sid, "disk", "The disk buffer failed.")
        self.assertEqual(self.notices, [DISK])
        self.assertEqual(self.playback.plays, [])
        self.assertEqual(self.engine.closed, [])
        self.engine.buffer(sid, [], open_seq=0)
        self.engine.sessionReady.emit(sid)
        self.assertEqual(self.playback.plays, [self.stream(sid, 0)])
        self.assertEqual(self.modes, ["pipeline"])

    def test_a_late_ready_after_stop_is_ignored(self):
        sid = self.start()
        self.live.stop()
        self.assertEqual(self.engine.closed, [sid])
        self.engine.buffer(sid, [], open_seq=0)
        self.engine.sessionReady.emit(sid)
        self.assertEqual(self.playback.plays, [])
        self.assertEqual(self.modes, [])

    def test_zapping_waits_for_the_old_session_to_close(self):
        first = self.start()
        self.start()
        self.assertEqual(self.engine.closed, [first])
        self.assertEqual(len(self.engine.opened), 1)  # the new one opens after the close
        self.engine.buffer(first, [], open_seq=0)
        self.engine.sessionReady.emit(first)
        self.engine.sessionFailed.emit(first, "network", "The stream could not be reached.")
        self.assertEqual(self.playback.plays, [])
        self.assertEqual(self.releases, [])
        self.engine.answer()
        second = self.engine.last_sid
        self.assertEqual(self.engine.events, [("open", first), ("released",), ("open", second)])
        self.engine.buffer(second, [], open_seq=0)
        self.engine.sessionReady.emit(second)
        self.assertEqual(self.playback.plays, [self.stream(second, 0)])

    def test_zapping_opens_after_the_close_cap(self):
        self.patch("_CLOSE_CAP_MS", 50)
        self.start()
        with self.assertLogs("services.live.live_session", "WARNING"):
            self.start()
            self.wait_until(lambda: len(self.engine.opened) == 2)
        self.assertEqual(self.engine.events[1:], [("released",), ("open", "s2")])

    def test_start_resets_the_position(self):
        self.ready(segments(5, 3, start=10.0), live_edge=16.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.step(1.0)
        self.live.stop()
        self.engine.answer()
        self.start()
        self.assertEqual(self.live.position(), 0.0)


class ReleaseTests(LiveSessionCase):
    def test_released_follows_session_closed(self):
        sid = self.ready(segments(0, 2))
        self.live.stop()
        self.assertEqual(self.playback.stops, 1)
        self.assertEqual(self.engine.closed, [sid])
        self.assertEqual(self.releases, [])
        self.engine.sessionClosed.emit("other")
        self.assertEqual(self.releases, [])
        self.engine.answer()
        self.assertEqual(self.releases, [True])
        self.engine.sessionClosed.emit(sid)
        self.assertEqual(self.releases, [True])
        self.assertEqual(self.live.mode, "")
        self.assertEqual(self.active_timers(), [])

    def test_released_after_the_close_cap(self):
        self.patch("_CLOSE_CAP_MS", 50)
        self.ready(segments(0, 2))
        with self.assertLogs("services.live.live_session", "WARNING"):
            self.live.stop()
            self.wait_until(lambda: self.releases)
        self.engine.answer()
        self.assertEqual(self.releases, [True])
        self.assertEqual(self.active_timers(), [])

    def test_direct_mode_releases_right_after_stopping_playback(self):
        self.engine.lose()
        self.start()
        stops = []
        self.live.released.connect(lambda: stops.append(self.playback.stops))
        self.live.stop()
        self.assertEqual(stops, [1])
        self.live.stop()  # idle: nothing more
        self.assertEqual(self.releases, [True])
        self.assertEqual(self.active_timers(), [])

    def test_a_stop_during_the_fallback_wait_releases_on_that_close(self):
        sid = self.ready(segments(0, 3), live_edge=6.0)
        self.engine.sessionFailed.emit(sid, "network", "The stream could not be reached.")
        self.live.stop()
        self.assertEqual(self.releases, [])
        self.engine.answer()
        self.assertEqual(self.releases, [True])
        self.assertEqual(len(self.playback.plays), 1)  # the fallback was cancelled
        self.assertEqual(self.notices, [])
        self.assertEqual(self.active_timers(), [])

    def test_after_a_completed_fallback_stop_releases_at_once(self):
        sid = self.ready(segments(0, 3), live_edge=6.0)
        self.engine.sessionFailed.emit(sid, "network", "The stream could not be reached.")
        self.engine.answer()
        self.assertEqual(self.active_timers(), [])
        self.live.stop()
        self.assertEqual(self.releases, [True])

    def test_timers_are_idle_after_a_fallback(self):
        sid = self.start()
        self.engine.sessionFailed.emit(sid, "unsupported", "This stream format isn't supported.")
        self.engine.answer()
        self.assertEqual(self.playback.plays, [DIRECT])
        self.assertEqual(self.active_timers(), [])


class SeekTests(LiveSessionCase):
    def test_seek_maps_to_the_segment_and_keeps_the_pause(self):
        sid = self.ready(segments(0, 10), live_edge=20.5)
        self.playback.set_state(PlaybackState.PLAYING)
        self.live.toggle_pause()
        self.assertEqual(self.playback.paused, [True])
        self.playback.set_state(PlaybackState.PAUSED)

        self.live.seek_to(5.0)
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 2))
        self.assertEqual(self.live.position(), 4.0)
        self.playback.set_state(PlaybackState.PLAYING)  # the reopened stream starts
        self.assertEqual(self.playback.paused, [True, True])

        self.live.seek_to(9.0)  # a second seek is still paused
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 4))
        self.playback.set_state(PlaybackState.PLAYING)
        self.assertEqual(self.playback.paused, [True, True, True])

        self.step(0.25, media=0)
        self.assertEqual(self.timelines[-1]["mode"], "shifted")
        self.assertTrue(self.timelines[-1]["paused"])

    def test_seek_relative_moves_from_the_position(self):
        sid = self.ready(segments(0, 10), live_edge=20.5)
        self.playback.set_state(PlaybackState.PLAYING)
        self.step(1.0)
        self.step(1.0)
        self.step(1.0)  # 3.0
        self.live.seek_relative(10.0)
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 6))
        self.assertEqual(self.live.position(), 12.0)
        self.live.seek_relative(-10.0)
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 1))

    def test_seek_clamps_to_the_closed_segments(self):
        # A 10 s open segment follows the closed ones: it is not addressable.
        sid = self.ready(segments(3, 3, start=6.0), live_edge=22.0)
        self.live.seek_to(-50.0)
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 3))
        self.live.seek_to(15.0)
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 5))

    def test_seek_with_nothing_closed_yet_is_ignored(self):
        self.ready([], open_seq=0, live_edge=10.0)
        self.live.seek_to(0.0)
        self.assertEqual(len(self.playback.plays), 1)

    def test_seeking_back_leaves_live(self):
        self.ready(segments(0, 10), live_edge=20.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.live.seek_to(4.0)
        self.step(0.25)
        self.assertEqual(self.timelines[-1]["mode"], "shifted")

    def test_a_seek_near_live_goes_live(self):
        sid = self.ready(segments(0, 10), live_edge=20.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.live.seek_to(4.0)
        self.live.seek_to(18.0)  # within the 2.5 s target of the edge
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 8))
        self.step(0.25)
        self.assertEqual(self.timelines[-1]["mode"], "live")
        self.live.seek_to(4.0)
        self.live.seek_relative(30.0)  # past the edge
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 8))
        self.step(0.25)
        self.assertEqual(self.timelines[-1]["mode"], "live")

    def test_go_live_picks_the_newest_segment_at_least_the_target_behind(self):
        sid = self.ready(segments(0, 10), live_edge=20.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.live.seek_to(3.0)
        self.live.set_speed(1.5)
        self.assertEqual(self.playback.rates, [1.5])
        self.live.go_live()
        # Normal: 2.5 s behind 20.0 is 17.5; segment 8 starts at 16, segment 9 at 18.
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 8))
        self.assertEqual(self.playback.rates, [1.5, 1.0])
        self.assertEqual(self.live.position(), 16.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.step(0.25)  # back in LIVE: stay-live nudges the 3.75 s lag
        self.assertEqual(self.timelines[-1]["mode"], "live")
        self.assertEqual(self.playback.rates, [1.5, 1.0, 1.05])

    def test_go_live_with_bursts_aims_half_a_burst_further_back(self):
        sid = self.ready(segments(0, 10), live_edge=20.0, burst=6.0)
        self.live.go_live()
        # The 6 s burst is the target; plus half a burst, at most 11.0: segment 5.
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 5))

    def test_go_live_takes_the_newest_segment_when_none_is_far_enough_behind(self):
        sid = self.ready(segments(5, 2, start=10.0), live_edge=14.0, burst=6.0)
        self.live.go_live()
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 6))

    def test_go_live_resumes_a_paused_session(self):
        sid = self.ready(segments(0, 10), live_edge=20.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.live.toggle_pause()
        self.live.go_live()
        self.playback.set_state(PlaybackState.PLAYING)
        self.assertEqual(self.playback.paused, [True])  # not paused again
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 8))
        self.step(0.25)
        self.assertFalse(self.timelines[-1]["paused"])

    def test_speed_while_shifted_catches_up(self):
        self.ready(segments(0, 10), live_edge=20.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.live.seek_to(2.0)
        self.live.set_speed(1.5)
        self.step(0.25, media=0.375)
        self.assertEqual(self.timelines[-1]["mode"], "catching_up")
        self.assertEqual(self.timelines[-1]["rate"], 1.5)
        self.assertEqual(self.playback.rates, [1.5])

    def test_speed_at_live_is_ignored(self):
        self.ready(segments(0, 2), live_edge=4.0, latency="stable")
        self.playback.set_state(PlaybackState.PLAYING)
        self.live.set_speed(1.5)
        self.assertEqual(self.playback.rates, [])
        self.step(0.25)
        self.assertEqual(self.playback.rates, [])
        self.assertEqual(self.timelines[-1]["mode"], "live")

    def test_direct_mode_passes_pause_through_and_ignores_seeks(self):
        self.engine.lose()
        self.start()
        self.live.toggle_pause()
        self.live.seek_to(5.0)
        self.live.set_speed(2.0)
        self.live.go_live()
        self.assertEqual(self.playback.toggles, 1)
        self.assertEqual(len(self.playback.plays), 1)
        self.assertEqual(self.playback.rates, [])


class StayLiveTests(LiveSessionCase):
    def test_lag_above_target_nudges_the_rate_then_settles(self):
        self.ready(segments(0, 2), live_edge=4.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.step(0.25)  # position 0.25, lag 3.75 > 2.5 + 0.75
        self.assertEqual(self.playback.rates, [1.05])
        self.step(0.5, media=0.75)  # lag 3.0: inside the hysteresis band, keep 1.05
        self.assertEqual(self.playback.rates, [1.05])
        self.step(0.5, media=0.6)  # lag 2.4: within 0.2 s of the target
        self.assertEqual(self.playback.rates, [1.05, 1.0])
        rates = [timeline["rate"] for timeline in self.timelines]
        self.assertEqual(rates, [1.05, 1.05, 1.0])
        last = self.timelines[-1]
        self.assertAlmostEqual(last["position"], 1.6)
        self.assertAlmostEqual(last["lag"], 2.4)
        self.assertEqual(last["live_edge"], 4.0)
        self.assertEqual((last["start"], last["end"]), (0.0, 4.0))
        self.assertEqual(last["mode"], "live")
        self.assertFalse(last["paused"])
        self.assertFalse(last["ended"])
        self.assertEqual(last["target"], 2.5)

    def test_hls_bursts_raise_the_target(self):
        self.ready(segments(0, 2), live_edge=4.0, burst=6.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.step(0.25)  # lag 3.75 is under the 6 s burst: no nudge
        self.assertEqual(self.playback.rates, [])
        self.assertEqual(self.timelines[-1]["target"], 6.0)

    def play_bursts(self, initial_edge, seconds=120):
        """6 s segments arrive whole every 6 s while VLC plays at the rate it is given."""
        sid = self.start()
        edge, closed = initial_edge, int(initial_edge // 6)
        self.engine.buffer(
            sid, segments(0, closed, length=6.0), live_edge=edge, open_seq=closed, burst=6.0
        )
        self.engine.sessionReady.emit(sid)
        self.playback.set_state(PlaybackState.PLAYING)
        position, lags = 0.0, []
        for tick in range(1, seconds * 4 + 1):
            plays = len(self.playback.plays)
            advance = 0.25 * (self.playback.rates[-1] if self.playback.rates else 1.0)
            position += advance
            if tick % 24 == 0:
                edge, closed = edge + 6.0, closed + 1
                self.engine.buffer(
                    sid, segments(0, closed, length=6.0), live_edge=edge, open_seq=closed, burst=6.0
                )
            self.step(0.25, media=advance)
            if len(self.playback.plays) > plays:  # reopened: playback restarts there
                position = self.live.position()
                self.playback.set_state(PlaybackState.PLAYING)
            lags.append(edge - position)
        return lags

    def test_hls_bursts_settle_without_rate_flapping(self):
        lags = self.play_bursts(12.0)
        self.assertEqual(len(self.playback.plays), 1)  # no reopen
        self.assertEqual(self.playback.rates, [1.10, 1.05, 1.0])  # once down, no flapping
        self.assertGreater(min(lags[-24:]), 2.0)  # headroom before every burst
        reported = [timeline["lag"] for timeline in self.timelines[-24:]]
        self.assertLess(max(reported) - min(reported), 0.5)  # the mean, not the sawtooth

    def test_hls_bursts_settle_after_reopening_at_live(self):
        lags = self.play_bursts(60.0)
        self.assertEqual(len(self.playback.plays), 2)  # one reopen at live, no loop
        rates = self.playback.rates
        self.assertEqual(rates, sorted(rates, reverse=True))  # only ever down
        self.assertEqual(rates[-1], 1.0)
        self.assertGreater(min(lags[-24:]), 2.0)

    def test_no_rate_change_while_buffering(self):
        self.ready(segments(0, 2), live_edge=4.0)
        self.playback.set_state(PlaybackState.BUFFERING)
        self.step(0.25, media=0)
        self.assertEqual(self.playback.rates, [])

    def test_far_behind_reopens_at_live_once(self):
        sid = self.ready(segments(0, 20), live_edge=40.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.step(0.25)  # lag 39.75 > 2.5 + 20
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 18))
        self.playback.set_state(PlaybackState.PLAYING)
        for _ in range(8):
            self.step(0.25)
        self.assertEqual(len(self.playback.plays), 2)

    def test_latency_change_moves_the_target(self):
        self.ready(segments(0, 2), live_edge=4.0)
        self.live.set_latency("stable")
        self.playback.set_state(PlaybackState.PLAYING)
        self.step(0.25)
        self.assertEqual(self.playback.rates, [])
        self.assertEqual(self.timelines[-1]["target"], 6.0)


class PositionTests(LiveSessionCase):
    def test_position_follows_vlc_time(self):
        self.ready(segments(5, 3, start=10.0), live_edge=16.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.step(0.25)
        self.step(0.25, media=0.3)
        self.assertAlmostEqual(self.live.position(), 10.55)

    def test_a_vlc_time_jump_falls_back_to_wall_clock(self):
        # Stable's 6 s target: stay-live leaves the rate alone at this lag.
        self.ready(segments(0, 3), live_edge=6.0, latency="stable")
        self.playback.set_state(PlaybackState.PLAYING)
        self.step(0.25)
        self.assertAlmostEqual(self.live.position(), 0.25)
        self.step(0.25, media=30.25)  # get_time jumps by 30 s
        self.assertAlmostEqual(self.live.position(), 0.5)
        self.step(0.25)
        self.assertAlmostEqual(self.live.position(), 0.75)
        self.assertAlmostEqual(self.timelines[-1]["position"], 0.75)

    def test_wall_clock_counts_the_rate(self):
        self.ready(segments(0, 30), live_edge=60.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.live.seek_to(10.0)
        self.live.set_speed(2.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.step(1.0, media=40.0)
        self.assertAlmostEqual(self.live.position(), 12.0)

    def test_a_paused_player_does_not_advance(self):
        self.ready(segments(0, 30), live_edge=60.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.live.toggle_pause()
        self.playback.set_state(PlaybackState.PAUSED)
        self.step(30.0, media=0)
        self.assertEqual(self.live.position(), 0.0)


class InterruptionTests(LiveSessionCase):
    def test_a_long_pause_past_the_ring_rewinds_to_the_oldest(self):
        sid = self.ready(segments(0, 3), live_edge=6.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.step(1.0)
        self.live.toggle_pause()
        self.playback.set_state(PlaybackState.PAUSED)
        self.step(300.0, media=0)
        self.engine.buffer(sid, segments(150, 3, start=300.0), live_edge=306.5)
        self.live.toggle_pause()
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 150))
        self.assertEqual(self.notices, [REWOUND])
        self.assertEqual(self.live.position(), 300.0)
        self.assertEqual(self.playback.paused, [True])  # reopened, not unpaused in place
        self.playback.set_state(PlaybackState.PLAYING)
        self.assertEqual(self.playback.paused, [True])

    def test_a_resume_inside_the_buffer_unpauses_in_place(self):
        self.ready(segments(0, 3), live_edge=6.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.live.toggle_pause()
        self.live.toggle_pause()
        self.assertEqual(self.playback.paused, [True, False])
        self.assertEqual(len(self.playback.plays), 1)

    def test_an_ended_stream_past_the_ring_reopens_at_the_oldest(self):
        sid = self.ready(segments(0, 3), live_edge=6.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.step(1.0)
        self.engine.buffer(sid, segments(50, 3, start=100.0), live_edge=106.0)
        self.playback.set_state(PlaybackState.ENDED)
        self.playback.ended.emit()
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 50))
        self.assertEqual(self.notices, [REWOUND])

    def test_an_error_inside_the_buffer_reopens_at_the_position(self):
        sid = self.ready(segments(0, 5), live_edge=10.5)
        self.playback.set_state(PlaybackState.PLAYING)
        self.step(1.0)
        self.step(1.0)
        self.step(1.0)  # 3.0: segment 1
        self.playback.set_state(PlaybackState.ERROR)
        self.playback.errorOccurred.emit("The stream could not be played")
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 1))
        self.assertEqual(self.notices, [])
        self.assertEqual(self.failures, [])

    def test_errors_without_playing_end_in_direct_play(self):
        self.live = self.make(ready_timeout_ms=1000)
        sid = self.ready(segments(0, 5), live_edge=10.5)
        with self.assertLogs("services.live.live_session", "WARNING"):
            for _ in range(3):
                self.clock.now += 0.6
                self.playback.errorOccurred.emit("The stream could not be opened")
        self.assert_falls_back(sid, LOST, 3)  # ready, two reopens
        self.assertEqual(self.notices, [LOST])
        self.assertEqual(self.modes, ["pipeline", "direct"])

    def test_a_synchronous_open_error_retries_on_the_tick_without_recursion(self):
        self.live = self.make(ready_timeout_ms=1000)
        self.playback.fail_on_play = True
        sid = self.ready(segments(0, 5), live_edge=10.5)
        self.assertEqual(len(self.playback.plays), 1)
        self.step(0.5)
        self.assertEqual(len(self.playback.plays), 2)
        timelines = len(self.timelines)
        with self.assertLogs("services.live.live_session", "WARNING"):
            self.step(1.1)  # over a second of failures since the first retry
        self.assertEqual(len(self.timelines), timelines)  # nothing after the fallback
        self.playback.fail_on_play = False
        self.assert_falls_back(sid, LOST, 2)

    def test_after_the_session_ended_an_error_does_not_reopen(self):
        sid = self.ready(segments(0, 3), live_edge=6.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.engine.sessionEnded.emit(sid)
        self.engine.reset(sid)  # a helper death took the ended buffer
        self.playback.set_state(PlaybackState.ERROR)
        self.playback.errorOccurred.emit("The stream could not be played")
        self.assertEqual(len(self.playback.plays), 1)
        self.assertTrue(self.timelines[-1]["ended"])
        self.assertEqual(self.notices, [])
        self.assertEqual(self.engine.closed, [])
        self.step(0.25, media=0)
        self.assertTrue(self.timelines[-1]["ended"])
        self.assertEqual(self.timelines[-1]["live_edge"], 6.0)

    def test_a_disk_failure_after_ready_goes_live_on_the_ram_ring(self):
        sid = self.ready(segments(0, 10), live_edge=20.0)
        self.live.seek_to(2.0)
        # The failure arrives before any state of the swapped-in RAM ring.
        self.engine.sessionFailed.emit(sid, "disk", "The disk buffer failed.")
        self.assertEqual(self.notices, [DISK])
        self.assertEqual(len(self.playback.plays), 2)
        self.playback.errorOccurred.emit("The stream could not be played")  # cut off
        self.assertEqual(len(self.playback.plays), 2)  # not reopened on the old spans
        self.engine.buffer(sid, segments(10, 1, start=20.0), live_edge=23.0)
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 10))
        self.assertEqual(self.notices, [DISK])
        self.assertEqual(self.modes, ["pipeline"])

    def test_a_disk_failure_without_a_new_state_falls_back(self):
        self.live = self.make(ready_timeout_ms=1000)
        sid = self.ready(segments(0, 10), live_edge=20.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.engine.sessionFailed.emit(sid, "disk", "The disk buffer failed.")
        self.step(0.25)
        self.assertEqual(self.engine.closed, [])
        with self.assertLogs("services.live.live_session", "WARNING"):
            self.step(1.1)  # no state of the RAM ring for over the ready timeout
        self.assert_falls_back(sid, LOST, 1)

    def test_the_source_giving_up_before_the_ram_ring_reports_falls_back(self):
        sid = self.ready(segments(0, 10), live_edge=20.0)
        self.live.seek_to(4.0)
        self.engine.sessionFailed.emit(sid, "disk", "The disk buffer failed.")
        self.engine.sessionFailed.emit(sid, "network", "The stream could not be reached.")
        self.assert_falls_back(sid, LOST, 2)  # no history is left to play
        self.assertEqual(self.notices, [DISK, LOST])

    def test_the_source_giving_up_at_live_falls_back(self):
        for kind in ("network", "timeout", "http", "tls", "unsupported", "crashed", "new-kind"):
            with self.subTest(kind=kind):
                self.notices.clear()
                self.modes.clear()
                sid = self.ready(segments(0, 3), live_edge=6.0)
                self.playback.set_state(PlaybackState.PLAYING)
                self.step(0.25)  # lag 5.75: stay-live runs at 1.10
                self.assertEqual(self.playback.rates[-1], 1.10)
                plays = len(self.playback.plays)
                self.engine.sessionFailed.emit(sid, kind, "The stream could not be reached.")
                self.assert_falls_back(sid, LOST, plays)
                self.assertEqual(self.playback.rates[-1], 1.0)
                self.assertEqual(self.notices, [LOST])
                self.assertEqual(self.modes, ["pipeline", "direct"])
                self.engine.sessionFailed.emit(sid, "internal", "The live buffer stopped.")
                self.assertEqual(self.notices, [LOST])  # one fallback only

    def test_the_source_giving_up_while_shifted_plays_the_buffer_to_its_end(self):
        sid = self.ready(segments(0, 10), live_edge=20.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.live.seek_to(4.0)
        self.playback.set_state(PlaybackState.PLAYING)
        plays = len(self.playback.plays)
        self.engine.sessionFailed.emit(sid, "network", "The stream could not be reached.")
        self.engine.sessionFailed.emit(sid, "internal", "The live buffer stopped.")  # ignored
        self.assertEqual(self.notices, [BUFFERED])
        self.assertTrue(self.timelines[-1]["ended"])
        self.assertEqual(self.engine.closed, [])
        self.assertEqual(len(self.playback.plays), plays)
        self.live.seek_to(8.0)  # the buffer is still there
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 4))
        self.step(0.25)
        self.assertTrue(self.timelines[-1]["ended"])
        self.playback.set_state(PlaybackState.ENDED)
        self.playback.ended.emit()  # the end of what was buffered
        self.assert_falls_back(sid, LOST, plays + 1)
        self.assertEqual(self.notices, [BUFFERED, LOST])

    def test_go_live_after_the_source_gave_up_falls_back(self):
        sid = self.ready(segments(0, 10), live_edge=20.0)
        self.live.seek_to(4.0)
        self.engine.sessionFailed.emit(sid, "http", "The provider refused the stream.")
        self.assertEqual(self.notices, [BUFFERED])
        self.live.go_live()
        self.assert_falls_back(sid, LOST, 2)

    def test_a_session_crash_while_shifted_plays_the_buffer(self):
        sid = self.ready(segments(0, 10), live_edge=20.0)
        self.live.seek_to(4.0)
        self.engine.sessionFailed.emit(sid, "crashed", "The live buffer stopped unexpectedly.")
        self.assertEqual(self.notices, [BUFFERED])
        self.assertTrue(self.timelines[-1]["ended"])
        self.playback.errorOccurred.emit("The stream could not be played")
        self.assert_falls_back(sid, LOST, 2)

    def test_direct_play_errors_are_reported(self):
        sid = self.ready(segments(0, 3), live_edge=6.0)
        self.engine.sessionFailed.emit(sid, "network", "The stream could not be reached.")
        self.engine.answer()
        plays = len(self.playback.plays)
        self.playback.errorOccurred.emit("The stream could not be played")
        self.assertEqual(self.failures, ["The stream could not be played"])
        self.assertEqual(len(self.playback.plays), plays)


class HelperDeathTests(LiveSessionCase):
    def die(self, sid):
        self.engine.up = False
        self.engine.reset(sid)
        self.engine.sessionFailed.emit(sid, "internal", "The live buffer stopped unexpectedly.")

    def test_helper_death_reopens_at_live_then_a_second_death_plays_directly(self):
        sid = self.ready(segments(0, 3), live_edge=6.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.step(0.25)
        ticks = len(self.timelines)

        self.die(sid)
        self.playback.errorOccurred.emit("The stream could not be played")  # the dead server
        self.step(0.25)
        self.assertEqual(len(self.playback.plays), 1)
        self.assertEqual(len(self.timelines), ticks)

        self.engine.up = True
        self.engine.available.emit(True)
        self.assertEqual(len(self.playback.plays), 1)
        self.engine.buffer(sid, segments(0, 5), live_edge=10.0)  # the reopened session
        self.engine.sessionReady.emit(sid)
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 3))
        self.assertEqual(self.engine.closed, [])
        self.assertEqual(self.modes, ["pipeline"])

        self.die(sid)
        self.engine.lose()  # no helper is left, so neither is an upstream: at once
        self.assertEqual(self.playback.plays[-1], DIRECT)
        self.assertEqual(self.notices, [LOST])
        self.assertEqual(self.modes, ["pipeline", "direct"])
        self.assertEqual(self.engine.closed, [sid])
        self.assertEqual(self.active_timers(), [])

        self.engine.up = True  # never again in this run
        self.start()
        self.assertEqual(len(self.engine.opened), 1)
        self.assertEqual(self.notices, [LOST, UNAVAILABLE])

    def test_internal_always_waits_for_the_reopen(self):
        sid = self.ready(segments(0, 3), live_edge=6.0)
        self.engine.sessionFailed.emit(sid, "internal", "The live buffer stopped unexpectedly.")
        self.assertEqual(self.engine.closed, [])  # no is_available() guess: it is a death
        self.assertEqual(self.notices, [])
        self.engine.available.emit(True)
        self.engine.buffer(sid, segments(0, 5), live_edge=10.0)
        self.engine.sessionReady.emit(sid)
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 3))

    def test_internal_before_ready_waits_for_the_reopen(self):
        sid = self.start()
        self.engine.up = False
        self.engine.sessionFailed.emit(sid, "internal", "The live buffer stopped unexpectedly.")
        self.assertEqual(self.engine.closed, [])
        self.engine.up = True
        self.engine.available.emit(True)
        self.engine.buffer(sid, [], open_seq=0)
        self.engine.sessionReady.emit(sid)
        self.assertEqual(self.playback.plays, [self.stream(sid, 0)])
        self.assertEqual(self.notices, [])

    def test_recovery_is_bounded(self):
        self.patch("_RECOVERY_MS", 50)
        sid = self.ready(segments(0, 3), live_edge=6.0)
        self.die(sid)
        self.engine.up = True
        self.engine.available.emit(True)  # but the reopened session never gets ready
        self.wait_until(lambda: self.engine.closed)
        self.assert_falls_back(sid, LOST, 1)

    def test_recovery_is_bounded_without_a_new_helper(self):
        self.patch("_RECOVERY_MS", 50)
        sid = self.ready(segments(0, 3), live_edge=6.0)
        self.die(sid)
        self.wait_until(lambda: self.engine.closed)
        self.assert_falls_back(sid, LOST, 1)

    def test_a_reopen_that_fails_falls_back(self):
        sid = self.ready(segments(0, 3), live_edge=6.0)
        self.die(sid)
        self.engine.up = True
        self.engine.available.emit(True)
        self.engine.sessionFailed.emit(sid, "network", "The stream could not be reached.")
        self.assert_falls_back(sid, LOST, 1)

    def test_a_crash_of_the_reopened_session_falls_back(self):
        sid = self.ready(segments(0, 3), live_edge=6.0)
        self.die(sid)
        self.engine.up = True
        self.engine.available.emit(True)
        self.engine.sessionFailed.emit(sid, "crashed", "The live buffer stopped unexpectedly.")
        self.assert_falls_back(sid, LOST, 1)

    def test_internal_while_recovering_keeps_waiting_until_the_cap(self):
        self.patch("_RECOVERY_MS", 200)
        sid = self.ready(segments(0, 3), live_edge=6.0)
        self.die(sid)
        self.engine.up = True
        self.engine.available.emit(True)
        self.die(sid)  # the restarted helper died too: still waiting, but bounded
        self.assertEqual(self.engine.closed, [])
        self.wait_until(lambda: self.engine.closed)
        self.assert_falls_back(sid, LOST, 1)

    def test_stop_while_recovering(self):
        self.patch("_RECOVERY_MS", 50)
        sid = self.ready(segments(0, 3), live_edge=6.0)
        self.die(sid)
        self.live.stop()
        self.assertEqual(self.engine.closed, [sid])
        self.engine.answer()
        self.assertEqual(self.releases, [True])
        self.assertEqual(self.active_timers(), [])
        self.engine.up = True
        self.engine.available.emit(True)
        self.engine.buffer(sid, segments(0, 5), live_edge=10.0)
        self.engine.sessionReady.emit(sid)
        QTest.qWait(100)
        self.assertEqual(len(self.playback.plays), 1)
        self.assertEqual(self.notices, [])

    def test_engine_lost_during_the_wait_plays_directly_at_once(self):
        sid = self.start()
        self.engine.up = False
        self.engine.sessionFailed.emit(sid, "internal", "The live buffer is not available.")
        self.assertEqual(self.playback.plays, [])  # waiting for the reopen
        self.engine.lose()
        self.assertEqual(self.notices, [UNAVAILABLE])
        self.assertEqual(self.playback.plays, [DIRECT])
        self.assertEqual(self.active_timers(), [])

    def test_available_outside_recovery_is_harmless(self):
        sid = self.start()
        self.engine.available.emit(True)  # waiting
        self.engine.buffer(sid, segments(0, 3), live_edge=6.0)
        self.engine.sessionReady.emit(sid)
        self.engine.available.emit(True)  # playing
        self.assertEqual(self.playback.plays, [self.stream(sid, 0)])
        self.engine.sessionFailed.emit(sid, "unsupported", "This stream format isn't supported.")
        self.engine.answer()
        self.engine.available.emit(True)  # direct
        self.assertEqual(len(self.playback.plays), 2)
        self.assertEqual(self.notices, [LOST])
        self.assertEqual(self.modes, ["pipeline", "direct"])


if __name__ == "__main__":
    unittest.main()
