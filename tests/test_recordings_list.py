"""Recordings in the Library (2.8): the recordings folder's files as Library titles, newest
first, read on a thread of their own."""

from datetime import datetime
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from services import recordings_list
from services.recordings_list import RECORDING_PREFIX, RecordingsList, parse_name

app = QApplication.instance() or QApplication([])


def touch(folder: Path, name: str, size: int = 10, mtime: float = 1_761_000_000.0) -> Path:
    path = folder / name
    with open(path, "wb") as out:
        out.truncate(size)  # sparse: its size without writing it
    os.utime(path, (mtime, mtime))
    return path


class ParseNameTests(unittest.TestCase):
    def test_a_recordings_name_gives_its_channel_and_start(self):
        self.assertEqual(
            parse_name("My Channel 2026-10-19 21.30.ts"),
            ("My Channel", datetime(2026, 10, 19, 21, 30)),
        )

    def test_a_second_file_of_the_same_minute_counts_the_same(self):
        self.assertEqual(
            parse_name("My Channel 2026-10-19 21.30 (2).ts"),
            ("My Channel", datetime(2026, 10, 19, 21, 30)),
        )

    def test_another_name_is_its_own_title_without_a_start(self):
        self.assertEqual(parse_name("holiday clip.ts"), ("holiday clip", None))


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="qitv-recordings-"))
        self.addCleanup(shutil.rmtree, self.folder, True)

    def test_recordings_are_listed_newest_first_as_library_titles(self):
        old = touch(self.folder, "News 2026-10-19 21.30.ts", size=1_500_000_000)
        touch(self.folder, "Film Channel 2026-10-20 08.05.ts", size=300_000_000)
        items = recordings_list.scan(str(self.folder))
        self.assertEqual([i["name"] for i in items], ["Film Channel", "News"])
        news = items[1]
        self.assertEqual(news["id"], RECORDING_PREFIX + "News 2026-10-19 21.30.ts")
        self.assertEqual(news["path"], str(old))
        self.assertEqual(news["cmd"], old.absolute().as_uri())
        self.assertTrue(news["_recording"])
        self.assertEqual(news["added"], str(int(datetime(2026, 10, 19, 21, 30).timestamp())))
        self.assertIn("2026-10-19 21:30", news["description"])
        self.assertIn("1.5 GB", news["description"])

    def test_a_file_without_a_start_in_its_name_is_dated_by_the_disk(self):
        touch(self.folder, "clip.ts", mtime=1_700_000_000.0)
        (item,) = recordings_list.scan(str(self.folder))
        self.assertEqual(item["added"], "1700000000")

    def test_only_recordings_are_listed(self):
        touch(self.folder, "News 2026-10-19 21.30.ts")
        touch(self.folder, "notes.txt")
        touch(self.folder, ".hidden.ts")
        (self.folder / "sub.ts").mkdir()
        self.assertEqual([i["name"] for i in recordings_list.scan(str(self.folder))], ["News"])

    def test_a_folder_that_is_not_there_lists_nothing(self):
        self.assertEqual(recordings_list.scan(str(self.folder / "missing")), [])


class RecordingsListTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="qitv-recordings-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.config = type("Config", (), {"recordings_dir": str(self.folder)})()

    def wait_for(self, shelf, version):
        deadline = time.monotonic() + 5
        while shelf.version == version and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)

    def test_a_refresh_reads_the_folder_on_its_own_thread_and_says_so(self):
        touch(self.folder, "News 2026-10-19 21.30.ts")
        shelf = RecordingsList(self.config)
        changes = []
        shelf.changed.connect(lambda: changes.append(shelf.version))
        self.assertEqual(shelf.items(), [])
        shelf.refresh()
        self.wait_for(shelf, 0)
        self.assertEqual([i["name"] for i in shelf.items()], ["News"])
        self.assertEqual(changes, [1])

    def test_an_unchanged_folder_changes_nothing(self):
        touch(self.folder, "News 2026-10-19 21.30.ts")
        shelf = RecordingsList(self.config)
        shelf.refresh()
        self.wait_for(shelf, 0)
        shelf.refresh()
        deadline = time.monotonic() + 0.3
        while time.monotonic() < deadline:
            app.processEvents()
        self.assertEqual(shelf.version, 1)

    def test_deleting_a_listed_recording_removes_its_file_and_reads_again(self):
        path = touch(self.folder, "News 2026-10-19 21.30.ts")
        other = self.folder.parent / "not-a-recording.ts"
        shelf = RecordingsList(self.config)
        shelf.refresh()
        self.wait_for(shelf, 0)
        shelf.delete(str(other))  # not one of the list's files: nothing happens
        shelf.delete(str(path))
        self.wait_for(shelf, 1)
        self.assertFalse(path.exists())
        self.assertEqual(shelf.items(), [])

    def test_a_recording_being_written_is_marked(self):
        path = touch(self.folder, "News 2026-10-19 21.30.ts")
        shelf = RecordingsList(self.config, writing=lambda: {str(path)})
        shelf.refresh()
        self.wait_for(shelf, 0)
        self.assertTrue(shelf.items()[0]["_writing"])


if __name__ == "__main__":
    unittest.main()
