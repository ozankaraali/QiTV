"""The covered window's picture in picture (ui/desktop_pip.py, the covered reason; macOS): while
the TV window (not fullscreen) is covered whole by other apps' windows or minimized, its picture
floats in the desktop picture in picture's window, and comes back when the window is seen."""

import os
import types
from typing import Any
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QRect, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from services.config_manager import ConfigManager  # noqa: E402
from services.playback import PlaybackState  # noqa: E402
from tests import test_config_tv  # noqa: E402
from tests.test_desktop_pip import Config, mouse  # noqa: E402
from tests.test_tv_window import WindowCase  # noqa: E402
from ui import desktop_pip  # noqa: E402
from ui.desktop_pip import COVERED, DESKTOP, DesktopPip  # noqa: E402
from ui.pip import PipFrame  # noqa: E402

SETTLE_S = desktop_pip.COVERED_SETTLE_MS / 1000
FLOATING_RECT = QRect(900, 500, 320, 180)


class CoveredPipTests(unittest.TestCase):
    """The covered reason with what macOS answers played by the test."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = QWidget()
        self.window.setGeometry(100, 100, 1280, 720)
        self.page = QWidget(self.window)
        self.page.resize(1280, 720)
        self.main, self.small = QWidget(self.page), QWidget(self.page)
        self.frame = PipFrame(self.page, self.main, self.small, Config())
        self.others = False  # another app's window covers the whole TV window
        self.cant_tell = False  # `seen` answers None
        self.active = False  # QiTV is the active app
        self.allowed = True  # may_float_covered
        self.now = 0.0
        self.asked_seen = 0
        self.asked_space = 0
        self.activated = 0
        self.changes: list[bool] = []
        self.pip = self.make_pip()
        self.pip.arm(COVERED)

    def make_pip(self, **overrides):
        options: dict[str, Any] = dict(
            on_active_space=self.space,
            may_float=lambda: True,
            lend=self.frame.lend_large,
            take_back=self.frame.take_back,
            place=lambda: QRect(FLOATING_RECT),
            join_all_spaces=lambda _widget: None,
            seen=self.seen,
            may_float_covered=lambda: self.allowed,
            app_active=lambda: self.active,
            activate=self.activate,
            clock=lambda: self.now,
        )
        options.update(overrides)
        pip = DesktopPip(self.window, **options)
        pip.floatingChanged.connect(self.changes.append)
        self.addCleanup(pip.close)
        self.addCleanup(self.window.deleteLater)
        return pip

    # What macOS would answer: occlusion counts every window over the TV window, the floating
    # window (QiTV's own) too.
    def seen(self, window) -> bool | None:
        self.asked_seen += 1
        if self.cant_tell:
            return None
        if self.others:
            return False
        floating = self.pip.floating_window()
        over = (
            floating is not None
            and floating.isVisible()
            and floating.frameGeometry().contains(window.frameGeometry())
        )
        return not over

    def space(self, _window) -> bool:
        self.asked_space += 1
        return True

    def activate(self) -> bool:
        self.activated += 1
        return True

    def covered_for(self, seconds: float) -> None:
        self.others = True
        self.pip.check()
        self.now += seconds
        self.pip.check()

    def test_a_covered_window_floats_its_picture_after_the_settle_time(self):
        self.others = True
        self.pip.check()  # the first answer: not yet
        self.assertFalse(self.pip.floating())
        self.now += SETTLE_S - 0.1
        self.pip.check()
        self.assertFalse(self.pip.floating())
        self.now += 0.1
        self.pip.check()
        self.assertTrue(self.pip.floating())
        floating = self.pip.floating_window()
        self.assertIs(self.main.parentWidget(), floating)
        self.assertEqual(floating.geometry(), FLOATING_RECT)
        self.assertEqual(self.changes, [True])

    def test_seen_again_the_picture_goes_back_at_once(self):
        self.covered_for(SETTLE_S)
        floating = self.pip.floating_window()
        self.others = False  # the other app's window moved away (or a sliver of QiTV shows)
        self.pip.check()
        self.assertFalse(self.pip.floating())
        self.assertIs(self.main.parentWidget(), self.page)
        self.assertEqual(self.main.geometry(), self.page.rect())
        self.assertTrue(floating.isHidden())
        self.assertEqual(self.changes, [True, False])

    def test_a_moment_covered_floats_nothing(self):
        # A menu, a notification, a window dragged across: seen again before the settle time.
        self.others = True
        self.pip.check()
        self.now += SETTLE_S - 0.1
        self.others = False
        self.pip.check()
        self.others = True
        self.now += 0.1
        self.pip.check()  # covered again: the wait starts again
        self.now += SETTLE_S - 0.1
        self.pip.check()
        self.assertFalse(self.pip.floating())
        self.now += 0.1
        self.pip.check()
        self.assertTrue(self.pip.floating())

    def test_many_answers_at_once_never_shorten_the_wait(self):
        # A focus change and the app's own state change nudge it together.
        self.others = True
        for _ in range(5):
            self.pip.check()
        self.assertFalse(self.pip.floating())

    def test_minimized_floats_even_while_qitv_is_the_active_app(self):
        self.active = True  # macOS leaves QiTV active after its only window is minimized
        self.window.setWindowState(Qt.WindowState.WindowMinimized)
        self.pip.check()
        self.now += SETTLE_S
        self.pip.check()
        self.assertTrue(self.pip.floating())
        self.window.setWindowState(Qt.WindowState.WindowNoState)  # back from the Dock
        self.pip.check()
        self.assertFalse(self.pip.floating())
        self.assertIs(self.main.parentWidget(), self.page)

    def test_qitv_s_own_dialogs_and_menus_never_float_it(self):
        self.active = True  # QiTV is the active app: its dialog or menu is over the window
        self.covered_for(SETTLE_S * 4)
        self.assertFalse(self.pip.floating())

    def test_qitv_becoming_the_active_app_brings_the_picture_back(self):
        self.covered_for(SETTLE_S)
        self.assertTrue(self.pip.floating())
        self.active = True  # its Dock icon, ⌘Tab
        self.pip.check()
        self.assertFalse(self.pip.floating())

    def test_a_click_on_another_screen_while_qitv_shows_floats_nothing(self):
        self.active = False  # another app is active now, on the other screen
        self.others = False  # nothing covers the TV window
        for _ in range(4):
            self.pip.check()
            self.now += SETTLE_S
        self.assertFalse(self.pip.floating())
        self.assertGreater(self.asked_seen, 0)

    def test_what_cannot_be_told_changes_nothing(self):
        self.cant_tell = True
        self.covered_for(SETTLE_S)
        self.assertFalse(self.pip.floating())
        self.cant_tell = False
        self.covered_for(SETTLE_S)
        self.assertTrue(self.pip.floating())
        self.cant_tell = True
        self.others = False
        self.pip.check()
        self.assertTrue(self.pip.floating())  # it stays where it is

    def test_the_floating_window_never_hides_the_window_it_floats_for(self):
        # A small TV window that lies wholly under the floating window: once the other app's
        # window goes, only QiTV's own floating window is over it.
        self.window.setGeometry(950, 520, 200, 112)
        self.covered_for(SETTLE_S)
        self.assertTrue(self.pip.floating())
        self.others = False
        self.assertFalse(self.seen(self.window))  # (macOS would say it is covered whole)
        for _ in range(4):
            self.pip.check()  # can't tell: no flapping between floating and back
            self.now += SETTLE_S
        self.assertTrue(self.pip.floating())
        self.assertEqual(self.changes, [True])
        self.active = True  # QiTV clicked in the Dock: back
        self.pip.check()
        self.assertFalse(self.pip.floating())
        self.active = False
        for _ in range(4):
            self.pip.check()  # back, the floating window hidden: it is seen, nothing floats
            self.now += SETTLE_S
        self.assertFalse(self.pip.floating())
        self.assertEqual(self.changes, [True, False])

    def test_a_floating_window_over_part_of_it_lets_the_rest_be_seen(self):
        self.window.setGeometry(800, 400, 640, 360)  # the floating window covers a corner
        self.covered_for(SETTLE_S)
        self.others = False
        self.pip.check()
        self.assertFalse(self.pip.floating())

    def test_the_floating_window_itself_never_floats_the_picture(self):
        # Docked, the floating window is hidden: it covers nothing.
        self.window.setGeometry(950, 520, 200, 112)
        self.covered_for(SETTLE_S)
        self.others = False
        self.active = True
        self.pip.check()
        self.active = False
        self.assertTrue(self.pip.floating_window().isHidden())
        self.assertTrue(self.seen(self.window))

    def test_a_double_click_brings_qitv_back_from_the_dock_and_to_the_front(self):
        self.window.setWindowState(Qt.WindowState.WindowMinimized)
        self.covered_for(SETTLE_S)
        self.assertTrue(self.pip.floating())
        with (
            mock.patch.object(self.window, "raise_") as raised,
            mock.patch.object(self.window, "activateWindow") as activated,
        ):
            mouse(self.main, QEvent.Type.MouseButtonPress, QPoint(160, 90))
            mouse(
                self.main, QEvent.Type.MouseButtonRelease, QPoint(160, 90), Qt.MouseButton.NoButton
            )
            self.assertTrue(self.pip.floating())  # a click alone does nothing
            mouse(self.main, QEvent.Type.MouseButtonDblClick, QPoint(160, 90))
        self.assertFalse(self.pip.floating())
        self.assertIs(self.main.parentWidget(), self.page)
        self.assertFalse(self.window.isMinimized())
        self.assertEqual(self.activated, 1)  # QiTV in front of the app that covered it
        raised.assert_called_once()
        activated.assert_called_once()

    def test_after_a_double_click_nothing_floats_before_the_window_was_seen(self):
        self.covered_for(SETTLE_S)
        mouse(self.main, QEvent.Type.MouseButtonDblClick, QPoint(160, 90))
        self.assertFalse(self.pip.floating())
        for _ in range(4):  # macOS hasn't brought QiTV forward (yet): the picture stays
            self.pip.check()
            self.now += SETTLE_S
        self.assertFalse(self.pip.floating())
        self.others = False
        self.pip.check()  # seen
        self.covered_for(SETTLE_S)
        self.assertTrue(self.pip.floating())

    def test_nothing_floats_when_it_may_not_and_what_floats_comes_back(self):
        self.allowed = False  # the grid, the Library, Keep on Top, nothing playing
        self.covered_for(SETTLE_S * 2)
        self.assertFalse(self.pip.floating())
        self.assertEqual(self.asked_seen, 0)  # (not even asked)
        self.allowed = True
        self.covered_for(SETTLE_S)
        self.assertTrue(self.pip.floating())
        self.allowed = False
        self.pip.check()
        self.assertFalse(self.pip.floating())
        self.assertIs(self.main.parentWidget(), self.page)

    def test_the_multiview_lends_nothing(self):
        self.frame.suspend()
        self.covered_for(SETTLE_S)
        self.assertFalse(self.pip.floating())

    def test_another_reason_puts_the_picture_back_first(self):
        self.covered_for(SETTLE_S)
        self.assertEqual(self.pip.reason(), COVERED)
        self.pip.arm(COVERED)  # again (a window state change): it goes on floating
        self.assertTrue(self.pip.floating())
        self.pip.arm(DESKTOP)  # fullscreen
        self.assertFalse(self.pip.floating())
        self.assertEqual(self.pip.reason(), DESKTOP)
        self.pip.arm(None)
        self.assertIsNone(self.pip.reason())
        self.assertFalse(self.pip._timer.isActive())

    def test_fullscreen_asks_only_about_the_desktop(self):
        self.pip.arm(DESKTOP)
        self.others = True
        self.pip.check()
        self.pip.check()
        self.assertEqual(self.asked_seen, 0)
        self.assertEqual(self.asked_space, 2)
        self.assertFalse(self.pip.floating())  # (its desktop is shown)

    def test_without_a_way_to_tell_it_is_never_asked(self):
        pip = self.make_pip(seen=None)
        pip.arm(COVERED)
        self.assertIsNone(pip.reason())
        self.assertFalse(pip._timer.isActive())

    def test_the_second_answer_is_asked_after_the_settle_time_not_a_poll_later(self):
        from timer_fixtures import wait_until

        self.pip.close()
        pip = self.make_pip(clock=desktop_pip.time.monotonic)
        self.pip = pip  # (`seen` looks at its floating window)
        pip.arm(COVERED)
        self.others = True
        pip.check()
        self.assertFalse(pip.floating())
        self.assertTrue(wait_until(pip.floating, desktop_pip.COVERED_CONFIRM_MS + 300))


class CoveredPipConfigTests(unittest.TestCase):
    _load = test_config_tv.TvConfigTests._load

    def test_off_by_default_and_never_written_by_itself(self):
        config, path = self._load(None)
        self.assertIs(config.covered_pip, False)
        self.assertNotIn("covered_pip", path.read_text(encoding="utf-8"))

    def test_an_older_config_reads_and_only_true_turns_it_on(self):
        for saved, expected in (
            ({}, False),
            ({"covered_pip": True}, True),
            ({"covered_pip": 1}, False),
            ({"covered_pip": "yes"}, False),
            ({"covered_pip": None}, False),
        ):
            with self.subTest(saved=saved):
                config, _ = self._load(saved)
                self.assertIs(config.covered_pip, expected)

    def test_it_is_saved_and_read_back(self):
        config, path = self._load({"desktop_pip": False})
        config.covered_pip = True
        config.save_config()
        again = ConfigManager()
        self.assertIs(again.covered_pip, True)
        self.assertIs(again.desktop_pip, False)  # the other setting stays as it was


class CoveredPipWindowTests(WindowCase):
    """The TV window: when its picture may float for a covered window, and what is asked."""

    def playing(self):
        patch = mock.patch.object(
            self.window.controller.session, "state", return_value=PlaybackState.PLAYING
        )
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_shown_window_with_the_setting_on_may_float_its_picture(self):
        window = self.window
        window.show()
        self.app.processEvents()
        self.playing()
        self.assertFalse(window._may_float_covered())  # off by default
        window.config_manager.covered_pip = True
        self.assertTrue(window._may_float_covered())
        self.assertFalse(window._may_float_picture())  # (the fullscreen one: not in a window)
        window.showMinimized()
        self.app.processEvents()
        self.assertTrue(window.isMinimized())
        self.assertTrue(window._may_float_covered())  # minimized, it still counts as shown

    def test_never_with_keep_on_top_the_grid_the_library_or_the_mini_player(self):
        window = self.window
        window.show()
        self.app.processEvents()
        self.playing()
        window.config_manager.covered_pip = True
        window.config_manager.keep_on_top = True
        self.assertFalse(window._may_float_covered())
        window.config_manager.keep_on_top = False
        with mock.patch.object(window.controller, "multiview_shown", return_value=True):
            self.assertFalse(window._may_float_covered())
        with mock.patch.object(window.screens, "library_shown", return_value=True):
            self.assertFalse(window._may_float_covered())
        window._mini_restore = (window.geometry(), window.windowFlags(), False)
        self.assertFalse(window._may_float_covered())
        window._mini_restore = None
        self.assertTrue(window._may_float_covered())

    def test_never_without_a_picture_playing(self):
        window = self.window
        window.show()
        self.app.processEvents()
        window.config_manager.covered_pip = True
        self.assertFalse(window._may_float_covered())  # nothing plays (no libVLC)

    def test_never_in_fullscreen(self):
        window = self.window
        window.show()
        window.showFullScreen()
        self.app.processEvents()
        self.playing()
        window.config_manager.covered_pip = True
        self.assertFalse(window._may_float_covered())

    def test_what_is_asked_follows_the_window_and_the_setting(self):
        window = self.window
        with mock.patch("ui.tv_window._macos_desktops", return_value=True):
            window.show()
            self.app.processEvents()
            self.assertIsNone(window.desktop_pip.reason())  # off by default: nothing polls
            window.config_manager.covered_pip = True
            window._arm_desktop_pip()  # (Settings closed)
            self.assertEqual(window.desktop_pip.reason(), COVERED)
            window.showFullScreen()
            self.app.processEvents()
            self.assertEqual(window.desktop_pip.reason(), DESKTOP)
            window.showNormal()
            self.app.processEvents()
            self.assertEqual(window.desktop_pip.reason(), COVERED)
            window.config_manager.covered_pip = False
            window._arm_desktop_pip()
            self.assertIsNone(window.desktop_pip.reason())
        window.config_manager.covered_pip = True
        window._arm_desktop_pip()
        self.assertIsNone(window.desktop_pip.reason())  # only macOS can tell

    def test_a_step_in_settings_arms_it_at_once(self):
        # Settings › Playback › Picture in picture: its switch's hook arms the watch.
        window = self.window
        with mock.patch("ui.tv_window._macos_desktops", return_value=True):
            window.settings._values.set("covered_pip", True)  # a step, as the drawer writes it
            self.assertEqual(window.desktop_pip.reason(), COVERED)
            window.settings._values.set("covered_pip", False)
            self.assertIsNone(window.desktop_pip.reason())

    def test_a_covered_window_floats_its_picture_and_takes_it_back(self):
        window = self.window
        state = types.SimpleNamespace(seen=True, now=0.0)
        with mock.patch("ui.tv_window._macos_desktops", return_value=True):
            window.config_manager.covered_pip = True
            window.show()
            self.app.processEvents()
            window._arm_desktop_pip()
            pip = window.desktop_pip
            pip._seen = lambda _w: state.seen
            pip._may_float_covered = lambda: True  # (no libVLC here: nothing plays)
            pip._app_active = lambda: False
            pip._clock = lambda: state.now
            state.seen = False  # another app's window in front, over all of it
            pip.check()
            state.now += SETTLE_S
            pip.check()
            self.assertIsNot(window.surface.parentWidget(), window.player_page)
            self.assertTrue(window.floating_note.isVisible())
            state.seen = True
            pip.check()
            self.assertIs(window.surface.parentWidget(), window.player_page)
            self.assertFalse(window.floating_note.isVisible())
            # Minimized: asked at once (the state change), floating once it settled.
            window.showMinimized()
            self.app.processEvents()
            self.assertIsNotNone(pip._since)
            state.now += SETTLE_S
            pip.check()
            self.assertIsNot(window.surface.parentWidget(), window.player_page)
            window.showNormal()  # back from the Dock: back at once
            self.app.processEvents()
            self.assertIs(window.surface.parentWidget(), window.player_page)
            self.assertEqual(pip.reason(), COVERED)

    def test_qitv_s_own_activation_asks_at_once(self):
        window = self.window
        with (
            mock.patch("ui.tv_window._macos_desktops", return_value=True),
            mock.patch.object(window.desktop_pip, "nudge") as nudged,
        ):
            window._on_app_state(Qt.ApplicationState.ApplicationActive)
            nudged.assert_not_called()  # nothing asked about
            window.config_manager.covered_pip = True
            window._arm_desktop_pip()
            nudged.reset_mock()
            window._on_app_state(Qt.ApplicationState.ApplicationInactive)
            nudged.assert_called_once()


if __name__ == "__main__":
    unittest.main()
