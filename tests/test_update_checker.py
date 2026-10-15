"""The Windows self-update stops long-lived background work before it waits for threads."""

import gc
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from services import update_checker
from ui import update_dialogs


class Holder(QObject):
    """Stands in for the TV window: its live engine keeps a thread pending until stopped."""

    def __init__(self, calls, pending):
        super().__init__()
        self.calls = calls
        self.pending = pending

    def stop_live(self):
        self.calls.append("stop_live")
        self.pending[0] = False

    def restart_live(self):
        self.calls.append("restart_live")


class WindowsUpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        for name in ("_before_update", "_undo_update"):
            hooks = mock.patch.object(update_checker, name, [])
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

    def test_a_launch_that_fails_runs_the_undo_hooks_before_the_error_dialog(self):
        holder = Holder(self.calls, self.pending)
        update_checker.call_before_update(holder.stop_live, holder.restart_live)

        def dialog(*_args):
            self.calls.append("dialog")
            return mock.MagicMock()

        with (
            mock.patch.object(update_checker, "has_pending_threads", return_value=False),
            mock.patch.object(update_checker, "subprocess") as process,
            mock.patch.object(update_dialogs, "QMessageBox", side_effect=dialog),
            mock.patch.object(update_dialogs, "_polish_msgbox"),
            mock.patch.object(self.app, "closeAllWindows") as close_all,
        ):
            process.Popen.side_effect = OSError("not a valid application")
            update_checker._perform_windows_update(
                "C:/Downloads/qitv.exe", "https://release", update_dialogs.DIALOGS
            )
        self.assertEqual(self.calls, ["stop_live", "restart_live", "dialog"])
        close_all.assert_not_called()
        self.assertEqual(update_checker._undo_update, [])  # once

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
        with self.assertLogs("services.update_checker", "ERROR"):
            self.update()
        self.assertEqual(self.calls, ["stop_live"])


class LockedConfigTests(unittest.TestCase):
    """M9: a locked config.json (Windows antivirus, an indexer) must not stop the update flow."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.config = mock.MagicMock()
        self.config.save_config.side_effect = PermissionError(
            13, "Permission denied", "/home/someone/.config/qitv/config.json"
        )

    def test_download_and_install_starts_with_the_config_locked(self):
        msg, download = mock.MagicMock(), mock.MagicMock()
        msg.addButton.side_effect = [download, mock.MagicMock(), mock.MagicMock()]
        msg.clickedButton.return_value = download
        info = {
            "version": "9.9.9",
            "url": "https://release.invalid/qitv",
            "download_url": "https://release.invalid/qitv.zip",
            "file_size": 1,
        }
        with (
            mock.patch.object(update_dialogs, "QMessageBox", return_value=msg),
            mock.patch.object(update_dialogs, "_polish_msgbox"),
            mock.patch.object(update_dialogs, "start_download") as start_download,
            mock.patch.object(sys, "frozen", True, create=True),
            self.assertLogs("services.update_checker", "WARNING") as logs,
        ):
            update_dialogs.show_update_dialog(info, self.config, manual=True)
        start_download.assert_called_once_with(
            "https://release.invalid/qitv.zip", 1, "https://release.invalid/qitv"
        )
        self.assertEqual(self.config.skipped_update_version, "")
        self.assertIn("[Errno 13] Permission denied", logs.output[0])
        self.assertNotIn("someone", logs.output[0])  # no file names (safe_message)

    def test_skip_and_stop_reminders_go_on_with_the_config_locked(self):
        with self.assertLogs("services.update_checker", "WARNING") as logs:
            update_checker.skip_version(self.config, "9.9.9")
            update_checker.disable_update_reminders(self.config)
        self.assertEqual(self.config.skipped_update_version, "9.9.9")
        self.assertIs(self.config.check_updates, False)
        self.assertEqual(len([line for line in logs.output if "Permission denied" in line]), 2)


class LayoutTests(unittest.TestCase):
    """Review M9 (ruling): services/ holds the update logic; its dialogs are ui/'s."""

    def test_the_service_builds_no_widgets(self):
        source = Path(update_checker.__file__).read_text(encoding="utf-8")
        self.assertNotIn("QtWidgets", source)
        for name in ("QMessageBox", "QProgressDialog", "QCheckBox", "webbrowser"):
            self.assertFalse(hasattr(update_checker, name), name)

    def test_the_dialogs_check_through_the_service(self):
        with mock.patch.object(update_checker, "check_for_updates") as check:
            update_dialogs.check_for_updates(None, manual=True)
        check.assert_called_once_with(None, True, update_dialogs.DIALOGS)


if __name__ == "__main__":
    unittest.main()
