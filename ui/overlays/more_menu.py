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
goes on. It goes with its dock: when the transport leaves audio mode or hides.

It is painted as the mixer and the layouts are: the same panel, outline, radius and focus fill.
"""

from __future__ import annotations

import math
from typing import Optional

from PySide6.QtCore import QElapsedTimer, QEvent, QObject, QPoint, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QFont, QFontMetricsF, QMouseEvent, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QApplication, QWidget

from services.i18n import N_
from ui.keymap import Action
from ui.overlays.base import Overlay
from ui.overlays.painting import Units, draw_icon, fill_round, ui_units
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
        """Its rows by name (MENU_CONTROLS, top to bottom), in its own coordinates."""
        return {name: rect.toAlignedRect() for name, rect in self._rows().items()}

    # ---- layout -------------------------------------------------------------------------

    def _units(self) -> Units:
        parent = self.parentWidget()
        return ui_units(parent.width() if parent is not None else _FALLBACK_HOST_WIDTH)

    @staticmethod
    def _font(un: Units) -> QFont:
        return un.font(1.25)

    def _row_height(self, un: Units) -> float:
        return max(float(un.px(_ROW_U)), QFontMetricsF(self._font(un)).height() + un.px(1.2))

    def _text_left(self, un: Units) -> float:
        """Where a row's text starts, from the row's left."""
        return un.px(_ROW_PAD_U) + un.px(_ICON_U) + un.px(_ICON_GAP_U)

    def _size(self) -> tuple[int, int]:
        un = self._units()
        fm = QFontMetricsF(self._font(un))
        text = max(fm.horizontalAdvance(self.label(name)) for name in MENU_CONTROLS)
        row = self._text_left(un) + text + un.px(_ROW_PAD_U)
        width = 2 * un.px(_PAD_U) + row
        height = 2 * un.px(_PAD_U) + len(MENU_CONTROLS) * self._row_height(un)
        return math.ceil(width), math.ceil(height)

    def _layout(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        un = ui_units(parent.width())
        width, height = self._size()
        width = min(width, parent.width())
        edge = un.px(_EDGE_U)
        anchor = self._anchor if self._anchor is not None else QPoint(edge, parent.height())
        x = max(min(anchor.x(), parent.width() - width - edge), min(edge, parent.width() - width))
        bottom = anchor.y() - un.px(_FLOOR_GAP_U)
        rect = QRect(x, max(0, bottom - height), width, height)
        if rect != self.geometry():
            self.setGeometry(rect)

    def _rows(self) -> dict[str, QRectF]:
        un = self._units()
        pad, row_h = float(un.px(_PAD_U)), self._row_height(un)
        width = self.width() - 2 * pad
        return {
            name: QRectF(pad, pad + index * row_h, width, row_h)
            for index, name in enumerate(MENU_CONTROLS)
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
        for name, rect in self._rows().items():
            if name == self._hover:
                fill_round(p, rect, qcolor(PALETTE.accent, FOCUS_FILL_ALPHA), un.u * 0.8)
            x = rect.left() + un.px(_ROW_PAD_U)
            box = QRectF(x, rect.center().y() - icon / 2, icon, icon)
            glyph = _ICONS.get(name) or self._transport.icon(name)
            draw_icon(p, glyph, box, qcolor(PALETTE.text))
            left = rect.left() + self._text_left(un)
            text = QRectF(left, rect.top(), rect.right() - left, rect.height())
            label = self.label(name)
            if fm.horizontalAdvance(label) > text.width():  # only in a window too small for it
                label = fm.elidedText(label, Qt.TextElideMode.ElideRight, text.width())
            p.setPen(qcolor(PALETTE.text))
            p.drawText(text, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, label)
        p.end()

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
            if (
                kind == QEvent.Type.MouseButtonDblClick
                and self._shown.isValid()
                and self._shown.elapsed() < QApplication.doubleClickInterval()
            ):
                return True  # the second press of the double-click that opened it
            control = self._control_under(obj, event)
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
