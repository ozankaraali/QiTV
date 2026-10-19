"""The sleep timer's last minute (2.3): a pill at the top centre, under the toast's line, saying
"Playback stops in 0:42", with a Keep watching button. A click anywhere on it keeps watching
(`keepWatching`), and so does Enter (the shell's keys, ui/picture_controls.py). It stays until
then, or until the timer ends; the text is the controller's, set each second.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QEvent, QObject, QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QFont, QFontMetricsF, QMouseEvent, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QWidget

from services.i18n import N_, translate
from ui.keymap import key_hint
from ui.overlays.base import Overlay
from ui.overlays.painting import Units, fill_round
from ui.pointer import NAV_BUTTONS
from ui.theme import PALETTE, qcolor

# i18n: the sleep timer's last minute: a button that turns the timer off; short
KEEP_WATCHING = N_("Keep watching")
_FALLBACK_HOST_WIDTH = 1280
_TOP_U = 7.4  # under the toast (2.4u, its pill about 4u tall)
_EDGE_U = 1.0


class SleepNotice(Overlay):
    keepWatching = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self._text = ""
        if parent is not None:
            parent.installEventFilter(self)
        self.hide()

    def show_notice(self, text: str) -> None:
        """Show `text` (in the active language), or update it in place."""
        if text != self._text or self.isHidden():
            self._text = text
            self._layout()
            self.update()
        if self.isHidden():
            self.show()
        self.raise_()

    def text(self) -> str:
        return self._text

    def button_rect(self) -> QRectF:
        """Keep watching (the whole pill keeps watching too)."""
        un = self._units()
        label = QFontMetricsF(self._fonts(un)[1]).horizontalAdvance(translate(KEEP_WATCHING))
        h = self.height() - 2 * un.px(0.6)
        width = label + 2 * un.px(1.2)
        return QRectF(self.width() - un.px(0.6) - width, un.px(0.6), width, h)

    def _units(self) -> Units:
        parent = self.parentWidget()
        return Units(parent.width() if parent is not None else _FALLBACK_HOST_WIDTH)

    @staticmethod
    def _fonts(un: Units) -> tuple[QFont, QFont]:
        return un.font(1.3), un.font(1.2, QFont.Weight.DemiBold)

    def _layout(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        un = Units(parent.width())
        text_font, button_font = self._fonts(un)
        text_w = QFontMetricsF(text_font).horizontalAdvance(self._text)
        button_w = QFontMetricsF(button_font).horizontalAdvance(translate(KEEP_WATCHING))
        height = round(QFontMetricsF(text_font).height() + un.px(2.0))
        width = round(un.px(1.6) + text_w + un.px(1.6) + button_w + 2 * un.px(1.2) + un.px(0.6))
        width = min(width, parent.width() - 2 * un.px(_EDGE_U))
        rect = QRect((parent.width() - width) // 2, un.px(_TOP_U), width, height)
        if rect != self.geometry():
            self.setGeometry(rect)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is self.parentWidget() and event.type() == QEvent.Type.Resize:
            if not self.isHidden():
                self._layout()
        return False

    def tooltip_at(self, pos: QPointF) -> str:
        return key_hint(translate(KEEP_WATCHING), "Return")

    def paintEvent(self, event: QPaintEvent) -> None:
        un = self._units()
        text_font, button_font = self._fonts(un)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = rect.height() / 2
        p.setPen(QPen(qcolor(PALETTE.accent), 1.0))
        p.setBrush(qcolor(PALETTE.panel, 0.94))
        p.drawRoundedRect(rect, radius, radius)
        button = self.button_rect()
        p.setFont(text_font)
        p.setPen(qcolor(PALETTE.text))
        room = button.left() - un.px(1.6) - un.px(0.8)
        shown = QFontMetricsF(text_font).elidedText(
            self._text, Qt.TextElideMode.ElideRight, max(0.0, room)
        )
        p.drawText(
            QRectF(un.px(1.6), 0, room, self.height()),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            shown,
        )
        fill_round(p, button, qcolor(PALETTE.accent), button.height() / 2)
        p.setFont(button_font)
        p.setPen(qcolor(PALETTE.canvas))
        p.drawText(button, Qt.AlignmentFlag.AlignCenter, translate(KEEP_WATCHING))
        p.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() in NAV_BUTTONS:
            event.ignore()  # Back/Forward go on to the host
            return
        event.accept()  # never the host's: it would open the channel list
        if event.button() == Qt.MouseButton.LeftButton:
            self.keepWatching.emit()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        event.accept()  # never fullscreen

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        event.accept()
