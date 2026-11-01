import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QSlider,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ui.theme import FOCUS_FILL_ALPHA, PALETTE, RADIUS_CARD, RADIUS_PANEL, focus_qss, qcolor


class ThemeTests(unittest.TestCase):
    def test_spec_values(self):
        self.assertEqual(PALETTE.canvas, "#0b0f14")
        self.assertEqual(PALETTE.panel, "rgba(8,11,15,0.86)")
        self.assertEqual(PALETTE.panel_raised, "#0e1217")
        self.assertEqual(PALETTE.text, "#eef2f6")
        self.assertEqual(PALETTE.accent, "#e08a5f")
        self.assertEqual(PALETTE.brand, "#C96B43")
        self.assertEqual(PALETTE.live, "#d6453a")
        self.assertEqual(PALETTE.ok, "#5fbf7a")
        self.assertEqual(PALETTE.warn, "#e0b24a")
        self.assertAlmostEqual(PALETTE.text_muted_alpha, 0.62)
        self.assertEqual((RADIUS_CARD, RADIUS_PANEL), (10, 14))
        self.assertAlmostEqual(FOCUS_FILL_ALPHA, 0.22)

    def test_hex(self):
        color = qcolor("#e08a5f")
        self.assertEqual(color.name(), "#e08a5f")
        self.assertEqual(color.alpha(), 255)

    def test_rgba(self):
        color = qcolor(PALETTE.panel)
        self.assertEqual((color.red(), color.green(), color.blue()), (8, 11, 15))
        self.assertEqual(color.alpha(), round(0.86 * 255))

    def test_alpha_override(self):
        self.assertAlmostEqual(qcolor(PALETTE.text, 0.62).alphaF(), 0.62, places=2)

    def test_invalid(self):
        with self.assertRaises(ValueError):
            qcolor("not-a-colour")

    def test_css_eight_digit_hex_is_rrggbbaa(self):
        color = qcolor("#11223380")
        self.assertEqual(
            (color.red(), color.green(), color.blue(), color.alpha()), (17, 34, 51, 128)
        )

    def test_css_four_digit_hex_is_rgba(self):
        color = qcolor("#1238")
        self.assertEqual(
            (color.red(), color.green(), color.blue(), color.alpha()), (17, 34, 51, 136)
        )

    def test_short_hex(self):
        self.assertEqual(qcolor("#fff").name(), "#ffffff")

    def test_bad_hex_lengths_rejected(self):
        for bad in ("#12345", "#1234567", "#ggg", "#"):
            with self.assertRaises(ValueError, msg=bad):
                qcolor(bad)

    def test_rgba_components_validated(self):
        for bad in ("rgba(300,0,0,1)", "rgba(0,0,0,1.5)"):
            with self.assertRaises(ValueError, msg=bad):
                qcolor(bad)

    def test_alpha_override_validated(self):
        for bad in (-0.1, 1.1):
            with self.assertRaises(ValueError):
                qcolor("#ffffff", bad)


class FocusStyleTests(unittest.TestCase):
    """The keyboard's focus shows in Settings and the dialogs: the dark theme takes the native
    focus ring away and outlines only combo boxes and text fields (main.py adds focus_qss)."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_each_kind_of_control_changes_when_focused(self):
        import qdarktheme

        page = QWidget()  # its own style sheet: other tests' widgets keep the app's
        page.setStyleSheet(
            qdarktheme.load_stylesheet("dark", custom_colors={"primary": PALETTE.brand})
            + focus_qss()
        )
        self.addCleanup(page.deleteLater)
        self.addCleanup(page.close)
        layout = QVBoxLayout(page)
        tabs = QTabWidget()
        tabs.addTab(QWidget(), "Settings")
        tabs.addTab(QWidget(), "Providers")
        controls = {
            "tab": tabs.tabBar(),
            "button": QPushButton("Clear image cache"),
            "check": QCheckBox("Show channel logos"),
            "radio": QRadioButton("M3U Playlist"),
            "slider": QSlider(Qt.Orientation.Horizontal),
        }
        layout.addWidget(tabs)
        for name, widget in controls.items():
            if name != "tab":
                layout.addWidget(widget)
        elsewhere = QLineEdit()
        layout.addWidget(elsewhere)
        page.resize(420, 360)
        page.show()
        page.activateWindow()
        QTest.qWaitForWindowActive(page, 2000)
        for name, widget in controls.items():
            with self.subTest(name):
                elsewhere.setFocus()
                QApplication.processEvents()
                before = widget.grab().toImage()
                widget.setFocus(Qt.FocusReason.TabFocusReason)
                QApplication.processEvents()
                self.assertTrue(widget.hasFocus())
                self.assertNotEqual(widget.grab().toImage(), before)

    def test_a_button_keeps_its_size(self):
        """2 px of ring where the theme has 1 px of border: the padding gives up the other."""
        qss = focus_qss()
        self.assertIn(
            f"QPushButton:focus {{border: 2px solid {PALETTE.accent}; padding: 3px 7px}}", qss
        )


if __name__ == "__main__":
    unittest.main()
