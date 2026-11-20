"""Settings › Parental controls in the drawer (plan 3): the old Settings window's tab, row by
row, through services.parental's guard as the old tab used it: the PIN set, changed and removed
in cards (the old rules and messages), unlocked for an hour and locked, the adult rule, and the
list of what is hidden only while unlocked. Every change tells the window; no PIN is ever shown,
said or logged. Over a real TV window."""

import logging
import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from timer_fixtures import wait_until

from services import settings_model as m
from services.config_manager import ConfigManager
from services.i18n import translate
from services.provider_ids import NETWORK_PROVIDER
from tests.fixtures.local_provider import PROVIDER_NAME
from tests.test_settings_guide import GuideCase
from ui import settings_parental as parental
from ui.keymap import Action
from ui.overlays.settings_card import CANCEL

PIN = "2468"


class Captured(logging.Handler):
    """Every record logged meanwhile, as it reads."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


class ParentalCase(GuideCase):
    def setUp(self):
        super().setUp()
        self.log = Captured()
        root = logging.getLogger()
        root.addHandler(self.log)
        level = root.level
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.setLevel, level)
        self.addCleanup(root.removeHandler, self.log)

    def prepare_config(self):
        pass

    @property
    def guard(self):
        return self.window.parental

    @property
    def card(self):
        return self.window.parental_rows.card

    def open(self, key):
        """Settings at Parental controls, the keys on its row `key`."""
        return self.go_to(key) if self.window.settings.visible() else self.open_at(m.PARENTAL, key)

    def state(self):
        return self.drawer._right_rows()[0]

    def type_pins(self, *pins):
        """Each PIN into the card's fields in turn (from its first), then Enter: its OK."""
        for index, pin in enumerate(pins):
            if index:
                self.key(Action.NAV_DOWN)
            self.key(Action.NAV_TEXT, pin)
        self.key(Action.NAV_ACTIVATE)

    def refused(self, text):
        """The card still shows, saying `text` in the warning colour."""
        self.assertTrue(self.window.settings.card_shown())
        self.assertEqual(self.card.found(), (translate(text), False))

    def with_a_pin(self):
        self.guard.set_pin(PIN)  # (as Set PIN… does: locked)
        self.window.parental_changed()

    def tearDown(self):
        everything = "\n".join(self.log.lines)
        for pin in (PIN, "1357", "1243"):
            self.assertNotIn(pin, everything)  # never logged


class PinTests(ParentalCase):
    def test_set_pin_needs_four_digits_and_the_same_twice(self):
        self.open(parental.PIN)
        self.assertEqual((self.state().kind, self.state().text), (m.INFO, "Off: no PIN is set."))
        self.assertEqual(
            self.keys(), [parental.STATE, parental.PIN, parental.ADULT, parental.HIDDEN]
        )
        self.assertEqual(self.shown()[parental.PIN].text, "Set PIN…")
        self.fit()
        self.key(Action.NAV_ACTIVATE)
        card = self.card
        self.assertEqual(card.title(), "Set PIN")
        self.assertEqual([f.label for f in card._fields], ["New PIN", "The same PIN again"])
        self.assertTrue(all(f.secret for f in card._fields))
        self.type_pins("246", "246")  # too short
        self.refused(parental.SHORT_TEXT)
        self.assertEqual(card.values(), {parental.NEW: "", parental.AGAIN: ""})
        self.assertEqual(card.focus(), ("field", parental.NEW))
        self.assertFalse(self.guard.has_pin())
        self.type_pins("2468", "2486")  # not the same
        self.refused(parental.DIFFERENT_TEXT)
        self.assertFalse(self.guard.has_pin())
        self.key(Action.NAV_TEXT, "24a68")  # digits only: the letter is no part of it
        self.assertEqual(card.values()[parental.NEW], PIN)
        self.key(Action.NAV_DOWN)
        self.type_pins(PIN)
        self.assertFalse(self.window.settings.card_shown())
        self.assertTrue(self.guard.has_pin() and self.guard.check(PIN))
        self.assertTrue(self.guard.locked())
        self.assertEqual(self.state().text, "On: locked.")
        self.assertEqual(
            self.keys(),
            [
                parental.STATE,
                parental.PIN,
                parental.REMOVE_PIN,
                parental.UNLOCK,
                parental.ADULT,
                parental.HIDDEN,
            ],
        )
        self.assertEqual(self.shown()[parental.PIN].text, "Change PIN…")
        self.assertEqual(self.shown()[parental.REMOVE_PIN].kind, m.DANGER)
        self.fit()
        self.assertNotIn(PIN, self.drawer._spoken())
        self.config.save_config(wait=True)
        stored = ConfigManager().parental
        self.assertTrue(stored["pin"])
        self.assertNotIn(PIN, repr(stored))  # its hash only

    def test_change_pin_asks_the_current_one_and_a_wrong_one_is_refused(self):
        self.with_a_pin()
        self.open(parental.PIN)
        self.key(Action.NAV_ACTIVATE)
        card = self.card
        self.assertEqual(card.title(), "Change PIN")
        self.assertEqual(
            [f.label for f in card._fields], ["Your current PIN", "New PIN", "The same PIN again"]
        )
        self.type_pins("1111", "1357", "1357")  # a wrong current PIN
        self.refused(parental.WRONG_TEXT)
        self.assertEqual(card.focus(), ("field", parental.CURRENT))
        self.assertEqual(card.values()[parental.CURRENT], "")
        self.assertTrue(self.guard.check(PIN))
        self.key(Action.NAV_TEXT, PIN)
        self.key(Action.NAV_DOWN)
        self.key(Action.NAV_DOWN)
        self.key(Action.NAV_BACKSPACE)
        self.key(Action.NAV_ACTIVATE)  # the right one, but the new one typed twice differently
        self.refused(parental.DIFFERENT_TEXT)
        self.assertTrue(self.guard.check(PIN))
        self.key(Action.NAV_TEXT, "135")
        self.key(Action.NAV_DOWN)
        self.type_pins("135")  # the same, but too short
        self.refused(parental.SHORT_TEXT)
        self.key(Action.NAV_TEXT, "1357")
        self.key(Action.NAV_DOWN)
        self.type_pins("1357")
        self.assertFalse(self.window.settings.card_shown())
        self.assertTrue(self.guard.check("1357"))
        self.assertFalse(self.guard.check(PIN))
        # Cancel changes nothing.
        self.key(Action.NAV_ACTIVATE)
        self.key(Action.NAV_TEXT, "1357")
        self.key(Action.ESCAPE)
        self.assertFalse(self.window.settings.card_shown())
        self.assertTrue(self.guard.check("1357"))
        # Remove PIN…: the current one, a wrong one refused.
        self.key(Action.NAV_DOWN)
        self.assertEqual(self.drawer.focused_row().key, parental.REMOVE_PIN)
        self.key(Action.NAV_ACTIVATE)
        self.assertEqual(card.title(), "Remove PIN")
        self.assertEqual([f.label for f in card._fields], ["Your current PIN"])
        self.type_pins(PIN)
        self.refused(parental.WRONG_TEXT)
        self.assertTrue(self.guard.has_pin())
        self.type_pins("1357")
        self.assertFalse(self.window.settings.card_shown())
        self.assertFalse(self.guard.has_pin())
        self.assertEqual(self.state().text, "Off: no PIN is set.")

    def test_unlock_and_lock(self):
        self.with_a_pin()
        self.open(parental.UNLOCK)
        self.assertNotIn(parental.LOCK, self.keys())
        self.key(Action.NAV_ACTIVATE)
        card = self.card
        self.assertEqual(card.title(), "Unlock for an hour")
        self.assertEqual([f.label for f in card._fields], ["Your PIN"])
        self.type_pins("0000")
        self.refused(parental.WRONG_TEXT)
        self.assertTrue(self.guard.locked())
        self.type_pins(PIN)
        self.assertFalse(self.window.settings.card_shown())
        self.assertFalse(self.guard.locked())
        self.assertGreater(self.guard.unlocked_for(), 3500)  # an hour
        self.assertTrue(self.window._relock.isActive())  # locked again when it runs out
        self.assertEqual(self.state().text, "On: unlocked for now.")
        self.assertNotIn(parental.UNLOCK, self.keys())
        self.assertEqual(self.drawer.focused_row().key, parental.LOCK)  # in its place
        self.fit()
        self.key(Action.NAV_ACTIVATE)  # Lock now
        self.assertTrue(self.guard.locked())
        self.assertFalse(self.window._relock.isActive())
        self.assertEqual(self.state().text, "On: locked.")
        self.assertNotIn(parental.LOCK, self.keys())
        self.assertIn(parental.UNLOCK, self.keys())


class RulesTests(ParentalCase):
    def test_adult_rule_needs_a_pin(self):
        self.open(parental.PIN)
        row = self.shown()[parental.ADULT]
        self.assertEqual(row.text, "Hide groups whose names look adult")
        self.assertEqual(row.kind, m.INFO)  # no PIN: only says it is off
        self.assertEqual(self.window.settings.value_text(row), "Off")
        self.assertFalse(self.guard.set_adult_rule(True))
        self.with_a_pin()  # locked: it can be turned on ...
        row = self.go_to(parental.ADULT)
        self.assertEqual(row.kind, m.STEP)
        self.assertEqual(self.window.settings.value_text(row), "Off")
        self.key(Action.NAV_RIGHT)
        self.assertTrue(self.guard.adult_rule())
        row = self.shown()[parental.ADULT]  # ... but not off without the PIN
        self.assertEqual((row.kind, self.window.settings.value_text(row)), (m.INFO, "On"))
        self.assertNotEqual(self.drawer.focused_row().key, parental.ADULT)
        self.fit()
        self.guard.unlock(PIN)
        self.window.parental_changed()
        row = self.go_to(parental.ADULT)
        self.assertEqual(row.kind, m.STEP)
        self.key(Action.NAV_RIGHT)
        self.assertFalse(self.guard.adult_rule())
        self.config.save_config(wait=True)
        self.assertIs(ConfigManager().parental.get("adult"), False)

    def test_hidden_list_only_while_unlocked(self):
        self.with_a_pin()
        self.guard.hide_group("live", PROVIDER_NAME, "g1", "Kids")
        self.guard.hide_group("live", PROVIDER_NAME, "g2", "News")
        self.window.parental_changed()
        self.open(parental.PIN)
        row = self.shown()[parental.HIDDEN]
        self.assertEqual((row.kind, row.text), (m.INFO, "Unlock to see or change what is hidden."))
        self.assertIsNone(row.section)  # nothing of it listed while locked
        self.fit()
        self.guard.unlock(PIN)
        self.window.parental_changed()
        row = self.go_to(parental.HIDDEN)
        self.assertEqual((row.kind, row.text, row.note), (m.OPENS, "Hidden while locked", "2"))
        self.key(Action.NAV_ACTIVATE)
        self.assertEqual(self.drawer.path(), (m.PARENTAL, parental.HIDDEN))
        rows = self.drawer._right_rows()
        self.assertEqual([r.text for r in rows], ["Kids", "News"])
        self.assertEqual({r.sub for r in rows}, {PROVIDER_NAME})
        self.assertEqual({r.note for r in rows}, {"Show again"})
        self.fit()
        self.key(Action.NAV_ACTIVATE)  # Kids shows again, at once
        self.assertEqual([h["title"] for h in self.guard.hidden()], ["News"])
        self.assertEqual([r.text for r in self.drawer._right_rows()], ["News"])
        # The hour runs out while it shows: the list goes with it.
        self.guard.unlock(PIN, seconds=0.2)
        self.window.parental_changed()
        self.assertTrue(
            wait_until(lambda: self.shown().get(parental.HIDDEN, row).kind == m.INFO, 3000)
        )
        self.assertTrue(self.guard.locked())
        self.assertEqual(self.drawer.path(), (m.PARENTAL,))
        self.assertEqual([h["title"] for h in self.guard.hidden()], ["News"])

    def test_a_hidden_link_names_network_streams_under_it(self):
        # A link is hidden as Network streams' (TVController._guard_view): its row names the
        # list as the TV's drawer does, never the stamp.
        self.with_a_pin()
        link = {"_provider": NETWORK_PROVIDER, "id": "6ca35429d7503b1b", "name": "Harbour cam"}
        self.guard.unlock(PIN)
        self.guard.hide_item("live", link)
        self.window.parental_changed()
        self.open(parental.HIDDEN)
        self.key(Action.NAV_ACTIVATE)
        rows = self.drawer._right_rows()
        self.assertEqual([(r.text, r.sub) for r in rows], [("Harbour cam", "Network streams")])

    def test_every_change_tells_the_window(self):
        with mock.patch.object(
            self.window, "parental_changed", wraps=self.window.parental_changed
        ) as told:
            self.open(parental.PIN)
            self.key(Action.NAV_ACTIVATE)
            self.type_pins(PIN, PIN)  # Set PIN…
            self.assertEqual(told.call_count, 1)
            self.go_to(parental.UNLOCK)
            self.key(Action.NAV_ACTIVATE)
            self.type_pins("0000")  # refused: nothing changed
            self.assertEqual(told.call_count, 1)
            self.type_pins(PIN)  # unlocked
            self.assertEqual(told.call_count, 2)
            self.go_to(parental.ADULT)
            self.key(Action.NAV_RIGHT)  # on
            self.key(Action.NAV_RIGHT)  # off
            self.assertEqual(told.call_count, 4)
            self.guard.hide_group("live", PROVIDER_NAME, "g1", "Kids")
            self.window.settings.refresh_sections()
            self.go_to(parental.HIDDEN)
            self.key(Action.NAV_ACTIVATE)
            self.key(Action.NAV_ACTIVATE)  # Show again
            self.assertEqual(told.call_count, 5)
            self.key(Action.ESCAPE)
            self.go_to(parental.LOCK)
            self.key(Action.NAV_ACTIVATE)  # Lock now
            self.assertEqual(told.call_count, 6)
            self.go_to(parental.REMOVE_PIN)
            self.key(Action.NAV_ACTIVATE)
            self.key(Action.NAV_DOWN)
            self.card_focus(self.card, CANCEL)
            self.key(Action.NAV_ACTIVATE)  # Cancel: nothing changed
            self.assertEqual(told.call_count, 6)
            self.key(Action.NAV_ACTIVATE)
            self.type_pins(PIN)  # Remove PIN…
            self.assertEqual(told.call_count, 7)
        self.assertFalse(self.guard.has_pin())
        self.assertEqual(self.state().text, "Off: no PIN is set.")


if __name__ == "__main__":
    unittest.main()
