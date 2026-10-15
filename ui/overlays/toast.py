"""Transient top-centre message pill."""

from PySide6.QtCore import QEvent, QObject, QRectF, Qt, QTimer
from PySide6.QtGui import QFont, QFontMetrics, QPainter, QPaintEvent
from PySide6.QtWidgets import QWidget

from ui.overlays.base import Overlay
from ui.overlays.painting import Units
from ui.theme import PALETTE, qcolor


class Toast(Overlay):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._text = ""
        self._shown = ""
        self._kind = "info"
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)
        if parent is not None:
            parent.installEventFilter(self)
        self.hide()

    def show_message(self, text: str, kind: str = "info", ms: int = 3000) -> None:
        self._text = text
        self._kind = kind
        self._layout()
        self.show()
        self.raise_()
        self.update()
        self._timer.start(ms)

    def _units(self) -> Units:
        parent = self.parentWidget()
        return Units(parent.width() if parent is not None else 1280)

    def _font(self) -> QFont:
        return self._units().font(1.4)

    def _layout(self) -> None:
        units = self._units()
        metrics = QFontMetrics(self._font())
        parent = self.parentWidget()
        pad = units.px(3.2)
        host_w = (
            parent.width() if parent is not None else metrics.horizontalAdvance(self._text) + pad
        )
        max_text = max(1, host_w - 2 * units.px(2.4) - pad)
        self._shown = metrics.elidedText(self._text, Qt.ElideRight, max_text)
        width = metrics.horizontalAdvance(self._shown) + pad
        height = metrics.height() + units.px(1.6)
        self.setGeometry((host_w - width) // 2, units.px(2.4), width, height)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is self.parentWidget() and event.type() == QEvent.Type.Resize and self.isVisible():
            self._layout()
        return False

    def paintEvent(self, event: QPaintEvent) -> None:
        units = self._units()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        border = PALETTE.live if self._kind == "error" else PALETTE.text
        radius = units.u * 1.2
        p.setBrush(qcolor(PALETTE.panel, 0.9))
        p.setPen(qcolor(border, 1.0 if self._kind == "error" else 0.12))
        p.drawRoundedRect(rect, radius, radius)
        p.setFont(self._font())
        p.setPen(qcolor(PALETTE.text))
        p.drawText(self.rect(), Qt.AlignCenter, self._shown)
        p.end()
