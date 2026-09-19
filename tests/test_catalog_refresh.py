import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMainWindow

from channel_list import ChannelList
from config_manager import ConfigManager
from epg_manager import EpgManager
from image_manager import ImageManager
from provider_manager import ProviderManager


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
        with patch("provider_manager.time.time", return_value=1000):
            manager.current_provider_content = {"itv": {"categories": []}}
            manager.mark_content_fresh("itv")
            manager.save_provider()
        with patch("provider_manager.time.time", return_value=1000 + manager.CONTENT_CACHE_TTL - 1):
            manager.current_provider_content["itv"]["contents"] = {"news": []}
            manager.current_provider_content["series"] = {"categories": []}
            manager.mark_content_fresh("series")
            manager.save_provider()
            manager.set_current_provider(self.progress)
            self.assertFalse(manager.is_content_stale("itv"))
        manager._cache_writer.submit(lambda: None).result(timeout=5)
        with patch("provider_manager.time.time", return_value=1000 + manager.CONTENT_CACHE_TTL):
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


class PlayerSignals(QMainWindow):
    mediaEnded = Signal()
    positionChanged = Signal(int, int)
    backRequested = Signal()
    forwardRequested = Signal()
    channelNextRequested = Signal()
    channelPrevRequested = Signal()


class CatalogNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        config_path = str(Path(self.directory.name) / "config.json")
        with patch.object(ConfigManager, "_get_config_path", return_value=config_path):
            with patch.object(ConfigManager, "_migrate_old_config"):
                self.config = ConfigManager()
        self.config.epg_source = "No Source"
        self.config.channel_epg = False
        self.config.channel_logos = False
        self.config.show_stb_content_info = False
        self.config.selected_provider_name = "Fixture"
        self.manager = ProviderManager(self.config)
        self.manager.providers = [{"name": "Fixture", "type": "XTREAM", "url": ""}]
        self.manager.current_provider = self.manager.providers[0]
        self.images = ImageManager(self.config)
        self.epg = EpgManager(self.config, self.manager)
        self.player = PlayerSignals()
        with patch.object(ChannelList, "set_provider"):
            self.window = ChannelList(
                self.app, self.player, self.config, self.manager, self.images, self.epg
            )
        self.window.content_refresh_timer.stop()

    def tearDown(self):
        self.window.cancel_content_render()
        self.window.stop_image_loading()
        self.wait_until(lambda: not getattr(self.window, "_retired_image_loaders", []))
        self.window.refresh_on_air_timer.stop()
        self.window.deleteLater()
        self.player.deleteLater()
        self.app.processEvents()
        self.manager._cache_writer.shutdown(wait=True)
        self.directory.cleanup()

    def wait_until(self, predicate):
        for _ in range(500):
            self.app.processEvents()
            if predicate():
                return
            QTest.qWait(10)
        self.fail("Background UI operation did not complete")

    def test_back_reuses_cached_seasons_without_mutating_names(self):
        window = self.window
        series = {"id": "7", "name": "Show"}
        seasons = [{"id": "1", "number": "1", "o_name": "Season 1", "name": "Show.Season 1"}]
        self.manager.current_provider_content = {"series": {"seasons": {"7": seasons}}}
        window.content_type = "series"
        window.current_series = series
        window.current_season = seasons[0]
        window.navigation_stack = [("series", series, "1")]
        window.display_content([{"number": "1", "ename": "Episode 1"}], content="episode")
        with patch.object(
            window, "load_xtream_series_info", side_effect=AssertionError("refetched")
        ):
            window.go_back()
        self.assertEqual(window.current_list_content, "season")
        self.assertEqual(window.content_list.currentItem().text(1), "Season 1")
        self.assertEqual(seasons[0]["name"], "Show.Season 1")

    def test_logos_follow_items_after_sort_and_retire_on_back(self):
        window = self.window
        self.config.channel_logos = True
        urls = []
        for color in ("red", "blue"):
            path = Path(self.directory.name) / f"{color}.png"
            image = QImage(8, 8, QImage.Format_RGB32)
            image.fill(QColor(color))
            image.save(str(path))
            urls.append((f"http://example.test/{color}", str(path)))
        paths = dict(urls)

        async def cached_image(session, url, iconified):
            return paths[url]

        rows = [
            {"id": 1, "number": "2", "name": "Zed", "logo": urls[0][0]},
            {"id": 2, "number": "1", "name": "Alpha", "logo": urls[1][0]},
        ]
        with patch.object(self.images, "cache_image_from_url", side_effect=cached_image):
            window.display_content(rows, content="channel")
            window.content_list.sortItems(1, Qt.AscendingOrder)
            self.wait_until(lambda: window.image_loader is None)
            for row, expected in enumerate(("blue", "red")):
                icon = window.content_list.topLevelItem(row).icon(1)
                self.assertFalse(icon.isNull())
                self.assertEqual(icon.pixmap(8, 8).toImage().pixelColor(4, 4), QColor(expected))
            window.display_content(rows, content="channel")
            window.display_categories([{"id": "news", "title": "News"}])
            self.wait_until(lambda: not getattr(window, "_retired_image_loaders", []))
        self.assertEqual(window.content_list.topLevelItem(0).text(0), "News")
        self.assertTrue(window.content_list.topLevelItem(0).icon(0).isNull())

    def test_back_interrupts_large_population_without_late_rows(self):
        window = self.window
        category = {"id": "news", "title": "News"}
        rows = [{"id": i, "number": str(i), "name": f"Channel {i}"} for i in range(3000)]
        self.manager.current_provider_content = {
            "itv": {"categories": [category], "contents": rows}
        }
        self.manager.mark_content_fresh("itv")
        window.current_category = category
        window.navigation_stack = [("root", None, "News")]
        rows_when_clicked = []

        def click_back():
            rows_when_clicked.append(window.content_list.topLevelItemCount())
            window.top_bar.back_button.click()

        window.display_content(rows, content="channel")
        QTimer.singleShot(0, click_back)
        self.wait_until(lambda: window.current_list_content == "category")
        QTest.qWait(30)
        self.assertLess(rows_when_clicked[0], len(rows))
        self.assertEqual(window.content_list.topLevelItemCount(), 1)
        self.assertEqual(window.content_list.currentItem().text(0), "News")

    def test_refresh_preserves_category_selection_and_sort_after_population(self):
        window = self.window
        rows = [{"id": i, "number": str(i), "name": f"Channel {i:04d}"} for i in range(600)]
        other = {"id": 999, "number": "999", "name": "Other category"}
        window.current_category = {"id": "news", "title": "News"}
        self.manager.current_provider_content = {
            "itv": {"contents": rows + [other], "sorted_channels": {"news": list(range(600))}}
        }
        window.display_content(rows, content="channel")
        self.wait_until(lambda: window._content_renderer is None)
        window.content_list.sortItems(0, Qt.DescendingOrder)
        selected = window.content_list.findItems("500", Qt.MatchExactly, 0)[0]
        window.content_list.setCurrentItem(selected)
        window.refresh_channels()
        self.wait_until(lambda: window._content_renderer is None)
        self.assertEqual(window.content_list.topLevelItemCount(), 600)
        self.assertEqual(window.content_list.topLevelItem(0).text(0), "599")
        self.assertEqual(window.content_list.currentItem().data(0, Qt.UserRole)["data"]["id"], 500)

    def test_numeric_and_epg_sort_keys_preserve_display_values(self):
        window = self.window
        rows = [
            {"id": 1, "number": "10", "name": "Zed"},
            {"id": 2, "number": "002", "name": "Alpha"},
            {"id": 3, "number": "", "name": "Missing"},
        ]
        window.display_content(rows, content="channel")
        self.assertEqual(
            [window.content_list.topLevelItem(i).text(0) for i in range(3)],
            ["002", "10", ""],
        )
        window.content_list.sortItems(1, Qt.AscendingOrder)
        self.assertEqual(window.content_list.topLevelItem(0).text(1), "Alpha")
        window.content_list.setColumnCount(4)
        for i in range(3):
            item = window.content_list.topLevelItem(i)
            item.setData(2, Qt.UserRole, [75, None, 20][i])
            item.setData(3, Qt.UserRole, ["Zulu", "", "Alpha"][i])
        window.content_list.sortItems(2, Qt.AscendingOrder)
        self.assertEqual(window.content_list.topLevelItem(0).data(2, Qt.UserRole), 20)
        self.assertIsNone(window.content_list.topLevelItem(2).data(2, Qt.UserRole))
        window.content_list.sortItems(3, Qt.AscendingOrder)
        self.assertEqual(window.content_list.topLevelItem(0).data(3, Qt.UserRole), "")


if __name__ == "__main__":
    unittest.main()
