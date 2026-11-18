"""The Folders shelf (Plan 4, Task 12): the folders the viewer opened as folders, kept newest
first in a small versioned file beside the config (services.folder_shelf), as TrackMemory keeps
its own. A drop, Open Folder… or a folder handed to QiTV adds one once it plays; a single file,
several files dropped together or a playlist never add theirs. Fictional folders in a
temporary place; the router's play_list is the fake test_open_router's ListCase gives."""

import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from test_open_router import ListCase, series
from timer_fixtures import elapse, wait_until

from services import channel_store
from services.folder_shelf import (
    ALBUM,
    FILE_NAME,
    MAX_ENTRIES,
    SERIES,
    VIDEOS,
    FolderShelf,
    folder_kind,
    shelf_key,
)
from services.local_playlist import start_index
from tests.test_provider_drop import drop

SETTLE_MS = 300


def clock(start=1_000.0):
    """A clock that moves a second each time it is read."""
    now = [start]

    def tick():
        now[0] += 1.0
        return now[0]

    return tick


class ShelfFileCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.path = str(self.dir / FILE_NAME)

    def shelf(self, **how):
        shelf = FolderShelf(self.path, now=clock(), **how)
        self.addCleanup(shelf.flush)
        return shelf

    def written(self, shelf):
        shelf.flush()
        with open(self.path, "rb") as f:
            return json.loads(f.read())


class FolderShelfTests(ShelfFileCase):
    def test_the_shelf_keeps_30(self):
        shelf = self.shelf()
        for k in range(MAX_ENTRIES + 5):
            shelf.add(f"/media/Shelf {k}", f"Shelf {k}", VIDEOS)
        names = [entry["name"] for entry in shelf.folders()]
        self.assertEqual(len(names), MAX_ENTRIES)
        self.assertEqual(names[0], f"Shelf {MAX_ENTRIES + 4}")  # the newest first
        self.assertEqual(names[-1], "Shelf 5")  # the five opened longest ago went
        data = self.written(shelf)
        self.assertEqual((data["version"], len(data["folders"])), (1, MAX_ENTRIES))
        again = self.shelf()
        self.assertEqual([entry["name"] for entry in again.folders()], names)

    def test_an_entry_holds_its_path_name_kind_time_and_last_entry(self):
        shelf = self.shelf()
        shelf.add("/media/Lantern Coast", "Lantern Coast", SERIES, last_entry="S01E02.mkv")
        shelf.played("/media/Lantern Coast/", "S01E03.mkv")  # the next entry opened
        (entry,) = shelf.folders()
        self.assertEqual(
            set(entry), {"path", "name", "kind", "opened_at", "last_entry"}, "nothing more"
        )
        self.assertEqual(
            (entry["path"], entry["name"], entry["kind"], entry["last_entry"]),
            ("/media/Lantern Coast", "Lantern Coast", SERIES, "S01E03.mkv"),
        )
        stored = self.written(shelf)["folders"]
        self.assertEqual(list(stored.values()), [entry])

    def test_older_files_and_a_broken_file_are_ignored(self):
        one = {
            "path": "/media/Tidewick Isle",
            "name": "Tidewick Isle",
            "kind": VIDEOS,
            "opened_at": 5.0,
            "last_entry": "",
        }
        for data in (
            b"{not json",
            b"[1, 2]",
            json.dumps({"version": 0, "folders": {"k": one}}).encode(),  # an older build's
            json.dumps({"version": "1", "folders": {"k": one}}).encode(),
            json.dumps({"version": 1, "channels": {"k": one}}).encode(),  # not the shelf's
        ):
            with self.subTest(data=data[:40]):
                Path(self.path).write_bytes(data)
                with self.assertLogs("services.folder_shelf", "INFO"):
                    self.assertEqual(self.shelf().folders(), [])
        # An entry that is not one is left out, the others are read.
        broken = [{**one, "path": 7}, {**one, "kind": "film"}, {**one, "opened_at": "x"}, "x"]
        good = {**one, "path": "/media/Brannow Fell", "name": "Brannow Fell", "kind": ALBUM}
        entries = {f"b{k}": value for k, value in enumerate(broken)}
        entries["good"] = good
        Path(self.path).write_text(json.dumps({"version": 1, "folders": entries}))
        self.assertEqual(self.shelf().folders(), [good])
        # A newer build's file is left as it is: the shelf of this run is kept in memory only.
        newer = json.dumps({"version": 2, "folders": {"k": one}, "more": True}).encode()
        Path(self.path).write_bytes(newer)
        with self.assertLogs("services.folder_shelf", "INFO"):
            shelf = self.shelf()
        shelf.add("/media/Lantern Coast", "Lantern Coast", SERIES)
        shelf.flush()
        self.assertEqual(Path(self.path).read_bytes(), newer)
        self.assertEqual([entry["name"] for entry in shelf.folders()], ["Lantern Coast"])

    def test_the_file_is_written_off_the_gui_thread(self):
        threads = []
        atomic_write = channel_store.atomic_write

        def spy(path):
            threads.append(threading.current_thread())
            return atomic_write(path)

        shelf = self.shelf()
        with mock.patch.object(channel_store, "atomic_write", side_effect=spy):
            shelf.add("/media/Lantern Coast", "Lantern Coast", SERIES)
            shelf.flush()
        self.assertEqual(len(threads), 1)
        self.assertIsNot(threads[0], threading.main_thread())

    def test_remove_and_clear_tell_who_follows(self):
        shelf = self.shelf()
        calls = []
        shelf.subscribe(lambda: calls.append(len(shelf)))
        shelf.add("/media/Lantern Coast", "Lantern Coast", SERIES)
        shelf.add("/media/Brannow Fell", "Brannow Fell", ALBUM)
        self.assertTrue(shelf.remove("/media/Lantern Coast/"))
        self.assertFalse(shelf.remove("/media/Lantern Coast"))  # gone already: nothing
        shelf.clear()
        self.assertEqual(calls, [1, 2, 1, 0])
        self.assertEqual(self.written(shelf)["folders"], {})

    def test_a_drives_root_keeps_its_separator_and_a_too_long_path_is_left_out(self):
        # M9: "E:" alone is that drive's current folder, not its root (a USB stick opened as
        # a folder); "/" is the root itself. A path past what is kept is not cut: left out.
        shelf = self.shelf()
        for path, kept in (("E:\\", "E:\\"), ("E:/", "E:/"), ("/", "/"), ("/media/A/", "/media/A")):
            with self.subTest(path=path):
                shelf.add(path, "USB", VIDEOS)
                self.assertEqual(shelf.folders()[0]["path"], kept)
        self.assertEqual(shelf_key("E:\\"), shelf_key("E:\\\\"))
        self.assertNotEqual(shelf_key("E:\\"), shelf_key("E:"))
        far = "/media/" + "x" * 1100
        with self.assertLogs("services.folder_shelf", "INFO"):
            shelf.add(far, "Far", VIDEOS)
        self.assertNotIn(far, shelf)
        self.assertNotIn(far[:1024], [entry["path"] for entry in shelf.folders()])
        stored = {"path": far, "name": "Far", "kind": VIDEOS, "opened_at": 5.0, "last_entry": ""}
        Path(self.path).write_text(json.dumps({"version": 1, "folders": {"k": stored}}))
        self.assertEqual(self.shelf().folders(), [])  # a cut path names another folder

    def test_a_folders_key_is_its_path_however_it_was_spelled(self):
        self.assertEqual(shelf_key("/media/Lantern Coast/"), shelf_key("/media/Lantern Coast"))
        self.assertEqual(shelf_key("/media/x/../Lantern Coast"), shelf_key("/media/Lantern Coast"))
        self.assertNotEqual(shelf_key("/media/Lantern Coast"), shelf_key("/media/Brannow Fell"))

    def test_a_folders_kind_is_what_it_plays_as(self):
        episodes = ["/m/Lantern Coast S01E01.mkv", "/m/Lantern Coast S01E02.mkv"]
        self.assertEqual(folder_kind(episodes), SERIES)
        self.assertEqual(folder_kind(["/m/Tide.mp3", "/m/Harbour.flac"]), ALBUM)
        self.assertEqual(folder_kind(["/m/Beach day.mp4", "/m/Harbour.mkv"]), VIDEOS)
        two_shows = ["/m/Lantern Coast S01E01.mkv", "/m/Brannow Fell S01E01.mkv"]
        self.assertEqual(folder_kind(two_shows), VIDEOS)


class StartRuleTests(unittest.TestCase):
    """Where a folder's list starts, and so what its tile says is next (ruling 3): the entry
    stopped part-way into, else the one after the last one seen to its end, else the first."""

    entries = ("/m/E1.mkv", "/m/E2.mkv", "/m/E3.mkv", "/m/E4.mkv")

    def start(self, part_way=(), finished=(), sizes=(1, 2, 3, 4)):
        return start_index(
            self.entries,
            sizes,
            part_way=lambda path, size: path in part_way,
            finished=lambda path, size: path in finished,
        )

    def test_the_part_way_one_first(self):
        self.assertEqual(self.start(part_way={"/m/E2.mkv", "/m/E3.mkv"}), 1)
        self.assertEqual(self.start(part_way={"/m/E3.mkv"}, finished={"/m/E2.mkv"}), 2)
        self.assertEqual(self.start(part_way={"/m/E1.mkv"}, finished={"/m/E3.mkv"}), 0)

    def test_else_the_one_after_the_last_finished(self):
        self.assertEqual(self.start(finished={"/m/E1.mkv", "/m/E2.mkv"}), 2)
        self.assertEqual(self.start(finished={"/m/E3.mkv", "/m/E1.mkv"}), 3)
        # The next one's size unknown: it plays all the same (from its start).
        self.assertEqual(self.start(finished={"/m/E3.mkv"}, sizes=(1, 2, 3, None)), 3)

    def test_else_the_first(self):
        self.assertIsNone(self.start())
        self.assertIsNone(self.start(finished={"/m/E2.mkv", "/m/E4.mkv"}))  # the last was seen
        # An entry whose size is unknown has no resume key: it never counts.
        self.assertIsNone(self.start(part_way={"/m/E4.mkv"}, sizes=(1, 2, 3, None)))


class ShelfRouterTests(ListCase):
    """The router adds a folder to the shelf once it plays as one (Task 11's read)."""

    def setUp(self):
        super().setUp()
        self.season = self.folder / "Lantern Coast"
        self.episodes = series(self.season)
        self.album = self.folder / "Brannow Fell"
        self.album.mkdir()
        for name in ("01 Tide.mp3", "02 Harbour.mp3"):
            (self.album / name).write_bytes(b"song")
        self.shelf = self.window.folder_shelf

    def names(self):
        return [entry["name"] for entry in self.shelf.folders()]

    def test_an_opened_folder_is_added_once_and_moves_to_the_front(self):
        self.assertEqual(self.names(), [])
        drop(self.window, paths=[self.season])
        self.assertTrue(wait_until(lambda: len(self.lists()) == 1), self.calls)
        self.assertEqual(self.names(), ["Lantern Coast"])
        (entry,) = self.shelf.folders()
        self.assertEqual((entry["kind"], entry["path"]), (SERIES, str(self.season)))
        self.assertEqual(entry["last_entry"], self.episodes[0].name)  # where it started
        self.handover(self.album)  # handed to QiTV by the system
        self.assertTrue(wait_until(lambda: len(self.lists()) == 2), self.calls)
        self.assertEqual(self.names(), ["Brannow Fell", "Lantern Coast"])
        self.assertEqual(self.shelf.folders()[0]["kind"], ALBUM)
        # Opened again (as macOS's Finder spells a dropped folder): once, at the front.
        drop(self.window, paths=[f"{self.season}/"])
        self.assertTrue(wait_until(lambda: len(self.lists()) == 3), self.calls)
        self.assertEqual(self.names(), ["Lantern Coast", "Brannow Fell"])

    def test_the_entry_that_opened_last_is_kept(self):
        drop(self.window, paths=[self.season])
        self.assertTrue(wait_until(lambda: self.lists()), self.calls)
        self.open_entry(str(self.episodes[1]))  # the list goes on to its next entry
        self.assertEqual(self.shelf.folders()[0]["last_entry"], self.episodes[1].name)
        self.window.list_entry_opens(self.current, str(self.album / "01 Tide.mp3"))
        self.assertEqual(self.shelf.folders()[0]["last_entry"], self.episodes[1].name)

    def test_a_double_clicked_file_never_adds_its_folder(self):
        self.handover(self.episodes[0])  # a file double-clicked in the file manager
        self.wait_for_plays(1)
        drop(self.window, paths=[self.episodes[1]])  # one dropped
        self.wait_for_plays(2)
        drop(self.window, paths=self.episodes[:2])  # several dropped together: a list
        self.assertTrue(wait_until(lambda: self.lists()), self.calls)
        playlist = self.folder / "Evening.m3u"
        playlist.write_text(f"{self.episodes[2]}\n", encoding="utf-8")
        drop(self.window, paths=[playlist])  # a playlist of the folder's files
        self.assertTrue(wait_until(lambda: len(self.lists()) == 2), self.calls)
        empty = self.folder / "Empty Shelf"
        empty.mkdir()
        drop(self.window, paths=[empty, self.folder / "Gone Shelf"])  # none plays
        elapse(SETTLE_MS)
        self.assertEqual(self.names(), [])
        self.assertFalse(os.path.exists(os.path.join(self.config_dir, FILE_NAME)))

    def test_the_shelf_is_kept_beside_the_config(self):
        self.assertEqual(self.shelf._path, os.path.join(self.config_dir, FILE_NAME))


if __name__ == "__main__":
    unittest.main()
