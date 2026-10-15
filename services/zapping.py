"""Channel up/down, number entry and previous channel (spec §5.3)."""

from __future__ import annotations

import time
from typing import Callable, Optional

from PySide6.QtCore import QObject, QTimer, Signal


class ZapContext:
    """The ordered channels ↑/↓ moves through: the group the current channel was tuned from."""

    def __init__(self, group_key: str = "", channels: Optional[list[dict]] = None):
        self.group_key = group_key
        self.channels = list(channels or [])
        self.current_index = -1

    @staticmethod
    def same(a: Optional[dict], b: Optional[dict]) -> bool:
        """Channel identity: the stream address when both have one, else id, else name.

        M3U ids are list positions, so after a playlist change an id can name another
        channel; the address survives reordering.
        """
        if not a or not b:
            return False
        cmd_a, cmd_b = str(a.get("cmd") or "").strip(), str(b.get("cmd") or "").strip()
        if cmd_a and cmd_b:
            return cmd_a == cmd_b
        if a.get("id") is not None and b.get("id") is not None:
            return str(a.get("id")) == str(b.get("id"))
        # name is only a fallback when neither side has a stable identifier
        if all(c.get(k) in (None, "") for c in (a, b) for k in ("id", "cmd")):
            return a.get("name") is not None and a.get("name") == b.get("name")
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

    @staticmethod
    def _number(channel: dict) -> str:
        value = channel.get("number")
        return "" if value is None else str(value).strip()

    def by_number(self, text: str) -> Optional[dict]:
        # Compared numerically ("0" matches "00"); with duplicates the first channel wins.
        if not text.isdecimal():
            return None
        wanted = int(text)
        for channel in self.channels:
            number = self._number(channel)
            if number.isdecimal() and int(number) == wanted:
                return channel
        if not any(self._number(c) for c in self.channels):
            position = wanted
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
        self._pending: list[dict] = []  # requested, not yet confirmed by tuned()
        self._target: Optional[dict] = None  # last channel we asked to tune (may still be pending)
        self._previous: Optional[dict] = None
        self._digits = ""
        self._rest = QTimer(self, singleShot=True, interval=rest_ms)
        self._rest.timeout.connect(self._tune_highlight)
        self._digit_timer = QTimer(self, singleShot=True, interval=digits_ms)
        self._digit_timer.timeout.connect(self._commit_digits)

    def set_context(self, context: ZapContext) -> None:
        self._rest.stop()
        self.close_column()
        self._highlight = None
        self.context = context
        if self._tuned is not None:
            context.set_current(self._tuned)

    def reset(self) -> None:
        """Forget everything about the current provider (call when another one is ready).

        Clears the tuned, previous, target and pending channels and any digits, stops the
        timers, closes the column and leaves an empty context.
        """
        self._rest.stop()
        self._digit_timer.stop()
        self.close_column()
        self._highlight = None
        self._tuned = self._previous = self._target = None
        self._pending = []
        self._last_step = float("-inf")
        self.context = ZapContext()
        if self._digits:
            self._digits = ""
            self.digitsChanged.emit("")

    def tuned(self, channel: dict) -> None:
        if self._tuned is not None and not ZapContext.same(self._tuned, channel):
            self._previous = self._tuned
        self._tuned = channel
        self.context.set_current(channel)
        requested = any(ZapContext.same(p, channel) for p in self._pending)
        if requested and (self._rest.isActive() or not ZapContext.same(channel, self._target)):
            # Late confirmation of an older request: a newer one is still on its way.
            self._pending = [p for p in self._pending if not ZapContext.same(p, channel)]
            return
        self._pending.clear()
        self._target = channel
        self._rest.stop()
        if self._column_open:
            index = self.context.index_of(channel)
            if index >= 0:
                self._highlight = index
                self.highlightChanged.emit(channel, index)
            else:
                self.close_column()

    def _request(self, channel: dict) -> None:
        self._target = channel
        self._pending = [*self._pending, channel][-3:]
        self.tuneRequested.emit(channel)

    def step(self, delta: int) -> None:
        now = self._clock()
        burst = (now - self._last_step) < self._burst_s
        self._last_step = now
        if self._column_open and self._highlight is not None:
            start: Optional[int] = self._highlight
        elif self._target is not None:
            start = self.context.index_of(self._target)
            if start < 0:
                start = None
        else:
            start = None
        target = self.context.peek(delta, start)
        if target is None:
            return
        index, channel = target
        if not burst and not self._column_open:
            self._request(channel)
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
        if self._highlight is None or not 0 <= self._highlight < len(self.context.channels):
            return
        channel = self.context.channels[self._highlight]
        if not ZapContext.same(channel, self._target):
            self._request(channel)

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
            if not ZapContext.same(channel, self._target):
                self._request(channel)
        elif text:
            self.numberNotFound.emit(text)
        self.digitsChanged.emit("")

    def previous(self) -> None:
        dest = self._previous
        if dest is not None and ZapContext.same(dest, self._target):
            dest = self._tuned  # second press before tuned(): toggle back
        if dest is not None and not ZapContext.same(dest, self._target):
            self._request(dest)
