"""Help → Keyboard Shortcuts at its opening size: no action is cut short."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtWidgets import QApplication, QStyle

from ui.shortcuts_dialog import MIN_WIDTH, ShortcutsDialog


class ShortcutsDialogLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def dialog(self, point_scale=1.0):
        font = QApplication.font()
        self.addCleanup(QApplication.setFont, QFont(font))
        larger = QFont(font)
        larger.setPointSizeF(font.pointSizeF() * point_scale)
        QApplication.setFont(larger)
        dialog = ShortcutsDialog()
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(dialog.done, 0)
        dialog.show()  # offscreen: nothing shows on a screen
        QApplication.processEvents()
        return dialog

    def assert_whole(self, dialog):
        table = dialog.table
        metrics = QFontMetrics(table.font())
        style = table.style()
        margin = style.pixelMetric(QStyle.PixelMetric.PM_FocusFrameHMargin, None, table) + 1
        width = table.columnWidth(1) - 2 * margin  # the text's room, as the cell lays it out
        for row, (_keys, what, heading) in enumerate(dialog.rows):
            if heading:
                continue
            needed = metrics.boundingRect(QRect(0, 0, width, 10_000), Qt.TextWordWrap, what)
            with self.subTest(action=what[:40]):
                self.assertGreaterEqual(table.rowHeight(row), needed.height())

    def test_every_action_shows_whole(self):
        # "Picture: aspect ratio, deinterlacing, sleep timer; press…" was cut at 560 px.
        for scale in (1.0, 1.4):
            with self.subTest(scale=scale):
                dialog = self.dialog(scale)
                screen = dialog.screen().availableGeometry()
                self.assertGreaterEqual(dialog.width(), MIN_WIDTH)
                self.assertLessEqual(dialog.width(), screen.width())
                self.assert_whole(dialog)

    def test_a_narrower_window_wraps_the_long_actions(self):
        dialog = self.dialog()
        dialog.resize(MIN_WIDTH, dialog.height())
        QApplication.processEvents()
        self.assert_whole(dialog)


if __name__ == "__main__":
    unittest.main()
