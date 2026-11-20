"""Free channels (iptv-org's lists) as a layer of Settings › Providers (2.13.1: no menu over the
picture, no system dialog): All countries, the system's region, then every country A–Z in the
active language, found by typing inside it. Opened from Settings, the welcome card, the channel
list's row, the Add card and the menu bar's Another country…; a choice adds the list as before
(selected, saved and loaded): in Settings opened for it, Settings goes; browsed to, Providers
shows with the list's row focused."""

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from timer_fixtures import elapse, wait_until

from services import free_channels, i18n
from services.navigation import FREE_CHANNELS_ROW
from services.settings_model import ACTION, FREE_CHANNELS, PROVIDERS
from tests.fixtures.local_provider import PROVIDER_NAME
from tests.test_tv_window import WindowCase
from ui import free_channels_menu, provider_cards as pc
from ui.free_channels_menu import ALL_TEXT, OTHER_TEXT, layer_rows, quick_choices
from ui.keymap import Action
from ui.overlays.settings_layout import Item, title_of
from ui.settings_providers import layer_key

ALL = f"{FREE_CHANNELS}/@all"


def titles(rows):
    return [title_of(Item(row.key, row.text, row=row)) for row in rows]


class LayerRowsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_all_countries_the_region_then_every_country(self):
        rows = layer_rows(FREE_CHANNELS, territory="GB")
        self.assertEqual(
            titles(rows[:3]), [ALL_TEXT, "United Kingdom (your region)", "Afghanistan"]
        )
        self.assertEqual(
            [row.key for row in rows[:3]],
            [ALL, f"{FREE_CHANNELS}/@region-uk", f"{FREE_CHANNELS}/af"],
        )
        self.assertEqual(len(rows), 2 + len(free_channels.COUNTRIES))
        self.assertEqual(len({row.key for row in rows}), len(rows))  # a key each
        self.assertEqual({row.kind for row in rows}, {ACTION})
        self.assertEqual([row.key for row in rows if row.help], [])  # found by their names
        self.assertEqual(titles(rows[2:]), [name for _code, name in free_channels.COUNTRIES])

    def test_an_unknown_region_has_no_row(self):
        for territory in ("ZZ", ""):
            with self.subTest(territory=territory):
                self.assertEqual(
                    titles(layer_rows(FREE_CHANNELS, territory=territory)[:2]),
                    [ALL_TEXT, "Afghanistan"],
                )

    def test_the_region_comes_from_the_system_locale(self):
        with mock.patch.object(free_channels_menu, "system_territory", return_value="TR"):
            rows = layer_rows(FREE_CHANNELS)
        self.assertEqual(titles(rows)[1], "Türkiye (your region)")

    def test_names_in_the_active_language_in_its_order(self):
        self.assertEqual(i18n.set_language("tr"), "tr")
        self.addCleanup(i18n.set_language, "en")
        names = titles(layer_rows(FREE_CHANNELS, territory="DE"))
        self.assertEqual(names[:2], ["Tüm ülkeler", "Almanya (bölgeniz)"])
        countries = names[2:]
        self.assertLess(countries.index("Almanya"), countries.index("Andorra"))
        self.assertLess(countries.index("Cezayir"), countries.index("Çad"))
        self.assertLess(countries.index("Çad"), countries.index("Danimarka"))

    def test_the_menu_bars_quick_choices(self):
        self.assertEqual(
            quick_choices(territory="GB"),
            [(ALL_TEXT, None), ("United Kingdom (your region)", "uk")],
        )
        self.assertEqual(quick_choices(territory="ZZ"), [(ALL_TEXT, None)])


class LayerCase(WindowCase):
    """The window, its system with no region of its own unless a test gives one."""

    territory = ""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(
            free_channels_menu, "system_territory", return_value=self.territory
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def hold_setups(self):
        """From here on a choice selects its list, and nothing loads (`select`)."""
        patcher = mock.patch.object(self.window.catalog, "select_provider")
        self.select = patcher.start()
        self.addCleanup(patcher.stop)

    def start(self):
        """The window shown, its list loaded and open; then the setups held."""
        self.show()
        drawer = self.wait_for_drawer()
        self.hold_setups()
        return drawer

    @property
    def sd(self):
        return self.window.settings_drawer

    def key(self, action, payload=""):
        self.window.controller.handle(action, payload)

    def type(self, text):
        for char in text:
            self.key(Action.NAV_TEXT, char)

    def assert_layer(self, layer=FREE_CHANNELS):
        self.assertTrue(self.window.settings.visible())
        self.assertEqual(self.sd.path(), (PROVIDERS, layer))
        self.assertEqual(self.sd.focus_side(), "right")
        self.assertEqual(self.sd.focused_row().key, f"{layer}/@all")


class NoProviderCase(LayerCase):
    def provider_list(self):
        return []

    def start_with_card(self):
        self.show()
        self.card = self.window.overlays.welcome
        self.assertTrue(wait_until(self.card.isVisible))
        self.hold_setups()

    def click(self, rect):
        QTest.mouseClick(self.card, Qt.MouseButton.LeftButton, pos=rect.center().toPoint())


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

    def test_the_cards_button_opens_the_countries_and_a_choice_closes_settings(self):
        self.start_with_card()
        self.click(self.card.free_button_rect())
        self.assert_layer()
        self.assertFalse(self.card.isVisible())  # it waits under Settings
        self.select.assert_not_called()  # nothing added until a choice
        self.type("turkiye")  # folded as the channel list's find folds it
        self.assertEqual(self.sd.focused_row().key, f"{FREE_CHANNELS}/tr")
        self.key(Action.NAV_ACTIVATE)  # to the row
        self.assertFalse(self.sd.finding())
        self.assertEqual(self.sd.focused_row().key, f"{FREE_CHANNELS}/tr")
        self.key(Action.NAV_ACTIVATE)  # chosen
        entry = free_channels.provider_entry("tr")
        self.assertEqual(self.provider_manager.providers, [entry])
        self.assertEqual(self.config.selected_provider_name, entry["name"])
        self.select.assert_called_once_with(None, force_refresh=False)
        self.assertFalse(self.window.settings.visible())  # Settings opened for it went
        self.assertFalse(self.card.isVisible())

    def test_backing_out_gives_the_card_back(self):
        self.start_with_card()
        self.click(self.card.free_button_rect())
        self.assert_layer()
        for _ in range(3):  # the layer, the rows, the sections
            self.key(Action.ESCAPE)
        self.assertFalse(self.window.settings.visible())
        self.assertTrue(wait_until(self.card.isVisible))
        self.assertEqual(self.provider_manager.providers, [])


class AddFlowTests(LayerCase):
    def setUp(self):
        super().setUp()
        self.hold_setups()

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


class OldAddressTests(LayerCase):
    def setUp(self):
        super().setUp()
        self.hold_setups()

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


class SettingsLayerTests(LayerCase):
    territory = "GB"

    def open_layer(self):
        self.start()
        self.window.open_settings(section_key=PROVIDERS)
        self.key(Action.NAV_RIGHT)
        for _ in range(8):
            if self.sd.focused_row().key == FREE_CHANNELS:
                break
            self.key(Action.NAV_DOWN)
        self.key(Action.NAV_ACTIVATE)
        self.assert_layer()

    def test_browsed_to_a_choice_shows_the_new_list_in_providers(self):
        self.open_layer()
        crumbs = [text for text, _rect in self.sd._layout().crumbs]
        self.assertEqual(crumbs[-1], "Free channels")
        self.key(Action.NAV_DOWN)
        self.assertEqual(self.sd.focused_row().key, f"{FREE_CHANNELS}/@region-uk")
        self.key(Action.NAV_ACTIVATE)
        entry = free_channels.provider_entry("uk")
        self.assertEqual(self.provider_manager.providers[-1], entry)
        self.assertEqual(self.config.selected_provider_name, entry["name"])
        self.select.assert_called_once_with(None, force_refresh=False)
        self.assertTrue(self.window.settings.visible())  # the viewer stays in Settings
        self.assertEqual(self.sd.path(), (PROVIDERS,))
        self.assertEqual(self.sd.focused_row().key, layer_key(entry["name"]))
        self.assertEqual(self.sd.focused_row().sub, "M3U playlist · in use")

    def test_a_list_already_there_is_switched_to_and_shown(self):
        self.window.add_free_channels("us")
        count = len(self.provider_manager.providers)
        self.window.add_free_channels("fr")  # another one shows
        self.open_layer()
        self.assertTrue(self.window.settings.show_row(f"{FREE_CHANNELS}/us"))
        self.key(Action.NAV_ACTIVATE)
        self.assertEqual(len(self.provider_manager.providers), count + 1)  # no new one
        self.assertEqual(self.config.selected_provider_name, "iptv-org: United States")
        self.assertEqual(self.sd.path(), (PROVIDERS,))
        self.assertEqual(self.sd.focused_row().key, layer_key("iptv-org: United States"))

    def test_a_double_click_on_a_country_adds_it_once(self):
        self.open_layer()
        self.assertTrue(self.window.settings.show_row(f"{FREE_CHANNELS}/de"))
        self.sd.grab()
        index = [row.key for row in self.sd._right_rows()].index(f"{FREE_CHANNELS}/de")
        point = QPointF(self.sd.title_rect(index).center().toPoint())
        count = len(self.provider_manager.providers)
        QTest.mouseClick(self.sd, Qt.MouseButton.LeftButton, pos=point.toPoint())
        event = QMouseEvent(
            QEvent.Type.MouseButtonDblClick,
            point,
            QPointF(self.sd.mapToGlobal(point)),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(self.sd, event)
        self.assertEqual(len(self.provider_manager.providers), count + 1)
        self.assertEqual(self.sd.path(), (PROVIDERS,))  # the new list's layer not opened
        self.assertEqual(self.sd.focused_row().key, layer_key("iptv-org: Germany"))
        elapse(QApplication.doubleClickInterval() + 80)

    def test_the_add_cards_free_channels_opens_the_countries(self):
        self.start()
        self.window.add_provider()  # Settings opened for the card
        self.window.provider_cards._on_pressed(pc.FREE)
        self.assertFalse(self.window.provider_cards.form.isVisible())
        self.assert_layer()
        self.key(Action.NAV_ACTIVATE)  # All countries
        self.assertEqual(self.provider_manager.providers[-1], free_channels.provider_entry(None))
        self.assertFalse(self.window.settings.visible())  # as the card's Settings would


class ProvidersMenuTests(LayerCase):
    """The menu bar's Providers menu (the system's own menu) adds providers too: the welcome
    card rarely shows (the last provider stays). Another country… opens the countries."""

    territory = "GB"

    def entries(self):
        menu = self.window.menus.providers_menu
        return {action.text(): action for action in menu.actions() if action.text()}

    def test_free_channels_and_add_provider_follow_the_providers(self):
        self.window.menus.rebuild_providers()  # with the region given
        menu = self.window.menus.providers_menu
        texts = [action.text() for action in menu.actions()]
        self.assertEqual(texts[0], PROVIDER_NAME)
        self.assertEqual(texts[-3:-1], ["Add provider", "Free channels"])  # then Free radio
        free = self.entries()["Free channels"].menu()
        self.assertEqual(free.styleSheet(), "")  # the menu bar's own look
        self.assertEqual(
            [a.text() for a in free.actions()],
            [ALL_TEXT, "United Kingdom (your region)", OTHER_TEXT],
        )
        with mock.patch.object(self.window, "add_free_channels") as add:
            free.actions()[0].trigger()
            free.actions()[1].trigger()
        self.assertEqual(add.call_args_list, [mock.call(None), mock.call("uk")])
        with mock.patch.object(self.window, "add_provider") as add_provider:
            self.entries()["Add provider"].trigger()
        add_provider.assert_called_once_with()

    def test_another_country_opens_the_countries(self):
        self.start()
        free = self.entries()["Free channels"].menu()
        free.actions()[-1].trigger()
        self.assert_layer()
        self.key(Action.NAV_DOWN)
        self.key(Action.NAV_DOWN)  # past the region: Afghanistan
        self.assertEqual(self.sd.focused_row().key, f"{FREE_CHANNELS}/af")
        self.key(Action.NAV_ACTIVATE)
        self.assertEqual(self.provider_manager.providers[-1], free_channels.provider_entry("af"))
        self.assertFalse(self.window.settings.visible())  # opened for it: it goes

    def test_they_stay_after_a_rebuild(self):
        self.window.menus.rebuild_providers()
        self.window.menus.rebuild_providers()
        texts = [action.text() for action in self.window.menus.providers_menu.actions()]
        self.assertEqual(texts.count("Free channels"), 1)


class DrawerRowTests(LayerCase):
    def to_row(self):
        """The channel list's sources, on its Free channels row; Enter acts on it."""
        drawer = self.start()
        for key in (Qt.Key.Key_Left, Qt.Key.Key_Left):
            QTest.keyClick(self.window, key)  # the sources column
        for _ in range(3):  # ⚙, ＋, ＋ free channels
            QTest.keyClick(self.window, Qt.Key.Key_Down)
        self.assertEqual(drawer.highlighted().key, FREE_CHANNELS_ROW)
        QTest.keyClick(self.window, Qt.Key.Key_Return)
        return drawer

    def test_the_row_opens_the_countries_and_a_choice_gives_the_list_back(self):
        drawer = self.to_row()
        self.assert_layer()
        self.assertFalse(drawer.isVisible())
        self.key(Action.NAV_ACTIVATE)  # All countries
        self.assertEqual(self.provider_manager.providers[-1], free_channels.provider_entry(None))
        self.assertEqual(self.config.selected_provider_name, free_channels.ALL_NAME)
        self.assertFalse(self.window.settings.visible())
        self.assertTrue(wait_until(drawer.isVisible))  # as backing out of it gives it back

    def test_a_double_click_adds_once_and_its_second_press_reaches_nothing(self):
        # Its first press closes Settings and the list comes back under the pointer: the
        # second press lands on nothing there (Settings' guard), and the list stays where it
        # was.
        drawer = self.to_row()
        self.assert_layer()
        self.sd.grab()
        point = QPointF(self.sd.title_rect(0).center().toPoint())  # All countries
        where = self.sd.mapToGlobal(point.toPoint())
        count = len(self.provider_manager.providers)
        QTest.mouseClick(self.sd, Qt.MouseButton.LeftButton, pos=point.toPoint())
        self.assertFalse(self.window.settings.visible())
        self.assertTrue(wait_until(drawer.isVisible))
        under = QApplication.widgetAt(where) or drawer
        event = QMouseEvent(
            QEvent.Type.MouseButtonDblClick,
            QPointF(under.mapFromGlobal(where)),
            QPointF(where),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(under, event)
        self.assertEqual(len(self.provider_manager.providers), count + 1)
        self.assertTrue(drawer.isVisible())
        self.assertEqual(drawer.highlighted().key, FREE_CHANNELS_ROW)
        self.assertFalse(self.window.settings.visible())
        elapse(QApplication.doubleClickInterval() + 80)

    def test_backing_out_gives_the_list_back_where_it_was(self):
        drawer = self.to_row()
        self.assert_layer()
        for _ in range(3):
            self.key(Action.ESCAPE)
        self.assertFalse(self.window.settings.visible())
        self.assertTrue(wait_until(drawer.isVisible))
        self.assertEqual(drawer.highlighted().key, FREE_CHANNELS_ROW)
        self.select.assert_not_called()


if __name__ == "__main__":
    unittest.main()
