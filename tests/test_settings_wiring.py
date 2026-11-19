"""Settings in the drawer, wired into the TV window (plan 1): the drawer's Settings row and ⚙
and the menu's Settings… (Ctrl/⌘+,) open it over live TV, backing out of it gives the drawer
back on its Settings row when it came from there, and the Settings window opens on a tab."""

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtTest import QTest
from timer_fixtures import wait_until

from services.navigation import SETTINGS_ROW
from services.settings_model import GENERAL
from tests import test_tv_window as base
from ui.settings_dialog import OptionsDialog


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
                mock.call.open(from_drawer=True),
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
        self.window.menus.on_drawer_action(SETTINGS_ROW)  # its row, or ⚙ in its header
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

    def test_the_search_from_settings_leaves_it_for_the_library(self):
        self.show()
        self.wait_for_drawer()
        self.window.menus.on_drawer_action(SETTINGS_ROW)
        QTest.keyClick(self.window, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
        self.assertTrue(self.window.screens.library_shown())
        self.assertTrue(self.window.settings_drawer.isHidden())  # not only behind the page


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
