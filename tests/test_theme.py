import unittest

from ui.theme import FOCUS_FILL_ALPHA, PALETTE, RADIUS_CARD, RADIUS_PANEL, qcolor


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


if __name__ == "__main__":
    unittest.main()
