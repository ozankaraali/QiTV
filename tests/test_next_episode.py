import unittest

from services.catalog import format_xtream_episodes
from services.navigation import episode_key, item_key
from services.next_episode import (
    NeedEpisodes,
    NextUp,
    advanced_context,
    episode_label,
    following,
    genre_title,
    has_next,
    next_episode,
    next_movie,
    play_context,
)


def stb_episodes(series, season, numbers):
    """STB episodes as CatalogService builds them: the series dict plus season and number."""
    return [
        {
            **series,
            "number": f"{n}",
            "ename": f"Episode {n}",
            "cmd": season["cmd"],
            "series": n,
            "season_id": str(season["id"]),
            "season": str(season["id"]),
        }
        for n in numbers
    ]


def resolve(seasons, loaded, season, episode):
    return next_episode(
        seasons,
        lambda s: loaded.get(item_key(s)),
        season,
        episode,
        season_key=item_key,
        episode_key=episode_key,
    )


class NextEpisodeTests(unittest.TestCase):
    def setUp(self):
        self.show = {"id": "S1", "name": "Show"}
        self.s1 = {"id": "1", "series": [1, 2], "cmd": "CMD1"}
        self.s2 = {"id": "2", "series": [1, 2, 3], "cmd": "CMD2"}

    def test_stb_next_season_starts_at_its_first_episode(self):
        eps1 = stb_episodes(self.show, self.s1, [1, 2])
        eps2 = stb_episodes(self.show, self.s2, [1, 2, 3])
        result = resolve([self.s1, self.s2], {"1": eps1, "2": eps2}, self.s1, eps1[1])
        self.assertIsInstance(result, NextUp)
        self.assertIs(result.season, self.s2)
        self.assertEqual(result.item["series"], 1)
        self.assertEqual(result.item["cmd"], "CMD2")
        self.assertEqual(result.item["number"], "1")

    def test_within_a_season_gives_the_following_episode(self):
        eps1 = stb_episodes(self.show, self.s1, [1, 2])
        result = resolve([self.s1], {"1": eps1}, self.s1, eps1[0])
        self.assertEqual(result, NextUp(self.s1, eps1[1]))

    def test_stb_episode_numbers_as_strings(self):
        s1 = {"id": "1", "series": ["1"], "cmd": "CMD1"}
        s2 = {"id": "2", "series": ["4", "5"], "cmd": "CMD2"}
        eps1 = stb_episodes(self.show, s1, ["1"])
        eps2 = stb_episodes(self.show, s2, ["4", "5"])
        result = resolve([s1, s2], {"1": eps1, "2": eps2}, s1, eps1[0])
        self.assertIsInstance(result, NextUp)
        self.assertEqual(result.item["series"], "4")
        self.assertEqual(result.item["number"], "4")

    def test_last_season_has_no_next_episode(self):
        eps1 = stb_episodes(self.show, self.s1, [1, 2])
        self.assertIsNone(resolve([self.s1], {"1": eps1}, self.s1, eps1[1]))

    def test_xtream_next_season(self):
        s1 = {"id": "1", "episodes": [{"id": "e0", "episode_num": 1}]}
        s2 = {
            "id": "2",
            "episodes": [
                {"id": "e1", "episode_num": 1, "title": "Pilot", "container_extension": "mkv"}
            ],
        }
        eps1 = format_xtream_episodes(s1, "http://x", "mp4", "u", "p")
        eps2 = format_xtream_episodes(s2, "http://x", "mp4", "u", "p")
        result = resolve([s1, s2], {"1": eps1, "2": eps2}, s1, eps1[0])
        self.assertIsInstance(result, NextUp)
        self.assertIs(result.season, s2)
        self.assertEqual(result.item["cmd"], "http://x/series/u/p/e1.mkv")
        self.assertEqual(result.item["name"], "Pilot")

    def test_unloaded_next_season_needs_episodes(self):
        eps1 = stb_episodes(self.show, self.s1, [1, 2])
        result = resolve([self.s1, self.s2], {"1": eps1}, self.s1, eps1[1])
        self.assertEqual(result, NeedEpisodes(self.s2))

    def test_empty_following_season_is_skipped(self):
        s3 = {"id": "3", "series": [1], "cmd": "CMD3"}
        eps1 = stb_episodes(self.show, self.s1, [1, 2])
        eps3 = stb_episodes(self.show, s3, [1])
        result = resolve([self.s1, self.s2, s3], {"1": eps1, "2": [], "3": eps3}, self.s1, eps1[1])
        self.assertEqual(result, NextUp(s3, eps3[0]))

    def test_unknown_episode_gives_none(self):
        eps1 = stb_episodes(self.show, self.s1, [1, 2])
        stranger = {**self.show, "series": 9, "season_id": "1", "season": "1"}
        self.assertIsNone(resolve([self.s1], {"1": eps1}, self.s1, stranger))

    def test_episode_keys_differ_across_seasons(self):
        eps1 = stb_episodes(self.show, self.s1, [1])
        eps2 = stb_episodes(self.show, self.s2, [1])
        self.assertNotEqual(episode_key(eps1[0]), episode_key(eps2[0]))


class NextEpisodeEdgeTests(unittest.TestCase):
    def test_evicted_current_season_needs_episodes(self):
        show = {"id": "S1", "name": "Show"}
        s1 = {"id": "1", "series": [1], "cmd": "C1"}
        ep = stb_episodes(show, s1, [1])[0]
        self.assertEqual(resolve([s1], {}, s1, ep), NeedEpisodes(s1))

    def test_key_less_seasons_are_located_by_identity(self):
        show = {"id": "S1", "name": "Show"}
        a, b, c = {"cmd": ""}, {"cmd": ""}, {"cmd": ""}
        for s in (a, b, c):
            s["name"] = ""
        loaded = {}
        eps = {
            id(a): stb_episodes(show, {"id": "1", "cmd": "A"}, [1]),
            id(b): stb_episodes(show, {"id": "2", "cmd": "B"}, [1]),
            id(c): stb_episodes(show, {"id": "3", "cmd": "C"}, [1]),
        }
        result = next_episode(
            [a, b, c],
            lambda s: eps[id(s)],
            b,
            eps[id(b)][0],
            season_key=item_key,
            episode_key=episode_key,
        )
        self.assertEqual(result, NextUp(c, eps[id(c)][0]))


class NextMovieTests(unittest.TestCase):
    def test_middle_and_end(self):
        items = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        self.assertEqual(next_movie(items, items[1], key=item_key), items[2])
        self.assertIsNone(next_movie(items, items[2], key=item_key))
        self.assertIsNone(next_movie(items, {"id": "zzz"}, key=item_key))


class PlayContextTests(unittest.TestCase):
    def test_play_context_fills_the_librarys_keys(self):
        self.assertEqual(
            play_context({"start_over": True}),
            {
                "start_over": True,
                "series": None,
                "season": None,
                "episodes": None,
                "genre_items": None,
            },
        )

    def test_has_next_needs_a_season_or_a_genre(self):
        series, season = {"id": "s"}, {"id": "1"}
        self.assertTrue(has_next("episode", play_context({"series": series, "season": season})))
        self.assertFalse(has_next("episode", play_context({"series": series})))
        self.assertTrue(has_next("movie", play_context({"genre_items": [{"id": 1}]})))
        self.assertFalse(has_next("movie", play_context({})))

    def test_episode_label(self):
        self.assertEqual(episode_label({"number": "2"}, {"number": "1"}), "S2 · E1")
        self.assertEqual(episode_label(None, {"season": "3", "episode_num": "4"}), "S3 · E4")
        self.assertEqual(episode_label({}, {"series": 5}), "E5")

    def test_genre_title(self):
        groups = [("5", "Films"), ("6", "Drama")]
        self.assertEqual(genre_title({"category_id": "6"}, groups), "Drama")
        self.assertEqual(genre_title({"tv_genre_id": 5}, groups), "Films")
        self.assertEqual(genre_title({"genre": "Noir"}, groups), "Noir")
        self.assertEqual(genre_title({}, groups), "")

    def test_following_reads_the_play_context(self):
        series, s1, s2 = {"id": "s"}, {"id": "1"}, {"id": "2"}
        loaded = {"1": [{"id": "a"}, {"id": "b"}], "2": None}

        def resolve(kind, context, item, seasons=None):
            return following(
                kind,
                context,
                item,
                seasons=lambda _series: seasons,
                episodes=lambda _series, season: loaded[season["id"]],
                key=item_key,
                episode_key=item_key,
            )

        episode_context = play_context({"series": series, "season": s1})
        self.assertEqual(resolve("episode", episode_context, {"id": "a"}), NextUp(s1, {"id": "b"}))
        self.assertIsNone(resolve("episode", episode_context, {"id": "b"}))  # seasons unknown
        self.assertEqual(
            resolve("episode", episode_context, {"id": "b"}, [s1, s2]), NeedEpisodes(s2)
        )
        self.assertIsNone(resolve("episode", play_context({"series": series}), {"id": "a"}))
        films = play_context({"genre_items": [{"id": 1}, {"id": 2}]})
        self.assertEqual(resolve("movie", films, {"id": 1}), NextUp(None, {"id": 2}))
        self.assertIsNone(resolve("movie", films, {"id": 2}))

    def test_advanced_context_moves_to_the_next_season(self):
        old = play_context({"start_over": True, "series": {"id": "s"}, "season": {"id": "1"}})
        new = advanced_context(old, NextUp({"id": "2"}, {"id": "e"}), [{"id": "e"}])
        self.assertEqual(new["season"], {"id": "2"})
        self.assertEqual(new["episodes"], [{"id": "e"}])
        self.assertFalse(new["start_over"])
        self.assertTrue(old["start_over"])  # the old context is left alone
        movie_context = play_context({"genre_items": [{"id": 1}], "start_over": True})
        self.assertEqual(
            advanced_context(movie_context, NextUp(None, {"id": 1}), None),
            {**movie_context, "start_over": False},
        )


if __name__ == "__main__":
    unittest.main()
