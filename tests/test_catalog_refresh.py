"""Catalog freshness, cache writes and season reuse (once the old channel list's tests)."""

import os
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication
from http_fixtures import LocalSource
from job_fixtures import ImmediateRunner
from mixed_fixtures import mixed_config, shut_hub
import orjson
from timer_fixtures import wait_until

from services.catalog import UNEXPECTED_ANSWER_MESSAGE, CatalogService
from services.catalogs import Catalogs
from services.m3u import parse_m3u
from services.provider_manager import ProviderManager
from services.workers import not_a_playlist
from tests.test_catalog_service import FakeFactory, make_service
from tests.test_series_view import FakePosters
from ui.library.series_view import SeriesView


class ProviderCacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.config = SimpleNamespace(
            get_config_dir=lambda: self.directory.name,
            selected_provider_name="Fixture",
            ssl_verify=True,
        )
        self.manager = ProviderManager(self.config)
        self.manager.providers = [
            {"name": "Fixture", "type": "M3UPLAYLIST", "url": "https://example.test/a"}
        ]
        self.manager.current_provider = self.manager.providers[0]
        self.progress = SimpleNamespace(emit=lambda message: None)

    def tearDown(self):
        self.manager._cache_writer.shutdown(wait=True)
        self.directory.cleanup()

    def test_category_save_does_not_extend_catalog_freshness(self):
        manager = self.manager
        with patch("services.provider_manager.time.time", return_value=1000):
            manager.current_provider_content = {"itv": {"categories": []}}
            manager.mark_content_fresh("itv")
            manager.save_provider()
        with patch(
            "services.provider_manager.time.time", return_value=1000 + manager.CONTENT_CACHE_TTL - 1
        ):
            manager.current_provider_content["itv"]["contents"] = {"news": []}
            manager.current_provider_content["series"] = {"categories": []}
            manager.mark_content_fresh("series")
            manager.save_provider()
            manager.set_current_provider(self.progress)
            self.assertFalse(manager.is_content_stale("itv"))
        manager._cache_writer.submit(lambda: None).result(timeout=5)
        with patch(
            "services.provider_manager.time.time", return_value=1000 + manager.CONTENT_CACHE_TTL
        ):
            manager.set_current_provider(self.progress)
            self.assertTrue(manager.is_content_stale("itv"))
            self.assertFalse(manager.is_content_stale("series"))

    def test_only_a_list_past_its_time_for_these_settings_is_expired(self):  # 2.8
        manager = self.manager
        with patch("services.provider_manager.time.time", return_value=1000):
            manager.current_provider_content = {"vod": {"categories": []}}
            manager.mark_content_fresh("vod")
            self.assertFalse(manager.content_expired("vod"))  # fresh
        later = 1000 + manager.CONTENT_CACHE_TTL
        with patch("services.provider_manager.time.time", return_value=later):
            self.assertTrue(manager.content_expired("vod"))  # shown while it loads again
            self.assertFalse(manager.content_expired("series"))  # never saved
            manager.providers[0]["url"] = "https://example.test/other"
            self.assertFalse(manager.content_expired("vod"))  # another account's list

    def test_same_name_connection_change_rejects_cached_channels(self):
        manager = self.manager
        manager.current_provider_content = {"itv": {"contents": [{"name": "Old"}]}}
        manager.mark_content_fresh("itv")
        manager.save_provider()
        manager._cache_writer.submit(lambda: None).result(timeout=5)
        manager.providers[0]["url"] = "https://example.test/b"
        manager.set_current_provider(self.progress)
        self.assertNotIn("itv", manager.current_provider_content)
        self.assertTrue(manager.is_content_stale("itv"))

    def test_invalidation_prevents_pending_write_resurrecting_cache(self):
        manager = self.manager
        gate = threading.Event()
        manager._cache_writer.submit(gate.wait, 5)
        try:
            manager.current_provider_content = {"itv": {"contents": [{"name": "Old"}]}}
            manager.mark_content_fresh("itv")
            manager.save_provider()
            manager.invalidate_provider_cache("Fixture")
        finally:
            gate.set()
        manager._cache_writer.submit(lambda: None).result(timeout=5)
        manager.set_current_provider(self.progress)
        self.assertNotIn("itv", manager.current_provider_content)
        self.assertEqual(manager.get_all_providers_cached_content(), [])

    @staticmethod
    def playlist_cached_before_the_split():
        """An M3U playlist as 2.0 cached it before group titles with ";" were split."""
        return {
            "categories": [
                {"id": "*", "title": "All"},
                {"id": "Animation;Kids", "title": "Animation;Kids"},
                {"id": "Kids", "title": "Kids"},
            ],
            "contents": [
                {
                    "id": 1,
                    "name": "Toons",
                    "group": "Animation;Kids",
                    "tv_genre_id": "Animation;Kids",
                    "number": "1",
                    "cmd": "http://example.test/1",
                },
                {
                    "id": 2,
                    "name": "Blocks",
                    "group": "Kids",
                    "tv_genre_id": "Kids",
                    "number": "2",
                    "cmd": "http://example.test/2",
                },
            ],
            "sorted_channels": {"Animation;Kids": [0], "Kids": [1]},
        }

    def cache(self, payload):
        manager = self.manager
        manager.current_provider_content = {"itv": payload}
        manager.mark_content_fresh("itv")
        manager.save_provider()
        manager._cache_writer.submit(lambda: None).result(timeout=5)

    def test_a_playlist_cached_before_the_split_is_split_when_it_loads(self):
        # Upgraders see split groups at once, with no download.
        self.cache(self.playlist_cached_before_the_split())
        manager = self.manager
        manager.set_current_provider(self.progress)
        itv = manager.current_provider_content["itv"]
        self.assertEqual([c["title"] for c in itv["categories"]], ["All", "Animation", "Kids"])
        self.assertEqual(itv["sorted_channels"], {"Animation": [0], "Kids": [0, 1]})
        self.assertEqual([c["name"] for c in itv["contents"]], ["Toons", "Blocks"])
        self.assertFalse(manager.is_content_stale("itv"))  # still the cached, fresh content
        searched = dict(manager.get_all_providers_cached_content())["Fixture"]["itv"]
        self.assertEqual(searched["sorted_channels"], {"Animation": [0], "Kids": [0, 1]})

    def test_xtream_and_stb_caches_are_read_as_they_are(self):
        manager = self.manager
        for kind in ("XTREAM", "STB"):
            with self.subTest(kind=kind):
                manager.providers[0]["type"] = kind
                manager.current_provider = manager.providers[0]
                self.cache(self.playlist_cached_before_the_split())
                # The cached content as a provider setup and search read it (no handshake).
                itv = dict(manager.get_all_providers_cached_content())["Fixture"]["itv"]
                self.assertEqual(itv["sorted_channels"], {"Animation;Kids": [0], "Kids": [1]})


def _content(name):
    return {
        "categories": [{"id": "*", "title": "All"}],
        "contents": [{"id": 1, "name": name, "cmd": "http://example.test/1"}],
        "sorted_channels": {"*": [0]},
    }


class CatalogFreshnessTests(unittest.TestCase):
    """CatalogService serves a section from the provider cache only while it is fresh."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = SimpleNamespace(
            get_config_dir=lambda: self.directory.name,
            selected_provider_name="Fixture",
            ssl_verify=True,
            channel_epg=False,
            save_config=lambda: None,
        )
        self.manager = ProviderManager(self.config)
        self.addCleanup(self.manager._cache_writer.shutdown, wait=True)
        self.manager.providers = [
            {"name": "Fixture", "type": "M3UPLAYLIST", "url": "https://example.test/a"}
        ]
        self.manager.set_current_provider(SimpleNamespace(emit=lambda message: None))
        self.factory = FakeFactory()
        self.factory.results["m3u"] = {"result": _content("Fresh")}
        epg = SimpleNamespace(get_programs_for_channel=lambda channel: [])
        self.service = CatalogService(
            self.config, self.manager, epg, runner=ImmediateRunner(), factory=self.factory
        )

    def loads(self):
        return [call for call in self.factory.calls if call[0] == "m3u"]

    def request_live(self):
        self.service.request_section("live")
        QCoreApplication.processEvents()  # a cached section is reported from the event loop

    def test_a_section_is_reloaded_only_after_six_hours(self):
        self.assertEqual(ProviderManager.CONTENT_CACHE_TTL, 6 * 60 * 60)
        with patch("services.provider_manager.time.time", return_value=1000):
            self.request_live()
        self.assertEqual(len(self.loads()), 1)
        with patch("services.provider_manager.time.time", return_value=1000 + 6 * 60 * 60 - 1):
            self.request_live()
        self.assertEqual(len(self.loads()), 1)
        with patch("services.provider_manager.time.time", return_value=1000 + 6 * 60 * 60):
            self.request_live()
        self.assertEqual(len(self.loads()), 2)

    def test_a_same_name_provider_edit_reloads_instead_of_using_the_old_channels(self):
        self.request_live()
        self.manager._cache_writer.submit(lambda: None).result(timeout=5)
        self.manager.providers[0]["url"] = "https://example.test/b"
        self.manager.set_current_provider(SimpleNamespace(emit=lambda message: None))
        self.assertIsNone(self.service.items("live", "*"))
        self.factory.results["m3u"] = {"result": _content("Edited")}
        self.request_live()
        self.assertEqual(len(self.loads()), 2)
        self.assertEqual([i["name"] for i in self.service.items("live", "*")], ["Edited"])


class SeasonReuseTests(unittest.TestCase):
    """Back from a season's episodes shows the cached seasons, named as the provider names them."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_cached_seasons_are_reused_without_renaming_them(self):
        service, _, factory, _ = make_service(
            {"name": "X", "type": "XTREAM", "url": "h:1", "username": "u", "password": "p"}
        )
        factory.results["xtream_series"] = {
            "result": {"seasons": [{"id": "1", "name": "Season 1", "episodes": []}]}
        }
        series = {"id": "7", "name": "Show"}
        service.request_seasons(series)
        service.request_seasons(series)  # Back to the seasons: served from the cache
        QCoreApplication.processEvents()
        self.assertEqual(len(factory.calls), 1)
        [season] = service.seasons(series)
        self.assertEqual((season["name"], season["o_name"]), ("Show.Season 1", "Season 1"))

        view = SeriesView(FakePosters())
        self.addCleanup(view.close)
        self.assertEqual(view.season_label(season, 0), "Season 1")


PLAYLIST = "#EXTM3U\n" + "".join(
    f'#EXTINF:-1 group-title="News",Chan {i}\nhttp://127.0.0.1:9/{i}.ts\n' for i in range(3)
)


class PlaylistAnswerTests(unittest.TestCase):
    """A playlist's refresh answered 200 with an empty body or a page (an expired account's):
    the saved channels stay and the refresh says it failed, as an Xtream panel's answer that
    is no list does (before 2.8.12: no channels were stored over them, marked fresh)."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_an_answer_that_is_no_playlist_keeps_the_saved_channels(self):
        for label, body in (("empty", b""), ("page", b"<html><body>Expired</body></html>")):
            with self.subTest(label):
                self.refresh_answered_with(body)

    def refresh_answered_with(self, body):
        source = LocalSource({"/get.php": (200, {"Content-Type": "text/plain"}, body)})
        self.addCleanup(source.close)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        provider = {"type": "M3UPLAYLIST", "name": "Playlist", "url": source.url + "/get.php"}
        config, registry = mixed_config(
            directory.name, [provider], {"Playlist": parse_m3u(PLAYLIST, categorize=True)}
        )
        self.addCleanup(registry._cache_writer.shutdown, wait=True)
        path = registry._provider_cache_name("Playlist")
        with open(path, "rb") as f:
            cache = orjson.loads(f.read())
        cache["_cache"]["refreshed_at"]["itv"] = time.time() - 7 * 3600  # past its time
        with open(path, "wb") as f:
            f.write(orjson.dumps(cache))
        config.selected_provider_name = "Playlist"
        hub = Catalogs(config, registry, runner=ImmediateRunner())
        self.addCleanup(shut_hub, hub)
        hub.ensure("Playlist")
        self.assertTrue(wait_until(lambda: hub.is_ready("Playlist")))
        catalog = hub.catalog("Playlist")
        signals = []
        catalog.sectionReady.connect(lambda section: signals.append(("ready", section)))
        catalog.sectionFailed.connect(lambda section, m: signals.append(("failed", section, m)))
        catalog.request_section("live")
        self.assertTrue(wait_until(lambda: signals))
        self.assertEqual(signals, [("failed", "live", UNEXPECTED_ANSWER_MESSAGE)])
        self.assertEqual(len(catalog.items("live", "*") or []), 3)
        catalog.provider_manager._cache_writer.shutdown(wait=True)
        with open(path, "rb") as f:
            self.assertEqual(len(orjson.loads(f.read())["itv"]["contents"]), 3)
        self.assertTrue(catalog.provider_manager.is_content_stale("itv"))  # tried next time

    def test_what_is_a_playlist(self):
        for text, playlist in (
            ("", False),
            ("  \r\n", False),
            ("<!DOCTYPE html><html></html>", False),
            ("﻿<html>\n#EXTM3U\nhttp://a.test/1.ts\n</html>", False),
            ("Account expired", False),
            ("#EXTM3U\n", True),  # it says it is one: a playlist listing nothing (yet)
            ("http://a.test/1.ts\n", True),  # links alone: each a channel
            (PLAYLIST, True),
        ):
            with self.subTest(text=text[:20]):
                parsed = parse_m3u(text, categorize=True)
                self.assertEqual(not not_a_playlist(text, parsed), playlist)


if __name__ == "__main__":
    unittest.main()
