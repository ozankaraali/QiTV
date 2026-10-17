"""The channel list over every provider: the "All providers" source, the providers' headings,
the provider tag's rows, and the mixed lists' channels (Favorites, Recent, All)."""

import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QWidget
from mixed_fixtures import DEMO, HARBOR, live_content, mixed_config
from test_browsed_catalog import FakeEpg, ScriptedFactory

from services.browsed_catalog import BrowsedCatalog
from services.catalog import ALL_GROUP_ID
from services.catalogs import Catalogs
from services.jobs import ImmediateRunner
from services.navigation import FAVORITES_GROUP, RECENT_GROUP, NavModel, NavNode, item_key
from services.provider_ids import ALL_PROVIDERS, ALL_PROVIDERS_TITLE, mixed_group_id, provider_of
from services.zapping import ZapContext
from ui.overlays.channel_rows import ChannelRowsModel
from ui.overlays.drawer import Drawer


def demo_content() -> dict:
    """24 channels in News (the first 12) and Sports; the first is "News"."""
    names = ["News"] + [f"Demo {n}" for n in range(1, 24)]
    return live_content(names, {"News": list(range(12)), "Sports": list(range(12, 24))})


def harbor_content() -> dict:
    """69 channels in "TR: Haber" and "UK: News"; the first is also "News" (same id 1)."""
    names = ["News"] + [f"Harbor {n}" for n in range(1, 69)]
    content = live_content(names, {"41": list(range(41)), "42": list(range(41, 69))})
    titles = {"41": "TR: Haber", "42": "UK: News"}
    for category in content["categories"]:
        category["title"] = titles.get(category["id"], category["title"])
    return content


class MixedNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def build(self, providers, contents):
        directory = tempfile.TemporaryDirectory(prefix="qitv-mixed-nav-")
        self.addCleanup(directory.cleanup)
        config, registry = mixed_config(directory.name, providers, contents)
        self.addCleanup(registry._cache_writer.shutdown, wait=True)
        catalogs = Catalogs(
            config,
            registry,
            runner=ImmediateRunner(),
            factory=ScriptedFactory(),
            make_epg=lambda manager: FakeEpg(),
        )
        self.addCleanup(catalogs.shutdown)
        config.selected_provider_name = providers[0]["name"]
        view = BrowsedCatalog(
            catalogs, config, source_key="selected_provider_name", all_key="tv_all_providers"
        )
        config.watch_history = []
        return config, view, NavModel(view, config)

    def setUp(self):
        self.config, self.view, self.nav = self.build(
            [DEMO, HARBOR], {"Demo TV": demo_content(), "Harbor IPTV": harbor_content()}
        )

    def drawer(self):
        host = QWidget()
        host.resize(1280, 720)
        host.show()
        self.addCleanup(host.deleteLater)
        self.addCleanup(host.close)
        return Drawer(self.nav, None, lambda item: None, host)

    def open_all(self):
        """The channel list on "All providers", its live groups open."""
        self.nav.open(self.nav.children(None)[0])
        self.nav.reset_to("live")
        return self.nav.path[-1]

    def live_groups(self):
        return self.nav.children(self.open_all())

    # ---- the source row -----------------------------------------------------------------------

    def test_all_providers_row_comes_first_with_two_providers(self):
        rows = self.nav.children(None)
        self.assertEqual([n.key for n in rows][:3], [ALL_PROVIDERS, "Demo TV", "Harbor IPTV"])
        self.assertEqual(rows[0], NavNode("source", ALL_PROVIDERS, ALL_PROVIDERS_TITLE))
        self.assertEqual(rows[0].title, ALL_PROVIDERS_TITLE)
        _config, _view, single = self.build([DEMO], {"Demo TV": demo_content()})
        self.assertNotIn(ALL_PROVIDERS, [n.key for n in single.children(None)])

    def test_opening_the_all_providers_row_shows_every_provider(self):
        row = self.nav.children(None)[0]
        self.assertFalse(self.nav.needs_setup(row))  # nothing failed: it is only shown
        self.nav.open(row)
        self.assertTrue(self.config.tv_all_providers)
        self.assertEqual([n.key for n in self.nav.path], [ALL_PROVIDERS])
        self.assertEqual(self.nav.path[0].title, ALL_PROVIDERS_TITLE)
        self.assertFalse(self.nav.needs_setup(row))  # already on it

    # ---- the providers' headings ----------------------------------------------------------------

    def test_headers_are_not_openable_and_not_playable(self):
        groups = self.live_groups()
        headers = [n for n in groups if n.layer == "header"]
        self.assertEqual([n.title for n in headers], ["Demo TV", "Harbor IPTV"])
        before = self.nav.path
        for header in headers:
            self.assertFalse(self.nav.is_playable(header))
            self.nav.open(header)  # does nothing
        self.assertEqual(self.nav.path, before)
        self.assertEqual(
            [n.title for n in groups],
            ["All", "Demo TV", "News", "Sports", "Harbor IPTV", "TR: Haber", "UK: News"],
        )

    def test_up_and_down_in_the_drawer_skip_the_headings(self):
        self.open_all()
        drawer = self.drawer()
        drawer.open_drawer()
        self.assertEqual(drawer.highlighted().title, "All")
        walked = []
        for _ in range(5):
            drawer.move_by(1)
            walked.append(drawer.highlighted().title)
        self.assertEqual(walked, ["News", "Sports", "TR: Haber", "UK: News", "UK: News"])
        drawer.move_by(-1)
        drawer.move_by(-1)
        self.assertEqual(drawer.highlighted().title, "Sports")  # over Harbor IPTV's heading
        drawer.move_by(-1)
        drawer.move_by(-1)
        self.assertEqual(drawer.highlighted().title, "All")  # over Demo TV's
        drawer.move_by(-1)
        self.assertEqual(drawer.highlighted().title, "All")

    def test_a_click_on_a_heading_changes_nothing(self):
        self.open_all()
        drawer = self.drawer()
        drawer.open_drawer()
        right = drawer._columns["right"]
        row = next(i for i, n in enumerate(right.nodes) if n.layer == "header")
        played, path = [], self.nav.path
        drawer.playRequested.connect(lambda *a: played.append(a))
        drawer._on_row_clicked("right", row)
        self.assertEqual((drawer.highlighted().title, self.nav.path, played), ("All", path, []))

    def test_a_filter_matches_groups_not_headings(self):
        self.open_all()
        drawer = self.drawer()
        drawer.open_drawer()
        drawer.type_text("harbor")  # Harbor IPTV's heading is no group
        self.assertEqual(drawer._columns["right"].nodes, [])

    # ---- the mixed lists ----------------------------------------------------------------------

    def items_of(self, group_id):
        group = next(n for n in self.live_groups() if n.key == group_id)
        return self.nav.children(group)

    def test_same_ids_at_two_providers_stay_two_channels_in_all(self):
        both = [
            (provider_of(n.payload), n.title) for n in self.items_of(ALL_GROUP_ID) if n.key == "1"
        ]
        self.assertEqual(both, [("Demo TV", "News"), ("Harbor IPTV", "News")])
        self.config.favorites = ["News"]
        favorites = [(provider_of(n.payload), n.title) for n in self.items_of(FAVORITES_GROUP)]
        self.assertEqual(favorites, [("Demo TV", "News"), ("Harbor IPTV", "News")])
        keys = [(provider_of(i), item_key(i)) for i in self.nav._all_live_items()]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertIn(("Demo TV", "1"), keys)
        self.assertIn(("Harbor IPTV", "1"), keys)

    def test_numbers_in_the_mixed_all_find_the_first_provider(self):
        channels = [n.payload for n in self.items_of(ALL_GROUP_ID)]
        context = ZapContext(ALL_GROUP_ID, channels, fallback=channels)
        found = context.by_number("3")
        self.assertEqual((provider_of(found), found["number"]), ("Demo TV", "3"))
        # Number 30 is only Harbor's.
        self.assertEqual(provider_of(context.by_number("30")), "Harbor IPTV")

    def test_recent_lists_every_provider_newest_first_each_with_its_provider(self):
        def entry(provider, item_id, name):
            return {
                "provider_name": provider,
                "item_type": "channel",
                "item_data": {"id": item_id, "name": name},
            }

        self.config.watch_history = [
            entry("Harbor IPTV", 5, "Harbor 5"),
            entry("Demo TV", 5, "Demo 5"),
            entry("Gone", 9, "Lost"),  # a provider no longer in the list
            entry("Harbor IPTV", 5, "Harbor 5"),  # again: once
        ]
        recent = self.items_of(RECENT_GROUP)
        self.assertEqual(
            [(provider_of(n.payload), n.title) for n in recent],
            [("Harbor IPTV", "Harbor 5"), ("Demo TV", "Demo 5")],
        )

    def test_recent_with_one_provider_is_as_before(self):
        config, _view, nav = self.build([DEMO], {"Demo TV": demo_content()})
        config.watch_history = [
            {
                "provider_name": "Demo TV",
                "item_type": "channel",
                "item_data": {"id": 5, "name": "Demo 5"},
            }
        ]
        nav.open(nav.children(None)[0])
        nav.reset_to("live")
        recent = next(n for n in nav.children(nav.path[-1]) if n.key == RECENT_GROUP)
        self.assertEqual(nav.children(recent)[0].payload, {"id": 5, "name": "Demo 5"})
        self.assertFalse(nav.shows_provider(recent))  # one provider: nothing to tell apart

    # ---- where a channel names its provider -------------------------------------------------------

    def test_the_mixed_lists_show_their_providers_names(self):
        self.config.favorites = ["News"]
        by_key = {g.key: g for g in self.live_groups()}
        self.assertTrue(self.nav.shows_provider(by_key[FAVORITES_GROUP]))
        self.assertTrue(self.nav.shows_provider(by_key[ALL_GROUP_ID]))
        self.assertFalse(self.nav.shows_provider(by_key[mixed_group_id("Demo TV", "News")]))
        self.assertFalse(self.nav.shows_provider(None))
        self.assertTrue(self.nav.shows_provider(NavNode("group", RECENT_GROUP, "")))

    def test_all_names_its_providers_only_in_all_mode(self):
        self.nav.open(self.nav.children(None)[1])  # Demo TV alone
        self.nav.reset_to("live")
        group = next(n for n in self.nav.children(self.nav.path[-1]) if n.key == ALL_GROUP_ID)
        self.assertFalse(self.nav.shows_provider(group))

    def test_the_drawer_rows_carry_the_tag_in_the_mixed_lists_only(self):
        self.open_all()
        drawer = self.drawer()
        self.nav.reset_to("live", ALL_GROUP_ID)
        drawer.open_drawer()
        model = drawer._columns["right"].model
        tags = {model.index(r).data(ChannelRowsModel.ProviderRole) for r in range(3)}
        self.assertEqual(tags, {"Demo TV"})
        self.nav.reset_to("live", mixed_group_id("Harbor IPTV", "41"))
        drawer.open_drawer()
        self.assertEqual(model.index(0).data(ChannelRowsModel.ProviderRole), "")

    def test_a_provider_group_in_all_mode_lists_that_providers_channels(self):
        groups = self.live_groups()
        harbor_group = next(n for n in groups if n.key == mixed_group_id("Harbor IPTV", "41"))
        self.assertEqual(len(self.nav.children(harbor_group)), 41)


if __name__ == "__main__":
    unittest.main()
