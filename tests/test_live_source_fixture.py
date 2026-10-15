"""The smoke test's live channel: tests/fixtures/live.ts as an endless, continuous stream."""

from collections import defaultdict
import itertools
import time
import unittest
import urllib.error
import urllib.request

from tests.fixtures.live_source import LIVE_TS, LiveSource, LoopedStream, peak_of

PACKET = 188
CLOCK = 90_000


def _parse(data: bytes):
    """Per packet: (pid, cc, pcr base or None, pts or None)."""
    out = []
    for i in range(0, len(data), PACKET):
        p = data[i : i + PACKET]
        assert p[0] == 0x47
        pid = ((p[1] & 0x1F) << 8) | p[2]
        afc = (p[3] >> 4) & 3
        offset, pcr, pts = 4, None, None
        if afc & 2:
            length = p[4]
            if length and p[5] & 0x10:
                b = p[6:12]
                pcr = (b[0] << 25) | (b[1] << 17) | (b[2] << 9) | (b[3] << 1) | (b[4] >> 7)
            offset = 5 + length
        payload = p[offset:]
        if p[1] & 0x40 and payload[:3] == b"\0\0\1" and payload[7] & 0x80:
            b = payload[9:14]
            pts = ((b[0] >> 1) & 7) << 30 | b[1] << 22 | (b[2] >> 1) << 15 | b[3] << 7 | b[4] >> 1
        out.append((pid, p[3] & 0x0F, pcr, pts))
    return out


class LoopedStreamTests(unittest.TestCase):
    def setUp(self):
        self.data = LIVE_TS.read_bytes()
        self.stream = LoopedStream(self.data)

    def two_loops(self):
        loops: list[list[tuple[float, bytes]]] = [[], []]
        chunks = self.stream.chunks()
        per_loop = len(self.data) // PACKET
        sent = 0
        while sent < 2 * per_loop:
            due, chunk = next(chunks)
            loops[sent // per_loop].append((due, chunk))
            sent += len(chunk) // PACKET
        return loops

    def test_the_fixture_is_a_twelve_second_clip(self):
        self.assertEqual(len(self.data) % PACKET, 0)
        self.assertAlmostEqual(self.stream.period, 12.0, places=3)

    def test_each_pass_repeats_the_clip_one_period_later(self):
        first, second = (b"".join(chunk for _, chunk in loop) for loop in self.two_loops())
        self.assertEqual(first, self.data)  # the first pass is the file itself
        shift = round(self.stream.period * CLOCK)
        for a, b in zip(_parse(first), _parse(second)):
            self.assertEqual(a[0], b[0])
            if a[2] is not None:
                self.assertEqual(b[2], a[2] + shift)
            if a[3] is not None:
                self.assertEqual(b[3], a[3] + shift)

    def test_timestamps_and_counters_run_on_across_the_loop(self):
        stream = b"".join(chunk for loop in self.two_loops() for _, chunk in loop)
        pts, pcr, ccs = defaultdict(list), [], defaultdict(list)
        for pid, cc, packet_pcr, packet_pts in _parse(stream):
            ccs[pid].append(cc)
            if packet_pcr is not None:
                pcr.append(packet_pcr)
            if packet_pts is not None:
                pts[pid].append(packet_pts)
        self.assertTrue(all(b > a for a, b in itertools.pairwise(pcr)))
        for pid, values in pts.items():
            self.assertTrue(all(b > a for a, b in itertools.pairwise(values)), hex(pid))
        for pid, values in ccs.items():  # every packet of the clip carries a payload
            breaks = [i for i, (a, b) in enumerate(itertools.pairwise(values)) if b != (a + 1) % 16]
            self.assertEqual(breaks, [], hex(pid))

    def test_chunks_are_due_at_their_media_time(self):
        first, second = self.two_loops()
        dues = [due for due, _ in first + second]
        self.assertTrue(0.0 <= dues[0] < 0.5, dues[0])  # its last PCR: real time from the start
        self.assertTrue(all(b >= a for a, b in itertools.pairwise(dues)))
        self.assertLess(first[-1][0], self.stream.period)
        self.assertAlmostEqual(second[0][0], self.stream.period + dues[0], places=6)
        self.assertTrue(all(len(chunk) == 188 * 64 for _, chunk in first[:-1]))


class LiveSourceTests(unittest.TestCase):
    def setUp(self):
        self.source = LiveSource()
        self.addCleanup(self.source.close)

    def test_it_streams_the_channel_as_mpeg_ts(self):
        with urllib.request.urlopen(self.source.url, timeout=5) as response:
            self.assertEqual(response.headers["Content-Type"], "video/mp2t")
            head = response.read(188 * 64)
        self.assertEqual(head, LIVE_TS.read_bytes()[: len(head)])
        self.assertEqual(self.source.connections, 1)
        self.assertTrue(self.source.url.startswith("http://127.0.0.1:"))

    def wait_for(self, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while not predicate():
            self.assertLess(time.monotonic(), deadline, "timed out")
            time.sleep(0.01)

    def test_it_counts_the_connections_open_at_once(self):
        first = urllib.request.urlopen(self.source.url, timeout=5)
        second = urllib.request.urlopen(self.source.url, timeout=5)
        first.read(188)
        second.read(188)
        self.assertEqual(self.source.peak, 2)
        first.close()
        second.close()
        self.wait_for(lambda: len(self.source.events) == 4)
        self.assertEqual(self.source.connections, 2)
        self.assertEqual(self.source.peak, 2)

    def test_a_client_that_went_away_is_seen_at_once(self):
        with urllib.request.urlopen(self.source.url, timeout=5) as response:
            response.read(188)
        closed = time.monotonic()
        self.wait_for(lambda: len(self.source.events) == 2)
        self.assertLess(self.source.events[-1][0] - closed, 0.2)  # not at the next chunk
        with urllib.request.urlopen(self.source.url, timeout=5) as response:
            response.read(188)
        self.assertEqual(self.source.peak, 1)  # one after the other

    def test_the_peak_of_events_counts_a_close_before_an_open_at_the_same_moment(self):
        self.assertEqual(peak_of([(1.0, 1), (2.0, 1), (2.0, -1), (3.0, -1)]), 1)
        self.assertEqual(peak_of([(2.0, 1), (1.0, 1), (3.0, -1), (4.0, -1)]), 2)
        self.assertEqual(peak_of([]), 0)

    def test_other_paths_are_not_found(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(self.source.url + ".m3u8", timeout=5)
        self.assertEqual(caught.exception.code, 404)
        caught.exception.close()


if __name__ == "__main__":
    unittest.main()
