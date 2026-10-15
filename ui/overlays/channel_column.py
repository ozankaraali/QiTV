"""The fast-zap channel column (A2): a left-side list over the video."""

import logging

from PySide6.QtCore import QEvent, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QLinearGradient, QMouseEvent, QPainter, QWheelEvent
from PySide6.QtWidgets import QAbstractItemView

from ui.overlays.base import Overlay
from ui.overlays.channel_rows import ChannelListView, ChannelRowDelegate, ChannelRowsModel
from ui.overlays.painting import Units, elided
from ui.pointer import NAV_BUTTONS
from ui.theme import PALETTE, qcolor

logger = logging.getLogger(__name__)

COLUMN_FRACTION = 0.4


class ChannelColumn(Overlay):
    closed = Signal()
    rowClicked = Signal(int)

    def __init__(self, logos, now_provider, parent=None, idle_ms: int = 2500) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.NoFocus)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self._title = ""
        self._units = Units(100)
        self._model = ChannelRowsModel(logos, now_provider, self)
        self._view = ChannelListView(self)
        self._view.setModel(self._model)
        self._delegate = ChannelRowDelegate(1.0, self._view)
        self._view.setItemDelegate(self._delegate)
        self._view.rowClicked.connect(self._on_row_clicked)
        self._idle = QTimer(self)
        self._idle.setSingleShot(True)
        self._idle.setInterval(idle_ms)
        self._idle.timeout.connect(self._on_idle)
        # Hover keeps the column open once the pointer has moved over it (not a pointer
        # that was merely resting where the column opened).
        self._hovering = False
        self._pointer: QPointF | None = None
        self.setMouseTracking(True)
        self._view.viewport().setMouseTracking(True)
        self._view.viewport().installEventFilter(self)
        if parent is not None:
            parent.installEventFilter(self)
        self.hide()

    # ---- public API -------------------------------------------------------------------------

    def open(self, title: str, channels: list[dict], index: int) -> None:
        self._title = title
        self._hovering, self._pointer = False, None
        self._model.set_channels(channels)
        self._relayout()
        self.show()
        self.raise_()
        self.highlight(index)

    def current_index(self) -> int:
        return self._view.currentIndex().row()

    def highlight(self, index: int) -> None:
        if 0 <= index < self._model.rowCount():
            idx = self._model.index(index)
            self._view.setCurrentIndex(idx)
            self._view.scrollTo(idx, QAbstractItemView.PositionAtCenter)
        self._idle.start()

    def refresh_now(self) -> None:
        self._model.refresh_now()

    def close(self) -> bool:
        self._idle.stop()
        was_visible = self.isVisible()
        self.hide()
        if was_visible:
            self.closed.emit()
        return True

    # ---- internals --------------------------------------------------------------------------

    def _on_row_clicked(self, row: int) -> None:
        self._idle.start()
        self.rowClicked.emit(row)

    def eventFilter(self, obj, event):
        if obj is self.parentWidget() and event.type() == QEvent.Resize:
            self._relayout()
        elif obj is self._view.viewport() and event.type() == QEvent.Type.MouseMove:
            self._pointer_at(event.globalPosition())
        return False

    def _pointer_at(self, position: QPointF) -> None:
        if self._pointer is not None and position != self._pointer:
            self._hovering = True
            self._idle.start()
        self._pointer = position

    def _on_idle(self) -> None:
        if self._hovering and self.underMouse():
            self._idle.start()  # about to click a row
            return
        self.close()

    def leaveEvent(self, event: QEvent) -> None:
        self._hovering, self._pointer = False, None
        if self.isVisible():
            self._idle.start()
        super().leaveEvent(event)

    # ---- mouse: consumed here, so it never reaches the video behind -------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() in NAV_BUTTONS:
            event.ignore()  # Back/Forward go on to the host
            return
        event.accept()  # the header and padding: not a click on the video

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() in NAV_BUTTONS:
            event.ignore()
            return
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        self._pointer_at(event.globalPosition())
        event.accept()  # not the video's "show the transport"

    def wheelEvent(self, event: QWheelEvent) -> None:
        event.accept()  # the shell turns wheel over the column into zap steps (its filter)

    def _relayout(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        host_w, host_h = parent.width(), parent.height()
        self.setGeometry(QRect(0, 0, round(host_w * COLUMN_FRACTION), host_h))
        units = Units(host_w)
        self._units = units
        self._view.units_width = host_w
        top = units.px(2.6) + units.px(2.4) + units.px(1.0)
        side = units.px(2.4)
        self._view.setGeometry(side, top, self.width() - 2 * side, max(0, host_h - top))
        self._view.doItemsLayout()
        self._view.viewport().update()
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        grad = QLinearGradient(0, 0, self.width(), 0)
        grad.setColorAt(0.0, qcolor("rgba(6,9,13,0.94)"))
        grad.setColorAt(0.7, qcolor("rgba(6,9,13,0.94)"))
        grad.setColorAt(1.0, qcolor("rgba(6,9,13,0)"))
        p.fillRect(self.rect(), grad)  # over what is behind (a banner shows through the fade)
        units = self._units
        side = units.px(2.4)
        top = units.px(2.6)
        head = QRectF(side, top, self.width() - 2 * side, units.px(2.4))
        p.setFont(units.font(1.7, QFont.Weight.Bold))
        arrows = qcolor(PALETTE.text, 0.55)
        p.setPen(arrows)
        p.drawText(head, Qt.AlignLeft | Qt.AlignVCenter, "‹")
        p.drawText(head, Qt.AlignRight | Qt.AlignVCenter, "›")
        p.setPen(qcolor(PALETTE.text))
        p.drawText(
            head.adjusted(units.px(2.5), 0, -units.px(2.5), 0),
            Qt.AlignCenter,
            elided(p, self._title, head.width() - 2 * units.px(2.5)),
        )
