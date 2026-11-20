"""Typing finds a setting (Settings in the drawer, plan 3): the rows of every section and layer
whose title or help has the words typed, folded as the channel list's find folds them, each
with where it is; Enter or a click opens it there, Esc or ✕ puts the column back. Never by a
text's value or a secret. A text being edited takes the typing instead; the phone's typing
finds too."""

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from timer_fixtures import elapse

from services import settings_model as m
from services.settings_model import PLAYBACK, PROVIDERS, TIMESHIFT, Row, Section, sections
from tests import test_tv_window as base
from tests.test_settings_controller import FakeValues, providers_section
from ui.keymap import Action
from ui.overlays.drawer import find_clear_tooltip
from ui.overlays.settings_drawer import SettingsDrawer
from ui.settings_controller import MOUSE_FORWARD, SettingsController

KEY = "fictional0zebrafish0key0WXYZ"


def with_providers(*names):
    """Settings' sections ("Darwin"), Providers listing `names`, each opening its layer."""
    return tuple(providers_section(*names) if s.key == PROVIDERS else s for s in sections("Darwin"))


class FindCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.host = QWidget()
        self.host.resize(1280, 720)
        self.host.show()
        self.addCleanup(self.host.deleteLater)
        self.addCleanup(self.host.close)
        self.values = FakeValues(
            live_latency="auto",
            opensubtitles_api_key=KEY,
            opensubtitles_username="kumquat_viewer",
            opensubtitles_password="",
            tmdb_token="",
        )
        self.drawer = SettingsDrawer(
            lambda row: self.controller.value_text(row),
            lambda row: self.controller.lit(row),
            self.host,
        )
        self.shown = with_providers("Demo TV", "Harbour")
        self.controller = SettingsController(
            self.drawer, self.values, lambda: self.shown, lambda tab: None, lambda: None
        )

    def tearDown(self):
        elapse(QApplication.doubleClickInterval() + 80)  # (a later test's first click lands)

    def key(self, action, payload=""):
        self.assertTrue(self.controller.handle(action, payload), action)

    def type(self, text):
        for char in text:
            self.key(Action.NAV_TEXT, char)

    def found(self):
        """What the find lists: (row key, title, its sub line), in order."""
        self.assertTrue(self.drawer.finding())
        return [(row.key, row.text, row.sub) for row in self.drawer._right_rows()]

    def found_keys(self):
        return [key for key, _text, _sub in self.found()]

    def to_row(self, key):
        """From the left column, the keys on the right column's row `key` (↓ from its first)."""
        self.key(Action.NAV_RIGHT)
        for _ in range(len(self.drawer._right_rows())):
            if self.drawer.focused_row().key == key:
                return
            self.key(Action.NAV_DOWN)
        self.fail(f"no row {key}")

    def focus(self, key):
        """The keys on the found row `key` (↓ from the first)."""
        for _ in range(len(self.drawer._right_rows())):
            if self.drawer.focused_row().key == key:
                return
            self.key(Action.NAV_DOWN)
        self.fail(f"no found row {key}")

    def click(self, point: QPointF, double=False):
        """A click at `point`; `double`: and a double-click's second press there, as Qt
        reports it."""
        QTest.mouseClick(self.drawer, Qt.MouseButton.LeftButton, pos=point.toPoint())
        if double:
            where = QPointF(point.toPoint())
            event = QMouseEvent(
                QEvent.Type.MouseButtonDblClick,
                where,
                QPointF(self.drawer.mapToGlobal(where)),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
            QApplication.sendEvent(self.drawer, event)


class SettingsFindTests(FindCase):
    def test_typing_finds_rows_across_sections(self):
        self.controller.open(PLAYBACK)
        self.type("lat")
        self.assertEqual(self.drawer.find_text(), "lat")
        self.assertEqual(self.drawer.focus_side(), "right")
        self.assertEqual(self.found()[0], ("live_latency", "Live latency", "Playback"))
        self.assertEqual(self.drawer.focused_row().key, "live_latency")  # the best one
        self.assertEqual(self.drawer._spoken(), "Live latency, Playback")
        self.assertEqual(self.drawer.path(), (PLAYBACK,))  # nothing opened yet
        # Every section and layer: a provider's own rows, where each is as its sub line.
        self.key(Action.NAV_BACKSPACE)
        self.key(Action.NAV_BACKSPACE)
        self.key(Action.NAV_BACKSPACE)
        self.assertFalse(self.drawer.finding())  # its last letter went: so did the find
        self.type("check the conn")
        self.assertEqual(
            self.found(),
            [
                ("provider/Demo TV/check", "Check the connection", "Providers › Demo TV"),
                ("provider/Harbour/check", "Check the connection", "Providers › Harbour"),
            ],
        )
        self.controller.handle(Action.ESCAPE)
        self.type("covers")  # two layers down, its section and its layer named
        self.assertIn(
            (
                "covered_pip",
                "Float the picture when another window covers QiTV",
                "Playback › Picture in picture",
            ),
            self.found(),
        )
        self.controller.handle(Action.ESCAPE)
        # A provider's name is its row's title (as given).
        self.type("harbour")
        self.assertEqual(self.found_keys()[0], "provider/Harbour")
        self.controller.handle(Action.ESCAPE)
        # The words of the title or of the help, whole or in part: the titles' first.
        self.type("graphics chip")
        self.assertEqual(self.found_keys(), ["hardware_decoding"])  # its help's words
        self.controller.handle(Action.ESCAPE)
        self.type("decod")
        keys = self.found_keys()
        self.assertEqual(keys[0], "hardware_decoding")  # "Hardware video decoding"
        titled = [row.text for row in self.drawer._right_rows() if "decod" in row.text.lower()]
        self.assertEqual(titled, [row.text for row in self.drawer._right_rows()][: len(titled)])
        self.controller.handle(Action.ESCAPE)
        # Never a text's value, nor a secret's: only titles and helps.
        for typed in ("zebrafish", "WXYZ", "kumquat"):
            with self.subTest(typed=typed):
                self.type(typed)
                self.assertEqual(self.found(), [])
                self.assertIsNone(self.drawer.focused_row())
                said = f"No setting has “{typed}”"
                self.assertEqual(self.drawer._spoken(), said)
                self.drawer.grab()  # its notice paints in the column's place
                self.controller.handle(Action.ESCAPE)
        self.assertTrue(self.controller.visible())

    def test_the_find_line_fits_beside_the_breadcrumb(self):
        for width, height in ((640, 360), (1280, 720), (1920, 1080)):
            with self.subTest(size=(width, height)):
                self.host.resize(width, height)
                self.controller.open(PLAYBACK)
                self.type("float the picture when")
                self.drawer.grab()
                lay = self.drawer._layout()
                right = lay.columns["right"]
                self.assertFalse(lay.find.isEmpty())
                self.assertTrue(right.contains(lay.find), (lay.find, right))
                self.assertTrue(lay.find.contains(lay.find_clear))
                for _text, rect in lay.crumbs:  # the breadcrumb keeps to the left column
                    self.assertLessEqual(rect.right(), lay.columns["left"].right())
                    self.assertLess(rect.right(), lay.find.left())
                for index in range(len(self.drawer._right_rows())):
                    box = self.drawer._box("right", index)
                    if box is not None:  # under the find's line, never beside its ✕
                        self.assertGreaterEqual(box.rect.top(), lay.find.bottom())
                self.assertEqual(
                    self.drawer.tooltip_at(lay.find_clear.center()), find_clear_tooltip()
                )
                self.controller.handle(Action.ESCAPE)
                lay = self.drawer._layout()
                self.assertTrue(lay.find.isEmpty())
                # From a layer: its longer breadcrumb folds and elides in the left column.
                self.to_row("pip")
                self.key(Action.NAV_ACTIVATE)
                self.assertEqual(self.drawer.path(), (PLAYBACK, "pip"))
                self.type("float")
                lay = self.drawer._layout()
                for _text, rect in lay.crumbs:
                    self.assertLessEqual(rect.right(), lay.columns["left"].right())
                self.drawer.grab()
                self.controller.close()

    def test_enter_jumps_to_the_row_in_its_section(self):
        self.controller.open(PROVIDERS)
        self.type("latency")
        self.key(Action.NAV_ACTIVATE)
        self.assertFalse(self.drawer.finding())
        self.assertEqual(self.drawer.path(), (PLAYBACK,))
        self.assertEqual(self.drawer.focus_side(), "right")
        self.assertEqual(self.drawer.focused_row().key, "live_latency")
        self.assertEqual(self.values.sets, [])  # it only went there
        self.key(Action.NAV_RIGHT)  # and steps there as ever
        self.assertEqual(self.values.sets, [("live_latency", "low")])
        self.key(Action.ESCAPE)  # Esc goes up from there
        self.assertEqual(self.drawer.focus_side(), "left")
        self.assertEqual(self.drawer.focused_row().key, "live_latency")
        # Into a layer: its section's rows on the left, the layer's on the right.
        self.type("check the conn")
        self.focus("provider/Harbour/check")
        self.key(Action.NAV_RIGHT)  # → as Enter
        self.assertEqual(self.drawer.path(), (PROVIDERS, "provider/Harbour"))
        self.assertEqual(self.drawer.focused_row().key, "provider/Harbour/check")
        self.assertEqual(self.controller._drawer._said, "Check the connection")
        self.key(Action.ESCAPE)
        self.assertEqual(self.drawer.path(), (PROVIDERS,))
        # An info line takes no focus: the row after it, the line in sight above it.
        self.type("recordings folder")
        self.focus(m.RECORDINGS_FOLDER)
        self.key(Action.NAV_ACTIVATE)
        self.assertEqual(self.drawer.path(), (TIMESHIFT,))
        self.assertEqual(self.drawer.focused_row().key, m.CHANGE_FOLDER)
        rows = [row.key for row in self.drawer._right_rows()]
        self.assertIsNotNone(self.drawer._box("right", rows.index(m.RECORDINGS_FOLDER)))
        # The mouse's Forward goes there too.
        self.type("covers")
        self.focus("covered_pip")
        self.key(Action.NAV_RIGHT, MOUSE_FORWARD)
        self.assertEqual(self.drawer.path(), (PLAYBACK, "pip"))
        self.assertEqual(self.drawer.focused_row().key, "covered_pip")
        # A click on a match, at once; a double-click's second press steps nothing there.
        for double in (False, True):
            with self.subTest(double=double):
                self.controller.open(PROVIDERS)
                self.type("latency")
                self.drawer.grab()
                self.click(self.drawer.title_rect(0).center(), double=double)
                self.assertFalse(self.drawer.finding())
                self.assertEqual(self.drawer.path(), (PLAYBACK,))
                self.assertEqual(self.drawer.focused_row().key, "live_latency")
                self.assertEqual(self.values.sets, [("live_latency", "low")])  # as before
                elapse(QApplication.doubleClickInterval() + 80)

    def test_esc_puts_the_column_back(self):
        self.controller.open(PLAYBACK)
        self.key(Action.NAV_RIGHT)
        self.key(Action.NAV_DOWN)
        self.key(Action.NAV_DOWN)
        before = (self.drawer.path(), self.drawer.focus_side(), self.drawer.focused_row().key)
        said = self.drawer._said
        ends = (
            ("Esc (the mouse's Back too)", lambda: self.key(Action.ESCAPE)),
            ("←", lambda: self.key(Action.NAV_LEFT)),
            ("✕", lambda: self.click(self.drawer._layout().find_clear.center())),
            ("‹", lambda: self.click(self.drawer.back_rect().center())),
            ("Backspace", lambda: [self.key(Action.NAV_BACKSPACE) for _ in "subtx"]),
        )
        for name, end in ends:
            with self.subTest(end=name):
                self.type("subtx")  # nothing found
                self.assertEqual(self.found(), [])
                self.key(Action.NAV_BACKSPACE)  # Backspace edits it
                self.assertEqual(self.drawer.find_text(), "subt")
                self.assertTrue(self.found())
                self.type("x")
                self.drawer.grab()
                end()
                self.assertFalse(self.drawer.finding())
                self.assertTrue(self.controller.visible())
                where = (
                    self.drawer.path(),
                    self.drawer.focus_side(),
                    self.drawer.focused_row().key,
                )
                self.assertEqual(where, before)
                self.assertEqual(self.drawer._said, said)
                elapse(QApplication.doubleClickInterval() + 80)
        # A click on the left column ends it too, then does what it does there.
        self.type("lat")
        self.drawer.grab()
        self.click(self.drawer.row_rect("left", 0).center())
        self.assertFalse(self.drawer.finding())
        self.assertEqual(self.drawer.path(), (PROVIDERS,))
        self.assertEqual(self.drawer.focus_side(), "left")

    def test_the_find_stays_over_new_sections(self):
        # A row that measures (AI captions' files) builds the sections again soon after
        # Settings opens: the find stays, over them, and Esc goes back where it was.
        self.controller.open(PROVIDERS)
        self.key(Action.NAV_RIGHT)
        self.key(Action.NAV_DOWN)
        self.assertEqual(self.drawer.focused_row().key, "provider/Harbour")
        self.type("check the conn")
        self.focus("provider/Harbour/check")
        self.shown = with_providers("Atlas", "Demo TV", "Harbour")
        self.controller.refresh_sections()
        self.assertEqual(self.drawer.find_text(), "check the conn")
        self.assertEqual(
            self.found_keys(),
            ["provider/Atlas/check", "provider/Demo TV/check", "provider/Harbour/check"],
        )
        self.assertEqual(self.drawer.focused_row().key, "provider/Harbour/check")
        self.key(Action.NAV_ACTIVATE)  # where the new sections have it
        self.assertEqual(self.drawer.path(), (PROVIDERS, "provider/Harbour"))
        self.key(Action.ESCAPE)
        self.type("check the conn")
        self.shown = with_providers("Demo TV")  # Harbour went
        self.controller.refresh_sections()
        self.assertEqual(self.found_keys(), ["provider/Demo TV/check"])
        self.key(Action.ESCAPE)
        self.assertEqual(self.drawer.path(), (PROVIDERS,))
        self.assertEqual(self.drawer.focus_side(), "right")
        self.assertEqual(self.drawer.focused_row().key, "provider/@add")  # Harbour's place
        # Opened again: no find, and one of the sections as they are now.
        self.type("atlas")
        self.assertEqual(self.found(), [])
        self.controller.close()
        self.shown = with_providers("Atlas")
        self.controller.open(PROVIDERS)
        self.assertFalse(self.drawer.finding())
        self.type("atlas")
        self.assertEqual(self.found_keys()[0], "provider/Atlas")

    def test_typing_while_editing_is_not_a_find(self):
        self.controller.open(m.SUBTITLES)
        self.to_row("opensubtitles_username")
        self.key(Action.NAV_ACTIVATE)
        self.assertTrue(self.drawer.editing())
        self.type("abc")
        self.key(Action.NAV_BACKSPACE)
        self.assertFalse(self.drawer.finding())
        self.assertEqual(self.drawer._edit.text, "kumquat_viewerab")
        self.key(Action.NAV_ACTIVATE)
        self.assertEqual(self.values.sets, [("opensubtitles_username", "kumquat_viewerab")])
        # Kept: typing finds again. A space alone starts none.
        self.key(Action.NAV_TEXT, " ")
        self.assertFalse(self.drawer.finding())
        self.type("sign")
        self.assertEqual(self.drawer.find_text(), "sign")
        self.assertEqual(self.found_keys()[0], m.SIGN_OUT)
        self.assertFalse(self.drawer.editing())

    def test_turkish_letters_fold(self):
        rows = (
            Row("folder", "Kayıt klasörü", "Kayıtların gittiği yer"),
            Row("password", "Şifre", "Ebeveyn denetimi için"),
            Row("light", "IŞIK", "Ekranın parlaklığı"),
            Row("other", "Diğer"),
        )
        self.shown = (Section("tr", "Ayarlar", rows=rows),)
        for typed, keys in (
            ("kayit", ["folder"]),
            ("KAYIT KLASÖR", ["folder"]),
            ("klasor", ["folder"]),
            ("sifre", ["password"]),
            ("ŞİFRE", ["password"]),
            ("isik", ["light"]),
            ("ışık", ["light"]),
            ("parlakligi", ["light"]),  # in its help
            ("ebeveyn sifre", ["password"]),  # a word of each
            ("gitti", ["folder"]),
        ):
            with self.subTest(typed=typed):
                self.controller.open("tr")
                self.type(typed)
                self.assertEqual(self.found_keys(), keys)
                self.controller.close()


class PhoneFindTests(base.WindowCase):
    def test_the_phones_typing_finds_too(self):
        # The phone's keys (its digits) go the keyboard's way: typed, they find a setting.
        self.show()
        self.wait_for_drawer()
        only = (Section("s", "S", rows=(Row("zone", "Zone 4 sound"), Row("other", "Other"))),)
        settings, drawer = self.window.settings, self.window.settings_drawer
        with mock.patch.object(settings, "_make_sections", return_value=only):
            self.menu_actions()["Settings…"].trigger()
            self.assertTrue(settings.visible())
            self.window.remote_key(Qt.Key.Key_4.value, "4")
            self.assertEqual(drawer.find_text(), "4")
            self.assertEqual([row.key for row in drawer._right_rows()], ["zone"])
            self.window.remote_key(Qt.Key.Key_Return.value)  # its OK
            self.assertFalse(drawer.finding())
            self.assertEqual(drawer.path(), ("s",))
            self.assertEqual(drawer.focused_row().key, "zone")
            self.window.remote_key(Qt.Key.Key_4.value, "4")
            self.window.remote_key(Qt.Key.Key_Backspace.value)  # its Previous: Backspace
            self.assertFalse(drawer.finding())
            self.window.remote_key(Qt.Key.Key_4.value, "4")
            self.window.remote_key(Qt.Key.Key_Escape.value)  # its Back
            self.assertFalse(drawer.finding())
            self.assertTrue(settings.visible())
            self.assertEqual(drawer.focused_row().key, "zone")


if __name__ == "__main__":
    unittest.main()
