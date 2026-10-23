"""The REC sign (2.6, Record as a PVR): recordings go on while something else is watched.

A pill in a corner of the page that shows (the player's top left, the Library's bottom left):
a red dot, REC, the recording channel and how long it has recorded ("● REC News 1:02:03"),
or how many record ("● REC 2 channels"). A click opens its menu: per recording, "Go back to
{channel}" (`goBack`) and "Stop recording" (`stop`), each naming the recording's player. The
big picture's own recording shows on its controls and the small picture's on its label: the
shell lists only the recordings in the background (`set_recordings`).
"""

from __future__ import annotations

import math
import time
from typing import Callable, Optional

from PySide6.QtCore import QEvent, QObject, QPoint, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QEnterEvent,
    QFont,
    QFontMetricsF,
    QMouseEvent,
    QPainter,
    QPaintEvent,
)
from PySide6.QtWidgets import QMenu, QWidget

from services.i18n import N_, tr_n, translate
from services.recording import clock_text
from ui.context_actions import menu_style
from ui.overlays.base import Overlay
from ui.overlays.painting import Units, elided, fill_round
from ui.overlays.pip_label import REC
from ui.stream_question import STOP_RECORDING
from ui.theme import PALETTE, qcolor

# i18n: the REC sign's menu: play the channel that records in the background again
GO_BACK = N_("Go back to {channel}")
_FALLBACK_HOST_WIDTH = 1280
_EDGE_U = 2.4
_HEIGHT_U = 3.2
_PAD_U = 1.2
_FONT_U = 1.1
_NAME_MAX_U = 16.0  # a long channel name is cut


class RecSign(Overlay):
    goBack = Signal(str)  # the recording's slot name
    stop = Signal(str)

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        bottom: bool = False,
    ) -> None:
        """`clock` is what the recordings' starts are measured on (LiveSession's);
        `bottom`: in the bottom left corner (the Library's), else the top left."""
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._clock = clock
        self._bottom = bottom
        self._recordings: list[tuple[str, str, float]] = []
        self._hover = False
        self._timer = QTimer(self, interval=1000)
        self._timer.timeout.connect(self._tick)
        if parent is not None:
            parent.installEventFilter(self)
        self.hide()

    def set_recordings(self, recordings: list[tuple[str, str, float]]) -> None:
        """(slot name, channel name, recording since) of each recording in the background,
        the oldest first; none hides the sign."""
        self._recordings = list(recordings)
        if not recordings:
            self._timer.stop()
            self.hide()
            return
        self._layout()
        self.show()
        self.raise_()
        self.update()
        self._timer.start()

    def text(self) -> str:
        """What it says, its dot aside: "REC News 01:23", or "REC 2 channels"."""
        return " ".join(self._parts())

    def menu(self) -> QMenu:
        """Its menu, for the recordings it shows (a pair of choices each)."""
        menu = QMenu(self)
        menu.setStyleSheet(menu_style())
        for index, (name, channel, _since) in enumerate(self._recordings):
            if index:
                menu.addSeparator()
            back = menu.addAction(translate(GO_BACK, channel=channel))
            back.triggered.connect(lambda _checked=False, n=name: self.goBack.emit(n))
            stop = menu.addAction(translate(STOP_RECORDING))
            stop.triggered.connect(lambda _checked=False, n=name: self.stop.emit(n))
        return menu

    def open_menu(self) -> None:
        menu = self.menu()
        menu.aboutToHide.connect(menu.deleteLater)
        below = QPoint(0, self.height() + 4)
        above = QPoint(0, -menu.sizeHint().height() - 4)
        menu.popup(self.mapToGlobal(above if self._bottom else below))

    # ---- internals ----------------------------------------------------------------------

    def _parts(self) -> list[str]:
        recordings = self._recordings
        if not recordings:
            return []
        if len(recordings) > 1:
            # i18n: the REC sign when several channels record in the background
            return [translate(REC), tr_n("{n} channel", "{n} channels", len(recordings))]
        _name, channel, since = recordings[0]
        return [translate(REC), channel, clock_text(self._clock() - since)]

    def _tick(self) -> None:
        self._layout()  # the time may grow a digit
        self.update()

    def _units(self) -> Units:
        parent = self.parentWidget()
        width = parent.width() if parent is not None else 0
        return Units(width or _FALLBACK_HOST_WIDTH)

    def _layout(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        un = self._units()
        bold = QFontMetricsF(un.font(_FONT_U, QFont.Weight.Bold))
        plain = QFontMetricsF(un.font(_FONT_U, QFont.Weight.DemiBold))
        parts = self._parts()
        width = un.px(_PAD_U) * 2 + un.px(1.0) + un.px(0.7)  # the dot and its gap
        for index, part in enumerate(parts):
            metrics = bold if index == 0 else plain
            room = math.ceil(metrics.horizontalAdvance(part)) + 1  # the painter's rounding
            if index == 1 and len(self._recordings) == 1:
                room = min(room, un.px(_NAME_MAX_U))
            width += room + (un.px(0.7) if index else 0)
        height = un.px(_HEIGHT_U)
        x = un.px(_EDGE_U)
        y = parent.height() - un.px(_EDGE_U) - height if self._bottom else un.px(_EDGE_U)
        self.setGeometry(x, y, round(width), height)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.parentWidget() and event.type() == QEvent.Type.Resize:
            if not self.isHidden():
                self._layout()
        return False

    def paintEvent(self, event: QPaintEvent) -> None:
        un = self._units()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect())
        fill_round(
            p, rect, qcolor(PALETTE.panel_raised, 0.96 if self._hover else 0.86), rect.height() / 2
        )
        x = float(un.px(_PAD_U))
        size = un.px(1.0)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(qcolor(PALETTE.live))
        p.drawEllipse(QRectF(x, (rect.height() - size) / 2, size, size))
        x += size + un.px(0.7)
        for index, part in enumerate(self._parts()):
            weight = QFont.Weight.Bold if index == 0 else QFont.Weight.DemiBold
            p.setFont(un.font(_FONT_U, weight))
            if index == 0:
                p.setPen(qcolor(PALETTE.live))
            elif index == 2:
                p.setPen(qcolor(PALETTE.text, 0.7))
            else:
                p.setPen(qcolor(PALETTE.text))
            room = rect.width() - x - un.px(_PAD_U)
            if index == 1 and len(self._recordings) == 1:
                room = min(room, un.px(_NAME_MAX_U))
            shown = elided(p, part, room)
            p.drawText(QRectF(x, 0, room, rect.height()), Qt.AlignmentFlag.AlignVCenter, shown)
            x += math.ceil(QFontMetricsF(p.font()).horizontalAdvance(shown)) + 1 + un.px(0.7)
        p.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        event.accept()
        if event.button() == Qt.MouseButton.LeftButton:
            self.open_menu()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        event.accept()

    def enterEvent(self, event: QEnterEvent) -> None:
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        self._hover = False
        self.update()
        super().leaveEvent(event)
