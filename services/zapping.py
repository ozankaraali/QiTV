"""Channel up/down, number entry and previous channel (spec §5.3)."""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from PySide6.QtCore import QObject, QTimer, Signal

logger = logging.getLogger(__name__)


class ZapContext:
    """The ordered channels ↑/↓ moves through: the group the current channel was tuned from."""

    def __init__(self, group_key: str = "", channels: Optional[list[dict]] = None):
        self.group_key = group_key
        self.channels = list(channels or [])
        self.current_index = -1

    @staticmethod
    def same(a: Optional[dict], b: Optional[dict]) -> bool:
        if not a or not b:
            return False
        for key in ("id", "cmd", "name"):
            if a.get(key) is not None and b.get(key) is not None:
                return str(a.get(key)) == str(b.get(key))
        return False

    def index_of(self, channel: dict) -> int:
        return next((i for i, c in enumerate(self.channels) if self.same(c, channel)), -1)

    def set_current(self, channel: dict) -> bool:
        index = self.index_of(channel)
        if index >= 0:
            self.current_index = index
        return index >= 0

    def current(self) -> Optional[dict]:
        if 0 <= self.current_index < len(self.channels):
            return self.channels[self.current_index]
        return None

    def peek(self, delta: int, start: Optional[int] = None) -> Optional[tuple[int, dict]]:
        if not self.channels:
            return None
        base = self.current_index if start is None else start
        if base < 0:  # nothing tuned yet: +1 starts at the first channel, -1 at the last
            base = -1 if delta > 0 else 0
        index = (base + delta) % len(self.channels)
        return index, self.channels[index]

    def by_number(self, text: str) -> Optional[dict]:
        digits = text.lstrip("0") or "0"
        for channel in self.channels:
            number = str(channel.get("number") or "").strip().lstrip("0") or None
            if number == digits:
                return channel
        if not any(str(c.get("number") or "").strip() for c in self.channels):
            position = int(digits)
            if 1 <= position <= len(self.channels):
                return self.channels[position - 1]
        return None


class ZapController(QObject):
    tuneRequested = Signal(dict)
    highlightChanged = Signal(dict, int)
    columnRequested = Signal(bool)
    digitsChanged = Signal(str)
    numberNotFound = Signal(str)

    def __init__(
        self,
        rest_ms: int = 400,
        burst_ms: int = 1000,
        digits_ms: int = 1200,
        max_digits: int = 4,
        clock: Callable[[], float] = time.monotonic,
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        self.context = ZapContext()
        self._burst_s = burst_ms / 1000
        self._max_digits = max_digits
        self._clock = clock
        self._last_step = float("-inf")
        self._column_open = False
        self._highlight: Optional[int] = None
        self._tuned: Optional[dict] = None
        self._previous: Optional[dict] = None
        self._digits = ""
        self._rest = QTimer(self, singleShot=True, interval=rest_ms)
        self._rest.timeout.connect(self._tune_highlight)
        self._digit_timer = QTimer(self, singleShot=True, interval=digits_ms)
        self._digit_timer.timeout.connect(self._commit_digits)

    def set_context(self, context: ZapContext) -> None:
        self.context = context
        if self._tuned is not None:
            context.set_current(self._tuned)

    def tuned(self, channel: dict) -> None:
        if self._tuned is not None and not ZapContext.same(self._tuned, channel):
            self._previous = self._tuned
        self._tuned = channel
        self.context.set_current(channel)
        self._rest.stop()

    def step(self, delta: int) -> None:
        now = self._clock()
        burst = (now - self._last_step) < self._burst_s
        self._last_step = now
        start = self._highlight if (self._column_open and self._highlight is not None) else None
        target = self.context.peek(delta, start)
        if target is None:
            return
        index, channel = target
        if not burst and not self._column_open:
            self.tuneRequested.emit(channel)
            return
        if not self._column_open:
            self._column_open = True
            self.columnRequested.emit(True)
        self._highlight = index
        self.highlightChanged.emit(channel, index)
        self._rest.start()

    def close_column(self) -> None:
        if self._column_open:
            self._column_open = False
            self._highlight = None
            self.columnRequested.emit(False)

    def _tune_highlight(self) -> None:
        if self._highlight is None:
            return
        channel = self.context.channels[self._highlight]
        if not ZapContext.same(channel, self._tuned):
            self.tuneRequested.emit(channel)

    def digit(self, d: str) -> None:
        if not d.isdigit() or len(self._digits) >= self._max_digits:
            return
        self._digits += d
        self.digitsChanged.emit(self._digits)
        self._digit_timer.start()

    def _commit_digits(self) -> None:
        text, self._digits = self._digits, ""
        channel = self.context.by_number(text) if text else None
        if channel is not None:
            self.tuneRequested.emit(channel)
        elif text:
            self.numberNotFound.emit(text)
        self.digitsChanged.emit("")

    def previous(self) -> None:
        if self._previous is not None:
            self.tuneRequested.emit(self._previous)
