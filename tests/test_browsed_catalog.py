import logging
import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication
from mixed_fixtures import DEMO, HARBOR, live_content, mixed_config
from test_catalogs import RecordingFactory

from services.browsed_catalog import BrowsedCatalog
from services.catalog import ALL_GROUP_ID
from services.catalogs import Catalogs
from services.jobs import ImmediateRunner
from services.provider_ids import provider_of, with_provider
from services.provider_manager import ProviderManager


class FakeEpg:
    def release(self):
        pass

    def save_index(self):
        pass


class Progress:
    def emit(self, text):
        pass


class BrowsedCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="qitv-browsed-")
        self.addCleanup(directory.cleanup)
        self.config, self.registry = mixed_config(
            directory.name,
            [DEMO, HARBOR],
            {
                "Demo TV": live_content(["One", "Two"], {"News": [0, 1]}),
                "Harbor IPTV": live_content(["Four"], {"News": [0]}),
            },
        )
        self.addCleanup(self.registry._cache_writer.shutdown, wait=True)
        self.factory = RecordingFactory()
        self.catalogs = Catalogs(
            self.config,
            self.registry,
            runner=ImmediateRunner(),
            factory=self.factory,
            make_epg=lambda manager: FakeEpg(),
        )
        self.addCleanup(self.catalogs.shutdown)

    def view(self, source="Demo TV"):
        self.config.selected_provider_name = source
        return BrowsedCatalog(
            self.catalogs,
            self.config,
            source_key="selected_provider_name",
            all_key="tv_all_providers",
        )

    def library_view(self):
        return BrowsedCatalog(
            self.catalogs,
            self.config,
            source_key="library_provider_name",
            all_key="library_all_providers",
        )

    def test_one_provider_behaves_like_its_catalog(self):
        view = self.view("Demo TV")
        view.select_provider()
        self.assertEqual(view.current_provider_name(), "Demo TV")
        catalog = self.catalogs.catalog("Demo TV")
        self.assertEqual(view.groups("live"), catalog.groups("live"))
        self.assertTrue(view.groups("live"))
        self.assertEqual(view.items("live", "News"), catalog.items("live", "News"))
        self.assertEqual({provider_of(i) for i in view.items("live", ALL_GROUP_ID)}, {"Demo TV"})
        self.assertEqual(view.provider_names(), ["Demo TV", "Harbor IPTV"])
        self.assertEqual(view.sections(), catalog.sections())
        self.assertIs(view.provider_manager, catalog.provider_manager)

    def test_signals_of_other_providers_are_not_this_pages(self):
        view = self.view("Demo TV")
        ready = QSignalSpy(view.sectionReady)
        self.catalogs.catalog("Harbor IPTV").sectionReady.emit("live")
        self.assertEqual(ready.count(), 0)
        self.catalogs.catalog("Demo TV").sectionReady.emit("live")
        self.assertEqual(ready.count(), 1)

    def test_a_view_made_after_the_catalogs_still_hears_them(self):
        self.catalogs.catalog("Demo TV")
        view = self.view("Demo TV")
        ready = QSignalSpy(view.groupReady)
        self.catalogs.catalog("Demo TV").groupReady.emit("live", "News")
        self.assertEqual(ready.count(), 1)

    def test_the_two_pages_keep_their_own_source(self):
        tv, library = self.view("Demo TV"), self.library_view()
        self.assertEqual(library.source(), "Demo TV")  # empty: the selected provider
        library.select_provider("Harbor IPTV")
        self.assertEqual((tv.source(), library.source()), ("Demo TV", "Harbor IPTV"))
        self.assertEqual(self.config.library_provider_name, "Harbor IPTV")
        self.assertEqual(self.config.selected_provider_name, "Demo TV")
        self.assertEqual(self.factory.setups, ["Harbor IPTV"])

    def test_the_library_follows_the_selected_provider_until_it_chooses(self):
        tv, library = self.view("Demo TV"), self.library_view()
        tv.select_provider("Harbor IPTV")
        self.assertEqual(self.config.selected_provider_name, "Harbor IPTV")
        self.assertEqual(library.source(), "Harbor IPTV")
        self.assertEqual(self.config.library_provider_name, "")

    def test_a_signal_follows_the_source_when_it_changes(self):
        tv, library = self.view("Demo TV"), self.library_view()
        ready = QSignalSpy(library.sectionReady)
        tv.select_provider("Harbor IPTV")  # the Library follows
        self.catalogs.catalog("Demo TV").sectionReady.emit("live")
        self.assertEqual(ready.count(), 0)
        self.catalogs.catalog("Harbor IPTV").sectionReady.emit("live")
        self.assertEqual(ready.count(), 1)

    def test_content_versions_of_two_providers_never_match(self):
        view = self.view("Demo TV")
        first = view.content_version()
        view.select_provider("Harbor IPTV")
        self.assertNotEqual(view.content_version(), first)

    def test_a_missing_source_falls_back_to_the_first_provider(self):
        self.assertEqual(self.view("Gone").source(), "Demo TV")
        self.assertEqual(self.view("").source(), "Demo TV")

    def test_no_providers_is_empty_not_an_error(self):
        self.registry.providers[:] = []
        view = self.view("")
        self.assertEqual(view.source(), "")
        self.assertEqual(view.provider_names(), [])
        self.assertEqual(view.current_provider(), {})
        self.assertEqual(view.current_provider_name(), "")
        self.assertEqual(view.groups("live"), [])
        self.assertIsNone(view.items("live", ALL_GROUP_ID))
        self.assertIsNone(view.max_connections())
        self.assertFalse(view.is_loading())
        view.select_provider()  # nothing to set up
        view.request_section("live")
        self.assertEqual(self.factory.setups, [])

    def test_selecting_an_unknown_provider_changes_nothing(self):
        view = self.view("Demo TV")
        with self.assertLogs("services.browsed_catalog", "WARNING"):
            view.select_provider("Gone")
        self.assertEqual(self.config.selected_provider_name, "Demo TV")
        self.assertEqual(self.factory.setups, [])

    def test_links_come_from_the_hub_whatever_the_provider(self):
        view = self.view("Demo TV")
        links = []
        view.linkReady.connect(lambda rid, url: links.append((rid, url)))
        harbor = with_provider({"id": 9, "cmd": "http://h/9.ts"}, "Harbor IPTV")
        request = view.request_link(harbor, section="live")
        QTest.qWait(30)
        self.assertEqual(links, [(request, "http://h/9.ts")])

    def test_the_catalog_for_an_item_is_its_providers(self):
        view = self.view("Demo TV")
        harbor = with_provider({"id": 9}, "Harbor IPTV")
        self.assertIs(view.catalog_for(harbor), self.catalogs.catalog("Harbor IPTV"))
        self.assertIs(view.catalog_for({"id": 1}), self.catalogs.catalog("Demo TV"))

    def test_seasons_of_a_gone_provider_are_unknown_not_an_error(self):
        view = self.view("Demo TV")
        gone = with_provider({"id": 1}, "Gone")
        self.assertIsNone(view.seasons(gone))
        view.request_seasons(gone)

    def test_it_does_not_mix_yet(self):
        self.assertFalse(self.view("Demo TV").all_providers())

    def test_the_new_settings_have_their_defaults(self):
        self.assertFalse(self.config.tv_all_providers)
        self.assertEqual(self.config.library_provider_name, "")
        self.assertFalse(self.config.library_all_providers)


class PinnedManagerTests(unittest.TestCase):
    def test_a_pinned_manager_fails_for_a_provider_that_is_gone(self):
        with tempfile.TemporaryDirectory(prefix="qitv-pinned-") as directory:
            config, registry = mixed_config(directory, [DEMO], {})
            self.addCleanup(registry._cache_writer.shutdown, wait=True)
            manager = ProviderManager(config, provider_name="Gone", providers=registry.providers)
            self.addCleanup(manager._cache_writer.shutdown, wait=True)
            with self.assertLogs("services.provider_manager", logging.WARNING):
                with self.assertRaises(LookupError):
                    manager.set_current_provider(Progress())
            self.assertEqual(manager.current_provider, {})


if __name__ == "__main__":
    unittest.main()
