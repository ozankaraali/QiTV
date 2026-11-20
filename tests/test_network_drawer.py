"""Network streams in the channel list (plan 4, Task 5): the links that played, a source after
the providers' once the store has one. It is no provider: All providers, the providers' names
on rows and every other count of providers never see it. Its rows say what each link is ("Live",
"Film · stopped at 41:12") or why it failed last time, in the warning colour; one chosen plays
through the network player (its kind, whose stream it is), as one the find finds by its name
does. Fictional hosts; nothing is fetched."""

import os
from types import SimpleNamespace
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from job_fixtures import ImmediateRunner
from mixed_fixtures import live_content
from timer_fixtures import elapse, wait_until

from services.find import build_find_index, find_channels
from services.navigation import (
    LINK_LAYER,
    LINKS_LAYER,
    RECENT_GROUP,
    SETTINGS_ROW,
    LinkRow,
    NavModel,
    NavNode,
)
from services.network_streams import ACCESS_ENDED, FILM, LIVE
from services.provider_ids import ALL_PROVIDERS, NETWORK_PROVIDER
from services.watch_state import WatchState
from tests.test_find import FakeProviders
from tests.test_navigation import FakeCatalog
from tests.test_tv_window import WindowCase
from ui.find_session import DEBOUNCE_MS, FindSession
from ui.overlays import drawer as drawer_module
from ui.overlays.channel_rows import ChannelRowsModel
from ui.overlays.drawer import CountRole, Drawer, SubRole

LIVE_URL = "https://cdn.example/live/harbour/index.m3u8?token=aaa"
FILM_URL = "https://vod.example/films/lecture.mkv?token=aaa"
FAILED_URL = "https://cdn.example/live/channel/index.m3u8?token=old"
OTHER_FILM_URL = "https://vod.example/films/concert.mp4"
ROWS = [
    LinkRow("a1", "Harbour News", LIVE),
    LinkRow("b2", "Lecture recording", FILM, stopped_ms=2_472_000),
    LinkRow("c3", "cdn.example — channel", LIVE, failed=ACCESS_ENDED),
    LinkRow("d4", "Concert", FILM),
]
FIND_WAIT = DEBOUNCE_MS + 100


class OneProvider(FakeCatalog):
    """The navigation tests' catalog with one provider (they have two)."""

    def provider_names(self):
        return ["Home"]


def heads(nodes):
    """The sources column's rows above its action rows: (layer, key, title, count)."""
    return [(n.layer, n.key, n.title, n.count) for n in nodes if n.layer != "action"]


class SourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.catalog = OneProvider()
        self.nav = NavModel(self.catalog, SimpleNamespace(favorites=[], watch_history=[]))
        self.rows: list[LinkRow] = []
        self.nav.set_links(lambda: list(self.rows))
        self.changes = []
        self.nav.changed.connect(lambda: self.changes.append(True))

    def test_the_source_appears_with_the_first_link_and_is_not_a_provider(self):
        nav, catalog = self.nav, self.catalog
        self.assertEqual(heads(nav.children(None)), [("source", "Home", "Home", None)])
        self.rows.append(ROWS[0])
        nav.links_changed()  # the network player's `changed`
        self.assertTrue(self.changes)
        top = nav.children(None)
        title = "Network streams"
        self.assertEqual(
            heads(top),
            [("source", "Home", "Home", None), (LINKS_LAYER, NETWORK_PROVIDER, title, 1)],
        )
        self.assertNotIn(ALL_PROVIDERS, [n.key for n in top])  # one provider: nothing to mix
        self.assertEqual(top[2].key, SETTINGS_ROW)  # the action rows follow it
        self.assertEqual(catalog.provider_names(), ["Home"])
        self.assertFalse(nav.shows_provider(NavNode("group", RECENT_GROUP, "Recent")))
        # Opened: its links, no provider chosen, nothing loading (a provider's setup aside).
        catalog.loading = True
        source = top[1]
        nav.open(source)
        self.assertEqual(catalog.selected, [])
        self.assertEqual(nav.path, [source])
        self.assertFalse(nav.is_loading(source))
        links = nav.children(source)
        self.assertEqual(
            [(n.layer, n.key, n.title) for n in links], [(LINK_LAYER, "a1", "Harbour News")]
        )
        self.assertTrue(nav.is_playable(links[0]))
        nav.open(links[0])  # a link plays; it is no layer to open
        self.assertEqual(nav.path, [source])
        catalog.loading = False
        catalog.providerReady.emit("Home")  # the provider set up meanwhile: the list stays
        self.assertEqual(nav.path, [source])
        # A second link: its count; then none: the source goes, the list back at the provider.
        self.rows.append(ROWS[1])
        nav.links_changed()
        self.assertEqual(heads(nav.children(None))[1][3], 2)
        self.rows.clear()
        nav.links_changed()
        self.assertEqual(heads(nav.children(None)), [("source", "Home", "Home", None)])
        self.assertEqual([n.key for n in nav.path], ["Home"])
        # Two providers: All providers first, as before, and the links after the providers.
        two = NavModel(FakeCatalog(), SimpleNamespace(favorites=[], watch_history=[]))
        two.set_links(lambda: ROWS[:1])
        self.assertEqual(
            [n.key for n in two.children(None) if n.layer != "action"],
            [ALL_PROVIDERS, "Home", "Backup", NETWORK_PROVIDER],
        )


class RowTests(unittest.TestCase):
    """The drawer over the navigation tests' catalog, one provider and four links."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.catalog = OneProvider()
        self.nav = NavModel(self.catalog, SimpleNamespace(favorites=[], watch_history=[]))
        self.nav.set_links(lambda: list(ROWS))
        self.host = QWidget()
        self.host.resize(1280, 720)
        self.host.show()
        self.addCleanup(self.host.deleteLater)
        self.addCleanup(self.host.close)
        self.drawer = Drawer(self.nav, None, lambda item: None, self.host)
        self.played = []
        self.drawer.playRequested.connect(lambda *args: self.played.append(args))

    def test_a_failed_link_says_why_in_the_list(self):
        drawer, nav = self.drawer, self.nav
        source = next(n for n in nav.children(None) if n.layer == LINKS_LAYER)
        nav.open(source)
        drawer.open_drawer()
        left, right = drawer._columns["left"], drawer._columns["right"]
        at = list(left.nodes).index(source)
        self.assertEqual(left.model.index(at).data(CountRole), 4)  # its count: the links
        subs = [right.model.index(r).data(SubRole) for r in range(len(right.nodes))]
        self.assertEqual(
            subs,
            [
                ("Live", False),
                ("Film · stopped at 41:12", False),
                # in the warning colour, as in Settings
                ("Didn't open last time (401: the link's access ran out)", True),
                ("Film", False),  # a film never stopped: from its start
            ],
        )
        drawn, elided = [], drawer_module.elided

        def spy(p, text, width):
            drawn.append(text)
            return elided(p, text, width)

        with mock.patch.object(drawer_module, "elided", spy):
            drawer.grab()  # two lines a row: its name, then what it is
        failed = "Didn't open last time (401: the link's access ran out)"
        for text in ("Harbour News", "Live", "Film · stopped at 41:12", failed):
            self.assertIn(text, drawn)
        view = right.view
        self.assertGreater(view.sizeHintForRow(0), left.view.sizeHintForRow(0))
        # Enter on a link asks for it to play, as a channel's row does.
        self.assertEqual(drawer.highlighted().key, "a1")
        drawer.move_by(1)
        drawer.activate()
        node, _siblings, parent = self.played[-1]
        self.assertEqual((node.layer, node.key, parent), (LINK_LAYER, "b2", source))


class FoundLinkTests(unittest.TestCase):
    """The find over one provider's channel and a film link of Network streams: the link
    plays as it was kept (Enter, a click), and is no channel otherwise."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.catalog = OneProvider()
        self.nav = NavModel(self.catalog, SimpleNamespace(favorites=[], watch_history=[]))
        self.host = QWidget()
        self.host.resize(1280, 720)
        self.host.show()
        self.addCleanup(self.host.deleteLater)
        self.addCleanup(self.host.close)
        self.drawer = Drawer(self.nav, None, lambda item: None, self.host)
        caches = [("Home", {"itv": live_content(["Lecture One"], {"News": [0]})})]
        link = {"id": "b2", "name": "Lecture recording", "_provider": NETWORK_PROVIDER}
        finder = FindSession(FakeProviders(caches), ImmediateRunner(), links=lambda: [link])
        self.drawer.set_finder(finder)
        self.events = []
        for signal, name in (
            (self.drawer.channelDragged, "drag"),
            (self.drawer.channelDropped, "drop"),
            (self.drawer.contextRequested, "menu"),
            (self.drawer.channelFound, "found"),
        ):
            signal.connect(lambda node, *_rest, name=name: self.events.append((name, node)))
        self.drawer.set_drag_enabled(lambda: True)  # the multiview's grid shows
        self.nav.reset_to("live", "10")  # a live section under the find
        self.drawer.open_drawer()
        self.drawer.type_text("lecture")
        elapse(FIND_WAIT)
        self.right = self.drawer._columns["right"]
        self.assertTrue(wait_until(lambda: self.row("Lecture recording") is not None))

    def row(self, title):
        return next(
            (r for r, n in enumerate(self.right.nodes) if n.layer == "item" and n.title == title),
            None,
        )

    def centre(self, title):
        view = self.right.view
        return view.visualRect(view.model().index(self.row(title), 0)).center()

    def mouse(self, kind, point, buttons=Qt.MouseButton.LeftButton):
        viewport = self.right.view.viewport()
        event = QMouseEvent(
            kind,
            QPointF(point),
            QPointF(viewport.mapToGlobal(point)),
            Qt.MouseButton.LeftButton,
            buttons,
            Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(viewport, event)

    def drag(self, title):
        start = self.centre(title)
        self.mouse(QEvent.Type.MouseButtonPress, start)
        self.mouse(QEvent.Type.MouseMove, start + QPoint(60, 0))
        self.mouse(QEvent.Type.MouseMove, start + QPoint(700, 40))  # out over the grid
        self.mouse(QEvent.Type.MouseButtonRelease, start + QPoint(700, 40), Qt.MouseButton.NoButton)

    def right_click(self, title):
        view = self.right.view
        point = view.viewport().mapTo(self.host, self.centre(title))
        QTest.mouseClick(self.host.windowHandle(), Qt.MouseButton.RightButton, pos=point)

    def test_a_found_link_never_drags_onto_a_tile_nor_has_a_channels_menu(self):
        self.drag("Lecture recording")
        self.right_click("Lecture recording")
        self.assertEqual(self.events, [])
        self.assertIsNone(self.drawer.dragging())
        # The provider's channel found beside it is a channel: it drags out, it has its menu.
        self.drag("Lecture One")
        self.right_click("Lecture One")
        self.assertEqual(
            [(kind, node.title) for kind, node in self.events],
            [("drag", "Lecture One"), ("drop", "Lecture One"), ("menu", "Lecture One")],
        )
        elapse(QApplication.doubleClickInterval() + 80)  # (the next press is no double-click)
        self.events.clear()
        # A click on the link plays it as it was kept (the find's `channelFound`).
        view = self.right.view
        where = self.centre("Lecture recording")
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=where)
        self.assertEqual([(kind, node.get("id")) for kind, node in self.events], [("found", "b2")])


class FindIndexTests(unittest.TestCase):
    def test_links_are_found_by_name_and_no_providers(self):
        links = [{"id": "b2", "name": "Lecture recording", "_provider": NETWORK_PROVIDER}]
        index = build_find_index([], links)
        hits = find_channels(index, "lecture")
        self.assertEqual(
            [(h.provider, h.title, h.group_id) for h in hits],
            [(NETWORK_PROVIDER, "Lecture recording", "")],
        )
        self.assertEqual(index.genres, [])


class WindowTests(WindowCase):
    """The real window over the fixture provider (one), its network player's store."""

    def keep(self, url, name, kind, when):
        store = self.window.network_streams
        entry = store.keep(url, name, kind)
        store.opened(entry.id, when)
        return entry

    def loaded(self):
        player = self.window.network_player
        player.start()
        self.assertTrue(wait_until(player.loaded))

    def links_source(self):
        return next(n for n in self.window.nav.children(None) if n.layer == LINKS_LAYER)

    def test_the_source_appears_with_the_first_link_and_is_not_a_provider(self):
        window = self.window
        self.loaded()
        names = window.catalog.provider_names()
        self.assertEqual(len(names), 1)
        self.assertNotIn(LINKS_LAYER, [n.layer for n in window.nav.children(None)])
        self.keep(LIVE_URL, "Harbour News", LIVE, 2.0)
        window.network_player.changed.emit()  # as it does once a link played
        top = window.nav.children(None)
        self.assertEqual(self.links_source().count, 1)
        self.assertNotIn(ALL_PROVIDERS, [n.key for n in top])
        self.assertEqual(window.catalog.provider_names(), names)
        self.assertEqual(window.catalogs.names(), names)

    def test_activating_a_link_plays_it(self):
        window = self.window
        self.loaded()
        live = self.keep(LIVE_URL, "Harbour News", LIVE, 2.0)
        film = self.keep(FILM_URL, "Lecture recording", FILM, 1.0)
        window.network_player.changed.emit()
        self.show()
        drawer = self.wait_for_drawer()
        with mock.patch.object(window.controller, "play_link") as play:
            for key in (Qt.Key.Key_Left, Qt.Key.Key_Left):
                QTest.keyClick(window, key)  # the sources column, on the provider
            QTest.keyClick(window, Qt.Key.Key_Down)
            self.assertEqual(drawer.highlighted().title, "Network streams")
            QTest.keyClick(window, Qt.Key.Key_Return)  # its links, on the right
            self.assertEqual(drawer.highlighted().key, live.id)
            QTest.keyClick(window, Qt.Key.Key_Return)
            self.assertFalse(drawer.isVisible())  # it plays: the list closes
            item, section = play.call_args.args
            self.assertEqual(section, "live")
            self.assertEqual(
                item,
                {
                    "id": live.id,
                    "name": "Harbour News",
                    "cmd": LIVE_URL,
                    "_provider": NETWORK_PROVIDER,
                },
            )
            # The film by a click: as a film (where it stopped), by its kept address.
            drawer.open_drawer()
            right = drawer._columns["right"]
            row = [n.key for n in right.nodes].index(film.id)
            view = right.view
            rect = view.visualRect(view.model().index(row))
            QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
            item, section = play.call_args.args
            self.assertEqual((item["id"], item["cmd"], section), (film.id, FILM_URL, "movies"))
            self.assertEqual(play.call_count, 2)
        elapse(QApplication.doubleClickInterval() + 80)  # no second-click guard left armed

    def test_a_failed_link_says_why_in_the_list(self):
        window = self.window
        self.loaded()
        failed = self.keep(FAILED_URL, "cdn.example — channel", LIVE, 3.0)
        window.network_streams.failed(failed.id, ACCESS_ENDED)
        film = self.keep(FILM_URL, "Lecture recording", FILM, 2.0)
        self.keep(OTHER_FILM_URL, "Concert", FILM, 1.0)
        # Where the film stopped, under the key it resumes by (Network streams', its id).
        cid = WatchState.content_id(NETWORK_PROVIDER, {"id": film.id}, item_type="movie")
        window.watch.update_position(cid, 2_472_000, 6_000_000)
        window.network_player.changed.emit()
        rows = window.nav.children(self.links_source())
        self.assertEqual(
            [(n.title, n.payload.failed, n.payload.stopped_ms) for n in rows],
            [
                ("cdn.example — channel", ACCESS_ENDED, None),
                ("Lecture recording", "", 2_472_000),
                ("Concert", "", None),
            ],
        )

    def test_find_finds_links(self):
        window = self.window
        self.loaded()
        self.show()
        drawer = self.wait_for_drawer()
        film = self.keep(FILM_URL, "Lecture recording", FILM, 1.0)
        window.network_player.changed.emit()
        right = drawer._columns["right"]

        def found(title):
            return next((n for n in right.nodes if n.layer == "item" and n.title == title), None)

        drawer.type_text("lecture")
        elapse(FIND_WAIT)
        self.assertTrue(wait_until(lambda: found("Lecture recording") is not None))
        node = found("Lecture recording")
        self.assertEqual(node.payload["_provider"], NETWORK_PROVIDER)
        row = list(right.nodes).index(node)
        # One provider: no provider's name under the rows, as in 2.0.
        self.assertEqual(right.model.index(row).data(ChannelRowsModel.ProviderRole), "")
        # A link kept while a find shows: it is found too (the find runs again).
        drawer.type_text(" ")
        self.keep(LIVE_URL, "Lecture hall live", LIVE, 2.0)
        window.network_player.changed.emit()
        elapse(FIND_WAIT)
        self.assertTrue(wait_until(lambda: found("Lecture hall live") is not None))
        # Chosen, it plays through the network player: the film as a film, not a channel.
        drawer.type_text("rec")  # "lecture rec": the film alone, highlighted
        elapse(FIND_WAIT)
        self.assertTrue(wait_until(lambda: found("Lecture hall live") is None))
        self.assertEqual(drawer.highlighted(), found("Lecture recording"))
        with mock.patch.object(window.controller, "play_link") as play:
            QTest.keyClick(window, Qt.Key.Key_Return)
        item, section = play.call_args.args
        self.assertEqual((item["id"], item["cmd"], section), (film.id, FILM_URL, "movies"))
        self.assertFalse(drawer.isVisible())


if __name__ == "__main__":
    unittest.main()
