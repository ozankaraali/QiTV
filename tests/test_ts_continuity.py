"""HLS segment joins with continuous continuity counters (services/live/ts.py, 2.0.2).

Some CDNs package each HLS segment on its own: every PID's continuity counter, PAT's and PMT's
too, restarts at 0. Joined as they are, VLC sees a counter break on every PID at every segment,
loses its reference clock and drops late pictures (measured on a public channel).
`ContinuityJoiner` makes the joins continue each PID's counter (ISO 13818-1, 2.4.3.3).

Run with `PYTHONPATH=tests` (or `discover -s tests`): it imports `ts_fixtures`.
"""

import random
import unittest

import ts_fixtures as fx

from services.live.ts import PACKET, ContinuityJoiner

VIDEO, AUDIO, PMT = fx.VIDEO_PID, fx.AUDIO_PID, fx.PMT_PID
NULL = 0x1FFF


def payload(pid, cc, *, pusi=False):
    return fx._packet(pid, b"\xaa" * 184, cc, pusi=pusi)


def adaptation_only(pid, cc):
    """A packet with an adaptation field and no payload (adaptation_field_control 10)."""
    return bytes([0x47, pid >> 8, pid & 0xFF, 0x20 | cc, 183, 0x00]) + b"\xff" * 182


def with_pcr(pid, cc, pcr=900000):
    af = bytes([0x10]) + fx._pcr_bytes(pcr)
    return fx._packet(pid, b"\xaa" * 100, cc, pusi=True, af=af)


def counters(data):
    """[(pid, cc)] of every packet in `data`."""
    return [
        (((data[i + 1] & 0x1F) << 8) | data[i + 2], data[i + 3] & 0x0F)
        for i in range(0, len(data), PACKET)
    ]


def of(data, pid):
    return [cc for p, cc in counters(data) if p == pid]


def join(*segments, discontinuity_before=()):
    """The joined output of `segments`, each fed whole; a discontinuity before the indexes
    in `discontinuity_before`."""
    joiner, out = ContinuityJoiner(), b""
    for index, segment in enumerate(segments):
        joiner.segment()
        if index in discontinuity_before:
            joiner.discontinuity()
        out += joiner.feed(segment) + joiner.end()
    return out


class ContinuityJoinerTests(unittest.TestCase):
    def test_segments_that_restart_their_counters_run_on_across_the_join(self):
        def segment():
            return b"".join(
                [fx.pat(cc=0), fx.pmt(cc=0)]
                + [payload(VIDEO, cc, pusi=cc == 0) for cc in range(6)]
                + [payload(AUDIO, cc) for cc in range(3)]
            )

        out = join(segment(), segment(), segment())
        self.assertEqual(of(out, VIDEO), [cc % 16 for cc in range(18)])
        self.assertEqual(of(out, AUDIO), list(range(9)))
        self.assertEqual(of(out, 0), [0, 1, 2])  # PAT and PMT too
        self.assertEqual(of(out, PMT), [0, 1, 2])

    def test_an_adaptation_only_packet_keeps_the_previous_value(self):
        first = b"".join([payload(VIDEO, 3), adaptation_only(VIDEO, 3), payload(VIDEO, 4)])
        # The next segment restarts at 0, and its first packet of the PID has no payload.
        second = b"".join([adaptation_only(VIDEO, 0), payload(VIDEO, 0), payload(VIDEO, 1)])
        self.assertEqual(of(join(first, second), VIDEO), [3, 3, 4, 4, 5, 6])

    def test_the_first_counter_seen_for_a_pid_stays(self):
        first = b"".join([payload(VIDEO, 9), payload(VIDEO, 10)])
        second = b"".join([payload(VIDEO, 0), payload(AUDIO, 5), payload(AUDIO, 6)])
        out = join(first, second)
        self.assertEqual(of(out, VIDEO), [9, 10, 11])
        self.assertEqual(of(out, AUDIO), [5, 6])  # first seen in the second segment

    def test_a_loss_or_a_repeat_inside_a_segment_stays_as_it_was(self):
        first = b"".join(payload(VIDEO, cc) for cc in range(6))
        lost = b"".join(payload(VIDEO, cc) for cc in (0, 1, 3))
        repeated = b"".join(payload(VIDEO, cc) for cc in (0, 1, 1, 2))
        self.assertEqual(of(join(first, lost), VIDEO), [0, 1, 2, 3, 4, 5, 6, 7, 9])
        self.assertEqual(of(join(first, repeated), VIDEO), [0, 1, 2, 3, 4, 5, 6, 7, 7, 8])

    def test_null_and_errored_packets_are_left_alone(self):
        errored = bytearray(payload(VIDEO, 0))
        errored[1] |= 0x80  # transport_error_indicator: its header is not to be trusted
        first = b"".join([payload(VIDEO, 7), payload(NULL, 7)])
        second = b"".join([payload(NULL, 2), bytes(errored), payload(VIDEO, 0)])
        out = join(first, second)
        self.assertEqual(out[len(first) : len(first) + 2 * PACKET], second[: 2 * PACKET])
        self.assertEqual(of(out, VIDEO), [7, 0, 8])  # the errored one as it came

    def test_only_the_counters_change(self):
        segments = [fx.make_stream(1), fx.make_stream(1, pts_start=990000)]
        out = join(*segments)
        joined = b"".join(segments)
        self.assertEqual(len(out), len(joined))
        for i in range(0, len(out), PACKET):
            got, was = bytearray(out[i : i + PACKET]), bytearray(joined[i : i + PACKET])
            got[3] &= 0xF0
            was[3] &= 0xF0
            self.assertEqual(got, was, f"packet {i // PACKET}")

    def test_after_a_discontinuity_the_first_pcr_packet_says_so(self):
        first = b"".join([with_pcr(VIDEO, 0), payload(VIDEO, 1)])
        second = b"".join([payload(AUDIO, 0), with_pcr(VIDEO, 0), with_pcr(VIDEO, 1)])
        out = join(first, second, discontinuity_before={1})
        packets = [out[i : i + PACKET] for i in range(0, len(out), PACKET)]
        flagged = [bool(p[3] & 0x20 and p[4] and p[5] & 0x80) for p in packets]
        self.assertEqual(flagged, [False, False, False, True, False])
        self.assertEqual(of(out, VIDEO), [0, 1, 2, 3])  # still continuous across it
        self.assertFalse(any(bool(p[5] & 0x80) for p in packets[:3] if p[3] & 0x20))
        self.assertFalse(join(first, second)[3 * PACKET + 5] & 0x80)  # no marker, no flag

    def test_any_chunking_gives_the_same_bytes(self):
        segments = [fx.make_stream(1), fx.make_stream(1, pts_start=990000)]
        whole = join(*segments)
        rng = random.Random(7)
        for _ in range(5):
            joiner, out = ContinuityJoiner(), b""
            for segment in segments:
                joiner.segment()
                at = 0
                while at < len(segment):
                    size = rng.randint(1, 3 * PACKET)
                    out += joiner.feed(segment[at : at + size])
                    at += size
                out += joiner.end()
            self.assertEqual(out, whole)

    def test_a_partial_packet_at_the_end_of_a_segment_is_passed_on_as_it_was(self):
        first = b"".join(payload(VIDEO, cc) for cc in range(3)) + b"\x47\x01\x00"
        joiner = ContinuityJoiner()
        joiner.segment()
        out = joiner.feed(first)
        self.assertEqual(len(out), 3 * PACKET)
        self.assertEqual(joiner.end(), b"\x47\x01\x00")
        joiner.segment()
        self.assertEqual(of(joiner.feed(payload(VIDEO, 0)), VIDEO), [3])

    def test_a_segment_out_of_packet_sync_passes_through(self):
        first = b"".join(payload(VIDEO, cc) for cc in range(3))
        shifted = b"\x00" + b"".join(payload(VIDEO, cc) for cc in range(3))
        broken = payload(VIDEO, 0) + b"\x00" * PACKET + payload(VIDEO, 1)
        self.assertEqual(join(first, shifted)[len(first) :], shifted)
        out = join(first, broken)[len(first) :]
        self.assertEqual(of(out[:PACKET], VIDEO), [3])  # in sync until the break
        self.assertEqual(out[PACKET:], broken[PACKET:])  # then as it came


if __name__ == "__main__":
    unittest.main()
