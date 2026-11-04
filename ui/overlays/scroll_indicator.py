"""A thin scrollbar over a list's right edge (2.9.3): where the list is, and a way to drag it.

QiTV's lists hide their scrollbars (the remote's lists move with the highlight), so a long list
gave no idea of where it was or how long it was. `ScrollIndicator` paints a thumb over the
list's right edge while it scrolls, and while the pointer is over the list; it fades away a
moment after. A press on the thumb drags the list, a press beside it brings that part of the
list under the pointer (the thumb's middle there). It shows only when the list is longer than
its room.

A list tells it where it is with `metrics()` ((first, shown, total), in its own steps: pixels
for a QAbstractScrollArea, rows for the guide) and is moved with `scroll_to(first)`.
`for_scroll_area` wires one to a QAbstractScrollArea's vertical scrollbar. No keys: the remote's
keys move the lists as before.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QPointF, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import QMouseEvent, QPainter
from PySide6.QtWidgets import QAbstractScrollArea, QWidget

from ui.overlays.painting import fill_round
from ui.theme import PALETTE, qcolor

WIDTH_PX = 12  # where it takes the pointer: the list's right edge
THUMB_PX = 5  # the thumb as drawn (wider while the pointer is on it)
THUMB_HOVER_PX = 8
MIN_THUMB_PX = 24
LINGER_MS = 900  # it stays this long after the list stopped moving or the pointer left
THUMB_ALPHA = 0.32
THUMB_HOVER_ALPHA = 0.6

Metrics = Callable[[], tuple[float, float, float]]


class ScrollIndicator(QWidget):
    """The thumb, over `parent`'s area set with `set_area` (the parent's coordinates)."""

    def __init__(
        self,
        parent: QWidget,
        metrics: Metrics,
        scroll_to: Callable[[float], None],
        area_of: Callable[[], QRect] | None = None,
    ) -> None:
        """`area_of()`: the room `refit` puts it over (a scroll area's viewport by default)."""
        super().__init__(parent)
        self._area_of = area_of
        self.setFocusPolicy(Qt.NoFocus)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setMouseTracking(True)
        self._metrics = metrics
        self._scroll_to = scroll_to
        self._over_list = False
        self._over_self = False
        self._drag: tuple[float, float] | None = None  # (press y, first at the press)
        self._linger = QTimer(self)
        self._linger.setSingleShot(True)
        self._linger.setInterval(LINGER_MS)
        self._linger.timeout.connect(self._maybe_hide)
        self.hide()

    # ---- what the list tells it --------------------------------------------------------------
    def set_area(self, rect: QRect) -> None:
        """The list's room in the parent's coordinates: the thumb runs down its right edge."""
        self.setGeometry(
            QRect(rect.right() - WIDTH_PX + 1, rect.top(), WIDTH_PX, max(0, rect.height()))
        )
        self.update()

    def refit(self) -> None:
        """Over a scroll area's viewport (its parent's), as it is now."""
        area = self.parentWidget()
        if self._area_of is not None:
            self.set_area(self._area_of())
        elif isinstance(area, QAbstractScrollArea):
            self.set_area(area.viewport().geometry())
        if self.isVisible() and not self.scrollable():
            self.hide()

    def on_value(self, _value: int) -> None:
        self.poke()

    def on_range(self, _low: int, _high: int) -> None:
        self.refit()

    def scrollable(self) -> bool:
        first, shown, total = self._metrics()
        return total > shown > 0 and self.height() > 0

    def poke(self) -> None:
        """The list moved: the thumb shows a moment (or while the pointer is over it)."""
        if not self.scrollable():
            self._hide_now()
            return
        self._show()
        self._linger.start()
        self.update()

    def pointer_over(self, over: bool) -> None:
        """The pointer came onto the list (or left it): the thumb shows while it is there."""
        if over == self._over_list:
            return
        self._over_list = over
        if over and self.scrollable():
            self._show()
        elif not over:
            self._linger.start()

    def thumb_rect(self) -> QRectF:
        """The thumb in this widget's coordinates (empty when the list fits its room)."""
        first, shown, total = self._metrics()
        if total <= shown or shown <= 0 or self.height() <= 0:
            return QRectF()
        track = float(self.height())
        length = max(float(MIN_THUMB_PX), track * shown / total)
        length = min(length, track)
        span = max(1.0, total - shown)
        top = (track - length) * min(1.0, max(0.0, first / span))
        width = THUMB_HOVER_PX if (self._over_self or self._drag) else THUMB_PX
        return QRectF(self.width() - width - 2, top, width, length)

    # ---- showing ---------------------------------------------------------------------------
    def _show(self) -> None:
        if self.isHidden():
            self.show()
            self.raise_()

    def _maybe_hide(self) -> None:
        if self._over_list or self._over_self or self._drag is not None:
            return
        self._hide_now()

    def _hide_now(self) -> None:
        self._linger.stop()
        self.hide()

    # ---- the pointer -------------------------------------------------------------------------
    def enterEvent(self, event) -> None:
        self._over_self = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._over_self = False
        self._linger.start()
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.LeftButton:
            event.ignore()
            return
        event.accept()
        y = event.position().y()
        thumb = self.thumb_rect()
        if thumb.isEmpty():
            return
        if not (thumb.top() <= y <= thumb.bottom()):
            # Beside the thumb: that part of the list comes under the pointer.
            self._jump_to(y - thumb.height() / 2)
        self._drag = (y, self._metrics()[0])
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        event.accept()
        if self._drag is None or not event.buttons() & Qt.LeftButton:
            return
        press_y, first = self._drag
        _first, shown, total = self._metrics()
        thumb = self.thumb_rect()
        room = self.height() - thumb.height()
        if room <= 0:
            return
        step = (total - shown) / room  # the list's steps per pixel of the thumb's travel
        wanted = first + (event.position().y() - press_y) * step
        self._scroll_to(min(max(0.0, total - shown), max(0.0, wanted)))
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        event.accept()
        self._drag = None
        self._linger.start()
        self.update()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        event.accept()  # never the list's (a row played) nor the picture's fullscreen

    def wheelEvent(self, event) -> None:
        event.ignore()  # the list under it scrolls

    def _jump_to(self, thumb_top: float) -> None:
        first, shown, total = self._metrics()
        length = self.thumb_rect().height()
        room = self.height() - length
        if room <= 0:
            return
        fraction = min(1.0, max(0.0, thumb_top / room))
        self._scroll_to(fraction * (total - shown))

    def paintEvent(self, event) -> None:
        thumb = self.thumb_rect()
        if thumb.isEmpty():
            return
        p = QPainter(self)
        lit = self._over_self or self._drag is not None
        alpha = THUMB_HOVER_ALPHA if lit else THUMB_ALPHA
        fill_round(p, thumb, qcolor(PALETTE.text, alpha), thumb.width() / 2)
        p.end()


def for_scroll_area(
    area: QAbstractScrollArea, area_of: Callable[[], QRect] | None = None
) -> ScrollIndicator:
    """A scroll indicator for `area`'s vertical scrollbar (hidden or not), over its viewport.
    The area calls its `refit()` when it is resized, and `pointer_over()` on its viewport's
    hover events (a filter on them outlived the area as it was destroyed)."""
    bar = area.verticalScrollBar()

    def metrics() -> tuple[float, float, float]:
        page = float(bar.pageStep())
        return float(bar.value()), page, float(bar.maximum() - bar.minimum()) + page

    indicator = ScrollIndicator(area, metrics, lambda first: bar.setValue(round(first)), area_of)
    bar.valueChanged.connect(indicator.on_value)
    bar.rangeChanged.connect(indicator.on_range)
    indicator.refit()
    return indicator


def point_in(indicator: ScrollIndicator | None, point: QPointF) -> bool:
    """Whether `point` (the indicator parent's coordinates) is on a shown indicator."""
    return (
        indicator is not None
        and indicator.isVisible()
        and QRectF(indicator.geometry()).contains(point)
    )
