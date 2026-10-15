import os
from types import SimpleNamespace
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mixins.playback_mixin import PlaybackMixin


def _host(provider_type, seasons, current_season, content=None):
    host = PlaybackMixin.__new__(PlaybackMixin)
    host.provider_manager = SimpleNamespace(
        current_provider={
            "type": provider_type,
            "username": "u",
            "password": "p",
            "url": "http://x",
        },
        current_provider_content=content or {},
    )
    host.current_series = {"id": "S1", "name": "Show"}
    host.current_season = current_season
    host._current_seasons_list = seasons
    host._current_episode_list = []
    host._current_episode_index = -1
    return host


class NextSeasonAutoplayTests(unittest.TestCase):
    def test_stb_next_season_starts_at_its_first_episode(self):
        s1 = {"id": "1", "series": [1, 2], "cmd": "CMD1"}
        s2 = {"id": "2", "series": [1, 2, 3], "cmd": "CMD2"}
        host = _host("STB", [s1, s2], s1)

        episode = host._get_first_episode_of_next_season()

        self.assertIsNotNone(episode)
        self.assertEqual(episode["series"], 1)
        self.assertEqual(episode["cmd"], "CMD2")
        self.assertEqual(episode["number"], "1")
        self.assertIs(host.current_season, s2)
        self.assertEqual(len(host._current_episode_list), 3)
        self.assertEqual(host._current_episode_index, 0)

    def test_stb_episode_numbers_as_strings(self):
        s1 = {"id": "1", "series": ["1"], "cmd": "CMD1"}
        s2 = {"id": "2", "series": ["4", "5"], "cmd": "CMD2"}
        host = _host("STB", [s1, s2], s1)

        episode = host._get_first_episode_of_next_season()

        self.assertEqual(episode["series"], 4)
        self.assertEqual(len(host._current_episode_list), 2)

    def test_last_season_has_no_next_episode(self):
        s1 = {"id": "1", "series": [1], "cmd": "CMD1"}
        host = _host("STB", [s1], s1)
        self.assertIsNone(host._get_first_episode_of_next_season())

    def test_xtream_next_season_still_works(self):
        s1 = {"id": "1", "episodes": [{"id": "e0", "episode_num": 1}]}
        s2 = {
            "id": "2",
            "episodes": [
                {"id": "e1", "episode_num": 1, "title": "Pilot", "container_extension": "mkv"}
            ],
        }
        content = {"series": {"resolved_base": "http://x"}}
        host = _host("XTREAM", [s1, s2], s1, content)

        episode = host._get_first_episode_of_next_season()

        self.assertEqual(episode["cmd"], "http://x/series/u/p/e1.mkv")
        self.assertEqual(episode["name"], "Pilot")


if __name__ == "__main__":
    unittest.main()
