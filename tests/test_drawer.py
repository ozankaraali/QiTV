import os
from types import SimpleNamespace
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from services.catalog import Group
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
        self.addCleanup(self.host.deleteLater)
        self.addCleanup(self.host.close)
        self.nav.reset_to("live", "10")
        self.played = []
        self.closed = []
        self.drawer.playRequested.connect(lambda *a: self.played.append(a))
        self.drawer.closed.connect(lambda: self.closed.append(True))

    def test_open_focuses_right_and_highlights_key(self):
        self.drawer.open_drawer("1")
        self.assertEqual(self.drawer.focus_side, "right")
        self.assertEqual(self.drawer.highlighted().title, "One")

    def test_move_and_activate_plays_with_siblings_and_group(self):
        self.drawer.open_drawer("1")
        self.drawer.move_by(1)
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
        self.drawer.move_by(-1)
        QTest.qWait(320)
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

    def many_groups(self, count=8):
        self.catalog.groups = lambda section: [
            Group(section, str(i), f"Group {i}", 1) for i in range(count)
        ]
        self.catalog.live = {str(i): [{"id": i, "name": f"Chan {i}"}] for i in range(count)}
        self.nav.reset_to("live", "0")
        self.opened = []
        real_open = self.nav.open
        self.nav.open = lambda node: (self.opened.append(node), real_open(node))[1]

    def test_left_preview_is_debounced(self):
        self.many_groups()
        self.drawer.open_drawer()
        self.drawer.left()
        for _ in range(5):
            self.drawer.move_by(1)
        self.assertEqual(self.opened, [])
        self.assertEqual(self.drawer.highlighted().title, "Group 5")
        QTest.qWait(320)
        self.assertEqual([n.title for n in self.opened], ["Group 5"])

    def test_right_flushes_pending_preview(self):
        self.many_groups()
        self.drawer.open_drawer()
        self.drawer.left()
        self.drawer.move_by(1)
        self.drawer.move_by(1)
        self.drawer.right()
        self.assertEqual([n.title for n in self.opened], ["Group 2"])
        self.assertEqual(self.nav.path[-1].title, "Group 2")
        self.assertEqual(self.drawer.focus_side, "right")

    def test_sources_are_highlighted_not_previewed(self):
        self.nav.open(self.nav.path[0])
        self.drawer.open_drawer()
        self.drawer.left()
        self.drawer.move_by(1)
        QTest.qWait(320)
        self.assertEqual(self.catalog.selected, [])
        self.assertEqual(self.drawer.highlighted().title, "Backup")
        self.drawer.right()
        self.assertEqual(self.catalog.selected, ["Backup"])

    def test_left_typing_highlights_and_opens_first_match(self):
        self.many_groups()
        self.drawer.open_drawer()
        self.drawer.left()
        self.drawer.type_text("group 6")
        self.assertEqual(self.drawer.highlighted().title, "Group 6")
        self.drawer.activate()
        self.assertEqual(self.nav.path[-1].title, "Group 6")
        self.assertEqual(self.drawer.focus_side, "right")

    def test_filter_folds_turkish_and_accents(self):
        self.catalog.live["10"] = [
            {"id": 7, "name": "İstanbul"},
            {"id": 8, "name": "ısı"},
            {"id": 9, "name": "Café"},
        ]
        self.nav.changed.emit()
        self.drawer.open_drawer()
        self.drawer.type_text("ist")
        self.assertEqual(self.drawer.highlighted().title, "İstanbul")
        self.drawer.escape()
        self.drawer.type_text("isi")
        self.assertEqual(self.drawer.highlighted().title, "ısı")
        self.drawer.escape()
        self.drawer.type_text("cafe")
        self.assertEqual(self.drawer.highlighted().title, "Café")

    def test_filter_resets_when_parent_changes(self):
        self.nav.open(self.nav.path[0])
        self.drawer.open_drawer()
        self.drawer.type_text("liv")
        self.assertEqual(self.drawer.filter_text, "liv")
        self.drawer.right()  # opens Live TV; the right column's parent changes
        self.assertEqual(self.drawer.filter_text, "")

    def test_page_moves_several_rows(self):
        self.catalog.live["10"] = [{"id": i, "name": f"Chan {i}"} for i in range(60)]
        self.nav.changed.emit()
        self.drawer.open_drawer()
        self.drawer.page(1)
        first = self.drawer.highlighted().title
        self.assertNotIn(first, ("Chan 0", "Chan 1"))
        self.drawer.page(-1)
        self.assertEqual(self.drawer.highlighted().title, "Chan 0")


if __name__ == "__main__":
    unittest.main()
