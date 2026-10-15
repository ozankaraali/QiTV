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
    sectionFailed = Signal(str, str)
    groupFailed = Signal(str, str, str)
    seasonsFailed = Signal(str, str)
    episodesFailed = Signal(str, str, str)

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
        self.ptype = "STB"
        self.items_calls = 0
        self.loaded = True

    def provider_type(self):
        return self.ptype

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
        if not self.loaded:
            return []
        if section == "live":
            return [Group("live", "10", "News", 2)]
        return [Group(section, "5", "Films", None)]

    def items(self, section, group_id):
        self.items_calls += 1
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

    def _series_nav(self):
        self.nav.reset_to("series", "5")
        return NavNode("item", "s1", "Show", {"id": "s1", "name": "Show"})

    def test_stb_episodes_have_distinct_keys_and_titles(self):
        from services.navigation import _item_node

        base = {"id": "s1", "name": "Show", "cmd": "c"}
        e1 = _item_node({**base, "number": "1", "ename": "Episode 1", "series": 1}, "episode")
        e2 = _item_node({**base, "number": "2", "ename": "Episode 2", "series": 2}, "episode")
        self.assertNotEqual(e1, e2)
        self.assertEqual([e1.title, e2.title], ["Episode 1", "Episode 2"])
        x1 = _item_node({"id": 11, "name": "Pilot"}, "episode")
        x2 = _item_node({"id": 12, "name": "Second"}, "episode")
        self.assertNotEqual(x1, x2)
        self.assertEqual([x1.title, x2.title], ["Pilot", "Second"])

    def test_loading_series_requests_once(self):
        series = self._series_nav()
        for _ in range(3):
            self.assertIsNone(self.nav.children(series))
        self.assertEqual(self.catalog.requests.count(("seasons", "s1")), 1)

    def test_failure_stops_requests_until_retry(self):
        series = self._series_nav()
        self.nav.children(series)
        changes = []
        self.nav.changed.connect(lambda: changes.append(1))
        self.catalog.seasonsFailed.emit("s1", "boom")
        self.assertEqual(changes, [1])
        self.assertEqual(self.nav.load_error(series), "boom")
        self.assertEqual(self.nav.children(series), [])
        self.assertEqual(self.catalog.requests.count(("seasons", "s1")), 1)
        self.nav.retry(series)
        self.assertIsNone(self.nav.load_error(series))
        self.assertIsNone(self.nav.children(series))
        self.assertEqual(self.catalog.requests.count(("seasons", "s1")), 2)

    def test_group_failure_and_provider_reset_clear_errors(self):
        self.nav.reset_to("movies")
        films = [n for n in self.nav.children(self.nav.path[-1]) if n.key == "5"][0]
        self.nav.children(films)
        self.catalog.groupFailed.emit("movies", "5", "bad")
        self.assertEqual(self.nav.load_error(films), "bad")
        self.catalog.providerReady.emit("Home")
        self.assertIsNone(self.nav.load_error(films))

    def test_non_stb_unloaded_group_requests_section(self):
        self.catalog.ptype = "XTREAM"
        self.nav.reset_to("movies")
        films = [n for n in self.nav.children(self.nav.path[-1]) if n.key == "5"][0]
        before = self.catalog.requests.count(("section", "movies"))
        self.assertIsNone(self.nav.children(films))
        self.assertEqual(self.catalog.requests.count(("section", "movies")), before + 1)
        self.assertNotIn(("group", "movies", "5"), self.catalog.requests)

    def test_reset_to_waits_for_unloaded_group(self):
        self.catalog.loaded = False
        self.nav.reset_to("live", "10")
        self.assertEqual([n.layer for n in self.nav.path], ["source", "section"])
        self.catalog.loaded = True
        changes = []
        self.nav.changed.connect(lambda: changes.append(1))
        self.catalog.sectionReady.emit("live")
        self.assertEqual([n.layer for n in self.nav.path], ["source", "section", "group"])
        self.assertEqual(self.nav.path[-1].title, "News")
        self.assertTrue(changes)

    def test_pinned_groups_are_memoized(self):
        home = self.nav.children(None)[0]
        self.nav.open(home)
        live = self.nav.children(home)[0]
        self.nav.open(live)
        self.nav.children(live)
        calls = self.catalog.items_calls
        self.nav.children(live)
        self.assertEqual(self.catalog.items_calls, calls)
        self.config.favorites = []
        groups = self.nav.children(live)
        self.assertNotIn(FAVORITES_GROUP, [n.key for n in groups])

    def test_section_failure_stops_loading_and_retry_requests_again(self):
        self.catalog.ptype = "XTREAM"
        self.nav.reset_to("movies")
        section = self.nav.path[-1]
        changes = []
        self.nav.changed.connect(lambda: changes.append(1))
        self.catalog.sectionFailed.emit("movies", "down")
        self.assertEqual(changes, [1])
        self.assertEqual(self.nav.load_error(section), "down")
        self.assertEqual(self.nav.children(section), [])
        count = self.catalog.requests.count(("section", "movies"))
        self.nav.children(section)
        self.assertEqual(self.catalog.requests.count(("section", "movies")), count)
        self.nav.retry(section)
        self.assertIsNone(self.nav.load_error(section))
        self.assertTrue(self.nav.children(section))

    def test_reset_to_with_failing_section_does_not_hang(self):
        self.catalog.loaded = False
        self.nav.reset_to("live", "10")
        self.catalog.sectionFailed.emit("live", "down")
        self.catalog.loaded = True
        self.catalog.sectionReady.emit("live")
        self.assertEqual([n.layer for n in self.nav.path], ["source", "section"])

    def test_non_stb_group_without_items_after_ready_does_not_loop(self):
        self.catalog.ptype = "XTREAM"
        self.nav.reset_to("movies")
        films = [n for n in self.nav.children(self.nav.path[-1]) if n.key == "5"][0]
        self.catalog.sectionReady.emit("movies")
        before = self.catalog.requests.count(("section", "movies"))
        self.assertEqual(self.nav.children(films), [])
        self.assertEqual(self.catalog.requests.count(("section", "movies")), before)

    def test_missing_pending_group_is_dropped(self):
        self.catalog.loaded = False
        self.nav.reset_to("live", "nope")
        self.catalog.loaded = True
        self.catalog.sectionReady.emit("live")
        self.assertEqual([n.layer for n in self.nav.path], ["source", "section"])
        self.catalog.sectionReady.emit("live")
        self.assertEqual(len(self.nav.path), 2)

    def test_ready_clears_stale_error(self):
        series = self._series_nav()
        self.nav.children(series)
        self.catalog.seasonsFailed.emit("s1", "boom")
        self.catalog.season_map["s1"] = [{"id": "a", "name": "S1"}]
        self.catalog.seasonsReady.emit("s1")
        self.assertIsNone(self.nav.load_error(series))

    def test_retry_section_requests_it_again(self):
        self.catalog.loaded = False
        self.nav.reset_to("live")
        section = self.nav.path[-1]
        self.catalog.sectionFailed.emit("live", "down")
        count = self.catalog.requests.count(("section", "live"))
        self.nav.retry(section)
        self.assertEqual(self.catalog.requests.count(("section", "live")), count + 1)
        self.assertNotIn("10", [n.key for n in self.nav.children(section)])
        self.assertEqual(self.catalog.requests.count(("section", "live")), count + 1)
        self.catalog.loaded = True
        self.catalog.sectionReady.emit("live")
        self.assertIn("10", [n.key for n in self.nav.children(section)])


if __name__ == "__main__":
    unittest.main()
