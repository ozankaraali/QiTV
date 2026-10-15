import os
from types import SimpleNamespace
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QWidget

from services.navigation import NavModel
from tests.test_navigation import FakeCatalog
from ui.overlays.drawer import Drawer


def make(live=None):
    catalog = FakeCatalog()
    if live is not None:
        catalog.live = live
    config = SimpleNamespace(favorites=["Two"], watch_history=[])
    nav = NavModel(catalog, config)
    host = QWidget()
    host.resize(1280, 720)
    host.show()
    drawer = Drawer(nav, None, lambda item: None, host)
    return catalog, nav, host, drawer


class DrawerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.catalog, self.nav, self.host, self.drawer = make()
        self.nav.reset_to("live", "10")
        self.played = []
        self.closed = []
        self.drawer.playRequested.connect(lambda *a: self.played.append(a))
        self.drawer.closed.connect(lambda: self.closed.append(True))

    def tearDown(self):
        self.host.close()
        self.host.deleteLater()

    def test_open_focuses_right_and_highlights_key(self):
        self.drawer.open_drawer("1")
        self.assertEqual(self.drawer.focus_side, "right")
        self.assertEqual(self.drawer.highlighted().title, "One")

    def test_move_and_activate_plays_with_siblings_and_group(self):
        self.drawer.open_drawer("1")
        self.drawer.move(1)
        self.drawer.activate()
        node, siblings, parent = self.played[0]
        self.assertEqual(node.title, "Two")
        self.assertEqual([n.title for n in siblings], ["One", "Two"])
        self.assertEqual((parent.layer, parent.key), ("group", "10"))

    def test_left_previews_other_group(self):
        self.drawer.open_drawer()
        self.drawer.left()
        self.assertEqual(self.drawer.focus_side, "left")
        before = self.nav.path[-1]
        self.drawer.move(-1)
        self.assertNotEqual(self.nav.path[-1], before)

    def test_left_twice_goes_up(self):
        self.drawer.open_drawer()
        depth = len(self.nav.path)
        self.drawer.left()
        self.drawer.left()
        self.assertEqual(len(self.nav.path), depth - 1)
        self.assertEqual(self.drawer.focus_side, "left")

    def test_right_opens_section(self):
        self.nav.open(self.nav.path[0])
        self.drawer.open_drawer()
        self.assertEqual(self.drawer.highlighted().title, "Live TV")
        self.drawer.right()
        self.assertEqual(self.nav.path[-1].layer, "section")
        self.assertEqual(self.drawer.focus_side, "right")

    def test_filter_and_escape(self):
        self.drawer.open_drawer()
        self.drawer.type_text("tw")
        self.assertEqual(self.drawer.highlighted().title, "Two")
        self.assertTrue(self.drawer.escape())
        self.assertEqual(self.drawer.filter_text, "")
        self.assertTrue(self.drawer.isVisible())
        self.assertTrue(self.drawer.escape())
        self.assertFalse(self.drawer.isVisible())
        self.assertEqual(self.closed, [True])

    def test_backspace(self):
        self.drawer.open_drawer()
        self.drawer.type_text("tw")
        self.drawer.backspace()
        self.assertEqual(self.drawer.filter_text, "t")

    def test_filter_casefold(self):
        self.catalog.live["10"] = [{"id": 7, "name": "şov"}]
        self.nav.changed.emit()
        self.drawer.open_drawer()
        self.drawer.type_text("Ş")
        self.assertEqual(self.drawer.highlighted().title, "şov")

    def test_loading_layer_renders(self):
        self.catalog.live = {}
        self.nav.changed.emit()
        self.drawer.open_drawer()
        self.assertFalse(self.drawer.grab().isNull())

    def test_error_row_retries(self):
        self.catalog.live = {}
        self.nav.changed.emit()
        self.drawer.open_drawer()
        self.catalog.groupFailed.emit("live", "10", "boom")
        self.assertTrue(self.nav.load_error(self.nav.path[-1]))
        self.assertEqual(self.drawer.message_for("right"), "Couldn't load — press Enter to retry")
        self.assertFalse(self.drawer.grab().isNull())
        before = self.catalog.requests.count(("group", "live", "10"))
        self.drawer.activate()
        self.assertEqual(self.catalog.requests.count(("group", "live", "10")), before + 1)
        self.assertIsNone(self.nav.load_error(self.nav.path[-1]))


if __name__ == "__main__":
    unittest.main()
