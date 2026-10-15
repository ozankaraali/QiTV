"""Transport overlay: title row on top, seek line and buttons at the bottom.

Modes: VOD, plain live (direct play) and time-shift (live through the pipeline). Time-shift shows
the bar over the buffered range, or, while the session's stay-live mode is LIVE, the stay-live
latency indicator (it reads "catching up" while stay-live nudges the rate, and "Reconnecting…"
while the source has stalled). The bar and the Go live chip show only when the viewer left live
(shifted, or a chosen speed); a drag keeps the bar.

Left of fullscreen sit the mini player (the whole window small in a corner, Alt+P) and, on
live TV while the window offers picture in picture, the second channel (P). Hovering those
three names their keys.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from typing import Callable

from PySide6.QtCore import QEvent, QObject, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QHelpEvent,
    QHideEvent,
    QLinearGradient,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QRegion,
)
from PySide6.QtWidgets import QToolTip, QWidget

from services.live.stay_live import TARGETS, Mode
from ui.overlays.base import Overlay
from ui.overlays.painting import (
    Units,
    draw_icon,
    draw_progress,
    elided,
    fill_round,
    fmt_clock,
    fmt_duration,
)
from ui.pointer import NAV_BUTTONS
from ui.theme import PALETTE, qcolor

SPEEDS = (1.0, 1.25, 1.5, 2.0)
TOP_FRACTION = 0.22
BOTTOM_FRACTION = 0.42
TICK_LABEL_MAX_U = 25.0
MINUS = "−"
TOOLTIPS = {
    "pip": "Second channel (P)",
    "mini": "Mini player (Alt+P)",
    "fullscreen": "Fullscreen (F)",
}
_ICONS = {"mini": "mini_player", "pip": "pip"}  # button name -> painting icon, if they differ
_LABELS = ("live", "indicator", "times")  # shown, not clicked: over the PiP, clicks go on


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
class _Timeline:
    """One `LiveSession.timelineChanged` state, in timeline seconds."""

    start: float
    end: float
    position: float
    live_edge: float
    lag: float
    mode: Mode
    rate: float
    paused: bool
    target: float
    stalled: bool

    @classmethod
    def from_state(cls, state: dict) -> "_Timeline":
        return cls(
            start=float(state["start"]),
            end=float(state["end"]),
            position=float(state["position"]),
            live_edge=float(state["live_edge"]),
            lag=_seconds(float(state["lag"])),
            mode=Mode(state["mode"]),
            rate=float(state["rate"]),
            paused=bool(state["paused"]),
            target=float(state.get("target", TARGETS["normal"])),
            stalled=bool(state.get("stalled", False)),
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
        self._subtitle = ""
        self._position = 0
        self._length = 0
        self._paused = False
        self._speed = 1.0
        self._next_available = False
        self._pip_available = False  # the window can open a second channel
        self._pip_open = False
        self._timeline: _Timeline | None = None
        self._claimed: tuple[object, ...] = ()  # what hole_claims() covered last
        self._ticks: list[tuple[float, str]] = []
        self._edge = True
        self._edge_lag: float | None = None
        self._catching_up = False
        self._stalled = False  # the source stalled at the live edge: "Reconnecting…"
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

    def show_live(self, title: str, subtitle: str) -> None:
        self._set_mode(True, title, subtitle)

    def show_timeshift(self, title: str, subtitle: str) -> None:
        """Show a pipeline live channel. A different title (a zap) forgets the old timeline."""
        self._set_mode(True, title, subtitle, timeshift=True)

    def update_timeline(self, state: dict, ticks: list[tuple[float, str]]) -> None:
        """Take a `LiveSession.timelineChanged` state; never changes visibility.

        `ticks` are programme starts in timeline seconds with their labels. An optional
        `target` key is the session's effective stay-live target (default: Normal), and an
        optional `stalled` key says the source stalled (the live-edge row reads "Reconnecting…").
        """
        tl = _Timeline.from_state(state)
        self._timeline = tl
        self._ticks = [(float(t), str(label)) for t, label in ticks]
        live = tl.mode is Mode.LIVE
        self._edge = live
        self._edge_lag = tl.lag
        self._stalled = live and tl.stalled
        self._catching_up = live and tl.rate > 1.0 and not self._stalled
        self._rate = tl.rate
        self._speed = 1.0 if live else tl.rate  # stay-live's nudges are not a chosen speed
        self._paused = tl.paused
        self.update()
        self._controls_changed()

    def show_live_edge(self, lag_s: float, catching_up: bool) -> None:
        """Show the live-edge indicator instead of the bar; never changes visibility."""
        self._live = self._timeshift = True
        self._edge = True
        self._edge_lag = _seconds(lag_s)
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

    def set_pip_state(self, available: bool, open_: bool) -> None:
        """The second channel's button: shown on live TV when `available`; lit while open."""
        if (available, open_) != (self._pip_available, self._pip_open):
            self._pip_available, self._pip_open = available, open_
            self.update()
            self._controls_changed()

    def hole_claims(self) -> QRegion:
        """Over the PiP, the clicks this keeps (host coordinates): its buttons and its bar."""
        region = QRegion()
        for name, rect in self._layout().items():
            if name in _LABELS:
                continue
            if name == "seek":
                rect = rect.adjusted(0, -rect.height(), 0, rect.height())  # its hit area
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
        claimed = (self.size(), tuple(sorted(n for n in self._layout() if n not in _LABELS)))
        if claimed == self._claimed:
            return
        self._claimed = claimed
        host = self.parentWidget()
        if not self.isHidden() and host is not None and hasattr(host, "overlay_changed"):
            host.overlay_changed()
        self.bandChanged.emit()

    def tooltip_at(self, pos: QPointF) -> str:
        """What hovering `pos` says: a button's name and key, or nothing."""
        for name, rect in self._layout().items():
            if name in TOOLTIPS and rect.contains(pos):
                return TOOLTIPS[name]
        return ""

    def poke(self) -> None:
        """Show (fade later); already shown, only restart the fade: no repaint, no mask."""
        if self.isHidden():
            self._fit()
            self.show()  # paints, and the host takes the new mask
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
        tail = f"{self._edge_lag:.1f} s behind source"
        if self._catching_up:
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

    @staticmethod
    def _times_font(un: Units) -> QFont:
        return un.font(1.25)

    @staticmethod
    def _indicator_font(un: Units) -> QFont:
        return un.font(1.1, QFont.Weight.DemiBold)

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

        if not bar:
            put("play", big, un.px(1.5))
            x = float(right)
            for name in self._right_buttons("subtitles", "audio"):
                x -= icon
                rects[name] = QRectF(x, cy - icon / 2, icon, icon)
                x -= un.px(1.8)
            pill_w = un.px(5.2)
            rects["live"] = QRectF(x - pill_w - un.px(0.4), cy - un.px(1.1), pill_w, un.px(2.2))
            if self._timeshift and self._edge_lag is not None:
                head, tail = self._indicator_parts()
                fm = QFontMetricsF(self._indicator_font(un))
                width = (
                    2 * un.px(1.1)
                    + fm.horizontalAdvance(head)
                    + fm.horizontalAdvance(tail)
                    + 2 * un.px(0.6)
                    + un.px(0.8)
                )
                pill = rects["live"]
                rects["indicator"] = QRectF(
                    pill.x() - un.px(0.8) - width, cy - un.px(1.1), width, un.px(2.2)
                )
        else:
            skip = icon + un.px(1.6)
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
            chip_w = un.px(3.6)
            rects["speed"] = QRectF(x, cy - un.px(1.1), chip_w, un.px(2.2))
            x = float(right)
            for name in self._right_buttons("audio", "subtitles"):
                x -= icon
                rects[name] = QRectF(x, cy - icon / 2, icon, icon)
                x -= un.px(1.8)
            if shifted and self._behind():
                fm = QFontMetricsF(self._chip_font(un))
                width = 2 * un.px(1.0) + un.px(0.7) + un.px(0.5) + fm.horizontalAdvance("Go live")
                rects["golive"] = QRectF(x - width, cy - un.px(1.1), width, un.px(2.2))
        return rects

    def _right_buttons(self, *rest: str) -> tuple[str, ...]:
        """The icon buttons from the right edge inwards."""
        pip = ("pip",) if self._pip_available and self._live else ()
        return ("fullscreen", "mini", *pip, *rest)

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
        for name in ("subtitles", "audio", "pip", "mini", "fullscreen"):
            if name in rects:
                lit = name == "pip" and self._pip_open
                draw_icon(
                    p, _ICONS.get(name, name), rects[name], qcolor(PALETTE.accent) if lit else text
                )
        p.end()

    def _paint_scrims(self, p: QPainter) -> None:
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
        left = un.px(3)
        right = self.width() - un.px(3)
        y = un.px(2.4)
        row_h = un.px(2.6)
        clock = fmt_clock(self._wall(0.0))
        p.setFont(un.font(1.8, QFont.Weight.DemiBold))
        p.setPen(qcolor(PALETTE.text))
        clock_w = p.fontMetrics().horizontalAdvance(clock)
        p.drawText(
            QRectF(right - clock_w, y, clock_w, row_h), Qt.AlignVCenter | Qt.AlignRight, clock
        )
        p.setFont(un.font(1.8, QFont.Weight.Bold))
        title = elided(p, self._title, right - left - clock_w - un.px(4))
        title_w = p.fontMetrics().horizontalAdvance(title)
        p.drawText(QRectF(left, y, title_w + 2, row_h), Qt.AlignVCenter | Qt.AlignLeft, title)
        if self._subtitle:
            p.setFont(un.font(1.5))
            p.setPen(qcolor(PALETTE.text, 0.75))
            sx = left + title_w + un.px(1.2)
            sub = elided(p, self._subtitle, right - clock_w - un.px(2) - sx)
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

    def _paint_speed(self, p: QPainter, un: Units, chip: QRectF, text: QColor) -> None:
        fill_round(p, chip, qcolor(PALETTE.text, 0.14), chip.height() / 2)
        p.setFont(self._chip_font(un))
        p.setPen(text)
        p.drawText(chip, Qt.AlignCenter, _speed_label(self._speed))

    def _paint_vod(self, p: QPainter, un: Units, rects: dict[str, QRectF], text: QColor) -> None:
        kc = self._paint_track(p, un, rects["seek"], 0.14)
        self._paint_knob(p, un, kc)
        self._paint_times(p, un, rects)
        self._paint_skips(p, un, rects, text)
        self._paint_play(p, rects["play"], qcolor(PALETTE.canvas))
        self._paint_speed(p, un, rects["speed"], text)

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
        self._paint_speed(p, un, rects["speed"], text)
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
        # Green only on target: amber while catching up or reconnecting.
        dot = qcolor(PALETTE.warn if self._catching_up or self._stalled else PALETTE.ok)
        fill_round(p, QRectF(x, box.center().y() - d / 2, d, d), dot, d / 2)
        x += d + un.px(0.6)
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

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        pos = event.position()
        if pos != self._last_move:  # ignore synthetic moves from layout changes
            self._last_move = pos
            self.poke()
        if self._drag is not None:
            if not event.buttons() & Qt.LeftButton:
                self._end_drag()  # its release went elsewhere (a focus switch, a lost grab)
                return
            self._drag = self._frac_at(event.position().x())
            self.update()

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
        self._idle.start()
        self._controls_changed()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton and self._drag is not None:
            self._drag = self._frac_at(event.position().x())
            timeshift = self._timeshift
            target_t = self._shown_t() if timeshift else 0.0
            target_ms = 0 if timeshift else self._shown_ms()
            self._drag = None
            self.update()
            self._idle.start()
            self._controls_changed()  # the bar may go with the drag
            if timeshift:
                self.seekToRequested.emit(target_t)
            else:
                self.seekRequested.emit(target_ms)

    def _click(self, rects: dict[str, QRectF], pos: QPointF) -> None:
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
            elif name == "speed":
                nxt = SPEEDS[(self._nearest_speed() + 1) % len(SPEEDS)]
                self._speed = nxt
                self.update()
                self.speedRequested.emit(nxt)
            elif name == "fullscreen":
                self.fullscreenRequested.emit()
            elif name == "audio":
                self.audioRequested.emit()
            elif name == "subtitles":
                self.subtitleRequested.emit()
            elif name == "pip":
                self.pipRequested.emit()
            elif name == "mini":
                self.miniPlayerRequested.emit()
            return

    def _nearest_speed(self) -> int:
        return min(range(len(SPEEDS)), key=lambda i: abs(SPEEDS[i] - self._speed))

    def _set_mode(self, live: bool, title: str, subtitle: str, timeshift: bool = False) -> None:
        if not (timeshift and self._timeshift and title == self._title):
            self._clear_timeline()
        self._live = live
        self._timeshift = timeshift
        self._title = title
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
        self._catching_up = False
        self._stalled = False
        self._rate = 1.0
