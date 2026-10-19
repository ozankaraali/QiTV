"""Multiview's tiles, drawn over the grid: each tile's label, the focus ring, "+" and cards.

One overlay covers the host, under the player controls. Each tile shows:
- while its player plays (or opens): a label at its top-left, as the PiP label's pill: the
  channel's name and LIVE ("Connecting…" until it plays; "−MM:SS" behind live);
- with nothing to show: a "+" ("Choose a channel"): a click opens the channel list for it;
- when its provider had no stream to spare, or its stream failed: a card with the channel's
  name and why ("No stream to spare at <provider>"); a click tries again.
The focused tile has a ring in the accent colour.

Only what it draws shows over the grid (`hole_claims`: the host's hole covers the page), and a
click elsewhere reaches the tile's video below. The clicks on what it draws are taken by its
`hits`, a layer over the player controls that leaves their buttons and bar alone (`avoid`):
the controls stay drawn over the cards and the labels, and a label the controls' title row
would cover hides while they show. A press asks for the tile's focus at once (`tilePressed`);
the click follows after the double-click interval (`tileClicked`), or a double-click asks to
leave the grid (`leaveRequested`).

While a channel is dragged from the channel list (`set_drop`), the tile under the pointer is
lit as where it would play, and the channel's name follows the pointer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QFontMetrics, QMouseEvent, QPainter, QPaintEvent, QPen, QRegion
from PySide6.QtWidgets import QApplication, QWidget

from services.i18n import N_, translate
from ui.overlays.base import Overlay
from ui.overlays.painting import Units, draw_icon
from ui.overlays.pip_label import LIVE, paint_backdrop, paint_pill
from ui.pointer import NAV_BUTTONS
from ui.theme import PALETTE, qcolor

EMPTY, OPENING, PLAYING, DENIED, FAILED = "empty", "opening", "playing", "denied", "failed"
CARDS = (EMPTY, DENIED, FAILED)  # drawn whole: the tile's video shows nothing
CHOOSE = N_("Choose a channel")  # i18n: under the "+" of an empty multiview tile
RETRY = N_("Click to try again")  # i18n: on a multiview tile's card (no stream, or it failed)
_FALLBACK_HOST_WIDTH = 1280
RING_UNITS = 0.35  # the focus ring's width


@dataclass(frozen=True)
class TileView:
    """What a tile shows (host coordinates)."""

    name: str
    rect: QRect
    state: str  # EMPTY, OPENING, PLAYING, DENIED or FAILED
    title: str = ""  # the channel's name
    # The label's state (LIVE, "Connecting…", "−MM:SS", in English: painted translated) or
    # the card's why (translated already).
    detail: str = ""
    focused: bool = False


@dataclass(frozen=True)
class _Pill:
    rect: QRectF
    name: str  # elided to fit
    name_font: QFont
    state_font: QFont
    pad: float
    gap: float
    dot: float
    dot_gap: float


class TileHits(Overlay):
    """The clicks on what the tiles overlay draws, over the player controls but for their
    own buttons and bar (it draws nothing)."""

    def __init__(self, tiles: "GridTiles", parent: QWidget | None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self._tiles = tiles
        self.hide()

    def mask_region(self) -> QRegion:
        return self._tiles.hit_region().translated(self.geometry().topLeft())

    def hole_claims(self) -> QRegion:
        return self.mask_region()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self._tiles.pressed(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        self._tiles.double_clicked(event)


class GridTiles(Overlay):
    tilePressed = Signal(str)  # a left press on what a tile draws: its focus, at once
    tileClicked = Signal(str)  # that press's click, unless a double-click followed
    leaveRequested = Signal(str)  # a double-click: leave the grid with this tile large

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)  # `hits` take them
        self._tiles: list[TileView] = []
        self._avoided: Optional[QWidget] = None  # the player controls
        self._drop: Optional[tuple[Optional[str], str, QPoint]] = None  # a drag (set_drop)
        self._click = QTimer(self, singleShot=True)
        self._click.timeout.connect(self._clicked)
        self._pressed = ""  # the tile of the press whose click waits
        self.hits = TileHits(self, parent)  # the host adds it over the player controls
        if parent is not None:
            parent.installEventFilter(self)
            self._fit()
        self.hide()

    # ---- public API ---------------------------------------------------------------------

    def set_tiles(self, tiles: list[TileView]) -> None:
        if tiles != self._tiles:
            self._tiles = list(tiles)
            self._refresh()

    def tiles(self) -> list[TileView]:
        return list(self._tiles)

    def set_drop(self, name: Optional[str], title: str, point: QPoint) -> None:
        """A channel dragged from the channel list: tile `name` (None: no tile under the
        pointer) is lit as where it would play, and `title` follows the pointer at `point`
        (this widget's coordinates). None and "" end it."""
        drop = (name, title, QPoint(point)) if title else None
        if drop != self._drop:
            self._drop = drop
            self._refresh()

    def drop(self) -> Optional[tuple[Optional[str], str, QPoint]]:
        """The drag shown now: (the tile lit, the title, where), else None."""
        return self._drop

    def avoid(self, controls: QWidget) -> None:
        """Leave `controls`' own buttons and bar (`hole_claims()`) theirs while they show."""
        self._avoided = controls
        controls.installEventFilter(self)
        changed = getattr(controls, "bandChanged", None)
        if changed is not None:
            changed.connect(self._refresh)

    def tile_at(self, pos: QPointF) -> Optional[str]:
        """The tile whose drawn part takes clicks at `pos` (this widget's coordinates)."""
        point = pos.toPoint()
        if not self.hit_region().contains(point):
            return None
        for tile in self._tiles:
            if tile.rect.contains(point):
                return tile.name
        return None

    def mask_region(self) -> QRegion:
        """What it draws (host coordinates)."""
        return self._drawn().translated(self.geometry().topLeft())

    def hole_claims(self) -> QRegion:
        """What it draws shows over the grid (the host's hole covers the page)."""
        return self.mask_region()

    def hit_region(self) -> QRegion:
        """Where its drawn parts take clicks (this widget's coordinates): not on the
        controls' own buttons and bar while they show."""
        return self._drawn().subtracted(self._controls())

    def pill_rect(self, name: str) -> QRectF:
        """Where tile `name`'s label is (empty: a card, no such tile, or hidden under the
        controls' title row)."""
        for tile in self._tiles:
            if tile.name == name:
                pill = self._shown_pill(tile)
                return QRectF(pill.rect) if pill is not None else QRectF()
        return QRectF()

    def setVisible(self, visible: bool) -> None:
        super().setVisible(visible)
        hits = getattr(self, "hits", None)
        if hits is not None:
            hits.setVisible(visible)

    # ---- layout -------------------------------------------------------------------------

    def _units(self) -> Units:
        parent = self.parentWidget()
        return Units(parent.width() if parent is not None else _FALLBACK_HOST_WIDTH)

    def _fit(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        for widget in (self, self.hits):
            if widget.geometry() != parent.rect():
                widget.setGeometry(parent.rect())

    def _controls(self) -> QRegion:
        """The controls' own buttons and bar while they show (this widget's coordinates)."""
        avoided = self._avoided
        if avoided is None or not avoided.isVisible() or not hasattr(avoided, "hole_claims"):
            return QRegion()
        claims: QRegion = avoided.hole_claims()
        return claims.translated(-self.pos())

    def _ring(self, rect: QRect) -> QRegion:
        width = self._units().px(RING_UNITS)
        outer = QRegion(rect)
        return outer.subtracted(QRegion(rect.adjusted(width, width, -width, -width)))

    def _pill(self, tile: TileView) -> _Pill:
        """The label's pill and how its text fits."""
        un = self._units()
        name_font = un.font(1.0, QFont.Weight.DemiBold)
        state_font = un.font(1.0, QFont.Weight.Bold)
        names, states = QFontMetrics(name_font), QFontMetrics(state_font)
        inset, pad, gap = un.px(1), un.px(0.8), un.px(0.6)
        height = max(names.height(), states.height()) + 2 * un.px(0.35)
        dot = max(6, round(states.height() * 0.42))
        dot_gap = un.px(0.4)
        if tile.detail == LIVE:
            state_w = dot + dot_gap + states.horizontalAdvance(translate(LIVE))
        else:
            state_w = states.horizontalAdvance(translate(tile.detail))
        room = max(0.0, tile.rect.width() - 2 * inset)
        between = gap if tile.detail else 0
        name_room = int(room - 2 * pad - between - state_w)
        elide = Qt.TextElideMode.ElideRight
        name = names.elidedText(tile.title, elide, name_room) if name_room > 0 else ""
        width = 2 * pad + state_w + (between + names.horizontalAdvance(name) if name else 0)
        rect = QRectF(tile.rect.x() + inset, tile.rect.y() + inset, min(width, room), height)
        return _Pill(rect, name, name_font, state_font, pad, gap, dot, dot_gap)

    def _shown_pill(self, tile: TileView) -> Optional[_Pill]:
        """A playing tile's label, unless the controls' title row covers it now."""
        if tile.state in CARDS:
            return None
        pill = self._pill(tile)
        if self._controls().intersects(pill.rect.toAlignedRect()):
            return None
        return pill

    def _ghost(self) -> QRectF:
        """Where the dragged channel's name is drawn, beside the pointer (empty: no drag)."""
        drop = self._drop
        if drop is None:
            return QRectF()
        un = self._units()
        font = un.font(1.1, QFont.Weight.DemiBold)
        width = QFontMetrics(font).horizontalAdvance(drop[1]) + 2 * un.px(1.0)
        height = QFontMetrics(font).height() + 2 * un.px(0.5)
        point = drop[2]
        return QRectF(point.x() + un.px(1.2), point.y() + un.px(1.2), width, height)

    def _drawn(self) -> QRegion:
        region = QRegion()
        drop = self._drop
        if drop is not None:
            region = region.united(QRegion(self._ghost().toAlignedRect()))
            for tile in self._tiles:
                if tile.name == drop[0]:
                    region = region.united(QRegion(tile.rect))
        for tile in self._tiles:
            if tile.state in CARDS:
                region = region.united(QRegion(tile.rect))
            else:
                pill = self._shown_pill(tile)
                if pill is not None:
                    region = region.united(QRegion(pill.rect.toAlignedRect()))
            if tile.focused:
                region = region.united(self._ring(tile.rect))
        return region.intersected(QRegion(self.rect()))

    def _refresh(self) -> None:
        hits = self.hit_region()
        # Nothing of it takes the mouse: an empty mask would be no mask (the whole layer).
        self.hits.setMask(hits if not hits.isEmpty() else QRegion(0, 0, 1, 1))
        host = self.parentWidget()
        if not self.isHidden() and host is not None and hasattr(host, "overlay_changed"):
            host.overlay_changed()
        self.update()

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        kind = event.type()
        if obj is self.parentWidget() and kind == QEvent.Type.Resize:
            self._fit()
            self._refresh()
        elif obj is self._avoided and kind in (QEvent.Type.Show, QEvent.Type.Hide):
            self._refresh()
        return False

    # ---- painting -----------------------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:
        if not self._tiles:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        un = self._units()
        for tile in self._tiles:
            if tile.state in CARDS:
                self._paint_card(p, un, tile)
                continue
            pill = self._shown_pill(tile)
            if pill is not None:
                paint_pill(
                    p,
                    pill.rect,
                    pill.name,
                    tile.detail if tile.detail == LIVE else translate(tile.detail),
                    pill.name_font,
                    pill.state_font,
                    pill.pad,
                    pill.gap,
                    pill.dot,
                    pill.dot_gap,
                )
        for tile in self._tiles:
            if tile.focused:
                width = un.px(RING_UNITS)
                p.save()
                p.setPen(QPen(qcolor(PALETTE.accent), width))
                p.setBrush(Qt.BrushStyle.NoBrush)
                half = width / 2
                p.drawRect(QRectF(tile.rect).adjusted(half, half, -half, -half))
                p.restore()
        if self._drop is not None:
            self._paint_drop(p, un)
        p.end()

    def _paint_drop(self, p: QPainter, un: Units) -> None:
        """A channel dragged over the grid: the tile it would play in, and its name."""
        drop = self._drop
        assert drop is not None
        name, title, _point = drop
        for tile in self._tiles:
            if tile.name == name:
                rect = QRectF(tile.rect)
                p.fillRect(rect, qcolor(PALETTE.accent, 0.22))
                width = un.px(RING_UNITS) * 2
                p.save()
                pen = QPen(qcolor(PALETTE.accent), width, Qt.PenStyle.DashLine)
                p.setPen(pen)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRect(rect.adjusted(width / 2, width / 2, -width / 2, -width / 2))
                p.restore()
        ghost = self._ghost()
        paint_backdrop(p, ghost)
        p.setFont(un.font(1.1, QFont.Weight.DemiBold))
        p.setPen(qcolor(PALETTE.text))
        p.drawText(ghost, Qt.AlignmentFlag.AlignCenter, title)

    def _paint_card(self, p: QPainter, un: Units, tile: TileView) -> None:
        rect = QRectF(tile.rect)
        p.fillRect(rect, qcolor(PALETTE.canvas))
        inner = rect.adjusted(un.px(0.8), un.px(0.8), -un.px(0.8), -un.px(0.8))
        paint_backdrop(p, inner, radius=un.px(0.8))
        text = qcolor(PALETTE.text)
        muted = qcolor(PALETTE.text, PALETTE.text_muted_alpha)
        centre = inner.center()
        if tile.state == EMPTY:
            side = max(float(un.px(2.4)), min(inner.width(), inner.height()) * 0.16)
            plus = QRectF(centre.x() - side / 2, centre.y() - side * 0.8, side, side)
            draw_icon(p, "plus", plus, qcolor(PALETTE.text, 0.8))
            p.setFont(un.font(1.0, QFont.Weight.DemiBold))
            p.setPen(muted)
            caption = QRectF(inner.x(), plus.bottom() + un.px(0.6), inner.width(), un.px(2.4))
            top = Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop
            p.drawText(caption, top, translate(CHOOSE))
            return
        lines = (
            (tile.title, un.font(1.3, QFont.Weight.Bold), text),
            (tile.detail, un.font(1.0, QFont.Weight.DemiBold), qcolor(PALETTE.accent)),
            (translate(RETRY), un.font(0.9), muted),
        )
        heights = [QFontMetrics(font).height() for _, font, _ in lines]
        spacing = un.px(0.5)
        y = centre.y() - (sum(heights) + spacing * (len(lines) - 1)) / 2
        room = max(0, round(inner.width() - 2 * un.px(1.2)))
        for (line, font, color), height in zip(lines, heights):
            p.setFont(font)
            p.setPen(color)
            shown = QFontMetrics(font).elidedText(line, Qt.TextElideMode.ElideRight, room)
            p.drawText(
                QRectF(inner.x(), y, inner.width(), height), Qt.AlignmentFlag.AlignCenter, shown
            )
            y += height + spacing

    # ---- input (its `hits`') ------------------------------------------------------------

    def pressed(self, event: QMouseEvent) -> None:
        if event.button() in NAV_BUTTONS:
            event.ignore()  # Back/Forward go on to the host: the shell's drawer layers
            return
        event.accept()
        name = self.tile_at(event.position())
        if event.button() != Qt.MouseButton.LeftButton or name is None:
            return
        self._pressed = name
        self.tilePressed.emit(name)
        self._click.start(QApplication.doubleClickInterval())

    def double_clicked(self, event: QMouseEvent) -> None:
        if event.button() in NAV_BUTTONS:
            event.ignore()
            return
        event.accept()
        name = self.tile_at(event.position())
        if event.button() != Qt.MouseButton.LeftButton or name is None:
            return
        self._click.stop()
        self._pressed = ""
        self.leaveRequested.emit(name)

    def _clicked(self) -> None:
        name, self._pressed = self._pressed, ""
        if name:
            self.tileClicked.emit(name)

    def hideEvent(self, event) -> None:
        self._click.stop()
        self._pressed = ""
        super().hideEvent(event)
