import os
import time
import unittest
from unittest import mock
import zlib

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, Qt, Signal
from PySide6.QtGui import QFont, QFontMetricsF, QMouseEvent, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from timer_fixtures import elapse, timeline

from services.library import Details
from ui.library.poster_grid import (
    FAVORITE,
    HOVER_DWELL_MS,
    PLAY,
    REQUEST_DELAY_MS,
    START_OVER,
    PosterGrid,
    _wrap,
    card_hue,
)
from ui.logos import PosterCache

GENRE_U = 18.0  # the Library's genre column, left of the grid
NAV_U = 6.0  # the Library's top navigation, above the grid


class FakePosters(QObject):
    logoReady = Signal(str)

    def __init__(self):
        super().__init__()
        self.requested = []
        self.replace = []

    def request(self, urls, replace_pending=False):
        self.requested.append(list(urls))
        self.replace.append(replace_pending)

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

    def make(self, width=1280, height=720, posters=None):
        host = QWidget()
        host.resize(width, height)
        self.addCleanup(host.deleteLater)
        self.addCleanup(host.close)
        posters = FakePosters() if posters is None else posters
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

    def test_card_titles_are_wrapped_once_per_title_and_width(self):
        from ui.library import poster_grid

        items = [{"name": f"A fictional film with a long title, part {n}"} for n in range(30)]
        self.grid.set_sections([("", items)])
        with mock.patch.object(poster_grid, "_wrap", wraps=poster_grid._wrap) as wrap:
            self.grid.viewport().grab()
            first = wrap.call_count
            self.assertGreater(first, 0)
            self.grid.viewport().grab()  # the next frame paints the same cards again
            self.assertEqual(wrap.call_count, first)
        font = QFont()
        font.setPixelSize(14)
        fm = QFontMetricsF(font)
        title = "The title of a fictional film long enough to wrap onto several lines"
        self.assertEqual(
            self.grid._card_lines(fm, font, title, 90.0), poster_grid._wrap(fm, title, 90.0, 4)
        )
        with mock.patch.object(poster_grid, "WRAPPED_KEPT", 10):
            for n in range(40):
                self.grid._card_lines(fm, font, f"title {n}", 90.0)
            self.assertLessEqual(len(self.grid._wrapped), 10)

    def settle_requests(self):
        """Let the grid's request timer fire: paints restart it, so run the pending ones first."""
        QApplication.processEvents()
        elapse(REQUEST_DELAY_MS + 60)

    def visible_rows(self, grid, count):
        cols = grid.columns()
        view = grid.viewport().rect()
        return sorted({i // cols for i in range(count) if grid.item_rect(i).intersects(view)})

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
        QTest.qWait(QApplication.doubleClickInterval() + 50)
        point = self.grid.item_rect(5).center()
        # A real double-click is press, release, then the double-click event (no second press).
        QTest.mouseClick(self.grid.viewport(), Qt.LeftButton, Qt.NoModifier, point)
        QTest.mouseDClick(self.grid.viewport(), Qt.LeftButton, Qt.NoModifier, point)
        self.assertEqual(self.grid.current_index(), 5)
        self.assertEqual(self.named("activated"), [3, 5])

    def test_a_third_click_does_not_activate_again(self):
        self.grid.set_sections([("", titles(30))])
        point = self.grid.item_rect(0).center()
        QTest.mouseClick(self.grid.viewport(), Qt.LeftButton, Qt.NoModifier, point)
        QTest.mouseDClick(self.grid.viewport(), Qt.LeftButton, Qt.NoModifier, point)
        QTest.mouseClick(self.grid.viewport(), Qt.LeftButton, Qt.NoModifier, point)
        self.assertEqual(self.named("activated"), [0])
        QTest.qWait(QApplication.doubleClickInterval() + 50)
        QTest.mouseClick(self.grid.viewport(), Qt.LeftButton, Qt.NoModifier, point)
        self.assertEqual(self.named("activated"), [0, 0])
        self.grid.set_details_provider(lambda item: details())
        play = dict(self.buttons())[PLAY]
        QTest.qWait(QApplication.doubleClickInterval() + 50)
        for _ in range(2):
            QTest.mouseClick(self.grid.viewport(), Qt.LeftButton, Qt.NoModifier, play)
        self.assertEqual(self.named("activated"), [0, 0, 0])

    def test_button_labels(self):
        self.grid.set_sections([("", titles(3))])
        self.grid.set_details_provider(lambda item: details("series", None))
        self.assertEqual(self.grid.button_labels(), [(FAVORITE, ""), (PLAY, "Episodes")])
        self.grid.set_details_provider(lambda item: details())
        self.assertEqual(
            self.grid.button_labels(),
            [(START_OVER, "Start over"), (FAVORITE, ""), (PLAY, "Resume")],
        )
        self.grid.set_details_provider(lambda item: details(resume_ms=None))
        self.assertEqual(self.grid.button_labels(), [(FAVORITE, ""), (PLAY, "Play")])

    def test_narrow_header_keeps_the_active_chip_inside(self):
        sorts = [("added", "Recently added"), ("az", "A–Z"), ("year", "Year"), ("rating", "Rating")]
        self.grid.set_header(
            "A very long genre title for a narrow grid", "1,284 titles", sorts, "az"
        )
        wide = [self.grid.chip_rect(value) for value, _ in sorts]
        self.assertFalse(any(rect.isNull() for rect in wide))
        self.grid.setGeometry(self.grid.x(), self.grid.y(), 260, self.grid.height())
        self.assertTrue(self.grid.chip_rect("added").isNull())
        active = self.grid.chip_rect("az")
        self.assertFalse(active.isNull())
        self.assertGreaterEqual(active.left(), 0)
        self.assertLessEqual(active.right(), self.grid.width())
        self.assertFalse(self.grid.grab().isNull())
        self.grid.setGeometry(self.grid.x(), self.grid.y(), 60, self.grid.height())
        self.assertGreaterEqual(self.grid.chip_rect("az").left(), 0)
        self.assertFalse(self.grid.grab().isNull())

    def test_wrap_elides_long_words_and_extra_lines(self):
        # Only font-independent properties: the CI's Windows offscreen platform has no fonts,
        # so every glyph there is a box as wide as the pixel size.
        font = QFont()
        font.setPixelSize(14)
        fm = QFontMetricsF(font)
        lines = _wrap(fm, "Supercalifragilisticexpialidocious and more", 60, 4)
        self.assertTrue(lines[0].endswith("…"))
        self.assertTrue(all(fm.horizontalAdvance(line) <= 60 for line in lines))
        title = "The long and winding road that leads to your door " * 3
        lines = _wrap(fm, title, 100, 4)
        self.assertEqual(len(lines), 4)
        self.assertTrue(all(fm.horizontalAdvance(line) <= 100 for line in lines))
        words = title.split()
        taken = 0
        for line in lines[:-1]:  # greedy from the start: as many whole words as fit
            count = len(line.split())
            self.assertEqual(line.split(), words[taken : taken + count])
            self.assertGreater(fm.horizontalAdvance(f"{line} {words[taken + count]}"), 100)
            taken += count
        self.assertTrue(lines[-1].endswith("…"))  # the rest of the title, elided
        self.assertTrue(" ".join(words[taken:]).startswith(lines[-1][:-1]))
        self.assertEqual(_wrap(fm, "One two three", 1000, 1), ["One two three"])
        self.assertEqual(len(_wrap(fm, title, 100, 1)), 1)
        self.assertEqual(_wrap(fm, "", 100, 2), [])

    def test_wheel_scrolls(self):
        self.grid.set_sections([("", titles(200))])
        wheel(self.grid.viewport(), -240)
        self.assertGreater(self.grid.verticalScrollBar().value(), 0)
        self.assertEqual(self.grid.current_index(), 0)

    # 5
    def test_hover_dwell_makes_a_poster_current(self):
        self.grid.set_sections([("", titles(30))])
        move_mouse(self.grid.viewport(), self.grid.item_rect(4).center())
        self.assertEqual(elapse(100, self.grid.current_index), 0)
        elapse(HOVER_DWELL_MS + 50)
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
        elapse(HOVER_DWELL_MS + 50)
        self.assertEqual(self.grid.current_index(), below)
        self.assertEqual(self.grid.item_rect(below).top(), before.top())

    def test_hover_moving_on_restarts_the_dwell(self):
        self.grid.set_sections([("", titles(30))])
        move_mouse(self.grid.viewport(), self.grid.item_rect(4).center())
        _, current = timeline(
            # the pointer moves on before the first poster's dwell ends
            (150, lambda: move_mouse(self.grid.viewport(), self.grid.item_rect(5).center())),
            (300, self.grid.current_index),  # past the first dwell, before the second one ends
        )
        self.assertEqual(current, 0)
        elapse(HOVER_DWELL_MS + 50)
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
        self.grid.set_sections([("", titles(30, "Warm"))])
        self.grid.grab()  # load the fonts before timing
        self.settle_requests()
        self.posters.requested.clear()
        many = titles(20_000)
        # The GUI thread's CPU time: the work itself, not the time a loaded runner keeps the
        # thread waiting for a core.
        start = time.thread_time()
        self.grid.set_sections([("", many)])
        image = self.grid.grab()
        elapsed = time.thread_time() - start
        self.assertFalse(image.isNull())
        self.assertLess(elapsed, 0.150)
        self.assertEqual(self.posters.requested, [])  # requests wait for the view to settle
        self.settle_requests()
        cols = self.grid.columns()
        rows = self.visible_rows(self.grid, 200)
        self.assertEqual(len(self.posters.requested), 1)
        self.assertEqual(self.posters.replace, [True, True])
        requested = self.posters.requested[0]
        self.assertEqual(len(requested), len(set(requested)))  # de-duplicated
        self.assertEqual(len(requested), (len(rows) + 1) * cols)
        ahead = (rows[-1] + 1) * cols  # the first title of the row after the visible ones
        self.assertIn(many[0]["logo"], requested)
        self.assertIn(many[ahead]["logo"], requested)
        self.assertIn(many[ahead + cols - 1]["logo"], requested)
        self.assertNotIn(many[ahead + cols]["logo"], requested)

    def test_fast_scroll_queues_only_the_final_viewport(self):
        cache = PosterCache(mock.Mock(), mock.Mock(channel_logos=True, ssl_verify=True))
        cache._loader = mock.Mock()  # a batch is in flight, so new requests wait in the queue
        self.addCleanup(cache.shutdown)
        _host, grid, _ = self.make(posters=cache)
        many = titles(20_000)
        grid.set_sections([("", many)])
        bar = grid.verticalScrollBar()
        for step in range(1, 41):
            bar.setValue(bar.maximum() * step // 50)
            grid.grab()
            if step % 10 == 0:
                self.settle_requests()  # a pause long enough to request mid-scroll
        self.settle_requests()
        cols = grid.columns()
        rows = self.visible_rows(grid, len(many))
        allowed = {
            many[i]["logo"] for i in range(rows[0] * cols, min(len(many), (rows[-1] + 2) * cols))
        }
        fully_visible = {
            many[i]["logo"]
            for i in range(rows[0] * cols, (rows[-1] + 1) * cols)
            if grid.viewport().rect().contains(grid.item_rect(i))
        }
        self.assertTrue(fully_visible)
        self.assertLessEqual(set(cache._queue), allowed)
        self.assertLessEqual(fully_visible, set(cache._queue))
        self.assertLessEqual(len(cache._queue), (len(rows) + 1) * cols)

    def test_missing_posters_are_requested_again_on_a_later_pass(self):
        self.grid.set_sections([("", titles(30))])
        self.grid.grab()
        self.settle_requests()
        first = self.posters.requested[-1]
        self.grid.viewport().repaint()
        self.settle_requests()
        self.assertEqual(len(self.posters.requested), 2)
        self.assertEqual(self.posters.requested[-1], first)  # the cache bounds the retries

    def test_poster_url_provider(self):
        items = [
            dict(item, screenshot_uri=f"http://stb.invalid/{i}.jpg")
            for i, item in enumerate(titles(3))
        ]
        self.grid.set_poster_url_provider(lambda item: item.get("screenshot_uri", ""))
        self.grid.set_sections([("", items)])
        self.grid.grab()
        self.settle_requests()
        self.assertEqual(
            self.posters.requested,
            [["http://stb.invalid/0.jpg", "http://stb.invalid/1.jpg", "http://stb.invalid/2.jpg"]],
        )

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

    def test_set_sections_keeps_the_current_title_when_asked(self):
        items = titles(30)
        self.grid.set_sections([("", items)])
        self.grid.set_current_index(12)
        focus_events = len(self.named("focusChanged"))
        self.grid.set_sections([("", list(items))], keep_current=True)
        self.assertEqual(self.grid.current_index(), 12)
        self.assertEqual(len(self.named("focusChanged")), focus_events)
        self.grid.set_sections([("", list(reversed(items)))], keep_current=True)
        self.assertIs(self.grid.item(self.grid.current_index()), items[12])
        self.assertTrue(self.grid.viewport().rect().contains(self.grid.strip_rect()))
        self.grid.set_sections([("", titles(30, "Other"))], keep_current=True)
        self.assertEqual(self.grid.current_index(), 0)

    def test_set_sections_starts_at_the_top_by_default(self):
        items = titles(200)
        self.grid.set_sections([("", items)])
        self.grid.set_current_index(120)
        self.assertGreater(self.grid.verticalScrollBar().value(), 0)
        self.grid.set_sections([("", list(items))])
        self.assertEqual(self.grid.current_index(), 0)
        self.assertEqual(self.grid.verticalScrollBar().value(), 0)
        self.assertEqual(self.named("focusChanged")[-1], 0)

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
