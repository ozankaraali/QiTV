"""Catalog freshness, cache writes and season reuse (once the old channel list's tests)."""

import os
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from services.catalog import CatalogService
from services.jobs import ImmediateRunner
from services.provider_manager import ProviderManager
from tests.test_catalog_service import FakeFactory, make_service
from tests.test_series_view import FakePosters
from ui.library.series_view import SeriesView


class ProviderCacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.config = SimpleNamespace(
            get_config_dir=lambda: self.directory.name,
            selected_provider_name="Fixture",
            prefer_https=False,
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
            prefer_https=False,
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
        epg = SimpleNamespace(get_programs_for_channel=lambda channel, start, limit: [])
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


if __name__ == "__main__":
    unittest.main()
