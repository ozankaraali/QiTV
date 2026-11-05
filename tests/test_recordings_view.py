"""The Library's Recordings page (2.9.7): its chips, entries and buttons, by keys and mouse."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QWidget

from services import recording_desk as desk
from ui.library.recordings_view import RecordingsView

app = QApplication.instance() or QApplication([])


def entry(key, section, title="T", *buttons, note=""):
    return desk.Entry(
        key=key,
        section=section,
        title=title,
        note=note,
        buttons=tuple(buttons) or (desk.Button("play", "Play"),),
    )


def b(name, enabled=True):
    return desk.Button(name, name.title(), enabled=enabled)


ENTRIES = [
    entry("item:a", desk.COMING, "A", b("edit"), b("cancel")),
    entry("item:b", desk.COMING, "B", b("edit", enabled=False), b("cancel")),
    desk.Entry("add:coming", desk.COMING, "", buttons=(b("add_timer"), b("open_guide"))),
    entry("file:/r/x.ts", desk.RECORDED, "X", b("play"), b("folder"), b("delete")),
    desk.Entry("add:places", desk.PLACES, "", buttons=(b("add_place"),)),
]


class RecordingsViewTests(unittest.TestCase):
    def setUp(self):
        self.view = RecordingsView()
        self.view.resize(1280, 720)
        self.view.set_content(ENTRIES, [desk.Streams("P", 2, ())], "2 scheduled")
        self.clicked, self.ups, self.filters = [], [], []
        self.view.buttonClicked.connect(lambda key, name: self.clicked.append((key, name)))
        self.view.upEdge.connect(lambda: self.ups.append(True))
        self.view.filterChanged.connect(self.filters.append)
        self.view.set_focused(True)

    def tearDown(self):
        self.view.deleteLater()

    def test_all_lists_the_sections_with_something_to_say(self):
        self.assertEqual(
            [e.key for e in self.view.entries()],
            ["item:a", "item:b", "add:coming", "file:/r/x.ts", "add:places"],
        )
        counts = self.view.counts()
        self.assertEqual((counts[desk.COMING], counts[desk.NOW]), (2, 0))
        self.assertEqual(self.view.chip_text(desk.COMING), "Coming up  2")

    def test_the_keys_go_from_the_chips_to_the_entries_and_their_buttons(self):
        self.assertTrue(self.view.on_chips())
        self.view.move_by(0, -1)
        self.assertEqual(self.ups, [True])  # ↑ from the chips: the top bar
        self.view.move_by(0, 1)
        self.assertEqual((self.view.current_key(), self.view.current_button()), ("item:a", "edit"))
        self.view.move_by(1, 0)
        self.view.activate()
        self.assertEqual(self.clicked, [("item:a", "cancel")])
        self.view.move_by(0, 1)  # B's Edit is off: its Cancel
        self.assertEqual(self.view.current_button(), "cancel")
        self.view.move_by(-1, 0)  # nothing on the left to go to
        self.assertEqual(self.view.current_button(), "cancel")
        self.assertTrue(self.view.escape())  # back to the chips
        self.assertFalse(self.view.escape())  # then the Library leaves

    def test_the_chips_filter_the_entries(self):
        self.view.move_by(1, 0)  # All → Recording now
        self.assertEqual(self.filters, [desk.NOW])
        self.assertEqual(self.view.entries(), [])
        self.view.set_filter(desk.RECORDED)
        self.assertEqual([e.key for e in self.view.entries()], ["file:/r/x.ts"])
        self.view.activate()  # Enter on the chips: the first entry
        self.assertEqual(self.view.current_key(), "file:/r/x.ts")

    def test_new_content_keeps_the_entry_the_keys_were_on(self):
        self.view.move_by(0, 1)
        self.view.move_by(0, 1)
        self.view.set_content(ENTRIES[1:], [], "")
        self.assertEqual(self.view.current_key(), "item:b")
        self.view.set_content([ENTRIES[-1]], [], "")
        self.assertEqual(self.view.current_key(), "add:places")

    def test_a_click_on_a_button_presses_it(self):
        self.view.set_filter(desk.RECORDED)
        index = [e.key for e in self.view.entries()].index("file:/r/x.ts")
        (_play, _rect), (folder, rect) = self.view.button_rects(index)[:2]
        point = rect.center()
        for kind in (QMouseEvent.Type.MouseButtonPress, QMouseEvent.Type.MouseButtonRelease):
            event = QMouseEvent(
                kind,
                point,
                self.view.mapToGlobal(point),
                Qt.LeftButton,
                Qt.LeftButton,
                Qt.NoModifier,
            )
            if kind == QMouseEvent.Type.MouseButtonPress:
                self.view.mousePressEvent(event)
            else:
                self.view.mouseReleaseEvent(event)
        self.assertEqual(self.clicked, [("file:/r/x.ts", folder.name)])

    def test_a_click_on_a_chip_filters(self):
        rect = self.view.chip_rects()[desk.PLACES]
        point = QPointF(rect.center())
        for kind in (QMouseEvent.Type.MouseButtonPress, QMouseEvent.Type.MouseButtonRelease):
            event = QMouseEvent(
                kind,
                point,
                self.view.mapToGlobal(point),
                Qt.LeftButton,
                Qt.LeftButton,
                Qt.NoModifier,
            )
            (
                self.view.mousePressEvent
                if kind == QMouseEvent.Type.MouseButtonPress
                else self.view.mouseReleaseEvent
            )(event)
        self.assertEqual(self.view.section_filter(), desk.PLACES)

    def test_an_empty_section_says_so_and_it_paints(self):
        self.view.set_filter(desk.ATTENTION)
        self.assertEqual(self.view.entries(), [])
        self.view.grab()  # paints without a fault, empty or not
        self.view.set_filter(desk.ALL)
        self.view.set_content(
            ENTRIES
            + [desk.Entry("rec:main", desk.NOW, "News", status="REC", tone=desk.REC, since=0.0)],
            [desk.Streams("P", 2, (desk.use_of("main", "large", "News", True),))],
            "1 recording now",
        )
        self.view.grab()
        self.assertTrue(self.view.tooltip_at(self.view.stream_rects()[0].center()).startswith("P"))


class CornerPlayerTests(unittest.TestCase):  # the Library's corner player covers no button
    def test_the_page_keeps_clear_of_the_corner_player_wherever_it_sits(self):
        host = QWidget()
        self.addCleanup(host.deleteLater)
        host.resize(1280, 720)
        view = RecordingsView(host)
        view.setGeometry(0, 60, 1280, 660)
        view.set_content(ENTRIES, [], "")
        player = QWidget(host)
        player.setGeometry(840, 450, 420, 236)  # bottom right
        player.hide()
        host.show()
        view.avoid(player)
        self.assertEqual(view.reserved(), (0.0, 0.0))  # hidden: the whole width
        player.show()
        self.assertGreater(view.reserved()[1], 0)
        for index in range(len(view.entries())):
            for _button, rect in view.button_rects(index):
                self.assertLess(rect.right(), 840)
        player.move(20, 450)  # dragged to the bottom left
        self.assertEqual(view.reserved()[1], 0.0)
        self.assertGreater(view.button_rects(0)[0][1].left(), 440)


if __name__ == "__main__":
    unittest.main()
