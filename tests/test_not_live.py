"""2.8.10: a reconnect answered with a recording, not the channel (`NotLiveGuard`)."""

import unittest

from services.live import not_live
from services.live.not_live import NotLiveGuard
from tests import ts_fixtures as fx
from tests.test_ts_segmenter import pes_starts

FPS = 25


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def pictures(data):
    """`data` cut at each picture's PES start: [(media seconds, piece)]."""
    starts = [off for off, _ in pes_starts(data, (fx.VIDEO_PID,))]
    edges = [0, *starts[1:], len(data)]
    return [(k / FPS, data[a:b]) for k, (a, b) in enumerate(zip(edges, edges[1:]))]


class NotLiveGuardTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.program = (fx.VIDEO_PID, 0x1B)
        self.guard = NotLiveGuard(lambda: self.program, self.clock)
        self.passed = []  # (now, bytes)

    def feed(self, data, wall, *, start=0.0, ticks_to=None):
        """`data` fed picture by picture, the clock at `start` + wall(media); then ticks every
        quarter second up to `ticks_to`. What passed on, joined."""
        for media, piece in pictures(data):
            self.clock.now = start + wall(media)
            for out in self.guard.feed(piece):
                self.passed.append((self.clock.now, out))
        while ticks_to is not None and self.clock.now < ticks_to:
            self.clock.now += 0.25
            for out in self.guard.tick():
                self.passed.append((self.clock.now, out))
        return b"".join(out for _, out in self.passed)

    def first_passed(self):
        return self.passed[0][0] if self.passed else None

    def test_the_tune_passes_at_once(self):
        data = fx.make_stream(4)
        self.assertEqual(self.feed(data, lambda m: m), data)
        self.assertEqual(self.first_passed(), 0.0)

    def test_a_reconnect_at_real_time_passes_after_half_a_second(self):
        self.guard.reconnect()
        data = fx.make_stream(10)
        self.assertEqual(self.feed(data, lambda m: m, ticks_to=11.0), data)
        self.assertGreaterEqual(self.first_passed(), not_live._HOLD_MIN_S)
        self.assertLessEqual(self.first_passed(), not_live._HOLD_MIN_S + 0.2)
        self.assertFalse(self.guard.not_live)

    def test_a_backlog_lands_then_passes(self):
        # 10 s of the provider's buffer in half a second, then real time.
        self.guard.reconnect()
        data = fx.make_stream(20)
        out = self.feed(data, lambda m: m * 0.05 if m < 10 else 0.5 + (m - 10), ticks_to=21.0)
        self.assertEqual(out, data)
        self.assertGreater(self.first_passed(), 0.5)  # the burst settled first
        self.assertLessEqual(self.first_passed(), 1.5)
        self.assertFalse(self.guard.not_live)

    def test_a_recording_sent_far_faster_than_real_time_is_dropped(self):
        # 120 s of media at 250 times real time: the night's "connection limit" clip.
        self.guard.reconnect()
        self.assertEqual(self.feed(fx.make_stream(120), lambda m: m / 250, ticks_to=10.0), b"")
        self.assertTrue(self.guard.not_live)
        media, seconds = self.guard.last
        self.assertGreater(media - seconds, not_live._NOT_LIVE_S)
        self.assertLess(seconds, 1.0)
        self.assertEqual(self.guard.feed(b"\x47" + bytes(187)), [])  # the rest of it too
        self.assertEqual(self.guard.finish(), [])
        self.guard.reconnect()  # the next connection is judged afresh
        self.passed.clear()
        data = fx.make_stream(4)
        self.assertEqual(self.feed(data, lambda m: m, start=self.clock.now, ticks_to=30.0), data)
        self.assertFalse(self.guard.not_live)

    def test_a_backlog_under_the_bound_is_kept_whatever_its_speed(self):
        # 60 s of buffer at once is a lot, but under the bound: kept.
        self.guard.reconnect()
        data = fx.make_stream(70)
        out = self.feed(data, lambda m: m * 0.001 if m < 60 else 0.06 + (m - 60), ticks_to=80.0)
        self.assertEqual(out, data)
        self.assertFalse(self.guard.not_live)

    def test_without_readable_timestamps_it_passes_after_the_shortest_hold(self):
        # Nothing to measure: nothing ahead, so nothing held longer than at real time.
        self.program = (None, None)  # no program known
        self.guard.reconnect()
        data = fx.make_stream(2)
        self.assertEqual(self.feed(data, lambda m: m * 0.01, ticks_to=6.0), data)
        self.assertAlmostEqual(self.first_passed(), not_live._HOLD_MIN_S, delta=0.25)
        self.assertFalse(self.guard.not_live)

    def test_a_slow_trickle_passes_after_the_longest_hold(self):
        # Media at 2x for 4 s (never settled, never far ahead): held no longer than 4 s.
        self.guard.reconnect()
        data = fx.make_stream(10)
        self.assertEqual(self.feed(data, lambda m: m / 2, ticks_to=8.0), data)
        self.assertAlmostEqual(self.first_passed(), not_live._HOLD_MAX_S, delta=0.1)

    def test_the_end_of_the_input_passes_what_is_held(self):
        self.guard.reconnect()
        data = fx.make_stream(1)
        self.assertEqual(self.feed(data, lambda m: 0.0), b"")
        self.assertEqual(b"".join(self.guard.finish()), data)


if __name__ == "__main__":
    unittest.main()
