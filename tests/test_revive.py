"""A live channel that is still watched comes back by itself (services.revive)."""

import unittest

from services.revive import HEALTHY_S, REVIVE_S, Revive, fmt_wait, revive_wait


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class ReviveWaitTests(unittest.TestCase):
    def test_the_waits_grow_to_five_minutes_and_stay_there(self):
        # The owner's ask: about 1 s, 3 s, 10 s, 30 s, 60 s, 120 s, then every 300 s, no cap.
        self.assertEqual(REVIVE_S, (1.0, 3.0, 10.0, 30.0, 60.0, 120.0, 300.0))
        waits = [revive_wait(tries) for tries in range(10)]
        self.assertEqual(waits, [1.0, 3.0, 10.0, 30.0, 60.0, 120.0, 300.0, 300.0, 300.0, 300.0])
        self.assertEqual(revive_wait(10_000), 300.0)
        self.assertEqual(revive_wait(-1), 1.0)

    def test_a_countdown_reads_minutes_and_seconds_rounded_up(self):
        self.assertEqual(fmt_wait(25.0), "0:25")
        self.assertEqual(fmt_wait(24.2), "0:25")  # never "0:24" while 24.2 s are left
        self.assertEqual(fmt_wait(120.0), "2:00")
        self.assertEqual(fmt_wait(300.0), "5:00")
        self.assertEqual(fmt_wait(0.0), "0:00")
        self.assertEqual(fmt_wait(-3.0), "0:00")


class ReviveTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.revive = Revive(self.clock)

    def test_each_try_counts_and_waits_longer(self):
        waits = []
        for _ in range(8):
            self.revive.stopped()
            waits.append(self.revive.next_wait())
        self.assertEqual(waits, [1.0, 3.0, 10.0, 30.0, 60.0, 120.0, 300.0, 300.0])
        self.assertEqual(self.revive.tries, 8)

    def test_playing_briefly_keeps_the_waits_growing(self):
        self.revive.next_wait()
        self.revive.next_wait()
        self.revive.playing()
        self.clock.now += HEALTHY_S / 2  # a flapping stream: it played, then ended again
        self.revive.stopped()
        self.assertEqual(self.revive.next_wait(), 10.0)

    def test_playing_well_starts_the_waits_anew(self):
        for _ in range(5):
            self.revive.next_wait()
        self.revive.playing()
        self.clock.now += HEALTHY_S
        self.assertTrue(self.revive.played_well())
        self.revive.stopped()
        self.assertEqual(self.revive.next_wait(), 1.0)
        self.assertFalse(self.revive.played_well())  # its play counted once

    def test_reset_forgets_everything(self):
        self.revive.next_wait()
        self.revive.playing()
        self.revive.reset()
        self.assertEqual((self.revive.tries, self.revive.played_well()), (0, False))


class RetryTextTests(unittest.TestCase):
    def test_the_pill_says_the_stream_ended_and_when_it_is_tried_again(self):
        from ui.overlays.retry_notice import ended_text, retry_text

        self.assertEqual(retry_text(25.0), "Trying again in 0:25")
        self.assertEqual(retry_text(0.0), "Reconnecting…")  # the try opens now
        self.assertEqual(ended_text(300.0), "The stream ended · Trying again in 5:00")


if __name__ == "__main__":
    unittest.main()
