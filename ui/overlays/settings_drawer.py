"""Settings as a layer of the TV window's drawer (Settings in the drawer, plans 1 and 2).

It looks and moves like the channel list, with its columns, highlight and breadcrumb
(ui/overlays/drawer_look.py), and the picture plays on to the right of the fade. The left
column lists the sections and the right one the highlighted section's rows, shown at once as
the highlight moves. A row is of a kind (services/settings_model.py's KINDS): it steps through
its choices ("Live latency  ‹ Auto ›", the value lit in the accent while it is not the
default), opens a layer of rows of its own or a card (it ends in ›), acts, acts on what cannot
be undone (in red, its title ending in "…": the caller asks first), holds a text edited in
place ("Name  Demo TV"), or only says something (muted, and the focus goes past it: its value,
else its note). A secret text (a token) shows dots in its place, in its field too, and is never
said, given as a tooltip or copied out; one that shows its end (an API key's last four) shows
it after a fixed row of dots, so that its length doesn't show. An empty text shows its
placeholder, muted. A row may have an icon of its own before its title, in the accent (＋ Add
provider's, as the channel list's action rows; the first after rows without one has a gap
above it), a muted sub line under its title, and a note at its right end. In a layer the left
column lists the rows the layer is one of, the opened one marked as the drawer marks its path;
there only the rows that open a layer take the highlight, and a
click on another goes back up to it. A layer (or a section) without rows says its
`empty_text` and the keys stay on the left. Only the right column's focused row shows its
help under it, three lines at most (written for two); a note cut short at its row's end
shows there whole instead (a check's answer). Where everything goes is
ui/overlays/settings_layout.py's.

It changes nothing itself: a step is `stepped(key, ±1)`, an action (a card's row too)
`chosen(key)`, a text kept `edited(key, text)`, and backing out of the sections `closed`; the
caller applies them and calls `refresh()`. The value a row shows and whether it is lit are
asked of the caller at each paint (`value_text`, `lit`). The sections may change while they
show (a provider added or removed, here or elsewhere: `set_sections`): the place is kept by
key, and a line that went gives its place to its neighbour; a renamed one's new keys are told
(`renamed`), so the place follows it. A row's title is translated, unless it is a name the
viewer gave (`literal`: a provider's, shown as given, its layer's breadcrumb too).

The keys (the shell hands them over): ↑/↓ move. On a stepper of the right column ←/→ step and
Enter steps on; on another right-column row ← goes to the left column, and → or Enter opens its
layer or card, acts, or starts editing its text. On the left column → or Enter goes right, and
← is `back()`. `back()` goes one layer up: a layer, the rows it is one of, the sections, closed.
While a text is edited the field has the keys (`edit_key`): typing, Backspace, the caret keys,
⌘A, copy, cut and paste; Enter keeps the text, Esc (and Back) puts it back, the row staying
focused; ↑/↓ keep it and move on. The mouse's Forward (`forward()`) only goes a layer down.
Assistive technology hears the focused line and its value as they change (`announce`):
"Settings" first as it opens, and a layer's empty line as it is entered. Hidden, it is named
"Settings" again.

Typing finds a setting (`find_type`, and Paste: `find_paste`; a text being edited takes them
instead): the find's line shows over the right column, as the channel list's Find box, with the
text and a ✕ (the breadcrumb keeps to the left column meanwhile), and the right column lists
the sections whose title has the words and the rows of every section and layer whose title or
help has them (ui/overlays/settings_find.py), each row with where it is as its sub line
("Providers › Demo TV"); the keys are on the first. ↑/↓ move among them; Enter, → (the mouse's
Forward too) or a click on one opens its section or layer with that row focused (an info line:
the row after it; a section: its first row); Backspace edits the text (`find_backspace`),
its last letter going ends the find; Esc, ←, ✕, ‹, the fade (`back()`) and a click on the left
column end it (`end_find`), the right column back as it was. New sections while it shows: the
find stays, over them.

The mouse: a click on a row focuses it. On the right column a row whose click changes it in
place (a stepper's value, a text) only takes the focus while it has not got it, so that its
help shows before anything changes; a click on it focused steps it on or starts editing. A
row that opens or acts does so on the first click, as the drawer's rows do; an info line
takes no click. ‹ or › step their row at once, focused or not. While a text is edited, a
click in its field puts the caret (a drag selects, a double-click takes a word), its Paste at
the field's end puts the clipboard's first line in (a mouse cannot press Ctrl/⌘+V; the phone
types its own text in), and a click elsewhere keeps the text, then does what it does, unless
keeping it changed the rows under it or the layer (a rename: ‹ and the fade too). A
double-click's second press is a click on the row the first one was on (an action is taken
once). Hovering a row shows its help, the restart note too, as its tooltip. The wheel scrolls
the column under the pointer. A column longer than its room shows the drawer's thin scrollbar
(ui/overlays/scroll_indicator.py) in its side padding at its right edge while it scrolls and
while the pointer is over it, and dragging it scrolls the column. ‹ in the header and a click
on the fade over the picture are `back()`. The mouse's Back and Forward buttons go on to the
shell, as over the drawer.

Each line is laid out (its title and a long value wrapped) once for what it shows: again after
new sections, a value changed (`refresh`) or the window's width, never at a mouse move.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from functools import partial
import logging
from typing import Optional

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFontMetricsF, QMouseEvent, QPainter, QWheelEvent
from PySide6.QtWidgets import QWidget

from services.i18n import N_, tr, translate
from services.settings_model import DANGER, INFO, OPENS, STEP, TEXT, Row, Section
from ui import text_edit
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
    find_clear_tooltip,
    paint_backdrop,
    paint_highlight,
)
from ui.overlays.painting import Units, draw_icon, fill_round, paint_value_steps, ui_units
from ui.overlays.scroll_indicator import ScrollIndicator
from ui.overlays.settings_find import Target, find, find_index
from ui.overlays.settings_layout import (  # its metrics are offered from here too
    EMPTY_H_U,
    FIELD_H_U,
    FIELD_PAD_U,
    HELP_FONT_U,
    ICON_GAP_U,
    ICON_U,
    INSET_U,
    LEFT,
    PASTE_FONT_U,
    PASTE_TEXT,
    RIGHT,
    SUB_FONT_U,
    VALUE_FONT_U,
    Box,
    Item,
    Layout,
    back_hit,
    columns,
    find_line,
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
# i18n: in Settings' right column while what was typed finds no setting; {typed} is what was typed
NO_SETTING_FOUND = N_("No setting has “{typed}”")
MASK = "•"  # a secret text's every character, as a FormCard's secret field shows it
SECRET_DOTS = 8  # the dots before a secret's end that shows (Row.secret_tail), whatever its length

_FALLBACK_HOST_WIDTH = 1280
_BACK = "back"
_FADE = "fade"
_FIND = "find"  # the find's line
_FIND_CLEAR = "find_clear"  # its ✕
_FIELD = "field"  # the part of the row being edited that is its field
_PASTE = "paste"  # and its Paste
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


def masked(text: str, tail: int = 0) -> str:
    """A secret as its row shows it: a dot for each character; one that shows its last `tail`
    characters (an API key's last four) shows them after SECRET_DOTS dots, so that its length
    doesn't show, and only while at least as many stay hidden (a shorter one is the dots
    alone)."""
    if tail <= 0:
        return MASK * len(text)
    if len(text) < 2 * tail:
        return MASK * SECRET_DOTS
    return MASK * SECRET_DOTS + text[-tail:]


def _rekeyed(key: str, renamed: Mapping[str, str]) -> str:
    """`key` under its new name: a key `renamed` names, or a key under one (after a "/"),
    takes the new one in its place ("provider/A/name" → "provider/B/name"); "provider/A" is
    no part of "provider/AB"."""
    for old, new in renamed.items():
        if key == old or key.startswith(old + "/"):
            return new + key[len(old) :]
    return key


class SettingsDrawer(Overlay):
    accessible_name = SETTINGS_TITLE
    # A stepper's key, and -1 (‹, ←) or +1 (›, →, Enter, a click on it focused).
    stepped = Signal(str, int)
    # An action row's key, a destructive row's, or that of a row that opens a card.
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
        self._clicking = False  # a click on a row acts now (a hide meanwhile arms the guard)
        # The text being edited: its field (None while none is), its row's key, and the text
        # it started from (kept unchanged, nothing is said).
        self._edit: Optional[TextEdit] = None
        self._edit_row = ""
        self._edit_from = ""
        self._edit_secret = False  # its row's text is a secret: dots, never copied out
        self._drag = Dragging()  # the field's mouse: the caret, a drag's selection
        self._paste_hovered = False
        self._said = ""  # what assistive technology was last told
        # The find: its text (None while there is none), the rows it looks through (made at
        # an opening's first find, again for new sections), what it found (and those as the
        # right column lists them), and where the keys and the right column were before it
        # (its side, focused row and first row in sight: Esc puts them back).
        self._find: Optional[TextEdit] = None
        self._targets: Optional[list[Target]] = None
        self._found: list[Target] = []
        self._found_rows: tuple[Row, ...] = ()
        self._before_find: Optional[tuple[str, int, int]] = None
        # Each line's place at y 0, by what it is laid out from (`_placed_key`): a line is
        # wrapped once for what it shows, not at each paint or mouse move. Emptied for new
        # sections, a value changed and another width (`_placed_width`: the host's).
        self._placed: dict[tuple, Box] = {}
        self._placed_width = -1
        self._indicators = {
            side: ScrollIndicator(
                self,
                partial(self._scroll_metrics, side),
                partial(self._drag_to, side),
                partial(self._list_area, side),
            )
            for side in (LEFT, RIGHT)
        }
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
        self._drop_find(restore=False)
        self._targets = None
        self._said = ""
        self._second_click.disarm()
        self._placed.clear()
        self._relayout()
        self._reveal(LEFT)
        self.show()
        self.raise_()
        self._say(opening=True)

    def set_sections(
        self, sections: Sequence[Section], renamed: Optional[Mapping[str, str]] = None
    ) -> None:
        """The sections as they are now, while they show (a provider added or removed, here or
        elsewhere): the place stays, by key. A section, layer or row that is still there keeps
        its place (its line may have moved); one that went gives it to its neighbour, the line
        that took its place (else the one before), past the info lines. A layer that went
        closes, the keys going onto the rows it was one of, on its neighbour (never into the
        layer that took its place: that is another provider). A text being edited stays while
        its row does, else it is put back. A line is known by its key alone: one whose key
        changed went, and another came, unless `renamed` says it is the same one under a new
        key (old key → new: a provider renamed, `provider/<name>` and every key under it), so
        that the viewer stays where they were in it."""
        find_text, found_key = "", ""
        if self._find is not None:  # the place before it is kept by key, then it finds anew
            find_text, found_key = self._find.text, self._focused_key()
            self._drop_find(restore=True)
        old_section = self._sections[self._section].key if self._sections else ""
        old_path = self.path()[1:]
        old_opened = list(self._opened)
        rows = self._right_rows()
        old_focus = rows[self._right].key if 0 <= self._right < len(rows) else ""
        if renamed:
            old_path = tuple(_rekeyed(key, renamed) for key in old_path)
            old_focus = _rekeyed(old_focus, renamed)
            found_key = _rekeyed(found_key, renamed)
            self._edit_row = _rekeyed(self._edit_row, renamed)
            if self._pressed is not None:
                target, key, part, did = self._pressed
                self._pressed = (target, _rekeyed(key, renamed), part, did)
        self._sections = tuple(sections)
        self._targets = None
        self._placed.clear()
        self._hover = None
        keys = [section.key for section in self._sections]
        if old_section in keys:
            self._section = keys.index(old_section)
        else:  # the section went: its neighbour, from its top
            self._section = max(0, min(self._section, len(keys) - 1))
            old_path, old_focus, self._side, self._right = (), "", LEFT, 0
        gone_at = self._reopen(old_path, old_opened)
        items = self._items(RIGHT)
        if gone_at is not None:
            at, self._side = gone_at, RIGHT
            self._first = {LEFT: 0, RIGHT: 0}
        else:
            at = next((i for i, item in enumerate(items) if item.key == old_focus), self._right)
        found = self._selectable(items, max(0, min(len(items) - 1, at)), 1) if items else None
        self._right = 0 if found is None else found
        if found is None:
            self._side = LEFT
        if self._edit is not None and self._editing_index() < 0:
            self._edit = None  # its row went (or the keys left it): the text is put back
            self._drag.release()
        if find_text:
            self._start_find(find_text, found_key)
        self._reveal(LEFT)
        self._reveal(RIGHT)
        self.update()
        self._say()

    def _reopen(self, keys: Sequence[str], indices: list[int]) -> Optional[int]:
        """The layers `keys` opened again in the new sections, as far as each is still there;
        where one went, the index it had (`indices`), else None."""
        self._opened = []
        rows = self._sections[self._section].rows if self._sections else ()
        for key, index in zip(keys, indices):
            at = next((i for i, row in enumerate(rows) if row.key == key), None)
            if at is None or not _opens_layer(rows[at]):
                return index
            self._opened.append(at)
            section = rows[at].section
            rows = section.rows if section is not None else ()
        return None

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
        """A value changed: lay the rows out and paint them again (their values are asked
        anew), and say the focused row's new value."""
        self._placed.clear()
        self._refit_indicators()
        self.update()
        self._say()

    def scroll_indicator(self, side: str) -> ScrollIndicator:
        """The thin scrollbar of the column `side` ("left" or "right")."""
        return self._indicators[side]

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
        if self._find is not None:
            self.end_find()  # as ← in the channel list's find: back to where it started
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
        puts the text back instead (Esc); in a find, it ends the find."""
        if self._edit is not None:
            self._end_edit(keep=False)
            return
        if self._find is not None:
            self.end_find()
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
            if self._edit_secret and action in (Action.COPY, Action.CUT):
                return True  # a secret is never copied out
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
        self._edit_secret = row.secret
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

    # ---- the find ---------------------------------------------------------------------------

    def finding(self) -> bool:
        """Whether the right column lists what the find found (its line shows)."""
        return self._find is not None

    def find_text(self) -> str:
        """What the find finds ("" while there is none)."""
        return self._find.text if self._find is not None else ""

    def find_type(self, text: str) -> None:
        """Typing while no text is edited: into the find, a find started by it (a space alone
        starts none). While a text is edited the field has the typing (`edit_key`)."""
        if self._edit is not None:
            return
        if self._find is None:
            if not text.strip():
                return
            self._start_find(text)
        else:
            if not self._find.insert(text):
                return
            self._match()
        self._find_shown()

    def find_backspace(self) -> None:
        """Backspace in the find: its last letter goes; with it, the find (`end_find`)."""
        if self._find is None or not self._find.backspace():
            return
        if not self._find.text:
            self.end_find()
            return
        self._match()
        self._find_shown()

    def end_find(self) -> None:
        """Esc, ←, ✕, ‹ or the fade in the find: it ends, the keys and the right column back
        where they were before it."""
        if self._find is None:
            return
        self._drop_find(restore=True)
        self._reveal(LEFT)
        self._reveal(RIGHT)
        self._refit_indicators()
        self.update()
        self._say()

    def _start_find(self, text: str, focus_key: str = "") -> None:
        """A find of `text` (the place it starts from kept for Esc), the keys on the match
        `focus_key` (else the first). Its rows are made once for the sections shown."""
        if self._targets is None:
            self._targets = find_index(self._sections)
        self._before_find = (self._side, self._right, self._first[RIGHT])
        self._find = TextEdit(text)
        self._side = RIGHT
        self._match(focus_key)

    def _match(self, focus_key: str = "") -> None:
        """What the find's text finds, the keys on the match `focus_key` (else the first:
        the best), the right column from its top."""
        if self._find is None:
            return
        self._found = find(self._targets or [], self._find.text)
        self._found_rows = tuple(
            Row(
                target.row.key,
                target.title,  # as shown already (translated, "…" on a destructive row)
                target.row.help,
                kind=OPENS,
                restart=target.row.restart,
                sub=target.where,
                literal=True,
            )
            for target in self._found
        )
        keys = [row.key for row in self._found_rows]
        self._right = keys.index(focus_key) if focus_key in keys else 0
        self._first[RIGHT] = 0
        self._hover = None
        self._indicators[RIGHT].refit()  # other rows

    def _find_shown(self) -> None:
        """The find's line and rows changed: in sight, painted and said."""
        self._reveal(RIGHT)
        self.update()
        self._say()

    def _drop_find(self, restore: bool) -> None:
        """No find any more; with `restore`, the keys and the right column where they were
        before it."""
        if self._find is None:
            return
        before, self._before_find = self._before_find, None
        self._find = None
        self._found, self._found_rows = [], ()
        self._hover = None
        if restore and before is not None:
            self._side, self._right, self._first[RIGHT] = before
        self._indicators[RIGHT].refit()  # other rows

    def _focused_key(self) -> str:
        rows = self._right_rows()
        return rows[self._right].key if 0 <= self._right < len(rows) else ""

    def _go_to_found(self, index: int) -> None:
        """Enter, → or a click on the find's row `index`: its section or layer opens, that row
        focused (an info line: the nearest row after it, else before it; a section found by
        its title: its first row, else the section on the left), and the find ends; Esc then
        goes up from there."""
        if not 0 <= index < len(self._found):
            return
        target = self._found[index]
        self._drop_find(restore=False)
        self._pressed = None  # a double-click's second press does not step what it reached
        self._section = target.section
        self._opened = list(target.opened)
        self._right = max(target.index, 0)  # a section found: its first row
        self._first = {LEFT: 0, RIGHT: 0}
        if not self._focus_rows():
            self._side = LEFT
        self._reveal(LEFT)
        self._reveal(RIGHT)
        self._refit_indicators()
        self.update()
        self._say()

    def find_paste(self) -> None:
        """Paste (Ctrl/⌘+V) while no text is edited: the clipboard's first line into the find,
        as the channel list's find takes it (a find started by it)."""
        line = text_edit.clipboard_line()
        if line:
            self.find_type(line)

    def _find_notice(self) -> str:
        """What the right column says while the find finds nothing (said whole)."""
        return translate(NO_SETTING_FOUND, typed=" ".join(self.find_text().split()))

    def _shown_notice(self, un: Units, width: float) -> str:
        """The notice as the column paints it, on one line of `width` where it can be: what
        was typed elided ("…") so that the notice is never cut; whole when it fits."""
        typed = " ".join(self.find_text().split())
        whole = translate(NO_SETTING_FOUND, typed=typed)
        fm = QFontMetricsF(un.font(TITLE_FONT_U))
        if fm.horizontalAdvance(whole) <= width:
            return whole
        room = width - fm.horizontalAdvance(translate(NO_SETTING_FOUND, typed=""))
        room = max(room, fm.horizontalAdvance("…"))  # (a notice longer than its line wraps)
        return translate(
            NO_SETTING_FOUND, typed=fm.elidedText(typed, Qt.TextElideMode.ElideRight, room)
        )

    # ---- what assistive technology hears ----------------------------------------------------

    def _say(self, opening: bool = False) -> None:
        """The focused line, and a row's value, said when either changed: the focus moved or
        a value did. `opening`: what it is is said first ("Settings, Playback")."""
        text = self._spoken() if self.isVisible() else ""
        if text and opening:
            text = ", ".join((translate(SETTINGS_TITLE), text))
        if text and text != self._said:
            self._said = text
            announce(self, text)

    def _spoken(self) -> str:
        """The focused line's title, then its sub line, value or note ("Live latency, Auto");
        on the left, the line its layer shows when it has no rows ("No hidden groups")."""
        if self._side == LEFT:
            items = self._items(LEFT)
            index = self._left_index()
            if not 0 <= index < len(items):
                return ""
            item = items[index]
            parts = [title_of(item), item.row.sub if item.row is not None else ""]
            chain = self._chain()
            if not self._right_rows() and chain and chain[-1].empty_text:
                parts.append(translate(chain[-1].empty_text))
        else:
            row = self.focused_row()
            if row is None:
                return self._find_notice() if self._find is not None else ""
            if row.kind == STEP:
                value = self._value_text(row)
            elif row.kind == TEXT:  # a secret's dots are no value to say
                value = "" if row.secret and self._value_text(row) else self._tail_text(row)
            else:
                value = row.note
            parts = [title_of(Item(row.key, row.text, row=row)), row.sub, value]
        return ", ".join(part for part in parts if part)

    def _tail_text(self, row: Row) -> str:
        """What the row shows at its right end: a text or an info line's value (a secret's in
        dots, an empty text's placeholder; an info line without one, its note), else its
        note."""
        if row.kind not in (TEXT, INFO):
            return row.note
        value = self._value_text(row)
        if row.kind == INFO:
            return value or row.note
        if row.secret and value:
            return masked(value, row.secret_tail)
        if self._shows_placeholder(row):
            return translate(row.placeholder)
        return value

    def _shows_placeholder(self, row: Row) -> bool:
        """Whether the text row shows its placeholder: it has one, and no text."""
        return row.kind == TEXT and bool(row.placeholder) and not self._value_text(row)

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

    def paste_rect(self, index: int) -> QRectF:
        """The Paste at the end of the field the right column's row `index` is edited in;
        empty while it is not edited."""
        box = self._box(RIGHT, index)
        return QRectF(box.paste) if box is not None else QRectF()

    def column_rect(self, side: str) -> QRectF:
        return QRectF(self._layout().columns[side])

    def back_rect(self) -> QRectF:
        """‹ at the start of the breadcrumb."""
        return QRectF(self._layout().back)

    def row_anchor(self, key: str) -> QPoint:
        """The global point under the right column's row `key`, at its left (where a menu it
        opens pops up: Free channels); the overlay's corner while it is out of sight."""
        keys = [row.key for row in self._right_rows()]
        box = self._box(RIGHT, keys.index(key)) if key in keys else None
        if box is None:
            return self.mapToGlobal(QPoint(0, 0))
        return self.mapToGlobal(box.line.bottomLeft().toPoint())

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
        if self._find is not None:
            return self._found_rows
        chain = self._chain()
        if not chain:
            return ()
        return chain[-1].rows

    def _items(self, side: str) -> list[Item]:
        if side == RIGHT:
            return [
                Item(row.key, row.text, row.icon, row=row, selectable=row.kind != INFO)
                for row in self._right_rows()
            ]
        chain = self._chain()
        if len(chain) < 2:
            return [Item(s.key, s.text, s.icon, literal=s.literal) for s in self._sections]
        return [
            Item(row.key, row.text, row.icon, row=row, selectable=_opens_layer(row))
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
            self._indicators[RIGHT].refit()  # other rows
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
        keys on the left column, on the row that opened it. A row the find found goes to its
        row, in its section or layer."""
        if self._find is not None:
            self._go_to_found(self._right)
            return
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
            self._refit_indicators()
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
        if parent.width() != self._placed_width:
            self._placed.clear()  # other units: every line is laid out again
            self._placed_width = parent.width()
        un = self._units()
        left_w, right_w = column_widths(un)
        rect = QRect(0, 0, left_w + right_w + un.px(FADE_U), parent.height())
        if rect != self.geometry():
            self.setGeometry(rect)
        self._refit_indicators()
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
        y 0: laid out once for what it shows (`_placed`)."""
        items = self._items(side)
        # The sections keep their icons' room; a row's own icon (＋, ▶) is its line's alone.
        icons = side == LEFT and len(self._chain()) < 2 and any(item.icon for item in items)
        focus = self._right if side == RIGHT and self._side == RIGHT else -1
        editing = self._editing_index() if side == RIGHT else -1
        fonts = un.font(TITLE_FONT_U).key()  # the family and the size the lines are set in
        boxes = []
        for index, item in enumerate(items):
            row = item.row if side == RIGHT else None
            value = ""
            choices: tuple[str, ...] = ()
            if row is not None and row.kind == STEP:
                value = self._value_text(row)
                choices = tuple(c.shown() for c in row.choices)
            elif row is not None and row.kind in (TEXT, INFO):
                value = self._tail_text(row)
            focused = row is not None and index == focus
            # The first row with an icon of its own after rows without one: a gap above it.
            apart = not icons and index > 0 and bool(item.icon) and not items[index - 1].icon
            key = self._placed_key(
                side, item, un, area, icons, value, choices, focused, index == editing, fonts
            ) + (apart,)
            box = self._placed.get(key)
            if box is None:
                help = self._focused_help(un, area, item) if focused else []
                box = place(
                    un,
                    area,
                    item,
                    ends=side == RIGHT,
                    icons=icons,
                    value=value,
                    choices=choices,
                    help=help,
                    editing=index == editing,
                    apart=apart,
                )
                self._placed[key] = box
            boxes.append(box)
        return boxes

    def _placed_key(
        self,
        side: str,
        item: Item,
        un: Units,
        area: QRectF,
        icons: bool,
        value: str,
        choices: tuple[str, ...],
        focused: bool,
        editing: bool,
        fonts: str,
    ) -> tuple:
        """All that a line's place (settings_layout.place) is laid out from, as shown now (the
        texts translated): two lines with the same key have the same place."""
        row = item.row
        kind, sub, note = (row.kind, row.sub, row.note) if row is not None else ("", "", "")
        warn = row is not None and row.warn  # (its sub line's colour: painted, kept apart)
        helps = self._help_parts(row) if focused and row is not None else None
        paste = translate(PASTE_TEXT) if editing else ""
        return (
            side,
            title_of(item),
            item.icon,
            kind,
            sub,
            warn,
            note,
            value,
            choices,
            helps,
            editing,
            paste,
            icons,
            un.u,
            area.x(),
            area.width(),
            fonts,
        )

    def _focused_help(self, un: Units, area: QRectF, item: Item) -> list[str]:
        """The lines under the focused row `item`: its help and its restart note, or its note
        whole when it is cut short at the row's end."""
        row = item.row
        if row is None:
            return []
        help_text, restart = self._help_parts(row)
        if self._note_cut(un, area, item):
            help_text = row.note  # whole under it while focused (a check's answer)
        width = area.width() - 2 * INSET_U * un.u
        if item.icon:  # it starts under the title, past the row's own icon (Play's ▶)
            width -= (ICON_U + ICON_GAP_U) * un.u
        return help_lines(help_text, restart, un, width)

    @staticmethod
    def _note_cut(un: Units, area: QRectF, item: Item) -> bool:
        """Whether the right column's row `item` has a note cut short at its end ("Failed to
        verify pro…"): the note is then shown whole under it, in its help's place, while it is
        focused (the keys and the phone have no tooltip)."""
        row = item.row
        if row is None or not row.note or row.kind in (STEP, TEXT, INFO):
            return False
        box = place(un, area, item, ends=True, icons=False)
        advance = QFontMetricsF(un.font(VALUE_FONT_U)).horizontalAdvance(row.note)
        return advance > box.end.width() + 0.5

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
            self._indicators[side].refit()
            return
        index = self._right if side == RIGHT else self._left_index()
        index = max(0, min(len(heights) - 1, index))
        items = self._items(side)
        top = index
        while top > 0 and not items[top - 1].selectable:
            top -= 1
        first = min(self._first[side], top)
        total = sum(heights[first : index + 1])
        while first < index and total > area.height() + 0.5:
            total -= heights[first]
            first += 1
        first = min(first, last_first(heights, area.height()))
        if first != self._first[side]:
            self._first[side] = first
            self._indicators[side].poke()  # it moved: its thumb shows a moment
        else:
            self._indicators[side].refit()  # a help shown elsewhere: another length

    def _scroll_by(self, side: str, rows: int) -> None:
        self._scroll_to(side, self._first[side] + rows)

    def _scroll_to(self, side: str, first: int) -> None:
        """The column `side` scrolled to show its line `first` first (as far as it goes: its
        last line at its bottom); the highlight stays where it is."""
        un = self._units()
        area = columns(un, float(self.height()))[1][side]
        heights = self._heights(side, un, area)
        first = max(0, min(first, last_first(heights, area.height())))
        if first != self._first[side]:
            self._first[side] = first
            self._hover = None
            self._indicators[side].poke()
            self.update()

    # ---- the columns' scrollbars ------------------------------------------------------------

    def _list_rect(self, side: str) -> QRectF:
        """Where the column `side`'s lines scroll, its side padding included: its scrollbar
        runs down its right edge, in the padding (never over a row's ›)."""
        cols, areas = columns(self._units(), float(self.height()))
        col, area = cols[side], areas[side]
        return QRectF(col.x(), area.y(), col.width(), area.height())

    def _list_area(self, side: str) -> QRect:
        return self._list_rect(side).toAlignedRect()

    def _drag_to(self, side: str, first: float) -> None:
        """The scrollbar dragged: the column shows its line `first` (rounded) first."""
        self._scroll_to(side, round(first))

    def _scroll_metrics(self, side: str) -> tuple[float, float, float]:
        """For the column's scrollbar, in lines: (the first in sight, the lines its end shows,
        all of them); it scrolls when all of them do not fit."""
        un = self._units()
        area = columns(un, float(self.height()))[1][side]
        heights = self._heights(side, un, area)
        last = last_first(heights, area.height())
        return float(min(self._first[side], last)), float(len(heights) - last), float(len(heights))

    def _refit_indicators(self) -> None:
        """The scrollbars over the columns as they are now (gone from one that fits)."""
        for indicator in self._indicators.values():
            indicator.refit()

    def _layout(self) -> Layout:
        un = self._units()
        cols, areas = columns(un, float(self.height()))
        names = [translate(SETTINGS_TITLE)]
        names += [s.text if s.literal else translate(s.text) for s in self._chain()]
        finding = self._find is not None
        back, crumbs = header(un, cols, names, upto=LEFT if finding else RIGHT)
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
        lay = Layout(un, cols, areas, back, crumbs, shown)
        if finding:
            lay.find, lay.find_clear = find_line(un, cols)
        return lay

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
        if self._find is not None:
            self._paint_find(p, lay, self._find)
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

    def _paint_find(self, p: QPainter, lay: Layout, edit: TextEdit) -> None:
        """The find's line: the channel list's Find box as it looks while it finds (⌕, the
        text and its caret, ✕)."""
        u = lay.units.u
        box, clear = lay.find, lay.find_clear
        fill_round(p, box, qcolor(PALETTE.canvas, 0.6), 0.9 * u)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(qcolor(PALETTE.accent, 0.7))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(box, 0.9 * u, 0.9 * u)
        p.restore()
        size = 1.5 * u
        icon = QRectF(box.x() + 1.0 * u, box.center().y() - size / 2, size, size)
        draw_icon(p, "search", icon, qcolor(PALETTE.text, 0.8))
        inner = QRectF(icon.right() + 0.7 * u, box.y(), 0, box.height())
        inner.setRight(max(inner.left(), clear.left() - 0.6 * u))
        p.setFont(lay.units.font(TITLE_FONT_U))
        selection = qcolor(PALETTE.accent, SELECTION_ALPHA)
        paint_edit(p, inner, edit, qcolor(PALETTE.text), caret=True, selection=selection)
        mark = 1.1 * u
        hovered = self._hover == (_FIND_CLEAR, 0)
        tone = qcolor(PALETTE.accent) if hovered else qcolor(PALETTE.text, 0.6)
        center = clear.center()
        draw_icon(
            p, "close", QRectF(center.x() - mark / 2, center.y() - mark / 2, mark, mark), tone
        )

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
                tone = qcolor(PALETTE.danger_text)  # (readable over a bright picture)
            elif row is not None and row.warn and not row.sub:
                tone = qcolor(PALETTE.warn)  # an info line that says why something failed
            warned = row is not None and row.warn  # its sub line says why it failed
            self._paint_title(p, un, box, tone, qcolor(PALETTE.warn) if warned else muted)
            if side == LEFT or row is None:
                continue
            if row.kind == STEP:
                p.setFont(un.font(VALUE_FONT_U))
                lit, accent = self._lit(row), qcolor(PALETTE.accent)
                lines = box.values
                one = lines[0] if len(lines) == 1 else ""  # (it paints a value on one line)
                paint_value_steps(p, un, box.less, box.value, box.more, one, lit, accent)
                if len(lines) > 1:
                    self._paint_value_lines(p, box.value, lines, accent if lit else muted)
            elif index == editing and self._edit is not None:
                self._paint_field(p, un, box.entry, self._edit, secret=row.secret)
                self._paint_paste(p, un, box.paste, self._paste_hovered)
            elif not box.end.isEmpty():
                tail = self._tail_text(row)
                p.setFont(un.font(VALUE_FONT_U))
                p.setPen(text if row.kind == TEXT and not self._shows_placeholder(row) else muted)
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
        """A line's title (a long one on two lines) and its sub line (on its lines), centred
        as one."""
        align = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        title_font, sub_font = un.font(TITLE_FONT_U), un.font(SUB_FONT_U)
        spacing = QFontMetricsF(title_font).lineSpacing()
        sub_spacing = QFontMetricsF(sub_font).lineSpacing()
        y = box.line.center().y() - (spacing * len(box.titles) + sub_spacing * len(box.subs)) / 2
        p.setFont(title_font)
        p.setPen(tone)
        for line in box.titles:
            p.drawText(QRectF(box.title.x(), y, box.title.width(), spacing), align, line)
            y += spacing
        p.setFont(sub_font)
        p.setPen(muted)
        for line in box.subs:
            p.drawText(QRectF(box.title.x(), y, box.title.width(), sub_spacing), align, line)
            y += sub_spacing

    @staticmethod
    def _paint_value_lines(p: QPainter, rect: QRectF, lines: list[str], color: QColor) -> None:
        """A stepper's value too wide for one line, on its lines (wrapped and elided to fit
        `rect`), centred one under another between its ‹ and ›, in the painter's font."""
        spacing = QFontMetricsF(p.font()).lineSpacing()
        y = rect.center().y() - spacing * len(lines) / 2
        p.setPen(color)
        for line in lines:
            p.drawText(
                QRectF(rect.x(), y, rect.width(), spacing), Qt.AlignmentFlag.AlignCenter, line
            )
            y += spacing

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
    def _paint_field(
        p: QPainter, un: Units, rect: QRectF, edit: TextEdit, *, secret: bool = False
    ) -> None:
        """The field a text is edited in, in its value's place: the find box's look, and the
        text (a secret's dots) with its caret and selection."""
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
        shown = MASK * len(edit.text) if secret else None
        paint_edit(
            p, inner, edit, qcolor(PALETTE.text), caret=True, selection=selection, shown=shown
        )

    @staticmethod
    def _paint_paste(p: QPainter, un: Units, rect: QRectF, hovered: bool) -> None:
        """The field's Paste: a button as the OpenSubtitles page's that is not the accent."""
        if rect.isEmpty():
            return
        fill_round(p, rect, qcolor(PALETTE.text, 0.22 if hovered else 0.14), rect.height() / 2)
        p.setFont(un.font(PASTE_FONT_U))
        p.setPen(qcolor(PALETTE.text))
        label = QFontMetricsF(p.font()).elidedText(
            translate(PASTE_TEXT), Qt.TextElideMode.ElideRight, rect.width()
        )
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)

    def _paint_empty(self, p: QPainter, lay: Layout) -> None:
        """A layer without rows (or a find that found none): its line, muted and centred at
        the column's top (as the drawer's column says it has nothing)."""
        chain = self._chain()
        area = lay.areas[RIGHT]
        if self._find is not None:
            text = self._shown_notice(lay.units, area.width())
        else:
            text = translate(chain[-1].empty_text) if chain and chain[-1].empty_text else ""
        if not text:
            return
        font = lay.units.font(TITLE_FONT_U)
        flags = Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap
        # As tall as its lines need (a long translation wraps), never cut at its box's foot.
        needed = QFontMetricsF(font).boundingRect(QRectF(0, 0, area.width(), 1e6), int(flags), text)
        height = max(EMPTY_H_U * lay.units.u, needed.height())
        p.setFont(font)
        p.setPen(qcolor(PALETTE.text, PALETTE.text_muted_alpha))
        p.drawText(QRectF(area.x(), area.y(), area.width(), height), flags, text)

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
        """What is at `pos`: (_BACK, 0, ""), (_FADE, 0, ""), the find's line ((_FIND, 0, "")
        or its ✕: (_FIND_CLEAR, 0, "")), or (column, line, part), the part of a right-column
        row being "less", "more", "line", "help" or, on the row being edited, _FIELD or its
        _PASTE; None elsewhere."""
        lay = self._layout()
        if lay.find_clear.contains(pos):
            return (_FIND_CLEAR, 0, "")
        if lay.find.contains(pos):
            return (_FIND, 0, "")
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
                if side == RIGHT and index == editing and box.paste.contains(pos):
                    return (side, index, _PASTE)
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
        if target == _FIND_CLEAR:
            self.end_find()
            return
        if target == _FIND:
            return  # its text is typed, not clicked into
        if target == LEFT:
            if self._find is not None:
                self._drop_find(restore=True)  # the left column's rows: the find gives way
            self._click_left(index)
            return
        if self._find is not None:
            if part == "line":
                self._go_to_found(index)  # at once, as a row that opens
            return
        row = self._right_rows()[index]
        if row.kind == INFO:
            return  # it only says something
        focused = self._side == RIGHT and self._right == index
        self._side = RIGHT
        self._right = index
        self._reveal(RIGHT)
        self._clicking = True  # a row that closes Settings arms the guard as it hides
        try:
            done = self._press(row, part, focused)
        finally:
            self._clicking = False
        self._pressed = (RIGHT, row.key, part, done)
        if not self.isVisible():
            return  # it closed Settings (a link's Play)
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
        it selects; a double-click takes a word), on its Paste it pastes (a double-click's
        second press does not again), and elsewhere on its row it does nothing (True: taken).
        Elsewhere it keeps the text, and the press goes on as a click on what was under it
        (False): the field gone, the rows under it may have moved."""
        if self._edit is None:
            return False
        if hit is not None and hit[0] == RIGHT and hit[1] == self._editing_index():
            self._pressed = None
            if hit[2] == _PASTE:
                if not double:
                    self._paste_by_mouse()
            elif hit[2] == _FIELD:
                if double:
                    self._drag.double(self._edit, pos.x())
                else:
                    self._drag.press(self._edit, pos.x(), shift=shift)
                self.update()
            return True
        self._end_edit(keep=True)
        return False

    def _paste_by_mouse(self) -> None:
        """The field's Paste: the clipboard's first line in place of the text, or of the part
        selected in it (as the cards' Paste; Ctrl/⌘+V types it at the caret)."""
        edit = self._edit
        line = text_edit.clipboard_line() if edit is not None else ""
        if edit is None or not line:
            return
        if edit.has_selection():
            edit.insert(line)
        else:
            edit.set_text(line)
        self.update()

    def _line_at(self, hit: Optional[tuple[str, int, str]]) -> Optional[tuple[str, str]]:
        """The column and key of the line `hit` is on (‹ and the fade: theirs alone); None
        for none, or for a line no longer there."""
        if hit is None:
            return None
        target, index, _part = hit
        if target in (_BACK, _FADE, _FIND, _FIND_CLEAR):
            return (target, "")
        items = self._items(target)
        return (target, items[index].key) if 0 <= index < len(items) else None

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() in NAV_BUTTONS:
            event.ignore()  # Back/Forward go on to the host: the shell's layer up/down
            return
        event.accept()
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.position()
            hit = self._hit(pos)
            line, path = self._line_at(hit), self.path()
            shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            if not self._press_while_editing(pos, hit, shift, double=False):
                if self._line_at(hit) != line or self.path() != path:
                    # Keeping the text changed the rows under it, or the layer (a rename):
                    # the press was aimed at what went, ‹ and the fade too (never a layer
                    # further up than the one seen).
                    hit = None
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
        shown = self.isVisible()
        for side, indicator in self._indicators.items():
            indicator.pointer_over(shown and self._list_rect(side).contains(pos))
        hit = self._hit(pos) if shown else None
        over = (hit[0], hit[1]) if hit is not None and hit[0] != _FADE else None
        paste = hit is not None and hit[2] == _PASTE
        if over != self._hover or paste != self._paste_hovered:
            self._hover = over
            self._paste_hovered = paste
            button = paste or (over is not None and over[0] in (_BACK, _FIND_CLEAR))
            self.setCursor(
                Qt.CursorShape.PointingHandCursor if button else Qt.CursorShape.ArrowCursor
            )
            self.update()

    def leaveEvent(self, event: QEvent) -> None:
        for indicator in self._indicators.values():
            indicator.pointer_over(False)
        if self._hover is not None or self._paste_hovered:
            self._hover = None
            self._paste_hovered = False
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
        """‹ and the field's Paste name their key; a line cut short ("…") says its title (and
        value or note) whole; a row says its help and its restart note (under it only while it
        is focused), so that they can be read before a click."""
        hit = self._hit(pos)
        if hit is None or hit[0] in (_FADE, _FIND):
            return ""
        if hit[0] == _BACK:
            return key_hint(tr("Back"), "Esc")  # i18n: tooltip of ‹: a layer up (verb)
        if hit[0] == _FIND_CLEAR:
            return find_clear_tooltip()
        if hit[2] == _PASTE:
            return key_hint(translate(PASTE_TEXT), "Ctrl+V")
        side, index, _part = hit
        box, item = self._box(side, index), self._items(side)[index]
        if box is None:
            return ""
        whole_texts = []
        title = title_of(item)
        row = item.row
        if not whole(box.titles, title):
            whole_texts.append(title)
        if row is not None and row.sub and not whole(box.subs, row.sub):
            whole_texts.append(row.sub)
        if side == RIGHT and row is not None and index != self._editing_index():
            tail, cut = "", False
            fm = QFontMetricsF(self._units().font(VALUE_FONT_U))
            if row.kind == STEP:
                tail = self._value_text(row)
                if len(box.values) > 1:  # wrapped: whole unless its last line was elided
                    cut = not whole(box.values, tail)
                else:
                    cut = fm.horizontalAdvance(tail) > box.value.width()
            elif not box.end.isEmpty() and not (row.kind == TEXT and row.secret):
                tail = self._tail_text(row)  # (a secret's, even its dots, is never a tooltip)
                cut = fm.horizontalAdvance(tail) > box.end.width()
            if tail and cut:
                whole_texts.append(tail)
        if row is not None and (help_text := self._help_text(row)):
            whole_texts.append(help_text)
        return "\n".join(whole_texts)

    def hideEvent(self, event) -> None:
        if self._clicking and self.isHidden():
            # A click on a row closed Settings (a link's Play): a double-click's second press
            # would land on what is under it, the picture. Armed now, not once the click is
            # over: what the click does next may wait in a modal window, and the guard is
            # spent while it shows, never swallowing the click after it.
            self._second_click.arm()
        self._hover = None
        self._paste_hovered = False
        for indicator in self._indicators.values():
            indicator.pointer_over(False)
            indicator.hide()  # not there when Settings opens again, on rows that may fit
        self._edit = None  # hidden while editing: the text is put back
        self._drag.release()
        self._drop_find(restore=True)  # (shown again, as it was before the find)
        # announce() named it after what it said last: assistive technology finds "Settings".
        announce(self, translate(SETTINGS_TITLE), speak=False)
        super().hideEvent(event)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is self.parentWidget() and event.type() == QEvent.Type.Resize:
            self._relayout()
            if self.isVisible():  # the highlighted lines stay in sight
                self._reveal(LEFT)
                self._reveal(RIGHT)
        return False
