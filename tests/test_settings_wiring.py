"""Settings in the drawer, wired into the TV window (plan 1): the drawer's Settings row and ⚙
and the menu's Settings… (Ctrl/⌘+,) open it over live TV, backing out of it gives the drawer
back (on its Settings row from there, where it was from ⚙), a step reaches what it changes,
and the Settings window opens on a tab. Every Add provider opens it at Providers with the Add
card (plan 2), and a provider added there is selected and said; so does File → Open Link…'s
Add as a provider… (plan 4), and whichever card shows on top has the keys."""

from itertools import combinations
import os
import threading
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QGuiApplication, QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from timer_fixtures import elapse, wait_until

from services.config_manager import ConfigManager
from services.navigation import ADD_PROVIDER_ROW, OPEN_LINK_ROW, SETTINGS_ROW
from services.provider_check import Outcome
from services.settings_model import GENERAL, PROVIDERS, TEXT, Row, Section
from tests import test_tv_window as base
from ui import settings_layer
from ui.keymap import Action, resolve
from ui.settings_dialog import OptionsDialog
from ui.settings_layer import SettingsLayer
from ui.tv_window import SAVE_FAILED_TEXT

# The actions the drawer's keys give that Settings lets through on purpose, as over the picture:
# the playback and window keys (play/pause, Stop, the media keys' next and previous, the volume,
# mute, fullscreen, the mini player) and the shortcuts of every context: the quick search (it
# leaves Settings for the Library), Ctrl/⌘+D (the channel playing's favourite) and Ctrl/⌘+P
# (picture in picture).
PASSED_ON = frozenset(
    {
        Action.PLAY_PAUSE,
        Action.STOP,
        Action.MEDIA_NEXT,
        Action.MEDIA_PREV,
        Action.VOL_UP,
        Action.VOL_DOWN,
        Action.MUTE,
        Action.FULLSCREEN,
        Action.MINI_PLAYER,
        Action.SEARCH,
        Action.FAVORITE,
        Action.PIP,
    }
)


def drawer_actions() -> dict:
    """Every action keymap.resolve() gives in the drawer's context (Settings has the drawer's
    keys), with a payload it comes with: each key, held with each set of modifiers, typing
    nothing or its character, on each platform."""
    flags = [
        Qt.KeyboardModifier.ShiftModifier.value,
        Qt.KeyboardModifier.ControlModifier.value,
        Qt.KeyboardModifier.AltModifier.value,
        Qt.KeyboardModifier.MetaModifier.value,
        Qt.KeyboardModifier.KeypadModifier.value,
    ]
    held = [sum(chosen) for n in range(len(flags) + 1) for chosen in combinations(flags, n)]
    found: dict = {}
    for platform in ("darwin", "win32", "linux"):
        for key in Qt.Key:
            code = key.value
            texts = {""} | ({chr(code), chr(code).lower()} if 0x20 <= code < 0x7F else set())
            for mods in held:
                for text in texts:
                    got = resolve(code, mods, text, context="drawer", platform=platform)
                    if got is not None:
                        found.setdefault(got[0], got[1])
    return found


class SettingsWiringTests(base.WindowCase):
    def setUp(self):
        super().setUp()
        # Never a modal window here: a stray key on a section still in the Settings window
        # would wait for an answer.
        patch = mock.patch("ui.tv_window.OptionsDialog")
        self.dialog = patch.start()
        self.addCleanup(patch.stop)

    def test_opening_from_the_library_goes_back_to_the_tv(self):
        window = self.window
        order = mock.Mock()
        with (
            mock.patch.object(window.screens, "library_shown", return_value=True),
            mock.patch.object(window.screens, "back_to_tv", order.back_to_tv),
            mock.patch.object(window, "leave_mini_player", order.leave_mini_player),
            mock.patch.object(window.settings, "open", order.open),
        ):
            window.menus.on_drawer_action(SETTINGS_ROW)
        self.assertEqual(
            order.mock_calls,
            [
                mock.call.back_to_tv(),
                mock.call.leave_mini_player(),
                mock.call.open("", from_drawer=True),
            ],
        )

    def test_the_menu_from_the_library_shows_settings_over_the_tv(self):
        self.show()
        drawer = self.wait_for_drawer()
        screens = self.window.screens
        screens.open_library("movies")
        self.assertTrue(screens.library_shown())
        self.menu_actions()["Settings…"].trigger()
        self.assertFalse(screens.library_shown())
        self.assertTrue(self.window.settings.visible())
        self.assertFalse(drawer.isVisible())  # the drawer live TV opened on gave way

    def test_the_drawer_row_opens_settings_and_back_returns_to_it(self):
        self.show()
        drawer = self.wait_for_drawer()
        settings = self.window.settings
        QTest.keyClicks(self.window, "o")  # mid-find
        self.assertEqual(drawer.find_text, "o")
        self.window.menus.on_drawer_action(SETTINGS_ROW)  # its row
        self.assertTrue(settings.visible())
        self.assertFalse(drawer.isVisible())
        self.assertEqual(drawer.find_text, "")  # the find ended
        QTest.keyClick(self.window, Qt.Key.Key_Right)  # the section's rows
        self.assertEqual(self.window.settings_drawer.focus_side(), "right")
        for _ in range(2):  # rows → sections → closed
            QTest.keyClick(self.window, Qt.Key.Key_Escape)
        self.assertFalse(settings.visible())
        self.assertTrue(drawer.isVisible())
        self.assertEqual(drawer.focus_side, "left")
        self.assertEqual(drawer.highlighted().key, SETTINGS_ROW)
        QTest.keyClick(self.window, Qt.Key.Key_Return)  # Enter chooses it again
        self.assertTrue(settings.visible())
        self.assertFalse(drawer.isVisible())
        self.dialog.assert_not_called()

    def test_the_gear_in_a_group_backs_out_to_where_the_list_was(self):
        # ⚙ in a group, mid-find: backing out of Settings gives the list back in that group, on
        # the row and the side the keys were on before the find (closing the list ended it).
        self.show()
        drawer = self.wait_for_drawer()
        QTest.keyClick(self.window, Qt.Key.Key_Return)  # into the highlighted group

        def on(title):
            node = drawer.highlighted()
            return node is not None and node.layer == "item" and node.title == title

        self.assertTrue(wait_until(lambda: on("Channel 1")))
        QTest.keyClick(self.window, Qt.Key.Key_Down)
        self.assertTrue(wait_until(lambda: on("Channel 2")))
        path = list(self.window.nav.path)
        QTest.keyClicks(self.window, "o")  # mid-find
        self.assertTrue(drawer.finding())
        gear = drawer.settings_gear_rect().center().toPoint()
        QTest.mouseClick(drawer, Qt.MouseButton.LeftButton, pos=gear)
        self.assertTrue(self.window.settings.visible())
        self.assertFalse(drawer.isVisible())
        QTest.keyClick(self.window, Qt.Key.Key_Right)  # the section's rows
        for _ in range(2):  # rows → sections → closed
            QTest.keyClick(self.window, Qt.Key.Key_Escape)
        self.assertFalse(self.window.settings.visible())
        self.assertTrue(drawer.isVisible())
        self.assertEqual(self.window.nav.path, path)
        self.assertEqual(drawer.focus_side, "right")
        self.assertTrue(wait_until(lambda: on("Channel 2")))
        self.assertEqual(drawer.find_text, "")
        self.dialog.assert_not_called()

    def test_a_live_latency_step_reaches_the_running_channel(self):
        # The window's hooks are keyed by the rows' keys: a step on Live latency is applied to
        # the tuners at once (TVController.settings_saved), not only written.
        self.show()
        self.wait_for_drawer()
        self.menu_actions()["Settings…"].trigger()
        for key in (Qt.Key.Key_Down, Qt.Key.Key_Down, Qt.Key.Key_Right):  # Playback's rows
            QTest.keyClick(self.window, key)
        self.assertEqual(self.window.settings_drawer.focused_row().key, "live_latency")
        self.assertEqual(self.config.live_latency, "auto")
        tuner = mock.Mock()
        with mock.patch.object(self.window.controller, "_tuners", return_value=[tuner]):
            QTest.keyClick(self.window, Qt.Key.Key_Right)
        self.assertEqual(self.config.live_latency, "low")
        tuner.set_latency.assert_called_once_with("low")

    def test_a_deinterlacing_step_reaches_every_player(self):
        # Off is given to the players at the step (TVController.apply_deinterlacing), not only
        # once the Settings window closes; back to Auto waits for QiTV's next start, as a toast
        # says (libVLC 3 keeps the Off a player was given).
        self.show()
        self.wait_for_drawer()
        self.menu_actions()["Settings…"].trigger()
        for key in (Qt.Key.Key_Down, Qt.Key.Key_Down, Qt.Key.Key_Right):  # Playback's rows
            QTest.keyClick(self.window, key)
        for _ in range(2):  # past Hardware video decoding
            QTest.keyClick(self.window, Qt.Key.Key_Down)
        self.assertEqual(self.window.settings_drawer.focused_row().key, "deinterlace")
        self.assertEqual(self.config.deinterlace, "auto")
        players = [player for _name, player in self.window._players()]
        self.assertTrue(players)
        given = {}
        for player in players:
            given[player] = mock.Mock(return_value=True)
            patch = mock.patch.object(player, "set_deinterlace", given[player])
            patch.start()
            self.addCleanup(patch.stop)
        QTest.keyClick(self.window, Qt.Key.Key_Right)
        self.assertEqual(self.config.deinterlace, "off")
        for player in players:
            given[player].assert_called_once_with("off")
            given[player].reset_mock(return_value=True)
            given[player].return_value = False  # Auto cannot come back to it before a restart
            patch = mock.patch.object(player, "deinterlace", return_value="off")
            patch.start()
            self.addCleanup(patch.stop)
        QTest.keyClick(self.window, Qt.Key.Key_Right)  # round to Auto
        self.assertEqual(self.config.deinterlace, "auto")
        for player in players:
            given[player].assert_called_once_with("auto")
        self.assertIn(("Deinterlacing is back on Auto when QiTV starts again", "info"), self.toasts)

    def test_the_menu_opens_settings_without_the_drawer(self):
        self.show()
        drawer = self.wait_for_drawer()
        action = self.menu_actions()["Settings…"]
        self.assertEqual(action.shortcut(), QKeySequence("Ctrl+,"))
        action.trigger()
        self.assertTrue(self.window.settings.visible())
        self.assertFalse(drawer.isVisible())  # the open list gave way
        QTest.keyClick(self.window, Qt.Key.Key_Escape)  # the sections: closed
        self.assertFalse(self.window.settings.visible())
        self.assertFalse(drawer.isVisible())
        self.assertTrue(self.window.overlays.welcome.isHidden())  # a provider: no card
        self.dialog.assert_not_called()

    def test_settings_asked_again_while_it_shows_stays_as_it_is(self):
        self.show()
        drawer = self.wait_for_drawer()
        self.window.menus.on_drawer_action(SETTINGS_ROW)
        QTest.keyClick(self.window, Qt.Key.Key_Down)  # the next section
        path = self.window.settings_drawer.path()
        self.menu_actions()["Settings…"].trigger()  # Ctrl/⌘+, while it shows
        self.assertEqual(self.window.settings_drawer.path(), path)
        QTest.keyClick(self.window, Qt.Key.Key_Escape)
        self.assertTrue(drawer.isVisible())  # still from the drawer: it comes back
        self.assertEqual(drawer.highlighted().key, SETTINGS_ROW)

    def test_opening_settings_closes_the_guide(self):
        self.open_first_channel()
        guide, zap = self.window.overlays.guide, self.window.zap
        self.assertTrue(wait_until(lambda: zap.context.current() is not None))  # tuned
        QTest.keyClick(self.window, Qt.Key.Key_G)
        self.assertTrue(guide.isVisible())
        self.menu_actions()["Settings…"].trigger()
        self.assertTrue(guide.isHidden())  # it kept the keys, hidden under Settings
        self.assertEqual(self.window._key_context(), "drawer")
        QTest.keyClick(self.window, Qt.Key.Key_Down)
        self.assertEqual(self.window.settings_drawer.path(), (GENERAL,))

    def test_opening_settings_closes_the_subtitle_search(self):
        self.show()
        self.wait_for_drawer()
        search = self.window.subtitle_search
        search.show_key_page()  # the OpenSubtitles search's key page
        self.assertEqual(self.window._key_context(), "subtitles")
        self.menu_actions()["Settings…"].trigger()
        self.assertTrue(search.isHidden())
        self.assertEqual(self.window._key_context(), "drawer")
        QTest.keyClick(self.window, Qt.Key.Key_Down)
        self.assertEqual(self.window.settings_drawer.path(), (GENERAL,))

    def test_opening_settings_closes_the_pairing_screen(self):
        self.show()
        self.wait_for_drawer()
        pairing = self.window.pair_overlay
        pairing.open()
        self.menu_actions()["Settings…"].trigger()
        self.assertTrue(pairing.isHidden())  # any key would have closed it, unseen
        self.assertTrue(self.window.settings.visible())

    def test_a_question_keeps_its_place_above_settings(self):
        # It keeps the keys (it answers itself after a while), so it stays in sight too.
        self.show()
        self.wait_for_drawer()
        card, settings = self.window.question_card, self.window.settings_drawer
        card.ask("Stop the recording?", "Stop", "Keep")
        self.addCleanup(card.withdraw)
        self.menu_actions()["Settings…"].trigger()
        self.assertTrue(card.isVisible())
        stacked = [w for w in self.window.host.children() if w is card or w is settings]
        self.assertEqual(stacked, [settings, card])  # the card above

    def test_the_mouse_buttons_reach_a_question_over_settings_first(self):
        # A question shown over Settings (from the phone, say) keeps the keys: the mouse's
        # Back and Forward too.
        self.show()
        self.wait_for_drawer()
        self.menu_actions()["Settings…"].trigger()
        card = self.window.quality_card
        card.ask("Convert it on the server?", "Convert", "Not now")
        self.addCleanup(card.withdraw)
        self.window.controller.mouse_forward()  # the card's →
        self.assertEqual(self.window.settings_drawer.focus_side(), "left")
        self.window.controller.mouse_back()  # the card's Esc: Not now
        self.assertTrue(card.isHidden())
        self.assertTrue(self.window.settings.visible())  # Settings stays where it was
        self.window.controller.mouse_back()  # now Settings': the sections close
        self.assertFalse(self.window.settings.visible())

    def test_the_mouse_forward_goes_a_layer_down_but_never_steps(self):
        self.show()
        self.wait_for_drawer()
        self.menu_actions()["Settings…"].trigger()
        for key in (Qt.Key.Key_Down, Qt.Key.Key_Down):  # Playback
            QTest.keyClick(self.window, key)
        settings = self.window.settings_drawer
        self.window.controller.mouse_forward()  # into its rows
        self.assertEqual(settings.focus_side(), "right")
        self.assertEqual(settings.focused_row().key, "live_latency")
        self.window.controller.mouse_forward()
        self.assertEqual(self.config.live_latency, "auto")
        self.assertTrue(self.window.settings.visible())

    def test_a_click_on_the_picture_keeps_a_text_being_edited(self):
        # A click elsewhere keeps the text, as Enter does: the picture beyond the fade too,
        # though that click closes Settings.
        self.show()
        self.wait_for_drawer()
        only = (Section("s", "S", rows=(Row("probe", "Name", kind=TEXT),)),)
        settings = self.window.settings
        with mock.patch.object(settings, "_make_sections", return_value=only):
            self.menu_actions()["Settings…"].trigger()
        values = settings._values
        with (
            mock.patch.object(values, "get", return_value="old"),
            mock.patch.object(values, "set") as written,
        ):
            self.window.controller.handle(Action.NAV_RIGHT)
            self.window.controller.handle(Action.NAV_ACTIVATE)
            self.assertTrue(self.window.settings_drawer.editing())
            self.window.controller.handle(Action.NAV_TEXT, "x")
            self.window.controller.video_clicked()
        written.assert_called_once_with("probe", "oldx")
        self.assertFalse(settings.visible())

    def test_the_wiring_moved_without_change(self):
        # Plan 1's wiring is ui/settings_layer.py's now (the tests above run through it): the
        # TV controller hands its Settings entry points on, and the list's keys are as before.
        controller = self.window.controller
        layer = controller.settings_layer
        self.assertIsInstance(layer, SettingsLayer)
        for name, kwargs in (
            ("make_way_for_settings", {"keep_place": True}),
            ("reopen_drawer_on_settings", {}),
            ("close_settings", {}),
        ):
            with mock.patch.object(layer, name) as moved:
                getattr(controller, name)(**kwargs)
            moved.assert_called_once_with(**kwargs)
        self.assertEqual(
            settings_layer._LIST_KEYS,
            {
                Action.NAV_UP,
                Action.NAV_DOWN,
                Action.NAV_PAGE_UP,
                Action.NAV_PAGE_DOWN,
                Action.NAV_LEFT,
                Action.NAV_RIGHT,
                Action.NAV_ACTIVATE,
                Action.NAV_TEXT,
                Action.NAV_BACKSPACE,
                Action.NAV_HOME,
                Action.NAV_END,
                Action.NAV_DELETE,
                Action.FORGET,
                Action.SELECT_ALL,
                Action.COPY,
                Action.CUT,
                Action.PASTE,
            },
        )

    def test_every_drawer_action_is_handled_or_passed_on_purpose(self):
        # Settings has the drawer's keys: each action they give is Settings' own (a move, a
        # step, a layer up or down; while a text is edited, its field's too), one of the
        # channel list's that reaches nothing under it (_LIST_KEYS: the field's keys, while no
        # text is edited), or one it lets through on purpose (PASSED_ON). An action added to
        # the drawer's keys fails here until it is one of them.
        produced = drawer_actions()
        self.assertLessEqual(PASSED_ON, set(produced))
        # Delete is the list's FORGET in the drawer's keys; NAV_DELETE (the forward Delete of
        # the Library's and the subtitle search's fields) never comes from them.
        self.assertEqual(settings_layer._LIST_KEYS - set(produced), {Action.NAV_DELETE})
        self.show()
        self.wait_for_drawer()
        rows = (Row("probe", "Name", kind=TEXT), Row("other", "Other", kind=TEXT))
        only = (Section("s", "S", rows=rows),)
        settings, layer = self.window.settings, self.window.controller.settings_layer
        real = settings.handle
        answers: list = []  # what Settings' own handle answered, under the layer's

        def spy(what, data=None):
            answers.append(real(what, data))
            return answers[-1]

        own: dict = {False: set(), True: set()}
        swallowed: dict = {False: set(), True: set()}
        passed: dict = {False: set(), True: set()}
        values = settings._values
        with (
            mock.patch.object(settings, "_make_sections", return_value=only),
            mock.patch.object(values, "get", return_value="old"),
            mock.patch.object(values, "set"),
        ):
            for editing in (False, True):
                for action, payload in produced.items():
                    settings.open()  # afresh: Esc, Backspace and ← on the sections close it
                    if editing:
                        settings.handle(Action.NAV_RIGHT)  # onto Name,
                        settings.handle(Action.NAV_ACTIVATE)  # edited
                        self.assertTrue(self.window.settings_drawer.editing())
                    self.assertTrue(layer.shown())
                    answers.clear()
                    with mock.patch.object(settings, "handle", side_effect=spy):
                        taken = layer.handle(action, payload)
                    where = (action, payload, editing)
                    self.assertEqual(len(answers), 1, where)
                    if answers[0]:
                        own[editing].add(action)
                    elif taken:
                        swallowed[editing].add(action)
                    else:
                        passed[editing].add(action)
        for editing in (False, True):
            self.assertEqual(passed[editing], PASSED_ON, editing)
            self.assertLessEqual(swallowed[editing], settings_layer._LIST_KEYS, editing)
        moves = {
            Action.NAV_UP,
            Action.NAV_DOWN,
            Action.NAV_PAGE_UP,
            Action.NAV_PAGE_DOWN,
            Action.NAV_LEFT,
            Action.NAV_RIGHT,
            Action.NAV_ACTIVATE,
            Action.NAV_BACKSPACE,
            Action.ESCAPE,
        }
        self.assertEqual(own[False], moves)
        # Each of the list's keys that Settings does not use is a key of the field being
        # edited: while one is, Settings uses every action the keys give but PASSED_ON.
        self.assertEqual(swallowed[True], set())
        self.assertEqual(own[True], set(produced) - PASSED_ON)
        self.assertEqual(swallowed[False], own[True] - moves)

    def test_a_failed_save_is_said_once_when_settings_hides(self):
        # A step saves in the background (its failure is only logged there): Settings says
        # so as it goes, once, not at each step nor at each visit while it still fails.
        self.show()
        self.wait_for_drawer()

        def visit(*keys):
            self.menu_actions()["Settings…"].trigger()
            for key in keys:
                QTest.keyClick(self.window, key)
            for _ in range(3):  # rows → sections → closed, at most
                if self.window.settings.visible():
                    QTest.keyClick(self.window, Qt.Key.Key_Escape)
            self.assertFalse(self.window.settings.visible())

        def said():
            return self.toasts.count((SAVE_FAILED_TEXT, "error"))

        down, right = Qt.Key.Key_Down, Qt.Key.Key_Right
        with mock.patch.object(self.config, "save_failed", return_value=True):
            self.menu_actions()["Settings…"].trigger()
            for key in (down, down, right, right, right):  # Live latency, stepped twice
                QTest.keyClick(self.window, key)
            self.assertEqual(self.config.live_latency, "normal")
            self.assertEqual(said(), 0)  # never at a step
            QTest.keyClick(self.window, Qt.Key.Key_Escape)
            QTest.keyClick(self.window, Qt.Key.Key_Escape)
            self.assertFalse(self.window.settings.visible())
            self.assertEqual(said(), 1)
            visit(down, down, right, right)  # it still fails: said already
            self.assertEqual(said(), 1)
        visit()  # a save worked since: nothing to say
        self.assertEqual(said(), 1)
        with mock.patch.object(self.config, "save_failed", return_value=True):
            visit(down, down, right, right)  # it fails again: said again
        self.assertEqual(said(), 2)

    def test_the_channel_list_opened_over_settings_closes_it(self):
        # The list opened while Settings shows (a menu, a provider ready) shows on top and
        # takes the keys: Settings goes.
        self.show()
        drawer = self.wait_for_drawer()
        self.menu_actions()["Settings…"].trigger()
        self.assertFalse(drawer.isVisible())
        self.window.controller.handle(Action.OPEN_DRAWER)
        self.assertTrue(drawer.isVisible())
        self.assertFalse(self.window.settings.visible())
        QTest.keyClick(self.window, Qt.Key.Key_Escape)  # the list's own Esc
        self.assertFalse(drawer.isVisible())

    def test_the_search_from_settings_leaves_it_for_the_library(self):
        self.show()
        self.wait_for_drawer()
        self.window.menus.on_drawer_action(SETTINGS_ROW)
        QTest.keyClick(self.window, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
        self.assertTrue(self.window.screens.library_shown())
        self.assertTrue(self.window.settings_drawer.isHidden())  # not only behind the page


class AddProviderTests(base.WindowCase):
    """Every Add provider opens Settings at Providers with the Add card over it (plan 2)."""

    # A playlist on a closed port of the loopback: the new provider's setup is never real.
    LINK = "http://127.0.0.1:9/list.m3u"

    def setUp(self):
        super().setUp()
        patch = mock.patch("ui.tv_window.OptionsDialog")  # never the Settings window's form
        self.dialog = patch.start()
        self.addCleanup(patch.stop)

    def tearDown(self):
        elapse(QApplication.doubleClickInterval() + 80)  # no second-click guard left armed
        super().tearDown()

    def menu_entry(self, text):
        menu = self.window.menus.providers_menu
        return next(action for action in menu.actions() if action.text() == text)

    def assert_the_add_card(self):
        window, form = self.window, self.window.provider_cards.form
        self.assertTrue(window.settings.visible())
        self.assertEqual(window.settings_drawer.path(), (PROVIDERS,))
        self.assertTrue(form.isVisible())
        self.assertEqual(form.title(), "Add a provider")
        self.assertTrue(window.settings.card_shown())  # it has the keys
        self.assertEqual(form.focus(), ("field", "url"))
        self.dialog.assert_not_called()

    def test_every_add_provider_opens_the_card(self):
        self.show()
        drawer = self.wait_for_drawer()
        window = self.window
        screens = window.screens
        form = window.provider_cards.form

        def the_drawers_row():
            for key in (Qt.Key.Key_Left, Qt.Key.Key_Left):  # the sources column
                QTest.keyClick(window, key)
            for _ in range(8):
                if drawer.highlighted().key == ADD_PROVIDER_ROW:
                    break
                QTest.keyClick(window, Qt.Key.Key_Down)
            self.assertEqual(drawer.highlighted().key, ADD_PROVIDER_ROW)
            QTest.keyClick(window, Qt.Key.Key_Return)

        def the_menu_bar():
            self.menu_entry("Add provider").trigger()

        def the_welcome_card():
            window.overlays.welcome.addRequested.emit()  # its button (tests/test_welcome.py)

        def the_library():
            screens.open_library("movies")
            self.assertTrue(screens.library_shown())
            window.library_controller.addProviderRequested.emit()  # "… has no movies"
            self.assertFalse(screens.library_shown())  # back to the TV first

        for source in (the_drawers_row, the_menu_bar, the_welcome_card, the_library):
            with self.subTest(source=source.__name__):
                source()
                self.assert_the_add_card()
                self.assertFalse(drawer.isVisible())  # it gave way
                QTest.keyClick(window, Qt.Key.Key_Escape)  # the card's Cancel
                self.assertFalse(form.isVisible())
                self.assertTrue(window.settings.visible())  # a provider: Settings stays
                self.assertEqual(window.settings_drawer.path(), (PROVIDERS,))
                QTest.keyClick(window, Qt.Key.Key_Escape)  # out of Settings
                self.assertFalse(window.settings.visible())
                if source is the_drawers_row:  # the list again, where it was
                    self.assertTrue(drawer.isVisible())
                    self.assertEqual(drawer.highlighted().key, ADD_PROVIDER_ROW)
                    drawer.close_drawer()
        self.assertEqual(self.provider_manager.providers[0]["name"], base.PROVIDER_NAME)
        self.assertEqual(len(self.provider_manager.providers), 1)  # nothing added

    def test_a_double_clicks_second_click_on_the_librarys_add_provider_reaches_nothing(self):
        # Its button acts on the release and the Library goes: the second press of a
        # double-click would land on the card's dimmed picture, and cancel it.
        self.show()
        self.wait_for_drawer()
        window = self.window
        window.screens.open_library("movies")
        elapse(QApplication.doubleClickInterval() + 80)  # the Library's own guard gone
        window.library_controller.addProviderRequested.emit()  # its button, clicked
        form = window.provider_cards.form
        self.assertTrue(form.isVisible())
        host = form.window()
        dimmed = QPoint(20, host.height() - 20)  # the picture beside the card
        QTest.mouseClick(host.windowHandle(), Qt.MouseButton.LeftButton, pos=dimmed)
        self.assertTrue(form.isVisible())  # it reached nothing
        elapse(QApplication.doubleClickInterval() + 80)
        QTest.mouseClick(host.windowHandle(), Qt.MouseButton.LeftButton, pos=dimmed)
        self.assertFalse(form.isVisible())  # a click of its own: the dimmed picture cancels

    def test_add_provider_while_settings_shows_moves_to_providers(self):
        self.show()
        drawer = self.wait_for_drawer()
        window = self.window
        window.menus.on_drawer_action(SETTINGS_ROW)  # from the list's Settings row
        QTest.keyClick(window, Qt.Key.Key_Down)  # another section
        self.assertNotEqual(window.settings_drawer.path()[0], PROVIDERS)
        self.menu_entry("Add provider").trigger()
        self.assert_the_add_card()
        QGuiApplication.clipboard().setText(self.LINK)
        window.controller.handle(Action.PASTE)
        self.menu_entry("Add provider").trigger()  # asked again: what was typed stays
        self.assertEqual(window.provider_cards.form.values()["url"], self.LINK)
        QTest.keyClick(window, Qt.Key.Key_Escape)  # the card's Cancel
        QTest.keyClick(window, Qt.Key.Key_Escape)  # out of Settings: where it came from
        self.assertFalse(window.settings.visible())
        self.assertTrue(drawer.isVisible())
        self.assertEqual(drawer.highlighted().key, SETTINGS_ROW)

    def test_an_add_through_the_card_reaches_the_toast_and_selects_it(self):
        self.show()
        self.wait_for_drawer()
        window = self.window
        form = window.provider_cards.form
        self.menu_entry("Add provider").trigger()
        self.assert_the_add_card()
        QGuiApplication.clipboard().setText(self.LINK)
        window.controller.handle(Action.PASTE)  # Ctrl/⌘+V into the link's field
        self.assertEqual(form.found(), ("✓ M3U playlist at 127.0.0.1:9", True))
        with (
            mock.patch("services.provider_check.check", return_value=Outcome(True)) as check,
            mock.patch.object(window.catalog, "select_provider") as select,
        ):
            QTest.keyClick(window, Qt.Key.Key_Return)  # Check and add
            self.assertTrue(wait_until(lambda: not form.isVisible()))
        asked = check.call_args.args[0]
        self.assertEqual((asked["type"], asked["url"]), ("M3UPLAYLIST", self.LINK))
        self.assertIn(("Added 127.0.0.1 to your providers", "info"), self.toasts)
        self.assertEqual(self.config.selected_provider_name, "127.0.0.1")
        self.assertEqual(ConfigManager().selected_provider_name, "127.0.0.1")  # saved
        select.assert_called_once_with(None, force_refresh=False)  # the TV shows it
        names = [p["name"] for p in self.provider_manager.providers]
        self.assertEqual(names, [base.PROVIDER_NAME, "127.0.0.1"])
        menu = [a.text() for a in window.menus.providers_menu.actions() if a.isCheckable()]
        self.assertIn("127.0.0.1", menu)
        self.assertTrue(window.settings.visible())  # on Providers, the new one listed
        rows = [row.text for row in window.provider_rows.rows()]
        self.assertEqual(rows, [base.PROVIDER_NAME, "127.0.0.1"])

    # ---- File → Open Link…'s Add as a provider… (plan 4) ---------------------------------------

    # A whole Xtream account's playlist link, as Open Link reads it (never asked: no stream).
    ACCOUNT = "http://panel.example/get.php?username=demo&password=secret&type=m3u_plus"

    def link_in_card(self, text):
        """The Open Link card over what shows, `text` typed into it."""
        window, card = self.window, self.window.open_link_card
        window.open_link()
        QTest.keyClicks(window, text)
        self.assertEqual(card.values(), {"link": text})
        return card

    def assert_on_top(self, upper, lower):
        stack = self.window.host.children()  # in stacking order: the last raised last
        at = {id(widget): i for i, widget in enumerate(stack)}
        self.assertGreater(at[id(upper)], at[id(lower)])

    def test_open_links_add_as_a_provider_opens_the_add_card_filled_in(self):
        """Its Add as a provider… is plan 2's Add card, filled in from the link, with the keys
        (never the Settings window's form); opened from the channel list's row, backing out of
        Settings gives the list back where it was, as the list's Add provider does."""
        self.show()
        drawer = self.wait_for_drawer()
        window = self.window
        form = window.provider_cards.form
        highlighted = drawer.highlighted()
        window.menus.on_drawer_action(OPEN_LINK_ROW)  # the list's Open a link
        card = window.open_link_card
        self.assertFalse(card.isHidden())
        self.assertTrue(drawer.isVisible())  # under the card
        QTest.keyClicks(window, self.ACCOUNT)
        self.assertEqual(card.found(), ("✓ Xtream at panel.example, with its login", True))
        QTest.keyClick(window, Qt.Key.Key_Return)  # Add as a provider…
        self.assertTrue(card.isHidden())
        self.assertFalse(drawer.isVisible())  # it gave way to Settings
        self.assertTrue(window.settings.visible())
        self.assertEqual(window.settings_drawer.path(), (PROVIDERS,))
        self.assertTrue(form.isVisible())
        self.assertEqual(form.title(), "Add a provider")
        self.assertTrue(window.settings.card_shown())
        values = form.values()
        self.assertEqual(
            (values["url"], values["username"], values["password"]),
            ("http://panel.example", "demo", "secret"),
        )
        self.dialog.assert_not_called()
        focus = form.focus()
        QTest.keyClick(window, Qt.Key.Key_Down)  # the Add card's keys
        self.assertNotEqual(form.focus(), focus)
        self.assertTrue(card.isHidden())
        QTest.keyClick(window, Qt.Key.Key_Escape)  # the card's Cancel
        self.assertFalse(form.isVisible())
        self.assertTrue(window.settings.visible())
        QTest.keyClick(window, Qt.Key.Key_Escape)  # out of Settings: the list, where it was
        self.assertFalse(window.settings.visible())
        self.assertTrue(drawer.isVisible())
        self.assertEqual(drawer.highlighted(), highlighted)
        self.assertEqual(len(self.provider_manager.providers), 1)  # nothing added

    def test_add_provider_over_the_open_link_card_cancels_it(self):
        """The Open Link card over Settings has the keys; an Add provider meanwhile (the menu
        bar's, a dropped portal link) brings Settings and its Add card over it: it cancels,
        as when Settings opens, and the Add card has the keys, not the card under it."""
        self.show()
        self.wait_for_drawer()
        window = self.window
        form = window.provider_cards.form
        self.menu_actions()["Settings…"].trigger()
        card = self.link_in_card("a")  # its keys, not Settings' under it
        self.assertTrue(window.settings.visible())
        self.menu_entry("Add provider").trigger()
        self.assertTrue(card.isHidden())
        self.assert_the_add_card()
        QTest.keyClick(window, Qt.Key.Key_B)
        self.assertEqual(form.values()["url"], "b")
        QTest.keyClick(window, Qt.Key.Key_Escape)  # the Add card's Cancel
        self.assertFalse(form.isVisible())
        self.assertTrue(card.isHidden())  # not back by itself
        self.assertTrue(window.settings.visible())

    def test_the_open_link_card_over_the_add_card_keeps_the_keys(self):
        """File → Open Link… over the Add card has the keys; the check's answer, come
        meanwhile, shows on the Add card under it, and the keys stay where the eye is. Once
        the link's card goes, the Add card has them again."""
        self.show()
        self.wait_for_drawer()
        window = self.window
        form = window.provider_cards.form
        self.menu_entry("Add provider").trigger()
        QGuiApplication.clipboard().setText(self.LINK)
        window.controller.handle(Action.PASTE)
        answer = threading.Event()

        def held(*_args, **_kwargs):
            answer.wait(10)
            return Outcome(False, "The provider took too long to answer")

        with mock.patch("services.provider_check.check", side_effect=held):
            QTest.keyClick(window, Qt.Key.Key_Return)  # Check and add: on its way
            self.assertTrue(form.busy())
            card = self.link_in_card("a")  # File → Open Link… meanwhile
            self.assert_on_top(card, form)
            answer.set()
            self.assertTrue(wait_until(lambda: not form.busy()))
        self.assertTrue(form.isVisible())
        found, ok = form.found()
        self.assertFalse(ok)
        self.assertIn("took too long", found)  # said on the card, under the link's
        self.assert_on_top(card, form)  # shown again under it
        QTest.keyClick(window, Qt.Key.Key_B)
        self.assertEqual(card.values(), {"link": "ab"})  # still the link card's
        self.assertEqual(form.values()["url"], self.LINK)
        QTest.keyClick(window, Qt.Key.Key_Escape)  # the link's card goes
        self.assertTrue(card.isHidden())
        self.assertTrue(form.isVisible())
        self.assertTrue(window.settings.card_shown())  # the keys again
        QTest.keyClick(window, Qt.Key.Key_Escape)  # its Cancel
        self.assertFalse(form.isVisible())
        self.assertEqual(len(self.provider_manager.providers), 1)  # nothing added

    def test_a_double_click_on_add_as_a_provider_keeps_the_add_card(self):
        # Its button acts on the press and the Add card shows under the pointer: a
        # double-click's second press would land on its dimmed picture, and cancel it.
        self.show()
        self.wait_for_drawer()
        window = self.window
        form = window.provider_cards.form
        card = self.link_in_card(self.ACCOUNT)
        host = card.window()  # the window the cards are on
        button = card.control_rect("button", "provider").center().toPoint()
        handle = host.windowHandle()
        QTest.mouseClick(handle, Qt.MouseButton.LeftButton, pos=card.mapTo(host, button))
        self.assertTrue(card.isHidden())
        self.assertTrue(form.isVisible())
        dimmed = QPoint(20, host.height() - 20)  # the picture beside the card
        QTest.mouseClick(handle, Qt.MouseButton.LeftButton, pos=dimmed)  # the second, quick
        self.assertTrue(form.isVisible())  # it reached nothing
        elapse(QApplication.doubleClickInterval() + 80)
        QTest.mouseClick(handle, Qt.MouseButton.LeftButton, pos=dimmed)
        self.assertFalse(form.isVisible())  # a click of its own: the dimmed picture cancels


class NoProviderSettingsTests(base.WindowCase):
    def provider_list(self):
        return []

    def test_the_welcome_card_hides_under_settings_and_comes_back(self):
        self.show()
        card = self.window.overlays.welcome
        self.assertTrue(wait_until(card.isVisible))
        with mock.patch("ui.tv_window.OptionsDialog"):
            self.menu_actions()["Settings…"].trigger()
            self.assertTrue(self.window.settings.visible())
            self.assertTrue(card.isHidden())
            QTest.keyClick(self.window, Qt.Key.Key_Escape)  # the sections: closed
        self.assertFalse(self.window.settings.visible())
        self.assertTrue(card.isVisible())  # still no provider: the card again


class SettingsWindowTabTests(base.WindowCase):
    TABS = {
        "": "General",
        "general": "General",
        "timeshift": "Time-shift",
        "subtitles": "Subtitles",
        "remote": "Phone remote",
        "parental": "Parental controls",
        "providers": "Providers",
        "epg": "EPG",
    }

    def test_options_dialog_selects_a_tab_by_key(self):
        shown = []

        def exec_at_once(dialog):
            tabs = dialog.options_tab
            shown.append(tabs.tabText(tabs.currentIndex()))
            epg.append(tabs.currentWidget() is dialog.epg_tab)
            dialog.deleteLater()
            return 0

        epg: list[bool] = []
        with mock.patch.object(OptionsDialog, "exec", exec_at_once):
            self.window.options_dialog(tab="epg")
            self.assertEqual(epg, [True])  # the EPG tab is the current one
            for key in self.TABS:
                self.window.options_dialog(tab=key)
        self.assertEqual(shown[1:], list(self.TABS.values()))


if __name__ == "__main__":
    unittest.main()
