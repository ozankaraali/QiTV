"""The macOS disk image opens as a window drawn for dragging QiTV onto Applications: the image's
settings put each icon on the card the background draws for it, the background comes at 1x and
2x, Finder's names read on it in light and dark mode, and both macOS builds make it so."""

from pathlib import Path
import unittest

from PySide6.QtGui import QColor, QImage

from scripts import make_dmg
from scripts.draw_dmg_background import arrow_ends, card_rect, plate_rect
from ui.theme import PALETTE, qcolor

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "main.yml"
RETINA = make_dmg.BACKGROUND.with_name("background@2x.png")  # the name dmgbuild looks for


def luminance(color: QColor) -> float:
    """WCAG's relative luminance."""

    def linear(channel: float) -> float:
        return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4

    red, green, blue = (linear(c) for c in (color.redF(), color.greenF(), color.blueF()))
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast(first: QColor, second: QColor) -> float:
    lighter, darker = sorted((luminance(first), luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


class DmgWindowTests(unittest.TestCase):
    def setUp(self):
        self.settings = make_dmg.settings(Path("dist/QiTV.app"))

    def test_the_image_holds_the_app_and_a_shortcut_to_applications(self):
        self.assertEqual(self.settings["files"], [("dist/QiTV.app", "QiTV.app")])
        self.assertEqual(self.settings["symlinks"], {"Applications": "/Applications"})
        self.assertTrue(Path(self.settings["icon"]).is_file())  # the volume's icon

    def test_the_window_is_the_background_with_nothing_around_it(self):
        self.assertEqual(self.settings["window_rect"][1], make_dmg.WINDOW_SIZE)
        self.assertEqual(self.settings["default_view"], "icon-view")
        for bar in ("toolbar", "sidebar", "pathbar", "status_bar", "tab_view"):
            self.assertIs(self.settings[f"show_{bar}"], False, bar)
        self.assertEqual(self.settings["background"], str(make_dmg.BACKGROUND))
        width, height = make_dmg.WINDOW_SIZE
        for scale, path in ((1, make_dmg.BACKGROUND), (2, RETINA)):
            image = QImage(str(path))
            self.assertEqual((image.width(), image.height()), (width * scale, height * scale))
            # The same size in points: tiffutil joins them into one TIFF only so.
            self.assertAlmostEqual(image.dotsPerMeterX() * 0.0254, 72 * scale, delta=0.5)

    def test_each_icon_stands_on_its_card_and_the_arrow_runs_between_them(self):
        locations = self.settings["icon_locations"]
        self.assertEqual(locations, make_dmg.ICON_CENTRES)  # what the background was drawn for
        (app_x, app_y), (applications_x, applications_y) = (
            locations["QiTV.app"],
            locations["Applications"],
        )
        self.assertEqual(app_y, applications_y)
        self.assertLess(app_x, applications_x)  # QiTV on the left
        half = make_dmg.ICON_SIZE / 2
        width, height = make_dmg.WINDOW_SIZE
        for x, y in locations.values():
            card = card_rect((x, y))
            self.assertTrue(0 < card.left() and card.right() < width and card.bottom() < height)
            self.assertTrue(card.left() < x - half and x + half < card.right())
            self.assertTrue(card.top() < y - half and plate_rect((x, y)).bottom() < card.bottom())
        tail, tip = arrow_ends(locations)
        app, applications = card_rect(locations["QiTV.app"]), card_rect(locations["Applications"])
        self.assertTrue(app.right() < tail.x() < tip.x() < applications.left())
        middle = QImage(str(make_dmg.BACKGROUND)).pixelColor(((tail + tip) / 2).toPoint())
        self.assertEqual(middle, qcolor(PALETTE.accent))

    def test_finders_names_read_on_their_plates_in_light_and_dark_mode(self):
        # Finder writes the names black in light mode and white in dark mode, on the picture.
        black, white = QColor("black"), QColor("white")
        image = QImage(str(make_dmg.BACKGROUND))
        for centre in make_dmg.ICON_CENTRES.values():
            plate = plate_rect(centre)
            for dx in (-35, 0, 35):
                for dy in (-10, 0, 10):
                    point = (plate.center().x() + dx, plate.center().y() + dy)
                    color = image.pixelColor(round(point[0]), round(point[1]))
                    self.assertGreaterEqual(contrast(color, black), 4.5, point)
                    self.assertGreaterEqual(contrast(color, white), 4.5, point)

    def test_both_macos_builds_make_and_check_the_image_with_its_window(self):
        steps = WORKFLOW.read_text(encoding="utf-8").split("      - name: ")
        made = [i for i, step in enumerate(steps) if step.startswith("Make the macOS disk image")]
        self.assertEqual(len(made), 2)
        for index, arch in zip(made, ("universal", "intel")):
            self.assertIn(
                f"scripts/make_dmg.py dist/QiTV.app dist/qitv-macos-{arch}.dmg", steps[index]
            )
            check = steps[index + 1]
            self.assertTrue(check.startswith("Check the macOS disk image"))
            self.assertIn('codesign --verify --deep --strict "$volume/QiTV.app"', check)
            self.assertIn('test -s "$volume/.DS_Store"', check)


if __name__ == "__main__":
    unittest.main()
