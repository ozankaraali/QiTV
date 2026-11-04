"""The painted find and search fields' text (2.9.3): a caret and a selection, as a text field
has them.

QiTV paints its fields (the channel list's Find, the Library's search and genre filter, the
subtitles search) so that a remote drives them: typing goes in, Backspace takes out, and the
arrows go between the lists. They kept only a string, so text went in at its end alone (the
owner: "ctrl a and click to change cursor place do not work"). `TextEdit` keeps the caret and
the selection; `paint_edit` draws them and `index_at` finds the place a click points at.

The keys stay the remote's until the caret is put in the text: a click in it, a drag, ⌘A (Ctrl+A)
or Shift with an arrow make the field `placed`, and then ←/→ move the caret (with Shift they
select, with ⌥ on macOS or Ctrl elsewhere by words, with ⌘ on macOS to an end) and Home/End go
to the ends. ↑/↓, Return and Escape go on as before and the caret goes back to the end, where
the remote types. No widgets here.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QGuiApplication, QPainter

from ui.keymap import Action

CARET_W = 0.08  # the caret's width, of the font's height
SELECTION_ALPHA = 0.55  # the selection's accent behind the text


def _is_word(char: str) -> bool:
    return char.isalnum() or char == "_"


class TextEdit:
    """A field's text, its caret (`cursor`, 0…len) and the selection's other end (`anchor`)."""

    def __init__(self, text: str = "") -> None:
        self.text = text
        self.cursor = len(text)
        self.anchor: Optional[int] = None
        self.placed = False  # the caret was put in the text: ←/→ edit instead of moving on
        self.scroll = 0.0  # how far the painted text is moved left (`paint_edit`)
        self.painted: Optional[tuple[QRectF, QFont, str]] = None  # where, how and what was drawn

    # ---- the text, from elsewhere ----------------------------------------------------------
    def set_text(self, text: str) -> None:
        """The text as set from elsewhere (a phone, a clear): the caret at its end (the same
        text: the caret stays)."""
        if text == self.text:
            return
        self.text = text
        self.cursor = len(text)
        self.anchor = None
        self.placed = False

    def leave(self) -> None:
        """The keys went on to the lists: the caret back at the end, where the remote types."""
        self.cursor = len(self.text)
        self.anchor = None
        self.placed = False

    # ---- the selection ---------------------------------------------------------------------
    def selection(self) -> tuple[int, int]:
        if self.anchor is None:
            return self.cursor, self.cursor
        return min(self.anchor, self.cursor), max(self.anchor, self.cursor)

    def has_selection(self) -> bool:
        start, end = self.selection()
        return start != end

    def selected_text(self) -> str:
        start, end = self.selection()
        return self.text[start:end]

    def select_all(self) -> None:
        if self.text:
            self.anchor, self.cursor = 0, len(self.text)
            self.placed = True

    def select_word(self, index: int) -> None:
        """The word at `index` selected (a double click)."""
        index = max(0, min(index, len(self.text)))
        start = end = index
        while start > 0 and _is_word(self.text[start - 1]):
            start -= 1
        while end < len(self.text) and _is_word(self.text[end]):
            end += 1
        if start == end:  # not on a word: the one character there
            end = min(len(self.text), index + 1)
        self.anchor, self.cursor = start, end
        self.placed = True

    def place(self, index: int, *, select: bool = False) -> None:
        """The caret put at `index` (a click; with `select` a drag or a Shift-click)."""
        index = max(0, min(index, len(self.text)))
        if select:
            if self.anchor is None:
                self.anchor = self.cursor
        else:
            self.anchor = None
        self.cursor = index
        self.placed = True

    # ---- editing ---------------------------------------------------------------------------
    def insert(self, text: str) -> bool:
        """`text` typed at the caret, over the selection; whether the text changed."""
        start, end = self.selection()
        if not text and start == end:
            return False
        self.text = self.text[:start] + text + self.text[end:]
        self.cursor = start + len(text)
        self.anchor = None
        return True

    def backspace(self) -> bool:
        start, end = self.selection()
        if start == end:
            if start == 0:
                return False
            start -= 1
        self.text = self.text[:start] + self.text[end:]
        self.cursor, self.anchor = start, None
        return True

    def delete(self) -> bool:
        """The character after the caret (or the selection) deleted."""
        start, end = self.selection()
        if start == end:
            if end >= len(self.text):
                return False
            end += 1
        self.text = self.text[:start] + self.text[end:]
        self.cursor, self.anchor = start, None
        return True

    def move(self, step: int, *, select: bool = False, word: bool = False, line: bool = False):
        """The caret one character (a word, to the end) left (`step` < 0) or right."""
        if not select and self.has_selection() and not (word or line):
            start, end = self.selection()  # an arrow ends a selection at its side
            self.cursor, self.anchor = (start if step < 0 else end), None
            self.placed = True
            return
        if line:
            target = 0 if step < 0 else len(self.text)
        elif word:
            target = self._word_edge(step)
        else:
            target = max(0, min(len(self.text), self.cursor + (-1 if step < 0 else 1)))
        self.place(target, select=select)
        if self.anchor == self.cursor:
            self.anchor = None

    def _word_edge(self, step: int) -> int:
        text, i = self.text, self.cursor
        if step < 0:
            while i > 0 and not _is_word(text[i - 1]):
                i -= 1
            while i > 0 and _is_word(text[i - 1]):
                i -= 1
        else:
            while i < len(text) and not _is_word(text[i]):
                i += 1
            while i < len(text) and _is_word(text[i]):
                i += 1
        return i


# ---- the keys --------------------------------------------------------------------------------

# Keys that move on to the lists: the caret goes back to the end, where the remote types.
_LEAVING = frozenset(
    {
        Action.NAV_UP,
        Action.NAV_DOWN,
        Action.NAV_PAGE_UP,
        Action.NAV_PAGE_DOWN,
        Action.NAV_ACTIVATE,
        Action.ESCAPE,
    }
)


def clipboard_line() -> str:
    """The clipboard's first line with text in it ("" when none)."""
    clipboard = QGuiApplication.clipboard()
    text = clipboard.text() if clipboard is not None else ""
    return next((part.strip() for part in text.splitlines() if part.strip()), "")


def _copy(text: str) -> None:
    clipboard = QGuiApplication.clipboard()
    if clipboard is not None and text:
        clipboard.setText(text)


def edit_key(edit: TextEdit, action: Action, payload: str = "", *, arrows: bool = False):
    """A key in a field: True when it changed the text, False when it moved the caret or the
    selection only, None when it isn't the field's (the caller does what it did before).
    `arrows`: ←/→ and Home/End are always the field's (nothing else there uses them)."""
    if action is Action.NAV_TEXT:
        return edit.insert(payload) or None
    if action is Action.NAV_BACKSPACE:
        return edit.backspace() if edit.text else None
    if action is Action.SELECT_ALL:
        edit.select_all()
        return False
    if action is Action.COPY:
        _copy(edit.selected_text())
        return False
    if action is Action.CUT:
        if not edit.has_selection():
            return False
        _copy(edit.selected_text())
        return edit.backspace()
    if action is Action.PASTE:
        return edit.insert(clipboard_line())
    mods = set(payload.split())
    if action in (Action.NAV_LEFT, Action.NAV_RIGHT):
        if "select" in mods:
            edit.placed = True  # Shift with an arrow selects, wherever the caret was
        if not (edit.placed or arrows) or not edit.text:
            return None
        step = -1 if action is Action.NAV_LEFT else 1
        edit.move(step, select="select" in mods, word="word" in mods, line="line" in mods)
        return False
    if action in (Action.NAV_HOME, Action.NAV_END):
        if not (edit.placed or arrows) or not edit.text:
            return None
        edit.move(-1 if action is Action.NAV_HOME else 1, select="select" in mods, line=True)
        return False
    if action is Action.NAV_DELETE:
        return edit.delete() if edit.placed or arrows else None
    if action in _LEAVING:
        edit.leave()
    return None


# ---- painting and the mouse ------------------------------------------------------------------


def paint_edit(
    p: QPainter,
    rect: QRectF,
    edit: TextEdit,
    tone: QColor,
    *,
    caret: bool,
    selection: QColor,
    shown: Optional[str] = None,
) -> None:
    """`edit`'s text in `rect` with the painter's font, at the left and in the middle, moved so
    the caret shows; the selection behind it and, with `caret`, the caret. `shown`: what is
    drawn instead of the text, character for character (a key's dots)."""
    text = edit.text if shown is None else shown
    fm = QFontMetricsF(p.font())
    height = fm.height()
    width = max(1.0, height * CARET_W)
    room = max(1.0, rect.width() - width)
    cursor = min(edit.cursor, len(text))
    caret_x = fm.horizontalAdvance(text[:cursor])
    total = fm.horizontalAdvance(text)
    if total <= room:
        scroll = 0.0
    else:  # the caret kept in view, and no room wasted after the end
        scroll = min(max(edit.scroll, caret_x - room), caret_x)
        scroll = max(0.0, min(scroll, total - room))
    edit.scroll = scroll
    edit.painted = (QRectF(rect), QFont(p.font()), text)
    top = rect.center().y() - height / 2
    left = rect.left() - scroll
    p.save()
    p.setClipRect(rect.adjusted(0, -2, width + 1, 2))
    start, end = edit.selection()
    if start != end:
        x0 = left + fm.horizontalAdvance(text[:start])
        x1 = left + fm.horizontalAdvance(text[:end])
        p.fillRect(QRectF(x0, top, x1 - x0, height), selection)
    p.setPen(tone)
    p.drawText(QPointF(left, top + fm.ascent()), text)
    if caret:
        p.fillRect(QRectF(left + caret_x, top, width, height), tone)
    p.restore()


def index_at(edit: TextEdit, x: float) -> int:
    """The place in the text nearest to `x` (the field's coordinates), as last painted."""
    if edit.painted is None:
        return len(edit.text)
    rect, font, text = edit.painted
    fm = QFontMetricsF(font)
    target = x - rect.left() + edit.scroll
    best, best_gap = 0, abs(target)
    for i in range(1, len(text) + 1):
        gap = abs(fm.horizontalAdvance(text[:i]) - target)
        if gap < best_gap:
            best, best_gap = i, gap
    return best


def text_rect(edit: TextEdit) -> QRectF:
    """Where the text was last painted (empty before)."""
    return QRectF(edit.painted[0]) if edit.painted is not None else QRectF()


class Dragging:
    """A field's mouse: a press puts the caret, a drag selects, a double click takes a word."""

    def __init__(self) -> None:
        self.active: Optional[TextEdit] = None

    def press(self, edit: TextEdit, x: float, *, shift: bool) -> None:
        edit.place(index_at(edit, x), select=shift)
        self.active = edit

    def move(self, x: float) -> bool:
        if self.active is None:
            return False
        self.active.place(index_at(self.active, x), select=True)
        if self.active.anchor == self.active.cursor:
            self.active.anchor = None
        return True

    def release(self) -> None:
        self.active = None

    def double(self, edit: TextEdit, x: float) -> None:
        edit.select_word(index_at(edit, x))
        self.active = None
