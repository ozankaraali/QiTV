"""The Settings layer of the drawer (Settings in the drawer, plan 1): sections on the left,
the chosen section's rows on the right, its keys, its clicks and its fit in a small window."""

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFontMetricsF, QImage, QMouseEvent, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from timer_fixtures import elapse

from services import i18n, settings_model as m
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
from ui.keymap import Action, key_hint
from ui.overlays.guide import wrap_lines
from ui.overlays.painting import ui_units
from ui.overlays.settings_drawer import (
    HELP_FONT_U,
    INSET_U,
    RESTART_NOTE,
    SUB_FONT_U,
    TITLE_FONT_U,
    VALUE_FONT_U,
    SettingsDrawer,
)
from ui.theme import PALETTE


def make(width=1280, height=720, values=None):
    """A host and Settings on it; `values` by row key (a stepper's unset one is its default,
    a text or info row's unset one is "")."""
    host = QWidget()
    host.resize(width, height)
    host.show()
    values = dict(values or {})

    def value_text(row):
        if not row.choices:
            return str(values.get(row.key, ""))
        return choice_of(row, values.get(row.key, row.default)).shown()

    drawer = SettingsDrawer(
        value_text,
        lambda row: bool(row.choices) and is_lit(row, values.get(row.key, row.default)),
        host,
    )
    return host, drawer, values


def near(color: QColor, target: QColor, slack: int = 24) -> bool:
    """Whether `color` is `target`, give or take the antialiasing at a glyph's edge."""
    ours = (color.red(), color.green(), color.blue())
    theirs = (target.red(), target.green(), target.blue())
    return all(abs(a - b) <= slack for a, b in zip(ours, theirs))


def inked(image: QImage, rect: QRectF) -> list[float]:
    """The x of each pixel in `rect` (every other one) unlike the rect's bottom-right corner."""
    ratio = image.devicePixelRatio()
    base = image.pixelColor(int((rect.right() - 3) * ratio), int((rect.bottom() - 3) * ratio))
    return [
        x / ratio
        for y in range(int(rect.top() * ratio), int(rect.bottom() * ratio), 2)
        for x in range(int(rect.left() * ratio), int(rect.right() * ratio), 2)
        if image.pixelColor(x, y) != base
    ]


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

    def test_one_click_opens_a_layer_or_acts(self):
        # Nothing changes in place: a row that opens a layer or acts does so on the first
        # click, focused or not, as the drawer's rows do.
        self.drawer.open_at(sections("Darwin"), PLAYBACK)
        self.drawer.grab()
        self.click(self.drawer.title_rect(playback_index("autoplay")).center())
        self.assertEqual(self.drawer.path(), ("playback", "autoplay"))
        self.assertEqual(self.drawer.focus_side(), "right")
        self.assertEqual(self.steps, [])
        self.drawer.open_at(sections("Darwin"), GUIDE)  # the keys on the sections
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
        index = playback_index("hardware_decoding")
        other = playback_index("start_with_last_channel")  # (both titles on one line)
        focused = self.drawer.row_rect("right", index)
        self.assertGreater(focused.height(), self.drawer.row_rect("right", other).height())
        self.assertLessEqual(focused.bottom(), self.drawer.row_rect("right", index + 1).top())
        self.drawer.back()  # the sections: no help on the right
        self.drawer.grab()
        self.assertEqual(
            self.drawer.row_rect("right", index).height(),
            self.drawer.row_rect("right", other).height(),
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
        index = playback_index("start_with_last_channel")
        rect = self.drawer.row_rect("right", index)
        point = QPointF(self.drawer.title_rect(index).center().x(), rect.bottom() - 3)
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

    def test_the_deinterlacing_help_shows_whole_with_its_restart(self):
        # Only back to Auto waits for a restart, so the row has no restart note: its help says
        # it, last, where an elided line would lose it.
        index = playback_index("deinterlace")
        row = playback_rows()[index]
        self.assertFalse(row.restart)
        self.addCleanup(i18n.set_language, "en")
        for language in ("en", "tr", "de", "es", "fr"):
            self.assertEqual(i18n.set_language(language), language)
            if language != "en":
                self.assertNotEqual(translate(row.help), row.help, language)
            for size in ((1280, 720), (960, 540)):
                with self.subTest(language=language, size=size):
                    self.use(*make(*size))
                    self.focus_on("deinterlace")
                    self.drawer.grab()
                    lines = self.drawer._box("right", index).help
                    self.assertFalse(any("…" in line for line in lines), lines)
                    self.assertEqual(" ".join(lines).split(), translate(row.help).split())

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

    # ---- row kinds (plan 2) ---------------------------------------------------------------

    def open_rows(self, *rows, **values):
        """One section of `rows` open, the keys on its first row that takes them; `values` by
        row key."""
        self.values.update(values)
        self.drawer.open_at((Section("p", "Provider", rows=rows),))
        self.drawer.right()

    def edits(self):
        said = []
        self.drawer.edited.connect(lambda key, text: said.append((key, text)))
        return said

    def test_info_rows_are_skipped_by_the_keys(self):
        self.open_rows(
            m.Row("kind", "Kind", kind=m.INFO),
            m.Row("name", "Name", kind=m.TEXT),
            m.Row("about", "About", kind=m.INFO),
            m.Row("check", "Check the connection"),
            m.Row("last", "Last", kind=m.INFO),
            kind="Xtream",
            name="Demo TV",
        )
        self.assertEqual(self.drawer.focused_row().key, "name")  # never the line above it
        self.drawer.move_by(1)
        self.assertEqual(self.drawer.focused_row().key, "check")
        self.drawer.move_by(1)  # only an info line under it: the focus stays
        self.assertEqual(self.drawer.focused_row().key, "check")
        self.drawer.move_by(-1)
        self.assertEqual(self.drawer.focused_row().key, "name")
        self.drawer.move_by(-1)
        self.assertEqual(self.drawer.focused_row().key, "name")
        self.drawer.grab()
        self.assertFalse(self.drawer.row_rect("right", 0).isEmpty())  # in sight above it
        self.click(self.drawer.title_rect(2).center())  # a click on one does nothing
        self.assertEqual(self.drawer.focused_row().key, "name")
        self.assertFalse(self.drawer.editing())
        self.assertEqual((self.chosen, self.steps), ([], []))
        self.assertFalse(self.drawer._items("right")[0].selectable)  # painted muted

    def test_an_info_row_shows_its_value_and_an_action_its_note(self):
        self.open_rows(
            m.Row("kind", "Kind", kind=m.INFO),
            m.Row("check", "Check the connection", note="answered 2 min ago"),
            kind="Xtream",
        )
        self.drawer.grab()
        for index in (0, 1):
            box = self.drawer._box("right", index)
            self.assertFalse(box.end.isEmpty(), index)
            self.assertLessEqual(box.title.right(), box.end.left(), index)
            self.assertTrue(self.drawer.step_rect(index, -1).isEmpty(), index)

    def test_a_danger_row_paints_red(self):
        self.open_rows(
            m.Row("plain", "Check the connection"),
            m.Row("remove", "Remove provider", kind=m.DANGER),
            m.Row("again", "Remove provider…", kind=m.DANGER),
        )
        self.drawer.back()  # the keys on the sections: no highlight over the rows
        image = self.drawer.grab().toImage()
        self.assertEqual(self.drawer._box("right", 1).titles, ["Remove provider…"])
        self.assertEqual(self.drawer._box("right", 2).titles, ["Remove provider…"])  # one "…"
        live, accent = QColor(PALETTE.live), QColor(PALETTE.accent)
        self.assertFalse(near(live, accent))  # the test tells them apart
        ratio = image.devicePixelRatio()

        def reds(index):
            r = self.drawer.title_rect(index)
            return sum(
                1
                for y in range(int(r.top() * ratio), int(r.bottom() * ratio))
                for x in range(int(r.left() * ratio), int(r.right() * ratio))
                if near(image.pixelColor(x, y), live)
            )

        self.assertGreater(reds(1), 20)
        self.assertEqual(reds(0), 0)

    def test_a_danger_row_acts_on_the_first_click(self):
        self.open_rows(m.Row("plain", "Plain"), m.Row("remove", "Remove provider…", kind=m.DANGER))
        self.drawer.grab()
        self.click(self.drawer.title_rect(1).center())
        self.assertEqual(self.chosen, ["remove"])

    def test_an_empty_layer_shows_its_line_and_keeps_the_focus_left(self):
        def ink():
            image = self.drawer.grab().toImage()
            column = self.drawer.column_rect("right")
            column.setTop(self.drawer.back_rect().bottom() + 8)
            return inked(image, column), column

        hidden = Section("hidden", "Hidden groups", empty_text="No hidden groups")
        self.open_rows(
            m.Row("name", "Name", kind=m.TEXT),
            m.Row("hidden", "Hidden groups", section=hidden),
            name="Demo TV",
        )
        self.drawer.move_by(1)
        self.drawer.activate()
        self.assertEqual(self.drawer.path(), ("p", "hidden"))
        self.assertEqual(self.drawer.focus_side(), "left")
        self.assertIsNone(self.drawer.focused_row())
        self.drawer.right()  # nothing to go to
        self.assertEqual(self.drawer.focus_side(), "left")
        xs, column = ink()
        self.assertTrue(xs)
        middle = (min(xs) + max(xs)) / 2
        self.assertLess(abs(middle - column.center().x()), 4)  # centred
        self.drawer.back()
        self.assertEqual(self.drawer.path(), ("p",))
        # A layer without its line shows nothing there; an empty section keeps the keys left.
        self.drawer.open_at((Section("e", "Empty"),))
        self.drawer.right()
        self.assertEqual(self.drawer.focus_side(), "left")
        self.assertEqual(ink()[0], [])

    def test_a_row_that_opens_a_card_asks_for_it(self):
        # A row that opens something that is no layer of rows (a card): › and `chosen`.
        self.open_rows(m.Row("card", "Address and login", kind=m.OPENS))
        self.drawer.grab()
        self.assertFalse(self.drawer._box("right", 0).chevron.isEmpty())
        self.drawer.activate()
        self.assertEqual(self.chosen, ["card"])
        self.assertEqual(self.drawer.path(), ("p",))

    def test_a_two_line_row_shows_its_sub_under_the_title(self):
        layer = Section("demo", "Demo TV", rows=(m.Row("x", "X", kind=m.TEXT),))
        self.open_rows(
            m.Row("demo", "Demo TV", section=layer, sub="Xtream · in use"),
            m.Row("free", "Free TV", section=layer),
        )
        self.drawer.grab()
        two, one = self.drawer._box("right", 0), self.drawer._box("right", 1)
        self.assertEqual(two.sub, "Xtream · in use")
        self.assertGreater(two.line.height(), one.line.height())
        self.drawer.activate()  # a layer down: the left column lists them, with the sub
        self.drawer.grab()
        self.assertEqual(self.drawer._box("left", 0).sub, "Xtream · in use")

    def test_a_row_without_help_keeps_its_height_when_focused(self):
        self.open_rows(m.Row("a", "Check the connection"), m.Row("b", "Check again"))
        self.drawer.grab()
        self.assertEqual(self.drawer._box("right", 0).help, [])
        self.assertEqual(
            self.drawer.row_rect("right", 0).height(), self.drawer.row_rect("right", 1).height()
        )

    def test_a_text_row_edits_in_place(self):
        edits = self.edits()
        self.open_rows(m.Row("k", "Name", kind=m.TEXT), k="old")
        self.drawer.grab()
        box = self.drawer._box("right", 0)
        self.assertLessEqual(box.title.right(), box.end.left())  # its value, at the right
        self.assertTrue(self.drawer.step_rect(0, 1).isEmpty())  # no ‹ ›
        self.assertFalse(self.drawer.editing())
        self.drawer.activate()  # Enter: editing in the value's place
        self.assertTrue(self.drawer.editing())
        self.drawer.grab()
        self.drawer.type_text("abc")
        self.assertEqual(edits, [])
        self.drawer.activate()
        self.assertEqual(edits, [("k", "oldabc")])
        self.assertFalse(self.drawer.editing())
        self.assertEqual(self.steps, [])

    def test_esc_puts_a_text_back(self):
        edits = self.edits()
        self.open_rows(m.Row("k", "Name", kind=m.TEXT), k="old")
        self.drawer.activate()
        self.drawer.type_text("abc")
        self.drawer.backspace()
        self.assertTrue(self.drawer.edit_key(Action.ESCAPE, ""))
        self.assertFalse(self.drawer.editing())
        self.assertEqual(edits, [])
        self.assertEqual(self.drawer.focused_row().key, "k")  # still on it, Settings open
        self.assertEqual(self.drawer.focus_side(), "right")
        self.assertEqual(self.closed, [])
        self.drawer.activate()  # again: from the value, not from what was typed
        self.drawer.activate()  # kept unchanged: nothing to say
        self.assertEqual(edits, [])
        self.drawer.activate()
        self.drawer.back()  # Back while editing is Esc: the layer stays
        self.assertFalse(self.drawer.editing())
        self.assertEqual(self.drawer.focus_side(), "right")
        self.assertEqual(edits, [])

    def test_the_keys_go_to_the_field_while_editing(self):
        edits = self.edits()
        self.open_rows(m.Row("k", "Name", kind=m.TEXT), m.Row("x", "X", "", ON_OFF, True), k="ab")
        self.drawer.activate()
        for action in (Action.NAV_LEFT, Action.NAV_LEFT, Action.NAV_RIGHT):  # the caret
            self.assertTrue(self.drawer.edit_key(action, ""))
        self.assertTrue(self.drawer.edit_key(Action.NAV_TEXT, "Z"))
        self.assertTrue(self.drawer.edit_key(Action.FORGET, ""))  # Delete: forward
        for _ in range(4):  # past the start: never a layer up
            self.assertTrue(self.drawer.edit_key(Action.NAV_BACKSPACE, ""))
        self.assertTrue(self.drawer.editing())
        self.assertTrue(self.drawer.edit_key(Action.NAV_TEXT, "new"))
        self.assertFalse(self.drawer.edit_key(Action.VOL_UP, ""))  # not the field's
        self.assertEqual((self.steps, self.closed), ([], []))
        # ↓ leaves the row as a click elsewhere does: the text kept, then the move.
        self.assertFalse(self.drawer.edit_key(Action.NAV_DOWN, ""))
        self.assertEqual(edits, [("k", "new")])
        self.assertFalse(self.drawer.editing())
        self.drawer.move_by(1)
        self.assertEqual(self.drawer.focused_row().key, "x")
        self.assertFalse(self.drawer.edit_key(Action.NAV_TEXT, "q"))  # not editing

    def test_a_click_edits_a_focused_text_row_and_a_click_elsewhere_keeps_it(self):
        edits = self.edits()
        self.open_rows(m.Row("x", "X", "", ON_OFF, True), m.Row("k", "Name", kind=m.TEXT), k="old")
        self.drawer.grab()
        self.click(self.drawer.title_rect(1).center())  # unfocused: only the focus
        self.assertFalse(self.drawer.editing())
        self.assertEqual(self.drawer.focused_row().key, "k")
        self.drawer.grab()
        self.click(self.drawer.title_rect(1).center())
        self.assertTrue(self.drawer.editing())
        self.drawer.grab()
        field = self.drawer._box("right", 1).end
        self.click(QPointF(field.left() + 2, field.center().y()))  # the caret to the start
        self.assertTrue(self.drawer.editing())
        self.drawer.type_text("Z")
        self.drawer.grab()
        self.click(self.drawer.title_rect(0).center())  # elsewhere: kept, and focused there
        self.assertEqual(edits, [("k", "Zold")])
        self.assertFalse(self.drawer.editing())
        self.assertEqual(self.drawer.focused_row().key, "x")
        self.assertEqual(self.steps, [])

    def test_a_double_click_on_an_unfocused_text_row_edits_it(self):
        self.open_rows(m.Row("x", "X", "", ON_OFF, True), m.Row("k", "Name", kind=m.TEXT), k="old")
        self.drawer.grab()
        point = self.drawer.title_rect(1).center()
        self.click(point)
        self.double_click(point)
        self.assertTrue(self.drawer.editing())
        self.double_click(point)  # its second press again: still editing
        self.assertTrue(self.drawer.editing())

    def test_paste_goes_into_the_field_while_editing(self):
        edits = self.edits()
        self.open_rows(m.Row("k", "Address", kind=m.TEXT), k="http://old.example")
        with mock.patch("ui.text_edit.clipboard_line", return_value="http://new.example"):
            self.assertFalse(self.drawer.edit_key(Action.PASTE, ""))  # not editing: not taken
            self.drawer.activate()
            self.assertTrue(self.drawer.edit_key(Action.SELECT_ALL, ""))
            self.assertTrue(self.drawer.edit_key(Action.PASTE, ""))
        self.assertTrue(self.drawer.edit_key(Action.NAV_ACTIVATE, ""))
        self.assertEqual(edits, [("k", "http://new.example")])

    def test_hiding_ends_an_edit_without_keeping_it(self):
        edits = self.edits()
        self.open_rows(m.Row("k", "Name", kind=m.TEXT), k="old")
        self.drawer.activate()
        self.drawer.type_text("abc")
        self.drawer.hide()
        self.assertFalse(self.drawer.editing())
        self.assertEqual(edits, [])
        self.open_rows(m.Row("k", "Name", kind=m.TEXT))
        self.assertFalse(self.drawer.editing())

    def test_forward_never_steps_a_value(self):
        # The mouse's Forward goes a layer down: from the sections into the rows, and into a
        # row's layer; never a step, an edit or an action.
        self.drawer.open_at(sections("Darwin"), PLAYBACK)
        self.drawer.forward()
        self.assertEqual(self.drawer.focus_side(), "right")
        self.assertEqual(self.drawer.focused_row().key, "live_latency")
        self.drawer.forward()
        self.assertEqual(self.steps, [])
        self.focus_on("autoplay")
        self.drawer.forward()
        self.assertEqual(self.drawer.path(), ("playback", "autoplay"))
        self.drawer.forward()  # a stepper in it
        self.assertEqual(self.steps, [])
        self.open_rows(
            m.Row("k", "Name", kind=m.TEXT),
            m.Row("act", "Check the connection"),
            m.Row("remove", "Remove provider…", kind=m.DANGER),
        )
        for _ in range(3):
            self.drawer.forward()
            self.assertFalse(self.drawer.editing())
            self.drawer.move_by(1)
        self.assertEqual((self.chosen, self.steps), ([], []))

    def test_focus_moves_are_announced(self):
        with mock.patch("ui.overlays.settings_drawer.announce") as said:
            self.drawer.open_at(sections("Darwin"), PLAYBACK)
            self.drawer.right()
            self.drawer.move_by(1)
            self.values["hardware_decoding"] = False
            self.drawer.refresh()
            self.drawer.refresh()  # nothing changed: nothing said
            self.drawer.back()  # the sections
        texts = [call.args[1] for call in said.call_args_list]
        self.assertEqual(
            texts,
            [
                "Settings, Playback",  # opening says what it is
                "Live latency, Auto (recommended)",
                "Hardware video decoding, On",
                "Hardware video decoding, Off",
                "Playback",
            ],
        )
        self.assertTrue(all(call.args[0] is self.drawer for call in said.call_args_list))

    # ---- plan 2, Task 2 ---------------------------------------------------------------------

    def press(self, point: QPointF):
        """A press and its release, the overlay's own handlers called (an error in them shows
        here, not only on stderr as from Qt's event loop)."""
        where = QPointF(point.toPoint())
        for kind, handler in (
            (QEvent.Type.MouseButtonPress, self.drawer.mousePressEvent),
            (QEvent.Type.MouseButtonRelease, self.drawer.mouseReleaseEvent),
        ):
            event = QMouseEvent(
                kind,
                where,
                QPointF(self.drawer.mapToGlobal(where)),
                Qt.MouseButton.LeftButton,
                (
                    Qt.MouseButton.LeftButton
                    if kind == QEvent.Type.MouseButtonPress
                    else Qt.MouseButton.NoButton
                ),
                Qt.KeyboardModifier.NoModifier,
            )
            handler(event)

    def test_settings_keeps_its_name_for_assistive_technology(self):
        # announce() names the overlay after what it says; hidden, it is "Settings" again.
        self.drawer.open_at(sections("Darwin"), PLAYBACK)
        self.assertEqual(self.drawer.accessibleName(), "Settings, Playback")
        self.drawer.right()
        self.assertEqual(self.drawer.accessibleName(), "Live latency, Auto (recommended)")
        self.drawer.hide()
        self.assertEqual(self.drawer.accessibleName(), "Settings")
        self.assertEqual(i18n.set_language("tr"), "tr")
        self.addCleanup(i18n.set_language, "en")
        self.drawer.open_at(sections("Darwin"), PLAYBACK)
        self.drawer.hide()
        self.assertEqual(self.drawer.accessibleName(), translate("Settings"))
        self.assertNotEqual(translate("Settings"), "Settings")

    def test_entering_an_empty_layer_says_its_line(self):
        hidden = Section("hidden", "Hidden groups", empty_text="No hidden groups")
        self.open_rows(m.Row("hidden", "Hidden groups", section=hidden))
        with mock.patch("ui.overlays.settings_drawer.announce") as said:
            self.drawer.activate()
        self.assertEqual(said.call_args.args[1], "Hidden groups, No hidden groups")

    def providers(self, *names, layer_rows=None):
        """A Providers section: a row per provider opening its layer, then Add provider."""

        def layer(name):
            rows = (
                layer_rows(name)
                if layer_rows is not None
                else (
                    m.Row(f"provider/{name}/name", "Name", kind=m.TEXT),
                    m.Row(f"provider/{name}/check", "Check the connection"),
                )
            )
            return Section(f"provider/{name}", name, rows=rows)

        rows = tuple(
            m.Row(f"provider/{name}", name, section=layer(name), sub="Xtream") for name in names
        )
        return (Section("providers", "Providers", rows=rows + (m.Row("add", "Add provider"),)),)

    def focus(self, key):
        """The keys onto the right column's row `key` (from its top, a row at a time)."""
        if self.drawer.focus_side() == "left":
            self.drawer.right()
        self.drawer.move_by(-len(self.drawer._right_rows()))
        for _ in range(len(self.drawer._right_rows())):
            if self.drawer.focused_row().key == key:
                return
            self.drawer.move_by(1)
        self.fail(f"no row {key}")

    def test_new_sections_keep_the_place_by_key(self):
        self.drawer.open_at(self.providers("A", "B", "C"))
        self.focus("provider/C")
        self.drawer.set_sections(self.providers("0", "A", "C"))  # one in before it, B gone
        self.assertEqual(self.drawer.focused_row().key, "provider/C")
        self.focus("add")  # the last row
        without_add = self.providers("0", "A", "C")[0].rows[:-1]
        self.drawer.set_sections((Section("providers", "Providers", rows=without_add),))
        self.assertEqual(self.drawer.focused_row().key, "provider/C")  # the row before it
        self.assertEqual(self.drawer.focus_side(), "right")

    def test_a_layer_gone_gives_way_to_its_neighbour(self):
        # In B's layer when B is removed elsewhere: never C's layer (its Remove row under the
        # keys), but the list, on C.
        self.drawer.open_at(self.providers("A", "B", "C"))
        self.focus("provider/B")
        self.drawer.activate()
        self.assertEqual(self.drawer.path(), ("providers", "provider/B"))
        self.drawer.move_by(1)
        self.drawer.set_sections(self.providers("A", "C"))
        self.assertEqual(self.drawer.path(), ("providers",))
        self.assertEqual(self.drawer.focus_side(), "right")
        self.assertEqual(self.drawer.focused_row().key, "provider/C")
        # The keys on the left column of a layer (the providers), its provider removed.
        self.focus("provider/A")
        self.drawer.activate()
        self.drawer.left()
        self.assertEqual(self.drawer.focus_side(), "left")
        self.drawer.set_sections(self.providers("C"))
        self.assertEqual(self.drawer.path(), ("providers",))
        self.assertEqual(self.drawer.focused_row().key, "provider/C")

    def test_a_neighbour_that_only_says_something_is_passed(self):
        def section(*keys):
            rows = (
                m.Row("kind", "Kind", kind=m.INFO),
                *(m.Row(key, key.title()) for key in keys),
                m.Row("last", "Last", kind=m.INFO),
            )
            return (Section("p", "P", rows=rows),)

        self.drawer.open_at(section("a", "b"))
        self.focus("b")
        self.drawer.set_sections(section("a"))  # an info line where it was
        self.assertEqual(self.drawer.focused_row().key, "a")
        self.drawer.set_sections(section())  # nothing to focus
        self.assertEqual(self.drawer.focus_side(), "left")
        self.assertIsNone(self.drawer.focused_row())

    def test_an_edit_stays_while_its_row_does(self):
        edits = self.edits()
        self.drawer.open_at(self.providers("A", "B"))
        self.focus("provider/A")
        self.drawer.activate()
        self.drawer.activate()  # its Name, edited
        self.assertTrue(self.drawer.editing())
        self.drawer.type_text("x")
        self.drawer.set_sections(self.providers("A", "C"))  # B went: A's Name is still there
        self.assertTrue(self.drawer.editing())
        self.drawer.type_text("y")
        self.drawer.set_sections(self.providers("C"))  # A went with its Name: put back
        self.assertFalse(self.drawer.editing())
        self.assertEqual(edits, [])
        self.assertFalse(self.drawer.edit_key(Action.NAV_TEXT, "z"))  # no field takes keys

    def test_a_click_after_an_edit_that_rebuilt_the_rows_is_dropped(self):
        # A rename rebuilds the rows as the edit closes: the click that closed it, on what
        # was under it before, lands only if that line is still there and the same.
        before = (
            m.Row("k", "Name", kind=m.TEXT),
            m.Row("x", "X", "", ON_OFF, True),
            m.Row("act", "Check the connection"),
            m.Row("more", "More"),
        )
        for after in (before[:2], (before[0], m.Row("new", "New"), before[2], m.Row("z", "Z"))):
            with self.subTest(after=[row.key for row in after]):
                self.chosen.clear()
                self.drawer.open_at((Section("p", "Provider", rows=before),))
                self.drawer.right()
                self.values["k"] = "old"
                self.drawer.activate()
                self.drawer.type_text("!")
                rebuilt = (Section("p", "Provider", rows=after),)
                self.drawer.edited.connect(lambda *_: self.drawer.set_sections(rebuilt))
                self.drawer.grab()
                self.press(self.drawer.title_rect(3).center())  # "More", out of range or Z
                self.drawer.edited.disconnect()
                self.assertFalse(self.drawer.editing())
                self.assertEqual(self.chosen, [])
                self.assertEqual(self.drawer.focused_row().key, "k")
        # The left column of a layer: the providers the press was over gone with the edit.
        self.drawer.open_at(self.providers("A", "B", "C"))
        self.focus("provider/A")
        self.drawer.activate()
        self.values["provider/A/name"] = "A"
        self.drawer.activate()
        self.drawer.grab()
        point = self.drawer.row_rect("left", 2).center()  # C, on the left
        self.drawer.edited.connect(lambda *_: self.drawer.set_sections(self.providers("A")))
        self.drawer.type_text("!")
        self.press(point)
        self.drawer.edited.disconnect()
        self.assertEqual(self.drawer.path(), ("providers", "provider/A"))

    def test_new_sections_follow_a_rename(self):
        # A rename changes every key of the provider's (provider/<name>/…): told so, the
        # viewer stays in its layer, on the row they were on, the keys on their side.
        self.drawer.open_at(self.providers("A", "B"))
        self.focus("provider/B")
        self.drawer.activate()
        self.drawer.move_by(1)  # its Check row
        renamed = {"provider/B": "provider/Bee"}
        self.drawer.set_sections(self.providers("A", "Bee"), renamed=renamed)
        self.assertEqual(self.drawer.path(), ("providers", "provider/Bee"))
        self.assertEqual(self.drawer.focused_row().key, "provider/Bee/check")
        self.assertEqual(self.drawer.focus_side(), "right")
        # Only a whole key or a key under it: "provider/Be" is no part of "provider/Bee".
        renamed = {"provider/Be": "provider/Q"}
        self.drawer.set_sections(self.providers("A", "Bee", "Q"), renamed=renamed)
        self.assertEqual(self.drawer.path(), ("providers", "provider/Bee"))
        self.assertEqual(self.drawer.focused_row().key, "provider/Bee/check")
        # The keys on the left column of the layer: the renamed provider stays the one open.
        self.drawer.left()
        renamed = {"provider/Bee": "provider/Cee"}
        self.drawer.set_sections(self.providers("A", "Cee", "Q"), renamed=renamed)
        self.assertEqual(self.drawer.path(), ("providers", "provider/Cee"))
        self.assertEqual(self.drawer.focus_side(), "left")
        # Without the hint, the renamed layer is one that went (plan 2, Task 2).
        self.drawer.set_sections(self.providers("A", "Dee", "Q"))
        self.assertEqual(self.drawer.path(), ("providers",))

    def test_back_after_a_rename_goes_up_once(self):
        # ‹ or the fade pressed while the name is edited: the edit is kept first. A rename
        # that rebuilt the rows drops the press (it was aimed at the layer as it was), so the
        # viewer is never taken a layer further than they saw; a name kept as it was lets
        # the press go up, once.
        def back():
            return self.drawer.back_rect().center()

        def fade():
            return QPointF(self.drawer.column_rect("right").right() + 20, 300)

        for where in (back, fade):
            for renamed in (True, False):
                with self.subTest(where=where.__name__, renamed=renamed):
                    self.drawer.open_at(self.providers("A", "B"))
                    self.focus("provider/B")
                    self.drawer.activate()
                    self.values["provider/B/name"] = "B"
                    self.drawer.activate()  # Name, edited
                    self.drawer.type_text("x")

                    def rename(*_args, renamed=renamed):
                        if renamed:
                            self.drawer.set_sections(
                                self.providers("A", "Bx"), renamed={"provider/B": "provider/Bx"}
                            )

                    self.drawer.edited.connect(rename)
                    self.drawer.grab()
                    self.press(where())
                    self.drawer.edited.disconnect(rename)
                    self.assertFalse(self.drawer.editing())
                    self.assertTrue(self.drawer.isVisible())
                    expected = ("providers", "provider/Bx") if renamed else ("providers",)
                    self.assertEqual(self.drawer.path(), expected)

    def test_the_paste_control_pastes_into_the_field(self):
        # A mouse cannot press Ctrl/⌘+V: the field has its Paste.
        edits = self.edits()
        self.open_rows(m.Row("k", "Name", kind=m.TEXT), m.Row("x", "X", "", ON_OFF, True), k="old")
        self.drawer.grab()
        self.assertTrue(self.drawer.paste_rect(0).isEmpty())  # only while editing
        self.drawer.activate()
        self.drawer.grab()
        paste, field = self.drawer.paste_rect(0), self.drawer._box("right", 0).end
        self.assertFalse(paste.isEmpty())
        self.assertTrue(field.contains(paste))  # at the field's right end
        self.assertEqual(self.drawer.tooltip_at(paste.center()), key_hint("Paste", "Ctrl+V"))
        with mock.patch("ui.text_edit.clipboard_line", return_value="Demo TV"):
            self.press(paste.center())  # in place of the text, as the cards' Paste
            self.assertTrue(self.drawer.editing())
            self.drawer.edit_key(Action.NAV_LEFT)  # the caret put in the text
            self.drawer.edit_key(Action.NAV_LEFT, "select")
            self.press(paste.center())  # a selection: the paste takes its place
            self.double_click(paste.center())  # a double-click's second press: once
        with mock.patch("ui.text_edit.clipboard_line", return_value=""):
            self.press(paste.center())  # nothing to paste: nothing changes
        self.assertEqual(edits, [])
        self.drawer.activate()
        self.assertEqual(edits, [("k", "Demo Demo TVV")])

    def test_a_small_window_keeps_subs_notes_values_and_the_field_apart(self):
        self.use(*make(640, 360))
        layer = Section("demo", "Demo TV", rows=(m.Row("x", "X"),))
        self.open_rows(
            m.Row("demo", "Demo TV Premium Sports", section=layer, sub="Xtream · in use"),
            m.Row("name", "Name", kind=m.TEXT),
            m.Row("kind", "Kind", kind=m.INFO),
            m.Row("check", "Check the connection", note="answered 2 min ago"),
            m.Row("remove", "Remove provider", kind=m.DANGER, note="2 recordings"),
            name="Demo TV Premium Sports Package",
            kind="Xtream",
        )
        un = ui_units(640)
        fonts = {
            name: QFontMetricsF(un.font(size))
            for name, size in (
                ("title", TITLE_FONT_U),
                ("sub", SUB_FONT_U),
                ("value", VALUE_FONT_U),
            )
        }
        column = self.drawer.column_rect("right")

        def check(index, editing=False):
            box = self.drawer._box("right", index)
            row = self.drawer._right_rows()[index]
            where = (row.key, editing)
            self.assertTrue(column.contains(box.rect), where)
            for line in box.titles:  # each title line fits its room
                self.assertLessEqual(
                    fonts["title"].horizontalAdvance(line), box.title.width() + 0.5, where
                )
            spacing = fonts["title"].lineSpacing()
            sub_h = fonts["sub"].lineSpacing() if box.sub else 0.0
            self.assertLessEqual(spacing * len(box.titles) + sub_h, box.line.height() + 0.5, where)
            if box.sub:
                self.assertLessEqual(
                    fonts["sub"].horizontalAdvance(box.sub), box.title.width() + 0.5, where
                )
            for part in (box.end, box.chevron, self.drawer.paste_rect(index)):
                if part.isEmpty():
                    continue
                self.assertLessEqual(box.title.right(), part.left() + 0.5, where)
                self.assertTrue(box.line.contains(part), where)
            if not box.end.isEmpty() and not box.chevron.isEmpty():
                self.assertLessEqual(box.end.right(), box.chevron.left() + 0.5, where)
            paste = self.drawer.paste_rect(index)
            if editing:
                self.assertFalse(paste.isEmpty(), where)
                self.assertGreater(
                    paste.left() - box.end.left(), fonts["value"].averageCharWidth() * 4, where
                )

        for focus in range(5):
            self.drawer.grab()
            for index in range(5):
                if not self.drawer.row_rect("right", index).isEmpty():
                    check(index)
            self.drawer.move_by(1)
        self.focus("name")
        self.drawer.activate()
        self.drawer.grab()
        check(1, editing=True)
        last_bottom = None
        for index in range(5):
            rect = self.drawer.row_rect("right", index)
            if rect.isEmpty():
                continue
            if last_bottom is not None:
                self.assertLessEqual(last_bottom, rect.top() + 0.01, index)
            last_bottom = rect.bottom()


if __name__ == "__main__":
    unittest.main()
