import unittest

import ts_fixtures as ts


def packets(data: bytes, size: int = 188):
    off = 4 if size == 192 else 0
    return [data[i + off : i + off + 188] for i in range(0, len(data), size)]


def pid_of(p: bytes) -> int:
    return ((p[1] & 0x1F) << 8) | p[2]


class TsFixtureTests(unittest.TestCase):
    def test_structure(self):
        data = ts.make_stream(3)
        self.assertEqual(len(data) % 188, 0)
        pk = packets(data)
        self.assertTrue(all(p[0] == 0x47 for p in pk))

    def test_first_packet_is_pat_with_valid_crc(self):
        p = packets(ts.make_stream(3))[0]
        self.assertEqual(pid_of(p), 0)
        self.assertTrue(p[1] & 0x40)
        section_length = ((p[6] & 0x0F) << 8) | p[7]
        section = p[5 : 5 + 3 + section_length]
        self.assertEqual(ts.crc32_mpeg(section[:-4]), int.from_bytes(section[-4:], "big"))
        self.assertEqual(ts.crc32_mpeg(section), 0)

    def test_crc_matches_the_mpeg2_check_value(self):
        # CRC-32/MPEG-2's published check value, so the fixture CRC is not only self-consistent.
        self.assertEqual(ts.crc32_mpeg(b"123456789"), 0x0376E6E7)

    def test_rai_exactly_at_keyframes(self):
        rai = 0
        for p in packets(ts.make_stream(3)):
            if pid_of(p) == 0x100 and p[1] & 0x40:
                self.assertTrue(p[3] & 0x20)
                if p[5] & 0x40:
                    rai += 1
        self.assertEqual(rai, 3)

    def test_pcr_on_every_video_frame_start(self):
        starts = [p for p in packets(ts.make_stream(2)) if pid_of(p) == 0x100 and p[1] & 0x40]
        self.assertEqual(len(starts), 50)
        self.assertTrue(all(p[5] & 0x10 for p in starts))

    def test_continuity_counters_increment_per_pid(self):
        last = {}
        for p in packets(ts.make_stream(2)):
            pid, cc = pid_of(p), p[3] & 0x0F
            if pid in last:
                self.assertEqual(cc, (last[pid] + 1) & 0xF, hex(pid))
            last[pid] = cc

    def test_packet_size_192(self):
        data = ts.make_stream(1, packet_size=192)
        self.assertEqual(len(data) % 192, 0)
        self.assertTrue(all(data[i + 4] == 0x47 for i in range(0, len(data), 192)))

    def test_packet_size_204(self):
        data = ts.make_stream(1, packet_size=204)
        self.assertEqual(len(data) % 204, 0)
        self.assertTrue(all(data[i] == 0x47 for i in range(0, len(data), 204)))

    def test_scrambled_flag_on_video_only(self):
        for p in packets(ts.make_stream(1, scrambled=True)):
            if pid_of(p) == 0x100:
                self.assertEqual(p[3] >> 6, 2)
            else:
                self.assertEqual(p[3] >> 6, 0)

    def test_pts_reset_jumps_back(self):
        data = ts.make_stream(3, resets=(1.5,), pts_start=900000)
        pts = []
        for p in packets(data):
            if pid_of(p) == 0x100 and p[1] & 0x40:
                i = 4 + 1 + p[4]
                b = p[i + 9 : i + 14]
                pts.append(
                    ((b[0] >> 1) & 7) << 30 | b[1] << 22 | (b[2] >> 1) << 15 | b[3] << 7 | b[4] >> 1
                )
        self.assertTrue(any(b < a for a, b in zip(pts, pts[1:])))
        self.assertEqual(pts[0], 900000)


if __name__ == "__main__":
    unittest.main()
