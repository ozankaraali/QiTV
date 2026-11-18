"""The Library's Folders tab (Plan 4, Task 12): the shelf of the folders the viewer opened
(ui.library.folders_view). It shows at once with their names, then each tile's line and cover
are read on a worker (services.local_playlist.read_folder, no tags): "S2 · E5 next" for a
series, "Album · 12 songs", "8 videos"; a folder that is gone shows as missing with Remove. A
click plays the folder from where it stopped (Task 11's start rule, through the router); Clear
asks first, on the app's question card. The tab is always there (2.11.1): empty, it has its
Add folder tile and a line that says what it is for. Fictional folders in a temporary place."""

import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget
import test_library_controller as library_tests  # its fakes (not its tests: no class here)
from test_open_router import ListCase, series
from timer_fixtures import elapse, wait_until

from services.folder_shelf import ALBUM, SERIES, VIDEOS, FolderShelf
from services.local_playlist import read_folder
from services.navigation import OPEN_FOLDER_ROW
from services.watch_state import WatchState, local_key
from tests.test_provider_drop import drop
from ui.keymap import Action
from ui.library import folders_view
from ui.library.folders_view import FoldersDesk, FoldersView
from ui.overlays.question_card import QuestionCard

app = QApplication.instance() or QApplication([])

SEEN = {"position": 1_150_000, "duration": 1_200_000}  # past 90%: seen to its end


def make_series(folder, season=2, count=6):
    """A season of a fictional show: its episodes in order (sizes 1, 2, …)."""
    folder.mkdir(parents=True, exist_ok=True)
    episodes = []
    for k in range(1, count + 1):
        path = folder / f"Lantern Coast S{season:02d}E{k:02d}.mkv"
        path.write_bytes(b"x" * k)
        episodes.append(path)
    return episodes


def make_album(folder, count=12, cover=True):
    folder.mkdir(parents=True, exist_ok=True)
    for k in range(1, count + 1):
        (folder / f"{k:02d} Tide {k}.mp3").write_bytes(b"song")
    if cover:
        image = QImage(64, 64, QImage.Format.Format_RGB32)
        image.fill(QColor("#336699"))
        image.save(str(folder / "Cover.JPG"), "JPG")


def make_videos(folder, count=8):
    folder.mkdir(parents=True, exist_ok=True)
    for k in range(1, count + 1):
        (folder / f"Beach day {k}.mp4").write_bytes(b"v")


class DeskCase(unittest.TestCase):
    """A FoldersView and its desk over a shelf kept in memory, on a host of the Library's
    size; the watch state's points are set as a played file leaves them."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.host = QWidget()
        self.host.resize(1280, 720)
        self.addCleanup(self.host.deleteLater)
        self.addCleanup(self.host.close)
        self.view = FoldersView(self.host)
        self.view.setGeometry(0, 60, 1280, 660)
        self.card = QuestionCard(self.host)
        self.config = mock.Mock(playback_positions={})
        self.watch = WatchState(self.config)
        self.shelf = FolderShelf(None)
        self.opened, self.asked = [], []
        self.desk = FoldersDesk(
            self.shelf,
            self.view,
            self.card,
            self.watch,
            open_folder=self.opened.append,
            add_folder=lambda: self.asked.append(True),
        )
        self.host.show()

    def seen(self, path, size, position=None):
        point = dict(SEEN) if position is None else {"position": position, "duration": 1_200_000}
        self.config.playback_positions[local_key(os.path.basename(path), size)] = point

    def add(self, folder, kind):
        self.shelf.add(str(folder), folder.name, kind)

    def lines(self):
        return [(tile.name, tile.line) for tile in self.view.tiles()]

    def wait_for_lines(self, count):
        def filled():
            tiles = self.view.tiles()
            return len(tiles) == count and all(tile.read for tile in tiles)

        self.assertTrue(wait_until(filled), self.lines())

    def click(self, rect):
        QTest.mouseClick(self.view, Qt.MouseButton.LeftButton, pos=rect.center().toPoint())


class FoldersViewTests(DeskCase):
    def test_a_tile_says_next_episode_album_or_count(self):
        season = self.root / "Lantern Coast"
        episodes = make_series(season)
        for path in episodes[:4]:
            self.seen(path, os.path.getsize(path))  # S2 E1–E4 seen: E5 is next
        make_album(self.root / "Brannow Fell")
        make_videos(self.root / "Tidewick Isle")
        self.add(self.root / "Tidewick Isle", VIDEOS)
        self.add(self.root / "Brannow Fell", ALBUM)
        self.add(season, SERIES)
        self.desk.show()
        self.wait_for_lines(3)
        self.assertEqual(
            self.lines(),
            [
                ("Lantern Coast", "S2 · E5 next"),
                ("Brannow Fell", "Album · 12 songs"),
                ("Tidewick Isle", "8 videos"),
            ],
        )
        tiles = self.view.tiles()
        self.assertIsNone(tiles[0].cover)  # no cover of its own: the drawn stand-in
        self.assertFalse(tiles[1].cover.isNull())  # Cover.JPG, in any case
        self.assertLessEqual(max(tiles[1].cover.width(), tiles[1].cover.height()), 64)
        # Part-way into E2 (rewatched): the part-way one is next, as the start rule says.
        self.seen(episodes[1], 2, position=300_000)
        self.desk.hide()
        self.desk.show()  # each time the tab opens, read again
        self.assertTrue(wait_until(lambda: self.lines()[0] == ("Lantern Coast", "S2 · E2 next")))

    def test_the_shelf_shows_at_once_and_its_lines_are_read_on_a_worker(self):
        make_videos(self.root / "Tidewick Isle")
        make_series(self.root / "Lantern Coast")
        self.add(self.root / "Tidewick Isle", VIDEOS)
        self.add(self.root / "Lantern Coast", SERIES)
        gate = threading.Event()
        reads = []

        def slow(path, **how):
            reads.append((threading.current_thread(), how))
            gate.wait(10)
            return read_folder(path, **how)

        with mock.patch.object(folders_view, "read_folder", side_effect=slow):
            self.desk.show()
            # Names and the kind kept on the shelf at once; nothing read yet.
            self.assertEqual(
                self.lines(), [("Lantern Coast", "Series"), ("Tidewick Isle", "Videos")]
            )
            self.assertTrue(all(not tile.read for tile in self.view.tiles()))
            gate.set()
            self.wait_for_lines(2)
        self.assertEqual(
            self.lines(), [("Lantern Coast", "S2 · E1 next"), ("Tidewick Isle", "8 videos")]
        )
        self.assertTrue(reads)
        for thread, how in reads:
            self.assertIsNot(thread, threading.main_thread())
            self.assertEqual(how.get("cap"), 0)  # no song's tags are read for a line

    def test_a_folder_opened_while_the_shelf_is_read_is_the_only_one_read_again(self):
        # M1: the shelf changing while its folders are read reads the new folder alone: the
        # ones still being read aren't read a second time on a thread of their own.
        names = ("Tidewick Isle", "Brannow Fell", "Lantern Coast")
        for name in names:
            make_videos(self.root / name, count=1)
        for name in names[:2]:
            self.add(self.root / name, VIDEOS)
        gate = threading.Event()
        reads: list[str] = []

        def slow(path, **how):
            reads.append(path)
            gate.wait(10)
            return read_folder(path, **how)

        def threads_done():
            return not any(t.name == "qitv-folder-shelf" for t in threading.enumerate())

        with mock.patch.object(folders_view, "read_folder", side_effect=slow):
            self.desk.show()
            self.add(self.root / names[2], VIDEOS)  # opened while the tab shows
            gate.set()
            self.wait_for_lines(3)
            self.assertTrue(wait_until(threads_done))
            self.assertEqual(sorted(reads), sorted(str(self.root / name) for name in names))
            # Left and opened again: every folder is read again, once.
            gate.clear()
            self.desk.hide()
            self.desk.show()
            gate.set()
            self.assertTrue(wait_until(lambda: len(reads) >= 6 and threads_done()))
        self.assertEqual(sorted(reads[3:]), sorted(str(self.root / name) for name in names))

    def test_a_cover_is_read_as_large_as_its_tile_shows_it(self):
        # M3+M4: a large cover is read at its tile's size on this screen (its short side
        # filling the tile, in the screen's pixels), and never past COVER_MAX_PX.
        folder = self.root / "Brannow Fell"
        make_album(folder, count=2, cover=False)
        image = QImage(1600, 1200, QImage.Format.Format_RGB32)
        image.fill(QColor("#336699"))
        image.save(str(folder / "Cover.JPG"), "JPG")
        self.add(folder, ALBUM)
        self.desk.show()
        self.wait_for_lines(1)
        cover = self.view.tiles()[0].cover
        side = self.view.cover_px()
        shown = self.view.tile_rect(0).width() * self.view.devicePixelRatioF()
        self.assertEqual(side, round(shown))
        self.assertEqual((cover.width(), cover.height()), (round(side * 4 / 3), side))
        look = folders_view.look_at(str(folder), cover_px=5000)  # a tile larger than the cap
        self.assertEqual(look.cover.height(), folders_view.COVER_MAX_PX)
        small = self.root / "Tidewick Isle"
        make_album(small, count=1)  # a 64 px cover: never made larger
        self.assertEqual(folders_view.look_at(str(small), cover_px=300).cover.width(), 64)

    def test_a_gone_folder_shows_missing_and_can_be_removed(self):
        make_videos(self.root / "Tidewick Isle")
        self.add(self.root / "Marrow Sound", VIDEOS)  # gone since
        self.add(self.root / "Tidewick Isle", VIDEOS)
        with mock.patch.object(QMessageBox, "exec", side_effect=AssertionError("a dialog")):
            self.desk.show()
            self.wait_for_lines(2)
            gone = self.view.tiles()[1]
            self.assertEqual(
                (gone.name, gone.line, gone.missing), ("Marrow Sound", "Missing", True)
            )
            self.assertFalse(self.view.tiles()[0].missing)
            remove = self.view.remove_rect(1)
            self.assertFalse(remove.isEmpty())  # the missing one offers Remove
            self.click(remove)
        self.assertEqual([entry["name"] for entry in self.shelf.folders()], ["Tidewick Isle"])
        self.assertEqual([tile.name for tile in self.view.tiles()], ["Tidewick Isle"])
        self.assertEqual(self.opened, [])  # nothing was played
        # × on a tile that is there: off the shelf too (the folder stays on the disk).
        self.click(self.view.remove_rect(0))
        self.assertEqual(self.shelf.folders(), [])
        self.assertTrue((self.root / "Tidewick Isle" / "Beach day 1.mp4").exists())

    def test_a_click_on_a_tile_plays_its_folder(self):
        make_videos(self.root / "Tidewick Isle")
        self.add(self.root / "Tidewick Isle", VIDEOS)
        self.desk.show()
        self.wait_for_lines(1)
        self.click(self.view.tile_rect(0))
        self.assertEqual(self.opened, [str(self.root / "Tidewick Isle")])
        self.click(self.view.add_rect())
        self.assertEqual(self.asked, [True])  # Add folder: Open Folder…

    def test_clear_asks_first(self):
        make_videos(self.root / "Tidewick Isle")
        self.add(self.root / "Tidewick Isle", VIDEOS)
        self.add(self.root / "Brannow Fell", ALBUM)
        with mock.patch.object(QMessageBox, "exec", side_effect=AssertionError("a dialog")):
            self.desk.show()
            self.click(self.view.clear_rect())
            self.assertTrue(self.card.isVisible())  # the app's question card, no dialog
            self.assertIn("Folders tab", self.card.question())
            self.assertEqual(len(self.shelf), 2)  # nothing went yet
            self.assertTrue(self.desk.answer_key(Action.ESCAPE))  # no: kept
            self.assertFalse(self.card.isVisible())
            self.assertEqual(len(self.shelf), 2)
            self.view.home()  # the keys: from the first tile up to Clear
            self.view.move_by(0, -1)
            self.assertEqual(self.view.current(), "clear")
            self.view.activate()
            self.assertTrue(self.card.isVisible())
            self.assertTrue(self.desk.answer_key(Action.NAV_RIGHT))  # to its yes
            self.assertTrue(self.desk.answer_key(Action.NAV_ACTIVATE))
        self.assertEqual(len(self.shelf), 0)
        self.assertFalse(self.desk.answer_key(Action.NAV_ACTIVATE))  # nothing asked now

    def test_leaving_the_tab_withdraws_the_question(self):
        self.add(self.root / "Tidewick Isle", VIDEOS)
        self.desk.show()
        self.click(self.view.clear_rect())
        self.assertTrue(self.card.isVisible())
        self.desk.hide()
        self.assertFalse(self.card.isVisible())
        elapse(50)
        self.assertEqual(len(self.shelf), 1)

    def test_the_tiles_keep_clear_of_the_corner_player(self):
        for k in range(8):
            folder = self.root / f"Tidewick Isle {k}"
            make_videos(folder, count=1)
            self.add(folder, VIDEOS)
        self.desk.show()
        wide = [self.view.tile_rect(i).right() for i in range(8)]
        corner = QWidget(self.host)  # the Library's corner player, at its bottom right
        corner.setGeometry(1280 - 400, 720 - 230, 400, 230)
        self.view.avoid(corner)
        corner.show()
        self.assertLess(max(self.view.tile_rect(i).right() for i in range(8)), corner.x())
        self.assertLess(self.view.clear_rect().right(), corner.x())
        corner.hide()
        self.assertEqual([self.view.tile_rect(i).right() for i in range(8)], wide)

    def test_the_keys_move_over_the_tiles_and_remove_one(self):
        for name in ("Tidewick Isle", "Brannow Fell", "Lantern Coast"):
            make_videos(self.root / name, count=1)
            self.add(self.root / name, VIDEOS)
        self.desk.show()
        self.assertEqual(self.view.current(), str(self.root / "Lantern Coast"))  # the newest
        self.view.move_by(1, 0)
        self.view.activate()
        self.assertEqual(self.opened, [str(self.root / "Brannow Fell")])
        self.view.remove_current()  # Delete
        self.assertEqual(
            [tile.name for tile in self.view.tiles()], ["Lantern Coast", "Tidewick Isle"]
        )
        self.assertEqual(self.view.current(), str(self.root / "Tidewick Isle"))  # its neighbour
        self.view.end()
        self.assertEqual(self.view.current(), "add")
        self.view.activate()
        self.assertEqual(self.asked, [True])
        ups = []
        self.view.upEdge.connect(lambda: ups.append(True))
        self.view.move_by(0, -1)
        self.assertEqual(self.view.current(), "clear")
        self.view.move_by(0, -1)
        self.assertEqual(ups, [True])  # on to the top bar

    def test_an_empty_tab_has_add_folder_and_says_what_it_is_for(self):
        # 2.11.1, the controller's ruling: the tab is always there; empty, its Add folder tile
        # and a line that says what the tab is for (no Clear, no count)
        empty = "Folders you open stay here, to play again with a click."
        empty += " Videos and episodes go on from where you stopped."
        self.desk.show()
        self.assertEqual(self.view.summary(), empty)
        self.assertEqual(self.view.tiles(), [])
        controls = self.view.controls()
        self.assertFalse(controls["add"].isEmpty())
        self.assertTrue(controls["clear"].isEmpty())
        self.assertTrue(controls["tiles"].isEmpty())
        self.assertEqual(self.view.current(), "add")
        self.assertEqual(self.view.accessibleDescription(), f"Add folder. {empty}")
        self.view.activate()  # Enter on it, as a click
        self.click(self.view.add_rect())
        self.assertEqual(self.asked, [True, True])
        ups = []
        self.view.upEdge.connect(lambda: ups.append(True))
        self.view.move_by(0, -1)  # no Clear to go to: the top bar
        self.assertEqual(ups, [True])
        self.add(self.root / "Tidewick Isle", VIDEOS)  # a folder opened: the tab's own line
        self.assertEqual(
            self.view.summary(),
            "The folders you opened. Videos and episodes play on from where you stopped.",
        )
        self.shelf.clear()
        self.assertEqual((self.view.summary(), self.view.current()), (empty, "add"))


class FoldersTabTests(unittest.TestCase):
    """The Library's tab (LibraryController): always there (2.11.1, the controller's ruling:
    before, it showed only while the shelf had a folder), after Recordings."""

    def setUp(self):
        library_tests.LibraryControllerTests.setUp(self)  # its fakes, not its tests
        self.ctl = library_tests.LibraryControllerTests.build(self)
        self.shelf = FolderShelf(None)
        self.card = QuestionCard(self.screen)
        self.opened = []
        self.desk = FoldersDesk(
            self.shelf,
            self.screen.folders,
            self.card,
            self.watch,
            open_folder=self.opened.append,
            add_folder=lambda: None,
        )
        self.ctl.set_folders(self.desk)

    def tabs(self):
        return list(self.screen.nav._tabs)

    def test_the_tab_is_there_with_an_empty_shelf_and_stays_when_its_last_folder_goes(self):
        self.assertEqual(self.tabs()[-1], "folders")  # nothing opened yet
        self.ctl.open("movies")
        self.screen.nav.tabRequested.emit("folders")  # its tab, clicked
        self.assertEqual((self.ctl.mode, self.ctl.zone), ("folders", "folders"))
        self.assertTrue(self.screen.folders.isVisible())
        self.assertFalse(self.screen.grid.isVisible())
        self.assertEqual(self.screen.folders.current(), "add")  # its Add folder tile
        self.assertNotIn("source", self.screen.nav._order())  # every provider's, as Recordings
        self.assertEqual(self.ctl.section, "movies")  # never a section of the providers'
        self.shelf.add("/media/Tidewick Isle", "Tidewick Isle", VIDEOS)
        self.assertEqual(len(self.screen.folders.tiles()), 1)
        self.assertEqual(self.tabs()[-1], "folders")
        # The last one removed: the tab stays, empty, its Add folder tile in the keys' focus.
        self.screen.folders.home()
        self.ctl.handle(Action.NAV_DELETE)
        self.assertEqual(self.tabs()[-1], "folders")
        self.assertEqual((self.ctl.mode, self.screen.nav._active), ("folders", "folders"))
        self.assertTrue(self.screen.folders.isVisible())
        self.assertEqual((self.screen.folders.tiles(), self.screen.folders.current()), ([], "add"))

    def test_after_clear_the_empty_tab_stays(self):
        self.ctl.set_recordings_desk(library_tests.FakeDesk())
        self.assertEqual(self.tabs()[-2:], ["recordings", "folders"])  # after Recordings
        self.shelf.add("/media/Tidewick Isle", "Tidewick Isle", VIDEOS)
        self.ctl.open("folders")
        self.screen.folders.clearRequested.emit()
        for action in (Action.NAV_RIGHT, Action.NAV_ACTIVATE):  # the card has the keys
            self.assertTrue(self.ctl.handle(action))
        self.assertEqual(len(self.shelf), 0)
        self.assertEqual(self.tabs()[-1], "folders")
        self.assertEqual((self.ctl.mode, self.screen.nav._active), ("folders", "folders"))
        self.assertEqual(self.screen.folders.current(), "add")

    def test_an_empty_tab_opens_and_a_search_from_it_comes_back(self):
        self.ctl.open("folders")
        self.assertEqual((self.ctl.mode, self.ctl.zone), ("folders", "folders"))
        self.assertTrue(self.desk.shown())
        self.ctl.handle(Action.NAV_TEXT, "t")  # typing searches, as on every tab
        self.assertEqual(self.ctl.mode, "search")
        self.ctl.handle(Action.NAV_BACKSPACE)
        self.ctl.handle(Action.ESCAPE)
        self.assertEqual(self.ctl.mode, "folders")  # back where the search came from

    def test_the_tab_keeps_its_place_as_providers_come_and_go(self):
        self.shelf.add("/media/Tidewick Isle", "Tidewick Isle", VIDEOS)
        self.ctl.open("folders")
        self.catalog.providerReady.emit("P")
        self.assertEqual((self.ctl.mode, self.screen.nav._active), ("folders", "folders"))
        self.ctl.handle(Action.NAV_UP)  # the top bar, then back down to the shelf
        self.ctl.handle(Action.NAV_UP)
        self.assertEqual(self.ctl.zone, "nav")
        self.ctl.handle(Action.NAV_DOWN)
        self.assertEqual((self.ctl.zone, self.screen.folders.current()), ("folders", "clear"))
        self.ctl.handle(Action.NAV_DOWN)
        self.ctl.handle(Action.NAV_ACTIVATE)
        self.assertEqual(self.opened, ["/media/Tidewick Isle"])
        self.ctl.handle(Action.NAV_TEXT, "t")  # typing searches, as on every tab
        self.assertEqual(self.ctl.mode, "search")
        self.ctl.handle(Action.NAV_BACKSPACE)
        self.ctl.handle(Action.ESCAPE)
        self.assertEqual(self.ctl.mode, "folders")  # back where the search came from


class ShelfWindowTests(ListCase):
    """In the window: Open Folder… and a tile's click take the dropped folder's path."""

    def setUp(self):
        super().setUp()
        self.season = self.folder / "Lantern Coast"
        self.episodes = series(self.season)

    def test_a_click_plays_from_where_it_stopped(self):
        self.show()  # the Library's tiles take the clicks
        drop(self.window, paths=[self.season])  # on the shelf now
        self.assertTrue(wait_until(lambda: self.lists()), self.calls)
        first, second, third = self.episodes
        watch = self.window.watch
        watch.update_position(local_key(first.name, 1), 1_150_000, 1_200_000)  # seen
        watch.update_position(local_key(second.name, 2), 600_000, 1_200_000)  # part-way
        self.window.screens.open_library("folders")
        library = self.window.library_controller
        self.assertEqual(library.mode, "folders")
        view = self.window.library_page.folders
        self.assertTrue(wait_until(lambda: view.tiles() and view.tiles()[0].read))
        self.assertEqual(view.tiles()[0].line, "S1 · E2 next")
        QTest.mouseClick(view, Qt.MouseButton.LeftButton, pos=view.tile_rect(0).center().toPoint())
        self.assertTrue(wait_until(lambda: len(self.lists()) == 2), self.calls)
        self.assertEqual(self.starts, [None, 1])  # the part-way one, the whole list kept
        self.assertEqual(self.current.item["cmd"], second.absolute().as_uri())
        # Seen to its end too: the one after it next.
        watch.update_position(local_key(second.name, 2), 1_190_000, 1_200_000)
        self.window.screens.open_library("folders")
        self.assertTrue(wait_until(lambda: view.tiles()[0].line == "S1 · E3 next"))
        QTest.mouseClick(view, Qt.MouseButton.LeftButton, pos=view.tile_rect(0).center().toPoint())
        self.assertTrue(wait_until(lambda: len(self.lists()) == 3), self.calls)
        self.assertEqual(self.starts[-1], 2)

    def test_open_folder_picks_a_folder_and_plays_it(self):
        menu = [a for a in self.window.menuBar().actions() if a.text() == "File"][0].menu()
        entry = [a for a in menu.actions() if a.text() == "Open Folder…"][0]
        self.assertEqual(entry.shortcut().toString(), "Ctrl+Shift+O")
        with mock.patch(
            "ui.tv_pages.QFileDialog.getExistingDirectory", return_value=str(self.season)
        ) as pick:
            entry.trigger()
            self.assertTrue(wait_until(lambda: self.lists()), self.calls)
        pick.assert_called_once()
        self.assertEqual(self.lists()[0][0], "Lantern Coast")
        self.assertEqual([e["name"] for e in self.window.folder_shelf.folders()], ["Lantern Coast"])
        with mock.patch("ui.tv_pages.QFileDialog.getExistingDirectory", return_value=""):
            entry.trigger()  # cancelled: nothing
        elapse(300)
        self.assertEqual(len(self.lists()), 1)
        # P4-26: a folder chosen so plays as a dropped one: Esc doesn't bring the Library
        self.assertFalse(self.window.screens.escape_to_library())

    def test_the_tab_is_there_before_any_folder_and_its_add_folder_picks_one(self):
        """2.11.1, the controller's ruling: the Folders tab shows with an empty shelf."""
        self.show()
        screens, library = self.window.screens, self.window.library_controller
        screens.open_library("folders")
        self.assertEqual((screens.library_shown(), library.mode), (True, "folders"))
        view = self.window.library_page.folders
        self.assertTrue(view.isVisible())
        self.assertEqual(view.tiles(), [])
        with mock.patch(
            "ui.tv_pages.QFileDialog.getExistingDirectory", return_value=str(self.season)
        ) as pick:
            QTest.mouseClick(
                view, Qt.MouseButton.LeftButton, pos=view.add_rect().center().toPoint()
            )
            self.assertTrue(wait_until(lambda: self.lists()), self.calls)
        pick.assert_called_once()
        self.assertEqual([e["name"] for e in self.window.folder_shelf.folders()], ["Lantern Coast"])

    def test_add_folders_play_goes_back_to_the_folders_tab(self):
        """Ruling P4-29: the tab's Add folder is a start from the Library, as a tile is: Esc
        after its play goes back to the Folders tab."""
        self.show()
        drop(self.window, paths=[self.season])  # a folder on the shelf
        self.assertTrue(wait_until(lambda: self.lists()), self.calls)
        album = self.folder / "Brannow Fell"
        album.mkdir()
        for name in ("01 Tide.mp3", "02 Harbour.mp3"):
            (album / name).write_bytes(b"song")
        screens, library = self.window.screens, self.window.library_controller
        screens.open_library("folders")
        view = self.window.library_page.folders
        with mock.patch(
            "ui.tv_pages.QFileDialog.getExistingDirectory", return_value=str(album)
        ) as pick:
            QTest.mouseClick(
                view, Qt.MouseButton.LeftButton, pos=view.add_rect().center().toPoint()
            )
            self.assertTrue(wait_until(lambda: len(self.lists()) == 2), self.calls)
        pick.assert_called_once()
        self.assertEqual(self.lists()[-1][0], "Brannow Fell")
        self.assertEqual(self.window.folder_shelf.folders()[0]["name"], "Brannow Fell")
        self.assertTrue(screens.escape_to_library())
        self.assertEqual((screens.library_shown(), library.mode), (True, "folders"))

    def test_a_tiles_cover_is_read_at_the_size_the_tab_shows_it(self):
        """M3: the tab's first open in the window takes the cover's size from the tiles as
        they are laid out, not from a default width."""
        self.show()
        album = self.folder / "Brannow Fell"
        album.mkdir()
        for name in ("01 Tide.mp3", "02 Harbour.mp3"):
            (album / name).write_bytes(b"song")
        image = QImage(1600, 1200, QImage.Format.Format_RGB32)
        image.fill(QColor("#336699"))
        image.save(str(album / "Cover.JPG"), "JPG")
        drop(self.window, paths=[album])
        self.assertTrue(wait_until(lambda: self.lists()), self.calls)
        self.window.screens.open_library("folders")
        view = self.window.library_page.folders
        self.assertTrue(wait_until(lambda: view.tiles() and view.tiles()[0].read))
        cover = view.tiles()[0].cover
        self.assertNotEqual(view.width(), folders_view.DEFAULT_WIDTH)  # (a fallback would show)
        self.assertEqual(min(cover.width(), cover.height()), view.cover_px())

    def test_the_channel_lists_open_a_folder_row_asks_for_one(self):
        """P4-25: the channel list's row under Open file, as File → Open Folder…."""
        with mock.patch(
            "ui.tv_pages.QFileDialog.getExistingDirectory", return_value=str(self.season)
        ) as pick:
            self.window.overlays.drawer.actionRequested.emit(OPEN_FOLDER_ROW)
            self.assertTrue(wait_until(lambda: self.lists()), self.calls)
        pick.assert_called_once()
        self.assertEqual([e["name"] for e in self.window.folder_shelf.folders()], ["Lantern Coast"])
        self.assertFalse(self.window.screens.escape_to_library())  # as File → Open Folder…

    def click_tile(self, index=0):
        view = self.window.library_page.folders
        self.assertTrue(wait_until(lambda: len(view.tiles()) > index and view.tiles()[index].read))
        QTest.mouseClick(
            view, Qt.MouseButton.LeftButton, pos=view.tile_rect(index).center().toPoint()
        )

    def test_esc_after_a_tiles_play_goes_back_to_the_folders_tab(self):
        """P4-26: as a Recordings tile's recording goes back to Recordings; a drop doesn't."""
        self.show()
        drop(self.window, paths=[self.season])
        self.assertTrue(wait_until(lambda: self.lists()), self.calls)
        self.assertFalse(self.window.screens.escape_to_library())  # dropped: no Library
        self.window.screens.open_library("folders")
        self.click_tile()
        self.assertTrue(wait_until(lambda: len(self.lists()) == 2), self.calls)
        screens, library = self.window.screens, self.window.library_controller
        self.assertFalse(screens.library_shown())  # it plays large
        QTest.keyClick(self.window, Qt.Key.Key_Escape)
        self.assertTrue(screens.library_shown())
        self.assertEqual(
            (library.mode, self.window.library_page.nav._active), ("folders", "folders")
        )
        # A song folder from a tile: back to the Folders tab too (Esc, and the screen's ‹).
        album = self.folder / "Brannow Fell"
        album.mkdir()
        for name in ("01 Tide.mp3", "02 Harbour.mp3"):
            (album / name).write_bytes(b"song")
        drop(self.window, paths=[album])
        self.assertTrue(wait_until(lambda: len(self.lists()) == 3), self.calls)
        self.window.screens.open_library("folders")
        self.click_tile(0)  # the album, opened last
        self.assertTrue(wait_until(lambda: len(self.lists()) == 4), self.calls)
        self.assertEqual(self.lists()[-1][0], "Brannow Fell")
        self.assertTrue(screens.escape_to_library())
        self.assertEqual((screens.library_shown(), library.mode), (True, "folders"))
        drop(self.window, paths=[album])  # dropped again: Esc no longer goes back
        self.assertTrue(wait_until(lambda: len(self.lists()) == 5), self.calls)
        self.assertFalse(screens.escape_to_library())
        screens.library_from_sound()  # the audio screen's ‹: the Library as it was left
        self.assertEqual((screens.library_shown(), library.mode), (True, "folders"))


if __name__ == "__main__":
    unittest.main()
