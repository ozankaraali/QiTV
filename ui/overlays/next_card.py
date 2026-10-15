"""Next-episode card: countdown with Play now / Cancel, bottom-right over the video."""

import logging

from PySide6.QtCore import QEvent, QObject, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QMouseEvent, QPainter, QPaintEvent, QRegion
from PySide6.QtWidgets import QWidget

from ui.overlays.base import Overlay
from ui.overlays.painting import Units, draw_progress, elided, fill_round
from ui.theme import PALETTE, RADIUS_PANEL, qcolor

logger = logging.getLogger(__name__)


class NextCard(Overlay):
    playNow = Signal()
    cancelled = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self._title = ""
        self._subtitle = ""
        self._total = 10
        self._remaining = 0
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        if parent is not None:
            parent.installEventFilter(self)
        self.hide()

    # ---- public API ---------------------------------------------------------------------

    def show_next(self, title: str, subtitle: str, seconds: int = 10) -> None:
        self._title = title
        self._subtitle = subtitle
        self._total = max(1, seconds)
        self._remaining = self._total
        self._place()
        self.show()
        self.raise_()
        self.update()
        self._timer.start()

    def activate(self) -> None:
        """Play now."""
        if self.isHidden():
            return
        self._finish()
        self.playNow.emit()

    def escape(self) -> bool:
        """Cancel; True when the card was visible."""
        if self.isHidden():
            return False
        self._finish()
        self.cancelled.emit()
        return True

    def remaining(self) -> int:
        return self._remaining

    # ---- internals ----------------------------------------------------------------------

    def _finish(self) -> None:
        self._timer.stop()
        self.hide()

    def _tick(self) -> None:
        self._remaining -= 1
        if self._remaining <= 0:
            self._remaining = 0
            self.activate()
            return
        self.update()

    def hideEvent(self, event) -> None:
        self._timer.stop()
        super().hideEvent(event)

    def _units(self) -> Units:
        return Units(self.width() if self.width() > 1 else 1280)

    def _host_size(self) -> tuple[int, int]:
        parent = self.parentWidget()
        if parent is not None:
            return parent.width(), parent.height()
        return self.width(), self.height()

    def _place(self) -> None:
        host_w, host_h = self._host_size()
        un = Units(host_w)
        width = un.px(30)
        height = un.px(14.5)
        self.setGeometry(host_w - un.px(2.4) - width, host_h - un.px(14) - height, width, height)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is self.parentWidget() and event.type() == QEvent.Type.Resize:
            if not self.isHidden():
                self._place()
        return False

    def body_rect(self) -> QRectF:
        return QRectF(self.rect())

    def mask_region(self) -> QRegion:
        return QRegion(self.geometry())

    def button_rect(self, name: str) -> QRectF:
        un = Units(self._host_size()[0] or 1280)
        pad = un.px(1.6)
        height = un.px(3.6)
        y = self.height() - un.px(1.8) - height - un.px(0.6)
        gap = un.px(1.0)
        play_w = un.px(11.5)
        cancel_w = un.px(7.5)
        if name == "play":
            return QRectF(pad, y, play_w, height)
        return QRectF(pad + play_w + gap, y, cancel_w, height)

    # ---- painting -----------------------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:
        un = Units(self._host_size()[0] or 1280)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        radius = float(RADIUS_PANEL)
        rect = QRectF(self.rect())
        fill_round(p, rect, qcolor(PALETTE.panel_raised, 0.96), radius)
        p.setPen(qcolor(PALETTE.text, 0.12))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
        pad = un.px(1.6)
        inner = self.width() - 2 * pad
        label = f"Next episode in {self._remaining} s"
        p.setFont(un.font(1.0))
        p.setPen(qcolor(PALETTE.text, 0.6))
        p.drawText(QRectF(pad, un.px(1.2), inner, un.px(1.6)), Qt.AlignVCenter, label)
        p.setFont(un.font(1.4, QFont.Weight.Bold))
        p.setPen(qcolor(PALETTE.text))
        p.drawText(
            QRectF(pad, un.px(2.9), inner, un.px(2.2)),
            Qt.AlignVCenter,
            elided(p, self._title, inner),
        )
        p.setFont(un.font(1.05))
        p.setPen(qcolor(PALETTE.text, 0.7))
        p.drawText(
            QRectF(pad, un.px(5.1), inner, un.px(1.8)),
            Qt.AlignVCenter,
            elided(p, self._subtitle, inner),
        )
        self._paint_button(p, un, "play", "▶ Play now", True)
        self._paint_button(p, un, "cancel", "Cancel", False)
        bar_h = max(2.0, un.px(0.35))
        frac = self._remaining / self._total
        p.save()
        p.setClipRect(QRectF(0, self.height() - bar_h, self.width(), bar_h))
        draw_progress(
            p,
            QRectF(0, self.height() - bar_h, self.width() * 1.0, bar_h * 2),
            frac,
            track_alpha=0.0,
        )
        p.restore()
        p.end()

    def _paint_button(self, p: QPainter, un: Units, name: str, text: str, accent: bool) -> None:
        r = self.button_rect(name)
        fill = qcolor(PALETTE.accent) if accent else qcolor(PALETTE.text, 0.14)
        fill_round(p, r, fill, r.height() / 2)
        p.setFont(un.font(1.2, QFont.Weight.DemiBold))
        p.setPen(qcolor(PALETTE.canvas) if accent else qcolor(PALETTE.text))
        p.drawText(r, Qt.AlignCenter, text)

    # ---- input: consume everything so nothing reaches the host below ---------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:
        event.accept()
        if event.button() != Qt.LeftButton:
            return
        pos = event.position()
        if self.button_rect("play").contains(pos):
            self.activate()
        elif self.button_rect("cancel").contains(pos):
            self.escape()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        event.accept()
