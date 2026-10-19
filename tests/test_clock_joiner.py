"""Tests for ClockJoiner (services/live/ts.py): a restarted run's timestamps re-based so the
clock runs on, on synthetic streams (`ts_fixtures.make_stream`: PCR on each video PES start)."""

from __future__ import annotations

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
    return [
        ts._pes_times(p, 0)[0]
        for p in packets(data)
        if pid_of(p) == pid and p[1] & 0x40 and ts._pes_times(p, 0)[0] is not None
    ]


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


def run_on(old: bytes, kept: bytes) -> bytes:
    """`kept` as RepeatTrimmer passes a trimmed run on: its counters run on from `old`'s, and
    its first PCR is flagged."""
    counters = {}
    for p in packets(old):
        if p[3] & 0x10:
            counters[pid_of(p)] = p[3] & 0x0F
    joiner = ts.ContinuityJoiner(counters)
    joiner.segment()
    joiner.discontinuity()
    return joiner.feed(kept) + joiner.end()


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


def joined(data: bytes, chunk: int | None = None) -> tuple[bytes, ClockJoiner]:
    joiner = ClockJoiner(program=lambda: (VIDEO_PID, 0x1B))
    step = chunk or len(data)
    out = b"".join(b"".join(joiner.feed(data[at : at + step])) for at in range(0, len(data), step))
    return out + joiner.finish(), joiner


class ClockJoinerTests(unittest.TestCase):
    def assert_runs_on(self, out: bytes) -> None:
        self.assertEqual(steps(times(out, VIDEO_PID)), {FRAME})
        # The sound follows on too: by its frame, but at a junction by what the new run's own
        # alignment gives (the old run's sound may end a little before its picture).
        audio = [pts_delta(a, b) for a, b in zip(times(out, AUDIO_PID), times(out, AUDIO_PID)[1:])]
        self.assertLessEqual(sum(1 for step in audio if step != AUDIO_FRAME_TICKS), 1)
        self.assertTrue(all(0 < step <= AUDIO_FRAME_TICKS + FRAME for step in audio))
        self.assertTrue(all(0 < step <= FRAME for step in steps(pcrs(out))))
        self.assertEqual(cc_breaks(out), 0)

    def test_nothing_is_rewritten_before_a_restart(self):
        data = make_stream(6)
        for chunk in (None, 187, 5000):
            out, joiner = joined(data, chunk)
            self.assertEqual(out, data)
            self.assertEqual(list(joiner.junctions), [])

    def test_a_restart_runs_on_a_frame_after_the_old_run(self):
        data = make_stream(10, resets=(5.0,))
        for chunk in (None, 187, 5000):
            with self.subTest(chunk=chunk):
                out, joiner = joined(data, chunk)
                self.assertEqual(len(out), len(data))
                self.assert_runs_on(out)
                self.assertEqual(
                    list(joiner.junctions),
                    [(pts_delta(900000 + 5 * CLOCK - FRAME, 900000) / CLOCK, FRAME / CLOCK)],
                )
                self.assertEqual(joiner.offset_s, 5.0)

    def test_a_flagged_restart_across_the_33_bit_wrap_leaves_no_trace(self):
        # The new run's clock starts 0.7 s below the wrap, as a restarted muxer's does, and
        # its first packets carry the flag: the flags go, the counters run on.
        old = make_stream(5)
        new = flag_first(make_stream(5, pts_start=WRAP - 63000))
        self.assertEqual(flags(new), 4)  # PAT, PMT, the picture's PCR, the sound's stuffing
        out, joiner = joined(old + new, 5000)
        self.assert_runs_on(out)
        self.assertEqual(flags(out), 0)
        self.assertEqual(len(joiner.junctions), 1)

    def test_a_trimmed_run_kept_from_a_keyframe_ahead_runs_on(self):
        # A trimmed run starts at a keyframe 0.64 s past the old run's end, its first PCR
        # flagged: a junction all the same (not a frame on).
        old = make_stream(5)
        kept = run_on(old, make_stream(5, pts_start=900000 + round(5.64 * CLOCK))[2 * PACKET :])
        self.assertEqual(flags(kept), 1)
        out, joiner = joined(old + kept, 5000)
        self.assert_runs_on(out)
        self.assertEqual(flags(out), 0)
        self.assertEqual(joiner.junctions[0][1], FRAME / CLOCK)

    def test_a_flag_on_a_stream_that_runs_on_changes_nothing(self):
        data = make_stream(10)
        at = 125 * 6 * PACKET  # somewhere in the middle: the next video PES start gets a flag
        video = next(
            k for k in range(at, len(data), PACKET)
            if pid_of(data[k : k + 3]) == VIDEO_PID and data[k + 1] & 0x40
        )  # fmt: skip
        flagged = data[:video] + flag_first(data[video : video + PACKET]) + data[video + PACKET :]
        out, joiner = joined(flagged, 5000)
        self.assertEqual(out, flagged)
        self.assertEqual(list(joiner.junctions), [])

    def test_two_restarts_in_a_row_add_up(self):
        data = make_stream(12, resets=(4.0, 8.0))
        out, joiner = joined(data, 187)
        self.assert_runs_on(out)
        self.assertEqual(len(joiner.junctions), 2)
        self.assertEqual(joiner.offset_s, 8.0)

    def test_the_new_runs_audio_to_video_alignment_is_kept(self):
        # In the new run the sound is 0.2 s ahead of the picture: it stays so after re-basing.
        old = make_stream(5)
        new = shift_pid(make_stream(5), AUDIO_PID, round(0.2 * CLOCK))
        out, _ = joined(old + new, 5000)
        tail = out[len(old) :]
        video, audio = times(tail, VIDEO_PID), times(tail, AUDIO_PID)
        self.assertEqual(pts_delta(video[0], audio[0]), round(0.2 * CLOCK))
        self.assertEqual(steps(video), {FRAME})

    def test_a_reconnect_ends_the_rebasing(self):
        first = make_stream(10, resets=(5.0,))
        joiner = ClockJoiner(program=lambda: (VIDEO_PID, 0x1B))
        out = b"".join(joiner.feed(first))
        out += joiner.discontinuity()
        after = make_stream(3)
        self.assertEqual(b"".join(joiner.feed(after)) + joiner.finish(), after)
        self.assertEqual(joiner.offset_s, 0.0)
        self.assertEqual(len(out), len(first))


if __name__ == "__main__":
    unittest.main()
