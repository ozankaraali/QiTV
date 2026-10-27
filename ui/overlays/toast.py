"""Top-centre message pill: transient messages over an optional persistent hint.

A message or hint shows in the active language: an English text kept in a constant (marked
with services.i18n.N_) is translated here; any other text (already translated, or a name)
shows as it is.

Every message and hint is logged as it shows (2.8.2), once when both pages' toasts show it.
"""

import logging
import time

from PySide6.QtCore import QEvent, QObject, QRectF, Qt, QTimer
from PySide6.QtGui import QFont, QFontMetrics, QPainter, QPaintEvent
from PySide6.QtWidgets import QWidget

from services.i18n import translate
from ui.overlays.base import Overlay
from ui.overlays.painting import Units
from ui.theme import PALETTE, qcolor

logger = logging.getLogger(__name__)

_SAME_S = 1.0  # the same message again this soon (the other page's toast) is not logged again
_last_logged: tuple[str, str, float] = ("", "", 0.0)


def _log_shown(text: str, kind: str) -> None:
    """The log says what the user was shown: "Error shown: …" or "Message shown: …"."""
    global _last_logged
    now = time.monotonic()
    if (text, kind) == _last_logged[:2] and now - _last_logged[2] < _SAME_S:
        return
    _last_logged = (text, kind, now)
    logger.info("%s shown: %s", "Error" if kind == "error" else "Message", text)


class Toast(Overlay):
    stays_on_top = True  # OverlayHost keeps messages above the drawer and bars

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._text = ""
        self._shown = ""
        self._kind = "info"
        self._hint: str | None = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._message_ended)
        if parent is not None:
            parent.installEventFilter(self)
        self.hide()

    def show_message(self, text: str, kind: str = "info", ms: int = 3000) -> None:
        self._show(text, kind)
        self._timer.start(ms)

    def set_hint(self, text: str | None) -> None:
        """A message that stays up until it is cleared (None), e.g. "Loading Home…".

        Messages show over it and give it back when they end. A new hint shows at once,
        cutting a message short (it is about what came before); the same hint again, or
        None, leaves a message to end on its own.
        """
        text = text or None
        changed, self._hint = text != self._hint, text
        if text is not None and (changed or not self._timer.isActive()):
            self._timer.stop()
            self._show(text, "info")
        elif text is None and not self._timer.isActive() and not self.isHidden():
            self.hide()

    def text(self) -> str:
        """The message or hint shown last (as shown: in the active language)."""
        return self._text

    def _message_ended(self) -> None:
        if self._hint is not None:
            self._show(self._hint, "info")
        else:
            self.hide()

    def _show(self, text: str, kind: str) -> None:
        self._text = translate(text)
        self._kind = kind
        _log_shown(self._text, kind)
        self._layout()
        self.show()
        self.raise_()
        self.update()

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
        # Not isVisible(): shown while the host is hidden, the host's resize comes first.
        parent_resized = obj is self.parentWidget() and event.type() == QEvent.Type.Resize
        if parent_resized and not self.isHidden():
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
