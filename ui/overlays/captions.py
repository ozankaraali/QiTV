"""AI captions over the picture (2.9): the words being spoken, at the bottom, in step.

ui/captions_controller.py shows each caption (`show_text`) as its words start and clears it
(`clear`) after. It sits at the bottom centre, BOTTOM_U above the bottom, or just above the
player controls while they show (`follow`: the transport's controls_top() and bandChanged, as
the Sound panel does). Its text is the palette's text colour on a translucent panel box,
word-wrapped and centred, two lines at most, of about one length (2.9, the owner: a full top
line over a short one stretched the box from edge to edge): a longer caption's font shrinks a
little, and past that its second line ends in "…".

Its size follows the picture: FONT_U units of a 16:9 picture as wide as the window, or as tall
in a window wider than 16:9 (never under MIN_FONT_PX: the mini player's stays readable), so it
reads alike in a small window and at fullscreen. Clicks go through it to the picture.
"""

from __future__ import annotations

import sys
from typing import Optional

from PySide6.QtCore import QEvent, QObject, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import (
    QFont,
    QFontInfo,
    QFontMetricsF,
    QPainter,
    QPaintEvent,
    QTextLayout,
    QTextOption,
)
from PySide6.QtWidgets import QWidget

from services.i18n import N_
from ui.overlays.base import Overlay
from ui.overlays.painting import Units, fill_round
from ui.theme import PALETTE, qcolor

# i18n: what a screen reader calls the captions over the picture
ACCESSIBLE_NAME = N_("AI captions")
FONT_U = 3.2  # the text's size, in units of a 16:9 picture's width
MAX_LINES = 2
BOTTOM_U = 4.0  # from the bottom, without the player controls
FLOOR_GAP_U = 1.0  # above the player controls
BOX_ALPHA = 0.78
_SHRINK = (1.0, 0.9, 0.8)  # a caption longer than two lines shrinks this far, then is cut
_LINE_U = 76.0  # the longest line
_EDGE_U = 2.0  # from the window's sides
_PAD_X_U, _PAD_Y_U = 1.3, 0.7
_RADIUS_U = 0.8
_EVENTS = (QEvent.Type.Show, QEvent.Type.Hide, QEvent.Type.Resize)


# One family for the captions and the subtitles libVLC draws (2.9, the owner's: on par). A
# Mac's system font is hidden from libVLC under Qt's name (".AppleSystemUIFont": it found a
# serif), and asked for as "System Font" it hung libVLC's text renderer as it opened (the
# packaged app on macOS 26, libVLC 3.0.24): there both use libVLC's own, Helvetica Neue.
CAPTION_FAMILY = "Helvetica Neue" if sys.platform == "darwin" else ""  # "": Qt's own


def subtitle_font() -> str:
    """The family for the subtitles libVLC draws (services.playback.subtitle_args), the
    captions' own; "": libVLC's own (a Mac's, which is the captions' there)."""
    if CAPTION_FAMILY:
        return ""  # libVLC's default family is it: no name to look up
    family = QFontInfo(QFont()).family()
    return "" if family.startswith(".") else family


def picture_units(host: QWidget) -> Units:
    """Units of a 16:9 picture as wide as `host`, or as tall when `host` is wider than 16:9."""
    width = host.width()
    if host.height() > 0:
        width = min(width, round(host.height() * 16 / 9))
    return Units(max(1, width))


def balanced(text: str, font: QFont, width: float) -> float:
    """The narrowest width up to `width` at which `text` wraps into as few lines as at `width`:
    two lines of about one length rather than a full one over a few words."""
    lines = len(wrap(text, font, width))
    if lines < 2:
        return width
    narrow, wide = width / lines, width
    while wide - narrow > 2.0:
        middle = (narrow + wide) / 2
        if len(wrap(text, font, middle)) <= lines:
            wide = middle
        else:
            narrow = middle
    return wide


def wrap(text: str, font: QFont, width: float) -> list[tuple[int, int]]:
    """Where each line of `text` starts and how long it is, word-wrapped at `width` (a word
    too long for a line, or a language written without spaces, breaks where it must)."""
    layout = QTextLayout(text, font)
    option = QTextOption()
    option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    layout.setTextOption(option)
    lines: list[tuple[int, int]] = []
    layout.beginLayout()
    while True:
        line = layout.createLine()
        if not line.isValid():
            break
        line.setLineWidth(width)
        lines.append((line.textStart(), line.textLength()))
    layout.endLayout()
    return lines


class CaptionsOverlay(Overlay):
    accessible_name = ACCESSIBLE_NAME

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)  # click-through
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._text = ""
        self._lines: list[str] = []
        self._font = QFont()
        self._floor: Optional[int] = None  # the player controls' top while they show
        self._transport: Optional[QWidget] = None
        if parent is not None:
            parent.installEventFilter(self)
        self.hide()

    # ---- public API ---------------------------------------------------------------------

    def show_text(self, text: str) -> None:
        """Show the caption `text` (its lines as one; "" clears it)."""
        text = " ".join(str(text).split())
        if not text:
            self.clear()
            return
        if text != self._text:
            self._text = text
            self._layout()
            self.update()
        if self.isHidden():
            self.show()

    def clear(self) -> None:
        self._text, self._lines = "", []
        self.hide()

    def text(self) -> str:
        return self._text

    def lines(self) -> list[str]:
        """The lines shown (two at most; a cut one ends in "…")."""
        return list(self._lines)

    def font_px(self) -> int:
        return self._font.pixelSize()

    def follow(self, transport) -> None:
        """Sit above `transport`'s controls while it shows (its controls_top(), bandChanged)."""
        self._transport = transport
        transport.installEventFilter(self)
        transport.bandChanged.connect(self._follow_transport)
        self._follow_transport()

    def set_floor(self, top: Optional[int]) -> None:
        """Sit just above `top` (host coordinates), or with None near the bottom."""
        if top != self._floor:
            self._floor = top
            if self._text:
                self._layout()

    # ---- layout -------------------------------------------------------------------------

    def _follow_transport(self) -> None:
        transport = self._transport
        if transport is not None:
            self.set_floor(transport.controls_top() if not transport.isHidden() else None)

    def _layout(self) -> None:
        host = self.parentWidget()
        if host is None or not self._text:
            return
        un = picture_units(host)
        pad_x, pad_y = un.px(_PAD_X_U), un.px(_PAD_Y_U)
        room = max(1, host.width() - 2 * un.px(_EDGE_U))
        width = float(max(1, min(un.px(_LINE_U), room) - 2 * pad_x))
        text = self._text
        for scale in _SHRINK:
            font = un.font(FONT_U * scale)  # regular, as libVLC's subtitles (no medium there)
            if CAPTION_FAMILY:
                font.setFamily(CAPTION_FAMILY)
            spans = wrap(text, font, width)
            if len(spans) <= MAX_LINES:
                spans = wrap(text, font, balanced(text, font, width))
                break
        fm = QFontMetricsF(font)
        lines = [text[start : start + length].strip() for start, length in spans[:MAX_LINES]]
        if len(spans) > MAX_LINES:  # still too long: the second line ends in "…"
            rest = text[spans[MAX_LINES - 1][0] :].strip()
            lines[-1] = fm.elidedText(rest, Qt.TextElideMode.ElideRight, width)
        self._font, self._lines = font, lines
        line_w = max(fm.horizontalAdvance(line) for line in lines)
        box_w = min(room, round(line_w + 2 * pad_x) + 2)
        box_h = round(len(lines) * fm.lineSpacing() + 2 * pad_y)
        bottom = (
            host.height() - un.px(BOTTOM_U)
            if self._floor is None
            else self._floor - un.px(FLOOR_GAP_U)
        )
        rect = QRect(round((host.width() - box_w) / 2), max(0, bottom - box_h), box_w, box_h)
        if rect != self.geometry():
            self.setGeometry(rect)

    # ---- painting -----------------------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:
        if not self._lines:
            return
        host = self.parentWidget()
        un = picture_units(host) if host is not None else Units(self.width())
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        fill_round(p, QRectF(self.rect()), qcolor(PALETTE.panel, BOX_ALPHA), un.u * _RADIUS_U)
        p.setFont(self._font)
        p.setPen(qcolor(PALETTE.text))
        fm = QFontMetricsF(self._font)
        y = (self.height() - len(self._lines) * fm.lineSpacing()) / 2
        for line in self._lines:
            rect = QRectF(QPointF(0, y), QPointF(self.width(), y + fm.lineSpacing()))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, line)
            y += fm.lineSpacing()
        p.end()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        kind = event.type()
        if watched is self._transport and kind in _EVENTS:
            self._follow_transport()
        elif watched is self.parentWidget() and kind == QEvent.Type.Resize and self._text:
            self._layout()
        return False
