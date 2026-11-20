"""Where the Settings layer of the drawer (ui/overlays/settings_drawer.py) draws its lines: its
two columns and the areas their lines scroll in, the breadcrumb, and each line's parts.

A line has its title, wrapped onto a second line when it does not fit on one, and under it a
row's muted sub line ("Xtream · in use"); at its right end, by the row's kind, a stepper's
‹ value ›, a text row's value (the field it is edited in, while it is, with its Paste at the
field's end), an info row's value or a row's note ("answered 2 min ago"), and the › of a row
that opens; under a focused row, its help. Geometry only: the overlay keeps the state and
paints.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
import math
from typing import Optional

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QFont, QFontMetricsF

from services.i18n import N_, translate
from services.settings_model import DANGER, INFO, OPENS, STEP, TEXT, Row
from ui.overlays.drawer_look import (
    COUNT_FONT_U,
    CRUMB_FONT_U,
    CRUMB_H_U,
    FILTER_H_U,
    GEAR_GAP_U,
    GEAR_U,
    MIN_HIT_PX,
    PAD_SIDE_U,
    PAD_TOP_U,
    ROW_H_U,
    SEPARATOR,
    TITLE_FONT_U,
    column_widths,
)
from ui.overlays.guide import wrap_lines
from ui.overlays.painting import STEP_ICON_U, Units

LEFT, RIGHT = "left", "right"
HEAD_TOP_U = 1.0  # the lists start this far under the header row (the drawer's right column)
INSET_U = 1.2  # a row's text from its sides (the drawer's rows)
ICON_U = 1.8  # a section's icon
ICON_GAP_U = 0.7
VALUE_FONT_U = 1.2  # a row's value or note (its title is TITLE_FONT_U)
SUB_FONT_U = COUNT_FONT_U  # a row's sub line, as the drawer's two-line rows have theirs
HELP_FONT_U = 1.05
HELP_LINES = 2
HELP_PAD_U = 0.8  # under the help
TITLE_LINES = 2  # a long title wraps, its line growing; beyond this it elides (small windows)
STEP_U = 1.8  # a stepper's ‹ and › (the panels')
STEP_GAP_U = 0.6
TITLE_GAP_U = 0.8  # between a row's title and what is at its right end
VALUE_SHARE = 0.5  # a value too wide beside its whole title keeps this share of the row
FIELD_H_U = 2.6  # the field a text is edited in, this tall in its line
FIELD_PAD_U = 0.7  # the field's text from its sides
EMPTY_H_U = 6.0  # a layer without rows: its line, centred in this much of the column's top
PASTE_FONT_U = 1.05  # the field's Paste: its label
PASTE_PAD_U = 0.7  # its label from its sides
PASTE_GAP_U = 0.5  # between the field and it
ELLIPSIS = "…"
# The field's Paste (a mouse cannot press Ctrl/⌘+V; the phone types its own text in).
PASTE_TEXT = N_("Paste")  # i18n: a button and its tooltip (verb)


@dataclass(frozen=True)
class Item:
    """A line of a column: a section, or a row (`row`). Only a selectable one takes the
    highlight (on the right, a row that is no info line; in a layer, the left column's rows
    that open a layer). A `literal` one (or one of a literal row) shows its text as given."""

    key: str
    text: str
    icon: str = ""
    row: Optional[Row] = None
    selectable: bool = True
    literal: bool = False


@dataclass
class Box:
    """Where a line is drawn (widget coordinates)."""

    rect: QRectF  # all of it, with its help when that shows
    line: QRectF  # its title's lines and its sub line (the highlight)
    title: QRectF  # the room its title wraps in
    titles: list[str] = field(default_factory=list)  # the title as shown, a line each
    sub: str = ""  # the sub line as shown
    icon: QRectF = field(default_factory=QRectF)
    less: QRectF = field(default_factory=QRectF)  # a stepper's ‹, value and ›
    value: QRectF = field(default_factory=QRectF)
    more: QRectF = field(default_factory=QRectF)
    end: QRectF = field(default_factory=QRectF)  # a text or info row's value, a note, a field
    chevron: QRectF = field(default_factory=QRectF)  # a row that opens: ›
    # While its text is edited: its field (in `end`), and the field's Paste at the end of `end`.
    entry: QRectF = field(default_factory=QRectF)
    paste: QRectF = field(default_factory=QRectF)
    help: list[str] = field(default_factory=list)

    def moved(self, dy: float) -> Box:
        """The same line, `dy` lower."""
        names = (
            "rect",
            "line",
            "title",
            "icon",
            "less",
            "value",
            "more",
            "end",
            "chevron",
            "entry",
            "paste",
        )
        return replace(self, **{name: getattr(self, name).translated(0, dy) for name in names})


@dataclass
class Layout:
    units: Units
    columns: dict[str, QRectF]
    areas: dict[str, QRectF]  # where each column's lines scroll
    back: QRectF  # ‹ in the header
    crumbs: list[tuple[str, QRectF]]  # the breadcrumb's names (the last is where it is)
    boxes: dict[str, list[Optional[Box]]]  # by column, each line's (None: out of sight)


def wrap(text: str, font: QFont, width: float, most: int) -> list[str]:
    """`text` word-wrapped into `most` lines at most, the last elided; a word wider than the
    line elides too, so that no line runs past `width`."""
    text = " ".join(text.split())
    fm = QFontMetricsF(font)
    if fm.horizontalAdvance(text) <= width:
        return [text]  # (most texts: no layout of lines needed)
    return [
        fm.elidedText(line, Qt.TextElideMode.ElideRight, max(0.0, width))
        for line in wrap_lines(text, font, width, most)
    ]


def whole(lines: list[str], text: str) -> bool:
    """Whether `lines` show all of `text` (a line may break where there is no space: after a
    hyphen, or in Chinese)."""
    return "".join("".join(lines).split()) == "".join(text.split())


def title_of(item: Item) -> str:
    """A line's title as shown: translated (a literal one as given: a provider's name), and a
    destructive row's ends in "…" (it asks before it acts), whether or not its text (or a
    translation) has one."""
    row = item.row
    literal = item.literal or (row is not None and row.literal)
    title = item.text if literal else translate(item.text)
    if row is not None and row.kind == DANGER and not title.rstrip().endswith((ELLIPSIS, "...")):
        title = title.rstrip() + ELLIPSIS
    return title


def columns(un: Units, height: float) -> tuple[dict[str, QRectF], dict[str, QRectF]]:
    """Each column (the drawer's widths: drawer_look.column_widths), and the area its lines
    scroll in."""
    left_w, right_w = column_widths(un)
    cols = {
        LEFT: QRectF(0, 0, left_w, height),
        RIGHT: QRectF(left_w, 0, right_w, height),
    }
    side = float(un.px(PAD_SIDE_U))
    top = float(un.px(PAD_TOP_U + FILTER_H_U + HEAD_TOP_U))
    areas = {
        key: QRectF(col.x() + side, top, col.width() - 2 * side, max(0.0, height - side - top))
        for key, col in cols.items()
    }
    return cols, areas


def last_first(heights: list[float], room: float) -> int:
    """The first line in sight when the column is scrolled to its end."""
    first, total = len(heights), 0.0
    while first > 0 and total + heights[first - 1] <= room + 0.5:
        first -= 1
        total += heights[first]
    return min(first, max(0, len(heights) - 1))


def help_lines(help_text: str, note: str, un: Units, width: float) -> list[str]:
    """The help under a focused row, two lines at most (the last elided), its restart `note`
    after it; when both do not fit, the note keeps the last line to itself."""
    font = un.font(HELP_FONT_U)
    text = " ".join(part for part in (help_text, note) if part)
    if not text:
        return []  # a row without help keeps its height when focused
    lines = wrap(text, font, width, HELP_LINES)
    if help_text and note and not whole(lines, text):
        lines = wrap(help_text, font, width, HELP_LINES - 1) + wrap(note, font, width, 1)
    return lines


def help_height(lines: list[str], un: Units) -> float:
    if not lines:
        return 0.0
    spacing = QFontMetricsF(un.font(HELP_FONT_U)).lineSpacing()
    return float(math.ceil(len(lines) * spacing + HELP_PAD_U * un.u))


def steps_width(
    un: Units, room: float, title: str, value: str, choices: Sequence[str]
) -> tuple[float, float, float]:
    """A stepper's ‹ value › at the end of `room`: (its width, ‹'s and ›'s, the gaps).
    The value is as wide as its widest choice, so that it keeps its place while stepping,
    when that fits beside the title on one line; else as wide as the value shown, when
    that does. Else the title wraps and the value keeps its widest width within
    VALUE_SHARE of the row, or what the title leaves (at least that share), eliding."""
    fm = QFontMetricsF(un.font(VALUE_FONT_U))
    step = max(float(un.px(STEP_U)), fm.height())  # the font stops at a floor, u does not
    gap = float(un.px(STEP_GAP_U))
    shown = fm.horizontalAdvance(value)
    widest = max([shown] + [fm.horizontalAdvance(choice) for choice in choices])
    title_w = math.ceil(QFontMetricsF(un.font(TITLE_FONT_U)).horizontalAdvance(title))
    least = 2 * step + 2 * gap
    left_over = room - TITLE_GAP_U * un.u - title_w
    share = room * VALUE_SHARE
    if least + widest <= left_over:
        return least + widest, step, gap
    if least + shown <= left_over:
        return least + shown, step, gap
    if least + widest <= share:
        return least + widest, step, gap
    return min(least + shown, max(least, left_over, share)), step, gap


def tail_width(un: Units, room: float, title: str, text: str, editing: bool) -> float:
    """A value or a note at the end of `room`: as wide as it is when that fits beside the
    whole title on one line, else what the title leaves, at least VALUE_SHARE of the row
    (eliding). The field a text is edited in takes all the title leaves, at least that share,
    so that it keeps its width while it is typed into."""
    title_w = math.ceil(QFontMetricsF(un.font(TITLE_FONT_U)).horizontalAdvance(title))
    left_over = room - TITLE_GAP_U * un.u - title_w
    share = room * VALUE_SHARE
    if editing:
        return max(0.0, left_over, share)
    if not text:
        return 0.0
    width = float(math.ceil(QFontMetricsF(un.font(VALUE_FONT_U)).horizontalAdvance(text)))
    return max(0.0, min(width, max(left_over, share)))


def place(
    un: Units,
    area: QRectF,
    item: Item,
    *,
    ends: bool,
    icons: bool,
    value: str = "",
    choices: Sequence[str] = (),
    help: Sequence[str] = (),
    editing: bool = False,
) -> Box:
    """Line `item` of a column at y 0. `ends`: its row's right end shows (the right column;
    the left lists titles only); `icons`: the column keeps room for the sections' icons.
    `value`: what a stepper, a text or an info row shows (`choices`: a stepper's, shown);
    `help`: the lines under a focused row; `editing`: its text is edited, in a field."""
    inset = INSET_U * un.u
    x, right = area.x() + inset, area.right() - inset
    icon_size = ICON_U * un.u
    icon_x = x
    if icons:
        x += icon_size + ICON_GAP_U * un.u
    row = item.row
    kind = row.kind if ends and row is not None else ""
    title = title_of(item)
    title_gap = TITLE_GAP_U * un.u
    chevron = float(un.px(STEP_ICON_U))
    end = right  # where the title's room ends
    steps_w, step, gap = 0.0, 0.0, 0.0
    tail_right, tail_w = right, 0.0  # a value or a note: its right edge, its width
    if row is not None and kind == STEP:
        steps_w, step, gap = steps_width(un, right - x, title, value, choices)
        end = right - steps_w - title_gap
    elif row is not None and kind:
        if kind == OPENS:
            tail_right = right - chevron - title_gap
        tail = value if kind in (TEXT, INFO) else row.note
        tail_w = tail_width(un, tail_right - x, title, tail, editing and kind == TEXT)
        end = tail_right - tail_w - title_gap if tail_w else tail_right
    title_font = un.font(TITLE_FONT_U)
    title_w = max(0.0, end - x)
    titles = wrap(title, title_font, title_w, TITLE_LINES)
    spacing = QFontMetricsF(title_font).lineSpacing()
    sub = ""
    sub_h = 0.0
    if row is not None and row.sub:
        sub_font = un.font(SUB_FONT_U)
        sub = QFontMetricsF(sub_font).elidedText(
            " ".join(row.sub.split()), Qt.TextElideMode.ElideRight, title_w
        )
        sub_h = QFontMetricsF(sub_font).lineSpacing()
    line_h = float(un.px(ROW_H_U)) + max(0, len(titles) - 1) * spacing + sub_h
    middle = line_h / 2
    box = Box(QRectF(), QRectF(area.x(), 0, area.width(), line_h), QRectF(), titles, sub)
    box.title = QRectF(x, 0, title_w, line_h)
    if icons:
        box.icon = QRectF(icon_x, middle - icon_size / 2, icon_size, icon_size)
    if kind == STEP:
        box.more = QRectF(right - step, 0, step, line_h)
        box.less = QRectF(right - steps_w, 0, step, line_h)
        value_w = max(0.0, box.more.left() - gap - box.less.right() - gap)
        box.value = QRectF(box.less.right() + gap, 0, value_w, line_h)
    if kind == OPENS:
        box.chevron = QRectF(right - chevron, middle - chevron / 2, chevron, chevron)
    if tail_w:
        box.end = QRectF(tail_right - tail_w, 0, tail_w, line_h)
        if editing and kind == TEXT:
            box.entry, box.paste = field_and_paste(un, box.end)
    box.rect = QRectF(area.x(), 0, area.width(), line_h + help_height(list(help), un))
    box.help = list(help)
    return box


def field_and_paste(un: Units, end: QRectF) -> tuple[QRectF, QRectF]:
    """The field a text is edited in, in `end` (its line's right end), and its Paste at its
    right end, as tall as the field: the label's width, at most a third of `end`."""
    fm = QFontMetricsF(un.font(PASTE_FONT_U))
    label_w = math.ceil(fm.horizontalAdvance(translate(PASTE_TEXT))) + 2 * PASTE_PAD_U * un.u
    paste_w = min(label_w, end.width() / 3)
    height = min(end.height(), float(un.px(FIELD_H_U)))
    top = end.center().y() - height / 2
    paste = QRectF(end.right() - paste_w, top, paste_w, height)
    field_w = max(0.0, paste.left() - PASTE_GAP_U * un.u - end.left())
    return QRectF(end.left(), end.top(), field_w, end.height()), paste


def header(
    un: Units, cols: dict[str, QRectF], names: list[str]
) -> tuple[QRectF, list[tuple[str, QRectF]]]:
    """‹, then `names` ("Settings › Playback", › a layer's name): the names between the first
    and the last fold into "…" when they do not fit, and the last elides."""
    side = float(un.px(PAD_SIDE_U))
    row = QRectF(
        side,
        un.px(PAD_TOP_U) + (un.px(FILTER_H_U) - un.px(CRUMB_H_U)) / 2,
        cols[RIGHT].right() - 2 * side,
        un.px(CRUMB_H_U),
    )
    size = float(un.px(GEAR_U))
    back = QRectF(row.x(), row.center().y() - size / 2, size, size)
    x = back.right() + un.px(GEAR_GAP_U)
    fm = QFontMetricsF(un.font(CRUMB_FONT_U))
    sep = fm.horizontalAdvance(SEPARATOR)
    room = row.right() - x

    def width(texts: list[str]) -> float:
        return sum(fm.horizontalAdvance(t) for t in texts) + sep * (len(texts) - 1)

    names = list(names)
    if len(names) > 2 and width(names) > room:
        names = [names[0], ELLIPSIS, names[-1]]
    head_w = width(names[:-1]) + sep if len(names) > 1 else 0.0
    names[-1] = fm.elidedText(names[-1], Qt.TextElideMode.ElideRight, max(0.0, room - head_w))
    crumbs: list[tuple[str, QRectF]] = []
    for text in names:
        text_w = fm.horizontalAdvance(text)
        crumbs.append((text, QRectF(x, row.y(), text_w, row.height())))
        x += text_w + sep
    return back, crumbs


def back_hit(rect: QRectF, un: Units) -> QRectF:
    """Where ‹ takes clicks: a MIN_HIT_PX square at least (as the drawer's header)."""
    grow = max(0.0, min((MIN_HIT_PX - rect.width()) / 2, un.px(GEAR_GAP_U) / 2))
    return rect.adjusted(-grow, -grow, grow, grow)
