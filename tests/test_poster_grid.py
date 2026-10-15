import os
import time
import unittest
import zlib

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, Qt, Signal
from PySide6.QtGui import QMouseEvent, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from services.library import Details
from ui.library.poster_grid import FAVORITE, PLAY, START_OVER, PosterGrid, card_hue

GENRE_U = 18.0  # the Library's genre column, left of the grid
NAV_U = 6.0  # the Library's top navigation, above the grid


class FakePosters(QObject):
    logoReady = Signal(str)

    def __init__(self):
        super().__init__()
        self.requested = []

    def request(self, urls):
        self.requested.append(list(urls))

    def pixmap(self, url):
        return None


def titles(n, prefix="Title"):
    return [
        {"name": f"{prefix} {i}", "logo": f"http://posters.invalid/{prefix}/{i}.jpg"}
        for i in range(n)
    ]


def details(kind="movie", resume_ms=2_232_000, favorite=False):
    return Details(
        title="Salt Roads",
        year="2024",
        duration="1h 38m",
        genre="Documentary",
        rating="7.8",
        plot="Caravans, coastlines and the people who still move salt across three continents.",
        resume_ms=resume_ms,
        progress=0.4 if resume_ms else None,
        favorite=favorite,
        kind=kind,
    )


def move_mouse(widget, pos):
    event = QMouseEvent(
        QEvent.Type.MouseMove,
        QPointF(pos),
        QPointF(widget.mapToGlobal(pos)),
        Qt.MouseButton.NoButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(widget, event)


def wheel(widget, dy):
    event = QWheelEvent(
        QPointF(50, 50),
        QPointF(widget.mapToGlobal(QPoint(50, 50))),
        QPoint(0, 0),
        QPoint(0, dy),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(widget, event)


class PosterGridTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make(self, width=1280, height=720):
        host = QWidget()
        host.resize(width, height)
        self.addCleanup(host.deleteLater)
        self.addCleanup(host.close)
        posters = FakePosters()
        grid = PosterGrid(posters, host)
        u = width / 100
        left, top = round(GENRE_U * u), round(NAV_U * u)
        grid.setGeometry(left, top, width - left, height - top)
        host.show()
        self.addCleanup(grid.close)
        return host, grid, posters

    def setUp(self):
        self.host, self.grid, self.posters = self.make()
        self.events = []
        for name in (
            "activated",
            "startOverRequested",
            "favoriteToggled",
            "focusChanged",
            "sortRequested",
        ):
            getattr(self.grid, name).connect(lambda v, n=name: self.events.append((n, v)))
        self.grid.leftEdge.connect(lambda: self.events.append(("leftEdge", None)))
        self.grid.upEdge.connect(lambda: self.events.append(("upEdge", None)))

    def named(self, name):
        return [v for n, v in self.events if n == name]

    def buttons(self):
        """The strip buttons left to right, each with a point inside it, found via button_at."""
        strip = self.grid.strip_rect()
        y = strip.center().y()
        spans = {}
        for x in range(strip.left(), strip.right() + 1):
            name = self.grid.button_at(QPoint(x, y))
            if name is not None:
                spans.setdefault(name, []).append(x)
        ordered = sorted(spans.items(), key=lambda kv: kv[1][0])
        return [(name, QPoint((xs[0] + xs[-1]) // 2, y)) for name, xs in ordered]

    # 1
    def test_columns_follow_the_parent_width(self):
        self.grid.set_sections([("", titles(30))])
        self.assertEqual(self.grid.columns(), 7)
        host, grid, _ = self.make(width=400)
        grid.set_sections([("", titles(30))])
        self.assertGreaterEqual(grid.columns(), 3)
        grid.setGeometry(0, 0, 120, 400)
        self.assertEqual(grid.columns(), 3)

    def test_posters_fill_the_row_at_two_to_three(self):
        self.grid.set_sections([("", titles(30))])
        first, last = self.grid.item_rect(0), self.grid.item_rect(6)
        self.assertAlmostEqual(first.height() / first.width(), 1.5, delta=0.02)
        margin_left = first.left()
        margin_right = self.grid.viewport().width() - 1 - last.right()
        self.assertLessEqual(abs(margin_left - margin_right), 2)

    # 2
    def test_move_by_edges_and_clamping(self):
        self.grid.set_sections([("", titles(10))])  # rows: 0..6, 7..9
        self.grid.set_current_index(6)
        self.grid.move_by(1, 0)
        self.assertEqual(self.grid.current_index(), 6)
        self.grid.set_current_index(9)
        self.grid.move_by(1, 0)
        self.assertEqual(self.grid.current_index(), 9)
        self.grid.set_current_index(7)
        self.grid.move_by(-1, 0)
        self.assertEqual(self.grid.current_index(), 7)
        self.assertEqual(self.named("leftEdge"), [None])
        self.grid.set_current_index(3)
        self.grid.move_by(0, -1)
        self.assertEqual(self.grid.current_index(), 3)
        self.assertEqual(self.named("upEdge"), [None])
        self.grid.set_current_index(5)
        self.grid.move_by(0, 1)
        self.assertEqual(self.grid.current_index(), 9)  # clamped into the short last row
        self.grid.move_by(0, 1)
        self.assertEqual(self.grid.current_index(), 9)
        self.grid.move_by(0, -1)
        self.assertEqual(self.grid.current_index(), 2)
        self.grid.move_by(1, 0)
        self.assertEqual(self.grid.current_index(), 3)
        self.assertEqual(self.named("focusChanged")[-3:], [9, 2, 3])

    def test_empty_grid_has_no_current(self):
        self.assertEqual(self.grid.current_index(), -1)
        self.grid.set_sections([("", [])])
        self.assertEqual(self.grid.current_index(), -1)
        self.assertEqual(self.named("focusChanged"), [])
        self.grid.move_by(1, 0)
        self.grid.move_by(0, 1)
        self.assertEqual(self.grid.current_index(), -1)
        self.assertTrue(self.grid.strip_rect().isNull())
        self.assertIsNone(self.grid.item(0))

    def test_page_home_end(self):
        self.grid.set_sections([("", titles(200))])
        cols = self.grid.columns()
        self.grid.set_current_index(2)
        self.grid.page(1)
        moved = self.grid.current_index()
        self.assertEqual(moved % cols, 2)
        self.assertGreater(moved, 2)
        self.grid.page(-1)
        self.assertEqual(self.grid.current_index(), 2)
        self.grid.end()
        self.assertEqual(self.grid.current_index(), 199)
        rect = self.grid.item_rect(199)
        self.assertTrue(self.grid.viewport().rect().contains(rect))
        self.assertTrue(self.grid.viewport().rect().contains(self.grid.strip_rect()))
        self.grid.home()
        self.assertEqual(self.grid.current_index(), 0)
        self.assertEqual(self.grid.verticalScrollBar().value(), 0)

    # 3
    def test_strip_follows_the_current_row(self):
        self.grid.set_sections([("", titles(30))])
        for current, next_row in ((0, 7), (8, 14)):
            self.grid.set_current_index(current)
            strip = self.grid.strip_rect()
            self.assertGreater(strip.top(), self.grid.item_rect(current).bottom())
            self.assertGreater(self.grid.item_rect(next_row).top(), strip.bottom())
            self.assertTrue(self.grid.viewport().rect().contains(strip))
        self.assertLess(self.grid.item_rect(1).bottom(), self.grid.strip_rect().top())

    # 4
    def test_strip_buttons_emit_for_the_current_item(self):
        self.grid.set_sections([("", titles(30))])
        self.grid.set_details_provider(lambda item: details())
        self.grid.set_current_index(9)
        buttons = self.buttons()
        self.assertEqual([n for n, _ in buttons], [START_OVER, FAVORITE, PLAY])
        for name, point in buttons:
            QTest.mouseClick(self.grid.viewport(), Qt.LeftButton, Qt.NoModifier, point)
        self.assertEqual(self.named("startOverRequested"), [9])
        self.assertEqual(self.named("favoriteToggled"), [9])
        self.assertEqual(self.named("activated"), [9])
        self.assertEqual(self.grid.current_index(), 9)

    def test_strip_buttons_by_kind(self):
        self.grid.set_sections([("", titles(30))])
        cases = {
            ("movie", None): [FAVORITE, PLAY],
            ("series", None): [FAVORITE, PLAY],
            ("episode", 60_000): [START_OVER, PLAY],
        }
        for (kind, resume), expected in cases.items():
            self.grid.set_details_provider(lambda item, k=kind, r=resume: details(k, r))
            self.assertEqual([n for n, _ in self.buttons()], expected, kind)
            self.assertFalse(self.grid.grab().isNull())
        self.grid.set_details_provider(lambda item: None)
        self.assertEqual([n for n, _ in self.buttons()], [PLAY])
        self.assertFalse(self.grid.grab().isNull())

    def test_clicks_select_then_activate(self):
        self.grid.set_sections([("", titles(30))])
        point = self.grid.item_rect(3).center()
        QTest.mouseClick(self.grid.viewport(), Qt.LeftButton, Qt.NoModifier, point)
        self.assertEqual(self.grid.current_index(), 3)
        self.assertEqual(self.named("activated"), [])
        QTest.mouseClick(self.grid.viewport(), Qt.LeftButton, Qt.NoModifier, point)
        self.assertEqual(self.named("activated"), [3])
        point = self.grid.item_rect(5).center()
        # A real double-click is press, release, then the double-click event (no second press).
        QTest.mouseClick(self.grid.viewport(), Qt.LeftButton, Qt.NoModifier, point)
        QTest.mouseDClick(self.grid.viewport(), Qt.LeftButton, Qt.NoModifier, point)
        self.assertEqual(self.grid.current_index(), 5)
        self.assertEqual(self.named("activated"), [3, 5])

    def test_wheel_scrolls(self):
        self.grid.set_sections([("", titles(200))])
        wheel(self.grid.viewport(), -240)
        self.assertGreater(self.grid.verticalScrollBar().value(), 0)
        self.assertEqual(self.grid.current_index(), 0)

    # 5
    def test_hover_dwell_makes_a_poster_current(self):
        self.grid.set_sections([("", titles(30))])
        move_mouse(self.grid.viewport(), self.grid.item_rect(4).center())
        QTest.qWait(100)
        self.assertEqual(self.grid.current_index(), 0)
        QTest.qWait(200)
        self.assertEqual(self.grid.current_index(), 4)
        self.assertEqual(self.grid.verticalScrollBar().value(), 0)

    def test_hover_keeps_the_hovered_poster_in_place(self):
        self.grid.set_sections([("", titles(200))])
        self.grid.set_current_index(70)  # scrolled well down, strip under row 10
        below = 70 + 2 * self.grid.columns()
        self.grid.verticalScrollBar().setValue(
            self.grid.verticalScrollBar().value() + self.grid.item_rect(below).top() - 40
        )
        before = self.grid.item_rect(below)
        move_mouse(self.grid.viewport(), before.center())
        QTest.qWait(300)
        self.assertEqual(self.grid.current_index(), below)
        self.assertEqual(self.grid.item_rect(below).top(), before.top())

    def test_hover_moving_on_restarts_the_dwell(self):
        self.grid.set_sections([("", titles(30))])
        move_mouse(self.grid.viewport(), self.grid.item_rect(4).center())
        QTest.qWait(150)
        move_mouse(self.grid.viewport(), self.grid.item_rect(5).center())
        QTest.qWait(150)
        self.assertEqual(self.grid.current_index(), 0)
        QTest.qWait(150)
        self.assertEqual(self.grid.current_index(), 5)

    # 6
    def test_jump_to_letter(self):
        self.grid.set_sections([("", titles(100))])
        self.grid.set_letter_index({"#": 0, "a": 3, "m": 40})
        self.grid.jump_to_letter("m")
        self.assertEqual(self.grid.current_index(), 40)
        self.grid.jump_to_letter("A")
        self.assertEqual(self.grid.current_index(), 3)
        self.grid.jump_to_letter("q")  # not in the index
        self.assertEqual(self.grid.current_index(), 3)
        self.assertTrue(self.grid.viewport().rect().contains(self.grid.item_rect(3)))

    def test_rail_click_jumps_when_shown(self):
        self.grid.set_sections([("", titles(100))])
        self.grid.set_letter_index({"#": 0, "a": 3, "m": 40})
        vp = self.grid.viewport()
        u = self.host.width() / 100
        slot = vp.height() / 27
        m = QPoint(vp.width() - round(0.8 * u), round(13.5 * slot))  # "#ABCDEFGHIJKL" then M
        QTest.mouseClick(vp, Qt.LeftButton, Qt.NoModifier, m)
        self.assertEqual(self.grid.current_index(), 40)
        self.grid.set_sections([("", titles(59))])  # below 60 titles: no rail
        self.grid.set_current_index(1)
        QTest.mouseClick(vp, Qt.LeftButton, Qt.NoModifier, m)
        self.assertEqual(self.grid.current_index(), 1)
        self.assertFalse(self.grid.grab().isNull())

    # 7
    def test_twenty_thousand_titles_paint_fast_and_request_only_visible_posters(self):
        many = titles(20_000)
        start = time.perf_counter()
        self.grid.set_sections([("", many)])
        image = self.grid.grab()
        elapsed = time.perf_counter() - start
        self.assertFalse(image.isNull())
        self.assertLess(elapsed, 0.150)
        cols = self.grid.columns()
        view = self.grid.viewport().rect()
        rows = {i // cols for i in range(200) if self.grid.item_rect(i).intersects(view)}
        requested = [url for call in self.posters.requested for url in call]
        self.assertEqual(len(requested), len(set(requested)))  # de-duplicated
        self.assertLessEqual(len(requested), (len(rows) + 1) * cols)
        self.assertIn(many[0]["logo"], requested)

    def test_poster_ready_repaints_only_when_visible(self):
        many = titles(500)
        self.grid.set_sections([("", many)])
        self.grid.grab()
        viewport = self.grid.viewport()
        updates = []
        viewport.update = lambda *args: updates.append(args)
        self.posters.logoReady.emit(many[400]["logo"])
        self.assertEqual(updates, [])
        self.posters.logoReady.emit(many[0]["logo"])
        self.assertEqual(len(updates), 1)

    # 8
    def test_sections_flow_rows_across_boundaries(self):
        first, second = titles(10, "Movie"), titles(5, "Series")
        self.grid.set_sections(
            [("Provider · Movies (10)", first), ("Provider · Series (5)", second)]
        )
        self.assertEqual(self.grid.item(10)["name"], "Series 0")
        self.grid.set_current_index(8)
        self.grid.move_by(0, 1)
        self.assertEqual(self.grid.current_index(), 11)
        self.grid.move_by(0, -1)
        self.assertEqual(self.grid.current_index(), 8)
        self.grid.set_current_index(3)
        self.grid.move_by(0, 1)
        self.assertEqual(self.grid.current_index(), 9)
        self.grid.move_by(0, 1)
        self.assertEqual(self.grid.current_index(), 12)
        self.grid.set_current_index(10)
        self.grid.move_by(-1, 0)
        self.assertEqual(self.named("leftEdge"), [None])
        # The second section's title row sits between the sections (the strip is below them).
        self.grid.set_current_index(10)
        row_gap = self.grid.item_rect(7).top() - self.grid.item_rect(0).bottom()
        self.assertGreater(
            self.grid.item_rect(10).top() - self.grid.item_rect(7).bottom(), 2 * row_gap
        )
        self.assertFalse(self.grid.grab().isNull())

    def test_set_sections_keeps_the_current_title(self):
        items = titles(30)
        self.grid.set_sections([("", items)])
        self.grid.set_current_index(12)
        focus_events = len(self.named("focusChanged"))
        self.grid.set_sections([("", list(items))])
        self.assertEqual(self.grid.current_index(), 12)
        self.assertEqual(len(self.named("focusChanged")), focus_events)
        self.grid.set_sections([("", list(reversed(items)))])
        self.assertIs(self.grid.item(self.grid.current_index()), items[12])
        self.grid.set_sections([("", titles(30, "Other"))])
        self.assertEqual(self.grid.current_index(), 0)

    # 9
    def test_message_paints_without_items(self):
        self.grid.set_header("Documentary", "", [], "")
        self.grid.set_message("Loading…")
        self.assertFalse(self.grid.grab().isNull())
        self.assertEqual(self.grid.index_at(QPoint(100, 100)), -1)
        self.grid.set_message(None)
        self.grid.set_sections([("", titles(3))])
        self.assertFalse(self.grid.grab().isNull())

    # 10
    def test_sort_chip_click_emits_sort_requested(self):
        self.grid.set_header(
            "Documentary",
            "1,284 titles",
            [("added", "Recently added"), ("az", "A–Z"), ("year", "Year"), ("rating", "Rating")],
            "added",
        )
        rect = self.grid.chip_rect("az")
        self.assertFalse(rect.isNull())
        self.assertGreater(rect.left(), self.grid.width() / 2)  # right-aligned
        target = self.grid.childAt(rect.center())
        self.assertIsNotNone(target)
        QTest.mouseClick(
            target, Qt.LeftButton, Qt.NoModifier, target.mapFrom(self.grid, rect.center())
        )
        self.assertEqual(self.named("sortRequested"), ["az"])
        self.assertLess(rect.bottom(), self.grid.viewport().mapTo(self.grid, QPoint(0, 0)).y())

    def test_card_hue_is_stable_and_folded(self):
        self.assertEqual(card_hue("Salt Roads"), (zlib.crc32(b"salt roads") % 360) / 360)
        self.assertEqual(card_hue("ÇAY"), card_hue("cay"))


if __name__ == "__main__":
    unittest.main()
