import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time
import unittest

from PySide6.QtCore import QModelIndex, QObject, Qt, Signal
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QStyleOptionViewItem, QWidget

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
        self.assertEqual(logos.requested, [["http://x/0.png"]])
        changed = []
        model.dataChanged.connect(lambda a, b, r=None: changed.append(a.row()))
        pm = QPixmap(4, 4)
        pm.fill(QColor("red"))
        logos.pixmaps["http://x/0.png"] = pm
        logos.logoReady.emit("http://x/0.png")
        self.assertEqual(changed, [0])
        self.assertIsNotNone(model.index(0).data(ChannelRowsModel.LogoRole))

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
        start = time.perf_counter()
        model.set_channels(data)
        self.assertLess(time.perf_counter() - start, 0.1)
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
        QTest.qWait(300)
        self.assertFalse(col.isVisible())
        self.assertEqual(closed, [1])

    def test_highlight_restarts_timer(self):
        host, col = self.make(idle_ms=200)
        host.show()
        col.open("Docs", items(8), 0)
        for i in range(4):
            QTest.qWait(80)
            col.highlight(i)
        self.assertTrue(col.isVisible())

    def test_row_click(self):
        host, col = self.make()
        host.show()
        col.open("Docs", items(8), 0)
        got = []
        col.rowClicked.connect(got.append)
        col._view.rowClicked.emit(4)
        self.assertEqual(got, [4])


if __name__ == "__main__":
    unittest.main()
