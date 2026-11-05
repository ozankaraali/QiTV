"""Recordings in the Library (2.8): the recordings folder's files as Library titles, newest
first, read on a thread of their own."""

from datetime import datetime
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from services import recording_schedule as rs, recordings_list
from services.recordings_index import RecordingsIndex
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

    def test_a_file_that_cannot_go_says_why_and_stays_listed(self):
        # Windows refuses to delete a file another program (a player) has open.
        path = touch(self.folder, "News 2026-10-19 21.30.ts")
        in_use = PermissionError(13, "The process cannot access the file")
        setattr(in_use, "winerror", 32)  # ERROR_SHARING_VIOLATION (a Windows error)
        denied = PermissionError(13, "Access is denied")
        for error, words in ((in_use, "open in another program"), (denied, "Access is denied")):
            with self.subTest(words):
                shelf = RecordingsList(self.config)
                shelf.refresh()
                self.wait_for(shelf, 0)
                said = []
                shelf.delete_failed.connect(said.append)
                with (
                    mock.patch.object(recordings_list.os, "remove", side_effect=error),
                    self.assertLogs("services.recordings_list", "WARNING"),
                ):
                    shelf.delete(str(path))
                    deadline = time.monotonic() + 5
                    while not said and time.monotonic() < deadline:
                        app.processEvents()
                        time.sleep(0.01)
                self.assertEqual(len(said), 1)
                self.assertIn("“News 2026-10-19 21.30.ts”", said[0])
                self.assertIn(words, said[0])
                self.assertTrue(path.exists())
                self.assertEqual(len(shelf.items()), 1)

    def test_a_recording_still_being_written_is_not_deleted(self):  # 2.8 review M10
        path = touch(self.folder, "News 2026-10-19 21.30.ts")
        shelf = RecordingsList(self.config, writing=lambda: {str(path)})
        shelf.refresh()
        self.wait_for(shelf, 0)
        self.assertTrue(shelf.items()[0]["_writing"])
        shelf.delete(str(path))
        deadline = time.monotonic() + 0.3
        while time.monotonic() < deadline:
            app.processEvents()
        self.assertTrue(path.exists())

    def test_a_scheduled_recording_is_named_by_its_programme(self):
        path = touch(self.folder, "News 2026-10-19 21.30.ts")
        titles = {str(path): "The Evening News"}
        shelf = RecordingsList(self.config, titles=lambda p: titles.get(p, ""))
        shelf.refresh()
        self.wait_for(shelf, 0)
        (item,) = shelf.items()
        self.assertEqual(item["name"], "The Evening News")
        self.assertIn("News", item["description"])  # its channel stays in the details

    def test_a_recording_the_index_knows_shows_its_programme(self):  # 2.9.7
        path = touch(self.folder, "Northwind News 2026-11-05 19.00.ts")
        index = RecordingsIndex(str(self.folder.parent / "idx.json"), write=rs.write_now)
        self.addCleanup(lambda: Path(self.folder.parent / "idx.json").unlink(missing_ok=True))
        index.note(
            str(path),
            title="Evening News",
            channel="Northwind News",
            provider="Demo TV",
            channel_id="7",
            logo="http://logos.test/7.png",
            start=1_000.0,
            stop=4_600.0,
        )
        shelf = RecordingsList(self.config, index=index)
        shelf.refresh()
        self.wait_for(shelf, 0)
        (item,) = shelf.files()
        self.assertEqual(item["name"], "Evening News")
        self.assertEqual(item["_title"], "Evening News")
        self.assertEqual(item["_channel"], "Northwind News")
        self.assertEqual(item["_provider"], "Demo TV")
        self.assertEqual(item["_logo"], "http://logos.test/7.png")
        self.assertEqual((item["_start"], item["_stop"]), (1_000.0, 4_600.0))

    def test_a_recording_it_does_not_know_shows_its_channel(self):  # 2.9.7
        touch(self.folder, "Northwind News 2026-11-05 19.00.ts")
        index = RecordingsIndex(str(self.folder / "none.json"), write=rs.write_now)
        shelf = RecordingsList(self.config, index=index)
        shelf.refresh()
        self.wait_for(shelf, 0)
        (item,) = shelf.files()
        self.assertEqual((item["name"], item["_title"]), ("Northwind News", ""))
        self.assertIsNone(item["_stop"])

    def test_deleting_a_recording_forgets_its_index_entry(self):  # 2.9.7
        path = touch(self.folder, "News 2026-10-19 21.30.ts")
        index = RecordingsIndex(str(self.folder.parent / "idx2.json"), write=rs.write_now)
        self.addCleanup(lambda: Path(self.folder.parent / "idx2.json").unlink(missing_ok=True))
        index.note(
            str(path),
            title="T",
            channel="News",
            provider="",
            channel_id="",
            logo="",
            start=1.0,
            stop=None,
        )
        shelf = RecordingsList(self.config, index=index)
        shelf.refresh()
        self.wait_for(shelf, 0)
        shelf.delete(str(path))
        self.wait_for(shelf, 1)
        self.assertIsNone(index.get(str(path)))

    def test_scheduled_ones_come_first_and_change_the_list(self):
        touch(self.folder, "News 2026-10-19 21.30.ts")
        shelf = RecordingsList(self.config)
        shelf.refresh()
        self.wait_for(shelf, 0)
        version = shelf.version
        shelf.set_scheduled(
            [
                {
                    "id": "a1",
                    "kind": "once",
                    "title": "Late Show",
                    "start": 1_800_000_000.0,
                    "stop": 1_800_003_600.0,
                    "channel": {"name": "Two"},
                },
                {"id": "b2", "kind": "series", "title": "Quiz", "channel": {"name": "One"}},
            ]
        )
        self.assertGreater(shelf.version, version)
        names = [i["name"] for i in shelf.items()]
        self.assertEqual(names[:2], ["Late Show", "Quiz"])
        self.assertTrue(shelf.items()[0]["_scheduled"])
        self.assertEqual(shelf.items()[1]["_item_id"], "b2")
        self.assertEqual(names[2], "News")
        # 2.8.12: the Library's strip shows a programme's time where Play is; a series none.
        start = recordings_list.short_when(1_800_000_000.0)
        self.assertEqual(shelf.items()[0]["_when"], start)
        self.assertRegex(start, r"\d{2}:\d{2}$")
        self.assertEqual(shelf.items()[1]["_when"], "")

    def test_a_timer_says_its_time_and_days(self):  # 2.8 (owner): recorded at a time
        from PySide6.QtCore import QLocale

        from services.i18n import app_locale

        shelf = RecordingsList(self.config)
        base = {"kind": "timer", "title": "", "begin": 21 * 60, "minutes": 90}
        base["channel"] = {"name": "Two"}
        shelf.set_scheduled(
            [
                dict(base, id="t1", days=list(range(7))),
                dict(base, id="t2", days=[0, 1, 2, 3, 4], title="Late film"),
                dict(base, id="t3", days=[5, 6]),
                dict(base, id="t4", days=[0, 2]),
            ]
        )
        items = {i["_item_id"]: i for i in shelf.items()}
        self.assertEqual(items["t1"]["name"], "21:00–22:30")  # no name: its time
        self.assertEqual(items["t2"]["_when"], "21:00–22:30")  # on the strip, where Play is
        self.assertEqual(items["t2"]["name"], "Late film")
        self.assertEqual(items["t1"]["description"], "Records Two 21:00–22:30, every day")
        self.assertEqual(items["t2"]["description"], "Records Two 21:00–22:30, Monday to Friday")
        self.assertEqual(items["t3"]["description"], "Records Two 21:00–22:30, Saturday and Sunday")
        mon, wed = (app_locale().dayName(d, QLocale.FormatType.ShortFormat) for d in (1, 3))
        self.assertEqual(items["t4"]["description"], f"Records Two 21:00–22:30, {mon}, {wed}")
        self.assertEqual(items["t4"]["genre"], "Scheduled")

    def test_a_recording_being_written_is_marked(self):
        path = touch(self.folder, "News 2026-10-19 21.30.ts")
        shelf = RecordingsList(self.config, writing=lambda: {str(path)})
        shelf.refresh()
        self.wait_for(shelf, 0)
        self.assertTrue(shelf.items()[0]["_writing"])


if __name__ == "__main__":
    unittest.main()
