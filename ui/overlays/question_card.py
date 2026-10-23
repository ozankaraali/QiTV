"""A question QiTV asks before it stops something the user would miss (2.6: a recording when no
stream is free). A card at the top centre of the video, in the app's look: a red REC dot, the
question, and two buttons, the safe answer first and focused. Enter answers the focused one,
the arrows and Tab move between them, Esc and Backspace keep (answer no); a click answers
either. Left unanswered it keeps (no) after `QUESTION_MS`. The shell routes its keys here while
it shows (`handle`), and lets the sound keys and fullscreen through.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QEvent, QObject, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QFontMetricsF, QMouseEvent, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QWidget

from ui.keymap import Action
from ui.overlays.base import Overlay
from ui.overlays.painting import Units, fill_round
from ui.theme import PALETTE, RADIUS_PANEL, qcolor

QUESTION_MS = 60_000  # unanswered for a minute: the safe answer
_FALLBACK_HOST_WIDTH = 1280
_TOP_U = 7.4  # under the toast's line, as the sleep timer's pill
_WIDTH_U = 40.0
_PAD_U = 1.8
_BUTTON_H_U = 3.6
_GAP_U = 1.0
_TEXT_U = 1.35
_MOVES = (
    Action.SEEK_BACK,
    Action.SEEK_FWD,
    Action.CH_NEXT,
    Action.CH_PREV,
    Action.NAV_LEFT,
    Action.NAV_RIGHT,
    Action.NAV_UP,
    Action.NAV_DOWN,
    Action.TAB,
)
# Keys that never wait for the answer: the sound and the window.
_THROUGH = (
    Action.MUTE,
    Action.VOL_UP,
    Action.VOL_DOWN,
    Action.FULLSCREEN,
    Action.MINI_PLAYER,
)


class QuestionCard(Overlay):
    answered = Signal(bool)  # True: yes (the action that costs), False: no (the safe one)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self._question = ""
        self._yes = ""
        self._no = ""
        self._focus = "no"
        self._hover: Optional[str] = None
        self._timer = QTimer(self, singleShot=True)
        self._timer.timeout.connect(lambda: self._answer(False))
        if parent is not None:
            parent.installEventFilter(self)
        self.hide()

    # ---- public API ---------------------------------------------------------------------

    def ask(self, question: str, yes: str, no: str, *, ms: int = QUESTION_MS) -> None:
        """Show `question` (in the active language) with its answers; a question still shown
        is replaced, unanswered."""
        self._question, self._yes, self._no = question, yes, no
        self._focus = "no"
        self._layout()
        self.show()
        self.raise_()
        self.update()
        self._timer.start(max(1, ms))

    def question(self) -> str:
        return self._question

    def focused(self) -> str:
        """ "yes" or "no": the button Enter answers."""
        return self._focus

    def activate(self) -> None:
        """Enter: the focused answer."""
        if not self.isHidden():
            self._answer(self._focus == "yes")

    def escape(self) -> bool:
        """Esc: no; True when the card showed."""
        if self.isHidden():
            return False
        self._answer(False)
        return True

    def handle(self, action: Action) -> bool:
        """A key while it shows: True when it is the card's (every key but the sound's and
        the window's waits for the answer)."""
        if not self.isVisibleTo(self.window()) or action in _THROUGH:
            return False  # also when its page hid (the shell moves the question along)
        if action in (Action.OK, Action.NAV_ACTIVATE):
            self.activate()
        elif action in (Action.ESCAPE, Action.PREVIOUS, Action.NAV_BACKSPACE):
            self.escape()
        elif action in _MOVES:
            self._focus = "yes" if self._focus == "no" else "no"
            self.update()
        return True

    def button_rect(self, name: str) -> QRectF:
        """ "no" (left) or "yes" (right), in the card's coordinates."""
        un = self._units()
        pad, gap, height = un.px(_PAD_U), un.px(_GAP_U), un.px(_BUTTON_H_U)
        p_font = un.font(1.2, QFont.Weight.DemiBold)
        metrics = QFontMetricsF(p_font)
        y = self.height() - pad - height
        widths = {
            name: metrics.horizontalAdvance(text) + 2 * un.px(1.6)
            for name, text in (("no", self._no), ("yes", self._yes))
        }
        right = self.width() - pad
        if name == "yes":
            return QRectF(right - widths["yes"], y, widths["yes"], height)
        return QRectF(right - widths["yes"] - gap - widths["no"], y, widths["no"], height)

    # ---- internals ----------------------------------------------------------------------

    def _answer(self, yes: bool) -> None:
        self._timer.stop()
        self.hide()
        self.answered.emit(yes)

    def _units(self) -> Units:
        parent = self.parentWidget()
        width = parent.width() if parent is not None else 0
        return Units(width or _FALLBACK_HOST_WIDTH)

    def _text_rect(self, un: Units) -> QRectF:
        pad = un.px(_PAD_U)
        dot = un.px(2.4)
        return QRectF(pad + dot, pad, self.width() - 2 * pad - dot, 0)

    def _layout(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        un = self._units()
        width = min(un.px(_WIDTH_U), max(1, parent.width() - 2 * un.px(1.0)))
        self.resize(width, 1)  # the text's width follows from it
        text = self._text_rect(un)
        metrics = QFontMetricsF(un.font(_TEXT_U, QFont.Weight.DemiBold))
        flags = int(Qt.TextFlag.TextWordWrap)
        bound = metrics.boundingRect(QRectF(0, 0, text.width(), 10_000), flags, self._question)
        pad = un.px(_PAD_U)
        height = round(pad + bound.height() + un.px(1.4) + un.px(_BUTTON_H_U) + pad)
        self.setGeometry((parent.width() - width) // 2, un.px(_TOP_U), width, height)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.parentWidget() and event.type() == QEvent.Type.Resize:
            if not self.isHidden():
                self._layout()
        return False

    # ---- painting -----------------------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:
        un = self._units()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        radius = float(RADIUS_PANEL)
        rect = QRectF(self.rect())
        fill_round(p, rect, qcolor(PALETTE.panel_raised, 0.97), radius)
        p.setPen(QPen(qcolor(PALETTE.live, 0.55), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
        pad = un.px(_PAD_U)
        font = un.font(_TEXT_U, QFont.Weight.DemiBold)
        line = QFontMetricsF(font).height()
        size = un.px(1.0)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(qcolor(PALETTE.live))
        p.drawEllipse(QRectF(pad, pad + (line - size) / 2, size, size))  # REC's dot
        text = self._text_rect(un)
        text.setHeight(self.height() - text.top() - pad - un.px(_BUTTON_H_U))
        p.setFont(font)
        p.setPen(qcolor(PALETTE.text))
        p.drawText(text, int(Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignLeft), self._question)
        self._paint_button(p, un, "no", self._no)
        self._paint_button(p, un, "yes", self._yes)
        p.end()

    def _paint_button(self, p: QPainter, un: Units, name: str, text: str) -> None:
        r = self.button_rect(name)
        focused = self._focus == name
        if focused:
            fill = qcolor(PALETTE.accent)
        else:
            fill = qcolor(PALETTE.text, 0.22 if self._hover == name else 0.14)
        fill_round(p, r, fill, r.height() / 2)
        p.setFont(un.font(1.2, QFont.Weight.DemiBold))
        if focused:
            p.setPen(qcolor(PALETTE.canvas))
        else:
            p.setPen(qcolor(PALETTE.live) if name == "yes" else qcolor(PALETTE.text))
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, text)

    # ---- the mouse: the buttons answer; nothing reaches the video below ----------------

    def _button_at(self, event: QMouseEvent) -> Optional[str]:
        pos = event.position()
        for name in ("no", "yes"):
            if self.button_rect(name).contains(pos):
                return name
        return None

    def mousePressEvent(self, event: QMouseEvent) -> None:
        event.accept()
        if event.button() == Qt.MouseButton.LeftButton:
            name = self._button_at(event)
            if name is not None:
                self._answer(name == "yes")

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        event.accept()
        hover = self._button_at(event)
        if hover != self._hover:
            self._hover = hover
            self.setCursor(
                Qt.CursorShape.PointingHandCursor if hover else Qt.CursorShape.ArrowCursor
            )
            self.update()

    def leaveEvent(self, event: QEvent) -> None:
        self._hover = None
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        event.accept()
