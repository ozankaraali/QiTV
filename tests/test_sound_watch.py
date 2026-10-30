"""2.8.8: the sound watch (`services.sound_watch.SoundWatch`) on libVLC's media counters."""

import unittest

from services.picture_watch import Counters
from services.sound_watch import AGAIN_S, SILENT_S, SoundWatch

TICK = 0.25


class SoundWatchTests(unittest.TestCase):
    def setUp(self):
        self.watch = SoundWatch()
        self.now = 0.0
        self.shown = self.video = self.played = self.audio = 0

    def run_for(self, seconds, *, shown=25, audio=47, played=None, playing=True, track=True):
        """Ticks for `seconds`: pictures shown and audio decoded a second, played as decoded
        unless given; the verdicts with their times."""
        played = audio if played is None else played
        verdicts = []
        for _ in range(round(seconds / TICK)):
            self.now += TICK
            self.shown += round(shown * TICK)
            self.video += round(shown * TICK)
            self.audio += round(audio * TICK)
            self.played += round(played * TICK)
            counters = Counters(self.shown, 0, self.video, self.played, self.audio)
            verdict = self.watch.update(self.now, playing, counters, track)
            if verdict is not None:
                verdicts.append((self.now, verdict))
        return verdicts

    def test_sound_that_plays_is_left_alone(self):
        self.assertEqual(self.run_for(120.0), [])

    def test_sound_that_stops_while_pictures_show_is_cycled_then_reopened_once(self):
        self.run_for(10.0)
        lost = self.now
        verdicts = self.run_for(30.0, played=0)
        self.assertEqual([verdict for _, verdict in verdicts], ["cycle", "reopen"])
        (cycled, _), (reopened, _) = verdicts
        self.assertAlmostEqual(cycled - lost, SILENT_S + TICK)
        self.assertAlmostEqual(reopened - cycled, AGAIN_S)

    def test_sound_back_after_the_cycle_ends_it_and_a_later_loss_starts_over(self):
        self.run_for(10.0)
        self.assertEqual([v for _, v in self.run_for(4.0, played=0)], ["cycle"])
        self.assertEqual(self.run_for(30.0), [])  # the new decoder plays
        self.assertEqual([v for _, v in self.run_for(4.0, played=0)], ["cycle"])

    def test_a_source_sending_no_sound_is_cycled_but_not_reopened(self):
        self.run_for(10.0)
        verdicts = self.run_for(30.0, audio=0)  # nothing reaches the audio decoder
        self.assertEqual([verdict for _, verdict in verdicts], ["cycle"])

    def test_no_verdict_while_vlc_waits_nothing_shows_or_no_track_plays(self):
        self.run_for(10.0)
        self.assertEqual(self.run_for(20.0, played=0, playing=False), [])  # paused, held
        self.assertEqual(self.run_for(20.0, played=0, shown=0), [])  # a stall: no picture
        self.assertEqual(self.run_for(20.0, played=0, track=False), [])  # its track off

    def test_a_stream_is_watched_once_its_sound_played(self):
        self.assertEqual(self.run_for(20.0, played=0), [])  # never played: not lost
        self.watch.restart()  # the next stream
        self.run_for(5.0)
        self.assertEqual([v for _, v in self.run_for(4.0, played=0)], ["cycle"])


if __name__ == "__main__":
    unittest.main()
