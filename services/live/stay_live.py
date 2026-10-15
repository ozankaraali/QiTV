"""Stay-live controller: nudges the playback rate to hold a latency target.

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
        elif lag_s <= self.target + _SETTLE:
            self._rate = 1.0
        return Decision(self._rate)

    def _leave_live(self) -> None:
        if self.mode is Mode.LIVE:
            self.mode = Mode.SHIFTED
            self._user_rate = 1.0
