"""The recordings index (2.9.7): each recording's programme, channel and logo, kept by its file."""

import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from services import recording_schedule as rs
from services.recordings_index import FILE_NAME, KEPT, RecordingsIndex

NEWS = dict(
    title="Evening News",
    channel="Northwind News",
    provider="Demo TV",
    channel_id="7",
    logo="http://logos.test/7.png",
    start=1_000.0,
    stop=4_600.0,
)


class RecordingsIndexTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="qitv-index-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.path = str(self.folder / FILE_NAME)

    def index(self):
        return RecordingsIndex(self.path, write=rs.write_now)

    def test_a_noted_recording_is_read_back_after_a_restart(self):
        self.index().note("/r/Northwind News 2026-11-05 19.00.ts", **NEWS)
        entry = self.index().get("/r/Northwind News 2026-11-05 19.00.ts")
        self.assertEqual(entry["title"], "Evening News")
        self.assertEqual((entry["channel"], entry["provider"]), ("Northwind News", "Demo TV"))
        self.assertEqual((entry["start"], entry["stop"]), (1_000.0, 4_600.0))

    def test_a_file_the_helper_renamed_keeps_its_entry(self):
        index = self.index()
        old, new = "/r/A 2026-11-05 19.00.ts", "/r/A 2026-11-05 19.00 (2).ts"
        index.note(old, **NEWS)
        index.moved(old, new)
        self.assertEqual(self.index().get(new)["title"], "Evening News")
        self.assertIsNone(self.index().get(old))

    def test_a_deleted_file_forgets_its_entry(self):
        index = self.index()
        index.note("/r/a.ts", **NEWS)
        index.forget("/r/a.ts")
        self.assertIsNone(self.index().get("/r/a.ts"))

    def test_it_keeps_at_most_the_newest_entries(self):
        self.assertEqual(KEPT, 2000)
        with mock.patch("services.recordings_index.KEPT", 10):
            index = self.index()
            for n in range(15):
                index.note(f"/r/{n}.ts", **dict(NEWS, start=float(n)))
            again = self.index()
        self.assertIsNone(again.get("/r/0.ts"))  # the oldest went
        self.assertIsNotNone(again.get("/r/14.ts"))

    def test_an_unreadable_or_other_version_file_starts_empty(self):
        Path(self.path).write_text(json.dumps({"version": 2, "entries": {"/r/a.ts": NEWS}}))
        self.assertIsNone(self.index().get("/r/a.ts"))
        Path(self.path).write_text("{not json")
        self.assertIsNone(self.index().get("/r/a.ts"))

    def test_a_recording_without_an_end_keeps_none(self):
        self.index().note("/r/b.ts", **dict(NEWS, stop=None))
        self.assertIsNone(self.index().get("/r/b.ts")["stop"])


if __name__ == "__main__":
    unittest.main()


class PathKeyTests(unittest.TestCase):  # Stage 1 review: one file, however its path is written
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="qitv-index-keys-"))
        self.addCleanup(shutil.rmtree, self.folder, True)

    def test_a_path_written_another_way_finds_the_same_entry(self):
        index = RecordingsIndex(str(self.folder / FILE_NAME), write=rs.write_now)
        decomposed = "/r/Café Channel 2026-11-05 19.00.ts"  # NFD, as some volumes list it
        index.note("/r/./Café Channel 2026-11-05 19.00.ts", **NEWS)
        self.assertEqual(index.get(decomposed)["title"], "Evening News")
        index.forget(decomposed)
        self.assertIsNone(index.get("/r/Café Channel 2026-11-05 19.00.ts"))
