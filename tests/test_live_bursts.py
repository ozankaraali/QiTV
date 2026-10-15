"""Raw MPEG-TS that arrives in bursts (2.0.1, L1).

Some providers' restreams send ~10 s of media at once every ~10 s. The helper measures that
(`BurstMeter`) and reports it as the session's `burst_s`, so LiveSession handles it as it
handles HLS segments: the target, the mean lag, the stall bound and Go live.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import multiprocessing
from pathlib import Path
import tempfile
import time
import unittest

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from services.live import worker
from services.live.engine import LiveEngine
from services.live.live_session import LiveSession
from services.live.stay_live import TARGETS
from services.playback import PlaybackState
from services.thread_cleanup import has_pending_threads
from services.vlc_profile import PlayKind
from tests.fixtures.live_source import LiveSource

app = QApplication.instance() or QApplication([])


def steady(start, seconds, *, media=0.0, step=0.1):
    """Arrivals every `step` seconds from `start`, each bringing `step` seconds of media."""
    return [(start + i * step, media + (i + 1) * step) for i in range(round(seconds / step))]


def bursts(start, size, every, count, *, media=0.0, spread=0.2, chunks=5):
    """`count` bursts of `size` media seconds, one every `every` seconds from `start`, each
    arriving in `chunks` chunks over `spread` seconds."""
    series = []
    for k in range(count):
        for c in range(chunks):
            wall = start + k * every + spread * c / (chunks - 1)
            series.append((wall, media + k * size + size * (c + 1) / chunks))
    return series


def measured(series):
    """`burst_s` after each arrival of `series` ((wall, media) pairs) on a fresh meter."""
    meter = worker.BurstMeter()
    readings = []
    for wall, media in series:
        meter.add(wall, media)
        readings.append(meter.burst_s)
    return readings


class BurstMeterTests(unittest.TestCase):
    def test_a_continuous_source_is_steady(self):
        self.assertEqual(set(measured(steady(0.0, 120.0))), {0.0})

    def test_ten_second_bursts_every_ten_seconds(self):
        readings = measured(bursts(0.0, 10.0, 10.0, 6))
        # From the first clear burst on: the second one's first chunk shows the gap.
        self.assertEqual(set(readings[:5]), {0.0})
        self.assertEqual(set(readings[5:]), {10.0})

    def test_two_second_bursts_every_two_seconds(self):
        readings = measured(bursts(0.0, 2.0, 2.0, 10))
        self.assertEqual(set(readings[:5]), {0.0})
        self.assertEqual(set(readings[5:]), {2.0})

    def test_a_burst_under_a_second_and_a_half_waits_for_a_second_alike(self):
        readings = [round(reading, 6) for reading in measured(bursts(0.0, 1.2, 1.2, 6, spread=0.1))]
        self.assertEqual(set(readings[:10]), {0.0})
        self.assertEqual(set(readings[10:]), {1.2})

    def test_a_connect_backlog_counts_only_up_to_the_next_burst(self):
        # The owner's provider: 12 s at connect, then 10 s every 10 s. Only what plays
        # until the next burst lands counts; the rest was a backlog.
        series = bursts(0.0, 12.0, 10.0, 1)
        series += bursts(10.0, 10.0, 10.0, 5, media=12.0)
        readings = measured(series)
        self.assertEqual(set(readings[:5]), {0.0})
        self.assertEqual(set(readings[5:]), {10.0})

    def test_a_large_connect_backlog_is_not_the_estimate(self):
        series = bursts(0.0, 30.0, 10.0, 1)
        series += bursts(10.0, 10.0, 10.0, 5, media=30.0)
        self.assertEqual(set(measured(series)[5:]), {10.0})

    def test_a_short_connect_burst_takes_its_gap_as_the_period(self):
        # Re-measured on the owner's channel: 3.84 s at connect, the first 10 s burst 8.5 s
        # later, then 10 s every 10 s. The gap says more than the connect burst's media.
        series = bursts(0.0, 3.84, 0.0, 1)
        series += bursts(8.5, 10.0, 10.0, 4, media=3.84)
        readings = measured(series)
        self.assertEqual(set(readings[:5]), {0.0})
        self.assertEqual(set(readings[5:15]), {8.5})  # one 10 s burst is not two alike
        self.assertEqual(set(readings[15:]), {10.0})

    def test_the_connect_period_counts_for_at_most_three_times_its_media(self):
        # A steady source that paused 20 s after its first 2 s, then sends in real time.
        series = bursts(0.0, 2.0, 0.0, 1) + steady(22.0, 30.0, media=2.0)
        readings = measured(series)
        self.assertEqual(readings[5], 6.0)
        self.assertEqual(set(readings[5 + round(18.0 / 0.1) + 1 :]), {0.0})  # 3 x 6 s steady

    def test_the_connect_period_counts_for_at_most_twelve_seconds(self):
        series = bursts(0.0, 6.0, 0.0, 1) + bursts(20.0, 6.0, 6.0, 2, media=6.0)
        self.assertEqual(measured(series)[5], 12.0)

    def test_only_the_connect_burst_takes_its_gap(self):
        # Bursty, then steady (back to 0), then bursty again: the media, not the gap.
        series = bursts(0.0, 4.0, 4.0, 3) + steady(12.0, 13.0, media=12.0)
        series += bursts(28.0, 2.0, 0.0, 1, media=25.0) + bursts(35.0, 2.0, 2.0, 1, media=27.0)
        readings = measured(series)
        self.assertEqual(readings[15 + 130 - 1], 0.0)  # steady again
        self.assertEqual(readings[-5], 2.0)

    def test_the_estimate_is_the_largest_of_the_last_three_alike_bursts(self):
        # 9, 10, 11 and 9 s bursts, each landing as the one before has played.
        series, media = [], 0.0
        for wall, size in ((0.0, 9.0), (10.0, 10.0), (21.0, 11.0), (33.0, 9.0), (43.0, 9.0)):
            series += bursts(wall, size, 0.0, 1, media=media)
            media += size
        readings = measured(series)
        self.assertEqual(readings[5], 10.0)  # the connect burst counts for its period
        self.assertEqual(readings[10], 10.0)  # 9 and 10 alike: the larger
        self.assertEqual(readings[15], 11.0)
        self.assertEqual(readings[20], 11.0)  # 10, 11, 9

    def test_bursts_unlike_the_first_correct_it_once_two_are_alike(self):
        # 10 s at connect, a 10 s wait while the restream starts, then 4 s every 4 s.
        series = bursts(0.0, 10.0, 10.0, 1)
        series += bursts(10.0, 4.0, 4.0, 5, media=10.0)
        readings = measured(series)
        self.assertEqual(readings[5], 10.0)  # provisional
        self.assertEqual(readings[10], 10.0)  # one 4 s burst: not yet
        self.assertEqual(set(readings[15:]), {4.0})  # two alike: corrected

    def test_one_hiccup_does_not_make_a_continuous_source_bursty(self):
        # 30 s steady, 3 s without a byte, then the backlog at once and steady again.
        series = steady(0.0, 30.0)
        series += [(33.0, 33.0)]
        series += steady(33.1, 60.0, media=33.0)
        self.assertEqual(set(measured(series)), {0.0})

    def test_two_hiccups_do_not_make_a_continuous_source_bursty(self):
        series = steady(0.0, 30.0)
        series += steady(33.0, 30.0, media=33.0)
        series += steady(66.0, 30.0, media=66.0)
        self.assertEqual(set(measured(series)), {0.0})

    def test_a_steady_source_with_a_connect_backlog_and_a_hiccup_stays_steady(self):
        series = [(0.0, 30.0)]  # 30 s at connect, then real time
        series += steady(0.1, 20.0, media=30.0)
        series += steady(23.1, 30.0, media=50.0)
        self.assertEqual(set(measured(series)), {0.0})

    def test_a_hiccup_right_after_connect_is_steady_again_soon(self):
        # 2 s at connect, 3 s without a byte, then steady: a clear burst at first, counted
        # for its period (3 s, within three times its media).
        series = bursts(0.0, 2.0, 0.0, 1) + steady(3.0, 20.0, media=2.0)
        readings = measured(series)
        self.assertEqual(readings[5], 3.0)
        steady_from = 5 + round(9.0 / 0.1) + 1  # three estimates without a gap: 9 s
        self.assertEqual(readings[steady_from - 12], 3.0)
        self.assertEqual(set(readings[steady_from:]), {0.0})

    def test_a_bursty_source_that_turns_continuous_is_steady_again(self):
        bursty = bursts(0.0, 4.0, 4.0, 5)
        readings = measured(bursty + steady(20.0, 30.0, media=20.0))
        self.assertEqual(readings[len(bursty) - 1], 4.0)
        continuous = readings[len(bursty) :]  # one arrival every 0.1 s from 20.0
        # Steady again once nothing paused for three bursts' worth of seconds (12 s).
        self.assertEqual(continuous[115], 4.0)
        self.assertEqual(set(continuous[121:]), {0.0})


class ModelPlayback(QObject):
    """VLC playing the loopback stream in real time: at the rate set while the buffer holds
    media past the position; at the live edge its clock stands still and, like libVLC 3, it
    keeps reporting Playing. Opening takes `open_s`."""

    stateChanged = Signal(str)
    positionChanged = Signal(int, int)
    errorOccurred = Signal(str)
    ended = Signal()
    inputEnded = Signal()

    STEP_MS = 50

    def __init__(self, open_s=0.3):
        super().__init__()
        self.live = None
        self.plays = []
        self.current = PlaybackState.IDLE
        self.rate = 1.0
        self.ms = 0.0
        self.starved_s = 0.0
        self._open_s = open_s
        self._opened = 0.0
        self._last = time.monotonic()
        self._timer = QTimer(self, interval=self.STEP_MS)
        self._timer.timeout.connect(self._step)
        self._timer.start()

    def _set(self, state):
        if state is not self.current:
            self.current = state
            self.stateChanged.emit(state.value)

    def _step(self):
        now = time.monotonic()
        dt, self._last = now - self._last, now
        if self.current is PlaybackState.OPENING and now - self._opened >= self._open_s:
            self._set(PlaybackState.PLAYING)
            return
        if self.current is not PlaybackState.PLAYING or self.live is None:
            return
        _, edge = self.live.buffered_range()
        ahead = max(0.0, edge - self.live.position())
        advance = min(self.rate * dt, ahead)
        if advance < self.rate * dt - 1e-6:
            self.starved_s += dt
        self.ms += advance * 1000.0

    def play(self, url, *, kind, latency="normal", user_agent=None, start_ms=None):
        self.plays.append((url, kind))
        self.ms, self._opened = 0.0, time.monotonic()
        self._set(PlaybackState.OPENING)

    def stop(self):
        self._set(PlaybackState.IDLE)

    def state(self):
        return self.current

    def time_ms(self):
        return round(self.ms)

    def holds_input(self):
        return False

    def set_paused(self, paused):
        if paused and self.current is PlaybackState.PLAYING:
            self._set(PlaybackState.PAUSED)
        elif not paused and self.current is PlaybackState.PAUSED:
            self._set(PlaybackState.PLAYING)

    def toggle_pause(self):
        pass

    def set_rate(self, rate):
        self.rate = rate


def wait_until(predicate, timeout):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            return False
        QTest.qWait(20)
    return True


class BurstyRawTsTests(unittest.TestCase):
    """End to end in real time: the fixture channel in 3 s bursts, the helper, LiveSession."""

    BURST = 3.0

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="qitv-live-bursts-test-")
        self.addCleanup(temporary.cleanup)
        self.source = LiveSource(burst_s=self.BURST)
        self.addCleanup(self.source.close)
        self.engine = LiveEngine(str(Path(temporary.name) / "timeshift"))
        self.addCleanup(self.retire)
        self.engine.start()
        self.assertTrue(wait_until(self.engine.is_available, 60), "the helper never came up")
        self.playback = ModelPlayback()
        self.live = LiveSession(self.engine, self.playback, tick_ms=250)
        self.playback.live = self.live
        self.timelines, self.notices, self.failures = [], [], []
        self.live.timelineChanged.connect(self.timelines.append)
        self.live.notice.connect(self.notices.append)
        self.live.failed.connect(self.failures.append)

    def retire(self):
        released = []
        self.live.released.connect(lambda: released.append(True))
        self.live.stop()
        wait_until(lambda: released, 5)
        self.engine.shutdown()
        self.assertTrue(wait_until(lambda: not has_pending_threads(), 5))
        self.assertEqual(multiprocessing.active_children(), [])

    def test_measured_bursts_raise_the_target_without_stalls_or_catch_up(self):
        latency = "low"  # a 1 s target: the 3 s bursts raise it
        self.live.start(
            self.source.url,
            headers={},
            verify_ssl=True,
            latency=latency,
            user_agent=None,
            disk=False,
            max_bytes=64 * 1024 * 1024,
        )
        known = wait_until(lambda: any(t["target"] > TARGETS[latency] for t in self.timelines), 25)
        self.assertTrue(known, "the bursts were never measured")
        self.assertEqual(self.live.mode, "pipeline")
        self.assertEqual(self.playback.plays[0][1], PlayKind.LIVE_PIPELINE)
        first = next(i for i, t in enumerate(self.timelines) if t["target"] > TARGETS[latency])
        QTest.qWait(round(2.5 * self.BURST * 1000))  # more bursts land meanwhile
        after = self.timelines[first:]
        self.assertGreater(len(after), 20)
        for state in after:
            # Shown as the source's own pace, never as "Reconnecting…" between bursts.
            self.assertFalse(state["stalled"], state)
            self.assertAlmostEqual(state["target"], self.BURST, delta=0.3)
            # No catch-up into the next gap: the buffer is a burst, not a lag to remove.
            self.assertLessEqual(state["rate"], 1.0, state)
            self.assertEqual(state["mode"], "live")
        self.assertEqual(self.live.mode, "pipeline")
        self.assertEqual(len(self.playback.plays), 1)  # no reopen
        self.assertEqual((self.notices, self.failures), ([], []))


if __name__ == "__main__":
    unittest.main()
