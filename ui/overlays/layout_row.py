"""The multiview's layouts to pick from: a small row above the player controls' layout chip.

A click on the layout chip (ui/overlays/transport.py) opens it just above the chip and the
controls (which stay up while it shows): the four layouts drawn as small pictures of their
tiles (1×2, 2×2, 3×2 and 3×3), each named under it, the one showing marked as the mixer marks
its selected row. One click picks a layout (`layoutChosen`) and closes the row. A layout this
computer does not offer (6 and 9 tiles need hardware decoding) is dimmed: hovering it says
why, and a click on it says so too (`unavailableChosen`) and picks nothing.

It closes as the Sound panel does (ui/overlays/track_panel.py): a click anywhere else in the
window closes it and does nothing more, but on another of the player controls' buttons
(`set_controls`) it closes it and that button acts; the chip itself only closes it. The
mouse's Back and Forward buttons go on to the shell, IDLE_MS without a touch closes it, and
`closed` says so. The keys reach it through the shell while it shows: ←/→ move between the
layouts, Enter picks, Esc closes.

Shown as a hint (`hint=True`, the first time a grid shows) it closes by itself after
HINT_MS, and a click elsewhere closes it and goes on to what was clicked (on the chip: the
row opens again, as a click opens it).

It is painted as the mixer is: the same panel, outline and radius, unit-based metrics and
focus fill.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Callable, Optional, Sequence

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
from PySide6.QtGui import QFont, QFontMetricsF, QMouseEvent, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QApplication, QWidget

from ui.overlays.base import Overlay
from ui.overlays.painting import LAYOUT_ICONS, Units, draw_icon, fill_round, layout_frame
from ui.pointer import NAV_BUTTONS
from ui.theme import FOCUS_FILL_ALPHA, PALETTE, qcolor

IDLE_MS = 10_000  # after the last touch
HINT_MS = 5_000  # shown as a hint, it closes by itself after this
DIM_ALPHA = 0.32  # a layout not offered
_FALLBACK_HOST_WIDTH = 1280
_PAD_U = 0.7
_CELL_GAP_U = 0.4
_CELL_PAD_U = 0.8
_PICTURE_U = 4.6  # the picture's box (painting's layout icons: its frame is 0.84 of it wide)
_MIN_PICTURE_PX = 34  # a 3×3 picture stays readable in a small window
_LABEL_GAP_U = 0.35
_FLOOR_GAP_U = 1.0  # above the player controls
_BOTTOM_U = 4.0  # from the bottom, without them
_EDGE_U = 1.0  # from the host's sides
# The tallest picture's frame in its box: every picture is centred on its band.
_FRAME_HEIGHT = max(layout_frame(*tiles)[1] for tiles in LAYOUT_ICONS.values())
_EVENTS_OUTSIDE = (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick)


@dataclass(frozen=True)
class LayoutChoice:
    cells: int  # 2, 4, 6 or 9
    name: str  # "2×2"
    available: bool = True
    tooltip: str = ""  # what hovering it says (why it is dimmed)


class LayoutRow(Overlay):
    pass_empty_tooltip = True  # off its layouts, the parent's tooltip shows

    layoutChosen = Signal(int)  # a layout offered was clicked (or Enter on it): its tiles
    unavailableChosen = Signal(int)  # a dimmed one was clicked: the shell says why
    closed = Signal()  # it hid itself (a click elsewhere, Esc, the idle wait) or was dismissed

    def __init__(self, parent: Optional[QWidget] = None, idle_ms: int = IDLE_MS) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self._choices: list[LayoutChoice] = []
        self._current = 0
        self._anchor_x: Optional[float] = None  # the chip's centre (host coordinates)
        self._floor: Optional[int] = None  # the player controls' top while they show
        self._focus: Optional[int] = None  # the keys' layout (an index)
        self._hover: Optional[int] = None  # the layout under the pointer (an index)
        self._hint = False
        self._shown = QElapsedTimer()  # since it showed: a double-click's second press
        self._filtering = False
        self._button = "layout"  # the player controls' button it belongs to
        self._controls: Optional[QWidget] = None
        self._controls_hit: Optional[Callable[[QPointF], Optional[str]]] = None
        self._idle_ms = idle_ms
        self._idle = QTimer(self, singleShot=True, interval=idle_ms)
        self._idle.timeout.connect(self.dismiss)
        if parent is not None:
            parent.installEventFilter(self)
        self.hide()

    # ---- public API ---------------------------------------------------------------------

    def show_layouts(
        self,
        choices: Sequence[LayoutChoice],
        current: int,
        anchor: Optional[QRectF] = None,
        floor: Optional[int] = None,
        *,
        hint: bool = False,
    ) -> None:
        """Show `choices` (the layout of `current` tiles marked) above `anchor` (the chip's
        rect, host coordinates) and `floor` (where the player controls start; None: near the
        bottom), or update them in place. `hint`: it closes by itself after HINT_MS, and a
        click elsewhere goes on."""
        fresh = self.isHidden()
        self._choices = list(choices)
        self._current = current
        if fresh:
            self._focus = self._hover = None
        if anchor is not None:
            self._anchor_x = anchor.center().x()
            self._floor = floor
        self._hint = hint
        self._layout()
        if fresh:
            self._shown.start()
            self.show()
        self.raise_()
        self.update()
        self._touch()

    def choices(self) -> list[LayoutChoice]:
        return list(self._choices)

    def current(self) -> int:
        return self._current

    def hinting(self) -> bool:
        """It shows as the first grid's hint."""
        return self._hint and not self.isHidden()

    def dismiss(self) -> None:
        self._idle.stop()
        if not self.isHidden():
            self.hide()
            self.closed.emit()

    def set_controls(
        self, controls: QWidget, control_at: Callable[[QPointF], Optional[str]]
    ) -> None:
        """The player controls (the transport) and its control at a point of it (None: none):
        a press on one of them closes the row and goes on to it, but on the layout chip,
        which only closes it."""
        self._controls, self._controls_hit = controls, control_at

    def set_floor(self, top: Optional[int]) -> None:
        """Sit just above `top` (host coordinates), or with None near the bottom."""
        if top != self._floor:
            self._floor = top
            if not self.isHidden():
                self._layout()

    def cell_rect(self, cells: int) -> QRectF:
        """Where the layout of `cells` tiles is (this widget's coordinates; empty: none)."""
        for choice, rect in zip(self._choices, self._cells()):
            if choice.cells == cells:
                return rect
        return QRectF()

    def focused(self) -> Optional[int]:
        """The tiles of the layout the keys are on, or None."""
        focus = self._focus
        return self._choices[focus].cells if focus is not None else None

    def move_focus(self, delta: int) -> None:
        """←/→: the keys' layout moves; the first press starts from the one showing."""
        if not self._choices:
            return
        if self._focus is None:
            shown = [i for i, c in enumerate(self._choices) if c.cells == self._current]
            start = shown[0] if shown else 0
            self._focus = max(0, min(len(self._choices) - 1, start + delta))
        else:
            self._focus = max(0, min(len(self._choices) - 1, self._focus + delta))
        self._hint = False  # the keys use it: it waits as one opened by a click
        self._touch()
        self.update()

    def activate(self) -> None:
        """Enter: pick the keys' layout (a dimmed one says why)."""
        self._touch()
        if self._focus is not None:
            self._choose(self._focus)

    # ---- layout -------------------------------------------------------------------------

    def _units(self) -> Units:
        parent = self.parentWidget()
        return Units(parent.width() if parent is not None else _FALLBACK_HOST_WIDTH)

    @staticmethod
    def _font(un: Units) -> QFont:
        return un.font(1.05, QFont.Weight.DemiBold)

    def _picture(self, un: Units) -> float:
        return max(float(_MIN_PICTURE_PX), float(un.px(_PICTURE_U)))

    def _cell_size(self, un: Units) -> tuple[float, float]:
        fm = QFontMetricsF(self._font(un))
        picture = self._picture(un)
        names = max((fm.horizontalAdvance(c.name) for c in self._choices), default=0.0)
        width = max(picture * 0.9, names) + 2 * un.px(_CELL_PAD_U)
        height = 2 * un.px(_CELL_PAD_U) + picture * _FRAME_HEIGHT + un.px(_LABEL_GAP_U)
        return width, height + fm.height()

    def _size(self) -> tuple[int, int]:
        un = self._units()
        width, height = self._cell_size(un)
        count = max(1, len(self._choices))
        total = 2 * un.px(_PAD_U) + count * width + (count - 1) * un.px(_CELL_GAP_U)
        return math.ceil(total), math.ceil(height + 2 * un.px(_PAD_U))

    def _layout(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        un = Units(parent.width())
        width, height = self._size()
        width = min(width, parent.width())
        edge = un.px(_EDGE_U)
        bottom = (
            parent.height() - un.px(_BOTTOM_U)
            if self._floor is None
            else self._floor - un.px(_FLOOR_GAP_U)
        )
        center = self._anchor_x if self._anchor_x is not None else parent.width() / 2
        x = round(center - width / 2)
        x = max(min(x, parent.width() - width - edge), min(edge, parent.width() - width))
        rect = QRect(x, max(0, bottom - height), width, height)
        if rect != self.geometry():
            self.setGeometry(rect)

    def _cells(self) -> list[QRectF]:
        un = self._units()
        width, height = self._cell_size(un)
        pad, gap = float(un.px(_PAD_U)), float(un.px(_CELL_GAP_U))
        return [
            QRectF(pad + index * (width + gap), pad, width, height)
            for index in range(len(self._choices))
        ]

    # ---- painting -----------------------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:
        if not self._choices:
            return
        un = self._units()
        font = self._font(un)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        panel = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = un.u * 1.2
        p.setPen(QPen(qcolor(PALETTE.text, 0.12), 1.0))
        p.setBrush(qcolor(PALETTE.panel, 0.92))
        p.drawRoundedRect(panel, radius, radius)
        picture = self._picture(un)
        lit = self._hover if self._hover is not None else self._focus
        for index, (choice, rect) in enumerate(zip(self._choices, self._cells())):
            current = choice.cells == self._current
            corner = un.u * 0.8
            if current or index == lit:
                fill_round(p, rect, qcolor(PALETTE.accent, FOCUS_FILL_ALPHA), corner)
            if current or index == self._focus:
                p.save()
                p.setPen(QPen(qcolor(PALETTE.accent), max(1.0, un.u * 0.12)))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(rect, corner, corner)
                p.restore()
            if not choice.available:
                color = qcolor(PALETTE.text, DIM_ALPHA)
            else:
                color = qcolor(PALETTE.accent) if current else qcolor(PALETTE.text)
            frame_top = rect.top() + un.px(_CELL_PAD_U)
            band = frame_top + picture * _FRAME_HEIGHT / 2  # the pictures' middle
            box = QRectF(rect.center().x() - picture / 2, band - picture / 2, picture, picture)
            draw_icon(p, f"layout_{choice.cells}", box, color)
            p.setFont(font)
            p.setPen(color)
            label_top = frame_top + picture * _FRAME_HEIGHT + un.px(_LABEL_GAP_U)
            p.drawText(
                QRectF(rect.left(), label_top, rect.width(), QFontMetricsF(font).height()),
                Qt.AlignmentFlag.AlignCenter,
                choice.name,
            )
        p.end()

    # ---- input --------------------------------------------------------------------------

    def _index_at(self, pos: QPointF) -> Optional[int]:
        for index, rect in enumerate(self._cells()):
            if rect.contains(pos):
                return index
        return None

    def _choose(self, index: int) -> None:
        choice = self._choices[index]
        if not choice.available:
            self.unavailableChosen.emit(choice.cells)
            return
        self.dismiss()
        self.layoutChosen.emit(choice.cells)

    def _touch(self) -> None:
        self._idle.start(HINT_MS if self._hint else self._idle_ms)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() in NAV_BUTTONS:
            event.ignore()  # Back/Forward go on to the host: the shell's drawer layers
            return
        event.accept()  # never the host's: it would open the channel list
        self._hint = False  # touched: it waits as one opened by a click
        self._touch()
        if event.button() != Qt.MouseButton.LeftButton:
            return
        index = self._index_at(event.position())
        if index is not None:
            self._choose(index)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() in NAV_BUTTONS:
            event.ignore()
            return
        event.accept()  # a quick second click picks nothing more, nor goes fullscreen

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        hover = self._index_at(event.position())
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

    def tooltip_at(self, pos: QPointF) -> str:
        index = self._index_at(pos)
        return self._choices[index].tooltip if index is not None else ""

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
                and not self._hint
            ):
                return True  # the second press of the double-click that opened it
            hint = self._hint
            control = self._control_under(obj, event)
            self.dismiss()
            if hint:  # a hint's: what was clicked acts (the chip opens the row for good)
                return False
            # Another of the player controls' buttons acts; the chip only closes it.
            return control is None or control == self._button
        return False

    def _control_under(self, obj: QObject, event: QMouseEvent) -> Optional[str]:
        """The player controls' control under a press on them (None: elsewhere)."""
        if obj is not self._controls or self._controls_hit is None:
            return None
        return self._controls_hit(event.position())

    def _outside(self, obj: QObject) -> bool:
        """A press on another widget of this window (its overlays, the video): not on the
        row, nor in a dialog or another window."""
        if not isinstance(obj, QWidget) or obj is self or self.isAncestorOf(obj):
            return False
        host = self.window()
        ours = {host}
        outer = host.parentWidget()
        if outer is not None:
            ours.add(outer.window())
        return obj.window() in ours

    def closeEvent(self, event) -> None:
        self._stop_filtering()
        super().closeEvent(event)
