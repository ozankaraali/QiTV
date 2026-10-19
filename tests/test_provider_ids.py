import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from job_fixtures import ImmediateRunner
from mixed_fixtures import DEMO, HARBOR, live_content, mixed_config
from test_catalog_lifecycle import RealSetupFactory
from test_catalog_service import FakeWorker

from services.catalog import ALL_GROUP_ID, CatalogService
from services.provider_ids import (
    mixed_group_id,
    provider_of,
    split_group_id,
    stamp,
    with_provider,
)
from services.provider_manager import ProviderManager
from services.zapping import ZapContext


class PinnedFactory(RealSetupFactory):
    """The real provider setup against a real ProviderManager; the guide is a fake."""

    def epg(self, epg_manager, force_refresh):
        return FakeWorker({})


class ProviderIdsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="qitv-mixed-")
        self.addCleanup(directory.cleanup)
        self.config, self.manager = mixed_config(
            directory.name,
            [DEMO, HARBOR],
            {
                "Demo TV": live_content(["One", "Two", "Three"], {"News": [0, 1], "Kids": [2]}),
                "Harbor IPTV": live_content(["Four"], {"News": [0]}),
            },
        )
        self.addCleanup(self.manager._cache_writer.shutdown, wait=True)

    def pinned_catalog(self, name):
        manager = ProviderManager(self.config, provider_name=name, providers=self.manager.providers)
        self.addCleanup(manager._cache_writer.shutdown, wait=True)
        catalog = CatalogService(
            self.config,
            manager,
            None,
            runner=ImmediateRunner(),
            factory=PinnedFactory(),
            provider_name=name,
        )
        catalog.select_provider()
        return catalog

    def test_stamp_and_read(self):
        items = [{"id": 1, "name": "One"}, {"id": 2, "name": "Two"}]
        stamp(items, "Harbor")
        self.assertEqual([provider_of(i) for i in items], ["Harbor", "Harbor"])
        self.assertEqual(provider_of({"id": 3}), "")
        copy = with_provider(items[0], "Demo")
        self.assertEqual((provider_of(copy), provider_of(items[0])), ("Demo", "Harbor"))

    def test_mixed_group_ids_round_trip(self):
        key = mixed_group_id("Harbor IPTV", "10")
        self.assertEqual(split_group_id(key), ("Harbor IPTV", "10"))
        self.assertEqual(split_group_id("10"), ("", "10"))

    def test_same_id_at_two_providers_is_two_channels(self):
        a = with_provider({"id": 7, "name": "News", "cmd": "http://a/7.ts"}, "A")
        b = with_provider({"id": 7, "name": "News", "cmd": "http://a/7.ts"}, "B")
        self.assertFalse(ZapContext.same(a, b))
        self.assertTrue(ZapContext.same(a, dict(a)))
        self.assertTrue(ZapContext.same({"id": 7}, {"id": 7}))  # unstamped: as today

    def test_catalog_items_carry_their_provider(self):
        catalog = self.pinned_catalog("Demo TV")
        items = catalog.items("live", ALL_GROUP_ID)
        self.assertTrue(items)
        self.assertEqual({provider_of(i) for i in items}, {"Demo TV"})
        self.assertEqual(self.config.selected_provider_name, "")  # pinned: never written

    def test_pinned_catalog_ignores_the_selected_provider(self):
        self.config.selected_provider_name = "Harbor IPTV"
        catalog = self.pinned_catalog("Demo TV")
        self.assertEqual(catalog.current_provider_name(), "Demo TV")
        self.assertEqual([i["name"] for i in catalog.items("live", "Kids")], ["Three"])
        self.assertEqual({g.provider for g in catalog.groups("live")}, {"Demo TV"})
        self.assertEqual(self.config.selected_provider_name, "Harbor IPTV")

    def test_has_content(self):
        catalog = self.pinned_catalog("Demo TV")
        self.assertTrue(catalog.has_content("live"))
        self.assertFalse(catalog.has_content("movies"))


if __name__ == "__main__":
    unittest.main()
