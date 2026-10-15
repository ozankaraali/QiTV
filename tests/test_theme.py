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


if __name__ == "__main__":
    unittest.main()
