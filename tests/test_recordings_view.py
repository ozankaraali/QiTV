"""The Library's Recordings page (2.9.7): its chips, entries and buttons, by keys and mouse."""

import os
import unittest
import unittest.mock

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
        self.view.set_name_providers(True)  # two providers or more: each card names its own
        self.assertTrue(self.view.tooltip_at(self.view.stream_rects()[0].center()).startswith("P"))

    def test_a_mouse_move_over_many_recordings_measures_one_card(self):
        # Hundreds of recorded files: each move over the page measured every card's buttons
        # and every chip again per card (it grew with the square of the recordings).
        many = [entry(f"file:/r/{n}.ts", desk.RECORDED, f"R{n}", b("play")) for n in range(400)]
        self.view.set_content(many, [], "")
        self.view.chip_rects()  # the chips are placed once for this content and size
        measured = []
        real = RecordingsView.button_rects

        def counted(view, index):
            measured.append(index)
            return real(view, index)

        rect = self.view.entry_rect(1)  # the second card, on screen
        with unittest.mock.patch.object(RecordingsView, "button_rects", counted):
            with unittest.mock.patch.object(RecordingsView, "chip_text") as chip_text:
                target = self.view._target_at(rect.center())
        self.assertEqual(target[:2], ("entry", "file:/r/1.ts"))
        self.assertEqual(measured, [1])
        chip_text.assert_not_called()
        self.assertEqual(self.view.counts()[desk.RECORDED], 400)


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


class FindViewTests(unittest.TestCase):  # 2.9.7: the rail's find, each part headed
    def test_each_part_has_its_own_header_and_an_empty_one_is_left_out(self):
        view = RecordingsView()
        self.addCleanup(view.deleteLater)
        view.resize(1280, 720)
        recorded = [entry(f"file:/r/{k}.ts", desk.RECORDED, f"R{k}") for k in range(3)]
        found = [entry("found:0", desk.FOUND, "Late News", b("record"), b("record_series"))]
        view.set_content(recorded + found, [], "")
        view.set_section((desk.RECORDED, desk.FOUND), headed=True)
        self.assertEqual(view.header_text(), "")
        self.assertEqual(view.section_headers(), ["Recorded  ·  3", "In the guide  ·  1"])
        self.assertEqual(view.current_key(), "file:/r/0.ts")  # the first entry, not a header
        view.set_content(found, [], "")
        self.assertEqual(view.section_headers(), ["In the guide  ·  1"])
        self.assertNotIn("empty", [block.kind for block in view._blocks])  # no "No recordings"
        view.grab()

    def test_sections_together_say_their_last_as_the_filter(self):
        view = RecordingsView()
        self.addCleanup(view.deleteLater)
        said = []
        view.filterChanged.connect(said.append)
        view.set_section((desk.NOW, desk.COMING))  # a tuple can't cross a str signal
        self.assertEqual(said, [desk.COMING])


class OneProviderTests(unittest.TestCase):  # 2.9.7 review: with one provider, none is named
    def test_the_streams_card_names_its_provider_only_with_two(self):
        view = RecordingsView()
        self.addCleanup(view.deleteLater)
        view.resize(1280, 720)
        streams = [desk.Streams("Demo TV", 2, (desk.Use("main", "Watching", False),))]
        view.set_content(ENTRIES, streams, "")
        view.set_section(desk.SETTINGS)
        centre = view.stream_rects()[0].center()
        self.assertNotIn("Demo TV", view.tooltip_at(centre))
        view.set_name_providers(True)
        self.assertIn("Demo TV", view.tooltip_at(centre))
        view.grab()


if __name__ == "__main__":
    unittest.main()


class SectionViewTests(unittest.TestCase):  # 2.9.7: one section, as a row of the tab's rail
    def setUp(self):
        self.view = RecordingsView()
        self.view.resize(1280, 720)
        self.addCleanup(self.view.deleteLater)
        self.view.set_content(ENTRIES, [desk.Streams("P", 2, ())], "")
        self.ups = []
        self.view.upEdge.connect(lambda: self.ups.append(True))
        self.view.set_focused(True)

    def test_a_section_alone_shows_without_title_or_chips(self):
        self.view.set_section(desk.COMING)
        self.assertEqual(self.view.chip_rects(), {})
        self.assertEqual(self.view.stream_rects(), [])
        self.assertEqual(self.view.header_text(), "Coming up  ·  2")
        self.assertEqual([e.key for e in self.view.entries()], ["item:a", "item:b", "add:coming"])
        self.assertLess(self.view.entry_rect(0).top(), 0.15 * 720)  # no title, summary, chips
        self.view.grab()  # paints

    def test_settings_shows_places_and_streams(self):
        self.view.set_section(desk.SETTINGS)
        self.assertEqual(len(self.view.stream_rects()), 1)
        self.assertEqual([e.key for e in self.view.entries()], ["add:places"])

    def test_buttons_are_rounded_rectangles_as_the_librarys(self):
        self.view.set_section(desk.COMING)
        (_button, box), *_ = self.view.button_rects(0)
        self.assertLess(self.view.button_radius(), box.height() / 2)

    def test_up_from_the_first_row_goes_to_the_top_bar(self):
        self.view.set_section(desk.COMING)
        self.assertEqual(self.view.current_key(), "item:a")  # no chips: the first row
        self.view.move_by(0, -1)
        self.assertEqual(self.ups, [True])
        self.assertFalse(self.view.escape())  # Esc: the tab's rail


class ComingUpWithNowTests(unittest.TestCase):  # 2.9.7 review: what records or waits shows too
    def test_coming_up_lists_what_records_or_waits_first(self):
        view = RecordingsView()
        self.addCleanup(view.deleteLater)
        view.resize(1280, 720)
        now = entry("rec:sched:x", desk.NOW, "Evening News", b("watch"), b("stop"))
        waiting = entry("wait:k", desk.NOW, "Late film", b("try_now"), b("skip"))
        view.set_content([now, waiting] + ENTRIES, [], "")
        view.set_section((desk.NOW, desk.COMING))
        keys = [e.key for e in view.entries()]
        self.assertEqual(keys[:2], ["rec:sched:x", "wait:k"])
        self.assertIn("item:a", keys)
        self.assertEqual(view.header_text(), "Coming up  ·  4")
        view.grab()


class ComingUpQuietTests(unittest.TestCase):
    def test_nothing_recording_is_not_said_above_the_airings(self):
        view = RecordingsView()
        self.addCleanup(view.deleteLater)
        view.resize(1280, 720)
        view.set_content(ENTRIES, [], "")
        view.set_section((desk.NOW, desk.COMING))
        self.assertNotIn("empty", [b.kind for b in view._blocks if b.section == desk.NOW])
