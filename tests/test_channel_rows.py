import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time
import unittest

from PySide6.QtCore import QModelIndex, QObject, QPoint, QPointF, QRect, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QStyle, QStyleOptionViewItem, QWidget
from timer_fixtures import elapse, timeline, wait_until

from ui.overlay_host import OverlayHost
from ui.overlays.channel_column import ChannelColumn
from ui.overlays.channel_rows import (
    ChannelListView,
    ChannelRowDelegate,
    ChannelRowsModel,
)


class FakeLogos(QObject):
    logoReady = Signal(str)

    def __init__(self):
        super().__init__()
        self.requested = []
        self.pixmaps = {}

    def request(self, urls):
        self.requested.append(list(urls))

    def pixmap(self, url):
        return self.pixmaps.get(url)


def now_provider(item):
    return ("Show " + item["name"], 0.5) if item.get("epg") else None


def items(n=5):
    return [
        {"name": f"Chan {i}", "number": 100 + i, "logo": f"http://x/{i}.png", "epg": i % 2 == 0}
        for i in range(n)
    ]


class ModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_roles(self):
        model = ChannelRowsModel(FakeLogos(), now_provider)
        model.set_channels(items())
        self.assertEqual(model.rowCount(), 5)
        idx = model.index(0)
        self.assertEqual(idx.data(Qt.DisplayRole), "Chan 0")
        self.assertEqual(idx.data(ChannelRowsModel.NumberRole), 100)
        self.assertEqual(idx.data(ChannelRowsModel.NowRole), "Show Chan 0")
        self.assertEqual(idx.data(ChannelRowsModel.ProgressRole), 0.5)
        idx = model.index(1)
        self.assertEqual(idx.data(ChannelRowsModel.NowRole), "")
        self.assertEqual(idx.data(ChannelRowsModel.ProgressRole), -1)
        self.assertEqual(model.item(1)["name"], "Chan 1")

    def test_logo_lazy_and_ready(self):
        logos = FakeLogos()
        model = ChannelRowsModel(logos, now_provider)
        model.set_channels(items())
        self.assertEqual(logos.requested, [])
        self.assertIsNone(model.index(0).data(ChannelRowsModel.LogoRole))
        model.index(0).data(ChannelRowsModel.LogoRole)
        self.assertEqual({u for batch in logos.requested for u in batch}, {"http://x/0.png"})
        changed = []
        model.dataChanged.connect(lambda a, b, r=None: changed.append(a.row()))
        pm = QPixmap(4, 4)
        pm.fill(QColor("red"))
        logos.pixmaps["http://x/0.png"] = pm
        logos.logoReady.emit("http://x/0.png")
        self.assertEqual(changed, [0])
        self.assertIsNotNone(model.index(0).data(ChannelRowsModel.LogoRole))

    def test_an_evicted_logo_is_requested_again(self):
        logos = FakeLogos()
        model = ChannelRowsModel(logos, now_provider)
        model.set_channels(items(1))
        url = "http://x/0.png"
        model.index(0).data(ChannelRowsModel.LogoRole)
        pm = QPixmap(4, 4)
        logos.pixmaps[url] = pm
        logos.logoReady.emit(url)
        logos.requested.clear()
        shown = model.index(0).data(ChannelRowsModel.LogoRole)
        self.assertEqual(shown.cacheKey(), pm.cacheKey())
        self.assertEqual(logos.requested, [])  # in the cache: no request
        del logos.pixmaps[url]  # the cache evicted it (byte budget)
        self.assertIsNone(model.index(0).data(ChannelRowsModel.LogoRole))
        self.assertEqual(logos.requested, [[url]])  # LogoCache de-duplicates repeats

    def test_a_ready_logo_updates_its_rows_without_scanning_every_row(self):
        class Counting(dict):
            reads = 0

            def get(self, key, default=None):
                Counting.reads += 1
                return super().get(key, default)

        logos = FakeLogos()
        model = ChannelRowsModel(logos, now_provider)
        rows = [Counting(item) for item in items(2000)]
        rows[7]["logo"] = rows[1500]["logo"] = "http://x/shared.png"
        model.set_channels(rows)
        changed = []
        model.dataChanged.connect(lambda a, b, r=None: changed.append(a.row()))
        Counting.reads = 0
        logos.logoReady.emit("http://x/shared.png")
        self.assertEqual(sorted(changed), [7, 1500])
        self.assertLess(Counting.reads, 10)  # a url → rows index, not a scan of 2000 rows

    def test_unnumbered_channels_show_their_list_position(self):
        model = ChannelRowsModel(None, now_provider)
        model.set_channels([{"name": f"Chan {i}"} for i in range(3)])
        numbers = [model.index(r).data(ChannelRowsModel.NumberRole) for r in range(3)]
        self.assertEqual(numbers, ["1", "2", "3"])  # what the banner shows and digits tune

    def test_numbers_win_when_any_channel_has_one(self):
        model = ChannelRowsModel(None, now_provider)
        model.set_channels([{"name": "A", "number": "7"}, {"name": "B"}])
        numbers = [model.index(r).data(ChannelRowsModel.NumberRole) for r in range(2)]
        self.assertEqual(numbers, ["7", None])  # as ZapContext.by_number

    def test_given_numbers_are_shown_for_filtered_rows(self):
        model = ChannelRowsModel(None, now_provider)
        model.set_channels([{"name": "C"}], numbers=["3"])
        self.assertEqual(model.index(0).data(ChannelRowsModel.NumberRole), "3")

    def test_refresh_now_recomputes(self):
        calls = []

        def provider(item):
            calls.append(item["name"])
            return None

        model = ChannelRowsModel(None, provider)
        model.set_channels(items(2))
        model.index(0).data(ChannelRowsModel.NowRole)
        model.index(0).data(ChannelRowsModel.NowRole)
        self.assertEqual(len(calls), 1)
        model.refresh_now()
        model.index(0).data(ChannelRowsModel.NowRole)
        self.assertEqual(len(calls), 2)

    def test_set_channels_speed(self):
        model = ChannelRowsModel(FakeLogos(), now_provider)
        data = items(5000)
        # The GUI thread's CPU time: a loaded runner's wait for a core is not the model's.
        start = time.thread_time()
        model.set_channels(data)
        self.assertLess(time.thread_time() - start, 0.1)
        self.assertEqual(model.rowCount(), 5000)


class DelegateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_size_hint_scales(self):
        view = ChannelListView()
        delegate = ChannelRowDelegate()
        option = QStyleOptionViewItem()
        option.widget = view
        view.units_width = 1000
        h1 = delegate.sizeHint(option, QModelIndex()).height()
        view.units_width = 2000
        h2 = delegate.sizeHint(option, QModelIndex()).height()
        self.assertEqual(h1, 66)
        self.assertEqual(h2, 132)
        self.assertEqual(ChannelRowDelegate(2.0).sizeHint(option, QModelIndex()).height(), 264)


class RowFocusTests(unittest.TestCase):
    """Live rows dim their highlight when their column is not focused (as the other rows)."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def highlight_red(self, focused):
        view = ChannelListView()
        view.units_width = 1000
        if focused is not None:
            view.setProperty("columnFocused", focused)
        model = ChannelRowsModel(None, now_provider)
        model.set_channels(items(1))
        option = QStyleOptionViewItem()
        option.rect = QRect(0, 0, 600, 66)
        option.state = QStyle.StateFlag.State_Selected
        option.widget = view
        image = QImage(600, 66, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor("black"))
        painter = QPainter(image)
        ChannelRowDelegate(1.0, view).paint(painter, option, model.index(0))
        painter.end()
        view.deleteLater()
        return image.pixelColor(592, 33).red()  # the fill, right of the text

    def test_an_unfocused_column_dims_its_highlight(self):
        focused, unfocused = self.highlight_red(True), self.highlight_red(False)
        self.assertLess(unfocused, focused)

    def test_a_view_without_columns_counts_as_focused(self):
        self.assertEqual(self.highlight_red(None), self.highlight_red(True))  # the zap column


class ColumnTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make(self, idle_ms=2500):
        host = QWidget()
        host.resize(1280, 720)
        col = ChannelColumn(FakeLogos(), now_provider, parent=host, idle_ms=idle_ms)
        return host, col

    def test_open_highlight_close(self):
        host, col = self.make(idle_ms=60)
        host.show()
        col.open("Docs", items(8), 2)
        self.assertTrue(col.isVisible())
        self.assertEqual(col.current_index(), 2)
        self.assertEqual(col.geometry().width(), 512)
        self.assertEqual(col.geometry().height(), 720)
        col.highlight(3)
        self.assertEqual(col.current_index(), 3)
        self.assertFalse(col.grab().isNull())
        closed = []
        col.closed.connect(lambda: closed.append(1))
        self.assertTrue(wait_until(lambda: not col.isVisible()))  # idle, then the fade
        self.assertEqual(closed, [1])

    def test_opening_again_lays_the_list_out_only_after_a_resize(self):
        from unittest import mock

        host, col = self.make()
        host.show()
        col.open("Docs", items(8), 2)
        col.close()
        with mock.patch.object(ChannelListView, "doItemsLayout", autospec=True) as layout:
            col.open("Docs", items(9), 3)
            self.assertEqual(layout.call_count, 0)
            self.assertEqual(col.current_index(), 3)
            host.resize(960, 540)
            self.assertEqual(layout.call_count, 1)
        self.assertEqual(col.geometry().width(), 384)

    def test_highlight_restarts_timer(self):
        host, col = self.make(idle_ms=200)
        host.show()
        col.open("Docs", items(8), 0)
        # Every 80 ms a step restarts the 200 ms idle: at 320 ms it is still open.
        *_, shown = timeline(
            (80, lambda: col.highlight(1)),
            (160, lambda: col.highlight(2)),
            (240, lambda: col.highlight(3)),
            (320, col.isVisible),
        )
        self.assertTrue(shown)

    def test_the_fade_lets_the_banner_behind_show_through(self):
        host, col = self.make()
        behind = QWidget(host)  # e.g. the banner of the first press of a zap burst
        behind.setGeometry(0, 0, 1280, 720)
        behind.setAutoFillBackground(True)
        palette = behind.palette()
        palette.setColor(behind.backgroundRole(), QColor("blue"))
        behind.setPalette(palette)
        behind.lower()
        host.show()
        col.open("Docs", items(8), 0)
        shot = host.grab().toImage()
        fade_x = round(col.width() * 0.97)  # where the column is nearly transparent
        self.assertGreater(shot.pixelColor(fade_x, 360).blue(), 150)

    def hosted_column(self, idle_ms):
        window = QWidget()
        window.resize(1280, 720)
        surface = QWidget(window)
        surface.setGeometry(0, 0, 1280, 720)
        self.addCleanup(window.deleteLater)
        self.addCleanup(window.close)
        host = OverlayHost(window, surface)
        col = ChannelColumn(FakeLogos(), now_provider, parent=host, idle_ms=idle_ms)
        host.add_overlay(col)
        window.show()
        col.open("Docs", items(8), 0)
        self.assertTrue(QTest.qWaitForWindowExposed(host))
        col.highlight(0)  # a zap step: the idle time starts now
        return host, col

    def test_hover_keeps_the_column_open(self):
        host, col = self.hosted_column(idle_ms=200)
        for x in (240, 250, 260):  # onto the column's rows, moving
            QTest.mouseMove(host.windowHandle(), QPoint(x, x + 100))
        elapse(600)
        self.assertTrue(col.isVisible())  # the pointer rests on it, about to click a row
        QTest.mouseMove(host.windowHandle(), QPoint(1000, 300))  # off the column
        self.assertTrue(wait_until(lambda: not col.isVisible(), 1000))

    def test_a_pointer_resting_where_the_column_opens_does_not_hold_it(self):
        host, col = self.hosted_column(idle_ms=200)
        QTest.mouseMove(host.windowHandle(), QPoint(200, 300))  # one event, no movement
        self.assertTrue(wait_until(lambda: not col.isVisible(), 1000))

    def test_row_click(self):
        host, col = self.make()
        host.show()
        col.open("Docs", items(8), 0)
        got = []
        col.rowClicked.connect(got.append)
        col._view.rowClicked.emit(4)
        self.assertEqual(got, [4])


class HostedColumnInputTests(unittest.TestCase):
    """Clicks and wheel over the column stay in it (a real OverlayHost behind it)."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = QWidget()
        self.window.resize(1280, 720)
        surface = QWidget(self.window)
        surface.setGeometry(0, 0, 1280, 720)
        self.addCleanup(self.window.deleteLater)
        self.addCleanup(self.window.close)
        self.host = OverlayHost(self.window, surface)
        self.column = ChannelColumn(FakeLogos(), now_provider, parent=self.host)
        self.host.add_overlay(self.column)
        self.window.show()
        self.assertTrue(QTest.qWaitForWindowExposed(self.window))
        self.column.open("Docs", items(40), 0)
        self.assertTrue(QTest.qWaitForWindowExposed(self.host))
        self.events = []
        for name in ("clicked", "doubleClicked", "wheelSteps"):
            getattr(self.host, name).connect(lambda *a, n=name: self.events.append(n))

    def test_clicks_on_the_header_and_padding_do_not_reach_the_host(self):
        for point in (QPoint(200, 30), QPoint(8, 400), QPoint(480, 400)):
            QTest.mouseClick(self.host.windowHandle(), Qt.MouseButton.LeftButton, pos=point)
        QTest.mouseDClick(self.host.windowHandle(), Qt.MouseButton.LeftButton, pos=QPoint(200, 30))
        elapse(QApplication.doubleClickInterval() + 80)
        self.assertEqual(self.events, [])
        self.assertTrue(self.column.isVisible())

    def test_wheel_over_the_header_and_at_the_end_does_not_reach_the_host(self):
        view = self.column._view
        view.scrollToBottom()
        for point in (QPoint(200, 30), view.geometry().center()):
            QTest.wheelEvent(self.host.windowHandle(), QPointF(point), QPoint(0, -120))
        self.assertEqual(self.events, [])


if __name__ == "__main__":
    unittest.main()
