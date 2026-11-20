"""The Settings layer of the drawer (Settings in the drawer, plan 1): sections on the left,
the chosen section's rows on the right, its keys, its clicks and its fit in a small window."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from timer_fixtures import elapse

from services import i18n
from services.i18n import translate
from services.settings_model import (
    GUIDE,
    ON_OFF,
    PLAYBACK,
    Row,
    Section,
    choice_of,
    is_lit,
    sections,
)
from ui.overlays.guide import wrap_lines
from ui.overlays.painting import ui_units
from ui.overlays.settings_drawer import HELP_FONT_U, INSET_U, RESTART_NOTE, SettingsDrawer


def make(width=1280, height=720, values=None):
    host = QWidget()
    host.resize(width, height)
    host.show()
    values = dict(values or {})
    drawer = SettingsDrawer(
        lambda row: choice_of(row, values.get(row.key, row.default)).shown(),
        lambda row: is_lit(row, values.get(row.key, row.default)),
        host,
    )
    return host, drawer, values


def playback_rows():
    return next(s for s in sections("Darwin") if s.key == PLAYBACK).rows


def playback_index(key):
    return [r.key for r in playback_rows()].index(key)


def tip(row):
    """What hovering a row says when nothing of it is cut short: its help, and the restart
    note of a row that applies only once QiTV starts again ("" for a section)."""
    if row is None:
        return ""
    parts = (
        translate(row.help) if row.help else "",
        translate(RESTART_NOTE) if row.restart else "",
    )
    return " ".join(part for part in parts if part)


class SettingsDrawerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.use(*make())

    def use(self, host, drawer, values):
        self.host, self.drawer, self.values = host, drawer, values
        self.addCleanup(host.deleteLater)
        self.addCleanup(host.close)
        self.steps, self.chosen, self.closed = [], [], []
        drawer.stepped.connect(lambda key, delta: self.steps.append((key, delta)))
        drawer.chosen.connect(self.chosen.append)
        drawer.closed.connect(lambda: self.closed.append(True))

    def focus_on(self, key):
        """Playback open, the keys on its row `key` in the right column."""
        self.drawer.open_at(sections("Darwin"), PLAYBACK)
        self.drawer.right()
        for _ in range(20):
            row = self.drawer.focused_row()
            if row is not None and row.key == key:
                return
            self.drawer.move_by(1)
        self.fail(f"no row {key}")

    def click(self, point: QPointF):
        QTest.mouseClick(self.drawer, Qt.MouseButton.LeftButton, pos=point.toPoint())

    def wait_out_the_second_click(self):
        """A click that closed Settings swallows the next press for a double-click's time (an
        app-wide filter): let it pass, so that no later test loses its click."""
        elapse(QApplication.doubleClickInterval() + 80)

    def double_click(self, point: QPointF):
        """A double-click's second press, as Qt reports it."""
        where = QPointF(point.toPoint())
        event = QMouseEvent(
            QEvent.Type.MouseButtonDblClick,
            where,
            QPointF(self.drawer.mapToGlobal(where)),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(self.drawer, event)

    def test_opens_on_the_section_asked(self):
        self.drawer.open_at(sections("Darwin"), PLAYBACK)
        self.assertTrue(self.drawer.isVisible())
        self.assertEqual(self.drawer.path(), ("playback",))
        self.assertEqual(self.drawer.focus_side(), "left")

    def test_opens_on_the_first_section_without_one(self):
        self.drawer.open_at(sections("Darwin"))
        self.assertEqual(self.drawer.path(), ("providers",))

    def test_right_steps_a_stepper_and_left_steps_back(self):
        self.focus_on("live_latency")
        self.drawer.right()
        self.assertEqual(self.steps, [("live_latency", 1)])
        self.drawer.left()
        self.assertEqual(self.steps, [("live_latency", 1), ("live_latency", -1)])
        self.assertEqual(self.drawer.focus_side(), "right")
        self.drawer.activate()  # Enter steps on
        self.assertEqual(self.steps[-1], ("live_latency", 1))

    def test_left_on_a_plain_row_goes_to_the_sections(self):
        self.focus_on("autoplay")
        self.drawer.left()
        self.assertEqual(self.drawer.focus_side(), "left")
        self.assertEqual(self.steps, [])
        self.assertEqual(self.drawer.path(), ("playback",))
        self.drawer.left()  # on the sections: a layer up, out of Settings
        self.assertEqual(self.closed, [True])
        self.assertFalse(self.drawer.isVisible())

    def test_a_help_broken_after_a_hyphen_keeps_both_its_lines(self):
        # A line may break where there is no space (after a hyphen, in Chinese): the help and
        # the restart note that fit in two lines together are not cut short.
        note = translate("Applies when QiTV starts again.")
        units = ui_units(self.host.width())
        font = units.font(HELP_FONT_U)
        for words in range(2, 30):  # a help that breaks after a hyphen, both fitting in two lines
            help_text = "-".join(["abcdefghij"] * words)
            row = Row("hyphens", "Hyphens", help_text, ON_OFF, True, restart=True)
            self.drawer.open_at((Section("s", "S", rows=(row,)),))
            self.drawer.right()
            width = self.drawer.row_rect("right", 0).width() - 2 * INSET_U * units.u
            whole = wrap_lines(f"{help_text} {note}", font, width, 3)
            if len(whole) == 2 and whole[0].endswith("-"):
                break
        else:
            self.fail("no help breaking after a hyphen")
        self.drawer.grab()
        lines = self.drawer._box("right", 0).help
        self.assertEqual(len(lines), 2)
        self.assertFalse(any("…" in line for line in lines), lines)
        self.assertTrue(lines[-1].endswith(note), lines)

    def test_enter_opens_a_layer_and_back_climbs(self):
        self.focus_on("autoplay")
        self.drawer.activate()
        self.assertEqual(self.drawer.path(), ("playback", "autoplay"))
        self.assertEqual(self.drawer.focused_row().key, "auto_play_episodes")
        self.drawer.back()
        self.assertEqual(self.drawer.path(), ("playback",))
        self.assertEqual(self.drawer.focus_side(), "right")
        self.assertEqual(self.drawer.focused_row().key, "autoplay")
        self.drawer.back()
        self.assertEqual(self.drawer.focus_side(), "left")
        self.assertEqual(self.closed, [])
        self.drawer.back()
        self.assertEqual(self.closed, [True])
        self.assertFalse(self.drawer.isVisible())

    def test_a_legacy_section_offers_the_old_window(self):
        self.drawer.open_at(sections("Darwin"), GUIDE)
        self.assertEqual(self.drawer.path(), (GUIDE,))
        self.drawer.right()
        self.assertEqual(self.drawer.focused_row().key, "legacy:guide")
        self.drawer.activate()
        self.assertEqual(self.chosen, ["legacy:guide"])

    def test_highlighting_a_section_shows_its_rows_at_once(self):
        self.drawer.open_at(sections("Darwin"), GUIDE)
        self.drawer.move_by(-3)  # Guide → Playback
        self.assertEqual(self.drawer.path(), ("playback",))
        self.drawer.right()
        self.assertEqual(self.drawer.focused_row().key, "live_latency")

    def test_clicks_on_the_steps(self):
        self.drawer.open_at(sections("Darwin"), PLAYBACK)
        self.drawer.grab()
        self.click(self.drawer.step_rect(0, 1).center())
        self.assertEqual(self.steps, [("live_latency", 1)])
        self.click(self.drawer.step_rect(0, -1).center())
        self.assertEqual(self.steps, [("live_latency", 1), ("live_latency", -1)])
        self.assertEqual(self.drawer.focus_side(), "right")

    def test_a_click_on_an_unfocused_row_only_focuses_it(self):
        # Its help shows first, so that a click never changes what was not read ("Play in"
        # VLC by mistake); a click on the row now focused steps it on.
        self.drawer.open_at(sections("Darwin"), PLAYBACK)
        self.drawer.grab()
        index = playback_index("play_in")
        self.click(self.drawer.title_rect(index).center())
        self.assertEqual(self.steps, [])
        self.assertEqual(self.drawer.focus_side(), "right")
        self.assertEqual(self.drawer.focused_row().key, "play_in")
        self.drawer.grab()
        self.assertTrue(self.drawer._box("right", index).help)  # its help shows under it
        self.click(self.drawer.title_rect(index).center())
        self.assertEqual(self.steps, [("play_in", 1)])

    def test_the_steps_of_an_unfocused_row_step_it_at_once(self):
        self.drawer.open_at(sections("Darwin"), PLAYBACK)
        self.drawer.grab()
        index = playback_index("play_in")
        self.click(self.drawer.step_rect(index, 1).center())
        self.assertEqual(self.steps, [("play_in", 1)])
        self.assertEqual(self.drawer.focused_row().key, "play_in")

    def test_hovering_a_row_shows_its_help(self):
        # The help of a row not focused, the restart note too; nothing steps.
        self.drawer.open_at(sections("Darwin"), PLAYBACK)
        self.drawer.grab()
        rows = playback_rows()
        for key in ("play_in", "hardware_decoding", "autoplay"):
            index = playback_index(key)
            tooltip = self.drawer.tooltip_at(self.drawer.title_rect(index).center())
            self.assertEqual(tooltip, tip(rows[index]), key)
            self.assertTrue(tooltip, key)
        self.assertIn(translate(RESTART_NOTE), tip(rows[playback_index("hardware_decoding")]))
        self.assertEqual(self.steps, [])

    def test_clicks_open_a_layer_and_choose_an_action(self):
        # As a stepper: the first click focuses the row, the next one opens or acts.
        self.drawer.open_at(sections("Darwin"), PLAYBACK)
        self.drawer.grab()
        index = playback_index("autoplay")
        self.click(self.drawer.title_rect(index).center())
        self.assertEqual(self.drawer.path(), ("playback",))
        self.assertEqual(self.drawer.focused_row().key, "autoplay")
        self.drawer.grab()
        self.click(self.drawer.title_rect(index).center())
        self.assertEqual(self.drawer.path(), ("playback", "autoplay"))
        self.drawer.open_at(sections("Darwin"), GUIDE)
        self.drawer.grab()
        self.click(self.drawer.title_rect(0).center())
        self.assertEqual(self.chosen, [])
        self.drawer.grab()
        self.click(self.drawer.title_rect(0).center())
        self.assertEqual(self.chosen, ["legacy:guide"])

    def test_a_click_on_a_section_shows_its_rows(self):
        self.drawer.open_at(sections("Darwin"), PLAYBACK)
        self.drawer.right()
        self.click(self.drawer.row_rect("left", 5).center())  # Guide
        self.assertEqual(self.drawer.path(), ("guide",))
        self.assertEqual(self.drawer.focus_side(), "left")

    def test_the_back_chevron_is_back(self):
        self.focus_on("live_latency")
        self.drawer.grab()
        self.click(self.drawer.back_rect().center())
        self.assertEqual(self.drawer.focus_side(), "left")
        self.click(self.drawer.back_rect().center())
        self.assertEqual(self.closed, [True])
        self.wait_out_the_second_click()

    def test_the_help_shows_under_the_focused_row_only(self):
        self.focus_on("hardware_decoding")
        self.drawer.grab()
        focused = self.drawer.row_rect("right", 1)
        other = self.drawer.row_rect("right", 3)  # (both titles on one line)
        self.assertGreater(focused.height(), other.height())
        self.assertLessEqual(focused.bottom(), self.drawer.row_rect("right", 2).top())
        self.drawer.back()  # the sections: no help on the right
        self.drawer.grab()
        self.assertEqual(
            self.drawer.row_rect("right", 1).height(), self.drawer.row_rect("right", 3).height()
        )

    def test_every_title_shows_whole_at_the_default_size(self):
        # Keys and the phone's remote cannot hover: a long title wraps onto a second line.
        def check(where, count):
            self.drawer.right()
            self.drawer.grab()
            for side, lines in (("left", len(self.drawer._items("left"))), ("right", count)):
                for i in range(lines):
                    rect = self.drawer.row_rect(side, i)
                    self.assertFalse(rect.isEmpty(), (where, side, i))
                    point = self.drawer.title_rect(i).center() if side == "right" else rect.center()
                    row = self.drawer._items(side)[i].row
                    # Nothing cut short: only the help (a section has none).
                    self.assertEqual(self.drawer.tooltip_at(point), tip(row), (where, side, i))

        everything = sections("Darwin")
        for section in everything:
            self.drawer.open_at(everything, section.key)
            check(section.key, len(section.rows) or 1)
            for i, row in enumerate(section.rows):
                if row.section is None:
                    continue
                self.drawer.open_at(everything, section.key)
                self.drawer.right()
                self.drawer.move_by(i)
                self.drawer.activate()
                self.assertEqual(self.drawer.path(), (section.key, row.key))
                check(row.key, len(row.section.rows))

    def test_a_long_title_takes_a_second_line_and_its_row_grows(self):
        long = Row(
            "long", "Float the picture when you leave a fullscreen desktop", "", ON_OFF, True
        )
        short = Row("short", "Short", "", ON_OFF, True)
        self.drawer.open_at((Section("s", "S", rows=(long, short)),))
        self.drawer.grab()
        self.assertEqual(len(self.drawer._box("right", 0).titles), 2)
        self.assertGreater(
            self.drawer.row_rect("right", 0).height(), self.drawer.row_rect("right", 1).height()
        )
        self.assertLessEqual(self.drawer.title_rect(0).right(), self.drawer.step_rect(0, -1).left())

    def test_a_double_click_on_an_unfocused_row_steps_that_row_once(self):
        # The first press focuses the row clicked and moves the help from Hardware video
        # decoding to it, so the rows under it move: the second press is a click on that row,
        # now focused, not on the one now under the pointer.
        self.focus_on("hardware_decoding")
        self.drawer.grab()
        rect = self.drawer.row_rect("right", 3)  # Start with the last channel
        point = QPointF(self.drawer.title_rect(3).center().x(), rect.bottom() - 3)
        self.click(point)
        self.assertEqual(self.steps, [])
        self.double_click(point)
        self.assertEqual(self.steps, [("start_with_last_channel", 1)])

    def test_a_double_click_on_the_focused_row_steps_it_twice(self):
        self.focus_on("start_with_last_channel")
        self.drawer.grab()
        point = self.drawer.title_rect(playback_index("start_with_last_channel")).center()
        self.click(point)
        self.double_click(point)
        self.assertEqual(self.steps, [("start_with_last_channel", 1)] * 2)

    def test_a_double_click_takes_an_action_once(self):
        for focused in (False, True):
            with self.subTest(focused=focused):
                self.chosen.clear()
                self.drawer.open_at(sections("Darwin"), GUIDE)
                if focused:
                    self.drawer.right()
                self.drawer.grab()
                point = self.drawer.title_rect(0).center()
                self.click(point)
                self.double_click(point)
                self.assertEqual(self.chosen, ["legacy:guide"])

    def test_a_double_click_on_the_back_step_steps_back_twice(self):
        self.use(*make(640, 360))
        self.drawer.open_at(sections("Darwin"), PLAYBACK)
        self.drawer.grab()
        point = self.drawer.step_rect(0, -1).center()
        self.click(point)
        self.double_click(point)
        self.assertEqual(self.steps, [("live_latency", -1)] * 2)

    def test_a_double_click_opens_a_layer_once(self):
        self.drawer.open_at(sections("Darwin"), PLAYBACK)
        index = [r.key for r in playback_rows()].index("autoplay")
        point = self.drawer.row_rect("right", index).center()
        self.click(point)
        self.double_click(point)
        self.assertEqual(self.drawer.path(), ("playback", "autoplay"))
        self.assertEqual(self.steps, [])

    def test_the_sections_show_their_icons(self):
        self.drawer.open_at(sections("Darwin"), PLAYBACK)
        self.drawer.right()  # the left column's highlight dims: an icon stands out on it
        image = self.drawer.grab().toImage()
        ratio = image.devicePixelRatio()
        keys = [s.key for s in sections("Darwin")]

        def accent_pixels(index):
            box = self.drawer._box("left", index)
            self.assertLessEqual(box.icon.right(), box.title.left())
            r = box.icon
            return sum(
                1
                for y in range(int(r.top() * ratio), int(r.bottom() * ratio))
                for x in range(int(r.left() * ratio), int(r.right() * ratio))
                if (c := image.pixelColor(x, y)).red() > 170 and c.blue() < 140
            )

        for key in ("general", "playback", "guide", "advanced"):
            self.assertGreater(accent_pixels(keys.index(key)), 0, key)
        self.assertEqual(accent_pixels(keys.index("remote")), 0)  # none fits yet: room kept

    def test_a_small_window_elides_without_overlap(self):
        self.use(*make(640, 360))
        self.drawer.open_at(sections("Darwin"), PLAYBACK)
        self.drawer.right()
        rows = playback_rows()
        column = self.drawer.column_rect("right")
        for focus in range(len(rows)):
            self.drawer.grab()
            last_bottom = None
            for i, row in enumerate(rows):
                rect = self.drawer.row_rect("right", i)
                if rect.isEmpty():
                    continue  # scrolled out of sight
                self.assertTrue(column.contains(rect), (focus, row.key, rect, column))
                if last_bottom is not None:
                    self.assertLessEqual(last_bottom, rect.top() + 0.01, row.key)
                last_bottom = rect.bottom()
                if row.choices:
                    less = self.drawer.step_rect(i, -1)
                    self.assertLessEqual(self.drawer.title_rect(i).right(), less.left(), row.key)
                    self.assertTrue(rect.contains(self.drawer.step_rect(i, 1)), row.key)
            self.assertFalse(self.drawer.row_rect("right", focus).isEmpty())
            self.drawer.move_by(1)

    def test_the_hardware_help_and_its_restart_note_show_whole_in_turkish(self):
        # The help says nothing of the restart: the note under it does, and both fit (at 960
        # wide, the help that also said so was cut).
        self.assertEqual(i18n.set_language("tr"), "tr")
        self.addCleanup(i18n.set_language, "en")
        index = playback_index("hardware_decoding")
        for size in ((1280, 720), (960, 540)):
            with self.subTest(size=size):
                self.use(*make(*size))
                self.focus_on("hardware_decoding")
                self.drawer.grab()
                lines = self.drawer._box("right", index).help
                self.assertFalse(any("…" in line for line in lines), lines)
                self.assertEqual(" ".join(lines).split(), tip(playback_rows()[index]).split())

    def test_the_restart_note_keeps_its_line_in_a_small_window(self):
        # A help too long to share two lines with the note: the note keeps the last one.
        self.use(*make(640, 360))
        help_text = "Lets the graphics chip decode the video, so that the processor does less."
        row = Row("long", "Long", help_text, ON_OFF, True, restart=True)
        self.drawer.open_at((Section("s", "S", rows=(row,)),))
        self.drawer.right()
        self.drawer.grab()
        lines = self.drawer._box("right", 0).help
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].endswith("…"), lines)
        self.assertTrue(lines[-1].startswith("Applies"), lines)

    def test_a_value_gives_way_to_its_title_in_a_small_window(self):
        # "Mouse wheel  ‹ Volume ›": the column sized for "Change channel" would cut the title.
        self.use(*make(640, 360))
        self.focus_on("mouse_wheel_action")
        self.drawer.grab()
        index = playback_index("mouse_wheel_action")
        point = self.drawer.title_rect(index).center()
        self.assertEqual(self.drawer.tooltip_at(point), tip(playback_rows()[index]))  # no title

    def test_a_click_over_the_picture_backs_out(self):
        self.drawer.open_at(sections("Darwin"), PLAYBACK)
        point = QPointF(self.drawer.width() * 0.95, self.drawer.height() * 0.5)
        self.click(point)
        self.assertEqual(self.closed, [True])
        self.assertFalse(self.drawer.isVisible())
        self.wait_out_the_second_click()

    def test_mouse_back_and_forward_reach_the_shell(self):
        self.drawer.open_at(sections("Darwin"), PLAYBACK)
        center = self.drawer.row_rect("left", 2).center().toPoint()
        for button in (Qt.MouseButton.BackButton, Qt.MouseButton.ForwardButton):
            QTest.mouseClick(self.drawer, button, pos=center)
        self.assertEqual(self.closed, [])
        self.assertEqual(self.drawer.focus_side(), "left")

    def test_a_long_section_scrolls_to_keep_the_focus_in_sight(self):
        self.use(*make(640, 360))
        rows = tuple(Row(f"row{i}", f"Row {i}", "Its help.", ON_OFF, True) for i in range(40))
        self.drawer.open_at((Section("long", "Long", rows=rows),))
        self.drawer.right()
        self.drawer.move_by(39)
        self.drawer.grab()
        self.assertTrue(self.drawer.row_rect("right", 0).isEmpty())
        last = self.drawer.row_rect("right", 39)
        self.assertFalse(last.isEmpty())
        self.assertTrue(self.drawer.column_rect("right").contains(last))
        over = self.drawer.row_rect("right", 39).center()
        wheel = QWheelEvent(
            over,
            self.drawer.mapToGlobal(over),
            QPoint(),
            QPoint(0, 120 * 50),  # up, to the top
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase,
            False,
        )
        QApplication.sendEvent(self.drawer, wheel)
        self.assertFalse(self.drawer.row_rect("right", 0).isEmpty())
        self.assertEqual(self.drawer.focused_row().key, "row39")  # the wheel only scrolls

    def test_a_parent_row_on_the_left_opens_its_own_layer(self):
        # Deeper, the left column lists the parent's rows: only those that open a layer take
        # its highlight, and that layer's rows show on the right at once.
        self.focus_on("autoplay")
        self.drawer.activate()
        keys = [r.key for r in playback_rows()]
        self.click(self.drawer.row_rect("left", keys.index("pip")).center())
        self.assertEqual(self.drawer.path(), ("playback", "pip"))
        self.assertEqual(self.drawer.focus_side(), "left")
        self.drawer.move_by(-1)  # the rows above only step: the highlight stays
        self.assertEqual(self.drawer.path(), ("playback", "pip"))
        self.drawer.move_by(1)
        self.assertEqual(self.drawer.path(), ("playback", "autoplay"))
        self.drawer.left()  # a layer up, the focus staying left: on the sections
        self.assertEqual(self.drawer.path(), ("playback",))
        self.assertEqual(self.drawer.focus_side(), "left")
        self.drawer.right()
        self.assertEqual(self.drawer.focused_row().key, "autoplay")


if __name__ == "__main__":
    unittest.main()
