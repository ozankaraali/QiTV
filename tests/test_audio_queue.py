"""The album's play order (ui/audio_queue.py): in order or shuffled, repeat off, all or one,
the next track at a song's end or on a press, and the previous one (spec §10)."""

import random
import unittest

from ui.audio_queue import RESTART_MS, AudioQueue


def album(count):
    return [{"path": f"/music/{n:02d}.mp3", "name": f"{n:02d}.mp3"} for n in range(1, count + 1)]


class AudioQueueTests(unittest.TestCase):
    def test_queue_order_shuffle_repeat(self):
        queue = AudioQueue(album(4), 1)
        self.assertEqual(queue.order(), [0, 1, 2, 3])
        self.assertEqual((queue.shuffle, queue.repeat), (False, "off"))
        # In order, at the end and on a press alike; after the last, nothing (repeat off).
        self.assertEqual(queue.next(auto=True), 2)
        self.assertEqual(queue.current, 2)
        self.assertEqual(queue.next(auto=False), 3)
        self.assertIsNone(queue.next(auto=True))
        self.assertIsNone(queue.next(auto=False))
        self.assertEqual(queue.current, 3)  # nothing after it: it stays the current one
        # Repeat all: round again from the first.
        self.assertEqual(queue.cycle_repeat(), "all")
        self.assertEqual(queue.next(auto=True), 0)
        # Repeat one: the same song again at its end; a press goes on (and round) all the same.
        self.assertEqual(queue.cycle_repeat(), "one")
        self.assertEqual(queue.next(auto=True), 0)
        self.assertEqual(queue.next(auto=False), 1)
        queue.pick(3)
        self.assertEqual(queue.next(auto=False), 0)
        self.assertEqual(queue.cycle_repeat(), "off")  # off → all → one → off
        # Shuffle: the song playing stays first, the others follow in a new order, each once.
        shuffled = AudioQueue(album(8), 5, rng=random.Random(4))
        self.assertTrue(shuffled.toggle_shuffle())
        order = shuffled.order()
        self.assertEqual(order[0], 5)
        self.assertEqual(sorted(order), list(range(8)))
        self.assertNotEqual(order, [5, 0, 1, 2, 3, 4, 6, 7])  # (seeded: a new order)
        played = [shuffled.next(auto=True) for _ in range(7)]
        self.assertEqual(played, order[1:])
        self.assertIsNone(shuffled.next(auto=True))
        # Off again: back to the album's order, on from the song playing.
        shuffled.pick(2)
        self.assertFalse(shuffled.toggle_shuffle())
        self.assertEqual(shuffled.order(), list(range(8)))
        self.assertEqual(shuffled.next(auto=True), 3)
        # Shuffled from the start (the remembered setting): the given song first.
        again = AudioQueue(album(5), 3, shuffle=True, repeat="all", rng=random.Random(1))
        self.assertEqual(again.order()[0], 3)
        self.assertEqual(sorted(again.order()), list(range(5)))
        for _ in range(4):
            again.next(auto=True)
        self.assertEqual(again.next(auto=True), 3)  # repeat all: round the same order again

    def test_previous_restarts_after_3_s(self):
        queue = AudioQueue(album(3), 1)
        self.assertEqual(queue.previous(RESTART_MS + 1), 1)  # over 3 s: this one from its start
        self.assertEqual(queue.current, 1)
        self.assertEqual(queue.previous(RESTART_MS), 0)  # else the one before
        self.assertEqual(queue.current, 0)
        # The first, near its start: none before it (the caller plays it from its start).
        self.assertIsNone(queue.previous(500))
        self.assertEqual(queue.current, 0)
        queue.cycle_repeat()  # all: round backwards to the last
        self.assertEqual(queue.previous(500), 2)
        # Shuffled, the one before in the shuffled order.
        shuffled = AudioQueue(album(6), 2, rng=random.Random(9))
        shuffled.toggle_shuffle()
        order = shuffled.order()
        shuffled.next(auto=False)
        shuffled.next(auto=False)
        self.assertEqual(shuffled.previous(0), order[1])

    def test_a_picked_track_plays_and_the_order_goes_on_from_it(self):
        queue = AudioQueue(album(5), 0)
        queue.pick(3)
        self.assertEqual(queue.current, 3)
        self.assertEqual(queue.next(auto=True), 4)
        with self.assertRaises(IndexError):
            queue.pick(5)
        self.assertEqual(queue.index_of("/music/02.mp3"), 1)
        self.assertIsNone(queue.index_of("/music/09.mp3"))
        self.assertEqual(len(queue), 5)
        self.assertEqual(queue.track(2)["name"], "03.mp3")

    def test_an_unknown_repeat_is_off(self):
        queue = AudioQueue(album(2), 0, repeat="twice")
        self.assertEqual(queue.repeat, "off")
        with self.assertRaises(ValueError):
            AudioQueue([], 0)


if __name__ == "__main__":
    unittest.main()
