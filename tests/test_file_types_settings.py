"""Settings → General → Open videos with QiTV (ui/file_types_settings.py): one button registers
QiTV for video files, or takes it back, on a worker; the hint follows the system and the result.
And at start, a QiTV that was moved registers itself again (main.py)."""

from pathlib import Path
import threading
import unittest
from unittest import mock

from PySide6.QtCore import QCoreApplication, QEvent
from shiboken6 import isValid
from timer_fixtures import wait_until

from services import file_types
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


class FileTypesRowTests(base.WindowCase):
    def setUp(self):
        super().setUp()
        self.threads: list[threading.Thread] = []

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
        label = dialog.file_types_hint
        return "" if label.isHidden() else label.text()

    def press(self, dialog, system="linux"):
        """A click, its answer delivered (the status as the record says, on `system`)."""
        with (
            mock.patch.object(file_types, "status", side_effect=recorded_status),
            mock.patch.object(row_module, "_system", return_value=system),
        ):
            dialog.file_types_button.click()
            self.assertTrue(wait_until(dialog.file_types_button.isEnabled))
            for thread in worker(row_module.PRESS_THREAD):
                thread.join(5)

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

    def test_row_follows_status(self):
        expected = {
            (Status.NOT_REGISTERED, "win32"): (USE_TEXT, WINDOWS_HINT, True),
            (Status.NOT_REGISTERED, "darwin"): (USE_TEXT, MAC_HINT, True),
            (Status.NOT_REGISTERED, "linux"): (USE_TEXT, LINUX_HINT, True),
            (Status.REGISTERED, "win32"): (STOP_TEXT, None, True),
            (Status.REGISTERED, "darwin"): (STOP_TEXT, MAC_REGISTERED_HINT, True),
            (Status.REGISTERED, "linux"): (STOP_TEXT, None, True),
            (Status.TEMPORARY, "win32"): (USE_TEXT, TEMPORARY_HINT, False),
            (Status.TEMPORARY, "darwin"): (USE_TEXT, MAC_TEMPORARY_HINT, False),
            (Status.TEMPORARY, "linux"): (USE_TEXT, TEMPORARY_HINT, False),
        }
        for (state, system), (button, hint, enabled) in expected.items():
            with self.subTest(state=state, system=system):
                dialog = self.dialog(state, system)
                layout = dialog.settings_layout
                self.assertTrue(layout.isRowVisible(dialog.file_types_button.parentWidget()))
                self.assertEqual(dialog.file_types_button.text(), translate(button))
                self.assertEqual(dialog.file_types_button.isEnabled(), enabled)
                self.assertEqual(self.hint(dialog), translate(hint) if hint else "")
        dialog = self.dialog(Status.UNAVAILABLE)  # a source run: no row at all
        self.assertFalse(
            dialog.settings_layout.isRowVisible(dialog.file_types_button.parentWidget())
        )

    def test_the_row_sits_under_start_with_the_last_channel(self):
        dialog = self.dialog(Status.NOT_REGISTERED)
        layout = dialog.settings_layout
        row, _role = layout.getWidgetPosition(dialog.file_types_button.parentWidget())
        above, _role = layout.getWidgetPosition(dialog.start_last_channel_checkbox)
        self.assertEqual(row, above + 1)
        self.assertEqual(
            layout.labelForField(dialog.file_types_button.parentWidget()).text(),
            "Open videos with QiTV",
        )
        self.assertFalse(dialog.file_types_button.autoDefault())  # Enter still saves

    def test_the_mac_hint_names_every_type_macos_takes(self):
        for ext in file_types.MAC_DEFAULT_VIDEO:
            self.assertIn(ext, translate(MAC_HINT).lower())

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
        self.assertEqual(dialog.file_types_button.text(), "Stop using QiTV for videos")
        self.assertEqual(saved, [threading.main_thread()])  # saved on the GUI thread
        self.assertEqual(ConfigManager().file_types, {"videos": EXE})

    def test_stop_using_unregisters_off_the_gui_thread(self):
        self.config.file_types = {"videos": EXE}
        dialog = self.dialog()
        self.assertEqual(dialog.file_types_button.text(), translate(STOP_TEXT))

        def unregister(kind, config, *args, **kwargs):
            self.threads.append(threading.current_thread())
            config.file_types = {}
            return True

        with mock.patch.object(file_types, "unregister", side_effect=unregister):
            self.press(dialog)
        self.assertIsNot(self.threads[0], threading.main_thread())
        self.assertEqual(dialog.file_types_button.text(), translate(USE_TEXT))
        self.assertEqual(self.hint(dialog), translate(LINUX_HINT))
        self.assertEqual(ConfigManager().file_types, {})

    def test_windows_opens_default_apps_after_register(self):
        for system in SYSTEMS:
            with self.subTest(system=system):
                self.config.file_types = {}
                dialog = self.dialog(system=system)
                with (
                    self.registering(),
                    mock.patch.object(file_types, "open_default_apps") as default_apps,
                ):
                    self.press(dialog, system)
                self.assertEqual(default_apps.call_count, 1 if system == "win32" else 0)

    def test_a_failed_press_says_so_and_can_be_tried_again(self):
        dialog = self.dialog()
        with (
            self.registering(RegisterResult(False)),
            mock.patch.object(file_types, "open_default_apps") as default_apps,
            mock.patch.object(self.config, "save_config") as save,
        ):
            self.press(dialog, "win32")
        self.assertEqual(self.hint(dialog), "Couldn't change it. See the log.")
        self.assertIn("color", dialog.file_types_hint.styleSheet())  # an error's colour
        self.assertEqual(dialog.file_types_button.text(), translate(USE_TEXT))
        self.assertTrue(dialog.file_types_button.isEnabled())
        default_apps.assert_not_called()
        save.assert_not_called()
        with self.registering():  # a second try that works: the error goes
            self.press(dialog, "win32")
        self.assertEqual(self.hint(dialog), "")
        self.assertEqual(dialog.file_types_hint.styleSheet(), "")

    def test_a_service_that_raises_still_answers(self):
        dialog = self.dialog()
        with (
            mock.patch.object(file_types, "register", side_effect=RuntimeError("boom")),
            self.assertLogs("ui.file_types_settings", "WARNING"),
        ):
            self.press(dialog)
        self.assertEqual(self.hint(dialog), translate(FAILED_HINT))
        self.assertTrue(dialog.file_types_button.isEnabled())

    def test_linux_open_with_only_says_so_for_this_session(self):
        dialog = self.dialog()
        with self.registering(RegisterResult(True, False)):  # no xdg-mime: no default taken
            self.press(dialog)
        self.assertEqual(dialog.file_types_button.text(), translate(STOP_TEXT))
        self.assertEqual(self.hint(dialog), translate(LINUX_OPEN_WITH_HINT))
        self.assertEqual(self.dialog().file_types_button.text(), translate(STOP_TEXT))
        self.assertEqual(self.hint(self.dialog()), "")  # the next Settings: no extra hint

    def test_the_button_waits_while_a_press_runs(self):
        dialog = self.dialog()
        gate = threading.Event()
        with self.registering(gate=gate):
            with mock.patch.object(file_types, "status", side_effect=recorded_status):
                dialog.file_types_button.click()
                self.assertFalse(dialog.file_types_button.isEnabled())
                dialog.file_types_button.click()  # disabled: nothing more starts
            gate.set()
            self.press_answered(dialog)
        self.assertEqual(len(self.threads), 1)
        self.assertEqual(dialog.file_types_button.text(), translate(STOP_TEXT))

    def press_answered(self, dialog):
        with (
            mock.patch.object(file_types, "status", side_effect=recorded_status),
            mock.patch.object(row_module, "_system", return_value="linux"),
        ):
            self.assertTrue(wait_until(dialog.file_types_button.isEnabled))

    def test_closing_settings_mid_press_still_saves(self):
        dialog = self.dialog()
        gate = threading.Event()
        with self.registering(gate=gate):
            with mock.patch.object(file_types, "status", side_effect=recorded_status):
                dialog.file_types_button.click()
            dialog.done(0)  # Settings closed while the system is told
            gate.set()
            self.assertTrue(wait_until(lambda: ConfigManager().file_types == {"videos": EXE}))

    def test_settings_gone_mid_press_breaks_nothing(self):
        dialog = self.dialog()
        gate = threading.Event()
        with (
            self.registering(gate=gate),
            mock.patch.object(threading, "excepthook") as raised,
        ):
            with mock.patch.object(file_types, "status", side_effect=recorded_status):
                dialog.file_types_button.click()
            dialog.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.assertFalse(isValid(dialog))
            gate.set()
            for thread in worker(row_module.PRESS_THREAD):
                thread.join(5)
            QCoreApplication.processEvents()
        raised.assert_not_called()  # the worker's answer found no one: nothing raised
        self.assertEqual(self.config.file_types, {"videos": EXE})  # the service recorded it


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
