"""Recording on screen (2.2): file names and the folder, the record button and its REC chip,
R, the controller's toggle and its toasts, and REC on the small picture's label."""

from datetime import datetime
import os
from pathlib import Path
from types import SimpleNamespace
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QWidget

from services import recording
from services.live.recorder import ENDED, FAILED, FOLDER, LIMIT, SPACE, STOPPED, WRITE, candidates
from tests.test_tv_controller import NOW, PipelineCase
from ui.keymap import Action, resolve
from ui.overlays.pip_label import REC, PipLabel
from ui.overlays.transport import NO_RECORDING, Transport

TIMELINE = {
    "start": 0.0,
    "end": 600.0,
    "position": 599.0,
    "live_edge": 600.0,
    "lag": 1.0,
    "mode": "live",
    "rate": 1.0,
    "paused": False,
}


class NamingTests(unittest.TestCase):
    WHEN = datetime(2026, 10, 19, 21, 30, 59)

    def test_a_file_is_named_after_the_channel_and_the_minute(self):
        self.assertEqual(
            recording.file_name("My Channel", self.WHEN), "My Channel 2026-10-19 21.30.ts"
        )

    def test_what_file_systems_refuse_is_replaced(self):
        name = recording.file_name('News: "A/B" <HD>|*?\\x\t1', self.WHEN)
        self.assertEqual(name, "News_ _A_B_ _HD_____x_1 2026-10-19 21.30.ts")  # the tab too
        self.assertEqual(recording.safe_name(" .hidden. "), "hidden")
        self.assertEqual(recording.safe_name("CON"), "_CON")
        self.assertEqual(recording.safe_name("lpt1.ts"), "_lpt1.ts")
        self.assertEqual(recording.safe_name("..."), "QiTV")
        self.assertEqual(recording.safe_name(""), "QiTV")
        self.assertEqual(len(recording.safe_name("x" * 300)), recording.NAME_BYTES)
        self.assertEqual(recording.safe_name("Çay  TV ★"), "Çay TV ★")

    def test_a_long_name_is_cut_by_bytes_between_characters_and_stays_unique(self):
        # Review 3: 255 bytes is the limit of most file systems, whatever the characters.
        for long in ("ğ" * 200, "電視" * 100, "x" * 300, "é" * 59 + "x" + "é" * 100):
            with self.subTest(long[:3]):
                name = recording.file_name(long, self.WHEN)
                channel = recording.safe_name(long)
                self.assertLessEqual(len(channel.encode("utf-8")), recording.NAME_BYTES)
                self.assertGreater(len(channel.encode("utf-8")), recording.NAME_BYTES - 4)
                self.assertTrue(long.startswith(channel))  # no character was cut in two
                taken = list(candidates(Path("/rec") / name))
                self.assertLessEqual(len(taken[-1].name.encode("utf-8")), 255)
                self.assertEqual(len({path.name for path in taken}), len(taken))

    def test_the_folder_is_settings_else_the_systems_movies_folder(self):
        default = recording.default_folder()
        self.assertEqual(os.path.basename(default), "QiTV")
        self.assertEqual(recording.folder(SimpleNamespace()), default)
        self.assertEqual(recording.folder(SimpleNamespace(recordings_dir="  ")), default)
        self.assertEqual(recording.folder(SimpleNamespace(recordings_dir="/rec")), "/rec")
        config = SimpleNamespace(recordings_dir="/rec")
        self.assertEqual(
            recording.path_for(config, "One", self.WHEN),
            os.path.join("/rec", "One 2026-10-19 21.30.ts"),
        )

    def test_each_end_says_what_became_of_the_file(self):
        path = os.path.join("/rec", "One 2026-10-19 21.30.ts")
        saved = ("Recording saved: One 2026-10-19 21.30.ts", "info")
        self.assertEqual(recording.ended_text(STOPPED, path, 10), saved)
        self.assertEqual(recording.ended_text(ENDED, path, 10), saved)
        self.assertEqual(
            recording.ended_text(LIMIT, path, 10),
            ("Recording stopped after 4 hours. Saved: One 2026-10-19 21.30.ts", "info"),
        )
        self.assertEqual(
            recording.ended_text(SPACE, path, 10)[0],
            "Recording stopped: the disk is almost full. Saved: One 2026-10-19 21.30.ts",
        )
        self.assertEqual(
            recording.ended_text(SPACE, "", 0), ("Can't record: the disk is almost full", "error")
        )
        self.assertEqual(
            recording.ended_text(WRITE, path, 10),
            (
                "Recording stopped: the file couldn't be written. Saved: One 2026-10-19 21.30.ts",
                "error",
            ),
        )
        self.assertEqual(
            recording.ended_text(FAILED, path, 10),
            ("Recording stopped. Saved: One 2026-10-19 21.30.ts", "error"),
        )
        self.assertEqual(
            recording.ended_text(FOLDER, "", 0),
            ("Can't record: the Recordings folder can't be written to", "error"),
        )
        self.assertEqual(
            recording.ended_text("lost", path, 4096),
            ("Recording stopped unexpectedly. Saved: One 2026-10-19 21.30.ts", "error"),
        )
        self.assertEqual(
            recording.ended_text("lost", "", -1), ("Recording stopped unexpectedly", "error")
        )

    def test_without_a_file_only_a_folder_error_blames_the_folder(self):
        # Review 2: the helper restarting, a closed channel, a busy one, an empty recording.
        for reason, kind in ((FAILED, "error"), (WRITE, "error"), (STOPPED, "info")):
            with self.subTest(reason):
                self.assertEqual(
                    recording.ended_text(reason, "", 0), ("Nothing was recorded", kind)
                )

    def test_the_elapsed_time_reads_minutes_then_hours(self):
        self.assertEqual(recording.elapsed_text(83.9), "REC 01:23")
        self.assertEqual(recording.elapsed_text(3723), "REC 1:02:03")
        self.assertEqual(recording.elapsed_text(-5), "REC 00:00")


class TransportRecordTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make(self):
        self.now = [1000.0]
        host = QWidget()
        host.resize(1280, 720)
        self.addCleanup(host.close)
        t = Transport(host, clock=lambda: self.now[0])
        t.show()
        return t

    def record_at(self, t):
        return t.title_rects()["record"].center()

    def test_live_tv_has_the_record_button_after_the_star_and_a_movie_has_none(self):
        t = self.make()
        t.show_timeshift("One", "News")
        t.update_timeline(TIMELINE, [])
        rects = t.title_rects()
        self.assertGreater(rects["record"].left(), rects["favorite"].right())
        self.assertEqual(t.tooltip_at(self.record_at(t)), "Record (R)")
        t.update_timeline(dict(TIMELINE, position=300.0, lag=300.0, mode="shifted"), [])
        self.assertIn("record", t.title_rects())  # behind live too
        t.show_vod("Film", "")
        t.update_position(0, 100_000)
        self.assertNotIn("record", t.title_rects())
        self.assertNotIn("record", t._layout())

    def test_a_channel_played_directly_says_why_it_cannot_record(self):
        t = self.make()
        t.show_live("One", "")
        self.assertEqual(
            t.tooltip_at(self.record_at(t)), "Can't record: this channel plays without time-shift"
        )
        self.assertEqual(NO_RECORDING, "Can't record: this channel plays without time-shift")
        pressed = []
        t.recordRequested.connect(lambda: pressed.append(True))
        t._click(t._layout(), self.record_at(t))  # the shell says why
        self.assertEqual(pressed, [True])

    def test_while_it_records_the_button_is_lit_and_the_chip_counts(self):
        t = self.make()
        t.show_timeshift("One", "News")
        t.update_timeline(TIMELINE, [])
        self.assertTrue(t.rec_rect().isEmpty())
        before = t._title_row().subtitle_x
        t.set_recording(1000.0)
        self.now[0] = 1083.0
        self.assertTrue(t.recording())
        self.assertEqual(t.rec_text(), "REC 01:23")
        row = t._title_row()
        self.assertGreater(row.rec.left(), row.record.right())
        self.assertGreater(row.subtitle_x, before)  # what is on now makes room
        self.assertLess(row.rec.right(), row.clock_rect.left())
        self.assertEqual(t.tooltip_at(self.record_at(t)), "Stop recording (R)")
        self.assertIsNone(t.control_at(row.rec.center()))  # it only says
        t.grab()  # paints
        t.set_recording(None)
        self.assertTrue(t.rec_rect().isEmpty())
        self.assertEqual(t.rec_text(), "")

    def test_the_button_asks_for_a_recording_and_a_hand_shows_over_it(self):
        t = self.make()
        t.show_timeshift("One", "")
        pressed = []
        t.recordRequested.connect(lambda: pressed.append(True))
        t._click(t._layout(), self.record_at(t))
        self.assertEqual(pressed, [True])
        self.assertEqual(t.control_at(self.record_at(t)), "record")
        t._point_at(self.record_at(t))
        self.assertEqual(t.cursor().shape(), Qt.CursorShape.PointingHandCursor)

    def test_a_long_name_gives_way_and_a_small_window_keeps_it_all(self):
        for width in (1280, 400):
            with self.subTest(width=width):
                t = self.make()
                t.parentWidget().resize(width, width * 9 // 16)
                t.show_timeshift("A channel with a very long name indeed, " * 3, "News")
                t.set_recording(1000.0)
                row = t._title_row()
                self.assertTrue(row.name.endswith("…"))
                self.assertLess(row.rec.right(), row.clock_rect.left())
                self.assertGreater(row.record.left(), row.star.right())


class KeyTests(unittest.TestCase):
    def test_r_records_on_the_player_and_types_in_the_lists(self):
        none = Qt.KeyboardModifier.NoModifier
        self.assertEqual(resolve(Qt.Key.Key_R, none, "r", context="player"), (Action.RECORD, ""))
        self.assertEqual(resolve(Qt.Key.Key_R, none, "r", context="drawer"), (Action.NAV_TEXT, "r"))
        self.assertIsNone(resolve(Qt.Key.Key_R, none, "r", context="guide"))
        ctrl = Qt.KeyboardModifier.ControlModifier
        self.assertNotEqual(resolve(Qt.Key.Key_R, ctrl, "r", context="player"), (Action.RECORD, ""))


class ControllerRecordTests(PipelineCase):
    def test_r_records_the_time_shifted_channel_into_the_recordings_folder(self):
        self.config.recordings_dir = "/rec"
        self.start_pipeline(0)
        self.controller.handle(Action.RECORD)
        name = f"One {NOW.astimezone().strftime('%Y-%m-%d %H.%M')}.ts"
        self.assertEqual(self.live.named("start_recording"), [(os.path.join("/rec", name),)])
        self.assertEqual(self.transport.recording_since, 100.0)  # lit, the REC chip counts
        self.assertTrue(self.transport.visible)
        self.transport.recordRequested.emit()  # the button stops it
        self.assertEqual(self.live.named("stop_recording"), [()])
        self.assertIsNone(self.transport.recording_since)

    def test_a_channel_played_directly_or_still_opening_says_why(self):
        self.start_channel(0)
        self.controller.handle(Action.RECORD)
        self.assertEqual(
            self.toast.messages()[-1], ("Recording can start once the channel plays", "info")
        )
        self.live.play_direct()
        self.controller.handle(Action.RECORD)
        self.assertEqual(self.toast.messages()[-1], (NO_RECORDING, "info"))
        self.assertEqual(self.live.named("start_recording"), [])

    def test_a_channel_change_stops_it_and_its_end_is_toasted(self):
        self.start_pipeline(0)
        self.controller.handle(Action.RECORD)
        self.start_channel(1)  # the old channel's stop ends the recording
        self.assertEqual(self.live.named("stop_recording"), [()])
        self.assertIsNone(self.transport.recording_since)
        self.live.recordingEnded.emit(STOPPED, "/rec/One 2026-10-15 20.30.ts", 4096)
        self.assertEqual(
            self.toast.messages()[-1], ("Recording saved: One 2026-10-15 20.30.ts", "info")
        )
        self.live.recordingEnded.emit(SPACE, "/rec/Two.ts", 4096)
        self.assertEqual(self.toast.messages()[-1][1], "error")


class LabelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_a_small_picture_that_records_says_rec_whole(self):
        host = QWidget()
        host.resize(640, 360)
        self.addCleanup(host.close)
        label = PipLabel(host)
        label.setGeometry(0, 0, 120, 68)
        label.set_info("A channel with a very long name", REC)
        self.assertEqual(label.shown_text()[1], "REC")
        self.assertEqual(label.info(), ("A channel with a very long name", REC))
        label.grab()  # paints its red dot


if __name__ == "__main__":
    unittest.main()
