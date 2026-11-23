"""The painted menu (2.13.1): every right-click and button menu, in the drawer's look.

A menu is plain data (Menu: its entries) that one overlay per page shows (PopupMenu). The keys
(the phone's arrows, OK and Back among them) and the mouse reach every entry; it never waits
for an answer: an entry's callback runs once the menu closed, after its close callbacks. It
opens at the pointer or beside a row or a button, and stays inside the window, scrolling what
does not fit.
"""

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QSize, Qt
from PySide6.QtGui import QAccessible, QMouseEvent, QRegion, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from timer_fixtures import pause

from services import i18n
from ui.keymap import Action
from ui.overlays.popup_menu import ABOVE, BELOW, RIGHT, Menu, PopupMenu, popup, popup_for

# The Turkish of a channel's longest entries (2.13's catalog), to fit at 640×360.
LONG_TEXTS = ("Gizle (ebeveyn denetimi)", "Son izlenenleri temizle", "Yayın bağlantısını kopyala")


class _Host(QWidget):
    """The page: what the menu takes never reaches it."""

    def __init__(self) -> None:
        super().__init__()
        self.setMouseTracking(True)
        self.presses: list[QPoint] = []
        self.moves: list[QPoint] = []
        self.wheels: list[QPoint] = []

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self.presses.append(event.position().toPoint())

    def wheelEvent(self, event: QWheelEvent) -> None:
        self.wheels.append(event.position().toPoint())

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        self.moves.append(event.position().toPoint())


class PopupCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.done: list[str] = []

    def host(self, width=1280, height=720) -> _Host:
        host = _Host()
        host.resize(width, height)
        host.show()
        self.addCleanup(host.deleteLater)
        self.addCleanup(host.close)
        return host

    def overlay(self, host=None) -> PopupMenu:
        return PopupMenu(host if host is not None else self.host())

    def note(self, what):
        return lambda: self.done.append(what)

    def channel_menu(self) -> Menu:
        """A channel's, as ui/context_actions.py builds it (one entry disabled to test)."""
        menu = Menu()
        menu.add("Add to favorites", self.note("fav"))
        menu.add("Open in picture-in-picture", self.note("pip"))
        menu.add("Record at a time…", self.note("rec"), enabled=False)
        menu.add_separator()
        sub = menu.add_menu("Clear Recents")
        sub.add("Clear all 3 channels", self.note("clear"))
        menu.add("Hide (parental controls)", self.note("hide"))
        menu.add_separator()
        menu.add("Play in VLC", self.note("vlc"))
        menu.add("Play in MPV", self.note("mpv"))
        menu.add_separator()
        menu.add("Copy stream link", self.note("copy"))
        menu.on_close(self.note("closed"))
        return menu

    def open_at(self, overlay, menu, point=QPoint(100, 80)):
        overlay.open(menu, at=overlay.parentWidget().mapToGlobal(point))
        self.assertTrue(overlay.shown())
        return overlay

    def current(self, overlay) -> str:
        entry = overlay.current()
        return entry.text if entry is not None else ""

    def send(self, overlay, kind, point, button=Qt.MouseButton.LeftButton):
        buttons = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseMove else button
        button = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseMove else button
        local = QPointF(point)
        event = QMouseEvent(
            kind,
            local,
            QPointF(overlay.mapToGlobal(local.toPoint())),
            button,
            buttons,
            Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(overlay, event)

    def click(self, overlay, text, button=Qt.MouseButton.LeftButton):
        center = overlay.entry_rect(overlay_entry(overlay, text)).center()
        self.send(overlay, QEvent.Type.MouseButtonPress, center, button)
        self.send(overlay, QEvent.Type.MouseButtonRelease, center, button)

    def assertInside(self, host, rect):
        self.assertTrue(
            QRect(0, 0, host.width(), host.height()).contains(rect), (rect, host.size())
        )


def overlay_entry(overlay, text):
    for level in range(overlay.depth()):
        for entry in overlay.entries(level):
            if not entry.separator and entry.text == text:
                return entry
    raise AssertionError(f"{text!r} is not shown")


class MenuModelTests(PopupCase):
    def test_separators_never_lead_end_or_double(self):
        menu = Menu()
        menu.add_separator()
        menu.add("One")
        menu.add_separator()
        menu.add_separator()
        menu.add("Two")
        menu.add_separator()
        shown = menu.shown_entries()
        self.assertEqual([e.text if not e.separator else "-" for e in shown], ["One", "-", "Two"])
        self.assertEqual(menu.texts(), ["One", "Two"])
        self.assertIs(menu.entry("Two"), shown[-1])

    def test_an_entry_ticks_then_acts_and_a_disabled_one_does_nothing(self):
        menu = Menu()
        tick = menu.add("Demo TV", self.note("tick"), checked=False)
        off = menu.add("Looking for devices…", self.note("off"), enabled=False)
        tick.trigger()
        self.assertTrue(tick.checked)
        off.trigger()
        self.assertEqual(self.done, ["tick"])
        sub = menu.add_menu("Clear Recents")
        sub.add("Clear all 3 channels", self.note("clear"))
        menu.entry("Clear Recents").trigger()  # a submenu's entry only opens it
        self.assertEqual(self.done, ["tick"])

    def test_its_close_callbacks_run_once(self):
        menu = Menu()
        menu.on_close(self.note("closed"))
        menu.closed()
        menu.closed()
        self.assertEqual(self.done, ["closed"])


class KeyTests(PopupCase):
    def test_it_opens_at_the_pointer_on_its_first_entry(self):
        overlay = self.open_at(self.overlay(), self.channel_menu())
        self.assertEqual(overlay.panel().topLeft(), QPoint(100, 80))
        self.assertEqual(self.current(overlay), "Add to favorites")
        self.assertEqual(overlay.depth(), 1)

    def test_the_arrows_pass_separators_and_disabled_entries_and_go_round(self):
        overlay = self.open_at(self.overlay(), self.channel_menu())
        order = []
        for _ in range(8):
            self.assertTrue(overlay.handle(Action.NAV_DOWN))
            order.append(self.current(overlay))
        self.assertEqual(
            order,
            [
                "Open in picture-in-picture",
                "Clear Recents",  # Record at a time… is disabled
                "Hide (parental controls)",
                "Play in VLC",
                "Play in MPV",
                "Copy stream link",
                "Add to favorites",  # round
                "Open in picture-in-picture",
            ],
        )
        overlay.handle(Action.NAV_UP)
        overlay.handle(Action.NAV_UP)
        self.assertEqual(self.current(overlay), "Copy stream link")  # round, upwards
        overlay.handle(Action.NAV_HOME)
        self.assertEqual(self.current(overlay), "Add to favorites")
        overlay.handle(Action.NAV_PAGE_DOWN)
        self.assertEqual(self.current(overlay), "Copy stream link")
        overlay.handle(Action.NAV_PAGE_UP)
        self.assertEqual(self.current(overlay), "Add to favorites")
        overlay.handle(Action.NAV_END)
        self.assertEqual(self.current(overlay), "Copy stream link")

    def test_enter_closes_the_menu_then_acts(self):
        overlay = self.open_at(self.overlay(), self.channel_menu())
        overlay.handle(Action.NAV_DOWN)
        self.assertTrue(overlay.handle(Action.NAV_ACTIVATE))
        self.assertEqual(self.done, ["closed", "pip"])
        self.assertFalse(overlay.shown())
        self.assertFalse(overlay.isVisible())
        self.assertFalse(overlay.handle(Action.NAV_ACTIVATE))  # closed: keys go on
        self.assertEqual(self.done, ["closed", "pip"])

    def test_ok_space_and_right_choose_too(self):
        for action, payload in ((Action.OK, ""), (Action.NAV_TEXT, " "), (Action.NAV_RIGHT, "")):
            with self.subTest(action=action):
                self.done = []
                overlay = self.open_at(self.overlay(), self.channel_menu())
                overlay.handle(action, payload)
                self.assertEqual(self.done, ["closed", "fav"])

    def test_esc_back_left_and_backspace_close_it(self):
        for action in (Action.ESCAPE, Action.NAV_LEFT, Action.NAV_BACKSPACE, Action.PREVIOUS):
            with self.subTest(action=action):
                self.done = []
                overlay = self.open_at(self.overlay(), self.channel_menu())
                self.assertTrue(overlay.handle(action))
                self.assertFalse(overlay.shown())
                self.assertEqual(self.done, ["closed"])

    def test_right_opens_the_submenu_beside_its_entry_and_left_closes_only_it(self):
        overlay = self.open_at(self.overlay(), self.channel_menu())
        overlay.handle(Action.NAV_TEXT, "c")
        self.assertEqual(self.current(overlay), "Clear Recents")
        overlay.handle(Action.NAV_RIGHT)
        self.assertEqual(overlay.depth(), 2)
        self.assertEqual(self.current(overlay), "Clear all 3 channels")
        self.assertGreaterEqual(overlay.panel(1).left(), overlay.panel(0).right())
        opener = overlay.entry_rect(overlay_entry(overlay, "Clear Recents"))
        self.assertLessEqual(overlay.panel(1).top(), opener.top())
        self.assertEqual(self.done, [])
        overlay.handle(Action.NAV_LEFT)
        self.assertEqual(overlay.depth(), 1)
        self.assertTrue(overlay.shown())
        self.assertEqual(self.current(overlay), "Clear Recents")
        overlay.handle(Action.NAV_ACTIVATE)  # Enter opens it too
        self.assertEqual(overlay.depth(), 2)
        overlay.handle(Action.ESCAPE)  # Back: one level
        self.assertEqual(overlay.depth(), 1)
        overlay.handle(Action.NAV_RIGHT)
        overlay.handle(Action.NAV_ACTIVATE)
        self.assertEqual(self.done, ["closed", "clear"])
        self.assertFalse(overlay.shown())

    def test_a_letter_goes_to_the_next_entry_it_starts(self):
        overlay = self.open_at(self.overlay(), self.channel_menu())
        hops = []
        for letter in ("p", "P", "p", "c", "c", "z", "o"):
            overlay.handle(Action.NAV_TEXT, letter)
            hops.append(self.current(overlay))
        self.assertEqual(
            hops,
            [
                "Play in VLC",
                "Play in MPV",
                "Play in VLC",  # round
                "Copy stream link",
                "Clear Recents",
                "Clear Recents",  # nothing starts with z: it stays
                "Open in picture-in-picture",
            ],
        )
        overlay.handle(Action.NAV_TEXT, "r")  # Record at a time… is disabled: never reached
        self.assertEqual(self.current(overlay), "Open in picture-in-picture")
        menu = Menu()
        menu.add("★ Favorites")
        menu.add("Recent")
        self.open_at(overlay, menu)
        overlay.handle(Action.NAV_TEXT, "r")
        overlay.handle(Action.NAV_TEXT, "f")
        self.assertEqual(self.current(overlay), "★ Favorites")  # its first letter, not ★

    def test_the_menu_key_closes_it(self):  # 2.13.1: as it opened it
        overlay = self.open_at(self.overlay(), self.channel_menu())
        self.click(overlay, "Clear Recents")
        self.assertTrue(overlay.handle(Action.CONTEXT_MENU))
        self.assertFalse(overlay.shown())
        self.assertEqual(self.done, ["closed"])

    def test_the_sound_keys_and_fullscreen_go_on_and_every_other_key_is_its_own(self):
        overlay = self.open_at(self.overlay(), self.channel_menu())
        for action in (Action.MUTE, Action.VOL_UP, Action.VOL_DOWN, Action.FULLSCREEN):
            self.assertFalse(overlay.handle(action))
        for action in (Action.SEARCH, Action.FAVORITE, Action.GUIDE, Action.DIGIT):
            self.assertTrue(overlay.handle(action, "1"))
        self.assertTrue(overlay.shown())
        self.assertEqual(self.done, [])
        overlay.handle(Action.CH_NEXT)  # Ch± (the player's arrows) move as ↑ ↓ do
        self.assertEqual(self.current(overlay), "Open in picture-in-picture")
        overlay.handle(Action.CH_PREV)
        self.assertEqual(self.current(overlay), "Add to favorites")

    def test_a_tick_that_keeps_the_menu_open(self):
        menu = Menu()
        demo = menu.add("Demo TV", self.note("demo"), checked=True, keep_open=True)
        harbour = menu.add("Harbour TV", self.note("harbour"), checked=False, keep_open=True)
        menu.add_separator()
        menu.add("Refresh films and series", self.note("refresh"))
        menu.on_close(self.note("closed"))
        overlay = self.open_at(self.overlay(), menu)
        overlay.handle(Action.NAV_ACTIVATE)
        self.assertFalse(demo.checked)
        self.assertTrue(overlay.shown())
        overlay.handle(Action.NAV_DOWN)
        overlay.handle(Action.NAV_TEXT, " ")
        self.assertTrue(harbour.checked)
        self.click(overlay, "Harbour TV")
        self.assertFalse(harbour.checked)
        self.assertTrue(overlay.shown())
        self.assertEqual(self.done, ["demo", "harbour", "harbour"])
        overlay.handle(Action.NAV_END)
        overlay.handle(Action.NAV_ACTIVATE)
        self.assertEqual(self.done[-2:], ["closed", "refresh"])


class MouseTests(PopupCase):
    def test_the_entry_under_the_pointer_is_highlighted_and_a_click_chooses_it(self):
        overlay = self.open_at(self.overlay(), self.channel_menu())
        mpv = overlay.entry_rect(overlay_entry(overlay, "Play in MPV"))
        self.send(overlay, QEvent.Type.MouseMove, mpv.center())
        self.assertEqual(self.current(overlay), "Play in MPV")
        record = overlay.entry_rect(overlay_entry(overlay, "Record at a time…"))
        self.send(overlay, QEvent.Type.MouseMove, record.center())
        self.assertEqual(self.current(overlay), "Play in MPV")  # disabled: never highlighted
        self.click(overlay, "Record at a time…")
        self.assertTrue(overlay.shown())
        self.click(overlay, "Play in MPV")
        self.assertEqual(self.done, ["closed", "mpv"])
        self.assertFalse(overlay.isVisible())

    def test_a_right_click_chooses_too_but_not_a_release_alone(self):
        overlay = self.open_at(self.overlay(), self.channel_menu())
        vlc = overlay.entry_rect(overlay_entry(overlay, "Play in VLC")).center()
        # The press that opened the menu ended over it: that release is no choice.
        self.send(overlay, QEvent.Type.MouseButtonRelease, vlc, Qt.MouseButton.RightButton)
        self.assertEqual(self.done, [])
        self.click(overlay, "Play in VLC", Qt.MouseButton.RightButton)
        self.assertEqual(self.done, ["closed", "vlc"])

    def test_a_click_on_the_submenus_entry_opens_it(self):
        overlay = self.open_at(self.overlay(), self.channel_menu())
        self.click(overlay, "Clear Recents")
        self.assertEqual(overlay.depth(), 2)
        hide = overlay.entry_rect(overlay_entry(overlay, "Hide (parental controls)"))
        self.send(overlay, QEvent.Type.MouseMove, hide.center())  # another of its menu's
        self.assertEqual(overlay.depth(), 1)
        self.assertEqual(self.current(overlay), "Hide (parental controls)")
        self.click(overlay, "Clear Recents")
        self.click(overlay, "Clear all 3 channels")
        self.assertEqual(self.done, ["closed", "clear"])

    def test_a_click_beside_the_menu_closes_it_and_does_nothing_more(self):
        host = self.host()
        overlay = self.open_at(self.overlay(host), self.channel_menu())
        pause(QApplication.doubleClickInterval() + 20)
        QTest.mouseClick(host.windowHandle(), Qt.MouseButton.LeftButton, pos=QPoint(900, 600))
        self.assertFalse(overlay.shown())
        self.assertEqual(self.done, ["closed"])
        self.assertEqual(host.presses, [])

    def test_a_click_elsewhere_in_the_window_closes_it_and_does_nothing_more(self):
        host = self.host()
        page = QWidget(host)  # the menu's page covers part of the window
        page.setGeometry(0, 0, 600, 400)
        page.show()
        overlay = self.open_at(self.overlay(page), self.channel_menu())
        pause(QApplication.doubleClickInterval() + 20)
        QTest.mouseClick(host.windowHandle(), Qt.MouseButton.LeftButton, pos=QPoint(900, 600))
        self.assertFalse(overlay.shown())
        self.assertEqual(host.presses, [])  # it only closed the menu
        QTest.mouseClick(host.windowHandle(), Qt.MouseButton.LeftButton, pos=QPoint(900, 600))
        self.assertEqual(host.presses, [QPoint(900, 600)])  # then clicks go on

    def test_the_wheel_elsewhere_closes_it_as_a_click_does_and_does_nothing_more(self):
        host = self.host()
        overlay = self.open_at(self.overlay(host), self.channel_menu())

        def turn() -> None:
            at = QPointF(900, 600)
            wheel = QWheelEvent(
                at,
                QPointF(host.mapToGlobal(at.toPoint())),
                QPoint(),
                QPoint(0, -120),
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
                Qt.ScrollPhase.NoScrollPhase,
                False,
            )
            QApplication.sendEvent(host, wheel)

        turn()  # over the page, not the menu
        self.assertFalse(overlay.shown())
        self.assertEqual(host.wheels, [])  # the list under it stays where the menu was for
        turn()
        self.assertEqual(host.wheels, [QPoint(900, 600)])  # then the wheel goes on

    def test_the_second_click_of_the_double_click_that_opened_it_keeps_it(self):
        host = self.host()
        overlay = self.open_at(self.overlay(host), self.channel_menu())
        self.send(overlay, QEvent.Type.MouseButtonDblClick, QPoint(900, 600))
        self.assertTrue(overlay.shown())

    def test_the_second_click_on_the_button_that_opened_it_closes_it(self):  # 2.13.5
        class Controls(_Host):
            def control_at(self, pos):
                return "cast" if pos.x() >= 800 else "play"

        host = Controls()
        host.resize(1280, 720)
        host.show()
        self.addCleanup(host.close)
        overlay = self.open_at(self.overlay(host), self.channel_menu())
        QApplication.sendEvent(host, self.mouse(host, QEvent.Type.MouseButtonDblClick, 900, 600))
        self.assertFalse(overlay.shown())  # a menu's own button, quickly again
        overlay = self.open_at(self.overlay(host), self.channel_menu())
        QApplication.sendEvent(host, self.mouse(host, QEvent.Type.MouseButtonDblClick, 100, 600))
        self.assertTrue(overlay.shown())  # another's: still the double-click that opened it

    @staticmethod
    def mouse(widget, kind, x, y):
        local = QPointF(x, y)
        return QMouseEvent(
            kind,
            local,
            QPointF(widget.mapToGlobal(local.toPoint())),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )

    def test_the_mouses_back_button_closes_one_level(self):
        overlay = self.open_at(self.overlay(), self.channel_menu())
        self.click(overlay, "Clear Recents")
        inside = overlay.panel(1).center()
        self.send(overlay, QEvent.Type.MouseButtonPress, inside, Qt.MouseButton.BackButton)
        self.assertEqual(overlay.depth(), 1)
        self.send(overlay, QEvent.Type.MouseButtonPress, inside, Qt.MouseButton.BackButton)
        self.assertFalse(overlay.shown())

    def test_the_wheel_scrolls_a_long_menu_and_never_reaches_the_page(self):
        host = self.host(640, 360)
        menu = Menu()
        for n in range(30):
            menu.add(f"Day {n}")
        overlay = self.open_at(self.overlay(host), menu, QPoint(10, 10))
        first = overlay.entry_rect(menu.entries[0])
        self.assertFalse(first.isEmpty())
        self.assertTrue(overlay.entry_rect(menu.entries[29]).isEmpty())  # not in sight
        center = QPointF(overlay.panel().center())
        wheel = QWheelEvent(
            center,
            QPointF(overlay.mapToGlobal(center.toPoint())),
            QPoint(),
            QPoint(0, -120 * 40),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase,
            False,
        )
        QApplication.sendEvent(overlay, wheel)
        self.assertTrue(wheel.isAccepted())
        self.assertTrue(overlay.entry_rect(menu.entries[0]).isEmpty())
        self.assertFalse(overlay.entry_rect(menu.entries[29]).isEmpty())


class PlaceTests(PopupCase):
    def small_menu(self) -> Menu:
        menu = Menu()
        for text in ("Play in VLC", "Play in MPV", "Copy stream link"):
            menu.add(text)
        return menu

    def test_beside_a_row_below_or_above_a_button(self):
        host = self.host()
        overlay = self.overlay(host)
        row = QRect(host.mapToGlobal(QPoint(100, 200)), QSize(300, 40))
        overlay.open(self.small_menu(), beside=row, side=RIGHT)
        self.assertGreaterEqual(overlay.panel().left(), 400)
        self.assertLessEqual(abs(overlay.panel().top() - 200), 12)  # its first row by the row
        button = QRect(host.mapToGlobal(QPoint(500, 300)), QSize(40, 40))
        overlay.open(self.small_menu(), beside=button, side=BELOW)
        self.assertGreaterEqual(overlay.panel().top(), 340)
        self.assertEqual(overlay.panel().left(), 500)
        overlay.open(self.small_menu(), beside=button, side=ABOVE)
        self.assertLessEqual(overlay.panel().bottom(), 300)
        self.assertEqual(overlay.panel().left(), 500)

    def test_a_rect_for_where_it_opens_is_a_row_the_keys_are_on(self):  # the menu key
        host = self.host()
        overlay = self.overlay(host)
        row = QRect(host.mapToGlobal(QPoint(100, 200)), QSize(300, 40))
        self.assertIs(popup(self.small_menu(), host, at=row), overlay)
        self.assertGreaterEqual(overlay.panel().left(), 400)  # beside it, not at a point
        self.assertLessEqual(abs(overlay.panel().top() - 200), 12)

    def test_it_turns_over_at_the_windows_edges(self):
        host = self.host()
        overlay = self.overlay(host)
        self.open_at(overlay, self.channel_menu(), QPoint(1270, 710))  # the bottom right
        panel = overlay.panel()
        self.assertLessEqual(panel.right(), 1270)
        self.assertLessEqual(panel.bottom(), 710)
        row = QRect(host.mapToGlobal(QPoint(980, 100)), QSize(290, 40))
        overlay.open(self.channel_menu(), beside=row, side=RIGHT)  # no room on its right
        self.assertLessEqual(overlay.panel().right(), 980)
        button = QRect(host.mapToGlobal(QPoint(40, 10)), QSize(40, 40))
        overlay.open(self.channel_menu(), beside=button, side=ABOVE)  # no room above
        self.assertGreaterEqual(overlay.panel().top(), 50)

    def test_it_stays_inside_a_small_window_in_turkish_with_its_submenu_open(self):
        host = self.host(640, 360)
        overlay = self.overlay(host)
        menu = self.channel_menu()
        for entry, text in zip((menu.entries[5], menu.entries[4], menu.entries[10]), LONG_TEXTS):
            entry.text = text
        menu.entries[4].submenu.entries[0].text = "3 kanalın tümünü temizle"
        for point in (QPoint(630, 350), QPoint(5, 5), QPoint(320, 180), QPoint(600, 20)):
            with self.subTest(point=point):
                self.open_at(overlay, menu, point)
                overlay.handle(Action.NAV_TEXT, "s")  # Son izlenenleri temizle ›
                overlay.handle(Action.NAV_RIGHT)
                self.assertEqual(overlay.depth(), 2)
                for level in (0, 1):
                    self.assertInside(host, overlay.panel(level))
                self.assertFalse(overlay.panel(0).intersects(overlay.panel(1)))
                for entry in overlay.entries(0):
                    if not entry.separator:  # every entry in sight, whole
                        rect = overlay.entry_rect(entry)
                        self.assertTrue(overlay.panel(0).contains(rect), entry.text)
                        self.assertEqual(overlay.cut(entry), entry.text)
                overlay.close_menu()

    def test_a_menu_taller_than_the_window_scrolls_to_its_highlight(self):
        host = self.host(640, 360)
        menu = Menu()
        for n in range(30):
            menu.add(f"Day {n}")
        overlay = self.open_at(self.overlay(host), menu, QPoint(200, 300))
        self.assertInside(host, overlay.panel())
        overlay.handle(Action.NAV_END)
        last = overlay.entry_rect(menu.entries[29])
        self.assertFalse(last.isEmpty())
        self.assertTrue(overlay.panel().contains(last))
        overlay.handle(Action.NAV_DOWN)  # round: the first again, in sight
        self.assertTrue(overlay.panel().contains(overlay.entry_rect(menu.entries[0])))


class LifecycleTests(PopupCase):
    def test_it_closes_once_when_its_page_changes_size_or_hides(self):
        host = self.host()
        overlay = self.open_at(self.overlay(host), self.channel_menu())
        host.resize(1000, 600)
        QApplication.processEvents()
        self.assertFalse(overlay.shown())
        self.assertEqual(self.done, ["closed"])
        self.open_at(overlay, self.channel_menu())
        host.hide()
        QApplication.processEvents()
        self.assertFalse(overlay.shown())
        host.show()
        QApplication.processEvents()
        self.assertFalse(overlay.isVisible())  # it stays closed
        self.assertEqual(self.done, ["closed", "closed"])

    def test_a_page_resized_while_hidden_keeps_the_menu_it_shows_for(self):
        # The player's overlay host is hidden while nothing shows on it: a right-click on the
        # picture shows it, and its resize, waiting while it was hidden, comes then.
        host = _Host()
        self.addCleanup(host.deleteLater)
        self.addCleanup(host.close)
        host.resize(1280, 720)
        overlay = self.overlay(host)
        overlay.open(self.channel_menu(), at=host.mapToGlobal(QPoint(100, 80)))
        host.show()
        QApplication.processEvents()
        self.assertTrue(overlay.shown())
        self.assertEqual(self.done, [])

    def test_the_pages_own_watch_outlives_a_menu_it_opened(self):
        host = self.host()
        overlay = self.overlay(host)
        overlay.open(self.channel_menu(), at=host.mapToGlobal(QPoint(50, 50)), opener=host)
        overlay.close_menu()  # opened by its page itself (the Library's genres, its pill)
        self.open_at(overlay, self.channel_menu())
        host.resize(1000, 600)
        QApplication.processEvents()
        self.assertFalse(overlay.shown())  # the page's resize still closes it

    def test_a_move_over_it_goes_on_to_its_page(self):
        host = self.host()
        overlay = self.open_at(self.overlay(host), self.channel_menu())
        mpv = overlay.entry_rect(overlay_entry(overlay, "Play in MPV")).center()
        self.send(overlay, QEvent.Type.MouseMove, mpv)
        self.assertEqual(self.current(overlay), "Play in MPV")
        self.assertIn(mpv, host.moves)  # the pointer stays up while it moves (ui.pointer)

    def test_it_closes_when_the_list_that_opened_it_hides(self):
        host = self.host()
        drawer = QWidget(host)
        drawer.show()
        overlay = self.overlay(host)
        overlay.open(self.channel_menu(), at=host.mapToGlobal(QPoint(50, 50)), opener=drawer)
        drawer.hide()
        self.assertFalse(overlay.shown())
        self.assertEqual(self.done, ["closed"])

    def test_another_menu_closes_the_one_shown(self):
        overlay = self.open_at(self.overlay(), self.channel_menu())
        other = Menu()
        other.add("Show in Folder", self.note("folder"))
        self.open_at(overlay, other)
        self.assertEqual(self.done, ["closed"])
        self.assertIs(overlay.menu(), other)
        self.assertEqual(self.current(overlay), "Show in Folder")

    def test_a_menu_with_nothing_in_it_does_not_show(self):
        menu = Menu()
        menu.add_separator()
        menu.on_close(self.note("closed"))
        overlay = self.overlay()
        overlay.open(menu, at=QPoint(0, 0))
        self.assertFalse(overlay.shown())
        self.assertEqual(self.done, ["closed"])

    def test_its_mask_is_its_panels(self):
        overlay = self.open_at(self.overlay(), self.channel_menu())
        self.assertEqual(overlay.mask_region(), QRegion(overlay.panel()))
        self.click(overlay, "Clear Recents")
        both = QRegion(overlay.panel(0)).united(QRegion(overlay.panel(1)))
        self.assertEqual(overlay.mask_region(), both)


class PageTests(PopupCase):
    def test_a_menu_shows_on_the_page_of_the_widget_that_asks(self):
        host = self.host()
        page = QWidget(host)
        page.resize(host.size())
        page.show()
        own = PopupMenu(page)
        row_list = QWidget(page)
        self.assertIs(popup_for(row_list), own)
        self.assertIs(popup(self.channel_menu(), row_list, at=QPoint(10, 10)), own)
        self.assertTrue(own.shown())
        lone = QWidget()
        self.addCleanup(lone.deleteLater)
        made = popup_for(lone)  # a widget of no page with one: one is made in its window
        self.assertIsInstance(made, PopupMenu)
        self.assertIs(popup_for(lone), made)
        self.assertIsNone(popup(self.channel_menu(), None, at=QPoint(10, 10)))


class SaidTests(PopupCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(QAccessible, "updateAccessibility")
        self.posted = patcher.start()
        self.addCleanup(patcher.stop)

    def said(self):
        return [
            call.args[0].message()
            for call in self.posted.call_args_list
            if call.args[0].type() == QAccessible.Event.Announcement
        ]

    def test_it_says_what_it_is_then_each_entry_highlighted(self):
        overlay = self.overlay()
        self.assertEqual(overlay.accessibleName(), "Menu")
        menu = self.channel_menu()
        menu.entries[1].checked = True
        self.open_at(overlay, menu)
        overlay.handle(Action.NAV_DOWN)
        overlay.handle(Action.NAV_DOWN)
        self.assertEqual(
            self.said(), ["Menu, Add to favorites", "✓ Open in picture-in-picture", "Clear Recents"]
        )
        overlay.handle(Action.ESCAPE)
        self.assertEqual(overlay.accessibleName(), "Menu")

    def test_its_name_is_in_the_active_language(self):
        self.assertEqual(i18n.set_language("tr"), "tr")
        self.addCleanup(i18n.set_language, "en")
        self.assertEqual(self.overlay().accessibleName(), "Menü")


if __name__ == "__main__":
    unittest.main()
