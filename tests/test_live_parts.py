import unittest

from services.live.parts import (
    Part,
    from_state,
    index_of,
    junctions,
    newest_first_seq,
    t_at,
    wall_at,
)

P = [Part(0, 0.0, 1_000.0), Part(30, 60.0, 10_000.0)]  # 60 s kept, then hours away
EDGE, NOW = 120.0, 20_000.0


class WallTests(unittest.TestCase):
    def test_newest_part_is_relative_to_now(self):
        self.assertEqual(wall_at(P, 100.0, live_edge=EDGE, now=NOW), 19_980.0)

    def test_older_part_keeps_its_own_time(self):
        self.assertEqual(wall_at(P, 30.0, live_edge=EDGE, now=NOW), 1_030.0)

    def test_no_parts_is_todays_rule(self):
        self.assertEqual(wall_at([], 5.0, live_edge=10.0, now=50.0), 45.0)

    def test_clock_stepped_back_never_maps_backwards(self):
        stepped = [Part(0, 0.0, 5_000.0), Part(30, 60.0, 4_000.0)]
        times = [wall_at(stepped, t, live_edge=EDGE, now=4_100.0) for t in (0.0, 59.0, 61.0)]
        self.assertEqual(times, sorted(times))

    def test_three_parts_with_a_clock_step_stay_in_order(self):
        stepped = [Part(0, 0.0, 9_000.0), Part(10, 20.0, 3_000.0), Part(20, 40.0, 5_000.0)]
        ts = [0.0, 19.0, 20.0, 39.0, 40.0, 50.0]
        times = [wall_at(stepped, t, live_edge=60.0, now=6_000.0) for t in ts]
        self.assertEqual(times, sorted(times))


class InverseTests(unittest.TestCase):
    def test_in_the_gap_is_none(self):
        self.assertIsNone(t_at(P, 5_000.0, live_edge=EDGE, now=NOW))

    def test_before_the_oldest_is_none(self):
        self.assertIsNone(t_at(P, 900.0, live_edge=EDGE, now=NOW))

    def test_round_trip_in_each_part(self):
        for t in (10.0, 100.0):
            wall = wall_at(P, t, live_edge=EDGE, now=NOW)
            self.assertAlmostEqual(t_at(P, wall, live_edge=EDGE, now=NOW), t)

    def test_no_parts_is_todays_rule(self):
        self.assertEqual(t_at([], 45.0, live_edge=10.0, now=50.0), 5.0)


class ShapeTests(unittest.TestCase):
    def test_junctions_and_index(self):
        self.assertEqual(junctions(P), [60.0])
        self.assertEqual(index_of(P, 59.9), 0)
        self.assertEqual(index_of(P, 60.0), 1)
        self.assertEqual(index_of([], 5.0), 0)

    def test_newest_first_seq(self):
        self.assertEqual(newest_first_seq(P), 30)
        self.assertIsNone(newest_first_seq([]))

    def test_from_state_drops_bad_entries(self):
        self.assertEqual(from_state([[0, 0.0, 1.0], ["x"], None]), [Part(0, 0.0, 1.0)])
        self.assertEqual(from_state(None), [])
        self.assertEqual(from_state([[0, 0.0, float("nan")]]), [])

    def test_from_state_keeps_parts_in_order(self):
        self.assertEqual(
            from_state([[0, 0.0, 1.0], [5, 10.0, 2.0], [3, 4.0, 3.0]]),
            [Part(0, 0.0, 1.0), Part(5, 10.0, 2.0)],
        )


if __name__ == "__main__":
    unittest.main()
