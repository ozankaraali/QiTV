import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from services.library import Genre
from ui.library.genre_list import PREVIEW_DEBOUNCE_MS, GenreList
from ui.library.top_nav import TopNav

GENRES = [
    Genre("continue", "Continue", 4, pinned=True),
    Genre("new", "New this week", 48, pinned=True),
    Genre("action", "Action", 1920),
    Genre("comedy", "Comedy", 2640),
    Genre("doc", "Documentary", 1284),
    Genre("drama", "Drama", 3310),
]


def host(width=1280):
    w = QWidget()
    w.resize(width, 720)
    w.show()
    return w


class GenreListTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.host = host()
        self.addCleanup(self.host.deleteLater)
        self.addCleanup(self.host.close)
        self.list = GenreList(self.host)
        self.list.setGeometry(0, 0, self.list.width(), 720)
        self.list.set_genres(GENRES)
        self.selected, self.activated = [], []
        self.list.genreSelected.connect(self.selected.append)
        self.list.genreActivated.connect(self.activated.append)
        self.list._cancel_preview()

    def test_sections(self):
        self.assertEqual(
            self.list.rows(),
            [
                "PINNED",
                "Continue",
                "New this week",
                "GENRES · 4",
                "Action",
                "Comedy",
                "Documentary",
                "Drama",
            ],
        )
        self.assertEqual(self.list.current_id(), "continue")

    def test_width_is_18_units(self):
        self.assertEqual(self.list.width(), round(1280 * 0.18))

    def test_move_by_debounces_to_one_signal(self):
        self.list.move_by(1)
        self.list.move_by(1)
        self.list.move_by(1)
        self.assertEqual(self.selected, [])
        QTest.qWait(PREVIEW_DEBOUNCE_MS + 150)
        self.assertEqual(self.selected, ["comedy"])

    def test_escape_clears_filter(self):
        changes = []
        self.list.filterChanged.connect(changes.append)
        self.list.type_text("doc")
        self.assertEqual(self.list.filter_text, "doc")
        self.assertTrue(self.list.escape())
        self.assertEqual(self.list.filter_text, "")
        self.assertFalse(self.list.escape())
        self.assertEqual(changes, ["doc", ""])

    def test_backspace(self):
        self.list.type_text("ab")
        self.list.backspace()
        self.assertEqual(self.list.filter_text, "a")

    def test_activate_flushes_preview_first(self):
        order = []
        self.list.genreSelected.connect(lambda g: order.append(("sel", g)))
        self.list.genreActivated.connect(lambda g: order.append(("act", g)))
        self.list.move_by(2)
        self.list.activate()
        self.assertEqual(order, [("sel", "action"), ("act", "action")])
        QTest.qWait(PREVIEW_DEBOUNCE_MS + 150)
        self.assertEqual(len(order), 2)

    def test_filtered_list_keeps_current_or_previews_first_match(self):
        self.list.set_current("doc")
        self.list.set_genres([g for g in GENRES if g.id in ("doc", "drama")])
        self.assertEqual(self.list.current_id(), "doc")
        QTest.qWait(PREVIEW_DEBOUNCE_MS + 150)
        self.assertEqual(self.selected, [])
        self.list.set_genres([GENRES[3]])
        self.assertEqual(self.list.current_id(), "comedy")
        QTest.qWait(PREVIEW_DEBOUNCE_MS + 150)
        self.assertEqual(self.selected, ["comedy"])

    def test_empty_list_has_no_current(self):
        self.list.set_genres([])
        self.assertIsNone(self.list.current_id())
        self.assertEqual(self.list.rows(), [])
        self.list.move_by(1)
        self.list.activate()
        self.assertEqual(self.activated, [])

    def test_grab_at_widths(self):
        for width in (1280, 800):
            h = host(width)
            self.addCleanup(h.deleteLater)
            self.addCleanup(h.close)
            col = GenreList(h)
            col.setGeometry(0, 0, col.width(), 720)
            col.set_genres(GENRES)
            col.type_text("do")
            col.set_focused(True)
            self.assertFalse(col.grab().isNull())


class TopNavTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.host = host()
        self.addCleanup(self.host.deleteLater)
        self.addCleanup(self.host.close)
        self.nav = TopNav(self.host)
        self.nav.setGeometry(0, 0, 1280, self.nav.height())
        self.nav.set_source_name("Demo provider")
        self.tabs, self.sources, self.searches = [], [], []
        self.nav.tabRequested.connect(self.tabs.append)
        self.nav.sourceRequested.connect(self.sources.append)
        self.nav.searchRequested.connect(lambda: self.searches.append(True))

    def test_height_is_6_units(self):
        self.assertEqual(self.nav.height(), round(1280 * 0.06))

    def test_set_tabs_hides_movies_and_series(self):
        self.nav.set_tabs(["live"])
        rects = self.nav._item_rects()
        self.assertNotIn("movies", rects)
        self.assertNotIn("series", rects)
        self.assertIn("live", rects)

    def test_click_movies_tab(self):
        rect = self.nav._item_rects()["movies"]
        QTest.mouseClick(self.nav, Qt.LeftButton, pos=rect.center().toPoint())
        self.assertEqual(self.tabs, ["movies"])

    def test_click_source_pill_gives_point_under_it(self):
        rect = self.nav._item_rects()["source"]
        QTest.mouseClick(self.nav, Qt.LeftButton, pos=rect.center().toPoint())
        self.assertEqual(len(self.sources), 1)
        local = self.nav.mapFromGlobal(self.sources[0])
        self.assertGreaterEqual(local.y(), rect.bottom() - 1)
        self.assertTrue(rect.left() - 1 <= local.x() <= rect.right())

    def test_click_hidden_tab_area_does_nothing(self):
        self.nav.set_tabs(["live"])
        rect = self.nav._item_rects()["live"]
        QTest.mouseClick(self.nav, Qt.LeftButton, pos=QPoint(int(rect.right()) + 60, 10))
        self.assertEqual(self.tabs, [])

    def test_keyboard_moves_between_tabs_and_pills(self):
        self.nav.move_by(1)
        self.nav.move_by(1)
        self.nav.activate()
        self.assertEqual(self.tabs, ["series"])
        self.nav.move_by(1)
        self.nav.activate()
        self.assertEqual(self.searches, [True])
        self.nav.move_by(5)
        self.nav.activate()
        self.assertEqual(len(self.sources), 1)

    def test_grab_at_widths(self):
        for width in (1280, 800):
            h = host(width)
            self.addCleanup(h.deleteLater)
            self.addCleanup(h.close)
            nav = TopNav(h)
            nav.setGeometry(0, 0, width, nav.height())
            nav.set_source_name("A rather long provider name for the pill")
            nav.set_active("series")
            nav.set_focused(True)
            self.assertFalse(nav.grab().isNull())


if __name__ == "__main__":
    unittest.main()
