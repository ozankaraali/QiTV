"""Tests for RepeatTrimmer (services/live/ts.py): a restart inside one connection that sends
media twice, on synthetic streams with arrival times.

Each picture is unique to its frame index, so a repeat is one on purpose; a run after the first
starts with its first PCR flagged, as the provider's restarted muxer does (or unflagged: `flag`;
with `tables`, its first PAT and PMT flagged too, every counter from 0). With `audio`, each
frame's sound comes just before its picture: a new run's sound and tables then come before its
first picture.
"""

from __future__ import annotations

import unittest
from unittest import mock

from services.live import ts
from services.live.ts import PACKET, ClockJoiner, RepeatTrimmer, pts_delta
from tests.ts_fixtures import AUDIO_PID, CLOCK, PMT_PID, VIDEO_PID, pat, pes_packets, pmt

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


def stream(
    runs, *, audio: bool = False, flag: bool = True, tables: bool = False
) -> list[tuple[float, bytes]]:
    """(arrival, packet) of `runs`: (frame indexes, first PTS, arrival, GOP in frames), where
    arrival maps a frame's media position in its run to the second it arrives."""
    counters = {0: 0, PMT_PID: 0, VIDEO_PID: 0, AUDIO_PID: 0}
    timed: list[tuple[float, bytes]] = []
    for number, (indexes, first_pts, arrival, gop) in enumerate(runs):
        for k, index in enumerate(indexes):
            at = arrival(k / FPS)
            restart = number and not k and flag
            if restart and tables:
                counters = dict.fromkeys(counters, 0)
            if k % FPS == 0:
                psi = [pat(cc=counters[0])]
                psi.append(pmt(audio_pids=(AUDIO_PID,) if audio else (), cc=counters[PMT_PID]))
                if restart and tables:  # the stuffing's last two bytes make a flagged field
                    psi = [
                        bytes([p[0], p[1], p[2], p[3] | 0x20]) + b"\x01\x80" + p[4:-2] for p in psi
                    ]
                timed.extend((at, packet) for packet in psi)
                counters[0] = (counters[0] + 1) & 0x0F
                counters[PMT_PID] = (counters[PMT_PID] + 1) & 0x0F
            pts = first_pts + k * TICKS
            if audio:
                sound = b"\xff\xf1" + bytes(0x80 | (index >> (7 * j)) & 0x7F for j in range(3))
                sound_packets, counters[AUDIO_PID] = pes_packets(
                    AUDIO_PID, 0xC0, sound + b"\xaa" * 150, pts, cc_start=counters[AUDIO_PID]
                )
                timed.extend((at, packet) for packet in sound_packets)
            key = k % gop == 0
            packets, counters[VIDEO_PID] = pes_packets(
                VIDEO_PID, 0xE0, picture(index, key), pts, rai=key,
                cc_start=counters[VIDEO_PID], pcr=pts,
            )  # fmt: skip
            if restart:
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
    """Feed a timed stream to a trimmer read by read, at each read's arrival time; with
    `clock`, through a ClockJoiner after it, as the helper does."""

    def __init__(
        self,
        timed,
        *,
        reconnect_before: int | None = None,
        clock: bool = False,
        finish: bool = True,
    ):
        self.now = 0.0
        self.trimmer = RepeatTrimmer(
            program=lambda: (VIDEO_PID, 0x1B), clock=lambda: self.now, join_counters=not clock
        )
        self.joiner = ClockJoiner(program=lambda: (VIDEO_PID, 0x1B)) if clock else None
        self.joined: list[bytes] = []
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
                    self._take(self.trimmer.discontinuity())
                    if self.joiner is not None:
                        self.joined.append(self.joiner.discontinuity())
                chunk.append(timed[k][1])
                k += 1
            self.now = at
            self._take(self.trimmer.feed(b"".join(chunk)))
        if not finish:  # the source goes silent: the caller ticks
            return
        self._take(self.trimmer.finish())
        if self.joiner is not None:
            self.joined.append(self.joiner.finish())

    def _take(self, pieces: list[bytes]) -> None:
        self.pieces.append(pieces)
        if self.joiner is not None:
            for k, piece in enumerate(pieces):
                self.joined.append(b"".join(self.joiner.feed(piece, junction=k > 0)))

    @property
    def output(self) -> bytes:
        """What the trimmer passed on (with `clock`, what the clock joiner did)."""
        if self.joiner is not None:
            return b"".join(self.joined)
        return b"".join(b"".join(pieces) for pieces in self.pieces)

    @property
    def junctions(self) -> int:
        """Where a new run starts: between two of the trimmer's pieces of one call."""
        return sum(len(pieces) - 1 for pieces in self.pieces)


def pid_of(p: bytes) -> int:
    return ((p[1] & 0x1F) << 8) | p[2]


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
        self.assertEqual(run.junctions, 1)
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
        # Released as soon as its first `_TRIM_NO_BURST_S` showed no burst, untouched (a run
        # of its own all the same).
        self.assertEqual(trim.reason, "no burst")
        self.assertLess(trim.released_at - trim.held_at, ts._TRIM_NO_BURST_S + 0.5)
        self.assertEqual((run.junctions, trim.dropped_bytes), (1, 0))
        self.assertEqual(run.output, run.input)

    def test_a_burst_a_second_after_the_restart_is_still_measured(self):
        # Seen live: the new run's first packets, a second's pause (the caller ticks), then
        # 9 s at once (a repeat), then real time. Let go as "no burst" on a tick within the
        # pause, all of it went through.
        silence = 1.0
        resume = backlogged(20.0) + silence
        first = 11 * FPS  # run A's last 9 s again

        def paused(media: float) -> float:
            return resume if media < 0.2 else resume + 1.0 + max(0.0, media - 11.0)

        timed = stream(
            [
                (range(20 * FPS), 900000, backlogged, FPS),
                (range(first, 40 * FPS), 900000 + first * TICKS, paused, FPS),
            ]
        )
        run = Run([item for item in timed if item[0] <= resume], finish=False)
        self.assertTrue(run.trimmer.holding)
        for k in range(1, 4):
            run.now = resume + 0.25 * k
            run._take(run.trimmer.tick())
        rest = [item for item in timed if item[0] > resume]
        k = 0
        while k < len(rest):
            run.now = rest[k][0]
            chunk = []
            while k < len(rest) and rest[k][0] == run.now:
                chunk.append(rest[k][1])
                k += 1
            run._take(run.trimmer.feed(b"".join(chunk)))
        run._take(run.trimmer.finish())
        (trim,) = run.trimmer.trims
        self.assertEqual(trim.reason, "dropped")
        self.assertGreaterEqual(trim.dropped_s, 7.0)

    def test_a_late_burst_is_measured_when_stay_live_asked_too(self):
        # As above with 3 s asked and the burst 1.3 s after the restart: the settle point
        # waits for the burst, so R is the repeat (about 9 s) and the drop reaches R and the
        # ask, not the ask alone (a settle point taken at 1 s measured R as 0).
        silence = 1.0
        resume = backlogged(20.0) + silence
        first = 11 * FPS

        def paused(media: float) -> float:
            return resume if media < 0.2 else resume + 1.3 + max(0.0, media - 11.0)

        timed = stream(
            [
                (range(20 * FPS), 900000, backlogged, FPS),
                (range(first, 40 * FPS), 900000 + first * TICKS, paused, FPS),
            ]
        )
        run = Run([item for item in timed if item[0] < resume], finish=False)
        run.trimmer.extra = 3.0  # asked before the restart came
        run.now = resume
        run._take(run.trimmer.feed(b"".join(p for at, p in timed if at == resume)))
        self.assertTrue(run.trimmer.holding)
        for k in range(1, 6):
            run.now = resume + 0.25 * k
            run._take(run.trimmer.tick())
        rest = [item for item in timed if item[0] > resume]
        k = 0
        while k < len(rest):
            run.now = rest[k][0]
            chunk = []
            while k < len(rest) and rest[k][0] == run.now:
                chunk.append(rest[k][1])
                k += 1
            run._take(run.trimmer.feed(b"".join(chunk)))
        run._take(run.trimmer.finish())
        (trim,) = run.trimmer.trims
        self.assertGreater(trim.excess_s, 7.0)
        self.assertGreaterEqual(trim.dropped_s, 10.0)
        self.assertGreater(trim.extra_s, 0.0)

    def test_after_a_silence_it_did_not_make_up_a_run_goes_at_once(self):
        # Seen live: 9.4 s of silence, VLC ran dry; a 2 s wait for a late burst froze it on.
        silence = 9.0
        resume = backlogged(20.0) + silence
        runs = [
            (range(20 * FPS), 900000, backlogged, FPS),
            (range(20 * FPS, 35 * FPS), 0, after(resume, 0.0), FPS),
        ]
        run = Run(stream(runs))
        (trim,) = run.trimmer.trims
        self.assertEqual(trim.reason, "no burst")
        self.assertLess(trim.released_at - trim.held_at, ts._TRIM_NO_BURST_S + 0.5)

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

    def ask_before_restart(self, timed, extra: float, at: float, quick_at=None):
        """Feed `timed`, setting the trimmer's `extra` once the clock reaches `at` (and with
        `quick_at`, when its last quick restart came)."""
        run = Run([item for item in timed if item[0] < at], finish=False)
        run.trimmer.extra = extra
        if quick_at is not None:
            run.trimmer._quick_at = quick_at
        rest = [item for item in timed if item[0] >= at]
        k = 0
        while k < len(rest):
            now = rest[k][0]
            chunk = []
            while k < len(rest) and rest[k][0] == now:
                chunk.append(rest[k][1])
                k += 1
            run.now = now
            run._take(run.trimmer.feed(b"".join(chunk)))
        run._take(run.trimmer.finish())
        return run

    def test_stay_live_s_extra_is_taken_at_the_next_restart(self):
        # R reads 4.75 s; stay-live is 3 s behind: the run is held until it brought 7.75 s
        # and dropped to its keyframe at 7 s, 2.25 s of it for stay-live. Asked once.
        resume = backlogged(20.0) + 1.0
        run = self.ask_before_restart(stream(repeat_runs(5.0, 1.0)), 3.0, resume)
        (trim,) = run.trimmer.trims
        self.assertEqual((trim.reason, trim.dropped_s), ("dropped", 7.0))
        self.assertAlmostEqual(trim.extra_s, 7.0 - (5.0 - ts._TRIM_MARGIN_S), delta=0.05)
        self.assertGreater(trim.released_at - trim.held_at, 1.7)  # until 7.75 s had come
        self.assertEqual((run.trimmer.extra, run.trimmer.steered, run.trimmer.quick), (0.0, 1, 1))
        self.assertEqual(video_pts(run.output)[20 * FPS], 7 * CLOCK)
        self.assertEqual(cc_breaks(run.output), 0)

    def test_a_run_without_a_burst_is_held_for_what_stay_live_asks(self):
        # A quick splice at real time would go on untouched; asked for 3 s, it is held until
        # 3 s came and dropped to its keyframe at 3 s.
        silence = 1.0
        resume = backlogged(20.0) + silence
        runs = [
            (range(20 * FPS), 900000, backlogged, FPS),
            (range(20 * FPS, 35 * FPS), 0, after(resume, silence), FPS),
        ]
        run = self.ask_before_restart(stream(runs), 3.0, resume)
        (trim,) = run.trimmer.trims
        self.assertEqual((trim.reason, trim.dropped_s, trim.extra_s), ("dropped", 3.0, 3.0))
        self.assertEqual(video_pts(run.output)[20 * FPS], 3 * CLOCK)

    def test_an_ask_that_changes_while_a_restart_is_held_is_the_next_ones(self):
        # Asked 3 s when the restart came: it takes them though the ask went to 0 meanwhile.
        timed = stream(repeat_runs(5.0, 1.0))
        resume = backlogged(20.0) + 1.0
        run = Run([item for item in timed if item[0] < resume], finish=False)
        run.trimmer.extra = 3.0
        for at, packet in [item for item in timed if item[0] >= resume]:
            run.now = at
            run._take(run.trimmer.feed(packet))
            if run.trimmer.holding and run.trimmer._confirmed:
                run.trimmer.extra = 0.0  # stay-live took it back meanwhile
        (trim,) = run.trimmer.trims
        self.assertEqual(trim.dropped_s, 7.0)
        self.assertAlmostEqual(trim.extra_s, 7.0 - (5.0 - ts._TRIM_MARGIN_S), delta=0.05)

    def test_an_ask_beyond_the_most_one_restart_takes_is_taken_once(self):
        # Asked 20 s, a restart takes at most 15: the ask is spent, never taken again.
        resume = backlogged(20.0) + 1.0
        run = self.ask_before_restart(stream(repeat_runs(5.0, 1.0, new=30.0)), 20.0, resume)
        (trim,) = run.trimmer.trims
        self.assertGreater(trim.extra_s, 0.0)
        self.assertEqual((run.trimmer.extra, run.trimmer.steered), (0.0, 1))

    def test_a_slow_restart_takes_nothing_of_what_stay_live_asks(self):
        # Three seconds of silence before it: the provider measured repeats nothing there.
        resume = backlogged(20.0) + 3.0
        run = self.ask_before_restart(stream(repeat_runs(5.0, 3.0)), 3.0, resume)
        (trim,) = run.trimmer.trims
        self.assertEqual((trim.reason, trim.dropped_s, trim.extra_s), ("dropped", 4.0, 0.0))
        self.assertEqual((run.trimmer.extra, run.trimmer.quick), (3.0, 0))

    def test_far_behind_long_after_a_quick_restart_a_slower_one_takes_the_ask(self):
        # Asked 9 s (far behind), 3 s of silence before it: with no quick restart for
        # `_TRIM_WAIT_S` it takes the ask less the 1.5 s of silence beyond a quick one's, held
        # until R (4.75 s) and 7.5 s came, and dropped to its keyframe at 12 s. A quick one
        # just before, a smaller ask or a longer silence: nothing (R alone, to 4 s).
        far = ts._TRIM_FAR_S + 1.0
        cases = (
            (3.0, far, -ts._TRIM_WAIT_S, 12.0),
            (3.0, far, -30.0, 4.0),
            (3.0, ts._TRIM_FAR_S - 1.0, -ts._TRIM_WAIT_S, 4.0),
            (ts._TRIM_SLOW_S + 1.0, far, -ts._TRIM_WAIT_S, 4.0),
        )
        for silence, extra, quick_ago, dropped in cases:
            with self.subTest(silence=silence, extra=extra, quick_ago=quick_ago):
                resume = backlogged(20.0) + silence
                timed = stream(repeat_runs(5.0, silence, new=30.0))
                run = self.ask_before_restart(timed, extra, resume, quick_at=resume + quick_ago)
                (trim,) = run.trimmer.trims
                taken = int(dropped > 4.0)
                self.assertEqual((trim.reason, trim.dropped_s), ("dropped", dropped))
                self.assertEqual((run.trimmer.steered, run.trimmer.quick), (taken, 0))
                self.assertEqual(run.trimmer.restarts, 1)  # what the session steers by
                self.assertEqual(run.trimmer.extra, 0.0 if taken else extra)

    def test_what_stay_live_asks_waits_for_a_restart_that_can_take_it(self):
        # A reconnect takes nothing: what was asked stays for the next restart.
        timed = stream(repeat_runs(5.0, 1.0))
        restart = next(k for k, (at, _) in enumerate(timed) if at > backlogged(20.0))
        run = Run(timed[:restart], finish=False)
        run.trimmer.extra = 2.0
        run._take(run.trimmer.discontinuity())
        self.assertEqual((run.trimmer.extra, run.trimmer.steered), (2.0, 0))

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
        # Runs of 10 s and 3 s (it makes up its silence: R under `_TRIM_MIN_S`), then one
        # whose burst claims 20 s: 13 s is the most it drops.
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

    def test_a_hold_is_judged_on_ticks_while_the_source_pauses(self):
        # The burst lands, then the source sends nothing: on the caller's ticks the hold sees
        # its media stand still, settles and drops the repeat then, not at the next read.
        timed = stream(repeat_runs(5.0, 1.0))
        resume = backlogged(20.0) + 1.0
        run = Run([item for item in timed if item[0] <= resume], finish=False)
        trimmer = run.trimmer
        self.assertTrue(trimmer.holding)
        released = []
        for k in range(1, 8):
            run.now = resume + 0.25 * k
            pieces = trimmer.tick()
            if len(pieces) > 1:
                released.append((run.now - resume, pieces))
                break
        ((after, pieces),) = released
        self.assertGreaterEqual(after, 1.0)  # a second of standing still: settled
        self.assertLessEqual(after, 1.5)
        self.assertFalse(trimmer.holding)
        (trim,) = trimmer.trims
        self.assertEqual((trim.reason, trim.dropped_s), ("dropped", 4.0))
        self.assertEqual(video_pts(pieces[1])[0], 4 * CLOCK)  # from the keyframe at 4 s
        self.assertEqual(trimmer.tick(), [b""])  # nothing held: nothing to say

    def assert_runs_on(self, out: bytes) -> None:
        """One clock: pictures a frame apart, sound and PCRs only forward, counters on."""

        def steps(pid):
            values = [
                ts._pes_times(p, 0)[0] for p in packets(out) if pid_of(p) == pid and p[1] & 0x40
            ]
            return [
                pts_delta(a, b)
                for a, b in zip(values, values[1:])
                if a is not None and b is not None
            ]

        self.assertEqual(set(steps(VIDEO_PID)), {TICKS})
        self.assertTrue(all(0 < step <= 2 * TICKS for step in steps(AUDIO_PID)))
        pcrs = [
            ts._pcr_base(p, 6) for p in packets(out) if p[3] & 0x20 and p[4] >= 7 and p[5] & 0x10
        ]
        self.assertTrue(all(0 < pts_delta(a, b) <= TICKS for a, b in zip(pcrs, pcrs[1:])))
        self.assertEqual(cc_breaks(out), 0)
        self.assertFalse(any(p[3] & 0x20 and p[4] and p[5] & 0x80 for p in packets(out)[50:]))

    def test_an_unflagged_restart_with_sound_before_its_picture_runs_on(self):
        # No flag anywhere; each run's sound and tables come before its first picture. Both a
        # burst that makes up its silence (kept whole) and one that repeats 5 s (trimmed to its
        # keyframe at 4 s) come out on one clock, the sound cut at the keyframe's time.
        silence = 1.0
        resume = backlogged(20.0) + silence
        cases = {
            "untouched": ([*range(20 * FPS, 30 * FPS)], silence, "no burst"),
            "trimmed": ([*range(15 * FPS, 30 * FPS)], 5.0 + silence, "dropped"),
        }
        for name, (indexes, burst, reason) in cases.items():
            with self.subTest(name):
                runs = [
                    (range(20 * FPS), 900000, backlogged, FPS),
                    (indexes, 0, after(resume, burst), FPS),
                ]
                run = Run(stream(runs, audio=True, flag=False), clock=True)
                self.assertEqual([trim.reason for trim in run.trimmer.trims], [reason])
                self.assertEqual(len(run.joiner.junctions), 1)
                self.assert_runs_on(run.output)

    def test_a_restarts_flagged_tables_go_with_it(self):
        # The restarted muxer flags its first PAT and PMT (counters from 0) right before its
        # flagged PCR: the held run begins with them. Through the clock joiner no flag and no
        # counter break is left, kept whole or trimmed; trimmed alone the counters run on, the
        # kept run's first PCR flagged.
        silence = 1.0
        untouched = [
            (range(20 * FPS), 900000, backlogged, FPS),
            (range(20 * FPS, 30 * FPS), 0, after(backlogged(20.0) + silence, silence), FPS),
        ]
        cases = [
            ("no burst", untouched, True, 0),
            ("dropped", repeat_runs(5.0, silence), True, 0),
            ("dropped", repeat_runs(5.0, silence), False, 1),
        ]
        for reason, runs, clock, flags_left in cases:
            with self.subTest(reason=reason, clock=clock):
                timed = stream(runs, tables=True)
                flagged = [
                    k for k, (_, p) in enumerate(timed) if p[3] & 0x20 and p[4] and p[5] & 0x80
                ]
                self.assertEqual(len(flagged), 3)  # PAT, PMT, the PCR
                run = Run(timed, clock=clock)
                (trim,) = run.trimmer.trims
                self.assertEqual((trim.reason, trim.offset), (reason, flagged[0] * PACKET))
                self.assertEqual(cc_breaks(run.output), 0)
                left = [p for p in packets(run.output) if p[3] & 0x20 and p[4] and p[5] & 0x80]
                self.assertEqual(len(left), flags_left)
                if clock:
                    self.assert_runs_on(run.output)

    def test_a_step_back_of_the_sound_alone_is_no_restart(self):
        # A sound PES steps back, but the PCR and the picture go on: it passes as it came.
        timed = stream([(range(20 * FPS), 900000, backlogged, FPS)], audio=True)
        sound = 900000 + 300 * TICKS
        k = next(
            k
            for k, (_, p) in enumerate(timed)
            if pid_of(p) == AUDIO_PID and p[1] & 0x40 and ts._pes_times(p, 0)[0] == sound
        )
        buf = bytearray(timed[k][1])
        ts._shift_packet(buf, 0, -5 * CLOCK)
        timed[k] = (timed[k][0], bytes(buf))
        run = Run(timed, clock=True)
        self.assertEqual(list(run.trimmer.trims), [])
        self.assertEqual(run.output, run.input)

    def test_a_restart_with_nothing_before_it_to_measure_from_goes_on_at_once(self):
        # The whole input in one read: no media passed on before the restart.
        timed = [(0.0, packet) for _, packet in stream(repeat_runs(5.0, 1.0))]
        run = Run(timed)
        (trim,) = run.trimmer.trims
        self.assertEqual((trim.reason, trim.released_at - trim.held_at), ("first", 0.0))
        self.assertEqual(run.output, run.input)

    def test_counters_seed_a_joiner_that_runs_them_on(self):
        joiner = ts.ContinuityJoiner({VIDEO_PID: 7})
        packet = pes_packets(VIDEO_PID, 0xE0, picture(0, True), 0, cc_start=3)[0][0]
        self.assertEqual(joiner.feed(packet)[3] & 0x0F, 8)


if __name__ == "__main__":
    unittest.main()
