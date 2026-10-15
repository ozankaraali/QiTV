"""The Library's wrapping poster grid: inline detail strip, A–Z rail and sort chips (spec §5.4).

Everything is painted: only the visible rows are drawn. Once the view has settled, only they
(plus one row ahead) ask the poster cache for images. 1 u = 1% of the parent (Library screen)
width.
"""

from bisect import bisect_right
from collections.abc import Callable
import logging
import time
from typing import NamedTuple
import zlib

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QLinearGradient,
    QPainter,
    QPen,
    QPixmap,
    QTransform,
)
from PySide6.QtWidgets import QAbstractScrollArea, QApplication, QFrame, QWidget

from services.library import Details, letter_of, title_of
from services.text_fold import fold
from ui.overlays.painting import Units, draw_icon, draw_progress, elided, fill_round, fmt_duration
from ui.theme import PALETTE, RADIUS_CARD, qcolor

logger = logging.getLogger(__name__)

DEFAULT_HOST_WIDTH = 1280
HEADER_H_U = 5.0
HEADER_TITLE_U = 1.7
HEADER_COUNT_U = 1.0
CHIP_FONT_U = 1.0
SEPARATOR = " · "  # between sort chips and between meta parts
PAD_U = 2.2  # side margin of the grid content; the rail sits in the right one
TOP_PAD_U = 0.6  # room above the first row for the focus lift
BOTTOM_PAD_U = 1.2
GAP_U = 1.2
POSTER_TARGET_U = 9.5
POSTER_ASPECT = 1.5
MIN_COLUMNS = 3
SECTION_H_U = 3.0
SECTION_FONT_U = 1.3
CARD_TITLE_U = 1.1
CARD_MARGIN_U = 0.8
CARD_SATURATION = 0.45
CARD_VALUE_TOP = 0.42
CARD_VALUE_BOTTOM = 0.18
CARD_BAR_CLEARANCE_U = 0.6  # the card title sits this far above a progress bar
CARD_TITLE_LINES = 4
PROGRESS_H_U = 0.45
PROGRESS_INSET_U = 0.8
PROGRESS_TRACK_ALPHA = 0.25
LIFT_U = 0.3  # the focused poster is drawn this much larger
FOCUS_RING_PX = 1.5
UNFOCUSED_RING_ALPHA = 0.35  # the current poster's ring while keys move another zone
TILE_LOGO_W = 0.7  # a channel logo fits in this share of its tile's width…
TILE_LOGO_H = 0.4  # …and of its height
STRIP_H_U = 11.0
STRIP_PAD_U = 2.0
STRIP_TINT_ALPHA = 0.2
STRIP_TITLE_U = 2.2
STRIP_META_U = 1.1
STRIP_META_ALPHA = 0.6
STRIP_PLOT_U = 1.15
PLOT_LINES = 2
BUTTON_H_U = 4.0
BUTTON_RADIUS_U = 0.8
BUTTON_FONT_U = 1.1
BUTTON_PAD_U = 1.6
BUTTON_GAP_U = 0.8
BUTTON_RAISED_ALPHA = 0.12
BUTTON_ICON_U = 1.1
STAR_ICON_U = 1.8
RAIL_W_U = 1.6
RAIL_FONT_U = 0.85
RAIL_MIN_ITEMS = 60
RAIL_LETTERS = "#ABCDEFGHIJKLMNOPQRSTUVWXYZ"
MESSAGE_FONT_U = 1.35
MESSAGE_ALPHA = 0.7
HOVER_DWELL_MS = 250
WRAPPED_KEPT = 2048  # card titles' wrapped lines kept (a few screens' worth)
REQUEST_DELAY_MS = 120  # poster requests wait until the view settles

PLAY, START_OVER, FAVORITE = "play", "start_over", "favorite"
_ACCENT, _RAISED, _SQUARE = "accent", "raised", "square"


def card_hue(title: str) -> float:
    """A stable hue (0–1) for a generated title card: CRC-32 of the folded title.

    Python's hash() is salted per process, so it would change the colours on every launch.
    """
    return (zlib.crc32(fold(title).encode("utf-8")) % 360) / 360


def poster_url(item: dict) -> str:
    """The poster URL of a title: Xtream/M3U `logo`, STB `screenshot_uri`, else `cover`."""
    return str(item.get("logo") or item.get("screenshot_uri") or item.get("cover") or "")


def _wrap(fm: QFontMetricsF, text: str, width: float, max_lines: int) -> list[str]:
    """Greedy word wrap from the start into at most `max_lines` lines.

    Every line is elided to `width` (so is a word longer than the line), and the last allowed
    line takes the rest of the text, elided.
    """
    words = text.split()
    lines: list[str] = []
    line = ""
    start = 0  # the index of the first word on the current line
    for k, word in enumerate(words):
        candidate = f"{line} {word}" if line else word
        if line and fm.horizontalAdvance(candidate) > width:
            if len(lines) + 1 >= max_lines:
                lines.append(fm.elidedText(" ".join(words[start:]), Qt.ElideRight, width))
                return lines
            lines.append(fm.elidedText(line, Qt.ElideRight, width))
            line, start = word, k
        else:
            line = candidate
    if line:
        lines.append(fm.elidedText(line, Qt.ElideRight, width))
    return lines


def _button_specs(details: Details | None) -> list[tuple[str, str, str]]:
    """(name, label, style) left to right, per the kind of the current title."""
    if details is None:
        return [(PLAY, "Play", _ACCENT)]
    if details.kind == "series":
        return [(FAVORITE, "", _SQUARE), (PLAY, "Episodes", _ACCENT)]
    if details.kind == "episode":
        if details.resume_ms:
            return [(START_OVER, "Start over", _RAISED), (PLAY, "Resume", _ACCENT)]
        return [(PLAY, "Play", _ACCENT)]
    if details.resume_ms:
        return [
            (START_OVER, "Start over", _RAISED),
            (FAVORITE, "", _SQUARE),
            (PLAY, "Resume", _ACCENT),
        ]
    return [(FAVORITE, "", _SQUARE), (PLAY, "Play", _ACCENT)]


class _Line(NamedTuple):
    """A laid-out line: a row of posters, or a section title (start == -1)."""

    top: float  # content y without the detail strip
    height: float
    start: int
    size: int
    title: str


class _Header(QWidget):
    """The fixed header above the viewport; the grid paints it and handles its clicks."""

    def __init__(self, grid: "PosterGrid") -> None:
        super().__init__(grid)
        self._grid = grid
        self.setFocusPolicy(Qt.NoFocus)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        self._grid._paint_header(p, QRectF(self.rect()))

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.LeftButton:
            return
        value = self._grid._chip_at(event.position())
        if value is not None:
            self._grid.sortRequested.emit(value)


class PosterGrid(QAbstractScrollArea):
    activated = Signal(int)  # Enter / double-click / ▶ button (flat index)
    startOverRequested = Signal(int)
    favoriteToggled = Signal(int)
    focusChanged = Signal(int)
    sortRequested = Signal(str)  # a Sort value
    leftEdge = Signal()  # ← in the first column
    upEdge = Signal()  # ↑ in the first row

    def __init__(self, posters, parent=None) -> None:
        super().__init__(parent)
        self._posters = posters
        self.setFrameShape(QFrame.NoFrame)
        self.setFocusPolicy(Qt.NoFocus)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        viewport = self.viewport()
        viewport.setFocusPolicy(Qt.NoFocus)
        viewport.setMouseTracking(True)
        viewport.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self._header = _Header(self)
        self._units = Units(DEFAULT_HOST_WIDTH)
        # header
        self._title = ""
        self._count_text = ""
        self._sorts: list[tuple[str, str]] = []
        self._active_sort = ""
        # content
        self._items: list[dict] = []
        self._sections: list[tuple[str, int, int]] = []  # (title, start, count), non-empty
        self._message: str | None = None
        self._letters: dict[str, int] = {}
        self._details_provider: Callable[[dict], Details | None] | None = None
        self._poster_url: Callable[[dict], str] = poster_url
        self._progress_provider: Callable[[dict], float | None] | None = None
        self._tile_provider: Callable[[dict], bool] | None = None
        self._focused = True
        self._details_cache: tuple[int, dict, Details | None] | None = None
        self._progress_cache: dict[int, float | None] = {}
        self._wrapped: dict[tuple[str, str, float], list[str]] = {}  # see _card_lines
        # layout (see _layout_rows)
        self._cols = MIN_COLUMNS
        self._pad = self._gap = self._pw = self._ph = self._strip_h = 0.0
        self._lines: list[_Line] = []
        self._line_tops: list[float] = []
        self._row_lines: list[int] = []  # item row -> line index
        self._sec_starts: list[int] = []
        self._sec_first_rows: list[int] = []
        self._base_height = 0.0
        self._cur = -1
        self._cur_line = -1
        # posters on demand: requested once the view has settled
        self._visible_urls: set[str] = set()
        self._request_timer = QTimer(self)
        self._request_timer.setSingleShot(True)
        self._request_timer.setInterval(REQUEST_DELAY_MS)
        self._request_timer.timeout.connect(self._request_posters)
        # mouse
        self._hover = -1
        self._hover_timer = QTimer(self)
        self._hover_timer.setSingleShot(True)
        self._hover_timer.setInterval(HOVER_DWELL_MS)
        self._hover_timer.timeout.connect(self._on_hover_dwell)
        self._pressed = -1
        self._press_activated = False
        # The last mouse activation: (target, time.monotonic()), target ("poster", i) or a button.
        self._last_activation: tuple[object, float] = (None, float("-inf"))
        if posters is not None:
            posters.logoReady.connect(self._on_poster_ready)
        if parent is not None:
            parent.installEventFilter(self)
        self._relayout()

    # ---- public API -------------------------------------------------------------------------

    def set_header(
        self, title: str, count_text: str, sorts: list[tuple[str, str]], active: str
    ) -> None:
        self._title = title
        self._count_text = count_text
        self._sorts = list(sorts)
        self._active_sort = active
        self._header.update()

    def set_sections(
        self, sections: list[tuple[str, list[dict]]], *, keep_current: bool = False
    ) -> None:
        """Show titles: [("", items)] in normal mode, titled sections in search mode.

        By default the first title becomes current and the view returns to the top (a new
        genre, sort or search). With `keep_current` (a refresh of the same genre and sort,
        such as after ★), the current title stays current when it is still listed (found by
        identity) and the view keeps its place; otherwise the first title becomes current.
        """
        old_index = self._cur
        old_item = self.item(old_index)
        items: list[dict] = []
        kept: list[tuple[str, int, int]] = []
        for title, section_items in sections:
            if section_items:
                kept.append((title, len(items), len(section_items)))
                items.extend(section_items)
        self._items = items
        self._sections = kept
        self._details_cache = None
        self._progress_cache.clear()
        self._cancel_hover()
        current = -1
        if items:
            current = 0
            if keep_current and old_item is not None:
                if old_index < len(items) and items[old_index] is old_item:
                    current = old_index
                else:
                    current = next((k for k, it in enumerate(items) if it is old_item), 0)
        self._cur = current
        self._layout_rows()
        same = current >= 0 and items[current] is old_item
        if not (keep_current and same):
            self.verticalScrollBar().setValue(0)
        elif current != old_index:
            self._reveal()
        self.viewport().update()
        if current != old_index or (current >= 0 and not same):
            self.focusChanged.emit(current)

    def set_details_provider(self, fn: Callable[[dict], Details | None]) -> None:
        self._details_provider = fn
        self._details_cache = None
        self.viewport().update()

    def set_poster_url_provider(self, fn: Callable[[dict], str]) -> None:
        """How a title's poster URL is found; `poster_url` by default."""
        self._poster_url = fn
        self.viewport().update()

    def set_progress_provider(self, fn: Callable[[dict], float | None]) -> None:
        self._progress_provider = fn
        self._progress_cache.clear()
        self.viewport().update()

    def set_tile_provider(self, fn: Callable[[dict], bool] | None) -> None:
        """Titles for which `fn` is True are channel logos: their image is drawn centred on a
        raised tile instead of covering the poster."""
        self._tile_provider = fn
        self.viewport().update()

    def set_focused(self, focused: bool) -> None:
        """Whether keys move this grid. The current poster is lifted with the accent ring;
        otherwise it keeps its place with a dimmed ring."""
        if focused != self._focused:
            self._focused = focused
            self.viewport().update()

    def set_letter_index(self, index: dict[str, int] | None) -> None:
        """Letter ('#', 'a'…'z') -> first flat index; None or {} hides the A–Z rail."""
        self._letters = dict(index) if index else {}
        self.viewport().update()

    def set_message(self, text: str | None) -> None:
        """Paint `text` ("Loading…", an error, "Nothing here") instead of the grid."""
        self._message = text
        self._cancel_hover()
        self.viewport().update()

    def columns(self) -> int:
        return self._cols

    def current_index(self) -> int:
        return self._cur

    def item(self, i: int) -> dict | None:
        return self._items[i] if 0 <= i < len(self._items) else None

    def set_current_index(self, i: int, *, reveal: bool = True) -> None:
        """Make title `i` current. The current row keeps its place on screen while the strip
        moves under it; with `reveal`, the view then scrolls to show the row and its strip."""
        if not self._items:
            return
        i = max(0, min(len(self._items) - 1, i))
        if i == self._cur:
            if reveal:
                self._reveal()
            return
        line = self._line_of(i)
        before = self._line_top(line)
        self._cur = i
        self._cur_line = line
        self._update_scroll_range()
        bar = self.verticalScrollBar()
        bar.setValue(round(bar.value() + self._line_top(line) - before))
        if reveal:
            self._reveal()
        self.viewport().update()
        self.focusChanged.emit(i)

    def move_by(self, dx: int, dy: int) -> None:
        """Move one column (dx = ±1) or one row (dy = ±1); rows flow across sections."""
        if self._cur < 0:
            return
        if dx:
            col = self._col_of(self._cur)
            if dx < 0:
                if col == 0:
                    self.leftEdge.emit()
                    return
                self.set_current_index(self._cur - 1)
            else:
                start, count = self._row_span(self._row_of(self._cur))
                if self._cur + 1 < start + count:
                    self.set_current_index(self._cur + 1)
            return
        if dy:
            row = self._row_of(self._cur)
            if dy < 0 and row == 0:
                self.upEdge.emit()
                return
            target = row + (1 if dy > 0 else -1)
            if target < len(self._row_lines):
                self.set_current_index(self._index_in_row(target, self._col_of(self._cur)))

    def page(self, delta: int) -> None:
        """Move by the number of rows that fit in the viewport next to the strip."""
        if self._cur < 0 or not delta:
            return
        step = self._ph + self._gap
        rows = max(1, int((self.viewport().height() - self._strip_h) // step)) if step > 0 else 1
        row = self._row_of(self._cur)
        target = max(0, min(len(self._row_lines) - 1, row + delta * rows))
        if target != row:
            self.set_current_index(self._index_in_row(target, self._col_of(self._cur)))

    def home(self) -> None:
        self.set_current_index(0)

    def end(self) -> None:
        self.set_current_index(len(self._items) - 1)

    def activate(self) -> None:
        if self._cur >= 0:
            self.activated.emit(self._cur)

    def jump_to_letter(self, letter: str) -> None:
        """Make the first title of `letter` current, when the letter index has it."""
        index = self._letters.get(letter_of(letter))
        if index is not None:
            self.set_current_index(index)

    def index_at(self, pos: QPoint) -> int:
        """The flat index of the poster at `pos` (viewport coordinates), else -1."""
        if self._message is not None or not self._items:
            return -1
        if self._rail_visible() and pos.x() >= self._rail_rect().left():
            return -1
        line_index = self._line_at(pos.y() + self.verticalScrollBar().value())
        if line_index < 0:
            return -1
        line = self._lines[line_index]
        if line.start < 0:
            return -1
        x = pos.x() - self._pad
        step = self._pw + self._gap
        col = int(x // step) if x >= 0 else -1
        if col < 0 or col >= line.size or x - col * step >= self._pw:
            return -1
        return line.start + col

    def button_at(self, pos: QPoint) -> str | None:
        """The strip button (PLAY, START_OVER or FAVORITE) at `pos` (viewport coordinates)."""
        if self._message is not None or self._cur < 0:
            return None
        point = QPointF(pos)
        for name, _label, _style, rect in self._strip_buttons(QRectF(self.strip_rect())):
            if rect.contains(point):
                return name
        return None

    def button_labels(self) -> list[tuple[str, str]]:
        """The current title's strip buttons left to right, as (name, label)."""
        return [(name, label) for name, label, _style in _button_specs(self._details())]

    def strip_rect(self) -> QRect:
        """The detail strip in viewport coordinates; null when nothing is current."""
        if self._cur < 0:
            return QRect()
        top = self._strip_top() - self.verticalScrollBar().value()
        width = self.viewport().width() - 2 * self._pad
        return QRect(round(self._pad), round(top), round(width), round(self._strip_h))

    def item_rect(self, i: int) -> QRect:
        """Title `i`'s poster in viewport coordinates (without the focus lift)."""
        if not 0 <= i < len(self._items):
            return QRect()
        rect = self._poster_rect(i).translated(0, -self.verticalScrollBar().value())
        return rect.toRect()

    def chip_rect(self, value: str) -> QRect:
        """The sort chip for `value` in grid coordinates; null when there is none."""
        for chip_value, _label, rect in self._chip_rects():
            if chip_value == value:
                return rect.toRect()
        return QRect()

    # ---- geometry ---------------------------------------------------------------------------

    def _host_width(self) -> int:
        parent = self.parentWidget()
        if parent is not None and parent.width() > 0:
            return parent.width()
        return self.width() if self.width() > 0 else DEFAULT_HOST_WIDTH

    def eventFilter(self, obj, event) -> bool:
        if obj is self.parentWidget() and event.type() == QEvent.Resize:
            self._relayout()
        return False

    def event(self, event) -> bool:
        if event.type() == QEvent.ParentAboutToChange and self.parentWidget() is not None:
            self.parentWidget().removeEventFilter(self)
        elif event.type() == QEvent.ParentChange and self.parentWidget() is not None:
            self.parentWidget().installEventFilter(self)
            self._relayout()
        return super().event(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._relayout()

    def _relayout(self) -> None:
        self._units = Units(self._host_width())
        header_h = self._units.px(HEADER_H_U)
        self.setViewportMargins(0, header_h, 0, 0)
        self._header.setGeometry(0, 0, self.width(), header_h)
        self._layout_rows()
        if self._cur >= 0:
            self._reveal()
        self._header.update()
        self.viewport().update()

    def _layout_rows(self) -> None:
        u = self._units.u
        self._gap = gap = GAP_U * u
        self._pad = pad = PAD_U * u
        self._strip_h = STRIP_H_U * u
        content_w = max(1.0, self.viewport().width() - 2 * pad)
        self._cols = cols = max(MIN_COLUMNS, int((content_w + gap) // (POSTER_TARGET_U * u + gap)))
        self._pw = max(1.0, (content_w - (cols - 1) * gap) / cols)
        self._ph = self._pw * POSTER_ASPECT
        lines: list[_Line] = []
        row_lines: list[int] = []
        sec_starts: list[int] = []
        sec_first_rows: list[int] = []
        y = TOP_PAD_U * u
        title_h = SECTION_H_U * u
        for title, start, count in self._sections:
            if title:
                lines.append(_Line(y, title_h, -1, 0, title))
                y += title_h
            sec_starts.append(start)
            sec_first_rows.append(len(row_lines))
            end = start + count
            for first in range(start, end, cols):
                row_lines.append(len(lines))
                lines.append(_Line(y, self._ph, first, min(cols, end - first), ""))
                y += self._ph + gap
        self._lines = lines
        self._line_tops = [line.top for line in lines]
        self._row_lines = row_lines
        self._sec_starts = sec_starts
        self._sec_first_rows = sec_first_rows
        self._base_height = (y - gap if row_lines else y) + BOTTOM_PAD_U * u
        self._cur_line = self._line_of(self._cur) if self._cur >= 0 else -1
        self._update_scroll_range()

    def _update_scroll_range(self) -> None:
        height = self._base_height + (self._strip_extra() if self._cur >= 0 else 0.0)
        view_h = self.viewport().height()
        bar = self.verticalScrollBar()
        bar.setRange(0, max(0, round(height - view_h)))
        bar.setPageStep(max(1, view_h))
        bar.setSingleStep(max(1, round((self._ph + self._gap) / 3)))

    def _strip_extra(self) -> float:
        """The height the strip adds below the current row (the strip and its lower gap)."""
        return self._strip_h + self._gap

    def _section_of(self, i: int) -> int:
        return bisect_right(self._sec_starts, i) - 1

    def _row_of(self, i: int) -> int:
        s = self._section_of(i)
        return self._sec_first_rows[s] + (i - self._sec_starts[s]) // self._cols

    def _col_of(self, i: int) -> int:
        return (i - self._sec_starts[self._section_of(i)]) % self._cols

    def _line_of(self, i: int) -> int:
        return self._row_lines[self._row_of(i)]

    def _row_span(self, row: int) -> tuple[int, int]:
        line = self._lines[self._row_lines[row]]
        return line.start, line.size

    def _index_in_row(self, row: int, col: int) -> int:
        start, count = self._row_span(row)
        return start + min(col, count - 1)

    def _line_top(self, k: int) -> float:
        top = self._line_tops[k]
        return top + self._strip_extra() if 0 <= self._cur_line < k else top

    def _strip_top(self) -> float:
        return self._line_tops[self._cur_line] + self._ph + self._gap

    def _line_at(self, y: float) -> int:
        """The line at content y, else -1 (the strip, a gap or past the end)."""
        if not self._lines:
            return -1
        base = y
        if self._cur_line >= 0:
            strip_top = self._strip_top()
            if strip_top <= y < strip_top + self._strip_extra():
                return -1
            if y >= strip_top:
                base = y - self._strip_extra()
        k = bisect_right(self._line_tops, base) - 1
        if k < 0 or base >= self._line_tops[k] + self._lines[k].height:
            return -1
        return k

    def _poster_rect(self, i: int) -> QRectF:
        top = self._line_top(self._line_of(i))
        x = self._pad + self._col_of(i) * (self._pw + self._gap)
        return QRectF(x, top, self._pw, self._ph)

    def _reveal(self) -> None:
        """Scroll so the current row (with its section title) and its strip are visible."""
        if self._cur < 0:
            return
        line = self._cur_line
        top = self._line_top(line) - TOP_PAD_U * self._units.u
        if line > 0 and self._lines[line - 1].start < 0:
            top = self._line_top(line - 1)
        if self._row_of(self._cur) == 0:
            top = 0.0
        bottom = self._strip_top() + self._strip_extra()
        bar = self.verticalScrollBar()
        view_h = self.viewport().height()
        value = float(bar.value())
        if top < value or bottom - top > view_h:
            value = top
        elif bottom > value + view_h:
            value = bottom - view_h
        bar.setValue(round(value))

    def _rail_visible(self) -> bool:
        return bool(self._letters) and len(self._items) >= RAIL_MIN_ITEMS

    def _rail_rect(self) -> QRectF:
        width = RAIL_W_U * self._units.u
        view = self.viewport()
        return QRectF(view.width() - width, 0, width, view.height())

    def _rail_letter_at(self, pos: QPoint) -> str | None:
        if not self._rail_visible() or self._message is not None:
            return None
        rect = self._rail_rect()
        if not rect.contains(QPointF(pos)):
            return None
        slot = rect.height() / len(RAIL_LETTERS)
        k = min(len(RAIL_LETTERS) - 1, max(0, int(pos.y() // slot))) if slot > 0 else 0
        return RAIL_LETTERS[k]

    # ---- data -------------------------------------------------------------------------------

    def _details(self) -> Details | None:
        item = self.item(self._cur)
        if item is None:
            return None
        cached = self._details_cache
        if cached is not None and cached[0] == self._cur and cached[1] is item:
            return cached[2]
        result: Details | None = None
        if self._details_provider is not None:
            try:
                result = self._details_provider(item)
            except Exception:
                logger.exception("details_provider failed")
        self._details_cache = (self._cur, item, result)
        return result

    def _progress(self, i: int) -> float | None:
        if self._progress_provider is None:
            return None
        if i not in self._progress_cache:
            value: float | None = None
            try:
                value = self._progress_provider(self._items[i])
            except Exception:
                logger.exception("progress_provider failed")
            self._progress_cache[i] = value
        return self._progress_cache[i]

    def _request_posters(self) -> None:
        """Ask the cache for the missing posters of the visible rows and one item row ahead.

        Runs once the view has settled (paints and scrolls restart the timer) and replaces
        the cache's queue, so rows scrolled past never delay the ones on screen. A poster that
        is still missing is asked for again on a later pass; the cache bounds the retries.
        """
        if self._posters is None or self._message is not None or not self.isVisible():
            return
        first, last = self._visible_lines()
        if first < 0:
            return
        last += 1
        while last < len(self._lines) and self._lines[last].start < 0:
            last += 1
        wanted: dict[str, None] = {}  # ordered and de-duplicated
        for line in self._lines[first : last + 1]:
            for i in range(line.start, line.start + line.size):
                url = self._poster_url(self._items[i])
                if url and url not in wanted and self._posters.pixmap(url) is None:
                    wanted[url] = None
        if wanted:
            self._posters.request(list(wanted), replace_pending=True)

    def _on_poster_ready(self, url: str) -> None:
        if url in self._visible_urls:
            self.viewport().update()

    # ---- mouse ------------------------------------------------------------------------------

    def _cancel_hover(self) -> None:
        self._hover_timer.stop()
        self._hover = -1

    def _on_hover_dwell(self) -> None:
        if 0 <= self._hover < len(self._items) and self._message is None:
            self.set_current_index(self._hover, reveal=False)
        self._hover = -1

    def viewportEvent(self, event) -> bool:
        if event.type() == QEvent.Leave:
            self._cancel_hover()
        return super().viewportEvent(event)

    def mouseMoveEvent(self, event) -> None:
        i = self.index_at(event.position().toPoint())
        if i < 0 or i == self._cur:
            self._cancel_hover()
        elif i != self._hover or not self._hover_timer.isActive():
            self._hover = i
            self._hover_timer.start()

    def _mouse_activate(self, target: object, signal, index: int) -> None:
        """Emit an activation from the mouse, unless it repeats the previous one within the
        double-click interval: the third click of a triple-click must not activate again."""
        now = time.monotonic()
        last_target, last_time = self._last_activation
        if target == last_target and (now - last_time) * 1000 < QApplication.doubleClickInterval():
            return
        self._last_activation = (target, now)
        signal.emit(index)

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        pos = event.position().toPoint()
        self._cancel_hover()
        self._pressed = -1
        self._press_activated = False
        letter = self._rail_letter_at(pos)
        if letter is not None:
            self.jump_to_letter(letter)
            return
        button = self.button_at(pos)
        if button == FAVORITE:
            self.favoriteToggled.emit(self._cur)
            return
        if button is not None:
            signal = self.activated if button == PLAY else self.startOverRequested
            self._mouse_activate((button, self._cur), signal, self._cur)
            return
        i = self.index_at(pos)
        if i < 0:
            return
        self._pressed = i
        if i == self._cur:
            self._press_activated = True
            self._mouse_activate(("poster", i), self.activated, i)
        else:
            self.set_current_index(i)

    def mouseDoubleClickEvent(self, event) -> None:
        # The first press made the poster current (and may have scrolled), so the second
        # click activates that poster rather than whatever is now under the pointer.
        if event.button() != Qt.LeftButton:
            return
        if self._pressed >= 0 and self._pressed == self._cur and not self._press_activated:
            self._press_activated = True
            self._mouse_activate(("poster", self._cur), self.activated, self._cur)

    def wheelEvent(self, event) -> None:
        self._cancel_hover()
        super().wheelEvent(event)

    def scrollContentsBy(self, dx: int, dy: int) -> None:
        self.viewport().update()  # the rail does not scroll, so no blitting
        self._request_timer.start()

    # ---- painting ---------------------------------------------------------------------------

    def _chip_rects(self) -> list[tuple[str, str, QRectF]]:
        """Right-aligned sort chips; only the active one when the row does not fit."""
        fm = QFontMetricsF(self._units.font(CHIP_FONT_U))
        height = self._units.px(HEADER_H_U)
        sep_w = fm.horizontalAdvance(SEPARATOR)
        available = self.width() - 2 * self._pad

        def total(sorts: list[tuple[str, str]]) -> float:
            widths = sum(fm.horizontalAdvance(label) for _value, label in sorts)
            return widths + sep_w * max(0, len(sorts) - 1)

        sorts = self._sorts
        if total(sorts) > available:
            sorts = [(value, label) for value, label in sorts if value == self._active_sort]
        widths = [fm.horizontalAdvance(label) for _value, label in sorts]
        x = max(self._pad, self.width() - self._pad - total(sorts))
        out = []
        for (value, label), width in zip(sorts, widths):
            out.append((value, label, QRectF(x, 0, width, height)))
            x += width + sep_w
        return out

    def _chip_at(self, pos: QPointF) -> str | None:
        for value, _label, rect in self._chip_rects():
            if rect.contains(pos):
                return value
        return None

    def _paint_header(self, p: QPainter, rect: QRectF) -> None:
        units = self._units
        u = units.u
        p.fillRect(rect, qcolor(PALETTE.canvas))
        chips = self._chip_rects()
        title_font = units.font(HEADER_TITLE_U, QFont.Weight.Bold)
        baseline = rect.center().y() + QFontMetricsF(title_font).capHeight() / 2
        right = chips[0][2].left() - 2 * u if chips else rect.right() - self._pad
        x = self._pad
        if self._title:
            p.setFont(title_font)
            p.setPen(qcolor(PALETTE.text))
            title = elided(p, self._title, right - x)
            p.drawText(QPointF(x, baseline), title)
            x += p.fontMetrics().horizontalAdvance(title) + 0.8 * u
        if self._count_text and x < right:
            p.setFont(units.font(HEADER_COUNT_U))
            p.setPen(qcolor(PALETTE.text, 0.5))
            p.drawText(QPointF(x, baseline), elided(p, self._count_text, right - x))
        if not chips:
            return
        p.setFont(units.font(CHIP_FONT_U))
        muted = qcolor(PALETTE.text, 0.5)
        for k, (value, label, chip) in enumerate(chips):
            if k:
                p.setPen(muted)
                p.drawText(
                    QPointF(chip.left() - p.fontMetrics().horizontalAdvance(SEPARATOR), baseline),
                    SEPARATOR,
                )
            p.setPen(qcolor(PALETTE.text) if value == self._active_sort else muted)
            p.drawText(QPointF(chip.left(), baseline), label)

    def paintEvent(self, event) -> None:
        p = QPainter(self.viewport())
        view = QRectF(self.viewport().rect())
        p.fillRect(view, qcolor(PALETTE.canvas))
        if self._message is not None:
            self._visible_urls = set()
            self._paint_message(p, view)
            return
        if not self._items:
            self._visible_urls = set()
            return
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        scroll = self.verticalScrollBar().value()
        y0, y1 = float(scroll), float(scroll + view.height())
        visible: set[str] = set()
        focus: QRectF | None = None
        title_font = self._units.font(SECTION_FONT_U, QFont.Weight.Bold)
        card_font = self._units.font(CARD_TITLE_U, QFont.Weight.Bold)
        p.translate(0, -scroll)
        first, last = self._visible_lines()
        for k in range(first, last + 1) if first >= 0 else ():
            top = self._line_top(k)
            line = self._lines[k]
            if line.start < 0:
                self._paint_section_title(p, line.title, top, line.height, title_font)
                continue
            for col in range(line.size):
                i = line.start + col
                rect = QRectF(self._pad + col * (self._pw + self._gap), top, self._pw, self._ph)
                if i == self._cur:
                    focus = rect
                else:
                    self._paint_poster(p, i, rect, card_font, visible)
        if self._cur >= 0:
            strip_top = self._strip_top()
            if strip_top < y1 and strip_top + self._strip_h > y0:
                width = view.width() - 2 * self._pad
                self._paint_strip(p, QRectF(self._pad, strip_top, width, self._strip_h))
        if focus is not None:
            if self._focused:
                half = LIFT_U * self._units.u / 2
                lifted = focus.adjusted(-half, -half, half, half)
                self._paint_poster(p, self._cur, lifted, card_font, visible)
                self._paint_focus_ring(p, lifted, qcolor(PALETTE.accent))
            else:
                self._paint_poster(p, self._cur, focus, card_font, visible)
                self._paint_focus_ring(p, focus, qcolor(PALETTE.text, UNFOCUSED_RING_ALPHA))
        p.translate(0, scroll)
        if self._rail_visible():
            self._paint_rail(p)
        self._visible_urls = visible
        self._request_timer.start()  # request the visible posters once the view settles

    def _content_to_base(self, y: float) -> float:
        if self._cur_line < 0 or y < self._strip_top():
            return y
        return max(self._strip_top(), y - self._strip_extra())

    def _visible_lines(self) -> tuple[int, int]:
        """The first and last line that intersect the viewport, else (-1, -1)."""
        scroll = self.verticalScrollBar().value()
        y0, y1 = float(scroll), float(scroll + self.viewport().height())
        first = last = -1
        k = max(0, bisect_right(self._line_tops, self._content_to_base(y0)) - 1)
        while k < len(self._lines):
            top = self._line_top(k)
            if top >= y1:
                break
            if top + self._lines[k].height > y0:
                first = k if first < 0 else first
                last = k
            k += 1
        return first, last

    def _paint_message(self, p: QPainter, view: QRectF) -> None:
        u = self._units.u
        p.setFont(self._units.font(MESSAGE_FONT_U))
        p.setPen(qcolor(PALETTE.text, MESSAGE_ALPHA))
        p.drawText(
            view.adjusted(4 * u, 0, -4 * u, 0),
            Qt.AlignCenter | Qt.TextWordWrap,
            self._message or "",
        )

    def _paint_section_title(
        self, p: QPainter, title: str, top: float, height: float, font: QFont
    ) -> None:
        p.setFont(font)
        p.setPen(qcolor(PALETTE.text))
        width = self.viewport().width() - 2 * self._pad
        rect = QRectF(self._pad, top, width, height)
        p.drawText(rect, Qt.AlignLeft | Qt.AlignVCenter, elided(p, title, width))

    def _paint_poster(
        self, p: QPainter, i: int, rect: QRectF, card_font: QFont, visible: set[str]
    ) -> None:
        item = self._items[i]
        url = self._poster_url(item)
        pixmap: QPixmap | None = None
        if url:
            visible.add(url)
            if self._posters is not None:
                pixmap = self._posters.pixmap(url)
        progress = self._progress(i)
        if pixmap is not None and not pixmap.isNull():
            if self._tile_provider is not None and self._tile_provider(item):
                self._paint_tile(p, rect, pixmap)
            else:
                self._paint_cover(p, rect, pixmap)
        else:
            self._paint_card(p, rect, title_of(item), card_font, progress is not None)
        if progress is not None:
            u = self._units.u
            inset = PROGRESS_INSET_U * u
            bar_h = PROGRESS_H_U * u
            bar = QRectF(
                rect.left() + inset, rect.bottom() - inset - bar_h, rect.width() - 2 * inset, bar_h
            )
            draw_progress(p, bar, progress, track_alpha=PROGRESS_TRACK_ALPHA)

    def _paint_cover(self, p: QPainter, rect: QRectF, pixmap: QPixmap) -> None:
        """Scale to cover the rect (centre crop) with rounded corners."""
        scale = max(rect.width() / pixmap.width(), rect.height() / pixmap.height())
        dx = rect.x() + (rect.width() - pixmap.width() * scale) / 2
        dy = rect.y() + (rect.height() - pixmap.height() * scale) / 2
        brush = QBrush(pixmap)
        brush.setTransform(QTransform(scale, 0, 0, scale, dx, dy))
        p.save()
        p.setPen(Qt.NoPen)
        p.setBrush(brush)
        p.drawRoundedRect(rect, RADIUS_CARD, RADIUS_CARD)
        p.restore()

    def _paint_tile(self, p: QPainter, rect: QRectF, pixmap: QPixmap) -> None:
        """A channel logo centred on a raised tile, scaled at paint time."""
        fill_round(p, rect, qcolor(PALETTE.panel_raised), RADIUS_CARD)
        scale = min(
            rect.width() * TILE_LOGO_W / pixmap.width(),
            rect.height() * TILE_LOGO_H / pixmap.height(),
        )
        width, height = pixmap.width() * scale, pixmap.height() * scale
        target = QRectF(
            rect.center().x() - width / 2, rect.center().y() - height / 2, width, height
        )
        p.drawPixmap(target, pixmap, QRectF(pixmap.rect()))

    def _paint_card(
        self, p: QPainter, rect: QRectF, title: str, font: QFont, has_progress: bool
    ) -> None:
        """A generated title card: a vertical gradient in the title's hue, title bottom-left."""
        hue = card_hue(title)
        gradient = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        gradient.setColorAt(0.0, QColor.fromHsvF(hue, CARD_SATURATION, CARD_VALUE_TOP))
        gradient.setColorAt(1.0, QColor.fromHsvF(hue, CARD_SATURATION, CARD_VALUE_BOTTOM))
        p.save()
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(gradient))
        p.drawRoundedRect(rect, RADIUS_CARD, RADIUS_CARD)
        u = self._units.u
        margin = CARD_MARGIN_U * u
        bottom = margin + ((PROGRESS_H_U + CARD_BAR_CLEARANCE_U) * u if has_progress else 0.0)
        fm = QFontMetricsF(font)
        width = rect.width() - 2 * margin
        lines = self._card_lines(fm, font, title, width)
        line_h = fm.lineSpacing()
        y = rect.bottom() - bottom - line_h * len(lines)
        p.setFont(font)
        p.setPen(qcolor(PALETTE.text))
        for line in lines:
            p.drawText(
                QRectF(rect.left() + margin, y, width, line_h), Qt.AlignLeft | Qt.AlignVCenter, line
            )
            y += line_h
        p.restore()

    def _card_lines(self, fm: QFontMetricsF, font: QFont, title: str, width: float) -> list[str]:
        """A card title's wrapped lines; each title is measured once per font and width
        (scrolling paints the same cards frame after frame)."""
        key = (font.key(), title, width)
        lines = self._wrapped.get(key)
        if lines is None:
            lines = _wrap(fm, title, width, CARD_TITLE_LINES)
            while self._wrapped and len(self._wrapped) >= WRAPPED_KEPT:
                del self._wrapped[next(iter(self._wrapped))]  # the oldest
            self._wrapped[key] = lines
        return lines

    def _paint_focus_ring(self, p: QPainter, rect: QRectF, color: QColor) -> None:
        p.save()
        p.setPen(QPen(color, FOCUS_RING_PX))
        p.setBrush(Qt.NoBrush)
        half = FOCUS_RING_PX / 2
        p.drawRoundedRect(rect.adjusted(half, half, -half, -half), RADIUS_CARD, RADIUS_CARD)
        p.restore()

    def _strip_buttons(self, strip: QRectF) -> list[tuple[str, str, str, QRectF]]:
        """(name, label, style, rect) for the current title's buttons, right-aligned."""
        u = self._units.u
        fm = QFontMetricsF(self._units.font(BUTTON_FONT_U, QFont.Weight.DemiBold))
        height = BUTTON_H_U * u
        y = strip.center().y() - height / 2
        x = strip.right() - STRIP_PAD_U * u
        out: list[tuple[str, str, str, QRectF]] = []
        for name, label, style in reversed(_button_specs(self._details())):
            if style == _SQUARE:
                width = height
            else:
                width = fm.horizontalAdvance(label) + 2 * BUTTON_PAD_U * u
                if name == PLAY:
                    width += BUTTON_ICON_U * u + 0.5 * u
            x -= width
            out.append((name, label, style, QRectF(x, y, width, height)))
            x -= BUTTON_GAP_U * u
        out.reverse()
        return out

    def _paint_strip(self, p: QPainter, rect: QRectF) -> None:
        units = self._units
        u = units.u
        details = self._details()
        fill_round(p, rect, qcolor(PALETTE.panel_raised), RADIUS_CARD)
        tint = QLinearGradient(rect.left(), 0, rect.right(), 0)
        tint.setColorAt(0.0, qcolor(PALETTE.accent, 0.0))
        tint.setColorAt(1.0, qcolor(PALETTE.accent, STRIP_TINT_ALPHA))
        p.save()
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(tint))
        p.drawRoundedRect(rect, RADIUS_CARD, RADIUS_CARD)
        p.restore()
        buttons = self._strip_buttons(rect)
        for name, label, style, button in buttons:
            self._paint_button(p, name, label, style, button, details)
        left = rect.left() + STRIP_PAD_U * u
        right = (buttons[0][3].left() if buttons else rect.right()) - STRIP_PAD_U * u
        width = max(0.0, right - left)
        item = self._items[self._cur]
        title = details.title if details is not None else title_of(item)
        title_font = units.font(STRIP_TITLE_U, QFont.Weight.Bold)
        meta_font = units.font(STRIP_META_U)
        plot_font = units.font(STRIP_PLOT_U)
        title_h = QFontMetricsF(title_font).height()
        meta_h = QFontMetricsF(meta_font).height()
        plot_fm = QFontMetricsF(plot_font)
        meta, watched = self._meta_parts(details)
        plot = _wrap(plot_fm, details.plot, width, PLOT_LINES) if details and details.plot else []
        block = title_h
        if meta or watched:
            block += 0.3 * u + meta_h
        if plot:
            block += 0.6 * u + plot_fm.lineSpacing() * len(plot)
        y = rect.top() + (rect.height() - block) / 2
        p.setFont(title_font)
        p.setPen(qcolor(PALETTE.text))
        p.drawText(
            QRectF(left, y, width, title_h), Qt.AlignLeft | Qt.AlignVCenter, elided(p, title, width)
        )
        y += title_h
        if meta or watched:
            y += 0.3 * u
            self._paint_meta(p, QRectF(left, y, width, meta_h), meta, watched, meta_font)
            y += meta_h
        if plot:
            y += 0.6 * u
            p.setFont(plot_font)
            p.setPen(qcolor(PALETTE.text, 0.85))
            for text in plot:
                p.drawText(
                    QRectF(left, y, width, plot_fm.lineSpacing()),
                    Qt.AlignLeft | Qt.AlignVCenter,
                    text,
                )
                y += plot_fm.lineSpacing()

    @staticmethod
    def _meta_parts(details: Details | None) -> tuple[str, str]:
        """('year · duration · genre · ★ rating', 'mm:ss watched'), empty parts omitted."""
        if details is None:
            return "", ""
        parts = [details.year, details.duration, details.genre]
        if details.rating:
            parts.append(f"★ {details.rating}")
        meta = SEPARATOR.join(part for part in parts if part)
        watched = f"{fmt_duration(details.resume_ms)} watched" if details.resume_ms else ""
        return meta, watched

    def _paint_meta(self, p: QPainter, rect: QRectF, meta: str, watched: str, font: QFont) -> None:
        p.setFont(font)
        fm = p.fontMetrics()
        tail = ""
        if watched:
            tail = (SEPARATOR if meta else "") + watched
        tail_w = fm.horizontalAdvance(tail)
        meta = elided(p, meta, max(0.0, rect.width() - tail_w))
        p.setPen(qcolor(PALETTE.text, STRIP_META_ALPHA))
        p.drawText(rect, Qt.AlignLeft | Qt.AlignVCenter, meta)
        if tail:
            x = rect.left() + fm.horizontalAdvance(meta)
            if meta:
                p.drawText(
                    QRectF(x, rect.top(), rect.width(), rect.height()),
                    Qt.AlignLeft | Qt.AlignVCenter,
                    SEPARATOR,
                )
                x += fm.horizontalAdvance(SEPARATOR)
            p.setPen(qcolor(PALETTE.accent))
            p.drawText(
                QRectF(x, rect.top(), max(0.0, rect.right() - x), rect.height()),
                Qt.AlignLeft | Qt.AlignVCenter,
                elided(p, watched, rect.right() - x),
            )

    def _paint_button(
        self,
        p: QPainter,
        name: str,
        label: str,
        style: str,
        rect: QRectF,
        details: Details | None,
    ) -> None:
        u = self._units.u
        radius = BUTTON_RADIUS_U * u
        if style == _ACCENT:
            fill_round(p, rect, qcolor(PALETTE.accent), radius)
            color = qcolor(PALETTE.canvas)
        else:
            fill_round(p, rect, qcolor(PALETTE.text, BUTTON_RAISED_ALPHA), radius)
            favorite = name == FAVORITE and details is not None and details.favorite
            color = qcolor(PALETTE.accent) if favorite else qcolor(PALETTE.text)
        if style == _SQUARE:
            size = STAR_ICON_U * u
            icon = QRectF(rect.center().x() - size / 2, rect.center().y() - size / 2, size, size)
            draw_icon(p, "star", icon, color)
            return
        font = self._units.font(BUTTON_FONT_U, QFont.Weight.DemiBold)
        p.setFont(font)
        p.setPen(color)
        x = rect.left() + BUTTON_PAD_U * u
        if name == PLAY:
            size = BUTTON_ICON_U * u
            draw_icon(p, "play", QRectF(x, rect.center().y() - size / 2, size, size), color)
            x += size + 0.5 * u
        p.drawText(
            QRectF(x, rect.top(), rect.right() - x, rect.height()),
            Qt.AlignLeft | Qt.AlignVCenter,
            label,
        )

    def _paint_rail(self, p: QPainter) -> None:
        rect = self._rail_rect()
        slot = rect.height() / len(RAIL_LETTERS)
        current = ""
        best = -1
        for key, index in self._letters.items():
            if best < index <= self._cur:
                best, current = index, key
        p.setFont(self._units.font(RAIL_FONT_U))
        for k, letter in enumerate(RAIL_LETTERS):
            key = letter_of(letter)
            if key == current:
                p.setPen(qcolor(PALETTE.accent))
            else:
                p.setPen(qcolor(PALETTE.text, 0.6 if key in self._letters else 0.2))
            p.drawText(QRectF(rect.left(), k * slot, rect.width(), slot), Qt.AlignCenter, letter)
