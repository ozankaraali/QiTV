"""The macOS mini player (ui/mini_player.py, 2.13.5): the player page in a small window of its
own on every desktop, under the TV window's own controls, the window hidden meanwhile."""

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QMimeData, QPoint, QRect, QSize, Qt  # noqa: E402
from PySide6.QtGui import QDragEnterEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402
from timer_fixtures import elapse, wait_until  # noqa: E402

from services import picture  # noqa: E402
from services.catalog import ALL_GROUP_ID  # noqa: E402
from tests.test_desktop_pip import Config, _BoxedPlayer, mouse  # noqa: E402
from tests.test_tv_window import WindowCase  # noqa: E402
from ui import mini_player, small_window  # noqa: E402
from ui.keymap import Action  # noqa: E402
from ui.mini_player import MiniPlayer, MiniWindow  # noqa: E402
from ui.pip import PipFrame  # noqa: E402


class KeyTarget(QWidget):
    """The TV window's part: the keys, and what is dropped, it is handed; its close."""

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.keys: list[int] = []
        self.drops: list[QEvent.Type] = []
        self.closes = 0

    def keyPressEvent(self, event):
        self.keys.append(event.key())
        event.accept()

    def dragEnterEvent(self, event):
        self.drops.append(event.type())
        event.acceptProposedAction()

    def closeEvent(self, event):
        self.closes += 1
        event.ignore()


class Picture(QWidget):
    """A surface's part: the mouse that reaches it."""

    def __init__(self, parent):
        super().__init__(parent)
        self.events: list[QEvent.Type] = []

    def event(self, event):
        if event.type() in (
            QEvent.Type.MouseButtonPress,
            QEvent.Type.MouseButtonRelease,
            QEvent.Type.MouseButtonDblClick,
            QEvent.Type.MouseMove,
        ):
            self.events.append(event.type())
        return super().event(event)


class MiniCase(unittest.TestCase):
    """The controller with a page of two pictures and a TV window's stand-in."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = QWidget()
        self.window.resize(1280, 720)
        self.addCleanup(self.window.deleteLater)
        self.page = QWidget(self.window)
        self.page.resize(1280, 720)
        self.main, self.small = Picture(self.page), Picture(self.page)
        self.frame = PipFrame(self.page, self.main, self.small, Config())
        self.keys = KeyTarget()
        self.addCleanup(self.keys.deleteLater)  # (never after the QApplication: a crash at exit)
        self.ratio = 16 / 9
        self.hovers: list[bool] = []
        self.active = True
        self.at = QPoint(0, 0)
        screen = QApplication.primaryScreen().availableGeometry()
        self.place = QRect(screen.x() + 40, screen.y() + 40, 320, 180)
        self.mini = MiniPlayer(
            lend=self.lend,
            take_back=self.take_back,
            place=lambda: self.place,
            join_all_spaces=lambda _widget: None,
            shape=lambda: self.ratio,
            keys=self.keys,
            hover=lambda: self.hovers.append(True),
            app_active=lambda: self.active,
            cursor_pos=lambda: self.at,
        )
        self.addCleanup(self.mini.close)

    def lend(self):
        return self.page if self.frame.set_floating(True) else None

    def take_back(self, page):
        page.setParent(self.window)
        page.setGeometry(self.window.rect())
        page.show()
        self.frame.set_floating(False)


class MiniPlayerTests(MiniCase):
    def test_the_page_floats_in_its_small_window_and_comes_back(self):
        self.assertTrue(self.mini.start())
        window = self.mini.window()
        self.assertIs(self.page.parentWidget(), window)  # the page: its PiP and all
        self.assertEqual(self.page.geometry(), window.rect())
        self.assertTrue(self.frame.shows_small("main"))  # filled to the last pixel there
        self.mini.end("back to the window")
        self.assertIs(self.page.parentWidget(), self.window)
        self.assertFalse(self.frame.shows_small("main"))
        self.assertTrue(window.isHidden())

    def test_the_multiview_lends_nothing(self):
        self.frame.suspend()
        self.assertFalse(self.mini.start())
        self.assertFalse(self.mini.shown())

    def test_its_keys_drops_and_close_are_the_tv_windows(self):
        self.mini.start()
        window = self.mini.window()
        self.assertFalse(window.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus)
        self.assertTrue(window.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating))
        QTest.keyClick(window, Qt.Key.Key_Up)
        self.assertEqual(self.keys.keys, [Qt.Key.Key_Up])
        data = QMimeData()
        data.setText("http://example.invalid/stream")
        event = QDragEnterEvent(
            QPoint(10, 10),
            Qt.DropAction.CopyAction,
            data,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(window, event)
        self.assertEqual(self.keys.drops, [QEvent.Type.DragEnter])
        window.close()  # the system's Quit: the TV window closes its own way
        self.assertEqual(self.keys.closes, 1)
        self.assertTrue(window.isVisible())

    def test_clicks_on_the_picture_are_the_windows_its_edges_resize_it(self):
        self.mini.start()
        window = self.mini.window()
        start = window.geometry()
        mouse(self.main, QEvent.Type.MouseButtonPress, QPoint(160, 90))
        mouse(self.main, QEvent.Type.MouseButtonRelease, QPoint(160, 90), Qt.MouseButton.NoButton)
        mouse(self.main, QEvent.Type.MouseButtonDblClick, QPoint(160, 90))
        self.assertEqual(
            self.main.events,
            [
                QEvent.Type.MouseButtonPress,
                QEvent.Type.MouseButtonRelease,
                QEvent.Type.MouseButtonDblClick,
            ],
        )
        self.main.events.clear()
        with mock.patch("ui.small_window.reclaim_first_responder") as reclaimed:
            mouse(self.main, QEvent.Type.MouseButtonPress, QPoint(318, 178))  # its corner
            mouse(self.main, QEvent.Type.MouseMove, QPoint(318 + 160, 178 + 10))
            mouse(
                self.main,
                QEvent.Type.MouseButtonRelease,
                QPoint(318 + 160, 178 + 10),
                Qt.MouseButton.NoButton,
            )
        reclaimed.assert_called_with(window)  # (libVLC's view gives the keys back)
        self.assertEqual(self.main.events, [])  # the resize's, not the window's
        self.assertEqual(window.size(), QSize(480, 270))
        self.assertEqual(window.geometry().topLeft(), start.topLeft())

    def test_it_follows_its_pictures_shape_and_a_resize_macos_made_ends_in_it(self):
        self.mini.start()
        window = self.mini.window()
        self.ratio = 4 / 3
        self.mini._follow_shape()
        self.assertTrue(picture.same_shape(window.width() / window.height(), 4 / 3))
        window.setGeometry(QRect(window.pos(), QSize(546, 330)))  # macOS's own edge resize
        self.assertTrue(
            wait_until(lambda: picture.same_shape(window.width() / window.height(), 4 / 3))
        )

    def test_the_hover_poll_runs_only_while_qitv_is_not_the_active_app(self):
        self.mini.start()
        window = self.mini.window()
        self.assertFalse(self.mini._hover_poll.isActive())  # QiTV active: Qt's moves arrive
        self.active = False
        self.mini.activity_changed()
        self.assertTrue(self.mini._hover_poll.isActive())
        inside = window.geometry().center()
        self.mini._poll_hover()
        self.assertEqual(self.hovers, [])  # resting elsewhere
        self.at = inside
        self.mini._poll_hover()
        self.assertEqual(self.hovers, [True])  # a move over it: its hover
        self.mini._poll_hover()
        self.assertEqual(self.hovers, [True])  # resting: no move
        self.at = inside + QPoint(4, 2)
        self.assertTrue(wait_until(lambda: len(self.hovers) == 2, mini_player.HOVER_MS * 4))
        self.active = True
        self.mini.activity_changed()
        self.assertFalse(self.mini._hover_poll.isActive())
        self.active = False
        self.mini.activity_changed()
        self.mini.end("back to the window")
        self.assertFalse(self.mini._hover_poll.isActive())

    def test_its_window_is_the_small_window_kind(self):
        self.mini.start()
        window = self.mini.window()
        self.assertIsInstance(window, MiniWindow)
        self.assertIsInstance(window, small_window.SmallWindow)


class MiniPlayerWindowTests(WindowCase):
    """The TV window's macOS mini player: the page in the small window, the window's controls
    over it, the window hidden, and every way back."""

    def setUp(self):
        super().setUp()
        macos = mock.patch("ui.tv_window._macos_desktops", return_value=True)
        macos.start()
        self.addCleanup(macos.stop)

    def small(self):
        window = self.window.mini_player.window()
        self.assertIsNotNone(window)
        return window

    def test_the_mini_player_holds_the_page_under_the_windows_own_controls(self):
        window = self.window
        self.show()
        self.mini()  # Alt+P
        self.app.processEvents()
        small = self.small()
        self.assertTrue(window.mini_player_active())
        self.assertFalse(window.isVisible())  # the window gives way to it
        self.assertTrue(small.isVisible())
        self.assertTrue(small.isAncestorOf(window.surface))
        self.assertTrue(small.isAncestorOf(window.pip_surface))  # the page, its PiP too
        drawer = window.overlays.drawer
        if not drawer.isVisible():
            window.controller.handle(Action.OPEN_DRAWER)
        self.assertTrue(wait_until(drawer.isVisible))
        self.assertTrue(window.host.isVisible())
        self.assertEqual(window.host.geometry(), small.geometry())  # the controls over it
        self.assertEqual(window.overlays.transport.icon("mini"), "full_window")
        moves = []
        window.surface.mouseMoved.connect(lambda: moves.append(True))
        mouse(window.surface, QEvent.Type.MouseMove, QPoint(120, 60), Qt.MouseButton.NoButton)
        self.assertEqual(moves, [True])  # the hover: the controls come
        with mock.patch.object(window.controller, "handle") as handle:
            QTest.keyClick(small, Qt.Key.Key_Up)
        handle.assert_called()
        QTest.keyClick(small, Qt.Key.Key_P, Qt.KeyboardModifier.AltModifier)  # back
        self.app.processEvents()
        self.assertFalse(window.mini_player_active())
        self.assertTrue(window.isVisible())
        self.assertFalse(small.isVisible())
        self.assertIs(window.player_page.parentWidget(), window.pages)
        self.assertIs(window.host._window, window)
        self.assertEqual(window.overlays.transport.icon("mini"), "mini_player")

    def test_a_drag_on_the_picture_moves_it_and_is_remembered(self):
        window = self.window
        self.show()
        self.mini()
        small = self.small()
        start = small.pos()
        with mock.patch.object(window.controller, "video_clicked") as clicked:
            mouse(window.surface, QEvent.Type.MouseButtonPress, QPoint(160, 90))
            mouse(window.surface, QEvent.Type.MouseMove, QPoint(220, 130))
            mouse(
                window.surface,
                QEvent.Type.MouseButtonRelease,
                QPoint(220, 130),
                Qt.MouseButton.NoButton,
            )
            elapse(QApplication.doubleClickInterval() + 50)
        clicked.assert_not_called()  # a drag, not a click (the channel list stays)
        self.assertEqual(small.pos(), start + QPoint(60, 40))
        geometry = small.geometry()
        self.assertEqual(
            window.config_manager.desktop_pip_rect,
            [geometry.x(), geometry.y(), geometry.width(), geometry.height()],
        )
        window.toggle_mini_player()  # (back before the window goes: see the Library's test)
        self.app.processEvents()

    def test_alt_p_in_fullscreen_floats_once_fullscreen_is_left(self):
        window = self.window
        pip = window.desktop_pip
        self.show()
        window.showFullScreen()
        self.app.processEvents()
        self.mini()
        self.assertIsNone(pip.reason())  # nothing floats for the desktop on the way out
        self.assertTrue(wait_until(window.mini_player_active))
        self.assertFalse(window.isFullScreen())
        self.assertTrue(window.mini_player.shown())
        self.assertFalse(pip.floating())
        window.toggle_mini_player()
        self.assertFalse(window.mini_player.shown())
        self.assertTrue(window.isVisible())

    def test_quitting_from_the_mini_player_closes_the_window_its_own_way(self):
        # The system's Quit closes only the windows shown, and Qt quits after the last window
        # closes only for one that was shown: the window comes back for its last steps.
        window = self.window
        self.show()
        self.mini()
        self.assertFalse(window.isVisible())
        self.app.closeAllWindows()  # the system's Quit (the Dock, logging out)
        self.assertTrue(window.closing())
        self.assertTrue(window.isVisible())
        self.assertIs(window.player_page.parentWidget(), window.pages)
        self.assertTrue(
            wait_until(lambda: not window.isVisible() and not window.close_retry.isActive())
        )

    def test_quit_in_the_mini_player_shows_the_window_for_its_last_steps(self):
        window = self.window
        self.show()
        self.mini()
        window.quit_chosen()  # ⌘Q, its keys reaching the window
        self.assertTrue(window.closing())
        self.assertTrue(window.isVisible())
        self.assertFalse(window.mini_player.shown())
        self.assertTrue(
            wait_until(lambda: not window.isVisible() and not window.close_retry.isActive())
        )

    def test_a_question_is_asked_in_the_window(self):  # as for the mini player in 2.13.1
        window = self.window
        self.show()
        self.wait_for_drawer()
        self.assertTrue(wait_until(lambda: window.catalog.items("live", ALL_GROUP_ID)))
        self.mini()
        self.assertTrue(window.mini_player.shown())
        window.record_at(window.catalog.items("live", ALL_GROUP_ID)[0])  # asked: it makes way
        self.assertFalse(window.mini_player_active())
        self.assertFalse(window.mini_player.shown())
        self.assertTrue(window.isVisible())
        self.assertTrue(window.record_card.isVisible())

    def test_a_question_asked_for_in_the_mini_player_brings_the_window_back(self):
        from ui import window_cards

        window = self.window
        self.show()
        self.mini()
        window_cards.tell("Check for updates", "QiTV is up to date.")  # asked for
        self.assertTrue(wait_until(window.window_cards.tv_card.isVisible))
        self.assertFalse(window.mini_player_active())
        self.assertTrue(window.isVisible())

    def test_the_library_leaves_the_mini_player_first(self):
        window = self.window
        self.show()
        self.wait_for_drawer()
        self.mini()
        small = self.small()
        # The menu's shortcut, typed in the small window (the window itself is hidden).
        QTest.keyClick(small, Qt.Key.Key_2, Qt.KeyboardModifier.ControlModifier)
        self.assertTrue(window.screens.library_shown())
        self.assertFalse(window.mini_player_active())
        self.assertFalse(window.mini_player.shown())
        self.assertTrue(window.isVisible())
        self.assertIs(window.player_page.parentWidget(), window.pages)
        # (Back to live TV before the window goes: Qt's offscreen platform crashes at exit
        # tearing down a window left in the Library after its page floated; 2.13.4 too.)
        window.screens.back_to_tv()
        self.app.processEvents()

    def test_a_maximized_window_and_keep_on_top_come_back_as_they_were(self):
        window = self.window
        self.show()
        window.showMaximized()
        self.assertTrue(wait_until(window.isMaximized))
        self.mini()
        self.assertTrue(window.mini_player.shown())
        self.mini()
        self.assertTrue(wait_until(window.isMaximized))
        window.showNormal()
        self.app.processEvents()
        self.mini()
        window.menus.keep_on_top.trigger()  # chosen in the mini player: applies once it ends
        self.mini()
        self.assertTrue(window.isVisible())
        self.assertTrue(window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
        self.assertFalse(window.windowFlags() & Qt.WindowType.FramelessWindowHint)

    def test_the_first_click_after_the_mini_player_is_not_swallowed(self):
        window = self.window
        self.show()
        drawer = self.wait_for_drawer()
        QTest.keyClick(window, Qt.Key.Key_Escape)
        self.mini()
        mouse(window.surface, QEvent.Type.MouseButtonPress, QPoint(60, 40))
        mouse(window.surface, QEvent.Type.MouseMove, QPoint(70, 60))
        self.mini()  # back (the press still held)
        drawer.close_drawer()
        window.host.clicked.emit()  # a click on the video around an overlay
        self.assertTrue(drawer.isVisible())

    def test_qitv_started_again_brings_the_mini_player_forward_not_a_dead_window(self):
        window = self.window
        self.show()
        self.mini()
        small = self.small()
        with mock.patch.object(small, "activateWindow") as activated:
            window.come_to_front()
        activated.assert_called_once()
        self.assertFalse(window.isVisible())
        self.assertTrue(window.mini_player_active())
        self.assertTrue(small.isVisible())

    def test_while_qitv_is_inactive_the_pointer_over_it_shows_the_controls(self):
        window = self.window
        self.show()
        self.mini()
        small = self.small()
        moves = []
        window.pointer.realMoved.connect(lambda: moves.append(True))
        window.pointer._signalled = None
        window.mini_player._app_active = lambda: False
        window._on_app_state(Qt.ApplicationState.ApplicationInactive)  # another app in front
        where = small.geometry().center()
        window.mini_player._cursor_pos = lambda: where
        self.assertTrue(wait_until(lambda: bool(moves), mini_player.HOVER_MS * 6))
        window.toggle_mini_player()  # (back before the window goes: see the Library's test)
        self.app.processEvents()

    def test_presses_on_it_and_its_controls_bring_qitv_forward(self):
        window = self.window
        with mock.patch("ui.tv_window.macos_window.let_presses_activate") as activating:
            self.show()
            self.mini()
        targets = {call.args[0] for call in activating.call_args_list}
        self.assertEqual(targets, {self.small(), window.host})

    def test_a_desktop_change_is_asked_about_never_while_it_shows(self):
        # The window is hidden (not fullscreen) for the mini player: nothing floats for it.
        window = self.window
        self.show()
        window.config_manager.covered_pip = True
        self.mini()
        self.assertIsNone(window.desktop_pip.reason())
        self.assertFalse(window._may_float_covered())
        self.mini()
        self.assertEqual(window.desktop_pip.reason(), "covered")  # its watch is back

    def test_the_small_window_keeps_its_pictures_shape_and_its_picture_fills_it(self):
        main, small = _BoxedPlayer(16 / 9), _BoxedPlayer()
        players = [("main", main), ("pip", small)]
        with mock.patch.object(self.window, "_players", return_value=players):
            window = self.window
            self.show()
            self.mini()
            self.app.processEvents()
            mini = self.small()
            # macOS's own edge resize (Qt makes the frameless window resizable): no press of
            # QiTV's, at a size the picture would show black bars in.
            mini.setGeometry(QRect(mini.pos(), QSize(546, 330)))

            def in_shape():
                return picture.same_shape(mini.width() / mini.height(), 16 / 9)

            self.assertTrue(wait_until(in_shape))
            self.app.processEvents()
            box = main.box()
            self.assertIsNotNone(box)  # told its box: the picture fills it to the last pixel
            self.assertIsNotNone(picture.box_aspect(16 / 9, *box))
            window.toggle_mini_player()
            self.app.processEvents()
            self.assertIsNone(main.box())  # the large view again: the viewer's own shape


if __name__ == "__main__":
    unittest.main()
