"""2.7: a server part placed on the live session's timeline by time of day."""

import unittest

from services.live.parts import Part
from services.live.server_part import ServerPart, back_for, buffer_first_media, hop_seq
from services.live.timeline import Span

# The live buffer: one part that began at 10,000 (Unix seconds) at timeline second 0; its live
# edge at 600 s is now (10,600).
PARTS = [Part(0, 0.0, 10_000.0)]
EDGE, NOW = 600.0, 10_600.0


class ServerPartTests(unittest.TestCase):
    def test_a_part_is_placed_by_its_time_of_day(self):
        part = ServerPart("p", asked=-300.0, until=100)
        part.place(9_700.0, PARTS, live_edge=EDGE, now=NOW)
        self.assertEqual(part.offset, -300.0)
        own = [Span(0, 0.0, 6.0, False), Span(1, 6.0, 12.0, False)]
        self.assertEqual(
            [(s.seq, s.start, s.end) for s in part.spans(own)],
            [(0, -300.0, -294.0), (1, -294.0, -288.0)],
        )
        self.assertTrue(part.holds(own, -290.0))
        self.assertFalse(part.holds(own, -280.0))

    def test_a_part_is_placed_once(self):
        part = ServerPart("p", asked=-300.0, until=100)
        part.place(9_700.0, PARTS, live_edge=EDGE, now=NOW)
        part.place(9_000.0, PARTS, live_edge=EDGE, now=NOW)  # a later state: it stays
        self.assertEqual(part.offset, -300.0)

    def test_an_unplaced_part_holds_nothing(self):
        part = ServerPart("p", asked=-300.0, until=100)
        own = [Span(0, 0.0, 6.0, False)]
        self.assertEqual(part.spans(own), [])
        self.assertFalse(part.holds(own, -300.0))

    def test_back_for_a_moment_before_the_buffer(self):
        self.assertEqual(back_for(-300.0, PARTS, live_edge=EDGE, now=NOW), 900.0)

    def test_the_buffers_first_media_sequence(self):
        live = [Span(5, 0.0, 2.0, False), Span(6, 2.0, 6.0, False, media_seq=101)]
        self.assertEqual(buffer_first_media(live), 101)
        self.assertIsNone(buffer_first_media([Span(5, 0.0, 2.0, False)]))

    def test_the_hop_lands_on_the_segment_beginning_the_stop(self):
        live = [
            Span(5, 0.0, 2.0, False),
            Span(6, 2.0, 6.0, False, media_seq=101),
            Span(7, 6.0, 12.0, False, media_seq=102),
        ]
        self.assertEqual(hop_seq(live, 102), 7)
        self.assertEqual(hop_seq(live, 999), 5)  # evicted past: the buffer's oldest
        self.assertEqual(hop_seq(live, None), 5)
        self.assertIsNone(hop_seq([], 102))


if __name__ == "__main__":
    unittest.main()
