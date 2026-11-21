"""The painted menu (2.13.1): every right-click and button menu QiTV opens, in the drawer's look.

A menu is plain data (`Menu`: its entries), built where it is asked for, and shown by one
painted overlay per page (`PopupMenu`): the player's overlay host has one, the Library's page
another. `popup(menu, near, …)` shows it on the page of `near`, the widget that asks (a row's
list, a button's bar); while the Library is in front, a menu the player asks for (the corner
player's right-click) opens there, beside the corner (`defer_to`).

- An entry has its text, and may be disabled (dimmed; the keys and the pointer pass it by),
  ticked or not (a ✓ column), a submenu (›), or a separator (a rule: none leads, ends or
  doubles, as QMenu's collapse). A tick that keeps the menu open (`keep_open`: the Library's
  providers) turns while it stays; any other entry closes the menu, then acts.
- It opens at the pointer for a right-click (`at`), or beside a row or a button (`beside`, on
  `side`: RIGHT of a row, BELOW or ABOVE a button), turned over where there is no room and
  kept inside the page; what does not fit scrolls (the wheel; ▴ ▾ say entries lie beyond). A
  submenu opens beside its entry.
- Keys reach it through the shell while it shows (`handle`; the shell resolves them as the
  drawer's then, so the phone's arrows, OK and Back reach it too): ↑ ↓ move (round), Home,
  End and Page ↑ ↓ to the ends, Enter, Space and → choose (→ opens a submenu), Esc, ← and
  Backspace close one level, a letter goes to the next entry it starts, the menu key (or
  Shift+F10, which open a highlighted row's menu beside it: `at` its rect) closes it. The sound keys and
  fullscreen go on; every other key is the menu's while it shows.
- The pointer: the entry under it is highlighted; a click (pressed and released on it, either
  button) chooses it; a click anywhere else in the window closes the menu and does nothing more
  (but the second click of the double-click that opened it); the mouse's Back closes a level.
- It never waits: what an entry does is its callback, run once the menu closed. The menu's
  close callbacks (`Menu.on_close`) run first, once, whatever closes it: a choice, Esc, a
  click elsewhere, its page changing size or hiding, the list that opened it hiding (`opener`),
  another menu opening, or anything in its window that has the keys showing (an overlay with
  `takes_keys`: a card, a question, a panel, Settings), since the keys reach a menu that shows
  first: it is only ever the topmost thing.

It is painted as the drawer's rows are: the raised panel, the row's metrics and font, the
accent highlight (the parent's entry dimmer while its submenu is open). Assistive technology
calls it "Menu" and hears the entry highlighted as it moves ("✓ " before a ticked one).
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import math
from typing import Callable, Optional, Union

from PySide6.QtCore import QElapsedTimer, QEvent, QObject, QPoint, QPointF, QRect, QRectF, QSize, Qt
from PySide6.QtGui import (
    QFont,
    QFontMetricsF,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QRegion,
    QWheelEvent,
)
from PySide6.QtWidgets import QApplication, QWidget
from shiboken6 import isValid

from services.i18n import N_, translate
from ui.keymap import Action
from ui.overlays.base import Overlay, announce
from ui.overlays.drawer_look import ROW_H_U, TITLE_FONT_U, paint_highlight
from ui.overlays.painting import Units, draw_icon, ui_units
from ui.theme import PALETTE, qcolor

logger = logging.getLogger(__name__)

RIGHT, BELOW, ABOVE = "right", "below", "above"  # beside a row; under or over a button
_POINT = "point"  # at the pointer
# Where a menu opens, in global coordinates: the point a right-click was at, or the rect of the
# row (a poster, a tile) the menu key was pressed on: beside it (2.13.1).
Where = Union[QPoint, QRect]
# i18n: what a screen reader calls a right-click menu (or a button's menu) as it opens
MENU_NAME = N_("Menu")
TICK_SAID = "✓ "  # before a ticked entry's text, as assistive technology hears it
_FALLBACK_HOST_WIDTH = 1280
_PAD_U = 0.5  # inside the panel, above and below its rows
_SIDE_U = 1.2  # a row's text from its sides (the drawer's rows: 0.9u + 0.3u)
_TICK_U = 1.6  # the ✓ (a column, when any entry ticks)
_ARROW_U = 1.3  # a submenu's ›
_MARK_GAP_U = 0.6  # between a ✓ or › and the text
_SCROLL_U = 1.0  # ▴ ▾
_SEPARATOR_U = 1.2
_MIN_WIDTH_U = 14.0
_EDGE_U = 0.6  # from the page's sides
_GAP_U = 0.3  # from the row or the button it opens beside
_RADIUS_U = 1.0
_DISABLED_ALPHA = 0.38
_WHEEL_STEP = 120  # one notch: one entry
_THROUGH = (Action.MUTE, Action.VOL_UP, Action.VOL_DOWN, Action.FULLSCREEN, Action.MINI_PLAYER)
_DOWN = (Action.NAV_DOWN, Action.CH_NEXT, Action.TAB)
_UP = (Action.NAV_UP, Action.CH_PREV)
_FIRST = (Action.NAV_HOME, Action.NAV_PAGE_UP)
_LAST = (Action.NAV_END, Action.NAV_PAGE_DOWN)
_CHOOSE = (Action.NAV_ACTIVATE, Action.OK, Action.NAV_RIGHT, Action.SEEK_FWD)
_BACK = (Action.ESCAPE, Action.NAV_LEFT, Action.NAV_BACKSPACE, Action.PREVIOUS, Action.SEEK_BACK)
_PRESSES = (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick)
_CLICKS = (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton)


@dataclass(eq=False)
class MenuEntry:
    text: str = ""
    act: Optional[Callable[[], None]] = None
    enabled: bool = True
    checked: Optional[bool] = None  # None: no tick; True or False: ticked or not
    keep_open: bool = False  # a tick that turns while the menu stays open
    submenu: Optional["Menu"] = None
    separator: bool = False

    def choosable(self) -> bool:
        return self.enabled and not self.separator

    def trigger(self) -> None:
        """What choosing it does (a submenu's entry only opens it, shown): its tick turns,
        then it acts. A disabled entry does nothing."""
        if not self.choosable() or self.submenu is not None:
            return
        if self.checked is not None:
            self.checked = not self.checked
        if self.act is not None:
            self.act()


class Menu:
    """A menu's entries, top to bottom, and what runs as it closes."""

    def __init__(self) -> None:
        self.entries: list[MenuEntry] = []
        self._on_close: list[Callable[[], None]] = []

    def add(
        self,
        text: str,
        act: Optional[Callable[[], None]] = None,
        *,
        enabled: bool = True,
        checked: Optional[bool] = None,
        keep_open: bool = False,
    ) -> MenuEntry:
        entry = MenuEntry(text, act, enabled, checked, keep_open)
        self.entries.append(entry)
        return entry

    def add_separator(self) -> None:
        self.entries.append(MenuEntry(separator=True))

    def add_menu(self, text: str) -> Menu:
        """An entry that opens a submenu (›): the submenu, to fill."""
        submenu = Menu()
        self.entries.append(MenuEntry(text, submenu=submenu))
        return submenu

    def on_close(self, callback: Callable[[], None]) -> None:
        """`callback` runs once the menu closes, before the chosen entry acts."""
        self._on_close.append(callback)

    def closed(self) -> None:
        """It closed: its close callbacks run, once."""
        callbacks, self._on_close = self._on_close, []
        for callback in callbacks:
            callback()

    def shown_entries(self) -> list[MenuEntry]:
        """Its entries as they show: no separator first, last, or right after another."""
        shown: list[MenuEntry] = []
        for entry in self.entries:
            if entry.separator and (not shown or shown[-1].separator):
                continue
            shown.append(entry)
        while shown and shown[-1].separator:
            shown.pop()
        return shown

    def texts(self) -> list[str]:
        """Its entries' texts, separators left out."""
        return [entry.text for entry in self.entries if not entry.separator]

    def entry(self, text: str) -> MenuEntry:
        """The entry that says `text` (StopIteration: none does)."""
        return next(e for e in self.entries if not e.separator and e.text == text)


class _Level:
    """A menu shown: its entries as they show, its panel (in the page's coordinates), the
    entry highlighted (-1: none) and the first in sight."""

    def __init__(self, menu: Menu, entries: list[MenuEntry], panel: QRect, scrolls: bool):
        self.menu = menu
        self.entries = entries
        self.panel = panel
        self.scrolls = scrolls
        self.current = -1
        self.top = 0


def _first_letter(text: str) -> str:
    """What typing finds an entry by: its first letter or digit ("★ Favorites": f)."""
    return next((c for c in text if c.isalnum()), "").casefold()[:1]


def _span(after: int, before: int, length: int, low: int, high: int, prefer_after: bool) -> int:
    """Where a panel `length` long starts along one of the page's sides (`low` to `high`): at
    `after` (it runs on past what it opens from) or at `before` (it ends before it); the side
    preferred when it fits, else the other when that fits, else the roomier, kept inside."""
    fits_after, fits_before = after + length <= high, before >= low
    room_after, room_before = high - after, before + length - low
    if prefer_after:
        start = after if fits_after or (not fits_before and room_after >= room_before) else before
    else:
        start = before if fits_before or (not fits_after and room_before >= room_after) else after
    return max(low, min(start, high - length))


class PopupMenu(Overlay):
    accessible_name = MENU_NAME

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self._levels: list[_Level] = []
        self._opener: Optional[QWidget] = None
        self._pressed: Optional[tuple[int, int]] = None  # (level, entry) a press was on
        self._shown = QElapsedTimer()  # since it opened: a double-click's second press
        self._filtering = False
        self._wheel = 0
        self._defer: Optional[tuple[PopupMenu, Callable[[], bool]]] = None
        self._page_size = QSize()  # its page's, as it was laid out against
        if parent is not None:
            parent.installEventFilter(self)  # its page's size or hiding closes it
        self.hide()

    # ---- public API ---------------------------------------------------------------------

    def defer_to(self, other: PopupMenu, when: Callable[[], bool]) -> None:
        """While `when()`, a menu asked of this one opens on `other` (another page in front:
        the Library, while the player sits in its corner), beside this one's page."""
        self._defer = (other, when)

    def open(
        self,
        menu: Menu,
        *,
        at: Optional[Where] = None,
        beside: Optional[QRect] = None,
        side: str = RIGHT,
        opener: Optional[QWidget] = None,
    ) -> PopupMenu:
        """Show `menu` at `at` (a global point: a right-click's; a rect: the row the menu key
        was pressed on, beside it), else beside `beside` (a global rect: a row's, a button's)
        on `side`; a menu shown closes first. It closes too when `opener` (the list that
        asked) hides. Returns the overlay that shows it."""
        if isinstance(at, QRect):
            at, beside, side = None, at, RIGHT
        parent = self.parentWidget()
        if self._defer is not None and parent is not None and self._defer[1]():
            page = QRect(parent.mapToGlobal(QPoint(0, 0)), parent.size())
            return self._defer[0].open(menu, beside=page, side=RIGHT, opener=opener)
        self.close_menu()
        if parent is None:
            menu.closed()
            return self
        if self.geometry() != parent.rect():
            self.setGeometry(parent.rect())  # its own coordinates are the page's
        if at is not None:
            anchor, side = QRect(parent.mapFromGlobal(at), QSize(1, 1)), _POINT
        elif beside is not None:
            anchor = QRect(parent.mapFromGlobal(beside.topLeft()), beside.size())
        else:
            anchor, side = QRect(0, 0, 1, 1), _POINT
        level = self._level(menu, anchor, side)
        if level is None:  # nothing in it to show
            menu.closed()
            return self
        self._levels = [level]
        self._first(level)
        self._page_size = parent.size()
        self._opener = opener if opener is not parent else None  # (its page: filtered always)
        if self._opener is not None:
            self._opener.installEventFilter(self)
        self._pressed, self._wheel = None, 0
        self._shown.start()
        self.show()
        self.raise_()
        self._start_filtering()
        self.update()
        self._say(opening=True)
        return self

    def close_menu(self) -> None:
        """Close it, every level: its close callbacks run, once."""
        if self._levels:
            self._finish()
        elif not self.isHidden():
            self.hide()

    def shown(self) -> bool:
        return bool(self._levels)

    def menu(self) -> Optional[Menu]:
        """The menu shown (its first level), None while closed."""
        return self._levels[0].menu if self._levels else None

    def depth(self) -> int:
        """How many levels show: 1, 2 with a submenu open; 0 closed."""
        return len(self._levels)

    def entries(self, level: int = -1) -> list[MenuEntry]:
        return list(self._levels[level].entries) if self._levels else []

    def panel(self, level: int = -1) -> QRect:
        """A level's panel, in its own (its page's) coordinates."""
        return QRect(self._levels[level].panel) if self._levels else QRect()

    def current(self) -> Optional[MenuEntry]:
        """The entry the keys act on: the one highlighted in the deepest level."""
        if not self._levels:
            return None
        level = self._levels[-1]
        return level.entries[level.current] if 0 <= level.current < len(level.entries) else None

    def entry_rect(self, entry: MenuEntry) -> QRect:
        """`entry`'s row in sight, in its own coordinates (empty: not in sight)."""
        un = self._units()
        for level in self._levels:
            for index, rect in self._rows(level, un):
                if level.entries[index] is entry:
                    return rect.toAlignedRect()
        return QRect()

    def cut(self, entry: MenuEntry) -> str:
        """`entry`'s text as its row shows it: whole, or cut short ("…") where it can't fit."""
        un = self._units()
        for level in self._levels:
            if entry in level.entries:
                left, right = self._margins(level.entries, un, level.scrolls)
                width = level.panel.width() - 2 * un.px(_PAD_U) - left - right
                return self._elided(entry.text, QFontMetricsF(self._font(un)), width)
        return entry.text

    def handle(self, action: Action, payload: str = "") -> bool:
        """A key while it shows: True when it is the menu's (every key but the sound's and
        the window's). Closed, nothing is its own."""
        if not self._levels or action in _THROUGH:
            return False
        if action in _DOWN:
            self._move(1)
        elif action in _UP:
            self._move(-1)
        elif action in _FIRST or action in _LAST:
            self._to_end(last=action in _LAST)
        elif action in _CHOOSE or (action is Action.NAV_TEXT and payload == " "):
            level = self._levels[-1]
            if level.current >= 0:
                self._choose(len(self._levels) - 1, level.current)
        elif action in _BACK:
            self.back()
        elif action is Action.CONTEXT_MENU:
            self.close_menu()  # the menu key again: as it opened it, it closes it (2.13.1)
        elif action is Action.NAV_TEXT:
            self._jump(payload)
        return True

    def back(self) -> None:
        """One level back: its submenu closes, or the menu."""
        if len(self._levels) > 1:
            self._levels.pop()
            self._levels_changed()
        else:
            self.close_menu()

    def mask_region(self) -> QRegion:
        """Its panels: the page's other overlays keep the rest (a click there closes it)."""
        region = QRegion()
        for level in self._levels:
            region = region.united(QRegion(level.panel.translated(self.pos())))
        return region

    # ---- layout -------------------------------------------------------------------------

    def _units(self) -> Units:
        parent = self.parentWidget()
        return ui_units(parent.width() if parent is not None else _FALLBACK_HOST_WIDTH)

    @staticmethod
    def _font(un: Units) -> QFont:
        return un.font(TITLE_FONT_U)

    def _height(self, entry: MenuEntry, un: Units) -> float:
        if entry.separator:
            return float(un.px(_SEPARATOR_U))
        text = QFontMetricsF(self._font(un)).height() + un.px(1.0)
        return float(max(un.px(ROW_H_U), math.ceil(text)))

    @staticmethod
    def _margins(entries: list[MenuEntry], un: Units, scrolls: bool) -> tuple[float, float]:
        """Where a row's text starts from its left and ends from its right: past the ✓
        column (when any entry ticks), before the › (a submenu's) and the ▴ ▾ (it scrolls)."""
        left = right = float(un.px(_SIDE_U))
        if any(entry.checked is not None for entry in entries):
            left += un.px(_TICK_U) + un.px(_MARK_GAP_U)
        if any(entry.submenu is not None for entry in entries):
            right += un.px(_ARROW_U) + un.px(_MARK_GAP_U)
        if scrolls:
            right += un.px(_SCROLL_U) + un.px(_MARK_GAP_U)
        return left, right

    def _level(self, menu: Menu, anchor: QRect, side: str) -> Optional[_Level]:
        """`menu` laid out beside `anchor` (the page's coordinates), or None with nothing in it."""
        parent = self.parentWidget()
        entries = menu.shown_entries()
        if parent is None or not any(not entry.separator for entry in entries):
            return None
        un = self._units()
        pad, edge = un.px(_PAD_U), un.px(_EDGE_U)
        room_w, room_h = parent.width() - 2 * edge, parent.height() - 2 * edge
        total = 2 * pad + sum(self._height(entry, un) for entry in entries)
        scrolls = total > room_h
        rows = 2 * pad + max(self._height(entry, un) for entry in entries)
        height = math.ceil(max(rows, min(total, room_h)))
        fm = QFontMetricsF(self._font(un))
        text = max(fm.horizontalAdvance(e.text) for e in entries if not e.separator)
        left, right = self._margins(entries, un, scrolls)
        width = max(math.ceil(2 * pad + left + text + right), un.px(_MIN_WIDTH_U))
        size = QSize(max(1, min(width, room_w)), height)
        return _Level(menu, entries, self._place(size, anchor, side, un), scrolls)

    def _place(self, size: QSize, anchor: QRect, side: str, un: Units) -> QRect:
        """The panel's place beside `anchor`, turned over where there is no room, inside the
        page: at a point, its top left there; RIGHT of a row, its first entry level with the
        row; BELOW or ABOVE a button, at its left."""
        parent = self.parentWidget()
        width, height = (parent.width(), parent.height()) if parent is not None else (0, 0)
        edge, gap, pad = un.px(_EDGE_U), un.px(_GAP_U), un.px(_PAD_U)
        w, h = size.width(), size.height()
        low_x, high_x, low_y, high_y = edge, width - edge, edge, height - edge
        a = anchor
        if side == RIGHT:
            x = _span(a.right() + 1 + gap, a.left() - gap - w, w, low_x, high_x, True)
            y = _span(a.top() - pad, a.bottom() + 1 + pad - h, h, low_y, high_y, True)
        elif side in (BELOW, ABOVE):
            x = _span(a.left(), a.right() + 1 - w, w, low_x, high_x, True)
            below, above = a.bottom() + 1 + gap, a.top() - gap - h
            y = _span(below, above, h, low_y, high_y, side == BELOW)
        else:
            x = _span(a.left(), a.left() - w, w, low_x, high_x, True)
            y = _span(a.top(), a.top() - h, h, low_y, high_y, True)
        return QRect(x, y, w, h)

    def _rows(self, level: _Level, un: Units) -> list[tuple[int, QRectF]]:
        """The entries in sight (by their index) and their rows."""
        pad = un.px(_PAD_U)
        panel = level.panel
        bottom = panel.top() + panel.height() - pad
        y = float(panel.top() + pad)
        rows: list[tuple[int, QRectF]] = []
        for index in range(level.top, len(level.entries)):
            height = self._height(level.entries[index], un)
            if rows and y + height > bottom + 0.5:
                break
            rows.append((index, QRectF(panel.left() + pad, y, panel.width() - 2 * pad, height)))
            y += height
        return rows

    def _last_top(self, level: _Level, un: Units) -> int:
        """The first entry in sight once scrolled to the end."""
        room = level.panel.height() - 2 * un.px(_PAD_U)
        top, used = len(level.entries), 0.0
        while top > 0 and used + self._height(level.entries[top - 1], un) <= room + 0.5:
            top -= 1
            used += self._height(level.entries[top], un)
        return min(top, len(level.entries) - 1)

    def _reveal(self, level: _Level) -> None:
        """The highlighted entry in sight."""
        if level.current < 0:
            return
        un = self._units()
        if level.current < level.top:
            level.top = level.current
        last = self._last_top(level, un)
        while level.top < last and level.current not in dict(self._rows(level, un)):
            level.top += 1

    def _scroll(self, depth: int, rows: int) -> None:
        level = self._levels[depth]
        top = max(0, min(level.top + rows, self._last_top(level, self._units())))
        if top != level.top:
            level.top = top
            self.update()

    # ---- moving and choosing ------------------------------------------------------------

    def _first(self, level: _Level) -> None:
        level.current = next((i for i, e in enumerate(level.entries) if e.choosable()), -1)
        self._reveal(level)

    def _focus(self, depth: int, index: int) -> None:
        """Highlight entry `index` of level `depth`; the levels past it close."""
        level = self._levels[depth]
        deeper = len(self._levels) > depth + 1
        if index == level.current and not deeper:
            return
        del self._levels[depth + 1 :]
        level.current = index
        self._reveal(level)
        if deeper:
            self._levels_changed()
        else:
            self._moved()

    def _move(self, step: int) -> None:
        level = self._levels[-1]
        choosable = [i for i, e in enumerate(level.entries) if e.choosable()]
        if not choosable:
            return
        if level.current in choosable:
            index = choosable[(choosable.index(level.current) + step) % len(choosable)]
        else:
            index = choosable[0] if step > 0 else choosable[-1]
        self._focus(len(self._levels) - 1, index)

    def _to_end(self, last: bool) -> None:
        choosable = [i for i, e in enumerate(self._levels[-1].entries) if e.choosable()]
        if choosable:
            self._focus(len(self._levels) - 1, choosable[-1] if last else choosable[0])

    def _jump(self, text: str) -> None:
        """A letter: the next entry it starts (round)."""
        letter = _first_letter(text)
        if not letter:
            return
        level = self._levels[-1]
        count = len(level.entries)
        for step in range(1, count + 1):
            index = (level.current + step) % count
            entry = level.entries[index]
            if entry.choosable() and _first_letter(entry.text) == letter:
                self._focus(len(self._levels) - 1, index)
                return

    def _choose(self, depth: int, index: int) -> None:
        """Entry `index` of level `depth`: its submenu opens, a tick that keeps the menu open
        turns, any other entry closes the menu, then acts."""
        entry = self._levels[depth].entries[index]
        if not entry.choosable():
            return
        self._focus(depth, index)
        if entry.submenu is not None:
            self._open_submenu(depth)
        elif entry.keep_open:
            entry.trigger()
            self.update()
            self._say()
        else:
            self.close_menu()
            entry.trigger()

    def _open_submenu(self, depth: int) -> None:
        level = self._levels[depth]
        entry = level.entries[level.current]
        if entry.submenu is None or len(self._levels) > depth + 1:
            return
        row = dict(self._rows(level, self._units())).get(level.current)
        if row is None:
            return
        anchor = QRect(
            level.panel.left(), round(row.top()), level.panel.width(), round(row.height())
        )
        sub = self._level(entry.submenu, anchor, RIGHT)
        if sub is not None:
            self._levels.append(sub)
            self._first(sub)
            self._levels_changed()

    def _levels_changed(self) -> None:
        """A submenu opened or closed: the page's mask follows (OverlayHost), then the rest."""
        host = self.parentWidget()
        if host is not None and hasattr(host, "overlay_changed"):
            host.overlay_changed()
        self._moved()

    def _moved(self) -> None:
        self.update()
        self._say()

    def _finish(self) -> None:
        menu = self._levels[0].menu
        self._levels = []
        self._pressed = None
        self._stop_filtering()
        opener, self._opener = self._opener, None
        if opener is not None and isValid(opener):
            opener.removeEventFilter(self)
        if not self.isHidden():
            self.hide()
        announce(self, translate(MENU_NAME), speak=False)  # what it is again, said by nothing
        menu.closed()

    # ---- what assistive technology hears ------------------------------------------------

    def _say(self, opening: bool = False) -> None:
        """The entry highlighted (a menu with none: its first), after what it is on opening."""
        entry = self.current()
        if entry is None and self._levels:
            entry = next((e for e in self._levels[-1].entries if not e.separator), None)
        text = "" if entry is None else (TICK_SAID if entry.checked else "") + entry.text
        if opening:
            text = ", ".join(part for part in (translate(MENU_NAME), text) if part)
        if text:
            announce(self, text)

    # ---- painting -----------------------------------------------------------------------

    @staticmethod
    def _elided(text: str, fm: QFontMetricsF, width: float) -> str:
        if fm.horizontalAdvance(text) <= width:
            return text
        return fm.elidedText(text, Qt.TextElideMode.ElideRight, max(0.0, width))

    def paintEvent(self, event: QPaintEvent) -> None:
        if not self._levels:
            return
        un = self._units()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        deepest = len(self._levels) - 1
        for depth, level in enumerate(self._levels):
            self._paint_level(p, level, un, focused=depth == deepest)
        p.end()

    def _paint_level(self, p: QPainter, level: _Level, un: Units, focused: bool) -> None:
        radius = un.u * _RADIUS_U
        p.setPen(QPen(qcolor(PALETTE.text, 0.12), 1.0))
        p.setBrush(qcolor(PALETTE.panel_raised, 0.97))
        p.drawRoundedRect(QRectF(level.panel).adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
        font = self._font(un)
        fm = QFontMetricsF(font)
        left, right = self._margins(level.entries, un, level.scrolls)
        side = float(un.px(_SIDE_U))
        rows = self._rows(level, un)
        for index, rect in rows:
            entry = level.entries[index]
            if entry.separator:
                y = math.floor(rect.center().y())
                rule = QRectF(rect.left() + side, y, rect.width() - 2 * side, 1)
                p.fillRect(rule, qcolor(PALETTE.text, 0.12))
                continue
            if index == level.current:
                paint_highlight(p, rect, un, focused)
            color = qcolor(PALETTE.text, 1.0 if entry.enabled else _DISABLED_ALPHA)
            if entry.checked:
                size = float(un.px(_TICK_U))
                box = QRectF(rect.left() + side, rect.center().y() - size / 2, size, size)
                draw_icon(p, "check", box, qcolor(PALETTE.accent) if entry.enabled else color)
            if entry.submenu is not None:
                size = float(un.px(_ARROW_U))
                x = rect.right() - side - size
                draw_icon(
                    p, "chevron_right", QRectF(x, rect.center().y() - size / 2, size, size), color
                )
            text = QRectF(
                rect.left() + left, rect.top(), rect.width() - left - right, rect.height()
            )
            p.setFont(font)
            p.setPen(color)
            p.drawText(
                text,
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                self._elided(entry.text, fm, text.width()),
            )
        if level.scrolls and rows:
            self._paint_arrows(p, level, un, rows)

    def _paint_arrows(
        self, p: QPainter, level: _Level, un: Units, rows: list[tuple[int, QRectF]]
    ) -> None:
        """A small ▴ on the first row in sight when entries lie above it, ▾ on the last for
        below (as the Sound panel's and the ⋯ menu's)."""
        size = float(un.px(_SCROLL_U))
        half = size / 2
        above, below = level.top > 0, rows[-1][0] < len(level.entries) - 1
        for rect, beyond, up in ((rows[0][1], above, True), (rows[-1][1], below, False)):
            if not beyond:
                continue
            cx, cy = rect.right() - un.px(_SIDE_U) - half, rect.center().y()
            tip, base = (
                (cy - half * 0.6, cy + half * 0.6) if up else (cy + half * 0.6, cy - half * 0.6)
            )
            path = QPainterPath(QPointF(cx - half, base))
            path.lineTo(cx + half, base)
            path.lineTo(cx, tip)
            path.closeSubpath()
            p.save()
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qcolor(PALETTE.text, 0.6))
            p.drawPath(path)
            p.restore()

    # ---- input --------------------------------------------------------------------------

    def _hit(self, pos: QPointF) -> Optional[tuple[int, int]]:
        """(level, entry) under `pos`; (level, -1) on a panel but no entry; None off them."""
        un = self._units()
        for depth in range(len(self._levels) - 1, -1, -1):
            level = self._levels[depth]
            if not QRectF(level.panel).contains(pos):
                continue
            for index, rect in self._rows(level, un):
                if rect.contains(pos):
                    return depth, index
            return depth, -1
        return None

    def _just_opened(self) -> bool:
        return self._shown.isValid() and self._shown.elapsed() < QApplication.doubleClickInterval()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        event.accept()  # never the page's under it
        if not self._levels:
            return
        if event.button() == Qt.MouseButton.BackButton:
            self.back()
            return
        hit = self._hit(event.position())
        if hit is None:
            if event.button() != Qt.MouseButton.ForwardButton:
                self.close_menu()  # beside the menu: it closes, and nothing more
            return
        self._pressed = hit if hit[1] >= 0 else None

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        event.accept()
        if self._hit(event.position()) is None and self._just_opened():
            return  # the second press of the double-click that opened it
        self.mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        event.accept()
        pressed, self._pressed = self._pressed, None
        if not self._levels or event.button() not in _CLICKS:
            return
        hit = self._hit(event.position())
        if hit is not None and hit == pressed:  # else the press that opened it: no choice
            self._choose(*hit)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        """The entry under the pointer is highlighted; the move goes on to its page (the
        pointer stays shown while it moves: ui.pointer)."""
        event.ignore()
        hit = self._hit(event.position())
        if hit is None or hit[1] < 0:
            return
        depth, index = hit
        level = self._levels[depth]
        if not level.entries[index].choosable():
            return
        if index == level.current and len(self._levels) > depth + 1:
            return  # its submenu stays open
        self._focus(depth, index)

    def wheelEvent(self, event: QWheelEvent) -> None:
        """A menu's entries scroll, when not all fit; never the page's (the volume) under it."""
        event.accept()
        hit = self._hit(event.position())
        if hit is None:
            return
        self._wheel += event.angleDelta().y()
        rows = int(self._wheel / _WHEEL_STEP)  # towards zero: a notch up shows the rows above
        if rows:
            self._wheel -= rows * _WHEEL_STEP
            self._scroll(hit[0], -rows)

    def tooltip_at(self, pos: QPointF) -> str:
        """An entry cut short ("…") names itself whole."""
        hit = self._hit(pos)
        if hit is None or hit[1] < 0:
            return ""
        entry = self._levels[hit[0]].entries[hit[1]]
        return entry.text if self.cut(entry) != entry.text else ""

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        if self._levels:  # hidden with its page (or the overlay host's show_only)
            self._finish()

    def closeEvent(self, event) -> None:
        self._stop_filtering()
        super().closeEvent(event)

    def _start_filtering(self) -> None:
        app = QApplication.instance()
        if app is not None and not self._filtering:
            app.installEventFilter(self)  # a click elsewhere in the window closes it
            self._filtering = True

    def _stop_filtering(self) -> None:
        app = QApplication.instance()
        if app is not None and self._filtering:
            app.removeEventFilter(self)
        self._filtering = False

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if not self._levels:
            return False
        kind = event.type()
        parent = self.parentWidget()
        if obj is parent and kind == QEvent.Type.Hide:
            self.close_menu()  # its page went
        elif obj is parent and kind == QEvent.Type.Resize and parent.size() != self._page_size:
            # Its page changed size: asked again, it opens where it is. (A hidden page's resize
            # comes as the page shows: the size the menu was laid out against keeps it.)
            self.close_menu()
        elif obj is self._opener and kind == QEvent.Type.Hide:
            self.close_menu()  # the list that opened it went
        elif kind == QEvent.Type.Show and self._over_it(obj):
            self.close_menu()  # what has the keys came over it (it shows on)
        elif kind in _PRESSES and isinstance(event, QMouseEvent) and self._outside(obj):
            if kind == QEvent.Type.MouseButtonDblClick and self._just_opened():
                return True  # the second press of the double-click that opened it
            if event.button() == Qt.MouseButton.BackButton:
                self.back()
            elif event.button() != Qt.MouseButton.ForwardButton:
                self.close_menu()
            return True  # it closed the menu (or a level), and does nothing more
        return False

    def _over_it(self, obj: QObject) -> bool:
        """`obj` takes the keys while it shows (Overlay.takes_keys) and is in this window:
        showing, it comes over the menu."""
        return (
            obj is not self
            and isinstance(obj, QWidget)
            and getattr(obj, "takes_keys", False) is True
            and obj.window() is self.window()
        )

    def _outside(self, obj: QObject) -> bool:
        """A press on another widget of this window (its pages, the overlays, the video): not
        on the menu, nor in a dialog or another window."""
        if not isinstance(obj, QWidget) or obj is self or self.isAncestorOf(obj):
            return False
        host = self.window()
        ours = {host}
        outer = host.parentWidget()
        if outer is not None:
            ours.add(outer.window())
        top = obj.window()
        owner = top.parentWidget()
        return top in ours or (owner is not None and owner.window() in ours)


def popup_for(near: Optional[QObject]) -> Optional[PopupMenu]:
    """The menu overlay of the page `near` is on: its own, or that of the nearest widget it is
    in (the player's overlay host has one, the Library's page another); else one made for its
    window. None when `near` is no widget."""
    if not isinstance(near, QWidget):
        return None
    widget: Optional[QWidget] = near
    while widget is not None:
        for child in widget.children():
            if isinstance(child, PopupMenu):
                return child
        widget = widget.parentWidget()
    return PopupMenu(near.window())


def popup(
    menu: Menu,
    near: Optional[QObject],
    *,
    at: Optional[Where] = None,
    beside: Optional[QRect] = None,
    side: str = RIGHT,
    follow: bool = True,
) -> Optional[PopupMenu]:
    """Show `menu` on the page of `near` (the widget that asks), at `at` (Where: a
    right-click's point, or the row the menu key was pressed on) or beside the global rect
    `beside` on `side` (a button's). With `follow` it closes when `near` hides (a list; not the player controls,
    which fade). Returns the overlay that shows it (None: `near` is no widget, and the menu
    closes at once)."""
    overlay = popup_for(near)
    if overlay is None:
        logger.debug("A menu was asked for with no page to show it on")
        menu.closed()
        return None
    opener = near if follow and isinstance(near, QWidget) else None
    return overlay.open(menu, at=at, beside=beside, side=side, opener=opener)
