import logging
import os
import tempfile
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication
from mixed_fixtures import DEMO, HARBOR, live_content, mixed_config
import orjson
from test_catalog_service import FakeWorker
from test_catalogs import RecordingFactory

from services.browsed_catalog import BrowsedCatalog
from services.catalog import ALL_GROUP_ID
from services.catalogs import Catalogs
from services.jobs import ImmediateRunner
from services.navigation import NavModel
from services.provider_ids import (
    ALL_PROVIDERS,
    mixed_group_id,
    provider_of,
    split_group_id,
    with_provider,
)
from services.provider_manager import ProviderManager


class FakeEpg:
    def __init__(self, name=""):
        self.name = name  # the recording factory notes whose guide is loaded

    def release(self):
        pass

    def save_index(self):
        pass


class Progress:
    def emit(self, text):
        pass


def demo_content() -> dict:
    """24 channels in two groups of 12."""
    return live_content(
        [f"Demo {n}" for n in range(24)], {"News": list(range(12)), "Sports": list(range(12, 24))}
    )


def harbor_content() -> dict:
    """69 channels: group 41 ("TR: Haber") has 41 of them, group 42 ("UK: News") 28."""
    content = live_content(
        [f"Harbor {n}" for n in range(69)], {"41": list(range(41)), "42": list(range(41, 69))}
    )
    titles = {"41": "TR: Haber", "42": "UK: News"}
    for category in content["categories"]:
        category["title"] = titles.get(category["id"], category["title"])
    return content


class ScriptedFactory(RecordingFactory):
    """The real setups; a provider named in `fail_setup` loads its cache, then fails."""

    def __init__(self):
        super().__init__()
        self.failures = {}

    def fail_setup(self, name, message):
        self.failures[name] = message

    def provider_setup(self, provider_manager, force_refresh):
        name = provider_manager._pinned_provider
        message = self.failures.get(name)
        if message is None:
            return super().provider_setup(provider_manager, force_refresh)
        self.setups.append(name)
        return FakeWorker(
            fail=message, on_run=lambda: provider_manager.set_current_provider(Progress())
        )


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
                "Demo TV": demo_content(),
                "Harbor IPTV": harbor_content(),
            },
        )
        self.addCleanup(self.registry._cache_writer.shutdown, wait=True)
        self.factory = ScriptedFactory()
        self.catalogs = Catalogs(
            self.config,
            self.registry,
            runner=ImmediateRunner(),
            factory=self.factory,
            make_epg=lambda manager: FakeEpg(manager._pinned_provider),
        )
        self.addCleanup(self.catalogs.shutdown)

    def view(self, source="Demo TV"):
        """A TV page showing `source` (ALL_PROVIDERS: every provider, set up from the caches)."""
        self.config.selected_provider_name = "Demo TV" if source == ALL_PROVIDERS else source
        view = BrowsedCatalog(
            self.catalogs,
            self.config,
            source_key="selected_provider_name",
            all_key="tv_all_providers",
        )
        if source == ALL_PROVIDERS:
            view.select_provider(ALL_PROVIDERS)
        return view

    def remove_cache(self, name):
        os.remove(self.registry._provider_cache_name(name))

    def finish_setup(self, name, content):
        """The provider's content arrives (its cache is written) and its setup runs again."""
        provider = self.catalogs.provider(name)
        cache = {
            "itv": content,
            "_cache": {
                "identity": self.registry.provider_cache_identity(provider),
                "refreshed_at": {"itv": time.time()},
            },
        }
        with open(self.registry._provider_cache_name(name), "wb") as f:
            f.write(orjson.dumps(cache))
        self.catalogs.select(name)

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

    def test_a_page_shows_one_provider_until_it_chooses_all(self):
        self.assertFalse(self.view("Demo TV").all_providers())

    def test_all_providers_lists_each_providers_groups_under_its_name(self):
        view = self.view(ALL_PROVIDERS)
        groups = view.groups("live")
        self.assertEqual(
            [(g.title, g.header) for g in groups],
            [
                ("All", False),
                ("Demo TV", True),
                ("News", False),
                ("Sports", False),
                ("Harbor IPTV", True),
                ("TR: Haber", False),
                ("UK: News", False),
            ],
        )
        self.assertEqual(split_group_id(groups[5].id), ("Harbor IPTV", "41"))
        self.assertEqual(groups[0].id, ALL_GROUP_ID)
        self.assertEqual(groups[0].count, 93)
        self.assertEqual([g.provider for g in groups[1:3]], ["Demo TV", "Demo TV"])

    def test_the_mixed_all_is_every_provider_in_settings_order(self):
        items = self.view(ALL_PROVIDERS).items("live", ALL_GROUP_ID)
        self.assertEqual([provider_of(i) for i in items], ["Demo TV"] * 24 + ["Harbor IPTV"] * 69)

    def test_a_mixed_group_is_that_providers_group(self):
        view = self.view(ALL_PROVIDERS)
        items = view.items("live", mixed_group_id("Harbor IPTV", "42"))
        self.assertEqual(len(items), 28)
        self.assertEqual({provider_of(i) for i in items}, {"Harbor IPTV"})
        self.assertIsNone(view.items("live", mixed_group_id("Harbor IPTV", "")))
        self.assertIsNone(view.items("live", mixed_group_id("Gone", "42")))

    def test_the_mixed_lists_are_built_once_per_change(self):
        view = self.view(ALL_PROVIDERS)
        self.assertIs(view.items("live", ALL_GROUP_ID), view.items("live", ALL_GROUP_ID))
        self.assertEqual(view.groups("live"), view.groups("live"))
        first = view.items("live", ALL_GROUP_ID)
        self.finish_setup("Demo TV", live_content(["Only"], {"News": [0]}))
        second = view.items("live", ALL_GROUP_ID)
        self.assertIsNot(second, first)
        self.assertEqual(len(second), 1 + 69)

    def test_all_providers_mode_is_the_pages_setting(self):
        view = self.view(ALL_PROVIDERS)
        self.assertTrue(self.config.tv_all_providers)
        self.assertEqual(self.config.selected_provider_name, "Demo TV")  # the single one stays
        self.assertTrue(view.all_providers())
        self.assertEqual(view.source(), ALL_PROVIDERS)
        self.assertEqual(view.current_provider_name(), ALL_PROVIDERS)
        self.assertEqual(view.current_provider(), {"name": ALL_PROVIDERS, "type": "ALL"})
        self.assertEqual(sorted(self.factory.setups), ["Demo TV", "Harbor IPTV"])
        view.select_provider("Harbor IPTV")
        self.assertFalse(self.config.tv_all_providers)
        self.assertEqual(view.source(), "Harbor IPTV")
        self.assertEqual(view.current_provider_name(), "Harbor IPTV")

    def test_selecting_all_says_the_lists_are_ready_at_once(self):
        view = self.view("Demo TV")
        ready = QSignalSpy(view.providerReady)
        view.select_provider(ALL_PROVIDERS)
        self.assertEqual([ready.at(i)[0] for i in range(ready.count())][-1], ALL_PROVIDERS)

    def test_all_providers_has_the_sections_any_provider_has(self):
        self.assertEqual(self.view(ALL_PROVIDERS).sections(), ["live", "movies", "series"])
        self.registry.providers[:] = [dict(DEMO)]
        self.assertEqual(self.view(ALL_PROVIDERS).sections(), ["live"])

    def test_a_provider_that_fails_to_sign_in_keeps_its_saved_channels(self):
        self.factory.fail_setup("Harbor IPTV", "The provider refused the login")
        items = self.view(ALL_PROVIDERS).items("live", mixed_group_id("Harbor IPTV", "41"))
        self.assertEqual(len(items), 41)

    def test_moving_through_all_does_not_sign_a_failed_provider_in_again(self):
        # Polish 2: a reset_to (no provider named) ensured every provider, so the one that
        # just failed signed in again with every move.
        self.factory.fail_setup("Harbor IPTV", "The provider refused the login")
        view = self.view(ALL_PROVIDERS)
        view.request_section("live")
        self.assertEqual(self.factory.setups.count("Harbor IPTV"), 1)
        view.request_section("live")
        view.request_section("live")
        self.assertEqual(self.factory.setups.count("Harbor IPTV"), 1)
        view.sign_in_again("Harbor IPTV")  # the user asks (a refresh)
        self.assertEqual(self.factory.setups.count("Harbor IPTV"), 2)

    def test_a_provider_never_loaded_shows_its_header_and_starts_loading(self):
        self.remove_cache("Harbor IPTV")
        view = self.view(ALL_PROVIDERS)
        self.assertIn("Harbor IPTV", [g.title for g in view.groups("live") if g.header])
        self.assertIn("Harbor IPTV", self.factory.setups)
        rows = [g for g in view.groups("live") if g.provider == "Harbor IPTV" and not g.header]
        self.assertEqual(rows, [])
        self.finish_setup("Harbor IPTV", harbor_content())
        rows = [g for g in view.groups("live") if g.provider == "Harbor IPTV" and not g.header]
        self.assertTrue(rows)

    def test_content_version_changes_when_any_provider_does(self):
        view = self.view(ALL_PROVIDERS)
        before = view.content_version()
        self.assertEqual(view.content_version(), before)
        self.finish_setup("Harbor IPTV", harbor_content())
        self.assertNotEqual(view.content_version(), before)
        again = view.content_version()
        self.finish_setup("Demo TV", demo_content())
        self.assertNotEqual(view.content_version(), again)

    def test_signals_of_every_provider_reach_the_all_page(self):
        view = self.view(ALL_PROVIDERS)
        ready, group = QSignalSpy(view.sectionReady), QSignalSpy(view.groupReady)
        self.catalogs.catalog("Harbor IPTV").sectionReady.emit("live")
        self.catalogs.catalog("Demo TV").sectionReady.emit("live")
        self.assertEqual(ready.count(), 2)
        self.catalogs.catalog("Harbor IPTV").groupReady.emit("live", "41")
        self.assertEqual(group.at(0), ["live", mixed_group_id("Harbor IPTV", "41")])

    def test_a_group_failure_names_the_mixed_group(self):
        view = self.view(ALL_PROVIDERS)
        failed = QSignalSpy(view.groupFailed)
        self.catalogs.catalog("Harbor IPTV").groupFailed.emit("live", "41", "No")
        self.assertEqual(failed.at(0), ["live", mixed_group_id("Harbor IPTV", "41"), "No"])

    def test_requests_go_to_the_provider_of_the_group(self):
        view = self.view(ALL_PROVIDERS)
        asked = []
        for name in ("Demo TV", "Harbor IPTV"):
            catalog = self.catalogs.catalog(name)
            catalog.request_group = lambda s, g, n=name: asked.append(("group", n, s, g))
            catalog.request_section = lambda s, force=False, n=name: asked.append(("section", n, s))
        view.request_group("live", mixed_group_id("Harbor IPTV", "41"))
        view.request_group("live", ALL_GROUP_ID)
        self.assertEqual(
            asked,
            [
                ("group", "Harbor IPTV", "live", "41"),
                ("section", "Demo TV", "live"),
                ("section", "Harbor IPTV", "live"),
            ],
        )

    def test_a_single_page_keeps_its_group_ids(self):
        view = self.view("Demo TV")
        group = QSignalSpy(view.groupReady)
        self.catalogs.catalog("Demo TV").groupReady.emit("live", "News")
        self.assertEqual(group.at(0), ["live", "News"])

    def test_the_new_settings_have_their_defaults(self):
        self.assertFalse(self.config.tv_all_providers)
        self.assertEqual(self.config.library_provider_name, "")
        self.assertFalse(self.config.library_all_providers)

    def give_guide(self, name, titles):
        catalog = self.catalogs.catalog(name)
        catalog.programmes = lambda channel, now, limit=8: titles

    def test_a_channels_programmes_come_from_its_provider(self):
        self.give_guide("Harbor IPTV", ["Ana Haber"])
        self.give_guide("Demo TV", ["Wrong"])
        channel = with_provider({"id": 2, "xmltv_id": "harbor-news"}, "Harbor IPTV")
        for source in (ALL_PROVIDERS, "Demo TV"):
            self.assertEqual(self.view(source).programmes(channel, None), ["Ana Haber"])

    def test_a_guide_in_use_repaints_whichever_provider_the_list_shows(self):
        view = self.view("Demo TV")
        spy = QSignalSpy(view.epgReady)
        self.catalogs.catalog("Harbor IPTV").epgReady.emit()
        self.assertEqual(spy.count(), 1)


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


class RenewingFactory(ScriptedFactory):
    """Xtream lists answer Harbor's channels; the passwords they were asked with are kept."""

    def __init__(self):
        super().__init__()
        self.passwords = []

    def xtream(self, base_url, username, password, *args):
        self.passwords.append(password)
        return FakeWorker(harbor_content())


class EditedProviderTests(unittest.TestCase):
    """Settings edits a provider a page shows (final review I4)."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="qitv-edited-")
        self.addCleanup(directory.cleanup)
        self.config, self.registry = mixed_config(
            directory.name,
            [DEMO, HARBOR],
            {"Demo TV": demo_content(), "Harbor IPTV": harbor_content()},
        )
        self.addCleanup(self.registry._cache_writer.shutdown, wait=True)
        self.factory = RenewingFactory()
        self.views = []
        self.catalogs = Catalogs(
            self.config,
            self.registry,
            runner=ImmediateRunner(),
            factory=self.factory,
            make_epg=lambda manager: FakeEpg(manager._pinned_provider),
            in_use=self.in_use,
        )
        self.addCleanup(self.catalogs.shutdown)
        self.config.selected_provider_name = "Demo TV"

    def in_use(self):
        """As the window's: the providers its pages show."""
        return set().union(*(view.shown_providers() for view in self.views))

    def page(self, source_key="selected_provider_name", all_key="tv_all_providers"):
        view = BrowsedCatalog(self.catalogs, self.config, source_key=source_key, all_key=all_key)
        self.views.append(view)
        return view

    def edit_harbor(self, password):
        """As Settings → Save: the list edited in place, Harbor's cache dropped, the catalogs
        reloaded."""
        edited = [dict(p) for p in self.registry.providers]
        edited[1]["password"] = password
        self.registry.providers[:] = edited
        self.catalogs.catalog("Harbor IPTV").provider_manager.invalidate_provider_cache(
            "Harbor IPTV"
        )
        self.catalogs.reload()

    def test_editing_a_provider_all_providers_shows_brings_its_channels_back(self):
        view = self.page()
        view.select_provider(ALL_PROVIDERS)
        nav = NavModel(view, self.config)
        nav.reset_to("live", ALL_GROUP_ID)
        QTest.qWait(10)
        before = len(nav.children(nav.path[-1]))
        self.assertEqual(before, 24 + 69)
        self.edit_harbor("renewed")  # not the selected provider
        QTest.qWait(10)
        self.assertEqual(len(nav.children(nav.path[-1])), before)
        manager = self.catalogs.catalog("Harbor IPTV").provider_manager
        self.assertEqual(manager.current_provider["password"], "renewed")
        self.assertEqual(self.factory.passwords, ["renewed"])

    def test_editing_the_provider_only_the_library_shows_gives_it_the_new_record(self):
        self.page().select_provider()  # the TV page: Demo TV
        self.config.library_provider_name = "Harbor IPTV"
        library = self.page("library_provider_name", "library_all_providers")
        library.select_provider()
        QTest.qWait(10)
        ready = []
        library.providerReady.connect(ready.append)
        self.edit_harbor("renewed")
        manager = self.catalogs.catalog("Harbor IPTV").provider_manager
        self.assertEqual(manager.current_provider["password"], "renewed")
        self.assertEqual(ready, ["Harbor IPTV"])  # the Library opens its lists again
        library.request_section("live")
        QTest.qWait(10)
        self.assertEqual(self.factory.passwords, ["renewed"])
        self.assertTrue(library.has_content("live"))

    def test_a_provider_added_while_all_providers_shows_is_set_up(self):
        view = self.page()
        view.select_provider(ALL_PROVIDERS)
        self.registry.providers.append(
            {"type": "M3UPLAYLIST", "name": "Third", "url": "http://127.0.0.1:9/third.m3u"}
        )
        self.catalogs.reload()
        self.assertIn("Third", self.factory.setups)


if __name__ == "__main__":
    unittest.main()
