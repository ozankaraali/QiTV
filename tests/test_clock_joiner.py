"""Tests for ClockJoiner (services/live/ts.py): a restarted run's timestamps re-based so the
clock runs on, on synthetic streams (`ts_fixtures.make_stream`: PCR on each video PES start).
The junctions are given as RepeatTrimmer gives them (`feed(..., junction=True)`)."""

from __future__ import annotations

from typing import Callable
import unittest

from services.live import ts
from services.live.ts import PACKET, ClockJoiner, pts_delta
from tests.ts_fixtures import AUDIO_FRAME_TICKS, AUDIO_PID, CLOCK, VIDEO_PID, make_stream

FRAME = CLOCK // 25
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


def pcrs(data: bytes) -> list[int]:
    return [ts._pcr_base(p, 6) for p in packets(data) if p[3] & 0x20 and p[4] >= 7 and p[5] & 0x10]


def steps(values: list[int]) -> set[int]:
    return {pts_delta(a, b) for a, b in zip(values, values[1:])}


def flags(data: bytes) -> int:
    return sum(1 for p in packets(data) if p[3] & 0x20 and p[4] and p[5] & 0x80)


def cc_breaks(data: bytes) -> int:
    breaks, last = 0, {}
    for p in packets(data):
        pid, b3 = pid_of(p), p[3]
        if not b3 & 0x10:
            continue
        flagged = b3 & 0x20 and p[4] and p[5] & 0x80
        if pid in last and (b3 & 0x0F) not in (last[pid], (last[pid] + 1) & 0x0F) and not flagged:
            breaks += 1
        last[pid] = b3 & 0x0F
    return breaks


def flag_first(data: bytes) -> bytes:
    """`data` with the discontinuity_indicator on each PID's first packet that has an
    adaptation field, or stuffing to make one of (PAT, PMT), as a restarted muxer's stream
    starts (video: on its PCR)."""
    out, seen = [], set()
    for p in packets(data):
        if pid_of(p) not in seen:
            if p[3] & 0x20 and p[4]:
                seen.add(pid_of(p))
                p = p[:5] + bytes([p[5] | 0x80]) + p[6:]
            elif p[-2:] == b"\xff\xff":
                seen.add(pid_of(p))
                p = bytes([p[0], p[1], p[2], p[3] | 0x20]) + b"\x01\x80" + p[4:-2]
        out.append(p)
    return b"".join(out)


def shift_pid(data: bytes, pid: int, delta: int) -> bytes:
    """`data` with every PTS (and PCR) on `pid` moved by `delta` ticks."""
    out = []
    for p in packets(data):
        if pid_of(p) == pid:
            buf = bytearray(p)
            ts._shift_packet(buf, 0, delta)
            p = bytes(buf)
        out.append(p)
    return b"".join(out)


def joined(
    *runs: bytes, chunk: int | None = None, clock: Callable[[], float] | None = None
) -> tuple[bytes, ClockJoiner, int]:
    """The runs through a ClockJoiner, each after the first fed as a junction; returns its
    output, the joiner and how many pieces split it (re-based junctions). With `clock`, the
    joiner looks for forward jumps too, as the helper's does."""
    joiner = ClockJoiner(program=lambda: (VIDEO_PID, 0x1B), clock=clock)
    out, splits = [], 0
    for number, run in enumerate(runs):
        step = chunk or len(run)
        for at in range(0, len(run), step):
            pieces = joiner.feed(run[at : at + step], junction=bool(number) and at == 0)
            splits += len(pieces) - 1
            out.append(b"".join(pieces))
    return b"".join(out) + joiner.finish(), joiner, splits


class ClockJoinerTests(unittest.TestCase):
    def assert_runs_on(self, out: bytes, junctions: int = 1) -> None:
        self.assertEqual(steps(times(out, VIDEO_PID)), {FRAME})
        # The sound follows on too: by its frame, but at a junction by what the new run's own
        # alignment gives (the old run's sound may end a little before its picture).
        audio = times(out, AUDIO_PID)
        gaps = [pts_delta(a, b) for a, b in zip(audio, audio[1:])]
        self.assertLessEqual(sum(1 for gap in gaps if gap != AUDIO_FRAME_TICKS), junctions)
        self.assertTrue(all(0 < gap <= AUDIO_FRAME_TICKS + FRAME for gap in gaps))
        self.assertTrue(all(0 < step <= FRAME for step in steps(pcrs(out))))
        self.assertEqual(cc_breaks(out), 0)

    def test_nothing_is_rewritten_before_a_junction(self):
        data = make_stream(6)
        for chunk in (None, 187, 5000):
            out, joiner, splits = joined(data, chunk=chunk)
            self.assertEqual((out, splits), (data, 0))
            self.assertEqual(list(joiner.junctions), [])

    def test_a_restart_runs_on_a_frame_after_the_old_run(self):
        old, new = make_stream(5), make_stream(5)
        for chunk in (None, 187, 5000):
            with self.subTest(chunk=chunk):
                out, joiner, splits = joined(old, new, chunk=chunk)
                self.assertEqual(len(out), len(old) + len(new))
                self.assert_runs_on(out)
                self.assertEqual(splits, 1)
                self.assertEqual(
                    list(joiner.junctions), [(-(5 * CLOCK - FRAME) / CLOCK, FRAME / CLOCK)]
                )
                self.assertEqual(joiner.offset_s, 5.0)

    def test_a_flagged_restart_across_the_33_bit_wrap_leaves_no_trace(self):
        # The new run's clock starts 0.7 s below the wrap, as a restarted muxer's does, and
        # its first packets carry the flag (with counters from 0): the flags go, the counters
        # run on.
        old = make_stream(5)
        new = flag_first(make_stream(5, pts_start=WRAP - 63000))
        self.assertEqual(flags(new), 4)  # PAT, PMT, the picture's PCR, the sound's stuffing
        out, joiner, _ = joined(old, new, chunk=5000)
        self.assert_runs_on(out)
        self.assertEqual(flags(out), 0)
        self.assertEqual(len(joiner.junctions), 1)

    def test_tables_and_sound_before_the_first_picture_move_with_it(self):
        # An unflagged restart whose sound and PCR come before its first picture.
        old = make_stream(5)
        new = packets(make_stream(5, pts_start=200000))
        audio = next(k for k, p in enumerate(new) if pid_of(p) == AUDIO_PID)
        video = next(k for k, p in enumerate(new) if pid_of(p) == VIDEO_PID)
        first_audio = [k for k in range(audio, len(new)) if pid_of(new[k]) == AUDIO_PID][:2]
        moved = [new[k] for k in first_audio]
        rest = [p for k, p in enumerate(new) if k not in first_audio]
        new_run = b"".join(rest[:video] + moved + rest[video:])
        self.assertEqual(pid_of(new_run[2 * PACKET : 3 * PACKET]), AUDIO_PID)
        out, _, _ = joined(old, new_run, chunk=187)
        self.assert_runs_on(out)

    def test_two_restarts_in_a_row_add_up(self):
        out, joiner, splits = joined(make_stream(4), make_stream(4), make_stream(4), chunk=187)
        self.assert_runs_on(out, junctions=2)
        self.assertEqual((len(joiner.junctions), splits), (2, 2))
        self.assertEqual(joiner.offset_s, 8.0)

    def test_the_new_runs_audio_to_video_alignment_is_kept(self):
        # In the new run the sound is 0.2 s ahead of the picture: it stays so after re-basing.
        old = make_stream(5)
        new = shift_pid(make_stream(5), AUDIO_PID, round(0.2 * CLOCK))
        out, _, _ = joined(old, new, chunk=5000)
        tail = out[len(old) :]
        video, audio = times(tail, VIDEO_PID), times(tail, AUDIO_PID)
        self.assertEqual(pts_delta(video[0], audio[0]), round(0.2 * CLOCK))
        self.assertEqual(steps(video), {FRAME})

    def test_a_junction_whose_clock_runs_on_is_left_be(self):
        # Within two frames of running on (a continuous stream): nothing to re-base or split.
        data = make_stream(8)
        cut = len(data) // 2 - len(data) // 2 % PACKET
        out, joiner, splits = joined(data[:cut], data[cut:], chunk=5000)
        self.assertEqual((out, splits), (data, 0))
        self.assertEqual(list(joiner.junctions), [])

    def test_bytes_out_of_sync_inside_a_rebased_run_leave_the_packets_after_them_rebased(self):
        # A desync in the middle of a re-based run (bytes that are no packet, in one read with
        # the packets around them): they pass as they are, and the packets after them still
        # run on the joined clock and counters.
        old, new = make_stream(5), make_stream(5)
        junk = b"\x99" * 37
        cut = len(new) // 2 - len(new) // 2 % PACKET
        for chunk in (None, 5000):
            with self.subTest(chunk=chunk):
                out, _, _ = joined(old, new[:cut] + junk + new[cut:], chunk=chunk)
                self.assertEqual(out.count(junk), 1)
                self.assert_runs_on(out.replace(junk, b""))

    def test_a_reconnect_ends_the_rebasing(self):
        for clock in (None, lambda: 0.0):  # (the helper's joiner looks for jumps too)
            with self.subTest(watching=clock is not None):
                joiner = ClockJoiner(program=lambda: (VIDEO_PID, 0x1B), clock=clock)
                old, new = make_stream(5), make_stream(5)
                out = b"".join(joiner.feed(old)) + b"".join(joiner.feed(new, junction=True))
                out += joiner.discontinuity()
                after = make_stream(3, pts_start=900000 + 40 * CLOCK)  # a clock that went on
                self.assertEqual(b"".join(joiner.feed(after)) + joiner.finish(), after)
                self.assertEqual(joiner.offset_s, 0.0)
                self.assertEqual(len(out), len(old) + len(new))

    def test_a_reconnect_onto_a_clock_behind_follows_the_last_picture(self):
        # A new connection whose muxer started over (its clock 2.4 hours back, flagged): libVLC
        # 3 keeps reading its time as 0 behind where it opened while the picture plays on, so
        # the new clock follows the last picture by a frame, its counters run on, unflagged.
        long_run = (2 * 3600 + 25 * 60) * CLOCK
        for clock in (None, lambda: 0.0):
            for chunk in (None, 5000):
                with self.subTest(watching=clock is not None, chunk=chunk):
                    joiner = ClockJoiner(program=lambda: (VIDEO_PID, 0x1B), clock=clock)
                    old = make_stream(5, pts_start=900000 + long_run)
                    after = flag_first(make_stream(5))
                    out = b"".join(joiner.feed(old)) + joiner.discontinuity()
                    step = chunk or len(after)
                    for at in range(0, len(after), step):
                        pieces = joiner.feed(after[at : at + step])
                        self.assertEqual(len(pieces), 1)  # the reconnect cut there already
                        out += pieces[0]
                    out += joiner.finish()
                    self.assert_runs_on(out)
                    self.assertEqual(flags(out), 0)
                    self.assertAlmostEqual(joiner.offset_s, long_run / CLOCK + 5.0, places=1)

    def test_a_reconnect_slightly_behind_follows_the_last_picture_too(self):
        # A few seconds back (a backlog sent again on another clock): VLC drops the pictures
        # it has played the time of as late; following on, it plays them.
        joiner = ClockJoiner(program=lambda: (VIDEO_PID, 0x1B))
        old, after = make_stream(8), make_stream(5, pts_start=900000 + 5 * CLOCK)
        out = b"".join(joiner.feed(old)) + joiner.discontinuity()
        out += b"".join(joiner.feed(after)) + joiner.finish()
        self.assert_runs_on(out)

    def test_a_reconnect_onto_a_clock_ahead_passes_as_it_comes(self):
        # Ahead (an outage the source's clock went on through): the segmenter leaves the step
        # out of the timeline and says where, as before.
        joiner = ClockJoiner(program=lambda: (VIDEO_PID, 0x1B), clock=lambda: 0.0)
        old, after = make_stream(5), make_stream(3, pts_start=900000 + 35 * CLOCK)
        out = b"".join(joiner.feed(old)) + joiner.discontinuity()
        out += b"".join(joiner.feed(after)) + joiner.finish()
        self.assertEqual(out, old + after)
        self.assertEqual(joiner.offset_s, 0.0)

    def test_a_junction_before_any_picture_after_a_reconnect_follows_the_last_one(self):
        # Where a loop the loop trimmer dropped across a reconnect ends: the new content is
        # re-based onto the last picture before the reconnect, as after a pause. Once a
        # picture passed after the reconnect, a junction follows that one instead.
        for clock in (None, lambda: 0.0):
            with self.subTest(watching=clock is not None):
                joiner = ClockJoiner(program=lambda: (VIDEO_PID, 0x1B), clock=clock)
                old = make_stream(5)
                later = shift_pid(
                    shift_pid(make_stream(5), VIDEO_PID, 40 * CLOCK), AUDIO_PID, 40 * CLOCK
                )
                out = b"".join(joiner.feed(old)) + joiner.discontinuity()
                out += b"".join(joiner.feed(later, junction=True)) + joiner.finish()
                self.assertEqual(steps(times(out, VIDEO_PID)), {FRAME})
                self.assertEqual(list(joiner.junctions), [(40 - 5 + FRAME / CLOCK, FRAME / CLOCK)])

                joiner = ClockJoiner(program=lambda: (VIDEO_PID, 0x1B), clock=clock)
                after = make_stream(3)
                out = b"".join(joiner.feed(old)) + joiner.discontinuity()
                out += b"".join(joiner.feed(after))
                out += b"".join(joiner.feed(later, junction=True)) + joiner.finish()
                self.assertEqual(list(joiner.junctions), [(40 - 3 + FRAME / CLOCK, FRAME / CLOCK)])

    def test_pes_without_the_optional_header_carry_no_time(self):
        # A padding stream (0xBE): its header has no flags byte to read as one.
        pkt = bytes([0x47, 0x41, 0x02, 0x10]) + b"\x00\x00\x01\xbe\x00\xb2" + b"\xc0\xc0" * 89
        self.assertEqual(ts._pes_times(pkt, 0), (None, None))

    def test_restarts_rebase_alike_while_forward_jumps_are_looked_for(self):
        # The helper's joiner has an arrival clock: restarts, flagged or not, two in a row, and
        # a junction whose clock runs on, come out as they did without one.
        cases = {
            "restart": (make_stream(5), make_stream(5)),
            "flagged": (make_stream(5), flag_first(make_stream(5, pts_start=WRAP - 63000))),
            "two": (make_stream(4), make_stream(4), make_stream(4)),
            "runs on": (make_stream(8)[: 600 * PACKET], make_stream(8)[600 * PACKET :]),
        }
        for name, runs in cases.items():
            for chunk in (187, 5000):
                with self.subTest(case=name, chunk=chunk):
                    blind = joined(*runs, chunk=chunk)
                    seen = joined(*runs, chunk=chunk, clock=lambda: 0.0)
                    self.assertEqual(seen[0], blind[0])
                    self.assertEqual(seen[2], blind[2])
                    self.assertEqual(list(seen[1].jumps), [])


def video_times(data: bytes) -> list[tuple[int, int]]:
    """(PTS, DTS) of each picture, in order; the DTS is the PTS where there is none."""
    out = []
    for p in packets(data):
        if pid_of(p) == VIDEO_PID and p[1] & 0x40:
            pts, dts = ts._pes_times(p, 0)
            if pts is not None:
                out.append((pts, pts if dts is None else dts))
    return out


def unwrapped(values: list[int]) -> list[int]:
    out = [values[0]] if values else []
    for a, b in zip(values, values[1:]):
        out.append(out[-1] + pts_delta(a, b))
    return out


def watched(
    segments: list[bytes],
    *,
    chunk: int | None = None,
    extinf: list[float] | None = None,
    times: list[float] | None = None,
    marks: tuple[int, ...] = (),
) -> tuple[bytes, ClockJoiner, int]:
    """The segments through a ClockJoiner that looks for forward jumps: with `extinf`, each
    segment arrives as an HLS segment of that EXTINF; else as raw TS whose arrival clock reads
    `times[k]` (0.0 by default: one burst) while segment k is fed. A discontinuity comes
    before each segment numbered in `marks`. Returns the output, the joiner and how many
    pieces split it."""
    now = [0.0]
    joiner = ClockJoiner(
        program=lambda: (VIDEO_PID, 0x1B), clock=None if extinf else (lambda: now[0])
    )
    out, splits = [], 0
    for number, segment in enumerate(segments):
        if number in marks:
            out.append(joiner.discontinuity())
        if extinf:
            joiner.arrive(extinf[number])
        elif times:
            now[0] = times[number]
        step = chunk or len(segment)
        for at in range(0, len(segment), step):
            pieces = joiner.feed(segment[at : at + step])
            splits += len(pieces) - 1
            out.append(b"".join(pieces))
    return b"".join(out) + joiner.finish(), joiner, splits


def at_seconds(data: bytes, *marks: float) -> list[bytes]:
    """`data` cut at the program tables nearest each media time in `marks` (they come every
    half second), as HLS segments are cut."""
    tables = [k * PACKET for k, p in enumerate(packets(data)) if pid_of(p) == 0]
    cuts = [tables[round(mark * 2)] for mark in marks]
    edges = [0, *cuts, len(data)]
    return [data[a:b] for a, b in zip(edges, edges[1:])]


def segmented(data: bytes) -> tuple[list[ts.Event], list[tuple[int, float | None, float]]]:
    """What the segmenter makes of `data`: its events, and the jumps it left out."""
    seg = ts.Segmenter(clock=lambda: 0.0)
    events = seg.feed(data) + seg.finish()
    return events, seg.dropped


class ForwardJumpTests(unittest.TestCase):
    """A clock that leaps ahead where arrival does not explain it is re-based (ClockJoiner
    with an arrival clock for raw TS, or HLS segments that `arrive`)."""

    def assert_runs_on(self, out: bytes) -> None:
        pictures = video_times(out)
        self.assertEqual(steps([dts for _, dts in pictures]), {FRAME})  # decode order
        shown = sorted(unwrapped([pts for pts, _ in pictures]))
        self.assertEqual({b - a for a, b in zip(shown, shown[1:])}, {FRAME})  # display order
        self.assertTrue(all(pts_delta(dts, pts) >= 0 for pts, dts in pictures))
        audio = times(out, AUDIO_PID)
        gaps = [pts_delta(a, b) for a, b in zip(audio, audio[1:])]
        self.assertTrue(all(0 < gap <= AUDIO_FRAME_TICKS + FRAME for gap in gaps), gaps)
        self.assertTrue(all(0 < step <= FRAME for step in steps(pcrs(out))))
        self.assertEqual(cc_breaks(out), 0)
        events, dropped = segmented(out)
        self.assertEqual(dropped, [])  # the timeline leaves nothing out
        opened = [event for event in events if isinstance(event, ts.Opened)]
        self.assertFalse(any(event.discontinuity for event in opened[1:]))

    def test_a_jump_inside_a_raw_ts_burst_runs_on_a_frame_after(self):
        # Every PID 13 s ahead from 12 s on, the whole stream in one burst (as a provider's
        # backlog): nothing in its arrival explains the step.
        data = make_stream(20, jumps=((12.0, 13.0),))
        for chunk in (None, 187, 5000):
            with self.subTest(chunk=chunk):
                out, joiner, splits = watched([data], chunk=chunk)
                self.assertEqual((len(out), splits), (len(data), 0))  # no piece split
                self.assert_runs_on(out)
                self.assertEqual(len(joiner.jumps), 1)
                step, unexplained = joiner.jumps[0]
                self.assertAlmostEqual(step, 13 + FRAME / CLOCK)
                self.assertGreater(unexplained, 12.0)
                self.assertAlmostEqual(joiner.offset_s, -13.0)

    def test_pictures_decoded_before_the_jump_and_shown_after_it_settle(self):
        # As measured: with B pictures the PTS leaps with the presentation time and the DTS
        # with the decode time, so a P picture decoded before the leap shows after it (its PTS
        # leapt, its DTS not) and the next B picture is the other way round. Each timestamp
        # takes the offset that keeps its own kind running on.
        data = make_stream(20, bframes=True, jumps=((311 / 25, 13.0),))
        straddles = [
            (pts, dts)
            for pts, dts in video_times(data)
            if abs(pts_delta(dts, pts)) > 10 * CLOCK  # one of its two leapt, the other not
        ]
        self.assertEqual(len(straddles), 2)
        for chunk in (None, 187, 5000):
            with self.subTest(chunk=chunk):
                out, joiner, splits = watched([data], chunk=chunk)
                self.assertEqual((len(out), splits), (len(data), 0))
                self.assert_runs_on(out)
                self.assertEqual(len(joiner.jumps), 1)

    def test_sound_muxed_ahead_of_the_jump_moves_with_it(self):
        # The sound comes 0.3 s ahead of its pictures: its first frames past the leap arrive
        # before the picture that jumps, and wait for it.
        data = make_stream(20, jumps=((12.0, 13.0),), audio_ahead_s=0.3)
        for chunk in (None, 187):
            with self.subTest(chunk=chunk):
                out, joiner, _ = watched([data], chunk=chunk)
                self.assertEqual(len(out), len(data))
                self.assert_runs_on(out)
                self.assertEqual(len(joiner.jumps), 1)

    def test_a_jump_inside_an_hls_segment_runs_on(self):
        # The measured case: 4 s segments, the clock 13 s ahead from 14 s on, inside the
        # fourth: it holds 4 s of media over 17 s of timestamps.
        data = make_stream(24, bframes=True, jumps=((14.0, 13.0),))
        segments = at_seconds(data, 4.0, 8.0, 12.0, 16.0, 20.0)
        for chunk in (None, 187):
            with self.subTest(chunk=chunk):
                out, joiner, splits = watched(segments, chunk=chunk, extinf=[4.0] * 6)
                self.assertEqual((len(out), splits), (len(data), 0))
                self.assert_runs_on(out)
                self.assertEqual(len(joiner.jumps), 1)

    def test_a_step_its_arrival_explains_passes_as_it_came(self):
        # The same 4 s step: within the segment's EXTINF (it lists 8 s, with 4 s of media)
        # it stays, and so it does where a raw TS source paused as long; in one raw burst
        # nothing explains it, and it runs on.
        data = make_stream(20, jumps=((14.0, 4.0),))
        segments = at_seconds(data, 4.0, 8.0, 12.0, 16.0)
        out, joiner, _ = watched(segments, extinf=[4.0, 4.0, 4.0, 8.0, 4.0])
        self.assertEqual((out, list(joiner.jumps)), (data, []))
        before, after = at_seconds(data, 14.0)
        out, joiner, _ = watched([before, after], chunk=5000, times=[14.0, 22.0])
        self.assertEqual((out, list(joiner.jumps)), (data, []))
        out, joiner, _ = watched([data], chunk=5000)
        self.assertEqual(len(joiner.jumps), 1)
        self.assert_runs_on(out)

    def test_ordinary_streams_pass_byte_for_byte(self):
        # B pictures, 29.97 fps, a slideshow (a still every 5 s, its sound going on), a 4 s
        # gap in the pictures that the sound fills, a 2 s step (under JUMP_S): all in one
        # burst, the hardest case for arrival, and nothing is rewritten.
        streams = {
            "plain": make_stream(20),
            "b frames": make_stream(20, bframes=True),
            "29.97": make_stream(20, fps=30000 / 1001),
            "slideshow": make_stream(30, fps=0.2),
            "picture gap": make_stream(24, video_gaps=((14.0, 18.0),)),
            "2 s step": make_stream(20, jumps=((14.0, 2.0),)),
        }
        for name, data in streams.items():
            for chunk in (None, 187, 5000):
                with self.subTest(stream=name, chunk=chunk):
                    out, joiner, splits = watched([data], chunk=chunk)
                    self.assertEqual((out, splits), (data, 0))
                    self.assertEqual(list(joiner.jumps), [])

    def test_stills_without_sound_pass_byte_for_byte(self):
        # A still every 5 s and no sound PID (nothing goes on through the gaps): its first
        # steps come before its cadence is known, and in one raw burst, or in 6 s HLS
        # segments, the first read as a 5 s jump (its first still flashed for a frame). No
        # step is judged before the pictures went on 10 s; by then each is its cadence.
        data = make_stream(60, fps=0.2, audio=False)
        for chunk in (None, 187, 5000):
            with self.subTest(arrival="raw burst", chunk=chunk):
                out, joiner, splits = watched([data], chunk=chunk)
                self.assertEqual((out, splits, list(joiner.jumps)), (data, 0, []))
        segments = at_seconds(data, *range(6, 60, 6))
        out, joiner, splits = watched(segments, extinf=[6.0] * len(segments))
        self.assertEqual((out, splits, list(joiner.jumps)), (data, 0, []))

    def test_no_step_is_judged_before_the_pictures_went_on_ten_seconds(self):
        # The same 13 s jump 5 s into a burst passes as it came (the segmenter leaves it out
        # of the timeline, as before); 12 s in, it runs on.
        early = make_stream(20, jumps=((5.0, 13.0),))
        out, joiner, _ = watched([early], chunk=5000)
        self.assertEqual((out, list(joiner.jumps)), (early, []))
        late = make_stream(20, jumps=((12.0, 13.0),))
        out, joiner, _ = watched([late], chunk=5000)
        self.assertEqual(len(joiner.jumps), 1)

    def test_what_passes_as_it_came_the_segmenter_tells_apart(self):
        # A gap in the pictures that the sound fills passes as it came (raw TS, or HLS), and
        # the segmenter keeps it on the timeline; a jump before the pictures went on 10 s also
        # passes as it came, and the segmenter leaves it out of the timeline, as before.
        gap = make_stream(24, video_gaps=((14.0, 18.0),))
        for arrival, segments, extinf in (
            ("raw TS", [gap], None),
            ("HLS", at_seconds(gap, 4.0, 8.0, 12.0, 16.0, 20.0), [4.0] * 6),
        ):
            with self.subTest(arrival=arrival):
                out, joiner, _ = watched(segments, chunk=5000, extinf=extinf)
                self.assertEqual((out, list(joiner.jumps)), (gap, []))
                events, dropped = segmented(out)
                self.assertEqual(dropped, [])
                opened = [event for event in events if isinstance(event, ts.Opened)]
                self.assertFalse(any(event.discontinuity for event in opened))
        early = make_stream(20, jumps=((5.0, 13.0),))
        out, joiner, _ = watched([early], chunk=5000)
        self.assertEqual((out, list(joiner.jumps)), (early, []))
        _, dropped = segmented(out)
        self.assertEqual(len(dropped), 1)
        self.assertAlmostEqual(dropped[0][2], 13.0, places=3)

    def test_backward_steps_are_left_to_the_junctions(self):
        # A restart nobody reported (a step back) is no forward jump: it passes as it came.
        data = make_stream(12) + make_stream(12)
        out, joiner, _ = watched([data], chunk=5000)
        self.assertEqual((out, list(joiner.jumps)), (data, []))

    def test_a_discontinuity_keeps_what_a_jump_added_while_the_clock_goes_on(self):
        # An HLS ad break marked as a discontinuity whose timestamps go on: what the jump took
        # out stays taken out, so the clock does not leap there again.
        data = make_stream(24, jumps=((12.0, 13.0),))
        segments = at_seconds(data, 4.0, 8.0, 12.0, 16.0, 20.0)
        out, joiner, _ = watched(segments, extinf=[4.0] * 6, marks=(5,))
        self.assert_runs_on(out)
        self.assertEqual(len(joiner.jumps), 1)
        self.assertAlmostEqual(joiner.offset_s, -13.0)

    def test_a_discontinuity_onto_another_clock_passes_as_it_comes(self):
        # After a jump, a discontinuity whose content starts on a clock of its own: from there
        # the bytes pass as they came, as without a jump (the segmenter cuts there).
        first = make_stream(20, jumps=((12.0, 13.0),))
        other = make_stream(4, pts_start=900000 + 200 * CLOCK)  # ahead, past the window
        segments = [*at_seconds(first, 4.0, 8.0, 12.0, 16.0), other]
        out, joiner, _ = watched(segments, extinf=[4.0] * 6, marks=(5,))
        self.assertTrue(out.endswith(other))
        self.assertEqual(joiner.offset_s, 0.0)
        self.assertEqual(len(joiner.jumps), 1)
        self.assert_runs_on(out[: len(out) - len(other)])

    def test_a_discontinuity_onto_a_clock_far_behind_follows_the_last_picture(self):
        # After a jump, an HLS discontinuity whose content starts on a clock far behind (an
        # inserted break with its own timestamps): it follows the last picture, as a reconnect
        # onto a clock behind does.
        first = make_stream(20, pts_start=900000 + 3600 * CLOCK, jumps=((12.0, 13.0),))
        other = make_stream(4)
        segments = [*at_seconds(first, 4.0, 8.0, 12.0, 16.0), other]
        out, joiner, _ = watched(segments, extinf=[4.0] * 6, marks=(5,))
        self.assert_runs_on(out)
        self.assertEqual(len(joiner.jumps), 1)

    def test_a_discontinuity_onto_a_clock_a_little_behind_follows_the_last_picture(self):
        # 2.8 (a 2.4.2 review note): after a jump, a discontinuity whose clock is 20 s behind
        # the last picture (inside the window a clock that goes on keeps the jump's offset):
        # keeping the offset stepped the pictures back 20 s; it follows the last one instead.
        first = make_stream(20, pts_start=900000 + 30 * CLOCK, jumps=((12.0, 13.0),))
        other = make_stream(4, pts_start=900000 + 43 * CLOCK)  # 20 s behind its last picture
        segments = [*at_seconds(first, 4.0, 8.0, 12.0, 16.0), other]
        out, joiner, _ = watched(segments, extinf=[4.0] * 6, marks=(5,))
        self.assert_runs_on(out)
        self.assertEqual(len(joiner.jumps), 1)


if __name__ == "__main__":
    unittest.main()
