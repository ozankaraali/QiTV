"""Settings → General → Open videos with QiTV, and Open music with QiTV
(ui/file_types_settings.py): each row's button registers QiTV for its kind of file, or takes it
back, on a worker; its hint follows the system and the result. The rows are independent. And
at start, a QiTV that was moved registers itself again (main.py)."""

from pathlib import Path
import threading
import unittest
from unittest import mock

from PySide6.QtCore import QCoreApplication, QEvent
from shiboken6 import isValid
from test_dde_windows import FakeDdeml
from timer_fixtures import wait_until

from services import dde_windows, file_types
from services.config_manager import ConfigManager
from services.file_types import RegisterResult, Status
from services.i18n import translate
from tests import test_tv_window as base
from ui import file_types_settings as row_module
from ui.file_types_settings import (
    FAILED_HINT,
    LINUX_HINT,
    LINUX_OPEN_WITH_HINT,
    MAC_HINT,
    MAC_REGISTERED_HINT,
    MAC_TEMPORARY_HINT,
    STOP_TEXT,
    TEMPORARY_HINT,
    USE_TEXT,
    WINDOWS_HINT,
    refresh_at_start,
)
from ui.settings_dialog import OptionsDialog

REPO = Path(__file__).resolve().parent.parent
EXE = "/opt/QiTV/qitv"
SYSTEMS = ("win32", "darwin", "linux")


def recorded_status(kind, config, frozen=None):
    """As the service answers in a packaged QiTV: from the record."""
    return Status.REGISTERED if kind in config.file_types else Status.NOT_REGISTERED


def worker(name):
    return [t for t in threading.enumerate() if t.name == name]


def join_default_apps():
    for thread in worker(row_module.DEFAULT_APPS_THREAD):
        thread.join(5)


class RowCase(base.WindowCase):
    """Settings' row on a real window: its dialog, a press and its answer."""

    def setUp(self):
        super().setUp()
        self.threads: list[threading.Thread] = []

    KIND = "videos"
    ROW = "file_types"  # the dialog's attributes: file_types_button, file_types_hint, ...
    LABEL = "Open videos with QiTV"
    USE, STOP, MAC, LINUX = USE_TEXT, STOP_TEXT, MAC_HINT, LINUX_HINT
    MAC_SHORT_LIST: tuple[str, ...] = ("mkv", "mp4", "mov", "avi", "webm")

    def button(self, dialog):
        return getattr(dialog, f"{self.ROW}_button")

    def hint_label(self, dialog):
        return getattr(dialog, f"{self.ROW}_hint")

    def default_apps(self, dialog):
        return getattr(dialog, f"{self.ROW}_default_apps_button")

    def dialog(self, state=None, system="linux"):
        status = (
            mock.patch.object(file_types, "status", return_value=state)
            if state is not None
            else mock.patch.object(file_types, "status", side_effect=recorded_status)
        )
        with status, mock.patch.object(row_module, "_system", return_value=system):
            dialog = OptionsDialog(self.window)
        self.addCleanup(lambda: isValid(dialog) and dialog.deleteLater())
        return dialog

    def hint(self, dialog) -> str:
        label = self.hint_label(dialog)
        return "" if label.isHidden() else label.text()

    def press(self, dialog, system="linux"):
        """A click, its answer delivered (the status as the record says, on `system`)."""
        with (
            mock.patch.object(file_types, "status", side_effect=recorded_status),
            mock.patch.object(row_module, "_system", return_value=system),
        ):
            self.button(dialog).click()
            self.assertTrue(wait_until(self.button(dialog).isEnabled))
            for thread in worker(row_module.PRESS_THREAD):
                thread.join(5)
            join_default_apps()  # opened after a Windows register, on a thread of its own

    def registering(self, result=RegisterResult(True, True), gate=None):
        """A register that records as the service does, and the thread it ran on."""

        def register(kind, config, *args, **kwargs):
            self.threads.append(threading.current_thread())
            if gate is not None:
                gate.wait(5)
            if result:
                config.file_types = {**config.file_types, kind: EXE}
            return result

        return mock.patch.object(file_types, "register", side_effect=register)


class FileTypesRowTests(RowCase):
    def test_row_follows_status(self):
        expected = {
            (Status.NOT_REGISTERED, "win32"): (self.USE, WINDOWS_HINT, True),
            (Status.NOT_REGISTERED, "darwin"): (self.USE, self.MAC, True),
            (Status.NOT_REGISTERED, "linux"): (self.USE, self.LINUX, True),
            (Status.REGISTERED, "win32"): (self.STOP, None, True),
            (Status.REGISTERED, "darwin"): (self.STOP, MAC_REGISTERED_HINT, True),
            (Status.REGISTERED, "linux"): (self.STOP, None, True),
            (Status.TEMPORARY, "win32"): (self.USE, TEMPORARY_HINT, False),
            (Status.TEMPORARY, "darwin"): (self.USE, MAC_TEMPORARY_HINT, False),
            (Status.TEMPORARY, "linux"): (self.USE, TEMPORARY_HINT, False),
        }
        for (state, system), (button, hint, enabled) in expected.items():
            with self.subTest(state=state, system=system):
                dialog = self.dialog(state, system)
                layout = dialog.settings_layout
                self.assertTrue(layout.isRowVisible(self.button(dialog).parentWidget()))
                self.assertEqual(self.button(dialog).text(), translate(button))
                self.assertEqual(self.button(dialog).isEnabled(), enabled)
                self.assertEqual(self.hint(dialog), translate(hint) if hint else "")
                default_apps = self.default_apps(dialog)
                windows_registered = (state, system) == (Status.REGISTERED, "win32")
                self.assertEqual(not default_apps.isHidden(), windows_registered)
        dialog = self.dialog(Status.UNAVAILABLE)  # a source run: no row at all
        self.assertFalse(dialog.settings_layout.isRowVisible(self.button(dialog).parentWidget()))

    def test_windows_default_apps_stays_reachable_once_registered(self):  # Ruling 26
        dialog = self.dialog(Status.REGISTERED, "win32")
        button = self.default_apps(dialog)
        self.assertEqual(button.text(), "Default Apps…")
        self.assertFalse(button.autoDefault())
        opened_on = []
        with mock.patch.object(
            file_types,
            "open_default_apps",
            side_effect=lambda: opened_on.append(threading.current_thread()),
        ) as default_apps:
            button.click()
            join_default_apps()
        default_apps.assert_called_once_with()
        # Review M6: ShellExecute (os.startfile) may stall: never on the GUI thread
        self.assertIsNot(opened_on[0], threading.main_thread())
        self.assertEqual(opened_on[0].name, row_module.DEFAULT_APPS_THREAD)
        self.assertEqual(self.button(dialog).text(), translate(self.STOP))  # no press

    def test_default_apps_goes_once_windows_stops_using_qitv(self):
        self.config.file_types = {self.KIND: EXE}
        dialog = self.dialog(system="win32")
        self.assertFalse(self.default_apps(dialog).isHidden())

        def unregister(kind, config, *args, **kwargs):
            config.file_types = {}
            return True

        with mock.patch.object(file_types, "unregister", side_effect=unregister):
            self.press(dialog, "win32")
        self.assertTrue(self.default_apps(dialog).isHidden())

    def above(self, dialog):
        """What the row sits right under."""
        return dialog.start_last_channel_checkbox

    def test_the_row_sits_where_it_belongs(self):
        dialog = self.dialog(Status.NOT_REGISTERED)
        layout = dialog.settings_layout
        row, _role = layout.getWidgetPosition(self.button(dialog).parentWidget())
        above, _role = layout.getWidgetPosition(self.above(dialog))
        self.assertEqual(row, above + 1)
        self.assertEqual(
            layout.labelForField(self.button(dialog).parentWidget()).text(), self.LABEL
        )
        self.assertFalse(self.button(dialog).autoDefault())  # Enter still saves

    def test_the_mac_hint_names_every_type_macos_takes(self):
        for ext in self.MAC_SHORT_LIST:
            self.assertIn(ext, translate(self.MAC).lower())

    def test_press_runs_off_gui_thread_then_updates(self):
        dialog = self.dialog()
        saved = []
        save = self.config.save_config
        with (
            self.registering(),
            mock.patch.object(
                self.config,
                "save_config",
                side_effect=lambda **kw: (saved.append(threading.current_thread()), save(**kw)),
            ),
        ):
            self.press(dialog)
        self.assertEqual(len(self.threads), 1)
        self.assertIsNot(self.threads[0], threading.main_thread())
        self.assertEqual(self.button(dialog).text(), translate(self.STOP))
        self.assertEqual(saved, [threading.main_thread()])  # saved on the GUI thread
        self.assertEqual(ConfigManager().file_types, {self.KIND: EXE})

    def test_stop_using_unregisters_off_the_gui_thread(self):
        self.config.file_types = {self.KIND: EXE}
        dialog = self.dialog()
        self.assertEqual(self.button(dialog).text(), translate(self.STOP))

        def unregister(kind, config, *args, **kwargs):
            self.threads.append(threading.current_thread())
            config.file_types = {}
            return True

        with mock.patch.object(file_types, "unregister", side_effect=unregister):
            self.press(dialog)
        self.assertIsNot(self.threads[0], threading.main_thread())
        self.assertEqual(self.button(dialog).text(), translate(self.USE))
        self.assertEqual(self.hint(dialog), translate(self.LINUX))
        self.assertEqual(ConfigManager().file_types, {})

    def test_windows_opens_default_apps_after_register(self):
        for system in SYSTEMS:
            with self.subTest(system=system):
                self.config.file_types = {}
                dialog = self.dialog(system=system)
                opened_on = []
                with (
                    self.registering(),
                    mock.patch.object(
                        file_types,
                        "open_default_apps",
                        side_effect=lambda: opened_on.append(threading.current_thread()),
                    ) as default_apps,
                ):
                    self.press(dialog, system)
                self.assertEqual(default_apps.call_count, 1 if system == "win32" else 0)
                for thread in opened_on:  # never on the GUI thread (Review M6)
                    self.assertIsNot(thread, threading.main_thread())

    def test_a_failed_press_says_so_and_can_be_tried_again(self):
        dialog = self.dialog()
        with (
            self.registering(RegisterResult(False)),
            mock.patch.object(file_types, "open_default_apps") as default_apps,
            mock.patch.object(self.config, "save_config") as save,
        ):
            self.press(dialog, "win32")
        self.assertEqual(self.hint(dialog), "Couldn't change it. See the log.")
        self.assertIn("color", self.hint_label(dialog).styleSheet())  # an error's colour
        self.assertEqual(self.button(dialog).text(), translate(self.USE))
        self.assertTrue(self.button(dialog).isEnabled())
        default_apps.assert_not_called()
        save.assert_not_called()
        with self.registering():  # a second try that works: the error goes
            self.press(dialog, "win32")
        self.assertEqual(self.hint(dialog), "")
        self.assertEqual(self.hint_label(dialog).styleSheet(), "")

    def test_a_service_that_raises_still_answers(self):
        dialog = self.dialog()
        with (
            mock.patch.object(file_types, "register", side_effect=RuntimeError("boom")),
            self.assertLogs("ui.file_types_settings", "WARNING"),
        ):
            self.press(dialog)
        self.assertEqual(self.hint(dialog), translate(FAILED_HINT))
        self.assertTrue(self.button(dialog).isEnabled())

    def test_linux_open_with_only_says_so_for_this_session(self):
        dialog = self.dialog()
        with self.registering(RegisterResult(True, False)):  # no xdg-mime: no default taken
            self.press(dialog)
        self.assertEqual(self.button(dialog).text(), translate(self.STOP))
        self.assertEqual(self.hint(dialog), translate(LINUX_OPEN_WITH_HINT))
        self.assertEqual(self.button(self.dialog()).text(), translate(self.STOP))
        self.assertEqual(self.hint(self.dialog()), "")  # the next Settings: no extra hint

    def test_the_button_waits_while_a_press_runs(self):
        dialog = self.dialog()
        gate = threading.Event()
        with self.registering(gate=gate):
            with mock.patch.object(file_types, "status", side_effect=recorded_status):
                self.button(dialog).click()
                self.assertFalse(self.button(dialog).isEnabled())
                self.button(dialog).click()  # disabled: nothing more starts
            gate.set()
            self.press_answered(dialog)
        self.assertEqual(len(self.threads), 1)
        self.assertEqual(self.button(dialog).text(), translate(self.STOP))

    def press_answered(self, dialog):
        with (
            mock.patch.object(file_types, "status", side_effect=recorded_status),
            mock.patch.object(row_module, "_system", return_value="linux"),
        ):
            self.assertTrue(wait_until(self.button(dialog).isEnabled))

    def test_closing_settings_mid_press_still_saves(self):
        dialog = self.dialog()
        gate = threading.Event()
        with (
            self.registering(gate=gate),
            mock.patch.object(file_types, "status", side_effect=recorded_status),
            mock.patch.object(row_module, "_system", return_value="linux"),
            mock.patch.object(file_types, "open_default_apps"),
        ):
            self.button(dialog).click()
            dialog.done(0)  # Settings closed while the system is told
            gate.set()
            self.assertTrue(wait_until(lambda: ConfigManager().file_types == {self.KIND: EXE}))

    def test_settings_gone_mid_press_breaks_nothing(self):
        dialog = self.dialog()
        gate = threading.Event()
        with (
            self.registering(gate=gate),
            mock.patch.object(threading, "excepthook") as raised,
        ):
            with mock.patch.object(file_types, "status", side_effect=recorded_status):
                self.button(dialog).click()
            dialog.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.assertFalse(isValid(dialog))
            gate.set()
            for thread in worker(row_module.PRESS_THREAD):
                thread.join(5)
            QCoreApplication.processEvents()
        raised.assert_not_called()  # the worker's answer found no one: nothing raised
        self.assertEqual(self.config.file_types, {self.KIND: EXE})  # the service recorded it


class MusicFileTypesRowTests(FileTypesRowTests):
    KIND = "music"
    ROW = "music_file_types"
    LABEL = "Open music with QiTV"
    USE = "Use QiTV for music"
    STOP = "Stop using QiTV for music"
    MAC = "QiTV becomes the default for MP3, M4A and FLAC."
    LINUX = "QiTV becomes the default music player."
    MAC_SHORT_LIST = ("mp3", "m4a", "flac")

    def above(self, dialog):
        return dialog.file_types_button.parentWidget()  # right under the videos' row


class TwoRowsTests(RowCase):
    """Videos and music: each row says and does its own kind only."""

    def rows(self, dialog):
        return dialog.file_types_button, dialog.music_file_types_button

    def press_row(self, button, work, system="linux"):
        with (
            work,
            mock.patch.object(file_types, "status", side_effect=recorded_status),
            mock.patch.object(row_module, "_system", return_value=system),
        ):
            button.click()
            self.assertTrue(wait_until(button.isEnabled))
            for thread in worker(row_module.PRESS_THREAD):
                thread.join(5)

    def unregistering(self):
        def unregister(kind, config, *args, **kwargs):
            config.file_types = {k: v for k, v in config.file_types.items() if k != kind}
            return True

        return mock.patch.object(file_types, "unregister", side_effect=unregister)

    def test_rows_are_independent(self):
        dialog = self.dialog()
        videos, music = self.rows(dialog)
        self.assertEqual(
            (videos.text(), music.text()), ("Use QiTV for videos", "Use QiTV for music")
        )
        self.press_row(music, self.registering())
        self.assertEqual(
            (videos.text(), music.text()), ("Use QiTV for videos", "Stop using QiTV for music")
        )
        self.assertEqual(self.config.file_types, {"music": EXE})
        self.press_row(videos, self.registering())
        self.press_row(music, self.unregistering())
        self.assertEqual(
            (videos.text(), music.text()), ("Stop using QiTV for videos", "Use QiTV for music")
        )
        self.assertEqual(ConfigManager().file_types, {"videos": EXE})  # saved as it is now
        # a new Settings window reads each row from the record
        videos, music = self.rows(self.dialog())
        self.assertEqual(
            (videos.text(), music.text()), ("Stop using QiTV for videos", "Use QiTV for music")
        )

    def test_one_row_failing_leaves_the_other(self):
        self.config.file_types = {"videos": EXE}
        dialog = self.dialog()
        videos, music = self.rows(dialog)
        self.press_row(music, self.registering(RegisterResult(False)))
        self.assertEqual(dialog.music_file_types_hint.text(), translate(FAILED_HINT))
        self.assertTrue(dialog.file_types_hint.isHidden())  # the videos' row says nothing
        self.assertEqual(videos.text(), "Stop using QiTV for videos")

    def test_each_row_has_its_own_widgets(self):
        dialog = self.dialog(Status.REGISTERED, "win32")
        names = {
            widget.objectName()
            for widget in (
                dialog.file_types_button,
                dialog.file_types_hint,
                dialog.file_types_default_apps_button,
                dialog.music_file_types_button,
                dialog.music_file_types_hint,
                dialog.music_file_types_default_apps_button,
            )
        }
        self.assertEqual(len(names), 6)
        self.assertIs(dialog.file_types_row.button, dialog.file_types_button)
        self.assertIs(dialog.music_file_types_row.button, dialog.music_file_types_button)


class DdeFollowsSettingsTests(RowCase):
    """Ruling 27: on Windows, Settings' press turns the running QiTV's DDE on, or off."""

    def setUp(self):
        super().setUp()
        self.ddeml = FakeDdeml()
        self.added: list[str] = []
        patches = (
            mock.patch.object(dde_windows, "_user32", side_effect=lambda: self.ddeml),
            mock.patch.object(
                file_types,
                "add_ddeexec",
                side_effect=lambda exe, wanted=None: self.added.append(exe),
            ),
            mock.patch.object(file_types, "remove_ddeexec", return_value=True),
            mock.patch.object(file_types, "open_default_apps"),
        )
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def serving(self):
        """The run's DDE owner, as main() gives the window on Windows (not registered yet)."""
        serving = self.window.dde = dde_windows.Serving(exe=EXE)
        self.addCleanup(serving.close)
        return serving

    def settle(self):
        for thread in worker(dde_windows.WRITER_THREAD):
            thread.join(5)

    def test_windows_register_starts_the_server_and_writes_the_key(self):
        serving = self.serving()
        dialog = self.dialog(system="win32")
        with self.registering():
            self.press(dialog, "win32")
        self.settle()
        self.assertTrue(serving.running)
        self.assertEqual(self.added, [EXE])  # on a worker
        # a second register while it runs (QiTV unregistered behind its back, say): the same
        # server, the key written over (Ruling 28c: a registration rewrote the ProgID)
        self.config.file_types = {}
        dialog = self.dialog(system="win32")
        with self.registering():
            self.press(dialog, "win32")
        self.settle()
        self.assertEqual(self.ddeml.calls.count("DdeInitializeW"), 1)  # no second server
        self.assertEqual(self.added, [EXE, EXE])

    def test_a_closing_window_follows_no_more(self):
        serving = self.serving()
        self.window._closing = True  # QiTV is closing: Settings' late answer starts nothing
        try:
            self.window.dde_follow_registration(True)
        finally:
            self.window._closing = False
        self.settle()
        self.assertFalse(serving.running)
        self.assertEqual(self.ddeml.calls, [])
        self.assertEqual(self.added, [])
        self.window.dde_follow_registration(True)  # open again: it follows
        self.settle()
        self.assertTrue(serving.running)

    def test_unregister_stops_the_server(self):
        serving = self.serving()
        self.assertTrue(serving.follow(True))
        self.config.file_types = {"videos": EXE}
        dialog = self.dialog(system="win32")

        def unregister(kind, config, *args, **kwargs):
            config.file_types = {}
            return True

        with mock.patch.object(file_types, "unregister", side_effect=unregister):
            self.press(dialog, "win32")
        self.assertFalse(serving.running)
        self.assertEqual(self.ddeml.calls.count("DdeUninitialize"), 1)

    def press_row_music(self, dialog, system):
        with (
            self.registering(),
            mock.patch.object(file_types, "status", side_effect=recorded_status),
            mock.patch.object(row_module, "_system", return_value=system),
        ):
            dialog.music_file_types_button.click()
            self.assertTrue(wait_until(dialog.music_file_types_button.isEnabled))
        self.assertEqual(dialog.music_file_types_button.text(), "Stop using QiTV for music")

    def test_a_failed_press_leaves_it_as_it_was(self):
        serving = self.serving()
        dialog = self.dialog(system="win32")
        with self.registering(RegisterResult(False)):
            self.press(dialog, "win32")
        self.settle()
        self.assertFalse(serving.running)
        self.assertEqual(self.ddeml.calls, [])
        self.assertEqual(self.added, [])

    def test_registering_music_starts_the_server(self):
        serving = self.serving()
        dialog = self.dialog(system="win32")
        with self.registering():
            with (
                mock.patch.object(file_types, "status", side_effect=recorded_status),
                mock.patch.object(row_module, "_system", return_value="win32"),
            ):
                dialog.music_file_types_button.click()
                self.assertTrue(wait_until(dialog.music_file_types_button.isEnabled))
        self.settle()
        self.assertTrue(serving.running)
        self.assertEqual(self.added, [EXE])

    def test_stopping_one_kind_keeps_serving_the_other(self):
        serving = self.serving()
        self.assertTrue(serving.follow(True))
        self.settle()
        self.added.clear()
        self.config.file_types = {"videos": EXE, "music": EXE}
        dialog = self.dialog(system="win32")

        def unregister(kind, config, *args, **kwargs):
            config.file_types = {k: v for k, v in config.file_types.items() if k != kind}
            return True

        def stop(button):
            with (
                mock.patch.object(file_types, "unregister", side_effect=unregister),
                mock.patch.object(file_types, "status", side_effect=recorded_status),
                mock.patch.object(row_module, "_system", return_value="win32"),
            ):
                button.click()
                self.assertTrue(wait_until(button.isEnabled))
            self.settle()

        stop(dialog.music_file_types_button)  # videos still double-click into this QiTV
        self.assertTrue(serving.running)
        self.assertEqual(self.ddeml.calls.count("DdeUninitialize"), 0)
        stop(dialog.file_types_button)  # none left: it stops
        self.assertFalse(serving.running)
        self.assertEqual(self.ddeml.calls.count("DdeUninitialize"), 1)

    def test_elsewhere_the_press_has_no_dde(self):
        self.assertIsNone(self.window.dde)  # main() gives it one on Windows only
        for system in ("darwin", "linux"):
            with self.subTest(system=system):
                self.config.file_types = {}
                dialog = self.dialog(system=system)
                with self.registering():
                    self.press(dialog, system)
                self.assertEqual(dialog.file_types_button.text(), translate(STOP_TEXT))
                self.press_row_music(dialog, system)
        self.assertEqual(self.ddeml.calls, [])
        self.assertEqual(self.added, [])


class StartRefreshTests(base.WindowCase):
    def refreshing(self, moved_to=None):
        self.threads: list[threading.Thread] = []

        def refresh(config, *args, **kwargs):
            self.threads.append(threading.current_thread())
            if moved_to:
                config.file_types = {"videos": moved_to}

        return mock.patch.object(file_types, "refresh", side_effect=refresh)

    def settle(self):
        for thread in worker(row_module.REFRESH_THREAD):
            thread.join(5)
        QCoreApplication.processEvents()

    def test_a_moved_qitv_registers_again_at_start_and_saves(self):
        self.config.file_types = {"videos": "/old/qitv"}
        self.config.save_config()
        with self.refreshing("/new/qitv"):
            refresh_at_start(self.config, self.window, frozen=True)
            self.assertTrue(
                wait_until(lambda: ConfigManager().file_types == {"videos": "/new/qitv"})
            )
        self.assertIsNot(self.threads[0], threading.main_thread())

    def test_nothing_changed_saves_nothing(self):
        self.config.file_types = {"videos": "/opt/qitv"}
        with self.refreshing(), mock.patch.object(self.config, "save_config") as save:
            refresh_at_start(self.config, self.window, frozen=True)
            self.settle()
        self.assertEqual(len(self.threads), 1)
        save.assert_not_called()

    def test_music_alone_registers_again_too(self):
        self.config.file_types = {"music": "/opt/qitv"}
        with self.refreshing(), mock.patch.object(self.config, "save_config"):
            refresh_at_start(self.config, self.window, frozen=True)
            self.settle()
        self.assertEqual(len(self.threads), 1)

    def test_never_from_source_nor_with_nothing_recorded(self):
        with self.refreshing():
            self.config.file_types = {"videos": "/opt/qitv"}
            refresh_at_start(self.config, self.window)  # the tests are a source run
            self.config.file_types = {}
            refresh_at_start(self.config, self.window, frozen=True)
            self.settle()
        self.assertEqual(self.threads, [])

    def test_main_refreshes_once_the_window_shows(self):
        body = (REPO / "main.py").read_text(encoding="utf-8").split("def main()", 1)[1]
        self.assertLess(
            body.index("window.show()"), body.index("refresh_at_start(config_manager, window)")
        )
        self.assertLess(
            body.index("refresh_at_start(config_manager, window)"), body.index("app.exec()")
        )


if __name__ == "__main__":
    unittest.main()
