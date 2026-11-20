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
from timer_fixtures import elapse, wait_until

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
from ui.overlays import settings_layout
from ui.overlays.guide import wrap_lines
from ui.overlays.painting import ui_units
from ui.overlays.scroll_indicator import LINGER_MS
from ui.overlays.settings_drawer import (
    HELP_FONT_U,
    INSET_U,
    RESTART_NOTE,
    SUB_FONT_U,
    TITLE_FONT_U,
    VALUE_FONT_U,
    SettingsDrawer,
)
from ui.overlays.settings_layout import whole
from ui.theme import PALETTE, qcolor


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


def contrast(one: QColor, other: QColor) -> float:
    """The WCAG contrast ratio of two opaque colours."""

    def luminance(color: QColor) -> float:
        def linear(c: int) -> float:
            s = c / 255
            return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4

        r, g, b = (linear(c) for c in color.getRgb()[:3])
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    high, low = sorted((luminance(one), luminance(other)), reverse=True)
    return (high + 0.05) / (low + 0.05)


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


def signed_in():
    """The sections with someone signed in to OpenSubtitles: Subtitles has its Sign out."""
    return sections("Darwin", context=m.Context(opensubtitles_signed_in=True))


def subtitles_index(key):
    """Where the row `key` is in Subtitles & AI captions (its Sign out acts at once)."""
    rows = next(s for s in signed_in() if s.key == m.SUBTITLES).rows
    return [r.key for r in rows].index(key)


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
        self.drawer.open_at(signed_in(), m.SUBTITLES)  # the keys on the sections
        self.drawer.grab()
        self.click(self.drawer.title_rect(subtitles_index(m.SIGN_OUT)).center())
        self.assertEqual(self.chosen, [m.SIGN_OUT])

    def test_a_row_that_closes_settings_guards_its_second_press_only_meanwhile(self):
        # A link's Play closes Settings on the press, and a double-click's second press would
        # land on the picture under it. What the press does next may wait in a modal window as
        # Settings closes: the guard is armed as Settings hides, spent while that window shows,
        # and never swallows the click after it.
        armed = []

        def close_and_wait(_key):
            self.drawer.hide()
            armed.append(self.drawer._second_click._open.isActive())
            elapse(QApplication.doubleClickInterval() + 80)  # the modal window, a while

        self.drawer.chosen.connect(close_and_wait)
        self.drawer.open_at(signed_in(), m.SUBTITLES)
        self.drawer.grab()
        self.click(self.drawer.title_rect(subtitles_index(m.SIGN_OUT)).center())
        self.assertEqual(self.chosen, [m.SIGN_OUT])
        self.assertEqual(armed, [True])
        self.assertFalse(self.drawer._second_click._open.isActive())  # the next click counts

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
            check(section.key, len(section.rows))  # (Files may have none)
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
                self.drawer.open_at(signed_in(), m.SUBTITLES)
                if focused:
                    self.drawer.right()
                    while self.drawer.focused_row().key != m.SIGN_OUT:
                        self.drawer.move_by(1)
                self.drawer.grab()
                point = self.drawer.title_rect(subtitles_index(m.SIGN_OUT)).center()
                self.click(point)
                self.double_click(point)
                self.assertEqual(self.chosen, [m.SIGN_OUT])

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

        for key in keys:  # every section has one (2.12: the phone remote's and the lock too)
            self.assertGreater(accent_pixels(keys.index(key)), 0, key)

    def test_a_row_with_an_icon_paints_it_in_the_accent_after_a_gap(self):
        # ＋ Add provider and ＋ Free channels…, as the channel list's action rows: the ＋ in
        # the accent before the title, the first of them a gap below the providers; a row
        # without one keeps its title at the row's start (no room kept for an icon).
        layer = Section("demo", "Demo TV", rows=(m.Row("x", "X"),))
        self.drawer.open_at(
            (
                Section(
                    "p",
                    "Providers",
                    rows=(
                        m.Row("demo", "Demo TV", section=layer, sub="Xtream · in use"),
                        m.Row("add", "Add provider", kind=m.ACTION, icon="plus"),
                        m.Row("free", "Free channels…", kind=m.ACTION, icon="plus"),
                    ),
                ),
            )
        )
        image = self.drawer.grab().toImage()
        plain, add, free = (self.drawer._box("right", index) for index in range(3))
        self.assertTrue(plain.icon.isEmpty())
        self.assertLessEqual(add.icon.right(), add.title.left())
        self.assertGreater(add.title.left(), plain.title.left())
        self.assertEqual(add.title.left(), free.title.left())
        self.assertGreater(add.line.top(), add.rect.top())  # the gap, above the first only
        self.assertEqual(free.line.top(), free.rect.top())
        self.assertLessEqual(plain.rect.bottom(), add.rect.top() + 0.01)
        icon = add.icon.toAlignedRect()
        accent = qcolor(PALETTE.accent)
        painted = [
            image.pixelColor(x, y)
            for y in range(icon.top(), icon.bottom())
            for x in range(icon.left(), icon.right())
        ]
        self.assertTrue(any(near(color, accent) for color in painted))
        # A click in the gap reaches no row; one on the row acts.
        self.click(QPointF(add.line.center().x(), (add.rect.top() + add.line.top()) / 2))
        self.assertEqual(self.chosen, [])
        self.click(add.line.center())
        self.assertEqual(self.chosen, ["add"])

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
        # A help too long to share its three lines (a small window's) with the note: the note
        # keeps the last one, whole.
        self.use(*make(640, 360))
        help_text = (
            "Lets the graphics chip decode the video, so that the processor does less and the"
            " fans of a small computer stay quiet while a film or a channel plays."
        )
        row = Row("long", "Long", help_text, ON_OFF, True, restart=True)
        self.drawer.open_at((Section("s", "S", rows=(row,)),))
        self.drawer.right()
        self.drawer.grab()
        lines = self.drawer._box("right", 0).help
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[1].endswith("…"), lines)
        self.assertEqual(lines[-1], translate(RESTART_NOTE))

    def test_a_small_window_gives_a_help_a_title_and_a_value_a_line_more(self):
        # At 640×360 the columns shrink with the window but the fonts stop at their floor:
        # a help (its restart note too), a title and a stepper's value take a third line
        # before they elide, and a value's long word widens its room, the title wrapping
        # instead. A window wide enough for every font keeps two lines (1280, 1600).
        for width in (1280, 1600):
            un = ui_units(width)
            for font_u, most in (
                (HELP_FONT_U, settings_layout.HELP_LINES),
                (TITLE_FONT_U, settings_layout.TITLE_LINES),
                (VALUE_FONT_U, settings_layout.VALUE_LINES),
            ):
                self.assertEqual(settings_layout.most_lines(un, font_u, most), most, width)
        self.addCleanup(i18n.set_language, "en")
        self.assertEqual(i18n.set_language("tr"), "tr")
        self.use(*make(640, 360))
        self.drawer.open_at(sections("Darwin"), m.GENERAL)
        self.drawer.right()  # Language: "QiTV'nin menülerinin ve metinlerinin dili." + restart
        self.drawer.grab()
        lines = self.drawer._box("right", 0).help
        self.assertEqual(len(lines), 3, lines)
        language = next(s for s in sections("Darwin") if s.key == m.GENERAL).rows[0]
        self.assertEqual(" ".join(lines).split(), tip(language).split())  # whole
        advanced = next(s for s in sections("Darwin") if s.key == m.ADVANCED)
        self.drawer.open_at(sections("Darwin"), m.ADVANCED)
        self.drawer.grab()
        index = [r.key for r in advanced.rows].index("detailed_log")
        title = translate(advanced.rows[index].text)  # "Ayrıntılı günlük (sorun gidermek için)"
        titles = self.drawer._box("right", index).titles
        self.assertEqual(len(titles), 3, titles)
        self.assertTrue(whole(titles, title), titles)
        source = Row(
            "source",
            "Rehber kaynağı",
            "",
            (m.Choice("stb", "Sağlayıcının kendi rehberi", literal=True),),
            "stb",
            literal=True,
        )
        self.drawer.open_at((Section("s", "S", rows=(source,)),))
        self.drawer.grab()
        box = self.drawer._box("right", 0)
        self.assertTrue(whole(box.values, "Sağlayıcının kendi rehberi"), box.values)
        value_fm = QFontMetricsF(ui_units(640).font(VALUE_FONT_U))
        title_fm = QFontMetricsF(ui_units(640).font(TITLE_FONT_U))
        for line in box.values:
            self.assertLessEqual(value_fm.horizontalAdvance(line), box.value.width() + 0.5)
        self.assertTrue(whole(box.titles, "Rehber kaynağı"), box.titles)
        for line in box.titles:
            self.assertLessEqual(title_fm.horizontalAdvance(line), box.title.width() + 0.5)
        self.assertLessEqual(box.title.right(), box.less.left() + 0.5)

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

    # ---- long lists and long values (plan 3) ------------------------------------------------

    def mouse(self, widget, kind, point, buttons=Qt.MouseButton.NoButton):
        """A mouse event at `point` (`widget`'s coordinates), as Qt reports it: a press or a
        release of the left button, or a move with `buttons` held."""
        where = QPointF(point)
        moving = kind == QEvent.Type.MouseMove
        event = QMouseEvent(
            kind,
            where,
            QPointF(widget.mapToGlobal(where.toPoint())),
            Qt.MouseButton.NoButton if moving else Qt.MouseButton.LeftButton,
            buttons,
            Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(widget, event)

    def move_to(self, point):
        self.mouse(self.drawer, QEvent.Type.MouseMove, point)

    def test_a_long_column_shows_its_scroll_indicator(self):
        # Forty rows in a small window: the right column's thumb shows while it scrolls and
        # while the pointer is over it, in the column's side padding (never over a row's ‹ or
        # ›), and dragging it scrolls the column; the sections, which fit, have none.
        self.use(*make(640, 360))
        rows = tuple(Row(f"row{i}", f"Row {i}", "Its help.", ON_OFF, True) for i in range(40))
        self.drawer.open_at((Section("long", "Long", rows=rows), Section("short", "Short")))
        right = self.drawer.scroll_indicator("right")
        left = self.drawer.scroll_indicator("left")
        self.assertTrue(right.scrollable())
        self.assertFalse(left.scrollable())
        self.assertFalse(right.isVisible())  # not until it scrolls, or the pointer comes
        self.drawer.right()
        self.drawer.move_by(39)
        self.drawer.grab()
        self.assertTrue(right.isVisible())
        self.assertFalse(left.isVisible())
        strip = QRectF(right.geometry())
        self.assertTrue(self.drawer.column_rect("right").contains(strip), strip)
        for i in range(40):
            for delta in (-1, 1):
                self.assertFalse(strip.intersects(self.drawer.step_rect(i, delta)), (i, delta))
        thumb = right.thumb_rect()
        self.assertFalse(thumb.isEmpty())
        self.assertAlmostEqual(thumb.bottom(), right.height(), delta=1)  # at the end
        # Dragged to the top, the column shows its first rows; the focus stays.
        press = thumb.center()
        self.mouse(right, QEvent.Type.MouseButtonPress, press, Qt.MouseButton.LeftButton)
        self.mouse(
            right,
            QEvent.Type.MouseMove,
            QPointF(press.x(), press.y() - right.height()),
            Qt.MouseButton.LeftButton,
        )
        self.mouse(right, QEvent.Type.MouseButtonRelease, press)
        self.drawer.grab()
        self.assertFalse(self.drawer.row_rect("right", 0).isEmpty())
        self.assertTrue(self.drawer.row_rect("right", 39).isEmpty())
        self.assertEqual(self.drawer.focused_row().key, "row39")
        self.assertAlmostEqual(right.thumb_rect().top(), 0, delta=1)
        elapse(LINGER_MS + 100)
        self.assertFalse(right.isVisible())  # it fades once nothing moves
        # The pointer over the column shows it, and it fades after the pointer left.
        self.move_to(self.drawer.row_rect("right", 2).center())
        self.assertTrue(right.isVisible())
        self.move_to(self.drawer.row_rect("left", 0).center())
        self.assertFalse(left.isVisible())  # the sections fit
        elapse(LINGER_MS + 100)
        self.assertFalse(right.isVisible())
        # Shown as Settings went, it is not there when it opens on rows that fit.
        self.move_to(self.drawer.row_rect("right", 2).center())
        self.assertTrue(right.isVisible())
        self.drawer.hide()
        self.drawer.open_at((Section("short", "Short", rows=rows[:3]),))
        self.drawer.grab()
        self.assertFalse(right.isVisible())
        self.assertFalse(right.scrollable())

    def test_a_long_list_lays_out_once_per_change(self):
        # The pointer moving over a long list never wraps a line again: each line is laid out
        # once per change (the sections, a value, the window's size), not at each mouse move.
        rows = tuple(
            Row(f"row{i}", f"Row {i} of a long list of settings", "Its help.", ON_OFF, True)
            for i in range(60)
        )
        section = (Section("long", "Long", rows=rows),)
        self.drawer.open_at(section)
        self.drawer.right()
        self.drawer.move_by(3)
        self.drawer.grab()
        once = len(rows) + 1 + 3  # a title each (the section's too), the focused row's help

        def moves():
            for i in range(50):
                column = self.drawer.column_rect("left" if i % 7 == 0 else "right")
                self.move_to(QPointF(column.center().x(), 60 + (i * 37) % 600))
                self.drawer.tooltip_at(QPointF(column.center().x(), 60 + (i * 53) % 600))
                if i % 10 == 0:
                    self.drawer.grab()  # its paints too

        with mock.patch.object(settings_layout, "wrap", wraps=settings_layout.wrap) as wrap:
            moves()
            self.assertEqual(wrap.call_count, 0)
            for name, change in (
                ("a value", self.drawer.refresh),
                ("the sections", lambda: self.drawer.set_sections(section)),
                ("the size", lambda: self.host.resize(1100, 700)),
            ):
                with self.subTest(change=name):
                    wrap.reset_mock()
                    change()
                    self.assertTrue(wait_until(lambda: self.drawer.height() == self.host.height()))
                    self.drawer.grab()
                    moves()
                    self.assertGreater(wrap.call_count, 0)  # laid out again,
                    self.assertLessEqual(wrap.call_count, once)  # once

    def test_a_long_value_wraps_instead_of_cutting(self):
        # "4K-Versionen (Jellyfin, Emby)  ‹ Auf einem 4K-Bildschirm ›" at 1280×720: the value,
        # wider than half the row, takes a second line as a title does, whole, and its row
        # grows; nothing overlaps, and nothing is cut short to read in a tooltip.
        self.addCleanup(i18n.set_language, "en")
        fm = QFontMetricsF(ui_units(1280).font(VALUE_FONT_U))
        for language, key, english in (
            ("de", "media_4k_versions", "On a 4K screen"),
            ("es", "live_latency", "Auto (recommended)"),
        ):
            with self.subTest(language=language):
                self.assertEqual(i18n.set_language(language), language)
                self.drawer.open_at(sections("Darwin"), PLAYBACK)
                image = self.drawer.grab().toImage()
                index = playback_index(key)
                box = self.drawer._box("right", index)
                value = translate(english)
                self.assertGreater(fm.horizontalAdvance(value), box.value.width())
                self.assertEqual(len(box.values), 2, box.values)
                self.assertTrue(whole(box.values, value), box.values)
                for line in box.values:
                    self.assertLessEqual(fm.horizontalAdvance(line), box.value.width() + 0.5)
                self.assertLessEqual(2 * fm.lineSpacing(), box.line.height())
                self.assertLessEqual(box.title.right(), box.less.left() + 0.5)
                self.assertTrue(box.line.contains(box.more))
                below = self.drawer.row_rect("right", index + 1)
                self.assertLessEqual(box.rect.bottom(), below.top() + 0.01)
                # A line painted in each half of the value's room (the gap before › is in the
                # rect: its corner is the background).
                room = box.value.adjusted(0, 0, box.more.left() - box.value.right() - 1, 0)
                half = room.height() / 2
                for part in (room.adjusted(0, 0, 0, -half - 1), room.adjusted(0, half + 1, 0, 0)):
                    self.assertTrue(inked(image, part), (part, box.values))
                point = box.value.center()
                self.assertEqual(self.drawer.tooltip_at(point), tip(playback_rows()[index]))
        # Past its lines, it elides (a small window: three).
        i18n.set_language("en")
        self.use(*make(640, 360))
        text = (
            "A value of many words that cannot fit in three short lines at all, not even in a"
            " window much larger than this one"
        )
        long = Row("long", "Long", "", (m.Choice("a", text),), "a")
        self.drawer.open_at((Section("s", "S", rows=(long,)),))
        self.drawer.grab()
        box = self.drawer._box("right", 0)
        self.assertEqual(len(box.values), 3, box.values)
        self.assertTrue(box.values[-1].endswith("…"), box.values)
        small = QFontMetricsF(ui_units(640).font(VALUE_FONT_U))
        for line in box.values:
            self.assertLessEqual(small.horizontalAdvance(line), box.value.width() + 0.5)
        self.assertLessEqual(3 * small.lineSpacing(), box.line.height())
        self.assertIn(text, self.drawer.tooltip_at(box.value.center()))

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
        red, accent = QColor(PALETTE.danger_text), QColor(PALETTE.accent)
        self.assertFalse(near(red, accent))  # the test tells them apart
        ratio = image.devicePixelRatio()

        def reds(index):
            r = self.drawer.title_rect(index)
            return sum(
                1
                for y in range(int(r.top() * ratio), int(r.bottom() * ratio))
                for x in range(int(r.left() * ratio), int(r.right() * ratio))
                if near(image.pixelColor(x, y), red)
            )

        self.assertGreater(reds(1), 20)
        self.assertEqual(reds(0), 0)
        # Readable over the brightest picture: the right column's panel over white (2.12: the
        # live red there was 3.1:1), at the 4.5:1 normal-size text needs.
        panel = qcolor(PALETTE.panel)
        alpha = panel.alphaF()
        under = QColor(*(round(c * alpha + 255 * (1 - alpha)) for c in panel.getRgb()[:3]))
        self.assertGreaterEqual(contrast(red, under), 4.5)
        self.assertLess(contrast(QColor(PALETTE.live), under), 4.5)  # (why it is not the live red)

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
        self.assertEqual(two.subs, ["Xtream · in use"])
        self.assertGreater(two.line.height(), one.line.height())
        self.drawer.activate()  # a layer down: the left column lists them, with the sub
        self.drawer.grab()
        self.assertEqual(self.drawer._box("left", 0).subs, ["Xtream · in use"])

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

    def test_a_secret_text_shows_dots_and_is_never_said_or_copied(self):
        # A token: dots in its value's place, in the field while it is edited too; never in the
        # tooltip, never said, never copied out; what is edited and kept is the text itself.
        edits = self.edits()
        token = "eyJ.made-up-token"
        self.open_rows(m.Row("k", "TMDB token", kind=m.TEXT, secret=True), k=token)
        row = self.drawer.focused_row()
        self.drawer.grab()
        self.assertEqual(self.drawer._tail_text(row), "•" * len(token))
        tip = self.drawer.tooltip_at(self.drawer._box("right", 0).end.center())
        self.assertNotIn("eyJ", tip)
        self.assertNotIn("eyJ", self.drawer._spoken())
        self.drawer.activate()
        self.assertEqual(self.drawer._edit.text, token)  # from the text, not from its dots
        self.drawer.grab()  # painted as dots (paint_edit's `shown`)
        self.assertTrue(self.drawer.edit_key(Action.SELECT_ALL, ""))
        with mock.patch("ui.text_edit._copy") as copied:
            self.assertTrue(self.drawer.edit_key(Action.COPY, ""))  # taken, and nothing copied
            self.assertTrue(self.drawer.edit_key(Action.CUT, ""))
        copied.assert_not_called()
        self.assertEqual(self.drawer._edit.text, token)  # nor cut
        self.drawer.type_text("9")
        self.drawer.activate()
        self.assertEqual(edits, [("k", "9")])  # the selection typed over

    def test_an_info_line_shows_its_value_else_its_note(self):
        # A provider's Kind is its value; a measured size or a folder its note.
        self.open_rows(
            m.Row("kind", "Kind", kind=m.INFO),
            m.Row("files", "Downloaded files", kind=m.INFO, note="Downloaded: 880 MB"),
            m.Row("name", "Name", kind=m.TEXT),
            kind="Xtream",
        )
        self.drawer.grab()
        rows = {row.key: row for row in self.drawer._right_rows()}
        self.assertEqual(self.drawer._tail_text(rows["kind"]), "Xtream")
        self.assertEqual(self.drawer._tail_text(rows["files"]), "Downloaded: 880 MB")
        self.assertFalse(self.drawer._box("right", 1).end.isEmpty())  # room for it at the end

    def test_an_empty_text_shows_its_placeholder(self):
        self.open_rows(
            m.Row("k", "TMDB token", kind=m.TEXT, secret=True, placeholder="Built in"), k=""
        )
        row = self.drawer.focused_row()
        self.drawer.grab()
        self.assertEqual(self.drawer._tail_text(row), "Built in")
        self.assertFalse(self.drawer._box("right", 0).end.isEmpty())
        self.assertIn("Built in", self.drawer._spoken())
        self.drawer.activate()
        self.assertEqual(self.drawer._edit.text, "")  # the placeholder is no text to edit
        self.drawer.activate()
        self.values["k"] = "abc"
        self.drawer.refresh()
        self.assertEqual(self.drawer._tail_text(row), "•••")

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

    def test_a_note_cut_short_shows_whole_under_its_focused_row(self):
        # A check's answer is longer than a row's end holds: the keys and the phone have no
        # tooltip, so while its row is focused it shows whole in its help's place.
        answer = "Failed to verify provider: The username or password is wrong."
        self.open_rows(
            m.Row("check", "Check the connection", "Asks the provider.", note=answer),
            m.Row("short", "Hidden group", "Shows it again.", note="Show again"),
        )
        self.drawer.grab()
        box = self.drawer._box("right", 0)
        fm = QFontMetricsF(ui_units(1280).font(VALUE_FONT_U))
        self.assertGreater(fm.horizontalAdvance(answer), box.end.width())  # cut at the end
        self.assertTrue(whole(box.help, answer))
        self.drawer.move_by(1)
        self.assertEqual(self.drawer._box("right", 0).help, [])  # only while focused
        self.assertEqual(self.drawer._box("right", 1).help, ["Shows it again."])  # it fits

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
            sub_h = fonts["sub"].lineSpacing() * len(box.subs)
            self.assertLessEqual(spacing * len(box.titles) + sub_h, box.line.height() + 0.5, where)
            for line in box.subs:  # each sub line fits its room
                self.assertLessEqual(
                    fonts["sub"].horizontalAdvance(line), box.title.width() + 0.5, where
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
