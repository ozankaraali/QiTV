"""The channel list over every provider: the "All providers" source, the providers' headings,
the provider tag's rows, and the mixed lists' channels (Favorites, Recent, All)."""

import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from job_fixtures import ImmediateRunner
from mixed_fixtures import DEMO, HARBOR, HeldSetups, live_content, mixed_config, shut_hub
from test_browsed_catalog import FakeEpg, ScriptedFactory
from test_catalog_service import FakeWorker
from timer_fixtures import elapse, wait_until

from services.browsed_catalog import BrowsedCatalog
from services.catalog import ALL_GROUP_ID
from services.catalogs import Catalogs
from services.m3u import parse_m3u
from services.navigation import FAVORITES_GROUP, RECENT_GROUP, NavModel, NavNode, item_key
from services.provider_ids import (
    ALL_PROVIDERS,
    ALL_PROVIDERS_TITLE,
    mixed_group_id,
    provider_of,
    with_provider,
)
from services.zapping import ZapContext
from tests.test_channel_rows import FakeLogos
from ui.find_session import DEBOUNCE_MS, FindSession
from ui.overlays.channel_rows import ChannelRowsModel
from ui.overlays.drawer import Drawer

EMPTY_LIST = {"type": "M3UPLAYLIST", "name": "Empty list", "url": "http://127.0.0.1:9/empty.m3u"}


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


def placed(drawer, row):
    """Whether the right column's layout reached the row."""
    view = drawer._columns["right"].view
    return view.visualRect(view.model().index(row)).isValid()


def in_sight(drawer, row):
    view = drawer._columns["right"].view
    rect = view.visualRect(view.model().index(row))
    return rect.isValid() and view.viewport().rect().contains(rect)


class MixedNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def build(self, providers, contents, runner=None):
        directory = tempfile.TemporaryDirectory(prefix="qitv-mixed-nav-")
        self.addCleanup(directory.cleanup)
        config, registry = mixed_config(directory.name, providers, contents)
        self.registry = registry
        self.addCleanup(registry._cache_writer.shutdown, wait=True)
        self.factory = ScriptedFactory()
        catalogs = Catalogs(
            config,
            registry,
            runner=runner or ImmediateRunner(),
            factory=self.factory,
            make_epg=lambda manager: FakeEpg(manager._pinned_provider),
        )
        self.addCleanup(shut_hub, catalogs)
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

    def test_a_page_jump_that_lands_on_a_heading_goes_on_past_it(self):
        self.open_all()
        drawer = self.drawer()
        drawer.open_drawer()
        drawer.move_by(4)  # row 4 is Harbor IPTV's heading
        self.assertEqual(drawer.highlighted().title, "TR: Haber")
        drawer.move_by(-1)  # row 4 again, going up
        self.assertEqual(drawer.highlighted().title, "Sports")

    def test_a_slow_provider_does_not_blank_the_mixed_list(self):
        self.nav.open(self.nav.children(None)[0])  # All providers
        self.view.is_loading = lambda: True  # one provider is still setting up
        self.assertFalse(self.nav.is_loading(self.nav.path[0]))
        drawer = self.drawer()
        drawer.open_drawer()
        self.assertIsNone(drawer.message_for("right"))
        self.assertIn("Live TV", [n.title for n in drawer._columns["right"].nodes])
        self.nav.reset_to("live", ALL_GROUP_ID)
        drawer.open_drawer()
        names = {provider_of(n.payload) for n in drawer._columns["right"].nodes}
        self.assertEqual(names, {"Demo TV", "Harbor IPTV"})

    def test_focus_finds_the_row_of_the_provider_that_plays(self):
        self.open_all()
        self.nav.reset_to("live", ALL_GROUP_ID)
        drawer = self.drawer()
        drawer.open_drawer(focus_key=("Harbor IPTV", "1"))  # Harbor's News, id 1 like Demo's
        shown = drawer.highlighted()
        self.assertEqual((provider_of(shown.payload), shown.key), ("Harbor IPTV", "1"))
        drawer.open_drawer(focus_key=("Demo TV", "1"))
        self.assertEqual(provider_of(drawer.highlighted().payload), "Demo TV")
        drawer.open_drawer(focus_key="1")  # an unstamped channel: by id, as before
        self.assertEqual(provider_of(drawer.highlighted().payload), "Demo TV")

    # ---- the find: genres and channels at both providers --------------------------------------

    def find(self, drawer, text):
        drawer.set_finder(
            FindSession(self.registry, ImmediateRunner(), loaded=self.view.loaded_contents)
        )
        drawer.open_drawer()
        drawer.type_text(text)
        elapse(DEBOUNCE_MS + 100)
        return drawer._columns["right"]

    def test_the_find_names_both_providers_and_a_genre_opens_under_its_heading_in_all(self):
        self.open_all()
        drawer = self.drawer()
        right = self.find(drawer, "news")
        genres = [(n.payload.provider, n.title) for n in right.nodes if n.layer == "found_genre"]
        self.assertEqual(genres, [("Demo TV", "News"), ("Harbor IPTV", "UK: News")])
        channels = [(provider_of(n.payload), n.title) for n in right.nodes if n.layer == "item"]
        self.assertEqual(channels, [("Demo TV", "News"), ("Harbor IPTV", "News")])  # same id
        harbor_news = mixed_group_id("Harbor IPTV", "42")
        drawer.move_by(1)  # Harbor IPTV's UK: News
        drawer.activate()
        path = [n.key for n in self.nav.path]
        self.assertEqual(path, [ALL_PROVIDERS, "live", harbor_news])  # where it is in All
        self.assertEqual(drawer._columns["left"].current.key, harbor_news)
        self.assertEqual(len(drawer._columns["right"].nodes), 28)
        drawer.go_up()  # ‹: every provider's genres, under their headings, on UK: News
        self.assertEqual(right.current.key, harbor_news)

    def test_a_genre_found_at_the_other_provider_opens_there(self):
        drawer = self.drawer()
        self.nav.reset_to("live")  # Demo TV's genres
        self.find(drawer, "haber")
        drawer.activate()  # TR: Haber, Harbor IPTV's
        self.assertEqual(self.view.current_provider_name(), "Harbor IPTV")
        self.assertEqual([n.key for n in self.nav.path], ["Harbor IPTV", "live", "41"])
        self.assertEqual(len(drawer._columns["right"].nodes), 41)

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
        keys = [(provider_of(i), item_key(i)) for i in self.nav.all_live_items()]
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

    def test_recent_with_one_provider_is_stamped_and_shows_no_provider_tag(self):
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
        # An entry older than 2.1 (no provider in it) names its provider too, as with two
        # (the drawer's focus key names the provider of what plays).
        self.assertEqual(
            nav.children(recent)[0].payload, with_provider({"id": 5, "name": "Demo 5"}, "Demo TV")
        )
        self.assertEqual(config.watch_history[0]["item_data"], {"id": 5, "name": "Demo 5"})
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

    # ---- one provider's setup while "All providers" shows -----------------------------------

    def test_another_providers_setup_keeps_the_path(self):
        self.open_all()
        self.nav.reset_to("live", ALL_GROUP_ID)
        before = [n.key for n in self.nav.path]
        self.assertEqual(before, [ALL_PROVIDERS, "live", ALL_GROUP_ID])
        count = len(self.nav.children(self.nav.path[-1]))
        # A retry, a setup the Library starts, an STB sign-in again: new content, same place.
        self.view.catalogs.sign_in_again("Harbor IPTV")
        QTest.qWait(10)
        self.assertEqual([n.key for n in self.nav.path], before)
        self.assertEqual(len(self.nav.children(self.nav.path[-1])), count)

    def test_another_providers_failure_keeps_the_path_and_says_so_at_its_source(self):
        self.open_all()
        self.nav.reset_to("live", mixed_group_id("Demo TV", "News"))
        before = [n.key for n in self.nav.path]
        self.factory.fail_setup("Harbor IPTV", "Portal down")
        self.view.catalogs.sign_in_again("Harbor IPTV")
        QTest.qWait(10)
        self.assertEqual([n.key for n in self.nav.path], before)
        harbor = NavNode("source", "Harbor IPTV", "Harbor IPTV", "Harbor IPTV")
        self.assertTrue(self.nav.provider_error(harbor))
        self.assertEqual(len(self.nav.children(self.nav.path[-1])), 12)  # Demo's News stays

    def test_a_provider_still_setting_up_does_not_blank_the_others_groups(self):
        runner = HeldSetups("Harbor IPTV")
        _config, _view, nav = self.build(
            [DEMO, HARBOR], {"Demo TV": demo_content(), "Harbor IPTV": harbor_content()}, runner
        )
        nav.open(nav.children(None)[0])
        nav.reset_to("live", ALL_GROUP_ID)
        QTest.qWait(10)  # Demo answers, Harbor refuses while it sets up
        live = nav.path[1]
        self.assertIn("News", [n.title for n in nav.children(live)])
        self.assertEqual([n.key for n in nav.path], [ALL_PROVIDERS, "live", ALL_GROUP_ID])
        runner.release("Harbor IPTV")
        QTest.qWait(10)
        self.assertEqual([n.key for n in nav.path], [ALL_PROVIDERS, "live", ALL_GROUP_ID])
        self.assertIn("TR: Haber", [n.title for n in nav.children(live)])
        self.assertEqual(len(nav.children(nav.path[-1])), 24 + 69)

    def test_a_provider_without_a_cache_shows_a_loading_row_then_its_error(self):
        runner = HeldSetups("Harbor IPTV")
        _config, _view, nav = self.build([DEMO, HARBOR], {"Demo TV": demo_content()}, runner)
        self.factory.fail_setup("Harbor IPTV", "Portal down")
        nav.open(nav.children(None)[0])
        nav.reset_to("live")
        QTest.qWait(10)
        rows = self.rows_under(nav, "Harbor IPTV")
        self.assertEqual([(n.layer, n.title) for n in rows], [("notice", "Loading…")])
        runner.release("Harbor IPTV")
        QTest.qWait(10)
        rows = self.rows_under(nav, "Harbor IPTV")
        self.assertEqual([n.layer for n in rows], ["notice"])
        self.assertNotEqual(rows[0].title, "Loading…")  # its error
        self.assertFalse(nav.is_playable(rows[0]))
        path = nav.path
        nav.open(rows[0])  # never opened
        self.assertEqual(nav.path, path)

    def test_a_provider_without_a_cache_whose_list_fails_shows_why(self):
        # An Xtream panel signs in with its first list: its setup itself never fails.
        _config, _view, nav = self.build([DEMO, HARBOR], {"Demo TV": demo_content()})
        self.factory.xtream = lambda *args: FakeWorker(fail="Wrong password")
        nav.open(nav.children(None)[0])
        nav.reset_to("live")
        QTest.qWait(10)
        rows = self.rows_under(nav, "Harbor IPTV")
        self.assertEqual([n.layer for n in rows], ["notice"])
        self.assertNotEqual(rows[0].title, "Loading…")
        self.assertIn("Demo TV", [n.title for n in nav.children(nav.path[1])])

    def test_only_a_failed_refresh_of_a_saved_list_says_the_saved_channels_show(self):
        # The toast came for a provider without a cache whose list failed too (nothing
        # saved of it shows; its heading says why).
        _config, view, nav = self.build([DEMO, HARBOR], {"Demo TV": demo_content()})
        notices = []
        nav.notice.connect(notices.append)
        self.factory.xtream = lambda *args: FakeWorker(fail="Wrong password")
        nav.open(nav.children(None)[0])
        nav.reset_to("live")
        QTest.qWait(10)
        self.assertEqual([n.layer for n in self.rows_under(nav, "Harbor IPTV")], ["notice"])
        self.assertEqual(notices, [])
        self.factory.m3u = lambda *args: FakeWorker(fail="Portal down")
        view.catalogs.catalog("Demo TV").request_section("live", force=True)  # Demo's refresh
        QTest.qWait(10)
        self.assertEqual(notices, ["Couldn't refresh Live TV; showing saved channels"])
        self.assertIn("News", [n.title for n in nav.children(nav.path[1])])

    def test_moving_through_all_asks_nothing_of_providers_whose_setup_failed(self):
        # A failed provider was not signed in again, but its lists were still asked for
        # (an STB portal: a request with the failed session at every move).
        _config, view, nav = self.build([DEMO, HARBOR], {})
        self.factory.fail_setup("Demo TV", "Portal down")
        self.factory.fail_setup("Harbor IPTV", "Portal down")
        nav.open(nav.children(None)[0])
        nav.reset_to("live")
        QTest.qWait(10)
        self.assertEqual([c[0] for c in self.factory.calls if c[0] in ("m3u", "xtream")], [])
        self.assertFalse(nav.is_loading(nav.path[1]))  # nothing comes: the headings say why
        rows = [(n.layer, n.title) for n in self.rows_under(nav, "Demo TV")]
        self.assertEqual([layer for layer, _title in rows], ["notice", "header", "notice"])
        self.assertNotIn("Loading…", [title for _layer, title in rows])
        nav.reset_to("live")
        QTest.qWait(10)
        self.assertEqual(self.factory.setups.count("Harbor IPTV"), 1)
        view.select_provider(ALL_PROVIDERS)  # chosen again: every provider is tried
        self.assertEqual(self.factory.setups.count("Harbor IPTV"), 2)

    def test_a_playlist_without_channels_set_up_after_another_failed_shows_no_loading_row(self):
        # Polish: Harbor's failure (first) forgot that the playlist had refused the live
        # list while it set up; it was never asked again and said "Loading…" for good.
        runner = HeldSetups("Harbor IPTV", "Empty list")
        _config, _view, nav = self.build([HARBOR, EMPTY_LIST], {}, runner)
        self.factory.fail_setup("Harbor IPTV", "Portal down")
        self.factory.m3u = lambda *args: FakeWorker(parse_m3u("#EXTM3U\n", categorize=True))
        nav.open(nav.children(None)[0])
        nav.reset_to("live")
        QTest.qWait(10)
        runner.release("Harbor IPTV")
        QTest.qWait(10)
        self.assertEqual([n.title for n in self.rows_under(nav, "Empty list")], ["Loading…"])
        runner.release("Empty list")  # set up: its playlist has no channels
        QTest.qWait(10)
        self.assertEqual(self.rows_under(nav, "Empty list"), [])
        self.assertEqual([n.layer for n in self.rows_under(nav, "Harbor IPTV")][:1], ["notice"])

    def test_a_provider_set_up_while_anothers_list_downloads_is_asked_for_its_own(self):
        # Polish: the second setup ended while the first provider's list was on its way;
        # nothing asked it for its list, and its heading said "Loading…" for good.
        runner = HeldSetups("Demo TV", "Empty list")
        _config, _view, nav = self.build([DEMO, EMPTY_LIST], {}, runner)

        def playlist(url, *args):
            if url == DEMO["url"]:
                runner.release("Empty list")  # its setup ends meanwhile
                return FakeWorker(demo_content())
            return FakeWorker(parse_m3u("#EXTM3U\n", categorize=True))

        self.factory.m3u = playlist
        nav.open(nav.children(None)[0])
        nav.reset_to("live")
        QTest.qWait(10)
        runner.release("Demo TV")
        QTest.qWait(10)
        self.assertEqual(self.rows_under(nav, "Empty list"), [])
        self.assertIn("News", [n.title for n in nav.children(nav.path[1])])

    def test_sections_refused_in_all_mode_do_not_outlive_it(self):
        # Polish 2: leaving All while a provider still set up kept the sections it refused;
        # back in All later, the next provider to finish was asked for an old section (an
        # unrequested movies download).
        runner = HeldSetups("Harbor IPTV")
        _config, _view, nav = self.build([DEMO, HARBOR], {"Demo TV": demo_content()}, runner)
        nav.open(nav.children(None)[0])
        nav.reset_to("live")
        QTest.qWait(10)
        self.assertEqual(nav._refused_sections, {"live"})  # Harbor refused: it sets up
        nav.open(nav.children(None)[1])  # leaves All for one provider
        QTest.qWait(10)
        self.assertEqual(nav._refused_sections, set())

    @staticmethod
    def rows_under(nav, provider):
        """The rows after `provider`'s heading in the open section."""
        rows = nav.children(nav.path[1])
        at = next(i for i, n in enumerate(rows) if n.layer == "header" and n.title == provider)
        return rows[at + 1 :]

    def test_a_provider_group_in_all_mode_lists_that_providers_channels(self):
        groups = self.live_groups()
        harbor_group = next(n for n in groups if n.key == mixed_group_id("Harbor IPTV", "41"))
        self.assertEqual(len(self.nav.children(harbor_group)), 41)

    def test_a_channel_of_a_providers_own_group_is_revealed_in_all(self):
        # Tuned from Harbor's own list (group "41"), then the page went to All providers,
        # which lists that group under a mixed id: the list missed it.
        self.open_all()
        channel = self.view.catalogs.catalog("Harbor IPTV").items("live", "41")[3]
        self.nav.reveal(ALL_PROVIDERS, "live", "41", channel)
        group = mixed_group_id("Harbor IPTV", "41")
        self.assertEqual([n.key for n in self.nav.path], [ALL_PROVIDERS, "live", group])
        self.assertIs(self.nav.revealed(), channel)

    def test_a_channel_outside_the_group_it_was_tuned_in_is_revealed_in_all(self):
        # A number typed outside the group tunes a channel of another group (TV numbers
        # are global); the list opened on that group, without the channel.
        _config, view, nav = self.build([DEMO], {"Demo TV": demo_content()})
        nav.open(nav.children(None)[0])
        sports = view.items("live", "Sports")[0]
        nav.reveal("Demo TV", "live", "News", sports)
        self.assertEqual([n.key for n in nav.path], ["Demo TV", "live", ALL_GROUP_ID])
        news = view.items("live", "News")[2]
        nav.reveal("Demo TV", "live", "News", news)  # in its group: opens there
        self.assertEqual([n.key for n in nav.path], ["Demo TV", "live", "News"])

    def test_a_provider_whose_only_group_is_all_lists_it_under_its_heading(self):
        # A playlist without group titles had an empty heading; its channels were only in
        # the mixed All, among the others'.
        plain = live_content(["Plain 1", "Plain 2"], {})
        _config, _view, nav = self.build(
            [DEMO, HARBOR], {"Demo TV": plain, "Harbor IPTV": harbor_content()}
        )
        nav.open(nav.children(None)[0])
        nav.reset_to("live")
        rows = self.rows_under(nav, "Demo TV")
        self.assertEqual([(n.layer, n.title, n.count) for n in rows[:1]], [("group", "All", 2)])
        self.assertEqual([n.title for n in nav.children(rows[0])], ["Plain 1", "Plain 2"])
        self.assertEqual((rows[1].layer, rows[1].title), ("header", "Harbor IPTV"))
        mixed_all = nav.children(nav.path[1])[0]
        self.assertEqual((mixed_all.key, mixed_all.count), (ALL_GROUP_ID, 2 + 69))

    # ---- a channel far down the mixed All -----------------------------------------------------

    def test_the_list_rests_on_a_channel_far_down_all_through_refreshes(self):
        # Reported on 2.3.0: channel 9594 played and the list showed, and kept going back to,
        # the channels around 995. Here All mixes 11,000 and 6,000 channels on two-line rows.
        def numbered(count, prefix):
            return live_content([f"{prefix} {n}" for n in range(1, count + 1)], {})

        _config, view, nav = self.build(
            [DEMO, HARBOR],
            {"Demo TV": numbered(11_000, "Demo"), "Harbor IPTV": numbered(6_000, "Harbor")},
        )
        nav.open(nav.children(None)[0])
        nav.reset_to("live", ALL_GROUP_ID)
        host = QWidget()
        host.resize(1280, 720)
        host.show()
        self.addCleanup(host.deleteLater)
        self.addCleanup(host.close)
        logos = FakeLogos()
        drawer = Drawer(nav, logos, lambda item: None, host)
        model = drawer._columns["right"].model
        bar = drawer._columns["right"].view.verticalScrollBar()
        for provider, number, row in (("Demo TV", 9594, 9593), ("Harbor IPTV", 5000, 15_999)):
            drawer.open_drawer(focus_key=(provider, str(number)))
            self.assertTrue(wait_until(lambda: in_sight(drawer, row) and placed(drawer, 16_999)))
            node = drawer.highlighted()
            self.assertEqual((provider_of(node.payload), node.key), (provider, str(number)))
            self.assertEqual(model.index(row).data(ChannelRowsModel.ProviderRole), provider)
            moves = []
            bar.valueChanged.connect(moves.append)
            nav.changed.emit()  # a guide loaded
            view.groupReady.emit("live", ALL_GROUP_ID)  # a catalog event
            logos.logoReady.emit("http://127.0.0.1:9/logo.png")
            elapse(50)
            self.assertEqual(moves, [])
            self.assertTrue(in_sight(drawer, row))
            bar.valueChanged.disconnect(moves.append)


if __name__ == "__main__":
    unittest.main()
