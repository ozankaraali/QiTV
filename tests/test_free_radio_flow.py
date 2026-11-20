"""Free radio (2.13) beside Free channels: its own layer of Settings › Providers (2.13.1) over
radio-browser.info's lists, from the welcome card, the channel list's row, the Providers menu,
Settings and the Add card."""

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from timer_fixtures import wait_until

from services import free_channels, free_radio, i18n
from services.m3u import parse_stations
from services.navigation import FREE_RADIO_ROW
from services.settings_model import FREE_CHANNELS, FREE_RADIO, PROVIDERS
from tests.fixtures.local_provider import PROVIDER_NAME
from tests.test_free_channels_flow import LayerCase, NoProviderCase, titles
from tests.test_free_radio import STATIONS
from tests.test_tv_window import WindowCase
from ui.free_channels_menu import ALL_TEXT, OTHER_TEXT, layer_rows
from ui.keymap import Action
from ui.settings_providers import layer_key


class LayerRowsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_every_country_but_international(self):
        rows = layer_rows(FREE_RADIO, territory="GB")
        self.assertEqual(titles(rows[:2]), [ALL_TEXT, "United Kingdom (your region)"])
        self.assertEqual(rows[1].key, f"{FREE_RADIO}/@region-uk")
        countries = titles(rows[2:])
        self.assertEqual(len(countries), len(free_channels.COUNTRIES) - 1)
        self.assertNotIn("International", countries)
        self.assertEqual(rows[2].key, f"{FREE_RADIO}/af")


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

    def test_the_cards_radio_button_opens_its_countries(self):
        self.start_with_card()
        free, radio = self.card.free_button_rect(), self.card.radio_button_rect()
        self.assertEqual(radio.height(), free.height())
        self.assertGreater(radio.left(), free.right())  # beside Free channels…
        self.click(radio)
        self.assert_layer(FREE_RADIO)
        self.select.assert_not_called()
        self.key(Action.NAV_ACTIVATE)  # All countries
        self.assertEqual(self.provider_manager.providers, [free_radio.provider_entry(None)])
        self.assertFalse(self.window.settings.visible())


class AddFlowTests(LayerCase):
    def setUp(self):
        super().setUp()
        self.hold_setups()

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


class SettingsLayerTests(LayerCase):
    def test_a_country_typed_in_free_radio_is_free_radios(self):
        # Both layers list the countries: a find typed in Free radio finds its own first,
        # never Free channels' (which would add the other list), in Turkish too.
        self.assertEqual(i18n.set_language("tr"), "tr")
        self.addCleanup(i18n.set_language, "en")
        self.start()
        self.window.show_free_radio()
        self.assert_layer(FREE_RADIO)
        for typed, code in (("almanya", "de"), ("turkiye", "tr")):
            with self.subTest(typed=typed):
                self.type(typed)
                found = [row.key for row in self.sd._right_rows()]
                self.assertEqual(found[0], f"{FREE_RADIO}/{code}")
                self.assertNotIn(f"{FREE_CHANNELS}/{code}", found)
                self.key(Action.ESCAPE)
        self.type("almanya")
        self.key(Action.NAV_ACTIVATE)
        self.key(Action.NAV_ACTIVATE)
        self.assertEqual(self.provider_manager.providers[-1], free_radio.provider_entry("de"))
        self.assertFalse(self.window.settings.visible())  # Settings opened for it

    def test_browsed_to_a_choice_shows_the_new_list_in_providers(self):
        self.start()
        self.window.open_settings(section_key=PROVIDERS)
        self.key(Action.NAV_RIGHT)
        for _ in range(8):
            if self.sd.focused_row().key == FREE_RADIO:
                break
            self.key(Action.NAV_DOWN)
        self.key(Action.NAV_ACTIVATE)
        self.assert_layer(FREE_RADIO)
        self.key(Action.NAV_ACTIVATE)  # All countries
        entry = free_radio.provider_entry(None)
        self.assertEqual(self.provider_manager.providers[-1], entry)
        self.assertTrue(self.window.settings.visible())
        self.assertEqual(self.sd.path(), (PROVIDERS,))
        self.assertEqual(self.sd.focused_row().key, layer_key(entry["name"]))


class ProvidersMenuTests(LayerCase):
    territory = "GB"

    def entries(self):
        menu = self.window.menus.providers_menu
        return {action.text(): action for action in menu.actions() if action.text()}

    def test_free_radio_follows_free_channels(self):
        self.window.menus.rebuild_providers()  # with the region given
        texts = [a.text() for a in self.window.menus.providers_menu.actions()]
        self.assertEqual(texts[-3:], ["Add provider", "Free channels", "Free radio"])
        radio = self.entries()["Free radio"].menu()
        self.assertEqual(radio.styleSheet(), "")  # the menu bar's own look
        actions = radio.actions()
        self.assertEqual(
            [a.text() for a in actions], [ALL_TEXT, "United Kingdom (your region)", OTHER_TEXT]
        )
        self.assertTrue(radio.toolTipsVisible())
        for action in actions:
            self.assertIn("radio-browser.info", action.toolTip())
        with mock.patch.object(self.window, "add_free_radio") as add:
            actions[0].trigger()
            actions[1].trigger()
        self.assertEqual(add.call_args_list, [mock.call(None), mock.call("uk")])
        self.start()  # (a provider ready builds the menu again)
        self.entries()["Free radio"].menu().actions()[-1].trigger()  # Another country…
        self.assert_layer(FREE_RADIO)

    def test_it_stays_once_after_a_rebuild(self):
        self.window.menus.rebuild_providers()
        self.window.menus.rebuild_providers()
        texts = [a.text() for a in self.window.menus.providers_menu.actions()]
        self.assertEqual(texts.count("Free radio"), 1)


class AllStationsTests(WindowCase):
    """A radio list's first group is All stations (its "All", renamed): every station, the
    most played first; its genres follow."""

    def seed(self, provider):
        manager = self.provider_manager
        manager.current_provider = provider
        manager.current_provider_content = {"itv": parse_stations(STATIONS)}
        manager.mark_content_fresh("itv")
        manager.save_provider()

    def test_the_first_group_holds_every_station_in_the_directorys_order(self):
        def groups():
            return [(g.title, g.count) for g in self.tv_catalog().groups("live")]

        self.show()
        self.wait_for_drawer()  # the list loaded (from the saved one)
        self.assertTrue(wait_until(lambda: bool(groups())))
        self.assertEqual(
            groups(),
            [("All stations", 5), ("News & talk", 1), ("Pop", 2), ("Rock", 1), ("Other", 1)],
        )
        every = self.tv_catalog().items("live", "*")
        self.assertEqual([c["name"] for c in every][:2], ["Wexlo FM", "Pell Rock"])


class DrawerRowTests(LayerCase):
    def test_the_row_opens_its_countries(self):
        drawer = self.start()
        for key in (Qt.Key.Key_Left, Qt.Key.Key_Left):
            QTest.keyClick(self.window, key)  # the sources column
        for _ in range(4):  # ⚙, ＋, ＋ free channels, ＋ free radio
            QTest.keyClick(self.window, Qt.Key.Key_Down)
        self.assertEqual(drawer.highlighted().key, FREE_RADIO_ROW)
        QTest.keyClick(self.window, Qt.Key.Key_Return)
        self.assert_layer(FREE_RADIO)
        self.assertFalse(drawer.isVisible())
        for _ in range(3):
            self.key(Action.ESCAPE)
        self.assertTrue(wait_until(drawer.isVisible))  # backed out of: the list where it was
        self.assertEqual(drawer.highlighted().key, FREE_RADIO_ROW)


if __name__ == "__main__":
    unittest.main()
