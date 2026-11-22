"""Plex's sign-in card (2.14): "Plex" on the Add card shows a code to enter at plex.tv/link,
looks until it is entered (one look at a time, LOOK_MS apart), then adds the account's server
(one at once, several a choice) with the server's own token, never the account's. Driven by
the keys over a real Settings drawer, with a fake TV window over a real provider list; each
job is held, and answered by the test (none reaches plex.tv). Fictional names and hosts."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QWidget
from timer_fixtures import elapse

from services import plex_workers as pw
from services.settings_model import PROVIDERS, sections
from tests.test_provider_changes import PANEL, PLAYLIST, manager_of, temp_manager
from tests.test_settings_providers import FakeValues, FakeWindow
from ui import plex_signin as ps
from ui.keymap import Action
from ui.overlays.settings_card import CANCEL
from ui.overlays.settings_drawer import SettingsDrawer
from ui.provider_cards import ProviderCards
from ui.settings_controller import SettingsController
from ui.settings_providers import ProviderRows, layer_key, row_key

HOME = {
    "name": "Home Media",
    "machine_id": "machine-home",
    "token": "server-token",
    "owned": True,
    "url": "https://10-0-0-5.example.plex.direct:32400",
}
FRIEND = {
    "name": "A Friend's Films",
    "machine_id": "machine-friend",
    "token": "friend-token",
    "owned": False,
    "url": "https://198-51-100-9.example.plex.direct:32400",
}


class AnsweringRunner:
    """Holds each job; the test answers it (its finished or its error), never running it."""

    def __init__(self):
        self.jobs = []

    def run_worker(self, worker, on_finished, on_error=None, context=None):
        self.jobs.append((worker, on_finished, on_error, context))
        return worker

    def last(self, kind):
        worker = self.jobs[-1][0]
        assert isinstance(worker, kind), (type(worker).__name__, kind.__name__)
        return worker

    def answer(self, payload, index=-1):
        _worker, on_finished, _on_error, context = self.jobs.pop(index)
        on_finished(context, payload)

    def fail(self, message, index=-1):
        _worker, _on_finished, on_error, context = self.jobs.pop(index)
        on_error(context, message)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class PlexSignInTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.config, self.manager = temp_manager(self, [PLAYLIST, PANEL])
        self.config.ssl_verify = True
        self.config.selected_provider_name = "Lists"
        self.window = FakeWindow(self.config, self.manager)
        self.window.jobs = AnsweringRunner()
        self.host = QWidget()
        self.host.resize(1280, 720)
        self.host.show()
        self.addCleanup(self.host.deleteLater)
        self.addCleanup(self.host.close)
        self.drawer = SettingsDrawer(
            lambda row: self.settings.value_text(row),
            lambda row: self.settings.lit(row),
            self.host,
        )
        self.settings = SettingsController(
            self.drawer,
            FakeValues(),
            lambda: sections("Darwin", self.rows.rows()),
            lambda: None,
        )
        self.rows = ProviderRows(self.window, self.settings, self.drawer)
        self.window.rows = self.rows
        self.cards = ProviderCards(self.window, self.settings, self.drawer, self.rows)
        self.rows.cards = self.cards
        self.signin = self.cards.plex
        self.clock = Clock()
        self.signin._clock = self.clock
        self.card = self.signin.card
        self.jobs = self.window.jobs
        self.settings.open(PROVIDERS)

    def tearDown(self):
        elapse(QApplication.doubleClickInterval() + 80)

    # ---- helpers ------------------------------------------------------------------------------

    def key(self, action):
        self.assertTrue(self.settings.handle(action), action)

    def press_on_add_card(self, key):
        """The Add card's other way `key`, by the keys."""
        form = self.cards.form
        for _ in range(12):
            self.key(Action.NAV_DOWN)
        for _ in range(10):
            self.key(Action.NAV_LEFT)
        for _ in range(10):
            if form.focus() == ("alt", key):
                break
            self.key(Action.NAV_RIGHT)
        self.assertEqual(form.focus(), ("alt", key))
        self.key(Action.NAV_ACTIVATE)

    def answer(self, key):
        """The card's button `key`, by the keys."""
        self.assertTrue(self.card.isVisible())
        for _ in range(6):
            self.key(Action.NAV_LEFT)
            self.key(Action.NAV_UP)
        for _ in range(6):
            if self.card.focus() == ("button", key):
                break
            self.key(Action.NAV_RIGHT)
            if self.card.focus() != ("button", key):
                self.key(Action.NAV_DOWN)
        self.assertEqual(self.card.focus(), ("button", key))
        self.key(Action.NAV_ACTIVATE)

    def buttons(self):
        return [(b.key, b.text) for b in self.card._buttons]

    def saved(self):
        return manager_of(self, self.config).providers

    def signed_in(self, servers, listed=None):
        """Through the code to the servers' answer."""
        self.cards.add()
        self.press_on_add_card("PLEX")
        self.jobs.answer({"id": 7, "code": "AB12", "expires_in": 900})
        self.signin._look()
        self.jobs.answer({"token": "account-token"})
        self.jobs.last(pw.ServersJob)
        self.jobs.answer({"listed": len(servers) if listed is None else listed, "servers": servers})

    # ---- the code -----------------------------------------------------------------------------

    def test_plex_on_the_add_card_shows_a_code_and_looks_until_it_is_entered(self):
        self.cards.add()
        alternates = [b.key for b in self.cards.form._alternates]
        self.assertEqual(alternates[1:5], ["STB", "JELLYFIN", "EMBY", "PLEX"])
        self.assertEqual(self.cards.form._alternates[4].text, "Plex")
        self.press_on_add_card("PLEX")
        self.assertTrue(self.cards.form.isHidden())  # the Add card gave way
        self.assertTrue(self.settings.card_shown())
        self.assertEqual(self.card.title(), "Sign in to Plex")
        self.assertEqual(self.card.body(), "Asking plex.tv for a code…")
        job = self.jobs.last(pw.PinJob)
        self.assertEqual(len(job.client_id), 32)  # QiTV's own, random
        self.jobs.answer({"id": 7, "code": "AB12", "expires_in": 900})
        self.assertEqual((self.card.code(), self.card.link()), ("AB12", "plex.tv/link"))
        self.assertEqual(
            self.card.body(),
            "Enter it at plex.tv/link, on any phone or computer. This card waits for it.",
        )
        self.assertEqual(self.buttons(), [(CANCEL, "Cancel")])
        # Large, spaced and over the words: read from across the room.
        code, link, body = (self.card.control_rect(zone) for zone in ("code", "link", "body"))
        self.assertTrue(code.bottom() <= link.top() and link.bottom() <= body.top())
        self.assertGreater(code.height(), 2 * link.height())
        self.assertTrue(self.signin._timer.isActive())
        self.assertEqual(self.signin._timer.interval(), ps.LOOK_MS)
        self.signin._look()  # (the timer's)
        look = self.jobs.last(pw.PinCheckJob)
        self.assertEqual((look.client_id, look.pin_id), (job.client_id, 7))
        self.assertFalse(self.signin._timer.isActive())  # one look at a time
        self.jobs.answer({"token": ""})  # not yet
        self.assertTrue(self.signin._timer.isActive())
        self.signin._look()
        self.jobs.fail("plex.tv could not be reached (ConnectionError).")  # a moment off
        self.assertTrue(self.signin._timer.isActive())  # it looks on
        self.assertEqual(self.card.code(), "AB12")
        self.signin._look()
        self.jobs.answer({"token": "account-token"})
        self.assertEqual(self.card.body(), "Signed in. Looking for your Plex servers…")
        self.assertEqual(self.card.code(), "")  # the code went with its step
        self.assertFalse(self.signin._timer.isActive())
        servers = self.jobs.last(pw.ServersJob)
        self.assertEqual(servers.client_id, job.client_id)
        self.assertTrue(servers.verify)

    def test_a_code_that_expired_offers_a_new_one(self):
        self.cards.add()
        self.press_on_add_card("PLEX")
        self.jobs.answer({"id": 7, "code": "AB12", "expires_in": 900})
        self.clock.now += 901
        self.signin._look()
        self.assertEqual(self.jobs.jobs, [])  # not asked: it ran out here
        self.assertEqual(self.card.body(), "The code expired before it was entered.")
        self.assertEqual(self.card.focus(), ("button", ps.NEW_CODE))
        self.answer(ps.NEW_CODE)
        self.jobs.last(pw.PinJob)
        self.jobs.answer({"id": 8, "code": "CD34", "expires_in": 900})
        self.assertEqual(self.card.code(), "CD34")
        # plex.tv saying it expired is the same.
        self.signin._look()
        self.jobs.answer({"token": "", "expired": True})
        self.assertEqual(self.card.body(), "The code expired before it was entered.")

    def test_no_code_from_plex_tv_is_said_with_try_again(self):
        self.cards.add()
        self.press_on_add_card("PLEX")
        self.jobs.fail("plex.tv could not be reached (ConnectionError).")
        self.assertEqual(self.card.body(), "plex.tv could not be reached (ConnectionError).")
        self.answer(ps.RETRY)
        self.jobs.last(pw.PinJob)

    def test_cancel_drops_what_is_on_its_way(self):
        self.cards.add()
        self.press_on_add_card("PLEX")
        self.jobs.answer({"id": 7, "code": "AB12", "expires_in": 900})
        self.signin._look()
        self.key(Action.ESCAPE)
        self.assertTrue(self.card.isHidden())
        self.assertFalse(self.signin._timer.isActive())
        self.jobs.answer({"token": "account-token"})  # late
        self.assertEqual(self.jobs.jobs, [])
        self.assertTrue(self.card.isHidden())
        self.assertNotIn("PLEX", [p["type"] for p in self.saved()])

    def test_settings_going_cancels_it(self):
        self.cards.add()
        self.press_on_add_card("PLEX")
        self.jobs.answer({"id": 7, "code": "AB12", "expires_in": 900})
        self.settings.close()
        self.assertTrue(self.card.isHidden())
        self.assertFalse(self.signin._timer.isActive())

    # ---- the servers --------------------------------------------------------------------------

    def test_one_server_is_added_with_its_own_token_selected_and_said(self):
        self.signed_in([HOME])
        self.assertTrue(self.card.isHidden())
        client_id = self.signin_client_id
        entry = next(p for p in self.saved() if p["type"] == "PLEX")
        self.assertEqual(
            entry,
            {
                "type": "PLEX",
                "name": "Home Media",
                "url": HOME["url"],
                "token": "server-token",
                "server_id": "machine-home",
                "device_id": client_id,
            },
        )
        self.assertNotIn("account-token", repr(self.saved()))
        self.assertEqual(self.config.selected_provider_name, "Home Media")
        self.assertIn(("Added Home Media to your providers", "info"), self.window.toasts)
        # An older build's list never names it (services.newer_kinds).
        with open(self.manager.index_file, encoding="utf-8") as f:
            self.assertNotIn("PLEX", f.read())

    @property
    def signin_client_id(self):
        return next(p for p in self.saved() if p["type"] == "PLEX")["device_id"]

    def test_several_servers_are_a_choice(self):
        self.signed_in([HOME, FRIEND])
        self.assertEqual(self.card.title(), "Choose a Plex server")
        self.assertEqual(
            self.buttons(),
            [
                (CANCEL, "Cancel"),
                ("server:machine-home", "Home Media"),
                ("server:machine-friend", "A Friend's Films"),
            ],
        )
        self.answer("server:machine-friend")
        names = [p["name"] for p in self.saved()]
        self.assertEqual(names, ["Lists", "Panel", "A Friend's Films"])

    def test_a_name_taken_is_made_unique(self):
        self.signed_in([{**HOME, "name": "panel"}])
        self.assertIn("panel (2)", [p["name"] for p in self.saved()])

    def test_none_answering_says_so_and_tries_again(self):
        self.signed_in([], listed=2)
        self.assertEqual(
            self.card.body(),
            "None of your Plex servers answered. Check that one is on, then try again.",
        )
        self.answer(ps.RETRY)
        self.jobs.last(pw.ServersJob)
        self.jobs.answer({"listed": 0, "servers": []})
        self.assertEqual(self.card.body(), "Your Plex account has no server.")

    # ---- signing in again ---------------------------------------------------------------------

    def add_plex(self):
        self.signed_in([HOME])
        self.jobs.jobs.clear()
        return self.signin_client_id

    def test_address_and_login_on_a_plex_provider_signs_in_again_in_place(self):
        client_id = self.add_plex()
        self.settings.open(PROVIDERS, row=layer_key("Home Media"))
        self.key(Action.NAV_ACTIVATE)
        for _ in range(4):
            if self.drawer.focused_row().key == row_key("Home Media", "login"):
                break
            self.key(Action.NAV_DOWN)
        self.assertEqual(self.drawer.focused_row().key, row_key("Home Media", "login"))
        self.key(Action.NAV_ACTIVATE)
        self.assertEqual(self.card.title(), "Sign in to Plex")
        self.assertEqual(self.jobs.last(pw.PinJob).client_id, client_id)  # the same client
        self.jobs.answer({"id": 9, "code": "EF56", "expires_in": 900})
        self.signin._look()
        self.jobs.answer({"token": "account-token-2"})
        moved = {**HOME, "token": "server-token-2", "url": "http://10.0.0.5:32400"}
        self.jobs.answer({"listed": 2, "servers": [FRIEND, moved]})
        self.assertTrue(self.card.isHidden())
        entry = next(p for p in self.saved() if p["type"] == "PLEX")
        self.assertEqual(
            (entry["name"], entry["token"], entry["url"]),
            ("Home Media", "server-token-2", "http://10.0.0.5:32400"),
        )
        self.assertEqual(len([p for p in self.saved() if p["type"] == "PLEX"]), 1)

    def test_signing_in_again_to_an_account_without_that_server_adds_another(self):
        self.add_plex()
        self.cards.edit("Home Media")
        self.jobs.answer({"id": 9, "code": "EF56", "expires_in": 900})
        self.signin._look()
        self.jobs.answer({"token": "account-token-2"})
        self.jobs.answer({"listed": 1, "servers": [FRIEND]})
        self.assertEqual(self.card.title(), "Choose a Plex server")
        self.assertIn("Home Media's server is not among them", self.card.body())
        self.answer("server:machine-friend")
        self.assertEqual(len([p for p in self.saved() if p["type"] == "PLEX"]), 2)


if __name__ == "__main__":
    unittest.main()
