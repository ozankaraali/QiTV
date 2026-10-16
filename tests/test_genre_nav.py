import os
from pathlib import Path
import sys
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QFont, QFontMetricsF, QHelpEvent, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from timer_fixtures import elapse

from services.library import Genre
from ui import brand
from ui.brand import logo_pixmap
from ui.keymap import key_hint
from ui.library.genre_list import PREVIEW_DEBOUNCE_MS, GenreList
from ui.library.top_nav import TopNav
from ui.overlays.painting import Units

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
        elapse(PREVIEW_DEBOUNCE_MS + 150)
        self.selected.clear()

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

    def test_the_same_genres_again_keep_the_list(self):
        # Every genre switch and sort sets the genres again: the list is not rebuilt for that.
        resets = []
        self.list._model.modelReset.connect(lambda: resets.append(True))
        self.list.set_current("comedy")
        self.list.set_genres(list(GENRES))
        self.assertEqual(resets, [])
        self.assertEqual(self.list.current_id(), "comedy")
        self.list.set_genres([*GENRES[:3], Genre("comedy", "Comedy", 2641), *GENRES[4:]])
        self.assertEqual(resets, [True])
        self.assertEqual(self.list.current_id(), "comedy")
        self.list.set_genres(GENRES[2:])
        self.assertEqual(self.list.rows()[0], "GENRES · 4")

    def test_width_is_18_units(self):
        self.assertEqual(self.list.width(), round(1280 * 0.18))

    def test_move_by_debounces_to_one_signal(self):
        self.list.move_by(1)
        self.list.move_by(1)
        self.list.move_by(1)
        self.assertEqual(self.selected, [])
        elapse(PREVIEW_DEBOUNCE_MS + 150)
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
        elapse(PREVIEW_DEBOUNCE_MS + 150)
        self.assertEqual(len(order), 2)

    def test_activate_without_pending_preview_emits_only_activated(self):
        self.list.move_by(1)
        elapse(PREVIEW_DEBOUNCE_MS + 150)
        self.assertEqual(self.selected, ["new"])
        self.list.activate()
        self.assertEqual(self.selected, ["new"])
        self.assertEqual(self.activated, ["new"])

    def test_click_genre_row_activates_it(self):
        view = self.list._view
        row = self.list._rows_by_id["comedy"]
        pos = view.visualRect(self.list._model.index(row)).center()
        QTest.mouseClick(view.viewport(), Qt.LeftButton, pos=pos)
        self.assertEqual(self.list.current_id(), "comedy")
        self.assertEqual(self.selected, ["comedy"])
        self.assertEqual(self.activated, ["comedy"])

    def test_click_header_does_nothing(self):
        view = self.list._view
        pos = view.visualRect(self.list._model.index(0)).center()
        QTest.mouseClick(view.viewport(), Qt.LeftButton, pos=pos)
        self.assertEqual(self.activated, [])

    def test_many_genres_move_and_page(self):
        many = [Genre(f"g{i}", f"Genre {i}", i) for i in range(150)]
        self.list.set_genres(many)
        self.assertEqual(self.list.current_id(), "g0")
        self.list.move_by(5)
        self.assertEqual(self.list.current_id(), "g5")
        self.list.page(1)
        after = int(self.list.current_id()[1:])
        self.assertGreater(after, 5)
        self.list.move_by(1000)
        self.assertEqual(self.list.current_id(), "g149")
        self.list.page(-1)
        self.assertLess(int(self.list.current_id()[1:]), 149)

    def test_filtered_list_keeps_current_or_previews_first_match(self):
        self.list.set_current("doc")
        self.list.set_genres([g for g in GENRES if g.id in ("doc", "drama")])
        self.assertEqual(self.list.current_id(), "doc")
        elapse(PREVIEW_DEBOUNCE_MS + 150)
        self.assertEqual(self.selected, [])
        self.list.set_genres([GENRES[3]])
        self.assertEqual(self.list.current_id(), "comedy")
        elapse(PREVIEW_DEBOUNCE_MS + 150)
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

    def test_set_active_unknown_tab_raises(self):
        with self.assertRaises(ValueError):
            self.nav.set_active("music")

    def test_activation_happens_on_release(self):
        pos = self.nav._item_rects()["movies"].center().toPoint()
        QTest.mousePress(self.nav, Qt.LeftButton, pos=pos)
        self.assertEqual(self.tabs, [])
        QTest.mouseRelease(self.nav, Qt.LeftButton, pos=pos)
        self.assertEqual(self.tabs, ["movies"])

    def test_hit_rects_do_not_overlap(self):
        rects = self.nav._item_rects()
        for a in rects:
            for b in rects:
                if a < b:
                    c = rects[a].center()
                    self.assertEqual(self.nav._item_at(c), a)

    def test_click_hidden_tab_area_does_nothing(self):
        self.nav.set_tabs(["live"])
        rect = self.nav._item_rects()["live"]
        QTest.mouseClick(self.nav, Qt.LeftButton, pos=QPoint(int(rect.right()) + 60, 10))
        self.assertEqual(self.tabs, [])

    def test_a_fullscreen_button_ends_the_bar(self):
        # The owner's mouse-only rule (2.0.1): F11 (⌃⌘F) has a button in the Library too.
        fullscreens = []
        self.nav.fullscreenRequested.connect(lambda: fullscreens.append(True))
        rects = self.nav._item_rects()
        button = rects["fullscreen"]
        self.assertGreater(button.left(), rects["source"].right())
        self.assertLessEqual(button.right(), self.nav.width())
        self.assertGreaterEqual(min(button.width(), button.height()), 28)
        keys = "Ctrl+Meta+F" if sys.platform == "darwin" else "F11"
        self.assertEqual(self.nav.tooltip_at(button.center()), key_hint("Fullscreen", keys))
        QTest.mouseClick(self.nav, Qt.LeftButton, pos=button.center().toPoint())
        self.assertEqual(fullscreens, [True])
        self.assertEqual((self.tabs, self.sources, self.searches), ([], [], []))

    def double_click(self, point):
        """As a mouse does it: press, release, then a press that is a double-click."""
        self.nav.show()  # made after its host showed: shown for the window's events
        host = self.host.windowHandle()
        QTest.mouseDClick(host, Qt.LeftButton, Qt.NoModifier, self.nav.mapTo(self.host, point))

    def test_a_quick_second_click_on_fullscreen_asks_once(self):
        # 2.0.1 review M1: not into fullscreen and straight out again.
        fullscreens = []
        self.nav.fullscreenRequested.connect(lambda: fullscreens.append(True))
        self.double_click(self.nav._item_rects()["fullscreen"].center().toPoint())
        self.assertEqual(fullscreens, [True])

    def test_a_click_on_fullscreen_leaves_the_keys_where_they_were(self):
        # 2.0.1 review M2: ⛶ is not in the keys' order, so it never takes their focus.
        self.nav.set_active("movies")
        self.nav.fullscreenRequested.connect(lambda: None)
        button = self.nav._item_rects()["fullscreen"].center().toPoint()
        QTest.mouseClick(self.nav, Qt.LeftButton, pos=button)
        self.nav.set_focused(True)
        self.nav.activate()  # Enter after ↑ into the bar
        self.assertEqual(self.tabs, ["movies"])

    def test_hovering_shows_the_tooltip(self):
        # A real tooltip event: QHelpEvent has pos(), not position().
        button = self.nav._item_rects()["fullscreen"].center().toPoint()
        for point, text in ((button, "Fullscreen"), (self.nav.logo_rect().center().toPoint(), "")):
            event = QHelpEvent(QEvent.Type.ToolTip, point, self.nav.mapToGlobal(point))
            with mock.patch("ui.library.top_nav.QToolTip.showText") as show:
                self.assertTrue(self.nav.event(event))
            show.assert_called_once()
            self.assertTrue(show.call_args[0][1].startswith(text or "QiTV"))

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

    def painted(self):
        """What the nav paints: the texts it draws and the target rects of its pixmaps."""
        texts, pixmaps = [], []
        draw_text, draw_pixmap = QPainter.drawText, QPainter.drawPixmap

        def text(painter, *args):
            texts.append(args[-1])
            return draw_text(painter, *args)

        def pixmap(painter, *args):
            pixmaps.append(args[0])
            return draw_pixmap(painter, *args)

        with (
            mock.patch.object(QPainter, "drawText", text),
            mock.patch.object(QPainter, "drawPixmap", pixmap),
        ):
            self.nav.grab()
        return texts, pixmaps

    def test_the_logo_replaces_the_wordmark(self):
        texts, pixmaps = self.painted()
        self.assertNotIn("QiTV", texts)
        self.assertIn("Live TV", texts)
        logo = self.nav.logo_rect()
        self.assertEqual(pixmaps, [logo.topLeft()])  # whole pixels: crisp
        units = Units(1280)
        cap = QFontMetricsF(units.font(1.6, QFont.Weight.Bold)).capHeight()
        self.assertAlmostEqual(logo.height(), cap * 1.6, delta=0.5)
        self.assertAlmostEqual(logo.center().y(), self.nav.height() / 2, delta=0.5)
        self.assertAlmostEqual(logo.left(), 2.0 * units.u, delta=0.5)
        self.assertEqual(self.nav.accessibleName(), "QiTV")

    def test_the_tabs_start_after_the_logo(self):
        u = Units(1280).u
        logo = self.nav.logo_rect()
        self.assertGreater(logo.width(), 0)
        self.assertAlmostEqual(self.nav._item_rects()["live"].left(), logo.right() + 3.0 * u)

    def test_without_the_logo_file_the_name_is_drawn(self):
        missing = Path(__file__).parent / "no-such-logo.png"
        with (
            mock.patch.object(brand, "_master", None),
            mock.patch.object(brand, "_missing_logged", False),
            mock.patch.object(brand, "logo_path", return_value=missing),
            self.assertLogs("ui.brand", "WARNING"),
        ):
            texts, pixmaps = self.painted()
            u = Units(1280).u
            live = self.nav._item_rects()["live"].left()
            self.assertAlmostEqual(live, self.nav.logo_rect().right() + 3.0 * u)
        self.assertIn("QiTV", texts)
        self.assertEqual(pixmaps, [])

    def test_the_logo_is_made_for_the_widgets_own_screen(self):
        drawn = []
        draw_pixmap = QPainter.drawPixmap

        def pixmap(painter, *args):
            drawn.append(args[1])
            return draw_pixmap(painter, *args)

        with (
            mock.patch.object(self.nav, "devicePixelRatioF", return_value=3.0),
            mock.patch.object(QPainter, "drawPixmap", pixmap),
        ):
            self.nav.grab()
        self.assertEqual([p.devicePixelRatio() for p in drawn], [3.0])
        self.assertEqual(drawn[0].height(), round(self.nav.logo_rect().height() * 3))

    def test_the_logo_is_crisp_at_the_screen_density(self):
        pixmap = logo_pixmap(24, 2.0)
        self.assertEqual(pixmap.devicePixelRatio(), 2.0)
        self.assertEqual(pixmap.deviceIndependentSize().height(), 24)
        self.assertEqual(pixmap.height(), 48)

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
