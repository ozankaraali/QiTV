"""The Recordings home (2.9.7): what records now, what's next, adding one, the guide and the
recent recordings, by mouse and keys."""

from datetime import datetime, timedelta
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from services import recording_desk as desk, recording_schedule as rs
from ui.library.recorder_home import RecorderHome

app = QApplication.instance() or QApplication([])
NOW = datetime(2026, 11, 5, 20, 12, 30).astimezone()
MONO = 10_000.0
NEWS = desk.Recording(
    "sched:x",
    "Northwind News",
    MONO - 4350,
    "scheduled",
    "Evening News",
    (NOW + timedelta(seconds=6330)).timestamp(),
)
MANUAL = desk.Recording("main", "Harbor Sports", MONO - 4350, "large", "", 0.0)
STREAMS = (desk.Streams("Demo TV", 2, (desk.Use("scheduled", "Evening News", True),)),)


def upcoming(minutes, kind=rs.ONCE):
    start = (NOW + timedelta(minutes=minutes)).timestamp()
    return desk.Next(
        "i1", "Mountain Rivers", "Copperline World", "Demo TV", start, start + 3600, kind
    )


def tile(path, title):
    return {
        "id": f"recording:{path}",
        "name": title,
        "path": path,
        "_title": title,
        "_channel": "Northwind News",
        "_start": NOW.timestamp() - 86400,
        "_stop": None,
    }


class RecorderHomeTests(unittest.TestCase):
    def setUp(self):
        self.home = RecorderHome(lambda c, s, n: [], lambda: NOW, clock=lambda: MONO)
        self.addCleanup(self.home.deleteLater)
        self.home.resize(1200, 760)
        self.events = []
        for name in ("watchRequested", "stopRequested", "editRequested", "playRequested"):
            getattr(self.home, name).connect(lambda value, n=name: self.events.append((n, value)))
        self.home.addRequested.connect(lambda: self.events.append(("addRequested", None)))
        self.home.allRequested.connect(lambda: self.events.append(("allRequested", None)))

    def click(self, rect):
        QTest.mouseClick(self.home, Qt.LeftButton, Qt.NoModifier, rect.center().toPoint())

    def test_the_hero_shows_what_records_with_its_progress(self):
        self.home.set_hero(desk.Hero((NEWS,), None, STREAMS))
        ((title, text, progress),) = self.home.now_cards()
        self.assertEqual(title, "Evening News")
        self.assertEqual(text, "1:12:30 of 2:58 · until 21:58")
        self.assertAlmostEqual(progress, 4350 / 10680, places=3)
        self.assertFalse(self.home.grab().isNull())

    def test_a_recording_without_an_end_shows_its_elapsed_time_only(self):
        self.home.set_hero(desk.Hero((MANUAL,), None, ()))
        ((title, text, progress),) = self.home.now_cards()
        self.assertEqual((title, text, progress), ("Harbor Sports", "1:12:30", None))

    def test_two_recordings_now_both_show(self):
        self.home.set_hero(desk.Hero((NEWS, MANUAL), None, STREAMS))
        self.assertEqual([c[0] for c in self.home.now_cards()], ["Evening News", "Harbor Sports"])
        self.assertIn("stop:main", self.home.hero_rects())
        self.assertFalse(self.home.grab().isNull())

    def test_next_counts_down_then_names_the_time_then_the_day(self):
        self.home.set_hero(desk.Hero((), upcoming(48), ()))
        self.assertEqual(self.home.next_when(), "in 48 min")
        self.home.set_hero(desk.Hero((), upcoming(150), ()))
        self.assertEqual(self.home.next_when(), "at 22:42")
        self.home.set_hero(desk.Hero((), upcoming(24 * 60), ()))
        when = self.home.next_when()
        self.assertTrue(when.endswith("20:12") and not when.startswith(("in ", "at ")), when)

    def test_add_opens_record_at_a_time_and_says_the_streams(self):
        self.home.set_hero(desk.Hero((), None, STREAMS))
        self.assertEqual(self.home.stream_lines(), ["Demo TV · 1 of 2 streams"])
        self.click(self.home.hero_rects()["add"])
        self.assertEqual(self.events, [("addRequested", None)])

    def test_watch_stop_and_edit_reach_their_recording(self):
        self.home.set_hero(desk.Hero((NEWS,), upcoming(48), STREAMS))
        rects = self.home.hero_rects()
        self.click(rects["watch:sched:x"])
        self.click(rects["stop:sched:x"])
        self.click(rects["edit"])
        self.assertEqual(
            self.events,
            [("watchRequested", "sched:x"), ("stopRequested", "sched:x"), ("editRequested", "i1")],
        )

    def test_a_series_next_offers_no_edit(self):
        self.home.set_hero(desk.Hero((), upcoming(48, rs.SERIES), ()))
        self.assertNotIn("edit", self.home.hero_rects())

    def test_all_recordings_and_a_tile_play(self):
        items = [tile("/r/a.ts", "Night Match"), tile("/r/b.ts", "Kitchen Stories")]
        self.home.set_shelf(items)
        self.click(self.home.all_rect())
        (first, rect), _second = self.home.shelf_rects()
        self.click(rect)
        self.assertEqual(self.events, [("allRequested", None), ("playRequested", first)])

    def test_nothing_recording_or_scheduled_says_so_and_still_offers_add(self):
        self.home.set_hero(desk.Hero((), None, ()))
        self.assertEqual(self.home.now_cards(), [])
        self.assertEqual(self.home.next_when(), "")
        self.assertIn("add", self.home.hero_rects())
        self.assertFalse(self.home.grab().isNull())

    def test_keys_reach_the_hero_the_guide_and_the_shelf(self):
        self.home.set_hero(desk.Hero((NEWS,), upcoming(48), STREAMS))
        self.home.set_shelf([tile("/r/a.ts", "Night Match")])
        self.assertEqual(self.home.focus_region(), "hero")
        self.assertEqual(self.home.focused_button(), "watch:sched:x")  # the accented one first
        self.home.move_by(1, 0)
        self.assertEqual(self.home.focused_button(), "stop:sched:x")
        self.home.move_by(1, 0)
        self.home.move_by(1, 0)
        self.assertEqual(self.home.focused_button(), "add")
        self.home.activate()
        self.assertEqual(self.events[-1], ("addRequested", None))
        self.home.move_by(0, 1)
        self.assertEqual(self.home.focus_region(), "guide")
        self.home.move_by(0, 1)  # no channels: past the guide, the shelf
        self.assertEqual(self.home.focus_region(), "shelf")
        self.home.activate()
        self.assertEqual(self.events[-1][0], "playRequested")
        ups = []
        self.home.upEdge.connect(lambda: ups.append(True))
        self.home.move_by(0, -1)
        self.home.move_by(0, -1)
        self.home.move_by(0, -1)
        self.assertEqual(ups, [True])


if __name__ == "__main__":
    unittest.main()
