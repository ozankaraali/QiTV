import unittest

import ts_fixtures as fx

from services.live.timeline import (
    PTS_TICKS_PER_SECOND as HZ,
    START_KEY,
    START_NONE,
    START_OPEN,
    Span,
    Timeline,
    _pts_delta,
)
from services.live.ts import Closed, Opened, Segmenter

WRAP = 1 << 33
NTSC = 30000 / 1001


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
            "oversized": (1000 + 12 * HZ, 50.0, 11.0, True),
            "forced cut plus a frame": (1000 + 10 * HZ + 3003, 1.3, 10 + 3003 / HZ, False),
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
        self.assertEqual(spans[0].start_quality, START_NONE)  # its start was never reported

    def test_spans_carry_their_segments_start_quality(self):
        tl = Timeline()
        for seq, quality in enumerate((START_OPEN, START_NONE, START_KEY)):
            tl.on_opened(seq, seq * 2 * HZ, seq * 2.0, False)
            tl.on_closed(seq, (seq + 1) * 2 * HZ, (seq + 1) * 2.0, quality)
        tl.on_opened(3, 6 * HZ, 6.0, False)
        tl.on_closed(3, 8 * HZ, 8.0)  # a caller that knows no quality: the old behaviour
        self.assertEqual(
            [s.start_quality for s in tl.spans()], [START_OPEN, START_NONE, START_KEY, START_KEY]
        )
        # Spans made without one (older readers, tests) start on a keyframe, as before.
        self.assertEqual(Span(0, 0.0, 2.0, False), Span(0, 0.0, 2.0, False, START_KEY))

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
        self.assertAlmostEqual(tl.live_edge(None, 99.0), 13.0)  # clamped: forced cut + 1 s


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def segmented(parts, chunk=7 * 188):
    """Feed the real Segmenter into a Timeline as the live worker does.

    ``parts`` are ``(data, seconds of media, real_time, gap before, marker before)``: a real-time
    part arrives at its own pace, otherwise all at once. Returns the timeline and how far the
    live edge ever ran ahead of the media fed so far.
    """
    clock = Clock()
    seg = Segmenter(clock=clock)
    tl = Timeline()
    fed = lead = 0.0

    def apply(events):
        for event in events:
            if isinstance(event, Opened):
                tl.on_opened(event.seq, event.first_pts, event.wall, event.discontinuity)
            elif isinstance(event, Closed):
                tl.on_closed(event.seq, event.next_first_pts, event.wall, event.start_quality)

    for data, seconds, real_time, gap, marker in parts:
        clock.now += gap
        if marker:
            seg.discontinuity()
        for start in range(0, len(data), chunk):
            piece = data[start : start + chunk]
            share = seconds * len(piece) / len(data)
            if real_time:
                clock.now += share
            fed += share
            apply(seg.feed(piece))
            lead = max(lead, tl.live_edge(seg.progress_pts, seg.wall) - fed)
    apply(seg.finish())
    return tl, lead


class SegmentedTimelineTests(unittest.TestCase):
    """The timeline must end within 0.2 s of the media it was fed."""

    def assert_sized(self, parts):
        tl, lead = segmented(parts)
        media = sum(part[1] for part in parts)
        self.assertAlmostEqual(tl.end, media, delta=0.2)
        self.assertLessEqual(lead, 0.2, "the live edge ran ahead of the media")
        return tl

    def test_a_stall_then_a_reconnect_with_moved_timestamps(self):
        # Raw TS at real time; the provider stalls 60 s, and its clock moved on 70 s.
        before = fx.make_stream(5.5)
        after = fx.make_stream(6.0, pts_start=900000 + 70 * HZ)
        self.assert_sized([(before, 5.5, True, 0.0, False), (after, 6.0, True, 60.0, True)])
        # The same with an encoder restart (PTS back to the start) after the first backoff.
        restart = fx.make_stream(6.0)
        self.assert_sized([(before, 5.5, True, 0.0, False), (restart, 6.0, True, 0.8, True)])

    def test_hls_ad_breaks_with_timestamp_jumps(self):
        # 6 s segments; the 4th (an ad) and the 5th each jump their PTS.
        shows = [fx.make_stream(6.0, pts_start=900000 + 6 * HZ * k) for k in range(3)]
        ad = fx.make_stream(6.0, pts_start=5_000_000)
        back = fx.make_stream(6.0, pts_start=900000 + 24 * HZ)
        for name, gap in (("at the live edge", 6.0), ("downloaded back to back", 0.2)):
            with self.subTest(name):
                parts = [
                    (data, 6.0, False, gap if k else 0.0, False) for k, data in enumerate(shows)
                ]
                parts += [(ad, 6.0, False, gap, True), (back, 6.0, False, gap, True)]
                tl = self.assert_sized(parts)
                discontinuities = [span for span in tl.spans() if span.discontinuity]
                self.assertEqual(len(discontinuities), 2)

    def test_ntsc_forced_cuts_keep_their_pts_duration(self):
        # 29.97 fps with 12 s GOPs: each forced cut lands on the first frame past 10 s.
        data = fx.make_stream(30.0, fps=NTSC, gop_s=12.0)
        media = round(30.0 * NTSC) / NTSC
        for name, real_time in (("real time", True), ("burst", False)):
            with self.subTest(name):
                tl = self.assert_sized([(data, media, real_time, 0.0, False)])
                spans = tl.spans()
                self.assertAlmostEqual(spans[0].end - spans[0].start, 300 * 3003 / HZ)
                self.assertFalse(any(span.discontinuity for span in spans))
                # IDR starts at 0, 12 and 24 s; forced cuts without a keyframe at 10 and 22 s.
                self.assertEqual(
                    [span.start_quality for span in spans],
                    [START_KEY, START_NONE, START_KEY, START_NONE, START_KEY],
                )


class PartTests(unittest.TestCase):
    """A kept buffer's runs (2.6): each begins with the time its first segment opened."""

    @staticmethod
    def run_of(tl, first, count, pts, wall, *, discontinuity=False):
        tl.on_opened(first, pts, wall, discontinuity)
        for seq in range(first, first + count):
            pts += 2 * HZ
            wall += 2.0
            tl.on_closed(seq, pts, wall)
            if seq + 1 < first + count:
                tl.on_opened(seq + 1, pts, wall, False)
        return pts, wall

    def test_no_part_until_one_begins(self):
        tl = Timeline()
        self.run_of(tl, 0, 2, 0, 0.0)
        self.assertEqual(tl.parts(), [])

    def test_begin_part_marks_the_next_opened_segment(self):
        tl = Timeline()
        tl.begin_part(1_000.0)
        pts, wall = self.run_of(tl, 0, 1, 0, 0.0)
        tl.begin_part(9_000.0)
        self.run_of(tl, 1, 1, 5 * HZ, wall + 0.1, discontinuity=True)
        parts = tl.parts()
        self.assertEqual([(p.first_seq, p.wall) for p in parts], [(0, 1_000.0), (1, 9_000.0)])
        self.assertEqual(parts[1].start, tl.spans()[1].start)
        self.assertEqual(parts[0].start, 0.0)

    def test_eviction_moves_and_drops_parts(self):
        tl = Timeline()
        tl.begin_part(1_000.0)
        pts, wall = self.run_of(tl, 0, 3, 0, 0.0)
        tl.begin_part(9_000.0)
        self.run_of(tl, 3, 1, pts, wall, discontinuity=True)
        tl.on_evicted(0)
        first = tl.parts()[0]
        self.assertEqual((first.first_seq, first.start), (1, tl.spans()[0].start))
        self.assertAlmostEqual(first.wall, 1_000.0 + first.start)  # the same moment as before
        tl.on_evicted(2)
        self.assertEqual([(p.first_seq, p.wall) for p in tl.parts()], [(3, 9_000.0)])


if __name__ == "__main__":
    unittest.main()
