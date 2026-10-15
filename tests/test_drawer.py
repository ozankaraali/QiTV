import os
import sys
from types import SimpleNamespace
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, Qt, qInstallMessageHandler
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from services.catalog import PROVIDER_LOADING_MESSAGE, Group
from services.navigation import ADD_PROVIDER_ROW, SETTINGS_ROW, NavModel
from tests.test_navigation import FakeCatalog
from ui.overlay_host import OverlayHost
from ui.overlays.channel_rows import NumberRole
from ui.overlays.drawer import (
    EMPTY_TEXT,
    LOADING_TEXT,
    PREVIEW_DEBOUNCE_MS,
    PROVIDER_ERROR_TEXT,
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
        QTest.qWait(320)
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
        QTest.qWait(320)
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
        QTest.qWait(320)
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

    def test_the_sources_column_ends_with_settings_and_add_provider(self):
        column = self.open_on_the_sources()
        rows = [column.model.index(r) for r in range(column.model.rowCount())]
        self.assertEqual(
            [(r.data(), r.data(IconRole)) for r in rows],
            [("Home", None), ("Backup", None), ("Settings", "gear"), ("Add provider", "plus")],
        )
        self.assertEqual(self.drawer.highlighted().key, "Home")

    def test_arrows_and_enter_reach_settings_and_add_provider(self):
        self.open_on_the_sources()
        path = self.nav.path
        self.drawer.move_by(1)
        self.drawer.move_by(1)
        self.assertEqual(self.drawer.highlighted().key, SETTINGS_ROW)
        QTest.qWait(PREVIEW_WAIT_MS)  # highlighting an action row starts nothing
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
        QTest.qWait(PREVIEW_WAIT_MS)
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
        QTest.qWait(320)
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
        QTest.qWait(QApplication.doubleClickInterval() + 80)  # a click is reported late

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
        QTest.qWait(QApplication.doubleClickInterval() + 80)
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
