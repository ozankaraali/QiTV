"""Stay-live controller: nudges the playback rate to hold a latency target.

Behind the target it speeds up (1.05x, 1.10x, or reopens at live); it slows down only to
build a bursty source's room: 0.95x while less than half a burst would be left when a burst
lands, so the player never meets the live edge just before each one.

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


class Mode(str, Enum):
    LIVE = "live"
    SHIFTED = "shifted"
    CATCHING_UP = "catching_up"


@dataclass(frozen=True)
class Decision:
    rate: float
    reopen_live: bool = False


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
        target (at least a burst), so it never ends in a slow-down.
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
