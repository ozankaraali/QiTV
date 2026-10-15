"""The Windows self-update stops long-lived background work before it waits for threads."""

import gc
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import update_checker


class Holder(QObject):
    """Stands in for the TV window: its live engine keeps a thread pending until stopped."""

    def __init__(self, calls, pending):
        super().__init__()
        self.calls = calls
        self.pending = pending

    def stop_live(self):
        self.calls.append("stop_live")
        self.pending[0] = False


class WindowsUpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        hooks = mock.patch.object(update_checker, "_before_update", [])
        hooks.start()
        self.addCleanup(hooks.stop)
        self.calls, self.pending = [], [True]

    def update(self, *, threads_finish_later=False):
        """Run the update with Popen mocked; it must have launched once this returns."""
        with (
            mock.patch.object(
                update_checker, "has_pending_threads", side_effect=lambda: self.pending[0]
            ),
            mock.patch.object(update_checker, "subprocess") as process,
            mock.patch.object(self.app, "closeAllWindows") as close_all,
        ):
            update_checker._perform_windows_update("C:/Downloads/qitv.exe", "https://release")
            if threads_finish_later:
                QTest.qWait(100)  # retried every 25 ms meanwhile
                self.assertFalse(process.Popen.called)
                self.pending[0] = False
            waited = 0
            while not process.Popen.called and waited < 2000:
                QTest.qWait(25)
                waited += 25
            process.Popen.assert_called_once()  # inside the patches: no retry is left behind
        return process, close_all

    def test_the_hooks_run_before_the_wait_for_background_threads(self):
        holder = Holder(self.calls, self.pending)
        update_checker.call_before_update(holder.stop_live)
        process, close_all = self.update()
        self.assertEqual(self.calls, ["stop_live"])
        self.assertEqual(
            process.Popen.call_args[0][0], ["C:/Downloads/qitv.exe", "--replace", sys.executable]
        )
        close_all.assert_called_once_with()

    def test_without_hooks_the_update_still_waits_for_pending_threads(self):
        self.update(threads_finish_later=True)
        self.assertEqual(self.calls, [])

    def test_a_gone_owner_is_skipped_and_a_failing_hook_does_not_stop_the_update(self):
        gone = Holder(self.calls, [True])
        update_checker.call_before_update(gone.stop_live)
        del gone
        gc.collect()

        class Broken(QObject):
            def stop_live(self):
                raise RuntimeError("already deleted")

        broken = Broken()
        holder = Holder(self.calls, self.pending)
        update_checker.call_before_update(broken.stop_live)
        update_checker.call_before_update(holder.stop_live)
        with self.assertLogs("update_checker", "ERROR"):
            self.update()
        self.assertEqual(self.calls, ["stop_live"])


if __name__ == "__main__":
    unittest.main()
