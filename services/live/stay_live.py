"""Stay-live controller: nudges the playback rate to hold a latency target.

Behind the target it speeds up (1.05x, 1.10x, or reopens at live); it slows down only to
build a bursty source's room: 0.95x while less than half a burst would be left when a burst
lands, so the player never meets the live edge just before each one.

On some channels VLC cannot play at any rate but 1x: its position crawls instead (measured in
2.0.2: 4.5 s of media in 37 s). `RateHealth` notices that from VLC's progress, and the caller
then calls `lock_rate()`: from there on stay-live keeps 1x and only reopens at live when the
lag is beyond the target plus 20 s, as before.

Pure logic; the caller feeds the measured lag and applies the returned Decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

TARGETS = {"low": 1.0, "normal": 2.5, "stable": 6.0}

_SETTLE = 0.2
_NUDGE = 0.75
_PUSH = 3.0
_PUSH_HOLD = 1.0  # from 1.10, back to 1.05 only this far below the push threshold
_REOPEN = 20.0
_SLOW = 0.95  # builds a bursty source's room; never slower
_ROOM_BAND = 0.5  # slows only this far under the room it builds: no flapping at its edge
_HEALTH_WINDOW_S = 3.0  # rate health: VLC's progress is judged over this much counted time
_HEALTH_SHARE = 0.7  # under this share of rate x wall time, VLC cannot play at that rate


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
    slow). Each window of `_HEALTH_WINDOW_S` counted seconds is judged on its own; `share` is
    the last window's progress against what its rates asked.
    """

    def __init__(self) -> None:
        self.share = 1.0
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
        if self._wall < _HEALTH_WINDOW_S - 1e-9:
            return False
        self.share = self._played / self._asked if self._asked > 0 else 1.0
        self.reset()
        return self.share < _HEALTH_SHARE


class StayLive:
    def __init__(self, latency: str = "normal") -> None:
        self.mode = Mode.LIVE
        self.target = TARGETS[latency]
        # The source's burst (0.0: steady). Set by the caller with `target`; the lag it feeds
        # is then the mean over a burst: the room left when a burst lands plus half a burst.
        self.burst = 0.0
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
        self.target = TARGETS[name]

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
        burst of room), then 1x. Between the two the rate stays as it was. Never while
        buffering, time-shifted or at a speed the user chose; a catch-up settles at the
        target (at least a burst), so it never ends in a slow-down. After ``lock_rate()``
        it is 1x in LIVE, but for the reopen.
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
        if lag_s > self.target + _PUSH:
            self._rate = 1.10
        elif lag_s > self.target + _NUDGE:
            # Hysteresis, as between 1.0 and 1.05: a measured lag jitters (bursty sources
            # most), and without it the rate would chatter at the push threshold.
            if self._rate != 1.10 or lag_s <= self.target + _PUSH - _PUSH_HOLD:
                self._rate = 1.05
        elif self.burst > 0 and lag_s < self.burst - _ROOM_BAND:
            self._rate = _SLOW  # under half a burst of room: build it
        elif self._rate == _SLOW and lag_s < self.burst:
            pass  # still building it
        elif lag_s <= self.target + _SETTLE or self._rate == _SLOW:
            # Built, or no longer needed (the burst shrank, or the source turned steady):
            # the 1.05 hold band above the target never keeps a slow-down.
            self._rate = 1.0
        return Decision(self._rate)

    def _leave_live(self) -> None:
        if self.mode is Mode.LIVE:
            self.mode = Mode.SHIFTED
            self._user_rate = 1.0
