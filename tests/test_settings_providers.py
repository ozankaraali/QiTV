"""Settings › Providers in the drawer (plan 2): the list of providers, each one's own rows
(its name edited in place, its kind, its address and login card, the per-provider settings,
its hidden groups, the check and the removal), driven by the keys over a real drawer, with a
fake TV window over a real provider list. Fictional hosts."""

import os
from types import SimpleNamespace
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from timer_fixtures import elapse, wait_until

from services import i18n, provider_check
from services.epg_view import GUIDE_OFFSET_KEY
from services.hidden_groups import HiddenGroups
from services.i18n import translate
from services.provider_check import Outcome
from services.settings_model import (
    ACTION,
    ADD_PROVIDER,
    DANGER,
    FREE_CHANNELS,
    INFO,
    OPENS,
    PROVIDER_PREFIX,
    PROVIDERS,
    STEP,
    TEXT,
    sections,
)
from tests.fixtures.local_provider import PROVIDER_NAME
from tests.test_provider_changes import PANEL, PLAYLIST, manager_of, temp_manager
from tests.test_tv_window import WindowCase
from ui import parental_settings
from ui.keymap import Action
from ui.overlays.settings_card import BUSY_TEXT, CANCEL
from ui.overlays.settings_drawer import SettingsDrawer
from ui.overlays.settings_layout import Item, title_of, whole
from ui.settings_controller import SettingsController
from ui.settings_dialog import (
    HTTP_ONLY_NOTE,
    NAME_TAKEN_TEXT,
    REMOVE_QUESTION,
    REMOVE_RECORDINGS_NOTE,
)
from ui.settings_providers import (
    REMOVE as REMOVE_KEY,
    SAVE_FAILED_TEXT,
    ProviderRows,
    kind_text,
    layer_key,
    row_key,
)

PIN = "2468"


class HeldRunner:
    """A runner that holds each job until the test runs it."""

    def __init__(self):
        self.jobs = []

    def run_worker(self, worker, on_finished, on_error=None, context=None):
        self.jobs.append((worker, on_finished, context))
        return worker

    def run(self, index=-1):
        worker, on_finished, context = self.jobs.pop(index)
        worker.finished.connect(lambda payload: on_finished(context, payload))
        worker.run()


class FakeGuard:
    """Parental controls: locked or not, and the PIN that opens them."""

    def __init__(self):
        self.is_locked = False

    def locked(self):
        return self.is_locked

    def check(self, pin):
        return pin == PIN


class FakeValues:
    """The config's rows (none of these tests' rows reach it)."""

    def __init__(self):
        self.sets = []

    def get(self, key):
        raise KeyError(key)

    def set(self, key, value):
        self.sets.append((key, value))


class FakeWindow:
    """What the Providers rows and ProviderActions ask of the TV window. Its providers_saved
    stands for the catalogs' reload, whose providersChanged reaches the rows at once, inside
    the change (as the real signal does)."""

    def __init__(self, config, manager):
        self.config_manager = config
        self.provider_manager = manager
        self.events = []
        self.toasts = []
        self.free = []
        self.scheduled = set()
        self.rows = None  # the ProviderRows, once built
        self.catalogs = SimpleNamespace(created=lambda: [], catalog=None)
        self.menus = SimpleNamespace(rebuild_providers=lambda: None)
        self.hidden_groups = HiddenGroups(config)
        self.controller = SimpleNamespace(lists_changed=mock.Mock())
        self.recording_schedule = SimpleNamespace(providers=lambda: set(self.scheduled))
        self.parental = FakeGuard()
        self.screens = SimpleNamespace(notify=self.notify)
        self.jobs = HeldRunner()

    def notify(self, text, kind="info", ms=0, **_button):
        self.toasts.append((text, kind))

    def show_free_channels(self, global_pos):
        self.free.append(global_pos)

    def providers_saved(self):
        self.events.append("providers_saved")
        if self.rows is not None:
            self.rows.providers_changed()

    def providers_renamed(self, renames, removed=()):
        self.events.append(("providers_renamed", dict(renames.names), list(removed)))

    def set_provider(self, force_update=False):
        self.events.append(("set_provider", force_update))

    def _show_provider(self, name):
        self.config_manager.selected_provider_name = name

    def save_config(self):
        self.events.append("save_config")


class ProvidersTests(unittest.TestCase):
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
        self.cards = mock.Mock()
        self.settings = SettingsController(
            self.drawer,
            FakeValues(),
            lambda: sections("Darwin", self.rows.rows()),
            lambda tab: None,
            lambda: None,
        )
        self.rows = ProviderRows(self.window, self.settings, self.drawer, cards=self.cards)
        self.window.rows = self.rows

    def tearDown(self):
        elapse(QApplication.doubleClickInterval() + 80)  # no second-click guard left armed

    # ---- helpers ------------------------------------------------------------------------------

    def key(self, action, payload=""):
        self.assertTrue(self.settings.handle(action, payload), action)

    def shown(self):
        """The right column's rows."""
        return self.drawer._right_rows()

    def move_to(self, key):
        """The keys onto the right column's row `key` (↓ from where they are)."""
        for _ in range(len(self.shown()) + 1):
            row = self.drawer.focused_row()
            if row is not None and row.key == key:
                return row
            self.key(Action.NAV_DOWN)
        self.fail(f"no row {key}")

    def into(self, name):
        """Provider `name`'s own rows, the keys on the first."""
        self.settings.open(PROVIDERS)
        self.key(Action.NAV_RIGHT)
        self.move_to(layer_key(name))
        self.key(Action.NAV_ACTIVATE)
        self.assertEqual(self.drawer.path(), (PROVIDERS, layer_key(name)))

    def on(self, name, field):
        """Provider `name`'s row `field`, focused."""
        self.into(name)
        return self.move_to(row_key(name, field))

    def value(self, name, field):
        return self.settings.value_text(self.row(name, field))

    def row(self, name, field):
        rows = {row.key: row for row in self.shown()}
        return rows[row_key(name, field)]

    def saved(self, name=None):
        """The provider list as index.json holds it now (read anew), or provider `name`."""
        providers = manager_of(self, self.config).providers
        if name is None:
            return [p["name"] for p in providers]
        return next(p for p in providers if p["name"] == name)

    def type_name(self, text, *, replace=False):
        """Edit the focused Name row: select all first when `replace`, type `text`, Enter."""
        self.key(Action.NAV_ACTIVATE)
        self.assertTrue(self.drawer.editing())
        if replace:
            self.key(Action.SELECT_ALL)
        self.key(Action.NAV_TEXT, text)
        self.key(Action.NAV_ACTIVATE)
        self.assertFalse(self.drawer.editing())

    # ---- the list ----------------------------------------------------------------------------

    def test_providers_list_with_in_use_and_add_rows(self):
        self.settings.open(PROVIDERS)
        rows = self.shown()
        self.assertEqual(
            [row.key for row in rows],
            [layer_key("Lists"), layer_key("Panel"), ADD_PROVIDER, FREE_CHANNELS],
        )
        self.assertEqual(
            [title_of(Item(row.key, row.text, row=row)) for row in rows],
            ["Lists", "Panel", "＋ Add provider", "Free channels"],
        )
        self.assertEqual([row.kind for row in rows], [OPENS, OPENS, ACTION, OPENS])
        self.assertEqual([row.sub for row in rows[:2]], ["M3U playlist · in use", "Xtream"])
        self.assertEqual(
            [kind_text(kind) for kind in ("STB", "JELLYFIN", "EMBY", "M3USTREAM", "ODD")],
            ["STB portal", "Jellyfin", "Emby", "M3U Stream", "ODD"],
        )
        # The TV moved to another one (the menu bar, a provider ready): its row says so.
        self.config.selected_provider_name = "Panel"
        self.rows.providers_changed()
        self.assertEqual([row.sub for row in self.shown()[:2]], ["M3U playlist", "Xtream · in use"])
        # ＋ Add provider is the cards' (Tasks 8 and 9).
        self.key(Action.NAV_RIGHT)
        self.move_to(ADD_PROVIDER)
        self.key(Action.NAV_ACTIVATE)
        self.cards.add.assert_called_once_with()

    def test_a_providers_rows_in_their_order(self):
        self.into("Panel")
        self.assertEqual(
            [(row.key, row.kind) for row in self.shown()],
            [
                (row_key("Panel", "name"), TEXT),
                (row_key("Panel", "kind"), INFO),
                (row_key("Panel", "login"), OPENS),
                (row_key("Panel", "ssl"), STEP),
                (row_key("Panel", "streams"), STEP),
                (row_key("Panel", "offset"), STEP),
                (row_key("Panel", "hidden"), OPENS),
                (row_key("Panel", "check"), ACTION),
                (row_key("Panel", "remove"), DANGER),
            ],
        )
        self.assertEqual(self.value("Panel", "name"), "Panel")
        self.assertEqual(self.value("Panel", "kind"), "Xtream")
        self.assertEqual(self.row("Panel", "login").note, "panel.test:8080")  # no login in it
        for row in self.shown():
            if row.kind != INFO:
                self.assertTrue(row.help, row.key)
                self.assertLessEqual(len(row.help), 90, row.key)

    def test_provider_names_are_never_translated(self):
        # A provider is called what its viewer called it: "General" and "Movies" are QiTV
        # texts too (a section, a group's kind), but a name stays as typed, as does a hidden
        # group's title.
        self.manager.providers[1]["name"] = "General"
        self.window.hidden_groups.hide("live", "General", "7", "Movies")
        self.assertEqual(i18n.set_language("tr"), "tr")
        self.addCleanup(i18n.set_language, "en")
        self.assertNotEqual(translate("General"), "General")
        self.into("General")
        crumbs = [text for text, _rect in self.drawer._layout().crumbs]
        self.assertEqual(crumbs, [translate("Settings"), translate("Providers"), "General"])
        left = self.drawer._items("left")
        self.assertEqual([title_of(item) for item in left][:2], ["Lists", "General"])
        self.assertEqual(self.value("General", "name"), "General")
        self.move_to(row_key("General", "hidden"))
        self.key(Action.NAV_ACTIVATE)
        self.assertEqual([title_of(Item(r.key, r.text, row=r)) for r in self.shown()], ["Movies"])

    def test_free_channels_opens_the_menu_at_the_row(self):
        self.settings.open(PROVIDERS)
        self.key(Action.NAV_RIGHT)
        self.move_to(FREE_CHANNELS)
        self.key(Action.NAV_ACTIVATE)
        index = [row.key for row in self.shown()].index(FREE_CHANNELS)
        line = self.drawer.row_rect("right", index)
        self.assertEqual(len(self.window.free), 1)
        point = self.drawer.mapFromGlobal(self.window.free[0])
        self.assertTrue(line.adjusted(0, 0, 0, 1).contains(point), (line, point))
        self.assertEqual(self.drawer.row_anchor(FREE_CHANNELS), self.window.free[0])
        self.assertTrue(self.settings.visible())  # the menu pops up over Settings

    def test_address_and_login_asks_for_its_card(self):
        self.on("Panel", "login")
        self.key(Action.NAV_ACTIVATE)
        self.cards.edit.assert_called_once_with("Panel")

    # ---- the settings of a provider ----------------------------------------------------------

    def test_streams_at_once_steps_and_saves_only_max_streams(self):
        row = self.on("Panel", "streams")
        self.assertEqual(
            [choice.shown() for choice in row.choices],
            ["Automatic"] + [str(n) for n in range(1, 11)],
        )
        self.assertEqual(self.value("Panel", "streams"), "Automatic")
        self.key(Action.NAV_RIGHT)
        self.assertEqual(self.saved("Panel"), {**PANEL, "max_streams": 1})
        self.assertEqual(self.value("Panel", "streams"), "1")
        self.assertTrue(self.settings.lit(self.row("Panel", "streams")))
        self.key(Action.NAV_LEFT)  # Automatic again: a key that was there keeps its 0
        self.assertEqual(self.saved("Panel"), {**PANEL, "max_streams": 0})
        self.key(Action.NAV_LEFT)  # round to the most
        self.assertEqual(self.saved("Panel")["max_streams"], 10)
        # A hand-edited value past the most shows as the most, and steps down from it.
        self.manager.providers[1] = {**self.manager.providers[1], "max_streams": 50}
        self.assertEqual(self.value("Panel", "streams"), "10")
        self.key(Action.NAV_LEFT)
        self.assertEqual(self.saved("Panel")["max_streams"], 9)

    def test_verify_certificates_follows_the_global_setting(self):
        row = self.on("Panel", "ssl")
        self.assertEqual(self.value("Panel", "ssl"), "On")
        self.assertIs(row.default, True)
        self.key(Action.NAV_RIGHT)
        self.assertEqual(self.saved("Panel"), {**PANEL, "ssl_verify": False})
        self.assertEqual(self.value("Panel", "ssl"), "Off")
        self.key(Action.NAV_RIGHT)
        self.assertEqual(self.saved("Panel"), {**PANEL, "ssl_verify": True})  # it was there
        self.config.ssl_verify = False  # the global setting off: the default follows it
        self.rows.providers_changed()
        self.assertIs(self.row("Panel", "ssl").default, False)
        self.assertEqual(self.saved("Lists"), PLAYLIST)

    def test_offset_steps_and_zero_pops_the_key(self):
        row = self.on("Panel", "offset")
        self.assertEqual(len(row.choices), 49)  # −12 h … +12 h in 30-minute steps
        self.assertEqual(self.value("Panel", "offset"), "No offset")
        self.key(Action.NAV_RIGHT)
        self.assertEqual(self.saved("Panel"), {**PANEL, GUIDE_OFFSET_KEY: 30})
        self.assertEqual(self.value("Panel", "offset"), "+30 min")
        self.key(Action.NAV_LEFT)
        self.assertEqual(self.saved("Panel"), PANEL)  # no offset: no key
        self.key(Action.NAV_LEFT)
        self.assertEqual(self.saved("Panel")[GUIDE_OFFSET_KEY], -30)
        self.assertEqual(self.value("Panel", "offset"), "−30 min")
        # An offset the setting cannot show (hand-edited) reads as none.
        self.manager.providers[1] = {**self.manager.providers[1], GUIDE_OFFSET_KEY: 45}
        self.assertEqual(self.value("Panel", "offset"), "No offset")

    def failing_save(self):
        """The provider list's writes refused (a full disk, a read-only folder)."""
        return mock.patch.object(self.manager, "save_providers", side_effect=PermissionError())

    def test_a_failed_save_says_so_and_does_not_crash(self):
        # Nothing was changed: the list stays what the disk holds, and the toast says so.
        self.on("Panel", "streams")
        with self.failing_save(), self.assertLogs("ui.settings_providers", "WARNING"):
            self.key(Action.NAV_RIGHT)
        self.assertEqual(self.window.toasts, [(translate(SAVE_FAILED_TEXT), "error")])
        self.assertEqual(self.value("Panel", "streams"), "Automatic")
        self.assertEqual(self.manager.providers, [PLAYLIST, PANEL])
        self.assertEqual(self.window.events, [])  # no follow-ups
        self.assertTrue(self.settings.visible())

    def test_a_rename_or_removal_that_cannot_be_saved_changes_nothing(self):
        self.on("Panel", "name")
        with self.failing_save(), self.assertLogs("ui.settings_providers", "WARNING"):
            self.type_name("Films", replace=True)
        self.assertEqual(self.rows._actions.changes.names(), ["Lists", "Panel"])
        self.assertEqual(self.drawer.path(), (PROVIDERS, layer_key("Panel")))  # its old name
        self.assertEqual(self.drawer.focused_row().key, row_key("Panel", "name"))
        self.assertEqual(self.value("Panel", "name"), "Panel")
        self.on("Lists", "remove")
        self.key(Action.NAV_ACTIVATE)
        self.key(Action.NAV_RIGHT)
        with self.failing_save(), self.assertLogs("ui.settings_providers", "WARNING"):
            self.key(Action.NAV_ACTIVATE)
        self.assertEqual(self.rows._actions.changes.names(), ["Lists", "Panel"])
        self.assertEqual(self.config.selected_provider_name, "Lists")  # never handed over
        self.assertEqual(self.drawer.path(), (PROVIDERS, layer_key("Lists")))
        self.assertEqual(self.window.events, [])  # no follow-ups: nothing carried, reloaded
        self.assertEqual(self.window.toasts, [(translate(SAVE_FAILED_TEXT), "error")] * 2)
        self.assertEqual(self.saved(), ["Lists", "Panel"])

    # ---- the name ----------------------------------------------------------------------------

    def test_rename_in_place_and_a_taken_name_is_refused(self):
        self.on("Panel", "name")
        self.type_name(" TV")
        self.assertEqual(self.saved(), ["Lists", "Panel TV"])
        # The viewer stays in the renamed provider's layer, on its Name row.
        self.assertEqual(self.drawer.path(), (PROVIDERS, layer_key("Panel TV")))
        self.assertEqual(self.drawer.focused_row().key, row_key("Panel TV", "name"))
        self.assertEqual(self.drawer.focus_side(), "right")
        self.assertIn(("providers_renamed", {"Panel": "Panel TV"}, []), self.window.events)
        # Another provider's name, in any case: said, and the name stays.
        self.type_name("LISTS", replace=True)
        self.assertEqual(self.saved(), ["Lists", "Panel TV"])
        self.assertEqual(self.window.toasts, [(translate(NAME_TAKEN_TEXT, name="LISTS"), "error")])
        self.assertEqual(self.value("Panel TV", "name"), "Panel TV")
        self.assertEqual(self.drawer.path(), (PROVIDERS, layer_key("Panel TV")))
        # Only Enter or a click elsewhere renames: typing alone writes nothing.
        self.key(Action.NAV_ACTIVATE)
        self.key(Action.NAV_TEXT, "!")
        self.assertEqual(self.saved(), ["Lists", "Panel TV"])
        self.key(Action.ESCAPE)  # put back
        self.assertEqual(self.saved(), ["Lists", "Panel TV"])

    def test_a_name_with_slashes_keys_and_renames(self):
        # An unnamed provider is named after its address: its name holds "/" (and may start
        # with "@", as the list's own rows' keys do).
        self.manager.providers[1]["name"] = "http://panel.test/a"
        self.manager.providers.append({**PLAYLIST, "name": "@add", "url": "http://b.test/x"})
        self.settings.open(PROVIDERS)
        keys = [row.key for row in self.shown()]
        self.assertEqual(len(set(keys)), len(keys))
        self.assertNotIn(ADD_PROVIDER, keys[:3])
        self.into("http://panel.test/a")
        self.assertEqual(self.value("http://panel.test/a", "name"), "http://panel.test/a")
        self.move_to(row_key("http://panel.test/a", "name"))
        self.type_name("Films/Series", replace=True)
        self.assertEqual(self.saved()[1], "Films/Series")
        self.assertEqual(self.drawer.path(), (PROVIDERS, layer_key("Films/Series")))
        self.assertTrue(layer_key("Films/Series").startswith(PROVIDER_PREFIX))
        self.on("@add", "streams")
        self.key(Action.NAV_RIGHT)
        self.assertEqual(self.saved("@add")["max_streams"], 1)
        self.cards.add.assert_not_called()

    def test_rename_asks_the_pin_when_locked(self):
        self.window.parental.is_locked = True
        self.on("Panel", "name")
        with (
            mock.patch.object(parental_settings, "_ask_pin", return_value="1111") as asked,
            mock.patch.object(parental_settings.QMessageBox, "warning") as wrong,
        ):
            self.type_name("Renamed", replace=True)
        asked.assert_called_once()
        wrong.assert_called_once()
        self.assertEqual(self.saved(), ["Lists", "Panel"])
        self.assertEqual(self.value("Panel", "name"), "Panel")
        with mock.patch.object(parental_settings, "_ask_pin", return_value=PIN) as asked:
            self.type_name("Renamed", replace=True)
            self.type_name(" again")  # once per visit
        asked.assert_called_once()
        self.assertEqual(self.saved(), ["Lists", "Renamed again"])
        self.settings.close()  # the visit is over: asked again
        with mock.patch.object(parental_settings, "_ask_pin", return_value=None) as asked:
            self.on("Renamed again", "name")
            self.type_name("Third", replace=True)
        asked.assert_called_once()
        self.assertEqual(self.saved(), ["Lists", "Renamed again"])

    # ---- hidden groups -----------------------------------------------------------------------

    def test_hidden_groups_layer_and_show_again(self):
        tidy = self.window.hidden_groups
        tidy.hide("live", "Panel", "10", "Foreign news")
        tidy.hide("movies", "Panel", "20", "Drama")
        tidy.hide("live", "Lists", "30", "Local")  # another provider's
        hidden = self.on("Panel", "hidden")
        self.assertEqual(hidden.note, "2")
        self.key(Action.NAV_ACTIVATE)
        self.assertEqual(self.drawer.path()[-1], row_key("Panel", "hidden"))
        rows = self.shown()
        self.assertEqual([row.text for row in rows], ["Foreign news", "Drama"])
        self.assertEqual([row.sub for row in rows], ["Live TV", "Movies"])
        self.assertEqual({row.note for row in rows}, {"Show again"})
        self.assertEqual({row.kind for row in rows}, {ACTION})
        self.key(Action.NAV_ACTIVATE)  # at once, as the old box's Show again
        self.assertEqual([e["title"] for e in tidy.entries()], ["Drama", "Local"])
        self.window.controller.lists_changed.assert_called_once_with()
        self.assertEqual([row.text for row in self.shown()], ["Drama"])
        self.assertEqual(self.drawer.focused_row().text, "Drama")
        self.key(Action.NAV_ACTIVATE)
        self.assertEqual([e["title"] for e in tidy.entries()], ["Local"])
        self.assertEqual(self.shown(), ())
        self.assertEqual(self.drawer.focus_side(), "left")  # nothing left to focus
        self.assertEqual(self.drawer._chain()[-1].empty_text, "No hidden groups")

    def test_no_hidden_groups_says_so(self):
        hidden = self.on("Lists", "hidden")
        self.assertEqual(hidden.note, "")
        with mock.patch("ui.overlays.settings_drawer.announce") as said:
            self.key(Action.NAV_ACTIVATE)
        self.assertEqual(self.shown(), ())
        self.assertEqual(self.drawer.focus_side(), "left")
        self.assertIn("No hidden groups", said.call_args.args[1])

    # ---- the check ---------------------------------------------------------------------------

    def test_check_the_connection_notes_the_answer(self):
        self.on("Panel", "check")
        runner = self.window.jobs
        self.key(Action.NAV_ACTIVATE)
        self.assertEqual(self.row("Panel", "check").note, translate(BUSY_TEXT))
        self.key(Action.NAV_ACTIVATE)  # asked again while it checks: one check
        self.assertEqual(len(runner.jobs), 1)
        asked = []

        def answer(outcome):
            def check(provider, ssl_verify, password=""):
                asked.append((provider["name"], ssl_verify, password))
                return outcome

            with mock.patch.object(provider_check, "check", side_effect=check):
                runner.run()

        answer(Outcome(True))
        self.assertEqual(asked, [("Panel", True, "")])
        self.assertEqual(self.row("Panel", "check").note, "Provider verified successfully.")
        self.key(Action.NAV_ACTIVATE)
        answer(Outcome(False, "The username or password is wrong"))
        failed = "Failed to verify provider: The username or password is wrong."
        self.assertEqual(self.row("Panel", "check").note, failed)
        # Cut short at the row's end, the answer shows whole under the focused row (the keys
        # and the phone have no tooltip).
        index = [row.key for row in self.shown()].index(row_key("Panel", "check"))
        self.assertTrue(whole(self.drawer._box("right", index).help, failed))
        self.key(Action.NAV_ACTIVATE)
        answer(Outcome(True, http_only=True))  # the only place QiTV says so
        self.assertEqual(
            self.row("Panel", "check").note,
            f"Provider verified successfully. {translate(HTTP_ONLY_NOTE)}",
        )
        self.assertEqual(self.drawer.focused_row().key, row_key("Panel", "check"))
        # An answer keeps to its provider: renamed meanwhile, it moves with it ...
        self.key(Action.NAV_ACTIVATE)
        self.on("Panel", "name")
        self.type_name("Films", replace=True)
        answer(Outcome(True))
        self.assertEqual(self.row("Films", "check").note, "Provider verified successfully.")
        # ... and removed meanwhile, it goes with it.
        self.on("Lists", "check")
        self.key(Action.NAV_ACTIVATE)
        self.move_to(row_key("Lists", "remove"))
        self.key(Action.NAV_ACTIVATE)
        self.key(Action.NAV_RIGHT)
        self.key(Action.NAV_ACTIVATE)
        self.assertEqual(self.saved(), ["Films"])
        self.assertEqual(self.drawer.path(), (PROVIDERS,))
        answer(Outcome(False, "Gone"))
        self.assertNotIn("Lists", self.rows._notes)
        self.into("Films")
        self.assertEqual(self.row("Films", "check").note, "Provider verified successfully.")

    # ---- removing ----------------------------------------------------------------------------

    def test_remove_asks_and_hands_the_in_use_provider_over(self):
        self.window.scheduled = {"Lists"}
        self.on("Lists", "remove")
        self.key(Action.NAV_ACTIVATE)
        question = self.rows.question
        self.assertTrue(self.settings.card_shown())
        self.assertEqual(question.title(), translate(REMOVE_QUESTION, name="Lists"))
        self.assertEqual(question.body(), translate(REMOVE_RECORDINGS_NOTE))
        self.assertEqual(question.focus(), ("button", CANCEL))  # the safe answer first
        self.key(Action.NAV_ACTIVATE)  # Cancel
        self.assertFalse(self.settings.card_shown())
        self.assertEqual(self.saved(), ["Lists", "Panel"])
        self.assertEqual(self.drawer.path(), (PROVIDERS, layer_key("Lists")))
        self.key(Action.NAV_ACTIVATE)  # asked again
        self.key(Action.ESCAPE)  # Esc is Cancel too, never a layer up
        self.assertEqual(self.drawer.path(), (PROVIDERS, layer_key("Lists")))
        self.key(Action.NAV_ACTIVATE)
        self.key(Action.NAV_RIGHT)  # Remove
        self.assertEqual(question.focus(), ("button", "remove"))
        self.key(Action.NAV_ACTIVATE)
        self.assertEqual(self.saved(), ["Panel"])
        self.assertEqual(self.config.selected_provider_name, "Panel")
        self.assertIn(("providers_renamed", {}, ["Lists"]), self.window.events)
        self.assertIn(("set_provider", False), self.window.events)
        # Back on the list, on the one that took its place, which the TV uses now.
        self.assertEqual(self.drawer.path(), (PROVIDERS,))
        self.assertEqual(self.drawer.focused_row().key, layer_key("Panel"))
        self.assertEqual(self.drawer.focused_row().sub, "Xtream · in use")
        # Without scheduled recordings, nothing about them; locked, the PIN first.
        self.window.parental.is_locked = True
        self.key(Action.NAV_ACTIVATE)
        self.move_to(row_key("Panel", "remove"))
        with mock.patch.object(parental_settings, "_ask_pin", return_value=None) as asked:
            self.key(Action.NAV_ACTIVATE)
        asked.assert_called_once()
        self.assertFalse(self.settings.card_shown())  # refused: never asked
        with mock.patch.object(parental_settings, "_ask_pin", return_value=PIN):
            self.key(Action.NAV_ACTIVATE)
        self.assertEqual(question.body(), "")
        self.key(Action.ESCAPE)
        self.assertEqual(self.saved(), ["Panel"])

    def test_remove_by_the_mouse_and_a_double_click_keeps_the_question(self):
        self.on("Lists", "remove")
        index = [row.key for row in self.shown()].index(row_key("Lists", "remove"))
        point = self.drawer.row_rect("right", index).center().toPoint()
        window = self.host.windowHandle()
        for _ in range(2):  # a double-click: its second press lands on the card it opened
            QTest.mouseClick(window, Qt.MouseButton.LeftButton, pos=point)
        question = self.rows.question
        self.assertTrue(question.isVisible())  # never cancelled by that second press
        self.assertEqual(question.focus(), ("button", CANCEL))
        elapse(QApplication.doubleClickInterval() + 80)
        remove = question.control_rect("button", REMOVE_KEY).center().toPoint()
        QTest.mouseClick(window, Qt.MouseButton.LeftButton, pos=remove)
        self.assertFalse(question.isVisible())
        self.assertEqual(self.saved(), ["Panel"])
        self.assertEqual(self.drawer.path(), (PROVIDERS,))
        self.assertEqual(self.drawer.focused_row().key, layer_key("Panel"))


class ProvidersWindowTests(WindowCase):
    """Settings › Providers in the TV window: the catalogs' signal and the welcome card."""

    def setUp(self):
        super().setUp()
        patch = mock.patch("ui.tv_window.OptionsDialog")  # never a modal window here
        patch.start()
        self.addCleanup(patch.stop)

    def tearDown(self):
        elapse(QApplication.doubleClickInterval() + 80)
        super().tearDown()

    def open_providers(self):
        self.show()
        self.wait_for_drawer()
        self.menu_actions()["Settings…"].trigger()
        self.assertTrue(self.window.settings.visible())
        self.assertEqual(self.window.settings_drawer.path(), (PROVIDERS,))

    def press(self, *keys):
        for key in keys:
            QTest.keyClick(self.window, key)

    def down_to(self, key):
        drawer = self.window.settings_drawer
        for _ in range(12):
            row = drawer.focused_row()
            if row is not None and row.key == key:
                return
            self.window.controller.handle(Action.NAV_DOWN)
        self.fail(f"no row {key}")

    def test_removing_the_last_provider_leaves_the_welcome_card(self):
        self.open_providers()
        controller = self.window.controller
        controller.handle(Action.NAV_RIGHT)
        self.down_to(layer_key(PROVIDER_NAME))
        controller.handle(Action.NAV_ACTIVATE)
        self.down_to(row_key(PROVIDER_NAME, "remove"))
        controller.handle(Action.NAV_ACTIVATE)
        self.assertTrue(self.window.settings.card_shown())
        controller.handle(Action.NAV_RIGHT)
        controller.handle(Action.NAV_ACTIVATE)
        self.assertEqual(self.provider_manager.providers, [])
        drawer = self.window.settings_drawer
        self.assertEqual(drawer.path(), (PROVIDERS,))
        self.assertEqual(drawer.focused_row().key, ADD_PROVIDER)  # nothing points at it
        welcome = self.window.overlays.welcome
        self.assertTrue(welcome.isHidden())  # it waits under Settings
        controller.handle(Action.ESCAPE)
        controller.handle(Action.ESCAPE)
        self.assertFalse(self.window.settings.visible())
        self.assertTrue(wait_until(welcome.isVisible))

    def test_a_rename_in_the_window_stays_in_its_layer(self):
        self.open_providers()
        controller = self.window.controller
        controller.handle(Action.NAV_RIGHT)
        self.down_to(layer_key(PROVIDER_NAME))
        controller.handle(Action.NAV_ACTIVATE)
        controller.handle(Action.NAV_ACTIVATE)  # Name: editing
        controller.handle(Action.SELECT_ALL)
        controller.handle(Action.NAV_TEXT, "Renamed")
        controller.handle(Action.NAV_ACTIVATE)
        self.assertEqual([p["name"] for p in self.provider_manager.providers], ["Renamed"])
        self.assertEqual(self.config.selected_provider_name, "Renamed")
        drawer = self.window.settings_drawer
        self.assertEqual(drawer.path(), (PROVIDERS, layer_key("Renamed")))
        self.assertEqual(drawer.focused_row().key, row_key("Renamed", "name"))
        self.assertEqual(self.window.settings.value_text(drawer.focused_row()), "Renamed")

    def test_a_provider_added_elsewhere_shows_in_place(self):
        # A drop, Free channels or the old window add one while Settings shows: the list
        # changes under the viewer, who stays on the provider they were on.
        self.open_providers()
        self.window.controller.handle(Action.NAV_RIGHT)
        drawer = self.window.settings_drawer
        self.assertEqual(drawer.focused_row().key, layer_key(PROVIDER_NAME))
        entry = {"type": "M3UPLAYLIST", "name": "Aardvark", "url": "http://a.test/tv.m3u"}
        self.window._add_provider_entry(entry, lambda a, b: False)
        keys = [row.key for row in drawer._right_rows()]
        self.assertEqual(keys[:2], [layer_key(PROVIDER_NAME), layer_key("Aardvark")])
        self.assertEqual(drawer.focused_row().key, layer_key(PROVIDER_NAME))

    def test_free_channels_row_opens_the_windows_menu(self):
        self.open_providers()
        controller = self.window.controller
        controller.handle(Action.NAV_RIGHT)
        self.down_to(FREE_CHANNELS)
        with mock.patch("ui.tv_window.show_free_channels_menu") as menu:
            controller.handle(Action.NAV_ACTIVATE)
        menu.assert_called_once()
        self.assertIsInstance(menu.call_args.args[1], QPoint)
        self.assertEqual(
            menu.call_args.args[1], self.window.settings_drawer.row_anchor(FREE_CHANNELS)
        )


if __name__ == "__main__":
    unittest.main()
