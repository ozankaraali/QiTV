"""Each player's live channel and the key it opened with (ui/player_keys.py): one memory for
the sound's sync and the picture's shape, kept while the channel plays there."""

import unittest

from ui.player_keys import PlayerKeys

ONE = {"id": "1", "name": "One", "cmd": "http://stream/1"}
TWO = {"id": "2", "name": "Two", "cmd": "http://stream/2"}


class PlayerKeysTests(unittest.TestCase):
    def setUp(self):
        self.browsed = "Home"  # an unstamped channel is the browsed provider's
        self.keys = PlayerKeys(lambda item: f"{self.browsed}:{item['id']}")
        self.large, self.small = object(), object()

    def test_a_channel_keeps_the_key_it_opened_with_in_its_player(self):
        self.assertEqual(self.keys.opens(self.large, ONE), "Home:1")
        self.browsed = "Backup"  # the channel list browses another provider meanwhile
        self.assertEqual(self.keys.key_of(self.large, ONE), "Home:1")
        self.assertEqual(self.keys.key_of(self.large, dict(ONE)), "Home:1")  # the same channel
        self.assertEqual(self.keys.key_of(self.small, ONE), "Backup:1")  # it never opened there
        self.assertEqual(self.keys.key_of(self.large, TWO), "Backup:2")  # not what it opened

    def test_a_player_that_closed_or_opened_something_else_keeps_nothing(self):
        self.keys.opens(self.large, ONE)
        self.keys.opens(self.small, TWO)
        self.keys.forget(self.small)
        self.keys.forget(self.small)  # again: nothing
        self.assertEqual(self.keys.players(), [self.large])
        self.browsed = "Backup"
        self.assertEqual(self.keys.key_of(self.small, TWO), "Backup:2")
        self.keys.opens(self.large, TWO)  # another channel: its own key
        self.assertEqual(self.keys.key_of(self.large, ONE), "Backup:1")


if __name__ == "__main__":
    unittest.main()
