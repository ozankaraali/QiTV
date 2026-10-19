"""Draw the macOS disk image's window background: assets/dmg/background.png and @2x.

    QT_QPA_PLATFORM=offscreen uv run python scripts/draw_dmg_background.py
    QT_QPA_PLATFORM=offscreen uv run python scripts/draw_dmg_background.py --preview DIR

QiTV's dark canvas and accent (ui/theme.py), a line saying what to do, a card where each icon
stands (scripts/make_dmg.py's layout) and an arrow from QiTV's card to Applications'. Finder
writes each icon's name under it in black (light mode) or white (dark mode) and the picture
cannot follow, so a mid-grey plate lies under each name that both colours read on.

Drawn on a Mac, so that the line is in the system's font. --preview also draws the window as
Finder should show it, light and dark, with the icons and their names, into DIR.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Mapping, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QBrush,
    QColor,
    QFont,
    QFontDatabase,
    QGuiApplication,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)

from scripts.make_dmg import (  # noqa: E402
    APP_NAME,
    APPLICATIONS,
    BACKGROUND,
    ICON_CENTRES,
    ICON_SIZE,
    TEXT_SIZE,
    VOLUME_NAME,
    WINDOW_SIZE,
)
from ui.theme import PALETTE, RADIUS_PANEL, qcolor  # noqa: E402

HEADING = "Drag QiTV to Applications"
# Relative luminance 0.18: black and white names both read on it at 4.5:1.
NAME_PLATE = "#6e7580"
_DOTS_PER_METER_1X = 2835  # 72 dpi; the @2x is 144 dpi, so both are the same size in points
_FONTS = (".AppleSystemUIFont", "Helvetica Neue", "Helvetica")
_APPLICATIONS_ICON = Path(
    "/System/Library/CoreServices/CoreTypes.bundle/Contents/Resources/ApplicationsFolderIcon.icns"
)


def card_rect(centre: tuple[float, float]) -> QRectF:
    """The card an icon and its name stand on."""
    x, y = centre
    return QRectF(x - 92, y - 84, 184, 192)


def plate_rect(centre: tuple[float, float]) -> QRectF:
    """The plate under an icon's name: Finder writes the name just under the icon, a line of
    about 17 points, and the plate leaves it room to sit a few points higher or lower."""
    x, y = centre
    return QRectF(x - 60, y + ICON_SIZE / 2 - 2, 120, 32)


def arrow_ends(centres: Mapping[str, tuple[float, float]]) -> tuple[QPointF, QPointF]:
    """The arrow's tail and tip, between the two cards."""
    app, applications = centres[APP_NAME], centres[APPLICATIONS]
    tail = QPointF(card_rect(app).right() + 20, app[1])
    tip = QPointF(card_rect(applications).left() - 20, applications[1])
    return tail, tip


def heading_rect() -> QRectF:
    width = WINDOW_SIZE[0]
    return QRectF(0, 52, width, 36)


def _font(pixel_size: float, weight: QFont.Weight) -> QFont:
    families = set(QFontDatabase.families())
    font = QFont()
    for family in _FONTS:
        if family in families:
            font = QFont(family)
            break
    font.setPixelSize(round(pixel_size))
    font.setWeight(weight)
    return font


def _paint_background(painter: QPainter) -> None:
    width, height = WINDOW_SIZE
    painter.fillRect(QRectF(0, 0, width, height), qcolor(PALETTE.canvas))
    tail, tip = arrow_ends(ICON_CENTRES)
    glow = QRadialGradient((tail + tip) / 2, width * 0.55)
    glow.setColorAt(0.0, qcolor(PALETTE.accent, 0.10))
    glow.setColorAt(1.0, qcolor(PALETTE.accent, 0.0))
    painter.fillRect(QRectF(0, 0, width, height), QBrush(glow))

    painter.setPen(qcolor(PALETTE.text, 0.88))
    painter.setFont(_font(17, QFont.Weight.DemiBold))
    painter.drawText(heading_rect(), Qt.AlignmentFlag.AlignCenter, HEADING)

    for name, centre in ICON_CENTRES.items():
        card = card_rect(centre)
        painter.setBrush(qcolor(PALETTE.text, 0.045))
        if name == APPLICATIONS:  # where QiTV goes: an accent outline, dashed
            pen = QPen(qcolor(PALETTE.accent, 0.55), 1.5, Qt.PenStyle.DashLine)
            pen.setDashPattern([4, 3])
        else:
            pen = QPen(qcolor(PALETTE.text, 0.12), 1.0)
        painter.setPen(pen)
        painter.drawRoundedRect(card.adjusted(0.75, 0.75, -0.75, -0.75), RADIUS_PANEL, RADIUS_PANEL)
        plate = plate_rect(centre)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(qcolor(NAME_PLATE))
        painter.drawRoundedRect(plate, plate.height() / 2, plate.height() / 2)

    _paint_arrow(painter, tail, tip)


def _paint_arrow(painter: QPainter, tail: QPointF, tip: QPointF) -> None:
    head = 14.0
    pen = QPen(qcolor(PALETTE.accent), 5.0)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawLine(tail, tip - QPointF(2.5, 0))
    chevron = QPainterPath(tip + QPointF(-head, -head))
    chevron.lineTo(tip)
    chevron.lineTo(tip + QPointF(-head, head))
    painter.drawPath(chevron)


def draw(scale: int) -> QImage:
    """The background at `scale` (1 or 2) times the window's size in points."""
    width, height = WINDOW_SIZE
    image = QImage(width * scale, height * scale, QImage.Format.Format_RGB32)
    dots = _DOTS_PER_METER_1X * scale
    image.setDotsPerMeterX(dots)
    image.setDotsPerMeterY(dots)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    painter.scale(scale, scale)
    _paint_background(painter)
    painter.end()
    return image


def _icon(name: str) -> QImage:
    if name == APP_NAME:
        return QImage(str(ROOT / "assets" / "qitv.png"))
    image = QImage(str(_APPLICATIONS_ICON)) if _APPLICATIONS_ICON.is_file() else QImage()
    if image.isNull():  # not on a Mac: a stand-in folder
        image = QImage(256, 256, QImage.Format.Format_ARGB32)
        image.fill(QColor(0, 0, 0, 0))
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor("#5aa9f0"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(QRectF(16, 48, 224, 176), 20, 20)
        painter.end()
    return image


def preview(
    background: QImage,
    centres: Mapping[str, tuple[float, float]] = ICON_CENTRES,
    window: tuple[int, int] = WINDOW_SIZE,
    icon_size: float = ICON_SIZE,
    text_size: float = TEXT_SIZE,
    dark: bool = True,
    title: str = VOLUME_NAME,
) -> QImage:
    """The window as Finder should show it (an approximation of its title bar and names), at
    2x: `background` drawn from the top left of the window's content, as Finder does."""
    scale, bar = 2, 28
    width, height = window
    image = QImage(width * scale, (height + bar) * scale, QImage.Format.Format_ARGB32)
    image.fill(QColor(0, 0, 0, 0))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    painter.scale(scale, scale)
    frame = QPainterPath()
    frame.addRoundedRect(QRectF(0, 0, width, height + bar), 10, 10)
    painter.setClipPath(frame)
    painter.fillRect(QRectF(0, 0, width, bar), QColor("#2b2b2d" if dark else "#e8e6e8"))
    for i, colour in enumerate(("#ff5f57", "#febc2e", "#28c840")):
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(colour))
        painter.drawEllipse(QPointF(20 + 20 * i, bar / 2), 6, 6)
    painter.setPen(QColor("#e5e5e7" if dark else "#3a3a3c"))
    painter.setFont(_font(13, QFont.Weight.DemiBold))
    painter.drawText(QRectF(0, 0, width, bar), Qt.AlignmentFlag.AlignCenter, title)
    painter.translate(0, bar)
    painter.setClipRect(QRectF(0, 0, width, height))
    painter.fillRect(QRectF(0, 0, width, height), QColor("white"))  # beyond the picture
    target = QRectF(0, 0, background.width(), background.height())
    if background.dotsPerMeterX() > _DOTS_PER_METER_1X * 1.5:
        target = QRectF(0, 0, background.width() / 2, background.height() / 2)
    painter.drawImage(target, background)
    font = _font(text_size, QFont.Weight.Normal)
    painter.setFont(font)
    for name, (x, y) in centres.items():
        icon_rect = QRectF(x - icon_size / 2, y - icon_size / 2, icon_size, icon_size)
        painter.drawImage(icon_rect, _icon(name))
        label = name.removesuffix(".app")
        painter.setPen(QColor("white") if dark else QColor("black"))
        painter.drawText(
            QRectF(x - 80, icon_rect.bottom() + 3, 160, text_size * 1.4),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
            label,
        )
    painter.end()
    return image


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--preview", type=Path, help="also draw the window into this folder")
    args = parser.parse_args(argv)
    app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])  # the fonts need one
    BACKGROUND.parent.mkdir(parents=True, exist_ok=True)
    retina = BACKGROUND.with_name(f"{BACKGROUND.stem}@2x{BACKGROUND.suffix}")
    for scale, path in ((1, BACKGROUND), (2, retina)):
        if not draw(scale).save(str(path)):
            raise SystemExit(f"could not write {path}")
        print(f"wrote {path.relative_to(ROOT)}")
    if args.preview:
        args.preview.mkdir(parents=True, exist_ok=True)
        background = QImage(str(retina))
        for dark in (False, True):
            path = args.preview / f"window-{'dark' if dark else 'light'}.png"
            preview(background, dark=dark).save(str(path))
            print(f"wrote {path}")
    del app
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
