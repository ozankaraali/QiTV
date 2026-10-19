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

The tile under the pointer (`hover_at`) shows its controls on a slim strip at its bottom, as
the player controls are drawn (units, painted icons, the focus fill under the pointer):
play/pause, its speaker (muted or not), LIVE or "−MM:SS" behind live (a click goes live),
the big picture (leave the grid with this tile big) and ✕ (close the tile). Each acts on that
tile (`tileAction`), never giving it the focus. A tile too narrow for them all keeps its
speaker and ✕; a card keeps ✕ (back to "+"), in its top corner; "+" has none. The strip
hides when the pointer leaves the tile, and after the player controls' idle time
(`set_idle_ms`). While the player controls show it sits just above their bottom row (their
bar too), else under the tile's label, else in the label's place (the label waits
meanwhile). A press on it never focuses the tile, and a quick second click on its buttons
counts once.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QFont,
    QFontMetrics,
    QFontMetricsF,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QRegion,
)
from PySide6.QtWidgets import QApplication, QWidget

from services.i18n import N_, translate
from ui.overlays.base import Overlay
from ui.overlays.painting import Units, draw_icon, fill_round
from ui.overlays.pip_label import DOTTED, LIVE, paint_backdrop, paint_pill
from ui.pointer import NAV_BUTTONS
from ui.theme import FOCUS_FILL_ALPHA, PALETTE, qcolor

EMPTY, OPENING, PLAYING, DENIED, FAILED = "empty", "opening", "playing", "denied", "failed"
CARDS = (EMPTY, DENIED, FAILED)  # drawn whole: the tile's video shows nothing
CHOOSE = N_("Choose a channel")  # i18n: under the "+" of an empty multiview tile
RETRY = N_("Click to try again")  # i18n: on a multiview tile's card (no stream, or it failed)
_FALLBACK_HOST_WIDTH = 1280
RING_UNITS = 0.35  # the focus ring's width
# A tile's strip: its controls (`tileAction`'s names), left to right; a narrow tile keeps
# ESSENTIALS, a card CARD_CONTROLS.
CONTROLS = ("play", "speaker", "live", "big", "close")
ESSENTIALS = ("speaker", "close")
CARD_CONTROLS = ("close",)
STRIP_IDLE_MS = 3000  # as the player controls' (set_idle_ms)
STRIP_UNITS = 2.8  # the strip's height (and each button's side)
STRIP_MIN_PX = 26  # each button's side at least, in a small window
STRIP_INSET_UNITS = 0.6
# The strip's tooltips (translated when shown).
TOOLTIPS = {
    "pause": N_("Pause this tile"),  # i18n: tooltip on a multiview tile's controls
    "play": N_("Play this tile"),  # i18n: tooltip on a multiview tile's controls
    "mute": N_("Mute this tile"),  # i18n: tooltip on a multiview tile's controls
    "unmute": N_("Unmute this tile"),  # i18n: tooltip on a multiview tile's controls
    "live": N_("Go live"),  # i18n: button: jump to the channel's live point
    # i18n: tooltip on a multiview tile's controls: leave the grid with this tile big
    "big": N_("Big picture (double-click)"),
    "close": N_("Close this tile"),  # i18n: tooltip on a multiview tile's controls
}


def speaker_icon(muted: bool, level: int) -> str:
    """The speaker's icon for a sound `muted` or at `level` (painting's draw_icon names)."""
    return "mute" if muted or level <= 0 else "volume"


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
    # Its strip: paused, its sound muted and its level, and where it plays: LIVE (English,
    # painted translated), "−MM:SS" behind live (`behind`: a click goes live), or "".
    paused: bool = False
    muted: bool = True
    level: int = 0
    position: str = ""
    behind: bool = False


@dataclass(frozen=True)
class _Strip:
    """A tile's strip (this widget's coordinates): the band it sits on, its two pills (play,
    speaker and LIVE at the left; the big picture and ✕ at the right) and each control."""

    bar: QRectF
    pills: tuple[QRectF, ...]
    parts: dict[str, QRectF]
    over_label: bool = False  # it sits where the tile's label is: the label hides meanwhile

    def region(self) -> QRegion:
        region = QRegion()
        for pill in self.pills:
            region = region.united(QRegion(pill.toAlignedRect()))
        return region


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

    pass_empty_tooltip = True  # off a tile's controls, the host's tooltip shows

    def __init__(self, tiles: "GridTiles", parent: QWidget | None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setMouseTracking(True)  # a tile's strip lights the control under the pointer
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

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        self._tiles.hover_at(event.position())
        event.ignore()  # on to the host: the player controls show, as over the video

    def tooltip_at(self, pos: QPointF) -> str:
        return self._tiles.tooltip_at(pos)


class GridTiles(Overlay):
    tilePressed = Signal(str)  # a left press on what a tile draws: its focus, at once
    tileClicked = Signal(str)  # that press's click, unless a double-click followed
    leaveRequested = Signal(str)  # a double-click: leave the grid with this tile large
    tileAction = Signal(str, str)  # a tile's strip: its name and the control (CONTROLS)

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
        self._hover: Optional[str] = None  # the tile whose strip shows
        self._part: Optional[str] = None  # the strip's control under the pointer
        self._strip_idle = QTimer(self, singleShot=True, interval=STRIP_IDLE_MS)
        self._strip_idle.timeout.connect(lambda: self._set_hover(None, None))
        self.hits = TileHits(self, parent)  # the host adds it over the player controls
        if parent is not None:
            parent.installEventFilter(self)
            self._fit()
        self.hide()

    # ---- public API ---------------------------------------------------------------------

    def set_tiles(self, tiles: list[TileView]) -> None:
        if tiles != self._tiles:
            self._tiles = list(tiles)
            if self._hover is not None and self._strip_of(self._hover) is None:
                self._hover = self._part = None  # its tile went, or shows "+" now
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
        """Leave `controls`' own buttons and bar (`hole_claims()`) theirs while they show; a
        tile's strip sits above them, and hides after their idle time (`idle_ms()`)."""
        self._avoided = controls
        controls.installEventFilter(self)
        changed = getattr(controls, "bandChanged", None)
        if changed is not None:
            changed.connect(self._refresh)
        idle = getattr(controls, "idle_ms", None)
        if idle is not None:
            self.set_idle_ms(idle())

    def set_idle_ms(self, ms: int) -> None:
        """How long a tile's strip stays after the pointer last moved."""
        self._strip_idle.setInterval(ms)

    def hover_at(self, pos: QPointF) -> None:
        """The pointer is at `pos` (this widget's coordinates): the tile under it shows its
        strip (until the pointer leaves it, or stays still for the idle time)."""
        point = pos.toPoint()
        name = next((t.name for t in self._tiles if t.rect.contains(point)), None)
        strip = self._strip_of(name) if name is not None else None
        if strip is None:
            self._set_hover(None, None)
            return
        part = next((n for n, r in strip.parts.items() if r.contains(pos)), None)
        self._set_hover(name, part)
        self._strip_idle.start()

    def hovered(self) -> Optional[str]:
        """The tile whose strip shows, else None."""
        return self._hover

    def strip_rect(self, name: str) -> QRectF:
        """Tile `name`'s strip while it shows: what its pills span (empty otherwise)."""
        strip = self._strip_of(name) if name == self._hover else None
        return QRectF(strip.region().boundingRect()) if strip is not None else QRectF()

    def control_rect(self, name: str, control: str) -> QRectF:
        """Where tile `name`'s strip has `control` (CONTROLS) while it shows; empty when it
        is left out or hidden."""
        strip = self._strip_of(name) if name == self._hover else None
        return QRectF(strip.parts.get(control, QRectF())) if strip is not None else QRectF()

    def tooltip_at(self, pos: QPointF) -> str:
        """What hovering a strip's control says (empty elsewhere)."""
        hit = self._strip_hit(pos)
        if hit is None or hit[1] is None:
            return ""
        tile, part = self._tile(hit[0]), hit[1]
        if tile is None:
            return ""
        if part == "play":
            key = "play" if tile.paused else "pause"
        elif part == "speaker":
            key = "unmute" if tile.muted else "mute"
        elif part == "live":
            key = "live" if tile.behind else ""
        else:
            key = part
        return translate(TOOLTIPS[key]) if key else ""

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
        if not visible and getattr(self, "_hover", None) is not None:
            self._hover = self._part = None
            self._strip_idle.stop()

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
        if tile.detail in DOTTED:  # LIVE or REC, after a red dot
            state_w = dot + dot_gap + states.horizontalAdvance(translate(tile.detail))
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
        """A playing tile's label, unless the controls' title row covers it now, or its own
        strip sits in its place."""
        if tile.state in CARDS:
            return None
        pill = self._pill(tile)
        if self._controls().intersects(pill.rect.toAlignedRect()):
            return None
        if tile.name == self._hover:
            strip = self._strip_of(tile.name)
            if strip is not None and strip.over_label:
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
        strip = self._strip_of(self._hover) if self._hover is not None else None
        if strip is not None:
            region = region.united(strip.region())
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
        elif (
            obj is self._avoided
            and kind == QEvent.Type.MouseMove
            and isinstance(event, QMouseEvent)
            and isinstance(obj, QWidget)
            and not self.isHidden()
        ):
            # The pointer over the player controls' buttons: still over a tile.
            self.hover_at(self.mapFromGlobal(obj.mapToGlobal(event.position())))
        return False

    # ---- a tile's strip -----------------------------------------------------------------

    def _tile(self, name: str) -> Optional[TileView]:
        return next((tile for tile in self._tiles if tile.name == name), None)

    def _set_hover(self, name: Optional[str], part: Optional[str]) -> None:
        if name is None:
            self._strip_idle.stop()
        if name != self._hover:
            self._hover, self._part = name, part
            self._refresh()
        elif part != self._part:
            self._part = part
            self.update()

    def _strip_font(self, un: Units) -> QFont:
        return un.font(1.0, QFont.Weight.Bold)

    def _position_width(self, un: Units, tile: TileView) -> float:
        """The LIVE or "−MM:SS" chip's width (0: it says nothing)."""
        if not tile.position:
            return 0.0
        metrics = QFontMetricsF(self._strip_font(un))
        text = translate(tile.position) if tile.position == LIVE else tile.position
        dot = max(6.0, round(metrics.height() * 0.42)) + un.px(0.4) if tile.position == LIVE else 0
        return metrics.horizontalAdvance(text) + dot + 2 * un.px(0.7)

    def _strip_of(self, name: Optional[str]) -> Optional[_Strip]:
        """Tile `name`'s strip, laid out (None: it has none, e.g. "+", or no room)."""
        tile = self._tile(name) if name is not None else None
        if tile is None or tile.state == EMPTY or tile.rect.isEmpty():
            return None
        un = self._units()
        side = max(
            float(un.px(STRIP_UNITS)),
            QFontMetricsF(self._strip_font(un)).height() + un.px(0.8),
            float(STRIP_MIN_PX),
        )
        inset = float(un.px(STRIP_INSET_UNITS))
        rect = QRectF(tile.rect)
        room = rect.width() - 2 * inset
        gap = float(un.px(0.3))
        if tile.state in CARDS:
            controls: tuple[str, ...] = CARD_CONTROLS
        else:
            position = self._position_width(un, tile)
            # Two pills, each padded both sides: ▶ 🔊 LIVE (two gaps) and ⤢ ✕ (one), a gap
            # between them.
            whole = 4 * side + position + 4 * gap + 4 * un.px(0.4)
            controls = CONTROLS if whole <= room else ESSENTIALS
        pills = 1 if controls == CARD_CONTROLS else 2
        least = len(controls) * side + 2 * pills * un.px(0.4) + (pills - 1) * gap
        if least > room or side + 2 * inset > rect.height():
            return None
        over_label = False
        if tile.state in CARDS:  # a card's ✕ only: in its top corner, else its bottom one
            width, corner = side + 2 * un.px(0.4), inset + un.px(0.8)  # inside the card's panel
            bar = QRectF(rect.right() - corner - width, rect.top() + corner, width, side)
            if any(band.intersects(bar) for band in self._control_bands()):
                bar.moveBottom(self._strip_bottom(un, tile, bar, inset)[0])
        else:
            bar = QRectF(rect.left() + inset, 0, room, side)
            bottom, over_label = self._strip_bottom(un, tile, bar, inset)
            bar.moveBottom(bottom)
        pad = float(un.px(0.4))
        parts: dict[str, QRectF] = {}
        x = bar.left() + pad
        for name in ("play", "speaker", "live"):
            if name not in controls:
                continue
            width = self._position_width(un, tile) if name == "live" else side
            if width <= 0:
                continue
            parts[name] = QRectF(x, bar.top(), width, side)
            x += width + gap
        x = bar.right() - pad
        for name in ("close", "big"):
            if name in controls:
                parts[name] = QRectF(x - side, bar.top(), side, side)
                x -= side + gap
        pills = []
        for group in (("play", "speaker", "live"), ("big", "close")):
            shown = [parts[name] for name in group if name in parts]
            if shown:
                left, right = min(r.left() for r in shown), max(r.right() for r in shown)
                pills.append(QRectF(left - pad, bar.top(), right - left + 2 * pad, side))
        return _Strip(bar, tuple(pills), parts, over_label)

    def _strip_bottom(
        self, un: Units, tile: TileView, bar: QRectF, inset: float
    ) -> tuple[float, bool]:
        """Where a tile's strip ends, and whether it covers the tile's label: at the tile's
        bottom; while the player controls show, clear of them (`_control_bands`): just above
        them, else under the tile's label, else in its place (the label hides meanwhile).
        With no room clear of them all, the label's place clear of their buttons and bar
        alone (their time bubble may cover it), else the bottom, under their buttons."""
        bottom = tile.rect.bottom() + 1 - inset
        bands = self._control_bands()
        if not bands:
            return bottom, False
        pill = self._pill(tile).rect if tile.state not in CARDS else QRectF()
        ceiling = (pill.bottom() if not pill.isEmpty() else tile.rect.top()) + inset
        height = bar.height()

        def span(lower: float) -> QRectF:
            return QRectF(bar.left(), lower - height, bar.width(), height)

        lowest = bottom
        while lowest - height >= ceiling:
            covered = [r.top() for r in bands if r.intersects(span(lowest))]
            if not covered:
                return lowest, False
            lowest = min(covered) - un.px(0.6)
        buttons = [QRectF(r) for r in self._controls()]
        places = ((ceiling + height, False), (tile.rect.top() + inset + height, True))
        for obstacles in (bands, buttons):
            for lower, over in places:
                if lower <= bottom and not any(r.intersects(span(lower)) for r in obstacles):
                    return lower, over and not pill.isEmpty()
        return bottom, False

    def _control_bands(self) -> list[QRectF]:
        """The player controls as a tile's strip keeps clear of them: across the page, from
        the top of their bottom part (their bar's time bubble included) down to the page's
        bottom, and from the page's top down to their title row's bottom."""
        rects = [QRectF(r) for r in self._controls()]
        middle = self.height() / 2
        lower = [r.top() for r in rects if r.center().y() >= middle]
        upper = [r.bottom() for r in rects if r.center().y() < middle]
        top_of = getattr(self._avoided, "controls_top", None)
        if lower and top_of is not None:
            lower.append(float(top_of() - self.pos().y()))  # host coordinates: the bubble
        bands = []
        if lower:
            top = min(lower)
            bands.append(QRectF(0, top, self.width(), self.height() - top))
        if upper:
            bands.append(QRectF(0, 0, self.width(), max(upper)))
        return bands

    def _strip_hit(self, pos: QPointF) -> Optional[tuple[str, Optional[str]]]:
        """(tile, control or None) when `pos` is on the strip shown, else None."""
        name = self._hover
        strip = self._strip_of(name) if name is not None else None
        if name is None or strip is None or not any(p.contains(pos) for p in strip.pills):
            return None
        return name, next((n for n, r in strip.parts.items() if r.contains(pos)), None)

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
                    tile.detail if tile.detail in DOTTED else translate(tile.detail),
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
        if self._hover is not None:
            self._paint_strip(p, un, self._hover)
        if self._drop is not None:
            self._paint_drop(p, un)
        p.end()

    def _paint_strip(self, p: QPainter, un: Units, name: str) -> None:
        """A tile's controls on their bar, as the player controls draw theirs."""
        tile, strip = self._tile(name), self._strip_of(name)
        if tile is None or strip is None:
            return
        for pill in strip.pills:
            radius = pill.height() / 2
            fill_round(p, pill, qcolor(PALETTE.panel, 0.9), radius)
            p.save()
            p.setPen(QPen(qcolor(PALETTE.text, 0.12), 1.0))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(pill.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
            p.restore()
        text = qcolor(PALETTE.text)
        for part, rect in strip.parts.items():
            lit = part == self._part and (part != "live" or tile.behind)
            if lit:
                fill_round(p, rect, qcolor(PALETTE.accent, FOCUS_FILL_ALPHA), rect.height() / 2)
            if part == "live":
                self._paint_position(p, un, tile, rect)
                continue
            if part == "play":
                icon = "play" if tile.paused else "pause"
            elif part == "speaker":
                icon = speaker_icon(tile.muted, tile.level)
            elif part == "big":
                icon = "enlarge"
            else:
                icon = "close"
            glyph = rect.height() * (0.56 if part == "close" else 0.6)
            box = QRectF(0, 0, glyph, glyph)
            box.moveCenter(rect.center())
            muted = part == "speaker" and (tile.muted or tile.level <= 0)
            draw_icon(
                p, icon, box, qcolor(PALETTE.text, PALETTE.text_muted_alpha) if muted else text
            )

    def _paint_position(self, p: QPainter, un: Units, tile: TileView, rect: QRectF) -> None:
        """LIVE after a red dot, or the time behind live in the accent colour."""
        font = self._strip_font(un)
        metrics = QFontMetricsF(font)
        p.setFont(font)
        x = rect.left() + un.px(0.7)
        if tile.position == LIVE:
            dot = max(6.0, round(metrics.height() * 0.42))
            fill_round(
                p,
                QRectF(x, rect.center().y() - dot / 2, dot, dot),
                qcolor(PALETTE.live),
                dot / 2,
            )
            x += dot + un.px(0.4)
            p.setPen(qcolor(PALETTE.text, 0.9))
            label = translate(LIVE)
        else:
            p.setPen(qcolor(PALETTE.accent))
            label = tile.position
        align = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        p.drawText(QRectF(x, rect.top(), rect.right() - x, rect.height()), align, label)

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
        strip = self._strip_hit(event.position())
        if strip is not None:  # a tile's controls: they act on it, never focusing it
            if event.button() == Qt.MouseButton.LeftButton and strip[1] is not None:
                self._strip_idle.start()
                self.tileAction.emit(*strip)
            return
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
        if self._strip_hit(event.position()) is not None:
            return  # a quick second click on a tile's control counts once
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
        self._strip_idle.stop()
        super().hideEvent(event)
