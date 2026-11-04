"""Top-centre message pill: transient messages over an optional persistent hint.

A message or hint shows in the active language: an English text kept in a constant (marked
with services.i18n.N_) is translated here; any other text (already translated, or a name)
shows as it is.

Every message and hint is logged as it shows (2.8.2), once when both pages' toasts show it.

Assistive technology reads each message as it shows (an error at once): the toast is named
after it (ui/overlays/base.announce).

A message may carry a button (`show_message`'s `action`: an export's Cancel), which a click
answers; the toast lets clicks through otherwise, and its next message or its end takes the
button away.
"""

import logging
import time
from typing import Callable, Optional

from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QFont, QFontMetrics, QMouseEvent, QPainter, QPaintEvent
from PySide6.QtWidgets import QWidget

from services.i18n import translate
from ui.overlays.base import Overlay, announce
from ui.overlays.painting import Units, fill_round, ui_units
from ui.pointer import NAV_BUTTONS
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
        self._action = ""  # the message's button's words ("": none)
        self._on_action: Optional[Callable[[], None]] = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._message_ended)
        if parent is not None:
            parent.installEventFilter(self)
        self.hide()

    def show_message(
        self,
        text: str,
        kind: str = "info",
        ms: int = 3000,
        *,
        action: str = "",
        on_action: Optional[Callable[[], None]] = None,
    ) -> None:
        """`action` (in the active language, or marked with N_) and `on_action`: a button on
        this message, which a click on it answers (a running export's Cancel)."""
        self._set_action(action, on_action)
        self._show(text, kind)
        self._timer.start(ms)

    def action_text(self) -> str:
        """The shown message's button's words ("": it has none)."""
        return self._action

    def _set_action(self, action: str, on_action: Optional[Callable[[], None]]) -> None:
        shown = bool(action) and on_action is not None
        self._action = translate(action) if shown else ""
        self._on_action = on_action if shown else None
        # Clicks reach the toast only while it has a button; else they go on to what is under.
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, not shown)

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
            self._set_action("", None)
            self._show(text, "info")
        elif text is None and not self._timer.isActive() and not self.isHidden():
            self.hide()

    def text(self) -> str:
        """The message or hint shown last (as shown: in the active language)."""
        return self._text

    def _message_ended(self) -> None:
        self._set_action("", None)
        if self._hint is not None:
            self._show(self._hint, "info", speak=False)  # said when it first showed
        else:
            self.hide()

    def _show(self, text: str, kind: str, *, speak: bool = True) -> None:
        self._text = translate(text)
        self._kind = kind
        _log_shown(self._text, kind)
        self._layout()
        self.show()
        self.raise_()
        self.update()
        announce(self, self._text, assertive=kind == "error", speak=speak)

    def _units(self) -> Units:
        parent = self.parentWidget()
        return ui_units(parent.width() if parent is not None else 1280)

    def _font(self) -> QFont:
        return self._units().font(1.4)

    def _button_font(self) -> QFont:
        return self._units().font(1.2, QFont.Weight.DemiBold)

    def _button_width(self) -> int:
        """The button's width and the gap before it; 0 without one."""
        if not self._action:
            return 0
        units = self._units()
        label = QFontMetrics(self._button_font()).horizontalAdvance(self._action)
        return label + 2 * units.px(1.2) + units.px(0.8)

    def button_rect(self) -> QRectF:
        """The message's button, at its right end (empty without one)."""
        if not self._action:
            return QRectF()
        units = self._units()
        inset = units.px(0.4)
        width = self._button_width() - units.px(0.8)
        height = self.height() - 2 * inset
        return QRectF(self.width() - inset - width, inset, width, height)

    def _layout(self) -> None:
        units = self._units()
        metrics = QFontMetrics(self._font())
        parent = self.parentWidget()
        pad = units.px(3.2)
        button = self._button_width()
        host_w = (
            parent.width()
            if parent is not None
            else metrics.horizontalAdvance(self._text) + pad + button
        )
        max_text = max(1, host_w - 2 * units.px(2.4) - pad - button)
        self._shown = metrics.elidedText(self._text, Qt.ElideRight, max_text)
        width = metrics.horizontalAdvance(self._shown) + pad + button
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
        text_rect = QRectF(self.rect()).adjusted(0, 0, -self._button_width(), 0)
        p.drawText(text_rect, Qt.AlignCenter, self._shown)
        if self._action:
            button = self.button_rect()
            fill_round(p, button, qcolor(PALETTE.accent), button.height() / 2)
            p.setFont(self._button_font())
            p.setPen(qcolor(PALETTE.canvas))
            p.drawText(button, Qt.AlignCenter, self._action)
        p.end()

    def tooltip_at(self, pos: QPointF) -> Optional[str]:
        return self._action if self._action and self.button_rect().contains(pos) else None

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() in NAV_BUTTONS:
            event.ignore()  # Back/Forward go on to the host
            return
        event.accept()  # never the host's: it would open the channel list
        answer = self._on_action
        if (
            event.button() == Qt.MouseButton.LeftButton
            and answer is not None
            and self.button_rect().contains(event.position())
        ):
            answer()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        event.accept()  # never fullscreen

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        event.accept()
