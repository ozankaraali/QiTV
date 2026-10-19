"""Tests for RepeatTrimmer (services/live/ts.py): a restart inside one connection that sends
media twice, on synthetic streams with arrival times.

Each picture is unique to its frame index, so a repeat is one on purpose; a run after the first
starts with its first PCR flagged, as the provider's restarted muxer does.
"""

from __future__ import annotations

import unittest
from unittest import mock

from services.live import ts
from services.live.ts import PACKET, RepeatTrimmer
from tests.ts_fixtures import CLOCK, PMT_PID, VIDEO_PID, pat, pes_packets, pmt

FPS = 25
TICKS = CLOCK // FPS
BACKLOG = 5.0  # media the source sends at connect
FIRST = 0.1  # when the first byte arrives


def picture(index: int, key: bool) -> bytes:
    """An H.264 access unit only frame `index` has: IDR or P."""
    tag = bytes(0x80 | ((index >> (7 * k)) & 0x7F) for k in range(3))  # never a start code
    body = b"\x00\x00\x00\x01\x09\xf0"
    if key:
        body += b"\x00\x00\x00\x01\x67" + b"\x55" * 15 + b"\x00\x00\x00\x01\x65\x55"
    else:
        body += b"\x00\x00\x00\x01\x41\x55"  # slice header 0x55: a P slice
    return body + tag + b"\x55" * 300


def stream(runs) -> list[tuple[float, bytes]]:
    """(arrival, packet) of `runs`: (frame indexes, first PTS, arrival, GOP in frames), where
    arrival maps a frame's media position in its run to the second it arrives."""
    counters = {0: 0, PMT_PID: 0, VIDEO_PID: 0}
    timed: list[tuple[float, bytes]] = []
    for number, (indexes, first_pts, arrival, gop) in enumerate(runs):
        for k, index in enumerate(indexes):
            at = arrival(k / FPS)
            if k % FPS == 0:
                timed.append((at, pat(cc=counters[0])))
                timed.append((at, pmt(audio_pids=(), cc=counters[PMT_PID])))
                counters[0] = (counters[0] + 1) & 0x0F
                counters[PMT_PID] = (counters[PMT_PID] + 1) & 0x0F
            pts = first_pts + k * TICKS
            key = k % gop == 0
            packets, counters[VIDEO_PID] = pes_packets(
                VIDEO_PID, 0xE0, picture(index, key), pts, rai=key,
                cc_start=counters[VIDEO_PID], pcr=pts,
            )  # fmt: skip
            if number and not k:
                flagged = bytearray(packets[0])
                flagged[5] |= 0x80  # discontinuity_indicator
                packets[0] = bytes(flagged)
            timed.extend((at, packet) for packet in packets)
    return timed


def backlogged(media: float) -> float:
    return FIRST + max(0.0, media - BACKLOG)


def after(start: float, burst: float, rate: float = 1.0):
    """A run arriving from `start`: `burst` seconds of media at once, then `rate` times real
    time."""
    return lambda media: start + max(0.0, media - burst) / rate


class Run:
    """Feed a timed stream to a trimmer read by read, at each read's arrival time."""

    def __init__(self, timed, *, reconnect_before: int | None = None):
        self.now = 0.0
        self.trimmer = RepeatTrimmer(program=lambda: (VIDEO_PID, 0x1B), clock=lambda: self.now)
        self.input = b"".join(packet for _, packet in timed)
        self.pieces: list[list[bytes]] = []
        k = 0
        while k < len(timed):
            at = timed[k][0]
            chunk = []
            while k < len(timed) and timed[k][0] == at:
                if k == reconnect_before and chunk:
                    break
                if k == reconnect_before:
                    self.pieces.append(self.trimmer.discontinuity())
                chunk.append(timed[k][1])
                k += 1
            self.now = at
            self.pieces.append(self.trimmer.feed(b"".join(chunk)))
        self.pieces.append(self.trimmer.finish())

    @property
    def output(self) -> bytes:
        return b"".join(b"".join(pieces) for pieces in self.pieces)

    @property
    def drops(self) -> int:
        """Where a run's start was dropped: between two pieces of one call."""
        return sum(len(pieces) - 1 for pieces in self.pieces)


def packets(data: bytes) -> list[bytes]:
    return [data[at : at + PACKET] for at in range(0, len(data) - PACKET + 1, PACKET)]


def video_pts(data: bytes) -> list[int]:
    """The PTS of each video PES start, in order."""
    out = []
    for p in packets(data):
        if ((p[1] & 0x1F) << 8 | p[2]) == VIDEO_PID and p[1] & 0x40:
            start = 4 + (1 + p[4] if p[3] & 0x20 else 0)
            out.append(ts._pes_start(p)[1] if p[start : start + 3] == b"\x00\x00\x01" else None)
    return out


def cc_breaks(data: bytes) -> int:
    """Counter breaks no discontinuity_indicator announces."""
    breaks, last = 0, {}
    for p in packets(data):
        pid, b3 = (p[1] & 0x1F) << 8 | p[2], p[3]
        if not b3 & 0x10:
            continue
        flagged = b3 & 0x20 and p[4] and p[5] & 0x80
        if pid in last and (b3 & 0x0F) not in (last[pid], (last[pid] + 1) & 0x0F) and not flagged:
            breaks += 1
        last[pid] = b3 & 0x0F
    return breaks


def repeat_runs(repeat: float, silence: float, *, gop: int = FPS, new: float = 10.0):
    """Run A, 20 s from connect; `silence`; run B restarts its clock at 0 and first sends A's
    last `repeat` seconds again (the same pictures), the silence and the repeat at once, then
    new frames at real time."""
    resume = backlogged(20.0) + silence
    first = int((20 - repeat) * FPS)
    again = [*range(first, 20 * FPS), *range(20 * FPS, int((20 + new) * FPS))]
    return [
        (range(20 * FPS), 900000, backlogged, gop),
        (again, 0, after(resume, repeat + silence), gop),
    ]


class RepeatTrimmerTests(unittest.TestCase):
    def test_a_repeat_is_dropped_up_to_the_last_keyframe_in_reach(self):
        run = Run(stream(repeat_runs(5.0, 1.0)))
        (trim,) = run.trimmer.trims
        self.assertEqual(trim.reason, "dropped")
        self.assertAlmostEqual(trim.excess_s, 5.0 - ts._TRIM_MARGIN_S, delta=0.1)
        self.assertEqual(trim.dropped_s, 4.0)  # keyframes every second: 4 s is the last in R
        self.assertAlmostEqual(trim.released_at - trim.held_at, 1.0, delta=0.1)
        self.assertEqual(run.drops, 1)
        # Run A whole, then run B from its keyframe at 4 s on: no picture of B's first 4 s.
        pts = video_pts(run.output)
        self.assertEqual(pts[: 20 * FPS], [900000 + k * TICKS for k in range(20 * FPS)])
        self.assertEqual(pts[20 * FPS], 4 * CLOCK)
        self.assertEqual(len(pts), 20 * FPS + 11 * FPS)
        # The counters run on, and the kept run's first PCR carries the flag.
        self.assertEqual(cc_breaks(run.output), 0)
        kept = next(pieces[1] for pieces in run.pieces if len(pieces) > 1)
        first_pcr = next(p for p in packets(kept) if p[3] & 0x20 and p[4] and p[5] & 0x10)
        self.assertTrue(first_pcr[5] & 0x80)
        self.assertEqual(trim.dropped_bytes, len(run.input) - len(run.output))

    def test_a_burst_that_only_makes_up_its_silence_passes_untouched(self):
        silence = 3.0
        resume = backlogged(20.0) + silence
        runs = [
            (range(20 * FPS), 900000, backlogged, FPS),
            (range(20 * FPS, 35 * FPS), 0, after(resume, silence), FPS),
        ]
        run = Run(stream(runs))
        (trim,) = run.trimmer.trims
        self.assertEqual(trim.reason, "small")
        self.assertAlmostEqual(trim.excess_s, -ts._TRIM_MARGIN_S, delta=0.1)
        self.assertEqual((run.drops, trim.dropped_bytes), (0, 0))
        self.assertEqual(run.output, run.input)

    def test_a_stall_and_backlog_without_a_restart_are_never_held(self):
        silence = 3.0
        resume = backlogged(20.0) + silence
        runs = [
            (range(20 * FPS), 900000, backlogged, FPS),
            (range(20 * FPS, 35 * FPS), 900000 + 20 * CLOCK, after(resume, silence + 5), FPS),
        ]
        run = Run(stream(runs))
        self.assertEqual(list(run.trimmer.trims), [])
        self.assertEqual(run.output, run.input)

    def test_a_reconnect_is_never_trimmed(self):
        timed = stream(repeat_runs(5.0, 1.0))
        restart = next(k for k, (at, _) in enumerate(timed) if at > backlogged(20.0))
        run = Run(timed, reconnect_before=restart)
        self.assertEqual([trim.reason for trim in run.trimmer.trims], [])
        self.assertEqual(run.output, run.input)

    def test_a_ten_second_gop_keeps_a_repeat_shorter_than_its_keyframe_interval(self):
        # 7 s repeated: no keyframe between 0 and R, so all of it stays (no new media lost);
        # 12 s repeated: the keyframe at 10 s is in reach.
        for repeat, dropped, reason in ((7.0, 0.0, "no keyframe"), (12.0, 10.0, "dropped")):
            with self.subTest(repeat=repeat):
                run = Run(stream(repeat_runs(repeat, 1.0, gop=10 * FPS, new=15.0)))
                (trim,) = run.trimmer.trims
                self.assertEqual((trim.reason, trim.dropped_s), (reason, dropped))
                if not dropped:
                    self.assertEqual(run.output, run.input)

    def test_the_hold_is_capped_in_time(self):
        # The new run comes at three times real time and never settles: released at the cap.
        resume = backlogged(20.0) + 1.0
        runs = [
            (range(20 * FPS), 900000, backlogged, FPS),
            (range(20 * FPS, 50 * FPS), 0, after(resume, 0.0, rate=3.0), FPS),
        ]
        run = Run(stream(runs))
        trim = run.trimmer.trims[0]
        self.assertGreaterEqual(trim.released_at - trim.held_at, ts._TRIM_HOLD_S)
        self.assertLess(trim.released_at - trim.held_at, ts._TRIM_HOLD_S + 0.1)

    def test_the_hold_is_capped_in_bytes(self):
        with mock.patch.object(ts, "_TRIM_HOLD_BYTES", 16 * 1024):
            run = Run(stream(repeat_runs(5.0, 1.0)))
        (trim,) = run.trimmer.trims
        self.assertLess(trim.released_at - trim.held_at, 0.5)  # with the burst's first read

    def test_a_repeat_reaches_no_further_back_than_the_last_two_runs(self):
        # Runs of 10 s and 3 s, then one whose burst claims 20 s: 13 s is the most it drops.
        runs = [
            (range(10 * FPS), 900000, backlogged, FPS),
            (range(1000, 1000 + 3 * FPS), 0, after(backlogged(10.0) + 1.0, 1.0), FPS),
            (range(2000, 2000 + 30 * FPS), 0, after(backlogged(10.0) + 5.0, 21.0), FPS),
        ]
        run = Run(stream(runs))
        trims = list(run.trimmer.trims)
        self.assertEqual([trim.reason for trim in trims], ["small", "dropped"])
        self.assertGreater(trims[1].excess_s, 15.0)
        self.assertEqual(trims[1].dropped_s, 13.0)

    def test_the_end_of_the_input_releases_a_held_run_untouched(self):
        timed = stream(repeat_runs(5.0, 1.0))
        resume = backlogged(20.0) + 1.0
        run = Run([item for item in timed if item[0] <= resume])  # it ends with the burst
        (trim,) = run.trimmer.trims
        self.assertEqual(trim.reason, "end")
        self.assertEqual(run.output, run.input)

    def test_counters_seed_a_joiner_that_runs_them_on(self):
        joiner = ts.ContinuityJoiner({VIDEO_PID: 7})
        packet = pes_packets(VIDEO_PID, 0xE0, picture(0, True), 0, cc_start=3)[0][0]
        self.assertEqual(joiner.feed(packet)[3] & 0x0F, 8)


if __name__ == "__main__":
    unittest.main()
