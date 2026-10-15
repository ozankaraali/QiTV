"""Tests for the MPEG-TS scanner and keyframe segmenter (services/live/ts.py).

Run with `uv run python -m unittest discover -s tests -p test_ts_segmenter.py` (the module-path
form cannot import `ts_fixtures`).
"""

import os
import random
import sys
import time
import unittest
from unittest import mock

import ts_fixtures as fx

from services.live.ts import (
    PACKET,
    Closed,
    Data,
    Opened,
    Segmenter,
    Unsupported,
    _crc32,
    _h264_intra,
    pts_delta,
)

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


def mpeg2_au(key, coding_type=None):
    """A sequence header and an I picture when `key`, else a P picture (or `coding_type`)."""
    coding_type = coding_type or (1 if key else 2)  # picture_coding_type: 1 I, 2 P, 3 B
    picture = b"\x00\x00\x01\x00\x01" + bytes([coding_type << 3]) + b"\x55" * 300
    return (b"\x00\x00\x01\xb3" + b"\x55" * 40 if key else b"") + picture


def h264_slice_at_packet_end(intra):
    """The NAL 1 header byte is the last byte of the PES's first packet.

    With the fixture's PCR and PTS headers the ES starts at packet byte 26: AUD (6 bytes) and
    an SEI (152 bytes) put the slice's start code at ES 158 and its NAL header at byte 187.
    """
    nal = b"\x00\x00\x00\x01"
    au = nal + b"\x09\xf0" + nal + b"\x06" + b"\x55" * 147 + b"\x00\x00\x01\x41"
    return au + (b"\x88" if intra else b"\x9a") + b"\x55" * 300


def exp_golomb(value):
    value += 1
    return "0" * (value.bit_length() - 1) + format(value, "b")


def with_subtitles(data, ahead):
    """Add a subtitle PID after every PMT, its PTS `ahead` ticks ahead of the latest audio."""
    out, cc, audio = [], 0, None
    for p in split_packets(data):
        out.append(p)
        if pid_of(p) == fx.AUDIO_PID and p[1] & 0x40:
            audio = pusi_pts(p)
        if pid_of(p) == fx.PMT_PID and audio is not None:
            pkts, cc = fx.pes_packets(0x102, 0xBD, b"\x20" * 40, audio + ahead, cc_start=cc)
            out.extend(pkts)
    return b"".join(out)


def escaped(raw):
    """Insert H.264 emulation prevention bytes."""
    out = bytearray()
    zeros = 0
    for b in raw:
        if zeros >= 2 and b <= 3:
            out.append(3)
            zeros = 0
        out.append(b)
        zeros = zeros + 1 if b == 0 else 0
    return bytes(out)


def h264_au_with_sps(idr):
    """An access unit that repeats SPS and PPS on every frame, IDR or not."""
    nals = [(9, 6), (7, 20), (8, 8), (5 if idr else 1, 300)]
    return b"".join(b"\x00\x00\x00\x01" + bytes([0x60 | t]) + b"\x55" * (n - 5) for t, n in nals)


def hevc_au_with_vps(irap):
    """An access unit that repeats VPS, SPS and PPS on every frame, IRAP or not."""
    nals = [(32, 20), (33, 20), (34, 8), (19 if irap else 1, 300)]
    return b"".join(b"\x00\x00\x00\x01" + bytes([t << 1, 1]) + b"\x55" * n for t, n in nals)


def h264_open_gop_au(intra):
    """Open-GOP H.264: keyframes are non-IDR (NAL 1) intra slices after SPS and PPS."""
    nal = b"\x00\x00\x00\x01"
    head = nal + b"\x09\xf0"
    if intra:  # slice header 0x88: first_mb_in_slice 0, slice_type 7 (I)
        head += nal + b"\x67" + b"\x55" * 15 + nal + b"\x68" + b"\x55" * 3
    return head + nal + b"\x41" + (b"\x88" if intra else b"\x9a") + b"\x55" * 300  # 0x9a: P


def au_with_sei(video_type, key):
    """An access unit whose slice (picture) starts beyond the first packet, after 200 bytes of
    SEI (user data for MPEG-2). Keyframes carry SPS, VPS or a sequence header before it.
    """
    if video_type == 0x02:  # sequence header (keyframes) and user data before the picture
        head = b"\x00\x00\x01\xb3" + b"\x55" * 40 if key else b""
        return head + b"\x00\x00\x01\xb2" + b"\x55" * 200 + mpeg2_au(False, 1 if key else 2)
    if video_type == 0x1B:
        nals = ([(7, 20), (8, 8)] if key else []) + [(6, 200), (5 if key else 1, 300)]
        return b"".join(b"\x00\x00\x00\x01" + bytes([0x60 | t]) + b"\x55" * n for t, n in nals)
    nals = ([(32, 20), (33, 20), (34, 8)] if key else []) + [(39, 200), (19 if key else 1, 300)]
    return b"".join(b"\x00\x00\x00\x01" + bytes([t << 1, 1]) + b"\x55" * n for t, n in nals)


def pes_starts(data, pids=(fx.VIDEO_PID, fx.AUDIO_PID)):
    """[(offset, pts or None)] of the PES starts on `pids`."""
    out = []
    for i, p in enumerate(split_packets(data)):
        if pid_of(p) in pids and p[1] & 0x40:
            start = 4 + (1 + p[4] if p[3] & 0x20 else 0)
            out.append((i * PACKET, pusi_pts(p) if p[start + 7] & 0x80 else None))
    return out


def by_pts(data, base=900000):
    """Feed points at every PES start, with the wall clock at its PTS (live pacing)."""
    return [(off, (pts - base) / CLOCK) for off, pts in pes_starts(data)]


def by_frame(data, fps=25):
    """Feed points at every video PES start, with the wall clock at frame index / fps."""
    return [(off, k / fps) for k, (off, _) in enumerate(pes_starts(data, (fx.VIDEO_PID,)))]


def run_at(data, points, **kwargs):
    """Feed `data` in pieces that start at each (offset, wall) point, the clock at `wall`."""
    now = [0.0]
    seg = Segmenter(clock=lambda: now[0], **kwargs)
    events = []
    bounds = [0] + [off for off, _ in points] + [len(data)]
    walls = [0.0] + [wall for _, wall in points]
    for k in range(len(bounds) - 1):
        now[0] = walls[k]
        events += seg.feed(data[bounds[k] : bounds[k + 1]])
    events += seg.finish()
    return seg, events


def shift_pts(data, from_frame, offset, pids=(fx.VIDEO_PID, fx.AUDIO_PID)):
    """Add `offset` ticks to the PES timestamps on `pids` from video frame `from_frame` on."""
    out = bytearray(data)
    frame = -1
    for i, p in enumerate(split_packets(data)):
        pid = pid_of(p)
        if not p[1] & 0x40 or pid not in (fx.VIDEO_PID, fx.AUDIO_PID):
            continue
        frame += pid == fx.VIDEO_PID
        if frame >= from_frame and pid in pids:
            at = i * PACKET + 4 + (1 + p[4] if p[3] & 0x20 else 0) + 9
            out[at : at + 5] = fx._pts_bytes(pusi_pts(p) + offset)
    return bytes(out)


def starts_with_idr(body):
    return b"\x00\x00\x01\x65" in body[:PACKET]


def frame_of(pts, base=900000):
    return pts_delta(base, pts) // FRAME


# --- tests ------------------------------------------------------------------------------------


class PtsDeltaTests(unittest.TestCase):
    def test_shortest_signed_difference_modulo_2_33(self):
        self.assertEqual(pts_delta(100, 400), 300)
        self.assertEqual(pts_delta(400, 100), -300)
        self.assertEqual(pts_delta(2**33 - 1, 0), 1)
        self.assertEqual(pts_delta(0, 2**33 - 1), -1)
        self.assertEqual(pts_delta(2**33 - 90000, 90000), 180000)


class CrcTests(unittest.TestCase):
    def test_the_section_crc_matches_the_mpeg2_check_value(self):
        self.assertEqual(_crc32(b"123456789"), 0x0376E6E7)


class SegmenterTests(unittest.TestCase):
    def assert_closed_pts(self, events):
        """A Closed carries the next first PTS; before a discontinuity it may be None (a jump
        must not be credited to the closing segment)."""
        nxt = {e.seq: e for e in opens(events)}
        for c in closes(events)[:-1]:
            o = nxt[c.seq + 1]
            allowed = (None, o.first_pts) if o.discontinuity else (o.first_pts,)
            self.assertIn(c.next_first_pts, allowed)

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
        self.assert_closed_pts(events)
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
    def test_without_rai_cuts_at_idr_access_units_from_the_start(self):
        whole = fx.make_stream(20, rai=False)
        # Join mid-GOP: from the PAT at 0.5 s, so frames 13-24 and their audio come first.
        join = [i * PACKET for i, p in enumerate(split_packets(whole)) if pid_of(p) == 0][1]
        data = whole[join:]
        first = pusi_offset(data, fx.VIDEO_PID, 900000 + 25 * FRAME)
        self.assertIn(fx.AUDIO_PID, [pid_of(p) for p in split_packets(data[:first])])
        _, events = run(data, chunk=4096)
        o = opens(events)
        self.assertEqual(o[0].first_pts, 900000 + CLOCK)
        self.assertEqual(deltas(events), [TARGET] * 9)
        # Everything before the first keyframe (audio included) is discarded.
        self.assertEqual(payload(events), data[first:])
        for opened, body in segments(events):
            self.assertTrue(starts_with_idr(body))

    def test_hevc_and_mpeg2_keyframes_are_detected_without_rai(self):
        for video_type, au in ((0x24, hevc_au), (0x02, mpeg2_au), (0x01, mpeg2_au)):
            with self.subTest(video_type=hex(video_type)):
                frames = [(900000 + i * FRAME, au(i % 25 == 0), False) for i in range(250)]
                _, events = run(video_only(frames, video_type=video_type))
                self.assertEqual(opens(events)[0].first_pts, 900000)
                self.assertEqual(deltas(events), [TARGET] * 4)

    def test_a_parameter_set_without_a_slice_in_the_first_packet_is_a_keyframe(self):
        for video_type in (0x1B, 0x24, 0x02):
            with self.subTest(video_type=hex(video_type)):
                frames = [
                    (900000 + i * FRAME, au_with_sei(video_type, i % 25 == 0), False)
                    for i in range(250)
                ]
                _, events = run(video_only(frames, video_type=video_type))
                self.assertEqual(deltas(events), [TARGET] * 4)

    def test_open_gop_intra_frames_are_keyframes_with_or_without_rai(self):
        for rai in (True, False):
            with self.subTest(rai=rai):
                frames = [
                    (900000 + i * FRAME, h264_open_gop_au(i % 25 == 0), rai and i % 25 == 0)
                    for i in range(250)
                ]
                _, events = run(video_only(frames))
                self.assertEqual(deltas(events), [TARGET] * 4)

    def test_parameter_sets_on_every_frame_do_not_make_keyframes(self):
        # No RAI, SPS (H.264) or VPS (HEVC) before every frame, an IDR/IRAP every 1.2 s.
        for video_type, au in ((0x1B, h264_au_with_sps), (0x24, hevc_au_with_vps)):
            with self.subTest(video_type=hex(video_type)):
                frames = [(900000 + i * FRAME, au(i % 30 == 0), False) for i in range(300)]
                _, events = run(video_only(frames, video_type=video_type))
                starts = [frame_of(e.first_pts) for e in opens(events)]
                self.assertEqual(starts, [0, 60, 120, 180, 240])

    # 4
    def test_rai_on_every_pes_cuts_only_idr_frames_at_least_two_seconds_apart(self):
        frames = [(900000 + i * FRAME, fx.h264_access_unit(i % 30 == 0), True) for i in range(300)]
        _, events = run(video_only(frames))
        self.assertEqual([frame_of(e.first_pts) for e in opens(events)], [0, 60, 120, 180, 240])
        for opened, body in segments(events):
            self.assertTrue(starts_with_idr(body))

    def test_once_rai_is_seen_unflagged_idr_frames_are_not_cut_points(self):
        # IDR every 0.8 s, RAI only on every other one (every 1.6 s).
        frames = [
            (900000 + i * FRAME, fx.h264_access_unit(i % 20 == 0), i % 40 == 0) for i in range(250)
        ]
        _, events = run(video_only(frames))
        self.assertEqual([frame_of(e.first_pts) for e in opens(events)], [0, 80, 160, 240])

    def test_joining_mid_gop_with_rai_on_every_pes_starts_at_an_idr(self):
        frames = [(900000 + i * FRAME, fx.h264_access_unit(i % 30 == 0), True) for i in range(300)]
        data = video_only(frames)
        join = [i * PACKET for i, p in enumerate(split_packets(data)) if pid_of(p) == 0][3]  # f36
        _, events = run(data[join:])
        self.assertEqual([frame_of(e.first_pts) for e in opens(events)], [60, 120, 180, 240])
        self.assertTrue(starts_with_idr(segments(events)[0][1]))

    def test_mpeg2_with_rai_on_every_pes_cuts_only_on_i_pictures(self):
        def au(i):  # an I picture every 1.2 s, P and B pictures between
            return mpeg2_au(True) if i % 30 == 0 else mpeg2_au(False, 3 if i % 3 else 2)

        for video_type in (0x02, 0x01):
            with self.subTest(video_type=hex(video_type)):
                frames = [(900000 + i * FRAME, au(i), True) for i in range(300)]
                _, events = run(video_only(frames, video_type=video_type))
                starts = [frame_of(e.first_pts) for e in opens(events)]
                self.assertEqual(starts, [0, 60, 120, 180, 240])

    def test_a_slice_header_cut_off_by_the_packet_end_is_undecided(self):
        # RAI on keyframes only; every slice header starts in the PES's second packet, so the
        # scan is inconclusive and the RAI is trusted.
        frames = [
            (900000 + i * FRAME, h264_slice_at_packet_end(i % 25 == 0), i % 25 == 0)
            for i in range(250)
        ]
        data = video_only(frames)
        first = split_packets(data)[pusi_offset(data, fx.VIDEO_PID, 900000) // PACKET]
        self.assertEqual(first[184:], b"\x00\x00\x01\x41")
        _, events = run(data)
        self.assertEqual(deltas(events), [TARGET] * 4)

    def test_h264_slice_types_are_read_through_emulation_prevention(self):
        # Up to the largest legal first_mb_in_slice (level 6.2: 139264 macroblocks), with a
        # zero-heavy tail so that emulation prevention bytes follow the two codes.
        for first_mb in (0, 1, 30, 1000, 8191, 32399, 139263):
            for slice_type in range(10):
                bits = exp_golomb(first_mb) + exp_golomb(slice_type) + "1" + "0" * 40
                bits += "0" * (-len(bits) % 8)
                header = escaped(int(bits, 2).to_bytes(len(bits) // 8, "big"))
                got = _h264_intra(header, 0)
                self.assertEqual(got, slice_type % 5 in (2, 4), (first_mb, slice_type))
                # Cut off before the codes end: undecided, never a guess.
                need = (len(exp_golomb(first_mb)) + len(exp_golomb(slice_type)) + 7) // 8
                self.assertIsNone(_h264_intra(header[: need - 1], 0), (first_mb, slice_type))
        self.assertIsNone(_h264_intra(b"", 0))
        for slice_type in (10, 11, 12, 30):  # not a slice type: undecided
            bits = "1" + exp_golomb(slice_type) + "1"
            bits += "0" * (-len(bits) % 8)
            self.assertIsNone(_h264_intra(int(bits, 2).to_bytes(len(bits) // 8, "big"), 0))

    def test_content_without_rai_after_a_discontinuity_is_cut_at_its_idr_frames(self):
        # A RAI stream, then content without RAI (an ad from another packager): after a PTS
        # jump, or after an external marker with continuous PTS.
        for name, start, marker in (("PTS jump", 66, False), ("marker", 6, True)):
            with self.subTest(name):
                seg = Segmenter(clock=lambda: 0.0)
                events = seg.feed(fx.make_stream(6))
                if marker:
                    seg.discontinuity()
                after = fx.make_stream(10, rai=False, pts_start=900000 + start * CLOCK)
                events += seg.feed(after) + seg.finish()
                o = opens(events)
                expected = [0, 2, 4] + [start + 2 * k for k in range(5)]
                self.assertEqual([e.first_pts - 900000 for e in o], [t * CLOCK for t in expected])
                self.assertEqual([e.discontinuity for e in o], [k == 3 for k in range(8)])
                for opened, body in segments(events):
                    self.assertTrue(starts_with_idr(body))
                # A marker with continuous PTS keeps the exact end; a jump does not.
                self.assertEqual(
                    closes(events)[2].next_first_pts, o[3].first_pts if marker else None
                )
                self.assert_closed_pts(events)

    def test_rai_on_every_pes_with_b_frame_reordering_is_not_a_discontinuity(self):
        # Decode order I0 P3 B1 B2 P6 B4 B5 ...: B-frames step back by one or two frames, and
        # the leading B-frames after each IDR (every 1.2 s) step back from the cut point.
        order = [0]
        for k in range(1, 100):
            order += [3 * k, 3 * k - 2, 3 * k - 1]
        frames = [(900000 + n * FRAME, fx.h264_access_unit(n % 30 == 0), True) for n in order]
        _, events = run(video_only(frames))
        o = opens(events)
        self.assertEqual([frame_of(e.first_pts) for e in o], [0, 60, 120, 180, 240])
        self.assertFalse(any(e.discontinuity for e in o))

    # 5
    def test_pts_reset_opens_a_discontinuity(self):
        _, events = run(fx.make_stream(10, resets=(5.0,)))
        o = opens(events)
        self.assertEqual([e.discontinuity for e in o], [False, False, False, True, False, False])
        self.assertEqual(o[3].first_pts, 900000)
        for a, b in zip(o, o[1:]):
            if not b.discontinuity:
                self.assertGreaterEqual(pts_delta(a.first_pts, b.first_pts), TARGET)
        self.assertIsNone(closes(events)[2].next_first_pts)
        self.assert_closed_pts(events)

    def test_timestamp_jumps_are_flagged_at_the_next_keyframe(self):
        # A step at frame 120 (4.8 s, mid-GOP; keyframes every 25 frames) is remembered and
        # cut at the keyframe at frame 125 with a discontinuity, never at a P-frame. (At 4.8 s
        # the -1.5 s step leaves the PTS within 1 s of the segment start at 4.0 s.)
        for name, step in (("+60 s", 60 * CLOCK), ("+5 s", 5 * CLOCK), ("-1.5 s", -135000)):
            with self.subTest(step=name):
                data = shift_pts(fx.make_stream(12), 120, step)
                _, events = run(data)
                o = opens(events)
                self.assertEqual([e.discontinuity for e in o].count(True), 1)
                self.assertTrue(o[3].discontinuity)
                self.assertEqual(o[3].first_pts, (900000 + 125 * FRAME + step) & MASK)
                for opened, body in segments(events):
                    self.assertTrue(starts_with_idr(body))
                for a, b in zip(o, o[1:]):
                    if not b.discontinuity:
                        self.assertEqual(pts_delta(a.first_pts, b.first_pts), TARGET)
                # The segment before the jump closes without the jumped PTS (1 s of media,
                # not 1 s plus the jump).
                self.assertIsNone(closes(events)[2].next_first_pts)
                self.assert_closed_pts(events)

    def test_an_outage_that_resumes_mid_gop_cuts_at_the_next_keyframe(self):
        # 10 s missing from 7.52 s (mid-GOP) to 17.52 s (mid-GOP); the wall clock and the PTS
        # both advance by 10 s, as after a reconnect to a live channel.
        whole = fx.make_stream(30)
        gone = pusi_offset(whole, fx.VIDEO_PID, 900000 + 188 * FRAME)
        back = pusi_offset(whole, fx.VIDEO_PID, 900000 + 438 * FRAME)
        data = whole[:gone] + whole[back:]
        _, events = run_at(data, by_pts(data))
        o = opens(events)
        starts = [frame_of(e.first_pts) for e in o]
        self.assertEqual(starts, [0, 50, 100, 150, 450, 500, 550, 600, 650, 700])
        self.assertEqual([e.discontinuity for e in o], [k == 4 for k in range(10)])
        for opened, body in segments(events):
            self.assertTrue(starts_with_idr(body))
        self.assert_closed_pts(events)

    def test_rai_is_relearned_after_every_discontinuity_cut(self):
        # Video without PTS (every cut is a discontinuity): 5 s with RAI on its IDR frames,
        # then 5 s from a packager without RAI. Its IDR frames are still cut points.
        frames = [
            (None, fx.h264_access_unit(i % 25 == 0), i % 25 == 0 and i < 125) for i in range(250)
        ]
        data = video_only(frames)
        _, events = run_at(data, by_frame(data))
        self.assertEqual([e.wall for e in opens(events)], [0.0, 2.0, 4.0, 6.0, 8.0])

    def test_a_pending_segment_is_bounded_in_media_time_under_bursty_feeding(self):
        # One keyframe only; PTS jumps +60 s at frame 112. HLS-like 6 s bursts pause the
        # feeds, which do not age the segment; 10 s of post-jump media still cut it.
        data = shift_pts(fx.make_stream(40, gop_s=30.0), 112, 60 * CLOCK)
        points = [(off, (int(t // 6) + 1) * 6.0) for off, t in by_frame(data)]
        _, events = run_at(data, points)
        o = opens(events)
        self.assertTrue(o[1].discontinuity)
        self.assertEqual(o[1].first_pts, 900000 + 362 * FRAME + 60 * CLOCK)
        self.assertIsNone(closes(events)[0].next_first_pts)

    def test_a_pending_discontinuity_is_force_cut_by_wall_time_only(self):
        # One keyframe only; PTS jumps +60 s at frame 112. The PTS delta must not cut there;
        # the wall clock does, at 10 s.
        data = shift_pts(fx.make_stream(20, gop_s=30.0), 112, 60 * CLOCK)
        _, events = run_at(data, by_frame(data))
        o = opens(events)
        self.assertEqual([(e.wall, e.discontinuity) for e in o], [(0.0, False), (10.0, True)])
        self.assertEqual(o[1].first_pts, 900000 + 250 * FRAME + 60 * CLOCK)

    def test_video_without_pts_keeps_two_second_segments_by_wall_clock(self):
        frames = [(None, fx.h264_access_unit(i % 25 == 0), i % 25 == 0) for i in range(250)]
        data = video_only(frames)
        _, events = run_at(data, by_frame(data))
        o = opens(events)
        self.assertEqual([e.wall for e in o], [0.0, 2.0, 4.0, 6.0, 8.0])
        self.assertEqual([e.discontinuity for e in o], [False, True, True, True, True])
        self.assertTrue(all(e.first_pts is None for e in o))

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

    def test_isolated_scrambling_bits_are_ignored(self):
        clean = fx.make_stream(6)
        points = by_pts(clean)
        wall = {}  # packet index -> wall time of the PES start before it
        for k, (off, t) in enumerate(points):
            end = points[k + 1][0] if k + 1 < len(points) else len(clean)
            for i in range(off // PACKET, end // PACKET):
                wall[i] = t
        video = [i for i, p in enumerate(split_packets(clean)) if pid_of(p) == fx.VIDEO_PID]
        early = [i for i in video if wall.get(i, 0) < 1.9 and not clean[i * PACKET + 1] & 0x40]
        late = [i for i in video if 4.2 < wall.get(i, 0) < 4.9]  # no keyframe in between
        cases = {
            "7 in a row later": (late[:7], False),
            "8 in a row later": (late[:8], True),
            "8 apart later": (late[::3][:8], False),
            "8 apart in the first 2 s": (early[::3][:8], True),
        }
        # The PES header of a scrambled packet is unreadable, so even an isolated scrambled
        # keyframe start is not a cut point: the cut at 2 s moves to the keyframe at 3 s.
        key = pusi_offset(clean, fx.VIDEO_PID, 900000 + 50 * FRAME) // PACKET
        cases["a scrambled keyframe start"] = ([key], False)
        for name, (marked, raises) in cases.items():
            with self.subTest(name):
                data = bytearray(clean)
                for i in marked:
                    data[i * PACKET + 3] |= 0x80
                if raises:
                    with self.assertRaises(Unsupported) as raised:
                        run_at(bytes(data), points)
                    self.assertEqual(str(raised.exception), "scrambled")
                else:
                    _, events = run_at(bytes(data), points)
                    expected = [3 * CLOCK, TARGET] if key in marked else [TARGET] * 2
                    self.assertEqual(deltas(events), expected)

    # 10
    def test_gop_longer_than_ten_seconds_is_force_cut_at_ten_seconds(self):
        _, events = run(fx.make_stream(24, gop_s=15.0))
        o = opens(events)
        self.assertEqual([e.first_pts - 900000 for e in o], [0, 10 * CLOCK, 15 * CLOCK])
        self.assertFalse(any(e.discontinuity for e in o))

    def test_a_silent_video_pid_is_cut_at_the_next_pes_of_any_pid(self):
        # The video stops at 6 s while audio and tables continue to 20 s.
        whole = fx.make_stream(20)
        stop = pusi_offset(whole, fx.VIDEO_PID, 900000 + 150 * FRAME)
        kept = [p for p in split_packets(whole[stop:]) if pid_of(p) != fx.VIDEO_PID]
        data = whole[:stop] + b"".join(kept)
        _, events = run_at(data, by_pts(data))
        o = opens(events)
        self.assertEqual(len(o), 4)
        last = o[3]
        self.assertIsNone(last.first_pts)
        self.assertTrue(last.discontinuity)
        self.assertGreaterEqual(last.wall, 149 * FRAME / CLOCK + 10)
        self.assertLess(last.wall, 149 * FRAME / CLOCK + 10.05)
        head = segments(events)[3][1][:PACKET]
        self.assertEqual((pid_of(head), bool(head[1] & 0x40)), (fx.AUDIO_PID, True))
        self.assertIsNone(closes(events)[2].next_first_pts)

    def test_a_silent_video_pid_is_noticed_in_media_time_under_bursty_feeding(self):
        # HLS-like: each 6 s of media arrives in one burst when complete, so feeds pause for
        # 6 s (a stall by wall clock). The audio PTS still shows the video has gone quiet.
        whole = fx.make_stream(30)
        stop = pusi_offset(whole, fx.VIDEO_PID, 900000 + 150 * FRAME)
        kept = [p for p in split_packets(whole[stop:]) if pid_of(p) != fx.VIDEO_PID]
        plain = whole[:stop] + b"".join(kept)
        # Each other PID is measured on its own clock: one 12 s ahead, or a single subtitle
        # PES right after the last video PES start, must not hide the audio's progress.
        packets = split_packets(plain)
        last = pusi_offset(plain, fx.VIDEO_PID, 900000 + 149 * FRAME) // PACKET
        sub, _ = fx.pes_packets(0x102, 0xBD, b"\x20" * 40, 900000 + 149 * FRAME + 1)
        cases = {
            "plain": plain,
            "offset subtitles": with_subtitles(plain, 12 * CLOCK),
            "one sparse subtitle PES first": b"".join(
                packets[: last + 1] + sub + packets[last + 1 :]
            ),
        }
        for name, data in cases.items():
            with self.subTest(name):
                points = [(off, (int(t // 6) + 1) * 6.0) for off, t in by_pts(data)]
                _, events = run_at(data, points)
                silent = [(o, body) for o, body in segments(events) if o.first_pts is None]
                self.assertEqual(len(silent), 2)
                o, body = silent[0]
                self.assertTrue(o.discontinuity)
                self.assertEqual(o.wall, 18.0)
                self.assertEqual(pid_of(body[:PACKET]), fx.AUDIO_PID)
                self.assertEqual(pusi_pts(body[:PACKET]), 900000 + 749 * fx.AUDIO_FRAME_TICKS)

    def test_offset_or_jumping_clocks_on_other_pids_are_not_silence(self):
        whole = fx.make_stream(10)
        # The audio jumps +15 s between two audio PES that follow the same video PES (frame
        # 112 at 4.48 s, then audio at 4.48 s and 4.5013 s).
        jumped = bytearray(whole)
        for i, p in enumerate(split_packets(whole)):
            if pid_of(p) == fx.AUDIO_PID and p[1] & 0x40:
                pts = pusi_pts(p)
                if pts >= 900000 + 211 * fx.AUDIO_FRAME_TICKS:
                    at = i * PACKET + 4 + (1 + p[4] if p[3] & 0x20 else 0) + 9
                    jumped[at : at + 5] = fx._pts_bytes(pts + 15 * CLOCK)
        cases = {
            "subtitles 12 s ahead": with_subtitles(whole, 12 * CLOCK),
            "audio jumps +15 s": bytes(jumped),
        }
        for name, data in cases.items():
            with self.subTest(name):
                _, events = run(data)
                o = opens(events)
                self.assertEqual([e.first_pts - 900000 for e in o], [k * TARGET for k in range(5)])

    def test_a_silent_video_pid_is_noticed_by_wall_clock_when_no_other_pid_has_pts(self):
        whole = fx.make_stream(20)
        stop = pusi_offset(whole, fx.VIDEO_PID, 900000 + 150 * FRAME)
        kept = [p for p in split_packets(whole[stop:]) if pid_of(p) != fx.VIDEO_PID]
        data = whole[:stop] + b"".join(kept)
        points = by_pts(data)  # live pacing, taken before the audio PTS are removed
        stripped = bytearray(data)
        for i, p in enumerate(split_packets(data)):
            if pid_of(p) == fx.AUDIO_PID and p[1] & 0x40:
                stripped[i * PACKET + 4 + (1 + p[4] if p[3] & 0x20 else 0) + 7] &= 0x3F
        _, events = run_at(bytes(stripped), points)
        silent = [e for e in opens(events) if e.first_pts is None]
        self.assertEqual(len(silent), 1)
        self.assertGreaterEqual(silent[0].wall, 149 * FRAME / CLOCK + 10)
        self.assertLess(silent[0].wall, 149 * FRAME / CLOCK + 10.05)

    def test_a_stalled_source_is_not_a_silent_video_pid(self):
        # The source stalls for 12 s right after the keyframe at 4 s; audio arrives first after.
        data = fx.make_stream(8)
        points = by_pts(data)
        resume = next(
            k for k, (off, t) in enumerate(points) if t >= 4.0 and k and points[k - 1][1] >= 4.0
        )
        points = points[:resume] + [(off, t + 12) for off, t in points[resume:]]
        self.assertEqual(pid_of(data[points[resume][0] :]), fx.AUDIO_PID)
        _, events = run_at(data, points)
        o = opens(events)
        self.assertEqual([e.first_pts - 900000 for e in o], [k * TARGET for k in range(4)])
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
        # The PTS stayed continuous: the segment before the marker keeps its exact end.
        self.assertEqual(closes(events)[1].next_first_pts, 900000 + 3 * CLOCK)

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
        # not match, is ignored even when repeated, as a valid PMT has been seen (accepting it
        # would lose the cut at 4 s).
        corrupt = bytearray(fx.pmt(video_pid=0x300, cc=7))
        corrupt[30] ^= 0xFF  # inside the CRC field
        key = pusi_offset(clean, fx.VIDEO_PID, 900000 + 2 * TARGET) // PACKET
        data = stream[:2] + junk + stream[2:key] + [bytes(corrupt)] * 3 + stream[key:]
        seg, events = run(b"".join(data))
        self.assertEqual(seg.video_pid, fx.VIDEO_PID)
        self.assertEqual(
            [e.first_pts for e in opens(events)], [900000 + k * TARGET for k in range(3)]
        )

    def test_a_repeated_bad_crc_is_tolerated_until_a_valid_table_arrives(self):
        def bad(cc):
            pmt = bytearray(fx.pmt(cc=cc))
            pmt[30] ^= 0xFF  # inside the CRC field; every copy is identical
            return [bytes(pmt)], (cc + 1) & 0x0F

        data = replace_pid_packets(fx.make_stream(6), fx.PMT_PID, bad)
        with self.assertLogs("services.live.ts", level="INFO") as logs:
            seg, events = run(data)
        self.assertEqual([r.levelname for r in logs.records], ["INFO"])
        self.assertEqual(seg.video_pid, fx.VIDEO_PID)
        # Accepted at its third copy, at 1 s (tables every 0.5 s).
        self.assertEqual(
            [e.first_pts - 900000 for e in opens(events)], [CLOCK, 3 * CLOCK, 5 * CLOCK]
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
            for i in range(3, len(data), PACKET):
                data[i] &= 0x3F  # and scrambling bits clear, to get past the scrambling check
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
