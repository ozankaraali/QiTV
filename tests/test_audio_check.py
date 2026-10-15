"""The smoke's audio read-back rule (Review I1)."""

import unittest

from tests.fixtures.audio_check import reading_matches, should_mute


class ReadingMatchesTests(unittest.TestCase):
    def test_a_player_at_the_pips_level_while_the_manager_says_100_fails(self):
        # CI run 38778479935: "the manager says muted at 100, libVLC (mute, volume) (1, 30)"
        self.assertFalse(reading_matches(True, 100, (1, 30)))

    def test_a_player_at_the_managers_level_and_mute_passes(self):
        self.assertTrue(reading_matches(False, 100, (0, 100)))
        self.assertTrue(reading_matches(True, 30, (1, 30)))

    def test_a_wrong_mute_fails(self):
        self.assertFalse(reading_matches(False, 100, (1, 100)))

    def test_a_player_a_worker_holds_is_not_a_match(self):
        self.assertFalse(reading_matches(False, 100, None))

    def test_levels_can_be_left_out(self):
        self.assertTrue(reading_matches(True, 100, (1, 30), check_level=False))


class ShouldMuteTests(unittest.TestCase):
    def test_a_local_run_is_muted_unless_it_asks_for_sound(self):
        self.assertTrue(should_mute({}))
        self.assertTrue(should_mute({"QITV_SMOKE_MUTE": "1"}))
        self.assertFalse(should_mute({"QITV_SMOKE_MUTE": "0"}))

    def test_ci_keeps_the_real_audio_output_unless_it_asks_to_mute(self):
        self.assertFalse(should_mute({"CI": "true"}))
        self.assertTrue(should_mute({"CI": "true", "QITV_SMOKE_MUTE": "1"}))


if __name__ == "__main__":
    unittest.main()
