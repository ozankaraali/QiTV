import unittest

from services.live.parts import (
    Part,
    behind_in,
    from_state,
    index_of,
    junctions,
    newest_first_seq,
    t_at,
    t_in,
    wall_at,
    wall_in,
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

    def test_before_the_oldest_goes_on_backwards(self):
        # The catch-up archive's part of the bar, before the buffer's oldest point.
        self.assertEqual(t_at(P, 900.0, live_edge=EDGE, now=NOW), -100.0)

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


class StateTests(unittest.TestCase):
    """The timeline state's view: its parts, its live edge."""

    STATE = {"live_edge": EDGE, "lag": 4.0, "parts": [[0, 0.0, 1_000.0], [30, 60.0, 10_000.0]]}

    def test_wall_and_t_in_a_state(self):
        self.assertEqual(wall_in(self.STATE, 30.0, NOW), 1_030.0)
        self.assertIsNone(t_in(self.STATE, 5_000.0, NOW))  # a programme in the gap
        self.assertEqual(t_in({"live_edge": 10.0}, 45.0, 50.0), 5.0)

    def test_behind_counts_the_time_away_once_there_is_a_gap(self):
        self.assertEqual(behind_in(dict(self.STATE, behind=900.0)), 900.0)
        one = {"live_edge": EDGE, "lag": 4.0, "parts": [[0, 0.0, 1_000.0]], "behind": 900.0}
        self.assertEqual(behind_in(one), 4.0)  # one part: the lag, as before
        self.assertIsNone(behind_in({"parts": []}))


if __name__ == "__main__":
    unittest.main()
