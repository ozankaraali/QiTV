"""Next-season autoplay over the episodes CatalogService builds.

These cases guarded the old player mixin's next-season lookup. Autoplay now follows the
play context with services.next_episode, over the seasons and episodes CatalogService
loads and caches, so they run against that pair.
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from services.navigation import episode_key, item_key
from services.next_episode import NeedEpisodes, NextUp, following
from tests.test_catalog_service import make_service

SERIES = {"id": "S1", "name": "Show", "category_id": "3"}


def stb_service(*seasons):
    """An STB catalog that lists `seasons` (each with its episode numbers in "series")."""
    service, _, factory, _ = make_service(
        {"name": "S", "type": "STB", "url": "http://portal.test/c/"}
    )
    factory.results["stb_list"] = {"result": {"items": list(seasons)}}
    service.request_seasons(SERIES)
    return service


def load(service, season):
    service.request_episodes(SERIES, season)
    return service.episodes(SERIES, season)


def after(service, season, episode):
    return following(
        "episode",
        {"series": SERIES, "season": season},
        episode,
        seasons=service.seasons,
        episodes=service.episodes,
        key=item_key,
        episode_key=episode_key,
    )


class NextSeasonAutoplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_stb_next_season_starts_at_its_first_episode(self):
        service = stb_service(
            {"id": "S1:1", "name": "Season 1", "series": [1, 2], "cmd": "CMD1"},
            {"id": "S1:2", "name": "Season 2", "series": [1, 2, 3], "cmd": "CMD2"},
        )
        s1, s2 = service.seasons(SERIES)
        last = load(service, s1)[-1]
        # The next season is not loaded yet: autoplay asks for its episodes first.
        self.assertEqual(after(service, s1, last), NeedEpisodes(s2))
        self.assertEqual(len(load(service, s2)), 3)

        result = after(service, s1, last)

        self.assertIsInstance(result, NextUp)
        self.assertIs(result.season, s2)
        self.assertEqual(result.item["series"], 1)
        self.assertEqual(result.item["cmd"], "CMD2")
        self.assertEqual(result.item["number"], "1")

    def test_stb_episode_numbers_as_strings(self):
        service = stb_service(
            {"id": "S1:1", "series": ["1"], "cmd": "CMD1"},
            {"id": "S1:4", "series": ["4", "5"], "cmd": "CMD2"},
        )
        s1, s2 = service.seasons(SERIES)
        first = load(service, s1)[0]
        self.assertEqual(len(load(service, s2)), 2)

        result = after(service, s1, first)

        self.assertEqual(result.item["series"], "4")
        self.assertEqual(result.item["number"], "4")

    def test_odd_episode_numbers_never_crash(self):
        # The old list builder converted numbers with int() ("²" is a digit but not a
        # decimal). The catalog keeps the portal's values, so nothing is converted.
        service = stb_service({"id": "S1:1", "series": ["1", " 2 ", "²", 3], "cmd": "C"})
        [season] = service.seasons(SERIES)
        episodes = load(service, season)
        self.assertEqual([e["series"] for e in episodes], ["1", " 2 ", "²", 3])
        self.assertEqual(after(service, season, episodes[2]), NextUp(season, episodes[3]))
        self.assertIsNone(after(service, season, episodes[3]))

    def test_last_season_has_no_next_episode(self):
        service = stb_service({"id": "S1:1", "series": [1], "cmd": "CMD1"})
        [season] = service.seasons(SERIES)
        self.assertIsNone(after(service, season, load(service, season)[-1]))

    def test_xtream_next_season_still_works(self):
        service, _, factory, _ = make_service(
            {"name": "X", "type": "XTREAM", "url": "h:1", "username": "u", "password": "p"}
        )
        factory.results["xtream_series"] = {
            "result": {
                "resolved_base": "http://x",
                "seasons": [
                    {"id": "1", "name": "Season 1", "episodes": [{"id": "e0", "episode_num": 1}]},
                    {
                        "id": "2",
                        "name": "Season 2",
                        "episodes": [
                            {
                                "id": "e1",
                                "episode_num": 1,
                                "title": "Pilot",
                                "container_extension": "mkv",
                            }
                        ],
                    },
                ],
            }
        }
        service.request_seasons(SERIES)
        s1, s2 = service.seasons(SERIES)

        result = after(service, s1, service.episodes(SERIES, s1)[0])

        self.assertIs(result.season, s2)
        self.assertEqual(result.item["cmd"], "http://x/series/u/p/e1.mkv")
        self.assertEqual(result.item["name"], "Pilot")


if __name__ == "__main__":
    unittest.main()
