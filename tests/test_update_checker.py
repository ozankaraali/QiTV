"""The self-update: the asset each machine downloads, Windows's replacement and macOS's disk
image (both stop long-lived background work before they wait for threads)."""

import gc
import os
from pathlib import Path
import sys
import tempfile
from typing import Callable, NamedTuple
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QApplication
from timer_fixtures import pause, wait_until

from services import update_checker
from ui import update_dialogs, window_cards
from ui.overlays.settings_card import CANCEL


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


class Card(NamedTuple):
    """One question as `window_cards.ask` was given it; buttons as (key, text, accent, danger)."""

    title: str
    body: str
    buttons: list
    default: str
    then: Callable[[str], None]
    alternates: list
    back: str
    unasked: bool = False  # QiTV says it by itself: it waits while the mini player shows


class Asked:
    """`window_cards.ask` recorded (no window): each question, its buttons and its callback."""

    def __init__(self):
        self.cards: list[Card] = []
        self._patch = mock.patch.object(window_cards, "ask", side_effect=self._ask)

    def _ask(
        self, title, body, buttons, default, then, *, alternates=(), back=CANCEL, unasked=False
    ):
        self.cards.append(
            Card(
                title,
                body,
                [(b.key, b.text, b.accent, b.danger) for b in buttons],
                default,
                then,
                [(b.key, b.text) for b in alternates],
                back,
                unasked,
            )
        )

    def only(self) -> Card:
        assert len(self.cards) == 1, self.cards
        return self.cards[0]

    def __enter__(self):
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        return False


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
                pause(100)  # retried every 25 ms meanwhile
                self.assertFalse(process.Popen.called)
                self.pending[0] = False
            waited = 0
            while not process.Popen.called and waited < 2000:
                pause(25)
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

    def test_the_updates_copy_shows_no_bootloader_splash(self):
        # An update's copy shows no splash (main.wants_splash): nor its bootloader's, which
        # would stand on top for its whole unpack, before its Python could close it.
        self.pending[0] = False
        process, _close_all = self.update()
        env = process.Popen.call_args[1]["env"]
        self.assertEqual(env["PYINSTALLER_SUPPRESS_SPLASH_SCREEN"], "1")
        self.assertEqual(env.get("PATH"), os.environ.get("PATH"))  # the rest as it is
        self.assertNotIn("PYINSTALLER_SUPPRESS_SPLASH_SCREEN", os.environ)  # this QiTV's own

    def test_an_updates_copy_updates_the_installed_qitv_again(self):
        # A run that is itself an update's copy (it replaced the installed QiTV and runs on
        # from Downloads): a newer update replaces the installed QiTV again, never the copy.
        from services import file_types

        installed = "C:\\Users\\Ayşe\\QiTV\\qitv-windows.exe"
        self.addCleanup(file_types.use_installed, None)
        file_types.use_installed(installed)
        self.pending[0] = False  # no background thread to wait for
        process, _close_all = self.update()
        self.assertEqual(
            process.Popen.call_args[0][0], ["C:/Downloads/qitv.exe", "--replace", installed]
        )

    def test_a_launch_that_fails_runs_the_undo_hooks_before_the_error_dialog(self):
        holder = Holder(self.calls, self.pending)
        update_checker.call_before_update(holder.stop_live, holder.restart_live)

        def dialog(*_args, **_kwargs):
            self.calls.append("dialog")

        with (
            mock.patch.object(update_checker, "has_pending_threads", return_value=False),
            mock.patch.object(update_checker, "subprocess") as process,
            mock.patch.object(window_cards, "ask", side_effect=dialog),
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
        self.ui.confirm_quit_and_open.side_effect = lambda then: self.calls.append("asked") or then(
            True
        )
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
                pause(25)
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
        self.ui.confirm_quit_and_open.side_effect = lambda then: then(False)
        process = self.update(until=None)
        process.Popen.assert_not_called()
        self.assertEqual(self.calls, [])  # no hook ran, nothing closed
        self.ui.downloaded.assert_not_called()

    def test_a_recording_is_asked_about_before_the_update_quits(self):
        note = "One is recording. Quit and stop the recording?"
        update_checker.set_quit_note(lambda: note)
        self.addCleanup(update_checker.set_quit_note, None)
        self.ui.confirm_stop_recordings.side_effect = lambda text, then: self.calls.append(
            text
        ) or then(False)
        process = self.update(until=None)
        self.assertEqual(self.calls, ["asked", note])  # Keep recording: nothing else
        process.Popen.assert_not_called()
        self.calls.clear()
        self.ui.confirm_stop_recordings.side_effect = lambda text, then: self.calls.append(
            text
        ) or then(True)
        self.update()
        self.assertEqual(self.calls, ["asked", note, "stop_live", "open", "close"])

    def test_the_update_waits_for_the_answers_however_late_they_come(self):
        # 2.13.1: the questions are cards: nothing waits in an exec(), the update goes on once
        # each is answered (the recording question after Update Now).
        note = "One is recording. Quit and stop the recording?"
        update_checker.set_quit_note(lambda: note)
        self.addCleanup(update_checker.set_quit_note, None)
        answers = []
        self.ui.confirm_quit_and_open.side_effect = answers.append
        self.ui.confirm_stop_recordings.side_effect = lambda _text, then: answers.append(then)
        process = self.update(until=None)
        self.ui.confirm_stop_recordings.assert_not_called()  # not before Update Now
        self.assertEqual(len(answers), 1)
        process.Popen.assert_not_called()
        self.pending[0] = False
        with (
            mock.patch.object(update_checker.platform, "system", return_value="Darwin"),
            mock.patch.object(update_checker, "has_pending_threads", return_value=False),
            mock.patch.object(update_checker, "subprocess") as process,
            mock.patch.object(
                self.app, "closeAllWindows", side_effect=lambda: self.calls.append("close")
            ),
        ):
            process.Popen.return_value = self.opened
            answers.pop()(True)  # Update Now
            self.ui.confirm_stop_recordings.assert_called_once()
            process.Popen.assert_not_called()  # the recordings' answer first
            answers.pop()(True)  # Quit
            self.assertTrue(wait_until(lambda: "close" in self.calls))
        self.assertEqual(process.Popen.call_args[0][0], ["open", self.PATH])

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

    def test_the_card_says_what_happens_and_answers_update_now(self):
        answers = []
        with Asked() as asked:
            update_dialogs.DIALOGS.confirm_quit_and_open(answers.append)
        card = asked.only()
        self.assertEqual(card.title, "Update Downloaded")
        self.assertIn("QiTV will close and open the update.", card.body)
        self.assertIn("Replace", card.body)
        self.assertEqual(
            card.buttons,
            [("not_now", "Not Now", False, False), ("update", "Update Now", True, False)],
        )
        self.assertEqual((card.default, card.back), ("update", "not_now"))
        card.then("update")
        card.then("not_now")
        self.assertEqual(answers, [True, False])


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

    def test_every_download_is_a_new_file(self):
        # Written under another name, then renamed over the last one: never rewritten.
        for name in ("qitv-macos-universal.zip", "qitv-windows.exe", "qitv-linux"):
            with self.subTest(name):
                self.assertEqual(self.download(name), b"old")


class DownloadFolderTests(unittest.TestCase):
    """2.7.1: macOS asks before an app touches the Downloads folder, and an ad-hoc signed
    app's Allow may not stick (a friend's Mac asked again and again): there an update
    downloads to QiTV's own cache folder, which macOS does not guard. Elsewhere the Downloads
    folder is the system's: a Linux desktop in another language names it in that language."""

    def folder(self, system, downloads="Downloads"):
        with (
            tempfile.TemporaryDirectory() as home,
            mock.patch.object(update_checker.Path, "home", return_value=Path(home)),
            mock.patch.object(update_checker.platform, "system", return_value=system),
            mock.patch.object(
                update_checker.QStandardPaths,
                "writableLocation",
                return_value=str(Path(home, downloads)) if downloads else "",
            ),
        ):
            folder = update_checker.get_downloads_folder()
            return folder.relative_to(home), folder.is_dir()

    def test_on_macos_an_update_downloads_to_qitvs_cache(self):
        self.assertEqual(self.folder("Darwin"), (Path("Library/Caches/QiTV/Updates"), True))

    def test_elsewhere_to_the_systems_downloads_folder_made_when_missing(self):
        for system in ("Windows", "Linux"):
            for name in ("Downloads", "İndirilenler", "Téléchargements"):
                with self.subTest(system=system, folder=name):
                    self.assertEqual(self.folder(system, name), (Path(name), True))

    def test_without_one_to_the_home_folder(self):
        self.assertEqual(self.folder("Linux", downloads=""), (Path("."), True))
        with tempfile.TemporaryDirectory() as home:
            blocker = Path(home, "file")
            blocker.write_bytes(b"")  # a file where the folder would be: it cannot be made
            with (
                mock.patch.object(update_checker.Path, "home", return_value=Path(home)),
                mock.patch.object(update_checker.platform, "system", return_value="Linux"),
                mock.patch.object(
                    update_checker.QStandardPaths,
                    "writableLocation",
                    return_value=str(blocker / "Downloads"),
                ),
                self.assertLogs("services.update_checker", "WARNING"),
            ):
                self.assertEqual(update_checker.get_downloads_folder(), Path(home))


class RunningCopyTests(unittest.TestCase):
    """A browser saves QiTV in Downloads and it runs from there: the update never writes over
    the running program (Windows: permission denied; Linux: text file busy)."""

    URL = "https://github.invalid/QiTV/releases/download/v9.9.9/qitv-windows.exe"

    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.running = self.folder / "qitv-windows.exe"
        self.running.write_bytes(b"running")

    def test_the_running_programs_name_takes_the_version(self):
        target = update_checker.download_target(
            self.folder, "qitv-windows.exe", self.URL, running=str(self.running)
        )
        self.assertEqual(target, self.folder / "qitv-windows-9.9.9.exe")
        url = "https://release.invalid/qitv-linux"
        (self.folder / "qitv-linux").write_bytes(b"")
        target = update_checker.download_target(
            self.folder, "qitv-linux", url, running=str(self.folder / "qitv-linux")
        )
        self.assertEqual(target, self.folder / "qitv-linux-new")  # no version in the address

    def test_another_file_of_that_name_is_replaced(self):
        elsewhere = self.folder / "Apps"
        elsewhere.mkdir()
        target = update_checker.download_target(
            self.folder, "qitv-windows.exe", self.URL, running=str(elsewhere / "qitv.exe")
        )
        self.assertEqual(target, self.running)

    def test_a_packaged_qitv_downloads_beside_itself(self):
        response = mock.MagicMock(headers={"content-length": "3"})
        response.iter_content.return_value = [b"new"]
        downloader = update_checker.UpdateDownloader(self.URL, 3)
        done = []
        downloader.finished.connect(done.append)
        with (
            mock.patch.object(update_checker, "get_downloads_folder", return_value=self.folder),
            mock.patch.object(update_checker, "requests") as requests,
            mock.patch.object(update_checker.sys, "frozen", True, create=True),
            mock.patch.object(update_checker.sys, "executable", str(self.running)),
        ):
            requests.get.return_value = response
            downloader.run()
        self.assertEqual(done, [str(self.folder / "qitv-windows-9.9.9.exe")])
        self.assertEqual(self.running.read_bytes(), b"running")
        self.assertEqual(Path(done[0]).read_bytes(), b"new")
        self.assertEqual(
            sorted(p.name for p in self.folder.iterdir()),
            sorted(["qitv-windows.exe", "qitv-windows-9.9.9.exe"]),
        )  # nothing half-written beside them

    def test_a_linux_download_can_start(self):
        response = mock.MagicMock(headers={"content-length": "3"})
        response.iter_content.return_value = [b"new"]
        downloader = update_checker.UpdateDownloader("https://release.invalid/qitv-linux", 3)
        done = []
        downloader.finished.connect(done.append)
        with (
            mock.patch.object(update_checker, "get_downloads_folder", return_value=self.folder),
            mock.patch.object(update_checker, "requests") as requests,
            mock.patch.object(update_checker.platform, "system", return_value="Linux"),
        ):
            requests.get.return_value = response
            downloader.run()
        self.assertTrue(os.access(done[0], os.X_OK))


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
            others = [path.name for path in Path(folder).iterdir() if path.name != name]
            self.assertEqual(others, [])  # nothing half-written stays
            return bool(done), bool(failed), Path(folder, name).exists()

    def test_a_cancelled_download_leaves_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            downloader = update_checker.UpdateDownloader("https://release.invalid/qitv-linux", 6)

            def chunks(**_options):
                yield b"abc"
                downloader.cancel()  # Cancel, halfway
                yield b"def"

            response = mock.MagicMock(headers={"content-length": "6"})
            response.iter_content.side_effect = chunks
            failed = []
            downloader.error.connect(failed.append)
            with (
                mock.patch.object(
                    update_checker, "get_downloads_folder", return_value=Path(folder)
                ),
                mock.patch.object(update_checker, "requests") as requests,
            ):
                requests.get.return_value = response
                downloader.run()
            self.assertEqual(failed, [update_checker.DOWNLOAD_CANCELLED])
            self.assertEqual(list(Path(folder).iterdir()), [])

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
        info = {
            "version": "9.9.9",
            "url": "https://release.invalid/qitv",
            "download_url": "https://release.invalid/qitv.zip",
            "file_size": 1,
        }
        with (
            Asked() as asked,
            mock.patch.object(update_dialogs, "start_download") as start_download,
            mock.patch.object(sys, "frozen", True, create=True),
            self.assertLogs("services.update_checker", "WARNING") as logs,
        ):
            update_dialogs.show_update_dialog(info, self.config, manual=True)
            asked.only().then("download")
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


class UpdateCardsTests(unittest.TestCase):
    """2.13.1: each update message is a card with the message box's texts and buttons, the
    main action accented; Back (Esc, the dimmed picture) decides nothing."""

    INFO = {
        "version": "9.9.9",
        "url": "https://release.invalid/qitv",
        "download_url": "https://release.invalid/qitv.zip",
        "file_size": 1,
    }

    def setUp(self):
        self.config = mock.MagicMock()
        self.config.skipped_update_version = "9.9.8"
        for name in ("skip_version", "disable_update_reminders", "save_settings"):
            patch = mock.patch.object(update_checker, name)
            setattr(self, name, patch.start())
            self.addCleanup(patch.stop)
        for name in ("open_url", "open_folder"):
            patch = mock.patch.object(update_dialogs.system_open, name)
            setattr(self, name, patch.start())
            self.addCleanup(patch.stop)
        patch = mock.patch.object(update_dialogs, "start_download")
        self.start_download = patch.start()
        self.addCleanup(patch.stop)

    def offer(self, *, frozen=True, manual=False):
        with Asked() as asked, mock.patch.object(sys, "frozen", frozen, create=True):
            update_dialogs.show_update_dialog(self.INFO, self.config, manual=manual)
        return asked.only()

    def nothing_done(self):
        for done in (
            self.skip_version,
            self.disable_update_reminders,
            self.open_url,
            self.start_download,
        ):
            done.assert_not_called()

    def test_an_update_offered_to_a_packaged_qitv(self):
        card = self.offer()
        self.assertEqual(card.title, "Update Available")
        self.assertEqual(
            card.body,
            "A new version (9.9.9) is available!\n"
            "Would you like to download and install the update?",
        )
        self.assertEqual(
            card.buttons,
            [
                ("skip", "Skip This Version", False, False),
                ("release", "Open Release Page", False, False),
                ("download", "Download & Install", True, False),  # "&&" painted as "&"
            ],
        )
        self.assertEqual(card.default, "download")
        # An automatic check's other way: its reminders off (the old box's check box with Skip).
        self.assertEqual(card.alternates, [("mute", "Do not remind me about updates")])
        self.assertTrue(card.unasked)  # the startup check's: it waits for the mini player
        card.then("download")
        self.start_download.assert_called_once_with(
            "https://release.invalid/qitv.zip", 1, "https://release.invalid/qitv"
        )
        self.assertEqual(self.config.skipped_update_version, "")

    def test_each_answer_and_back(self):
        card = self.offer()
        card.then(card.back)  # Esc, Back, the dimmed picture: offered again at the next check
        self.nothing_done()
        card.then("release")
        self.open_url.assert_called_once_with("https://release.invalid/qitv")
        card.then("skip")
        self.skip_version.assert_called_once_with(self.config, "9.9.9")
        card.then("mute")
        self.disable_update_reminders.assert_called_once_with(self.config)
        self.start_download.assert_not_called()

    def test_from_source_or_asked_for_it_offers_the_release_page(self):
        card = self.offer(frozen=False, manual=True)
        self.assertEqual(
            card.body,
            "A new version (9.9.9) is available!\nWould you like to open the release page?",
        )
        self.assertEqual(
            card.buttons,
            [
                ("skip", "Skip This Version", False, False),
                ("release", "Open Release Page", True, False),
            ],
        )
        self.assertEqual((card.default, card.alternates), ("release", []))  # asked: no mute

    def test_the_messages_say_it_with_ok(self):
        with (
            Asked() as asked,
            mock.patch.object(update_dialogs, "get_app_version", return_value="2.13.1"),
        ):
            update_dialogs.DIALOGS.up_to_date()
            update_dialogs.DIALOGS.check_failed("Connection refused")
            update_dialogs.replace_failed("C:/QiTV/qitv-windows.exe", "C:/Downloads/qitv.exe")
        ok = [("ok", "OK", True, False)]
        self.assertEqual(
            [(c.title, c.body, c.buttons) for c in asked.cards],
            [
                ("No Updates", "You're running the latest version (2.13.1).", ok),
                ("Update Check Failed", "Could not check for updates.\nConnection refused", ok),
                (
                    "Update Failed",
                    "The update could not replace the old version of QiTV.\n"
                    "This version runs from:\nC:/Downloads/qitv.exe\n\n"
                    "Use that file from now on, or close QiTV and copy it over the old one:\n"
                    "C:/QiTV/qitv-windows.exe",
                    ok,
                ),
            ],
        )

    def test_a_downloaded_update_opens_its_folder_once_said(self):
        with Asked() as asked:
            update_dialogs.DIALOGS.downloaded("/home/someone/Downloads/qitv-linux")
        card = asked.only()
        self.assertEqual(card.title, "Update Downloaded")
        self.assertTrue(card.body.startswith("Update downloaded successfully!\n"))
        self.assertIn("/home/someone/Downloads/qitv-linux", card.body)
        self.open_folder.assert_not_called()  # not before it was read
        card.then(card.back)
        self.open_folder.assert_called_once_with("/home/someone/Downloads")

    def test_a_failure_offers_the_release_page(self):
        with Asked() as asked:
            update_dialogs.DIALOGS.launch_failed("C:/Downloads/qitv.exe", "https://release")
            update_dialogs._show_download_error("Download failed: timed out", "https://release")
        close_release = [
            ("close", "Close", False, False),
            ("release", "Open Release Page", True, False),
        ]
        self.assertEqual(
            [(c.title, c.body, c.buttons, c.back) for c in asked.cards],
            [
                (
                    "Update Failed",
                    "Failed to launch the update.\n"
                    "The update was downloaded to:\nC:/Downloads/qitv.exe",
                    close_release,
                    "close",
                ),
                (
                    "Download Failed",
                    "Failed to download update.\nDownload failed: timed out",
                    close_release,
                    "close",
                ),
            ],
        )
        asked.cards[0].then("close")
        self.open_url.assert_not_called()
        asked.cards[1].then("release")
        self.open_url.assert_called_once_with("https://release")

    def test_quit_while_recording_keeps_recording_by_default(self):
        answers = []
        with Asked() as asked:
            update_dialogs.DIALOGS.confirm_stop_recordings(
                "Channel 1 is recording. Quit and stop the recording?", answers.append
            )
        card = asked.only()
        self.assertEqual(card.title, "Update Downloaded")
        self.assertEqual(card.body, "Channel 1 is recording. Quit and stop the recording?")
        self.assertEqual(
            card.buttons, [("keep", "Keep recording", False, False), ("quit", "Quit", False, True)]
        )
        self.assertEqual((card.default, card.back), ("keep", "keep"))
        card.then("keep")
        card.then("quit")
        self.assertEqual(answers, [False, True])


class DownloadCardTests(unittest.TestCase):
    """2.13.1: the download's progress is a card too (no system window): its bar, how much
    came, and Cancel, which stops it; Back takes the card away, the download going on."""

    URL, RELEASE = "https://release.invalid/qitv.dmg", "https://release.invalid/qitv"

    def setUp(self):
        self.handle = mock.MagicMock()  # the card's progress
        self.cancel = mock.MagicMock()
        patches = {
            "shown": mock.patch.object(window_cards, "progress", return_value=self.handle),
            "start": mock.patch.object(update_checker, "start_download", return_value=self.cancel),
            "perform": mock.patch.object(update_checker, "perform_update"),
            # none running, none downloaded before (this run's, module-wide)
            "running": mock.patch.object(update_dialogs, "_running", None),
            "downloaded": mock.patch.object(update_dialogs, "_downloaded", {}),
        }
        for name, patch in patches.items():
            setattr(self, name, patch.start())
            self.addCleanup(patch.stop)
        update_dialogs.start_download(self.URL, 1, self.RELEASE)
        self.assertEqual(self.start.call_args.args, (self.URL, 1))
        self.callbacks = self.start.call_args.kwargs

    def test_the_download_shows_its_progress_and_cancel(self):
        (title, text, buttons, default, then), kwargs = self.shown.call_args
        self.assertEqual((title, text), ("Downloading Update", "Downloading update..."))
        self.assertEqual(
            [(b.key, b.text, b.accent, b.danger) for b in buttons],
            [("stop", "Cancel", False, False)],
        )
        self.assertEqual((default, kwargs["back"]), ("stop", "hide"))
        self.callbacks["progress"](5 * 1024 * 1024, 10 * 1024 * 1024)
        self.handle.set.assert_called_once_with(0.5, "Downloading update... 5.0 / 10.0 MB")
        self.callbacks["progress"](1, 0)  # no size known: nothing to say
        self.assertEqual(self.handle.set.call_count, 1)
        then("hide")  # Back: the card goes, the download goes on
        self.cancel.assert_not_called()
        then("stop")  # Cancel
        self.cancel.assert_called_once_with()

    def test_once_downloaded_the_card_goes_and_the_update_goes_on(self):
        path = "/Users/someone/Library/Caches/QiTV/Updates/qitv.dmg"
        self.callbacks["finished"](path)
        self.handle.close.assert_called_once_with()
        self.perform.assert_called_once_with(
            path, self.RELEASE, update_dialogs.DIALOGS, ask_first=False
        )  # its card in sight to the end: as before

    def test_a_failed_download_says_so_a_cancelled_one_not(self):
        with Asked() as asked:
            self.callbacks["failed"](update_checker.DOWNLOAD_CANCELLED)
            self.assertEqual(asked.cards, [])
            self.callbacks["failed"]("Download failed: timed out")
        self.assertEqual(self.handle.close.call_count, 2)
        card = asked.only()
        self.assertEqual(card.title, "Download Failed")
        self.assertEqual(card.body, "Failed to download update.\nDownload failed: timed out")
        self.assertTrue(card.unasked)  # a download's result: it waits for the mini player

    def back(self):
        (_title, _text, _buttons, _default, then), _kwargs = self.shown.call_args
        then("hide")  # Back: the card goes, the download goes on

    def test_a_download_whose_card_went_asks_before_quitting_on_every_system(self):  # M1
        self.back()
        path = "C:/Users/someone/Downloads/qitv-windows.exe"
        self.callbacks["finished"](path)
        self.perform.assert_called_once_with(
            path, self.RELEASE, update_dialogs.DIALOGS, ask_first=True
        )

    def test_while_one_runs_another_brings_its_card_back_never_a_second_download(self):  # I2
        self.callbacks["progress"](5 * 1024 * 1024, 10 * 1024 * 1024)
        update_dialogs.start_download(self.URL, 1, self.RELEASE)  # its card in sight: nothing
        self.assertEqual(self.shown.call_count, 1)
        self.back()
        update_dialogs.start_download(self.URL, 1, self.RELEASE)  # Download & Install again
        self.assertEqual(self.start.call_count, 1)  # one download
        self.assertEqual(self.shown.call_count, 2)  # its card again, where it got to
        self.handle.set.assert_called_with(0.5, "Downloading update... 5.0 / 10.0 MB")
        self.back()
        info = {"version": "9.9.9", "url": self.RELEASE, "download_url": self.URL}
        with Asked() as asked:
            update_dialogs.show_update_dialog(info, None, manual=True)  # Help › Check again
        self.assertEqual(asked.cards, [])  # not offered again: its card instead
        self.assertEqual(self.shown.call_count, 3)
        self.assertEqual(self.start.call_count, 1)

    def test_an_update_downloaded_in_this_run_is_offered_downloaded(self):  # I2
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "qitv.dmg")
            Path(path).write_bytes(b"image")
            self.callbacks["finished"](path)  # then Not Now, say
            self.perform.reset_mock()
            info = {"version": "9.9.9", "url": self.RELEASE, "download_url": self.URL}
            with Asked() as asked:
                update_dialogs.show_update_dialog(info, None, manual=True)
            self.assertEqual(asked.cards, [])  # no offer to download it again
            self.perform.assert_called_once_with(
                path, self.RELEASE, update_dialogs.DIALOGS, ask_first=True
            )
            self.assertEqual(self.start.call_count, 1)
            # Once it ended, a new download may start
            update_dialogs.start_download("https://release.invalid/other.dmg", 1, self.RELEASE)
            self.assertEqual(self.start.call_count, 2)


class OneDownloadTests(unittest.TestCase):
    """2.13.1 review I2 and M1: never two downloads writing one file, and a Windows update
    whose download went on unseen asks before QiTV quits for it."""

    def test_each_download_has_a_part_file_of_its_own(self):
        target = Path("/tmp/qitv-windows.exe")
        first, second = update_checker.part_path(target), update_checker.part_path(target)
        self.assertNotEqual(first, second)
        for part in (first, second):
            self.assertEqual(part.parent, target.parent)
            self.assertTrue(part.name.startswith(f".{target.name}.{os.getpid()}."))

    def test_a_second_download_is_refused_while_one_runs(self):
        with (
            mock.patch.object(update_checker, "_download_jobs", [object()]),
            mock.patch.object(update_checker, "QThread") as thread,
            self.assertLogs("services.update_checker", "WARNING"),
        ):
            cancel = update_checker.start_download(
                "https://release.invalid/qitv.exe", 1, progress=print, finished=print, failed=print
            )
        thread.assert_not_called()
        cancel()  # cancels nothing

    def test_windows_asks_first_when_told_and_installs_only_on_update_now(self):
        ui = mock.MagicMock()
        with (
            mock.patch.object(update_checker.platform, "system", return_value="Windows"),
            mock.patch.object(update_checker, "_perform_windows_update") as install,
            mock.patch.object(update_checker, "_quit_note", None),
        ):
            update_checker.perform_update("C:/q.exe", "https://release", ui, ask_first=True)
            install.assert_not_called()
            (then,), _ = ui.confirm_quit_and_install.call_args
            then(False)  # Not Now: it waits in Downloads
            install.assert_not_called()
            then(True)
            install.assert_called_once_with("C:/q.exe", "https://release", ui)
            install.reset_mock()
            update_checker.perform_update("C:/q.exe", "https://release", ui)  # in sight: at once
            install.assert_called_once_with("C:/q.exe", "https://release", ui)
        self.assertEqual(ui.confirm_quit_and_install.call_count, 1)

    def test_the_windows_question_is_a_card_with_update_now_first(self):
        answers = []
        with Asked() as asked:
            update_dialogs.DIALOGS.confirm_quit_and_install(answers.append)
        card = asked.only()
        self.assertEqual(card.title, "Update Downloaded")
        self.assertEqual(card.body, "QiTV will close and open the update.")
        self.assertEqual(
            card.buttons,
            [("not_now", "Not Now", False, False), ("update", "Update Now", True, False)],
        )
        self.assertEqual((card.default, card.back, card.unasked), ("update", "not_now", True))
        card.then("not_now")
        card.then("update")
        self.assertEqual(answers, [False, True])


class LayoutTests(unittest.TestCase):
    """services/ holds the update logic; its dialogs are ui/'s."""

    def test_the_service_builds_no_widgets(self):
        source = Path(update_checker.__file__).read_text(encoding="utf-8")
        self.assertNotIn("QtWidgets", source)
        for name in ("QMessageBox", "QProgressDialog", "QCheckBox", "webbrowser"):
            self.assertFalse(hasattr(update_checker, name), name)

    def test_no_system_window_opens(self):
        # 2.13.1: every update message, and the download's progress, is QiTV's own card
        # (ui.window_cards); nothing waits in an exec().
        source = Path(update_dialogs.__file__).read_text(encoding="utf-8")
        for name in ("QMessageBox", "QInputDialog", "QCheckBox", "QProgressDialog", ".exec"):
            self.assertNotIn(name, source, name)

    def test_the_dialogs_check_through_the_service(self):
        with mock.patch.object(update_checker, "check_for_updates") as check:
            update_dialogs.check_for_updates(None, manual=True)
        check.assert_called_once_with(None, True, update_dialogs.DIALOGS)


if __name__ == "__main__":
    unittest.main()
