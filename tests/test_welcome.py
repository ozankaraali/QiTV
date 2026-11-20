"""The welcome card (spec §8): its look, and, in the TV window with no provider yet, its Add
provider opens the Add card in Settings over the empty canvas, whose Cancel gives the card
back (plan 2)."""

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QRectF, Qt
from PySide6.QtGui import QFont, QGuiApplication, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from timer_fixtures import elapse, pause, wait_until

from services.config_manager import ConfigManager
from services.provider_check import Outcome
from services.settings_model import ADD_PROVIDER, PROVIDERS
from tests.test_tv_window import WindowCase
from ui.keymap import Action
from ui.overlays.painting import Units
from ui.overlays.settings_card import CANCEL
from ui.overlays.welcome import (
    BODY_OPACITY,
    BODY_TEXT,
    BODY_U,
    BUTTON_TEXT,
    TITLE_TEXT,
    TITLE_U,
    WIDTH_U,
    WelcomeCard,
)


class WelcomeCardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.host = QWidget()
        self.host.resize(1280, 720)
        self.addCleanup(self.host.close)
        self.card = WelcomeCard(self.host)
        self.host.show()
        self.added = []
        self.card.addRequested.connect(lambda: self.added.append(True))

    def test_the_spec_texts_and_sizes(self):
        self.assertEqual(TITLE_TEXT, "Welcome to QiTV")
        self.assertEqual(
            BODY_TEXT,
            "Add your IPTV provider (STB portal, Xtream or M3U playlist) or your Jellyfin or Emby"
            " server to start watching.",
        )
        self.assertEqual(BUTTON_TEXT, "Add provider")
        self.assertEqual((WIDTH_U, TITLE_U, BODY_U, BODY_OPACITY), (44.0, 2.4, 1.2, 0.7))
        title = Units(1280).font(TITLE_U, QFont.Weight.Bold)
        self.assertEqual(title.pixelSize(), round(2.4 * 12.8))

    def test_hidden_until_shown_then_centred_and_44_units_wide(self):
        self.assertFalse(self.card.isVisible())
        self.card.show_card()
        self.assertTrue(self.card.isVisible())
        geometry = self.card.geometry()
        self.assertEqual(geometry.width(), Units(1280).px(WIDTH_U))
        self.assertAlmostEqual(geometry.center().x(), 640, delta=1)
        self.assertAlmostEqual(geometry.center().y(), 360, delta=1)
        self.assertEqual(self.card.mask_region().boundingRect(), geometry)
        self.assertFalse(self.card.grab().isNull())

    def test_the_logo_sits_above_the_title(self):
        self.card.show_card()
        logo, title = self.card.logo_rect(), self.card.title_rect()
        self.assertGreater(logo.height(), 0)
        self.assertLess(logo.bottom(), title.top())
        self.assertAlmostEqual(logo.center().x(), self.card.width() / 2, delta=1)
        self.assertTrue(QRectF(self.card.rect()).contains(logo))

    def test_the_logo_is_made_for_the_cards_own_screen(self):
        self.card.show_card()
        drawn = []
        draw_pixmap = QPainter.drawPixmap

        def pixmap(painter, *args):
            drawn.append(args[1])
            return draw_pixmap(painter, *args)

        with (
            mock.patch.object(self.card, "devicePixelRatioF", return_value=2.0),
            mock.patch.object(QPainter, "drawPixmap", pixmap),
        ):
            self.card.grab()
        self.assertEqual([p.devicePixelRatio() for p in drawn], [2.0])

    def test_it_follows_the_host_size(self):
        self.card.show_card()
        self.host.resize(800, 600)
        pause(10)
        self.assertEqual(self.card.width(), Units(800).px(WIDTH_U))
        self.assertAlmostEqual(self.card.geometry().center().x(), 400, delta=1)

    def test_the_button_asks_to_add_a_provider(self):
        self.card.show_card()
        button = self.card.button_rect()
        self.assertTrue(self.card.rect().contains(button.toRect()))
        QTest.mouseClick(self.card, Qt.MouseButton.LeftButton, pos=QPoint(10, 10))
        self.assertEqual(self.added, [])  # the card itself: nothing
        QTest.mouseClick(self.card, Qt.MouseButton.LeftButton, pos=button.center().toPoint())
        self.assertEqual(self.added, [True])

    def test_the_free_channels_button_sits_beside_the_first_and_asks_with_a_position(self):
        self.card.show_card()
        first, free = self.card.button_rect(), self.card.free_button_rect()
        self.assertFalse(first.intersects(free))
        self.assertLess(first.right(), free.left())
        self.assertEqual((first.top(), first.height()), (free.top(), free.height()))
        self.assertGreaterEqual(free.height(), 28)
        self.assertTrue(QRectF(self.card.rect()).contains(free))
        pair_centre = (first.left() + free.right()) / 2
        self.assertAlmostEqual(pair_centre, self.card.width() / 2, delta=1)
        asked = []
        self.card.freeRequested.connect(asked.append)
        QTest.mouseClick(self.card, Qt.MouseButton.LeftButton, pos=free.center().toPoint())
        self.assertEqual(self.added, [])  # not Add provider
        below = self.card.mapToGlobal(free.bottomLeft().toPoint())
        self.assertEqual(asked, [below])

    def test_both_buttons_fit_the_card_at_small_widths(self):
        self.host.resize(320, 240)
        self.card.show_card()
        card = QRectF(self.card.rect())
        self.assertTrue(card.contains(self.card.button_rect()))
        self.assertTrue(card.contains(self.card.free_button_rect()))
        self.assertGreaterEqual(self.card.button_rect().height(), 28)
        self.assertFalse(self.card.button_rect().intersects(self.card.free_button_rect()))

    def test_enter_activates_the_button_only_while_shown(self):
        self.assertFalse(self.card.activate())
        self.card.show_card()
        self.assertTrue(self.card.activate())
        self.assertEqual(self.added, [True])

    def test_dismiss_hides_it(self):
        self.card.show_card()
        self.card.dismiss()
        self.assertFalse(self.card.isVisible())


class WelcomeAddTests(WindowCase):
    """No provider yet: the card's Add provider opens Settings at Providers with the Add card
    over the empty canvas; its Cancel gives the welcome card back."""

    LINK = "http://127.0.0.1:9/list.m3u"  # a closed port of the loopback

    def provider_list(self):
        return []

    def setUp(self):
        super().setUp()
        patch = mock.patch("ui.tv_window.OptionsDialog")  # never the Settings window's form
        self.dialog = patch.start()
        self.addCleanup(patch.stop)

    def tearDown(self):
        elapse(QApplication.doubleClickInterval() + 80)  # no second-click guard left armed
        super().tearDown()

    def start(self):
        self.show()
        card = self.window.overlays.welcome
        self.assertTrue(wait_until(card.isVisible))
        return card

    def click_add(self, card):
        QTest.mouseClick(card, Qt.MouseButton.LeftButton, pos=card.button_rect().center().toPoint())

    def assert_the_add_card(self, card):
        window, form = self.window, self.window.provider_cards.form
        self.assertTrue(window.settings.visible())
        self.assertEqual(window.settings_drawer.path(), (PROVIDERS,))
        self.assertTrue(form.isVisible())
        self.assertEqual(form.title(), "Add a provider")
        self.assertTrue(card.isHidden())  # over the empty canvas, the welcome card under it
        self.assertFalse(window.overlays.drawer.isVisible())
        self.dialog.assert_not_called()

    def test_cancel_with_no_provider_goes_back_to_the_welcome_card(self):
        card = self.start()
        window, form = self.window, self.window.provider_cards.form
        self.click_add(card)
        self.assert_the_add_card(card)
        QTest.keyClick(window, Qt.Key.Key_Escape)  # the card's Cancel
        self.assertFalse(form.isVisible())
        self.assertFalse(window.settings.visible())
        self.assertTrue(card.isVisible())
        QTest.keyClick(window, Qt.Key.Key_Return)  # OK on a remote: the card's button
        self.assert_the_add_card(card)
        cancel = form.control_rect("button", CANCEL).center().toPoint()
        QTest.mouseClick(form, Qt.MouseButton.LeftButton, pos=cancel)  # its Cancel button
        self.assertFalse(form.isVisible())
        self.assertFalse(window.settings.visible())
        self.assertTrue(card.isVisible())
        self.assertEqual(self.provider_manager.providers, [])

    def test_settings_own_add_row_cancels_back_into_settings(self):
        # Settings opened by itself: Cancel on its ＋ Add provider stays in it, and backing out
        # of it gives the welcome card back.
        card = self.start()
        window, form = self.window, self.window.provider_cards.form
        self.menu_actions()["Settings…"].trigger()
        drawer = window.settings_drawer
        for _ in range(12):
            if drawer.path() == (PROVIDERS,):
                break
            window.controller.handle(Action.NAV_DOWN)
        window.controller.handle(Action.NAV_RIGHT)
        self.assertEqual(drawer.focused_row().key, ADD_PROVIDER)  # no provider: the first row
        window.controller.handle(Action.NAV_ACTIVATE)
        self.assertTrue(form.isVisible())
        QTest.keyClick(window, Qt.Key.Key_Escape)  # the card's Cancel
        self.assertFalse(form.isVisible())
        self.assertTrue(window.settings.visible())
        self.assertTrue(card.isHidden())
        for _ in range(2):  # rows → sections → closed
            QTest.keyClick(window, Qt.Key.Key_Escape)
        self.assertFalse(window.settings.visible())
        self.assertTrue(card.isVisible())

    def test_a_provider_added_from_the_welcome_card_is_selected_and_said(self):
        card = self.start()
        window, form = self.window, self.window.provider_cards.form
        self.click_add(card)
        self.assert_the_add_card(card)
        QGuiApplication.clipboard().setText(self.LINK)
        window.controller.handle(Action.PASTE)
        with (
            mock.patch("services.provider_check.check", return_value=Outcome(True)),
            mock.patch.object(window.catalog, "select_provider") as select,
        ):
            QTest.keyClick(window, Qt.Key.Key_Return)  # Check and add
            self.assertTrue(wait_until(lambda: not form.isVisible()))
        self.assertIn(("Added 127.0.0.1 to your providers", "info"), self.toasts)
        self.assertEqual(ConfigManager().selected_provider_name, "127.0.0.1")
        select.assert_called_once_with(None, force_refresh=False)
        self.assertEqual([p["name"] for p in self.provider_manager.providers], ["127.0.0.1"])
        self.assertTrue(window.settings.visible())  # on Providers, the new one listed
        QTest.keyClick(window, Qt.Key.Key_Escape)  # out of Settings
        self.assertFalse(window.settings.visible())
        self.assertTrue(card.isHidden())  # a provider now: no welcome card


if __name__ == "__main__":
    unittest.main()
