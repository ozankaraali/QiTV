"""Statistics over the picture (2.3): the large picture's codec, size and frame rate, its pictures,
bitrates, decoding and source, and how far behind live it plays, in a small panel at the top
left, under the player controls' title row. Shift+I and the Picture panel's Statistics row show
or hide it; ui/picture_controls.py fills it once a second. It never takes a click: they reach
the picture under it. Its width only grows while it shows, so it stands still as the counts
climb.
"""

from __future__ import annotations

import math
from typing import Optional

from PySide6.QtCore import QEvent, QObject, QRect, QRectF, Qt
from PySide6.QtGui import QFont, QFontMetricsF, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QWidget

from ui.overlays.base import Overlay
from ui.overlays.painting import Units
from ui.theme import PALETTE, qcolor

_FALLBACK_HOST_WIDTH = 1280
_LEFT_U = 3.0  # as the player controls' left edge
_TOP_U = 6.8  # under their title row and its logo (2.4u to 5.7u)
_PAD_U = 1.0
_GAP_U = 1.4  # between a row's name and its value
_TEXT_U = 1.05


class StatsOverlay(Overlay):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._title = ""
        self._rows: list[tuple[str, str]] = []
        self._width = 0  # the widest it was while shown
        if parent is not None:
            parent.installEventFilter(self)
        self.hide()

    def show_rows(self, title: str, rows: list[tuple[str, str]]) -> None:
        """Show `rows` ((name, value), in the active language) under `title`. It keeps its
        place among the host's overlays (never raised): the channel list and the guide cover
        it while they show."""
        if self.isHidden() or (title, rows) != (self._title, self._rows):
            self._title, self._rows = title, list(rows)
            self._layout()
            self.update()
        if self.isHidden():
            self.show()

    def rows(self) -> list[tuple[str, str]]:
        return list(self._rows)

    def hideEvent(self, event) -> None:
        self._width = 0  # next time it starts as wide as it needs
        super().hideEvent(event)

    def _units(self) -> Units:
        parent = self.parentWidget()
        return Units(parent.width() if parent is not None else _FALLBACK_HOST_WIDTH)

    @staticmethod
    def _fonts(un: Units) -> tuple[QFont, QFont, QFont]:
        return (
            un.font(1.15, QFont.Weight.Bold),
            un.font(_TEXT_U, QFont.Weight.DemiBold),
            un.font(_TEXT_U),
        )

    def _columns(self, un: Units) -> tuple[float, float]:
        """The names' column and the values' widths."""
        _title, name_font, value_font = self._fonts(un)
        names, values = QFontMetricsF(name_font), QFontMetricsF(value_font)
        name_w = max((names.horizontalAdvance(n) for n, _v in self._rows), default=0.0)
        value_w = max((values.horizontalAdvance(v) for _n, v in self._rows), default=0.0)
        return name_w, value_w

    def _line_h(self, un: Units) -> float:
        return QFontMetricsF(self._fonts(un)[2]).height() + un.px(0.3)

    def _layout(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        un = Units(parent.width())
        title_font = self._fonts(un)[0]
        name_w, value_w = self._columns(un)
        title_w = QFontMetricsF(title_font).horizontalAdvance(self._title)
        pad = un.px(_PAD_U)
        content = max(title_w, name_w + un.px(_GAP_U) + value_w)
        left = un.px(_LEFT_U)
        width = min(math.ceil(content + 2 * pad) + 2, parent.width() - 2 * left)
        self._width = max(self._width, width) if not self.isHidden() else width
        title_h = QFontMetricsF(title_font).height() + un.px(0.4)
        height = math.ceil(2 * pad + title_h + len(self._rows) * self._line_h(un))
        rect = QRect(left, un.px(_TOP_U), max(1, min(self._width, parent.width())), height)
        if rect != self.geometry():
            self.setGeometry(rect)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is self.parentWidget() and event.type() == QEvent.Type.Resize:
            if not self.isHidden():
                self._width = 0
                self._layout()
        return False

    def paintEvent(self, event: QPaintEvent) -> None:
        un = self._units()
        title_font, name_font, value_font = self._fonts(un)
        pad = float(un.px(_PAD_U))
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        panel = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = un.u * 1.0
        p.setPen(QPen(qcolor(PALETTE.text, 0.12), 1.0))
        p.setBrush(qcolor(PALETTE.panel, 0.86))
        p.drawRoundedRect(panel, radius, radius)
        left = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        title_h = QFontMetricsF(title_font).height() + un.px(0.4)
        inner = self.width() - 2 * pad
        p.setFont(title_font)
        p.setPen(qcolor(PALETTE.text))
        p.drawText(QRectF(pad, pad, inner, title_h), left, self._title)
        name_w, _value_w = self._columns(un)
        value_x = pad + name_w + un.px(_GAP_U)
        line_h = self._line_h(un)
        values = QFontMetricsF(value_font)
        y = pad + title_h
        for name, value in self._rows:
            p.setFont(name_font)
            p.setPen(qcolor(PALETTE.text, PALETTE.text_muted_alpha))
            p.drawText(QRectF(pad, y, name_w, line_h), left, name)
            p.setFont(value_font)
            p.setPen(qcolor(PALETTE.text))
            room = max(0.0, self.width() - pad - value_x)
            shown = values.elidedText(value, Qt.TextElideMode.ElideRight, room)
            p.drawText(QRectF(value_x, y, room, line_h), left, shown)
            y += line_h
        p.end()
