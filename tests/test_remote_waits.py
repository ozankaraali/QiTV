"""2.15: the channels phones wait for (ui/remote_waits.py)."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest
from unittest import mock

from PySide6.QtWidgets import QApplication
from gc_fixtures import collect_on_the_gui_thread

from ui import live_controls
from ui.link_lanes import Target
from ui.remote_waits import SiteWaits

OFFLINE = "examplechannel isn't live now"


class JoinTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        collect_on_the_gui_thread(cls)  # as main() does

    def test_a_phone_joining_leaves_the_channels_asks_alone(self):
        """Review 5 (batch 3): a second phone joining a channel's wait neither pushes its
        next ask back nor forgets the ask on its way."""
        self.enterContext(mock.patch.object(live_controls, "OFFLINE_ASK_S", 30.0))
        self.enterContext(mock.patch.object(live_controls, "OFFLINE_JITTER_S", 0.0))
        waits = SiteWaits(lambda wait: True)
        self.addCleanup(waits.end_all)
        target = Target({"name": "examplechannel · Kick", "cmd": "https://kick.com/x"}, "live", "K")
        self.assertEqual(waits.failed("d8", target, None, OFFLINE), "new")
        [wait] = waits.waits()
        wait.asking = True  # an ask on its way
        wait.timer.start(5_000)  # its lost-answer guard, as `_due` arms it
        self.assertEqual(waits.failed("d9", target, None, OFFLINE), "joined")
        self.assertTrue(wait.asking)
        self.assertLess(wait.timer.remainingTime(), 10_000)  # not armed anew (30 s)
        self.assertEqual(wait.devices, ["d8", "d9"])
        self.assertEqual(waits.failed("d8", target, None, "No channel named x on Kick"), None)
        self.assertEqual(wait.devices, ["d9"])


if __name__ == "__main__":
    unittest.main()
