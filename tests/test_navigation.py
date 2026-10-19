import os
from types import SimpleNamespace
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from services.catalog import PROVIDER_LOADING_MESSAGE, Group
from services.navigation import (
    ADD_PROVIDER_ROW,
    FAVORITES_GROUP,
    FREE_CHANNELS_ROW,
    OPEN_FILE_ROW,
    RECENT_GROUP,
    SETTINGS_ROW,
    NavModel,
    NavNode,
    recent_channel,
)
from services.provider_ids import ALL_PROVIDERS, ALL_PROVIDERS_TITLE, with_provider


class FakeCatalog(QObject):
    providerReady = Signal(str)
    providerFailed = Signal(str, str)
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
        self.loading = False  # a provider setup is under way

    def provider_type(self):
        return self.ptype

    def is_loading(self):
        return self.loading

    def content_version(self):
        # Changes whenever the fake's content does (tests mutate it directly).
        return hash(repr((self.live, self.movies, self.season_map, self.loaded, self.ptype)))

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

    def test_reveal_opens_the_list_at_the_item_s_source_section_and_group(self):
        self.catalog.select_provider = lambda name=None, **kw: setattr(
            self.catalog, "provider", name
        )
        item = {"id": 2, "name": "Two"}
        self.nav.reveal("Backup", "live", "10", item)
        self.assertEqual(self.catalog.provider, "Backup")
        self.assertEqual([n.key for n in self.nav.path][:2], ["Backup", "live"])
        self.assertIs(self.nav.revealed(), item)
        self.nav.reset_to("live")
        self.assertIsNone(self.nav.revealed())

    def test_root_is_sources_then_the_settings_rows(self):
        rows = self.nav.children(None)
        self.assertEqual(
            [(n.layer, n.key, n.title) for n in rows],
            [
                ("source", ALL_PROVIDERS, ALL_PROVIDERS_TITLE),
                ("source", "Home", "Home"),
                ("source", "Backup", "Backup"),
                ("action", SETTINGS_ROW, "Settings"),  # icons are painted, not glyphs
                ("action", ADD_PROVIDER_ROW, "Add provider"),
                ("action", FREE_CHANNELS_ROW, "Free channels…"),
                ("action", OPEN_FILE_ROW, "Open file"),
            ],
        )
        self.assertFalse(any(self.nav.is_playable(n) or self.nav.needs_setup(n) for n in rows[3:]))

    def test_open_other_source_selects_provider(self):
        backup = self.nav.children(None)[2]
        self.nav.open(backup)
        self.assertEqual(self.catalog.selected, ["Backup"])
        self.assertEqual(self.nav.path, [backup])

    def test_sections_and_pinned_live_groups(self):
        home = self.nav.children(None)[1]
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

    def test_a_watched_entry_names_its_channel_for_recent(self):
        entries = self.config.watch_history
        self.assertEqual(recent_channel(entries[0]), ("Home", "1"))
        self.assertEqual(recent_channel(entries[1]), ("Other", "3"))  # listed while it exists
        self.assertIsNone(recent_channel(entries[2]))  # a film
        self.assertIsNone(recent_channel({"item_type": "channel", "item_data": {"id": 1}}))

    def test_a_recent_row_is_one_of_recent_s_channels_while_the_list_shows_them(self):
        self.nav.reset_to("live", RECENT_GROUP)
        self.assertEqual(self.nav.recent_count(), 1)
        (row,) = self.nav.children(self.nav.path[-1])
        self.assertTrue(self.nav.is_recent_row(row))
        self.nav.reset_to("live", "10")
        one = self.nav.children(self.nav.path[-1])[0]
        self.assertFalse(self.nav.is_recent_row(one))  # the same channel, in its genre
        self.assertFalse(self.nav.is_recent_row(None))
        self.nav.reset_to("live", RECENT_GROUP)
        self.assertFalse(self.nav.is_recent_row(self.nav.path[-1]))  # the group, not a row
        other = NavNode("item", "1", "One", with_provider({"id": 1, "name": "One"}, "Backup"))
        self.assertFalse(self.nav.is_recent_row(other))  # watched at Home, not at Backup
        self.config.watch_history = []  # taken out: the row is no longer one of them
        self.assertFalse(self.nav.is_recent_row(row))
        self.assertEqual(self.nav.recent_count(), 0)

    def test_a_favorite_without_a_name_is_found_by_its_title(self):
        self.catalog.live["10"].append({"id": 3, "title": "Three"})
        self.config.favorites = ["Three"]
        home = self.nav.children(None)[1]
        self.nav.open(home)
        live = self.nav.children(home)[0]
        favorites = self.nav.children(self.nav.children(live)[0])
        self.assertEqual([n.payload.get("id") for n in favorites], [3])

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
        home = self.nav.children(None)[1]
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

    def test_public_key_helpers(self):
        from services import navigation
        from services.navigation import _item_node, episode_key, item_key

        self.assertEqual(item_key({"id": 7, "cmd": "c", "name": "N"}), "7")
        self.assertEqual(item_key({"cmd": "c", "name": "N"}), "c")
        self.assertEqual(item_key({"name": "N"}), "N")
        self.assertIs(navigation._item_key, item_key)
        episode = {"id": "s1", "season_id": "s1:2", "season": "2", "series": 3}
        self.assertEqual(episode_key(episode), "s1:s1:2:3")
        self.assertEqual(_item_node(episode, "episode").key, episode_key(episode))
        self.assertEqual(episode_key({"id": 11, "episode_num": "4"}), "11::4")

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
        home = self.nav.children(None)[1]
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

    def test_group_children_are_memoized_until_the_catalog_changes(self):
        # columns() rebuilt every node on each call (~70 ms for 50k channels).
        self.nav.reset_to("live", "10")
        group = self.nav.path[-1]
        first = self.nav.children(group)
        calls = self.catalog.items_calls
        self.assertIs(self.nav.children(group)[0], first[0])
        self.assertEqual(self.catalog.items_calls, calls)
        self.catalog.live["10"] = [{"id": 3, "name": "Three"}]
        self.assertEqual([n.title for n in self.nav.children(group)], ["Three"])

    def test_a_zap_does_not_rescan_favorites_or_rebuild_groups(self):
        home = self.nav.children(None)[1]
        self.nav.open(home)
        live = self.nav.children(home)[0]
        self.nav.open(live)
        self.nav.children(live)
        group = next(n for n in self.nav.children(live) if n.key == "10")
        nodes = self.nav.children(group)
        calls = self.catalog.items_calls
        # A zap records history: only the recent list changes.
        self.config.watch_history = [
            {"provider_name": "Home", "item_type": "channel", "item_data": {"id": 2, "name": "Two"}}
        ] + self.config.watch_history
        pinned = self.nav.children(live)
        self.assertEqual(self.catalog.items_calls, calls)
        recent = next(n for n in pinned if n.key == RECENT_GROUP)
        self.assertEqual([n.title for n in self.nav.children(recent)], ["Two", "One"])
        self.assertIs(self.nav.children(group)[0], nodes[0])
        self.assertEqual(self.catalog.items_calls, calls)

    def test_a_section_ready_again_keeps_the_memoized_nodes(self):
        # Opening the drawer asks for its section again: a fresh one answers at once, and its
        # channels were rebuilt for nothing.
        self.nav.reset_to("live", "10")
        self.catalog.sectionReady.emit("live")
        group = self.nav.path[-1]
        nodes = self.nav.children(group)
        groups = self.nav.children(self.nav.path[1])
        calls = self.catalog.items_calls
        changes = []
        self.nav.changed.connect(lambda: changes.append(True))
        self.nav.reset_to("live", "10")
        self.catalog.sectionReady.emit("live")
        self.assertTrue(changes)  # the section is no longer loading
        self.assertIs(self.nav.children(group)[0], nodes[0])
        self.assertIs(self.nav.children(self.nav.path[1])[-1], groups[-1])
        self.assertEqual(self.catalog.items_calls, calls)

    def test_without_a_content_version_catalog_events_invalidate(self):
        class VersionlessCatalog(FakeCatalog):
            content_version = None

        catalog = VersionlessCatalog()
        nav = NavModel(catalog, self.config)
        nav.reset_to("live", "10")
        group = nav.path[-1]
        self.assertEqual([n.title for n in nav.children(group)], ["One", "Two"])
        catalog.live["10"] = [{"id": 3, "name": "Three"}]
        self.assertEqual([n.title for n in nav.children(group)], ["One", "Two"])  # memoized
        catalog.groupReady.emit("live", "10")
        self.assertEqual([n.title for n in nav.children(group)], ["Three"])

    def test_section_failure_stops_loading_and_retry_requests_again(self):
        # Nothing saved for the section: the failure is an error row (a saved catalog is
        # kept instead; see test_failed_refresh_keeps_the_saved_groups).
        self.catalog.ptype = "XTREAM"
        self.catalog.loaded = False
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
        self.catalog.loaded = True
        self.assertEqual(self.nav.children(section), [])  # the error row, not the groups
        self.nav.retry(section)
        self.assertIsNone(self.nav.load_error(section))
        self.assertTrue(self.nav.children(section))

    def test_failed_refresh_keeps_the_saved_groups(self):
        # A stale cache whose refresh fails is still shown, with a notice.
        notices, changes = [], []
        self.nav.notice.connect(notices.append)
        self.nav.reset_to("live", "10")
        self.assertEqual(len(self.nav.path), 3)  # the saved group resolved at once
        self.nav.reset_to("live")
        self.nav.changed.connect(lambda: changes.append(1))
        section = self.nav.path[-1]
        self.catalog.sectionFailed.emit("live", "Couldn't reach the provider")
        self.assertEqual(notices, ["Couldn't refresh Live TV; showing saved channels"])
        self.assertTrue(changes)
        self.assertIsNone(self.nav.load_error(section))
        self.assertIn("10", [n.key for n in self.nav.children(section)])
        self.assertEqual(self.nav.children(self.nav.children(section)[-1])[0].title, "One")

    def test_still_loading_is_not_a_failed_refresh(self):
        # While the next provider sets up, the catalog still holds the previous provider's
        # groups. "Still loading" is not a failed refresh: no notice, and the old groups
        # are not shown as a ready section of the new provider.
        notices = []
        self.nav.notice.connect(notices.append)
        self.nav.reset_to("live")
        section = self.nav.path[-1]
        self.catalog.loading = True
        self.catalog.sectionFailed.emit("live", PROVIDER_LOADING_MESSAGE)
        self.assertEqual(notices, [])
        # Still loading, not a failure: no retry row for it.
        self.assertIsNone(self.nav.load_error(section))
        self.assertTrue(self.nav.is_loading(section))
        self.assertEqual(self.nav.children(section), [])
        # providerReady clears it, and the shell's reset_to asks for the section again.
        self.catalog.loading = False
        self.catalog.provider = "Backup"
        self.catalog.providerReady.emit("Backup")
        before = self.catalog.requests.count(("section", "live"))
        self.nav.reset_to("live")
        self.assertEqual(self.catalog.requests.count(("section", "live")), before + 1)
        section = self.nav.path[-1]
        self.assertIsNone(self.nav.load_error(section))
        self.catalog.sectionReady.emit("live")
        self.assertIn("10", [n.key for n in self.nav.children(section)])
        self.assertEqual(notices, [])

    def test_a_failure_while_the_provider_loads_is_not_a_failed_refresh(self):
        notices = []
        self.nav.notice.connect(notices.append)
        self.nav.reset_to("live")
        section = self.nav.path[-1]
        self.catalog.loading = True
        self.catalog.sectionFailed.emit("live", "Couldn't reach the provider")
        self.assertEqual(notices, [])
        self.assertEqual(self.nav.children(section), [])

    def test_a_requested_section_is_loading_until_it_settles(self):
        self.nav.reset_to("live")
        section = self.nav.path[-1]
        self.assertTrue(self.nav.is_loading(section))
        self.catalog.sectionReady.emit("live")
        self.assertFalse(self.nav.is_loading(section))
        movies = NavNode("section", "movies", "Movies", "movies")
        self.nav.open(movies)
        self.assertTrue(self.nav.is_loading(movies))
        self.catalog.loaded = False  # nothing saved to fall back on
        self.catalog.sectionFailed.emit("movies", "Timed out")
        self.assertFalse(self.nav.is_loading(movies))
        self.assertEqual(self.nav.load_error(movies), "Timed out")

    def test_a_provider_failure_ends_a_section_left_loading_by_its_setup(self):
        self.nav.reset_to("live")
        section = self.nav.path[-1]
        self.catalog.loading = True
        self.catalog.sectionFailed.emit("live", PROVIDER_LOADING_MESSAGE)
        self.assertTrue(self.nav.is_loading(section))
        self.catalog.loading = False
        self.catalog.providerFailed.emit("Home", "Couldn't reach the provider")
        self.assertFalse(self.nav.is_loading(section))  # not "Loading…" for good
        self.assertIn("10", [n.key for n in self.nav.children(section)])  # the saved groups

    def test_everything_is_loading_while_a_provider_sets_up(self):
        self.nav.reset_to("live")
        self.catalog.sectionReady.emit("live")
        self.catalog.loading = True
        self.assertTrue(self.nav.is_loading(self.nav.path[-1]))
        self.assertTrue(self.nav.is_loading(self.nav.path[0]))

    def test_a_failed_provider_is_set_up_again_when_its_source_is_opened(self):
        requested = []
        self.nav.providerRequested.connect(requested.append)
        home = self.nav.children(None)[1]
        self.catalog.providerFailed.emit("Home", "Couldn't reach the provider")
        self.assertEqual(self.nav.provider_error(home), "Couldn't reach the provider")
        self.nav.open(home)  # the current provider, but not ready: set it up again
        self.assertEqual(self.catalog.selected, ["Home"])
        self.assertEqual(requested, ["Home"])
        self.assertIsNone(self.nav.provider_error(home))
        self.assertEqual(self.nav.path, [home])

    def test_a_provider_error_of_another_provider_survives_a_ready(self):
        backup = self.nav.children(None)[2]
        self.catalog.providerFailed.emit("Backup", "down")
        self.catalog.providerReady.emit("Home")
        self.assertEqual(self.nav.provider_error(backup), "down")
        self.assertTrue(self.nav.needs_setup(backup))

    def test_a_ready_provider_needs_no_setup(self):
        self.assertFalse(self.nav.needs_setup(self.nav.children(None)[1]))

    def test_the_ready_current_provider_is_not_set_up_again(self):
        self.catalog.providerReady.emit("Home")
        self.nav.open(self.nav.children(None)[1])
        self.assertEqual(self.catalog.selected, [])

    def test_a_running_setup_is_not_started_again(self):
        home = self.nav.children(None)[1]
        self.catalog.providerFailed.emit("Home", "down")
        self.catalog.loading = True  # e.g. Settings started a refresh meanwhile
        self.nav.open(home)
        self.assertEqual(self.catalog.selected, [])

    def test_provider_ready_clears_a_provider_error(self):
        home = self.nav.children(None)[1]
        self.catalog.providerFailed.emit("Home", "down")
        self.catalog.providerReady.emit("Home")
        self.assertIsNone(self.nav.provider_error(home))

    def test_focus_source_shows_the_sources_with_it_at_the_end_of_the_path(self):
        self.nav.reset_to("live")
        self.nav.focus_source("Backup")
        self.assertEqual([(n.layer, n.key) for n in self.nav.path], [("source", "Backup")])

    def test_failed_refresh_resolves_a_pending_group_from_the_saved_catalog(self):
        self.catalog.loaded = False
        self.nav.reset_to("live", "10")  # nothing to show yet: the group waits
        self.catalog.loaded = True  # e.g. the provider setup loaded the saved catalog
        self.catalog.sectionFailed.emit("live", "down")
        self.assertEqual([n.layer for n in self.nav.path], ["source", "section", "group"])
        self.assertEqual(self.nav.path[-1].key, "10")

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
