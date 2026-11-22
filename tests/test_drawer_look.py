"""The drawer's look, shared (Settings in the drawer, plan 1): the drawer takes its metrics
from `drawer_look`, and `paint_value_steps` draws a setting's ‹ value ›."""

import os
from types import SimpleNamespace
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QFont, QImage, QPainter
from PySide6.QtWidgets import QApplication, QWidget

from services.navigation import NavModel
from services.settings_model import sections
from tests.test_navigation import FakeCatalog
from ui.overlays import drawer, drawer_look
from ui.overlays.painting import paint_value_steps, ui_units
from ui.overlays.settings_drawer import SettingsDrawer

BACKGROUND = QColor(20, 20, 20)


class DrawerLookTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_the_drawer_uses_the_shared_metrics(self) -> None:
        self.assertIs(drawer.column_widths, drawer_look.column_widths)  # LEFT_U, RIGHT_U
        self.assertEqual(drawer_look.RIGHT_U, 58.0)
        # The header's metrics live in the shared look too (the drawer still offers them).
        for name in ("FILTER_H_U", "GEAR_U", "GEAR_GAP_U", "MIN_HIT_PX", "SEPARATOR"):
            self.assertIs(getattr(drawer, name), getattr(drawer_look, name), name)

    def test_column_widths_match_the_drawer(self) -> None:
        # Settings shows its columns where the channel list has its own, at every size.
        for width, height in ((640, 360), (1280, 720), (1366, 768), (1920, 1080), (3840, 2160)):
            with self.subTest(width=width):
                host = QWidget()
                host.resize(width, height)
                host.show()
                self.addCleanup(host.deleteLater)
                self.addCleanup(host.close)
                nav = NavModel(FakeCatalog(), SimpleNamespace(favorites=[], watch_history=[]))
                channels = drawer.Drawer(nav, None, lambda item: None, host)
                channels._relayout()
                settings = SettingsDrawer(lambda row: "", lambda row: False, host)
                settings.open_at(sections("Darwin"))
                units = ui_units(width)
                left_w, right_w = drawer_look.column_widths(units)
                for side in ("left", "right"):
                    self.assertEqual(
                        QRectF(channels._columns[side].rect), settings.column_rect(side), side
                    )
                self.assertEqual(channels._columns["left"].rect.width(), left_w)
                self.assertEqual(channels._columns["right"].rect.width(), right_w)
                self.assertEqual(channels.width(), settings.width())

    def _paint(self, units, rects, lit: bool) -> QImage:
        less, text, more = rects
        image = QImage(300, 40, QImage.Format_ARGB32)
        image.fill(BACKGROUND)
        p = QPainter(image)
        p.setFont(QFont("Sans", 12))
        paint_value_steps(p, units, less, text, more, "16:9", lit)
        p.end()
        return image

    @staticmethod
    def _changed(image: QImage, rect: QRectF) -> bool:
        """Whether any pixel in `rect` differs from the background."""
        for y in range(int(rect.top()), int(rect.bottom())):
            for x in range(int(rect.left()), int(rect.right())):
                if image.pixelColor(x, y) != BACKGROUND:
                    return True
        return False

    @staticmethod
    def _pixels(image: QImage, rect: QRectF) -> list[int]:
        return [
            image.pixel(x, y)
            for y in range(int(rect.top()), int(rect.bottom()))
            for x in range(int(rect.left()), int(rect.right()))
        ]

    def test_value_steps_paint_the_chevrons_and_light_the_value(self) -> None:
        units = ui_units(1280)
        less = QRectF(10, 8, 24, 24)
        text = QRectF(40, 0, 220, 40)
        more = QRectF(266, 8, 24, 24)
        lit = self._paint(units, (less, text, more), True)
        dim = self._paint(units, (less, text, more), False)
        self.assertTrue(self._changed(lit, less))
        self.assertTrue(self._changed(lit, more))
        self.assertNotEqual(self._pixels(lit, text), self._pixels(dim, text))


if __name__ == "__main__":
    unittest.main()
