"""Why QiTV's window closes, in its log (2.8): QiTV's own Quit, a quit request from outside
(the Dock, another app, logging out) and the window's button are told apart. And the files
QiTV's application is asked to open (macOS's open event, another start's hand-over): kept
until the window takes them, then passed on."""

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from PySide6.QtCore import QEvent

from ui import quit_reason

REPO = Path(__file__).resolve().parents[1]

# QiTV's application in a process of its own (this one's may be a plain QApplication, made by
# another test module first): one JSON line with what it kept, passed on and logged.
OPENED = r"""
import json, logging
from PySide6.QtCore import QCoreApplication
from PySide6.QtGui import QFileOpenEvent
from services.single_instance import InstanceServer
from ui.quit_reason import QiTVApplication
lines = []
class Keep(logging.Handler):
    def emit(self, record):
        lines.append(record.getMessage())
log = logging.getLogger("ui.quit_reason")
log.addHandler(Keep())
log.setLevel(logging.INFO)
app = QiTVApplication([])
emitted = []
app.filesOpened.connect(emitted.append)
server = InstanceServer("qitv-test-not-listening")  # its `opened`, as main() connects it
server.opened.connect(app.add_opened)
QCoreApplication.sendEvent(app, QFileOpenEvent("/x/a.mkv"))  # macOS: a launch's open event
QCoreApplication.sendEvent(app, QFileOpenEvent(""))  # nothing to open
server.opened.emit(["/x/Dizi Bölüm 2.mkv"])  # handed over before the window
kept = app.kept_opened()  # what a second start hands over: they are still kept
first = app.take_opened()  # the window shows
QCoreApplication.sendEvent(app, QFileOpenEvent("/x/b.mkv"))
QCoreApplication.sendEvent(app, QFileOpenEvent("/x/c.mkv"))
server.opened.emit(["/x/d.mkv"])
print(json.dumps({"kept": kept, "first": first, "again": app.take_opened(),
                  "emitted": emitted, "log": lines}), flush=True)
"""


class _Event:
    def __init__(self, kind, spontaneous):
        self._kind, self._spontaneous = kind, spontaneous

    def type(self):
        return self._kind

    def spontaneous(self):
        return self._spontaneous


class DescribeTests(unittest.TestCase):
    def test_each_way_of_closing_has_its_own_words(self):
        self.assertEqual(
            quit_reason.describe(False, quit_reason.MENU), "QiTV's Quit, chosen in QiTV"
        )
        self.assertIn("the Dock, another app or logging out", quit_reason.describe(False, "system"))
        self.assertEqual(
            quit_reason.describe(True, None), "the window's close button or the system"
        )
        self.assertEqual(quit_reason.describe(False, None), "QiTV closed its window")

    def test_a_request_from_outside_wins_over_the_window_button(self):
        self.assertIn("asked QiTV to quit", quit_reason.describe(True, quit_reason.SYSTEM))

    def test_the_quit_action_counts_as_chosen_only_while_qitv_is_the_active_app(self):
        self.assertEqual(quit_reason.asked_by_menu(True), quit_reason.MENU)
        self.assertEqual(quit_reason.asked_by_menu(False), quit_reason.SYSTEM)

    def test_only_a_spontaneous_quit_event_is_the_systems(self):
        self.assertTrue(quit_reason.is_system_quit(_Event(QEvent.Type.Quit, True)))
        self.assertFalse(quit_reason.is_system_quit(_Event(QEvent.Type.Quit, False)))
        self.assertFalse(quit_reason.is_system_quit(_Event(QEvent.Type.Close, True)))


class OpenedFilesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env = dict(os.environ, PYTHONPATH=str(REPO), QT_QPA_PLATFORM="offscreen")
        cls.result = subprocess.run(
            [sys.executable, "-c", OPENED],
            cwd=REPO,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def opened(self):
        self.assertEqual(self.result.returncode, 0, self.result.stderr)
        return json.loads(self.result.stdout.strip().splitlines()[-1])

    def test_file_open_event_queues_then_emits(self):
        said = self.opened()
        # Before the window: kept for it, in order, and not passed on (the window takes them
        # once; passed on too, they would open twice).
        self.assertEqual(said["first"], ["/x/a.mkv", "/x/Dizi Bölüm 2.mkv"])
        # After: passed on only, each once; nothing is kept twice.
        self.assertEqual(said["again"], [])
        self.assertEqual(said["emitted"], [["/x/b.mkv"], ["/x/c.mkv"], ["/x/d.mkv"]])

    def test_the_kept_files_can_be_read_before_the_window_takes_them(self):
        # A second start hands them over with its own (main's claim); if it is the first,
        # the window still takes them.
        said = self.opened()
        self.assertEqual(said["kept"], ["/x/a.mkv", "/x/Dizi Bölüm 2.mkv"])
        self.assertEqual(said["first"], said["kept"])

    def test_a_hand_over_before_the_window_is_not_lost(self):
        # Ruling 6: a start's files reach this QiTV while its window is built.
        self.assertIn("/x/Dizi Bölüm 2.mkv", self.opened()["first"])

    def test_the_log_says_when_the_open_event_came(self):
        # Once each: macOS's launch event before the window, or one after it (Task 6 decides
        # the film's hold before the window shows).
        self.assertEqual(
            self.opened()["log"],
            ["FileOpen arrived before the window.", "FileOpen arrived after the window."],
        )


if __name__ == "__main__":
    unittest.main()
