"""Tests for the MPEG-TS scanner and keyframe segmenter (services/live/ts.py)."""

import os
import random
import sys
import time
import unittest
from unittest import mock

import ts_fixtures as fx

from services.live.ts import PACKET, Closed, Data, Opened, Segmenter, Unsupported, pts_delta

CLOCK = fx.CLOCK
TARGET = 2 * CLOCK
FRAME = CLOCK // 25
MASK = (1 << 33) - 1


# --- helpers ----------------------------------------------------------------------------------


def split_packets(data):
    return [data[i : i + PACKET] for i in range(0, len(data) - PACKET + 1, PACKET)]


def pid_of(p):
    return ((p[1] & 0x1F) << 8) | p[2]


def pusi_pts(p):
    start = 4 + (1 + p[4] if p[3] & 0x20 else 0)
    b = p[start + 9 : start + 14]
    return ((b[0] >> 1) & 7) << 30 | b[1] << 22 | (b[2] >> 1) << 15 | b[3] << 7 | b[4] >> 1


def pusi_offset(data, pid, pts):
    """Byte offset of the PUSI packet on `pid` whose PES carries `pts`."""
    for i, p in enumerate(split_packets(data)):
        if pid_of(p) == pid and p[1] & 0x40 and pusi_pts(p) == pts & MASK:
            return i * PACKET
    raise AssertionError(f"no PUSI with pts {pts}")


def run(data, chunk=None, **kwargs):
    seg = Segmenter(clock=lambda: 0.0, **kwargs)
    events = []
    step = chunk or len(data)
    for i in range(0, len(data), step):
        events += seg.feed(data[i : i + step])
    events += seg.finish()
    return seg, events


def merged(events):
    """Coalesce adjacent Data events (Data is only coalesced per feed call)."""
    out = []
    for e in events:
        if isinstance(e, Data) and out and isinstance(out[-1], Data):
            out[-1] = Data(out[-1].payload + e.payload)
        else:
            out.append(e)
    return out


def opens(events):
    return [e for e in events if isinstance(e, Opened)]


def closes(events):
    return [e for e in events if isinstance(e, Closed)]


def payload(events):
    return b"".join(e.payload for e in events if isinstance(e, Data))


def segments(events):
    """[(Opened, segment bytes)] in order."""
    segs = []
    for e in events:
        if isinstance(e, Opened):
            segs.append([e, b""])
        elif isinstance(e, Data):
            segs[-1][1] += e.payload
    return segs


def deltas(events):
    o = opens(events)
    return [pts_delta(a.first_pts, b.first_pts) for a, b in zip(o, o[1:])]


def psi_packets(pid, section, cc):
    """Packetize one PSI section (CRC appended), splitting it over packets when long."""
    data = b"\x00" + section + fx.crc32_mpeg(section).to_bytes(4, "big")
    out = []
    first = True
    while data:
        part, data = data[:184], data[184:]
        out.append(fx._packet(pid, part + b"\xff" * (184 - len(part)), cc, pusi=first))
        cc = (cc + 1) & 0x0F
        first = False
    return out, cc


def pat_section(programs):
    body = b"".join(n.to_bytes(2, "big") + (0xE000 | pid).to_bytes(2, "big") for n, pid in programs)
    head = bytes([0x00]) + (0xB000 | (5 + len(body) + 4)).to_bytes(2, "big")
    return head + (1).to_bytes(2, "big") + bytes([0xC1, 0, 0]) + body


def pmt_section(streams, *, pcr_pid=fx.VIDEO_PID, info=b"", program=1):
    body = (0xE000 | pcr_pid).to_bytes(2, "big") + (0xF000 | len(info)).to_bytes(2, "big") + info
    for stream_type, pid in streams:
        body += bytes([stream_type]) + (0xE000 | pid).to_bytes(2, "big") + b"\xf0\x00"
    head = bytes([0x02]) + (0xB000 | (5 + len(body) + 4)).to_bytes(2, "big")
    return head + program.to_bytes(2, "big") + bytes([0xC1, 0, 0]) + body


def replace_pid_packets(data, pid, make):
    """Replace every packet on `pid` by `make(cc) -> (packets, next_cc)`."""
    out = []
    cc = 0
    for p in split_packets(data):
        if pid_of(p) == pid:
            pkts, cc = make(cc)
            out.extend(pkts)
        else:
            out.append(p)
    return b"".join(out)


def remap_pid(data, old, new):
    out = []
    for p in split_packets(data):
        if pid_of(p) == old:
            p = bytes([p[0], (p[1] & 0xE0) | (new >> 8), new & 0xFF]) + p[3:]
        out.append(p)
    return b"".join(out)


def video_only(frames, *, video_type=0x1B):
    """frames: [(pts, access_unit, rai)] in decode order; PAT/PMT before every 12th frame."""
    out = []
    cc = {"pat": 0, "pmt": 0, "v": 0}
    for i, (pts, au, rai) in enumerate(frames):
        if i % 12 == 0:
            out.append(fx.pat(cc=cc["pat"]))
            out.append(fx.pmt(video_type=video_type, audio_pids=(), cc=cc["pmt"]))
            cc["pat"] = (cc["pat"] + 1) & 0x0F
            cc["pmt"] = (cc["pmt"] + 1) & 0x0F
        pkts, cc["v"] = fx.pes_packets(
            fx.VIDEO_PID, 0xE0, au, pts, rai=rai, cc_start=cc["v"], pcr=pts
        )
        out.extend(pkts)
    return b"".join(out)


def hevc_au(key):
    if key:
        nals = [(32, 20), (33, 20), (34, 8), (19, 300)]  # VPS, SPS, PPS, IDR_W_RADL
    else:
        nals = [(1, 350)]  # TRAIL_R
    return b"".join(b"\x00\x00\x00\x01" + bytes([t << 1, 1]) + b"\x55" * n for t, n in nals)


def mpeg2_au(key):
    picture = b"\x00\x00\x01\x00" + b"\x55" * 300
    return (b"\x00\x00\x01\xb3" + b"\x55" * 40 if key else b"") + picture


# --- tests ------------------------------------------------------------------------------------


class PtsDeltaTests(unittest.TestCase):
    def test_shortest_signed_difference_modulo_2_33(self):
        self.assertEqual(pts_delta(100, 400), 300)
        self.assertEqual(pts_delta(400, 100), -300)
        self.assertEqual(pts_delta(2**33 - 1, 0), 1)
        self.assertEqual(pts_delta(0, 2**33 - 1), -1)
        self.assertEqual(pts_delta(2**33 - 90000, 90000), 180000)


class SegmenterTests(unittest.TestCase):
    # 1
    def test_ten_second_stream_cuts_every_two_seconds_from_the_first_keyframe(self):
        data = fx.make_stream(10, gop_s=1.0)
        seg, events = run(data)
        self.assertIsInstance(events[0], Opened)
        o = opens(events)
        self.assertEqual([e.seq for e in o], [0, 1, 2, 3, 4])
        self.assertEqual([e.first_pts for e in o], [900000 + k * TARGET for k in range(5)])
        self.assertEqual(deltas(events), [TARGET] * 4)
        self.assertFalse(any(e.discontinuity for e in o))
        first = pusi_offset(data, fx.VIDEO_PID, 900000)
        self.assertEqual(payload(events), data[first:])
        # Closed carries the next segment's first PTS; the last one closes at finish().
        c = closes(events)
        self.assertEqual([e.seq for e in c], [0, 1, 2, 3, 4])
        self.assertEqual([e.next_first_pts for e in c[:-1]], [e.first_pts for e in o[1:]])
        self.assertEqual(seg.video_pid, fx.VIDEO_PID)
        for opened, body in segments(events):
            start = pusi_offset(data, fx.VIDEO_PID, opened.first_pts)
            before = split_packets(data[:start])
            pat = [p for p in before if pid_of(p) == 0][-1]
            pmt = [p for p in before if pid_of(p) == fx.PMT_PID][-1]
            self.assertEqual(opened.header, pat + pmt)
            head = body[:PACKET]
            self.assertEqual(pid_of(head), fx.VIDEO_PID)
            self.assertTrue(head[1] & 0x40 and head[3] & 0x20 and head[5] & 0x40)  # PUSI + RAI

    # 2
    def test_audio_only_cuts_about_every_two_seconds(self):
        out = []
        cc = {"pat": 0, "pmt": 0, "a": 0}
        for j in range(10 * CLOCK // fx.AUDIO_FRAME_TICKS):
            if j % 24 == 0:
                out.append(fx.pat(cc=cc["pat"]))
                pkts, cc["pmt"] = psi_packets(
                    fx.PMT_PID,
                    pmt_section([(0x0F, fx.AUDIO_PID)], pcr_pid=fx.AUDIO_PID),
                    cc["pmt"],
                )
                out.extend(pkts)
                cc["pat"] = (cc["pat"] + 1) & 0x0F
            pkts, cc["a"] = fx.pes_packets(
                fx.AUDIO_PID,
                0xC0,
                b"\xff\xf1" + b"\xaa" * 200,
                900000 + j * fx.AUDIO_FRAME_TICKS,
                cc_start=cc["a"],
            )
            out.extend(pkts)
        seg, events = run(b"".join(out))
        self.assertIsNone(seg.video_pid)
        self.assertEqual(opens(events)[0].first_pts, 900000)
        d = deltas(events)
        self.assertEqual(len(d), 4)
        for delta in d:
            self.assertGreaterEqual(delta, TARGET)
            self.assertLess(delta, TARGET + fx.AUDIO_FRAME_TICKS)

    # 3
    def test_without_rai_falls_back_to_idr_after_six_seconds(self):
        data = fx.make_stream(20, rai=False)
        _, events = run(data, chunk=4096)
        o = opens(events)
        self.assertEqual(o[0].first_pts, 900000 + 6 * CLOCK)
        self.assertEqual(deltas(events), [TARGET] * 6)
        # Everything before the first keyframe (audio included) is discarded.
        self.assertEqual(payload(events), data[pusi_offset(data, fx.VIDEO_PID, o[0].first_pts) :])
        for opened, body in segments(events):
            self.assertIn(b"\x00\x00\x01\x67", body[:PACKET])  # the SPS of an IDR access unit

    def test_nal_fallback_detects_hevc_and_mpeg2_keyframes(self):
        for video_type, au in ((0x24, hevc_au), (0x02, mpeg2_au), (0x01, mpeg2_au)):
            with self.subTest(video_type=hex(video_type)):
                frames = [(900000 + i * FRAME, au(i % 25 == 0), False) for i in range(250)]
                _, events = run(video_only(frames, video_type=video_type))
                o = opens(events)
                self.assertEqual(o[0].first_pts, 900000 + 6 * CLOCK)
                self.assertEqual(deltas(events), [TARGET] * 1)

    # 4
    def test_rai_on_every_pes_keeps_the_two_second_minimum(self):
        frames = [(900000 + i * FRAME, fx.h264_access_unit(i % 25 == 0), True) for i in range(250)]
        _, events = run(video_only(frames))
        self.assertEqual(deltas(events), [TARGET] * 4)

    def test_rai_on_every_pes_with_b_frame_reordering_is_not_a_discontinuity(self):
        # Decode order I0 P3 B1 B2 P6 B4 B5 ...: B-frames step back by one or two frames.
        order = [0]
        for k in range(1, 100):
            order += [3 * k, 3 * k - 2, 3 * k - 1]
        frames = [(900000 + n * FRAME, fx.h264_access_unit(n == 0), True) for n in order]
        _, events = run(video_only(frames))
        o = opens(events)
        self.assertGreaterEqual(len(o), 4)
        self.assertFalse(any(e.discontinuity for e in o))
        for delta in deltas(events):
            self.assertGreaterEqual(delta, TARGET)

    # 5
    def test_pts_reset_opens_a_discontinuity(self):
        _, events = run(fx.make_stream(10, resets=(5.0,)))
        o = opens(events)
        self.assertEqual([e.discontinuity for e in o], [False, False, False, True, False, False])
        self.assertEqual(o[3].first_pts, 900000)
        for a, b in zip(o, o[1:]):
            if not b.discontinuity:
                self.assertGreaterEqual(pts_delta(a.first_pts, b.first_pts), TARGET)

    # 6
    def test_pts_wrap_keeps_two_second_segments(self):
        start = 2**33 - CLOCK * 3
        _, events = run(fx.make_stream(10, pts_start=start))
        o = opens(events)
        self.assertEqual(deltas(events), [TARGET] * 4)
        self.assertFalse(any(e.discontinuity for e in o))
        self.assertLess(o[2].first_pts, o[1].first_pts)  # wrapped
        self.assertEqual(o[2].first_pts, (start + 2 * TARGET) & MASK)

    # 7
    def test_192_and_204_byte_packets_are_unsupported(self):
        for size in (192, 204):
            with self.subTest(size=size):
                with self.assertRaises(Unsupported) as raised:
                    run(fx.make_stream(3, packet_size=size), chunk=4096)
                self.assertEqual(str(raised.exception), "unsupported packet size")
                self.assertEqual(raised.exception.reason, "unsupported packet size")

    # 8
    def test_random_bytes_are_not_mpeg_ts(self):
        noise = random.Random(7).randbytes(128 * 1024)
        with self.assertRaises(Unsupported) as raised:
            run(noise, chunk=1000)
        self.assertEqual(str(raised.exception), "not MPEG-TS")

    # 9
    def test_scrambled_video_is_unsupported(self):
        with self.assertRaises(Unsupported) as raised:
            run(fx.make_stream(3, scrambled=True))
        self.assertEqual(str(raised.exception), "scrambled")

    # 10
    def test_gop_longer_than_ten_seconds_is_force_cut_at_ten_seconds(self):
        _, events = run(fx.make_stream(24, gop_s=15.0))
        o = opens(events)
        self.assertEqual([e.first_pts - 900000 for e in o], [0, 10 * CLOCK, 15 * CLOCK])
        self.assertFalse(any(e.discontinuity for e in o))

    # 11
    def test_chunk_boundaries_do_not_change_events(self):
        clean = fx.make_stream(10, resets=(6.0,))
        # 79 junk bytes starting with a stray sync byte: the junk is taken for a packet, and the
        # bytes up to the next proven packet start (three sync bytes 188 apart) are dropped.
        cut = 300 * PACKET
        junk = b"\x47" + b"\x00" * 76 + b"\x47\x47"
        dirty = clean[:cut] + junk + clean[cut:]
        # 1000 bytes with a sync byte every third byte but no three 188 apart: a resync that
        # waits for more input must not take a stray sync byte at the start of its tail.
        noise = b"\x47\x00\x00" * 333 + b"\x47"
        noisy = clean[:cut] + noise + clean[cut:]
        first = pusi_offset(clean, fx.VIDEO_PID, 900000)
        kept = {
            "clean": clean[first:],
            "dirty": dirty[first : cut + PACKET] + dirty[cut + len(junk) + PACKET :],
            "noisy": noisy[first : cut + PACKET] + noisy[cut + len(noise) :],
        }
        for name, data in (("clean", clean), ("dirty", dirty), ("noisy", noisy)):
            with self.subTest(stream=name):
                _, whole = run(data)
                expected = merged(whole)
                self.assertGreater(len(opens(expected)), 3)
                self.assertEqual(payload(expected), kept[name])
                for chunk in (1, 187, 100 * 1024):
                    _, events = run(data, chunk=chunk)
                    self.assertEqual(merged(events), expected, chunk)

    # 12
    def test_finish_closes_the_last_segment(self):
        seg = Segmenter(clock=lambda: 5.0)
        self.assertEqual(seg.finish(), [])
        events = seg.feed(fx.make_stream(5))
        last = opens(events)[-1]
        self.assertEqual(last.seq, 2)
        self.assertEqual(seg.finish(), [Closed(2, seg.latest_pts, 5.0)])
        self.assertEqual(seg.latest_pts, 900000 + 124 * FRAME)
        self.assertEqual(seg.finish(), [])
        # Input after finish() starts a new segment, numbered on, across a discontinuity.
        more = opens(seg.feed(fx.make_stream(1, pts_start=0)))
        self.assertEqual([(e.seq, e.first_pts, e.discontinuity) for e in more], [(3, 0, True)])

    # 13
    def test_pmt_spanning_two_packets_is_parsed(self):
        info = b"".join(bytes([0x0A, 4]) + b"eng\x00" for _ in range(32))  # 192 bytes
        section = pmt_section([(0x1B, fx.VIDEO_PID), (0x0F, fx.AUDIO_PID)], info=info)
        self.assertGreater(len(section), 183)
        data = replace_pid_packets(
            fx.make_stream(6), fx.PMT_PID, lambda cc: psi_packets(fx.PMT_PID, section, cc)
        )
        seg, events = run(data)
        self.assertEqual(seg.video_pid, fx.VIDEO_PID)
        o = opens(events)
        self.assertEqual(deltas(events), [TARGET] * 2)
        self.assertEqual(len(o[0].header), 3 * PACKET)
        self.assertEqual([pid_of(p) for p in split_packets(o[0].header)], [0, 0x1000, 0x1000])

    def test_pat_with_a_network_entry_spanning_two_packets_is_parsed(self):
        programs = [(0, 0x10), (1, fx.PMT_PID)] + [(n, 0x1100 + n) for n in range(2, 50)]
        section = pat_section(programs)
        self.assertGreater(len(section), 183)
        data = replace_pid_packets(fx.make_stream(6), 0, lambda cc: psi_packets(0, section, cc))
        seg, events = run(data)
        self.assertEqual(seg.video_pid, fx.VIDEO_PID)
        self.assertEqual(deltas(events), [TARGET] * 2)
        self.assertEqual(
            [pid_of(p) for p in split_packets(opens(events)[0].header)], [0, 0, 0x1000]
        )

    def test_pmt_change_moves_cuts_to_the_new_video_pid(self):
        first = fx.make_stream(6)
        second = remap_pid(fx.make_stream(6, pts_start=900000 + 6 * CLOCK), fx.VIDEO_PID, 0x200)
        second = replace_pid_packets(
            second,
            fx.PMT_PID,
            lambda cc: ([fx.pmt(video_pid=0x200, cc=cc)], (cc + 1) & 0x0F),
        )
        seg, events = run(first + second)
        self.assertEqual(seg.video_pid, 0x200)
        o = opens(events)
        self.assertEqual(deltas(events), [TARGET] * 5)
        self.assertFalse(any(e.discontinuity for e in o))
        for opened, body in segments(events)[3:]:
            self.assertEqual(pid_of(body[:PACKET]), 0x200)
            pmt = opened.header[PACKET:]
            self.assertEqual(pmt, fx.pmt(video_pid=0x200, cc=pmt[3] & 0x0F))

    def test_discontinuity_marks_the_next_cut(self):
        seg = Segmenter(clock=lambda: 0.0)
        events = seg.feed(fx.make_stream(3))
        seg.discontinuity()
        events += seg.feed(fx.make_stream(3, pts_start=900000 + 3 * CLOCK))
        o = opens(events)
        self.assertEqual([e.first_pts - 900000 for e in o], [0, 2 * CLOCK, 3 * CLOCK, 5 * CLOCK])
        self.assertEqual([e.discontinuity for e in o], [False, False, True, False])

    def test_no_program_tables_within_3_mib_is_unsupported(self):
        null = bytes([0x47, 0x1F, 0xFF, 0x10]) + b"\xff" * 184
        seg = Segmenter(clock=lambda: 0.0)
        seg.feed(null * (3 * 1024 * 1024 // PACKET - 2))
        with self.assertRaises(Unsupported) as raised:
            seg.feed(null * 4)
        self.assertEqual(str(raised.exception), "no program tables")

    def test_malformed_tables_and_pes_headers_are_skipped(self):
        clean = fx.make_stream(6)
        stream = split_packets(clean)
        overrun = pmt_section([(0x1B, fx.VIDEO_PID)])
        overrun = overrun[:-2] + b"\xf3\xff"  # ES_info_length runs past the section end
        junk = [
            fx._packet(0, b"\xb8" + b"\x00" * 183, 0, pusi=True),  # pointer field past the packet
            fx._packet(0, b"", 1, pusi=True, af=b"\x00"),  # adaptation field only
            fx._packet(fx.PMT_PID, b"\x00\x02\xbf\xff" + b"\xff" * 180, 0, pusi=True),  # 4 KiB
            psi_packets(fx.PMT_PID, overrun, 1)[0][0],
            bytes([0x47, 0x41, 0x00, 0x30, 200]) + b"\x00" * 183,  # adaptation field too long
            bytes([0x47, 0x41, 0x00, 0x20, 183]) + b"\x00" * 183,  # no payload
            bytes([0x47, 0x41, 0x00, 0x10])  # PES header length past the packet
            + b"\x00\x00\x01\xe0\x00\x00\x80\x80\xff"
            + b"\x00" * 175,
            bytes([0x47, 0xC1, 0x00, 0x90]) + b"\x00" * 184,  # transport error, "scrambled" bits
        ]
        # A PMT moving the video to PID 0x300 right before the 4 s keyframe, with a CRC that does
        # not match, is ignored (accepting it would lose the cut at 4 s).
        corrupt = bytearray(fx.pmt(video_pid=0x300, cc=7))
        corrupt[30] ^= 0xFF  # inside the CRC field
        key = pusi_offset(clean, fx.VIDEO_PID, 900000 + 2 * TARGET) // PACKET
        data = stream[:2] + junk + stream[2:key] + [bytes(corrupt)] + stream[key:]
        seg, events = run(b"".join(data))
        self.assertEqual(seg.video_pid, fx.VIDEO_PID)
        self.assertEqual(
            [e.first_pts for e in opens(events)], [900000 + k * TARGET for k in range(3)]
        )

    def test_corrupted_input_raises_nothing_but_unsupported(self):
        clean = fx.make_stream(3)
        for seed in range(40):
            rng = random.Random(seed)
            data = bytearray(clean)
            for _ in range(400):
                i = rng.randrange(len(data))
                if i % PACKET:  # keep sync bytes so the corruption reaches the parsers
                    data[i] = rng.randrange(256)
            if seed % 4 == 0:  # truncate a few packets as well
                for _ in range(5):
                    i = rng.randrange(len(data) - PACKET)
                    del data[i : i + rng.randrange(1, PACKET)]
            seg = Segmenter(clock=lambda: 0.0)
            try:
                pos = 0
                while pos < len(data):
                    step = rng.randrange(1, 5000)
                    seg.feed(bytes(data[pos : pos + step]))
                    pos += step
                seg.finish()
            except Unsupported:
                pass


@unittest.skipUnless(os.environ.get("QITV_BENCH") == "1", "set QITV_BENCH=1 to benchmark")
class SegmenterBenchmark(unittest.TestCase):
    def test_throughput_on_a_minute_of_1080p_sized_access_units(self):
        with mock.patch.object(fx, "FRAME_BYTES", 40_000):  # about 8 Mb/s at 25 fps
            data = fx.make_stream(60)
        seg = Segmenter()
        step = 64 * 1024
        started = time.process_time()
        for i in range(0, len(data), step):
            seg.feed(data[i : i + step])
        seg.finish()
        cpu = time.process_time() - started
        mb = len(data) / 1e6
        rate = mb / cpu
        mbit = len(data) * 8 / 60 / 1e6
        sys.stderr.write(
            f"\nsegmenter: {mb:.1f} MB ({mbit:.1f} Mb/s stream) in {cpu:.3f} s CPU"
            f" = {rate:.0f} MB/s; {100 / rate:.2f}% of a core at 8 Mb/s,"
            f" {312.5 / rate:.2f}% at 25 Mb/s\n"
        )


if __name__ == "__main__":
    unittest.main()
