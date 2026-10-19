"""Tests for ReconnectTrimmer (services/live/ts.py): a reconnect whose new connection re-sends
the last seconds in the old timebase, on synthetic streams (`ts_fixtures.make_stream`: a PCR on
each video PES start, an IDR picture every second, sound in between).

The old connection is the stream up to a picture; the new one starts at a keyframe with fresh
tables and every counter from 0, as a provider's new connection does.
"""

from __future__ import annotations

import unittest

from services.live import ts
from services.live.ts import PACKET, ReconnectTrimmer, pts_delta
from tests.ts_fixtures import AUDIO_FRAME_TICKS, AUDIO_PID, CLOCK, VIDEO_PID, make_stream, pat, pmt

FPS = 25
FRAME = CLOCK // FPS
START = 900000
WRAP = 1 << 33


def packets(data: bytes) -> list[bytes]:
    return [data[at : at + PACKET] for at in range(0, len(data) - PACKET + 1, PACKET)]


def pid_of(p: bytes) -> int:
    return ((p[1] & 0x1F) << 8) | p[2]


def times(data: bytes, pid: int) -> list[int]:
    """The PTS of each PES start on `pid`, in order."""
    out = []
    for p in packets(data):
        if pid_of(p) == pid and p[1] & 0x40:
            pts = ts._pes_times(p, 0)[0]
            if pts is not None:
                out.append(pts)
    return out


def cc_breaks(data: bytes) -> int:
    breaks, last = 0, {}
    for p in packets(data):
        pid, b3 = pid_of(p), p[3]
        if not b3 & 0x10:
            continue
        if pid in last and (b3 & 0x0F) != (last[pid] + 1) & 0x0F:
            breaks += 1
        last[pid] = b3 & 0x0F
    return breaks


def frame_at(data: bytes, index: int, start: int = START) -> int:
    """The packet index where video frame `index` starts."""
    want = (start + index * FRAME) % WRAP
    return next(
        k
        for k, p in enumerate(packets(data))
        if pid_of(p) == VIDEO_PID and p[1] & 0x40 and ts._pes_times(p, 0)[0] == want
    )


def new_connection(stream: bytes, first: int) -> bytes:
    """`stream` from packet `first` on as a new connection sends it: tables first, every
    counter from 0."""
    counters: dict[int, int] = {}
    out = [pat(cc=0), pmt(cc=0)]
    counters[0] = counters[0x1000] = 0
    for p in packets(stream)[first:]:
        pid = pid_of(p)
        if p[3] & 0x10:
            cc = counters[pid] = (counters[pid] + 1) & 0x0F if pid in counters else 0
            p = p[:3] + bytes([(p[3] & 0xF0) | cc]) + p[4:]
        out.append(p)
    return b"".join(out)


def run(*connections: bytes, chunk: int | None = None) -> tuple[bytes, ReconnectTrimmer]:
    trimmer = ReconnectTrimmer(program=lambda: (VIDEO_PID, 0x1B))
    out = []
    for number, data in enumerate(connections):
        if number:
            out.append(trimmer.reconnect())
        step = chunk or len(data)
        for at in range(0, len(data), step):
            out.append(trimmer.feed(data[at : at + step]))
    out.append(trimmer.finish())
    return b"".join(out), trimmer


class ReconnectTrimmerTests(unittest.TestCase):
    def assert_once_each(self, out: bytes, frames: list[int], start: int = START) -> None:
        """The pictures `frames` once each in order; the sound once each, a frame apart; the
        counters run on."""
        self.assertEqual(times(out, VIDEO_PID), [(start + k * FRAME) % WRAP for k in frames])
        sound = times(out, AUDIO_PID)
        self.assertEqual({pts_delta(a, b) for a, b in zip(sound, sound[1:])}, {AUDIO_FRAME_TICKS})
        self.assertEqual(cc_breaks(out), 0)

    def test_a_reconnect_that_resends_three_seconds_leaves_each_frame_once(self):
        # The old connection ends right before the keyframe at 6 s; the new one starts at the
        # keyframe at 3 s: its first 3 s go, the rest follows on without a gap.
        stream = make_stream(10)
        old = b"".join(packets(stream)[: frame_at(stream, 150)])
        new = new_connection(stream, frame_at(stream, 75))
        for chunk in (None, 1000, 187):
            with self.subTest(chunk=chunk):
                out, trimmer = run(old, new, chunk=chunk)
                self.assert_once_each(out, list(range(250)))
                (rejoin,) = trimmer.rejoins
                self.assertAlmostEqual(rejoin.step_s, -(150 - 1 - 75) / FPS)
                self.assertEqual(len(out), len(old) + len(new) - rejoin.dropped_bytes)

    def test_an_old_connection_that_ended_inside_a_gop_goes_on_at_the_next_keyframe(self):
        # It ended in the middle of frame 160: nothing is shown twice; the pictures up to the
        # next keyframe (175) depend on ones the new connection's drop took, so they go too.
        stream = make_stream(10)
        cut = frame_at(stream, 160) + 2
        old = b"".join(packets(stream)[:cut])
        out, trimmer = run(old, new_connection(stream, frame_at(stream, 125)))
        frames = times(out, VIDEO_PID)
        self.assertEqual(len(frames), len(set(frames)))
        self.assertEqual(frames, [START + k * FRAME for k in (*range(161), *range(175, 250))])
        self.assertEqual(cc_breaks(out), 0)

    def test_a_reconnect_that_resumes_after_the_old_connection_is_untouched(self):
        stream = make_stream(10)
        cut = frame_at(stream, 150)
        old, new = b"".join(packets(stream)[:cut]), b"".join(packets(stream)[cut:])
        out, trimmer = run(old, new)
        self.assertEqual(out, old + new)
        self.assertEqual(list(trimmer.rejoins), [])

    def test_a_reconnect_into_another_timebase_is_untouched(self):
        # A reset (the provider's clock started over): RepeatTrimmer's and the segmenter's.
        # One lands 4 s back, on a keyframe's PTS the old connection passed: other pictures.
        stream = make_stream(10)
        old = b"".join(packets(stream)[: frame_at(stream, 150)])
        for start in (0, START + 2 * CLOCK, START + 120 * CLOCK):
            with self.subTest(start=start):
                new = b"".join(
                    p[:-1] + b"\x99" if pid_of(p) == VIDEO_PID and p[1] & 0x40 else p
                    for p in packets(make_stream(5, pts_start=start))
                )
                out, trimmer = run(old, new)
                self.assertEqual(out, old + new)
                self.assertEqual(list(trimmer.rejoins), [])

    def test_the_overlap_is_found_across_the_33_bit_wrap(self):
        start = WRAP - 5 * CLOCK  # the clock wraps 5 s in, between the two keyframes
        stream = make_stream(10, pts_start=start)
        old = b"".join(packets(stream)[: frame_at(stream, 150, start)])
        out, _ = run(old, new_connection(stream, frame_at(stream, 75, start)))
        self.assert_once_each(out, list(range(250)), start)

    def test_nothing_is_touched_before_a_reconnect(self):
        stream = make_stream(6)
        out, trimmer = run(stream, chunk=1000)
        self.assertEqual(out, stream)
        self.assertEqual(list(trimmer.rejoins), [])

    def test_a_new_connection_that_dies_within_the_overlap_is_dropped_whole(self):
        # It re-sends 2 s and stalls; the next one re-sends 3 s from the same point: each
        # frame once all the same.
        stream = make_stream(10)
        old = b"".join(packets(stream)[: frame_at(stream, 150)])
        short = new_connection(stream, frame_at(stream, 100))
        short = b"".join(packets(short)[: len(packets(short)) // 5])
        out, trimmer = run(old, short, new_connection(stream, frame_at(stream, 75)))
        self.assert_once_each(out, list(range(250)))
        self.assertEqual(len(trimmer.rejoins), 2)


if __name__ == "__main__":
    unittest.main()
