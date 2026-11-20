"""Settings as a layer of the TV window's drawer (Settings in the drawer, plans 1 and 2).

It looks and moves like the channel list, with its columns, highlight and breadcrumb
(ui/overlays/drawer_look.py), and the picture plays on to the right of the fade. The left
column lists the sections and the right one the highlighted section's rows, shown at once as
the highlight moves. A row is of a kind (services/settings_model.py's KINDS): it steps through
its choices ("Live latency  ‹ Auto ›", the value lit in the accent while it is not the
default), opens a layer of rows of its own or a card (it ends in ›), acts, acts on what cannot
be undone (in red, its title ending in "…": the caller asks first), holds a text edited in
place ("Name  Demo TV"), or only says something (muted, and the focus goes past it). A row may
have a muted sub line under its title, and a note at its right end. In a layer the left
column lists the rows the layer is one of, the opened one marked as the drawer marks its
path; there only the rows that open a layer take the highlight, and a click on another goes
back up to it. A layer without rows says its `empty_text` and the keys stay on the left. A
section whose settings are still in the Settings window has one row that opens it there
(`legacy:<section key>`). Only the right column's focused row shows its help under it, two
lines at most. Where everything goes is ui/overlays/settings_layout.py's.

It changes nothing itself: a step is `stepped(key, ±1)`, an action (a card's row too)
`chosen(key)`, a text kept `edited(key, text)`, and backing out of the sections `closed`; the
caller applies them and calls `refresh()`. The value a row shows and whether it is lit are
asked of the caller at each paint (`value_text`, `lit`).

The keys (the shell hands them over): ↑/↓ move. On a stepper of the right column ←/→ step and
Enter steps on; on another right-column row ← goes to the left column, and → or Enter opens its
layer or card, acts, or starts editing its text. On the left column → or Enter goes right, and
← is `back()`. `back()` goes one layer up: a layer, the rows it is one of, the sections, closed.
While a text is edited the field has the keys (`edit_key`): typing, Backspace, the caret keys,
⌘A, copy, cut and paste; Enter keeps the text, Esc (and Back) puts it back, the row staying
focused; ↑/↓ keep it and move on. The mouse's Forward (`forward()`) only goes a layer down.
Assistive technology hears the focused line and its value as they change (`announce`).

The mouse: a click on a row focuses it. On the right column a row whose click changes it in
place (a stepper's value, a text) only takes the focus while it has not got it, so that its
help shows before anything changes; a click on it focused steps it on or starts editing. A
row that opens or acts does so on the first click, as the drawer's rows do; an info line
takes no click. ‹ or › step their row at once, focused or not. While a text is edited, a
click in its field puts the caret (a drag selects, a double-click takes a word) and a click
elsewhere keeps the text, then does what it does. A double-click's second press is a click on
the row the first one was on (an action is taken once). Hovering a row shows its help, the
restart note too, as its tooltip. The wheel scrolls the column under the pointer. ‹ in the
header and a click on the fade over the picture are `back()`. The mouse's Back and Forward
buttons go on to the shell, as over the drawer.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
import logging
from typing import Optional

from PySide6.QtCore import QEvent, QObject, QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFontMetricsF, QMouseEvent, QPainter, QWheelEvent
from PySide6.QtWidgets import QWidget

from services.i18n import N_, tr, translate
from services.settings_model import DANGER, INFO, OPENS, STEP, TEXT, Row, Section
from ui.keymap import Action, key_hint
from ui.overlays.base import Overlay, announce
from ui.overlays.channel_rows import HOVER_ALPHA
from ui.overlays.drawer_look import (
    CRUMB_FONT_U,
    FADE_U,
    SEPARATOR,
    TITLE_FONT_U,
    column_widths,
    draw_back,
    paint_backdrop,
    paint_highlight,
)
from ui.overlays.painting import Units, draw_icon, fill_round, paint_value_steps, ui_units
from ui.overlays.settings_layout import (  # its metrics are offered from here too
    EMPTY_H_U,
    FIELD_H_U,
    FIELD_PAD_U,
    HELP_FONT_U,
    INSET_U,
    LEFT,
    RIGHT,
    SUB_FONT_U,
    VALUE_FONT_U,
    Box,
    Item,
    Layout,
    back_hit,
    columns,
    header,
    help_lines,
    last_first,
    place,
    title_of,
    whole,
)
from ui.pointer import NAV_BUTTONS, WHEEL_NOTCH, SecondClickGuard
from ui.text_edit import SELECTION_ALPHA, Dragging, TextEdit, edit_key as field_key, paint_edit
from ui.theme import PALETTE, qcolor

logger = logging.getLogger(__name__)

# i18n: the Settings layer's name, first in its breadcrumb ("Settings › Playback")
SETTINGS_TITLE = N_("Settings")
# i18n: under a setting whose change takes effect only once QiTV restarts
RESTART_NOTE = N_("Applies when QiTV starts again.")
# i18n: the one row of a Settings section whose settings are still in the Settings window
LEGACY_TEXT = N_("Open in the Settings window")
LEGACY_PREFIX = "legacy:"  # the legacy row's key: LEGACY_PREFIX + the section's key

_FALLBACK_HOST_WIDTH = 1280
_BACK = "back"
_FADE = "fade"
_FIELD = "field"  # the part of the row being edited that is its field
_FAILED_ICONS: set[str] = set()  # icon names draw_icon has no drawing for (logged once each)
# While a text is edited: the keys that are its field's, and those that leave the row (the
# text kept, then the move).
_FIELD_KEYS = frozenset(
    {
        Action.NAV_TEXT,
        Action.NAV_BACKSPACE,
        Action.NAV_LEFT,
        Action.NAV_RIGHT,
        Action.NAV_HOME,
        Action.NAV_END,
        Action.NAV_DELETE,
        Action.FORGET,  # Delete, as the drawer's key context names it
        Action.SELECT_ALL,
        Action.COPY,
        Action.CUT,
        Action.PASTE,
    }
)
_LEAVING = frozenset({Action.NAV_UP, Action.NAV_DOWN, Action.NAV_PAGE_UP, Action.NAV_PAGE_DOWN})


def _opens_layer(row: Row) -> bool:
    """Whether the row opens a layer of rows (not a card)."""
    return row.kind == OPENS and row.section is not None


class SettingsDrawer(Overlay):
    accessible_name = SETTINGS_TITLE
    # A stepper's key, and -1 (‹, ←) or +1 (›, →, Enter, a click on it focused).
    stepped = Signal(str, int)
    # An action row's key (the legacy rows': "legacy:<section key>"), a destructive row's, or
    # that of a row that opens a card.
    chosen = Signal(str)
    # A text row's key and its text, kept (Enter, a click elsewhere, ↑/↓) when it changed.
    edited = Signal(str, str)
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
        self._right = 0  # the right column's focused row (never an info line while focused)
        self._first = {LEFT: 0, RIGHT: 0}  # each column's first row in sight
        self._hover: Optional[tuple[str, int]] = None  # (column or _BACK, line)
        self._wheel = 0  # a wheel's turn not yet a whole row
        # The last press: (column, row key, part, whether it stepped, opened or acted), or
        # (_BACK or _FADE, "", "", True): a double-click's second press follows it
        # (_click_again).
        self._pressed: Optional[tuple[str, str, str, bool]] = None
        self._second_click = SecondClickGuard(self)
        # The text being edited: its field (None while none is), its row's key, and the text
        # it started from (kept unchanged, nothing is said).
        self._edit: Optional[TextEdit] = None
        self._edit_row = ""
        self._edit_from = ""
        self._drag = Dragging()  # the field's mouse: the caret, a drag's selection
        self._said = ""  # what assistive technology was last told
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
        self._edit = None
        self._drag.release()
        self._said = ""
        self._second_click.disarm()
        self._relayout()
        self._reveal(LEFT)
        self.show()
        self.raise_()
        self._say()

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
        """The right column's focused row (also while the keys are on the left); None when it
        has none to focus (no rows, or info lines only)."""
        rows = self._right_rows()
        if 0 <= self._right < len(rows) and rows[self._right].kind != INFO:
            return rows[self._right]
        return None

    def editing(self) -> bool:
        """Whether a text row's text is being edited (the field has the keys: `edit_key`)."""
        return self._edit is not None

    def refresh(self) -> None:
        """A value changed: paint the rows again (their values are asked anew), and say the
        focused row's new value."""
        self.update()
        self._say()

    def move_by(self, delta: int) -> None:
        if not delta:
            return
        if self._edit is not None:
            self._end_edit(keep=True)  # leaving the row keeps its text
        if self._side == LEFT:
            items = self._items(LEFT)
            if not items:
                return
            at = self._left_index()
            target = self._selectable(items, max(0, min(len(items) - 1, at + delta)), delta)
            if target is not None and target != at:
                self._set_left(target)
        else:
            items = self._items(RIGHT)
            if not items:
                return
            at = max(0, min(len(items) - 1, self._right + delta))
            target = self._selectable(items, at, delta)  # past an info line
            if target is not None:
                self._right = target
            self._reveal(RIGHT)
        self.update()
        self._say()

    def page(self, delta: int) -> None:
        """A page of rows of the focused column (those in sight, but one)."""
        shown = sum(1 for box in self._layout().boxes[self._side] if box is not None)
        self.move_by(delta * max(1, shown - 1))

    def left(self) -> None:
        if self._edit is not None:
            self.edit_key(Action.NAV_LEFT)
            return
        if self._side == LEFT:
            self.back()  # a layer up, as ← on the drawer's left column
            return
        row = self.focused_row()
        if row is not None and row.kind == STEP:
            self.stepped.emit(row.key, -1)
            return
        self._side = LEFT
        self._reveal(LEFT)
        self.update()
        self._say()

    def right(self) -> None:
        if self._edit is not None:
            self.edit_key(Action.NAV_RIGHT)
            return
        if self._side == LEFT:
            if self._focus_rows():
                self.update()
                self._say()
            return
        row = self.focused_row()
        if row is not None:
            self._act(row)

    def activate(self) -> None:
        """Enter: as →; while a text is edited, it keeps the text."""
        if self._edit is not None:
            self.edit_key(Action.NAV_ACTIVATE)
            return
        self.right()

    def forward(self) -> None:
        """The mouse's Forward: a layer down, never more. From the left column into the rows,
        and on a row that opens a layer (or a card), into it; it never steps a value, edits a
        text or acts."""
        if self._edit is not None:
            return
        if self._side == LEFT:
            self.right()
            return
        row = self.focused_row()
        if row is not None and row.kind == OPENS:
            self._act(row)

    def back(self) -> None:
        """One layer up: a layer gives way to the rows it is one of (the keys staying in their
        column), the rows to the sections, and the sections close. While a text is edited, it
        puts the text back instead (Esc)."""
        if self._edit is not None:
            self._end_edit(keep=False)
            return
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
        self._say()

    # ---- the text being edited --------------------------------------------------------------

    def type_text(self, text: str) -> None:
        """Typing goes into the field while a text is edited (else nowhere)."""
        if self._edit is not None and self._edit.insert(text):
            self.update()

    def backspace(self) -> None:
        if self._edit is not None and self._edit.backspace():
            self.update()

    def edit_key(self, action: Action, payload: str = "") -> bool:
        """A key while a text is edited: True when it was taken. Typing, Backspace (an empty
        field too, never a layer up), the caret keys (←/→, Home/End, with Shift, ⌥ or ⌘),
        Delete, ⌘A, copy, cut and paste are the field's; Enter keeps the text and Esc puts it
        back, the row staying focused. ↑/↓ and a page keep the text and give the key back
        (False), for the move. False, and nothing done, while no text is edited, and for any
        other key."""
        edit = self._edit
        if edit is None:
            return False
        if action in (Action.NAV_ACTIVATE, Action.OK):
            self._end_edit(keep=True)
        elif action is Action.ESCAPE:
            self._end_edit(keep=False)
        elif action in _LEAVING:
            self._end_edit(keep=True)
            return False
        elif action in _FIELD_KEYS:
            if action is Action.FORGET:
                action = Action.NAV_DELETE
            field_key(edit, action, payload, arrows=True)
            self.update()
        else:
            return False
        return True

    def _start_edit(self, row: Row) -> None:
        """The text row `row` (focused) is edited in its value's place, the caret at its end."""
        text = self._value_text(row)
        self._edit = TextEdit(text)
        self._edit_row = row.key
        self._edit_from = text
        self._reveal(RIGHT)
        self.update()

    def _end_edit(self, keep: bool) -> None:
        """The field closes: its text kept (`edited`, when it changed) or put back."""
        edit, key = self._edit, self._edit_row
        if edit is None:
            return
        self._edit = None
        self._drag.release()
        self.update()
        if keep and edit.text != self._edit_from:
            self.edited.emit(key, edit.text)
        self._say()

    def _editing_index(self) -> int:
        """The right column's line being edited (-1: none)."""
        if self._edit is None:
            return -1
        rows = self._right_rows()
        if 0 <= self._right < len(rows) and rows[self._right].key == self._edit_row:
            return self._right
        return -1

    # ---- what assistive technology hears ----------------------------------------------------

    def _say(self) -> None:
        """The focused line, and a row's value, said when either changed: the focus moved or
        a value did."""
        text = self._spoken() if self.isVisible() else ""
        if text and text != self._said:
            self._said = text
            announce(self, text)

    def _spoken(self) -> str:
        """The focused line's title, then its sub line, value or note ("Live latency, Auto")."""
        if self._side == LEFT:
            items = self._items(LEFT)
            index = self._left_index()
            if not 0 <= index < len(items):
                return ""
            item = items[index]
            parts = [title_of(item), item.row.sub if item.row is not None else ""]
        else:
            row = self.focused_row()
            if row is None:
                return ""
            value = self._value_text(row) if row.kind in (STEP, TEXT) else row.note
            parts = [title_of(Item(row.key, row.text, row=row)), row.sub, value]
        return ", ".join(part for part in parts if part)

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

    def _items(self, side: str) -> list[Item]:
        if side == RIGHT:
            return [
                Item(row.key, row.text, row=row, selectable=row.kind != INFO)
                for row in self._right_rows()
            ]
        chain = self._chain()
        if len(chain) < 2:
            return [Item(s.key, s.text, s.icon) for s in self._sections]
        return [
            Item(row.key, row.text, row=row, selectable=_opens_layer(row)) for row in chain[-2].rows
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

    def _focus_rows(self) -> bool:
        """The keys onto the right column, on `_right` or the nearest row after it (else
        before it) that is no info line; False, nothing changed, when it has none."""
        items = self._items(RIGHT)
        if not items:
            return False
        at = self._selectable(items, max(0, min(len(items) - 1, self._right)), 1)
        if at is None:
            return False
        self._right = at
        self._side = RIGHT
        self._reveal(RIGHT)
        return True

    @staticmethod
    def _selectable(items: list[Item], at: int, delta: int) -> Optional[int]:
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
        """→, Enter or a click on a right-column row: step it on, start editing its text,
        open its layer, or act (a card's row too). A layer with no row to focus leaves the
        keys on the left column, on the row that opened it."""
        if row.kind == STEP:
            self.stepped.emit(row.key, 1)
        elif row.kind == TEXT:
            self._start_edit(row)
        elif _opens_layer(row):
            self._opened.append(self._right)
            self._right = 0
            self._first = {LEFT: 0, RIGHT: 0}
            self._reveal(LEFT)
            if not self._focus_rows():
                self._side = LEFT
            self.update()
            self._say()
        elif row.kind != INFO:
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
        left_w, right_w = column_widths(un)
        rect = QRect(0, 0, left_w + right_w + un.px(FADE_U), parent.height())
        if rect != self.geometry():
            self.setGeometry(rect)
        self.update()

    @staticmethod
    def _help_parts(row: Row) -> tuple[str, str]:
        """The row's help and its restart note, translated ("" for what it has not)."""
        return (
            translate(row.help) if row.help else "",
            translate(RESTART_NOTE) if row.restart else "",
        )

    def _help_text(self, row: Row) -> str:
        """The row's help, then its restart note, whole (its tooltip)."""
        return " ".join(part for part in self._help_parts(row) if part)

    def _boxes(self, side: str, un: Units, area: QRectF) -> list[Box]:
        """Each line of a column, as tall as its title's lines, its sub line and its help, at
        y 0."""
        items = self._items(side)
        icons = side == LEFT and any(item.icon for item in items)
        focus = self._right if side == RIGHT and self._side == RIGHT else -1
        editing = self._editing_index() if side == RIGHT else -1
        width = area.width() - 2 * INSET_U * un.u
        boxes = []
        for index, item in enumerate(items):
            row = item.row if side == RIGHT else None
            value, choices, help = "", [], []
            if row is not None and row.kind in (STEP, TEXT, INFO):
                value = self._value_text(row)
                choices = [choice.shown() for choice in row.choices] if row.kind == STEP else []
            if row is not None and index == focus:
                help = help_lines(*self._help_parts(row), un, width)
            boxes.append(
                place(
                    un,
                    area,
                    item,
                    ends=side == RIGHT,
                    icons=icons,
                    value=value,
                    choices=choices,
                    help=help,
                    editing=index == editing,
                )
            )
        return boxes

    def _heights(self, side: str, un: Units, area: QRectF) -> list[float]:
        return [box.rect.height() for box in self._boxes(side, un, area)]

    def _reveal(self, side: str) -> None:
        """Scroll a column so that its highlighted line shows whole, and the lines above it
        that take no highlight (an info line over the first row) while they fit: the keys
        and the phone cannot scroll to them."""
        un = self._units()
        area = columns(un, float(self.height()))[1][side]
        heights = self._heights(side, un, area)
        if not heights:
            self._first[side] = 0
            return
        index = self._right if side == RIGHT else self._left_index()
        index = max(0, min(len(heights) - 1, index))
        items = self._items(side)
        top = index
        while top > 0 and not items[top - 1].selectable:
            top -= 1
        first = min(self._first[side], top)
        while first < index and sum(heights[first : index + 1]) > area.height() + 0.5:
            first += 1
        self._first[side] = min(first, last_first(heights, area.height()))

    def _scroll_by(self, side: str, rows: int) -> None:
        un = self._units()
        area = columns(un, float(self.height()))[1][side]
        heights = self._heights(side, un, area)
        first = max(0, min(self._first[side] + rows, last_first(heights, area.height())))
        if first != self._first[side]:
            self._first[side] = first
            self._hover = None
            self.update()

    def _layout(self) -> Layout:
        un = self._units()
        cols, areas = columns(un, float(self.height()))
        names = [translate(SETTINGS_TITLE)] + [translate(s.text) for s in self._chain()]
        back, crumbs = header(un, cols, names)
        shown: dict[str, list[Optional[Box]]] = {}
        for side in (LEFT, RIGHT):
            area = areas[side]
            boxes = self._boxes(side, un, area)
            heights = [box.rect.height() for box in boxes]
            out: list[Optional[Box]] = [None] * len(boxes)
            first = min(self._first[side], last_first(heights, area.height()))
            y = area.top()
            for index in range(first, len(boxes)):
                if index > first and y + heights[index] > area.bottom() + 0.5:
                    break
                out[index] = boxes[index].moved(y)
                y += heights[index]
            shown[side] = out
        return Layout(un, cols, areas, back, crumbs, shown)

    def _box(self, side: str, index: int) -> Optional[Box]:
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

    def _paint_header(self, p: QPainter, lay: Layout) -> None:
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

    def _paint_column(self, p: QPainter, lay: Layout, side: str) -> None:
        un = lay.units
        items = self._items(side)
        if side == RIGHT and not items:
            self._paint_empty(p, lay)
            return
        marked = self._left_index() if side == LEFT else self._right
        focused = self._side == side
        editing = self._editing_index() if side == RIGHT else -1
        text, muted = qcolor(PALETTE.text), qcolor(PALETTE.text, PALETTE.text_muted_alpha)
        for index, box in enumerate(lay.boxes[side]):
            if box is None:
                continue
            item = items[index]
            if index == marked and item.selectable and (side == LEFT or focused):
                paint_highlight(p, box.line, un, focused)
            elif self._hover == (side, index) and item.selectable:
                inset = box.line.adjusted(0.3 * un.u, 0.2 * un.u, -0.3 * un.u, -0.2 * un.u)
                fill_round(p, inset, qcolor(PALETTE.text, HOVER_ALPHA), 1.0 * un.u)
            if item.icon and not box.icon.isEmpty():
                self._paint_icon(p, item.icon, box.icon)
            row = item.row
            tone = text if item.selectable else muted
            if side == RIGHT and row is not None and row.kind == DANGER:
                tone = qcolor(PALETTE.live)
            self._paint_title(p, un, box, tone, muted)
            if side == LEFT or row is None:
                continue
            if row.kind == STEP:
                p.setFont(un.font(VALUE_FONT_U))
                value, lit = self._value_text(row), self._lit(row)
                accent = qcolor(PALETTE.accent)
                paint_value_steps(p, un, box.less, box.value, box.more, value, lit, accent)
            elif index == editing and self._edit is not None:
                self._paint_field(p, un, box.end, self._edit)
            elif not box.end.isEmpty():
                tail = self._value_text(row) if row.kind in (TEXT, INFO) else row.note
                p.setFont(un.font(VALUE_FONT_U))
                p.setPen(text if row.kind == TEXT else muted)
                fm = QFontMetricsF(p.font())
                shown = fm.elidedText(tail, Qt.TextElideMode.ElideRight, box.end.width())
                p.drawText(
                    box.end, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, shown
                )
            if not box.chevron.isEmpty():
                draw_icon(p, "chevron_right", box.chevron, muted)
            if box.help:
                self._paint_help(p, un, box, muted)

    @staticmethod
    def _paint_title(p: QPainter, un: Units, box: Box, tone: QColor, muted: QColor) -> None:
        """A line's title (a long one on two lines) and its sub line, centred as one."""
        align = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        title_font, sub_font = un.font(TITLE_FONT_U), un.font(SUB_FONT_U)
        spacing = QFontMetricsF(title_font).lineSpacing()
        sub_h = QFontMetricsF(sub_font).lineSpacing() if box.sub else 0.0
        y = box.line.center().y() - (spacing * len(box.titles) + sub_h) / 2
        p.setFont(title_font)
        p.setPen(tone)
        for line in box.titles:
            p.drawText(QRectF(box.title.x(), y, box.title.width(), spacing), align, line)
            y += spacing
        if box.sub:
            p.setFont(sub_font)
            p.setPen(muted)
            p.drawText(QRectF(box.title.x(), y, box.title.width(), sub_h), align, box.sub)

    @staticmethod
    def _paint_help(p: QPainter, un: Units, box: Box, muted: QColor) -> None:
        align = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        font = un.font(HELP_FONT_U)
        spacing = QFontMetricsF(font).lineSpacing()
        p.setFont(font)
        p.setPen(muted)
        y = box.line.bottom()
        width = box.line.right() - INSET_U * un.u - box.title.x()
        for line in box.help:
            p.drawText(QRectF(box.title.x(), y, width, spacing), align, line)
            y += spacing

    @staticmethod
    def _paint_field(p: QPainter, un: Units, rect: QRectF, edit: TextEdit) -> None:
        """The field a text is edited in, in its value's place: the find box's look, and the
        text with its caret and selection."""
        u = un.u
        height = min(rect.height(), float(un.px(FIELD_H_U)))
        box = QRectF(rect.x(), rect.center().y() - height / 2, rect.width(), height)
        radius = 0.6 * u
        fill_round(p, box, qcolor(PALETTE.canvas, 0.6), radius)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(qcolor(PALETTE.accent, 0.7))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(box.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
        p.restore()
        p.setFont(un.font(VALUE_FONT_U))
        inner = box.adjusted(FIELD_PAD_U * u, 0, -FIELD_PAD_U * u, 0)
        selection = qcolor(PALETTE.accent, SELECTION_ALPHA)
        paint_edit(p, inner, edit, qcolor(PALETTE.text), caret=True, selection=selection)

    def _paint_empty(self, p: QPainter, lay: Layout) -> None:
        """A layer without rows: its line, muted and centred at the column's top (as the
        drawer's column says it has nothing)."""
        chain = self._chain()
        text = translate(chain[-1].empty_text) if chain and chain[-1].empty_text else ""
        if not text:
            return
        area = lay.areas[RIGHT]
        p.setFont(lay.units.font(TITLE_FONT_U))
        p.setPen(qcolor(PALETTE.text, PALETTE.text_muted_alpha))
        p.drawText(
            QRectF(area.x(), area.y(), area.width(), EMPTY_H_U * lay.units.u),
            Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
            text,
        )

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
        of a right-column row being "less", "more", "line", "help" or, on the row being
        edited, _FIELD; None elsewhere."""
        lay = self._layout()
        if back_hit(lay.back, lay.units).contains(pos):
            return (_BACK, 0, "")
        editing = self._editing_index()
        for side in (LEFT, RIGHT):
            for index, box in enumerate(lay.boxes[side]):
                if box is None or not box.rect.contains(pos):
                    continue
                if box.less.contains(pos):
                    return (side, index, "less")
                if box.more.contains(pos):
                    return (side, index, "more")
                if side == RIGHT and index == editing and box.end.contains(pos):
                    return (side, index, _FIELD)
                return (side, index, "line" if box.line.contains(pos) else "help")
        if pos.x() >= lay.columns[RIGHT].right():
            return (_FADE, 0, "")
        return None

    def _click(self, hit: Optional[tuple[str, int, str]]) -> None:
        """A click on `hit` (what `_hit` found under it); what it did is kept for a
        double-click's second press."""
        self._pressed = None
        if hit is None:
            return
        target, index, part = hit
        if target in (_BACK, _FADE):
            self._pressed = (target, "", "", True)
            self._back_by_mouse()
            return
        if target == LEFT:
            self._click_left(index)
            return
        row = self._right_rows()[index]
        if row.kind == INFO:
            return  # it only says something
        focused = self._side == RIGHT and self._right == index
        self._side = RIGHT
        self._right = index
        self._reveal(RIGHT)
        self._pressed = (RIGHT, row.key, part, self._press(row, part, focused))
        self.update()
        self._say()

    def _press(self, row: Row, part: str, focused: bool) -> bool:
        """A click on the right column's `row`, the focused row now: ‹ or › step it, and its
        line does what Enter does: at once for a row that opens or acts, and for a stepper or
        a text only if it was `focused` before (it changes in place: the first click only
        focuses it, so that its help shows first). A click on the help does nothing. True
        when it stepped, opened, acted or started editing."""
        if part in ("less", "more"):
            self.stepped.emit(row.key, -1 if part == "less" else 1)
        elif part == "line" and (focused or row.kind not in (STEP, TEXT)):
            self._act(row)
        else:
            return False
        return True

    def _click_again(self) -> None:
        """A double-click's second press: ‹ or the fade go up again, else a click on the row
        the first press was on, focused now: a stepper or a text that only took the focus
        steps on or is edited, and a stepper the first press stepped steps the same way
        again. Its row is found by its key, not under the pointer: the first press moved the
        help to it, so the rows below it (and, in a small window, its ‹ beside a value of
        another width) moved. An action is not taken twice, an edit is not started again, and
        a layer the first press opened or a section is left alone."""
        pressed, self._pressed = self._pressed, None
        if pressed is None:
            return
        target, key, part, did = pressed
        if target in (_BACK, _FADE):
            self._back_by_mouse()
            return
        rows = self._right_rows()
        index = next((i for i, row in enumerate(rows) if row.key == key), None)
        if index is None or (did and rows[index].kind != STEP):
            return
        self._side = RIGHT
        self._right = index
        self._press(rows[index], part, focused=True)
        self.update()
        self._say()

    def _back_by_mouse(self) -> None:
        self.back()
        if not self.isVisible():
            self._second_click.arm()  # a double-click's second press reaches nothing behind

    def _click_left(self, index: int) -> None:
        item = self._items(LEFT)[index]
        if self._opened and not item.selectable:
            if item.row is not None and item.row.kind == INFO:
                return  # an info line of the layer above: nothing to go back up to
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
        self._say()

    def _press_while_editing(
        self, pos: QPointF, hit: Optional[tuple[str, int, str]], shift: bool, double: bool
    ) -> bool:
        """A press on `hit` while a text is edited: on its field it puts the caret (with Shift,
        it selects; a double-click takes a word), and elsewhere on its row it does nothing
        (True: taken). Elsewhere it keeps the text, and the press goes on as a click on what
        was under it (False): the field gone, the rows under it may have moved."""
        if self._edit is None:
            return False
        if hit is not None and hit[0] == RIGHT and hit[1] == self._editing_index():
            self._pressed = None
            if hit[2] == _FIELD:
                if double:
                    self._drag.double(self._edit, pos.x())
                else:
                    self._drag.press(self._edit, pos.x(), shift=shift)
                self.update()
            return True
        self._end_edit(keep=True)
        return False

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() in NAV_BUTTONS:
            event.ignore()  # Back/Forward go on to the host: the shell's layer up/down
            return
        event.accept()
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.position()
            hit = self._hit(pos)
            shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            if not self._press_while_editing(pos, hit, shift, double=False):
                self._click(hit)
            self._hover_at(pos)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() in NAV_BUTTONS:
            event.ignore()
            return
        event.accept()  # never the video's fullscreen toggle
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.position()
            if not self._press_while_editing(pos, self._hit(pos), False, double=True):
                self._click_again()
            self._hover_at(pos)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag.release()
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        held = event.buttons() & Qt.MouseButton.LeftButton
        if held and self._drag.move(event.position().x()):
            self.update()  # a drag in the field selects
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
        cols = self._layout().columns
        x = event.position().x()
        if x >= cols[RIGHT].right():
            return
        self._wheel += event.angleDelta().y()
        rows = int(self._wheel / WHEEL_NOTCH)  # towards zero: a notch up shows the rows above
        if rows:
            self._wheel -= rows * WHEEL_NOTCH
            self._scroll_by(LEFT if x < cols[LEFT].right() else RIGHT, -rows)

    def tooltip_at(self, pos: QPointF) -> str:
        """‹ names its key; a line cut short ("…") says its title (and value or note) whole;
        a row says its help and its restart note (under it only while it is focused), so that
        they can be read before a click."""
        hit = self._hit(pos)
        if hit is None or hit[0] == _FADE:
            return ""
        if hit[0] == _BACK:
            return key_hint(tr("Back"), "Esc")  # i18n: tooltip of ‹: a layer up (verb)
        side, index, _part = hit
        box, item = self._box(side, index), self._items(side)[index]
        if box is None:
            return ""
        whole_texts = []
        title = title_of(item)
        row = item.row
        if not whole(box.titles, title):
            whole_texts.append(title)
        if row is not None and row.sub and box.sub != " ".join(row.sub.split()):
            whole_texts.append(row.sub)
        if side == RIGHT and row is not None and index != self._editing_index():
            tail, room = "", 0.0
            if row.kind == STEP:
                tail, room = self._value_text(row), box.value.width()
            elif not box.end.isEmpty():
                tail = self._value_text(row) if row.kind in (TEXT, INFO) else row.note
                room = box.end.width()
            fm = QFontMetricsF(self._units().font(VALUE_FONT_U))
            if tail and fm.horizontalAdvance(tail) > room:
                whole_texts.append(tail)
        if row is not None and (help_text := self._help_text(row)):
            whole_texts.append(help_text)
        return "\n".join(whole_texts)

    def hideEvent(self, event) -> None:
        self._hover = None
        self._edit = None  # hidden while editing: the text is put back
        self._drag.release()
        super().hideEvent(event)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is self.parentWidget() and event.type() == QEvent.Type.Resize:
            self._relayout()
            if self.isVisible():  # the highlighted lines stay in sight
                self._reveal(LEFT)
                self._reveal(RIGHT)
        return False
