"""Transport overlay: title row on top, seek line and buttons at the bottom.

Modes: VOD, plain live (direct play) and time-shift (live through the pipeline). Time-shift shows
the bar over the buffered range, or, while the session's stay-live mode is LIVE, the stay-live
latency indicator (it reads "catching up" while stay-live nudges the rate up, "adding buffer"
while it nudges it down, and "Reconnecting…" while the source has stalled). It shows the settled
lag (2.1: in half seconds, changed only once the lag moved for a few seconds): "≈ 2.5 s behind
source"; under Auto latency "≈ 7 s behind", with "adjusted" while trouble holds the channel's
learned target up (its starvation prior's margin; not what its arrivals need). Its dot is green
while stay-live plays at 1x and amber whenever the rate is another (0.95x included) or the
source stalled. The bar and the Go live chip show only when the viewer left live (shifted, or a
chosen speed); a drag keeps the bar.

Left of fullscreen sit the mini player (the whole window small in a corner, Alt+P), on live
TV while the window offers picture in picture the second channel (P), then "Open in external
player" (the shell's VLC/MPV menu for what plays now). Hovering those names them and their keys.
Left of the sound (♫) a speaker opens the volume mixer on the large picture's row; it shows
whether that picture is muted. ♫ opens the Sound panel above it and the subtitles button
the Subtitles panel (ui/track_controls.py): the controls stay up while one shows (`hold`).
Without a keyboard every key of the player has a control here too (the owner's mouse-only
rule): left of the speaker sit Search, and on live TV the guide and the previous channel; a
time-shifted channel at its live edge has ◀10 beside play; on live TV the channel's name (and
logo) shows its info, and a star after the name adds or removes it from the favorites. Each
names its key when hovered. A movie or an episode played from the Library has "‹ Library" at the
left of the right-hand buttons: back to where it came from, as Esc.
At a small width the Stay live indicator's text is shortened, or the indicator left out, so it
never runs under the play button.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from typing import Callable

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QFontMetricsF,
    QHelpEvent,
    QHideEvent,
    QLinearGradient,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QPixmap,
    QRegion,
)
from PySide6.QtWidgets import QToolTip, QWidget

from services.live.stay_live import TARGETS, Mode, SettledLag
from ui.keymap import key_hint
from ui.overlays.base import Overlay
from ui.overlays.painting import (
    Units,
    draw_icon,
    draw_logo,
    draw_progress,
    elided,
    fill_round,
    fmt_clock,
    fmt_duration,
)
from ui.pointer import NAV_BUTTONS
from ui.theme import PALETTE, qcolor

SPEEDS = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0)  # − and + step through these
TOP_FRACTION = 0.22
BOTTOM_FRACTION = 0.42
TICK_LABEL_MAX_U = 25.0
MINUS = "−"
TOOLTIPS = {
    "pip": "Second channel (P)",
    "mini": "Mini player (Alt+P)",
    "fullscreen": "Fullscreen (F)",
    "mini_back": "Back to the full window (Alt+P)",
    "fullscreen_back": "Exit fullscreen (F)",
    "external": "Open in external player",
    "volume": "Volume (+/−, M)",
    "slower": "Slower",
    "speed": "Normal speed",
    "faster": "Faster",
}
LIBRARY_LABEL = "‹ Library"
# Controls for a key of the player's (ui.keymap.SHORTCUTS): what they do, and that key.
KEY_HINTS = {
    "play": ("Play / pause", "Space"),
    "back10": ("Back 10 seconds", "Left"),
    "fwd10": ("Forward 10 seconds", "Right"),
    "golive": ("Go live", "L"),
    "previous": ("Previous channel", "Backspace"),
    "guide": ("Guide", "G"),
    "search": ("Search", "Ctrl+F"),
    "title": ("Channel info", "I"),
    "library": ("Back to the Library", "Esc"),
    # 2.2: each opens a panel whose − and + are these keys.
    "audio": ("Sound: track and sync", "J, K"),
    "subtitles": ("Subtitles: track, file and sync", "Shift+J, Shift+K"),
}
FAVORITE_KEY = "Ctrl+D"
# button name -> painting icon, if they differ
_ICONS = {"mini": "mini_player", "pip": "pip", "previous": "recent"}
_LABELS = ("live", "indicator", "times")  # shown, not clicked: over the PiP, clicks go on
# A quick second click (a double-click) repeats these steps; on any other control it counts
# once: a toggle (★, ⛶, ▶) would undo itself, a jump (⟲, Next) go one further.
_REPEATS = ("back10", "fwd10", "fwd60", "slower", "faster")
_ICON_BUTTONS = (  # painted as icons
    "previous",
    "guide",
    "search",
    "subtitles",
    "audio",
    "volume",
    "external",
    "pip",
    "mini",
    "fullscreen",
)


def _speed_label(rate: float) -> str:
    return f"{rate:g}x"


def _seconds(value: float) -> float:
    """A lag or age in seconds; a negative or non-finite value reads as zero."""
    return value if math.isfinite(value) and value > 0 else 0.0


def fmt_behind(seconds: float) -> str:
    """'−MM:SS' behind live ('−H:MM:SS' from an hour); a negative or non-finite lag reads as 0."""
    total = int(_seconds(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{MINUS}{hours}:{minutes:02d}:{secs:02d}"
    return f"{MINUS}{minutes:02d}:{secs:02d}"


def _local_now() -> datetime:
    return datetime.now().astimezone()


@dataclass(frozen=True)
class _TitleRow:
    """The top row: the channel's (or title's) name, on live TV a star after it, the clock."""

    name: str  # elided to its room
    name_rect: QRectF
    star: QRectF  # empty but on live TV
    subtitle_x: float
    clock: str
    clock_rect: QRectF


@dataclass(frozen=True)
class _Timeline:
    """One `LiveSession.timelineChanged` state, in timeline seconds."""

    start: float
    end: float
    position: float
    live_edge: float
    lag: float
    shown: float  # the lag as the Stay live indicator shows it (settled, in half seconds)
    mode: Mode
    rate: float
    nudge: float  # the rate stay-live's nudge ramps to (1.0: none)
    paused: bool
    target: float
    stalled: bool
    auto: bool  # Auto latency
    adjusted: bool  # the channel's Auto target moved from where Auto starts

    @classmethod
    def from_state(cls, state: dict) -> "_Timeline":
        return cls(
            start=float(state["start"]),
            end=float(state["end"]),
            position=float(state["position"]),
            live_edge=float(state["live_edge"]),
            lag=_seconds(float(state["lag"])),
            shown=SettledLag.rounded(_seconds(float(state.get("shown", state["lag"])))),
            mode=Mode(state["mode"]),
            rate=float(state["rate"]),
            nudge=float(state.get("nudge", 1.0)),
            paused=bool(state["paused"]),
            target=float(state.get("target", TARGETS["normal"])),
            stalled=bool(state.get("stalled", False)),
            auto=bool(state.get("auto", False)),
            adjusted=bool(state.get("adjusted", False)),
        )


class Transport(Overlay):
    playPauseRequested = Signal()
    seekRequested = Signal(int)
    seekRelativeRequested = Signal(int)
    speedRequested = Signal(float)
    fullscreenRequested = Signal()
    audioRequested = Signal()
    subtitleRequested = Signal()
    nextRequested = Signal()
    goLiveRequested = Signal()
    programmeStartRequested = Signal()
    seekToRequested = Signal(float)
    pipRequested = Signal()  # the second channel: open picture in picture, or close it
    miniPlayerRequested = Signal()
    # "Open in external player": the shell's menu opens from this point (the button's
    # top-left, global coordinates).
    externalRequested = Signal(QPoint)
    volumeRequested = Signal()  # the speaker: the mixer, on the large picture's row
    searchRequested = Signal()
    guideRequested = Signal()
    previousRequested = Signal()  # the channel before
    infoRequested = Signal()  # the channel's name: its info (the banner)
    favoriteRequested = Signal()  # the star after the name: add or remove the channel
    libraryRequested = Signal()  # ‹ Library: back to the Library the title came from (Esc)
    bandChanged = Signal()  # its controls changed (a mode, the bar): controls_top() moved

    def __init__(
        self,
        parent: QWidget | None = None,
        idle_ms: int = 3000,
        now: Callable[[], datetime] = _local_now,
    ) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self._now = now
        self._live = False
        self._timeshift = False
        self._title = ""
        self._logo: QPixmap | None = None
        self._subtitle = ""
        self._position = 0
        self._length = 0
        self._paused = False
        self._speed = 1.0
        self._next_available = False
        self._pip_available = False  # the window can open a second channel
        self._pip_open = False
        self._pip_rect = QRect()  # the small picture: the shades leave it alone
        self._muted = False  # the large picture's mute: the speaker shows it
        self._mini = self._fullscreen = False  # their buttons then show the way back
        self._favorite = False  # the live channel is a favorite: its star is lit
        self._library = False  # the title came from the Library: ‹ Library shows
        self._held = ""  # the button whose panel is open: no fade meanwhile, and it is lit
        self._hand = False  # the pointer is over the title row's controls
        self._timeline: _Timeline | None = None
        self._claimed: tuple[object, ...] = ()  # what hole_claims() covered last
        self._ticks: list[tuple[float, str]] = []
        self._edge = True
        self._edge_lag: float | None = None
        self._catching_up = False
        self._stalled = False  # the source stalled at the live edge: "Reconnecting…"
        self._auto: bool | None = None  # Auto latency: whether its target moved (else None)
        self._rate = 1.0
        # A bar fraction, not a time: the knob stays under the cursor while the buffer slides.
        self._drag: float | None = None
        self._last_move = QPointF(-1, -1)
        self._idle = QTimer(self)
        self._idle.setSingleShot(True)
        self._idle.setInterval(idle_ms)
        self._idle.timeout.connect(self.dismiss)
        self._clock = QTimer(self)  # repaint so the clock stays current while shown
        self._clock.setInterval(30_000)
        self._clock.timeout.connect(self._tick)
        self._clock.start()
        if parent is not None:
            parent.installEventFilter(self)
        self._fit()
        self.hide()

    # ---- public API ---------------------------------------------------------------------------

    def show_vod(self, title: str, subtitle: str) -> None:
        self._set_mode(False, title, subtitle)

    def show_live(self, title: str, subtitle: str, logo: QPixmap | None = None) -> None:
        """Live channels show a badge before the name: the logo, else the name's initials."""
        self._set_mode(True, title, subtitle, logo=logo)

    def show_timeshift(self, title: str, subtitle: str, logo: QPixmap | None = None) -> None:
        """Show a pipeline live channel. A different title (a zap) forgets the old timeline."""
        self._set_mode(True, title, subtitle, timeshift=True, logo=logo)

    def set_logo(self, logo: QPixmap | None) -> None:
        """The channel's logo arrived after the channel was shown."""
        self._logo = logo
        self.update()

    def badge_rect(self) -> QRectF:
        """The title row's logo/initials badge (empty for VOD)."""
        if not self._live:
            return QRectF()
        un = Units(self.width())
        size = QFontMetricsF(un.font(1.8, QFont.Weight.Bold)).height() * 1.9
        y = un.px(2.4) + (un.px(2.6) - size) / 2
        return QRectF(un.px(3), y, size, size)

    def update_timeline(self, state: dict, ticks: list[tuple[float, str]]) -> None:
        """Take a `LiveSession.timelineChanged` state; never changes visibility.

        `ticks` are programme starts in timeline seconds with their labels. An optional
        `target` key is the session's effective stay-live target (default: Normal), and an
        optional `stalled` key says the source stalled (the live-edge row reads "Reconnecting…").
        Optional `auto` and `adjusted` keys: Auto latency, and its target for the channel moved.
        """
        tl = _Timeline.from_state(state)
        self._timeline = tl
        self._ticks = [(float(t), str(label)) for t, label in ticks]
        live = tl.mode is Mode.LIVE
        self._edge = live
        self._edge_lag = tl.shown
        self._auto = tl.adjusted if tl.auto else None
        self._stalled = live and tl.stalled
        # Steering: any rate but 1x (a nudge's ramp included), slower ones too.
        self._catching_up = live and (tl.rate != 1.0 or tl.nudge != 1.0) and not self._stalled
        self._rate = tl.nudge if tl.nudge != 1.0 else tl.rate  # the nudge's, not the ramp's
        self._speed = 1.0 if live else tl.rate  # stay-live's nudges are not a chosen speed
        self._paused = tl.paused
        self.update()
        self._controls_changed()

    def show_live_edge(self, lag_s: float, catching_up: bool) -> None:
        """Show the live-edge indicator instead of the bar; never changes visibility."""
        self._live = self._timeshift = True
        self._edge = True
        self._edge_lag = SettledLag.rounded(_seconds(lag_s))
        self._auto = None
        self._catching_up = catching_up
        self._stalled = False
        self.update()
        self._controls_changed()

    def update_position(self, position_ms: int, length_ms: int) -> None:
        self._position = max(0, position_ms)
        self._length = max(0, length_ms)
        self.update()

    def set_paused(self, paused: bool) -> None:
        self._paused = paused
        self.update()

    def set_speed(self, rate: float) -> None:
        self._speed = rate
        self.update()

    def set_next_available(self, available: bool) -> None:
        self._next_available = available
        self.update()
        self._controls_changed()

    def set_pip_rect(self, rect: QRect) -> None:
        """Where the small picture is (this widget's coordinates; empty: no PiP). The shades
        leave it alone: cut by the host's mask to the PiP label's row, the bottom shade showed
        as a dark rectangle behind the label."""
        if rect != self._pip_rect:
            self._pip_rect = QRect(rect)
            self.update()

    def follow_pip(self, rect: QRect) -> None:
        """`set_pip_rect` for a rect in the host's coordinates (the PiP frame reports those).

        A slot of this widget, so a report that comes after it was deleted (the window
        closing) is dropped instead of reaching a deleted object."""
        self.set_pip_rect(rect.translated(-self.pos()))

    def set_pip_state(self, available: bool, open_: bool) -> None:
        """The second channel's button: shown on live TV when `available`; lit while open."""
        if (available, open_) != (self._pip_available, self._pip_open):
            self._pip_available, self._pip_open = available, open_
            self.update()
            self._controls_changed()

    def set_muted(self, muted: bool) -> None:
        """Whether the large picture is muted: its speaker shows it."""
        if muted != self._muted:
            self._muted = muted
            self.update()

    def set_window_mode(self, *, mini: bool, fullscreen: bool) -> None:
        """In the mini player or fullscreen, its button shows the way back."""
        if (mini, fullscreen) != (self._mini, self._fullscreen):
            self._mini, self._fullscreen = mini, fullscreen
            self.update()

    def icon(self, name: str) -> str:
        """The icon a button paints now (ui.overlays.painting.draw_icon's name)."""
        if name == "volume" and self._muted:
            return "mute"
        if name == "mini" and self._mini:
            return "full_window"
        if name == "fullscreen" and self._fullscreen:
            return "exit_fullscreen"
        return _ICONS.get(name, name)

    def set_library_available(self, available: bool) -> None:
        """The "‹ Library" button: shown during a movie or an episode when `available` (it was
        played from the Library); never on live TV."""
        if available != self._library:
            self._library = available
            self.update()
            self._controls_changed()

    def set_favorite(self, favorite: bool) -> None:
        """Whether the live channel is a favorite: its star is lit."""
        if favorite != self._favorite:
            self._favorite = favorite
            self.update()

    def title_rects(self) -> dict[str, QRectF]:
        """On live TV the title row's controls: "title" (the logo and the name: the channel's
        info) and "favorite" (the star after the name); nothing otherwise."""
        if not self._live:
            return {}
        row = self._title_row()
        title = row.name_rect
        badge = self.badge_rect()
        if not badge.isEmpty():
            title = title.united(badge)
        return {"title": title, "favorite": row.star}

    def hole_claims(self) -> QRegion:
        """Over the PiP, the clicks this keeps (host coordinates): its buttons and its bar, and
        on live TV the channel's name and star."""
        region = QRegion()
        for name, rect in self._layout().items():
            if name in _LABELS:
                continue
            if name == "seek":
                rect = rect.adjusted(0, -rect.height(), 0, rect.height())  # its hit area
            region = region.united(QRegion(rect.toAlignedRect()))
        for rect in self.title_rects().values():
            region = region.united(QRegion(rect.toAlignedRect()))
        return region.translated(self.geometry().topLeft())

    def controls_top(self) -> int:
        """Where its controls start (host coordinates): the bar's time bubble, else the row.

        The PiP rises above it and the mixer sits on it while the transport shows.
        """
        rects = self._layout()
        top = min(rect.top() for rect in rects.values())
        seek = rects.get("seek")
        if seek is not None:  # the wall-clock bubble over the knob, and the ticks' labels
            un = Units(self.width())
            bubble = max(
                float(un.px(2.1)), QFontMetricsF(un.font(1.05, QFont.Weight.Bold)).height()
            )
            top = min(top, seek.center().y() - un.px(2.7) - bubble)
        return self.geometry().top() + math.floor(top)

    def _controls_changed(self) -> None:
        """The host masks the PiP anew when this shows other controls (a mode, the bar)."""
        claimed = (
            self.size(),
            tuple(sorted(n for n in self._layout() if n not in _LABELS)),
            tuple((n, r.getRect()) for n, r in self.title_rects().items()),  # moves with the name
        )
        if claimed == self._claimed:
            return
        self._claimed = claimed
        host = self.parentWidget()
        if not self.isHidden() and host is not None and hasattr(host, "overlay_changed"):
            host.overlay_changed()
        self.bandChanged.emit()

    def tooltip_at(self, pos: QPointF) -> str:
        """What hovering `pos` says: a control's name and key, or nothing."""
        for name, rect in (*self.title_rects().items(), *self._layout().items()):
            text = self._tooltip(name)
            if text and rect.contains(pos):
                return text
        return ""

    def _tooltip(self, name: str) -> str:
        if name == "favorite":
            what = "Remove from favorites" if self._favorite else "Add to favorites"
            return key_hint(what, FAVORITE_KEY)
        if name in KEY_HINTS:
            return key_hint(*KEY_HINTS[name])
        if (name == "mini" and self._mini) or (name == "fullscreen" and self._fullscreen):
            return TOOLTIPS[f"{name}_back"]
        return TOOLTIPS.get(name, "")

    def poke(self) -> None:
        """Show (fade later); already shown, only restart the fade: no repaint, no mask."""
        if self.isHidden():
            self._fit()
            self.show()  # paints, and the host takes the new mask
        self._fade_later()

    def hold(self, button: str) -> None:
        """Stay up while `button`'s panel is open, the button lit (no fade); "": let go, the
        fade starts again."""
        if button != self._held:
            self._held = button
            self.update()
        if button:
            self._idle.stop()
        elif self.isVisible():
            self._idle.start()

    def held(self) -> str:
        """The button whose panel is open ("": none)."""
        return self._held

    def _fade_later(self) -> None:
        if not self._held:
            self._idle.start()

    def dismiss(self) -> None:
        if self._drag is not None:
            return  # a drag in progress keeps the bar until release
        self._idle.stop()
        self.hide()

    def _tick(self) -> None:
        if self.isVisible():
            self.update()

    def texts(self) -> tuple[str, str]:
        if self._timeshift:
            if not self._shows_bar():
                return ("LIVE", self.indicator_text())
            tl = self._timeline
            assert tl is not None
            buffer_from = fmt_clock(self._wall(tl.live_edge - tl.start))
            return (f"Buffer from {buffer_from}", f"{fmt_behind(self._shown_lag())} behind live")
        if self._live:
            return ("LIVE", "")
        shown = self._shown_ms()
        return (fmt_duration(shown), "-" + fmt_duration(max(0, self._length - shown)))

    def indicator_text(self) -> str:
        head, tail = self._indicator_parts()
        return f"{head} ● {tail}" if tail else ""

    def mask_region(self) -> QRegion:
        top_h = round(self.height() * TOP_FRACTION)
        bottom_h = round(self.height() * BOTTOM_FRACTION)
        region = QRegion(QRect(0, 0, self.width(), top_h))
        region += QRegion(QRect(0, self.height() - bottom_h, self.width(), bottom_h))
        return region.translated(self.geometry().topLeft())

    # ---- state helpers ------------------------------------------------------------------------

    def _shows_bar(self) -> bool:
        if not self._timeshift:
            return not self._live
        # A drag keeps the bar even if the session catches up meanwhile.
        return self._timeline is not None and (not self._edge or self._drag is not None)

    def _behind(self) -> bool:
        tl = self._timeline
        return tl is not None and tl.mode is not Mode.LIVE

    def _seekable(self) -> bool:
        if self._timeshift:
            return self._timeline is not None and self._timeline.end > self._timeline.start
        return self._length > 0

    def _shown_ms(self) -> int:
        return self._position if self._drag is None else round(self._drag * self._length)

    def _shown_t(self) -> float:
        tl = self._timeline
        assert tl is not None
        if self._drag is None:
            return tl.position
        return tl.start + self._drag * (tl.end - tl.start)

    def _shown_lag(self) -> float:
        tl = self._timeline
        assert tl is not None
        return tl.lag if self._drag is None else _seconds(tl.live_edge - self._shown_t())

    def _shown_frac(self) -> float:
        if self._timeshift:
            tl = self._timeline
            assert tl is not None
            span = tl.end - tl.start
            frac = (self._shown_t() - tl.start) / span if span > 0 else 1.0
        else:
            frac = self._shown_ms() / self._length if self._length else 0.0
        return min(1.0, max(0.0, frac))

    def _wall(self, seconds_ago: float) -> datetime:
        """Local wall-clock time `seconds_ago` before now, always timezone-aware."""
        now = self._now()
        if now.tzinfo is None:
            now = now.astimezone()  # a naive clock reads local time
        then = now.astimezone(timezone.utc) - timedelta(seconds=_seconds(seconds_ago))
        return then.astimezone()

    def _indicator_parts(self) -> tuple[str, str]:
        if self._edge_lag is None:
            return ("", "")
        if self._stalled:
            return ("Stay live", "Reconnecting…")
        lag = f"≈ {self._edge_lag:g} s behind"
        if self._auto is not None:
            tail = lag + (" · adjusted" if self._auto else "")
        else:
            tail = lag + " source"
        if self._catching_up:
            if self._rate < 1.0:
                tail += f" · adding buffer {self._rate:g}×"
            else:
                tail += " · catching up" + (f" {self._rate:g}×" if self._rate > 1.0 else "")
        return ("Stay live", tail)

    # ---- geometry -----------------------------------------------------------------------------

    def _fit(self) -> None:
        """Cover the whole host, as the banner and toast lay themselves out from it."""
        parent = self.parentWidget()
        if parent is not None and self.geometry() != parent.rect():
            self.setGeometry(parent.rect())

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        # Even while hidden: the next show then needs no layout pass.
        if obj is self.parentWidget() and event.type() == QEvent.Type.Resize:
            self._fit()
            self._controls_changed()
        return False

    @staticmethod
    def _chip_font(un: Units) -> QFont:
        return un.font(1.2, QFont.Weight.DemiBold)

    @classmethod
    def _chip_h(cls, un: Units) -> float:
        """A chip's height: 2.2u, or its text's at the font's floor (a small window)."""
        return max(float(un.px(2.2)), QFontMetricsF(cls._chip_font(un)).height() + un.px(0.4))

    @staticmethod
    def _times_font(un: Units) -> QFont:
        return un.font(1.25)

    @staticmethod
    def _indicator_font(un: Units) -> QFont:
        return un.font(1.1, QFont.Weight.DemiBold)

    def _title_row(self) -> _TitleRow:
        un = Units(self.width())
        left, right = float(un.px(3)), float(self.width() - un.px(3))
        y, row_h = float(un.px(2.4)), float(un.px(2.6))
        clock = fmt_clock(self._wall(0.0))
        clock_w = QFontMetrics(un.font(1.8, QFont.Weight.DemiBold)).horizontalAdvance(clock)
        clock_rect = QRectF(right - clock_w, y, clock_w, row_h)
        badge = self.badge_rect()
        if not badge.isEmpty():
            left = badge.right() + un.px(1.2)
        gap = un.px(0.6)
        room = right - left - clock_w - un.px(4)
        if self._live:
            room -= gap + row_h  # the star keeps its room: the name gives way
        fm = QFontMetrics(un.font(1.8, QFont.Weight.Bold))
        name = fm.elidedText(self._title, Qt.ElideRight, max(0, int(room)))
        name_w = fm.horizontalAdvance(name)
        name_rect = QRectF(left, y, name_w + 2, row_h)
        star = QRectF()
        subtitle_x = left + name_w + un.px(1.2)
        if self._live:
            star = QRectF(left + name_w + gap, y, row_h, row_h)
            subtitle_x = star.right() + gap
        return _TitleRow(name, name_rect, star, subtitle_x, clock, clock_rect)

    def _layout(self) -> dict[str, QRectF]:
        un = Units(self.width())
        left, right = un.px(3), self.width() - un.px(3)
        bottom = self.height() - un.px(3)
        icon = un.px(2.2)
        big = un.px(4.6)
        row_top = bottom - big
        rects: dict[str, QRectF] = {}
        bar = self._shows_bar()
        shifted = bar and self._timeshift
        if bar:
            seek_h = un.px(1.9)
            # As tall as its text: in the mini player the font floor outgrows 1.6u.
            times_h = max(float(un.px(1.6)), QFontMetricsF(self._times_font(un)).height())
            seek_y = row_top - un.px(1.2) - times_h - seek_h
            rects["seek"] = QRectF(left, seek_y, right - left, seek_h)
            rects["times"] = QRectF(left, seek_y + seek_h, right - left, times_h)
        cy = row_top + big / 2
        x = float(left)

        def put(name: str, size: float, gap: float) -> None:
            nonlocal x
            rects[name] = QRectF(x, cy - size / 2, size, size)
            x += size + gap

        skip = icon + un.px(1.6)
        if not bar:
            if self._timeshift:  # at the live edge: ← goes back into the buffer
                put("play", big, un.px(0.6))
                put("back10", skip, un.px(1.5))
            else:
                put("play", big, un.px(1.5))
            left_end = x  # the left buttons and their gap
            x = float(right)
            for name in self._right_buttons("subtitles", "audio", "volume"):
                x -= icon
                rects[name] = QRectF(x, cy - icon / 2, icon, icon)
                x -= un.px(1.8)
            pill_w = un.px(5.2)
            rects["live"] = QRectF(x - pill_w - un.px(0.4), cy - un.px(1.1), pill_w, un.px(2.2))
            if self._timeshift and self._edge_lag is not None:
                head, tail = self._indicator_parts()
                fm = QFontMetricsF(self._indicator_font(un))
                bare = 2 * un.px(1.1) + fm.horizontalAdvance(head) + 2 * un.px(0.6) + un.px(0.8)
                width = bare + fm.horizontalAdvance(tail)
                pill = rects["live"]
                right_edge = pill.x() - un.px(0.8)
                # A narrow transport shortens the tail (painted elided), else leaves it out.
                room = right_edge - left_end
                if width > room:
                    width = room if room >= bare + fm.horizontalAdvance("…") else 0.0
                if width > 0:
                    rects["indicator"] = QRectF(
                        right_edge - width, cy - un.px(1.1), width, un.px(2.2)
                    )
        else:
            if shifted:
                put("start", skip, un.px(0.6))
            put("back10", skip, un.px(0.6))
            put("play", big, un.px(0.6))
            if shifted:
                put("fwd10", skip, un.px(0.6))
                put("fwd60", skip, un.px(1.0))
            else:
                put("fwd10", skip, un.px(0.6) if self._next_available else un.px(1.0))
                if self._next_available:
                    put("next", skip, un.px(1.0))
            speed_x = x  # the chip goes in once the right side has taken its room
            x = float(right)
            for name in self._right_buttons("audio", "volume", "subtitles"):
                x -= icon
                rects[name] = QRectF(x, cy - icon / 2, icon, icon)
                x -= un.px(1.8)
            if shifted and self._behind():
                fm = QFontMetricsF(self._chip_font(un))
                width = 2 * un.px(1.0) + un.px(0.7) + un.px(0.5) + fm.horizontalAdvance("Go live")
                h = self._chip_h(un)
                rects["golive"] = QRectF(x - width, cy - h / 2, width, h)
            elif self._library and not self._live:
                fm = QFontMetricsF(self._chip_font(un))
                width = 2 * un.px(1.0) + fm.horizontalAdvance(LIBRARY_LABEL)
                h = self._chip_h(un)
                rects["library"] = QRectF(x - width, cy - h / 2, width, h)
            chips = [rects[n].left() - un.px(0.8) for n in ("golive", "library") if n in rects]
            rects.update(self._speed_rects(un, speed_x, cy, min(chips, default=x)))
        return rects

    def _speed_rects(self, un: Units, x: float, cy: float, room: float) -> dict[str, QRectF]:
        """− 1.25x + from `x`: as wide and tall as their text, whose font stops at a floor
        while u shrinks with the window (in a small one, "1.25x" was cut off). Short of
        `room` (a mini player's), the number alone (it goes back to 1x), else nothing."""
        fm = QFontMetricsF(self._chip_font(un))
        h = self._chip_h(un)
        step = max(float(un.px(2.2)), fm.horizontalAdvance("+") + 2 * un.px(0.6))
        widest = max(fm.horizontalAdvance(_speed_label(rate)) for rate in SPEEDS)
        label = max(float(un.px(3.6)), widest + 2 * un.px(0.4))
        top = cy - h / 2
        if x + 2 * step + label <= room:
            return {
                "slower": QRectF(x, top, step, h),
                "speed": QRectF(x + step, top, label, h),
                "faster": QRectF(x + step + label, top, step, h),
            }
        return {"speed": QRectF(x, top, label, h)} if x + label <= room else {}

    def _right_buttons(self, *rest: str) -> tuple[str, ...]:
        """The icon buttons from the right edge inwards: the window's and the picture's, the
        sound's (`rest`), then Search and, on live TV, the guide and the previous channel."""
        pip = ("pip",) if self._pip_available and self._live else ()
        tv = ("search", "guide", "previous") if self._live else ("search",)
        return ("fullscreen", "mini", *pip, "external", *rest, *tv)

    def seek_rect(self) -> QRectF:
        return self._layout()["seek"]

    def button_rect(self, name: str) -> QRectF:
        return self._layout()[name]

    def tick_marks(self) -> list[tuple[float, str, QRectF | None]]:
        """The bar's programme ticks: (x, shown label, label box or None when dropped)."""
        seek = self._layout().get("seek")
        if seek is None or not self._timeshift:
            return []
        return self._tick_marks(Units(self.width()), seek)

    @staticmethod
    def _tick_font(un: Units) -> QFont:
        return un.font(1.05)

    def _tick_marks(self, un: Units, seek: QRectF) -> list[tuple[float, str, QRectF | None]]:
        """Ticks inside the buffer, in time order. Each label is elided to TICK_LABEL_MAX_U;
        a label that would overlap the previous one is dropped, but its tick stays."""
        tl = self._timeline
        span = tl.end - tl.start if tl is not None else 0.0
        if tl is None or span <= 0:
            return []
        fm = QFontMetricsF(self._tick_font(un))
        label_h = max(float(un.px(1.5)), fm.height())
        top = seek.center().y() - un.px(1.5) / 2 - un.px(0.3) - label_h
        gap = un.px(0.8)
        taken = -math.inf  # right edge of the last label drawn
        marks: list[tuple[float, str, QRectF | None]] = []
        for t, label in sorted(self._ticks):
            if not tl.start <= t <= tl.end:
                continue
            x = seek.x() + seek.width() * (t - tl.start) / span
            text = fm.elidedText(label, Qt.ElideRight, un.px(TICK_LABEL_MAX_U))
            w = fm.horizontalAdvance(text)
            lx = min(max(x - w / 2, seek.x()), seek.right() - w)
            if not text or lx < taken + gap:
                marks.append((x, "", None))
                continue
            taken = lx + w
            marks.append((x, text, QRectF(lx, top, w + 1, label_h)))
        return marks

    # ---- painting -----------------------------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        un = Units(self.width())
        self._paint_scrims(p)
        self._paint_title(p, un)
        rects = self._layout()
        text = qcolor(PALETTE.text)
        if "seek" not in rects:
            self._paint_live(p, un, rects, text)
        elif self._timeshift:
            self._paint_timeshift(p, un, rects, text)
        else:
            self._paint_vod(p, un, rects, text)
        for name in _ICON_BUTTONS:
            if name in rects:
                lit = (name == "pip" and self._pip_open) or name == self._held
                draw_icon(p, self.icon(name), rects[name], qcolor(PALETTE.accent) if lit else text)
        p.end()

    def _paint_scrims(self, p: QPainter) -> None:
        p.save()
        if not self._pip_rect.isEmpty():
            p.setClipRegion(QRegion(self.rect()).subtracted(QRegion(self._pip_rect)))
        self._paint_scrim_bands(p)
        p.restore()

    def _paint_scrim_bands(self, p: QPainter) -> None:
        top_h = self.height() * TOP_FRACTION
        grad = QLinearGradient(0, 0, 0, top_h)
        grad.setColorAt(0, qcolor("#000000", 0.62))
        grad.setColorAt(1, qcolor("#000000", 0.0))
        p.fillRect(QRectF(0, 0, self.width(), top_h), grad)
        bottom_h = self.height() * BOTTOM_FRACTION
        y = self.height() - bottom_h
        grad = QLinearGradient(0, self.height(), 0, y)
        grad.setColorAt(0, qcolor("#000000", 0.78))
        grad.setColorAt(1, qcolor("#000000", 0.0))
        p.fillRect(QRectF(0, y, self.width(), bottom_h), grad)

    def _paint_title(self, p: QPainter, un: Units) -> None:
        row = self._title_row()
        right = self.width() - un.px(3)
        p.setFont(un.font(1.8, QFont.Weight.DemiBold))
        p.setPen(qcolor(PALETTE.text))
        p.drawText(row.clock_rect, Qt.AlignVCenter | Qt.AlignRight, row.clock)
        p.setFont(un.font(1.8, QFont.Weight.Bold))
        badge = self.badge_rect()
        if not badge.isEmpty():
            draw_logo(p, badge, self._title, self._logo, un.px(0.5))
            p.setPen(qcolor(PALETTE.text))
        p.drawText(row.name_rect, Qt.AlignVCenter | Qt.AlignLeft, row.name)
        if not row.star.isEmpty():
            size = un.px(1.9)
            c = row.star.center()
            lit = qcolor(PALETTE.accent) if self._favorite else qcolor(PALETTE.text, 0.75)
            draw_icon(p, "star", QRectF(c.x() - size / 2, c.y() - size / 2, size, size), lit)
        if self._subtitle:
            p.setFont(un.font(1.5))
            p.setPen(qcolor(PALETTE.text, 0.75))
            sx, y, row_h = row.subtitle_x, row.name_rect.y(), row.name_rect.height()
            sub = elided(p, self._subtitle, row.clock_rect.left() - un.px(2) - sx)
            p.drawText(QRectF(sx, y, right - sx, row_h), Qt.AlignVCenter | Qt.AlignLeft, sub)

    def _paint_track(self, p: QPainter, un: Units, seek: QRectF, track_alpha: float) -> QPointF:
        """Draw the line, played up to the shown position; return where the knob goes."""
        frac = self._shown_frac()
        track = QRectF(seek.x(), seek.center().y() - un.px(0.55) / 2, seek.width(), un.px(0.55))
        draw_progress(p, track, frac, track_alpha=track_alpha)
        return QPointF(seek.x() + seek.width() * frac, seek.center().y())

    def _paint_knob(self, p: QPainter, un: Units, kc: QPointF) -> None:
        knob_d = un.px(1.7)
        ring_d = knob_d + 2 * un.px(0.35)
        fill_round(
            p,
            QRectF(kc.x() - ring_d / 2, kc.y() - ring_d / 2, ring_d, ring_d),
            qcolor(PALETTE.accent, 0.45),
            ring_d / 2,
        )
        fill_round(
            p,
            QRectF(kc.x() - knob_d / 2, kc.y() - knob_d / 2, knob_d, knob_d),
            qcolor("#ffffff"),
            knob_d / 2,
        )

    def _paint_times(self, p: QPainter, un: Units, rects: dict[str, QRectF]) -> None:
        first, second = self.texts()
        p.setFont(self._times_font(un))
        p.setPen(qcolor(PALETTE.text, 0.8))
        times = rects["times"]
        p.drawText(times, Qt.AlignVCenter | Qt.AlignLeft, first)
        p.drawText(times, Qt.AlignVCenter | Qt.AlignRight, second)

    def _paint_skips(self, p: QPainter, un: Units, rects: dict[str, QRectF], text: QColor) -> None:
        for name, icon, side in (
            ("start", "prev", un.px(2.4)),
            ("back10", "back10", un.px(3.0)),
            ("fwd10", "fwd10", un.px(3.0)),
            ("fwd60", "next", un.px(2.4)),
            ("next", "next", un.px(2.4)),
        ):
            if name not in rects:
                continue
            r = rects[name]
            draw_icon(
                p,
                icon,
                QRectF(r.center().x() - side / 2, r.center().y() - side / 2, side, side),
                text,
            )

    def _paint_speed(self, p: QPainter, un: Units, rects: dict[str, QRectF], text: QColor) -> None:
        if "speed" not in rects:
            return  # no room (see _speed_rects)
        steps = "slower" in rects
        chip = rects["slower"].united(rects["faster"]) if steps else rects["speed"]
        fill_round(p, chip, qcolor(PALETTE.text, 0.14), chip.height() / 2)
        p.setFont(self._chip_font(un))
        p.setPen(text)
        p.drawText(rects["speed"], Qt.AlignCenter, _speed_label(self._speed))
        if not steps:
            return
        dim = QColor(text)
        dim.setAlphaF(text.alphaF() * 0.35)  # nothing slower or faster to go to
        index = self._nearest_speed()
        p.setPen(text if index > 0 else dim)
        p.drawText(rects["slower"], Qt.AlignCenter, MINUS)
        p.setPen(text if index < len(SPEEDS) - 1 else dim)
        p.drawText(rects["faster"], Qt.AlignCenter, "+")

    def _paint_vod(self, p: QPainter, un: Units, rects: dict[str, QRectF], text: QColor) -> None:
        kc = self._paint_track(p, un, rects["seek"], 0.14)
        self._paint_knob(p, un, kc)
        self._paint_times(p, un, rects)
        self._paint_skips(p, un, rects, text)
        self._paint_play(p, rects["play"], qcolor(PALETTE.canvas))
        self._paint_speed(p, un, rects, text)
        chip = rects.get("library")
        if chip is not None:
            fill_round(p, chip, qcolor(PALETTE.text, 0.14), chip.height() / 2)
            p.setFont(self._chip_font(un))
            p.setPen(text)
            p.drawText(chip, Qt.AlignCenter, LIBRARY_LABEL)

    def _paint_timeshift(
        self, p: QPainter, un: Units, rects: dict[str, QRectF], text: QColor
    ) -> None:
        tl = self._timeline
        assert tl is not None
        seek = rects["seek"]
        cy = seek.center().y()
        kc = self._paint_track(p, un, seek, 0.3)
        p.setFont(self._tick_font(un))
        p.setPen(qcolor(PALETTE.text, 0.75))
        tick_h = un.px(1.5)
        for x, label, box in self._tick_marks(un, seek):
            p.fillRect(QRectF(x - 1, cy - tick_h / 2, 2, tick_h), qcolor("#ffffff", 0.55))
            if box is not None:
                p.drawText(box, Qt.AlignHCenter | Qt.AlignBottom, label)
        dot_d = un.px(1.1)
        fill_round(
            p,
            QRectF(seek.right() - dot_d, cy - dot_d / 2, dot_d, dot_d),
            qcolor(PALETTE.live),
            dot_d / 2,
        )
        self._paint_knob(p, un, kc)
        self._paint_bubble(p, un, seek, kc)
        self._paint_times(p, un, rects)
        self._paint_skips(p, un, rects, text)
        dark = qcolor(PALETTE.canvas)
        self._paint_play(p, rects["play"], dark)
        self._paint_speed(p, un, rects, text)
        chip = rects.get("golive")
        if chip is not None:
            fill_round(p, chip, qcolor(PALETTE.accent), chip.height() / 2)
            d = un.px(0.7)
            fill_round(
                p, QRectF(chip.x() + un.px(1.0), chip.center().y() - d / 2, d, d), dark, d / 2
            )
            p.setFont(self._chip_font(un))
            p.setPen(dark)
            tx = chip.x() + un.px(1.0) + d + un.px(0.5)
            p.drawText(
                QRectF(tx, chip.y(), chip.right() - tx, chip.height()),
                Qt.AlignVCenter | Qt.AlignLeft,
                "Go live",
            )

    def _paint_bubble(self, p: QPainter, un: Units, seek: QRectF, kc: QPointF) -> None:
        """Wall-clock time of the shown position, above the knob."""
        label = self._wall(self._shown_lag()).strftime("%H:%M:%S")
        p.setFont(un.font(1.05, QFont.Weight.Bold))
        w = p.fontMetrics().horizontalAdvance(label) + 2 * un.px(0.6)
        h = max(float(un.px(2.1)), p.fontMetrics().height())
        x = min(max(kc.x() - w / 2, seek.x()), seek.right() - w)
        bubble = QRectF(x, seek.center().y() - un.px(2.7) - h, w, h)
        p.save()
        p.setPen(QPen(qcolor(PALETTE.accent, 0.8), max(1.0, un.u * 0.1)))
        p.setBrush(qcolor(PALETTE.canvas, 0.92))
        p.drawRoundedRect(bubble, un.px(0.35), un.px(0.35))
        p.restore()
        p.setPen(qcolor(PALETTE.text))
        p.drawText(bubble, Qt.AlignCenter, label)

    def _paint_play(self, p: QPainter, rect: QRectF, dark: QColor) -> None:
        fill_round(p, rect, qcolor(PALETTE.text), rect.width() / 2)
        inner = rect.width() * 0.5
        draw_icon(
            p,
            "play" if self._paused else "pause",
            QRectF(
                rect.center().x() - inner / 2 + (rect.width() * 0.03 if self._paused else 0),
                rect.center().y() - inner / 2,
                inner,
                inner,
            ),
            dark,
        )

    def _paint_live(self, p: QPainter, un: Units, rects: dict[str, QRectF], text: QColor) -> None:
        self._paint_play(p, rects["play"], qcolor(PALETTE.canvas))
        self._paint_skips(p, un, rects, text)  # ◀10 at a time-shifted channel's live edge
        pill = rects["live"]
        fill_round(p, pill, qcolor(PALETTE.live), pill.height() / 2)
        p.setFont(un.font(1.15, QFont.Weight.Bold))
        p.setPen(qcolor("#ffffff"))
        p.drawText(pill, Qt.AlignCenter, "LIVE")
        box = rects.get("indicator")
        if box is None:
            return
        fill_round(p, box, qcolor(PALETTE.panel), box.height() / 2)
        head, tail = self._indicator_parts()
        p.setFont(self._indicator_font(un))
        p.setPen(text)
        fm = p.fontMetrics()
        x = box.x() + un.px(1.1)
        p.drawText(QRectF(x, box.y(), box.right() - x, box.height()), Qt.AlignVCenter, head)
        x += fm.horizontalAdvance(head) + un.px(0.6)
        d = un.px(0.8)
        # Green only at 1x: amber while stay-live steers the rate (either way) or reconnecting.
        dot = qcolor(PALETTE.warn if self._catching_up or self._stalled else PALETTE.ok)
        fill_round(p, QRectF(x, box.center().y() - d / 2, d, d), dot, d / 2)
        x += d + un.px(0.6)
        tail = fm.elidedText(tail, Qt.ElideRight, max(0, round(box.right() - un.px(1.1) - x)))
        p.drawText(QRectF(x, box.y(), box.right() - x, box.height()), Qt.AlignVCenter, tail)

    # ---- input --------------------------------------------------------------------------------

    def _frac_at(self, x: float) -> float:
        seek = self.seek_rect()
        return min(1.0, max(0.0, (x - seek.x()) / seek.width()))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() in NAV_BUTTONS:
            event.ignore()  # Back/Forward go on to the host: the shell's drawer layers
            return
        event.accept()  # the overlay host behind would open the drawer
        self.poke()
        if event.button() != Qt.LeftButton:
            return
        pos = event.position()
        rects = self._layout()
        seek = rects.get("seek")
        if seek is not None and self._seekable():
            hit = seek.adjusted(0, -seek.height(), 0, seek.height())
            if hit.contains(pos):
                self._drag = self._frac_at(pos.x())
                self._idle.stop()
                self.update()
                return
        self._click(rects, pos)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        """The second press of a quick double-click: a step again, else nothing more."""
        if event.button() == Qt.LeftButton and self._control_at(event.position()) not in (
            *_REPEATS,
            "seek",
        ):
            event.accept()  # never the video's fullscreen toggle either
            self.poke()
            return
        self.mousePressEvent(event)  # as Qt's own: one more press

    def control_at(self, pos: QPointF) -> str | None:
        """The control under `pos` (this widget's coordinates), as `_control_at`: a press there
        acts (an open Sound or Subtitles panel lets it through)."""
        return self._control_at(pos)

    def _control_at(self, pos: QPointF) -> str | None:
        """The control under `pos`: a title row's, a button's, or None (the bar by its hit area)."""
        for name, rect in self.title_rects().items():
            if rect.contains(pos):
                return name
        for name, rect in self._layout().items():
            if name == "seek":
                rect = rect.adjusted(0, -rect.height(), 0, rect.height())
            if name not in _LABELS and rect.contains(pos):
                return name
        return None

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        pos = event.position()
        if pos != self._last_move:  # ignore synthetic moves from layout changes
            self._last_move = pos
            self.poke()
            self._point_at(pos)
        if self._drag is not None:
            if not event.buttons() & Qt.LeftButton:
                self._end_drag()  # its release went elsewhere (a focus switch, a lost grab)
                return
            self._drag = self._frac_at(event.position().x())
            self.update()

    def _point_at(self, pos: QPointF) -> None:
        """A pointing hand over the title row's controls: they are not drawn as buttons."""
        hand = any(rect.contains(pos) for rect in self.title_rects().values())
        if hand != self._hand:
            self._hand = hand
            if hand:
                self.setCursor(Qt.PointingHandCursor)
            else:
                self.unsetCursor()

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.ToolTip and isinstance(event, QHelpEvent):
            text = self.tooltip_at(QPointF(event.pos()))
            if text:
                QToolTip.showText(event.globalPos(), text, self)
            else:
                QToolTip.hideText()
                event.ignore()
            return True
        return super().event(event)

    def hideEvent(self, event: QHideEvent) -> None:
        # Hidden mid-drag (the host hides with a minimized window): no release will come.
        self._end_drag()
        super().hideEvent(event)

    def _end_drag(self) -> None:
        """Drop a drag whose release was lost, without seeking; the idle fade runs again."""
        if self._drag is None:
            return
        self._drag = None
        self.update()
        self._fade_later()
        self._controls_changed()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton and self._drag is not None:
            self._drag = self._frac_at(event.position().x())
            timeshift = self._timeshift
            target_t = self._shown_t() if timeshift else 0.0
            target_ms = 0 if timeshift else self._shown_ms()
            self._drag = None
            self.update()
            self._fade_later()
            self._controls_changed()  # the bar may go with the drag
            if timeshift:
                self.seekToRequested.emit(target_t)
            else:
                self.seekRequested.emit(target_ms)

    def _click(self, rects: dict[str, QRectF], pos: QPointF) -> None:
        for name, rect in self.title_rects().items():
            if rect.contains(pos):
                (self.infoRequested if name == "title" else self.favoriteRequested).emit()
                return
        for name, rect in rects.items():
            if not rect.contains(pos):
                continue
            if name == "play":
                self.playPauseRequested.emit()
            elif name == "back10":
                self.seekRelativeRequested.emit(-10_000)
            elif name == "fwd10":
                self.seekRelativeRequested.emit(10_000)
            elif name == "fwd60":
                self.seekRelativeRequested.emit(60_000)
            elif name == "start":
                self.programmeStartRequested.emit()
            elif name == "golive":
                self.goLiveRequested.emit()
            elif name == "next":
                self.nextRequested.emit()
            elif name in ("slower", "faster"):
                index = self._nearest_speed() + (1 if name == "faster" else -1)
                if 0 <= index < len(SPEEDS):
                    self._choose_speed(SPEEDS[index])
            elif name == "speed":
                if self._speed != 1.0:
                    self._choose_speed(1.0)
            elif name == "fullscreen":
                self.fullscreenRequested.emit()
            elif name == "audio":
                self.audioRequested.emit()
            elif name == "volume":
                self.volumeRequested.emit()
            elif name == "subtitles":
                self.subtitleRequested.emit()
            elif name == "pip":
                self.pipRequested.emit()
            elif name == "mini":
                self.miniPlayerRequested.emit()
            elif name == "external":
                self.externalRequested.emit(self.mapToGlobal(rect.topLeft().toPoint()))
            elif name == "search":
                self.searchRequested.emit()
            elif name == "guide":
                self.guideRequested.emit()
            elif name == "previous":
                self.previousRequested.emit()
            elif name == "library":
                self.libraryRequested.emit()
            return

    def _choose_speed(self, rate: float) -> None:
        self._speed = rate
        self.update()
        self.speedRequested.emit(rate)

    def _nearest_speed(self) -> int:
        return min(range(len(SPEEDS)), key=lambda i: abs(SPEEDS[i] - self._speed))

    def _set_mode(
        self,
        live: bool,
        title: str,
        subtitle: str,
        timeshift: bool = False,
        logo: QPixmap | None = None,
    ) -> None:
        if not (timeshift and self._timeshift and title == self._title):
            self._clear_timeline()
        self._live = live
        self._timeshift = timeshift
        self._title = title
        self._logo = logo
        self._subtitle = subtitle
        self._drag = None
        if live:
            self._position = self._length = 0
        self.update()  # already shown, the new mode needs a repaint
        self.poke()
        self._controls_changed()

    def _clear_timeline(self) -> None:
        self._timeline = None
        self._ticks = []
        self._edge = True
        self._edge_lag = None
        self._auto = None
        self._catching_up = False
        self._stalled = False
        self._rate = 1.0
