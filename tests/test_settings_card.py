"""The cards over the picture (2.12): a form (adding a provider, or its address and login) and a
question with two or three answers, painted over the dimmed TV and worked by the keys and the
mouse. No network: the cards only say what was typed and pressed."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QWidget

from ui.keymap import Action, key_hint
from ui.overlays.painting import ui_units
from ui.overlays.settings_card import CANCEL, Button, ChoiceCard, Field, FormCard
from ui.theme import PALETTE, qcolor

LINK = "http://tv.example/get.php?username=demo&password=secret"
FOUND = "✓ Xtream at tv.example, with its login"
BUTTONS = [Button(CANCEL, "Cancel"), Button("add", "Check and add", accent=True)]
ALTERNATES = [Button("file", "M3U file…"), Button("stb", "STB portal")]


def add_form(card: FormCard, *, url: str = "", found: str = "") -> FormCard:
    card.set_form(
        "Add a provider",
        [
            Field("url", "Link or address", url, placeholder="http://…", paste=True),
            Field("username", "Username"),
            Field("password", "Password", secret=True),
        ],
        found,
        BUTTONS,
        ALTERNATES,
    )
    return card


def painted(image, rect: QRectF) -> int:
    """How many pixels in `rect` differ from the card's own colour (something drawn there)."""
    panel = qcolor(PALETTE.panel_raised).rgb()
    count = 0
    for x in range(int(rect.left()) + 1, int(rect.right())):
        for y in range(int(rect.top()) + 1, int(rect.bottom())):
            if QColor(image.pixel(x, y)).rgb() != panel:
                count += 1
    return count


class Case(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.host = QWidget()
        self.host.resize(1280, 720)
        self.host.show()
        self.addCleanup(self.host.deleteLater)
        self.addCleanup(self.host.close)

    def form(self, **kwargs) -> FormCard:
        return add_form(FormCard(self.host), **kwargs)

    def click(self, widget, point, button=Qt.MouseButton.LeftButton):
        QTest.mouseClick(widget, button, Qt.KeyboardModifier.NoModifier, point.toPoint())


class FormCardTests(Case):
    def test_typing_goes_into_the_focused_field_and_paste_inserts_the_clipboard_line(self):
        card = self.form()
        changed = QSignalSpy(card.changed)
        self.assertTrue(card.isVisible())
        self.assertEqual(card.focus(), ("field", "url"))
        for ch in "http://":
            self.assertTrue(card.handle(Action.NAV_TEXT, ch))
        card.handle(Action.NAV_DOWN)
        self.assertEqual(card.focus(), ("field", "username"))
        card.handle(Action.NAV_TEXT, "demo")
        card.handle(Action.NAV_BACKSPACE)
        self.assertEqual(card.values(), {"url": "http://", "username": "dem", "password": ""})
        card.handle(Action.NAV_UP)
        card.handle(Action.NAV_LEFT)  # the caret's, not another control's
        self.assertEqual(card.focus(), ("field", "url"))
        QGuiApplication.clipboard().setText("  tv.example/get.php \nsecond line")
        card.handle(Action.PASTE)  # Ctrl/⌘+V: at the caret
        self.assertEqual(card.values()["url"], "http:/tv.example/get.php/")
        self.assertEqual(changed.count(), 10)  # 7 letters, "demo", Backspace, the paste
        self.assertEqual(changed.at(changed.count() - 1), ["url", "http:/tv.example/get.php/"])
        card.handle(Action.NAV_DOWN)
        card.handle(Action.NAV_DOWN)
        QGuiApplication.clipboard().setText("pw\n")
        card.handle(Action.PASTE)
        self.assertEqual(card.values()["password"], "pw")

    def test_enter_in_a_field_presses_the_accent_button(self):
        card = self.form()
        pressed = QSignalSpy(card.pressed)
        card.handle(Action.NAV_TEXT, LINK)
        card.handle(Action.NAV_ACTIVATE)
        self.assertEqual(pressed.at(0), ["add"])
        card.handle(Action.OK)  # the Settings controller's Enter, too
        self.assertEqual(pressed.count(), 2)
        self.assertTrue(card.isVisible())  # the caller closes it when the check is done
        self.assertEqual(card.values()["url"], LINK)

    def test_the_arrows_walk_the_fields_the_buttons_and_the_alternates(self):
        card = self.form()
        pressed, cancelled = QSignalSpy(card.pressed), QSignalSpy(card.cancelled)
        card.handle(Action.NAV_TEXT, "tv")
        card.handle(Action.NAV_RIGHT)  # at the text's end: its Paste
        self.assertEqual(card.focus(), ("paste", "url"))
        card.handle(Action.NAV_LEFT)
        card.handle(Action.NAV_LEFT)  # the caret moves in the text
        self.assertEqual(card.focus(), ("field", "url"))
        card.handle(Action.NAV_TEXT, "-")
        self.assertEqual(card.values()["url"], "t-v")
        card.handle(Action.NAV_DOWN)
        card.handle(Action.NAV_DOWN)
        card.handle(Action.NAV_DOWN)  # past the last field: the button row, on its accent one
        self.assertEqual(card.focus(), ("button", "add"))
        card.handle(Action.NAV_LEFT)
        card.handle(Action.NAV_LEFT)
        self.assertEqual(card.focus(), ("button", CANCEL))
        card.handle(Action.NAV_DOWN)
        self.assertEqual(card.focus(), ("alt", "file"))
        card.handle(Action.NAV_RIGHT)
        card.handle(Action.NAV_RIGHT)
        self.assertEqual(card.focus(), ("alt", "stb"))
        card.handle(Action.NAV_ACTIVATE)
        self.assertEqual(pressed.at(0), ["stb"])
        card.handle(Action.NAV_UP)
        self.assertEqual(card.focus(), ("button", CANCEL))  # the one it left
        card.handle(Action.NAV_UP)
        self.assertEqual(card.focus(), ("field", "password"))
        # Every key is the card's while it shows (none reaches the rows under it), but the
        # sound's: Backspace in an empty field, ↑ at the top, typing on a button.
        self.assertTrue(card.handle(Action.NAV_BACKSPACE))
        self.assertTrue(card.handle(Action.NAV_RIGHT))
        for _ in range(4):
            self.assertTrue(card.handle(Action.NAV_UP))
        self.assertEqual(card.focus(), ("field", "url"))
        for _ in range(3):
            card.handle(Action.NAV_DOWN)
        self.assertTrue(card.handle(Action.NAV_TEXT, "x"))
        self.assertTrue(card.handle(Action.NAV_BACKSPACE))  # never a stray Cancel of a form
        self.assertFalse(card.handle(Action.VOL_UP))
        self.assertEqual(card.values(), {"url": "t-v", "username": "", "password": ""})
        self.assertEqual((pressed.count(), cancelled.count()), (1, 0))
        card.handle(Action.NAV_LEFT)
        card.handle(Action.NAV_ACTIVATE)  # Cancel is the card's Esc, not a press
        self.assertEqual((pressed.count(), cancelled.count()), (1, 1))
        self.assertFalse(card.isVisible())

    def test_clicks_focus_put_the_caret_paste_and_press(self):
        card = self.form(url="http://old.example")
        pressed = QSignalSpy(card.pressed)
        changed = QSignalSpy(card.changed)
        QGuiApplication.clipboard().setText(" tv.example \nmore")
        self.click(card, card.control_rect("paste", "url").center())
        # No caret was put in the text: Paste puts the link in its place.
        self.assertEqual(card.values()["url"], "tv.example")
        self.assertEqual(card.focus(), ("field", "url"))
        self.assertEqual(changed.at(0), ["url", "tv.example"])
        self.click(card, card.control_rect("paste", "url").center())
        self.assertEqual(changed.count(), 1)  # the same text: nothing changed
        card.grab()  # where the text is drawn, for the clicks in it
        field = card.control_rect("field", "url")
        self.click(card, QPointF(field.left() + 2, field.center().y()))  # the caret at 0
        QGuiApplication.clipboard().setText("new.example")
        self.click(card, card.control_rect("paste", "url").center())
        self.assertEqual(card.values()["url"], "new.example")  # a click never splices a link
        card.handle(Action.NAV_HOME)
        card.handle(Action.NAV_RIGHT, "select")
        card.handle(Action.NAV_RIGHT, "select")
        card.handle(Action.NAV_RIGHT, "select")
        QGuiApplication.clipboard().setText("old")
        self.click(card, card.control_rect("paste", "url").center())
        self.assertEqual(card.values()["url"], "old.example")  # in place of the selection
        QGuiApplication.clipboard().setText("www.")
        card.handle(Action.NAV_HOME)
        card.handle(Action.PASTE)  # Ctrl/⌘+V in the field: at the caret
        self.assertEqual(card.values()["url"], "www.old.example")
        self.click(card, card.control_rect("field", "password").center())
        self.assertEqual(card.focus(), ("field", "password"))
        self.click(card, card.control_rect("button", "add").center())
        self.assertEqual(pressed.at(0), ["add"])
        self.assertEqual(card.focus(), ("button", "add"))
        self.click(card, card.control_rect("alt", "file").center())
        self.assertEqual(pressed.at(1), ["file"])
        tip = card.tooltip_at(card.control_rect("paste", "url").center())
        self.assertEqual(tip, key_hint("Paste", "Ctrl+V"))

    def test_a_form_set_again_while_it_shows_keeps_the_place(self):
        card = FormCard(self.host)
        link = [Field("url", "Link or address", paste=True)]
        card.set_form("Add a provider", link, "", BUTTONS, [])

        def grow(key: str, text: str) -> None:  # as the Add card does when a link has a login
            if key == "url" and text.endswith("?"):
                fields = [
                    Field("url", "Link or address", text, paste=True),
                    Field("username", "Username", "demo"),
                    Field("password", "Password", "pw", secret=True),
                ]
                card.set_form("Add a provider", fields, FOUND, BUTTONS, ALTERNATES)

        card.changed.connect(grow)
        for ch in "tv.example/get.php?":
            card.handle(Action.NAV_TEXT, ch)
        self.assertEqual(card.focus(), ("field", "url"))
        self.assertEqual(
            card.values(), {"url": "tv.example/get.php?", "username": "demo", "password": "pw"}
        )
        self.assertEqual(card.found(), (FOUND, True))
        card.set_found("The server refused the login.", False)
        fields = [Field("url", "Link or address", card.values()["url"], paste=True)]
        card.set_form("Add a provider", fields, "The server refused the login.", BUTTONS, [])
        self.assertEqual(card.found(), ("The server refused the login.", False))  # still red
        card.set_form("Add a provider", fields, FOUND, BUTTONS, [])
        self.assertEqual(card.found(), (FOUND, True))
        card.handle(Action.NAV_LEFT)
        card.handle(Action.NAV_TEXT, "x")  # the caret stayed where it was
        self.assertEqual(card.values()["url"], "tv.example/get.phpx?")
        card.handle(Action.NAV_DOWN)
        card.handle(Action.NAV_DOWN)
        card.handle(Action.NAV_DOWN)
        card.handle(Action.NAV_LEFT)
        self.assertEqual(card.focus(), ("button", CANCEL))
        anyway = [*BUTTONS, Button("anyway", "Add anyway")]
        card.set_form("Add a provider", [], "", anyway, [])  # the fields went
        self.assertEqual(card.focus(), ("button", CANCEL))
        card.handle(Action.NAV_RIGHT)
        card.handle(Action.NAV_RIGHT)
        card.set_form("Add a provider", [], "", BUTTONS, [])  # its button went
        self.assertEqual(card.focus(), ("button", "add"))
        card.set_busy(True)
        card.handle(Action.ESCAPE)
        add_form(card)  # shown afresh: its first field, nothing waiting
        self.assertEqual((card.focus(), card.busy()), (("field", "url"), False))
        self.assertEqual(card.values()["url"], "")

    def test_esc_and_a_backdrop_click_cancel(self):
        card = self.form()
        cancelled, pressed = QSignalSpy(card.cancelled), QSignalSpy(card.pressed)
        self.assertTrue(card.handle(Action.ESCAPE))
        self.assertFalse(card.isVisible())
        self.assertEqual(cancelled.count(), 1)
        self.assertFalse(card.handle(Action.ESCAPE))  # hidden: no key is its own
        add_form(card)
        panel = card.card_rect()
        self.click(card, QPointF(panel.left() + 4, panel.center().y()))  # on it, off its controls
        self.assertTrue(card.isVisible())
        self.click(card, QPointF(5, 5))  # the dimmed picture
        self.assertEqual(cancelled.count(), 2)
        self.assertFalse(card.isVisible())
        add_form(card)
        self.click(card, card.card_rect().center(), Qt.MouseButton.BackButton)
        self.assertEqual(cancelled.count(), 3)
        add_form(card)
        self.click(card, card.control_rect("button", CANCEL).center())
        self.assertEqual((cancelled.count(), pressed.count()), (4, 0))

    def test_secret_fields_paint_dots(self):
        card = FormCard(self.host)
        fields = [
            Field("username", "Username", "demo"),
            Field("password", "Password", "s3cret!", secret=True),
        ]
        card.set_form("Demo TV: address and login", fields, "", BUTTONS, [])
        card.grab()
        self.assertEqual(card.painted_text("username"), "demo")
        self.assertEqual(card.painted_text("password"), "•" * 7)
        card.handle(Action.NAV_DOWN)
        card.handle(Action.NAV_TEXT, "x")
        card.grab()
        self.assertEqual(card.values()["password"], "s3cret!x")
        self.assertEqual(card.painted_text("password"), "•" * 8)
        QGuiApplication.clipboard().setText("before")
        card.handle(Action.SELECT_ALL)
        card.handle(Action.COPY)
        card.handle(Action.CUT)  # a secret is never copied out
        self.assertEqual(QGuiApplication.clipboard().text(), "before")
        self.assertEqual(card.values()["password"], "s3cret!x")
        self.assertNotIn("s3cret", repr(fields[1]))
        self.assertNotIn("s3cret", card.accessibleName())
        self.assertNotIn("s3cret", card.tooltip_at(card.control_rect("field", "password").center()))

    def test_busy_disables_the_buttons(self):
        card = self.form(found=FOUND)
        pressed, cancelled = QSignalSpy(card.pressed), QSignalSpy(card.cancelled)
        card.handle(Action.NAV_TEXT, "tv.example")
        busy = card.control_rect("busy")
        self.assertFalse(busy.isEmpty())
        self.assertEqual(painted(card.grab().toImage(), busy), 0)
        card.set_busy(True)
        self.assertTrue(card.busy())
        self.assertGreater(painted(card.grab().toImage(), busy), 0)  # "Checking…"
        card.handle(Action.NAV_ACTIVATE)  # from the field: the accent button waits
        self.click(card, card.control_rect("button", "add").center())
        self.click(card, card.control_rect("alt", "file").center())
        self.assertEqual(pressed.count(), 0)
        card.handle(Action.NAV_TEXT, "x")  # the fields keep what is being checked
        self.click(card, card.control_rect("paste", "url").center())
        self.assertEqual(card.values()["url"], "tv.example")
        card.set_busy(False)
        self.assertEqual(painted(card.grab().toImage(), busy), 0)
        card.handle(Action.NAV_ACTIVATE)
        self.assertEqual(pressed.at(0), ["add"])
        # A check that never answers: the card can always be cancelled.
        card.set_busy(True)
        self.click(card, card.control_rect("button", CANCEL).center())
        add_form(card).set_busy(True)
        card.handle(Action.ESCAPE)
        add_form(card).set_busy(True)
        self.click(card, QPointF(5, 5))
        self.assertEqual(cancelled.count(), 3)

    def test_a_small_window_keeps_the_card_inside(self):
        card = self.form(found=FOUND)
        self.assertAlmostEqual(card.card_rect().width(), ui_units(1280).px(48), delta=1)
        self.assertEqual(
            card.card_rect().center().toPoint(), QRectF(card.rect()).center().toPoint()
        )
        controls = [("button", CANCEL), ("button", "add"), ("alt", "file"), ("alt", "stb")]
        for width, height in ((2560, 1440), (640, 360), (480, 270), (320, 180), (320, 120)):
            with self.subTest(size=(width, height)):
                self.host.resize(width, height)
                QApplication.processEvents()
                self.assertEqual(card.size().toTuple(), (width, height))
                panel = card.card_rect()
                self.assertTrue(QRectF(card.rect()).contains(panel))
                self.assertTrue(panel.contains(card.control_rect("title", "")))
                for zone, key in controls:
                    rect = card.control_rect(zone, key)
                    self.assertFalse(rect.isEmpty(), (zone, key))
                    self.assertTrue(panel.contains(rect), (zone, key))
                if width == 2560:  # the unit stops growing: no bigger than at 1600
                    self.assertAlmostEqual(panel.width(), ui_units(2560).px(48), delta=1)
                    self.assertAlmostEqual(panel.width(), ui_units(1600).px(48), delta=1)
                card.grab()  # paints without a fault


class ChoiceCardTests(Case):
    def test_choice_card_defaults_and_esc_cancels(self):
        card = ChoiceCard(self.host)
        answered = QSignalSpy(card.answered)
        guard = [
            Button(CANCEL, "Cancel"),
            Button("replace", "Replace Demo TV's login"),
            Button("new", "Add as new", accent=True),
        ]
        body = "It points to another account at tv.example. Add it as a new provider?"
        card.ask("This link is another provider", body, guard, "new")
        self.assertTrue(card.isVisible())
        self.assertEqual(card.focus(), ("button", "new"))
        self.assertTrue(card.handle(Action.NAV_LEFT))
        self.assertEqual(card.focus(), ("button", "replace"))
        card.handle(Action.NAV_ACTIVATE)
        self.assertEqual(answered.at(0), ["replace"])
        self.assertFalse(card.isVisible())
        card.ask("This link is another provider", body, guard, "new")
        self.assertTrue(card.handle(Action.ESCAPE))
        self.assertEqual(answered.at(1), [CANCEL])
        remove = [Button("remove", "Remove", danger=True), Button(CANCEL, "Cancel")]
        card.ask("Remove Demo TV?", "Its favourites and hidden groups go with it.", remove, "")
        self.assertEqual(card.focus(), ("button", CANCEL))  # no default: the safe answer
        self.click(card, QPointF(5, 5))
        self.assertEqual(answered.at(2), [CANCEL])
        card.ask("Remove Demo TV?", "", remove, CANCEL)
        self.click(card, card.control_rect("button", "remove").center())
        self.assertEqual(answered.at(3), ["remove"])
        card.ask("Remove Demo TV?", "", remove, "remove")
        self.assertTrue(card.handle(Action.NAV_BACKSPACE))  # Back
        self.assertEqual(answered.at(4), [CANCEL])
        card.ask("Remove Demo TV?", "", remove, "remove")
        self.assertFalse(card.handle(Action.MUTE))
        card.handle(Action.OK)
        self.assertEqual(answered.at(5), ["remove"])
        self.assertEqual(answered.count(), 6)
        card.grab()


if __name__ == "__main__":
    unittest.main()
