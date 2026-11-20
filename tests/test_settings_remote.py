"""Settings › Phone remote in the drawer (plan 3): the old Settings window's Phone remote tab,
row by row, each change applied at once (the remote started, changed or stopped; turned off
only once asked), its state as the remote tells it, the paired phones each removed once asked,
and a newer QiTV's settings left as they are. Over a real TV window; the remote's hub is a stub
(no network)."""

from datetime import datetime
import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from timer_fixtures import wait_until

from services import settings_model as m
from services.config_manager import ConfigManager
from services.i18n import translate
from services.remote.protocol import RELAY_URL, STORE_VERSION, Status
from tests.test_settings_guide import GuideCase
from ui import settings_remote as remote_rows
from ui.keymap import Action
from ui.overlays.settings_card import CANCEL
from ui.settings_remote import NEWER_SETTINGS

USED = 1_700_000_000.0
PHONE_A = {"id": "d1", "name": "Phone A", "paired_at": 1.0, "last_used": USED, "keys": []}
PHONE_B = {"id": "d/2", "name": "Phone B", "paired_at": 1.0, "last_used": 0, "keys": []}


class RemoteCase(GuideCase):
    stored: dict = {}

    def setUp(self):
        hub = mock.patch("services.remote.hub.RemoteHub")  # never a real hub's thread
        self.hub = hub.start()
        self.addCleanup(hub.stop)
        super().setUp()
        self.addCleanup(setattr, self.window.remote, "hub", None)  # (the stub has no thread)

    def prepare_config(self):
        if self.stored:
            self.config.remote = dict(self.stored)

    def value(self, key):
        return self.window.settings.value_text(self.shown()[key])

    def kinds(self):
        return {row.key: row.kind for row in self.drawer._right_rows()}


class RemoteTests(RemoteCase):
    stored = {"version": 1, "enabled": False, "devices": [PHONE_A], "room": "r"}

    def test_turning_the_remote_on_applies(self):
        row = self.open_at(m.REMOTE, remote_rows.ENABLED)
        self.assertEqual(
            self.keys(),
            [
                remote_rows.ENABLED,
                remote_rows.RELAY,
                remote_rows.HOME,
                remote_rows.RELAY_STATE,
                remote_rows.PHONES,
                remote_rows.PAIR,
            ],
        )
        self.assertEqual(
            [r.text for r in self.drawer._right_rows()],
            [
                "Phone remote",
                "Reach QiTV from any network",
                "Home address",
                "Relay",
                "Paired phones",
                "Show the pairing code on the TV",
            ],
        )
        self.assertEqual(self.window.settings.value_text(row), "Off")
        self.fit()
        self.key(Action.NAV_RIGHT)  # on: the remote starts
        data = self.config.remote
        self.assertEqual(
            (data["version"], data["enabled"], data["relay"], data["relay_url"]),
            (STORE_VERSION, True, True, RELAY_URL),
        )
        self.assertEqual((data["devices"], data["room"]), ([PHONE_A], "r"))  # kept
        self.hub.return_value.start.assert_called_once()
        self.assertIsNotNone(self.window.remote.hub)
        self.assertEqual(self.value(remote_rows.ENABLED), "On")
        self.assertEqual(self.drawer.focused_row().key, remote_rows.ENABLED)
        self.config.save_config(wait=True)
        self.assertTrue(ConfigManager().remote["enabled"])
        self.key(Action.NAV_RIGHT)  # off: asked first (RemoteOnTests), then it stops
        self.key(Action.NAV_RIGHT)
        self.key(Action.NAV_ACTIVATE)
        self.assertFalse(self.config.remote["enabled"])
        self.hub.return_value.stop_soon.assert_called_once()
        self.assertIsNone(self.window.remote.hub)

    def test_relay_row_is_info_while_off(self):
        self.open_at(m.REMOTE, remote_rows.ENABLED)
        self.assertEqual(self.kinds()[remote_rows.RELAY], m.INFO)  # says how it is set
        self.assertEqual(self.value(remote_rows.RELAY), "On")
        self.key(Action.NAV_DOWN)  # past it, and past the state's lines
        self.assertEqual(self.drawer.focused_row().key, remote_rows.PHONES)
        self.key(Action.NAV_UP)
        self.key(Action.NAV_RIGHT)  # the remote on: the relay can be turned off
        self.assertEqual(self.kinds()[remote_rows.RELAY], m.STEP)
        self.key(Action.NAV_DOWN)
        self.assertEqual(self.drawer.focused_row().key, remote_rows.RELAY)
        self.key(Action.NAV_RIGHT)  # off: asked first (RemoteOnTests)
        self.key(Action.NAV_RIGHT)
        self.key(Action.NAV_ACTIVATE)
        self.assertIs(self.config.remote["relay"], False)
        self.assertIs(self.config.remote["enabled"], True)
        self.hub.return_value.apply.assert_called_once()  # the running remote follows
        self.assertIs(self.hub.return_value.apply.call_args.args[0]["relay"], False)
        self.config.save_config(wait=True)
        self.assertIs(ConfigManager().remote["relay"], False)

    def test_status_note_follows_the_hub(self):
        self.open_at(m.REMOTE, remote_rows.ENABLED)
        self.assertEqual(self.value(remote_rows.HOME), "—")
        self.assertEqual(self.value(remote_rows.RELAY_STATE), "Off")
        post = self.window.remote.bridge.post
        with mock.patch.object(
            self.window.settings, "refresh_sections", wraps=self.window.settings.refresh_sections
        ) as refresh:
            post(Status("http://192.168.1.20:5384/", "online"))
            self.assertTrue(wait_until(lambda: refresh.call_count >= 1))
        self.assertEqual(self.value(remote_rows.HOME), "http://192.168.1.20:5384/")
        self.assertEqual(self.value(remote_rows.RELAY_STATE), "Connected")
        for state, text in (
            ("resting", "Resting until 00:00 UTC (its daily limit)"),
            ("away", "Can't connect; trying again"),
            ("replaced", "Another QiTV is using this relay room"),
            ("error", "The phone remote couldn't start"),
            ("something new", "Off"),
        ):
            post(Status("", state))
            self.assertTrue(wait_until(lambda: self.value(remote_rows.RELAY_STATE) == text), state)
        self.assertEqual(self.value(remote_rows.HOME), "—")
        self.fit()
        # Hidden, Settings builds nothing for it (it is built as it opens).
        self.window.settings.close()
        with mock.patch.object(self.window.settings, "refresh_sections") as refresh:
            post(Status("", "online"))
            self.assertTrue(wait_until(lambda: self.window.remote.status["relay"] == "online"))
            refresh.assert_not_called()

    def test_the_pairing_code_closes_settings_first(self):
        self.open_at(m.REMOTE, remote_rows.PAIR)
        self.key(Action.NAV_ACTIVATE)
        self.assertFalse(self.window.settings.visible())
        self.assertTrue(self.window.pair_overlay.isVisible())  # the remote turned on for it
        self.assertTrue(self.config.remote["enabled"])
        self.hub.return_value.set_pairing.assert_called_with(True)


class RemoteOnTests(RemoteCase):
    stored = {"version": 1, "enabled": True, "relay": True, "devices": [PHONE_A], "room": "r"}

    def index(self, key):
        return [row.key for row in self.drawer._right_rows()].index(key)

    def test_turning_the_remote_off_asks_first(self):
        # The first row the keys reach, so the phone's ← (meant as "back") and OK step it: a
        # question first, Cancel where the keys start; nothing changes until Turn off.
        row = self.open_at(m.REMOTE, remote_rows.ENABLED)
        self.window.remote.hub = self.hub.return_value  # (it runs)
        self.hub.return_value.reset_mock()
        self.assertEqual(self.window.settings.value_text(row), "On")
        for action in (Action.NAV_LEFT, Action.NAV_ACTIVATE, Action.NAV_RIGHT):
            self.key(action)
            question = self.window.remote_rows.question
            self.assertTrue(self.window.settings.card_shown(), action)
            self.assertEqual(question.title(), "Turn off the phone remote?")
            self.assertEqual(
                question.body(), "Paired phones can't control QiTV until it's on again."
            )
            self.assertEqual(question.focus(), ("button", CANCEL))
            self.key(Action.NAV_ACTIVATE if action is not Action.NAV_RIGHT else Action.ESCAPE)
            self.assertFalse(self.window.settings.card_shown())
            self.assertIs(self.config.remote["enabled"], True)
        # The mouse's ‹ asks too.
        QTest.mouseClick(
            self.drawer,
            Qt.MouseButton.LeftButton,
            pos=self.drawer.step_rect(self.index(row.key), -1).center().toPoint(),
        )
        self.assertTrue(self.window.settings.card_shown())
        self.key(Action.ESCAPE)
        # Settings going with the question up keeps it on.
        self.key(Action.NAV_LEFT)
        self.window.settings.close()
        self.assertTrue(wait_until(lambda: not self.window.settings.visible()))
        self.assertTrue(self.window.remote_rows.question.isHidden())
        self.assertIs(self.config.remote["enabled"], True)
        self.hub.return_value.stop_soon.assert_not_called()
        # Turn off: off, saved, and the remote stops.
        self.open_at(m.REMOTE, remote_rows.ENABLED)
        self.key(Action.NAV_LEFT)
        self.key(Action.NAV_RIGHT)
        self.assertEqual(self.window.remote_rows.question.focus(), ("button", remote_rows.TURN_OFF))
        self.key(Action.NAV_ACTIVATE)
        self.assertFalse(self.window.settings.card_shown())
        self.assertIs(self.config.remote["enabled"], False)
        self.hub.return_value.stop_soon.assert_called_once()
        self.assertIsNone(self.window.remote.hub)
        self.assertEqual(self.value(remote_rows.ENABLED), "Off")
        self.config.save_config(wait=True)
        self.assertIs(ConfigManager().remote["enabled"], False)
        # On again: at once, unasked.
        self.key(Action.NAV_LEFT)
        self.assertFalse(self.window.settings.card_shown())
        self.assertIs(self.config.remote["enabled"], True)

    def test_turning_the_relay_off_asks_first(self):
        self.open_at(m.REMOTE, remote_rows.RELAY)
        self.window.remote.hub = self.hub.return_value  # (it runs)
        self.hub.return_value.reset_mock()
        self.key(Action.NAV_LEFT)
        question = self.window.remote_rows.question
        self.assertTrue(self.window.settings.card_shown())
        self.assertEqual(question.title(), "Turn off the relay?")
        self.assertEqual(
            question.body(), "Phones on other networks can't reach QiTV until it's on again."
        )
        self.assertEqual(question.focus(), ("button", CANCEL))
        self.key(Action.NAV_ACTIVATE)  # Cancel: it stays on
        self.assertIs(self.config.remote["relay"], True)
        self.hub.return_value.apply.assert_not_called()
        self.key(Action.NAV_ACTIVATE)
        self.key(Action.NAV_RIGHT)
        self.key(Action.NAV_ACTIVATE)  # Turn off
        self.assertIs(self.config.remote["relay"], False)
        self.assertIs(self.config.remote["enabled"], True)
        self.hub.return_value.apply.assert_called_once()
        self.assertIs(self.hub.return_value.apply.call_args.args[0]["relay"], False)
        self.key(Action.NAV_RIGHT)  # on again: at once, unasked
        self.assertFalse(self.window.settings.card_shown())
        self.assertIs(self.config.remote["relay"], True)
        self.assertEqual(self.hub.return_value.apply.call_count, 2)

    def test_a_phones_removal_after_a_cancelled_turn_off_removes_only_it(self):
        # One card asks both questions: an answer is its own question's.
        self.open_at(m.REMOTE, remote_rows.ENABLED)
        self.window.remote.hub = self.hub.return_value  # (it runs)
        self.hub.return_value.reset_mock()
        self.key(Action.NAV_LEFT)
        self.key(Action.ESCAPE)  # the remote's question, cancelled
        self.window.remote_rows._ask("d1", "Phone A")
        question = self.window.remote_rows.question
        self.assertEqual(question.title(), "Remove the phone “Phone A”?")
        self.key(Action.NAV_RIGHT)
        self.key(Action.NAV_ACTIVATE)
        self.hub.return_value.remove_device.assert_called_once_with("d1")
        self.assertIs(self.config.remote["enabled"], True)  # only the phone goes
        self.hub.return_value.stop_soon.assert_not_called()


class RelayAddressTests(RemoteCase):
    stored = {"version": 1, "enabled": False, "relay_url": "https://relay.example"}

    def test_a_relay_address_set_in_the_config_stays(self):
        # It is set in config.json only (to test a relay): no row has it, and a step keeps it.
        self.open_at(m.REMOTE, remote_rows.ENABLED)
        self.assertFalse([row for row in self.drawer._right_rows() if "relay.example" in row.note])
        self.key(Action.NAV_RIGHT)
        self.assertEqual(self.config.remote["relay_url"], "https://relay.example")
        self.config.save_config(wait=True)
        self.assertEqual(ConfigManager().remote["relay_url"], "https://relay.example")


class PhonesTests(RemoteCase):
    stored = {"version": 1, "devices": [PHONE_A, PHONE_B]}

    def open_phones(self):
        self.open_at(m.REMOTE, remote_rows.PHONES)
        self.assertEqual(self.drawer.focused_row().note, "2")
        self.key(Action.NAV_ACTIVATE)
        self.assertEqual(self.drawer.path(), (m.REMOTE, remote_rows.PHONES))

    def test_removing_a_phone_asks(self):
        self.open_phones()
        rows = self.drawer._right_rows()
        self.assertEqual([r.text for r in rows], ["Phone A", "Phone B"])
        when = datetime.fromtimestamp(USED).strftime("%Y-%m-%d")
        self.assertEqual([r.sub for r in rows], [f"Last used {when}", "Last used —"])
        self.assertTrue(all(r.literal for r in rows))  # names as the phones gave them
        self.fit()
        self.key(Action.NAV_ACTIVATE)  # Phone A: asked first, Cancel the answer the keys are on
        question = self.window.remote_rows.question
        self.assertTrue(self.window.settings.card_shown())
        self.assertEqual(question.title(), "Remove the phone “Phone A”?")
        self.assertEqual(question.focus(), ("button", CANCEL))
        self.key(Action.NAV_ACTIVATE)
        self.assertEqual(len(self.config.remote["devices"]), 2)
        self.key(Action.NAV_ACTIVATE)
        self.key(Action.NAV_RIGHT)
        self.assertEqual(question.focus(), ("button", remote_rows.REMOVE))
        self.key(Action.NAV_ACTIVATE)  # the remote is off: unpaired in the settings at once
        self.assertEqual(self.config.remote["devices"], [PHONE_B])
        self.assertIn(("Phone A can no longer control QiTV", "info"), self.toasts)
        self.assertEqual([r.text for r in self.drawer._right_rows()], ["Phone B"])
        self.assertEqual(self.drawer.focused_row().text, "Phone B")
        # With the remote on, its hub removes it (the row goes once it has).
        self.window.remote.hub = self.hub.return_value
        self.key(Action.NAV_ACTIVATE)
        self.key(Action.NAV_RIGHT)
        self.key(Action.NAV_ACTIVATE)
        self.hub.return_value.remove_device.assert_called_once_with("d/2")
        self.window.remote.hub = None
        self.window.remote.remove_device("d/2")  # (as the hub's save would tell it)
        self.assertEqual(self.drawer._right_rows(), ())
        layer = self.drawer._chain()[-1]
        self.assertEqual(translate(layer.empty_text), "No paired phones")
        self.assertEqual(self.drawer.focus_side(), "left")


class NewerTests(RemoteCase):
    stored = {"version": 2, "enabled": True, "future": "kept"}

    def test_a_newer_qitvs_phone_remote_is_left_alone(self):
        self.window.open_settings(section_key=m.REMOTE)
        rows = self.drawer._right_rows()
        self.assertEqual(rows[0].text, NEWER_SETTINGS)  # the old tab's note, first
        self.assertEqual({row.kind for row in rows}, {m.INFO})  # every row only says
        self.assertEqual(self.value(remote_rows.ENABLED), "On")
        self.fit()
        self.key(Action.NAV_RIGHT)  # nothing to focus: the keys stay on the sections
        self.assertEqual(self.drawer.focus_side(), "left")
        handler = self.window.remote_rows
        for row in rows:  # nor anything a row could change
            handler.step(row, 1)
            handler.chosen(row)
        self.assertEqual(self.config.remote, self.stored)
        self.hub.assert_not_called()
        self.assertIsNone(self.window.remote.hub)
        self.config.save_config(wait=True)
        self.assertEqual(ConfigManager().remote, self.stored)

    def test_phone_remote_menu_opens_this_section(self):
        # The menu's Phone Remote… when the remote can't start: Settings at Phone remote, which
        # says why.
        self.window.open_phone_remote()
        self.assertTrue(self.window.settings.visible())
        self.assertEqual(self.drawer.path(), (m.REMOTE,))
        self.assertEqual(self.drawer._right_rows()[0].text, NEWER_SETTINGS)
        self.assertFalse(self.window.pair_overlay.isVisible())


if __name__ == "__main__":
    unittest.main()
