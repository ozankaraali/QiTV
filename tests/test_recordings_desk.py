"""The Recordings page's controller (2.9.7): what it shows of QiTV now, and its buttons."""

from datetime import datetime, timedelta
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from services import recording_desk as desk, recording_schedule as rs
from services.connection_budget import ConnectionBudget
from services.epg_view import Programme
from services.recordings_list import RecordingsList
from tests.timer_fixtures import wait_until
from ui.library.recordings_view import RecordingsView
from ui.recordings_desk import RecordingsDesk

app = QApplication.instance() or QApplication([])
NEWS = {"id": "7", "name": "News One", "_provider": "P"}
FILMS = {"id": "8", "name": "Films", "_provider": "P"}


class FakeCatalogs:
    def provider(self, name):
        if name != "P":
            raise KeyError(name)
        return {"name": "P", "type": "XTREAM"}

    def max_connections(self, name):
        return 1

    def provider_names(self):
        return ["P", "Q"]

    def programmes(self, channel, now, limit=8):
        return []


class FakeScreens:
    def __init__(self):
        self.notes, self.played, self.deleted = [], [], []

    def notify(self, text, kind="info", ms=0):
        self.notes.append(text)

    def play(self, item, kind, context):
        self.played.append((item["path"], kind))

    def delete_recording(self, item):
        self.deleted.append(item["path"])


class RecordingsDeskTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="qitv-desk-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        (self.folder / "rec").mkdir()
        (self.folder / "rec" / "News One 2026-10-26 20.00.ts").write_bytes(b"x" * 100)
        self.now = datetime.now().astimezone()
        self.schedule = rs.Schedule(str(self.folder / rs.FILE_NAME), write=rs.write_now)
        self.saved = []
        self.config = SimpleNamespace(
            recordings_dir=str(self.folder / "rec"),
            recording_places=[],
            save_config=lambda: self.saved.append(True),
        )
        self.cancelled = []
        self.budget = ConnectionBudget()
        self.screens = FakeScreens()
        self.window = SimpleNamespace(
            recording_schedule=self.schedule,
            scheduler=None,
            budget=self.budget,
            catalogs=FakeCatalogs(),
            catalog=SimpleNamespace(provider_names=lambda: ["P", "Q"]),
            controller=SimpleNamespace(sessions=None),
            screens=self.screens,
            config_manager=self.config,
            closing=lambda: False,
            cancel_scheduled=self.cancelled.append,
            schedule_changed=lambda: None,
        )
        self.view = RecordingsView()
        self.view.resize(1280, 720)
        self.addCleanup(self.view.deleteLater)
        self.recordings = RecordingsList(self.config)
        self.desk = RecordingsDesk(self.window, self.view, self.recordings)
        self.addCleanup(self.desk.hide)  # before the view goes: a late read refreshes nothing
        self.marks = []
        self.desk.markChanged.connect(self.marks.append)

    def programme(self, title, minutes, length=60):
        start = self.now + timedelta(minutes=minutes)
        return Programme(title, start, start + timedelta(minutes=length), "", None)

    def show(self):
        self.desk.show()
        self.assertTrue(wait_until(lambda: bool(self.recordings.places()), 5000))
        self.desk.refresh()

    def keys(self):
        return [e.key for e in self.view.entries()]

    def test_it_lists_the_schedule_files_places_and_the_streams_in_use(self):
        item = self.schedule.add_once(NEWS, self.programme("Late news", 120))
        self.schedule.add_once(FILMS, self.programme("Film", 150))  # clashes: P allows 1
        self.budget.request("P", 1, purpose="probe")
        self.show()
        self.assertIn(f"item:{item['id']}", self.keys())
        path = str(self.folder / "rec" / "News One 2026-10-26 20.00.ts")
        self.assertIn(f"file:{path}", self.keys())
        self.assertIn(f"place:{self.config.recordings_dir}", self.keys())
        coming = [e for e in self.view.entries() if e.key == f"item:{item['id']}"][0]
        self.assertIn("Clashes at", coming.note)
        ((streams, _rect),) = zip(self.view._streams, self.view.stream_rects())
        self.assertEqual((streams.provider, streams.limit), ("P", 1))
        self.assertEqual([u.kind for u in streams.uses], ["probe"])

    def test_its_buttons_cancel_dismiss_play_and_delete(self):
        item = self.schedule.add_once(NEWS, self.programme("Late news", 120))
        self.schedule.finished(
            rs.Job("x", "Missed one", NEWS, "P", 1.0, 2.0), rs.MISSED, why="closed"
        )
        self.show()
        self.assertEqual(self.marks, ["warn"])  # the tab's dot: a problem
        self.view.buttonClicked.emit(f"item:{item['id']}", desk.CANCEL)
        self.assertEqual(self.cancelled, [item["id"]])
        (outcome,) = self.schedule.outcomes()
        self.view.buttonClicked.emit(desk.outcome_key(outcome), desk.DISMISS)
        self.assertTrue(self.schedule.outcomes()[0]["seen"])
        self.assertEqual(self.marks, ["warn", ""])
        path = str(self.folder / "rec" / "News One 2026-10-26 20.00.ts")
        self.view.buttonClicked.emit(f"file:{path}", desk.PLAY)
        self.view.buttonClicked.emit(f"file:{path}", desk.DELETE)
        self.assertEqual(self.screens.played, [(path, "recording")])
        self.assertEqual(self.screens.deleted, [path])

    def test_a_place_becomes_where_new_recordings_go_and_the_old_stays(self):
        other = str(self.folder / "other")
        os.mkdir(other)
        self.config.recording_places = [other]
        self.show()
        self.view.buttonClicked.emit(f"place:{other}", desk.DEFAULT)
        self.assertEqual(self.config.recordings_dir, other)
        self.assertEqual(self.config.recording_places, [str(self.folder / "rec")])
        self.assertEqual(self.saved, [True])
        self.view.buttonClicked.emit(f"place:{self.folder / 'rec'}", desk.REMOVE)
        self.assertEqual(self.config.recording_places, [])

    def test_hidden_it_stops_looking(self):
        self.show()
        self.assertTrue(self.desk._timer.isActive())
        self.desk.hide()
        self.assertFalse(self.desk._timer.isActive() or self.desk._files.isActive())


if __name__ == "__main__":
    unittest.main()
