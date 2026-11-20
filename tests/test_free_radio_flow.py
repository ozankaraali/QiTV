"""Free radio… (2.13) beside Free channels…: the same menu over radio-browser.info's lists, from
the welcome card, the channel list's row, the Providers menu, Settings and the Add card."""

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from timer_fixtures import wait_until

from services import free_channels, free_radio
from services.i18n import translate
from services.navigation import FREE_RADIO_ROW
from tests.fixtures.local_provider import PROVIDER_NAME
from tests.test_free_channels_flow import NoProviderCase
from tests.test_tv_window import WindowCase
from ui import free_channels_menu
from ui.free_channels_menu import ALL_TEXT, OTHER_TEXT, RADIO_TITLE, build_radio_menu


class MenuTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.chosen = []

    def test_it_asks_as_free_channels_does_and_names_the_directory(self):
        menu = build_radio_menu(None, self.chosen.append, territory="GB")
        actions = menu.actions()
        self.assertEqual(
            [a.text() for a in actions], [ALL_TEXT, "United Kingdom (your region)", OTHER_TEXT]
        )
        self.assertTrue(menu.toolTipsVisible())
        for action in actions:
            self.assertIn("radio-browser.info", action.toolTip())
        actions[1].trigger()
        actions[0].trigger()
        self.assertEqual(self.chosen, ["uk", None])

    def test_another_country_asks_its_picker(self):
        menu = build_radio_menu(None, self.chosen.append, territory="", picker=lambda _p: "de")
        menu.actions()[1].trigger()
        self.assertTrue(wait_until(lambda: self.chosen == ["de"]))

    def test_the_picker_lists_every_country_but_international(self):
        with mock.patch.object(
            free_channels_menu.QInputDialog, "getItem", return_value=("Türkiye", True)
        ) as get_item:
            self.assertEqual(free_channels_menu.pick_radio_country(None), "tr")
        args = get_item.call_args.args
        self.assertEqual(args[1], translate(RADIO_TITLE))
        self.assertEqual(len(args[3]), len(free_channels.COUNTRIES) - 1)
        self.assertNotIn("International", args[3])


class WelcomeFlowTests(NoProviderCase):
    def test_a_country_from_the_card_is_added_selected_saved_and_loaded(self):
        self.start_with_card()
        self.window.add_free_radio("tr")
        entry = free_radio.provider_entry("tr")
        self.assertEqual(self.provider_manager.providers, [entry])
        self.assertEqual(self.config.selected_provider_name, "Radio: Türkiye")
        self.select.assert_called_once_with(None, force_refresh=False)
        self.assertFalse(self.card.isVisible())
        reloaded = type(self.provider_manager)(self.config)
        self.assertEqual(reloaded.providers, [entry])

    def test_the_cards_radio_button_opens_the_menu_under_it(self):
        self.start_with_card()
        free, radio = self.card.free_button_rect(), self.card.radio_button_rect()
        self.assertEqual(radio.height(), free.height())
        self.assertGreater(radio.left(), free.right())  # beside Free channels…
        with mock.patch("ui.tv_window.show_free_radio_menu") as shown:
            QTest.mouseClick(self.card, Qt.MouseButton.LeftButton, pos=radio.center().toPoint())
        (parent, point, choose), _ = shown.call_args
        self.assertIs(parent, self.window)
        self.assertIsInstance(point, QPoint)
        self.assertEqual(choose, self.window.add_free_radio)
        self.select.assert_not_called()


class AddFlowTests(WindowCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(self.window.catalog, "select_provider")
        self.select = patcher.start()
        self.addCleanup(patcher.stop)

    def test_it_is_a_provider_as_any_other(self):
        self.window.add_free_radio(None)
        self.assertEqual(self.provider_manager.providers[-1], free_radio.provider_entry(None))
        self.assertEqual(self.config.selected_provider_name, "Radio: all countries")
        self.select.assert_called_once_with(None, force_refresh=False)

    def test_an_existing_list_only_switches_to_it(self):
        self.window.add_free_radio("us")
        before = [dict(p) for p in self.provider_manager.providers]
        self.config.selected_provider_name = PROVIDER_NAME
        self.select.reset_mock()
        self.window.add_free_radio("us")
        self.assertEqual(self.provider_manager.providers, before)
        self.assertEqual(self.config.selected_provider_name, "Radio: United States")
        self.select.assert_called_once_with(None, force_refresh=False)

    def test_free_channels_of_the_same_country_are_another_list(self):
        self.window.add_free_channels("fr")
        self.window.add_free_radio("fr")
        names = [p["name"] for p in self.provider_manager.providers]
        self.assertEqual(names[-2:], ["iptv-org: France", "Radio: France"])


class ProvidersMenuTests(WindowCase):
    def entries(self):
        menu = self.window.menus.providers_menu
        return {action.text(): action for action in menu.actions() if action.text()}

    def test_free_radio_follows_free_channels(self):
        texts = [a.text() for a in self.window.menus.providers_menu.actions()]
        self.assertEqual(texts[-3:], ["Add provider", "Free channels", "Free radio"])
        radio = self.entries()["Free radio"].menu()
        self.assertEqual(radio.styleSheet(), "")  # the menu bar's own look
        self.assertEqual(radio.actions()[0].text(), ALL_TEXT)
        with mock.patch.object(self.window, "add_free_radio") as add:
            radio.actions()[0].trigger()
        add.assert_called_once_with(None)

    def test_it_stays_once_after_a_rebuild(self):
        self.window.menus.rebuild_providers()
        self.window.menus.rebuild_providers()
        texts = [a.text() for a in self.window.menus.providers_menu.actions()]
        self.assertEqual(texts.count("Free radio"), 1)


class DrawerRowTests(WindowCase):
    def test_the_row_opens_the_menu_at_the_row(self):
        self.show()
        drawer = self.wait_for_drawer()
        drawer.open_drawer(focus_side="left")
        with mock.patch("ui.tv_window.show_free_radio_menu") as shown:
            self.window.menus.on_drawer_action(FREE_RADIO_ROW)
        (parent, point, choose), _ = shown.call_args
        self.assertIs(parent, self.window)
        self.assertEqual(point, drawer.row_anchor(FREE_RADIO_ROW))
        self.assertEqual(choose, self.window.add_free_radio)


if __name__ == "__main__":
    unittest.main()
