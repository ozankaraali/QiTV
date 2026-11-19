"""The drawer's look, shared: its column metrics, the two columns' backdrop with the fade at
the right edge, a row's highlight and the ‹ back chevron. The Settings overlay takes the same
look from here, so the two read as one."""

from PySide6.QtCore import QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen

from ui.overlays.painting import Units, fill_round
from ui.theme import FOCUS_FILL_ALPHA, PALETTE, qcolor

LEFT_U = 25.0  # the left column (2.9.3: in u; a quarter of the width up to UNIT_CAP_PX)
RIGHT_U = 58.0  # the right column's right edge
FADE_U = 7.0
PAD_TOP_U = 2.4
PAD_SIDE_U = 1.6
CRUMB_H_U = 2.2
CRUMB_FONT_U = 1.15
ROW_H_U = 3.6  # a simple row's height
TITLE_FONT_U = 1.35  # a row's title
COUNT_FONT_U = 1.1  # a row's count or label


def paint_backdrop(p: QPainter, rect: QRect, units: Units) -> None:
    """The left column's raised panel, the right column's panel, and the fade into the video
    at the right edge, over `rect` (the drawer: both columns and the fade wide)."""
    left_w = round(LEFT_U * units.u)
    right_w = round(RIGHT_U * units.u) - left_w
    left = QRect(rect.x(), rect.y(), left_w, rect.height())
    right = QRect(left.right() + 1, rect.y(), right_w, rect.height())
    p.fillRect(left, qcolor(PALETTE.panel_raised, 0.95))
    p.fillRect(right, qcolor(PALETTE.panel))
    fade = QRect(right.right() + 1, rect.y(), rect.right() - right.right(), rect.height())
    grad = QLinearGradient(fade.left(), 0, fade.right(), 0)
    grad.setColorAt(0.0, qcolor(PALETTE.panel))
    grad.setColorAt(1.0, qcolor(PALETTE.panel, 0.0))
    p.fillRect(fade, grad)


def paint_highlight(p: QPainter, rect: QRectF, units: Units, focused: bool) -> None:
    """A row's highlight: the accent, stronger in the focused column."""
    u = units.u
    box = rect.adjusted(0.3 * u, 0.2 * u, -0.3 * u, -0.2 * u)
    fill_round(p, box, qcolor(PALETTE.accent, FOCUS_FILL_ALPHA if focused else 0.1), 1.0 * u)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setPen(qcolor(PALETTE.accent, 0.7 if focused else 0.35))
    p.setBrush(Qt.NoBrush)
    p.drawRoundedRect(box.adjusted(0.5, 0.5, -0.5, -0.5), 1.0 * u, 1.0 * u)


def draw_back(p: QPainter, rect: QRectF, color: QColor) -> None:
    """‹: a chevron, drawn like the refresh arrow (a unit box, round caps)."""
    p.save()
    p.setRenderHint(QPainter.Antialiasing, True)
    p.translate(rect.x(), rect.y())
    p.scale(rect.width(), rect.height())
    pen = QPen(color, 0.12)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    chevron = QPainterPath(QPointF(0.62, 0.2))
    chevron.lineTo(0.34, 0.5)
    chevron.lineTo(0.62, 0.8)
    p.drawPath(chevron)
    p.restore()
