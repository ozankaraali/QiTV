"""The phone remote's pairing screen (2.5): a big QR over the TV, to scan from the couch.

The code in it changes every minute while the screen shows (the remote's hub sends each new
address) and pairs one phone; phones can pair only while it shows. Esc, any key, a click, or a
phone that just paired closes it. `opened` and `closed` open and close the pairing window.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QElapsedTimer, QEvent, QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QMouseEvent, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QWidget

from services.i18n import N_, translate
from services.remote.protocol import CODE_ROTATE_S
from ui.keymap import key_hint
from ui.overlays.base import Overlay
from ui.overlays.painting import Units, fill_round
from ui.theme import PALETTE, RADIUS_PANEL, qcolor

# i18n: the title of the phone remote's pairing screen over the TV
PAIR_TITLE = N_("Pair a phone")
# i18n: under the pairing QR on the TV
PAIR_HINT = N_("Scan with your phone's camera")
# i18n: under the pairing QR, small: the code is replaced every minute
PAIR_ROTATES = N_("The code changes every minute and pairs one phone")
# i18n: the pairing screen while the phone remote is starting
PAIR_STARTING = N_("Starting the phone remote…")
# i18n: the pairing screen's close hint (Esc, or a click)
PAIR_CLOSE = N_("Close")

_QUIET = 4  # modules of white around the QR, as the standard asks


class PairOverlay(Overlay):
    opened = Signal()
    closed = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._url = ""
        self._matrix: list[bytes] = []
        self._since = QElapsedTimer()
        self._since.start()
        self._tick = QTimer(self)
        self._tick.setInterval(500)
        self._tick.timeout.connect(self.update)  # the countdown ring
        if parent is not None:
            parent.installEventFilter(self)
        self.hide()

    # ---- state -----------------------------------------------------------------------------

    def open(self) -> None:
        parent = self.parentWidget()
        if parent is not None:
            self.setGeometry(parent.rect())
        was_hidden = self.isHidden()
        self.show()
        self.raise_()
        self._tick.start()
        if was_hidden:
            self.opened.emit()

    def dismiss(self) -> None:
        if self.isHidden():
            return
        self.hide()
        self._tick.stop()
        self.closed.emit()

    def set_url(self, url: str) -> None:
        if url == self._url:
            return
        self._url = url
        self._matrix = _matrix(url) if url else []
        self._since.restart()
        self.update()

    def url(self) -> str:
        return self._url

    def module_count(self) -> int:
        return len(self._matrix)

    # ---- events ----------------------------------------------------------------------------

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is self.parentWidget() and event.type() == QEvent.Type.Resize:
            parent = self.parentWidget()
            if parent is not None and not self.isHidden():
                self.setGeometry(parent.rect())
        return False

    def mousePressEvent(self, event: QMouseEvent) -> None:
        event.accept()
        self.dismiss()

    def tooltip_at(self, pos: QPointF) -> Optional[str]:
        return key_hint(translate(PAIR_CLOSE), "Esc")

    # ---- painting --------------------------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:
        un = Units(max(1, self.width()))
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        p.fillRect(self.rect(), qcolor(PALETTE.canvas, 0.9))
        side = min(self.height() * 0.46, self.width() * 0.4)
        top = self.height() * 0.22
        square = QRectF((self.width() - side) / 2, top, side, side)
        p.setPen(qcolor(PALETTE.text))
        p.setFont(un.font(2.6, QFont.Weight.DemiBold))
        title = QRectF(0, top - un.px(6.0), self.width(), un.px(4.0))
        p.drawText(title, Qt.AlignmentFlag.AlignCenter, translate(PAIR_TITLE))
        if self._matrix:
            self._paint_qr(p, square)
        else:
            fill_round(p, square, qcolor(PALETTE.panel_raised), RADIUS_PANEL)
            p.setFont(un.font(1.4))
            p.setPen(qcolor(PALETTE.text, PALETTE.text_muted_alpha))
            p.drawText(square, Qt.AlignmentFlag.AlignCenter, translate(PAIR_STARTING))
        below = square.bottom() + un.px(1.6)
        p.setPen(qcolor(PALETTE.text))
        p.setFont(un.font(1.8))
        hint = QRectF(0, below, self.width(), un.px(3.0))
        p.drawText(hint, Qt.AlignmentFlag.AlignCenter, translate(PAIR_HINT))
        p.setFont(un.font(1.2))
        p.setPen(qcolor(PALETTE.text, PALETTE.text_muted_alpha))
        small = QRectF(0, below + un.px(3.2), self.width(), un.px(2.4))
        p.drawText(small, Qt.AlignmentFlag.AlignCenter, translate(PAIR_ROTATES))
        if self._matrix:
            self._paint_ring(p, un, square)

    def _paint_qr(self, p: QPainter, square: QRectF) -> None:
        count = len(self._matrix)
        cell = square.width() / (count + 2 * _QUIET)
        fill_round(p, square, qcolor("#ffffff"), RADIUS_PANEL)
        dark = qcolor("#000000")
        origin = square.topLeft() + QPointF(_QUIET * cell, _QUIET * cell)
        for y, row in enumerate(self._matrix):
            for x, on in enumerate(row):
                if on:
                    p.fillRect(
                        QRectF(
                            origin.x() + x * cell, origin.y() + y * cell, cell + 0.5, cell + 0.5
                        ),
                        dark,
                    )

    def _paint_ring(self, p: QPainter, un: Units, square: QRectF) -> None:
        left = max(0.0, 1.0 - self._since.elapsed() / 1000.0 / CODE_ROTATE_S)
        size = un.px(2.2)
        ring = QRectF(square.right() - size, square.bottom() + un.px(1.9), size, size)
        p.setPen(QPen(qcolor(PALETTE.text, 0.18), un.px(0.3)))
        p.drawEllipse(ring)
        p.setPen(QPen(qcolor(PALETTE.accent), un.px(0.3)))
        p.drawArc(ring, 90 * 16, int(360 * 16 * left))


def _matrix(url: str) -> list[bytes]:
    """The QR's modules, row by row (1 = dark), without the quiet zone."""
    import segno

    code = segno.make(url, error="m", micro=False)
    return [bytes(row) for row in code.matrix]
