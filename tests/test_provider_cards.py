"""The Add card and the Address-and-login card (plan 2): a link fills the card and says what it
is, a check runs before anything is kept (a media server signs in, its password never kept), a
failed one offers Add anyway (never for Jellyfin or Emby, or a playlist file QiTV can't find),
and the guard asks before a link would turn a provider into another one. Driven by the keys
over a real Settings drawer, with a fake TV window over a real provider list and each check
held until the test answers it. Fictional hosts."""

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QFileDialog, QWidget
from timer_fixtures import elapse, wait_until

from services import provider_check
from services.i18n import translate
from services.provider_check import Outcome
from services.settings_model import ADD_PROVIDER, PROVIDERS, sections
from tests.fixtures.local_provider import PROVIDER_NAME
from tests.test_provider_changes import PANEL, PLAYLIST, manager_of, temp_manager
from tests.test_settings_providers import FakeValues, FakeWindow
from tests.test_tv_window import WindowCase
from ui import provider_cards as pc
from ui.keymap import Action
from ui.overlays.question_card import QuestionCard
from ui.overlays.settings_card import CANCEL
from ui.overlays.settings_drawer import SettingsDrawer
from ui.provider_cards import NO_LINK_TEXT, ProviderCards
from ui.settings_controller import SettingsController
from ui.settings_providers import (
    HTTP_ONLY_NOTE,
    SAVE_FAILED_TEXT,
    ProviderRows,
    layer_key,
    row_key,
)

XTREAM_LINK = "http://other.test:8080/get.php?username=other&password=s3cret&type=m3u_plus"
PORTAL_LINK = "http://portal.test/stalker_portal/c/index.html?mac=00:1a:79:00:00:01"
M3U_LINK = "http://cdn.test/live/news.m3u8"
SIGNED_IN = {"token": "t1", "user_id": "u1", "device_id": "d1"}


class CardsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.config, self.manager = temp_manager(self, [PLAYLIST, PANEL])
        self.config.ssl_verify = True
        self.config.selected_provider_name = "Lists"
        self.window = FakeWindow(self.config, self.manager)
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
        self.form, self.question = self.cards.form, self.cards.question
        self.settings.open(PROVIDERS)

    def tearDown(self):
        elapse(QApplication.doubleClickInterval() + 80)  # no second-click guard left armed

    # ---- helpers ------------------------------------------------------------------------------

    def key(self, action, payload=""):
        self.assertTrue(self.settings.handle(action, payload), action)

    def type_text(self, key, text, *, at_once=False):
        """Field `key`'s text replaced by `text`, typed one character at a time (`at_once`: in
        one go, as the phone sends a word)."""
        self.form.focus_field(key)
        self.assertEqual(self.form.focus(), ("field", key))
        self.key(Action.SELECT_ALL)
        self.key(Action.NAV_BACKSPACE)
        for part in [text] if at_once else list(text):
            self.key(Action.NAV_TEXT, part)

    def paste(self, text):
        """The Paste button beside the link: the clipboard's line in place of the field's."""
        QGuiApplication.clipboard().setText(text)
        self.form.focus_field(pc.URL)
        self.key(Action.NAV_END)
        self.key(Action.NAV_RIGHT)
        self.assertEqual(self.form.focus(), ("paste", pc.URL))
        self.key(Action.NAV_ACTIVATE)

    def press(self, key, zone="button"):
        """The form's button (or alternate, `zone` "alt") `key`, by the keys."""
        form = self.form
        for _ in range(12):
            self.key(Action.NAV_UP)
        for _ in range(12):
            if form.focus()[0] == zone:
                break
            self.key(Action.NAV_DOWN)
        for _ in range(8):
            self.key(Action.NAV_LEFT)
        for _ in range(8):
            if form.focus() == (zone, key):
                break
            self.key(Action.NAV_RIGHT)
        self.assertEqual(form.focus(), (zone, key))
        self.key(Action.NAV_ACTIVATE)

    def answer(self, key):
        """The question's button `key`, by the keys."""
        question = self.question
        self.assertTrue(question.isVisible())
        for _ in range(4):
            self.key(Action.NAV_LEFT)
        for _ in range(4):
            if question.focus() == ("button", key):
                break
            self.key(Action.NAV_RIGHT)
        self.assertEqual(question.focus(), ("button", key))
        self.key(Action.NAV_ACTIVATE)

    def run_check(self, outcome, index=-1):
        """The held check answers `outcome`: what it was asked (provider, ssl_verify, password)."""
        asked = []

        def check(provider, ssl_verify, password=""):
            asked.append((dict(provider), ssl_verify, password))
            return outcome

        with mock.patch.object(provider_check, "check", side_effect=check):
            self.window.jobs.run(index)
        return asked[0]

    def placeholder(self, key):
        """What the form's field `key` shows while it is empty."""
        return next(f.placeholder for f in self.form._fields if f.key == key)

    def has(self, card, key):
        """Whether `card` shows the button `key`."""
        return not card.control_rect("button", key).isEmpty()

    def buttons(self, card):
        return [(b.key, b.text) for b in card._buttons]

    def saved(self, name=None):
        """The provider list as index.json holds it now (read anew), or provider `name`."""
        providers = manager_of(self, self.config).providers
        if name is None:
            return [p["name"] for p in providers]
        return next(p for p in providers if p["name"] == name)

    def note(self, name):
        """Provider `name`'s Check the connection note."""
        layer = next(row for row in self.rows.rows() if row.key == layer_key(name)).section
        return next(row for row in layer.rows if row.key == row_key(name, "check")).note

    # ---- the Add card --------------------------------------------------------------------------

    def test_a_link_fills_the_card_and_finds_its_kind(self):
        self.cards.add()
        self.assertTrue(self.settings.card_shown())
        self.assertEqual(self.form.title(), "Add a provider")
        self.assertEqual(list(self.form.values()), [pc.URL, pc.NAME])  # only what it needs yet
        self.assertEqual(self.form.focus(), ("field", pc.URL))
        self.assertEqual(self.form.found(), ("", True))
        self.assertEqual(
            [b.key for b in self.form._alternates],
            [pc.FILE, "STB", "JELLYFIN", "EMBY", pc.FREE, pc.RADIO],
        )
        # An Xtream link: its login goes to its own fields, out of the link's (not secret).
        self.paste(XTREAM_LINK)
        values = self.form.values()
        self.assertEqual(list(values), [pc.URL, pc.USERNAME, pc.PASSWORD, pc.NAME])
        self.assertEqual(values[pc.URL], "http://other.test:8080")
        self.assertEqual((values[pc.USERNAME], values[pc.PASSWORD]), ("other", "s3cret"))
        self.assertEqual(self.form.found(), ("✓ Xtream at other.test:8080, with its login", True))
        self.assertEqual(self.placeholder(pc.NAME), "other.test")  # the host
        self.form.grab()
        self.assertEqual(self.form.painted_text(pc.PASSWORD), "•" * 6)
        # A portal's link: its MAC from the link, its serial and device ID to fill in.
        self.paste(PORTAL_LINK)
        values = self.form.values()
        self.assertEqual(list(values), [pc.URL, pc.MAC, pc.SERIAL, pc.DEVICE, pc.NAME])
        self.assertEqual(values[pc.URL], "http://portal.test/stalker_portal/c/")
        self.assertEqual(values[pc.MAC], "00:1A:79:00:00:01")
        self.assertEqual(self.form.found(), ("✓ STB portal at portal.test", True))
        # A playlist's.
        self.paste(M3U_LINK)
        self.assertEqual(self.form.values(), {pc.URL: M3U_LINK, pc.NAME: ""})
        self.assertEqual(self.form.found(), ("✓ M3U playlist at cdn.test", True))
        self.assertEqual(self.placeholder(pc.NAME), "cdn.test")
        # A host another provider is named after already: made unique, as its name will be.
        self.paste("http://panel/tv.m3u")
        self.assertEqual(self.placeholder(pc.NAME), "panel (2)")
        # Typed one character at a time, the link stays as typed (the rest of a password would
        # land in the address), while its login fills the fields as it goes.
        typed = "panel.test/get.php?username=me&password=pw"
        self.type_text(pc.URL, typed)
        values = self.form.values()
        self.assertEqual(values[pc.URL], typed)
        self.assertEqual((values[pc.USERNAME], values[pc.PASSWORD]), ("me", "pw"))
        self.assertEqual(self.form.found(), ("✓ Xtream at panel.test, with its login", True))
        # An address that is no link: an Xtream server with a login, a playlist without one;
        # or the kind picked below (picked again: the guess again).
        self.type_text(pc.URL, "http://bare.test:8000", at_once=True)
        self.type_text(pc.USERNAME, "")
        self.type_text(pc.PASSWORD, "")
        self.assertEqual(list(self.form.values()), [pc.URL, pc.USERNAME, pc.PASSWORD, pc.NAME])
        guess = translate(pc.GUESS_TEXT, kind="M3U playlist", host="bare.test:8000")
        self.assertEqual(self.form.found(), (guess, True))
        self.type_text(pc.USERNAME, "u")
        guess = translate(pc.GUESS_TEXT, kind="Xtream", host="bare.test:8000")
        self.assertEqual(self.form.found(), (guess, True))
        self.press("JELLYFIN", zone="alt")
        self.assertEqual(self.form.found(), ("✓ Jellyfin at bare.test:8000", True))
        self.assertEqual(list(self.form.values()), [pc.URL, pc.USERNAME, pc.PASSWORD, pc.NAME])
        self.assertEqual(self.form.focus(), ("field", pc.PASSWORD))  # the first one empty
        self.press("STB", zone="alt")
        self.assertEqual(list(self.form.values()), [pc.URL, pc.MAC, pc.SERIAL, pc.DEVICE, pc.NAME])
        self.assertEqual(self.form.values()[pc.MAC], "00:1A:79:00:00:01")  # the portal link's
        self.assertEqual(self.form.focus(), ("alt", "STB"))  # nothing it needs is empty
        self.press("STB", zone="alt")
        self.assertEqual(self.form.found(), (guess, True))
        self.assertEqual(self.form.values()[pc.USERNAME], "u")  # kept while it was hidden
        # A pasted text that is no address at all is said.
        self.paste("just some words")
        self.assertEqual(self.form.found(), (translate(NO_LINK_TEXT), False))
        # What it lacks is said before anything is asked of the provider.
        self.paste("http://bare.test:8000")
        self.type_text(pc.USERNAME, "u")
        self.press(pc.CHECK)
        self.assertEqual(self.form.found(), (translate(pc.FILL_IN_TEXT, field="Password"), False))
        self.assertEqual(self.form.focus(), ("field", pc.PASSWORD))
        self.assertEqual(self.window.jobs.jobs, [])

    def test_a_pasted_link_logs_nothing_of_it(self):
        # Its login is filled in from it, and nothing of it reaches the log (2.7.1).
        self.cards.add()
        with self.assertNoLogs(level="DEBUG"):
            self.paste(XTREAM_LINK)
            self.type_text(pc.URL, PORTAL_LINK)
        self.assertEqual(self.form.values()[pc.MAC], "00:1A:79:00:00:01")

    def test_check_and_add_adds_selects_and_toasts(self):
        self.cards.add()
        self.paste(XTREAM_LINK)
        self.press(pc.CHECK)
        self.assertTrue(self.form.busy())
        self.assertEqual(self.saved(), ["Lists", "Panel"])  # nothing kept before it answers
        provider, ssl_verify, password = self.run_check(Outcome(True))
        self.assertEqual(
            provider,
            {
                "type": "XTREAM",
                "url": "http://other.test:8080",
                "username": "other",
                "password": "s3cret",
                "name": "other.test",
            },
        )
        self.assertEqual((ssl_verify, password), (True, ""))
        self.assertFalse(self.form.isVisible())
        self.assertFalse(self.settings.card_shown())
        self.assertEqual(self.saved("other.test"), {**provider})
        self.assertEqual(self.config.selected_provider_name, "other.test")
        self.assertIn(("Added other.test to your providers", "info"), self.window.toasts)
        rows = {row.key: row for row in self.drawer._right_rows()}
        self.assertEqual(rows[layer_key("other.test")].sub, "Xtream · in use")
        self.assertEqual(self.note("other.test"), "Provider verified successfully.")
        # A name typed is kept (made unique); an answer over HTTP only is noted on its row.
        self.cards.add()
        self.paste(M3U_LINK)
        self.type_text(pc.NAME, "panel")
        self.press(pc.CHECK)
        self.run_check(Outcome(True, http_only=True))
        self.assertEqual(self.saved()[-1], "panel (2)")
        self.assertEqual(
            self.saved("panel (2)"), {"type": "M3UPLAYLIST", "name": "panel (2)", "url": M3U_LINK}
        )
        self.assertIn(("Added panel (2) to your providers", "info"), self.window.toasts)
        self.assertEqual(
            self.note("panel (2)"),
            f"Provider verified successfully. {translate(HTTP_ONLY_NOTE)}",
        )
        # A list the disk refuses: said, and the card stays with what was typed.
        self.cards.add()
        self.paste(M3U_LINK.replace("news", "sport"))
        self.press(pc.CHECK)
        with (
            mock.patch.object(self.manager, "save_providers", side_effect=OSError("full")),
            self.assertLogs("ui.settings_providers", "WARNING"),
        ):
            self.run_check(Outcome(True))
        self.assertTrue(self.form.isVisible())
        self.assertFalse(self.form.busy())
        self.assertEqual(self.form.values()[pc.URL], M3U_LINK.replace("news", "sport"))
        self.assertEqual(self.window.toasts[-1], (translate(SAVE_FAILED_TEXT), "error"))
        self.assertEqual(self.form.found(), (translate(SAVE_FAILED_TEXT), False))  # on it too
        self.assertEqual(len(self.saved()), 4)

    def test_a_failed_check_offers_add_anyway_but_not_for_media_servers(self):
        self.cards.add()
        self.paste(XTREAM_LINK)
        self.press(pc.CHECK)
        self.run_check(Outcome(False, "The username or password is wrong"))
        self.assertTrue(self.form.isVisible())
        self.assertFalse(self.form.busy())
        failed = "Failed to verify provider: The username or password is wrong."
        self.assertEqual(self.form.found(), (failed, False))
        self.assertTrue(self.has(self.form, pc.ANYWAY))
        self.assertEqual(self.saved(), ["Lists", "Panel"])
        # A change, and the reason and Add anyway go: they were about what was checked.
        self.type_text(pc.PASSWORD, "other")
        self.assertFalse(self.has(self.form, pc.ANYWAY))
        self.assertEqual(self.form.found(), ("✓ Xtream at other.test:8080, with its login", True))
        self.press(pc.CHECK)
        self.run_check(Outcome(False, "The provider took too long to answer"))
        self.press(pc.ANYWAY)
        self.assertFalse(self.form.isVisible())
        self.assertEqual(self.saved("other.test")["password"], "other")
        self.assertIn(("Added other.test to your providers", "info"), self.window.toasts)
        self.assertEqual(self.window.jobs.jobs, [])  # added unchecked
        # Jellyfin and Emby keep only their sign-in's token: no sign-in, nothing to add.
        for kind, name in (("JELLYFIN", "Jellyfin"), ("EMBY", "Emby")):
            self.cards.add()
            self.type_text(pc.URL, "http://media.test:8096", at_once=True)
            self.press(kind, zone="alt")
            self.type_text(pc.USERNAME, "me")
            self.type_text(pc.PASSWORD, "pw")
            self.press(pc.CHECK)
            provider, _ssl, password = self.run_check(
                Outcome(False, "The user name or the password is not right.")
            )
            self.assertEqual(provider["type"], kind)
            self.assertNotIn("password", provider)  # given to the sign-in alone
            self.assertEqual(password, "pw")
            reason = "Failed to verify provider: The user name or the password is not right."
            self.assertEqual(self.form.found(), (reason, False))  # one full stop
            self.assertFalse(self.has(self.form, pc.ANYWAY), name)
            self.key(Action.ESCAPE)
        self.assertEqual(self.saved(), ["Lists", "Panel", "other.test"])

    def test_a_media_server_needs_its_sign_in(self):
        self.cards.add()
        self.type_text(pc.URL, "http://media.test:8096", at_once=True)
        self.press("JELLYFIN", zone="alt")
        self.type_text(pc.USERNAME, "me")
        self.type_text(pc.PASSWORD, "pw")
        self.press(pc.CHECK)
        provider, _ssl, password = self.run_check(Outcome(True, access=dict(SIGNED_IN)))
        self.assertNotIn("password", provider)
        self.assertEqual(password, "pw")
        self.assertEqual(
            self.saved("media.test"),
            {
                "type": "JELLYFIN",
                "name": "media.test",
                "url": "http://media.test:8096",
                "username": "me",
                **SIGNED_IN,
            },
        )
        # Signed in again in Address and login: the new access, never a password, and the
        # lists come again although the address and the user are the same (the token is not
        # what its lists are kept by).
        self.window.events.clear()
        self.cards.edit("media.test")
        self.assertEqual(self.form.title(), "media.test: address and login")
        self.assertEqual(
            self.form.values(),
            {pc.URL: "http://media.test:8096", pc.USERNAME: "me", pc.PASSWORD: ""},
        )
        self.type_text(pc.PASSWORD, "pw2")
        self.press(pc.CHECK)
        self.assertFalse(self.question.isVisible())  # the same server and user: no question
        provider, _ssl, password = self.run_check(
            Outcome(True, access={**SIGNED_IN, "token": "t2"})
        )
        self.assertEqual(provider["device_id"], "d1")  # signs in as the same device
        self.assertNotIn("password", provider)
        self.assertEqual(password, "pw2")
        self.assertEqual(self.saved("media.test")["token"], "t2")
        self.assertNotIn("password", self.saved("media.test"))
        self.assertIn(("set_provider", True), self.window.events)  # the one in use
        # Not the one in use: its kept lists go instead.
        self.config.selected_provider_name = "Lists"
        self.window.events.clear()
        with mock.patch.object(self.manager, "invalidate_provider_cache") as dropped:
            self.cards.edit("media.test")
            self.type_text(pc.PASSWORD, "pw3")
            self.press(pc.CHECK)
            self.run_check(Outcome(True, access={**SIGNED_IN, "token": "t3"}))
        dropped.assert_called_once_with("media.test")
        self.assertNotIn(("set_provider", True), self.window.events)
        # A sign-in refused keeps nothing, and offers no way around it.
        self.cards.edit("media.test")
        self.type_text(pc.USERNAME, "me")
        self.type_text(pc.PASSWORD, "bad")
        self.press(pc.CHECK)
        self.run_check(Outcome(False, "The user name or the password is not right."))
        self.assertTrue(self.form.isVisible())
        self.assertEqual([key for key, _text in self.buttons(self.form)], [CANCEL, pc.CHECK])
        self.assertEqual(self.saved("media.test")["token"], "t3")
        with open(self.manager.index_file, encoding="utf-8") as f:
            self.assertNotIn('"pw', f.read())  # no password typed, in any of the sign-ins

    # ---- the Address-and-login card and the guard ----------------------------------------------

    def test_pasting_another_kind_offers_only_add_as_new(self):
        self.cards.edit("Panel")
        self.assertEqual(self.form.title(), "Panel: address and login")
        self.assertEqual(
            self.form.values(),
            {pc.URL: "http://panel.test:8080", pc.USERNAME: "viewer", pc.PASSWORD: "secret"},
        )
        self.assertEqual(self.form.found(), ("✓ Xtream at panel.test:8080", True))
        self.assertEqual(
            self.buttons(self.form), [(CANCEL, "Cancel"), (pc.CHECK, "Check and save")]
        )
        self.assertEqual(self.form._alternates, [])
        # The 11-19 accident: a stream's link pasted into an Xtream provider.
        self.paste(M3U_LINK)
        self.assertEqual(self.form.values(), {pc.URL: M3U_LINK})
        self.assertEqual(self.form.found(), ("✓ M3U playlist at cdn.test", True))
        self.press(pc.CHECK)
        self.assertEqual(self.window.jobs.jobs, [])  # asked first
        question = self.question
        self.assertTrue(question.isVisible())
        self.assertTrue(self.settings.card_shown())
        self.assertEqual(question.title(), "This is another provider")
        self.assertEqual(
            question.body(),
            translate(
                pc.ANOTHER_KIND_BODY,
                name="Panel",
                old_kind="Xtream",
                old_host="panel.test:8080",
                kind="M3U playlist",
                host="cdn.test",
            ),
        )
        self.assertEqual(self.buttons(question), [(CANCEL, "Cancel"), (pc.ADD_NEW, "Add as new")])
        self.assertEqual(question.focus(), ("button", pc.ADD_NEW))
        stacked = [w for w in self.host.children() if w in (self.form, question)]
        self.assertEqual(stacked, [self.form, question])  # the question over the form
        # Cancel: back on the form, nothing changed.
        self.answer(CANCEL)
        self.assertFalse(question.isVisible())
        self.assertTrue(self.form.isVisible())
        self.assertEqual(self.form.values(), {pc.URL: M3U_LINK})
        self.assertEqual(self.saved("Panel"), PANEL)
        # Add as new: the Add card's way (checked, then added); Panel stays as it was.
        self.press(pc.CHECK)
        self.answer(pc.ADD_NEW)
        self.assertEqual(self.form.title(), "Add a provider")
        self.assertTrue(self.form.busy())
        provider, _ssl, _password = self.run_check(Outcome(True))
        self.assertEqual(provider, {"type": "M3UPLAYLIST", "url": M3U_LINK, "name": "cdn.test"})
        self.assertEqual(self.saved(), ["Lists", "Panel", "cdn.test"])
        self.assertEqual(self.saved("Panel"), PANEL)
        self.assertIn(("Added cdn.test to your providers", "info"), self.window.toasts)
        # Added as new, a failed check offers Add anyway as the Add card does.
        self.cards.edit("Panel")
        self.paste(M3U_LINK.replace("news", "sport"))
        self.press(pc.CHECK)
        self.answer(pc.ADD_NEW)
        self.run_check(Outcome(False))
        self.assertTrue(self.has(self.form, pc.ANYWAY))
        self.assertEqual(self.form.values()[pc.NAME], "")

    def test_another_host_offers_replace_and_add_as_new_defaults(self):
        self.config.selected_provider_name = "Panel"
        self.cards.edit("Panel")
        self.type_text(pc.URL, "http://moved.test:8080", at_once=True)
        self.assertEqual(self.form.found(), ("✓ Xtream at moved.test:8080", True))  # its kind
        self.press(pc.CHECK)
        question = self.question
        self.assertEqual(question.title(), "This is another server")
        self.assertEqual(
            question.body(),
            translate(
                pc.ANOTHER_HOST_BODY,
                name="Panel",
                old_host="panel.test:8080",
                host="moved.test:8080",
            ),
        )
        self.assertEqual(
            self.buttons(question),
            [
                (CANCEL, "Cancel"),
                (pc.REPLACE, "Replace Panel's address"),
                (pc.ADD_NEW, "Add as new"),
            ],
        )
        self.assertEqual(question.focus(), ("button", pc.ADD_NEW))  # the default
        self.answer(pc.REPLACE)
        self.assertTrue(self.form.busy())
        provider, _ssl, _password = self.run_check(Outcome(True))
        self.assertEqual(provider, {**PANEL, "url": "http://moved.test:8080"})
        self.assertFalse(self.form.isVisible())
        self.assertEqual(self.saved(), ["Lists", "Panel"])
        self.assertEqual(self.saved("Panel"), {**PANEL, "url": "http://moved.test:8080"})
        self.assertIn(("set_provider", True), self.window.events)  # its lists come again
        rows = {row.key: row for row in self.rows.rows()}
        login = next(r for r in rows[layer_key("Panel")].section.rows if r.key.endswith("/login"))
        self.assertEqual(login.note, "moved.test:8080")
        # Another user on the same server: another account.
        self.cards.edit("Panel")
        self.type_text(pc.USERNAME, "someone")
        self.press(pc.CHECK)
        self.assertEqual(question.title(), "This is another account")
        self.assertEqual(
            question.body(),
            translate(
                pc.ANOTHER_ACCOUNT_BODY,
                name="Panel",
                old_user="viewer",
                host="moved.test:8080",
                user="someone",
            ),
        )
        self.assertEqual(
            self.buttons(question)[1:],
            [(pc.REPLACE, "Replace Panel's login"), (pc.ADD_NEW, "Add as new")],
        )
        self.assertEqual(question.focus(), ("button", pc.ADD_NEW))
        self.answer(pc.REPLACE)
        self.run_check(Outcome(True))
        self.assertEqual(self.saved("Panel")["username"], "someone")
        # A failed check after Replace keeps the provider as it was.
        self.cards.edit("Panel")
        self.type_text(pc.URL, "http://again.test", at_once=True)
        self.press(pc.CHECK)
        self.answer(pc.REPLACE)
        self.run_check(Outcome(False, "Couldn't reach the provider"))
        self.assertEqual(self.saved("Panel")["url"], "http://moved.test:8080")
        self.assertFalse(self.has(self.form, pc.ANYWAY))

    def test_a_port_fixed_saves_without_asking(self):
        self.cards.edit("Panel")
        self.type_text(pc.URL, "http://panel.test:8081", at_once=True)
        self.press(pc.CHECK)
        self.assertFalse(self.question.isVisible())  # the same server and account
        self.run_check(Outcome(True))
        self.assertEqual(self.saved("Panel"), {**PANEL, "url": "http://panel.test:8081"})

    def test_a_pasted_file_link_leaves_an_xtream_address_its_server(self):
        # The same host's film or page (no provider link): its path is no panel's, so the
        # provider keeps its server (the panel answered at its root all along); a panel's own
        # path stays, an API file named at its end goes.
        for typed, kept in (
            ("http://panel.test:8080/media/film.mp4", "http://panel.test:8080"),
            ("http://panel.test:8080/xc/player_api.php", "http://panel.test:8080/xc"),
            ("http://panel.test:8080/xc", "http://panel.test:8080/xc"),
        ):
            with self.subTest(typed=typed):
                self.cards.edit("Panel")
                self.type_text(pc.URL, typed, at_once=True)
                self.press(pc.CHECK)
                self.assertFalse(self.question.isVisible())  # the same server and account
                provider, _ssl, _password = self.run_check(Outcome(True))
                self.assertEqual(provider["url"], kept)  # what was checked
                self.assertEqual(self.saved("Panel"), {**PANEL, "url": kept})

    def test_a_new_password_saves_without_asking(self):
        self.cards.edit("Panel")
        self.type_text(pc.PASSWORD, "newpass")
        self.press(pc.CHECK)
        self.assertFalse(self.question.isVisible())
        self.assertTrue(self.form.busy())
        provider, _ssl, _password = self.run_check(Outcome(True))
        self.assertEqual(provider, {**PANEL, "password": "newpass"})
        self.assertFalse(self.form.isVisible())
        self.assertEqual(self.saved("Panel"), {**PANEL, "password": "newpass"})
        self.assertEqual(self.note("Panel"), "Provider verified successfully.")
        # Its own link again (the same server and user, a new password in it): no question.
        self.cards.edit("Panel")
        self.paste("http://panel.test:8080/player_api.php?username=viewer&password=third")
        self.assertEqual(self.form.values()[pc.URL], "http://panel.test:8080")
        self.press(pc.CHECK)
        self.assertFalse(self.question.isVisible())
        self.run_check(Outcome(True))
        self.assertEqual(self.saved("Panel")["password"], "third")
        # A failed check keeps the provider as it was (nothing changes before it answers),
        # and offers only another try.
        self.cards.edit("Panel")
        self.type_text(pc.PASSWORD, "wrong")
        self.press(pc.CHECK)
        self.run_check(Outcome(False, "The username or password is wrong"))
        self.assertEqual(self.saved("Panel")["password"], "third")
        self.assertTrue(self.form.isVisible())
        self.assertEqual([key for key, _text in self.buttons(self.form)], [CANCEL, pc.CHECK])

    def test_a_playlists_own_address_stays_a_playlist(self):
        # An "M3U Stream" from an older QiTV, and a playlist from a panel's get.php (its login
        # in its address): neither turns into another kind by being checked or edited.
        stream = {"type": "M3USTREAM", "name": "Stream", "url": "http://s.test/live.m3u8"}
        panel_list = {
            "type": "M3UPLAYLIST",
            "name": "Panel list",
            "url": "http://p.test/get.php?username=a&password=b&type=m3u_plus",
        }
        self.rows.actions.changes.add(dict(stream))
        self.rows.actions.changes.add(dict(panel_list))
        self.cards.edit("Stream")
        self.assertEqual(self.form.found(), ("✓ M3U Stream at s.test", True))
        self.assertEqual(self.form._alternates[0].key, pc.FILE)
        self.press(pc.CHECK)
        self.assertFalse(self.question.isVisible())
        self.run_check(Outcome(True))
        self.assertEqual(self.saved("Stream"), stream)
        self.cards.edit("Stream")
        self.paste("http://s.test/other.m3u8")
        self.assertEqual(self.form.found(), ("✓ M3U Stream at s.test", True))
        self.press(pc.CHECK)
        # Its whole address is what a playlist is, but it is no other server: said so.
        self.assertEqual(self.question.title(), "This is another address")
        self.assertEqual(
            self.question.body(), translate(pc.SAME_HOST_BODY, name="Stream", host="s.test")
        )
        self.assertNotIn("other.m3u8", self.question.body())  # never a path
        self.answer(pc.REPLACE)
        self.run_check(Outcome(True))
        self.assertEqual(self.saved("Stream"), {**stream, "url": "http://s.test/other.m3u8"})
        self.cards.edit("Panel list")
        self.assertEqual(self.form.values(), {pc.URL: panel_list["url"]})
        self.press(pc.CHECK)
        self.assertFalse(self.question.isVisible())
        self.run_check(Outcome(True))
        self.cards.edit("Panel list")
        self.form.focus_field(pc.URL)
        self.key(Action.NAV_END)
        for character in "&output=ts":  # typed at its end
            self.key(Action.NAV_TEXT, character)
        self.assertEqual(self.form.found(), ("✓ M3U playlist at p.test", True))
        self.press(pc.CHECK)
        self.assertEqual(self.question.title(), "This is another address")
        body = translate(pc.SAME_HOST_BODY, name="Panel list", host="p.test")
        self.assertEqual(self.question.body(), body)  # never its query: the login is in it
        self.assertTrue(self.has(self.question, pc.REPLACE))
        self.answer(CANCEL)
        self.key(Action.ESCAPE)
        # Playlist files: another one of the same name, or of another name.
        folder = self.config.get_config_dir()
        files = {
            "type": "M3UPLAYLIST",
            "name": "Files",
            "url": os.path.join(folder, "a", "tv.m3u"),
        }
        self.rows.actions.changes.add(dict(files))
        for path, expected in (
            (
                os.path.join(folder, "b", "tv.m3u"),
                translate(pc.SAME_FILE_BODY, name="Files", file="tv.m3u"),
            ),
            (
                os.path.join(folder, "a", "news.m3u"),
                translate(pc.FILE_BODY, name="Files", old_where="tv.m3u", where="news.m3u"),
            ),
            (
                "http://cdn.test/tv.m3u",
                translate(pc.FILE_BODY, name="Files", old_where="tv.m3u", where="cdn.test"),
            ),
        ):
            self.cards.edit("Files")
            self.type_text(pc.URL, path, at_once=True)
            self.press(pc.CHECK)
            self.assertEqual(self.question.title(), "This is another address", path)
            self.assertEqual(self.question.body(), expected)
            self.assertNotIn(folder, self.question.body())
            self.answer(CANCEL)
            self.key(Action.ESCAPE)

    def test_a_late_check_answer_after_cancel_is_dropped(self):
        self.cards.add()
        self.paste(XTREAM_LINK)
        self.press(pc.CHECK)
        self.assertTrue(self.form.busy())
        self.key(Action.ESCAPE)  # Cancel works while it checks
        self.assertFalse(self.form.isVisible())
        self.assertFalse(self.settings.card_shown())
        self.run_check(Outcome(True))
        self.assertEqual(self.saved(), ["Lists", "Panel"])
        self.assertEqual(self.window.toasts, [])
        # Opened again, nothing typed comes back (the password least of all).
        self.cards.add()
        self.assertEqual(self.form.values(), {pc.URL: "", pc.NAME: ""})
        # Settings closing while it checks: the same.
        self.paste(XTREAM_LINK)
        self.press(pc.CHECK)
        self.settings.close()
        self.assertFalse(self.form.isVisible())
        self.run_check(Outcome(True))
        self.assertEqual(self.saved(), ["Lists", "Panel"])
        self.settings.open(PROVIDERS)
        self.cards.add()
        self.assertEqual(self.form.values(), {pc.URL: "", pc.NAME: ""})
        self.key(Action.ESCAPE)
        # A password typed in Address and login and cancelled: the kept one shows again.
        self.cards.edit("Panel")
        self.type_text(pc.PASSWORD, "typed")
        self.key(Action.ESCAPE)
        self.cards.edit("Panel")
        self.assertEqual(self.form.values()[pc.PASSWORD], "secret")
        self.key(Action.ESCAPE)
        # Cancelled, then checked anew: the first answer never lands on the second card.
        self.cards.add()
        self.paste(XTREAM_LINK)
        self.press(pc.CHECK)
        self.key(Action.ESCAPE)
        self.cards.add()
        self.paste(M3U_LINK)
        self.press(pc.CHECK)
        self.assertEqual(len(self.window.jobs.jobs), 2)
        self.run_check(Outcome(True), index=0)
        self.assertTrue(self.form.busy())
        self.assertEqual(self.saved(), ["Lists", "Panel"])
        self.run_check(Outcome(True))
        self.assertEqual(self.saved(), ["Lists", "Panel", "cdn.test"])

    def test_m3u_file_uses_the_system_picker(self):
        path = os.path.join(self.config.get_config_dir(), "Sports.m3u")
        with open(path, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
        self.cards.add()
        with mock.patch.object(QFileDialog, "getOpenFileName", return_value=(path, "")) as picker:
            self.press(pc.FILE, zone="alt")
        picker.assert_called_once()
        self.assertEqual(self.form.values(), {pc.URL: path, pc.NAME: ""})
        self.assertEqual(self.form.found(), ("✓ M3U playlist file “Sports.m3u”", True))
        self.assertEqual(self.placeholder(pc.NAME), "Sports")
        self.press(pc.CHECK)
        self.assertTrue(self.form.busy())
        self.window.jobs.run()  # the real check: the file is looked for on the worker
        self.assertFalse(self.form.isVisible())
        self.assertEqual(
            self.saved("Sports"), {"type": "M3UPLAYLIST", "name": "Sports", "url": path}
        )
        # The card gone while the picker waited (Settings closed under it): nothing shows.
        self.cards.add()

        def picked_after_settings_closed(*_args):
            self.settings.close()
            return path, ""

        with mock.patch.object(
            QFileDialog, "getOpenFileName", side_effect=picked_after_settings_closed
        ):
            self.press(pc.FILE, zone="alt")
        self.assertFalse(self.form.isVisible())
        self.assertFalse(self.settings.card_shown())
        self.settings.open(PROVIDERS)
        # The picker cancelled: the address stays.
        self.cards.add()
        self.type_text(pc.URL, M3U_LINK, at_once=True)
        with mock.patch.object(QFileDialog, "getOpenFileName", return_value=("", "")):
            self.press(pc.FILE, zone="alt")
        self.assertEqual(self.form.values()[pc.URL], M3U_LINK)
        # A file that is not there: not added, and no Add anyway (only a file QiTV finds).
        self.type_text(pc.URL, path + ".gone", at_once=True)
        self.press(pc.CHECK)
        self.window.jobs.run()
        self.assertEqual(self.form.found(), ("Failed to verify provider.", False))
        self.assertFalse(self.has(self.form, pc.ANYWAY))
        self.assertEqual(self.saved(), ["Lists", "Panel", "Sports"])

    def test_free_channels_closes_the_card_and_opens_its_countries(self):
        # 2.13.1: Settings' Free channels layer, as the card opened Settings (here: it
        # showed already), never a menu.
        self.cards.add()
        self.paste(XTREAM_LINK)
        self.press(pc.FREE, zone="alt")
        self.assertFalse(self.form.isVisible())
        self.assertFalse(self.settings.card_shown())
        self.assertEqual((self.window.free, self.window.radio), ([False], []))
        self.cards.add()
        self.assertEqual(self.form.values(), {pc.URL: "", pc.NAME: ""})

    def test_free_radio_closes_the_card_and_opens_its_countries(self):
        self.cards.add(opened_settings=True)  # Settings opened for the card
        free = self.form.control_rect("alt", pc.FREE)
        radio = self.form.control_rect("alt", pc.RADIO)
        # After Free channels…: beside it, or on the next line.
        self.assertGreater((radio.top(), radio.left()), (free.top(), free.left()))
        self.press(pc.RADIO, zone="alt")
        self.assertFalse(self.form.isVisible())
        self.assertEqual((self.window.free, self.window.radio), ([], [True]))

    def test_the_cards_stay_under_a_question_of_the_window(self):
        # A question keeps the keys above Settings (stop a recording?): a card shown or
        # changed meanwhile stays under it.
        self.window.question_card = QuestionCard(self.host)
        self.window.question_card.ask("Stop the recording?", "Stop", "Keep")
        self.cards.add()
        self.paste(M3U_LINK)
        stacked = [w for w in self.host.children() if w in (self.form, self.window.question_card)]
        self.assertEqual(stacked, [self.form, self.window.question_card])

    def test_a_provider_gone_meanwhile_closes_its_card(self):
        self.cards.edit("Panel")
        self.type_text(pc.PASSWORD, "newpass")
        self.press(pc.CHECK)
        self.rows.actions.remove("Panel")  # elsewhere (the old window)
        self.run_check(Outcome(True))
        self.assertFalse(self.form.isVisible())
        self.assertEqual(self.saved(), ["Lists"])


class CardsWindowTests(WindowCase):
    """The cards in the TV window: Settings › Providers' rows open them."""

    def tearDown(self):
        elapse(QApplication.doubleClickInterval() + 80)
        super().tearDown()

    def to(self, key):
        """The keys onto the right column's row `key`."""
        drawer = self.window.settings_drawer
        for _ in range(12):
            self.window.controller.handle(Action.NAV_UP)
        for _ in range(12):
            row = drawer.focused_row()
            if row is not None and row.key == key:
                return
            self.window.controller.handle(Action.NAV_DOWN)
        self.fail(f"no row {key}")

    def test_add_provider_and_address_and_login_open_the_cards(self):
        self.show()
        self.wait_for_drawer()
        self.menu_actions()["Settings…"].trigger()
        controller = self.window.controller
        controller.handle(Action.NAV_RIGHT)
        self.to(ADD_PROVIDER)
        controller.handle(Action.NAV_ACTIVATE)
        form = self.window.provider_cards.form
        self.assertTrue(wait_until(form.isVisible))
        self.assertEqual(form.title(), "Add a provider")
        controller.handle(Action.ESCAPE)
        self.assertFalse(form.isVisible())
        self.assertTrue(self.window.settings.visible())
        self.to(layer_key(PROVIDER_NAME))
        controller.handle(Action.NAV_ACTIVATE)
        self.to(row_key(PROVIDER_NAME, "login"))
        controller.handle(Action.NAV_ACTIVATE)
        self.assertTrue(form.isVisible())
        self.assertEqual(form.title(), f"{PROVIDER_NAME}: address and login")


if __name__ == "__main__":
    unittest.main()
