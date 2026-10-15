import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QRectF, Qt
from PySide6.QtGui import QFont, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from ui.overlays.painting import Units
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
            "Add your IPTV provider (STB portal, Xtream or M3U playlist) to start watching.",
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
        QTest.qWait(10)
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

    def test_enter_activates_the_button_only_while_shown(self):
        self.assertFalse(self.card.activate())
        self.card.show_card()
        self.assertTrue(self.card.activate())
        self.assertEqual(self.added, [True])

    def test_dismiss_hides_it(self):
        self.card.show_card()
        self.card.dismiss()
        self.assertFalse(self.card.isVisible())


if __name__ == "__main__":
    unittest.main()
