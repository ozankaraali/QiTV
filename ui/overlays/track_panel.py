"""A small panel above a player control's button: the Sound and Subtitles panels (2.2).

The transport's ♫ (or its subtitles button) opens it just above the button and the player
controls (which stay up while it shows). It lists what can be chosen, the one playing ticked
(the sound's tracks; Off and the subtitle tracks), maybe an action row ("Load a file…"),
then a Sync row: [ − ]  Sound 150 ms later  [ + ], with Reset beside "Sync" while it is not
in sync. A click on a row chooses it; − and + step (a quick second
click steps again); hovering them names their keys. The keys reach it through the shell
while it shows (ui/track_controls.py): ↑/↓ move between its rows, Enter chooses, ←/→ step,
Esc closes it. A click anywhere else in the window closes it and does nothing more, but on
another of the player controls' buttons (`set_controls`) it closes it and that button acts
(one click goes from Sound to Subtitles); its own button only closes it. The mouse's Back and
Forward buttons go on to the shell, which closes it as any key would, and IDLE_MS without a
touch closes it too; `closed` says so.

It never grows taller than the room above the player controls (a small window, the mini
player): beyond what fits, its rows scroll (the wheel, ↑/↓, a small arrow at each end that
has more), and the title and the Sync row always show.

It is painted as the mixer is (ui/overlays/mixer.py): the same panel, outline and radius, the
same unit-based metrics and focus fill, and the speed chip's round − and + (transport).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Callable, Optional

from PySide6.QtCore import (
    QElapsedTimer,
    QEvent,
    QObject,
    QPointF,
    QRect,
    QRectF,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QFont,
    QFontMetricsF,
    QHelpEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QWheelEvent,
)
from PySide6.QtWidgets import QApplication, QToolTip, QWidget

from ui.overlays.base import Overlay
from ui.overlays.painting import Units, draw_icon, fill_round
from ui.pointer import NAV_BUTTONS
from ui.theme import FOCUS_FILL_ALPHA, PALETTE, qcolor

IDLE_MS = 10_000  # after the last touch
MINUS = "−"
SYNC_LABEL = "Sync"
RESET_LABEL = "Reset"
_FALLBACK_HOST_WIDTH = 1280
_PAD_U = 1.0
_MIN_WIDTH_U = 24.0
_TEXT_MAX_U = 28.0  # a longer track name is elided
_ROW_U = 2.8
_TICK_U = 2.4  # the tick's column
_ICON_U = 1.5
_CHIP_U = 2.6  # − and +
_GAP_U = 0.6
_SECTION_U = 1.0  # between the rows and the Sync row
_FLOOR_GAP_U = 1.0  # above the transport's controls
_BOTTOM_U = 4.0  # from the bottom, without them
_EDGE_U = 1.0  # from the host's sides, and its top
_ARROW_U = 1.0  # the "more rows" arrows
_WHEEL_STEP = 120  # one notch: one row
_EVENTS_OUTSIDE = (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick)


@dataclass(frozen=True)
class PanelRow:
    key: object  # what `rowChosen` gives back: a track's id, or an action's name
    text: str
    checked: bool = False
    action: bool = False  # it does something ("Load a file…"): never ticked, in the accent
    icon: str = ""  # an action's icon (painting.draw_icon's name; "": a plus)


@dataclass(frozen=True)
class PanelSync:
    text: str  # "In sync", "Sound 150 ms later"
    widest: str  # the longest text it can show: the panel keeps its width while stepping
    earlier: bool = True  # − can step (not at the limit)
    later: bool = True
    reset: bool = False  # not in sync: Reset shows
    earlier_tip: str = ""  # − and +'s tooltips (they name the keys)
    later_tip: str = ""


@dataclass(frozen=True)
class PanelModel:
    title: str
    rows: tuple[PanelRow, ...] = ()
    sync: Optional[PanelSync] = None


@dataclass
class _Rects:
    title: QRectF
    rows: list[QRectF] = field(default_factory=list)
    separator: Optional[float] = None  # the line's y between the rows and Sync
    label: QRectF = field(default_factory=QRectF)  # "Sync"
    reset: QRectF = field(default_factory=QRectF)  # empty: in sync
    sync_row: QRectF = field(default_factory=QRectF)  # the focus fill of the Sync row
    earlier: QRectF = field(default_factory=QRectF)
    text: QRectF = field(default_factory=QRectF)
    later: QRectF = field(default_factory=QRectF)


class TrackPanel(Overlay):
    rowChosen = Signal(object)  # a row's key: a click, or Enter on it
    syncStepped = Signal(int)  # -1: earlier, +1: later
    syncReset = Signal()
    closed = Signal()  # it hid itself (a click elsewhere, Esc, the idle wait) or was dismissed

    def __init__(self, parent: Optional[QWidget] = None, idle_ms: int = IDLE_MS) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self._model = PanelModel("")
        self._anchor_x: Optional[float] = None  # the button's centre (host coordinates)
        self._floor: Optional[int] = None  # the transport's controls' top while they show
        self._focus: Optional[int] = None  # the keys' row (len(rows): the Sync row)
        self._hover: Optional[int] = None  # the row under the pointer
        self._shown = QElapsedTimer()  # since it showed: a double-click's second press
        self._filtering = False
        self._fit = 0  # how many rows fit (0: not laid out yet, all of them)
        self._scroll = 0  # the first row shown
        self._wheel = 0  # a wheel's turn not yet a whole row
        self._button = ""  # the player controls' button it belongs to
        # The player controls and what is under a point of them (see set_controls).
        self._controls: Optional[QWidget] = None
        self._controls_hit: Optional[Callable[[QPointF], Optional[str]]] = None
        self._idle_ms = idle_ms
        self._idle = QTimer(self, singleShot=True, interval=idle_ms)
        self._idle.timeout.connect(self.dismiss)
        if parent is not None:
            parent.installEventFilter(self)
        self.hide()

    # ---- public API ---------------------------------------------------------------------

    def show_panel(
        self,
        model: PanelModel,
        anchor: Optional[QRectF] = None,
        floor: Optional[int] = None,
        button: Optional[str] = None,
    ) -> None:
        """Show `model` above `anchor` (`button`'s rect, host coordinates) and `floor` (where
        the player controls start; None: near the bottom), or update it in place."""
        keys = [row.key for row in model.rows]
        fresh = self.isHidden() or keys != [row.key for row in self._model.rows]
        if fresh or (model.sync is None) != (self._model.sync is None):
            self._focus = self._hover = None  # other rows: the keys start again
        self._model = model
        if anchor is not None:
            self._anchor_x = anchor.center().x()
        self._floor = floor if anchor is not None else self._floor
        if button is not None:
            self._button = button
        self._layout()
        if fresh:  # the row ticked shows
            self._scroll = 0
            ticked = [i for i, row in enumerate(model.rows) if row.checked]
            if ticked:
                self._scroll_to(ticked[0])
        self._scroll = self._clamped(self._scroll)
        if self.isHidden():
            self._shown.start()
            self.show()
        self.raise_()
        self.update()
        self._touch()

    def model(self) -> PanelModel:
        return self._model

    def set_model(self, model: PanelModel) -> None:
        """What it shows changed by itself (a track came): no touch, the idle wait goes on."""
        if model != self._model and not self.isHidden():
            remaining = self._idle.remainingTime()
            self.show_panel(model)
            if remaining >= 0:
                self._idle.start(remaining)

    def _touch(self) -> None:
        """A touch: the idle wait starts again, whole."""
        self._idle.start(self._idle_ms)

    def dismiss(self) -> None:
        self._idle.stop()
        if not self.isHidden():
            self.hide()
            self.closed.emit()

    def set_controls(
        self, controls: QWidget, control_at: Callable[[QPointF], Optional[str]]
    ) -> None:
        """The player controls (the transport) and its control at a point of it (None: none):
        a press on one of them closes the panel and goes on to it, but on the panel's own
        button (`show_panel`'s), which only closes it."""
        self._controls, self._controls_hit = controls, control_at

    def set_floor(self, top: Optional[int]) -> None:
        """Sit just above `top` (host coordinates), or with None near the bottom."""
        if top != self._floor:
            self._floor = top
            if not self.isHidden():
                self._layout()

    def focused(self) -> Optional[int]:
        """The row the keys are on (len(rows): the Sync row), or None."""
        return self._focus

    def move_focus(self, delta: int) -> None:
        """↑/↓: the keys' row moves; the first press lands on the ticked row (else the first)."""
        stops = len(self._model.rows) + (1 if self._model.sync is not None else 0)
        if not stops:
            return
        if self._focus is None:
            ticked = [i for i, row in enumerate(self._model.rows) if row.checked]
            self._focus = ticked[0] if ticked else (0 if delta > 0 else stops - 1)
        else:
            self._focus = max(0, min(stops - 1, self._focus + delta))
        if self._focus < len(self._model.rows):
            self._scroll_to(self._focus)
        self._touch()
        self.update()

    def scroll_by(self, rows: int) -> None:
        """Show rows further down (positive) or up, when not all fit."""
        scroll = self._clamped(self._scroll + rows)
        if scroll != self._scroll:
            self._scroll = scroll
            self._hover = None
            self.update()

    def shown_rows(self) -> range:
        """The rows on show (all of them when they fit)."""
        return range(self._scroll, self._scroll + self._shown_count())

    def _shown_count(self) -> int:
        rows = len(self._model.rows)
        return min(rows, self._fit) if self._fit else rows

    def _clamped(self, scroll: int) -> int:
        return max(0, min(scroll, len(self._model.rows) - self._shown_count()))

    def _scroll_to(self, index: int) -> None:
        shown = self._shown_count()
        if index < self._scroll:
            self._scroll = index
        elif index >= self._scroll + shown:
            self._scroll = index - shown + 1
        self._scroll = self._clamped(self._scroll)

    def activate(self) -> None:
        """Enter: choose the keys' row (the Sync row has nothing to choose)."""
        self._touch()
        focus = self._focus
        if focus is not None and focus < len(self._model.rows):
            self.rowChosen.emit(self._model.rows[focus].key)

    def step(self, delta: int) -> None:
        """←/→: the Sync row's − or +, when it can step that way."""
        self._touch()
        sync = self._model.sync
        if sync is not None and (sync.earlier if delta < 0 else sync.later):
            self.syncStepped.emit(-1 if delta < 0 else 1)

    def row_rect(self, index: int) -> QRectF:
        """Where row `index` is; empty while it is scrolled out of sight."""
        return self._rects().rows[index]

    def control_rect(self, name: str) -> QRectF:
        """ "earlier" (−), "later" (+), "reset" or "title"; empty when not shown."""
        rects = self._rects()
        return QRectF(getattr(rects, name))

    # ---- layout -------------------------------------------------------------------------

    def _units(self) -> Units:
        parent = self.parentWidget()
        return Units(parent.width() if parent is not None else _FALLBACK_HOST_WIDTH)

    @staticmethod
    def _fonts(un: Units) -> dict[str, QFont]:
        return {
            "title": un.font(1.25, QFont.Weight.Bold),
            "row": un.font(1.2),
            "label": un.font(1.05, QFont.Weight.DemiBold),
            "sync": un.font(1.2, QFont.Weight.DemiBold),
            "chip": un.font(1.4, QFont.Weight.DemiBold),
        }

    def _heights(self, un: Units) -> dict[str, float]:
        fonts = self._fonts(un)
        height = {name: QFontMetricsF(font).height() for name, font in fonts.items()}
        return {
            "title": height["title"] + un.px(0.8),
            "row": max(float(un.px(_ROW_U)), height["row"] + un.px(0.8)),
            "label": height["label"] + un.px(0.4),
            "sync": max(float(un.px(_CHIP_U)), height["sync"] + un.px(0.8)),
        }

    def _fixed_height(self, un: Units) -> float:
        """Its height without the rows: the padding, the title and the Sync row."""
        heights = self._heights(un)
        height = 2 * un.px(_PAD_U) + heights["title"]
        if self._model.sync is not None:
            height += un.px(_SECTION_U) + heights["label"] + heights["sync"]
        return height

    def _size(self) -> tuple[int, int]:
        un = self._units()
        fonts, heights = self._fonts(un), self._heights(un)
        model = self._model
        title = QFontMetricsF(fonts["title"]).horizontalAdvance(model.title)
        row_fm = QFontMetricsF(fonts["row"])
        rows = max(
            (
                un.px(_TICK_U) + min(row_fm.horizontalAdvance(r.text), float(un.px(_TEXT_MAX_U)))
                for r in model.rows
            ),
            default=0.0,
        )
        if self._shown_count() < len(model.rows):
            rows += un.px(_ARROW_U) + un.px(_GAP_U)  # the arrows keep their room
        content = max(title, rows, float(un.px(_MIN_WIDTH_U - 2 * _PAD_U)))
        height = self._fixed_height(un) + self._shown_count() * heights["row"]
        sync = model.sync
        if sync is not None:
            label_fm = QFontMetricsF(fonts["label"])
            label = (
                label_fm.horizontalAdvance(SYNC_LABEL)
                + 4 * un.px(_GAP_U)
                + label_fm.horizontalAdvance(RESET_LABEL)
            )
            text = QFontMetricsF(fonts["sync"]).horizontalAdvance(sync.widest)
            controls = 2 * un.px(_CHIP_U) + 4 * un.px(_GAP_U) + text
            content = max(content, label, controls)
        return math.ceil(content + 2 * un.px(_PAD_U)) + 2, math.ceil(height)

    def _layout(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        un = Units(parent.width())
        edge = un.px(_EDGE_U)
        bottom = (
            parent.height() - un.px(_BOTTOM_U)
            if self._floor is None
            else self._floor - un.px(_FLOOR_GAP_U)
        )
        # As many rows as fit between the host's top and the controls (at least one).
        room = bottom - edge - self._fixed_height(un)
        self._fit = max(1, math.floor(room / self._heights(un)["row"]))
        self._scroll = self._clamped(self._scroll)
        width, height = self._size()
        width = min(width, parent.width())
        center = self._anchor_x if self._anchor_x is not None else parent.width() / 2
        x = round(center - width / 2)
        x = max(min(x, parent.width() - width - edge), min(edge, parent.width() - width))
        rect = QRect(x, max(0, bottom - height), width, height)
        if rect != self.geometry():
            self.setGeometry(rect)

    def _rects(self) -> _Rects:
        un = self._units()
        heights = self._heights(un)
        pad, gap = float(un.px(_PAD_U)), float(un.px(_GAP_U))
        left, right = pad, self.width() - pad
        y = pad
        rects = _Rects(QRectF(left, y, right - left, heights["title"]))
        y += heights["title"]
        shown = self.shown_rows()
        for index in range(len(self._model.rows)):
            if index not in shown:
                rects.rows.append(QRectF())  # scrolled out of sight
                continue
            rects.rows.append(QRectF(pad / 2, y, self.width() - pad, heights["row"]))
            y += heights["row"]
        sync = self._model.sync
        if sync is None:
            return rects
        rects.separator = y + un.px(_SECTION_U) / 2
        y += un.px(_SECTION_U)
        rects.label = QRectF(left, y, right - left, heights["label"])
        if sync.reset:
            fm = QFontMetricsF(self._fonts(un)["label"])
            width = fm.horizontalAdvance(RESET_LABEL) + 2 * gap
            rects.reset = QRectF(right - width + gap, y, width, heights["label"])
        y += heights["label"]
        row_h = heights["sync"]
        chip = float(un.px(_CHIP_U))
        cy = y + row_h / 2
        rects.sync_row = QRectF(pad / 2, y, self.width() - pad, row_h)
        rects.earlier = QRectF(left, cy - chip / 2, chip, chip)
        rects.later = QRectF(right - chip, cy - chip / 2, chip, chip)
        rects.text = QRectF(
            rects.earlier.right() + gap,
            y,
            rects.later.left() - gap - rects.earlier.right() - gap,
            row_h,
        )
        return rects

    # ---- painting -----------------------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:
        un = self._units()
        fonts = self._fonts(un)
        rects = self._rects()
        model = self._model
        text = qcolor(PALETTE.text)
        muted = qcolor(PALETTE.text, PALETTE.text_muted_alpha)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        panel = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = un.u * 1.2
        p.setPen(QPen(qcolor(PALETTE.text, 0.12), 1.0))
        p.setBrush(qcolor(PALETTE.panel, 0.92))
        p.drawRoundedRect(panel, radius, radius)
        left = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        p.setFont(fonts["title"])
        p.setPen(text)
        p.drawText(rects.title, left, model.title)
        lit = self._hover if self._hover is not None else self._focus
        row_fm = QFontMetricsF(fonts["row"])
        tick_w, icon = float(un.px(_TICK_U)), float(un.px(_ICON_U))
        shown = self.shown_rows()
        scrolls = len(shown) < len(model.rows)
        arrow = float(un.px(_ARROW_U)) if scrolls else 0.0
        for index, (row, rect) in enumerate(zip(model.rows, rects.rows)):
            if rect.isEmpty():
                continue
            if index == lit:
                self._paint_focus(p, un, rect, outline=index == self._focus)
            x = rect.left() + un.px(_PAD_U) / 2
            if row.checked or row.action:
                box = QRectF(x + (tick_w - icon) / 2 - un.px(0.3), 0, icon, icon)
                box.moveCenter(QPointF(box.center().x(), rect.center().y()))
                glyph = (row.icon or "plus") if row.action else "check"
                draw_icon(p, glyph, box, qcolor(PALETTE.accent))
            p.setFont(fonts["row"])
            p.setPen(qcolor(PALETTE.accent) if row.action else text)
            room = rect.right() - un.px(_PAD_U) / 2 - (x + tick_w) - arrow
            label = row_fm.elidedText(row.text, Qt.TextElideMode.ElideRight, max(0.0, room))
            p.drawText(QRectF(x + tick_w, rect.top(), room, rect.height()), left, label)
        if scrolls:
            self._paint_arrows(p, un, rects, shown, muted)
        sync = model.sync
        if sync is not None:
            self._paint_sync(p, un, fonts, rects, sync, text, muted)
        p.end()

    def _paint_arrows(self, p: QPainter, un: Units, rects: _Rects, shown: range, color) -> None:
        """A small ▴ on the first row shown when rows are above it, ▾ on the last for below."""
        size = float(un.px(_ARROW_U))
        for index, up in ((shown.start, True), (shown.stop - 1, False)):
            if (up and index == 0) or (not up and index == len(self._model.rows) - 1):
                continue
            rect = rects.rows[index]
            cx, cy = rect.right() - un.px(_PAD_U) / 2 - size / 2, rect.center().y()
            half = size / 2
            tip, base = (
                (cy - half * 0.6, cy + half * 0.6) if up else (cy + half * 0.6, cy - half * 0.6)
            )
            path = QPainterPath(QPointF(cx - half, base))
            path.lineTo(cx + half, base)
            path.lineTo(cx, tip)
            path.closeSubpath()
            p.save()
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(color)
            p.drawPath(path)
            p.restore()

    @staticmethod
    def _paint_focus(p: QPainter, un: Units, rect: QRectF, *, outline: bool) -> None:
        fill_round(p, rect, qcolor(PALETTE.accent, FOCUS_FILL_ALPHA), un.u * 0.8)
        if outline:  # the keys' row (the pointer's alone is only filled)
            p.save()
            p.setPen(QPen(qcolor(PALETTE.accent), max(1.0, un.u * 0.12)))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(rect, un.u * 0.8, un.u * 0.8)
            p.restore()

    def _paint_sync(self, p, un, fonts, rects: _Rects, sync: PanelSync, text, muted) -> None:
        if rects.separator is not None:
            p.setPen(QPen(qcolor(PALETTE.text, 0.12), 1.0))
            y = round(rects.separator) + 0.5
            p.drawLine(QPointF(rects.label.left(), y), QPointF(rects.label.right(), y))
        if self._focus == len(self._model.rows):
            self._paint_focus(p, un, rects.sync_row, outline=True)
        p.setFont(fonts["label"])
        p.setPen(muted)
        p.drawText(
            rects.label, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, SYNC_LABEL
        )
        if not rects.reset.isEmpty():
            p.setPen(qcolor(PALETTE.accent))
            p.drawText(rects.reset, Qt.AlignmentFlag.AlignCenter, RESET_LABEL)
        dim = qcolor(PALETTE.text, 0.35)
        for rect, glyph, able in (
            (rects.earlier, MINUS, sync.earlier),
            (rects.later, "+", sync.later),
        ):
            fill_round(p, rect, qcolor(PALETTE.text, 0.14), rect.height() / 2)
            p.setFont(fonts["chip"])
            p.setPen(text if able else dim)
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, glyph)
        p.setFont(fonts["sync"])
        p.setPen(text if sync.reset else muted)
        shown = QFontMetricsF(fonts["sync"]).elidedText(
            sync.text, Qt.TextElideMode.ElideRight, rects.text.width()
        )
        p.drawText(rects.text, Qt.AlignmentFlag.AlignCenter, shown)

    # ---- input --------------------------------------------------------------------------

    def _control_at(self, pos: QPointF) -> Optional[tuple[str, int]]:
        """("row", index), ("earlier"|"later"|"reset", 0), or None."""
        rects = self._rects()
        for index, rect in enumerate(rects.rows):
            if rect.contains(pos):
                return ("row", index)
        for name in ("earlier", "later", "reset"):
            rect = getattr(rects, name)
            if not rect.isEmpty() and rect.contains(pos):
                return (name, 0)
        return None

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() in NAV_BUTTONS:
            event.ignore()  # Back/Forward go on to the host: the shell's drawer layers
            return
        event.accept()  # never the host's: it would open the channel list
        self._touch()
        if event.button() != Qt.MouseButton.LeftButton:
            return
        hit = self._control_at(event.position())
        if hit is None:
            return
        name, index = hit
        if name == "row":
            self.rowChosen.emit(self._model.rows[index].key)
        elif name == "reset":
            self.syncReset.emit()
        else:
            self.step(-1 if name == "earlier" else 1)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() in NAV_BUTTONS:
            event.ignore()
            return
        self.mousePressEvent(event)  # a quick second click is another click, never fullscreen

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        hit = self._control_at(event.position())
        hover = hit[1] if hit is not None and hit[0] == "row" else None
        if hover != self._hover:
            self._hover = hover
            self.update()
        self._touch()
        event.ignore()  # the host shows the player controls

    def leaveEvent(self, event: QEvent) -> None:
        if self._hover is not None:
            self._hover = None
            self.update()
        super().leaveEvent(event)

    def wheelEvent(self, event: QWheelEvent) -> None:
        """Its rows scroll, when not all fit; never the host's channel change under it."""
        event.accept()
        self._touch()
        self._wheel += event.angleDelta().y()
        rows = int(self._wheel / _WHEEL_STEP)  # towards zero: a notch up shows the rows above
        if rows:
            self._wheel -= rows * _WHEEL_STEP
            self.scroll_by(-rows)

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.ToolTip and isinstance(event, QHelpEvent):
            text = self.tooltip_at(QPointF(event.pos()))
            if text:
                QToolTip.showText(event.globalPos(), text, self)
            else:
                QToolTip.hideText()
                event.ignore()
            return True
        return super().event(event)

    def tooltip_at(self, pos: QPointF) -> str:
        hit, sync = self._control_at(pos), self._model.sync
        if hit is None or sync is None:
            return ""
        return {"earlier": sync.earlier_tip, "later": sync.later_tip}.get(hit[0], "")

    def showEvent(self, event) -> None:
        super().showEvent(event)
        app = QApplication.instance()
        if app is not None and not self._filtering:
            app.installEventFilter(self)  # a click elsewhere closes it
            self._filtering = True

    def hideEvent(self, event) -> None:
        self._stop_filtering()
        self._hover = None
        super().hideEvent(event)

    def _stop_filtering(self) -> None:
        app = QApplication.instance()
        if app is not None and self._filtering:
            app.removeEventFilter(self)
        self._filtering = False

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        kind = event.type()
        if obj is self.parentWidget() and kind == QEvent.Type.Resize and not self.isHidden():
            self._layout()
        elif (
            kind in _EVENTS_OUTSIDE
            and not self.isHidden()
            and isinstance(event, QMouseEvent)
            and event.button() not in NAV_BUTTONS  # Back/Forward: the shell's drawer layers
            and self._outside(obj)
        ):
            if (
                kind == QEvent.Type.MouseButtonDblClick
                and self._shown.isValid()
                and self._shown.elapsed() < QApplication.doubleClickInterval()
            ):
                return True  # the second press of the double-click that opened it
            control = self._control_under(obj, event)
            self.dismiss()
            # Another of the player controls' buttons acts; its own button only closes it.
            return control is None or control == self._button
        return False

    def _control_under(self, obj: QObject, event: QMouseEvent) -> Optional[str]:
        """The player controls' control under a press on them (None: elsewhere)."""
        if obj is not self._controls or self._controls_hit is None:
            return None
        return self._controls_hit(event.position())

    def _outside(self, obj: QObject) -> bool:
        """A press on another widget of this window (its overlays, the video): not on the
        panel, nor in a dialog or another window."""
        if not isinstance(obj, QWidget) or obj is self or self.isAncestorOf(obj):
            return False
        host = self.window()
        ours = {host}
        owner = host.parentWidget()
        if owner is not None:
            ours.add(owner.window())
        return obj.window() in ours

    def closeEvent(self, event) -> None:
        self._stop_filtering()
        super().closeEvent(event)
