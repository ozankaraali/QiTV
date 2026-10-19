"""Tests for scripts/replay_capture.py on synthetic captures (no provider stream involved).

A capture is the bytes plus one "<cumulative bytes> <seconds>" line per read. These are built
from `ts_fixtures` with a unique picture per frame, so a byte-exact repeat is one on purpose.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import tempfile
import unittest

from scripts import replay_capture as rc
from tests.ts_fixtures import CLOCK, PMT_PID, VIDEO_PID, pat, pes_packets, pmt

FPS = 25
TICKS = CLOCK // FPS
BACKLOG = 5.0  # media the source sends at connect
FIRST = 0.1  # when the first byte arrives


def _picture(index: int) -> bytes:
    """An H.264 access unit only frame `index` has: an IDR picture every second, else P."""
    tag = bytes(0x80 | ((index >> (7 * k)) & 0x7F) for k in range(3))  # never a start code
    body = b"\x00\x00\x00\x01\x09\xf0"
    if index % FPS == 0:
        body += b"\x00\x00\x00\x01\x67" + b"\x55" * 15 + b"\x00\x00\x00\x01\x65\x55"
    else:
        body += b"\x00\x00\x00\x01\x41\x55"  # slice header 0x55: a P slice
    return body + tag + b"\x55" * 300


def write_capture(directory: str, name: str, runs, cut_last: int | None = None) -> str:
    """Write `<name>.ts` and `<name>.times`. `runs` are (frame indexes, first PTS, arrival),
    where arrival maps a frame's media position in its run to the second it arrives; each run
    after the first is flagged discontinuous on its first PCR. `cut_last` cuts the first run's
    last packet to that many bytes (a provider's junction)."""
    counters = {0: 0, PMT_PID: 0, VIDEO_PID: 0}
    timed: list[tuple[float, bytes]] = []
    for number, (indexes, first_pts, arrival) in enumerate(runs):
        for k, index in enumerate(indexes):
            at = arrival(k / FPS)
            if k % FPS == 0:
                timed.append((at, pat(cc=counters[0])))
                timed.append((at, pmt(audio_pids=(), cc=counters[PMT_PID])))
                counters[0] = (counters[0] + 1) & 0x0F
                counters[PMT_PID] = (counters[PMT_PID] + 1) & 0x0F
            pts = first_pts + k * TICKS
            packets, counters[VIDEO_PID] = pes_packets(
                VIDEO_PID,
                0xE0,
                _picture(index),
                pts,
                rai=index % FPS == 0,
                cc_start=counters[VIDEO_PID],
                pcr=pts,
            )
            if number and not k:
                flagged = bytearray(packets[0])
                flagged[5] |= 0x80  # discontinuity_indicator
                packets[0] = bytes(flagged)
            timed.extend((at, packet) for packet in packets)
        if number == 0 and cut_last is not None:
            at, packet = timed[-1]
            timed[-1] = (at, packet[:cut_last])
    data = bytearray()
    lines = []
    for k, (at, packet) in enumerate(timed):
        data += packet
        if k + 1 == len(timed) or timed[k + 1][0] != at:
            lines.append(f"{len(data)} {at:.3f}")
    path = Path(directory) / name
    path.with_suffix(".ts").write_bytes(bytes(data))
    path.with_suffix(".times").write_text("\n".join(lines) + "\n")
    return str(path)


def backlogged(media: float) -> float:
    """A run from connect: `BACKLOG` seconds at once, then real time."""
    return FIRST + max(0.0, media - BACKLOG)


class ReplayCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def measure(self, runs, cut_last=None):
        path = write_capture(self.tmp.name, "capture", runs, cut_last)
        return rc.measure(rc.load_capture(path))

    def test_a_steady_channel_shows_no_restart_no_repeat_and_a_flat_edge(self):
        result = self.measure([(range(40 * FPS), 900000, backlogged)])
        self.assertEqual(result["input"]["restarts"], 0)
        self.assertEqual(result["restarts"], [])
        self.assertEqual(result["ring"]["frames_repeated"], 0)
        self.assertEqual(result["ring"]["input_bytes_left_out"], 0)
        self.assertEqual(result["pipeline"]["discontinuities"], 0)
        edge = result["edge"]
        self.assertAlmostEqual(edge["ahead_at_backlog"], BACKLOG, delta=0.6)
        self.assertLess(abs(edge["drift_s_per_min"]), 0.5)

    def test_a_restart_that_resends_the_last_seconds_is_measured(self):
        # Run A: 20 s from connect, its last byte at 15.1 s; one second of silence; run B
        # restarts its clock at 0 and first sends A's last 5 s again (the same pictures),
        # with the silence and the repeat at once, then new frames at real time.
        silence, repeat = 1.0, 5.0
        resume = backlogged(20.0) + silence
        burst = repeat + silence
        again = [*range(int((20 - repeat) * FPS), 20 * FPS), *range(20 * FPS, 35 * FPS)]
        result = self.measure(
            [
                (range(20 * FPS), 900000, backlogged),
                (again, 0, lambda media: resume + max(0.0, media - burst)),
            ],
            cut_last=144,
        )
        self.assertEqual(result["input"]["restarts"], 1)
        row = result["restarts"][0]
        self.assertTrue(row["provider_indicator"])
        self.assertEqual(row["cut_short_bytes"], [144])
        self.assertAlmostEqual(row["silence_s"], silence, delta=0.1)
        self.assertAlmostEqual(row["r_s"], repeat, delta=0.3)
        self.assertAlmostEqual(row["r_edge_s"], repeat, delta=0.5)
        # A's last picture lost its tail where the packet was cut: one frame less repeats.
        self.assertEqual(row["exact_repeat"]["lead_s"], repeat - 1 / FPS)
        self.assertEqual(row["exact_repeat"]["starts_after_s"], 0.0)
        self.assertTrue(4.0 <= row["d_s"] <= row["r_s"])  # a keyframe every second
        self.assertEqual(row["pipeline"]["how"], "segmenter discontinuity")
        self.assertEqual(row["pipeline"]["cut_into_run_s"], 0.0)
        self.assertEqual(row["pipeline"]["start"], "key")
        # The provider's cut-short packet goes to VLC whole, its tail from the next packet.
        self.assertEqual(row["ring"]["cut_short_passed_on"], [144])
        self.assertTrue(row["ring"]["indicator_reaches_vlc"])
        self.assertEqual(result["ring"]["frames_repeated"], int(repeat * FPS) - 1)
        self.assertEqual(result["ring"]["input_bytes_left_out"], 144)
        # The edge runs ahead by the repeat; without R it would be flat.
        edge = result["edge"]
        ahead = [s["edge_minus_wall"] for s in edge["samples"]]
        self.assertAlmostEqual(ahead[-1] - ahead[10], repeat, delta=0.6)
        self.assertLess(abs(edge["drift_without_r_s_per_min"]), 1.0)

    def test_a_restart_whose_burst_makes_up_its_silence_has_nothing_to_drop(self):
        # After a 3 s stall the new run sends what was missed, then real time: R is 0.
        silence = 3.0
        resume = backlogged(20.0) + silence
        result = self.measure(
            [
                (range(20 * FPS), 900000, backlogged),
                (range(20 * FPS, 35 * FPS), 0, lambda media: resume + max(0.0, media - silence)),
            ]
        )
        row = result["restarts"][0]
        self.assertAlmostEqual(row["silence_s"], silence, delta=0.1)
        self.assertAlmostEqual(row["r_s"], 0.0, delta=0.3)
        self.assertEqual(row["d_s"], 0.0)
        self.assertEqual(row["exact_repeat"]["lead_s"], 0.0)
        self.assertEqual(row["ring"]["cut_short_passed_on"], [])
        self.assertEqual(result["ring"]["frames_repeated"], 0)
        edge = result["edge"]
        self.assertAlmostEqual(edge["ahead_at_end"], edge["ahead_at_backlog"], delta=0.6)

    def test_the_report_prints_every_restart(self):
        result = self.measure(
            [
                (range(10 * FPS), 900000, backlogged),
                (range(10 * FPS, 20 * FPS), 0, lambda media: backlogged(10.0) + 1.0 + media),
            ]
        )
        text = rc.report(result)
        self.assertIn("1 timestamp restarts", text)
        self.assertIn("disc. seg", text)


if __name__ == "__main__":
    unittest.main()
