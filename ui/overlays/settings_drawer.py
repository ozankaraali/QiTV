"""Settings as a layer of the TV window's drawer (Settings in the drawer, plan 1).

It looks and moves like the channel list, with its columns, highlight and breadcrumb
(ui/overlays/drawer_look.py), and the picture plays on to the right of the fade. The left
column lists the sections and the right one the highlighted section's rows, shown at once as
the highlight moves. A row steps through its choices ("Live latency  ‹ Auto ›", the value lit
in the accent while it is not the default), opens a layer of rows of its own (it ends in ›),
or acts. In a layer the left column lists the rows the layer is one of, the opened one
marked as the drawer marks its path; there only the rows that open a layer take the
highlight, and a click on another goes back up to it. A section whose settings are still in
the Settings window has one row that opens it there (`legacy:<section key>`). Only the right
column's focused row shows its help under it, two lines at most.

It changes nothing itself: a step is `stepped(key, ±1)`, an action `chosen(key)`, and backing
out of the sections `closed`; the caller applies them and calls `refresh()`. The value a row
shows and whether it is lit are asked of the caller at each paint (`value_text`, `lit`).

The keys (the shell hands them over): ↑/↓ move. On a stepper of the right column ←/→ step and
Enter steps on; on another right-column row ← goes to the left column, and → or Enter opens its
layer or acts. On the left column → or Enter goes right, and ← is `back()`. `back()` goes one
layer up: a layer, the rows it is one of, the sections, closed.

The mouse: a click on a row focuses it, and on the right column does what Enter does (on ‹
it steps back); a quick second click steps again. The wheel scrolls the column under the
pointer. ‹ in the header and a click on the fade over the picture are `back()`. The mouse's
Back and Forward buttons go on to the shell, as over the drawer.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
import logging
import math
from typing import Optional

from PySide6.QtCore import QEvent, QObject, QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QFont, QFontMetricsF, QMouseEvent, QPainter, QWheelEvent
from PySide6.QtWidgets import QWidget

from services.i18n import N_, tr, translate
from services.settings_model import Row, Section
from ui.keymap import key_hint
from ui.overlays.base import Overlay
from ui.overlays.channel_rows import HOVER_ALPHA
from ui.overlays.drawer import FILTER_H_U, GEAR_GAP_U, GEAR_U, MIN_HIT_PX, SEPARATOR
from ui.overlays.drawer_look import (
    CRUMB_FONT_U,
    CRUMB_H_U,
    FADE_U,
    LEFT_U,
    PAD_SIDE_U,
    PAD_TOP_U,
    RIGHT_U,
    ROW_H_U,
    TITLE_FONT_U,
    draw_back,
    paint_backdrop,
    paint_highlight,
)
from ui.overlays.guide import wrap_lines
from ui.overlays.painting import (
    STEP_ICON_U,
    Units,
    draw_icon,
    fill_round,
    paint_value_steps,
    ui_units,
)
from ui.pointer import NAV_BUTTONS, WHEEL_NOTCH, SecondClickGuard
from ui.theme import PALETTE, qcolor

logger = logging.getLogger(__name__)

# i18n: the Settings layer's name, first in its breadcrumb ("Settings › Playback")
SETTINGS_TITLE = N_("Settings")
# i18n: under a setting whose change takes effect only once QiTV restarts
RESTART_NOTE = N_("Applies when QiTV starts again.")
# i18n: the one row of a Settings section whose settings are still in the Settings window
LEGACY_TEXT = N_("Open in the Settings window")
LEGACY_PREFIX = "legacy:"  # the legacy row's key: LEGACY_PREFIX + the section's key

LEFT, RIGHT = "left", "right"
_FALLBACK_HOST_WIDTH = 1280
HEAD_TOP_U = 1.0  # the lists start this far under the header row (the drawer's right column)
INSET_U = 1.2  # a row's text from its sides (the drawer's rows)
ICON_U = 1.8  # a section's icon
ICON_GAP_U = 0.7
VALUE_FONT_U = 1.2  # a stepper's value (its title is TITLE_FONT_U)
HELP_FONT_U = 1.05
HELP_LINES = 2
HELP_PAD_U = 0.8  # under the help
TITLE_LINES = 2  # a long title wraps, its line growing; beyond this it elides (small windows)
STEP_U = 1.8  # a stepper's ‹ and › (the panels')
STEP_GAP_U = 0.6
TITLE_GAP_U = 0.8  # between a row's title and its ‹ value › or ›
VALUE_SHARE = 0.5  # a ‹ value › too wide beside its whole title keeps this share of the row
_BACK = "back"
_FADE = "fade"
_FAILED_ICONS: set[str] = set()  # icon names draw_icon has no drawing for (logged once each)


@dataclass(frozen=True)
class _Item:
    """A line of a column: a section, or a row (`row`). Only a selectable one takes the
    highlight (in a layer, the left column's rows that open a layer)."""

    key: str
    text: str
    icon: str = ""
    row: Optional[Row] = None
    selectable: bool = True


@dataclass
class _Box:
    """Where a line is drawn (widget coordinates)."""

    rect: QRectF  # all of it, with its help when that shows
    line: QRectF  # its title's lines (the highlight)
    title: QRectF  # the room its title wraps in
    titles: list[str] = field(default_factory=list)  # the title as shown, a line each
    icon: QRectF = field(default_factory=QRectF)
    less: QRectF = field(default_factory=QRectF)  # a stepper's ‹, value and ›
    value: QRectF = field(default_factory=QRectF)
    more: QRectF = field(default_factory=QRectF)
    chevron: QRectF = field(default_factory=QRectF)  # a row that opens a layer: ›
    help: list[str] = field(default_factory=list)

    def moved(self, dy: float) -> _Box:
        """The same line, `dy` lower."""
        names = ("rect", "line", "title", "icon", "less", "value", "more", "chevron")
        return replace(self, **{name: getattr(self, name).translated(0, dy) for name in names})


@dataclass
class _Layout:
    units: Units
    columns: dict[str, QRectF]
    back: QRectF  # ‹ in the header
    crumbs: list[tuple[str, QRectF]]  # the breadcrumb's names (the last is where it is)
    boxes: dict[str, list[Optional[_Box]]]  # by column, each line's (None: out of sight)


def _kind(row: Row) -> str:
    """What a row does: "step" through its choices, "open" a layer, or "act"."""
    if row.choices:
        return "step"
    return "open" if row.section is not None else "act"


def _wrap(text: str, font: QFont, width: float, most: int) -> list[str]:
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


def _whole(lines: list[str], text: str) -> bool:
    """Whether `lines` show all of `text` (a line may break where there is no space: after a
    hyphen, or in Chinese)."""
    return "".join("".join(lines).split()) == "".join(text.split())


class SettingsDrawer(Overlay):
    accessible_name = SETTINGS_TITLE
    stepped = Signal(str, int)  # a stepper's key, and -1 (‹, ←) or +1 (›, →, Enter, a click)
    chosen = Signal(str)  # an action row's key (the legacy rows': "legacy:<section key>")
    closed = Signal()  # backed out of the sections (not when hidden otherwise)

    def __init__(
        self,
        value_text: Callable[[Row], str],
        lit: Callable[[Row], bool],
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setMouseTracking(True)
        self._value_text = value_text
        self._lit = lit
        self._sections: tuple[Section, ...] = ()
        self._section = 0  # the highlighted section
        self._opened: list[int] = []  # the rows opened, a layer each (indices, from the top)
        self._side = LEFT
        self._right = 0  # the right column's focused row
        self._first = {LEFT: 0, RIGHT: 0}  # each column's first row in sight
        self._hover: Optional[tuple[str, int]] = None  # (column or _BACK, line)
        self._wheel = 0  # a wheel's turn not yet a whole row
        # The last press: (column, row key, part), or (_BACK or _FADE, "", ""): a double-click's
        # second press repeats it (_click_again).
        self._pressed: Optional[tuple[str, str, str]] = None
        self._second_click = SecondClickGuard(self)
        parent.installEventFilter(self)
        self.hide()

    # ---- public API -------------------------------------------------------------------------

    def open_at(self, sections: Sequence[Section], section_key: str = "") -> None:
        """Show the sections, the left column's highlight on `section_key` (else the first)."""
        self._sections = tuple(sections)
        keys = [s.key for s in self._sections]
        self._section = keys.index(section_key) if section_key in keys else 0
        self._opened = []
        self._side = LEFT
        self._right = 0
        self._first = {LEFT: 0, RIGHT: 0}
        self._hover = None
        self._wheel = 0
        self._pressed = None
        self._second_click.disarm()
        self._relayout()
        self._reveal(LEFT)
        self.show()
        self.raise_()

    def path(self) -> tuple[str, ...]:
        """The highlighted section's key, then the key of each row opened as a layer."""
        if not self._sections:
            return ()
        keys = [self._sections[self._section].key]
        rows = self._sections[self._section].rows
        for index in self._opened:
            row = rows[index]
            keys.append(row.key)
            rows = row.section.rows if row.section is not None else ()
        return tuple(keys)

    def focus_side(self) -> str:
        """The column the keys are in: "left" or "right"."""
        return self._side

    def focused_row(self) -> Optional[Row]:
        """The right column's focused row (also while the keys are on the left)."""
        rows = self._right_rows()
        return rows[self._right] if 0 <= self._right < len(rows) else None

    def refresh(self) -> None:
        """A value changed: paint the rows again (their values are asked anew)."""
        self.update()

    def move_by(self, delta: int) -> None:
        if not delta:
            return
        if self._side == LEFT:
            items = self._items(LEFT)
            if not items:
                return
            at = self._left_index()
            target = self._selectable(items, max(0, min(len(items) - 1, at + delta)), delta)
            if target is not None and target != at:
                self._set_left(target)
        else:
            rows = self._right_rows()
            if not rows:
                return
            self._right = max(0, min(len(rows) - 1, self._right + delta))
            self._reveal(RIGHT)
        self.update()

    def page(self, delta: int) -> None:
        """A page of rows of the focused column (those in sight, but one)."""
        shown = sum(1 for box in self._layout().boxes[self._side] if box is not None)
        self.move_by(delta * max(1, shown - 1))

    def left(self) -> None:
        if self._side == LEFT:
            self.back()  # a layer up, as ← on the drawer's left column
            return
        row = self.focused_row()
        if row is not None and _kind(row) == "step":
            self.stepped.emit(row.key, -1)
            return
        self._side = LEFT
        self._reveal(LEFT)
        self.update()

    def right(self) -> None:
        if self._side == LEFT:
            if self._right_rows():
                self._side = RIGHT
                self._reveal(RIGHT)
                self.update()
            return
        row = self.focused_row()
        if row is not None:
            self._act(row)

    def activate(self) -> None:
        """Enter: as →."""
        self.right()

    def back(self) -> None:
        """One layer up: a layer gives way to the rows it is one of (the keys staying in their
        column), the rows to the sections, and the sections close."""
        if self._opened:
            self._right = self._opened.pop()
            self._first = {LEFT: 0, RIGHT: 0}
            self._reveal(LEFT)
            self._reveal(RIGHT)
        elif self._side == RIGHT:
            self._side = LEFT
            self._reveal(LEFT)
        else:
            self.hide()
            self.closed.emit()
            return
        self.update()

    # ---- geometry (widget coordinates) ------------------------------------------------------

    def row_rect(self, side: str, index: int) -> QRectF:
        """Line `index` of a column, its help included while it shows; empty while out of
        sight."""
        box = self._box(side, index)
        return QRectF(box.rect) if box is not None else QRectF()

    def step_rect(self, index: int, delta: int) -> QRectF:
        """The right column's row `index`: its ‹ (`delta` -1) or › (+1); empty when none."""
        box = self._box(RIGHT, index)
        if box is None:
            return QRectF()
        return QRectF(box.less if delta < 0 else box.more)

    def title_rect(self, index: int) -> QRectF:
        """The room the right column's row `index` has for its title."""
        box = self._box(RIGHT, index)
        return QRectF(box.title) if box is not None else QRectF()

    def column_rect(self, side: str) -> QRectF:
        return QRectF(self._layout().columns[side])

    def back_rect(self) -> QRectF:
        """‹ at the start of the breadcrumb."""
        return QRectF(self._layout().back)

    # ---- state ------------------------------------------------------------------------------

    def _chain(self) -> list[Section]:
        """The highlighted section, then each opened layer."""
        if not self._sections:
            return []
        chain = [self._sections[self._section]]
        for index in self._opened:
            section = chain[-1].rows[index].section
            if section is None:
                break
            chain.append(section)
        return chain

    def _right_rows(self) -> tuple[Row, ...]:
        chain = self._chain()
        if not chain:
            return ()
        section = chain[-1]
        if section.rows or not section.legacy_tab:
            return section.rows
        return (Row(LEGACY_PREFIX + section.key, LEGACY_TEXT, action=True),)

    def _items(self, side: str) -> list[_Item]:
        if side == RIGHT:
            return [_Item(row.key, row.text, row=row) for row in self._right_rows()]
        chain = self._chain()
        if len(chain) < 2:
            return [_Item(s.key, s.text, s.icon) for s in self._sections]
        return [
            _Item(row.key, row.text, row=row, selectable=_kind(row) == "open")
            for row in chain[-2].rows
        ]

    def _left_index(self) -> int:
        return self._opened[-1] if self._opened else self._section

    def _set_left(self, index: int) -> None:
        """The left highlight on `index`: its rows show on the right at once."""
        if index != self._left_index():
            if self._opened:
                self._opened[-1] = index
            else:
                self._section = index
            self._right = 0
            self._first[RIGHT] = 0
        self._reveal(LEFT)

    @staticmethod
    def _selectable(items: list[_Item], at: int, delta: int) -> Optional[int]:
        """`at`, or the nearest line that takes the highlight: onward in the direction of
        `delta`, else back; None if there is none."""
        step = 1 if delta > 0 else -1
        for direction in (step, -step):
            index = at
            while 0 <= index < len(items):
                if items[index].selectable:
                    return index
                index += direction
        return None

    def _act(self, row: Row) -> None:
        """→, Enter or a click on a right-column row: step it on, open its layer, or act."""
        kind = _kind(row)
        if kind == "step":
            self.stepped.emit(row.key, 1)
        elif kind == "open":
            self._opened.append(self._right)
            self._right = 0
            self._first = {LEFT: 0, RIGHT: 0}
            self._reveal(LEFT)
            self.update()
        else:
            self.chosen.emit(row.key)

    # ---- layout -----------------------------------------------------------------------------

    def _units(self) -> Units:
        parent = self.parentWidget()
        return ui_units(parent.width() if parent is not None else _FALLBACK_HOST_WIDTH)

    def _relayout(self) -> None:
        """The drawer's place: both columns and the fade, the host's height."""
        parent = self.parentWidget()
        if parent is None:
            return
        un = self._units()
        width = round(RIGHT_U * un.u) + un.px(FADE_U)
        rect = QRect(0, 0, width, parent.height())
        if rect != self.geometry():
            self.setGeometry(rect)
        self.update()

    def _columns(self, un: Units) -> tuple[dict[str, QRectF], dict[str, QRectF]]:
        """Each column, and the area its lines scroll in."""
        left_w = round(LEFT_U * un.u)
        right_w = round(RIGHT_U * un.u) - left_w
        height = float(self.height())
        columns = {
            LEFT: QRectF(0, 0, left_w, height),
            RIGHT: QRectF(left_w, 0, right_w, height),
        }
        side = float(un.px(PAD_SIDE_U))
        top = float(un.px(PAD_TOP_U + FILTER_H_U + HEAD_TOP_U))
        areas = {
            key: QRectF(col.x() + side, top, col.width() - 2 * side, max(0.0, height - side - top))
            for key, col in columns.items()
        }
        return columns, areas

    def _help_lines(self, row: Row, un: Units, width: float) -> list[str]:
        """The help under a focused row, two lines at most (the last elided). The restart note
        follows it; when both do not fit, the note keeps the last line to itself."""
        font = un.font(HELP_FONT_U)
        help_text = translate(row.help) if row.help else ""
        note = translate(RESTART_NOTE) if row.restart else ""
        text = " ".join(part for part in (help_text, note) if part)
        lines = _wrap(text, font, width, HELP_LINES)
        if help_text and note and not _whole(lines, text):
            lines = _wrap(help_text, font, width, HELP_LINES - 1) + _wrap(note, font, width, 1)
        return lines

    def _help_height(self, lines: list[str], un: Units) -> float:
        if not lines:
            return 0.0
        spacing = QFontMetricsF(un.font(HELP_FONT_U)).lineSpacing()
        return float(math.ceil(len(lines) * spacing + HELP_PAD_U * un.u))

    def _boxes(self, side: str, un: Units, area: QRectF) -> list[_Box]:
        """Each line of a column, as tall as its title's lines and its help, at y 0."""
        items = self._items(side)
        icons = side == LEFT and any(item.icon for item in items)
        focus = self._right if side == RIGHT and self._side == RIGHT else -1
        return [
            self._place(side, un, area, item, icons, index == focus)
            for index, item in enumerate(items)
        ]

    def _heights(self, side: str, un: Units, area: QRectF) -> list[float]:
        return [box.rect.height() for box in self._boxes(side, un, area)]

    @staticmethod
    def _last_first(heights: list[float], room: float) -> int:
        """The first line in sight when the column is scrolled to its end."""
        first, total = len(heights), 0.0
        while first > 0 and total + heights[first - 1] <= room + 0.5:
            first -= 1
            total += heights[first]
        return min(first, max(0, len(heights) - 1))

    def _reveal(self, side: str) -> None:
        """Scroll a column so that its highlighted line shows whole."""
        un = self._units()
        area = self._columns(un)[1][side]
        heights = self._heights(side, un, area)
        if not heights:
            self._first[side] = 0
            return
        index = self._right if side == RIGHT else self._left_index()
        index = max(0, min(len(heights) - 1, index))
        first = min(self._first[side], index)
        while first < index and sum(heights[first : index + 1]) > area.height() + 0.5:
            first += 1
        self._first[side] = min(first, self._last_first(heights, area.height()))

    def _scroll_by(self, side: str, rows: int) -> None:
        un = self._units()
        area = self._columns(un)[1][side]
        heights = self._heights(side, un, area)
        first = max(0, min(self._first[side] + rows, self._last_first(heights, area.height())))
        if first != self._first[side]:
            self._first[side] = first
            self._hover = None
            self.update()

    def _layout(self) -> _Layout:
        un = self._units()
        columns, areas = self._columns(un)
        back, crumbs = self._header(un, columns)
        shown: dict[str, list[Optional[_Box]]] = {}
        for side in (LEFT, RIGHT):
            area = areas[side]
            boxes = self._boxes(side, un, area)
            heights = [box.rect.height() for box in boxes]
            out: list[Optional[_Box]] = [None] * len(boxes)
            first = min(self._first[side], self._last_first(heights, area.height()))
            y = area.top()
            for index in range(first, len(boxes)):
                if index > first and y + heights[index] > area.bottom() + 0.5:
                    break
                out[index] = boxes[index].moved(y)
                y += heights[index]
            shown[side] = out
        return _Layout(un, columns, back, crumbs, shown)

    def _place(
        self, side: str, un: Units, area: QRectF, item: _Item, icons: bool, focused: bool
    ) -> _Box:
        """Line `item` of a column at y 0: its title wraps onto a second line when it does not
        fit on one (the line grows), and a focused right-column row has its help under it.
        `icons`: the column keeps room for the sections' icons."""
        inset = INSET_U * un.u
        x, right = area.x() + inset, area.right() - inset
        icon_size = ICON_U * un.u
        icon_x = x
        if icons:
            x += icon_size + ICON_GAP_U * un.u
        row = item.row
        kind = _kind(row) if side == RIGHT and row is not None else ""
        title_gap = TITLE_GAP_U * un.u
        end, step, gap = 0.0, 0.0, 0.0
        chevron = float(un.px(STEP_ICON_U))
        if row is not None and kind == "step":
            end, step, gap = self._steps_width(row, un, right - x)
            title_right = right - end - title_gap
        elif kind == "open":
            title_right = right - chevron - title_gap
        else:
            title_right = right
        title_font = un.font(TITLE_FONT_U)
        title_w = max(0.0, title_right - x)
        titles = _wrap(translate(item.text), title_font, title_w, TITLE_LINES)
        spacing = QFontMetricsF(title_font).lineSpacing()
        line_h = float(un.px(ROW_H_U)) + max(0, len(titles) - 1) * spacing
        middle = line_h / 2
        box = _Box(QRectF(), QRectF(area.x(), 0, area.width(), line_h), QRectF(), titles)
        box.title = QRectF(x, 0, title_w, line_h)
        if icons:
            box.icon = QRectF(icon_x, middle - icon_size / 2, icon_size, icon_size)
        if kind == "step":
            box.more = QRectF(right - step, 0, step, line_h)
            box.less = QRectF(right - end, 0, step, line_h)
            value_w = max(0.0, box.more.left() - gap - box.less.right() - gap)
            box.value = QRectF(box.less.right() + gap, 0, value_w, line_h)
        elif kind == "open":
            box.chevron = QRectF(right - chevron, middle - chevron / 2, chevron, chevron)
        height = line_h
        if focused and row is not None:
            box.help = self._help_lines(row, un, area.width() - 2 * inset)
            height += self._help_height(box.help, un)
        box.rect = QRectF(area.x(), 0, area.width(), height)
        return box

    def _steps_width(self, row: Row, un: Units, room: float) -> tuple[float, float, float]:
        """A stepper's ‹ value › at the end of `room`: (its width, ‹'s and ›'s, the gaps).
        The value is as wide as its widest choice, so that it keeps its place while stepping,
        when that fits beside the title on one line; else as wide as the value shown, when
        that does. Else the title wraps and the value keeps its widest width within
        VALUE_SHARE of the row, or what the title leaves (at least that share), eliding."""
        fm = QFontMetricsF(un.font(VALUE_FONT_U))
        step = max(float(un.px(STEP_U)), fm.height())  # the font stops at a floor, u does not
        gap = float(un.px(STEP_GAP_U))
        value = fm.horizontalAdvance(self._value_text(row))
        widest = max([value] + [fm.horizontalAdvance(translate(c.text)) for c in row.choices])
        title_w = math.ceil(
            QFontMetricsF(un.font(TITLE_FONT_U)).horizontalAdvance(translate(row.text))
        )
        least = 2 * step + 2 * gap
        left_over = room - TITLE_GAP_U * un.u - title_w
        share = room * VALUE_SHARE
        if least + widest <= left_over:
            return least + widest, step, gap
        if least + value <= left_over:
            return least + value, step, gap
        if least + widest <= share:
            return least + widest, step, gap
        return min(least + value, max(least, left_over, share)), step, gap

    def _header(
        self, un: Units, columns: dict[str, QRectF]
    ) -> tuple[QRectF, list[tuple[str, QRectF]]]:
        """‹, then "Settings › Playback" (› a layer's name): the names between the first and
        the last fold into "…" when they do not fit, and the last elides."""
        side = float(un.px(PAD_SIDE_U))
        row = QRectF(
            side,
            un.px(PAD_TOP_U) + (un.px(FILTER_H_U) - un.px(CRUMB_H_U)) / 2,
            columns[RIGHT].right() - 2 * side,
            un.px(CRUMB_H_U),
        )
        size = float(un.px(GEAR_U))
        back = QRectF(row.x(), row.center().y() - size / 2, size, size)
        x = back.right() + un.px(GEAR_GAP_U)
        fm = QFontMetricsF(un.font(CRUMB_FONT_U))
        names = [translate(SETTINGS_TITLE)] + [translate(s.text) for s in self._chain()]
        sep = fm.horizontalAdvance(SEPARATOR)
        room = row.right() - x

        def width(texts: list[str]) -> float:
            return sum(fm.horizontalAdvance(t) for t in texts) + sep * (len(texts) - 1)

        if len(names) > 2 and width(names) > room:
            names = [names[0], "…", names[-1]]
        head_w = width(names[:-1]) + sep if len(names) > 1 else 0.0
        names[-1] = fm.elidedText(names[-1], Qt.TextElideMode.ElideRight, max(0.0, room - head_w))
        crumbs: list[tuple[str, QRectF]] = []
        for text in names:
            text_w = fm.horizontalAdvance(text)
            crumbs.append((text, QRectF(x, row.y(), text_w, row.height())))
            x += text_w + sep
        return back, crumbs

    def _back_hit(self, rect: QRectF, un: Units) -> QRectF:
        """Where ‹ takes clicks: a MIN_HIT_PX square at least (as the drawer's header)."""
        grow = max(0.0, min((MIN_HIT_PX - rect.width()) / 2, un.px(GEAR_GAP_U) / 2))
        return rect.adjusted(-grow, -grow, grow, grow)

    def _box(self, side: str, index: int) -> Optional[_Box]:
        boxes = self._layout().boxes[side]
        return boxes[index] if 0 <= index < len(boxes) else None

    # ---- painting ---------------------------------------------------------------------------

    def paintEvent(self, event) -> None:
        lay = self._layout()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        paint_backdrop(p, self.rect(), lay.units)
        self._paint_header(p, lay)
        for side in (LEFT, RIGHT):
            p.save()
            p.setClipRect(lay.columns[side])
            self._paint_column(p, lay, side)
            p.restore()
        p.end()

    def _paint_header(self, p: QPainter, lay: _Layout) -> None:
        hovered = self._hover == (_BACK, 0)
        draw_back(p, lay.back, qcolor(PALETTE.accent) if hovered else qcolor(PALETTE.text, 0.55))
        align = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        p.setFont(lay.units.font(CRUMB_FONT_U))
        for index, (text, rect) in enumerate(lay.crumbs):
            last = index == len(lay.crumbs) - 1
            p.setPen(qcolor(PALETTE.accent) if last else qcolor(PALETTE.text, 0.6))
            p.drawText(rect, align, text)
            if not last:
                p.setPen(qcolor(PALETTE.text, 0.6))
                after = QRectF(rect.right(), rect.y(), self.width() - rect.right(), rect.height())
                p.drawText(after, align, SEPARATOR)

    def _paint_column(self, p: QPainter, lay: _Layout, side: str) -> None:
        un = lay.units
        items = self._items(side)
        marked = self._left_index() if side == LEFT else self._right
        focused = self._side == side
        text, muted = qcolor(PALETTE.text), qcolor(PALETTE.text, PALETTE.text_muted_alpha)
        align = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        title_font = un.font(TITLE_FONT_U)
        for index, box in enumerate(lay.boxes[side]):
            if box is None:
                continue
            item = items[index]
            if index == marked and (side == LEFT or focused):
                paint_highlight(p, box.line, un, focused)
            elif self._hover == (side, index) and item.selectable:
                inset = box.line.adjusted(0.3 * un.u, 0.2 * un.u, -0.3 * un.u, -0.2 * un.u)
                fill_round(p, inset, qcolor(PALETTE.text, HOVER_ALPHA), 1.0 * un.u)
            if item.icon and not box.icon.isEmpty():
                self._paint_icon(p, item.icon, box.icon)
            p.setFont(title_font)
            p.setPen(text if item.selectable else muted)
            spacing = QFontMetricsF(title_font).lineSpacing()
            y = box.line.center().y() - spacing * len(box.titles) / 2
            for line in box.titles:  # a long title on two lines, centred as one is
                p.drawText(QRectF(box.title.x(), y, box.title.width(), spacing), align, line)
                y += spacing
            row = item.row
            if side == LEFT or row is None:
                continue
            if _kind(row) == "step":
                p.setFont(un.font(VALUE_FONT_U))
                value, lit = self._value_text(row), self._lit(row)
                accent = qcolor(PALETTE.accent)
                paint_value_steps(p, un, box.less, box.value, box.more, value, lit, accent)
            elif not box.chevron.isEmpty():
                draw_icon(p, "chevron_right", box.chevron, muted)
            if box.help:
                font = un.font(HELP_FONT_U)
                spacing = QFontMetricsF(font).lineSpacing()
                p.setFont(font)
                p.setPen(muted)
                y = box.line.bottom()
                for line in box.help:
                    width = box.line.right() - INSET_U * un.u - box.title.x()
                    p.drawText(QRectF(box.title.x(), y, width, spacing), align, line)
                    y += spacing

    @staticmethod
    def _paint_icon(p: QPainter, name: str, rect: QRectF) -> None:
        """A section's icon, when painting.draw_icon has a drawing of that name."""
        if name in _FAILED_ICONS:
            return
        try:
            draw_icon(p, name, rect, qcolor(PALETTE.accent))
        except KeyError:
            _FAILED_ICONS.add(name)
            logger.warning("No drawing for the section icon %r", name)

    # ---- mouse: consumed here, so it never reaches the video behind -------------------------

    def _hit(self, pos: QPointF) -> Optional[tuple[str, int, str]]:
        """What is at `pos`: (_BACK, 0, ""), (_FADE, 0, ""), or (column, line, part), the part
        of a right-column row being "less", "more", "line" or "help"; None elsewhere."""
        lay = self._layout()
        if self._back_hit(lay.back, lay.units).contains(pos):
            return (_BACK, 0, "")
        for side in (LEFT, RIGHT):
            for index, box in enumerate(lay.boxes[side]):
                if box is None or not box.rect.contains(pos):
                    continue
                if box.less.contains(pos):
                    return (side, index, "less")
                if box.more.contains(pos):
                    return (side, index, "more")
                return (side, index, "line" if box.line.contains(pos) else "help")
        if pos.x() >= lay.columns[RIGHT].right():
            return (_FADE, 0, "")
        return None

    def _click(self, pos: QPointF) -> None:
        """A click at `pos`; what it did is kept for a double-click's second press."""
        hit = self._hit(pos)
        self._pressed = None
        if hit is None:
            return
        target, index, part = hit
        if target in (_BACK, _FADE):
            self._pressed = (target, "", "")
            self._back_by_mouse()
            return
        if target == LEFT:
            self._click_left(index)
            return
        row = self._right_rows()[index]
        self._pressed = (RIGHT, row.key, part)
        self._side = RIGHT
        self._right = index
        self._reveal(RIGHT)
        if part == "less":
            self.stepped.emit(row.key, -1)
        elif part != "help":
            self._act(row)
        self.update()

    def _click_again(self) -> None:
        """A double-click's second press: ‹ or the fade go up again, and a stepper the first
        press stepped steps the same way again. Its row is found by its key, not under the
        pointer: the first press moved the help to it, so the rows below it (and, in a small
        window, its ‹ beside a value of another width) moved. Anything else is left alone: a
        layer the first press opened, an action, a section."""
        pressed, self._pressed = self._pressed, None
        if pressed is None:
            return
        target, key, part = pressed
        if target in (_BACK, _FADE):
            self._back_by_mouse()
            return
        rows = self._right_rows()
        index = next((i for i, row in enumerate(rows) if row.key == key), None)
        if index is None or _kind(rows[index]) != "step" or part == "help":
            return
        self._side = RIGHT
        self._right = index
        self.stepped.emit(key, -1 if part == "less" else 1)
        self.update()

    def _back_by_mouse(self) -> None:
        self.back()
        if not self.isVisible():
            self._second_click.arm()  # a double-click's second press reaches nothing behind

    def _click_left(self, index: int) -> None:
        item = self._items(LEFT)[index]
        if self._opened and not item.selectable:
            # A row of the layer above that only steps or acts: back up to it, focused.
            self._opened.pop()
            self._side = RIGHT
            self._right = index
            self._first = {LEFT: 0, RIGHT: 0}
            self._reveal(LEFT)
            self._reveal(RIGHT)
        elif item.selectable:
            self._side = LEFT
            self._set_left(index)
        self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() in NAV_BUTTONS:
            event.ignore()  # Back/Forward go on to the host: the shell's layer up/down
            return
        event.accept()
        if event.button() == Qt.MouseButton.LeftButton:
            self._click(event.position())
            self._hover_at(event.position())

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() in NAV_BUTTONS:
            event.ignore()
            return
        event.accept()  # never the video's fullscreen toggle
        if event.button() == Qt.MouseButton.LeftButton:
            self._click_again()
            self._hover_at(event.position())

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        self._hover_at(event.position())
        event.ignore()  # the host still hears the pointer move (the transport, the cursor)

    def _hover_at(self, pos: QPointF) -> None:
        hit = self._hit(pos) if self.isVisible() else None
        over = (hit[0], hit[1]) if hit is not None and hit[0] != _FADE else None
        if over != self._hover:
            self._hover = over
            back = over is not None and over[0] == _BACK
            self.setCursor(
                Qt.CursorShape.PointingHandCursor if back else Qt.CursorShape.ArrowCursor
            )
            self.update()

    def leaveEvent(self, event: QEvent) -> None:
        if self._hover is not None:
            self._hover = None
            self.unsetCursor()
            self.update()
        super().leaveEvent(event)

    def wheelEvent(self, event: QWheelEvent) -> None:
        """The column under the pointer scrolls; never a zap behind, even at a list's end."""
        event.accept()
        columns = self._layout().columns
        x = event.position().x()
        if x >= columns[RIGHT].right():
            return
        self._wheel += event.angleDelta().y()
        rows = int(self._wheel / WHEEL_NOTCH)  # towards zero: a notch up shows the rows above
        if rows:
            self._wheel -= rows * WHEEL_NOTCH
            self._scroll_by(LEFT if x < columns[LEFT].right() else RIGHT, -rows)

    def tooltip_at(self, pos: QPointF) -> str:
        """‹ names its key; a line cut short ("…") says its title (and value) whole."""
        hit = self._hit(pos)
        if hit is None or hit[0] == _FADE:
            return ""
        if hit[0] == _BACK:
            return key_hint(tr("Back"), "Esc")  # i18n: tooltip of ‹: a layer up (verb)
        side, index, _part = hit
        box, item = self._box(side, index), self._items(side)[index]
        if box is None:
            return ""
        un = self._units()
        whole = []
        title = translate(item.text)
        if not _whole(box.titles, title):
            whole.append(title)
        if side == RIGHT and item.row is not None and _kind(item.row) == "step":
            value = self._value_text(item.row)
            fm = QFontMetricsF(un.font(VALUE_FONT_U))
            if fm.horizontalAdvance(value) > box.value.width():
                whole.append(value)
        return "\n".join(whole)

    def hideEvent(self, event) -> None:
        self._hover = None
        super().hideEvent(event)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is self.parentWidget() and event.type() == QEvent.Type.Resize:
            self._relayout()
            if self.isVisible():  # the highlighted lines stay in sight
                self._reveal(LEFT)
                self._reveal(RIGHT)
        return False
