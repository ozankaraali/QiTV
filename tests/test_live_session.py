import os
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from services.live.live_session import LiveSession
from services.live.timeline import Span
from services.playback import PlaybackState
from services.vlc_profile import PlayKind

app = QApplication.instance() or QApplication([])

URL = "http://provider.example/live/user/secret/42.ts"
UNAVAILABLE = "Time-shift isn't available for this channel"
LOST = "Lost the time-shift buffer; playing live"
DISK = "Disk time-shift isn't available; using memory"
REWOUND = "Rewound past the buffer"
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
        return sid

    def close_session(self, sid):
        self.closed.append(sid)

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
        self.engine = FakeEngine()
        self.playback = FakePlayback()
        self.clock = Clock()
        self.live = self.make()

    def make(self, **kwargs):
        kwargs.setdefault("tick_ms", 3_600_000)  # ticks are driven by the test
        kwargs.setdefault("clock", self.clock)
        live = LiveSession(self.engine, self.playback, **kwargs)
        self.modes, self.notices, self.failures = [], [], []
        self.releases, self.timelines = [], []
        live.modeChanged.connect(self.modes.append)
        live.notice.connect(self.notices.append)
        live.failed.connect(self.failures.append)
        live.released.connect(lambda: self.releases.append(True))
        live.timelineChanged.connect(self.timelines.append)
        self.addCleanup(live.stop)
        return live

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

        self.engine.buffer(sid, [], open_seq=0)  # the first segment is still open
        self.engine.sessionReady.emit(sid)
        self.assertEqual(self.playback.plays, [self.stream(sid, 0)])
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

    def test_an_unavailable_engine_plays_directly(self):
        self.engine.up = False
        self.start(latency="low")
        self.assertEqual(self.engine.opened, [])
        self.assertEqual(self.playback.plays, [(URL, PlayKind.LIVE, "low")])
        self.assertEqual(self.notices, [UNAVAILABLE])
        self.assertEqual(self.modes, ["direct"])
        self.assertFalse(self.live.is_pipeline())

    def test_an_unsupported_source_plays_directly(self):
        sid = self.start()
        self.engine.sessionFailed.emit(sid, "unsupported", "This stream format isn't supported.")
        self.assertEqual(self.engine.closed, [sid])
        self.assertEqual(self.playback.plays, [(URL, PlayKind.LIVE, "normal")])
        self.assertEqual(self.notices, [UNAVAILABLE])
        self.assertEqual(self.modes, ["direct"])

    def test_not_ready_in_time_plays_directly(self):
        self.live = self.make(ready_timeout_ms=50)
        sid = self.start()
        self.wait_until(lambda: self.playback.plays)
        self.assertEqual(self.engine.closed, [sid])
        self.assertEqual(self.playback.plays, [(URL, PlayKind.LIVE, "normal")])
        self.assertEqual(self.notices, [UNAVAILABLE])
        self.assertEqual(self.modes, ["direct"])
        self.engine.buffer(sid, [], open_seq=0)
        self.engine.sessionReady.emit(sid)  # too late
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

    def test_zapping_during_the_wait_closes_the_old_session_and_ignores_it(self):
        first = self.start()
        second = self.start()
        self.assertEqual(self.engine.closed, [first])
        self.assertEqual(self.releases, [])  # the old upstream is not closed yet
        self.engine.buffer(first, [], open_seq=0)
        self.engine.sessionReady.emit(first)
        self.engine.sessionFailed.emit(first, "network", "The stream could not be reached.")
        self.assertEqual(self.playback.plays, [])
        self.engine.sessionClosed.emit(first)
        self.assertEqual(self.releases, [True])
        self.engine.buffer(second, [], open_seq=0)
        self.engine.sessionReady.emit(second)
        self.assertEqual(self.playback.plays, [self.stream(second, 0)])


class ReleaseTests(LiveSessionCase):
    def test_released_follows_session_closed(self):
        sid = self.ready(segments(0, 2))
        self.live.stop()
        self.assertEqual(self.playback.stops, 1)
        self.assertEqual(self.engine.closed, [sid])
        self.assertEqual(self.releases, [])
        self.engine.sessionClosed.emit("other")
        self.assertEqual(self.releases, [])
        self.engine.sessionClosed.emit(sid)
        self.assertEqual(self.releases, [True])
        self.engine.sessionClosed.emit(sid)
        self.assertEqual(self.releases, [True])
        self.assertEqual(self.live.mode, "")

    def test_direct_mode_releases_right_after_stopping_playback(self):
        self.engine.up = False
        self.start()
        stops = []
        self.live.released.connect(lambda: stops.append(self.playback.stops))
        self.live.stop()
        self.assertEqual(stops, [1])
        self.live.stop()  # idle: nothing more
        self.assertEqual(self.releases, [True])

    def test_after_a_fallback_stop_releases_at_once(self):
        sid = self.ready(segments(0, 2))
        self.engine.sessionFailed.emit(sid, "network", "The stream could not be reached.")
        self.assertEqual(self.engine.closed, [sid])
        self.live.stop()
        self.assertEqual(self.releases, [True])
        self.engine.sessionClosed.emit(sid)  # the fallback's close owes no release
        self.assertEqual(self.releases, [True])


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
        sid = self.ready(segments(3, 4, start=6.0), live_edge=15.0)
        self.live.seek_to(-50.0)
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 3))
        self.live.seek_to(500.0)
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 6))

    def test_seek_with_nothing_buffered_is_ignored(self):
        self.ready([], open_seq=0)
        self.live.seek_to(0.0)
        self.assertEqual(len(self.playback.plays), 1)

    def test_seeking_back_leaves_live_but_near_the_edge_does_not(self):
        self.ready(segments(0, 10), live_edge=20.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.live.seek_to(18.0)  # within the target of the edge: still live
        self.step(0.25)
        self.assertEqual(self.timelines[-1]["mode"], "live")
        self.live.seek_to(4.0)
        self.step(0.25)
        self.assertEqual(self.timelines[-1]["mode"], "shifted")

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

    def test_go_live_uses_the_hls_burst_as_the_target(self):
        sid = self.ready(segments(0, 10), live_edge=20.0, burst=6.0)
        self.live.go_live()
        # The 6 s burst outweighs Normal's 2.5 s: at most 14.0, so segment 7.
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 7))

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

    def test_direct_mode_passes_pause_through_and_ignores_seeks(self):
        self.engine.up = False
        self.start()
        self.live.toggle_pause()
        self.live.seek_to(5.0)
        self.live.set_speed(2.0)
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

    def test_hls_bursts_settle_without_rate_flapping(self):
        """6 s segments arrive whole every 6 s: one catch-up, then a steady rate."""
        sid = self.start()
        edge, closed = 12.0, 2
        burst = dict(length=6.0)
        self.engine.buffer(
            sid, segments(0, closed, **burst), live_edge=edge, open_seq=closed, burst=6.0
        )
        self.engine.sessionReady.emit(sid)
        self.playback.set_state(PlaybackState.PLAYING)
        position, lags = 0.0, []
        for tick in range(1, 481):  # two minutes
            advance = 0.25 * (self.playback.rates[-1] if self.playback.rates else 1.0)
            position += advance
            if tick % 24 == 0:
                edge, closed = edge + 6.0, closed + 1
                self.engine.buffer(
                    sid, segments(0, closed, **burst), live_edge=edge, open_seq=closed, burst=6.0
                )
            self.step(0.25, media=advance)
            lags.append(edge - position)
        self.assertEqual(len(self.playback.plays), 1)  # no reopen
        self.assertEqual(self.playback.rates, [1.10, 1.05, 1.0])  # once down, no flapping
        self.assertGreater(min(lags[-24:]), 2.0)  # headroom before every burst
        reported = [timeline["lag"] for timeline in self.timelines[-24:]]
        self.assertLess(max(reported) - min(reported), 0.5)  # the mean, not the sawtooth

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
        self.ready(segments(0, 5), live_edge=10.5)
        with self.assertLogs("services.live.live_session", "WARNING"):
            for _ in range(3):
                self.clock.now += 0.6
                self.playback.errorOccurred.emit("The stream could not be opened")
        self.assertEqual(self.playback.plays[-1], (URL, PlayKind.LIVE, "normal"))
        self.assertEqual(len(self.playback.plays), 4)  # ready, two reopens, direct
        self.assertEqual(self.notices, [LOST])
        self.assertEqual(self.modes, ["pipeline", "direct"])

    def test_a_synchronous_open_error_retries_on_the_tick_without_recursion(self):
        self.live = self.make(ready_timeout_ms=1000)
        self.playback.fail_on_play = True
        self.ready(segments(0, 5), live_edge=10.5)
        self.assertEqual(len(self.playback.plays), 1)
        self.step(0.5)
        self.assertEqual(len(self.playback.plays), 2)
        self.playback.fail_on_play = False
        with self.assertLogs("services.live.live_session", "WARNING"):
            self.step(0.6)
        self.assertEqual(self.playback.plays[-1], (URL, PlayKind.LIVE, "normal"))
        self.assertEqual(self.notices, [LOST])

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

    def test_a_disk_failure_after_ready_goes_live_in_memory(self):
        sid = self.ready(segments(0, 10), live_edge=20.0)
        self.live.seek_to(2.0)
        self.engine.buffer(sid, segments(10, 1, start=20.0), live_edge=23.0)
        self.engine.sessionFailed.emit(sid, "disk", "The disk buffer failed.")
        self.assertEqual(self.notices, [DISK])
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 10))
        self.assertEqual(self.modes, ["pipeline"])

    def test_the_source_giving_up_after_ready_plays_directly(self):
        for kind in ("network", "timeout", "http"):
            with self.subTest(kind=kind):
                self.notices.clear()
                self.modes.clear()
                sid = self.ready(segments(0, 3), live_edge=6.0)
                self.live.set_speed(1.5)
                self.engine.sessionFailed.emit(sid, kind, "The stream could not be reached.")
                self.assertIn(sid, self.engine.closed)
                self.assertEqual(self.playback.plays[-1], (URL, PlayKind.LIVE, "normal"))
                self.assertEqual(self.notices, [LOST])
                self.assertEqual(self.modes, ["pipeline", "direct"])
                self.engine.sessionFailed.emit(sid, "internal", "The live buffer stopped.")
                self.assertEqual(self.notices, [LOST])  # one fallback only

    def test_direct_play_errors_are_reported(self):
        sid = self.ready(segments(0, 3), live_edge=6.0)
        self.engine.sessionFailed.emit(sid, "network", "The stream could not be reached.")
        plays = len(self.playback.plays)
        self.playback.errorOccurred.emit("The stream could not be played")
        self.assertEqual(self.failures, ["The stream could not be played"])
        self.assertEqual(len(self.playback.plays), plays)


class HelperDeathTests(LiveSessionCase):
    def test_helper_death_reopens_at_live_then_a_second_death_plays_directly(self):
        sid = self.ready(segments(0, 3), live_edge=6.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.step(0.25)
        ticks = len(self.timelines)

        self.engine.up = False
        self.engine.reset(sid)
        self.engine.sessionFailed.emit(sid, "internal", "The live buffer stopped unexpectedly.")
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

        self.engine.up = False
        self.engine.reset(sid)
        self.engine.sessionFailed.emit(sid, "internal", "The live buffer stopped unexpectedly.")
        self.engine.available.emit(False)
        self.assertEqual(self.playback.plays[-1], (URL, PlayKind.LIVE, "normal"))
        self.assertEqual(self.notices, [LOST])
        self.assertEqual(self.modes, ["pipeline", "direct"])
        self.assertEqual(self.engine.closed, [sid])

        self.engine.up = True  # never again in this run
        self.start()
        self.assertEqual(len(self.engine.opened), 1)
        self.assertEqual(self.notices, [LOST, UNAVAILABLE])

    def test_an_internal_failure_while_the_helper_is_up_plays_directly(self):
        sid = self.ready(segments(0, 3), live_edge=6.0)
        self.engine.sessionFailed.emit(sid, "internal", "The live buffer stopped unexpectedly.")
        self.assertEqual(self.playback.plays[-1], (URL, PlayKind.LIVE, "normal"))
        self.assertEqual(self.notices, [LOST])

    def test_a_reopen_that_never_gets_ready_plays_directly(self):
        self.live = self.make(ready_timeout_ms=50)
        sid = self.ready(segments(0, 3), live_edge=6.0)
        self.engine.up = False
        self.engine.sessionFailed.emit(sid, "internal", "The live buffer stopped unexpectedly.")
        self.engine.up = True
        self.engine.available.emit(True)
        self.wait_until(lambda: self.notices)
        self.assertEqual(self.notices, [LOST])
        self.assertEqual(self.playback.plays[-1], (URL, PlayKind.LIVE, "normal"))

    def test_a_reopen_that_fails_plays_directly(self):
        sid = self.ready(segments(0, 3), live_edge=6.0)
        self.engine.up = False
        self.engine.sessionFailed.emit(sid, "internal", "The live buffer stopped unexpectedly.")
        self.engine.up = True
        self.engine.available.emit(True)
        self.engine.sessionFailed.emit(sid, "network", "The stream could not be reached.")
        self.assertEqual(self.notices, [LOST])
        self.assertEqual(self.playback.plays[-1], (URL, PlayKind.LIVE, "normal"))

    def test_engine_lost_during_the_wait_plays_directly_once(self):
        sid = self.start()
        self.engine.up = False
        self.engine.sessionFailed.emit(sid, "internal", "The live buffer is not available.")
        self.engine.available.emit(False)
        self.assertEqual(self.notices, [UNAVAILABLE])
        self.assertEqual(self.playback.plays, [(URL, PlayKind.LIVE, "normal")])


if __name__ == "__main__":
    unittest.main()
