"""The self-update: the asset each machine downloads, Windows's replacement and macOS's disk
image (both stop long-lived background work before they wait for threads)."""

import gc
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QTimer
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


def release_assets(*names):
    return [
        {"name": name, "browser_download_url": f"https://release.invalid/{name}", "size": size}
        for size, name in enumerate(names, 1)
    ]


# The release job's files, in its order; a release before 2.3 had the zips alone.
RELEASE = release_assets(
    "qitv-linux",
    "qitv-windows.exe",
    "qitv-macos-universal.zip",
    "qitv-macos-intel.zip",
    "qitv-macos-universal.dmg",
    "qitv-macos-intel.dmg",
)
OLD_RELEASE = release_assets(
    "qitv-linux", "qitv-windows.exe", "qitv-macos-universal.zip", "qitv-macos-intel.zip"
)


class AssetChoiceTests(unittest.TestCase):
    def choose(self, assets, system="Darwin", machine="arm64"):
        with (
            mock.patch.object(update_checker.platform, "system", return_value=system),
            mock.patch.object(update_checker.platform, "machine", return_value=machine),
        ):
            url, size = update_checker.get_download_url_for_platform(assets)
        return (url.rsplit("/", 1)[-1] if url else None), size

    def test_each_mac_gets_its_own_disk_image(self):
        self.assertEqual(self.choose(RELEASE, machine="arm64"), ("qitv-macos-universal.dmg", 5))
        self.assertEqual(self.choose(RELEASE, machine="x86_64"), ("qitv-macos-intel.dmg", 6))

    def test_an_older_release_gives_each_mac_its_zip(self):
        self.assertEqual(self.choose(OLD_RELEASE)[0], "qitv-macos-universal.zip")
        self.assertEqual(self.choose(OLD_RELEASE, machine="x86_64")[0], "qitv-macos-intel.zip")

    def test_without_the_machine_s_build_another_mac_build_comes_disk_image_first(self):
        assets = release_assets("qitv-macos-universal.zip", "qitv-macos-universal.dmg")
        self.assertEqual(self.choose(assets, machine="x86_64")[0], "qitv-macos-universal.dmg")
        self.assertEqual(self.choose(release_assets("qitv-linux")), (None, 0))

    def test_windows_and_linux_are_unchanged(self):
        self.assertEqual(self.choose(RELEASE, system="Windows")[0], "qitv-windows.exe")
        self.assertEqual(self.choose(RELEASE, system="Linux")[0], "qitv-linux")


class MacUpdateTests(unittest.TestCase):
    """A downloaded disk image opens and QiTV quits, so that Finder can replace the app."""

    PATH = "/Users/someone/Downloads/qitv-macos-universal.dmg"

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        for name in ("_before_update", "_undo_update"):
            hooks = mock.patch.object(update_checker, name, [])
            hooks.start()
            self.addCleanup(hooks.stop)
        self.calls, self.pending = [], [True]
        self.holder = Holder(self.calls, self.pending)
        update_checker.call_before_update(self.holder.stop_live, self.holder.restart_live)
        self.ui = mock.MagicMock()
        self.ui.confirm_quit_and_open.side_effect = lambda: self.calls.append("asked") or True
        self.ui.launch_failed.side_effect = lambda *_args: self.calls.append("launch_failed")
        self.open_error = None  # what starting `open` raises
        self.opened = mock.MagicMock()  # the `open` process: exits at once, fine
        self.opened.poll.return_value = 0

    def update(self, path=PATH, *, system="Darwin", until="close"):
        """Run the update with `open` mocked, until `until` happened (or two seconds passed;
        with None, a tenth of a second)."""

        def popen(*_args, **_kwargs):
            self.calls.append("open")
            if self.open_error is not None:
                raise self.open_error
            return self.opened

        with (
            mock.patch.object(update_checker.platform, "system", return_value=system),
            mock.patch.object(
                update_checker, "has_pending_threads", side_effect=lambda: self.pending[0]
            ),
            mock.patch.object(update_checker, "subprocess") as process,
            mock.patch.object(
                self.app, "closeAllWindows", side_effect=lambda: self.calls.append("close")
            ),
        ):
            process.Popen.side_effect = popen
            update_checker.perform_update(path, "https://release", self.ui)
            waited, limit = 0, (2000 if until else 100)
            while until not in self.calls and waited < limit:
                QTest.qWait(25)
                waited += 25
        return process

    def test_the_image_opens_after_the_hooks_then_qitv_closes(self):
        self.opened.poll.side_effect = [None, None, 0]  # `open` takes a moment
        process = self.update()
        self.assertEqual(self.calls, ["asked", "stop_live", "open", "close"])
        self.assertEqual(process.Popen.call_args[0][0], ["open", self.PATH])
        self.ui.launch_failed.assert_not_called()

    def test_it_waits_for_background_threads_before_opening(self):
        update_checker._before_update.clear()  # no hook ends the wait

        def finish_threads():
            self.calls.append("threads done")
            self.pending[0] = False

        QTimer.singleShot(100, finish_threads)
        self.update()
        self.assertEqual(self.calls, ["asked", "threads done", "open", "close"])

    def test_not_now_keeps_qitv_running(self):
        self.ui.confirm_quit_and_open.side_effect = None
        self.ui.confirm_quit_and_open.return_value = False
        process = self.update(until=None)
        process.Popen.assert_not_called()
        self.assertEqual(self.calls, [])  # no hook ran, nothing closed
        self.ui.downloaded.assert_not_called()

    def test_a_recording_is_asked_about_before_the_update_quits(self):
        note = "One is recording. Quit and stop the recording?"
        update_checker.set_quit_note(lambda: note)
        self.addCleanup(update_checker.set_quit_note, None)
        self.ui.confirm_stop_recordings.side_effect = lambda text: self.calls.append(text) or False
        process = self.update(until=None)
        self.assertEqual(self.calls, ["asked", note])  # Keep recording: nothing else
        process.Popen.assert_not_called()
        self.calls.clear()
        self.ui.confirm_stop_recordings.side_effect = lambda text: self.calls.append(text) or True
        self.update()
        self.assertEqual(self.calls, ["asked", note, "stop_live", "open", "close"])

    def test_without_a_recording_the_update_asks_nothing_more(self):
        update_checker.set_quit_note(lambda: "")
        self.addCleanup(update_checker.set_quit_note, None)
        self.update()
        self.ui.confirm_stop_recordings.assert_not_called()

    def test_a_zip_and_linux_are_replaced_by_hand_as_before(self):
        for path, system in (
            ("/Users/someone/Downloads/qitv-macos-universal.zip", "Darwin"),
            ("/home/someone/Downloads/qitv-linux", "Linux"),
        ):
            with self.subTest(system=system):
                self.ui.reset_mock()
                process = self.update(path, system=system, until=None)
                self.ui.downloaded.assert_called_once_with(path)
                self.ui.confirm_quit_and_open.assert_not_called()
                process.Popen.assert_not_called()
                self.assertEqual(self.calls, [])

    def test_an_open_that_fails_restores_what_stopped_and_says_so(self):
        failures = {
            "open cannot start": (OSError(2, "No such file or directory", self.PATH), 0),
            "open exits with an error": (None, 1),
            "open never answers": (None, None),
        }
        for name, (error, code) in failures.items():
            with self.subTest(name):
                self.calls.clear()
                self.pending[0] = True
                update_checker._before_update.clear()
                update_checker.call_before_update(self.holder.stop_live, self.holder.restart_live)
                self.open_error = error
                self.opened.poll.return_value = code
                with (
                    mock.patch.object(update_checker, "OPEN_TIMEOUT_S", 0.1),
                    self.assertLogs("services.update_checker", "ERROR") as logs,
                ):
                    self.update(until="launch_failed")
                self.assertEqual(
                    self.calls, ["asked", "stop_live", "open", "restart_live", "launch_failed"]
                )
                self.ui.launch_failed.assert_called_with(self.PATH, "https://release")
                self.assertEqual(update_checker._undo_update, [])  # once
                self.assertNotIn("someone", "".join(logs.output))  # no file names

    def test_the_dialog_says_what_happens_and_answers_update_now(self):
        for clicked, answer in ((0, True), (1, False)):
            with self.subTest(answer=answer):
                msg = mock.MagicMock()
                buttons = [mock.MagicMock(), mock.MagicMock()]  # Update Now, Not Now
                msg.addButton.side_effect = buttons
                msg.clickedButton.return_value = buttons[clicked]
                with (
                    mock.patch.object(update_dialogs, "QMessageBox", return_value=msg),
                    mock.patch.object(update_dialogs, "_polish_msgbox"),
                ):
                    self.assertIs(update_dialogs.DIALOGS.confirm_quit_and_open(), answer)
                self.assertIn("Replace", msg.setInformativeText.call_args[0][0])
                msg.setDefaultButton.assert_called_once_with(buttons[0])


class DiskImageDownloadTests(unittest.TestCase):
    """macOS may still have the last update's image mounted from the same file name."""

    def download(self, name):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder, name)
            target.write_bytes(b"old")
            mounted = Path(folder, "mounted")  # the old file, as a mounted image holds it
            os.link(target, mounted)
            response = mock.MagicMock(headers={"content-length": "3"})
            response.iter_content.return_value = [b"new"]
            downloader = update_checker.UpdateDownloader(f"https://release.invalid/{name}", 3)
            done = []
            downloader.finished.connect(done.append)
            with (
                mock.patch.object(
                    update_checker, "get_downloads_folder", return_value=Path(folder)
                ),
                mock.patch.object(update_checker, "requests") as requests,
            ):
                requests.get.return_value = response
                downloader.run()
            self.assertEqual(done, [str(target)])
            self.assertEqual(target.read_bytes(), b"new")
            return mounted.read_bytes()

    def test_a_disk_image_downloads_to_a_new_file(self):
        self.assertEqual(self.download("qitv-macos-universal.dmg"), b"old")

    def test_other_downloads_rewrite_the_file_as_before(self):
        self.assertEqual(self.download("qitv-macos-universal.zip"), b"new")


class ShortDownloadTests(unittest.TestCase):
    """A disk image must arrive whole: `open` succeeds before macOS checks it."""

    def download(self, name, size, body):
        with tempfile.TemporaryDirectory() as folder:
            response = mock.MagicMock(headers={"content-length": str(size)})
            response.iter_content.return_value = [body]
            downloader = update_checker.UpdateDownloader(f"https://release.invalid/{name}", size)
            done, failed = [], []
            downloader.finished.connect(done.append)
            downloader.error.connect(failed.append)
            with (
                mock.patch.object(
                    update_checker, "get_downloads_folder", return_value=Path(folder)
                ),
                mock.patch.object(update_checker, "requests") as requests,
            ):
                requests.get.return_value = response
                downloader.run()
            return bool(done), bool(failed), Path(folder, name).exists()

    def test_a_disk_image_a_byte_short_fails_and_is_removed(self):
        self.assertEqual(
            self.download("qitv-macos-universal.dmg", 100, b"x" * 99), (False, True, False)
        )

    def test_a_whole_disk_image_downloads(self):
        self.assertEqual(
            self.download("qitv-macos-universal.dmg", 100, b"x" * 100), (True, False, True)
        )

    def test_other_downloads_keep_the_ten_percent_margin(self):
        self.assertEqual(self.download("qitv-windows.exe", 100, b"x" * 95), (True, False, True))


class LockedConfigTests(unittest.TestCase):
    """A locked config.json (Windows antivirus, an indexer) must not stop the update flow."""

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
    """services/ holds the update logic; its dialogs are ui/'s."""

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
