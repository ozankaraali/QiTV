"""The audio dock's "⋯" menu (Plan 4): the player controls the dock has no button for.

The audio screen's dock (ui/overlays/transport.py's audio mode) keeps the song's own controls
(play, the tracks, shuffle, repeat, the volume, Words); the rest are here, one row each: Stop,
the Sound panel, the Subtitles panel (Words' language and Translate to), the Picture panel (the
sleep timer), fullscreen, the mini player, the Library and Search. Each row reads as its
button's tooltip does (what it does, and its key: fullscreen's and the mini player's say the
way back while they show) and asks for what its button does: it emits the transport's own
signal, so whatever is wired to the button acts, and nothing new is wired for the menu.

It opens above "⋯" (`open_at`, the point moreRequested gives), lit while it shows. One click on
a row closes it, then the row acts. It closes as the multiview's layouts do
(ui/overlays/layout_row.py): a click anywhere else in the window closes it and does nothing
more, but on another of the dock's buttons it closes it and that button acts; "⋯" itself only
closes it. The mouse's Back and Forward buttons go on to the shell. The keys reach it through
the shell while it shows (`handle`): Esc closes it, and only it; any other key closes it and
goes on. It goes with its dock: when the transport leaves audio mode or hides. It is sized in
the dock's unit (capped by a 16:9 page as tall as the window): as many rows as fit above "⋯"
show, and in a short window the rest scroll (the wheel; ▴ and ▾ say rows lie beyond), as the
Sound panel's do.

It is painted as the mixer and the layouts are: the same panel, outline, radius and focus fill.
"""

from __future__ import annotations

import math
from typing import Optional

from PySide6.QtCore import QElapsedTimer, QEvent, QObject, QPoint, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import (
    QFont,
    QFontMetricsF,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QWheelEvent,
)
from PySide6.QtWidgets import QApplication, QWidget

from services.i18n import N_
from ui.keymap import Action
from ui.overlays.audio_screen import screen_units
from ui.overlays.base import Overlay
from ui.overlays.painting import Units, draw_icon, fill_round
from ui.overlays.transport import MENU_CONTROLS, Transport
from ui.pointer import NAV_BUTTONS
from ui.theme import FOCUS_FILL_ALPHA, PALETTE, qcolor

# Each row's transport signal: what its button emits.
SIGNALS = {
    "stop": "stopRequested",
    "audio": "audioRequested",
    "subtitles": "subtitleRequested",
    "picture": "pictureRequested",
    "fullscreen": "fullscreenRequested",
    "mini": "miniPlayerRequested",
    "library": "libraryRequested",
    "search": "searchRequested",
}
# Each row's icon, where the transport's button has none of its own (the Library's is a chip).
_ICONS = {"library": "clapper"}
_FALLBACK_HOST_WIDTH = 1280
_PAD_U = 0.6  # inside the panel
_ROW_PAD_U = 0.9  # a row's sides
_ROW_U = 3.4  # a row's height
_ICON_U = 1.7
_ICON_GAP_U = 1.0  # between a row's icon and its text
_FLOOR_GAP_U = 0.8  # above "⋯"
_EDGE_U = 1.0  # from the host's sides
_ARROW_U = 1.0  # the "more rows" arrows
_WHEEL_STEP = 120  # one notch: one row
_EVENTS_OUTSIDE = (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick)


class MoreMenu(Overlay):
    # i18n: what a screen reader calls the music player's ⋯ menu (more of its controls)
    accessible_name = N_("More")
    pass_empty_tooltip = True

    def __init__(self, transport: Transport, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self._transport = transport
        self._anchor: Optional[QPoint] = None  # "⋯"'s top-left (host coordinates)
        self._hover: Optional[str] = None
        self._shown = QElapsedTimer()  # since it showed: a double-click's second press
        self._filtering = False
        self._fit = len(MENU_CONTROLS)  # the rows that fit above "⋯"
        self._scroll = 0  # the first row shown
        self._wheel = 0
        transport.bandChanged.connect(self._follow_dock)
        if parent is not None:
            parent.installEventFilter(self)
        self.hide()

    # ---- public API ---------------------------------------------------------------------

    def open_at(self, point: QPoint) -> None:
        """Open above `point` (global: "⋯"'s top-left, as moreRequested gives it), or move
        there while open."""
        parent = self.parentWidget()
        self._anchor = parent.mapFromGlobal(point) if parent is not None else QPoint(point)
        fresh = self.isHidden()
        if fresh:
            self._hover = None
            self._scroll = 0
        self._layout()
        if fresh:
            self._shown.start()
            self.show()
        self.raise_()
        self.update()
        self._transport.hold("more")  # ⋯ is lit while its menu shows

    def dismiss(self) -> None:
        if not self.isHidden():
            self.hide()

    def handle(self, action: Action) -> bool:
        """A key while it shows: Esc closes it (True: Esc goes no further); any other key
        closes it and goes on (False). Closed, nothing is its own (False)."""
        if self.isHidden():
            return False
        self.dismiss()
        return action is Action.ESCAPE

    def _follow_dock(self) -> None:
        """The dock went (another mode, end_audio()): its menu goes with it, and lets go of
        "⋯", so a film's controls fade as theirs do."""
        transport = self._transport
        if not self.isHidden() and (not transport.audio() or transport.isHidden()):
            self.dismiss()

    def label(self, name: str) -> str:
        """What a row says: its button's tooltip (what it does, and its key)."""
        return self._transport.control_text(name)

    def controls(self) -> dict[str, QRect]:
        """Its rows by name (MENU_CONTROLS, top to bottom), in its own coordinates: those in
        sight (a short window shows fewer and scrolls)."""
        return {name: rect.toAlignedRect() for name, rect in self._rows().items()}

    def scroll_by(self, rows: int) -> None:
        """Show the rows `rows` further down (up when negative), within the list."""
        scroll = self._clamped(self._scroll + rows)
        if scroll != self._scroll:
            self._scroll = scroll
            self.update()

    # ---- layout -------------------------------------------------------------------------

    def _units(self) -> Units:
        """The dock's: capped by a 16:9 page as tall as the host, so a short window keeps the
        menu as small as its dock."""
        parent = self.parentWidget()
        if parent is None:
            return Units(_FALLBACK_HOST_WIDTH)
        return screen_units(parent.width(), parent.height())

    @staticmethod
    def _font(un: Units) -> QFont:
        return un.font(1.25)

    def _row_height(self, un: Units) -> float:
        return max(float(un.px(_ROW_U)), QFontMetricsF(self._font(un)).height() + un.px(1.2))

    def _text_left(self, un: Units) -> float:
        """Where a row's text starts, from the row's left."""
        return un.px(_ROW_PAD_U) + un.px(_ICON_U) + un.px(_ICON_GAP_U)

    def _clamped(self, scroll: int) -> int:
        return max(0, min(scroll, len(MENU_CONTROLS) - self._fit))

    def _layout(self) -> None:
        """Above "⋯", as many rows as fit between the host's top and it (at least one); the
        rest scroll."""
        parent = self.parentWidget()
        if parent is None:
            return
        un = self._units()
        edge = un.px(_EDGE_U)
        anchor = self._anchor if self._anchor is not None else QPoint(edge, parent.height())
        bottom = anchor.y() - un.px(_FLOOR_GAP_U)
        pad, row_h = un.px(_PAD_U), self._row_height(un)
        room = bottom - edge - 2 * pad
        self._fit = max(1, min(len(MENU_CONTROLS), math.floor(room / row_h)))
        self._scroll = self._clamped(self._scroll)
        fm = QFontMetricsF(self._font(un))
        text = max(fm.horizontalAdvance(self.label(name)) for name in MENU_CONTROLS)
        arrow = un.px(_ARROW_U) + un.px(_ROW_PAD_U) if self._fit < len(MENU_CONTROLS) else 0
        width = math.ceil(2 * pad + self._text_left(un) + text + un.px(_ROW_PAD_U) + arrow)
        width = min(width, parent.width())
        height = math.ceil(2 * pad + self._fit * row_h)
        x = max(min(anchor.x(), parent.width() - width - edge), min(edge, parent.width() - width))
        rect = QRect(x, max(0, bottom - height), width, height)
        if rect != self.geometry():
            self.setGeometry(rect)

    def _rows(self) -> dict[str, QRectF]:
        """The rows in sight, by name."""
        un = self._units()
        pad, row_h = float(un.px(_PAD_U)), self._row_height(un)
        width = self.width() - 2 * pad
        shown = MENU_CONTROLS[self._scroll : self._scroll + self._fit]
        return {
            name: QRectF(pad, pad + index * row_h, width, row_h) for index, name in enumerate(shown)
        }

    # ---- painting -----------------------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:
        un = self._units()
        font = self._font(un)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        panel = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = un.u * 1.2
        p.setPen(QPen(qcolor(PALETTE.text, 0.12), 1.0))
        p.setBrush(qcolor(PALETTE.panel, 0.92))
        p.drawRoundedRect(panel, radius, radius)
        icon = float(un.px(_ICON_U))
        fm = QFontMetricsF(font)
        p.setFont(font)
        rows = self._rows()
        scrolls = self._fit < len(MENU_CONTROLS)
        arrow = un.px(_ARROW_U) + un.px(_ROW_PAD_U) if scrolls else 0.0
        for name, rect in rows.items():
            if name == self._hover:
                fill_round(p, rect, qcolor(PALETTE.accent, FOCUS_FILL_ALPHA), un.u * 0.8)
            x = rect.left() + un.px(_ROW_PAD_U)
            box = QRectF(x, rect.center().y() - icon / 2, icon, icon)
            glyph = _ICONS.get(name) or self._transport.icon(name)
            draw_icon(p, glyph, box, qcolor(PALETTE.text))
            left = rect.left() + self._text_left(un)
            text = QRectF(left, rect.top(), rect.right() - left - arrow, rect.height())
            label = self.label(name)
            if fm.horizontalAdvance(label) > text.width():  # only in a window too small for it
                label = fm.elidedText(label, Qt.TextElideMode.ElideRight, text.width())
            p.setPen(qcolor(PALETTE.text))
            p.drawText(text, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, label)
        if scrolls:
            self._paint_arrows(p, un, list(rows.values()))
        p.end()

    def _paint_arrows(self, p: QPainter, un: Units, rects: list[QRectF]) -> None:
        """A small ▴ on the first row shown when rows are above it, ▾ on the last for below
        (as the Sound panel's)."""
        size = float(un.px(_ARROW_U))
        half = size / 2
        ends = ((rects[0], self._scroll > 0, True), (rects[-1], self._more_below(), False))
        for rect, beyond, up in ends:
            if not beyond:
                continue
            cx, cy = rect.right() - un.px(_ROW_PAD_U) - half, rect.center().y()
            tip, base = (
                (cy - half * 0.6, cy + half * 0.6) if up else (cy + half * 0.6, cy - half * 0.6)
            )
            path = QPainterPath(QPointF(cx - half, base))
            path.lineTo(cx + half, base)
            path.lineTo(cx, tip)
            path.closeSubpath()
            p.save()
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qcolor(PALETTE.text, 0.6))
            p.drawPath(path)
            p.restore()

    def _more_below(self) -> bool:
        return self._scroll + self._fit < len(MENU_CONTROLS)

    # ---- input --------------------------------------------------------------------------

    def _row_at(self, pos: QPointF) -> Optional[str]:
        for name, rect in self._rows().items():
            if rect.contains(pos):
                return name
        return None

    def _choose(self, name: str) -> None:
        """Close, then ask for what the row's button asks for."""
        self.dismiss()
        getattr(self._transport, SIGNALS[name]).emit()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() in NAV_BUTTONS:
            event.ignore()  # Back/Forward go on to the host: the shell's drawer layers
            return
        event.accept()  # never the host's
        if event.button() != Qt.MouseButton.LeftButton:
            return
        name = self._row_at(event.position())
        if name is not None:
            self._choose(name)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() in NAV_BUTTONS:
            event.ignore()
            return
        event.accept()  # a quick second click picks nothing more, nor goes fullscreen

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        hover = self._row_at(event.position())
        if hover != self._hover:
            self._hover = hover
            self.update()
        event.ignore()

    def leaveEvent(self, event: QEvent) -> None:
        if self._hover is not None:
            self._hover = None
            self.update()
        super().leaveEvent(event)

    def wheelEvent(self, event: QWheelEvent) -> None:
        """Its rows scroll, when not all fit; never the host's volume under it."""
        event.accept()
        self._wheel += event.angleDelta().y()
        rows = int(self._wheel / _WHEEL_STEP)  # towards zero: a notch up shows the rows above
        if rows:
            self._wheel -= rows * _WHEEL_STEP
            self.scroll_by(-rows)

    def tooltip_at(self, pos: QPointF) -> str:
        return ""  # its rows say what they do

    def showEvent(self, event) -> None:
        super().showEvent(event)
        app = QApplication.instance()
        if app is not None and not self._filtering:
            app.installEventFilter(self)  # a click elsewhere closes it
            self._filtering = True

    def hideEvent(self, event) -> None:
        self._stop_filtering()
        self._hover = None
        if self._transport.held() == "more":
            self._transport.hold("")
        super().hideEvent(event)

    def _stop_filtering(self) -> None:
        app = QApplication.instance()
        if app is not None and self._filtering:
            app.removeEventFilter(self)
        self._filtering = False

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        kind = event.type()
        if obj is self.parentWidget() and kind == QEvent.Type.Resize and not self.isHidden():
            self.dismiss()  # "⋯" moved: it opens again where it is
        elif obj is self._transport and kind == QEvent.Type.Hide and not self.isHidden():
            self.dismiss()  # the dock hid (end_audio(), the mini player): its menu goes too
        elif (
            kind in _EVENTS_OUTSIDE
            and not self.isHidden()
            and isinstance(event, QMouseEvent)
            and event.button() not in NAV_BUTTONS  # Back/Forward: the shell's drawer layers
            and self._outside(obj)
        ):
            control = self._control_under(obj, event)
            if (
                kind == QEvent.Type.MouseButtonDblClick
                and control != "more"  # its own button again, quickly: it closes
                and self._shown.isValid()
                and self._shown.elapsed() < QApplication.doubleClickInterval()
            ):
                return True  # the second press of the double-click that opened it
            self.dismiss()
            # Another of the dock's buttons acts; "⋯" only closes it, elsewhere nothing more.
            return control is None or control == "more"
        return False

    def _control_under(self, obj: QObject, event: QMouseEvent) -> Optional[str]:
        """The dock's control under a press on it (None: elsewhere)."""
        if obj is not self._transport:
            return None
        return self._transport.control_at(event.position())

    def _outside(self, obj: QObject) -> bool:
        """A press on another widget of this window (its overlays, the video): not on the
        menu, nor in a dialog or another window."""
        if not isinstance(obj, QWidget) or obj is self or self.isAncestorOf(obj):
            return False
        host = self.window()
        ours = {host}
        outer = host.parentWidget()
        if outer is not None:
            ours.add(outer.window())
        return obj.window() in ours

    def closeEvent(self, event) -> None:
        self._stop_filtering()
        super().closeEvent(event)
