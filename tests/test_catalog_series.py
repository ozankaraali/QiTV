import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from services.catalog import format_xtream_episodes, normalize_seasons
from tests.test_catalog_service import ManualRunner, collect, make_service


class SeriesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_normalize_seasons(self):
        items = normalize_seasons(
            "Show",
            [
                {"id": "2", "name": "Season 2"},
                {"id": "x", "name": "Book IV"},
                {"id": "y", "name": "Extras"},
            ],
        )
        self.assertEqual([i["number"] for i in items], ["2", "4", "3"])
        self.assertEqual(items[0]["name"], "Show.Season 2")
        self.assertEqual(items[0]["o_name"], "Season 2")

    def test_format_xtream_episodes(self):
        season = {
            "episodes": [
                {
                    "id": "77",
                    "episode_num": "3",
                    "title": "Pilot",
                    "container_extension": "mkv",
                    "info": {"movie_image": "i.jpg"},
                }
            ]
        }
        [ep] = format_xtream_episodes(season, "http://b", "ts", "u", "p")
        self.assertEqual(ep["cmd"], "http://b/series/u/p/77.mkv")
        self.assertEqual((ep["number"], ep["name"], ep["logo"]), ("3", "Pilot", "i.jpg"))

    def test_xtream_seasons_and_episodes(self):
        provider = {"name": "X", "type": "XTREAM", "url": "h:1", "username": "u", "password": "p"}
        service, providers, factory, _ = make_service(provider)
        providers.current_provider_content["series"] = {
            "stream_base": "http://b",
            "stream_ext": "ts",
        }
        factory.results["xtream_series"] = {
            "result": {
                "resolved_base": "http://b",
                "seasons": [
                    {"id": "1", "name": "Season 1", "episodes": [{"id": "5", "episode_num": 1}]}
                ],
            }
        }
        ready = collect(service.seasonsReady)
        series = {"id": 42, "name": "Show"}
        self.assertIsNone(service.seasons(series))
        service.request_seasons(series)
        self.assertEqual(ready, [("42",)])
        [season] = service.seasons(series)
        self.assertEqual(season["name"], "Show.Season 1")
        [episode] = service.episodes(series, season)
        self.assertEqual(episode["cmd"], "http://b/series/u/p/5.ts")

    def test_stb_seasons_and_episodes(self):
        provider = {"name": "S", "type": "STB", "url": "http://portal/c/"}
        service, providers, factory, _ = make_service(provider)
        series = {"id": 9, "name": "Show", "category_id": "3"}
        factory.results["stb_list"] = {
            "result": {"items": [{"id": "9:1", "name": "Season 1", "cmd": "C1", "series": [1, 2]}]}
        }
        service.request_seasons(series)
        [season] = service.seasons(series)
        kwargs = factory.calls[-1][2]
        self.assertEqual(
            (kwargs["movie_id"], kwargs["season_id"], kwargs["category_id"]), (9, 0, "3")
        )
        self.assertIsNone(service.episodes(series, season))
        factory.results["stb_list"] = {
            "result": {"season_id": "9:1", "items": [{"id": "9:1", "cmd": "C1", "series": [1, 2]}]}
        }
        ready = collect(service.episodesReady)
        service.request_episodes(series, season)
        self.assertEqual(ready, [("9", "9:1")])
        episodes = service.episodes(series, season)
        self.assertEqual([e["series"] for e in episodes], [1, 2])
        self.assertEqual(episodes[0]["cmd"], "C1")
        self.assertEqual(episodes[0]["name"], "Show")

    def test_links(self):
        service, _, _, _ = make_service({"name": "X", "type": "XTREAM", "url": "h:1"})
        ready, failed = collect(service.linkReady), collect(service.linkFailed)
        rid = service.request_link({"cmd": " http://x/1.ts "}, section="live")
        empty = service.request_link({"cmd": ""}, section="live")
        QCoreApplication.processEvents()
        self.assertEqual(ready, [(rid, "http://x/1.ts")])
        self.assertEqual(failed, [(empty, "This item has no stream address")])

    def test_stb_link_uses_worker_and_drops_stale(self):
        provider = {"name": "S", "type": "STB", "url": "http://portal/c/"}
        service, _, factory, _ = make_service(provider)
        factory.results["link"] = {"result": {"link": " http://stream/1 "}}
        ready = collect(service.linkReady)
        rid = service.request_link({"cmd": "ffmpeg http://localhost/ch/1_"}, section="live")
        self.assertEqual(ready, [(rid, "http://stream/1")])
        kwargs = factory.calls[-1][2]
        self.assertEqual(kwargs["content_type"], "itv")
        factory.results["link"] = {
            "result": {"link": "http://stream/2"},
            "on_run": lambda: service.select_provider(),
        }
        factory.results["provider_setup"] = {"result": {"name": "S"}}
        factory.results["epg"] = {"result": {}}
        service.request_link({"cmd": "x"}, section="live")
        self.assertEqual(len(ready), 1)

    def test_stale_seasons_after_switch_are_dropped(self):
        provider = {"name": "X", "type": "XTREAM", "url": "h:1", "username": "u", "password": "p"}
        service, providers, factory, _ = make_service(provider)
        factory.results["xtream_series"] = {
            "result": {"seasons": [{"id": "1", "name": "Season 1"}]},
            "on_run": lambda: service.select_provider(),
        }
        factory.results["provider_setup"] = {"result": {"name": "X"}}
        factory.results["epg"] = {"result": {}}
        ready = collect(service.seasonsReady)
        service.request_seasons({"id": 42, "name": "Show"})
        self.assertEqual(ready, [])
        self.assertIsNone(service.seasons({"id": 42}))

    def test_duplicate_season_requests_start_one_worker(self):
        provider = {"name": "X", "type": "XTREAM", "url": "h:1", "username": "u", "password": "p"}
        runner = ManualRunner()
        service, _, factory, _ = make_service(provider, runner=runner)
        factory.results["xtream_series"] = {"result": {"seasons": [{"id": "1", "name": "S1"}]}}
        ready = collect(service.seasonsReady)
        series = {"id": 42, "name": "Show"}
        service.request_seasons(series)
        service.request_seasons(series)
        self.assertEqual(len(factory.calls), 1)
        runner.complete(0)
        self.assertEqual(ready, [("42",)])
        service.request_seasons(series)  # now served from the cache
        self.assertEqual(len(factory.calls), 1)
        QCoreApplication.processEvents()
        self.assertEqual(ready, [("42",), ("42",)])

    def test_duplicate_episode_requests_start_one_worker(self):
        provider = {"name": "S", "type": "STB", "url": "http://portal/c/"}
        runner = ManualRunner()
        service, _, factory, _ = make_service(provider, runner=runner)
        factory.results["stb_list"] = {
            "result": {"items": [{"id": "9:1", "cmd": "C1", "series": [1]}]}
        }
        series, season = {"id": 9, "name": "Show"}, {"id": "9:1"}
        ready = collect(service.episodesReady)
        service.request_episodes(series, season)
        service.request_episodes(series, season)
        self.assertEqual(len(factory.calls), 1)
        runner.complete(0)
        self.assertEqual(ready, [("9", "9:1")])
        service.request_episodes(series, season)
        self.assertEqual(len(factory.calls), 1)

    def test_pending_cleared_on_error_and_switch(self):
        provider = {"name": "X", "type": "XTREAM", "url": "h:1"}
        runner = ManualRunner()
        service, _, factory, _ = make_service(provider, runner=runner)
        factory.results["xtream_series"] = {"fail": "boom"}
        failed = collect(service.seasonsFailed)
        series = {"id": 1, "name": "Show"}
        service.request_seasons(series)
        runner.complete(0)
        self.assertEqual(failed, [("1", "boom")])
        service.request_seasons(series)  # error released the pending slot
        self.assertEqual(len(factory.calls), 2)
        service.select_provider()  # switch clears pending
        service.request_seasons(series)
        self.assertEqual([c[0] for c in factory.calls].count("xtream_series"), 3)

    def test_episodes_failed(self):
        service, _, factory, _ = make_service({"name": "S", "type": "STB", "url": "http://p/c/"})
        factory.results["stb_list"] = {"fail": "nope"}
        failed = collect(service.episodesFailed)
        service.request_episodes({"id": 9, "name": "Show"}, {"id": "9:1"})
        self.assertEqual(failed, [("9", "9:1", "nope")])

    def test_stale_episodes_after_switch_are_dropped(self):
        service, _, factory, _ = make_service({"name": "S", "type": "STB", "url": "http://p/c/"})
        factory.results["stb_list"] = {
            "result": {"items": [{"id": "9:1", "cmd": "C", "series": [1]}]},
            "on_run": lambda: service.select_provider(),
        }
        factory.results["provider_setup"] = {"result": {"name": "S"}}
        factory.results["epg"] = {"result": {}}
        ready = collect(service.episodesReady)
        series, season = {"id": 9, "name": "Show"}, {"id": "9:1"}
        service.request_episodes(series, season)
        self.assertEqual(ready, [])
        self.assertIsNone(service.episodes(series, season))

    def test_unsupported_provider_has_no_series(self):
        service, _, factory, _ = make_service({"name": "M", "type": "M3UPLAYLIST", "url": "/a"})
        seasons_failed, episodes_failed = collect(service.seasonsFailed), collect(
            service.episodesFailed
        )
        service.request_seasons({"id": 1})
        service.request_episodes({"id": 1}, {"id": 2})
        QCoreApplication.processEvents()
        self.assertEqual(seasons_failed, [("1", "This provider has no series")])
        self.assertEqual(episodes_failed, [("1", "2", "This provider has no series")])
        self.assertEqual(factory.calls, [])

    def test_unknown_link_section_fails(self):
        service, _, _, _ = make_service({"name": "S", "type": "STB", "url": "http://p/c/"})
        failed = collect(service.linkFailed)
        rid = service.request_link({"cmd": "x"}, section="bogus")
        QCoreApplication.processEvents()
        self.assertEqual(failed, [(rid, "Unknown section: bogus")])

    def test_stb_link_arguments(self):
        service, _, factory, _ = make_service({"name": "S", "type": "STB", "url": "http://p/c/"})
        factory.results["link"] = {"result": {"link": "http://s/1"}}
        service.request_link({"cmd": "C", "series": 3}, section="movies", is_episode=True)
        kwargs = factory.calls[-1][2]
        self.assertEqual((kwargs["content_type"], kwargs["series_param"]), ("vod", 3))
        self.assertTrue(kwargs["is_episode"])

    def test_non_dict_series_cache_is_replaced(self):
        service, providers, _, _ = make_service({"name": "X", "type": "XTREAM", "url": "h:1"})
        providers.current_provider_content["series"] = []
        service._series_cache()["seasons"] = {"1": []}
        self.assertEqual(providers.current_provider_content["series"], {"seasons": {"1": []}})


if __name__ == "__main__":
    unittest.main()
