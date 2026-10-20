import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from timer_fixtures import wait_until

from services import free_channels
from services.navigation import FREE_CHANNELS_ROW
from tests.fixtures.local_provider import PROVIDER_NAME
from tests.test_tv_window import WindowCase
from ui import free_channels_menu
from ui.free_channels_menu import ALL_TEXT, OTHER_TEXT, build_menu


class MenuTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.chosen = []

    def texts(self, menu):
        return [a.text() for a in menu.actions()]

    def test_a_known_region_adds_its_item(self):
        menu = build_menu(None, self.chosen.append, territory="GB")
        self.assertEqual(self.texts(menu), [ALL_TEXT, "United Kingdom (your region)", OTHER_TEXT])
        menu.actions()[1].trigger()
        menu.actions()[0].trigger()
        self.assertEqual(self.chosen, ["uk", None])

    def test_an_unknown_region_has_no_item(self):
        for territory in ("ZZ", ""):
            menu = build_menu(None, self.chosen.append, territory=territory)
            self.assertEqual(self.texts(menu), [ALL_TEXT, OTHER_TEXT])

    def test_the_region_comes_from_the_system_locale(self):
        with mock.patch.object(free_channels_menu, "system_territory", return_value="TR"):
            menu = build_menu(None, self.chosen.append)
        self.assertEqual(self.texts(menu)[1], "Türkiye (your region)")

    def test_another_country_asks_the_picker(self):
        menu = build_menu(None, self.chosen.append, territory="", picker=lambda _p: "de")
        menu.actions()[1].trigger()
        self.assertTrue(wait_until(lambda: self.chosen == ["de"]))

    def test_cancelling_the_picker_adds_nothing(self):
        asked = []
        menu = build_menu(None, self.chosen.append, territory="", picker=lambda _p: asked.append(1))
        menu.actions()[1].trigger()
        self.assertTrue(wait_until(lambda: asked == [1]))
        QTest.qWait(20)
        self.assertEqual(self.chosen, [])

    def test_the_default_picker_lists_every_country_and_maps_a_name_back(self):
        with mock.patch.object(
            free_channels_menu.QInputDialog, "getItem", return_value=("Türkiye", True)
        ) as get_item:
            self.assertEqual(free_channels_menu.pick_country(None), "tr")
        args = get_item.call_args.args
        self.assertEqual(len(args[3]), len(free_channels.COUNTRIES))
        self.assertIs(args[5], False)  # not editable
        with mock.patch.object(
            free_channels_menu.QInputDialog, "getItem", return_value=("Türkiye", False)
        ):
            self.assertIsNone(free_channels_menu.pick_country(None))


class NoProviderCase(WindowCase):
    def provider_list(self):
        return []

    def start_with_card(self):
        self.show()
        self.card = self.window.overlays.welcome
        self.assertTrue(wait_until(self.card.isVisible))
        patcher = mock.patch.object(self.window.catalog, "select_provider")
        self.select = patcher.start()
        self.addCleanup(patcher.stop)


class WelcomeFlowTests(NoProviderCase):
    def test_a_country_from_the_card_is_added_selected_saved_and_loaded(self):
        self.start_with_card()
        with mock.patch.object(self.window, "save_config", wraps=self.window.save_config) as save:
            self.window.add_free_channels("tr")
        entry = free_channels.provider_entry("tr")
        self.assertEqual(self.provider_manager.providers, [entry])
        self.assertEqual(self.config.selected_provider_name, entry["name"])
        save.assert_called_once_with()
        self.select.assert_called_once_with(None, force_refresh=False)
        self.assertFalse(self.card.isVisible())  # dismissed as after Settings → Save
        menu = self.window.menus.providers_menu
        listed = [a.text() for a in menu.actions() if a.isCheckable()]
        self.assertEqual(listed, [entry["name"]])  # the Providers menu was rebuilt
        # It is on disk too.
        reloaded = type(self.provider_manager)(self.config)
        self.assertEqual(reloaded.providers, [entry])

    def test_the_cards_button_opens_the_menu_under_it(self):
        self.start_with_card()
        with mock.patch("ui.tv_window.show_free_channels_menu") as shown:
            QTest.mouseClick(
                self.card,
                Qt.MouseButton.LeftButton,
                pos=self.card.free_button_rect().center().toPoint(),
            )
        (parent, point, choose), _ = shown.call_args
        self.assertIs(parent, self.window)
        self.assertIsInstance(point, QPoint)
        self.assertEqual(choose, self.window.add_free_channels)
        self.select.assert_not_called()  # nothing added until a choice


class AddFlowTests(WindowCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(self.window.catalog, "select_provider")
        self.select = patcher.start()
        self.addCleanup(patcher.stop)

    def test_an_existing_url_only_switches_to_it(self):
        self.window.add_free_channels("us")
        before = list(self.provider_manager.providers)
        self.config.selected_provider_name = PROVIDER_NAME
        self.select.reset_mock()
        self.window.add_free_channels("us")
        self.assertEqual(self.provider_manager.providers, before)
        self.assertEqual(self.config.selected_provider_name, "iptv-org: United States")
        self.select.assert_called_once_with(None, force_refresh=False)

    def test_the_provider_already_showing_is_not_loaded_again(self):
        url = free_channels.country_url("fr")
        current = self.window.catalog.current_provider_name()
        self.provider_manager.providers[0]["url"] = url
        self.provider_manager.providers[0]["name"] = current
        self.window.add_free_channels("fr")
        self.assertEqual(len(self.provider_manager.providers), 1)
        self.assertEqual(self.config.selected_provider_name, current)
        self.select.assert_not_called()

    def test_a_name_clash_gets_a_unique_name(self):
        clash = free_channels.provider_entry("uk")["name"]
        self.provider_manager.providers.append(
            {"type": "M3UPLAYLIST", "name": clash, "url": "http://example.invalid/a.m3u"}
        )
        self.window.add_free_channels("uk")
        names = [p["name"] for p in self.provider_manager.providers]
        self.assertEqual(names[-2:], [clash, f"{clash} (2)"])
        self.assertEqual(self.config.selected_provider_name, f"{clash} (2)")
        self.window.add_free_channels("uk")  # the same URL again: no third
        self.assertEqual(len(self.provider_manager.providers), len(names))


class OldAddressTests(WindowCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(self.window.catalog, "select_provider")
        self.select = patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_list_of_all_countries_at_its_old_address_is_only_switched_to(self):
        old = {
            "type": "M3UPLAYLIST",
            "name": free_channels.ALL_NAME,
            "url": "https://iptv-org.github.io/iptv/index.m3u",
        }
        self.provider_manager.providers.append(dict(old))
        self.window.add_free_channels(None)
        self.assertEqual(len(self.provider_manager.providers), 2)
        self.assertEqual(self.provider_manager.providers[-1], old)  # kept as it was
        self.assertEqual(self.config.selected_provider_name, free_channels.ALL_NAME)


class ProvidersMenuTests(WindowCase):
    """The menu bar's Providers menu adds providers too: the welcome card rarely shows (the
    last provider stays), and Settings → Providers is not where one looks for free lists."""

    def entries(self):
        menu = self.window.menus.providers_menu
        return {action.text(): action for action in menu.actions() if action.text()}

    def test_free_channels_and_add_provider_follow_the_providers(self):
        menu = self.window.menus.providers_menu
        texts = [action.text() for action in menu.actions()]
        self.assertEqual(texts[0], PROVIDER_NAME)
        self.assertEqual(texts[-2:], ["Add provider", "Free channels"])
        free = self.entries()["Free channels"].menu()
        self.assertEqual(free.styleSheet(), "")  # the menu bar's own look
        self.assertEqual(free.actions()[0].text(), ALL_TEXT)
        self.assertEqual(free.actions()[-1].text(), OTHER_TEXT)
        with mock.patch.object(self.window, "add_free_channels") as add:
            free.actions()[0].trigger()
        add.assert_called_once_with(None)
        with mock.patch.object(self.window, "add_provider") as add_provider:
            self.entries()["Add provider"].trigger()
        add_provider.assert_called_once_with()

    def test_they_stay_after_a_rebuild(self):
        self.window.menus.rebuild_providers()
        self.window.menus.rebuild_providers()
        texts = [action.text() for action in self.window.menus.providers_menu.actions()]
        self.assertEqual(texts.count("Free channels"), 1)


class SettingsFreeChannelsTests(WindowCase):
    def test_settings_lists_a_free_list_until_saved(self):
        from ui.settings_dialog import OptionsDialog

        dialog = OptionsDialog(self.window)
        self.addCleanup(dialog.deleteLater)
        with mock.patch("ui.settings_dialog.show_free_channels_menu") as shown:
            dialog.free_channels_button.click()
        (parent, point, choose), _ = shown.call_args
        self.assertIs(parent, dialog)
        self.assertIsInstance(point, QPoint)
        choose("uk")
        entry = free_channels.provider_entry("uk")
        self.assertEqual(dialog.providers[-1], entry)
        self.assertEqual(dialog.provider_combo.currentIndex(), len(dialog.providers) - 1)
        self.assertEqual(dialog.url_input.text(), entry["url"])
        self.assertEqual(len(self.provider_manager.providers), 1)  # nothing saved yet
        choose("uk")  # listed already: shown, not added again
        self.assertEqual(len(dialog.providers), 2)
        choose(None)
        self.assertEqual(dialog.providers[-1], free_channels.provider_entry(None))
        with mock.patch.object(self.window.catalog, "select_provider"):
            dialog.save_settings()
        self.assertEqual(
            self.provider_manager.providers[1:], [entry, free_channels.provider_entry(None)]
        )


class DrawerRowTests(WindowCase):
    def test_the_row_opens_the_menu_at_the_row(self):
        self.show()
        drawer = self.wait_for_drawer()
        drawer.open_drawer(focus_side="left")
        with mock.patch("ui.tv_window.show_free_channels_menu") as shown:
            self.window.menus.on_drawer_action(FREE_CHANNELS_ROW)
        (parent, point, _choose), _ = shown.call_args
        self.assertIs(parent, self.window)
        self.assertEqual(point, drawer.row_anchor(FREE_CHANNELS_ROW))


if __name__ == "__main__":
    unittest.main()
