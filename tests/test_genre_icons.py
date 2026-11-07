import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QFontMetricsF
from PySide6.QtWidgets import QApplication, QWidget

from services.library import CONTINUE, MY_LIST, NEW, PINNED_TITLES, Genre
from ui.library.genre_list import ICON_GAP_U, ICON_SCALE, GenreList, IconRole
from ui.overlays.painting import Units


def ink_columns(image, y0, y1, x0, x1, background):
    cols = []
    for x in range(x0, x1):
        if any(image.pixel(x, y) != background for y in range(y0, y1)):
            cols.append(x)
    return cols


class PinnedRowIconTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def render(self, genres, plain_id):
        host = QWidget()
        host.resize(1280, 720)
        host.show()
        self.addCleanup(host.close)
        genre_list = GenreList(host)
        genre_list.setGeometry(0, 0, genre_list.width(), 720)
        genre_list.set_genres(genres)
        genre_list.set_current(plain_id)  # keep the rows under test unselected
        view = genre_list._view
        image = view.viewport().grab().toImage()
        return genre_list, view, image

    def test_titles_carry_no_glyphs(self):
        for title in PINNED_TITLES.values():
            self.assertTrue(title.isascii() and title == title.strip(), title)
        # 2.9.7: no Recordings shelf (the Recordings tab has them)
        self.assertEqual(list(PINNED_TITLES.values()), ["Continue", "New", "My list"])

    def test_each_pinned_row_paints_an_icon_before_its_label(self):
        genres = [
            Genre(CONTINUE, "Continue", 4, pinned=True),
            Genre(NEW, "New", 48, pinned=True),
            Genre(MY_LIST, "My list", 2, pinned=True),
            Genre("plain", "Continue", 9),
        ]
        genre_list, view, image = self.render(genres, "plain")
        units = Units(1280)
        pad = 1.4 * units.u
        font = units.font(1.15)
        size = QFontMetricsF(font).capHeight() * ICON_SCALE
        gap = ICON_GAP_U * units.u
        background = image.pixel(2, 2)

        def row_ink(genre_id, x1):
            rect = view.visualRect(genre_list._model.index(genre_list._rows_by_id[genre_id]))
            return ink_columns(
                image, rect.top() + 2, rect.bottom() - 2, int(pad) - 1, x1, background
            )

        plain_left = row_ink("plain", int(pad + 40))[0]
        for genre_id in (CONTINUE, NEW, MY_LIST):
            icon_cols = row_ink(genre_id, int(pad + size))
            self.assertGreaterEqual(len(icon_cols), size * 0.5, genre_id)
            ink = row_ink(genre_id, int(pad + size + gap + 40))
            text_left = [x for x in ink if x >= pad + size + gap - 2][0]
            self.assertAlmostEqual(text_left, plain_left + size + gap, delta=3, msg=genre_id)
            between = [x for x in ink if pad + size + 1 <= x <= pad + size + gap - 3]
            self.assertEqual(between, [], genre_id)  # an icon, then the label: not a label alone
            # the plain row has nothing in the icon's slot beyond its own label start


if __name__ == "__main__":
    unittest.main()


class RailCaptionTests(unittest.TestCase):  # 2.9.7: the Recordings tab's rail
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def rail(self):
        host = QWidget()
        host.resize(1280, 720)
        host.show()
        self.addCleanup(host.close)
        rail = GenreList(host)
        rail.setGeometry(0, 0, rail.width(), 720)
        rail.set_captions(None, None)
        rail.set_genres(
            [
                Genre("rec:tonight", "Tonight", None, pinned=True),
                Genre("rec:coming", "Coming up", 4, pinned=True),
                Genre("", "RECORDED · 12", None, header=True),
                Genre("rec:all", "All", 12),
                Genre("", "SETTINGS", None, header=True),
                Genre("rec:settings", "Folders & streams", None),
            ]
        )
        return rail

    def test_a_rail_without_captions_paints_its_pinned_rows_first(self):
        rail = self.rail()
        self.assertEqual(
            rail.rows(),
            ["Tonight", "Coming up", "RECORDED · 12", "All", "SETTINGS", "Folders & streams"],
        )

    def test_header_rows_are_captions_and_never_selected(self):
        rail = self.rail()
        rail.set_current("rec:coming")
        rail.move_by(1)
        self.assertEqual(rail.current_id(), "rec:all")  # over "RECORDED · 12"
        rail.move_by(1)
        self.assertEqual(rail.current_id(), "rec:settings")

    def test_icons_given_for_rows_are_painted(self):
        rail = self.rail()
        rail.set_icons({"rec:tonight": "record", "rec:settings": "gear"})
        model = rail._view.model()
        icons = [model.data(model.index(row, 0), IconRole) for row in range(model.rowCount())]
        self.assertEqual(icons, ["record", None, None, None, None, "gear"])
        rail._view.viewport().grab()  # paints

    def test_the_default_captions_stay_for_movies(self):
        host = QWidget()
        self.addCleanup(host.close)
        movies = GenreList(host)
        movies.set_genres([Genre(CONTINUE, "Continue", 1, pinned=True), Genre("1", "Drama", 3)])
        self.assertEqual(movies.rows(), ["PINNED", "Continue", "GENRES · 1", "Drama"])


class RailPlaceholderTests(unittest.TestCase):  # 2.9.7: the Recordings rail's find
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_a_rail_names_what_its_find_finds(self):
        host = QWidget()
        self.addCleanup(host.close)
        rail = GenreList(host)
        self.assertEqual(rail.placeholder(), "Find a genre…")
        rail.set_placeholder("Find a recording or programme…")
        self.assertEqual(rail.placeholder(), "Find a recording or programme…")
