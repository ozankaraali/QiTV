"""Stay-live controller: nudges the playback rate to hold a latency target.

Behind the target it speeds up (1.05x, 1.10x, or reopens at live); it slows down only to
build a bursty source's room: 0.95x while less than half a burst would be left when a burst
lands, so the player never meets the live edge just before each one.

On some channels VLC cannot play at any rate but 1x: its position crawls instead (measured in
2.0.2: 4.5 s of media in 37 s). `RateHealth` notices that from VLC's progress, and the caller
then calls `lock_rate()`: from there on stay-live keeps 1x and only reopens at live when the
lag is beyond the target plus 20 s, as before.

"Auto" latency (`AUTO`, `AutoTarget`) learns what each channel adds to the target in force: it
starts at nothing, rises with each starvation or stall and falls again while the channel plays
without one. A target raised above the lag is reached without a jump back: at 0.95x (`build`) unless the
rate is locked, else as VLC waits at the live edge, which adds to the lag by itself.

Pure logic; the caller feeds the measured lag and applies the returned Decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from typing import Optional

TARGETS = {"low": 1.0, "normal": 2.5, "stable": 6.0}
AUTO = "auto"  # the latency setting that learns each channel's target (AutoTarget)
LATENCIES = (AUTO, *TARGETS)

_SETTLE = 0.2
_NUDGE = 0.75
_PUSH = 3.0
_PUSH_HOLD = 1.0  # from 1.10, back to 1.05 only this far below the push threshold
_REOPEN = 20.0
_SLOW = 0.95  # builds a bursty source's room; never slower
_ROOM_BAND = 0.5  # slows only this far under the room it builds: no flapping at its edge
_HEALTH_WINDOW_S = 3.0  # rate health: VLC's progress is judged over this much counted time
_HEALTH_SHARE = 0.7  # under this share of rate x wall time, VLC cannot play at that rate
_AUTO_UP = 1.5  # Auto: a starvation or a stall raises the channel's target this much
_AUTO_DOWN = 0.5  # and each _AUTO_QUIET_S without one lowers it this much
_AUTO_QUIET_S = 300.0
_AUTO_MAX_S = 60.0  # a remembered extra beyond this is not one Auto set: start afresh


def start_target(latency: str) -> float:
    """The target a latency setting starts with (Auto: Normal's); KeyError if unknown."""
    return TARGETS["normal"] if latency == AUTO else TARGETS[latency]


class Mode(str, Enum):
    LIVE = "live"
    SHIFTED = "shifted"
    CATCHING_UP = "catching_up"


@dataclass(frozen=True)
class Decision:
    rate: float
    reopen_live: bool = False


class RateHealth:
    """Whether VLC plays at the rates stay-live sets: its progress against rate x wall time.

    The caller adds only time that says something about the rate: at a rate other than 1x,
    with VLC playing and media ahead of it (a VLC waiting at the live edge is starved, not
    slow). Each window of `window_s` counted seconds (default `_HEALTH_WINDOW_S`) is judged on
    its own; `share` is the last window's progress against what its rates asked. The verdict
    needs `windows` bad windows in a row (a healthy one starts the count again; a new window
    after an open keeps it).
    """

    def __init__(self, window_s: float = _HEALTH_WINDOW_S, *, windows: int = 1) -> None:
        self.share = 1.0
        self._window = window_s
        self._needed = windows
        self._bad = 0  # bad windows in a row
        self.reset()

    def reset(self) -> None:
        """Start a new window (the stream was opened again, say)."""
        self._wall = self._asked = self._played = 0.0

    def add(self, wall: float, played: float, rate: float) -> bool:
        """`wall` seconds at `rate` in which VLC played `played` seconds of media.

        True once a window's progress is under `_HEALTH_SHARE` of what was asked: VLC cannot
        play this channel at that rate.
        """
        self._wall += wall
        self._asked += wall * rate
        self._played += played
        if self._wall < self._window - 1e-9:
            return False
        self.share = self._played / self._asked if self._asked > 0 else 1.0
        self.reset()
        self._bad = self._bad + 1 if self.share < _HEALTH_SHARE else 0
        if self._bad < self._needed:
            return False
        self._bad = 0
        return True


class AutoTarget:
    """One channel's Auto latency (2.0.2): what it adds to the target in force.

    The target in force is Normal's, or the source's burst when that is larger, as under any
    latency; Auto adds `extra` on top: 0 at first, or where the channel's last tune left it.
    Each starvation of VLC at the live edge, and each stall of the source, adds `_AUTO_UP`,
    so the target in force moves at once (on long HLS segments too), up to max(Stable's +
    4 s, burst + 2 s) in all; every `_AUTO_QUIET_S` of playing without one takes
    `_AUTO_DOWN` off, down to 0. The caller says what counts.
    """

    def __init__(self, extra: Optional[float] = None) -> None:
        self.extra = 0.0
        if extra is not None and math.isfinite(extra) and 0.0 < extra <= _AUTO_MAX_S:
            self.extra = extra
        self._quiet = 0.0

    @staticmethod
    def _base(burst: float) -> float:
        return max(TARGETS["normal"], burst)

    @staticmethod
    def _cap(burst: float) -> float:
        return max(TARGETS["stable"] + 4.0, burst + 2.0)

    def target(self, burst: float) -> float:
        """The target in force for a source with this burst (0.0: steady), with the extra."""
        base = self._base(burst)
        return max(base, min(base + self.extra, self._cap(burst)))

    @property
    def adjusted(self) -> bool:
        """The channel needed more than the target in force."""
        return self.extra > 0.0

    def trouble(self, burst: float) -> bool:
        """A starvation or a stall: 1.5 s more, up to the cap; True when the extra changed."""
        self._quiet = 0.0
        room = self._cap(burst) - self._base(burst)  # the most Auto may add on this source
        raised = max(self.extra, min(self.extra + _AUTO_UP, room))  # never lowered here
        changed, self.extra = raised != self.extra, raised
        return changed

    def quiet(self, seconds: float) -> bool:
        """`seconds` more of playing without trouble; True when the extra was lowered."""
        self._quiet += seconds
        if self._quiet < _AUTO_QUIET_S - 1e-9:
            return False
        self._quiet = 0.0
        lowered = max(0.0, self.extra - _AUTO_DOWN)
        changed, self.extra = lowered != self.extra, lowered
        return changed


class StayLive:
    def __init__(self, latency: str = "normal") -> None:
        self.mode = Mode.LIVE
        self.target = start_target(latency)
        # The source's burst (0.0: steady). Set by the caller with `target`; the lag it feeds
        # is then the mean over a burst: the room left when a burst lands plus half a burst.
        self.burst = 0.0
        # A raised Auto target the lag is to grow to (0.0: none). Set by the caller with
        # `target`: like a burst's room, it is built at 0.95x.
        self.build = 0.0
        self._rate = 1.0  # last rate decided in LIVE
        self._user_rate = 1.0
        self._reopened = False
        self.rate_locked = False  # VLC cannot play this channel at another rate: 1x for good

    def lock_rate(self) -> None:
        """VLC cannot play this channel at any rate but 1x (`RateHealth`): keep 1x from now.

        Stay-live then changes no rate: no nudge and no slow-down, only the reopen at live
        when the lag is beyond the target plus 20 s. A speed the user chooses still applies.
        """
        self.rate_locked = True
        self._rate = 1.0

    def set_latency(self, name: str) -> None:
        self.target = start_target(name)

    def user_paused(self) -> None:
        """A pause leaves LIVE, and a catch-up too: it resumes time-shifted at 1x."""
        self.mode = Mode.SHIFTED
        self._user_rate = 1.0

    def user_seeked_back(self) -> None:
        self._leave_live()

    def user_go_live(self) -> None:
        self.mode = Mode.LIVE
        self._rate = 1.0
        self._user_rate = 1.0
        self._reopened = False

    def user_speed(self, rate: float) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        if self.mode is Mode.LIVE:
            return
        self._user_rate = rate
        self.mode = Mode.CATCHING_UP if rate > 1 else Mode.SHIFTED

    def update(self, lag_s: float, buffering: bool) -> Decision:
        """Decide the rate for the measured lag.

        ``reopen_live`` is edge-triggered: once True is returned, later updates return
        False (rate 1.0) until the caller reopens and calls ``user_go_live()``.

        With a bursty source (``burst`` > 0) a lag under one burst less ``_ROOM_BAND`` means
        under half a burst of room when a burst lands: 0.95x until the lag is a burst (half a
        burst of room), then 1x. Between the two the rate stays as it was. A raised Auto
        target (``build``) is built the same way, the larger of the two. Never while
        buffering, time-shifted or at a speed the user chose; a catch-up settles at the
        target (at least both), so it never ends in a slow-down. After ``lock_rate()`` it is
        1x in LIVE, but for the reopen.
        """
        if self.mode is Mode.SHIFTED:
            return Decision(self._user_rate)
        if self.mode is Mode.CATCHING_UP:
            if not buffering and lag_s <= self.target + _SETTLE:
                self.user_go_live()
                return Decision(1.0)
            return Decision(self._user_rate)
        if buffering:
            return Decision(self._rate)
        if lag_s > self.target + _REOPEN:
            self._rate = 1.0
            if self._reopened:
                return Decision(1.0)
            self._reopened = True
            return Decision(1.0, reopen_live=True)
        if self.rate_locked:
            return Decision(self._rate)  # 1x: set by lock_rate(), never changed after
        room = max(self.burst, self.build)
        if lag_s > self.target + _PUSH:
            self._rate = 1.10
        elif lag_s > self.target + _NUDGE:
            # Hysteresis, as between 1.0 and 1.05: a measured lag jitters (bursty sources
            # most), and without it the rate would chatter at the push threshold.
            if self._rate != 1.10 or lag_s <= self.target + _PUSH - _PUSH_HOLD:
                self._rate = 1.05
        elif room > 0 and lag_s < room - _ROOM_BAND:
            self._rate = _SLOW  # under half a burst of room (or a raised target): build it
        elif self._rate == _SLOW and lag_s < room:
            pass  # still building it
        elif lag_s <= self.target + _SETTLE or self._rate == _SLOW:
            # Built, or no longer needed (the burst shrank, the source turned steady, or
            # the target fell): the 1.05 hold band above the target never keeps a slow-down.
            self._rate = 1.0
        return Decision(self._rate)

    def _leave_live(self) -> None:
        if self.mode is Mode.LIVE:
            self.mode = Mode.SHIFTED
            self._user_rate = 1.0
