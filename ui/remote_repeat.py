"""Phone-remote holds that behave like a held key: one press at once, from 400 ms one every 40 ms,
and a stop exactly where the finger lifted.

Pure (no Qt): the remote controller drives it with a timer at next_due(). One hold at a time for
the whole TV, as with a keyboard. The phone's release says how many steps it counted by the same
clock; missing steps run at once, and an overshoot from network delay is stepped back on buttons
that have an opposite (the arrows). A phone that goes quiet for 600 ms (asleep, out of Wi-Fi)
ends its hold without correction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from services.remote.protocol import BUTTON_META, DELAY_MS, INTERVAL_MS, SILENCE_MS

# A release may ask for steps QiTV hasn't fired yet (network jitter); at most a second's worth.
CATCH_UP_MAX = 1000 // INTERVAL_MS


@dataclass
class _Hold:
    sid: str
    button: str
    fired: int
    alive: int  # the last word from the phone
    next_at: int


class KeyRepeater:
    def __init__(self, fire: Callable[[str], None]) -> None:
        self._fire = fire
        self._hold: Optional[_Hold] = None

    def down(self, sid: str, button: str, now_ms: int) -> None:
        self._hold = None  # a new press ends any other hold, uncorrected
        self._fire(button)
        if BUTTON_META[button].repeat:
            self._hold = _Hold(sid, button, 1, now_ms, now_ms + DELAY_MS)

    def held(self, sid: str, now_ms: int) -> None:
        if self._hold is not None and self._hold.sid == sid:
            self._hold.alive = now_ms

    def up(self, sid: str, button: str, steps: int, now_ms: int) -> None:
        hold = self._hold
        if hold is None or hold.sid != sid or hold.button != button:
            return
        self.advance(now_ms)
        if self._hold is not hold:
            return  # it went quiet before the release arrived
        self._hold = None
        if steps > hold.fired:
            for _ in range(min(steps - hold.fired, CATCH_UP_MAX)):
                self._fire(button)
        reverse = BUTTON_META[button].reverse
        if steps < hold.fired and reverse is not None:
            for _ in range(hold.fired - steps):
                self._fire(reverse)

    def drop(self, sid: str) -> None:
        if self._hold is not None and self._hold.sid == sid:
            self._hold = None

    def advance(self, now_ms: int) -> None:
        hold = self._hold
        if hold is None:
            return
        quiet_at = hold.alive + SILENCE_MS
        while hold.next_at <= now_ms and hold.next_at < quiet_at:
            self._fire(hold.button)
            hold.fired += 1
            hold.next_at += INTERVAL_MS
            if self._hold is not hold:
                return  # the press itself ended the hold (a dispatch that reached us)
        if now_ms >= quiet_at:
            self._hold = None

    def next_due(self) -> Optional[int]:
        hold = self._hold
        if hold is None:
            return None
        return min(hold.next_at, hold.alive + SILENCE_MS)
