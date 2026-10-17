import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from mixed_fixtures import DEMO, HARBOR, live_content, mixed_config
from test_catalog_lifecycle import RealSetupFactory
from test_catalog_service import FakeWorker

from services.catalogs import Catalogs
from services.jobs import ImmediateRunner
from services.provider_ids import with_provider


class RecordingFactory(RealSetupFactory):
    """The real provider setup (recording whose it is); the guide is a fake."""

    def __init__(self):
        super().__init__()
        self.setups = []
        self.epg_loads = []

    def provider_setup(self, provider_manager, force_refresh):
        self.setups.append(provider_manager._pinned_provider)
        return super().provider_setup(provider_manager, force_refresh)

    def epg(self, epg_manager, force_refresh):
        self.epg_loads.append(epg_manager.name)
        return FakeWorker({})


class FakeEpg:
    def __init__(self, name=""):
        self.name = name
        self.released = 0

    def release(self):
        self.released += 1


class CatalogsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="qitv-catalogs-")
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
        self.in_use = set()
        self.epgs = []
        self.catalogs = Catalogs(
            self.config,
            self.registry,
            runner=ImmediateRunner(),
            factory=self.factory,
            make_epg=self.make_epg,
            in_use=lambda: self.in_use,
        )
        self.links, self.failures = [], []
        self.catalogs.linkReady.connect(lambda rid, url: self.links.append((rid, url)))
        self.catalogs.linkFailed.connect(lambda rid, msg: self.failures.append((rid, msg)))
        self.addCleanup(self.catalogs.shutdown)

    def make_epg(self, manager):
        epg = FakeEpg(manager._pinned_provider)
        self.epgs.append(epg)
        return epg

    def ready_links(self):
        QTest.qWait(30)  # a link answer is emitted from the event loop
        return list(self.links)

    def delete_provider(self, name):
        self.registry.providers[:] = [p for p in self.registry.providers if p["name"] != name]

    def test_a_catalog_per_provider_created_on_first_use(self):
        self.assertEqual(self.catalogs.names(), ["Demo TV", "Harbor IPTV"])
        self.assertIs(self.catalogs.catalog("Demo TV"), self.catalogs.catalog("Demo TV"))
        self.assertIsNot(self.catalogs.catalog("Demo TV"), self.catalogs.catalog("Harbor IPTV"))
        self.assertEqual(self.factory.setups, [])  # creating one starts no network

    def test_links_go_to_the_items_own_provider(self):
        harbor = with_provider({"id": 9, "cmd": "http://h/9.ts"}, "Harbor IPTV")
        request = self.catalogs.request_link(harbor, section="live")
        self.assertEqual(self.ready_links(), [(request, "http://h/9.ts")])
        self.assertEqual(self.factory.setups, ["Harbor IPTV"])  # signed in on first use
        self.assertTrue(self.catalogs.is_ready("Harbor IPTV"))
        self.assertFalse(self.catalogs.is_ready("Demo TV"))

    def test_the_ready_providers_hold_their_content_for_search_to_read(self):
        self.assertEqual(self.catalogs.loaded_contents(), {})  # nothing is set up yet
        self.catalogs.ensure("Harbor IPTV")
        QTest.qWait(30)
        loaded = self.catalogs.loaded_contents()
        self.assertEqual(list(loaded), ["Harbor IPTV"])
        self.assertEqual(loaded["Harbor IPTV"]["itv"]["contents"][0]["name"], "Four")

    def test_link_ids_are_unique_across_providers(self):
        ids = {
            self.catalogs.request_link(with_provider({"cmd": "x"}, n), section="live")
            for n in ("Demo TV", "Harbor IPTV", "Demo TV")
        }
        self.assertEqual(len(ids), 3)
        self.assertEqual({rid for rid, _ in self.ready_links()}, ids)

    def test_an_unknown_provider_fails_the_link(self):
        request = self.catalogs.request_link(with_provider({"cmd": "x"}, "Gone"), section="live")
        QTest.qWait(30)
        self.assertEqual([rid for rid, _ in self.failures], [request])

    def test_a_provider_deleted_while_it_plays_stays_until_out_of_use(self):  # Review Focus 3
        self.in_use = {"Harbor IPTV"}
        old = self.catalogs.catalog("Harbor IPTV")
        self.delete_provider("Harbor IPTV")
        self.catalogs.reload()
        self.assertIs(self.catalogs.catalog("Harbor IPTV"), old)
        self.assertNotIn("Harbor IPTV", self.catalogs.names())
        self.in_use = set()
        self.catalogs.reload()
        self.assertNotIn("Harbor IPTV", self.catalogs._by_name)

    def test_an_edited_provider_is_rebuilt_on_next_use(self):
        old = self.catalogs.catalog("Demo TV")
        self.registry.providers[0]["url"] = "http://127.0.0.1:9/other.m3u"
        self.catalogs.reload()
        self.assertIsNot(self.catalogs.catalog("Demo TV"), old)

    def test_an_unchanged_provider_keeps_its_catalog(self):
        old = self.catalogs.catalog("Demo TV")
        self.catalogs.reload()
        self.assertIs(self.catalogs.catalog("Demo TV"), old)

    def test_use_guides_releases_the_others(self):
        self.catalogs.catalog("Demo TV")
        self.catalogs.catalog("Harbor IPTV")
        self.catalogs.use_guides({"Demo TV"})
        self.assertEqual([e.released for e in self.epgs], [0, 1])

    def test_only_guides_in_use_are_loaded(self):
        self.catalogs.use_guides({"Demo TV"})
        QTest.qWait(50)
        self.assertEqual(self.factory.epg_loads, ["Demo TV"])
        self.catalogs.use_guides({"Harbor IPTV"})
        QTest.qWait(50)
        self.assertEqual(self.factory.epg_loads, ["Demo TV", "Harbor IPTV"])
        self.assertEqual([e.name for e in self.epgs if e.released], ["Demo TV"])

    def test_a_provider_set_up_for_browsing_loads_no_guide_once_guides_are_chosen(self):
        self.catalogs.use_guides({"Demo TV"})
        self.catalogs.ensure("Harbor IPTV")
        QTest.qWait(50)
        self.assertEqual(self.factory.epg_loads, ["Demo TV"])

    def test_every_guide_loads_until_guides_are_chosen(self):  # one provider: as before 2.1
        self.catalogs.ensure("Demo TV")
        QTest.qWait(50)
        self.assertEqual(self.factory.epg_loads, ["Demo TV"])


if __name__ == "__main__":
    unittest.main()
