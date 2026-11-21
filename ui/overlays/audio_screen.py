"""The audio screen (spec §10): what the player page shows while something plays with sound and
no picture (a song, a file with no video track, a radio channel), in place of the black.

Three views, drawn from the approved mockup (its px are at a 920 px wide page, here 1 mockup
px = 100/920 u):
- Cover: the cover large in the middle, the title, then artist · album;
- Album: the cover on the left, the album's tracks on the right (the one playing marked by
  three moving bars), each a click;
- Stage: a small cover and the title top left, the words large in the middle (the line before
  dimmed above, the translation under it), and 60 bars in the cover's colours below.

Behind every view, the cover's own light, as if it lit the room: two or three large radial
gradients in its dominant colours (services.song_facts.dominant_colours), breathing slowly
(scale 1 → 1.08, opacity .5 → .65 over 7 s; 2.4 s on the Stage), under the mockup's shade. It
is drawn small and scaled up (it is all soft light), only while the screen shows and plays, and
holds still when the system asks for less motion.

The top bar is the screen's: back (‹), the Words pill (the AI captions; lit: "Words · <its
language>"), "Up next · N" (Cover, with a list) and the view switch. The bottom DOCK_FRACTION is
left for the player controls' audio dock (the transport, above this overlay); nothing of the
screen is drawn there but its light. A radio channel keeps live TV's controls instead: the top
bar sits below their title row, whether they show or not (it never moves from under the
pointer), and they leave the screen's own controls their clicks and draw nothing over them
(`follow`; 2.13.3: they took every click on the screen). In the Library's corner player
the screen is the cover and the title only (`set_compact`), and its light holds still while
its window is covered whole.

Its own controls take their clicks; every other click, double-click and wheel goes on to the
overlay host (play/pause, fullscreen and volume stay where they are on the picture), and so do
the mouse's moves (the controls show at a move). It never takes the focus.

It holds no playback state of its own: the controller says what plays (show_song, set_cover,
set_tracks, set_playing), which view shows (set_view: never a pick; a click on the switch is,
and so is a step of the Picture panel's View row by the keys and the phone, step_view: each
says so with viewPicked), the words (set_words, show_lines) and the sound's bands
(set_levels: ui.visualizer feeds them while its Stage moves, stageMoving). The AI captions feed
its lines (ui.captions_controller: a place that shows lines); while its Stage shows
(takes_words, stageShown) the captions over the picture give way.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
import math
import time
from typing import Any, Optional

from PySide6.QtCore import QEvent, QObject, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QImage,
    QLinearGradient,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QPixmap,
    QRadialGradient,
    QRegion,
    QTransform,
    QWheelEvent,
)
from PySide6.QtWidgets import QWidget
from shiboken6 import isValid

from services.i18n import N_, tr, tr_n, translate, upper
from ui.opening_mark import OpeningMark
from ui.overlays.base import Overlay
from ui.overlays.captions import CAPTION_FAMILY, balanced, wrap
from ui.overlays.painting import UNIT_CAP_PX, Units, draw_icon, fill_round, fmt_duration
from ui.theme import PALETTE, qcolor

VIEWS = ("cover", "album", "stage")
# The views' names: the switch's tooltips and what a screen reader says for each.
VIEW_NAMES = {
    "cover": N_("Cover"),  # i18n: the audio screen's view with the album cover large
    "album": N_("Album"),  # i18n: the audio screen's view with the album's track list
    "stage": N_("Stage"),  # i18n: the audio screen's view with large lyrics and a visualizer
}
DOCK_FRACTION = 0.18  # the page's bottom part, left to the player controls' dock
MOCK_W = 920.0  # the mockup's page width: its px are fractions of the page's width

BREATH_S = {"cover": 7.0, "album": 7.0, "stage": 2.4}  # one breath of the light
GROW, OPACITY_LOW, OPACITY_HIGH = 0.08, 0.5, 0.65  # its breath: scale 1 → 1.08, .5 → .65
STILL_OPACITY = {"cover": 0.55, "album": 0.55, "stage": 0.42}  # its strength held still
SATURATE = 1.5  # the mockup's saturate(1.5)
GLOW_FRAME_MS = {"cover": 100, "album": 100, "stage": 66}  # a slow breath needs few frames
MINI_FRAME_MS = 33  # the playing row's bars: small, so redrawn alone
MINI_PERIODS_S = (0.8, 0.55, 0.7)  # each of the three bars' rise (then fall)
MINI_STILL = (0.55, 1.0, 0.7)  # their heights held still
BACKDROP_SCALE = 4  # the light and shade are drawn this many times smaller, then scaled up
BAR_COUNT = 60
BAR_FLOOR = 0.03  # a silent band still shows a sliver
BAR_DARKEST = 0.3  # the bars' foot is no darker (HSL lightness)
LIST_FADE_ALPHA = 0.45  # a row half out of the list (more above or below it)
WHEEL_ROW = 40  # wheel units a row (a notch is 120: three rows)
LOGO_RATIO = (0.87, 1.15)  # a cover's shape outside these is a logo: fitted, not cropped
LOGO_INSET = 0.14
LABEL_MIN_PX = 8  # the stand-in's label shows only on a cover that large
COMPACT_COVER = (0.62, 0.6)  # the corner's cover: at most these fractions of its height, width
# The followed transport's events that may move its top band (it shows, fades, is resized).
_FOLLOWED = (QEvent.Type.Show, QEvent.Type.Hide, QEvent.Type.Resize, QEvent.Type.Move)

# The mockup's colours (`.qa`), beside PALETTE's.
_TOP_FILL = "rgba(255,255,255,0.08)"
_TOP_HOVER = "rgba(255,255,255,0.14)"
_BACK_GLYPH = "#cfd6dd"
_PILL_TEXT = "#d9dfe5"
_ON_FILL = "rgba(224,138,95,0.22)"
_ON_RING = "rgba(224,138,95,0.55)"
_ON_TEXT = "#ffd9c6"
_SUBTITLE = "#c3ccd5"
_CAPTION = "#a3adb8"
_ROW_TEXT = "#cdd5dd"
_ROW_DIM = "#7d8793"
_ROW_ON = "rgba(255,255,255,0.08)"
_ROW_HOVER = "rgba(255,255,255,0.05)"
_PREVIOUS_LINE = "rgba(238,242,246,0.38)"
_TRANSLATION = "#ffd9c6"
_SHADE = (11, 15, 20)  # the canvas, as the shade's rgba(11,15,20,…)
# The stand-in cover: the mockup's drawn sunset (its sky #f2994a → #d4553f → #7a2d4f, then the
# sea) in QiTV's colours: the accent, the brand, then a deep rose.
_STAND_IN_SKY = ((0.0, "#f3a56c"), (0.38, "#d8653f"), (0.55, "#7e2f45"), (0.56, "#1b2533"))
_STAND_IN_SEA = "#0e1a2a"
_STAND_IN_SUN = ("#ffe6cf", "#ffbb8c")
_STAND_IN_STRIPES = (0.336, 0.756)  # the water's light: the CSS's 42% tall band at 58%
_LABEL_FAMILIES = ["Roboto Slab", "Rockwell", "Georgia", "DejaVu Serif", "Noto Serif", "Times"]

# The light's blobs in its box (the mockup's blurred cover art, 150% of the page): (centre x,
# centre y, radius x, radius y), fractions of the box. As a blurred sky falls: the cover's most
# vivid colour high and wide (a lighter heart, its sun), the lighter of the others as a band
# across the middle (it shows at the sides), the darkest low. Drawn last to first, so the lead
# colour lies over the others.
_BLOBS = ((0.50, 0.30, 0.60, 0.333), (0.50, 0.467, 0.667, 0.147), (0.50, 0.78, 0.733, 0.367))
# Alpha out from a blob's centre: broad, then an eased tail (a blur's edge, never a line).
_BLOB_STOPS = ((0.0, 1.0), (0.35, 0.86), (0.62, 0.52), (0.84, 0.17), (1.0, 0.0))
_HEART = 0.22  # the lead colour's centre, that much towards white
BLUR_STEP = 0.7  # blurred(): two passes this × the blur apart (σ ≈ blur / 2, as CSS's)

ACCESSIBLE_NAME = N_("Now playing")  # i18n: the audio screen, for a screen reader

Colour = tuple[int, int, int]


def screen_units(width: int, height: int) -> Units:
    """The screen's units: 1% of the width, capped as the controls' are (UNIT_CAP_PX) and by a
    16:9 page as tall as this one, so a wide window lays the mockup out as on a narrower one."""
    side = min(float(width), float(UNIT_CAP_PX), height * 16 / 9 if height > 0 else width)
    return Units(width, max(1.0, side) / 100.0)


class _Mock:
    """The mockup's px at this page's size (1 mockup px = 100/920 u)."""

    def __init__(self, width: int, height: int) -> None:
        self.un = screen_units(width, height)
        self.k = self.un.u * 100.0 / MOCK_W

    def __call__(self, px: float) -> float:
        return px * self.k

    def font(
        self,
        px: float,
        weight: QFont.Weight = QFont.Weight.Normal,
        *,
        spacing_em: float = 0.0,
        family: str = "",
    ) -> QFont:
        """A font of the mockup's `px` (never under the overlays' readable minimum)."""
        font = self.un.font(px * 100.0 / MOCK_W, weight)
        if family:
            font.setFamily(family)
        if spacing_em:
            font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing_em * font.pixelSize())
        return font


def line_height(font: QFont) -> float:
    """The mockup's line box: 1.5 × the font's size (or the font's own, when taller)."""
    return max(font.pixelSize() * 1.5, QFontMetricsF(font).lineSpacing())


def snapped(rect: QRectF) -> QRect:
    """`rect` in whole pixels; two rects that touch stay apart."""
    left, top = round(rect.left()), round(rect.top())
    right, bottom = round(rect.right()), round(rect.bottom())
    return QRect(left, top, max(1, right - left), max(1, bottom - top))


def saturated(colour: QColor, factor: float = SATURATE) -> QColor:
    hue, sat, light = colour.hslHueF(), colour.hslSaturationF(), colour.lightnessF()
    return QColor.fromHslF(max(0.0, hue), min(1.0, sat * factor), light)


def vividness(colour: QColor) -> float:
    return colour.hsvSaturationF() * colour.valueF()


def mixed(a: QColor, b: QColor, t: float) -> QColor:
    """`a` moved `t` of the way to `b`."""
    return QColor(
        round(a.red() + (b.red() - a.red()) * t),
        round(a.green() + (b.green() - a.green()) * t),
        round(a.blue() + (b.blue() - a.blue()) * t),
    )


_DOTTED_I_LETTERS = frozenset("ıİğĞşŞ")  # letters only Turkish and Azerbaijani write


def name_upper(text: str) -> str:
    """An album's or song's name in capitals, as its own language writes them, not the app's:
    "Tidewick Isle" is TIDEWICK ISLE in a Turkish QiTV too, and a name with a dotless ı (or
    ğ, ş, İ) takes the dotted capital İ for its i ("Bir İz" → BİR İZ) in any language."""
    if _DOTTED_I_LETTERS.intersection(text):
        text = text.replace("i", "İ").replace("ı", "I")
    return text.upper()


def eased_breath(seconds: float, period: float) -> float:
    """0 → 1 → 0 over `period`, eased at both ends (CSS ease-in-out, near enough)."""
    return (1.0 - math.cos(2.0 * math.pi * seconds / period)) / 2.0


def resampled(levels: Sequence[float], count: int = BAR_COUNT) -> list[float]:
    """`levels` as `count` bars, each 0–1."""
    if not levels:
        return []
    n = len(levels)
    picks = (levels[min(n - 1, i * n // count)] for i in range(count))
    return [min(1.0, max(0.0, float(v))) for v in picks]


def blurred(image: QImage, blur_px: float) -> QImage:
    """`image` softened as CSS blurs by `blur_px` (σ about half of it): twice averaged down,
    then smoothly scaled back up (no blur effect: the soft shadows are made once and kept)."""
    step = max(1.0, blur_px * BLUR_STEP)
    w, h = image.width(), image.height()
    for _ in range(2):
        small = image.scaled(
            max(1, round(w / step)),
            max(1, round(h / step)),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        image = small.scaled(
            w, h, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation
        )
    return image


def top_rounded(rect: QRectF, radius: float) -> QPainterPath:
    """`rect` with its top corners rounded (a bar's: border-radius 3px 3px 0 0)."""
    r = max(0.0, min(radius, rect.width() / 2, rect.height()))
    x, y, right, bottom = rect.left(), rect.top(), rect.right(), rect.bottom()
    path = QPainterPath(QPointF(x, bottom))
    path.lineTo(x, y + r)
    path.quadTo(x, y, x + r, y)
    path.lineTo(right - r, y)
    path.quadTo(right, y, right, y + r)
    path.lineTo(right, bottom)
    path.closeSubpath()
    return path


def is_logo(image: QImage) -> bool:
    """A picture shaped as no cover is (wide, tall) or with see-through corners: a channel's
    logo, fitted on a tile rather than cropped to fill it."""
    w, h = image.width(), image.height()
    if w <= 0 or h <= 0:
        return False
    if not LOGO_RATIO[0] <= w / h <= LOGO_RATIO[1]:
        return True
    if image.hasAlphaChannel():
        corners = ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1))
        return any(image.pixelColor(x, y).alpha() < 250 for x, y in corners)
    return False


def _view_icon(p: QPainter, view: str, rect: QRectF, colour: QColor) -> None:
    """The switch's three icons, in a unit box: a cover, a cover beside its list, and bars."""
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.translate(rect.topLeft())
    p.scale(rect.width(), rect.height())
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(colour)
    if view == "cover":
        p.drawRoundedRect(QRectF(0.16, 0.16, 0.68, 0.68), 0.12, 0.12)
    elif view == "album":
        p.drawRoundedRect(QRectF(0.06, 0.24, 0.4, 0.4), 0.07, 0.07)
        for y in (0.26, 0.47, 0.68):
            p.drawRoundedRect(QRectF(0.56, y, 0.38, 0.1), 0.05, 0.05)
    else:  # stage: the bars, as the visualizer's
        for x, h in ((0.1, 0.42), (0.32, 0.72), (0.54, 0.54), (0.76, 0.3)):
            p.drawRoundedRect(QRectF(x, 0.86 - h, 0.15, h), 0.05, 0.05)
    p.restore()


def paint_stand_in(p: QPainter, rect: QRectF, side: float, label: str) -> None:
    """The mockup's drawn cover in QiTV's colours: a sunset over the sea, the sun, its light on
    the water, and `label` (an album's name in capitals) in the label's small capitals. `rect`
    is in the pixmap's device pixels; `side` is the cover's size on the page (the label shows
    on a large one only, whatever the screen's pixel ratio). The Library's Folders tab draws a
    folder without a cover of its own so too (Task 12)."""
    s = rect.width()
    sky = QLinearGradient(rect.topLeft(), rect.bottomLeft())
    for at, colour in _STAND_IN_SKY:
        sky.setColorAt(at, QColor(colour))
    sky.setColorAt(1.0, QColor(_STAND_IN_SEA))
    p.fillRect(rect, sky)
    # The light on the water: 2 px lines every 9 px (at 200 px) over the CSS's band.
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(255, 255, 255, 26))
    y, end = s * _STAND_IN_STRIPES[0], s * _STAND_IN_STRIPES[1]
    step, line = s * 9 / 200, s * 2 / 200
    while y < end:
        p.drawRect(QRectF(0, y, s, min(line, end - y)))
        y += step
    # The sun: radial-gradient(circle at 50% 36%, #ffe2a8 0 15%, #ffb36b 16%, transparent 17%)
    centre = QPointF(s * 0.5, s * 0.36)
    reach = math.hypot(s * 0.5, s * 0.64)  # to the farthest corner
    sun = QRadialGradient(centre, reach * 0.17)
    core, rim = (QColor(c) for c in _STAND_IN_SUN)
    sun.setColorAt(0.0, core)
    sun.setColorAt(0.15 / 0.17, core)
    sun.setColorAt(0.16 / 0.17, rim)
    clear = QColor(rim)
    clear.setAlpha(0)
    sun.setColorAt(1.0, clear)
    p.setBrush(QBrush(sun))
    p.drawEllipse(centre, reach * 0.17, reach * 0.17)
    if not label or side * 11 / 200 < LABEL_MIN_PX:
        return
    px = s * 11 / 200  # drawn in device pixels
    font = QFont()
    font.setFamilies(_LABEL_FAMILIES)
    font.setStyleHint(QFont.StyleHint.Serif)
    font.setWeight(QFont.Weight.Bold)
    font.setPixelSize(round(px))
    font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.14 * px)
    p.setFont(font)
    p.setPen(QColor(255, 255, 255, 217))
    box = QRectF(s * 0.08, s * 0.93 - px * 1.5, s * 0.84, px * 1.5)
    text = QFontMetricsF(font).elidedText(label, Qt.TextElideMode.ElideRight, box.width())
    p.drawText(box, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)


@dataclass
class _Layout:
    """Where everything goes, for one size and state (the controls, hits and paint read it)."""

    view: str
    controls: dict[str, QRectF] = field(default_factory=dict)  # the clickable ones, by name
    pills: dict[str, str] = field(default_factory=dict)  # "words"/"up_next": its text
    cover: QRectF = field(default_factory=QRectF)
    title: QRectF = field(default_factory=QRectF)
    subtitle: QRectF = field(default_factory=QRectF)
    centred: bool = True  # the title's lines centred (Cover, Album) or left (Stage)
    caption: QRectF = field(default_factory=QRectF)  # Album: "ALBUM · N TRACKS"
    rows: list[tuple[int, QRectF]] = field(default_factory=list)  # (track index, its row)
    list_area: QRectF = field(default_factory=QRectF)
    first: int = 0  # the first row shown
    more_above: bool = False
    more_below: bool = False
    mini: QRectF = field(default_factory=QRectF)  # the playing row's bars
    bars: QRectF = field(default_factory=QRectF)  # Stage: the 60 bars' room
    bar_w: float = 0.0
    bar_gap: float = 0.0
    switch: QRectF = field(default_factory=QRectF)


class AudioScreen(Overlay):
    """The audio screen over the whole player page (module docstring)."""

    accessible_name = ACCESSIBLE_NAME

    viewPicked = Signal(str)  # the user picked a view on the switch ("cover", "album", "stage")
    trackPicked = Signal(int)  # an album row was clicked: its index in set_tracks' list
    wordsRequested = Signal()  # the Words pill
    upNextRequested = Signal()  # the "Up next" pill (Cover)
    backRequested = Signal()  # ‹ (the controller takes it to the Library)
    # Its Stage shows (True) or no longer does: the words are its own then (takes_words)
    stageShown = Signal(bool)
    # Its Stage shows and moves (it plays, its window shows, the system allows the motion), or
    # no longer does: the bars are fed only then (stage_moving; ui.visualizer)
    stageMoving = Signal(bool)
    # Its song, its cover or its showing changed (a song's tags and cover come after its
    # state): the system's media panel hears them (TVWindow._show_now_playing)
    shownChanged = Signal()

    def __init__(
        self, parent: Optional[QWidget] = None, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        super().__init__(parent)
        self._takes = False  # takes_words() as stageShown last said
        self._stage_moves = False  # stage_moving() as stageMoving last said
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMouseTracking(True)
        self._clock = clock
        self._title = self._artist = self._album = self._year = ""
        self._image: Optional[QImage] = None
        self._logo = False
        self._colours: list[Colour] = []
        self._cover_serial = 0
        self._tracks: list[dict] = []
        self._current = -1
        self._view = "cover"
        self._words_on = False
        self._language = ""
        self._lines = ("", "", "")
        self._levels: list[float] = []
        self._playing = False
        self._first = 0  # the album list's first row shown
        self._follow = True  # the next layout brings the playing row into view
        self._wheel_rest = 0
        self._hover: Optional[str] = None
        self._laid: Optional[_Layout] = None
        self._backdrop: Optional[QImage] = None
        # ... scaled up to the screen (from that small one, at that size), while kept
        self._backdrop_scaled: Optional[tuple[Optional[QImage], tuple[int, int], QImage]] = None
        self._covers: dict[tuple, QPixmap] = {}  # the cover drawn at a size (two views' sizes)
        self._shadows: dict[tuple, QPixmap] = {}
        self._words: Optional[tuple[tuple, QPixmap, QRectF]] = None
        self._glow_s = 0.0  # how far the light has breathed (it holds while paused)
        self._glow_at = 0.0  # the clock at its last frame
        self._mini_s = 0.0
        self._mini_at = 0.0
        self._glow_timer = QTimer(self)
        self._glow_timer.setInterval(GLOW_FRAME_MS["cover"])
        self._glow_timer.timeout.connect(self._glow_tick)
        self._mini_timer = QTimer(self)
        self._mini_timer.setInterval(MINI_FRAME_MS)
        self._mini_timer.timeout.connect(self._mini_tick)
        # The band looked at again once the page's resize is through: a hidden transport takes
        # its new size then, and hears no resize of its own until it shows (_follow_transport).
        self._band_check = QTimer(self)
        self._band_check.setSingleShot(True)
        self._band_check.setInterval(0)
        self._band_check.timeout.connect(self._follow_transport)
        self._compact = False  # the Library's corner player: the cover and the title only
        self._inset = 0  # the top bar's top, below live TV's title row while it shows (follow)
        self._transport: Any = None
        self._captions: Any = None  # the captions over the picture, given room (leave_room_for)
        self._room = False  # the last layout left the captions room (Words on)
        self._floor_seen: Optional[int] = None  # ... above this controls' top
        self._unexposed = False  # its window was found covered: the light waits for an Expose
        self._exposure: Optional[QObject] = None  # the window (QWindow) whose Expose it hears
        self._watched: Optional[QWidget] = None
        self._watch(parent)
        self.hide()

    # ---- what plays -------------------------------------------------------------------------

    def show_song(self, title: str, artist: str, album: str, *, year: str = "") -> None:
        """The song (or channel) playing: its title, artist, album and year ("" for none)."""
        self._title, self._artist, self._album, self._year = title, artist, album, year
        self.setAccessibleDescription(" · ".join(t for t in (title, artist, album) if t))
        self._covers.clear()  # the stand-in's label
        self._relayout()
        self.shownChanged.emit()

    def set_cover(self, image: Optional[QImage], colours: Sequence[Colour]) -> None:
        """Its cover (None: the drawn stand-in) and the colours its light takes (1 to 3,
        services.song_facts.dominant_colours; none: QiTV's)."""
        self._image = image if image is not None and not image.isNull() else None
        self._logo = self._image is not None and is_logo(self._image)
        self._colours = [(int(c[0]), int(c[1]), int(c[2])) for c in list(colours)[:3]]
        self._cover_serial += 1
        self._covers.clear()
        self._backdrop = None
        self.update()
        self.shownChanged.emit()

    def cover(self) -> Optional[QImage]:
        """The cover it shows (None: its drawn stand-in)."""
        return self._image

    def set_tracks(self, tracks: Sequence[dict], current: int) -> None:
        """The album's tracks (services.song_facts' Track, in their order) and the one playing
        (its index; -1 for none). Fewer than two: no list."""
        self._tracks = list(tracks)
        self._current = current
        self._follow = True
        self._relayout()
        self._update_timers()

    def update_tracks(self, tracks: Sequence[dict]) -> None:
        """The same tracks, their tags read since (as many, in their order: a playlist's): the
        rows say them where the list stands (one scrolled by hand stays where it was)."""
        if len(tracks) != len(self._tracks):
            self.set_tracks(tracks, self._current)
            return
        self._tracks = list(tracks)
        self._relayout()

    def set_view(self, view: str) -> None:
        """Show `view` ("cover", "album" or "stage"). The controller's choice: no viewPicked.
        Album with no list draws the Cover view (view() still says "album")."""
        if view not in VIEWS:
            raise ValueError(f"Unknown audio view: {view!r}")
        if view == self._view:
            return
        self._view = view
        self._glow_timer.setInterval(GLOW_FRAME_MS[view])
        self._backdrop = None
        self._follow = True
        self._relayout()
        self._update_timers()
        self._say_stage()

    def view(self) -> str:
        return self._view

    def views(self) -> tuple[str, ...]:
        """The views it can show now, as its switch offers them (Album only with a list)."""
        return tuple(view for view in VIEWS if view != "album" or self._has_list())

    def step_view(self, delta: int) -> None:
        """The view `delta` places on from the one shown, among views() and round the ends,
        picked as on the switch (viewPicked): the Picture panel's View row, by the keys and
        the phone (2.13.3: the switch was the mouse's alone)."""
        views = self.views()
        shown = self.shown_view()
        at = views.index(shown) if shown in views else 0
        self._pick(views[(at + delta) % len(views)])

    def _pick(self, view: str) -> None:
        self.set_view(view)
        self.viewPicked.emit(view)

    def takes_words(self) -> bool:
        """Its Stage shows (it is shown, and draws the Stage): the words are its own, and the
        captions over its page give way (ui.captions_controller, show_lines)."""
        return not self.isHidden() and self.shown_view() == "stage"

    def setVisible(self, visible: bool) -> None:  # show()/hide() route through here
        super().setVisible(visible)
        self._say_stage()
        self._leave_controls()
        self.shownChanged.emit()

    def _say_stage(self) -> None:
        takes = self.takes_words()
        if takes != self._takes:
            self._takes = takes
            self.stageShown.emit(takes)
        self._say_moving()

    def stage_moving(self) -> bool:
        """Its Stage shows and moves: shown, on the Stage, playing, its window not covered, and
        the system allows the motion (the bars move only then: ui.visualizer)."""
        return self._moving() and self.takes_words()

    def _say_moving(self) -> None:
        moves = self.stage_moving()
        if moves != self._stage_moves:
            self._stage_moves = moves
            self.stageMoving.emit(moves)

    def shown_view(self) -> str:
        """The view drawn: view(), but Cover for Album with no list, and in the corner."""
        if self._compact or (self._view == "album" and not self._has_list()):
            return "cover"
        return self._view

    def set_compact(self, compact: bool) -> None:
        """The Library's corner player (True): the cover and the title only, filling the small
        page, with no top bar and no room for the dock (the corner has its own controls)."""
        if compact != self._compact:
            self._compact = compact
            self._backdrop = None
            self._follow = True
            self._relayout()
            self._update_timers()
            self._say_stage()

    def compact(self) -> bool:
        return self._compact

    def follow(self, transport: Any) -> None:
        """Lay the top bar out below `transport`'s top band where it has one: live TV's title
        row and clock, over a radio channel (Transport.top_band_bottom: 0 for a film's controls
        and the audio dock), whether the controls show or not, so that it never moves from
        under the pointer as they come and fade (2.13.3). It follows the band (bandChanged),
        and tells `transport` where the screen's own controls are while it shows
        (Transport.leave_to_screen): over them it takes no click and draws nothing."""
        self._transport = transport
        transport.installEventFilter(self)
        transport.bandChanged.connect(self._follow_transport)
        self._follow_transport()

    def _follow_transport(self) -> None:
        transport = self._transport
        inset = 0
        if transport is not None and isValid(transport):
            inset = max(0, int(transport.top_band_bottom()) - self.geometry().top())
        if inset != self._inset:
            self._inset = inset
            self._relayout()
            self._update_timers()
        elif self._room and self._controls_top() != self._floor_seen:
            self._relayout()  # the room left for the captions follows the controls' top

    def _leave_controls(self) -> None:
        """Tell the followed transport where this screen's own controls are (none while it is
        hidden): live TV's controls over a radio channel leave them their clicks."""
        transport = self._transport
        leave = getattr(transport, "leave_to_screen", None)
        if leave is None or not isValid(transport):
            return
        region = QRegion()
        if not self.isHidden():
            offset = self.geometry().topLeft() - transport.geometry().topLeft()
            region = self.hole_claims().translated(offset)
        leave(region)

    def leave_room_for(self, captions: Any) -> None:
        """While Words is on, Cover and Album lift the cover and its title, and Album's list ends
        higher, so that two lines of `captions` (the captions over the picture:
        CaptionsOverlay.reach, at their real size) fit between them and the player controls;
        Words off, nothing moves (ruling P4-10). The
        room is Words', not a caption's: nothing moves as captions come and go."""
        self._captions = captions
        self._relayout()

    def _controls_top(self) -> Optional[int]:
        """The player controls' top (this screen's coordinates) the captions sit on: the dock's,
        or live TV's as when they show (so nothing moves as they fade); None without them."""
        transport = self._transport
        if transport is None or not isValid(transport) or transport.width() <= 0:
            return None
        return int(transport.controls_top()) - self.geometry().top()

    def _column_bottom(self, m: _Mock, dock_top: float) -> float:
        """Where Cover's column and Album's two end: `dock_top`, or with Words on, above two
        lines of the captions over the picture (`leave_room_for`)."""
        captions = self._captions
        self._room = self._words_on and captions is not None and isValid(captions)
        if not self._room:
            return dock_top
        floor = self._controls_top()
        self._floor_seen = floor
        top = (floor if floor is not None and floor > 0 else dock_top) - float(captions.reach())
        return min(dock_top, top - m(8))

    def set_words(self, on: bool, language: str) -> None:
        """Words (the AI captions) on or off, and the language they're shown in."""
        self._words_on, self._language = on, language
        self._relayout()

    def show_lines(self, previous: str, current: str, translation: str) -> None:
        """The Stage's words: the line before (dimmed), the line said, and its translation
        ("" for none)."""
        lines = (previous, current, translation)
        if lines == self._lines:
            return
        if not self.isVisible():  # drawn when it shows
            self._lines, self._words = lines, None
            return
        old = self._words_rect()
        self._lines = lines
        self._words = None
        self._update_rect(old.united(self._words_rect()))

    def clear_lines(self) -> None:
        self.show_lines("", "", "")

    def set_levels(self, bands: Optional[Sequence[float]]) -> None:
        """The Stage's bars: BAR_COUNT band levels, 0–1, at the player's position (None: no
        bars, as on a radio channel)."""
        self._levels = resampled(bands) if bands else []
        if self.shown_view() == "stage":
            self._update_rect(self._layout().bars.adjusted(-2, -2, 2, 2))

    def set_playing(self, playing: bool) -> None:
        """Whether it plays: the light breathes and the playing row's bars move only then."""
        if playing != self._playing:
            self._playing = playing
            self._update_timers()
            self._update_rect(self._layout().mini)

    def playing(self) -> bool:
        """What it shows plays (set_playing): not paused, ended or stopped."""
        return self._playing

    def animating(self) -> bool:
        """Its light breathes (it shows, plays, and the system allows the motion)."""
        return self._glow_timer.isActive()

    # ---- for the controller, the mouse audit and tests ------------------------------------

    def controls(self) -> dict[str, QRect]:
        """Its controls by name: back, words, up_next, view_cover/album/stage and track_<i>
        (the album's rows that show)."""
        return {name: snapped(rect) for name, rect in self._layout().controls.items()}

    def cover_rect(self) -> QRect:
        return snapped(self._layout().cover)

    def hole_claims(self) -> QRegion:
        """Where it takes clicks over a picture-in-picture (OverlayHost.set_hole): only its
        controls; elsewhere the small picture shows and takes them."""
        region = QRegion()
        for rect in self.controls().values():
            region = region.united(QRegion(rect))
        return region

    # ---- state helpers ----------------------------------------------------------------------

    def _has_list(self) -> bool:
        return len(self._tracks) >= 2

    def _relayout(self) -> None:
        self._laid = None
        self._words = None
        self.update()
        self._leave_controls()  # (they may have moved)

    def _update_rect(self, rect: QRectF) -> None:
        if not rect.isEmpty():
            self.update(rect.toAlignedRect())

    def _watch(self, parent: Optional[QWidget]) -> None:
        """Follow `parent`'s size: the screen covers it."""
        if self._watched is not None:
            self._watched.removeEventFilter(self)
        self._watched = parent
        if parent is not None:
            parent.installEventFilter(self)
            self._fit()

    def _fit(self) -> None:
        parent = self.parentWidget()
        if parent is not None and self.geometry() != parent.rect():
            self.setGeometry(parent.rect())

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.ParentChange and hasattr(self, "_watched"):
            self._watch(self.parentWidget())  # OverlayHost.add_overlay re-parents it
        return super().event(event)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        kind = event.type()
        if watched is self._watched and kind == QEvent.Type.Resize:
            self._fit()
        elif watched is self._transport and kind in _FOLLOWED:
            self._follow_transport()
        elif watched is self._exposure and kind == QEvent.Type.Expose:
            if getattr(watched, "isExposed", lambda: True)():
                self._window_exposed()
        return False

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._laid = None
        self._backdrop = None
        self._words = None
        self._shadows.clear()
        self._leave_controls()
        self._follow_transport()  # the band at the new size (the transport may follow later)
        self._band_check.start()

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        self._fit()
        self._watch_exposure()
        self._unexposed = False  # shown again (a minimised window restored): it breathes
        self._follow_transport()
        self._update_timers()

    def hideEvent(self, event: Any) -> None:
        super().hideEvent(event)
        self._backdrop_scaled = None  # (its memory back)
        self._update_timers()
        if self._hover is not None:
            self._hover = None
            self.unsetCursor()

    # ---- motion -------------------------------------------------------------------------------

    def _moving(self) -> bool:
        return (
            self.isVisible()
            and self._playing
            and not self._unexposed
            and not OpeningMark.reduced_motion()
        )

    def _exposed(self) -> bool:
        """Its window shows on the screen (not fully covered: macOS says so). Hidden or
        minimised, the screen is hidden already."""
        handle = self.window().windowHandle()
        return handle is None or handle.isExposed()

    def _watch_exposure(self) -> None:
        """Hear its window's Expose (the light breathes again once it is uncovered)."""
        handle = self.window().windowHandle()
        if handle is not None and handle is not self._exposure:
            if self._exposure is not None and isValid(self._exposure):
                self._exposure.removeEventFilter(self)
            handle.installEventFilter(self)
            self._exposure = handle

    def _window_exposed(self) -> None:
        """Its window shows again (an Expose): the light breathes on, if it plays."""
        if self._unexposed:
            self._unexposed = False
            self._update_timers()

    def _update_timers(self) -> None:
        moving = self._moving()
        if moving and not self._glow_timer.isActive():
            self._glow_at = self._clock()
            self._glow_timer.start()
        elif not moving and self._glow_timer.isActive():
            self._glow_timer.stop()
            self._backdrop = None  # held still: as reduced motion draws it, when it asks
        mini = moving and not self._layout().mini.isEmpty()
        if mini and not self._mini_timer.isActive():
            self._mini_at = self._clock()
            self._mini_timer.start()
        elif not mini:
            self._mini_timer.stop()
        self._say_moving()

    def _glow_tick(self) -> None:
        if OpeningMark.reduced_motion():  # known late (Linux asks on a worker): hold it still
            self._update_timers()
            self.update()
            return
        if not self._exposed():  # covered whole: no frames until its window shows again
            self._unexposed = True
            self._watch_exposure()
            self._update_timers()
            return
        now = self._clock()
        self._glow_s += max(0.0, now - self._glow_at)
        self._glow_at = now
        self._backdrop = None
        self.update()

    def _mini_tick(self) -> None:
        now = self._clock()
        self._mini_s += max(0.0, now - self._mini_at)
        self._mini_at = now
        self._update_rect(self._layout().mini)

    def _breath(self) -> tuple[float, float]:
        """The light's scale and opacity now."""
        view = self.shown_view()
        if OpeningMark.reduced_motion():
            return 1.0, STILL_OPACITY[view]
        b = eased_breath(self._glow_s, BREATH_S[view])
        return 1.0 + GROW * b, OPACITY_LOW + (OPACITY_HIGH - OPACITY_LOW) * b

    def _mini_heights(self) -> tuple[float, ...]:
        if OpeningMark.reduced_motion():
            return MINI_STILL
        return tuple(
            0.25 + 0.75 * eased_breath(self._mini_s, 2 * period) for period in MINI_PERIODS_S
        )

    # ---- layout -------------------------------------------------------------------------------

    def _layout(self) -> _Layout:
        if self._laid is None:
            self._laid = self._lay_out()
        return self._laid

    def _lay_out(self) -> _Layout:
        w, h = self.width(), self.height()
        m = _Mock(w, h)
        view = self.shown_view()
        lay = _Layout(view)
        self._room = False  # (Cover and Album: _column_bottom)
        if self._compact:
            self._lay_out_compact(lay, m, w, h)
            return lay
        self._lay_out_top(lay, m, w)
        dock_top = h * (1 - DOCK_FRACTION)
        top_bar = lay.controls["back"].bottom()
        # The cover and the list are laid out as on a song's screen, below the bar where it
        # rests at the top. Over a radio channel the bar sits lower, below live TV's title row
        # (follow): only Stage's small head, which it would cover, moves down with it.
        resting = top_bar - (lay.controls["back"].top() - m(14))
        if view == "cover":
            self._lay_out_cover(lay, m, w, resting, self._column_bottom(m, dock_top))
        elif view == "album":
            self._lay_out_album(lay, m, w, h, resting, dock_top, self._column_bottom(m, dock_top))
        else:
            self._lay_out_stage(lay, m, w, h, top_bar, dock_top)
        return lay

    def _lay_out_compact(self, lay: _Layout, m: _Mock, w: int, h: int) -> None:
        """The corner player's: the cover and the title, in the middle of the whole page."""
        t1 = line_height(m.font(22, QFont.Weight.Bold))
        gap = m(16)
        side = max(m(48), min(h * COMPACT_COVER[0], w * COMPACT_COVER[1], h - t1 - gap))
        y = max(0.0, (h - side - gap - t1) / 2)
        lay.cover = QRectF((w - side) / 2, y, side, side)
        text_w = w * 0.9
        lay.title = QRectF((w - text_w) / 2, lay.cover.bottom() + gap, text_w, t1)

    def _lay_out_top(self, lay: _Layout, m: _Mock, w: int) -> None:
        # Below live TV's title row while it shows over a radio channel (follow).
        top, size, gap = max(m(14), float(self._inset)), m(30), m(10)
        lay.controls["back"] = QRectF(m(18), top, size, size)
        pad = m(2)
        segment = size - 2 * pad + m(4)
        switch_w = 2 * pad + 3 * segment
        switch = QRectF(w - m(18) - switch_w, top, switch_w, size)
        lay.switch = switch
        for i, view in enumerate(VIEWS):
            if view == "album" and not self._has_list():
                continue
            rect = QRectF(switch.left() + pad + i * segment, top + pad, segment, size - 2 * pad)
            lay.controls[f"view_{view}"] = rect
        font = m.font(12)
        fm = QFontMetricsF(font)
        x = switch.left() - gap
        pills = [("words", self._words_text())]
        if lay.view == "cover" and self._has_list():
            pills.append(("up_next", tr("Up next · {n}", n=len(self._tracks) - 1)))
        for name, text in reversed(pills):
            width = fm.horizontalAdvance(text) + 2 * m(12)
            rect = QRectF(x - width, top, width, size)
            lay.controls[name] = rect
            lay.pills[name] = text
            x = rect.left() - gap

    def _words_text(self) -> str:
        if self._words_on and self._language:
            # i18n: the audio screen's pill, lit: a song's words (AI captions) in {language}
            return tr("Words · {language}", language=self._language)
        return tr("Words")  # i18n: the audio screen's pill: a song's words, by AI captions

    def _info_heights(self, m: _Mock, title_px: float) -> tuple[float, float]:
        return line_height(m.font(title_px, QFont.Weight.Bold)), line_height(m.font(13.5))

    def _lay_out_cover(self, lay: _Layout, m: _Mock, w: int, top_bar: float, bottom: float) -> None:
        # The cover, the title and artist · album, in the middle above `bottom` (the dock's top,
        # or the captions' room: _column_bottom).
        t1, t2 = self._info_heights(m, 22)
        info = m(16) + t1 + m(3) + t2
        room = bottom - top_bar - 2 * m(10)
        side = max(m(48), min(m(200), room - info))
        y = max(top_bar + m(10), (bottom - side - info) / 2)
        lay.cover = QRectF((w - side) / 2, y, side, side)
        text_w = min(w * 0.8, m(560))
        lay.title = QRectF((w - text_w) / 2, lay.cover.bottom() + m(16), text_w, t1)
        lay.subtitle = QRectF(lay.title.left(), lay.title.bottom() + m(3), text_w, t2)

    def _lay_out_album(
        self,
        lay: _Layout,
        m: _Mock,
        w: int,
        h: int,
        top_bar: float,
        dock_top: float,
        column_bottom: float,
    ) -> None:
        # Left (44%): the cover and its info, in the middle of the mockup's padding, above
        # `column_bottom` (the dock's top, or the captions' room: _column_bottom).
        t1, t2 = self._info_heights(m, 22)
        info = m(14) + t1 + m(3) + t2
        box = QRectF(m(30), max(m(30), top_bar), w * 0.44 - m(40), 0.0)
        box.setBottom(min(h - m(120), dock_top, column_bottom))
        side = max(m(48), min(m(200), box.width(), box.height() - info))
        y = box.top() + max(0.0, (box.height() - side - info) / 2)
        lay.cover = QRectF(box.center().x() - side / 2, y, side, side)
        lay.title = QRectF(box.left(), lay.cover.bottom() + m(14), box.width(), t1)
        lay.subtitle = QRectF(box.left(), lay.title.bottom() + m(3), box.width(), t2)
        # Right (56%): "ALBUM · N TRACKS", then a row per track.
        right = QRectF(w * 0.44 + m(18), max(m(62), top_bar + m(12)), 0.0, 0.0)
        right.setRight(w - m(34))
        right.setBottom(min(h - m(110), dock_top, column_bottom))  # Words: above the captions
        cap = line_height(m.font(10.5, spacing_em=0.14))
        lay.caption = QRectF(right.left() + m(10), right.top(), right.width() - m(10), cap)
        row_h = 2 * m(7) + line_height(m.font(13))
        area = QRectF(right.left(), lay.caption.bottom() + m(8), right.width(), 0.0)
        area.setBottom(max(area.top() + row_h, right.bottom()))
        lay.list_area = area
        shown = max(1, int(area.height() // row_h))
        count = len(self._tracks)
        if self._follow and 0 <= self._current < count:
            if self._current < self._first:
                self._first = self._current
            elif self._current >= self._first + shown:
                self._first = self._current - shown + 2
        self._follow = False
        self._first = max(0, min(self._first, count - shown))
        lay.first = self._first
        for row, index in enumerate(range(self._first, min(count, self._first + shown))):
            rect = QRectF(area.left(), area.top() + row * row_h, area.width(), row_h)
            lay.rows.append((index, rect))
            lay.controls[f"track_{index}"] = rect
            if index == self._current:
                lay.mini = QRectF(rect.left() + m(10), rect.center().y() - m(6), m(13), m(12))
        lay.more_above = self._first > 0
        lay.more_below = self._first + shown < count

    def _lay_out_stage(
        self, lay: _Layout, m: _Mock, w: int, h: int, top_bar: float, dock_top: float
    ) -> None:
        side = m(84)
        lay.cover = QRectF(m(22), max(m(58), top_bar + m(14)), side, side)
        t1, t2 = self._info_heights(m, 18)
        block = t1 + m(3) + t2
        x = lay.cover.right() + m(14)
        lay.title = QRectF(x, lay.cover.center().y() - block / 2, w * 0.6 - x, t1)
        lay.subtitle = QRectF(x, lay.title.bottom() + m(3), lay.title.width(), t2)
        lay.centred = False
        bar_w, gap = m(9), m(5)
        total = BAR_COUNT * bar_w + (BAR_COUNT - 1) * gap
        if total > w * 0.96:
            shrink = w * 0.96 / total
            bar_w, gap, total = bar_w * shrink, gap * shrink, total * shrink
        bottom = min(h - m(122), dock_top - m(8))
        lay.bars = QRectF((w - total) / 2, bottom - m(96), total, m(96))
        lay.bar_w, lay.bar_gap = bar_w, gap

    # ---- the mouse ----------------------------------------------------------------------------

    def _control_at(self, pos: QPointF) -> Optional[str]:
        for name, rect in self._layout().controls.items():
            if rect.contains(pos):
                return name
        return None

    def mousePressEvent(self, event: QMouseEvent) -> None:
        name = None
        if event.button() == Qt.MouseButton.LeftButton:
            name = self._control_at(event.position())
        if name is None:
            event.ignore()  # the host's: play/pause, the context menu, Back/Forward
            return
        event.accept()
        self._activate(name)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self._control_at(event.position()) is not None
        ):
            event.accept()  # a quick second click on a control: never fullscreen
            return
        event.ignore()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        self._set_hover(self._control_at(event.position()))
        event.ignore()  # the host shows the controls at a move

    def leaveEvent(self, event: QEvent) -> None:
        self._set_hover(None)
        super().leaveEvent(event)

    def wheelEvent(self, event: QWheelEvent) -> None:
        lay = self._layout()
        count = len(self._tracks)
        scrolls = lay.view == "album" and (lay.more_above or lay.more_below)
        if not scrolls or not lay.list_area.contains(event.position()):
            event.ignore()  # the host's: the volume
            return
        event.accept()
        self._wheel_rest += event.angleDelta().y()
        rows = int(self._wheel_rest / WHEEL_ROW)
        if rows:
            self._wheel_rest -= rows * WHEEL_ROW
            first = max(0, min(self._first - rows, count - len(lay.rows)))
            if first != self._first:
                self._first = first
                self._relayout()
                self._update_timers()
                self._set_hover(self._control_at(event.position()))

    def _set_hover(self, name: Optional[str]) -> None:
        if name == self._hover:
            return
        controls = self._layout().controls
        for old in (self._hover, name):
            if old in controls:
                self._update_rect(controls[old].adjusted(-2, -2, 2, 2))
        self._hover = name
        if name is None:
            self.unsetCursor()
        else:
            self.setCursor(Qt.CursorShape.PointingHandCursor)

    def _activate(self, name: str) -> None:
        if name == "back":
            self.backRequested.emit()
        elif name == "words":
            self.wordsRequested.emit()
        elif name == "up_next":
            self.upNextRequested.emit()
        elif name.startswith("view_"):
            self._pick(name[len("view_") :])
        elif name.startswith("track_"):
            self.trackPicked.emit(int(name[len("track_") :]))

    def tooltip_at(self, pos: QPointF) -> Optional[str]:
        name = self._control_at(pos)
        if name == "back":
            return tr("Back to the Library")
        if name == "words":
            return tr("AI captions")
        if name is not None and name.startswith("view_"):
            return translate(VIEW_NAMES[name[len("view_") :]])
        if name is not None and name.startswith("track_"):
            return self._row_title(self._tracks[int(name[len("track_") :])])
        return ""

    # ---- painting -----------------------------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:
        dirty = event.rect()
        lay = self._layout()
        m = _Mock(self.width(), self.height())
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            # the light, from its copy at the screen's size: a step from it to the device's
            # pixels needs no smoothing (soft light: a 2x step is invisible), a third of its
            # cost at DPR 2 on hel
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
            p.drawImage(QRectF(self.rect()), self._backdrop_full())
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            self._paint_cover(p, lay, m, dirty)
            self._paint_info(p, lay, m, dirty)
            if lay.view == "album":
                self._paint_list(p, lay, m, dirty)
            elif lay.view == "stage":
                self._paint_words(p, dirty)
                self._paint_bars(p, lay, m, dirty)
            self._paint_top(p, lay, m, dirty)
        finally:
            p.end()

    # the light and its shade

    def _backdrop_image(self) -> QImage:
        if self._backdrop is None or self._backdrop.isNull():
            self._backdrop = self._render_backdrop()
        return self._backdrop

    def _backdrop_full(self) -> QImage:
        """The light at the screen's size (its own pixels, not the device's: at DPR 2 a
        quarter of the memory, 3.7 MB at 1280x720), drawn into the same image at each light
        frame: a repaint of a part of the screen (the Stage's bars, 30 a second) copies its
        part. Let go while the screen is hidden."""
        small = self._backdrop_image()
        size = (max(1, self.width()), max(1, self.height()))
        held = self._backdrop_scaled
        if held is None or held[1] != size:
            held = (None, size, QImage(size[0], size[1], QImage.Format.Format_RGB32))
        if held[0] is not small:
            p = QPainter(held[2])
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            p.drawImage(QRectF(0, 0, size[0], size[1]), small)
            p.end()
        self._backdrop_scaled = (small, size, held[2])
        return held[2]

    def _glow_colours(self) -> list[QColor]:
        """The light's three colours, the most vivid first; one or two are made three."""
        given = [saturated(QColor(*c)) for c in self._colours] or [QColor(PALETTE.accent)]
        index = max(range(len(given)), key=lambda i: (vividness(given[i]), -i))
        lead = given[index]
        rest = [c for i, c in enumerate(given) if i != index]
        if not rest:
            rest.append(lead.darker(135))
        if len(rest) == 1:
            rest.append(mixed(lead, rest[0], 0.5))
        rest.sort(key=QColor.lightnessF, reverse=True)  # the lighter across the middle
        return [lead, *rest]

    def _glow_box(self, w: float, h: float, view: str) -> QRectF:
        if view == "album":  # inset: -30% 40% -30% -30%: behind the cover's side
            return QRectF(-0.3 * w, -0.3 * h, 0.9 * w, 1.6 * h)
        return QRectF(-0.25 * w, -0.25 * h, 1.5 * w, 1.5 * h)

    def _render_backdrop(self) -> QImage:
        w, h = max(1, self.width()), max(1, self.height())
        small_w = max(8, math.ceil(w / BACKDROP_SCALE))
        small_h = max(8, math.ceil(h / BACKDROP_SCALE))
        view = self.shown_view()
        glow = QImage(small_w, small_h, QImage.Format.Format_ARGB32_Premultiplied)
        glow.fill(Qt.GlobalColor.transparent)
        p = QPainter(glow)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.scale(small_w / w, small_h / h)
        scale, opacity = self._breath()
        box = self._glow_box(w, h, view)
        centre = box.center()
        p.translate(centre)
        p.scale(scale, scale)
        p.translate(-centre)
        colours = self._glow_colours()
        for i in (2, 1, 0):  # the lead colour over the others
            bx, by, rx, ry = _BLOBS[i]
            self._paint_blob(
                p,
                QPointF(box.left() + bx * box.width(), box.top() + by * box.height()),
                rx * box.width(),
                ry * box.height(),
                colours[i],
                heart=mixed(colours[i], QColor("#ffffff"), _HEART) if i == 0 else None,
            )
        p.end()
        image = QImage(small_w, small_h, QImage.Format.Format_RGB32)
        image.fill(qcolor(PALETTE.canvas))
        p = QPainter(image)
        p.setOpacity(opacity)
        p.drawImage(0, 0, glow)
        p.setOpacity(1.0)
        p.scale(small_w / w, small_h / h)
        self._paint_shade(p, w, h, view)
        p.end()
        return image

    @staticmethod
    def _paint_blob(
        p: QPainter,
        centre: QPointF,
        rx: float,
        ry: float,
        colour: QColor,
        *,
        heart: Optional[QColor] = None,
    ) -> None:
        """One soft ellipse of light, `heart` (a lighter colour) at its centre if given."""
        gradient = QRadialGradient(centre, rx)
        for at, alpha in _BLOB_STOPS:
            stop = QColor(heart if heart is not None and at == 0.0 else colour)
            stop.setAlphaF(alpha)
            gradient.setColorAt(at, stop)
        brush = QBrush(gradient)
        stretch = QTransform().translate(centre.x(), centre.y())
        brush.setTransform(stretch.scale(1.0, ry / rx).translate(-centre.x(), -centre.y()))
        p.fillRect(QRectF(centre.x() - rx, centre.y() - ry, 2 * rx, 2 * ry), brush)

    @staticmethod
    def _paint_shade(p: QPainter, w: float, h: float, view: str) -> None:
        def shade(alpha: float) -> QColor:
            return QColor(*_SHADE, round(alpha * 255))

        if view == "album":  # linear-gradient(90deg, .2, .82 55%)
            gradient = QLinearGradient(0, 0, w, 0)
            gradient.setColorAt(0.0, shade(0.2))
            gradient.setColorAt(0.55, shade(0.82))
            gradient.setColorAt(1.0, shade(0.82))
            p.fillRect(QRectF(0, 0, w, h), gradient)
            return
        # radial-gradient(ellipse at 50% 45%, .15, .78 75%), Stage at 50% 40%: .35, .85 70%;
        # the ellipse reaches the farthest corner (rx = √2·w/2, ry = √2 × the far side).
        cy, inner, outer, reach = (
            (0.4, 0.35, 0.85, 0.7) if view == "stage" else (0.45, 0.15, 0.78, 0.75)
        )
        centre = QPointF(w / 2, cy * h)
        rx = math.sqrt(2) * w / 2
        ry = math.sqrt(2) * max(cy, 1 - cy) * h
        radial = QRadialGradient(centre, rx)
        radial.setColorAt(0.0, shade(inner))
        radial.setColorAt(reach, shade(outer))
        radial.setColorAt(1.0, shade(outer))
        brush = QBrush(radial)
        stretch = QTransform().translate(centre.x(), centre.y())
        brush.setTransform(stretch.scale(1.0, ry / rx).translate(-centre.x(), -centre.y()))
        p.fillRect(QRectF(0, 0, w, h), brush)

    # the cover

    def _paint_cover(self, p: QPainter, lay: _Layout, m: _Mock, dirty: QRect) -> None:
        cover = lay.cover
        if cover.isEmpty():
            return
        radius = m(8) if lay.view == "stage" else m(10)
        shadow, at = self._shadow(cover, radius, m)
        if dirty.intersects(QRectF(at, shadow.deviceIndependentSize()).toAlignedRect()):
            p.drawPixmap(at, shadow)
        if not dirty.intersects(cover.toAlignedRect()):
            return
        p.drawPixmap(cover.topLeft(), self._cover_pixmap(cover.width(), radius, m))
        ring = QPen(QColor(255, 255, 255, 15), 1.0)  # 0 0 0 1px rgba(255,255,255,.06)
        p.setPen(ring)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(cover.adjusted(-0.5, -0.5, 0.5, 0.5), radius + 0.5, radius + 0.5)

    def _shadow(self, cover: QRectF, radius: float, m: _Mock) -> tuple[QPixmap, QPointF]:
        """The cover's shadow (0 26px 70px rgba(0,0,0,.6)), made once a size, and where."""
        blur, drop = m(70), m(26)
        dpr = self.devicePixelRatioF()
        key = (round(cover.width() * dpr), round(radius * dpr), round(blur * dpr), dpr)
        pad = blur * 1.2
        at = QPointF(cover.left() - pad, cover.top() + drop - pad)
        shadow = self._shadows.get(key)
        if shadow is None:
            size = round((cover.width() + 2 * pad) * dpr)
            image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(Qt.GlobalColor.transparent)
            painter = QPainter(image)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, 153))
            inner = QRectF(pad * dpr, pad * dpr, cover.width() * dpr, cover.height() * dpr)
            painter.drawRoundedRect(inner, radius * dpr, radius * dpr)
            painter.end()
            shadow = QPixmap.fromImage(blurred(image, blur * dpr))
            shadow.setDevicePixelRatio(dpr)
            self._shadows = {key: shadow, **dict(list(self._shadows.items())[:3])}
        return shadow, at

    def _label(self) -> str:
        """The stand-in's label: the album's name (a cover is the album's), else the title."""
        return name_upper(self._album or self._title)

    def _cover_pixmap(self, side: float, radius: float, m: _Mock) -> QPixmap:
        dpr = self.devicePixelRatioF()
        key = (self._cover_serial, round(side * dpr), round(radius * dpr), dpr, self._label())
        pixmap = self._covers.get(key)
        if pixmap is not None:
            return pixmap
        size = max(1, round(side * dpr))
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        p = QPainter(pixmap)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            rect = QRectF(0, 0, size, size)
            clip = QPainterPath()
            clip.addRoundedRect(rect, radius * dpr, radius * dpr)
            p.setClipPath(clip)
            if self._image is None:
                self._paint_stand_in(p, rect, side)
            elif self._logo:
                self._paint_logo(p, rect, self._image)
            else:
                scaled = self._image.scaled(
                    size,
                    size,
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation,
                )
                p.drawImage(
                    QPointF((size - scaled.width()) / 2, (size - scaled.height()) / 2), scaled
                )
        finally:
            p.end()
        pixmap.setDevicePixelRatio(dpr)
        if len(self._covers) >= 4:
            self._covers.clear()
        self._covers[key] = pixmap
        return pixmap

    @staticmethod
    def _paint_logo(p: QPainter, rect: QRectF, image: QImage) -> None:
        tile = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        tile.setColorAt(0.0, QColor("#151b22"))
        tile.setColorAt(1.0, qcolor(PALETTE.panel_raised))
        p.fillRect(rect, tile)
        inner = rect.adjusted(
            rect.width() * LOGO_INSET,
            rect.height() * LOGO_INSET,
            -rect.width() * LOGO_INSET,
            -rect.height() * LOGO_INSET,
        )
        scaled = image.scaled(
            round(inner.width()),
            round(inner.height()),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        p.drawImage(
            QPointF(
                inner.x() + (inner.width() - scaled.width()) / 2,
                inner.y() + (inner.height() - scaled.height()) / 2,
            ),
            scaled,
        )

    def _paint_stand_in(self, p: QPainter, rect: QRectF, side: float) -> None:
        paint_stand_in(p, rect, side, self._label())

    # the title and artist

    def _subtitle_text(self, view: str) -> str:
        parts = [self._artist, self._album]
        if view == "album":
            parts.append(self._year)
        return " · ".join(part for part in parts if part)

    def _paint_info(self, p: QPainter, lay: _Layout, m: _Mock, dirty: QRect) -> None:
        if not dirty.intersects(lay.title.united(lay.subtitle).toAlignedRect()):
            return
        align = (
            Qt.AlignmentFlag.AlignHCenter if lay.centred else Qt.AlignmentFlag.AlignLeft
        ) | Qt.AlignmentFlag.AlignVCenter
        title_px = 18 if lay.view == "stage" else 22
        font = m.font(title_px, QFont.Weight.Bold, spacing_em=-0.01)
        for rect, text, colour, f in (
            (lay.title, self._title, qcolor(PALETTE.text), font),
            (lay.subtitle, self._subtitle_text(lay.view), QColor(_SUBTITLE), m.font(13.5)),
        ):
            if not text or rect.isEmpty():
                continue  # (the corner player's has no artist line)
            p.setFont(f)
            p.setPen(colour)
            shown = QFontMetricsF(f).elidedText(text, Qt.TextElideMode.ElideRight, rect.width())
            p.drawText(rect, align, shown)

    # the album's list

    @staticmethod
    def _row_title(track: dict) -> str:
        title = track.get("title") or ""
        if title:
            return title
        name = track.get("name") or ""
        stem, dot, _ext = name.rpartition(".")
        return stem if dot else name

    def _caption_text(self) -> str:
        count = upper(tr_n("{n} track", "{n} tracks", len(self._tracks)))
        return f"{name_upper(self._album)} · {count}" if self._album else count

    def _paint_list(self, p: QPainter, lay: _Layout, m: _Mock, dirty: QRect) -> None:
        if dirty.intersects(lay.caption.toAlignedRect()):
            font = m.font(10.5, spacing_em=0.14)
            p.setFont(font)
            p.setPen(QColor(_CAPTION))
            text = QFontMetricsF(font).elidedText(
                self._caption_text(), Qt.TextElideMode.ElideRight, lay.caption.width()
            )
            p.drawText(
                lay.caption, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text
            )
        plain, bold = m.font(13), m.font(13, QFont.Weight.DemiBold)
        numbers = m.font(13)
        if hasattr(QFont, "Tag"):  # font-variant-numeric: tabular-nums (Qt 6.7+)
            numbers.setFeature(QFont.Tag("tnum"), 1)
        last = len(lay.rows) - 1
        for row, (index, rect) in enumerate(lay.rows):
            if not dirty.intersects(rect.toAlignedRect()):
                continue
            faded = (row == 0 and lay.more_above) or (row == last and lay.more_below)
            p.setOpacity(LIST_FADE_ALPHA if faded else 1.0)
            self._paint_row(p, m, index, rect, (plain, bold, numbers))
        p.setOpacity(1.0)

    def _paint_row(
        self, p: QPainter, m: _Mock, index: int, rect: QRectF, fonts: tuple[QFont, ...]
    ) -> None:
        plain, bold, numbers = fonts
        track = self._tracks[index]
        playing = index == self._current
        hovered = self._hover == f"track_{index}"
        if playing or hovered:
            fill_round(p, rect, qcolor(_ROW_ON if playing else _ROW_HOVER), m(8))
        inner = rect.adjusted(m(10), 0, -m(10), 0)
        number_cell = QRectF(inner.left(), inner.top(), m(34), inner.height())
        time_cell = QRectF(inner.right() - m(50), inner.top(), m(50), inner.height())
        title_cell = QRectF(
            number_cell.right(), inner.top(), time_cell.left() - number_cell.right(), inner.height()
        )
        left = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        p.setFont(numbers)
        p.setPen(QColor(_ROW_DIM))
        if playing:
            self._paint_mini(p, m, number_cell)
        else:
            number = track.get("track")
            shown = str(number) if isinstance(number, int) and number > 0 else str(index + 1)
            p.drawText(number_cell, left, shown)
        duration = track.get("duration_ms")
        if isinstance(duration, int) and duration > 0:
            if playing:  # the playing row is semibold throughout (.tr.on)
                semibold = QFont(numbers)
                semibold.setWeight(QFont.Weight.DemiBold)
                p.setFont(semibold)
            p.drawText(
                time_cell,
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                fmt_duration(duration),
            )
        font = bold if playing else plain
        p.setFont(font)
        p.setPen(QColor("#ffffff") if playing else QColor(_ROW_TEXT))
        title = QFontMetricsF(font).elidedText(
            self._row_title(track), Qt.TextElideMode.ElideRight, title_cell.width() - m(8)
        )
        p.drawText(title_cell, left, title)

    def _paint_mini(self, p: QPainter, m: _Mock, cell: QRectF) -> None:
        """The playing row's three bars (the mockup's .mini), rising from the bottom."""
        bar, gap, tall = m(3), m(2), m(12)
        bottom = cell.center().y() + tall / 2
        heights = self._mini_heights() if self._playing else MINI_STILL
        accent = qcolor(PALETTE.accent)
        for i, scale in enumerate(heights):
            h = tall * scale
            fill_round(p, QRectF(cell.left() + i * (bar + gap), bottom - h, bar, h), accent, m(1))

    # the Stage's words and bars

    def _words_rect(self) -> QRectF:
        if self.shown_view() != "stage" or not any(self._lines):
            return QRectF()
        return self._words_cache()[2]

    def _words_cache(self) -> tuple[tuple, QPixmap, QRectF]:
        w, h = self.width(), self.height()
        dpr = self.devicePixelRatioF()
        key = (self._lines, w, h, dpr)
        if self._words is not None and self._words[0] == key:
            return self._words
        m = _Mock(w, h)
        previous, current, translation = self._lines
        width = w * 0.84
        parts: list[tuple[list[str], QFont, QColor, bool, float]] = []  # lines, font, …, gap
        family = CAPTION_FAMILY
        if previous:
            font = m.font(15, family=family)
            parts.append(
                (self._wrapped(previous, font, width, 1), font, qcolor(_PREVIOUS_LINE), False, 0.0)
            )
        if current:
            font = m.font(25, QFont.Weight.Bold, family=family)
            gap = m(10) if previous else 0.0
            parts.append(
                (self._wrapped(current, font, width, 2), font, qcolor(PALETTE.text), True, gap)
            )
        if translation:
            font = m.font(17, family=family)
            gap = m(6) if current else (m(10) if previous else 0.0)
            parts.append(
                (self._wrapped(translation, font, width, 2), font, QColor(_TRANSLATION), True, gap)
            )
        height = sum(gap + len(lines) * line_height(font) for lines, font, _c, _s, gap in parts)
        pad = m(18) + m(2)
        rect = QRectF(
            (w - width) / 2 - pad, 0.4 * h - height / 2 - pad, width + 2 * pad, height + 2 * pad
        )
        size_w, size_h = max(1, round(rect.width() * dpr)), max(1, round(rect.height() * dpr))
        text = QImage(size_w, size_h, QImage.Format.Format_ARGB32_Premultiplied)
        text.fill(Qt.GlobalColor.transparent)
        shade = QImage(text.size(), QImage.Format.Format_ARGB32_Premultiplied)
        shade.fill(Qt.GlobalColor.transparent)
        tp, sp = QPainter(text), QPainter(shade)
        for painter in (tp, sp):
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
            painter.scale(dpr, dpr)
        y = pad
        for lines, font, colour, shadowed, gap in parts:
            y += gap
            for line in lines:
                box = QRectF(pad, y, width, line_height(font))
                tp.setFont(font)
                tp.setPen(colour)
                tp.drawText(box, Qt.AlignmentFlag.AlignCenter, line)
                if shadowed:  # text-shadow: 0 2px 18px rgba(0,0,0,.6)
                    sp.setFont(font)
                    sp.setPen(QColor(0, 0, 0, 153))
                    sp.drawText(box.translated(0, m(2)), Qt.AlignmentFlag.AlignCenter, line)
                y += line_height(font)
        tp.end()
        sp.end()
        result = blurred(shade, m(18) * dpr)
        painter = QPainter(result)
        painter.drawImage(0, 0, text)
        painter.end()
        pixmap = QPixmap.fromImage(result)
        pixmap.setDevicePixelRatio(dpr)
        self._words = (key, pixmap, rect)
        return self._words

    @staticmethod
    def _wrapped(text: str, font: QFont, width: float, most: int) -> list[str]:
        """`text` in at most `most` balanced lines, the last cut with "…" if it must be."""
        spans = wrap(text, font, width)
        if len(spans) <= most:
            spans = wrap(text, font, balanced(text, font, width))
        lines = [text[start : start + length].strip() for start, length in spans[:most]]
        if len(spans) > most:
            rest = text[spans[most - 1][0] :].strip()
            lines[-1] = QFontMetricsF(font).elidedText(rest, Qt.TextElideMode.ElideRight, width)
        return [line for line in lines if line]

    def _paint_words(self, p: QPainter, dirty: QRect) -> None:
        if not any(self._lines):
            return
        _key, pixmap, rect = self._words_cache()
        if dirty.intersects(rect.toAlignedRect()):
            p.drawPixmap(rect.topLeft(), pixmap)

    def _bar_colours(self) -> tuple[QColor, QColor, QColor]:
        """The bars' gradient, light at the top to dark at the foot, from the cover's colours
        (the mockup's #ffd08f → #e08a5f 45% → #a33b55): the lightest towards white, the most
        vivid, then the darkest of the rest. A near-black colour (a night sky's navy) is left
        out while others remain: bars fading into it would vanish on the dark page."""
        given = [QColor(*c) for c in self._colours] or [QColor(PALETTE.accent)]
        bright = [c for c in given if c.lightnessF() >= BAR_DARKEST] or given
        mid = max(bright, key=vividness)
        rest = [c for c in bright if c is not mid]
        top = mixed(max(bright, key=QColor.lightnessF), QColor("#ffffff"), 0.4)
        foot = min(rest, key=QColor.lightnessF) if rest else mid.darker(150)
        if foot.lightnessF() < BAR_DARKEST:
            foot = QColor.fromHslF(max(0.0, foot.hslHueF()), foot.hslSaturationF(), BAR_DARKEST)
        return top, mid, foot

    def _paint_bars(self, p: QPainter, lay: _Layout, m: _Mock, dirty: QRect) -> None:
        rect = lay.bars
        if not self._levels or rect.isEmpty() or not dirty.intersects(rect.toAlignedRect()):
            return
        top, mid, foot = self._bar_colours()
        w = max(1, self.width())
        radius = min(m(3), lay.bar_w / 2)
        p.setPen(Qt.PenStyle.NoPen)
        for i, level in enumerate(self._levels):
            x = rect.left() + i * (lay.bar_w + lay.bar_gap)
            height = max(BAR_FLOOR, level) * rect.height()
            y = rect.bottom() - height
            centre = (x + lay.bar_w / 2) / w  # the mockup's edge fade: 0 → 15%, 85% → 100%
            fade = min(1.0, centre / 0.15, (1.0 - centre) / 0.15)
            if fade <= 0:
                continue
            gradient = QLinearGradient(0, y, 0, rect.bottom())
            gradient.setColorAt(0.0, top)
            gradient.setColorAt(0.45, mid)
            gradient.setColorAt(1.0, foot)
            p.setOpacity(0.9 * fade)
            p.fillPath(top_rounded(QRectF(x, y, lay.bar_w, height), radius), QBrush(gradient))
        p.setOpacity(1.0)

    # the top bar

    def _paint_top(self, p: QPainter, lay: _Layout, m: _Mock, dirty: QRect) -> None:
        if "back" not in lay.controls:
            return  # the corner player's: no top bar
        top_bar = lay.controls["back"].united(lay.switch)
        if not dirty.intersects(top_bar.adjusted(-4, -4, 4, 4).toAlignedRect()):
            return
        back = lay.controls["back"]
        hover = qcolor(_TOP_HOVER)
        fill_round(
            p, back, hover if self._hover == "back" else qcolor(_TOP_FILL), back.height() / 2
        )
        glyph = m(13)  # the mockup's ‹ at 16 px: a glyph's thin stroke, so drawn smaller
        draw_icon(
            p,
            "chevron_left",
            QRectF(back.center().x() - glyph / 2, back.center().y() - glyph / 2, glyph, glyph),
            QColor(_BACK_GLYPH),
        )
        font = m.font(12)
        p.setFont(font)
        for name, text in lay.pills.items():
            rect = lay.controls[name]
            lit = name == "words" and self._words_on
            fill = qcolor(_ON_FILL if lit else (_TOP_HOVER if self._hover == name else _TOP_FILL))
            fill_round(p, rect, fill, rect.height() / 2)
            if lit:
                self._paint_ring(p, rect)
            p.setPen(QColor(_ON_TEXT) if lit else QColor(_PILL_TEXT))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
        self._paint_switch(p, lay, m)

    @staticmethod
    def _paint_ring(p: QPainter, rect: QRectF) -> None:
        """The lit pill's inset 1 px ring."""
        p.save()
        p.setPen(QPen(qcolor(_ON_RING), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        inner = rect.adjusted(0.5, 0.5, -0.5, -0.5)
        p.drawRoundedRect(inner, inner.height() / 2, inner.height() / 2)
        p.restore()

    def _paint_switch(self, p: QPainter, lay: _Layout, m: _Mock) -> None:
        """Three icons in one pill; the view showing sits in the lit pill's accent ring."""
        switch = lay.switch
        fill_round(p, switch, qcolor(_TOP_FILL), switch.height() / 2)
        pad = m(2)
        segment = (switch.width() - 2 * pad) / 3
        shown = lay.view
        icon = m(14)
        for i, view in enumerate(VIEWS):
            cell = QRectF(
                switch.left() + pad + i * segment,
                switch.top() + pad,
                segment,
                switch.height() - 2 * pad,
            )
            name = f"view_{view}"
            if view == shown:
                d = cell.height()
                ring = QRectF(cell.center().x() - d / 2, cell.top(), d, d)
                fill_round(p, ring, qcolor(_ON_FILL), d / 2)
                self._paint_ring(p, ring)
                colour = QColor(_ON_TEXT)
            elif name not in lay.controls:
                colour = qcolor(_PILL_TEXT, 0.25)  # Album, with no list to show
            else:
                colour = qcolor(_PILL_TEXT, 1.0 if self._hover == name else 0.7)
            _view_icon(
                p,
                view,
                QRectF(cell.center().x() - icon / 2, cell.center().y() - icon / 2, icon, icon),
                colour,
            )
