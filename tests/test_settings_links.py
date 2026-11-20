"""Settings › Providers › Network streams (plan 4, Task 6): the links that played, listed after
the providers once one is kept, and never one of them. Each link's layer plays it first (with
no provider the channel list never opens, so its links stay reachable here), then renames it,
replaces its address (the same stream with a fresh token at once, another stream asked first:
Keep as a new link or Replace), shows its kind and when it last played, and removes it, asked
first. Every change is the network player's, written on a worker, and the channel list
follows. Fictional hosts; nothing is fetched."""

import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from timer_fixtures import elapse, wait_until

from services import network_streams as ns
from services.i18n import translate
from services.jobs import JobRunner
from services.navigation import LinkRow
from services.network_streams import (
    ACCESS_ENDED,
    FILM,
    LIVE,
    NetworkStreams,
    default_name,
    stream_id,
)
from services.provider_ids import NETWORK_PROVIDER
from services.recordings_list import short_when
from services.settings_model import (
    ACTION,
    ADD_PROVIDER,
    DANGER,
    FREE_CHANNELS,
    INFO,
    OPENS,
    PROVIDERS,
    TEXT,
    sections,
)
from tests.test_provider_changes import PANEL, PLAYLIST, temp_manager
from tests.test_settings_providers import FakeValues, FakeWindow
from tests.test_tv_window import WindowCase
from ui.keymap import Action
from ui.network_player import NetworkPlayer
from ui.open_link import NOT_A_LINK_TEXT, WHOLE_TEXT
from ui.overlays.drawer import link_sub
from ui.overlays.settings_card import CANCEL
from ui.overlays.settings_drawer import SettingsDrawer
from ui.overlays.settings_layout import Item, title_of
from ui.settings_controller import SettingsController
from ui.settings_links import (
    ADDRESS,
    KEEP_NEW,
    KIND,
    LINKS,
    NAME,
    OPEN_LINK,
    OPENED,
    PLAY,
    REMOVE,
    REPLACE,
    LinkRows,
    link_key,
    row_key,
)
from ui.settings_providers import ProviderRows, layer_key
from ui.tv_window import DROP

LIVE_URL = "https://cdn.example/live/harbour/index.m3u8?token=aaa"
FRESH = "https://cdn.example/live/harbour/index.m3u8?token=bbb"
OTHER = "https://cdn2.example/live/harbour/index.m3u8?token=ccc"
OTHER_FRESH = "https://cdn2.example/live/harbour/index.m3u8?token=ddd"
FILM_URL = "https://vod.example/films/lecture.mkv?token=aaa"
FILM_FRESH = "https://vod.example/films/lecture.mkv?token=bbb"
NEW_FILM = "https://vod.example/films/concert.mp4"
RTMP = "rtmp://media.example/live/harbour"  # a live link by its address: nothing is read
ACCOUNT = "http://panel.example:8080/get.php?username=demo&password=secret&type=m3u_plus"
NOW = 1_800_000_000.0
LIVE_ID, FILM_ID = stream_id(LIVE_URL), stream_id(FILM_URL)


def item_of(url, name):
    """The item the network player hands the controller for `url` (as it plays)."""
    return {"id": stream_id(url), "name": name, "cmd": url, "_provider": NETWORK_PROVIDER}


class FakeController(QObject):
    """What the network player asks of the TV controller: what it was handed to play, and
    where a film stopped."""

    linkPlayed = Signal(object, str, str)
    linkFailed = Signal(object, str, str, str)

    def __init__(self):
        super().__init__()
        self.played = []
        self.stops = {}
        self.forgotten = []  # the links whose resume point was forgotten

    def set_links(self, resolve):
        self.resolve = resolve

    def set_link_player(self, play):
        self.play_kept = play

    def set_link_stepper(self, step):
        self.step = step

    def play_link(self, item, section):
        self.played.append((dict(item), section))

    def stopped_at(self, item):
        return self.stops.get(item.get("id"))

    def forget_stopped(self, item):
        self.forgotten.append(item.get("id"))
        self.stops.pop(item.get("id"), None)


class LinksTests(unittest.TestCase):
    """A real Settings drawer over the Providers rows of a fake TV window, and a real network
    player over a store in a temporary folder (its file read and written on real threads)."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.config, self.manager = temp_manager(self, [PLAYLIST, PANEL])
        self.config.selected_provider_name = "Lists"
        self.window = FakeWindow(self.config, self.manager)
        self.window.open_link = mock.Mock()
        self.host = QWidget()
        self.host.resize(1280, 720)
        self.host.show()
        self.addCleanup(self.host.deleteLater)
        self.addCleanup(self.host.close)
        self.drawer = SettingsDrawer(
            lambda row: self.settings.value_text(row),
            lambda row: self.settings.lit(row),
            self.host,
        )
        self.settings = SettingsController(
            self.drawer,
            FakeValues(),
            lambda: sections("Darwin", self.rows.rows()),
            lambda: None,
        )
        self.rows = ProviderRows(self.window, self.settings, self.drawer, cards=mock.Mock())
        self.window.rows = self.rows
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / ns.FILE_NAME
        self.jobs = JobRunner()
        self.addCleanup(lambda: wait_until(lambda: self.jobs.pending() == 0))
        self.controller = FakeController()
        self.store = NetworkStreams(self.path)
        self.player = NetworkPlayer(
            self.store, self.jobs, self.controller, owner=lambda url: "", now=lambda: NOW
        )
        self.links = LinkRows(self.window, self.settings, self.drawer, self.player)
        self.rows.links = self.links
        self.changes = []
        self.player.changed.connect(lambda: self.changes.append(True))

    def tearDown(self):
        elapse(QApplication.doubleClickInterval() + 80)  # no second-click guard left armed

    # ---- helpers ------------------------------------------------------------------------------

    def seed(self, *links):
        """Links kept in an earlier run, (url, name, kind, opened, failed) each; the file read
        as the window reads it once it started."""
        older = NetworkStreams(self.path)
        for url, name, kind, opened, failed in links:
            entry = older.keep(url, name, kind)
            if opened:
                older.opened(entry.id, opened)
            if failed:
                older.failed(entry.id, failed)
        ns.write(self.path, older.save_bytes())
        self.loaded()

    def loaded(self):
        self.player.start()
        self.assertTrue(wait_until(self.player.loaded))

    def on_disk(self):
        """The file's entries by id, once every write is done."""
        self.assertTrue(wait_until(lambda: self.jobs.pending() == 0))
        if not self.path.exists():
            return {}
        return {row["id"]: row for row in json.loads(self.path.read_text())["entries"]}

    def key(self, action, payload=""):
        self.assertTrue(self.settings.handle(action, payload), action)

    def shown(self):
        return self.drawer._right_rows()

    def titles(self):
        return [title_of(Item(row.key, row.text, row=row)) for row in self.shown()]

    def move_to(self, key):
        for _ in range(len(self.shown()) + 1):
            row = self.drawer.focused_row()
            if row is not None and row.key == key:
                return row
            self.key(Action.NAV_DOWN)
        self.fail(f"no row {key}")

    def into_links(self):
        self.settings.open(PROVIDERS)
        self.key(Action.NAV_RIGHT)
        self.move_to(LINKS)
        self.key(Action.NAV_ACTIVATE)
        self.assertEqual(self.drawer.path(), (PROVIDERS, LINKS))

    def into(self, key):
        """The link kept under `key`'s own rows, the keys on the first."""
        self.into_links()
        self.move_to(link_key(key))
        self.key(Action.NAV_ACTIVATE)
        self.assertEqual(self.drawer.path(), (PROVIDERS, LINKS, link_key(key)))

    def value(self, key, field):
        rows = {row.key: row for row in self.shown()}
        return self.settings.value_text(rows[row_key(key, field)])

    def type_text(self, text):
        """Edit the focused text row: everything replaced by `text`, then Enter."""
        self.key(Action.NAV_ACTIVATE)
        self.assertTrue(self.drawer.editing())
        self.key(Action.SELECT_ALL)
        self.key(Action.NAV_TEXT, text)
        self.key(Action.NAV_ACTIVATE)
        self.assertFalse(self.drawer.editing())

    # ---- the row in Providers, and its list ---------------------------------------------------

    def test_network_streams_row_only_with_links(self):
        self.loaded()  # nothing kept yet
        self.settings.open(PROVIDERS)
        providers = [layer_key("Lists"), layer_key("Panel")]
        self.assertEqual(
            [row.key for row in self.shown()], providers + [ADD_PROVIDER, FREE_CHANNELS]
        )
        # The first link that plays: its row after the providers', at once while Settings shows.
        self.controller.linkPlayed.emit(item_of(LIVE_URL, "Harbour News"), LIVE, LIVE_URL)
        rows = self.shown()
        self.assertEqual(
            [row.key for row in rows], providers + [LINKS, ADD_PROVIDER, FREE_CHANNELS]
        )
        self.assertEqual(self.titles()[2], "Network streams")
        self.assertEqual((rows[2].kind, rows[2].sub), (OPENS, "1 link"))
        self.assertTrue(rows[2].help)
        # It is no provider: the list, its saves and its count never see it.
        self.assertEqual([p["name"] for p in self.manager.providers], ["Lists", "Panel"])
        self.assertNotIn("providers_saved", self.window.events)
        # A film that never played, kept with why: two links.
        film = item_of(FILM_URL, "Lecture recording")
        self.controller.linkFailed.emit(film, FILM, FILM_URL, ACCESS_ENDED)
        self.assertEqual(self.shown()[2].sub, "2 links")
        # Its list: each link by its name, what it is (or why it failed, in the warning
        # colour) as the channel list says it, then ＋ Open a link….
        self.key(Action.NAV_RIGHT)
        self.move_to(LINKS)
        self.key(Action.NAV_ACTIVATE)
        rows = self.shown()
        self.assertEqual(
            [(row.key, row.kind) for row in rows],
            [(link_key(LIVE_ID), OPENS), (link_key(FILM_ID), OPENS), (OPEN_LINK, ACTION)],
        )
        self.assertEqual(self.titles(), ["Harbour News", "Lecture recording", "＋ Open a link…"])
        self.assertTrue(all(row.literal for row in rows[:2]))  # names as given
        live_sub = link_sub(LinkRow(LIVE_ID, "Harbour News", LIVE))
        film_sub = link_sub(LinkRow(FILM_ID, "Lecture recording", FILM, failed=ACCESS_ENDED))
        self.assertEqual([(row.sub, row.warn) for row in rows[:2]], [live_sub, film_sub])
        failed = "Didn't open last time (401: the link's access ran out)"  # (the mockup's)
        self.assertEqual(film_sub, (failed, True))
        self.assertTrue(rows[2].help)
        # A film that played says where it stopped, as the channel list does.
        self.controller.stops[FILM_ID] = 2_472_000
        self.controller.linkPlayed.emit(film, FILM, FILM_URL)
        film_row = {row.key: row for row in self.shown()}[link_key(FILM_ID)]
        self.assertEqual((film_row.sub, film_row.warn), ("Film · stopped at 41:12", False))
        # ＋ Open a link… opens File → Open Link…'s card (over Settings, which stays).
        self.move_to(OPEN_LINK)
        self.key(Action.NAV_ACTIVATE)
        self.window.open_link.assert_called_once_with()
        self.assertTrue(self.settings.visible())

    # ---- a link's own rows --------------------------------------------------------------------

    def test_rename_and_replace_address_keep_the_id(self):
        self.seed((LIVE_URL, "Harbour News", LIVE, NOW, ACCESS_ENDED))
        self.into(LIVE_ID)
        self.assertEqual(
            [(row.key, row.kind) for row in self.shown()],
            [
                (row_key(LIVE_ID, PLAY), ACTION),
                (row_key(LIVE_ID, NAME), TEXT),
                (row_key(LIVE_ID, ADDRESS), TEXT),
                (row_key(LIVE_ID, KIND), INFO),
                (row_key(LIVE_ID, OPENED), INFO),
                (row_key(LIVE_ID, REMOVE), DANGER),
            ],
        )
        self.assertEqual(self.drawer.focused_row().key, row_key(LIVE_ID, PLAY))  # Play first
        self.assertEqual(self.titles()[0], "Play")
        self.assertEqual(self.titles()[-1], "Remove…")
        for row in self.shown():
            if row.kind != INFO:
                self.assertTrue(row.help, row.key)
                self.assertLessEqual(len(row.help), 90, row.key)
        self.assertEqual(self.value(LIVE_ID, NAME), "Harbour News")
        self.assertEqual(self.value(LIVE_ID, ADDRESS), LIVE_URL)  # as kept
        self.assertEqual(self.value(LIVE_ID, KIND), "Live")
        self.assertEqual(self.value(LIVE_ID, OPENED), short_when(NOW))
        # Renamed: the same link, written on a worker, and the channel list follows.
        self.move_to(row_key(LIVE_ID, NAME))
        self.type_text("Harbour Live")
        self.assertEqual(self.store.get(LIVE_ID).name, "Harbour Live")
        self.assertTrue(self.changes)
        self.assertEqual(self.drawer.path(), (PROVIDERS, LINKS, link_key(LIVE_ID)))
        self.assertEqual(self.drawer.focused_row().key, row_key(LIVE_ID, NAME))
        self.assertEqual(self.on_disk()[LIVE_ID]["name"], "Harbour Live")
        crumbs = [text for text, _rect in self.drawer._layout().crumbs]
        self.assertEqual(crumbs[-1], "Harbour Live")
        # A blank name: one made of its address, as a provider's.
        self.type_text("   ")
        self.assertEqual(self.store.get(LIVE_ID).name, default_name(LIVE_URL))
        # The same stream with a fresh token: kept at once, the same id; why it failed (the
        # old token's) goes.
        self.move_to(row_key(LIVE_ID, ADDRESS))
        changes = len(self.changes)
        self.type_text(FRESH)
        self.assertFalse(self.settings.card_shown())  # nothing asked
        self.assertEqual([e.id for e in self.store.entries()], [LIVE_ID])
        entry = self.store.get(LIVE_ID)
        self.assertEqual((entry.url, entry.failed, entry.opened), (FRESH, "", NOW))
        self.assertGreater(len(self.changes), changes)
        self.assertEqual(self.value(LIVE_ID, ADDRESS), FRESH)
        self.assertEqual(self.on_disk()[LIVE_ID]["url"], FRESH)
        self.assertEqual(self.drawer.path(), (PROVIDERS, LINKS, link_key(LIVE_ID)))
        # The same address again, or none: nothing changes.
        changes = len(self.changes)
        self.type_text(FRESH)
        self.type_text("  ")
        self.assertEqual(len(self.changes), changes)
        self.assertEqual(self.store.get(LIVE_ID).url, FRESH)

    def test_a_different_stream_address_asks(self):
        self.seed((LIVE_URL, "Harbour News", LIVE, NOW, ""))
        self.into(LIVE_ID)
        self.move_to(row_key(LIVE_ID, ADDRESS))
        # Another stream: asked first, Keep as a new link the default; Cancel changes nothing.
        self.type_text(OTHER)
        question = self.links.question
        self.assertTrue(question.isVisible())
        self.assertTrue(self.settings.card_shown())  # it has the keys
        self.assertEqual(question.title(), "This is another stream")
        self.assertIn("Harbour News", question.body())
        self.assertNotIn("cdn2.example/live", question.body())  # never the address
        self.assertEqual(
            [(b.key, b.text) for b in question._buttons],
            [(CANCEL, "Cancel"), (REPLACE, "Replace"), (KEEP_NEW, "Keep as a new link")],
        )
        self.assertEqual(question.focus(), ("button", KEEP_NEW))
        self.key(Action.ESCAPE)
        self.assertFalse(question.isVisible())
        self.assertEqual([(e.id, e.url) for e in self.store.entries()], [(LIVE_ID, LIVE_URL)])
        # Replace: the link takes the address under its id, its name and place kept.
        self.type_text(OTHER)
        self.key(Action.NAV_LEFT)
        self.assertEqual(question.focus(), ("button", REPLACE))
        self.key(Action.NAV_ACTIVATE)
        self.assertFalse(question.isVisible())
        entry = self.store.get(LIVE_ID)
        self.assertEqual((entry.url, entry.name), (OTHER, "Harbour News"))
        self.assertEqual(len(self.store.entries()), 1)
        self.assertEqual(self.on_disk()[LIVE_ID]["url"], OTHER)
        self.assertEqual(self.drawer.path(), (PROVIDERS, LINKS, link_key(LIVE_ID)))
        # Its new stream with a fresh token is its own now: kept at once, nothing asked, and
        # Open Link plays it as this link (no second entry).
        self.type_text(OTHER_FRESH)
        self.assertFalse(self.settings.card_shown())
        self.assertEqual(self.store.get(LIVE_ID).url, OTHER_FRESH)
        self.player.play(OTHER, LIVE)
        played, section = self.controller.played[-1]
        self.assertEqual((played["id"], played["cmd"], section), (LIVE_ID, OTHER, "live"))
        self.controller.linkPlayed.emit(played, LIVE, OTHER)
        self.assertEqual([e.id for e in self.store.entries()], [LIVE_ID])
        # Keep as a new link: a link of its own (its kind as its address says), this one as
        # it was; the keys stay here.
        self.type_text(NEW_FILM)
        self.assertTrue(question.isVisible())
        self.key(Action.NAV_ACTIVATE)  # the default
        new = self.store.get(stream_id(NEW_FILM))
        self.assertIsNotNone(new)
        self.assertEqual((new.url, new.kind), (NEW_FILM, FILM))
        self.assertEqual(new.name, default_name(NEW_FILM))
        self.assertEqual(self.store.get(LIVE_ID).url, OTHER)
        self.assertEqual(set(self.on_disk()), {LIVE_ID, new.id})
        self.assertEqual(self.drawer.path(), (PROVIDERS, LINKS, link_key(LIVE_ID)))
        # No stream's link: said, and nothing changes (nothing asked either).
        for text, said in ((ACCOUNT, WHOLE_TEXT), ("not a link at all", NOT_A_LINK_TEXT)):
            self.window.toasts.clear()
            self.type_text(text)
            self.assertFalse(self.settings.card_shown())
            self.assertEqual(self.window.toasts, [(translate(said), "error")])
            self.assertEqual(self.store.get(LIVE_ID).url, OTHER)
            self.assertEqual(len(self.store.entries()), 2)

    def test_replace_forgets_where_a_film_stopped_and_a_fresh_token_keeps_it(self):
        self.seed((FILM_URL, "Lecture recording", FILM, NOW, ""))
        self.controller.stops[FILM_ID] = 2_472_000
        self.into(FILM_ID)
        self.move_to(row_key(FILM_ID, ADDRESS))
        # Its own film with a fresh token: where it stopped stays.
        self.type_text(FILM_FRESH)
        self.assertFalse(self.settings.card_shown())
        self.assertEqual(self.controller.forgotten, [])
        self.assertEqual(self.player.rows()[0].stopped_ms, 2_472_000)
        # Another film in its place (Replace): the same link, from that film's beginning.
        self.type_text(NEW_FILM)
        self.key(Action.NAV_LEFT)
        self.assertEqual(self.links.question.focus(), ("button", REPLACE))
        self.key(Action.NAV_ACTIVATE)
        self.assertEqual(self.controller.forgotten, [FILM_ID])
        entry = self.store.get(FILM_ID)
        self.assertEqual((entry.url, entry.name), (NEW_FILM, "Lecture recording"))
        self.assertIsNone(self.player.rows()[0].stopped_ms)
        listed = {row.key: row for row in self.links.row().section.rows}  # Network streams
        self.assertEqual(listed[link_key(FILM_ID)].sub, "Film")  # its row says so too
        # Back to its first film: another stream than the one it plays now, asked again, and
        # where the film between stopped is forgotten too (review M1).
        self.controller.stops[FILM_ID] = 900_000
        self.type_text(FILM_URL)
        self.assertTrue(self.links.question.isVisible())
        self.key(Action.NAV_LEFT)
        self.key(Action.NAV_ACTIVATE)  # Replace
        self.assertEqual(self.controller.forgotten, [FILM_ID, FILM_ID])
        self.assertEqual(self.store.get(FILM_ID).url, FILM_URL)

    def test_remove_asks_and_saves_off_the_gui_thread(self):
        self.seed(
            (LIVE_URL, "Harbour News", LIVE, NOW, ""),
            (FILM_URL, "Lecture recording", FILM, NOW - 60, ""),
        )
        threads = []
        real = ns.save

        def spy(*args, **kwargs):
            threads.append(threading.current_thread())
            return real(*args, **kwargs)

        with mock.patch.object(ns, "save", spy):
            self.into(LIVE_ID)
            self.move_to(row_key(LIVE_ID, REMOVE))
            self.key(Action.NAV_ACTIVATE)
            question = self.links.question
            self.assertTrue(question.isVisible())
            self.assertEqual(question.title(), "Remove “Harbour News” from Network streams?")
            self.assertEqual(
                [(b.key, b.danger) for b in question._buttons], [(CANCEL, False), (REMOVE, True)]
            )
            self.assertEqual(question.focus(), ("button", CANCEL))  # Cancel first
            self.key(Action.NAV_ACTIVATE)  # Cancel
            self.assertIsNotNone(self.store.get(LIVE_ID))
            self.assertEqual(self.drawer.path(), (PROVIDERS, LINKS, link_key(LIVE_ID)))
            # Asked again, and Remove: gone, written on a worker, the list follows; the keys
            # on the link that took its place in Network streams.
            self.key(Action.NAV_ACTIVATE)
            self.key(Action.NAV_RIGHT)
            self.key(Action.NAV_ACTIVATE)
            self.assertIsNone(self.store.get(LIVE_ID))
            self.assertTrue(self.changes)
            self.assertEqual(self.drawer.path(), (PROVIDERS, LINKS))
            self.assertEqual(self.drawer.focused_row().key, link_key(FILM_ID))
            self.assertEqual(self.shown()[0].key, link_key(FILM_ID))
            self.assertEqual(set(self.on_disk()), {FILM_ID})
            self.assertTrue(threads)
            self.assertTrue(all(t is not threading.main_thread() for t in threads))
            # Still playing, it fails afterwards: it is not kept again.
            item = item_of(LIVE_URL, "Harbour News")
            self.controller.linkFailed.emit(item, LIVE, LIVE_URL, ACCESS_ENDED)
            self.controller.linkPlayed.emit(item, LIVE, LIVE_URL)
            self.assertIsNone(self.store.get(LIVE_ID))
            # The last link removed: Network streams goes from Providers, the keys on the row
            # that took its place.
            self.key(Action.NAV_ACTIVATE)  # into the film's rows
            self.move_to(row_key(FILM_ID, REMOVE))
            self.key(Action.NAV_ACTIVATE)
            self.key(Action.NAV_RIGHT)
            self.key(Action.NAV_ACTIVATE)
            self.assertEqual(self.store.entries(), [])
            self.assertEqual(self.drawer.path(), (PROVIDERS,))
            self.assertNotIn(LINKS, [row.key for row in self.shown()])
            self.assertEqual(self.drawer.focused_row().key, ADD_PROVIDER)
            self.assertEqual(self.on_disk(), {})
        self.assertTrue(all(t is not threading.main_thread() for t in threads))
        # Opened again with Open Link, it is kept again once it plays.
        self.player.play(LIVE_URL, LIVE)
        self.controller.linkPlayed.emit(self.controller.played[-1][0], LIVE, LIVE_URL)
        self.assertIsNotNone(self.store.get(LIVE_ID))


class PlayRowTests(WindowCase):
    """The real window: a kept link's Play plays it through the network player, as the
    channel list's row does, and Settings closes."""

    def tearDown(self):
        elapse(QApplication.doubleClickInterval() + 80)  # no second-click guard left armed
        super().tearDown()

    def keep_a_link(self):
        player = self.window.network_player
        player.start()
        self.assertTrue(wait_until(player.loaded))
        player.keep_new(LIVE_URL, LIVE)
        player.rename(LIVE_ID, "Harbour News")
        self.assertTrue(wait_until(lambda: self.window.jobs.pending() == 0))

    def press(self, key, times=1):
        for _ in range(times):
            QTest.keyClick(self.window, key)

    def to_row(self, key):
        drawer = self.window.settings_drawer
        for _ in range(len(drawer._right_rows()) + 1):
            row = drawer.focused_row()
            if row is not None and row.key == key:
                return
            self.press(Qt.Key.Key_Down)
        self.fail(f"no row {key}")

    def start(self):
        """The window shown and settled (the channel list open on the fixture provider), a
        link kept."""
        self.show()
        self.wait_for_drawer()
        self.keep_a_link()

    def to_play(self):
        """Settings from the menu bar, by the keys into the link's own rows: on its Play."""
        window = self.window
        self.menu_actions()["Settings…"].trigger()
        self.assertTrue(window.settings.visible())
        drawer = window.settings_drawer
        self.assertEqual(drawer.path(), (PROVIDERS,))
        self.press(Qt.Key.Key_Right)
        self.to_row(LINKS)
        self.press(Qt.Key.Key_Return)
        self.to_row(link_key(LIVE_ID))
        self.press(Qt.Key.Key_Return)
        self.assertEqual(drawer.path(), (PROVIDERS, LINKS, link_key(LIVE_ID)))
        self.assertEqual(drawer.focused_row().key, row_key(LIVE_ID, PLAY))  # the first row
        return drawer

    def test_play_plays_the_link_and_closes_settings(self):
        self.start()
        window = self.window
        self.to_play()
        with mock.patch.object(
            window.controller, "play_link", wraps=window.controller.play_link
        ) as play:
            self.press(Qt.Key.Key_Return)
        play.assert_called_once()
        item, section = play.call_args.args
        self.assertEqual(
            (item["id"], item["name"], item["cmd"], item["_provider"], section),
            (LIVE_ID, "Harbour News", LIVE_URL, NETWORK_PROVIDER, "live"),
        )
        self.assertFalse(window.settings.visible())
        self.assertFalse(window.overlays.drawer.isVisible())  # nor is the list given back
        self.assertTrue(wait_until(lambda: window.controller.now_playing() is not None))
        self.assertEqual(window.controller.now_playing().item["id"], LIVE_ID)

    def test_a_double_click_on_play_plays_once_and_never_reaches_the_video(self):
        # Settings closes on the first press: the second would land on what is under it (the
        # video, where a double-click is fullscreen; the welcome card's Add provider).
        self.start()
        window = self.window
        drawer = self.to_play()
        index = [row.key for row in drawer._right_rows()].index(row_key(LIVE_ID, PLAY))
        host = drawer.window()  # the window the overlays are on
        point = drawer.mapTo(host, drawer.title_rect(index).center().toPoint())  # its line
        with (
            mock.patch.object(window.controller, "play_link") as play,
            mock.patch.object(window, "toggle_fullscreen") as fullscreen,
        ):
            QTest.mouseDClick(host.windowHandle(), Qt.MouseButton.LeftButton, pos=point)
            elapse(QApplication.doubleClickInterval() + 80)
        play.assert_called_once()
        fullscreen.assert_not_called()
        self.assertFalse(window.isFullScreen())
        self.assertFalse(window.settings.visible())  # nothing opened it again

    def plays_quietly(self):
        """Without libVLC every open fails at once: here each opens and plays on."""
        self.window.session.play = lambda url, **kwargs: None

    def test_what_waits_for_the_tv_while_a_link_plays(self):
        # With a provider a link plays as a channel does: nothing is held for it.
        self.start()
        self.plays_quietly()
        self.to_play()
        self.press(Qt.Key.Key_Return)
        self.assertTrue(wait_until(lambda: self.window.controller.now_playing() is not None))
        runs = []
        self.window.when_idle(lambda: runs.append(True))
        self.assertEqual(runs, [True])  # at once


class NoProviderPlayRowTests(PlayRowTests):
    """With no provider the channel list never opens (the welcome card shows): Settings is
    where the kept links play from, and a link that plays holds the welcome card off as a
    file opened in QiTV does, until it stops."""

    def provider_list(self):
        return []

    def start(self):
        self.show()
        self.assertTrue(wait_until(self.window.overlays.welcome.isVisible))
        self.keep_a_link()

    def test_what_waits_for_the_tv_while_a_link_plays(self):
        self.start()
        self.plays_quietly()
        window, welcome = self.window, self.window.overlays.welcome
        self.to_play()
        self.press(Qt.Key.Key_Return)  # Play
        self.assertFalse(window.settings.visible())
        self.assertTrue(wait_until(lambda: window.controller.now_playing() is not None))
        runs = []
        window.when_idle(lambda: runs.append(True))  # (the startup update question)
        elapse(300)
        self.assertFalse(welcome.isVisible())  # not over the link
        self.assertEqual(runs, [])
        window.controller.stop()
        self.assertTrue(wait_until(welcome.isVisible))  # stopped: as after a file
        self.assertEqual(runs, [True])

    def test_a_subtitle_or_a_gone_file_dropped_meanwhile_keeps_the_card_away(self):
        # Review I1: the window lets a file's hold go when no file plays; the link held for
        # plays on, and keeps it until it stops.
        self.start()
        self.plays_quietly()
        window, welcome = self.window, self.window.overlays.welcome
        self.to_play()
        self.press(Qt.Key.Key_Return)  # Play
        self.assertTrue(wait_until(lambda: window.controller.now_playing() is not None))
        runs = []
        window.when_idle(lambda: runs.append(True))
        folder = Path(self.config_dir)
        subtitles = folder / "harbour.srt"
        subtitles.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n", encoding="utf-8")
        window.open_paths([str(subtitles)], source=DROP)  # subtitles alone: for what plays
        elapse(300)
        self.assertFalse(welcome.isVisible())
        self.assertEqual(runs, [])
        window.open_paths([str(folder / "gone.mkv")], source=DROP)  # a film no longer there
        self.assertTrue(wait_until(lambda: any("gone.mkv" in text for text, _ in self.toasts)))
        elapse(300)
        self.assertFalse(welcome.isVisible())
        self.assertEqual(runs, [])
        self.assertEqual(window.controller.now_playing().item["id"], LIVE_ID)  # it plays on
        window.controller.stop()
        self.assertTrue(wait_until(welcome.isVisible))
        self.assertEqual(runs, [True])

    def test_the_open_link_cards_play_holds_the_welcome_card_off_too(self):
        self.start()
        self.plays_quietly()
        window, welcome = self.window, self.window.overlays.welcome
        window.open_link(RTMP)
        self.assertTrue(window.open_link_card.isVisible())
        QTest.keyClick(window, Qt.Key.Key_Return)  # Play
        self.assertTrue(window.open_link_card.isHidden())
        self.assertTrue(wait_until(lambda: window.controller.now_playing() is not None))
        self.assertEqual(window.controller.now_playing().item["cmd"], RTMP)
        elapse(300)
        self.assertFalse(welcome.isVisible())
        window.controller.stop()
        self.assertTrue(wait_until(welcome.isVisible))
        # Played again from the Stop's pill: held again, as its first play was.
        window.controller.play_again()
        self.assertTrue(wait_until(lambda: window.controller.now_playing() is not None))
        elapse(300)
        self.assertFalse(welcome.isVisible())


if __name__ == "__main__":
    unittest.main()
