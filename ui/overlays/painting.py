"""Painting helpers for overlays: unit scaling, rounded fills, logos and vector icons."""

from datetime import datetime
import hashlib
import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QTransform,
)

from ui.theme import PALETTE, qcolor

MIN_FONT_PX = 11  # overlay text stays readable in the mini player (u is 3.6 px there)

_BADGE_COLORS = [
    "#2f6f7a",
    "#6a5f9e",
    "#9b6a3e",
    "#4d7a4a",
    "#a2566a",
    "#355f9a",
    "#8a4a4a",
    "#3a6e4a",
]


class Units:
    """1 unit u = 1% of the overlay host width (the mockups' cqw)."""

    def __init__(self, width: int) -> None:
        self.width = width
        self.u = width / 100.0

    def px(self, units: float) -> int:
        return max(1, round(units * self.u))

    def font(self, units: float, weight=QFont.Weight.Normal) -> QFont:
        """A font `units` tall, but never under MIN_FONT_PX (layout sizes still follow u)."""
        font = QFont()
        font.setPixelSize(max(MIN_FONT_PX, self.px(units)))
        font.setWeight(weight)
        return font


def fill_round(p: QPainter, rect: QRectF, color: QColor, radius: float) -> None:
    p.save()
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setPen(Qt.NoPen)
    p.setBrush(color)
    p.drawRoundedRect(rect, radius, radius)
    p.restore()


def draw_progress(
    p: QPainter,
    rect: QRectF,
    fraction: float,
    *,
    track_alpha: float = 0.18,
    color: QColor | None = None,
) -> None:
    fraction = min(1.0, max(0.0, fraction))
    radius = rect.height() / 2
    fill_round(p, rect, qcolor(PALETTE.text, track_alpha), radius)
    if fraction > 0:
        done = QRectF(rect.x(), rect.y(), rect.width() * fraction, rect.height())
        fill_round(p, done, color or qcolor(PALETTE.accent), radius)


def elided(p: QPainter, text: str, width: float) -> str:
    return QFontMetrics(p.font()).elidedText(text, Qt.ElideRight, max(0, int(width)))


def initials(name: str) -> str:
    words = [w for w in name.split() if w]
    if len(words) >= 2:
        return (words[0][0] + words[1][0]).upper()
    text = words[0] if words else ""
    return text[:2].upper() or "?"


def badge_color(name: str) -> QColor:
    digest = hashlib.sha1(name.encode("utf-8")).digest()
    return QColor(_BADGE_COLORS[digest[0] % len(_BADGE_COLORS)])


def draw_logo(p: QPainter, rect: QRectF, name: str, pixmap: QPixmap | None, radius: float) -> None:
    if pixmap is not None and not pixmap.isNull():
        fill_round(p, rect, qcolor(PALETTE.canvas), radius)
        p.save()
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.setClipPath(path)
        inner = rect.adjusted(
            rect.width() * 0.08, rect.height() * 0.08, -rect.width() * 0.08, -rect.height() * 0.08
        )
        scaled = pixmap.scaled(
            int(inner.width()), int(inner.height()), Qt.KeepAspectRatio, Qt.FastTransformation
        )
        p.drawPixmap(
            QPointF(
                inner.x() + (inner.width() - scaled.width()) / 2,
                inner.y() + (inner.height() - scaled.height()) / 2,
            ),
            scaled,
        )
        p.restore()
        return
    fill_round(p, rect, badge_color(name), radius)
    p.save()
    font = QFont()
    font.setPixelSize(max(1, int(rect.height() * 0.4)))
    font.setBold(True)
    p.setFont(font)
    p.setPen(qcolor(PALETTE.text))
    p.drawText(rect, Qt.AlignCenter, initials(name))
    p.restore()


def fmt_clock(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def fmt_duration(ms: int) -> str:
    seconds = max(0, int(ms // 1000))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


# ---- icons -----------------------------------------------------------------------------------


def _poly(points: list[tuple[float, float]]) -> QPainterPath:
    path = QPainterPath(QPointF(*points[0]))
    for pt in points[1:]:
        path.lineTo(QPointF(*pt))
    path.closeSubpath()
    return path


def _rrect(x: float, y: float, w: float, h: float, r: float) -> QPainterPath:
    path = QPainterPath()
    path.addRoundedRect(QRectF(x, y, w, h), r, r)
    return path


def _speaker() -> QPainterPath:
    return _poly([(0.12, 0.38), (0.30, 0.38), (0.50, 0.2), (0.50, 0.8), (0.30, 0.62), (0.12, 0.62)])


def _arc(cx: float, cy: float, r: float, start: float, span: float) -> QPainterPath:
    path = QPainterPath()
    path.arcMoveTo(QRectF(cx - r, cy - r, 2 * r, 2 * r), start)
    path.arcTo(QRectF(cx - r, cy - r, 2 * r, 2 * r), start, span)
    return path


def _skip_arrow(forward: bool) -> tuple[QPainterPath, QPainterPath]:
    """Return (stroke path, fill path) for the 10-second skip arc and arrowhead."""
    cx, cy, r = 0.5, 0.56, 0.36
    # The arc leaves a 60 degree gap at the top; the arrowhead sits at the gap edge.
    start = 60.0 if forward else 120.0
    span = -300.0 if forward else 300.0
    arc = _arc(cx, cy, r, start, span)
    end = math.radians(start + span)
    ex, ey = cx + r * math.cos(end), cy - r * math.sin(end)
    # tangent direction of travel at the end of the arc
    sign = 1.0 if span > 0 else -1.0
    tx, ty = -math.sin(end) * sign, -math.cos(end) * sign
    nx, ny = -ty, tx
    size = 0.13
    tip = (ex + tx * size, ey + ty * size)
    head = _poly(
        [
            tip,
            (ex + nx * size * 0.8, ey + ny * size * 0.8),
            (ex - nx * size * 0.8, ey - ny * size * 0.8),
        ]
    )
    return arc, head


def draw_icon(p: QPainter, name: str, rect: QRectF, color: QColor) -> None:
    p.save()
    p.setRenderHint(QPainter.Antialiasing, True)
    base = p.transform()
    p.translate(rect.x(), rect.y())
    p.scale(rect.width(), rect.height())  # unit box 0..1
    pen = QPen(color, 0.08)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    stroke = QPen(pen)
    nopen = QPen(Qt.NoPen)

    def fill(path: QPainterPath) -> None:
        p.setPen(nopen)
        p.setBrush(color)
        p.drawPath(path)

    def line(path: QPainterPath) -> None:
        p.setPen(stroke)
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)

    try:
        if name == "play":
            fill(_poly([(0.25, 0.15), (0.85, 0.5), (0.25, 0.85)]))
        elif name == "pause":
            fill(_rrect(0.22, 0.15, 0.2, 0.7, 0.05))
            fill(_rrect(0.58, 0.15, 0.2, 0.7, 0.05))
        elif name == "prev":
            fill(_rrect(0.2, 0.18, 0.1, 0.64, 0.03))
            fill(_poly([(0.8, 0.18), (0.8, 0.82), (0.35, 0.5)]))
        elif name == "next":
            fill(_rrect(0.7, 0.18, 0.1, 0.64, 0.03))
            fill(_poly([(0.2, 0.18), (0.2, 0.82), (0.65, 0.5)]))
        elif name in ("back10", "fwd10"):
            arc, head = _skip_arrow(name == "fwd10")
            line(arc)
            fill(head)
            p.setTransform(base)  # text is sized in device pixels, not the unit box
            font = QFont()
            font.setPixelSize(max(1, round(rect.height() * 0.38)))
            font.setBold(True)
            p.setFont(font)
            p.setPen(color)
            p.drawText(
                QRectF(
                    rect.x() + rect.width() * 0.15,
                    rect.y() + rect.height() * 0.3,
                    rect.width() * 0.7,
                    rect.height() * 0.5,
                ),
                Qt.AlignCenter,
                "10",
            )
        elif name == "volume":
            fill(_speaker())
            line(_arc(0.5, 0.5, 0.2, -45, 90))
            line(_arc(0.5, 0.5, 0.36, -45, 90))
        elif name == "mute":
            fill(_speaker())
            cross = QPainterPath(QPointF(0.64, 0.38))
            cross.lineTo(0.88, 0.62)
            cross.moveTo(0.88, 0.38)
            cross.lineTo(0.64, 0.62)
            line(cross)
        elif name == "fullscreen":
            brackets = QPainterPath()
            for sx, sy in ((0.15, 0.15), (0.85, 0.15), (0.15, 0.85), (0.85, 0.85)):
                dx = 0.22 if sx < 0.5 else -0.22
                dy = 0.22 if sy < 0.5 else -0.22
                brackets.moveTo(sx + dx, sy)
                brackets.lineTo(sx, sy)
                brackets.lineTo(sx, sy + dy)
            line(brackets)
        elif name == "subtitles":
            line(_rrect(0.12, 0.22, 0.76, 0.56, 0.1))
            lines = QPainterPath(QPointF(0.26, 0.45))
            lines.lineTo(0.5, 0.45)
            lines.moveTo(0.58, 0.45)
            lines.lineTo(0.74, 0.45)
            lines.moveTo(0.26, 0.6)
            lines.lineTo(0.42, 0.6)
            lines.moveTo(0.5, 0.6)
            lines.lineTo(0.74, 0.6)
            line(lines)
        elif name == "audio":
            note = QPainterPath()
            note.addEllipse(QRectF(0.2, 0.6, 0.26, 0.22))
            fill(note)
            stem = QPainterPath(QPointF(0.44, 0.7))
            stem.lineTo(0.44, 0.2)
            stem.lineTo(0.8, 0.12)
            stem.lineTo(0.8, 0.5)
            line(stem)
            flag = QPainterPath()
            flag.addEllipse(QRectF(0.58, 0.44, 0.26, 0.22))
            fill(flag)
        elif name == "search":
            circle = QPainterPath()
            circle.addEllipse(QRectF(0.14, 0.14, 0.5, 0.5))
            line(circle)
            handle = QPainterPath(QPointF(0.6, 0.6))
            handle.lineTo(0.86, 0.86)
            line(handle)
        elif name == "star":
            pts = []
            for i in range(10):
                ang = math.radians(-90 + i * 36)
                rad = 0.4 if i % 2 == 0 else 0.17
                pts.append((0.5 + rad * math.cos(ang), 0.54 + rad * math.sin(ang)))
            fill(_poly(pts))
        elif name == "recent":
            line(_arc(0.5, 0.5, 0.34, 90, 270))
            hands = QPainterPath(QPointF(0.5, 0.3))
            hands.lineTo(0.5, 0.5)
            hands.lineTo(0.64, 0.58)
            line(hands)
            fill(_poly([(0.5, 0.08), (0.5, 0.32), (0.32, 0.2)]))
        elif name == "clock":
            face = QPainterPath()
            face.addEllipse(QRectF(0.1, 0.1, 0.8, 0.8))
            line(face)
            hands = QPainterPath(QPointF(0.5, 0.25))
            hands.lineTo(0.5, 0.5)
            hands.lineTo(0.68, 0.6)
            line(hands)
        elif name == "sparkle":
            pts = []
            for i in range(8):
                ang = math.radians(-90 + i * 45)
                rad = 0.46 if i % 2 == 0 else 0.13
                pts.append((0.5 + rad * math.cos(ang), 0.5 + rad * math.sin(ang)))
            fill(_poly(pts))
        elif name == "gear":  # Settings: a cog with eight teeth around a hole
            cog = QPainterPath()
            cog.addEllipse(QRectF(0.18, 0.18, 0.64, 0.64))
            for i in range(8):
                tooth = QPainterPath()
                tooth.addRoundedRect(QRectF(0.43, 0.04, 0.14, 0.2), 0.03, 0.03)
                spin = QTransform().translate(0.5, 0.5).rotate(i * 45).translate(-0.5, -0.5)
                cog = cog.united(spin.map(tooth))
            hole = QPainterPath()
            hole.addEllipse(QRectF(0.36, 0.36, 0.28, 0.28))
            fill(cog.subtracted(hole))
        elif name == "plus":  # Add provider
            fill(_rrect(0.16, 0.43, 0.68, 0.14, 0.05))
            fill(_rrect(0.43, 0.16, 0.14, 0.68, 0.05))
        elif name == "pip":  # a second channel: a screen with a picture inset
            line(_rrect(0.08, 0.2, 0.84, 0.6, 0.08))
            fill(_rrect(0.5, 0.48, 0.32, 0.24, 0.04))
        elif name == "mini_player":  # the window small in a corner: a frame, an arrow to it
            line(_rrect(0.08, 0.2, 0.84, 0.6, 0.08))
            fill(_rrect(0.56, 0.52, 0.28, 0.2, 0.04))
            arrow = QPainterPath(QPointF(0.22, 0.32))
            arrow.lineTo(0.46, 0.56)
            line(arrow)
            fill(_poly([(0.5, 0.6), (0.34, 0.58), (0.48, 0.44)]))
        elif name == "live_dot":
            dot = QPainterPath()
            dot.addEllipse(QRectF(0.25, 0.25, 0.5, 0.5))
            fill(dot)
        else:
            raise KeyError(f"Unknown icon: {name!r}")
    finally:
        p.restore()
