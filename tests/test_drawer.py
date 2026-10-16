import os
import sys
from types import SimpleNamespace
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, Qt, qInstallMessageHandler
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from timer_fixtures import elapse

from services.catalog import PROVIDER_LOADING_MESSAGE, Group
from services.navigation import ADD_PROVIDER_ROW, OPEN_FILE_ROW, SETTINGS_ROW, NavModel
from tests.test_navigation import FakeCatalog
from ui.keymap import key_hint
from ui.overlay_host import OverlayHost
from ui.overlays.channel_rows import NumberRole
from ui.overlays.drawer import (
    EMPTY_TEXT,
    LOADING_TEXT,
    PAD_SIDE_U,
    PREVIEW_DEBOUNCE_MS,
    PROVIDER_ERROR_TEXT,
    REFRESH_ACTION,
    SEARCH_ACTION,
    Drawer,
    IconRole,
)

PREVIEW_WAIT_MS = PREVIEW_DEBOUNCE_MS + 120
_OFFSCREEN_NOISE = (
    "This plugin does not support raise()",
    "This plugin does not support setting window masks",
)


def make(live=None):
    catalog = FakeCatalog()
    if live is not None:
        catalog.live = live
    config = SimpleNamespace(favorites=["Two"], watch_history=[])
    nav = NavModel(catalog, config)
    host = QWidget()
    host.resize(1280, 720)
    host.show()
    drawer = Drawer(nav, None, lambda item: None, host)
    return catalog, nav, host, drawer


class DrawerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.catalog, self.nav, self.host, self.drawer = make()
        self.addCleanup(self.host.deleteLater)
        self.addCleanup(self.host.close)
        self.nav.reset_to("live", "10")
        self.played = []
        self.closed = []
        self.drawer.playRequested.connect(lambda *a: self.played.append(a))
        self.drawer.closed.connect(lambda: self.closed.append(True))

    def test_open_focuses_right_and_highlights_key(self):
        self.drawer.open_drawer("1")
        self.assertEqual(self.drawer.focus_side, "right")
        self.assertEqual(self.drawer.highlighted().title, "One")

    def test_move_and_activate_plays_with_siblings_and_group(self):
        self.drawer.open_drawer("1")
        self.drawer.move_by(1)
        self.drawer.activate()
        node, siblings, parent = self.played[0]
        self.assertEqual(node.title, "Two")
        self.assertEqual([n.title for n in siblings], ["One", "Two"])
        self.assertEqual((parent.layer, parent.key), ("group", "10"))

    def test_left_previews_other_group(self):
        self.drawer.open_drawer()
        self.drawer.left()
        self.assertEqual(self.drawer.focus_side, "left")
        before = self.nav.path[-1]
        self.drawer.move_by(-1)
        elapse(320)
        self.assertNotEqual(self.nav.path[-1], before)

    def test_left_twice_goes_up(self):
        self.drawer.open_drawer()
        depth = len(self.nav.path)
        self.drawer.left()
        self.drawer.left()
        self.assertEqual(len(self.nav.path), depth - 1)
        self.assertEqual(self.drawer.focus_side, "left")

    def test_right_opens_section(self):
        self.nav.open(self.nav.path[0])
        self.drawer.open_drawer()
        self.assertEqual(self.drawer.highlighted().title, "Live TV")
        self.drawer.right()
        self.assertEqual(self.nav.path[-1].layer, "section")
        self.assertEqual(self.drawer.focus_side, "right")

    def test_filter_and_escape(self):
        self.drawer.open_drawer()
        self.drawer.type_text("tw")
        self.assertEqual(self.drawer.highlighted().title, "Two")
        self.assertTrue(self.drawer.escape())
        self.assertEqual(self.drawer.filter_text, "")
        self.assertTrue(self.drawer.isVisible())
        self.assertTrue(self.drawer.escape())
        self.assertFalse(self.drawer.isVisible())
        self.assertEqual(self.closed, [True])

    def test_a_refresh_with_the_same_rows_keeps_the_lists(self):
        self.drawer.open_drawer("1")
        resets = []
        for side in ("left", "right"):
            self.drawer._columns[side].model.modelReset.connect(lambda s=side: resets.append(s))
        self.nav.changed.emit()  # e.g. the section reported ready again
        self.assertEqual(resets, [])
        self.assertEqual(self.drawer.highlighted().title, "One")
        self.catalog.live["10"] = [{"id": 1, "name": "One"}, {"id": 2, "name": "Twain"}]
        self.nav.changed.emit()  # new content: the groups (Favorites went) and the channels
        self.assertEqual(sorted(resets), ["left", "right"])
        self.drawer.move_by(1)
        self.assertEqual(self.drawer.highlighted().title, "Twain")

    def test_a_refresh_with_the_same_rows_looks_now_up_again(self):
        guide = {}
        catalog, nav, host, drawer = make()
        drawer._columns["right"].model._now_provider = lambda item: guide.get(item.get("name"))
        self.addCleanup(host.deleteLater)
        self.addCleanup(host.close)
        nav.reset_to("live", "10")
        drawer.open_drawer("1")
        model = drawer._columns["right"].model
        self.assertEqual(model.data(model.index(0), model.NowRole), "")
        guide["One"] = ("Evening News", 0.5)  # the EPG became ready
        nav.changed.emit()
        self.assertEqual(model.data(model.index(0), model.NowRole), "Evening News")

    def test_opening_again_lays_the_lists_out_only_after_a_resize(self):
        # A forced layout of a 2,500-channel list cost tens of ms at every opening.
        from unittest import mock

        from ui.overlays.channel_rows import ChannelListView

        self.drawer.open_drawer("1")
        self.drawer.close_drawer()
        with mock.patch.object(ChannelListView, "doItemsLayout", autospec=True) as layout:
            self.drawer.open_drawer("1")
            self.assertEqual(layout.call_count, 0)
            self.host.resize(960, 540)
            self.assertEqual(layout.call_count, 2)  # both columns, once
        self.assertEqual(self.drawer._columns["right"].view.units_width, 960)
        self.assertEqual(self.drawer.highlighted().title, "One")

    def test_backspace(self):
        self.drawer.open_drawer()
        self.drawer.type_text("tw")
        self.drawer.backspace()
        self.assertEqual(self.drawer.filter_text, "t")

    def test_filter_casefold(self):
        self.catalog.live["10"] = [{"id": 7, "name": "şov"}]
        self.nav.changed.emit()
        self.drawer.open_drawer()
        self.drawer.type_text("Ş")
        self.assertEqual(self.drawer.highlighted().title, "şov")

    def test_loading_layer_renders(self):
        self.catalog.live = {}
        self.nav.changed.emit()
        self.drawer.open_drawer()
        self.assertFalse(self.drawer.grab().isNull())

    def test_error_row_retries(self):
        self.catalog.live = {}
        self.nav.changed.emit()
        self.drawer.open_drawer()
        self.catalog.groupFailed.emit("live", "10", "boom")
        self.assertTrue(self.nav.load_error(self.nav.path[-1]))
        self.assertEqual(self.drawer.message_for("right"), "Couldn't load — press Enter to retry")
        self.assertFalse(self.drawer.grab().isNull())
        before = self.catalog.requests.count(("group", "live", "10"))
        self.drawer.activate()
        self.assertEqual(self.catalog.requests.count(("group", "live", "10")), before + 1)
        self.assertIsNone(self.nav.load_error(self.nav.path[-1]))

    def test_a_pending_section_shows_loading_until_it_settles(self):
        self.catalog.loaded = False  # no groups yet
        self.nav.reset_to("live")
        self.drawer.open_drawer()
        self.assertEqual(self.drawer.message_for("right"), LOADING_TEXT)  # not "Nothing here"
        self.catalog.sectionReady.emit("live")
        self.assertEqual(self.drawer.message_for("right"), EMPTY_TEXT)

    def test_a_provider_that_is_still_loading_shows_loading_not_the_retry_row(self):
        self.catalog.loaded = False
        self.nav.reset_to("live")
        self.drawer.open_drawer()
        self.catalog.loading = True
        self.catalog.sectionFailed.emit("live", PROVIDER_LOADING_MESSAGE)
        self.assertEqual(self.drawer.message_for("right"), LOADING_TEXT)
        self.catalog.sectionFailed.emit("live", "Couldn't reach the provider")
        self.assertEqual(self.drawer.message_for("right"), LOADING_TEXT)
        before = list(self.catalog.requests)
        self.drawer.activate()  # Enter while loading: no retry into the same failure
        self.assertEqual(self.catalog.requests, before)

    def test_a_source_switch_shows_loading_instead_of_the_old_sections(self):
        self.drawer.open_drawer()
        backup = self.nav.children(None)[1]
        self.catalog.select_provider = lambda name=None, **kw: setattr(
            self.catalog, "loading", True
        )
        self.nav.open(backup)
        self.assertEqual(self.drawer.message_for("right"), LOADING_TEXT)
        self.assertIsNone(self.drawer._columns["right"].current)

    def test_a_failed_source_offers_to_connect_again(self):
        self.catalog.providerFailed.emit("Home", "Couldn't reach the provider")
        self.nav.focus_source("Home")
        self.drawer.open_drawer(focus_side="left")
        self.assertEqual(self.drawer.focus_side, "left")
        self.assertEqual(self.drawer.highlighted().key, "Home")
        self.assertEqual(self.drawer.message_for("right"), PROVIDER_ERROR_TEXT)
        self.drawer.activate()
        self.assertEqual(self.catalog.selected, ["Home"])

    def test_enter_on_the_connect_row_retries(self):
        self.catalog.providerFailed.emit("Home", "Couldn't reach the provider")
        self.nav.focus_source("Home")
        self.drawer.open_drawer()
        self.assertEqual(self.drawer.focus_side, "right")
        self.drawer.activate()
        self.assertEqual(self.catalog.selected, ["Home"])

    def test_unnumbered_rows_show_their_position_in_the_group_even_when_filtered(self):
        self.catalog.live["10"] = [{"id": i, "name": f"Chan {i}"} for i in range(5)]
        self.nav.changed.emit()
        self.drawer.open_drawer()
        col = self.drawer._columns["right"]
        numbers = [col.model.index(r).data(NumberRole) for r in range(5)]
        self.assertEqual(numbers, ["1", "2", "3", "4", "5"])
        self.drawer.type_text("chan 3")
        self.assertEqual(col.model.rowCount(), 1)
        self.assertEqual(col.model.index(0).data(NumberRole), "4")  # its number in the group

    def many_groups(self, count=8):
        self.catalog.groups = lambda section: [
            Group(section, str(i), f"Group {i}", 1) for i in range(count)
        ]
        self.catalog.live = {str(i): [{"id": i, "name": f"Chan {i}"}] for i in range(count)}
        self.nav.reset_to("live", "0")
        self.opened = []
        real_open = self.nav.open
        self.nav.open = lambda node: (self.opened.append(node), real_open(node))[1]

    def test_left_preview_is_debounced(self):
        self.many_groups()
        self.drawer.open_drawer()
        self.drawer.left()
        for _ in range(5):
            self.drawer.move_by(1)
        self.assertEqual(self.opened, [])
        self.assertEqual(self.drawer.highlighted().title, "Group 5")
        elapse(320)
        self.assertEqual([n.title for n in self.opened], ["Group 5"])

    def test_right_flushes_pending_preview(self):
        self.many_groups()
        self.drawer.open_drawer()
        self.drawer.left()
        self.drawer.move_by(1)
        self.drawer.move_by(1)
        self.drawer.right()
        self.assertEqual([n.title for n in self.opened], ["Group 2"])
        self.assertEqual(self.nav.path[-1].title, "Group 2")
        self.assertEqual(self.drawer.focus_side, "right")

    def test_sources_are_highlighted_not_previewed(self):
        self.nav.open(self.nav.path[0])
        self.drawer.open_drawer()
        self.drawer.left()
        self.drawer.move_by(1)
        elapse(320)
        self.assertEqual(self.catalog.selected, [])
        self.assertEqual(self.drawer.highlighted().title, "Backup")
        self.drawer.right()
        self.assertEqual(self.catalog.selected, ["Backup"])

    # ---- rows that leave the drawer: Settings, Add provider, the Library's sections ---------

    def record_hand_offs(self):
        self.actions, self.library = [], []
        self.drawer.actionRequested.connect(self.actions.append)
        self.drawer.libraryRequested.connect(self.library.append)

    def open_on_the_sources(self):
        self.nav.focus_source("Home")
        self.drawer.open_drawer(focus_side="left")
        self.record_hand_offs()
        return self.drawer._columns["left"]

    # ---- the header row's buttons: refresh, then Settings, at its right end ------------------

    def click(self, point):
        QTest.mouseClick(self.drawer, Qt.LeftButton, Qt.NoModifier, point.toPoint())

    def test_the_header_row_ends_with_refresh_then_settings(self):
        self.drawer.open_drawer("1")
        self.record_hand_offs()
        d = self.drawer
        gear, refresh, crumb = d.settings_gear_rect(), d.refresh_rect(), d.breadcrumb_text_rect()
        left, side = d._columns["left"].rect, d._units.px(PAD_SIDE_U)
        self.assertFalse(crumb.isEmpty())  # the provider's name is on the row
        self.assertAlmostEqual(gear.right(), left.right() + 1 - side, delta=1)  # right-aligned
        self.assertLess(refresh.right(), gear.left())  # refresh just left of the gear
        for icon in (gear, refresh):
            self.assertAlmostEqual(icon.center().y(), crumb.center().y(), delta=1)  # its row
        self.assertLessEqual(crumb.right(), refresh.left())  # the name never runs under them
        self.click(gear.center())
        self.click(refresh.center())
        self.assertEqual(self.actions, [SETTINGS_ROW, REFRESH_ACTION])

    def test_the_header_has_search_left_of_refresh(self):
        # The owner's mouse-only rule (2.0.1): Ctrl/⌘+F has a button in the channel list.
        self.drawer.open_drawer("1")
        self.record_hand_offs()
        d = self.drawer
        search, refresh, crumb = d.search_rect(), d.refresh_rect(), d.breadcrumb_text_rect()
        self.assertLess(search.right(), refresh.left())
        self.assertAlmostEqual(search.center().y(), crumb.center().y(), delta=1)  # its row
        self.assertLessEqual(crumb.right(), search.left())  # the names never run under it
        self.click(search.center())
        self.assertEqual(self.actions, [SEARCH_ACTION])
        self.assertEqual(self.library, [])
        self.assertTrue(self.drawer.isVisible())  # the shell opens the search

    def test_the_header_buttons_take_clicks_over_28_px(self):
        self.drawer.open_drawer("1")
        self.record_hand_offs()
        d = self.drawer
        for name, rect in (
            (SEARCH_ACTION, d.search_rect()),
            (REFRESH_ACTION, d.refresh_rect()),
            (SETTINGS_ROW, d.settings_gear_rect()),
        ):
            with self.subTest(name=name):
                hit = d.header_hit_rect(rect)
                self.assertGreaterEqual(min(hit.width(), hit.height()), 28)
                self.assertTrue(hit.contains(rect))
                self.actions.clear()
                self.click(QPointF(hit.left() + 1, hit.center().y()))  # its hit area's edge
                self.assertEqual(self.actions, [name])

    def test_in_a_small_window_the_hit_areas_never_reach_their_neighbours(self):
        self.host.resize(640, 360)
        self.drawer.open_drawer("1")
        d = self.drawer
        hits = [d.header_hit_rect(r) for r in (d.search_rect(), d.refresh_rect())]
        self.assertLessEqual(hits[0].right(), hits[1].left())
        self.assertLessEqual(
            d.header_hit_rect(d.back_rect()).right(), d.breadcrumb_text_rect().left()
        )
        self.assertLessEqual(d.breadcrumb_text_rect().right(), hits[0].left())

    def test_the_header_buttons_name_their_keys(self):
        self.drawer.open_drawer("1")
        d = self.drawer
        self.assertEqual(d.tooltip_at(d.search_rect().center()), key_hint("Search", "Ctrl+F"))
        self.assertEqual(
            d.tooltip_at(d.settings_gear_rect().center()), key_hint("Settings", "Ctrl+,")
        )
        self.assertEqual(
            d.tooltip_at(d.refresh_rect().center()),
            key_hint("Refresh channels and guide", "Ctrl+R"),
        )

    def test_the_buttons_are_there_on_the_sources_layer_too(self):
        self.open_on_the_sources()
        for rect in (
            self.drawer.settings_gear_rect(),
            self.drawer.refresh_rect(),
            self.drawer.search_rect(),
        ):
            self.assertTrue(self.drawer._columns["left"].rect.contains(rect.toRect()))
        self.click(self.drawer.settings_gear_rect().center())
        self.assertEqual(self.actions, [SETTINGS_ROW])

    # ---- the header row's left end: ‹ back and the names (owner, 2.0.1) ----------------------

    def crumb(self, index):
        """The header's name for the path's `index`-th node (None: the elided head)."""
        return next(rect for i, _text, rect in self.drawer.breadcrumb_parts() if i == index)

    def test_back_is_hidden_on_the_sources_and_goes_a_layer_up_elsewhere(self):
        self.drawer.open_drawer("1")  # Home › Live TV, the groups on the left
        self.record_hand_offs()
        back, text = self.drawer.back_rect(), self.drawer.breadcrumb_text_rect()
        self.assertFalse(back.isEmpty())
        self.assertLessEqual(back.right(), text.left())  # at the row's left, before the names
        self.assertAlmostEqual(back.center().y(), text.center().y(), delta=1)
        self.click(back.center())  # as ← with the focus on the left column
        self.assertEqual([n.layer for n in self.nav.path], ["source", "section"])
        self.assertEqual(self.drawer.focus_side, "left")
        self.assertEqual(self.drawer.highlighted().title, "Live TV")
        self.click(self.drawer.back_rect().center())
        self.assertEqual([n.layer for n in self.nav.path], ["source"])  # the sources
        self.assertEqual(self.drawer.highlighted().title, "Home")
        self.assertTrue(self.drawer.back_rect().isEmpty())  # the top layer
        self.assertEqual(self.drawer.breadcrumb_parts(), [])
        self.assertEqual(self.actions, [])

    def test_a_quick_second_click_on_back_goes_up_again(self):
        # 2.0.1 review M1: as the guide's ‹ ›, a double-click on ‹ counts twice: two layers.
        self.drawer.open_drawer("1")
        back = self.drawer.mapTo(self.host, self.drawer.back_rect().center().toPoint())
        QTest.mouseDClick(self.host.windowHandle(), Qt.LeftButton, Qt.NoModifier, back)
        self.assertEqual([n.layer for n in self.nav.path], ["source"])
        self.assertEqual(self.drawer.highlighted().title, "Home")

    def test_a_click_on_the_providers_name_shows_the_sources_to_switch_provider(self):
        self.drawer.open_drawer("1")
        self.record_hand_offs()
        self.assertEqual([t for _i, t, _r in self.drawer.breadcrumb_parts()], ["Home", "Live TV"])
        self.click(self.crumb(0).center())
        self.assertEqual([(n.layer, n.key) for n in self.nav.path], [("source", "Home")])
        self.assertEqual(self.drawer.focus_side, "left")
        self.assertEqual(self.drawer.highlighted().title, "Home")  # the current provider
        self.drawer.move_by(1)
        self.drawer.activate()  # another one is picked
        self.assertEqual(self.catalog.selected, ["Backup"])
        self.assertEqual(self.actions, [])

    def test_a_click_on_the_sections_name_shows_its_groups(self):
        self.drawer.open_drawer("1")  # the focus on the channels
        self.click(self.crumb(1).center())
        self.assertEqual(self.drawer.focus_side, "left")
        left = self.drawer._columns["left"]
        self.assertEqual(left.nodes, self.nav.children(self.nav.path[1]))  # Live TV's groups
        self.assertEqual(self.drawer.highlighted().title, "News")
        self.assertEqual(self.nav.path[-1].key, "10")  # its channels stay on the right

    def test_a_click_on_a_name_further_up_shows_that_layer(self):
        # Wide enough for three whole names beside ⌕ ↻ ⚙ (at 1280 the head elides).
        self.host.resize(1600, 900)
        self.catalog.movies = {"5": [{"id": "s1", "name": "Show"}]}
        self.nav.reset_to("series", "5")
        self.nav.open(self.nav.children(self.nav.path[-1])[0])  # Home › Series › Films › Show
        self.drawer.open_drawer()
        self.assertEqual(
            [t for _i, t, _r in self.drawer.breadcrumb_parts()], ["Home", "Series", "Films"]
        )
        self.click(self.crumb(1).center())  # Series: its groups on the left, Films chosen
        self.assertEqual([n.title for n in self.nav.path], ["Home", "Series", "Films"])
        self.assertEqual(self.drawer.highlighted().title, "Films")

    def test_the_names_never_run_under_the_buttons_and_an_elided_head_goes_up(self):
        self.catalog.provider = "A provider whose name is far too long for the column"
        self.nav.reset_to("live", "10")
        self.drawer.open_drawer("1")
        d = self.drawer
        back, search, text = d.back_rect(), d.search_rect(), d.breadcrumb_text_rect()
        self.assertLessEqual(back.right(), text.left())
        self.assertLessEqual(text.right(), search.left())
        parts = d.breadcrumb_parts()
        self.assertEqual([i for i, _t, _r in parts], [None, 1])  # the head is elided
        self.assertTrue(parts[0][1].startswith("…"), parts[0][1])
        self.assertEqual(parts[1][1], "Live TV")
        for _i, _t, rect in parts:
            self.assertTrue(text.contains(rect), rect)
        self.click(parts[0][2].center())  # a layer up
        self.assertEqual([n.layer for n in self.nav.path], ["source", "section"])

    def test_a_click_on_a_name_hands_nothing_to_the_shell(self):
        self.drawer.open_drawer("1")
        self.record_hand_offs()
        self.click(self.drawer.breadcrumb_text_rect().center())
        self.assertEqual(self.actions, [])

    def test_the_sources_column_ends_with_settings_add_provider_and_open_file(self):
        column = self.open_on_the_sources()
        rows = [column.model.index(r) for r in range(column.model.rowCount())]
        self.assertEqual(
            [(r.data(), r.data(IconRole)) for r in rows],
            [
                ("Home", None),
                ("Backup", None),
                ("Settings", "gear"),
                ("Add provider", "plus"),
                ("Open file", "file"),  # Ctrl/⌘+O without the menu bar (owner, 2.0.1)
            ],
        )
        self.assertEqual(self.drawer.highlighted().key, "Home")

    def test_a_click_on_open_file_asks_for_it(self):
        column = self.open_on_the_sources()
        path = self.nav.path
        self.drawer._on_row_clicked("left", 4)
        self.assertEqual(self.actions, [OPEN_FILE_ROW])
        self.assertEqual(column.current.key, OPEN_FILE_ROW)
        self.assertEqual((self.nav.path, self.catalog.selected, self.library), (path, [], []))

    def test_arrows_and_enter_reach_settings_and_add_provider(self):
        self.open_on_the_sources()
        path = self.nav.path
        self.drawer.move_by(1)
        self.drawer.move_by(1)
        self.assertEqual(self.drawer.highlighted().key, SETTINGS_ROW)
        elapse(PREVIEW_WAIT_MS)  # highlighting an action row starts nothing
        self.assertEqual((self.actions, self.nav.path, self.catalog.selected), ([], path, []))
        self.drawer.activate()  # Enter (OK on a remote)
        self.drawer.move_by(1)
        self.drawer.right()  # → like Enter
        self.assertEqual(self.actions, [SETTINGS_ROW, ADD_PROVIDER_ROW])
        # The drawer stays on the row (the dialog opens over it) and nothing else happened.
        self.assertTrue(self.drawer.isVisible())
        self.assertEqual(self.drawer.focus_side, "left")
        self.assertEqual(self.drawer.highlighted().key, ADD_PROVIDER_ROW)
        self.assertEqual((self.nav.path, self.catalog.selected, self.library), (path, [], []))

    def test_a_click_on_settings_asks_for_it(self):
        column = self.open_on_the_sources()
        self.drawer._on_row_clicked("left", 2)
        self.assertEqual(self.actions, [SETTINGS_ROW])
        self.assertEqual(column.current.key, SETTINGS_ROW)

    def test_choosing_movies_or_series_hands_them_to_the_library(self):
        self.nav.open(self.nav.path[0])  # the sources: the right column lists the sections
        self.drawer.open_drawer()
        self.record_hand_offs()
        self.drawer.move_by(1)
        self.assertEqual(self.drawer.highlighted().title, "Movies")
        self.drawer.activate()
        self.drawer.move_by(1)
        self.drawer.right()
        self.assertEqual(self.library, ["movies", "series"])
        self.assertEqual([n.layer for n in self.nav.path], ["source"])  # not browsed here

    def test_a_highlighted_library_section_is_not_previewed(self):
        self.drawer.open_drawer()  # on the live group: ← twice shows the sections on the left
        self.record_hand_offs()
        self.drawer.left()
        self.drawer.left()
        self.assertEqual(self.drawer.highlighted().title, "Live TV")
        self.drawer.move_by(1)
        elapse(PREVIEW_WAIT_MS)
        self.assertEqual(self.nav.path[-1].key, "live")  # Movies was not opened in the drawer
        self.assertNotIn(("section", "movies"), self.catalog.requests)
        self.drawer.right()
        self.assertEqual(self.library, ["movies"])
        self.assertEqual(self.nav.path[-1].key, "live")

    # ---- a highlight the filter hid is not chosen by Enter (review finding) -------------

    def test_enter_after_the_filter_hid_settings_chooses_what_is_highlighted(self):
        self.open_on_the_sources()
        self.drawer.move_by(2)
        self.assertEqual(self.drawer.highlighted().key, SETTINGS_ROW)
        self.drawer.type_text("h")  # only Home matches: the highlight falls back to it
        self.assertEqual(self.drawer.highlighted().key, "Home")
        self.drawer.activate()  # raised KeyError: 'action' (NavModel.open of the hidden row)
        self.assertEqual((self.actions, self.catalog.selected, self.library), ([], [], []))
        self.assertEqual([n.key for n in self.nav.path], ["Home"])

    def test_enter_after_the_filter_hid_a_provider_selects_nothing(self):
        self.open_on_the_sources()
        self.drawer.move_by(1)
        self.assertEqual(self.drawer.highlighted().key, "Backup")
        self.drawer.type_text("h")
        self.assertEqual(self.drawer.highlighted().key, "Home")
        self.drawer.activate()
        self.assertEqual(self.catalog.selected, [])  # Backup was hidden: never set up
        self.assertEqual([n.key for n in self.nav.path], ["Home"])

    def test_enter_after_the_filter_hid_movies_does_not_browse_them(self):
        self.drawer.open_drawer()
        self.record_hand_offs()
        self.drawer.left()
        self.drawer.left()  # the sections on the left, on Live TV
        self.drawer.move_by(1)
        self.assertEqual(self.drawer.highlighted().title, "Movies")
        self.drawer.type_text("l")  # only Live TV matches
        self.assertEqual(self.drawer.highlighted().title, "Live TV")
        self.drawer.activate()
        self.assertEqual(self.nav.path[-1].key, "live")  # not opened in the drawer
        self.assertNotIn(("section", "movies"), self.catalog.requests)
        self.assertEqual(self.library, [])

    def test_left_typing_highlights_and_opens_first_match(self):
        self.many_groups()
        self.drawer.open_drawer()
        self.drawer.left()
        self.drawer.type_text("group 6")
        self.assertEqual(self.drawer.highlighted().title, "Group 6")
        self.drawer.activate()
        self.assertEqual(self.nav.path[-1].title, "Group 6")
        self.assertEqual(self.drawer.focus_side, "right")

    def test_filter_folds_turkish_and_accents(self):
        self.catalog.live["10"] = [
            {"id": 7, "name": "İstanbul"},
            {"id": 8, "name": "ısı"},
            {"id": 9, "name": "Café"},
        ]
        self.nav.changed.emit()
        self.drawer.open_drawer()
        self.drawer.type_text("ist")
        self.assertEqual(self.drawer.highlighted().title, "İstanbul")
        self.drawer.escape()
        self.drawer.type_text("isi")
        self.assertEqual(self.drawer.highlighted().title, "ısı")
        self.drawer.escape()
        self.drawer.type_text("cafe")
        self.assertEqual(self.drawer.highlighted().title, "Café")

    def test_filter_resets_when_parent_changes(self):
        self.nav.open(self.nav.path[0])
        self.drawer.open_drawer()
        self.drawer.type_text("liv")
        self.assertEqual(self.drawer.filter_text, "liv")
        self.drawer.right()  # opens Live TV; the right column's parent changes
        self.assertEqual(self.drawer.filter_text, "")

    def test_page_moves_several_rows(self):
        self.catalog.live["10"] = [{"id": i, "name": f"Chan {i}"} for i in range(60)]
        self.nav.changed.emit()
        self.drawer.open_drawer()
        self.drawer.page(1)
        first = self.drawer.highlighted().title
        self.assertNotIn(first, ("Chan 0", "Chan 1"))
        self.drawer.page(-1)
        self.assertEqual(self.drawer.highlighted().title, "Chan 0")

    def test_focus_key_highlights_loaded_row(self):
        self.drawer.open_drawer("2")
        self.assertEqual(self.drawer.highlighted().title, "Two")

    def test_focus_key_applies_after_loading(self):
        self.catalog.live = {}
        self.nav.changed.emit()
        self.drawer.open_drawer("2")
        self.assertEqual(self.drawer.message_for("right"), "Loading…")
        self.catalog.live = {"10": [{"id": 1, "name": "One"}, {"id": 2, "name": "Two"}]}
        self.catalog.groupReady.emit("live", "10")
        self.assertEqual(self.drawer.highlighted().title, "Two")

    def test_unknown_focus_key_falls_back_to_first_row(self):
        self.drawer.open_drawer("nope")
        self.assertEqual(self.drawer.highlighted().title, "One")

    def test_stale_preview_is_dropped(self):
        self.many_groups()
        self.drawer.open_drawer()
        self.drawer.left()
        self.drawer.move_by(1)
        self.nav.reset_to("live")
        elapse(320)
        self.assertEqual(self.opened, [])


class HostedDrawerInputTests(unittest.TestCase):
    """Wheel and clicks over the drawer stay in it: the overlay host behind would zap or
    toggle the drawer (real widgets in a real OverlayHost; QTest's window events propagate
    like the platform's)."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        previous = None

        def quiet(mode, context, message):
            # The offscreen platform has no window stacking or masks and says so on every
            # raise_() and setMask() of the host: artefacts of the test platform.
            if message in _OFFSCREEN_NOISE:
                return
            if previous is not None:
                previous(mode, context, message)
            else:
                sys.stderr.write(f"{message}\n")

        previous = qInstallMessageHandler(quiet)
        cls.addClassCleanup(qInstallMessageHandler, previous)

    def setUp(self):
        self.catalog = FakeCatalog()
        self.catalog.live = {"10": [{"id": i, "name": f"Chan {i}"} for i in range(80)]}
        self.nav = NavModel(self.catalog, SimpleNamespace(favorites=[], watch_history=[]))
        self.window = QWidget()
        self.window.resize(1280, 720)
        surface = QWidget(self.window)
        surface.setGeometry(0, 0, 1280, 720)
        self.addCleanup(self.window.deleteLater)
        self.addCleanup(self.window.close)
        self.host = OverlayHost(self.window, surface)
        self.drawer = Drawer(self.nav, None, lambda item: None, self.host)
        self.host.add_overlay(self.drawer)
        self.window.show()
        self.assertTrue(QTest.qWaitForWindowExposed(self.window))
        self.nav.reset_to("live", "10")
        self.catalog.sectionReady.emit("live")
        self.drawer.open_drawer()
        self.assertTrue(QTest.qWaitForWindowExposed(self.host))
        self.events = []
        for name in ("clicked", "doubleClicked", "wheelSteps"):
            getattr(self.host, name).connect(lambda *a, n=name: self.events.append(n))
        self.closed = []
        self.drawer.closed.connect(lambda: self.closed.append(True))

    def wheel(self, point, steps=1):
        for _ in range(steps):
            QTest.wheelEvent(self.host.windowHandle(), QPointF(point), QPoint(0, -120))

    def click(self, point):
        QTest.mouseClick(self.host.windowHandle(), Qt.MouseButton.LeftButton, pos=point)
        elapse(QApplication.doubleClickInterval() + 80)  # a click is reported late

    def test_wheel_at_the_end_of_a_list_does_not_reach_the_host(self):
        view = self.drawer._columns["right"].view
        view.scrollToBottom()
        inside = view.geometry().center()
        self.wheel(inside, 3)
        self.assertEqual(self.events, [])

    def test_wheel_over_the_header_padding_and_fade_does_not_reach_the_host(self):
        for point in (QPoint(100, 30), QPoint(500, 30), QPoint(330, 400), QPoint(780, 360)):
            self.wheel(point)
        self.assertEqual(self.events, [])

    def test_clicks_on_the_breadcrumb_filter_and_padding_keep_the_drawer(self):
        for point in (QPoint(100, 30), QPoint(500, 30), QPoint(330, 400)):
            self.click(point)
        self.assertEqual(self.events, [])
        self.assertTrue(self.drawer.isVisible())

    def test_a_double_click_on_the_drawer_does_not_toggle_fullscreen(self):
        QTest.mouseDClick(self.host.windowHandle(), Qt.MouseButton.LeftButton, pos=QPoint(500, 30))
        elapse(QApplication.doubleClickInterval() + 80)
        self.assertNotIn("doubleClicked", self.events)
        self.assertTrue(self.drawer.isVisible())

    def test_a_click_on_the_fade_closes_the_drawer_like_a_click_on_the_video(self):
        self.click(QPoint(780, 360))
        self.assertFalse(self.drawer.isVisible())
        self.assertEqual(self.closed, [True])
        self.assertEqual(self.events, [])  # closed once, not toggled open again

    def test_a_quick_second_back_over_the_rows_counts(self):
        backs = []
        self.host.backPressed.connect(lambda: backs.append(True))
        over_rows = self.drawer._columns["right"].view.geometry().center()
        QTest.mouseDClick(self.host.windowHandle(), Qt.MouseButton.BackButton, pos=over_rows)
        self.assertEqual(len(backs), 2)  # two presses, not a double-click on a row

    def move(self, point):
        QTest.mouseMove(self.host.windowHandle(), point)

    def test_pointer_moves_over_the_drawer_still_reach_the_host(self):
        # Review Minor 5: the drawer tracks the pointer for its header buttons and passes the
        # moves on, so the host still hears them (the cursor, the player's controls).
        moved = []
        self.host.mouseMoved.connect(lambda: moved.append(True))
        drawer = self.drawer

        def on_host(rect):
            return drawer.mapTo(self.host, rect.center().toPoint())

        name = drawer.breadcrumb_parts()[0][2]
        points = (
            QPoint(330, 400),  # the padding
            on_host(name),  # the header's provider name
            on_host(drawer.back_rect()),
            on_host(drawer.refresh_rect()),
        )
        for count, point in enumerate(points, start=1):
            self.move(point)
            self.assertEqual(len(moved), count, point)

    def test_hovering_a_header_button_shows_the_pointing_hand(self):
        drawer = self.drawer
        names = [rect for _i, _t, rect in drawer.breadcrumb_parts()]
        for rect in (
            drawer.search_rect(),
            drawer.refresh_rect(),
            drawer.settings_gear_rect(),
            drawer.back_rect(),
            *names,
        ):
            self.move(QPoint(330, 400))
            self.assertEqual(drawer.cursor().shape(), Qt.CursorShape.ArrowCursor)
            self.move(drawer.mapTo(self.host, rect.center().toPoint()))
            self.assertEqual(drawer.cursor().shape(), Qt.CursorShape.PointingHandCursor)

    def test_hovering_a_name_brightens_and_underlines_it(self):
        drawer = self.drawer
        name = drawer.breadcrumb_parts()[0][2]
        self.move(QPoint(330, 400))
        plain = drawer.grab(name.toAlignedRect()).toImage()
        self.move(drawer.mapTo(self.host, name.center().toPoint()))
        self.assertNotEqual(drawer.grab(name.toAlignedRect()).toImage(), plain)

    def right_click(self, view, row):
        rect = view.visualRect(view.model().index(row, 0))
        point = view.viewport().mapTo(self.host, rect.center())
        QTest.mouseClick(self.host.windowHandle(), Qt.MouseButton.RightButton, pos=point)
        return point

    def test_a_right_click_on_a_channel_asks_for_its_context_menu(self):
        requests = []
        self.drawer.contextRequested.connect(lambda node, pos: requests.append((node, pos)))
        before = self.drawer.highlighted()
        point = self.right_click(self.drawer._columns["right"].view, 3)
        self.assertEqual([(n.layer, n.title) for n, _ in requests], [("item", "Chan 3")])
        self.assertEqual(requests[0][1], self.host.mapToGlobal(point))
        self.assertEqual(self.drawer.highlighted(), before)  # the highlight stays put
        self.assertEqual(self.events, [])
        self.assertTrue(self.drawer.isVisible())

    def test_a_right_click_on_a_group_asks_for_nothing(self):
        requests = []
        self.drawer.contextRequested.connect(lambda node, pos: requests.append(node))
        self.right_click(self.drawer._columns["left"].view, 0)
        self.assertEqual(requests, [])
        self.assertEqual(self.events, [])


if __name__ == "__main__":
    unittest.main()
