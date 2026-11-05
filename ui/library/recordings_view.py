"""The Library's Recordings page (2.9.7): one place for what records, what is to come, what
went wrong, what was recorded and where it is kept (`services.recording_desk` builds its
entries; `ui.recordings_desk` fills it and does what its buttons ask).

Units: 1 u = 1% of this view's width (the Library screen's: the page has no genre column).
Everything is painted. At the top: the page's title and a summary line, then a card per
provider with its streams (what holds each: a recording in red, a picture in the accent),
then the section chips (All, Recording now, Coming up, Needs attention, Recorded, Places;
each with its count). Under them the entries, scrolled: a card each, its status pill, title,
detail and note on the left and its buttons on the right; a section's last row may hold
buttons only (Record at a time…, Add a place…).

The widget never takes keyboard focus: the Library controller routes keys to `move_by`,
`page`, `home`, `end`, `activate` and `escape`. ↑ and ↓ go from the chips to the entries and
between them (↑ from the chips: `upEdge`, the top bar); ← and → change the chip, or the button
of the entry; Enter presses it (`buttonClicked(key, name)`); Esc from an entry goes back to
the chips. The mouse: a click on a chip or a button, the wheel scrolls.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Optional

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, QSizeF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QMouseEvent, QPainter, QPen, QWheelEvent
from PySide6.QtWidgets import QWidget

from services import recording_desk as desk
from services.i18n import N_, number, translate
from services.recording import clock_text
from ui.overlays.base import TooltipWidget
from ui.overlays.painting import Units, elided, fill_round, ui_units
from ui.theme import FOCUS_FILL_ALPHA, PALETTE, RADIUS_CARD, qcolor

MARGIN_U = 3.0
TOP_U = 2.2
MAX_W_U = 124.0  # the cards' widest (a wide window keeps them readable)
TITLE_U = 2.4
SUMMARY_U = 1.15
STREAM_W_U = 28.0
STREAM_H_U = 8.4
STREAM_GAP_U = 1.2
STREAM_NAME_U = 1.2
STREAM_TEXT_U = 1.0
BAR_H_U = 0.7
CHIP_H_U = 3.4
CHIP_FONT_U = 1.1
CHIP_PAD_U = 1.3
CHIP_GAP_U = 0.8
GAP_U = 1.6  # between the header's parts
HEADER_ROW_U = 4.2  # a section's name in All
CARD_GAP_U = 0.9
CARD_PAD_U = 1.8
CARD_TOP_U = 1.4  # a card's texts from its top, its buttons from its bottom
LINE_GAP_U = 0.25
NOTE_LINES = 2  # a note wraps to two lines at most
CARD_H_U = 11.0  # about a card's height (a page of them, for PgUp and PgDown)
AVOID_GAP_U = 2.0  # between the page and the corner player
ENTRY_TITLE_U = 1.35
ENTRY_TEXT_U = 1.05
PILL_U = 0.9
PILL_H_U = 2.0
BUTTON_H_U = 3.6
BUTTON_FONT_U = 1.05
BUTTON_PAD_U = 1.4
BUTTON_GAP_U = 0.7
EMPTY_H_U = 5.0
WHEEL_STEP_U = 6.0  # a wheel's notch scrolls this far
RING_PX = 1.5
TICK_MS = 1000  # a recording's clock goes on

# What a section with nothing in it says.
EMPTY_TEXTS = {
    # i18n: the Recordings page's Recording now, with nothing recording
    desk.NOW: N_("Nothing is recording."),
    # i18n: the Recordings page's Coming up, with nothing scheduled
    desk.COMING: N_(
        "Nothing is scheduled. Press R on a programme in the guide, or right-click a channel "
        "and choose Record at a time…"
    ),
    # i18n: the Recordings page's Needs attention, with no problem
    desk.ATTENTION: N_("Nothing was missed."),
    # i18n: the Recordings page's Recorded, with no file
    desk.RECORDED: N_("No recordings yet."),
    desk.PLACES: "",
}
# i18n: the Recordings page's title (the Library's tab of the same name)
TITLE_TEXT = N_("Recordings")
# i18n: on the Recordings page: no provider's stream is in use, nothing is scheduled
NO_STREAMS_TEXT = N_("No stream in use")
# i18n: a provider's stream that nothing uses, in the Recordings page's tooltip of its card
FREE_STREAM_TEXT = N_("Free")


@dataclass
class _Block:
    kind: str  # "header", "empty" or "entry"
    y: float
    h: float
    section: str = ""
    text: str = ""
    index: int = -1  # an entry's, in `_shown`


def _wrap(fm: QFontMetricsF, text: str, width: float, most: int) -> list[str]:
    """`text` in lines of `width` at most, word by word; the last of `most` lines takes the
    rest, cut with an ellipsis."""
    words = text.split()
    lines: list[str] = []
    line = ""
    for k, word in enumerate(words):
        candidate = f"{line} {word}" if line else word
        if line and fm.horizontalAdvance(candidate) > width:
            if len(lines) + 1 >= most:
                rest = " ".join(words[k - len(line.split()) :])
                lines.append(fm.elidedText(rest, Qt.ElideRight, width))
                return lines
            lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        lines.append(fm.elidedText(line, Qt.ElideRight, width))
    return lines


def tone_color(tone: str):
    if tone in (desk.REC, desk.ERROR):
        return qcolor(PALETTE.live)
    if tone == desk.WARN:
        return qcolor(PALETTE.warn)
    if tone == desk.OK:
        return qcolor(PALETTE.ok)
    return qcolor(PALETTE.text, PALETTE.text_muted_alpha)


class RecordingsView(TooltipWidget):
    buttonClicked = Signal(str, str)  # (entry key, button name)
    filterChanged = Signal(str)  # desk.ALL or a section
    upEdge = Signal()  # ↑ from the chips: the top bar

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.NoFocus)
        self.setMouseTracking(True)
        self.setAccessibleName(translate(TITLE_TEXT))
        self._entries: list[desk.Entry] = []
        self._streams: list[desk.Streams] = []
        self._summary = ""
        self._filter = desk.ALL
        self._shown: list[desk.Entry] = []
        self._blocks: list[_Block] = []
        self._entry_blocks: dict[int, _Block] = {}  # each shown entry's block, by its index
        # What every mouse move and paint asks for, made once per content and size: the
        # sections' counts, and the chips' places (keyed by what they depend on).
        self._counted: Optional[dict[str, int]] = None
        self._chips_at: Optional[tuple[tuple, dict[str, QRectF]]] = None
        self._content_h = 0.0
        self._focused = False
        self._row = -1  # -1: the chips; else an entry of `_shown`
        self._button = 0
        self._scroll = 0.0
        self._hover: Optional[tuple] = None  # ("chip", section) | ("button", key, name)
        self._pressed: Optional[tuple] = None
        self._notes: dict[int, list[str]] = {}  # an entry's note, wrapped
        self._avoid: Optional[QWidget] = None  # the corner player, kept clear of (`avoid`)
        self._reserve = (0.0, 0.0)  # what it takes on the left and on the right
        self._clock = QTimer(self)
        self._clock.setInterval(TICK_MS)
        self._clock.timeout.connect(self.update)
        self._relayout()

    def avoid(self, widget: QWidget) -> None:
        """The page keeps clear of `widget` (the Library's corner player) while it shows over
        it: everything is laid out beside it, on the side away from it."""
        self._avoid = widget
        widget.installEventFilter(self)
        self._follow_avoid()

    def eventFilter(self, watched, event) -> bool:
        kinds = (QEvent.Type.Move, QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.Hide)
        if watched is self._avoid and event.type() in kinds:
            self._follow_avoid()
        return False

    def _follow_avoid(self) -> None:
        widget = self._avoid
        reserve = (0.0, 0.0)
        if widget is not None and widget.isVisible() and self.isVisible():
            top_left = self.mapFromGlobal(widget.mapToGlobal(QPoint(0, 0)))
            rect = QRectF(QPointF(top_left), QSizeF(widget.size()))
            if rect.intersects(QRectF(self.rect())):
                gap = AVOID_GAP_U * self._units().u
                if rect.center().x() > self.width() / 2:
                    reserve = (0.0, max(0.0, self.width() - rect.left() + gap - self._margin()))
                else:
                    reserve = (max(0.0, rect.right() + gap - self._margin()), 0.0)
        if reserve != self._reserve:
            self._reserve = reserve
            self._relayout()
            self.update()

    def reserved(self) -> tuple[float, float]:
        """What the corner player takes of the page's width, on its left and its right."""
        return self._reserve

    # ---- content ----------------------------------------------------------------------------

    def set_content(
        self, entries: list[desk.Entry], streams: list[desk.Streams], summary: str
    ) -> None:
        """The page's entries, its providers' streams and its summary line. The entry the
        keys were on stays (by its key), and its button; one gone leaves them on the nearest."""
        current = self.current_key()
        self._entries = list(entries)
        self._streams = list(streams)
        self._counted = None
        self._summary = summary
        self._relayout()
        if current is not None:
            keys = [e.key for e in self._shown]
            if current in keys:
                self._row = keys.index(current)
            else:
                self._row = min(self._row, len(self._shown) - 1)
            self._button = self._fit_button(self._row, self._button)
        self._sync_clock()
        self.update()

    def entries(self) -> list[desk.Entry]:
        return list(self._shown)

    def section_filter(self) -> str:
        return self._filter

    def set_filter(self, section: str) -> None:
        if section != desk.ALL and section not in desk.SECTIONS:
            raise ValueError(f"Unknown section: {section!r}")
        if section == self._filter:
            return
        self._filter = section
        self._scroll = 0.0
        if self._row >= 0:
            self._row = 0
        self._relayout()
        if self._row >= len(self._shown):
            self._row = len(self._shown) - 1
        self._button = self._fit_button(self._row, 0)
        self.filterChanged.emit(section)
        self.update()

    def counts(self) -> dict[str, int]:
        """Each section's entries (rows of buttons alone not counted)."""
        if self._counted is None:
            found = {section: 0 for section in desk.SECTIONS}
            for entry in self._entries:
                if entry.title:
                    found[entry.section] += 1
            self._counted = found
        return dict(self._counted)

    def chips(self) -> list[str]:
        return [desk.ALL, *desk.SECTIONS]

    def chip_text(self, section: str) -> str:
        if section == desk.ALL:
            return translate(desk.ALL_TEXT)
        name = translate(desk.SECTION_TITLES[section])
        count = self.counts()[section]
        return f"{name}  {number(count)}" if count and section != desk.PLACES else name

    # ---- keys -------------------------------------------------------------------------------

    def set_focused(self, focused: bool) -> None:
        self._focused = focused
        self.update()

    def current_key(self) -> Optional[str]:
        if 0 <= self._row < len(self._shown):
            return self._shown[self._row].key
        return None

    def current_button(self) -> Optional[str]:
        entry = self._current()
        if entry is None or not entry.buttons:
            return None
        return entry.buttons[min(self._button, len(entry.buttons) - 1)].name

    def at_left_edge(self) -> bool:
        """2.9.7: the keys are on a row's first button (← goes to the tab's rail)."""
        return self._row < 0 or self._button <= 0

    def on_chips(self) -> bool:
        return self._row < 0

    def focus_entry(self, key: str) -> bool:
        keys = [e.key for e in self._shown]
        if key not in keys:
            return False
        self._row = keys.index(key)
        self._button = self._fit_button(self._row, 0)
        self._reveal()
        self.update()
        return True

    def move_by(self, dx: int, dy: int) -> None:
        if dy:
            if self._row < 0 and dy < 0:
                self.upEdge.emit()
                return
            target = max(-1, min(len(self._shown) - 1, self._row + dy))
            if target != self._row:
                self._row = target
                self._button = self._fit_button(target, 0)
                self._reveal()
        elif dx:
            if self._row < 0:
                chips = self.chips()
                at = chips.index(self._filter)
                self.set_filter(chips[max(0, min(len(chips) - 1, at + dx))])
            else:
                self._button = self._step_button(self._row, self._button, dx)
        self.update()

    def page(self, delta: int) -> None:
        if not self._shown:
            return
        rows = max(1, int(self._list_rect().height() // (CARD_H_U * self._units().u)))
        self._row = max(0, min(len(self._shown) - 1, max(0, self._row) + delta * rows))
        self._button = self._fit_button(self._row, 0)
        self._reveal()
        self.update()

    def home(self) -> None:
        if self._shown:
            self._row = 0
            self._button = self._fit_button(0, 0)
            self._reveal()
            self.update()

    def end(self) -> None:
        if self._shown:
            self._row = len(self._shown) - 1
            self._button = self._fit_button(self._row, 0)
            self._reveal()
            self.update()

    def activate(self) -> None:
        """Enter: on the chips, the first entry; on an entry, its button."""
        if self._row < 0:
            if self._shown:
                self._row = 0
                self._button = self._fit_button(0, 0)
                self._reveal()
                self.update()
            return
        entry = self._current()
        if entry is None or not entry.buttons:
            return
        chosen = entry.buttons[min(self._button, len(entry.buttons) - 1)]
        if chosen.enabled:
            self.buttonClicked.emit(entry.key, chosen.name)

    def escape(self) -> bool:
        """Esc on an entry: back to the chips (True); on the chips: False (the caller leaves)."""
        if self._row >= 0:
            self._row = -1
            self._scroll = 0.0
            self.update()
            return True
        return False

    def _current(self) -> Optional[desk.Entry]:
        return self._shown[self._row] if 0 <= self._row < len(self._shown) else None

    def _fit_button(self, row: int, wanted: int) -> int:
        """`wanted`, else the nearest enabled button of entry `row` (the first one first)."""
        if not 0 <= row < len(self._shown):
            return 0
        buttons = self._shown[row].buttons
        if not buttons:
            return 0
        wanted = max(0, min(len(buttons) - 1, wanted))
        if buttons[wanted].enabled:
            return wanted
        enabled = [k for k, b in enumerate(buttons) if b.enabled]
        return min(enabled, key=lambda k: abs(k - wanted)) if enabled else wanted

    def _step_button(self, row: int, at: int, delta: int) -> int:
        buttons = self._shown[row].buttons if 0 <= row < len(self._shown) else ()
        k = at + delta
        while 0 <= k < len(buttons):
            if buttons[k].enabled:
                return k
            k += delta
        return at

    # ---- geometry ---------------------------------------------------------------------------

    def _units(self) -> Units:
        return ui_units(max(1, self.width()))

    def _margin(self) -> float:
        return MARGIN_U * self._units().u

    def _left(self) -> float:
        return self._margin() + self._reserve[0]

    def _width(self) -> float:
        u = self._units().u
        room = self.width() - 2 * self._margin() - self._reserve[0] - self._reserve[1]
        return max(0.0, min(room, MAX_W_U * u))

    def _title_rect(self) -> QRectF:
        units = self._units()
        height = QFontMetricsF(units.font(TITLE_U, QFont.Weight.Bold)).height()
        return QRectF(self._left(), TOP_U * units.u, self._width(), height)

    def _summary_rect(self) -> QRectF:
        units = self._units()
        top = self._title_rect().bottom() + 0.2 * units.u
        height = QFontMetricsF(units.font(SUMMARY_U)).height()
        return QRectF(self._left(), top, self._width(), height)

    def stream_rects(self) -> list[QRectF]:
        """Each provider's card (or the one that says none is in use), wrapped in rows."""
        units = self._units()
        u = units.u
        top = self._summary_rect().bottom() + GAP_U * u
        if not self._streams:  # a line, not an empty card: nothing holds a stream
            height = QFontMetricsF(units.font(STREAM_TEXT_U)).height()
            return [QRectF(self._left(), top, self._width(), height)]
        count = len(self._streams)
        width = STREAM_W_U * u
        per_row = max(1, int((self._width() + STREAM_GAP_U * u) // (width + STREAM_GAP_U * u)))
        rects = []
        for k in range(count):
            row, col = divmod(k, per_row)
            x = self._left() + col * (width + STREAM_GAP_U * u)
            y = top + row * (STREAM_H_U + STREAM_GAP_U) * u
            rects.append(QRectF(x, y, width, STREAM_H_U * u))
        return rects

    def chip_rects(self) -> dict[str, QRectF]:
        counts = self.counts()
        key = (
            self.width(),
            self.height(),
            self._reserve,
            len(self._streams),
            tuple(counts[section] for section in desk.SECTIONS),
        )
        if self._chips_at is None or self._chips_at[0] != key:
            self._chips_at = (key, self._lay_chips())
        return dict(self._chips_at[1])

    def _lay_chips(self) -> dict[str, QRectF]:
        units = self._units()
        u = units.u
        streams = self.stream_rects()
        top = (streams[-1].bottom() if streams else self._summary_rect().bottom()) + GAP_U * u
        fm = QFontMetricsF(units.font(CHIP_FONT_U, QFont.Weight.DemiBold))
        x = self._left()
        rects = {}
        for section in self.chips():
            width = fm.horizontalAdvance(self.chip_text(section)) + 2 * CHIP_PAD_U * u
            rects[section] = QRectF(x, top, width, CHIP_H_U * u)
            x += width + CHIP_GAP_U * u
        return rects

    def _list_rect(self) -> QRectF:
        u = self._units().u
        chips = self.chip_rects()
        top = max(r.bottom() for r in chips.values()) + GAP_U * u
        return QRectF(0, top, self.width(), max(0.0, self.height() - top))

    def _relayout(self) -> None:
        """The entries shown (the filter's) and their blocks, from the list's top (0)."""
        u = self._units().u
        wanted = desk.SECTIONS if self._filter == desk.ALL else (self._filter,)
        self._shown = []
        self._blocks = []
        self._entry_blocks = {}
        y = 0.0
        for section in wanted:
            own = [e for e in self._entries if e.section == section]
            listed = [e for e in own if e.title]
            if self._filter == desk.ALL and not own:
                continue
            if self._filter == desk.ALL and not listed and section in (desk.NOW, desk.ATTENTION):
                continue  # nothing to say: All leaves them out
            if self._filter == desk.ALL:
                name = translate(desk.SECTION_TITLES[section])
                self._blocks.append(_Block("header", y, HEADER_ROW_U * u, section, name))
                y += HEADER_ROW_U * u
            if not listed and EMPTY_TEXTS.get(section):
                text = translate(EMPTY_TEXTS[section])
                self._blocks.append(_Block("empty", y, EMPTY_H_U * u, section, text))
                y += EMPTY_H_U * u
            for entry in own:
                height = self._entry_height(entry, len(self._shown))
                block = _Block("entry", y, height, section, index=len(self._shown))
                self._blocks.append(block)
                self._entry_blocks[block.index] = block
                self._shown.append(entry)
                y += height + CARD_GAP_U * u
        self._content_h = y + 2 * u
        self._clamp_scroll()

    def _entry_height(self, entry: desk.Entry, index: int) -> float:
        """A card: its title (after its pill), its detail, its note (wrapped, `_notes`) and its
        buttons under them; a row of buttons alone: the buttons."""
        units = self._units()
        u = units.u
        buttons = BUTTON_H_U * u if entry.buttons else 0.0
        if not entry.title:
            self._notes.pop(index, None)
            return buttons + 0.4 * u
        text = QFontMetricsF(units.font(ENTRY_TEXT_U))
        height = (
            2 * CARD_TOP_U * u
            + QFontMetricsF(units.font(ENTRY_TITLE_U, QFont.Weight.Bold)).height()
        )
        if entry.detail or entry.since is not None:
            height += LINE_GAP_U * u + text.height()
        width = max(1.0, self._width() - 2 * CARD_PAD_U * u)
        lines = _wrap(text, entry.note, width, NOTE_LINES) if entry.note else []
        self._notes[index] = lines
        if lines:
            height += LINE_GAP_U * u + text.lineSpacing() * len(lines)
        if buttons:
            height += CARD_TOP_U * u * 0.8 + buttons
        return height

    def _clamp_scroll(self) -> None:
        room = self._list_rect().height()
        self._scroll = max(0.0, min(self._scroll, max(0.0, self._content_h - room)))

    def _reveal(self) -> None:
        """The entry the keys are on scrolled into view."""
        block = next((b for b in self._blocks if b.kind == "entry" and b.index == self._row), None)
        if block is None:
            return
        room = self._list_rect().height()
        if block.y < self._scroll:
            header = self._filter == desk.ALL and block is self._first_of(block)
            self._scroll = block.y - (HEADER_ROW_U * self._units().u if header else 0.0)
        elif block.y + block.h > self._scroll + room:
            self._scroll = block.y + block.h - room
        self._clamp_scroll()

    def _first_of(self, block: _Block) -> Optional[_Block]:
        """The first entry block of `block`'s section (its header shows with it)."""
        return next(
            (b for b in self._blocks if b.kind == "entry" and b.section == block.section), None
        )

    def entry_rect(self, index: int) -> Optional[QRectF]:
        """Where entry `index` is painted now (None: not laid out)."""
        block = self._entry_blocks.get(index)
        if block is None:
            return None
        top = self._list_rect().top() + block.y - self._scroll
        return QRectF(self._left(), top, self._width(), block.h)

    def button_rects(self, index: int) -> list[tuple[desk.Button, QRectF]]:
        """Entry `index`'s buttons, at the left under its texts (a corner player at the right
        never covers them), or a row of buttons alone."""
        rect = self.entry_rect(index)
        if rect is None:
            return []
        entry = self._shown[index]
        units = self._units()
        u = units.u
        fm = QFontMetricsF(units.font(BUTTON_FONT_U, QFont.Weight.DemiBold))
        widths = [fm.horizontalAdvance(b.label) + 2 * BUTTON_PAD_U * u for b in entry.buttons]
        height = BUTTON_H_U * u
        out = []
        if entry.title:
            x, y = rect.left() + CARD_PAD_U * u, rect.bottom() - CARD_TOP_U * u - height
        else:
            x, y = rect.left(), rect.top()
        for chosen, width in zip(entry.buttons, widths):
            out.append((chosen, QRectF(x, y, width, height)))
            x += width + BUTTON_GAP_U * u
        return out

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._relayout()
        self._follow_avoid()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._sync_clock()
        self._follow_avoid()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._clock.stop()

    def _sync_clock(self) -> None:
        """A second's tick while a recording's clock shows."""
        counting = self.isVisible() and any(e.since is not None for e in self._shown)
        if counting and not self._clock.isActive():
            self._clock.start()
        elif not counting:
            self._clock.stop()

    # ---- the mouse --------------------------------------------------------------------------

    def _target_at(self, point: QPointF) -> Optional[tuple]:
        for section, rect in self.chip_rects().items():
            if rect.contains(point):
                return ("chip", section)
        listed = self._list_rect()
        if not listed.contains(point):
            return None
        y = point.y() - listed.top() + self._scroll  # on the list, as its blocks are placed
        for index, block in self._entry_blocks.items():
            if not block.y <= y <= block.y + block.h:
                continue  # only the card under the pointer is measured (many recordings)
            for chosen, rect in self.button_rects(index):
                if rect.contains(point):
                    return ("button", self._shown[index].key, chosen.name, index)
            card = self.entry_rect(index)
            if card is not None and card.contains(point) and self._shown[index].title:
                return ("entry", self._shown[index].key, "", index)
        return None

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self._pressed = self._target_at(event.position())
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        pressed, self._pressed = self._pressed, None
        target = self._target_at(event.position())
        if event.button() != Qt.LeftButton or pressed is None or target != pressed:
            return
        if target[0] == "chip":
            self._row = -1
            self.set_filter(target[1])
        elif target[0] in ("button", "entry"):
            index = target[3]
            self._row = index
            entry = self._shown[index]
            if target[0] == "button":
                names = [b.name for b in entry.buttons]
                self._button = names.index(target[2])
                chosen = entry.buttons[self._button]
                if chosen.enabled:
                    self.buttonClicked.emit(entry.key, chosen.name)
            else:
                self._button = self._fit_button(index, self._button)
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        target = self._target_at(event.position())
        hover = target[:3] if target is not None and target[0] != "entry" else None
        if hover != self._hover:
            self._hover = hover
            self.setCursor(Qt.PointingHandCursor if hover is not None else Qt.ArrowCursor)
            self.update()

    def leaveEvent(self, event) -> None:
        if self._hover is not None:
            self._hover = None
            self.update()
        super().leaveEvent(event)

    def wheelEvent(self, event: QWheelEvent) -> None:
        delta = event.angleDelta().y()
        if not delta:
            return
        self._scroll -= delta / 120 * WHEEL_STEP_U * self._units().u
        self._clamp_scroll()
        self.update()

    def tooltip_at(self, pos: QPointF) -> Optional[str]:
        for streams, rect in zip(self._streams, self.stream_rects()):
            if rect.contains(pos):
                lines = [streams.provider, desk.streams_text(streams)]
                lines += [f"• {use.label}" for use in streams.uses]
                free = (streams.limit or 0) - len(streams.uses)
                lines += [f"• {translate(FREE_STREAM_TEXT)}"] * max(0, free)
                return "\n".join(lines)
        return ""

    # ---- painting ---------------------------------------------------------------------------

    def paintEvent(self, event) -> None:
        units = self._units()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), qcolor(PALETTE.canvas))
        self._paint_header(p, units)
        self._paint_streams(p, units)
        self._paint_chips(p, units)
        listed = self._list_rect()
        p.save()
        p.setClipRect(listed)
        for block in self._blocks:
            top = listed.top() + block.y - self._scroll
            if top > listed.bottom() or top + block.h < listed.top():
                continue
            if block.kind == "header":
                self._paint_section(p, units, block, top)
            elif block.kind == "empty":
                p.setFont(units.font(ENTRY_TEXT_U))
                p.setPen(qcolor(PALETTE.text, PALETTE.text_muted_alpha))
                rect = QRectF(self._left() + 0.4 * units.u, top, self._width(), block.h)
                p.drawText(
                    rect, Qt.AlignLeft | Qt.AlignVCenter, elided(p, block.text, rect.width())
                )
            else:
                self._paint_entry(p, units, block.index)
        p.restore()
        p.end()

    def _paint_header(self, p: QPainter, units: Units) -> None:
        p.setPen(qcolor(PALETTE.text))
        p.setFont(units.font(TITLE_U, QFont.Weight.Bold))
        title = self._title_rect()
        p.drawText(title, Qt.AlignLeft | Qt.AlignVCenter, translate(TITLE_TEXT))
        if self._summary:
            rect = self._summary_rect()
            p.setFont(units.font(SUMMARY_U))
            p.setPen(qcolor(PALETTE.text, PALETTE.text_muted_alpha))
            p.drawText(rect, Qt.AlignLeft | Qt.AlignVCenter, elided(p, self._summary, rect.width()))

    def _paint_streams(self, p: QPainter, units: Units) -> None:
        u = units.u
        rects = self.stream_rects()
        if not self._streams:
            inner = rects[0]
            p.setFont(units.font(STREAM_TEXT_U))
            p.setPen(qcolor(PALETTE.text, PALETTE.text_muted_alpha))
            p.drawText(
                inner,
                Qt.AlignLeft | Qt.AlignVCenter,
                elided(p, translate(NO_STREAMS_TEXT), inner.width()),
            )
            return
        name_font = units.font(STREAM_NAME_U, QFont.Weight.DemiBold)
        text_font = units.font(STREAM_TEXT_U)
        name_h = QFontMetricsF(name_font).height()
        text_h = QFontMetricsF(text_font).height()
        for streams, rect in zip(self._streams, rects):
            fill_round(p, rect, qcolor(PALETTE.panel_raised), RADIUS_CARD)
            inner = rect.adjusted(CARD_PAD_U * u, 1.1 * u, -CARD_PAD_U * u, -1.1 * u)
            count = desk.streams_text(streams)
            full = streams.limit is not None and len(streams.uses) >= streams.limit
            p.setFont(text_font)
            count_w = QFontMetricsF(text_font).horizontalAdvance(count) + 2
            p.setPen(
                qcolor(PALETTE.warn) if full else qcolor(PALETTE.text, PALETTE.text_muted_alpha)
            )
            top = QRectF(inner.left(), inner.top(), inner.width(), name_h)
            p.drawText(top, Qt.AlignRight | Qt.AlignVCenter, count)
            p.setFont(name_font)
            p.setPen(qcolor(PALETTE.text))
            name_rect = QRectF(top.left(), top.top(), max(0.0, top.width() - count_w - u), name_h)
            p.drawText(
                name_rect,
                Qt.AlignLeft | Qt.AlignVCenter,
                elided(p, streams.provider, name_rect.width()),
            )
            bar_top = top.bottom() + 0.6 * u
            slots = max(streams.limit or 0, len(streams.uses), 1)
            gap = 0.4 * u
            width = (inner.width() - gap * (slots - 1)) / slots
            for k in range(slots):
                box = QRectF(inner.left() + k * (width + gap), bar_top, width, BAR_H_U * u)
                if k < len(streams.uses):
                    use = streams.uses[k]
                    color = qcolor(PALETTE.live) if use.recording else qcolor(PALETTE.accent)
                    if use.kind in ("probe", "other"):
                        color = qcolor(PALETTE.text, 0.5)
                else:
                    color = qcolor(PALETTE.text, 0.12)
                fill_round(p, box, color, BAR_H_U * u / 2)
            labels = " · ".join(
                ("● " if use.recording else "") + use.label for use in streams.uses
            ) or translate(FREE_STREAM_TEXT)
            p.setFont(text_font)
            p.setPen(qcolor(PALETTE.text, 0.8))
            line = QRectF(inner.left(), bar_top + BAR_H_U * u + 0.6 * u, inner.width(), text_h)
            p.drawText(line, Qt.AlignLeft | Qt.AlignVCenter, elided(p, labels, line.width()))

    def _paint_chips(self, p: QPainter, units: Units) -> None:
        p.setFont(units.font(CHIP_FONT_U, QFont.Weight.DemiBold))
        attention = self.counts()[desk.ATTENTION]
        for section, rect in self.chip_rects().items():
            active = section == self._filter
            radius = rect.height() / 2
            if active:
                fill_round(p, rect, qcolor(PALETTE.accent, 0.9), radius)
            elif self._hover == ("chip", section):
                fill_round(p, rect, qcolor(PALETTE.text, 0.13), radius)
            else:
                fill_round(p, rect, qcolor(PALETTE.text, 0.07), radius)
            if self._focused and self._row < 0 and active:
                self._ring(p, rect.adjusted(-2, -2, 2, 2), radius + 2)
            if active:
                p.setPen(qcolor(PALETTE.canvas))
            elif section == desk.ATTENTION and attention:
                p.setPen(qcolor(PALETTE.warn))
            else:
                p.setPen(qcolor(PALETTE.text, 0.85))
            p.drawText(rect, Qt.AlignCenter, self.chip_text(section))

    def _paint_section(self, p: QPainter, units: Units, block: _Block, top: float) -> None:
        rect = QRectF(self._left(), top, self._width(), block.h)
        p.setFont(units.font(1.25, QFont.Weight.DemiBold))
        p.setPen(qcolor(PALETTE.text, 0.85))
        count = self.counts()[block.section]
        text = (
            block.text
            if not count or block.section == desk.PLACES
            else f"{block.text}  ·  {number(count)}"
        )
        p.drawText(rect.adjusted(0.4 * units.u, 0, 0, 0), Qt.AlignLeft | Qt.AlignBottom, text)

    def _paint_entry(self, p: QPainter, units: Units, index: int) -> None:
        u = units.u
        entry = self._shown[index]
        rect = self.entry_rect(index)
        if rect is None:
            return
        current = self._focused and index == self._row
        buttons = self.button_rects(index)
        if entry.title:
            fill_round(p, rect, qcolor(PALETTE.panel_raised), RADIUS_CARD)
            if entry.tone in (desk.REC, desk.WARN, desk.ERROR):  # a stripe of its tone, left
                p.save()
                p.setClipRect(QRectF(rect.left(), rect.top(), 0.45 * u, rect.height()))
                fill_round(p, rect, tone_color(entry.tone), RADIUS_CARD)
                p.restore()
            if current:
                self._ring(p, rect, RADIUS_CARD)
            self._paint_texts(p, units, entry, rect, index)
        for chosen, box in buttons:
            focused = current and entry.buttons[self._button] is chosen
            hover = self._hover == ("button", entry.key, chosen.name)
            self._paint_button(p, units, chosen, box, focused, hover)

    def _paint_texts(self, p, units: Units, entry: desk.Entry, rect: QRectF, index: int) -> None:
        """From the card's top: its pill and title, its detail (a recording's clock after it),
        its note (wrapped, in its tone when it warns)."""
        u = units.u
        left = rect.left() + CARD_PAD_U * u
        width = max(0.0, rect.right() - CARD_PAD_U * u - left)
        title_font = units.font(ENTRY_TITLE_U, QFont.Weight.Bold)
        text_font = units.font(ENTRY_TEXT_U)
        title_h = QFontMetricsF(title_font).height()
        text = QFontMetricsF(text_font)
        y = rect.top() + CARD_TOP_U * u
        x = left
        if entry.status:  # the status pill, before the title
            pill_font = units.font(PILL_U, QFont.Weight.Bold)
            p.setFont(pill_font)
            pill_w = QFontMetricsF(pill_font).horizontalAdvance(entry.status) + 1.6 * u
            pill = QRectF(x, y + (title_h - PILL_H_U * u) / 2, pill_w, PILL_H_U * u)
            color = tone_color(entry.tone)
            ground = QColor(color)
            ground.setAlphaF(0.18)
            fill_round(p, pill, ground, pill.height() / 2)
            p.setPen(color)
            p.drawText(pill, Qt.AlignCenter, entry.status)
            x = pill.right() + 0.9 * u
        p.setFont(title_font)
        p.setPen(qcolor(PALETTE.text))
        title_rect = QRectF(x, y, max(0.0, left + width - x), title_h)
        p.drawText(
            title_rect, Qt.AlignLeft | Qt.AlignVCenter, elided(p, entry.title, title_rect.width())
        )
        y += title_h
        detail = entry.detail
        if entry.since is not None:
            elapsed = clock_text(time.monotonic() - entry.since)
            detail = f"{detail} · {elapsed}" if detail else elapsed
        p.setFont(text_font)
        if detail:
            y += LINE_GAP_U * u
            p.setPen(qcolor(PALETTE.text, PALETTE.text_muted_alpha))
            line = QRectF(left, y, width, text.height())
            p.drawText(line, Qt.AlignLeft | Qt.AlignVCenter, elided(p, detail, width))
            y += text.height()
        lines = self._notes.get(index, [])
        if lines:
            y += LINE_GAP_U * u
            tone = entry.tone if entry.tone in (desk.WARN, desk.ERROR) else desk.QUIET
            p.setPen(tone_color(tone) if tone != desk.QUIET else qcolor(PALETTE.text, 0.8))
            for text_line in lines:
                line = QRectF(left, y, width, text.lineSpacing())
                p.drawText(line, Qt.AlignLeft | Qt.AlignVCenter, text_line)
                y += text.lineSpacing()

    def _paint_button(
        self, p, units: Units, chosen: desk.Button, box: QRectF, focused: bool, hover: bool
    ) -> None:
        radius = box.height() / 2
        alpha = 1.0 if chosen.enabled else 0.35
        if chosen.style == desk.ACCENT:
            fill_round(
                p, box, qcolor(PALETTE.accent, (1.0 if hover or focused else 0.88) * alpha), radius
            )
            pen = qcolor(PALETTE.canvas, alpha)
        elif chosen.style == desk.DANGER:
            fill_round(
                p, box, qcolor(PALETTE.live, (0.3 if hover or focused else 0.16) * alpha), radius
            )
            pen = qcolor(PALETTE.text, alpha)
        else:
            fill_round(
                p, box, qcolor(PALETTE.text, (0.2 if hover or focused else 0.11) * alpha), radius
            )
            pen = qcolor(PALETTE.text, 0.92 * alpha)
        if focused:
            self._ring(p, box.adjusted(-2, -2, 2, 2), radius + 2)
        p.setFont(units.font(BUTTON_FONT_U, QFont.Weight.DemiBold))
        p.setPen(pen)
        p.drawText(box, Qt.AlignCenter, chosen.label)

    def _ring(self, p: QPainter, rect: QRectF, radius: float) -> None:
        p.save()
        fill_round(p, rect, qcolor(PALETTE.accent, FOCUS_FILL_ALPHA * 0.5), radius)
        p.setPen(QPen(qcolor(PALETTE.accent), RING_PX))
        p.setBrush(Qt.NoBrush)
        half = RING_PX / 2
        p.drawRoundedRect(rect.adjusted(half, half, -half, -half), radius, radius)
        p.restore()
