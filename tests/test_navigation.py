import os
from types import SimpleNamespace
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from services.catalog import Group
from services.navigation import FAVORITES_GROUP, RECENT_GROUP, NavModel, NavNode


class FakeCatalog(QObject):
    providerReady = Signal(str)
    sectionReady = Signal(str)
    groupReady = Signal(str, str)
    seasonsReady = Signal(str)
    episodesReady = Signal(str, str)

    def __init__(self):
        super().__init__()
        self.provider = "Home"
        self.selected = []
        self.requests = []
        self.live = {
            "10": [{"id": 1, "name": "One", "number": "1"}, {"id": 2, "name": "Two", "number": "2"}]
        }
        self.movies = {}
        self.season_map = {}

    def provider_names(self):
        return ["Home", "Backup"]

    def current_provider_name(self):
        return self.provider

    def select_provider(self, name=None, **kwargs):
        self.selected.append(name)

    def sections(self):
        return ["live", "movies", "series"]

    def request_section(self, section, **kwargs):
        self.requests.append(("section", section))

    def groups(self, section):
        if section == "live":
            return [Group("live", "10", "News", 2)]
        return [Group(section, "5", "Films", None)]

    def items(self, section, group_id):
        if section == "live":
            return self.live.get(group_id) if group_id != "*" else self.live["10"]
        return self.movies.get(group_id)

    def request_group(self, section, group_id):
        self.requests.append(("group", section, group_id))

    def seasons(self, series):
        return self.season_map.get(series["id"])

    def request_seasons(self, series):
        self.requests.append(("seasons", series["id"]))

    def episodes(self, series, season):
        return None

    def request_episodes(self, series, season):
        self.requests.append(("episodes", series["id"], season["id"]))


class NavModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.catalog = FakeCatalog()
        self.config = SimpleNamespace(
            favorites=["Two"],
            watch_history=[
                {
                    "provider_name": "Home",
                    "item_type": "channel",
                    "item_data": {"id": 1, "name": "One"},
                },
                {
                    "provider_name": "Other",
                    "item_type": "channel",
                    "item_data": {"id": 3, "name": "X"},
                },
                {
                    "provider_name": "Home",
                    "item_type": "movie",
                    "item_data": {"id": 4, "name": "M"},
                },
            ],
        )
        self.nav = NavModel(self.catalog, self.config)

    def test_root_is_sources(self):
        sources = self.nav.children(None)
        self.assertEqual(
            [(n.layer, n.title) for n in sources], [("source", "Home"), ("source", "Backup")]
        )

    def test_open_other_source_selects_provider(self):
        backup = self.nav.children(None)[1]
        self.nav.open(backup)
        self.assertEqual(self.catalog.selected, ["Backup"])
        self.assertEqual(self.nav.path, [backup])

    def test_sections_and_pinned_live_groups(self):
        home = self.nav.children(None)[0]
        self.nav.open(home)
        sections = self.nav.children(home)
        self.assertEqual([n.title for n in sections], ["Live TV", "Movies", "Series"])
        self.nav.open(sections[0])
        self.assertIn(("section", "live"), self.catalog.requests)
        groups = self.nav.children(sections[0])
        self.assertEqual([n.key for n in groups], [FAVORITES_GROUP, RECENT_GROUP, "10"])
        favorites = self.nav.children(groups[0])
        self.assertEqual([n.title for n in favorites], ["Two"])
        recent = self.nav.children(groups[1])
        self.assertEqual([n.title for n in recent], ["One"])

    def test_columns_show_two_neighbouring_layers(self):
        self.nav.reset_to("live", "10")
        (left_parent, left), (right_parent, right) = self.nav.columns()
        self.assertEqual(left_parent.key, "live")
        self.assertEqual(right_parent.key, "10")
        self.assertEqual([n.title for n in right], ["One", "Two"])
        self.assertTrue(self.nav.up())
        (left_parent, _), (right_parent, _) = self.nav.columns()
        self.assertEqual((left_parent.layer, right_parent.key), ("source", "live"))

    def test_on_demand_group_requests_and_reports_loading(self):
        self.nav.reset_to("movies")
        films = [n for n in self.nav.children(self.nav.path[-1]) if n.key == "5"][0]
        self.assertIsNone(self.nav.children(films))
        self.assertIn(("group", "movies", "5"), self.catalog.requests)
        changes = []
        self.nav.changed.connect(lambda: changes.append(1))
        self.catalog.movies["5"] = [{"id": 9, "name": "Film"}]
        self.catalog.groupReady.emit("movies", "5")
        self.assertEqual(changes, [1])
        self.assertEqual([n.title for n in self.nav.children(films)], ["Film"])

    def test_playable_and_series_layers(self):
        self.nav.reset_to("live", "10")
        channel = self.nav.children(self.nav.path[-1])[0]
        self.assertTrue(self.nav.is_playable(channel))
        series = NavNode("item", "s1", "Show", {"id": "s1", "name": "Show"})
        self.nav.reset_to("series", "5")
        self.assertFalse(self.nav.is_playable(series))
        self.assertIsNone(self.nav.children(series))
        self.assertIn(("seasons", "s1"), self.catalog.requests)

    def test_provider_change_resets_path(self):
        self.nav.reset_to("live", "10")
        self.catalog.provider = "Backup"
        self.catalog.providerReady.emit("Backup")
        self.assertEqual([n.layer for n in self.nav.path], ["source"])
        self.assertEqual(self.nav.path[0].key, "Backup")

    def test_up_at_root_returns_false(self):
        home = self.nav.children(None)[0]
        self.nav.open(home)
        self.nav.open(self.nav.children(home)[0])
        self.assertTrue(self.nav.up())
        self.assertFalse(self.nav.up())


if __name__ == "__main__":
    unittest.main()
