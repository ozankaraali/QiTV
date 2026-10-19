"""Tests for ClockJoiner (services/live/ts.py): a restarted run's timestamps re-based so the
clock runs on, on synthetic streams (`ts_fixtures.make_stream`: PCR on each video PES start).
The junctions are given as RepeatTrimmer gives them (`feed(..., junction=True)`)."""

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


def joined(*runs: bytes, chunk: int | None = None) -> tuple[bytes, ClockJoiner, int]:
    """The runs through a ClockJoiner, each after the first fed as a junction; returns its
    output, the joiner and how many pieces split it (re-based junctions)."""
    joiner = ClockJoiner(program=lambda: (VIDEO_PID, 0x1B))
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
        joiner = ClockJoiner(program=lambda: (VIDEO_PID, 0x1B))
        old, new = make_stream(5), make_stream(5)
        out = b"".join(joiner.feed(old)) + b"".join(joiner.feed(new, junction=True))
        out += joiner.discontinuity()
        after = make_stream(3)
        self.assertEqual(b"".join(joiner.feed(after)) + joiner.finish(), after)
        self.assertEqual(joiner.offset_s, 0.0)
        self.assertEqual(len(out), len(old) + len(new))

    def test_pes_without_the_optional_header_carry_no_time(self):
        # A padding stream (0xBE): its header has no flags byte to read as one.
        pkt = bytes([0x47, 0x41, 0x02, 0x10]) + b"\x00\x00\x01\xbe\x00\xb2" + b"\xc0\xc0" * 89
        self.assertEqual(ts._pes_times(pkt, 0), (None, None))


if __name__ == "__main__":
    unittest.main()
