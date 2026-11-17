"""QiTV's own mark as the opening draws it: the coral glyph of assets/qitv.png on its dark
rounded tile, outlined in coral, filled, and lit by a soft light behind it, on a 3.2 s loop.
A splash, the player page while a file opens, and the build's still frame all paint it with
paint_mark; OpeningMark is the widget that plays it and opens out of it.

The glyph is assets/qitv-mark.svg: one path on the logo's 416 grid, read once by the small
M/L/C/Z parser here rather than through QtSvg. Everything is drawn as vectors for the painter's
device, so it is sharp at any size and pixel ratio; a part that fades as one (the glyph as it
goes, all of it as it opens) is painted into a layer of the device's own pixels first.

The look is the approved mockup's (CSS keyframes): ease-in-out between keyframes; the line
draws from 4 % to 46 %, the fill comes in by 56 %, the glyph holds lit to 88 % and is gone at
98 %; the light breathes, warms as the line draws, blooms at the fill and pulses once more.

GUI thread only, except the worker that asks gsettings whether animations are on.
"""

from __future__ import annotations

from collections.abc import Callable
import ctypes
from dataclasses import dataclass
import logging
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from typing import Any, NamedTuple, Optional, Union
from xml.etree import ElementTree

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import (
    QBrush,
    QColor,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
    QTransform,
)
from PySide6.QtWidgets import QWidget

from ui.brand import asset_path

logger = logging.getLogger(__name__)

MARK_FILE = "qitv-mark.svg"
GRID = 416.0  # the logo's grid: the mark's square, assets/qitv-mark.svg's viewBox
TILE = QRectF(8, 8, 400, 400)
TILE_RADIUS = 92.0
STROKE_WIDTH = 10.0
TILE_COLOR = QColor("#1c1917")
STROKE_COLOR = QColor("#ff7a6b")
FILL_COLOR = QColor("#ff6656")
MARK_PX = 104  # the mark's side on screen
LIGHT_PX = 184  # the light behind it, at rest; it breathes between 0.85x and 1.14x
STAGE_PX = 240  # the room a MARK_PX mark needs for its light at the bloom and as it opens

LOOP_MS = 3200
OPEN_MS = 350  # the open: the lit mark grows by OPEN_GROWTH and fades
OPEN_GROWTH = 0.12
STILL_T = 0.7  # the held, lit frame (reduced motion)
DRAWN_AT = 0.46  # the outline drawn whole, not yet filled: the build's still (render_splash)
FILLED_AT = 0.56  # the draw is complete: outlined and filled
LIT_UNTIL = 0.88  # then the glyph fades, gone by 0.98
HOLD_BREATH_S = 2.4  # a held-lit mark's light breathes once in this long
CATCH_UP_SPEED = 2.0  # finish(): the rest of the draw at least twice as fast…
CATCH_UP_MAX_S = 0.4  # …and never adding more than this
FRAME_MS = 16
LOOP_S = LOOP_MS / 1000
OPEN_S = OPEN_MS / 1000
# A mark that takes over from the bootloader's still (the splash over it, and the player's on
# its origin) begins on the still's frame (DRAWN_AT), fills and blooms at the loop's own pace,
# then never goes out: its phase swings, eased as a sine is, between the fill (FILLED_AT) and
# where the glyph would begin to fade (LIT_UNTIL), the light breathing with it.
HOLD_FILL_S = (FILLED_AT - DRAWN_AT) * LOOP_S
_HOLD_MID = (FILLED_AT + LIT_UNTIL) / 2
_HOLD_AMP = (LIT_UNTIL - FILLED_AT) / 2

# The keyframes as (phase, value): how much of the outline is drawn, the fill's opacity, the
# glyph's opacity.
_DRAWN = ((0.0, 0.0), (0.04, 0.0), (0.46, 1.0), (1.0, 1.0))
_FILLED = ((0.0, 0.0), (0.46, 0.0), (0.56, 1.0), (1.0, 1.0))
_SHOWN = ((0.0, 1.0), (0.88, 1.0), (0.98, 0.0), (1.0, 0.0))
# The light: (phase, (r, g, b, alpha) of its centre, scale, opacity).
_LIGHT = (
    (0.00, (255, 170, 120, 0.20), 0.85, 0.60),
    (0.30, (255, 140, 100, 0.26), 0.95, 0.80),
    (0.56, (255, 102, 86, 0.38), 1.14, 1.00),
    (0.76, (255, 102, 86, 0.26), 0.97, 0.80),
    (0.90, (255, 102, 86, 0.34), 1.06, 0.95),
    (1.00, (255, 170, 120, 0.20), 0.85, 0.60),
)
_LIGHT_RADIUS = GRID / 2 * LIGHT_PX / MARK_PX  # 368: the light's circle, on the grid
# CSS's radial-gradient(circle, …, transparent 62%) runs to the box's farthest corner, so the
# light ends at 62 % of radius·√2 (0.88 of the circle's radius), not of the radius.
_LIGHT_END = 0.62
_LIGHT_RAY = _LIGHT_RADIUS * math.sqrt(2)
# drop-shadow(0 4px 10px rgba(0,0,0,.55)) on the tile, in grid units.
_SHADOW_ALPHA, _SHADOW_SIGMA, _SHADOW_DY = 0.55, 10.0, 4.0


def _cubic(s: float, a: float, b: float) -> float:
    """One coordinate of a CSS cubic-bezier(), from 0 to 1, with control values a and b."""
    return 3 * a * s * (1 - s) ** 2 + 3 * b * s * s * (1 - s) + s**3


def held_phase(seconds: float) -> float:
    """The loop phase of a held mark `seconds` after it began: DRAWN_AT at 0 (the still's
    frame: the outline whole), the fill and the bloom over HOLD_FILL_S, then always in
    FILLED_AT..LIT_UNTIL, so the glyph stays lit while the light breathes."""
    if seconds < HOLD_FILL_S:
        return DRAWN_AT + max(seconds, 0.0) / LOOP_S
    # from the swing's low point, the fill: no jump in phase
    angle = 2 * math.pi * (seconds - HOLD_FILL_S) / HOLD_BREATH_S - math.pi / 2
    return min(max(_HOLD_MID + _HOLD_AMP * math.sin(angle), FILLED_AT), LIT_UNTIL)


def _ease_in_out(u: float) -> float:
    """CSS's ease-in-out, cubic-bezier(0.42, 0, 0.58, 1), at time share `u`."""
    if u <= 0.0:
        return 0.0
    if u >= 1.0:
        return 1.0
    lo, hi = 0.0, 1.0
    for _ in range(30):  # its x rises with s: halve the range around u
        s = (lo + hi) / 2
        if _cubic(s, 0.42, 0.58) < u:
            lo = s
        else:
            hi = s
    return _cubic((lo + hi) / 2, 0.0, 1.0)


def _segment(phases: tuple[float, ...], t: float) -> tuple[int, float]:
    """The keyframe interval `t` falls in, and how far along it, eased."""
    for i in range(len(phases) - 1):
        if t <= phases[i + 1]:
            span = phases[i + 1] - phases[i]
            return i, _ease_in_out((t - phases[i]) / span if span > 0 else 1.0)
    return len(phases) - 2, 1.0


def _value(frames: tuple[tuple[float, float], ...], t: float) -> float:
    i, u = _segment(tuple(f[0] for f in frames), t)
    return frames[i][1] + (frames[i + 1][1] - frames[i][1]) * u


def _light(t: float) -> tuple[QColor, float]:
    """The light's centre colour (its opacity included) and scale at phase `t`. Its colour
    moves as a cross-fade does: premultiplied."""
    i, u = _segment(tuple(f[0] for f in _LIGHT), t)
    c0, s0, o0 = _LIGHT[i][1:]
    c1, s1, o1 = _LIGHT[i + 1][1:]
    alpha = c0[3] + (c1[3] - c0[3]) * u
    rgb = [(c0[k] * c0[3] + (c1[k] * c1[3] - c0[k] * c0[3]) * u) / alpha for k in range(3)]
    opacity = o0 + (o1 - o0) * u
    color = QColor.fromRgbF(rgb[0] / 255, rgb[1] / 255, rgb[2] / 255, alpha * opacity)
    return color, s0 + (s1 - s0) * u


def _shadow_rings() -> tuple[tuple[float, float], ...]:
    """(growth, alpha) of the rounded rects, outermost first, that stacked fall off outside
    the tile as the Gaussian blur of the CSS drop shadow does (a few px on screen)."""
    rings = []
    covered = 0.0
    for growth in (24.0 - 3.0 * k for k in range(12)):
        target = _SHADOW_ALPHA * 0.5 * math.erfc((growth - 1.5) / (_SHADOW_SIGMA * math.sqrt(2)))
        rings.append((growth, 1.0 - (1.0 - target) / (1.0 - covered)))
        covered = target
    return tuple(rings)


_SHADOW_RINGS = _shadow_rings()


# ---- the glyph -----------------------------------------------------------------------------

_TOKEN = re.compile(r"([A-Za-z])|([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)|([\s,]+)|(.)")
_NUMBERS = {"M": 2, "L": 2, "C": 6, "Z": 0}  # what each command takes


def _path_tokens(d: str) -> list[Union[str, float]]:
    tokens: list[Union[str, float]] = []
    for letter, number, _space, other in _TOKEN.findall(d):
        if other:
            raise ValueError(f"unexpected {other!r} in the path")
        if letter:
            tokens.append(letter)
        elif number:
            tokens.append(float(number))
    return tokens


def parse_path(d: str) -> QPainterPath:
    """An SVG path's `d` made of absolute M, L, C and Z commands, what assets/qitv-mark.svg
    uses; ValueError on anything else."""
    tokens = _path_tokens(d)
    if tokens and tokens[0] != "M":
        raise ValueError("a path starts with M")
    path = QPainterPath()
    command = "M"
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if isinstance(token, str):
            if token not in _NUMBERS:
                raise ValueError(f"unsupported path command {token!r}")
            command = token
            i += 1
            if command == "Z":
                path.closeSubpath()
            continue
        count = _NUMBERS[command]
        x = [a for a in tokens[i : i + count] if isinstance(a, float)]
        if not count or len(x) < count:
            raise ValueError(f"{command} takes {count} numbers")
        if command == "M":
            path.moveTo(x[0], x[1])
            command = "L"  # as in SVG: more pairs after M are lines
        elif command == "L":
            path.lineTo(x[0], x[1])
        else:
            path.cubicTo(x[0], x[1], x[2], x[3], x[4], x[5])
        i += count
    return path


def mark_file() -> Path:
    """assets/qitv-mark.svg next to the sources, or in a PyInstaller bundle's data."""
    return asset_path(MARK_FILE)


def _load_mark() -> QPainterPath:
    source = mark_file()
    try:
        root = ElementTree.parse(source).getroot()
        element = next((e for e in root.iter() if e.tag.rsplit("}", 1)[-1] == "path"), None)
        if element is None or not element.get("d"):
            raise ValueError("it has no <path d=…>")
        return parse_path(element.get("d", ""))
    except (OSError, ValueError, ElementTree.ParseError) as e:
        logger.warning(
            "QiTV's mark could not be read from %s (%s); the tile shows alone", source, e
        )
        return QPainterPath()


MARK_PATH: QPainterPath = _load_mark()
_MARK_LENGTH = MARK_PATH.length()
_GLYPH_BOUNDS = MARK_PATH.boundingRect().adjusted(
    -STROKE_WIDTH, -STROKE_WIDTH, STROKE_WIDTH, STROKE_WIDTH
)


# ---- painting ------------------------------------------------------------------------------


def mark_rect(area: QRectF, side: float = MARK_PX) -> QRectF:
    """The mark's square, `side` wide, in the middle of `area`."""
    centre = area.center()
    return QRectF(centre.x() - side / 2, centre.y() - side / 2, side, side)


def paint_mark(
    p: QPainter,
    rect: QRectF,
    t: float,
    *,
    still: bool = False,
    opening: float = 0.0,
    bare: bool = False,
    light: float = 1.0,
) -> None:
    """QiTV's mark at loop phase `t` (0..1): its light, the tile and its shadow, the outline
    drawn so far and the fill; `bare`, the tile and the glyph only (the opaque parts: where the
    same mark's light and shadow show under it, they are not drawn twice). `light` (0..1) is
    how much of its light and shadow shows (0: as bare).

    `rect` is the mark's square, its tile's 416 grid (MARK_PX on screen; a non-square rect
    uses the square in its middle). The light spills out of it, to LIGHT_PX/MARK_PX of its side
    and 1.14x that at the bloom, so leave room around it: STAGE_PX for a MARK_PX mark.
    `still` paints the held, lit frame whatever `t` is. `opening` (0..1) grows the whole mark
    up to 1 + OPEN_GROWTH times and fades it out as one. The painter's own opacity fades it
    as one too (the plain fade of reduced motion).
    """
    phase = STILL_T if still else t % 1.0
    opening = min(max(opening, 0.0), 1.0)
    side = min(rect.width(), rect.height())
    if side <= 0.0 or opening >= 1.0 or p.opacity() <= 0.0:
        return
    p.save()
    try:
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.translate(rect.center())
        scale = side / GRID * (1.0 + OPEN_GROWTH * opening)
        p.scale(scale, scale)
        p.translate(-GRID / 2, -GRID / 2)
        if light <= 0.0:
            bare = True
        light = min(light, 1.0)
        if opening > 0.0 or p.opacity() < 1.0:
            _in_layer(
                p,
                _stage_bounds(phase),
                1.0 - opening,
                lambda q: _paint_all(q, phase, bare, light),
            )
        else:
            _paint_all(p, phase, bare, light)
    finally:
        p.restore()


def _stage_bounds(t: float) -> QRectF:
    """What the mark paints at phase `t`, on the grid: its light's circle, the shadow inside."""
    _color, scale = _light(t)
    half = max(_LIGHT_RAY * _LIGHT_END * scale, 240.0) + 2.0
    return QRectF(GRID / 2 - half, GRID / 2 - half, 2 * half, 2 * half)


def _in_layer(
    p: QPainter, bounds: QRectF, opacity: float, draw: Callable[[QPainter], None]
) -> None:
    """`draw` (in `p`'s coordinates) into a layer of the device's own pixels, then the layer at
    `opacity` times the painter's: what overlaps inside it fades as one, as CSS's opacity does,
    and nothing is resampled."""
    device = p.deviceTransform()  # to the device's pixels: its pixel ratio and offset included
    box = device.mapRect(bounds).toAlignedRect()
    if box.isEmpty():
        return
    layer = QImage(box.size(), QImage.Format.Format_ARGB32_Premultiplied)
    layer.fill(Qt.GlobalColor.transparent)
    q = QPainter(layer)
    try:
        q.setRenderHints(p.renderHints())
        q.setTransform(device * QTransform.fromTranslate(-box.x(), -box.y()))
        draw(q)
    finally:
        q.end()
    inverse, _invertible = device.inverted()
    p.save()
    try:
        # the layer's pixels land on the device's, one for one
        place = QTransform.fromTranslate(box.x(), box.y()) * inverse * p.worldTransform()
        p.setWorldTransform(place)
        p.setOpacity(p.opacity() * opacity)
        p.drawImage(0, 0, layer)
    finally:
        p.restore()


def _paint_all(p: QPainter, t: float, bare: bool = False, light: float = 1.0) -> None:
    p.setPen(Qt.PenStyle.NoPen)
    if not bare:
        p.save()
        p.setOpacity(p.opacity() * light)
        _paint_light(p, t)
        p.setPen(Qt.PenStyle.NoPen)
        shadow = QRectF(TILE).translated(0, _SHADOW_DY)
        for growth, alpha in _SHADOW_RINGS:
            p.setBrush(QColor.fromRgbF(0.0, 0.0, 0.0, alpha))
            radius = TILE_RADIUS + growth
            p.drawRoundedRect(shadow.adjusted(-growth, -growth, growth, growth), radius, radius)
        p.restore()
    p.setBrush(TILE_COLOR)
    p.drawRoundedRect(TILE, TILE_RADIUS, TILE_RADIUS)
    _paint_glyph(p, t)


def _paint_light(p: QPainter, t: float) -> None:
    color, scale = _light(t)
    if color.alphaF() <= 0.0:
        return
    centre = QPointF(GRID / 2, GRID / 2)
    ray = _LIGHT_RAY * scale
    gradient = QRadialGradient(centre, ray)
    clear = QColor(color)
    clear.setAlphaF(0.0)
    gradient.setColorAt(0.0, color)
    gradient.setColorAt(_LIGHT_END, clear)
    gradient.setColorAt(1.0, clear)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(gradient))
    p.drawEllipse(centre, ray * _LIGHT_END, ray * _LIGHT_END)


def _paint_glyph(p: QPainter, t: float) -> None:
    drawn, filled, shown = _value(_DRAWN, t), _value(_FILLED, t), _value(_SHOWN, t)
    if drawn <= 0.0 or shown <= 0.0 or MARK_PATH.isEmpty():
        return
    if shown < 1.0:  # the outline over the fill, fading as one
        _in_layer(p, _GLYPH_BOUNDS, shown, lambda q: _draw_glyph(q, drawn, filled))
    else:
        _draw_glyph(p, drawn, filled)


def _draw_glyph(p: QPainter, drawn: float, filled: float) -> None:
    if filled > 0.0:
        fill = QColor(FILL_COLOR)
        fill.setAlphaF(filled)
        p.fillPath(MARK_PATH, fill)
    pen = QPen(
        STROKE_COLOR,
        STROKE_WIDTH,
        Qt.PenStyle.SolidLine,
        Qt.PenCapStyle.RoundCap,
        Qt.PenJoinStyle.RoundJoin,
    )
    if drawn < 1.0:
        # One dash the path's length, then as long a gap, moved along as the line draws.
        # Qt measures dashes in pen widths.
        whole = _MARK_LENGTH / STROKE_WIDTH
        pen.setDashPattern([whole, whole])
        pen.setDashOffset(whole * (1.0 - drawn))
    p.strokePath(MARK_PATH, pen)


# ---- the widget ----------------------------------------------------------------------------


def _coming_in(span: Optional[tuple[float, float]], now: float) -> float:
    """How far something that comes in over `span` (from when, for how long) is at `now`,
    eased: 1.0 with none."""
    if span is None:
        return 1.0
    since, length = span
    return _ease_in_out((now - since) / length)


class Frame(NamedTuple):
    """What OpeningMark paints at a moment: paint_mark's `t`, `still`, `opening` and `light`,
    and the plain fade's opacity (reduced motion; 1.0 otherwise)."""

    t: float
    still: bool
    opening: float
    fade: float
    light: float = 1.0


@dataclass(frozen=True)
class _Finish:
    at: float  # when finish() was called
    start: float  # the phase then
    end: float  # the lit phase the rest of the draw reaches
    catch_up_s: float  # how long that takes
    plain: bool  # reduced motion: the held frame fades, nothing grows


class OpeningMark(QWidget):
    """The mark, MARK_PX wide in the middle of the widget, playing its loop. It asks for
    STAGE_PX square, the room its light takes.

    start() runs the loop. finish(then) completes the draw (the rest at least twice as fast,
    adding at most 0.4 s; a glyph fading out lights again), plays the 350 ms open, then calls
    `then`. When the system asks for less motion, the held, lit frame shows and the open is a
    plain fade. The loop pauses while the widget is hidden; a finish runs to its end.
    """

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        mark_px: float = MARK_PX,
    ) -> None:
        super().__init__(parent)
        self._clock = clock
        self._mark_px = float(mark_px)
        self._started: Optional[float] = None
        self._light_in: Optional[tuple[float, float]] = None  # (from when, for how long)
        self._appear: Optional[tuple[float, float]] = None  # it fades in (appear()): as above
        # Only the tile and the glyph (paint_mark's `bare`): another mark's light shows under it.
        self.bare = False
        self._still = False
        self._held = False  # lit from the first moment: held_phase, never out (start(held=))
        self._finish: Optional[_Finish] = None
        self._done = False
        self._then: list[Callable[[], None]] = []
        self._timer = QTimer(self)
        self._timer.setInterval(FRAME_MS)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._tick)

    def sizeHint(self) -> QSize:
        return QSize(STAGE_PX, STAGE_PX)

    @staticmethod
    def reduced_motion() -> bool:
        """The system asks for less motion (macOS, Windows, GNOME's setting). It never waits:
        on Linux the first call asks on a worker and answers False until it knows."""
        return _reduced_motion()

    @property
    def origin(self) -> Optional[float]:
        """When its loop began, on its clock (None before start())."""
        return self._started

    def start(
        self, origin: Optional[float] = None, *, light_in_s: float = 0.0, held: bool = False
    ) -> None:
        """Runs the loop from its beginning: now, or at `origin`, a time on its clock (by
        default time.monotonic, one clock for every process on the computer: another process's
        mark started at the same origin shows the same frame at the same moment). While it
        loops, or while a finish runs (its `then` always comes), this does nothing; once the
        mark is done it starts again. `light_in_s`: its light and shadow come in from none
        over that long from now, eased (the splash taking over from the bootloader's still,
        which has neither); a plain fade under reduced motion too. `held`: from `origin` the
        mark plays on from the still's frame (its outline drawn whole), fills and blooms, then
        stays lit, its light breathing, until it finishes (held_phase: it never goes out and
        comes back); under reduced motion it holds the lit frame, still."""
        if not self._done and (self._started is not None or self._finish is not None):
            return
        now = self._clock()
        self._started = now if origin is None else origin
        self._light_in = (now, light_in_s) if light_in_s > 0.0 else None
        self._appear = None
        self._held = held
        self._finish = None
        self._done = False
        self._then = []
        self._still = self.reduced_motion()
        self._update_timer()
        self.update()

    def appear(self, seconds: float) -> None:
        """It fades in from nothing over `seconds` from now, eased: a mark that shows once
        another has gone (the splash's, ui/overlays/opening.py), never at full strength at
        once. A plain fade under reduced motion too."""
        self._appear = (self._clock(), seconds) if seconds > 0.0 else None
        self._update_timer()
        self.update()

    def finish(self, then: Callable[[], None]) -> None:
        """Completes the draw, opens, then calls `then`, always later from the event loop: a
        mark already done calls it on the loop's next turn, and a second call during a finish
        adds its `then` to the same end."""
        if self._done:
            QTimer.singleShot(0, then)
            return
        self._then.append(then)
        if self._finish is not None:
            return
        now = self._clock()
        if self._still or self.reduced_motion():
            self._still = True
            self._finish = _Finish(now, STILL_T, STILL_T, 0.0, plain=True)
        else:
            if self._started is None:
                self._started = now
            phase = self._phase(now)
            if phase < FILLED_AT:
                end, rest = FILLED_AT, (FILLED_AT - phase) * LOOP_S
            elif phase > LIT_UNTIL:
                end, rest = LIT_UNTIL, (phase - LIT_UNTIL) * LOOP_S
            else:
                end, rest = phase, 0.0
            catch_up = min(rest / CATCH_UP_SPEED, CATCH_UP_MAX_S)
            self._finish = _Finish(now, phase, end, catch_up, plain=False)
        self._update_timer()
        self.update()

    def frame(self, now: Optional[float] = None) -> Frame:
        """What the mark shows at `now` (the clock's time; by default, now)."""
        now = self._clock() if now is None else now
        frame = self._frame(now)._replace(light=self._light(now))
        shown = _coming_in(self._appear, now)
        return frame._replace(fade=frame.fade * shown) if shown < 1.0 else frame

    def _light(self, now: float) -> float:
        """How much of its light shows at `now`: all of it, unless it is coming in."""
        return _coming_in(self._light_in, now)

    def _lighting(self) -> bool:
        """Its light or the mark itself comes in (the timer runs, even held still)."""
        now = self._clock()
        return _coming_in(self._light_in, now) < 1.0 or _coming_in(self._appear, now) < 1.0

    def _frame(self, now: float) -> Frame:
        finish = self._finish
        if self._done:
            return Frame(finish.end if finish else STILL_T, self._still, 1.0, 0.0)
        if finish is not None:
            elapsed = max(0.0, now - finish.at)
            if finish.plain:
                return Frame(STILL_T, True, 0.0, 1.0 - _ease_in_out(elapsed / OPEN_S))
            if elapsed < finish.catch_up_s:
                share = elapsed / finish.catch_up_s
                return Frame(finish.start + (finish.end - finish.start) * share, False, 0.0, 1.0)
            opened = _ease_in_out((elapsed - finish.catch_up_s) / OPEN_S)
            return Frame(finish.end, False, opened, 1.0)
        if self._still:
            return Frame(STILL_T, True, 0.0, 1.0)
        if self._started is None:
            return Frame(0.0, False, 0.0, 1.0)
        return Frame(self._phase(now), False, 0.0, 1.0)

    def _phase(self, now: float) -> float:
        started = now if self._started is None else self._started
        if self._held:
            return held_phase(now - started)
        return ((now - started) / LOOP_S) % 1.0

    def _tick(self) -> None:
        finish = self._finish
        if finish is not None:
            if self._clock() - finish.at >= finish.catch_up_s + OPEN_S:
                self._end()
                return
        elif not self._still and self._started is not None and self.reduced_motion():
            self._still = True  # known late (Linux asks on a worker): hold the lit frame
            self._update_timer()
        elif self._still:
            self._update_timer()  # once its light has come in, nothing moves
        self.update()

    def _end(self) -> None:
        self._done = True
        self._update_timer()
        self.update()
        callbacks, self._then = self._then, []
        for callback in callbacks:
            try:
                callback()
            except Exception:
                logger.exception("A callback after the opening mark failed")

    def _update_timer(self) -> None:
        looping = self._started is not None and (not self._still or self._lighting())
        looping = looping and not self._done
        runs = not self._done and (self._finish is not None or (looping and self.isVisible()))
        if runs and not self._timer.isActive():
            self._timer.start()
        elif not runs:
            self._timer.stop()

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        self._update_timer()

    def hideEvent(self, event: Any) -> None:
        super().hideEvent(event)
        self._update_timer()

    def paintEvent(self, event: Any) -> None:
        if self._done:
            return
        frame = self.frame()
        p = QPainter(self)
        try:
            p.setOpacity(frame.fade)
            rect = mark_rect(QRectF(self.rect()), self._mark_px)
            paint_mark(
                p,
                rect,
                frame.t,
                still=frame.still,
                opening=frame.opening,
                bare=self.bare,
                light=frame.light,
            )
        finally:
            p.end()


# ---- whether the system asks for less motion ------------------------------------------------

GSETTINGS_ANIMATIONS = ("gsettings", "get", "org.gnome.desktop.interface", "enable-animations")
# "1" or "0" decides instead of the system (tests: a CI runner's macOS asks for less motion).
REDUCED_MOTION_ENV = "QITV_REDUCED_MOTION"
GSETTINGS_TIMEOUT_S = 1.0
SPI_GETCLIENTAREAANIMATION = 0x1042


class _MotionAnswer:
    def __init__(self) -> None:
        self.reduced: Optional[bool] = None  # None until the system has answered
        self.worker: Optional[threading.Thread] = None


_motion = _MotionAnswer()


def _forget_reduced_motion() -> None:
    """Tests: ask the system again."""
    global _motion
    _motion = _MotionAnswer()


def _platform() -> str:
    return sys.platform


def _reduced_motion() -> bool:
    forced = os.environ.get(REDUCED_MOTION_ENV)
    if forced in ("0", "1"):
        return forced == "1"
    state = _motion
    if state.reduced is not None:
        return state.reduced
    system = _platform()
    if system == "darwin":
        state.reduced = _macos_reduces_motion()
        return bool(state.reduced)
    if system == "win32":
        state.reduced = _windows_reduces_motion()
        return state.reduced
    if state.worker is None:
        state.worker = threading.Thread(
            target=_ask_gsettings, args=(state,), name="qitv-reduced-motion", daemon=True
        )
        state.worker.start()
    return False


def _macos_reduces_motion() -> Optional[bool]:
    """[[NSWorkspace sharedWorkspace] accessibilityDisplayShouldReduceMotion]; None when AppKit
    isn't loaded yet (no QApplication), so it is asked again."""
    try:
        from ui import macos_window

        objc = macos_window._objc()
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        workspace_class = objc.objc_getClass(b"NSWorkspace")
        if not workspace_class:
            return None
        workspace = macos_window._send(workspace_class, b"sharedWorkspace")
        if not workspace:
            return None
        selector = b"accessibilityDisplayShouldReduceMotion"
        return bool(macos_window._send(workspace, selector, ctypes.c_bool))
    except Exception as e:  # never let a Cocoa detail stop the opening
        logger.debug("Could not ask macOS whether to reduce motion: %s", e)
        return False


def _user32() -> Any:
    return ctypes.windll.user32


def _windows_reduces_motion() -> bool:
    """Windows' "Show animations" off: SPI_GETCLIENTAREAANIMATION answers FALSE."""
    try:
        system_parameters_info = _user32().SystemParametersInfoW
        # BOOL SystemParametersInfoW(UINT uiAction, UINT uiParam, PVOID pvParam, UINT fWinIni)
        system_parameters_info.argtypes = [
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.c_void_p,
            ctypes.c_uint,
        ]
        system_parameters_info.restype = ctypes.c_long
        enabled = ctypes.c_long(1)  # a BOOL
        ok = system_parameters_info(SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(enabled), 0)
        return bool(ok) and not enabled.value
    except Exception as e:
        logger.debug("Could not ask Windows whether animations are on: %s", e)
        return False


def _ask_gsettings(state: _MotionAnswer) -> None:
    """The worker: GNOME's enable-animations, 'false' meaning less motion; no answer, motion."""
    reduced = False
    try:
        answer = subprocess.run(
            list(GSETTINGS_ANIMATIONS),
            capture_output=True,
            text=True,
            timeout=GSETTINGS_TIMEOUT_S,
            env=_system_env(),
            check=False,
        )
        reduced = answer.returncode == 0 and answer.stdout.strip() == "false"
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug("gsettings could not tell whether animations are on: %s", e)
    state.reduced = reduced


def _system_env() -> dict[str, str]:
    """The environment for the system's gsettings. A frozen Linux app's LD_LIBRARY_PATH points
    into its bundle; the one from before QiTV comes back, as external_players.player_env does
    for the players (not imported: the splash's process stays small)."""
    env = dict(os.environ)
    if getattr(sys, "frozen", False):
        original = env.get("LD_LIBRARY_PATH_ORIG")
        if original is not None:
            env["LD_LIBRARY_PATH"] = original
        else:
            env.pop("LD_LIBRARY_PATH", None)
    return env
