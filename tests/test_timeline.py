import unittest

from services.live.timeline import PTS_TICKS_PER_SECOND as HZ, Timeline, _pts_delta

WRAP = 1 << 33


def feed(tl, count, resets=(), seg=2.0, base=1_000_000, jitter=0.0):
    """Open/close ``count`` segments of ``seg`` seconds; PTS drops back at ``resets``."""
    pts = base
    wall = 100.0
    tl.on_opened(0, pts, wall, False)
    for seq in range(count):
        pts += int(seg * HZ)
        wall += seg + (jitter if seq % 2 else -jitter)
        if seq + 1 in resets:
            pts = 5_000  # clock reset: backward jump
        tl.on_closed(seq, pts, wall)
        tl.on_opened(seq + 1, pts, wall, False)


class PtsDeltaTests(unittest.TestCase):
    def test_wrap_aware(self):
        self.assertEqual(_pts_delta(WRAP - 10, 5), 15)
        self.assertEqual(_pts_delta(5, WRAP - 10), -15)
        self.assertEqual(_pts_delta(100, 100), 0)


class TimelineTests(unittest.TestCase):
    def test_contiguous_spans_from_pts(self):
        tl = Timeline()
        feed(tl, 4)
        spans = tl.spans()
        self.assertEqual(spans[0].start, 0.0)
        for a, b in zip(spans, spans[1:]):
            self.assertEqual(a.end, b.start)
        self.assertAlmostEqual(tl.end, 8.0)
        self.assertFalse(any(s.discontinuity for s in spans))
        self.assertEqual((tl.oldest_seq, tl.newest_seq), (0, 3))

    def test_pts_wrap_is_not_a_reset(self):
        tl = Timeline()
        tl.on_opened(0, WRAP - HZ, 0.0, False)
        span = tl.on_closed(0, HZ, 2.0)
        self.assertAlmostEqual(span.end, 2.0)
        self.assertFalse(span.discontinuity)

    def test_reset_falls_back_to_wall_and_stays_monotone(self):
        tl = Timeline()
        feed(tl, 3, resets=(2,))
        spans = tl.spans()
        self.assertTrue(spans[1].discontinuity)
        self.assertAlmostEqual(spans[1].end - spans[1].start, 2.0)
        self.assertTrue(all(b.start == a.end and b.end > b.start for a, b in zip(spans, spans[1:])))

    def test_discontinuity_cases(self):
        # (next_pts, wall_close) -> (duration, discontinuity)
        cases = {
            "negative": (500, 1.3, 1.3, True),
            "oversized": (1000 + 11 * HZ, 50.0, 10.0, True),
            "missing": (None, 0.0, 0.04, True),
            "equal": (1000, 1.3, 1.3, False),
            "valid": (1000 + 2 * HZ, 1.3, 2.0, False),
        }
        for name, (nxt, wall, duration, disc) in cases.items():
            with self.subTest(name):
                tl = Timeline()
                tl.on_opened(0, 1000, 0.0, False)
                span = tl.on_closed(0, nxt, wall)
                self.assertAlmostEqual(span.end - span.start, duration)
                self.assertEqual(span.discontinuity, disc)

    def test_pts_preferred_over_wall(self):
        tl = Timeline()
        tl.on_opened(0, 0, 0.0, False)
        span = tl.on_closed(0, 2 * HZ, 1.3)
        self.assertAlmostEqual(span.end, 2.0)
        self.assertFalse(span.discontinuity)

    def test_b_frame_keyframe_pts_has_no_discontinuity(self):
        # Keyframe (I) PTS is monotone even though B-frames reorder between them.
        tl = Timeline()
        pts = [900_000, 900_000 + 2 * HZ + 3600, 900_000 + 4 * HZ + 3600]  # +0.04 s B delay
        tl.on_opened(0, pts[0], 0.0, False)
        for seq in (0, 1):
            span = tl.on_closed(seq, pts[seq + 1], 2.0 * (seq + 1))
            self.assertFalse(span.discontinuity)
            tl.on_opened(seq + 1, pts[seq + 1], 2.0 * (seq + 1), False)
        self.assertAlmostEqual(tl.end, 4.04, delta=0.001)

    def test_reopen_without_close_is_closed_by_wall(self):
        tl = Timeline()
        tl.on_opened(0, 0, 0.0, False)
        with self.assertLogs("services.live.timeline", "WARNING"):
            tl.on_opened(1, 2 * HZ, 1.5, False)
        spans = tl.spans()
        self.assertEqual([(x.seq, x.end) for x in spans], [(0, 1.5)])
        self.assertTrue(spans[0].discontinuity)

    def test_close_of_unknown_seq_raises(self):
        tl = Timeline()
        with self.assertRaises(ValueError):
            tl.on_closed(0, 0, 0.0)
        tl.on_opened(1, 0, 0.0, False)
        with self.assertRaises(ValueError):
            tl.on_closed(2, 0, 0.0)

    def test_declared_discontinuity_is_kept(self):
        tl = Timeline()
        tl.on_opened(0, 0, 0.0, True)
        self.assertTrue(tl.on_closed(0, 2 * HZ, 2.0).discontinuity)

    def test_resets_at_gops_3_6_9_end_near_24(self):
        tl = Timeline()
        feed(tl, 12, resets=(3, 6, 9), jitter=0.1)
        self.assertAlmostEqual(tl.end, 24.0, delta=0.15)
        for s in tl.spans():
            self.assertTrue(0 < s.end - s.start <= 2.2)
        spans = tl.spans()
        self.assertTrue(all(spans[i].discontinuity for i in (2, 5, 8)))

    def test_eviction_keeps_start_equal_to_baseline(self):
        base, reset = Timeline(), Timeline()
        feed(base, 12)
        feed(reset, 12, resets=(3, 6, 9), jitter=0.1)
        for tl in (base, reset):
            tl.on_evicted(7)
        self.assertEqual(reset.oldest_seq, 8)
        self.assertAlmostEqual(reset.start, base.start, delta=0.08)
        self.assertAlmostEqual(reset.start, 16.0, delta=0.08)

    def test_locate_clamps(self):
        tl = Timeline()
        feed(tl, 3)
        tl.on_evicted(0)
        self.assertEqual(tl.locate(-5), (1, 0.0))
        self.assertEqual(tl.locate(3.0), (1, 1.0))
        self.assertEqual(tl.locate(99), (2, 2.0))
        self.assertIsNone(Timeline().locate(0))
        self.assertEqual(tl.locate(tl.live_edge(None, 5.0)), (2, 2.0))

    def test_seq_after(self):
        tl = Timeline()
        feed(tl, 3)
        self.assertEqual(tl.seq_after(0.0), 0)
        self.assertEqual(tl.seq_after(0.1), 1)
        self.assertEqual(tl.seq_after(4.0), 2)
        self.assertEqual(tl.seq_after(6.5), 3)  # the open segment

    def test_seq_after_full_eviction(self):
        tl = Timeline()
        feed(tl, 3)
        tl.on_closed(3, None, 200.0)
        tl.on_evicted(3)
        self.assertEqual(tl.seq_after(0.0), 4)

    def test_live_edge_never_decreases(self):
        tl = Timeline()
        tl.on_opened(0, 0, 0.0, False)
        self.assertAlmostEqual(tl.live_edge(2 * HZ, 2.0), 2.0)
        span = tl.on_closed(0, HZ, 1.0)  # shorter than the progress already reported
        self.assertAlmostEqual(span.end, 1.0)
        self.assertAlmostEqual(tl.live_edge(None, 1.0), 2.0)

    def test_live_edge_includes_open_segment(self):
        tl = Timeline()
        tl.on_opened(0, 0, 10.0, False)
        tl.on_closed(0, 2 * HZ, 12.0)
        self.assertEqual(tl.live_edge(None, 12.0), 2.0)  # nothing open
        tl.on_opened(1, 2 * HZ, 12.0, False)
        self.assertAlmostEqual(tl.live_edge(2 * HZ + HZ // 2, 12.4), 2.5)  # from PTS
        self.assertAlmostEqual(tl.live_edge(None, 13.0), 3.0)  # wall fallback
        self.assertAlmostEqual(tl.live_edge(0, 13.0), 3.0)  # backward PTS: wall
        self.assertAlmostEqual(tl.live_edge(None, 99.0), 12.0)  # clamped to forced cut


if __name__ == "__main__":
    unittest.main()
