"""The controller that applies the Settings layer's steps at once and routes its keys."""

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QApplication, QWidget
from timer_fixtures import elapse

from services import i18n, settings_model
from services.i18n import translate
from services.settings_model import ON_OFF, PLAYBACK, Choice, Row, Section, sections
from ui import settings_controller
from ui.keymap import Action
from ui.overlays.base import Overlay
from ui.overlays.settings_drawer import SettingsDrawer
from ui.settings_controller import SettingsController


def _walk(rows):
    for row in rows:
        yield row
        if row.section is not None:
            yield from _walk(row.section.rows)


DEFAULTS = {
    row.key: row.default for s in sections("Darwin") for row in _walk(s.rows) if row.choices
}


class FakeValues:
    def __init__(self, **values):
        self.values = dict(values)
        self.sets = []

    def get(self, key):
        if key not in self.values:  # a row not set by the test reads as its default
            self.values[key] = DEFAULTS[key]
        return self.values[key]

    def set(self, key, value):
        self.sets.append((key, value))
        self.values[key] = value


class FakeHandler:
    """A RowHandler: each call, by its row's key; `values` by row key."""

    def __init__(self, **values):
        self.values = dict(values)
        self.calls = []

    def value(self, row):
        return self.values.get(row.key)

    def step(self, row, delta):
        self.calls.append(("step", row.key, delta))

    def edited(self, row, text):
        self.calls.append(("edited", row.key, text))
        self.values[row.key] = text

    def chosen(self, row):
        self.calls.append(("chosen", row.key))


class FakeCard(Overlay):
    """A card over the picture as ui/overlays/settings_card.py's are: every key but the sound's
    is its own while it shows, and Esc cancels it (it hides)."""

    cancelled = Signal()

    def __init__(self, parent):
        super().__init__(parent)
        self.keys = []
        self.hide()

    def handle(self, action, payload=""):
        if self.isHidden() or action is Action.VOL_UP:
            return False
        self.keys.append(action)
        if action is Action.ESCAPE:
            self.hide()
            self.cancelled.emit()
        return True


def provider_layer(name):
    """A provider's layer, its rows keyed provider/<name>/<field>."""
    return Section(
        f"provider/{name}",
        name,
        rows=(
            Row(f"provider/{name}/name", "Name", kind=settings_model.TEXT),
            Row(f"provider/{name}/check", "Check the connection"),
        ),
    )


def providers_section(*names):
    """Settings' Providers: a row per provider opening its layer, then Add provider."""
    rows = tuple(
        Row(f"provider/{name}", name, section=provider_layer(name), sub="Xtream") for name in names
    )
    return Section(
        settings_model.PROVIDERS, "Providers", rows=rows + (Row("provider/@add", "Add provider"),)
    )


class SettingsControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.host = QWidget()
        self.host.resize(1280, 720)
        self.host.show()
        self.addCleanup(self.host.deleteLater)
        self.addCleanup(self.host.close)
        self.values = FakeValues(live_latency="auto")
        self.legacy, self.reopened = [], []
        self.drawer = SettingsDrawer(
            lambda row: self.controller.value_text(row),
            lambda row: self.controller.lit(row),
            self.host,
        )
        self.shown = sections("Darwin")  # what the controller builds its sections from
        self.controller = SettingsController(
            self.drawer,
            self.values,
            lambda: self.shown,
            self.legacy.append,
            lambda: self.reopened.append(True),
        )

    def tearDown(self):
        # A mouse-free close arms no guard, but keep later modules clean all the same.
        elapse(QApplication.doubleClickInterval() + 80)

    def test_a_step_writes_the_next_value(self):
        self.controller.open(PLAYBACK)
        self.controller.handle(Action.NAV_RIGHT)  # into the rows
        for _ in range(30):
            row = self.drawer.focused_row()
            if row is not None and row.key == "live_latency":
                break
            self.controller.handle(Action.NAV_DOWN)
        self.assertEqual(self.drawer.focused_row().key, "live_latency")
        self.assertTrue(self.controller.handle(Action.NAV_RIGHT))
        self.assertEqual(self.values.sets, [("live_latency", "low")])

    def focus_on(self, key):
        """The keys on Playback's row `key` (a step through the rows, never past their end)."""
        self.controller.open(PLAYBACK)
        self.controller.handle(Action.NAV_RIGHT)
        for _ in range(len(self.drawer._right_rows())):
            if self.drawer.focused_row().key == key:
                return
            self.controller.handle(Action.NAV_DOWN)
        self.fail(f"no row {key}")

    def test_a_step_in_a_layer_writes_its_row(self):
        self.focus_on("autoplay")
        self.controller.handle(Action.NAV_ACTIVATE)  # its layer
        self.assertEqual(self.drawer.path(), (PLAYBACK, "autoplay"))
        self.controller.handle(Action.NAV_DOWN)
        self.assertEqual(self.drawer.focused_row().key, "auto_play_movies")
        self.controller.handle(Action.NAV_RIGHT)
        self.assertEqual(self.values.sets, [("auto_play_movies", False)])
        self.assertEqual(self.controller.value_text(self.drawer.focused_row()), "Off")

    def test_a_settings_on_and_off_are_its_own_words(self):
        # The plain "Off" is also the Subtitles panel's row, the relay's state and the phone's
        # Subtitles row (plural in Spanish and French): a setting's value has its own entry.
        self.focus_on("hardware_decoding")
        row = self.drawer.focused_row()
        for code, on, off, plain in (
            ("es", "Activado", "Desactivado", "Desactivados"),
            ("fr", "Activé", "Désactivé", "Désactivés"),
            ("tr", "Açık", "Kapalı", "Kapalı"),
        ):
            with self.subTest(code):
                self.assertEqual(i18n.set_language(code), code)
                self.addCleanup(i18n.set_language, "en")
                self.values.values["hardware_decoding"] = True
                self.assertEqual(self.controller.value_text(row), on)
                self.values.values["hardware_decoding"] = False
                self.assertEqual(self.controller.value_text(row), off)
                self.assertEqual(translate("Off"), plain)

    def test_legacy_closes_then_opens_the_old_tab(self):
        # A section whose settings are still in the Settings window (made up here: the
        # sections move into the drawer one by one).
        self.shown = sections("Darwin") + (Section("old", "Old", legacy_tab="old_tab"),)
        with mock.patch.dict(settings_model.LEGACY_TABS, {"old": "old_tab"}):
            self.controller.open("old")
            self.controller.handle(Action.NAV_RIGHT)
            self.controller.handle(Action.NAV_ACTIVATE)
        self.assertEqual(self.legacy, ["old_tab"])
        self.assertFalse(self.controller.visible())

    def test_back_from_the_sections_reopens_the_drawer_only_when_opened_from_it(self):
        self.controller.open(from_drawer=True)
        self.controller.handle(Action.ESCAPE)
        self.assertEqual(self.reopened, [True])
        self.assertFalse(self.controller.visible())
        self.controller.open()
        self.controller.handle(Action.NAV_BACKSPACE)
        self.assertEqual(self.reopened, [True])

    def test_close_does_not_reopen_the_drawer(self):
        self.controller.open(from_drawer=True)
        self.controller.close()
        self.assertFalse(self.controller.visible())
        self.assertEqual(self.reopened, [])

    def test_player_keys_pass_through(self):
        self.controller.open()
        self.assertFalse(self.controller.handle(Action.VOL_UP))
        self.assertFalse(self.controller.handle(Action.MUTE))

    def test_the_mouse_forward_opens_but_never_steps(self):
        forward = settings_controller.MOUSE_FORWARD
        self.focus_on("live_latency")
        self.assertTrue(self.controller.handle(Action.NAV_RIGHT, forward))
        self.assertEqual(self.values.sets, [])
        self.focus_on("autoplay")
        self.controller.handle(Action.NAV_RIGHT, forward)
        self.assertEqual(self.drawer.path(), (PLAYBACK, "autoplay"))

    def open_text_row(self, value):
        """Settings open on one section of a text row (key "name") and a stepper, the keys on
        the text row."""
        self.values.values["name"] = value
        playback = next(s for s in sections("Darwin") if s.key == PLAYBACK)
        rows = (
            Row("name", "Name", kind=settings_model.TEXT),
            next(r for r in playback.rows if r.key == "live_latency"),
        )
        self.shown = (Section("s", "S", rows=rows),)
        self.controller.open()
        self.controller.handle(Action.NAV_RIGHT)
        self.assertEqual(self.drawer.focused_row().key, "name")

    def test_a_text_row_shows_its_value_and_keeps_an_edit(self):
        self.open_text_row("Demo TV")
        self.assertEqual(self.controller.value_text(self.drawer.focused_row()), "Demo TV")
        self.assertTrue(self.controller.handle(Action.NAV_TEXT, "x"))  # not editing: a find
        self.assertTrue(self.drawer.finding())
        self.assertTrue(self.controller.handle(Action.ESCAPE))  # ended: on the row again
        self.assertFalse(self.drawer.finding())
        self.assertEqual(self.controller.value_text(self.drawer.focused_row()), "Demo TV")
        self.assertTrue(self.controller.handle(Action.NAV_ACTIVATE))
        self.assertTrue(self.drawer.editing())
        self.assertTrue(self.controller.handle(Action.NAV_TEXT, "!"))
        self.assertTrue(self.controller.handle(Action.NAV_ACTIVATE))  # Enter keeps it
        self.assertEqual(self.values.sets, [("name", "Demo TV!")])
        self.assertEqual(self.controller.value_text(self.drawer.focused_row()), "Demo TV!")

    def test_an_edit_of_a_key_the_values_refuse_is_logged(self):
        self.open_text_row("Demo TV")
        self.controller.handle(Action.NAV_ACTIVATE)
        self.controller.handle(Action.NAV_TEXT, "!")
        with (
            mock.patch.object(self.values, "set", side_effect=KeyError("name")),
            self.assertLogs("ui.settings_controller", "WARNING"),
        ):
            self.controller.handle(Action.NAV_ACTIVATE)  # no exception out of the slot
        self.assertFalse(self.drawer.editing())

    def test_keep_edit_keeps_the_text_being_edited(self):
        self.open_text_row("ab")
        self.controller.keep_edit()  # nothing edited: nothing
        self.assertEqual(self.values.sets, [])
        self.controller.handle(Action.NAV_ACTIVATE)
        self.controller.handle(Action.NAV_TEXT, "c")
        self.controller.keep_edit()
        self.assertEqual(self.values.sets, [("name", "abc")])
        self.assertFalse(self.drawer.editing())

    def test_the_keys_reach_the_field_while_editing(self):
        self.open_text_row("ab")
        self.controller.handle(Action.OK)  # Enter, as the phone's OK
        self.assertTrue(self.drawer.editing())
        for _ in range(4):  # Backspace past the start never backs out
            self.assertTrue(self.controller.handle(Action.NAV_BACKSPACE))
        self.assertTrue(self.controller.visible())
        self.assertTrue(self.controller.handle(Action.NAV_TEXT, "cd"))
        self.assertTrue(self.controller.handle(Action.NAV_LEFT, ""))  # the caret, not a step
        self.assertTrue(self.controller.handle(Action.NAV_HOME, ""))
        self.assertTrue(self.controller.handle(Action.FORGET))  # Delete: forward
        with mock.patch("ui.text_edit.clipboard_line", return_value="P"):
            self.assertTrue(self.controller.handle(Action.PASTE))
        self.assertEqual(self.drawer.focus_side(), "right")
        self.assertFalse(self.controller.handle(Action.VOL_UP))  # still the player's
        self.assertTrue(self.controller.handle(Action.ESCAPE))  # puts it back, stays open
        self.assertFalse(self.drawer.editing())
        self.assertTrue(self.controller.visible())
        self.assertEqual(self.values.sets, [])
        self.controller.handle(Action.NAV_ACTIVATE)
        self.controller.handle(Action.NAV_TEXT, "z")
        self.controller.handle(Action.NAV_DOWN)  # leaving the row keeps it, then moves
        self.assertEqual(self.values.sets, [("name", "abz")])
        self.assertEqual(self.drawer.focused_row().key, "live_latency")

    # ---- sections that change, row namespaces, cards (plan 2) ---------------------------------

    def focus_row(self, key):
        """The keys onto the right column's row `key` (from its top, a row at a time)."""
        if self.drawer.focus_side() == "left":
            self.controller.handle(Action.NAV_RIGHT)
        for _ in range(len(self.drawer._right_rows())):
            self.controller.handle(Action.NAV_UP)
        for _ in range(len(self.drawer._right_rows())):
            if self.drawer.focused_row().key == key:
                return
            self.controller.handle(Action.NAV_DOWN)
        self.fail(f"no row {key}")

    def test_sections_change_under_the_viewer_and_keep_the_place(self):
        self.shown = (providers_section("A", "B", "C"),)
        self.controller.open()
        self.focus_row("provider/B")
        self.shown = (providers_section("A", "C"),)  # B removed (here, a drop, the old window)
        self.controller.refresh_sections()
        self.assertEqual(self.drawer.focused_row().key, "provider/C")  # its neighbour
        self.assertEqual(self.drawer.path(), (settings_model.PROVIDERS,))
        self.assertEqual(self.drawer.focus_side(), "right")
        self.shown = (providers_section("0", "A", "C", "D"),)  # added, one before the focus
        self.controller.refresh_sections()
        self.assertEqual(self.drawer.focused_row().key, "provider/C")  # the focus stays
        self.controller.handle(Action.NAV_ACTIVATE)  # its layer, from the new sections
        self.assertEqual(self.drawer.path(), (settings_model.PROVIDERS, "provider/C"))
        self.controller.handle(Action.NAV_DOWN)
        self.controller.handle(Action.NAV_ACTIVATE)  # an action of the new rows: no warning
        self.assertEqual(self.values.sets, [])

    def test_a_prefix_handler_gets_its_rows(self):
        streams = (Choice(0, "Automatic"), Choice(1, "1"), Choice(2, "2"))
        latency = next(r for r in self.shown[2].rows if r.key == "live_latency")
        rows = (
            Row("provider/x/name", "Name", kind=settings_model.TEXT),
            Row("provider/x/streams", "Streams at once", "", streams, 0),
            Row("provider/x/ssl", "Verify certificates", "", ON_OFF, True),
            Row("provider/x/kind", "Kind", kind=settings_model.INFO),
            Row("provider/x/check", "Check the connection"),
            Row("provider/x/card", "Address and login", kind=settings_model.OPENS),
            latency,
        )
        self.shown = (Section("p", "Provider", rows=rows),)
        handler = FakeHandler(
            **{
                "provider/x/name": "Demo TV",
                "provider/x/streams": 2,
                "provider/x/ssl": True,
                "provider/x/kind": "Xtream",
            }
        )
        self.controller.register("provider/", handler)
        self.controller.open()
        by_key = {row.key: row for row in rows}
        value_text = self.controller.value_text
        self.assertEqual(value_text(by_key["provider/x/name"]), "Demo TV")
        self.assertEqual(value_text(by_key["provider/x/streams"]), "2")
        self.assertTrue(self.controller.lit(by_key["provider/x/streams"]))
        self.assertFalse(self.controller.lit(by_key["provider/x/ssl"]))
        self.assertEqual(value_text(by_key["provider/x/kind"]), "Xtream")
        self.assertEqual(value_text(by_key["provider/x/check"]), "")
        self.focus_row("provider/x/streams")
        self.controller.handle(Action.NAV_RIGHT)
        self.controller.handle(Action.NAV_LEFT)
        self.focus_row("provider/x/name")
        self.controller.handle(Action.NAV_ACTIVATE)
        self.controller.handle(Action.NAV_TEXT, "!")
        self.controller.handle(Action.NAV_ACTIVATE)
        self.focus_row("provider/x/check")
        self.controller.handle(Action.NAV_ACTIVATE)
        self.controller.handle(Action.NAV_DOWN)
        self.controller.handle(Action.NAV_ACTIVATE)  # it opens a card: the handler's to show
        self.assertEqual(
            handler.calls,
            [
                ("step", "provider/x/streams", 1),
                ("step", "provider/x/streams", -1),
                ("edited", "provider/x/name", "Demo TV!"),
                ("chosen", "provider/x/check"),
                ("chosen", "provider/x/card"),
            ],
        )
        self.assertEqual(self.values.sets, [])  # none reached the config
        self.controller.handle(Action.NAV_DOWN)  # a key of no prefix: the config, as ever
        self.controller.handle(Action.NAV_RIGHT)
        self.assertEqual(self.values.sets, [("live_latency", "low")])
        self.assertEqual(len(handler.calls), 5)

    def test_a_choice_paints_again_and_a_rename_is_passed_on(self):
        # A handler's action may change what its rows show: they paint again (and say it).
        # A rename's new keys reach the drawer with the hint that keeps the viewer in place.
        self.shown = (providers_section("A"),)
        handler = FakeHandler()
        self.controller.register("provider/", handler)
        self.controller.open()
        self.focus_row("provider/@add")
        with mock.patch.object(self.drawer, "refresh") as painted:
            self.controller.handle(Action.NAV_ACTIVATE)
        self.assertEqual(handler.calls, [("chosen", "provider/@add")])
        painted.assert_called_once_with()
        self.shown = (providers_section("B"),)
        with mock.patch.object(self.drawer, "set_sections") as given:
            self.controller.refresh_sections(renamed={"provider/A": "provider/B"})
        given.assert_called_once_with(self.shown, renamed={"provider/A": "provider/B"})

    def test_a_card_takes_the_keys_first_and_esc_cancels_it(self):
        self.controller.open(PLAYBACK)
        self.controller.handle(Action.NAV_RIGHT)
        card = FakeCard(self.host)
        cancelled = []
        card.cancelled.connect(lambda: cancelled.append(True))
        self.assertFalse(self.controller.card_shown())
        self.controller.show_card(card)
        self.assertTrue(self.controller.card_shown())
        self.assertTrue(card.isVisible())
        before = self.drawer.focused_row().key
        for action in (Action.NAV_DOWN, Action.NAV_RIGHT, Action.NAV_ACTIVATE, Action.NAV_TEXT):
            self.assertTrue(self.controller.handle(action, "x"))
        self.assertTrue(self.controller.handle(Action.NAV_RIGHT, settings_controller.MOUSE_FORWARD))
        self.assertFalse(self.controller.handle(Action.VOL_UP))  # the card's no: the player's
        self.assertEqual(self.drawer.focused_row().key, before)  # the rows heard nothing
        self.assertEqual(self.values.sets, [])
        self.assertTrue(self.controller.handle(Action.ESCAPE))  # its Cancel, not a layer up
        self.assertEqual(cancelled, [True])
        self.assertTrue(card.isHidden())
        self.assertFalse(self.controller.card_shown())
        self.assertTrue(self.controller.visible())
        self.assertEqual(self.drawer.focus_side(), "right")
        self.controller.handle(Action.ESCAPE)  # now the rows': a layer up
        self.assertEqual(self.drawer.focus_side(), "left")

    def test_only_a_card_in_sight_takes_the_keys(self):
        # A card whose page is out of sight (it is not hidden itself) has no keys: they reach
        # what shows, and the card has them again once it shows.
        self.controller.open(PLAYBACK)
        page = QWidget(self.host)
        page.hide()
        card = FakeCard(page)
        self.controller.show_card(card)
        self.assertFalse(card.isHidden())
        self.assertFalse(card.isVisible())
        self.assertFalse(self.controller.card_shown())
        self.controller.handle(Action.NAV_RIGHT)
        self.assertEqual(card.keys, [])
        self.assertEqual(self.drawer.focus_side(), "right")  # the rows had it
        page.show()
        self.assertTrue(self.controller.card_shown())
        self.controller.handle(Action.NAV_DOWN)
        self.assertEqual(card.keys, [Action.NAV_DOWN])

    def test_a_card_over_a_card_and_settings_going_cancels_them(self):
        self.controller.open(PLAYBACK)
        form, question = FakeCard(self.host), FakeCard(self.host)
        self.controller.show_card(form)
        self.controller.show_card(question)  # a question over the form: it has the keys
        self.controller.handle(Action.NAV_DOWN)
        self.assertEqual((form.keys, question.keys), ([], [Action.NAV_DOWN]))
        self.controller.handle(Action.ESCAPE)
        self.assertTrue(question.isHidden())
        self.controller.handle(Action.NAV_DOWN)  # the form's again
        self.assertEqual(form.keys, [Action.NAV_DOWN])
        self.controller.close()  # the phone's TV, the list opened over it: the card goes too
        self.assertTrue(form.isHidden())
        self.assertEqual(form.keys[-1], Action.ESCAPE)  # cancelled as its Esc cancels it
        self.assertFalse(self.controller.card_shown())


if __name__ == "__main__":
    unittest.main()
