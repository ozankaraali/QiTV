"""Settings › Files › Open videos with QiTV, and Open music with QiTV (the drawer's rows, through
ui/file_types_switch.py's FileTypesSwitch): a row's press registers QiTV for its kind of file,
or takes it back, on a worker; its help follows the system and the answer; the rows are
independent; on Windows the running QiTV's DDE follows. And at start, a QiTV that was moved
registers itself again (main.py). Over a real TV window; the system is never told."""

from pathlib import Path
import threading
import unittest
from unittest import mock

from PySide6.QtCore import QCoreApplication
from test_dde_windows import FakeDdeml
from timer_fixtures import wait_until

from services import dde_windows, file_types, settings_model as m
from services.config_manager import ConfigManager
from services.file_types import RegisterResult, Status
from services.i18n import translate
from tests import test_tv_window as base
from tests.test_settings_general import EXE, GeneralCase, recorded
from ui import file_types_switch as switch_module
from ui.file_types_switch import (
    FAILED_HINT,
    LINUX_HINT,
    LINUX_OPEN_WITH_HINT,
    MAC_HINT,
    MAC_REGISTERED_HINT,
    MAC_TEMPORARY_HINT,
    MUSIC_LINUX_HINT,
    MUSIC_MAC_HINT,
    TEMPORARY_HINT,
    WINDOWS_HINT,
    refresh_at_start,
)
from ui.keymap import Action
from ui.settings_general import DEFAULT_APPS, ELSEWHERE_TEXT, IN_QITV_TEXT, MUSIC, VIDEOS

REPO = Path(__file__).resolve().parent.parent
SYSTEMS = ("win32", "darwin", "linux")
_KINDS = {VIDEOS: "videos", MUSIC: "music"}


def worker(name):
    return [t for t in threading.enumerate() if t.name == name]


def join(*names):
    for name in names:
        for thread in worker(name):
            thread.join(5)


class FilesCase(GeneralCase):
    """Settings › Files on a real window: a row's press and its answer, the system as `system`
    says and the status as the record says (patched for the whole test: the rows are built
    again as they follow)."""

    KEY = VIDEOS
    MAC, LINUX = MAC_HINT, LINUX_HINT
    system = "linux"

    def setUp(self):
        super().setUp()
        self.threads: list[threading.Thread] = []
        self.status = mock.patch.object(file_types, "status", side_effect=recorded())
        self.status.start()
        self.addCleanup(self.status.stop)
        self.where = mock.patch.object(switch_module, "_system", side_effect=lambda: self.system)
        self.where.start()
        self.addCleanup(self.where.stop)
        self.default_apps = mock.patch.object(file_types, "open_default_apps")
        self.opened = self.default_apps.start()
        self.addCleanup(self.default_apps.stop)

    def switch(self, key=None):
        return self.window.general_rows._switches[key or self.KEY]

    def row(self, key=None):
        """The row `key` as Files shows it now (Settings opened at Files when it is not)."""
        if not self.window.settings.visible():
            self.window.open_settings(section_key=m.FILES)
        return self.shown().get(key or self.KEY)

    def press(self, key=None, system=None):
        """A press on the row `key` (by the keys), its answer heard."""
        if system is not None:
            self.system = system
        key = key or self.KEY
        self.window.settings.close()  # (open_at's → would press a focused row)
        self.open_at(m.FILES, key)
        self.key(Action.NAV_ACTIVATE)
        self.assertTrue(wait_until(lambda: not self.switch(key).busy))
        join(switch_module.PRESS_THREAD, switch_module.DEFAULT_APPS_THREAD)
        QCoreApplication.processEvents()

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

    def unregistering(self):
        def unregister(kind, config, *args, **kwargs):
            self.threads.append(threading.current_thread())
            config.file_types = {k: v for k, v in config.file_types.items() if k != kind}
            return True

        return mock.patch.object(file_types, "unregister", side_effect=unregister)


class FilesRowTests(FilesCase):
    def test_the_row_follows_the_status(self):
        expected = {
            (Status.NOT_REGISTERED, "win32"): (WINDOWS_HINT, False),
            (Status.NOT_REGISTERED, "darwin"): (self.MAC, False),
            (Status.NOT_REGISTERED, "linux"): (self.LINUX, False),
            (Status.REGISTERED, "win32"): ("", True),
            (Status.REGISTERED, "darwin"): (MAC_REGISTERED_HINT, True),
            (Status.REGISTERED, "linux"): ("", True),
            (Status.TEMPORARY, "win32"): (TEMPORARY_HINT, False),
            (Status.TEMPORARY, "darwin"): (MAC_TEMPORARY_HINT, False),
            (Status.TEMPORARY, "linux"): (TEMPORARY_HINT, False),
        }
        for (state, system), (hint, registered) in expected.items():
            with self.subTest(state=state, system=system):
                self.system = system
                with mock.patch.object(file_types, "status", return_value=state):
                    self.window.settings.close()
                    row = self.row()
                    self.assertEqual((row.kind, row.help), (m.ACTION, hint))
                    note = IN_QITV_TEXT if registered else ELSEWHERE_TEXT
                    self.assertEqual(row.note, translate(note))
                    windows_registered = (state, system) == (Status.REGISTERED, "win32")
                    self.assertEqual(DEFAULT_APPS in self.shown(), windows_registered)
                    if state is Status.TEMPORARY:  # what it says must be done first
                        self.assertFalse(self.switch().press())
        with mock.patch.object(file_types, "status", return_value=Status.UNAVAILABLE):
            self.window.settings.close()
            self.assertIsNone(self.row())  # a source run: no row at all

    def test_the_mac_hint_names_every_type_macos_takes(self):
        for hint, kinds in (
            (MAC_HINT, ("mkv", "mp4", "mov", "avi", "webm")),
            (MUSIC_MAC_HINT, ("mp3", "m4a", "flac")),
        ):
            for ext in kinds:
                self.assertIn(ext, translate(hint).lower())

    def test_windows_default_apps_opens_off_the_gui_thread(self):  # Ruling 26, review M6
        self.config.file_types = {_KINDS[self.KEY]: EXE}
        self.system = "win32"
        opened_on = []
        self.opened.side_effect = lambda: opened_on.append(threading.current_thread())
        self.window.settings.close()
        self.open_at(m.FILES, DEFAULT_APPS)
        self.key(Action.NAV_ACTIVATE)
        join(switch_module.DEFAULT_APPS_THREAD)
        self.opened.assert_called_once_with()
        self.assertIsNot(opened_on[0], threading.main_thread())  # ShellExecute may stall
        self.assertEqual(opened_on[0].name, switch_module.DEFAULT_APPS_THREAD)
        # It goes once Windows no longer opens anything with QiTV.
        with self.unregistering():
            self.press(system="win32")
        self.assertNotIn(DEFAULT_APPS, self.shown())

    def test_stop_using_unregisters_off_the_gui_thread(self):
        self.config.file_types = {_KINDS[self.KEY]: EXE}
        self.assertEqual(self.row().note, translate(IN_QITV_TEXT))
        with self.unregistering():
            self.press()
        self.assertIsNot(self.threads[0], threading.main_thread())
        self.assertEqual(self.row().note, translate(ELSEWHERE_TEXT))
        self.assertEqual(self.row().help, self.LINUX)
        self.assertEqual(ConfigManager().file_types, {})

    def test_windows_opens_default_apps_after_a_register(self):
        for system in SYSTEMS:
            with self.subTest(system=system):
                self.config.file_types = {}
                self.opened.reset_mock()
                with self.registering():
                    self.press(system=system)
                self.assertEqual(self.opened.call_count, 1 if system == "win32" else 0)

    def test_a_failed_press_is_said_and_can_be_tried_again(self):
        with (
            mock.patch.object(self.config, "save_config") as save,
            self.registering(RegisterResult(False)),
        ):
            self.press(system="win32")
        self.assertIn((translate(FAILED_HINT), "error"), self.toasts)
        self.assertEqual(self.row().note, translate(ELSEWHERE_TEXT))
        self.opened.assert_not_called()
        save.assert_not_called()
        self.toasts.clear()
        with self.registering():  # a second try that works: nothing is said
            self.press(system="win32")
        self.assertEqual(self.toasts, [])
        self.assertEqual(self.row().note, translate(IN_QITV_TEXT))

    def test_a_service_that_raises_still_answers(self):
        with (
            mock.patch.object(file_types, "register", side_effect=RuntimeError("boom")),
            self.assertLogs("ui.file_types_switch", "WARNING"),
        ):
            self.press()
        self.assertIn((translate(FAILED_HINT), "error"), self.toasts)
        self.assertFalse(self.switch().busy)  # it can be pressed again

    def test_linux_open_with_only_says_so_while_registered(self):
        with self.registering(RegisterResult(True, False)):  # no xdg-mime: no default taken
            self.press()
        self.assertEqual(self.row().note, translate(IN_QITV_TEXT))
        self.assertEqual(self.row().help, LINUX_OPEN_WITH_HINT)
        with self.unregistering():
            self.press()
        self.assertEqual(self.row().help, self.LINUX)

    def test_a_press_on_its_way_starts_no_other(self):
        gate = threading.Event()
        with self.registering(gate=gate):
            self.window.settings.close()
            self.open_at(m.FILES, self.KEY)
            self.key(Action.NAV_ACTIVATE)
            self.assertTrue(self.switch().busy)
            self.key(Action.NAV_ACTIVATE)  # on its way: nothing more starts
            gate.set()
            self.assertTrue(wait_until(lambda: not self.switch().busy))
            join(switch_module.PRESS_THREAD)
        self.assertEqual(len(self.threads), 1)
        self.assertEqual(self.row().note, translate(IN_QITV_TEXT))

    def test_closing_settings_mid_press_still_saves(self):
        gate = threading.Event()
        with self.registering(gate=gate):
            self.window.settings.close()
            self.open_at(m.FILES, self.KEY)
            self.key(Action.NAV_ACTIVATE)
            self.window.settings.close()  # Settings closed while the system is told
            gate.set()
            self.assertTrue(
                wait_until(lambda: ConfigManager().file_types == {_KINDS[self.KEY]: EXE})
            )


class MusicFilesRowTests(FilesRowTests):
    KEY = MUSIC
    MAC, LINUX = MUSIC_MAC_HINT, MUSIC_LINUX_HINT


class TwoRowsTests(FilesCase):
    """Videos and music: each row says and does its own kind only."""

    def test_rows_are_independent(self):
        notes = lambda: (self.row(VIDEOS).note, self.row(MUSIC).note)  # noqa: E731
        elsewhere, here = translate(ELSEWHERE_TEXT), translate(IN_QITV_TEXT)
        self.assertEqual(notes(), (elsewhere, elsewhere))
        with self.registering():
            self.press(MUSIC)
        self.assertEqual(notes(), (elsewhere, here))
        self.assertEqual(self.config.file_types, {"music": EXE})
        with self.registering():
            self.press(VIDEOS)
        with self.unregistering():
            self.press(MUSIC)
        self.assertEqual(notes(), (here, elsewhere))
        self.assertEqual(ConfigManager().file_types, {"videos": EXE})  # saved as it is now
        self.window.settings.close()  # Settings again reads each row from the record
        self.assertEqual(notes(), (here, elsewhere))

    def test_one_row_failing_leaves_the_other(self):
        self.config.file_types = {"videos": EXE}
        with self.registering(RegisterResult(False)):
            self.press(MUSIC)
        self.assertIn((translate(FAILED_HINT), "error"), self.toasts)
        self.assertEqual(self.row(VIDEOS).note, translate(IN_QITV_TEXT))
        self.assertFalse(self.switch(VIDEOS).failed)


class DdeFollowsSettingsTests(FilesCase):
    """Ruling 27: on Windows, a Files row's press turns the running QiTV's DDE on, or off."""

    system = "win32"

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
        join(dde_windows.WRITER_THREAD)

    def test_windows_register_starts_the_server_and_writes_the_key(self):
        serving = self.serving()
        with self.registering():
            self.press()
        self.settle()
        self.assertTrue(serving.running)
        self.assertEqual(self.added, [EXE])  # on a worker
        # A second register while it runs (QiTV unregistered behind its back, say): the same
        # server, the key written over (Ruling 28c: a registration rewrote the ProgID).
        self.config.file_types = {}
        self.window.settings.close()
        with self.registering():
            self.press()
        self.settle()
        self.assertEqual(self.ddeml.calls.count("DdeInitializeW"), 1)  # no second server
        self.assertEqual(self.added, [EXE, EXE])

    def test_a_closing_window_follows_no_more(self):
        serving = self.serving()
        self.window._closing = True  # QiTV is closing: a late answer starts nothing
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
        with self.unregistering():
            self.press()
        self.assertFalse(serving.running)
        self.assertEqual(self.ddeml.calls.count("DdeUninitialize"), 1)

    def test_a_failed_press_leaves_it_as_it_was(self):
        serving = self.serving()
        with self.registering(RegisterResult(False)):
            self.press()
        self.settle()
        self.assertFalse(serving.running)
        self.assertEqual(self.ddeml.calls, [])
        self.assertEqual(self.added, [])

    def test_registering_music_starts_the_server(self):
        serving = self.serving()
        with self.registering():
            self.press(MUSIC)
        self.settle()
        self.assertTrue(serving.running)
        self.assertEqual(self.added, [EXE])

    def test_stopping_one_kind_keeps_serving_the_other(self):
        serving = self.serving()
        self.assertTrue(serving.follow(True))
        self.settle()
        self.added.clear()
        self.config.file_types = {"videos": EXE, "music": EXE}
        with self.unregistering():
            self.press(MUSIC)  # videos still double-click into this QiTV
            self.settle()
            self.assertTrue(serving.running)
            self.assertEqual(self.ddeml.calls.count("DdeUninitialize"), 0)
            self.press(VIDEOS)  # none left: it stops
            self.settle()
        self.assertFalse(serving.running)
        self.assertEqual(self.ddeml.calls.count("DdeUninitialize"), 1)

    def test_elsewhere_the_press_has_no_dde(self):
        self.assertIsNone(self.window.dde)  # main() gives it one on Windows only
        for system in ("darwin", "linux"):
            with self.subTest(system=system):
                self.config.file_types = {}
                self.window.settings.close()
                with self.registering():
                    self.press(VIDEOS, system=system)
                    self.press(MUSIC, system=system)
                self.assertEqual(self.config.file_types, {"videos": EXE, "music": EXE})
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
        join(switch_module.REFRESH_THREAD)
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

    def test_types_registered_again_at_start_are_saved(self):
        # Ruling M4: the kind registered again with this version's types, the path the same
        self.config.file_types = {"music": "/opt/qitv"}
        self.config.save_config()

        def refresh(config, *args, **kwargs):
            config.file_types_registered = {"music": ["aif", "mp3"]}

        with mock.patch.object(file_types, "refresh", side_effect=refresh):
            refresh_at_start(self.config, self.window, frozen=True)
            self.assertTrue(
                wait_until(
                    lambda: ConfigManager().file_types_registered == {"music": ["aif", "mp3"]}
                )
            )

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
