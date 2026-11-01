"""Channel up/down, number entry and previous channel (spec §5.3)."""

from __future__ import annotations

from itertools import compress, count, repeat
import operator
import time
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from services.provider_ids import provider_of

# Where index_of looks first, around the current row: a zap asks for the channel it is on or
# the one beside it (a request still pending, the next one), and a list can be 264,000 long.
_NEAR = (0, 1, -1, 2, -2)
_NOT_BARE = object()  # (in _Rows._names: a channel with an id or an address)


def _bare(channel: dict) -> bool:
    """Neither an id nor an address: only the name tells it (as ZapContext.same reads it)."""
    return channel.get("id") in (None, "") and channel.get("cmd") in (None, "")


def _first_same(keys: list, key: Any, channels: list[dict], channel: dict, stop: int) -> int:
    """The first row before `stop` whose key is `key` and that is `channel`, else -1. The keys
    are compared by list.index, in C: 264,000 of them in a few milliseconds."""
    row = -1
    while True:
        try:
            row = keys.index(key, row + 1, stop)
        except ValueError:
            return -1
        if ZapContext.same(channels[row], channel):
            return row


class _Rows:
    """What ZapContext.same compares of each row of a list, made once: its address as `same`
    reads it, and when needed its id and, for a channel that has neither, its name. A row
    that is a channel has the channel's address, or (one of the two having none) its id, or
    (both bare) its name; each row found so is checked with `same`."""

    __slots__ = ("cmds", "addressless", "_ids", "_names")

    def __init__(self, channels: list[dict]) -> None:
        self.cmds = [str(c.get("cmd") or "").strip() if c else "" for c in channels]
        self.addressless = self.cmds.count("")  # rows matched by their id or name only
        self._ids: Optional[list[Optional[str]]] = None
        self._names: Optional[list[Any]] = None

    def first(self, channels: list[dict], channel: dict) -> int:
        stop = len(channels)
        best = -1
        cmd = str(channel.get("cmd") or "").strip()
        if cmd:
            best = _first_same(self.cmds, cmd, channels, channel, stop)
        ident = channel.get("id")
        if ident is not None and (not cmd or self.addressless):
            if self._ids is None:
                self._ids = [
                    None if not c or c.get("id") is None else str(c.get("id")) for c in channels
                ]
            row = _first_same(self._ids, str(ident), channels, channel, best if best >= 0 else stop)
            best = row if row >= 0 else best
        if channel.get("name") is not None and _bare(channel):
            if self._names is None:
                self._names = [c.get("name") if c and _bare(c) else _NOT_BARE for c in channels]
            names, name = self._names, channel.get("name")
            row = _first_same(names, name, channels, channel, best if best >= 0 else stop)
            best = row if row >= 0 else best
        return best


class _Numbers:
    """Each row's number as text, made once, and the rows whose number is decimal but not
    written as int() writes it ("007", other scripts' digits): a number is found by list.index
    (in C) among the others, and among these by its value."""

    __slots__ = ("numbers", "numbered", "odd")

    def __init__(self, channels: list[dict]) -> None:
        # (ZapContext.number_of, inline: a call a row cost as much again)
        numbers = ["" if (n := c.get("number")) is None else str(n).strip() for c in channels]
        self.numbers = numbers
        self.numbered = any(numbers)
        self.odd: list[int] = []
        joined = "\n".join(numbers)  # (most lists: plain digits, no leading zero; seen at once)
        if not joined.isascii() or "\n0" in joined or joined[:1] == "0":
            zeros = compress(count(), map(str.startswith, numbers, repeat("0")))
            others = compress(count(), map(operator.not_, map(str.isascii, numbers)))
            self.odd = sorted({*(r for r in zeros if len(numbers[r]) > 1), *others})

    def find(self, channels: list[dict], wanted: int) -> Optional[dict]:
        # Compared numerically ("0" matches "00"); with duplicates the first channel wins.
        numbers = self.numbers
        try:
            row = numbers.index(str(wanted))
        except ValueError:
            row = len(numbers)
        for odd in self.odd:
            if odd >= row:
                break
            if numbers[odd].isdecimal() and int(numbers[odd]) == wanted:
                row = odd
                break
        if row < len(numbers):
            return channels[row]
        if not self.numbered and 1 <= wanted <= len(channels):
            return channels[wanted - 1]  # nothing is numbered: list positions stand in
        return None


class ZapContext:
    """The ordered channels ↑/↓ moves through: the group the current channel was tuned from.

    Its lists are not changed in place: a new list is assigned (what the lookups read of each
    row is made once per list, see _Rows and _Numbers)."""

    def __init__(
        self,
        group_key: str = "",
        channels: Optional[list[dict]] = None,
        fallback: Optional[list[dict]] = None,
        source: str = "",
    ):
        self.group_key = group_key
        # The list the group was picked from (a provider or ALL_PROVIDERS); "" when unknown.
        self.source = source
        self.channels = list(channels or [])
        # Searched by number entry when the group has no match (the shell passes every
        # live channel: TV numbers are global, not per group).
        self.fallback = list(fallback or [])
        self.current_index = -1

    @property
    def channels(self) -> list[dict]:
        return self._channels

    @channels.setter
    def channels(self, channels: list[dict]) -> None:
        self._channels = channels
        self._rows: Optional[_Rows] = None
        self._numbers: Optional[_Numbers] = None
        self._same_lists: Optional[bool] = None

    @property
    def fallback(self) -> list[dict]:
        return self._fallback

    @fallback.setter
    def fallback(self, fallback: list[dict]) -> None:
        self._fallback = fallback
        self._fallback_numbers: Optional[_Numbers] = None
        self._same_lists = None

    @staticmethod
    def same(a: Optional[dict], b: Optional[dict]) -> bool:
        """Channel identity: the stream address when both have one, else id, else name.

        Items stamped with different providers are never the same channel.

        M3U ids are list positions, so after a playlist change an id can name another
        channel; the address survives reordering.
        """
        if not a or not b:
            return False
        provider_a, provider_b = provider_of(a), provider_of(b)
        if provider_a and provider_b and provider_a != provider_b:
            return False  # the same address or id at two providers is two channels
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
        """The row of `channel` (`same`): the current row or one beside it when it is one of
        those (where a zap looks), else the first row that is it; -1 when none is."""
        channels = self._channels
        count = len(channels)
        if not channel or not count:
            return -1
        current = self.current_index
        if 0 <= current < count:
            for delta in _NEAR:
                row = (current + delta) % count
                if self.same(channels[row], channel):
                    return row
        rows = self._rows
        if rows is None or len(rows.cmds) != count:
            rows = self._rows = _Rows(channels)
        return rows.first(channels, channel)

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
    def number_of(channel: dict) -> str:
        """A channel's number as text ("" when it has none)."""
        value = channel.get("number")
        return "" if value is None else str(value).strip()

    _number = number_of  # the earlier private name

    def by_number(self, text: str) -> Optional[dict]:
        """The channel with this number in the group, else in the fallback list."""
        if not text.isdecimal():
            return None
        found = self._group_numbers().find(self._channels, int(text))
        if found is None and self._fallback and not self._fallback_is_group():
            numbers = self._fallback_numbers
            if numbers is None or len(numbers.numbers) != len(self._fallback):
                numbers = self._fallback_numbers = _Numbers(self._fallback)
            found = numbers.find(self._fallback, int(text))
        return found

    def _fallback_is_group(self) -> bool:
        """Whether the fallback lists the group's very channels (the All group's is every
        channel): it would find no other."""
        if self._same_lists is None:
            group, fallback = self._channels, self._fallback
            self._same_lists = group is fallback or (
                len(group) == len(fallback) and all(map(operator.is_, group, fallback))
            )
        return self._same_lists

    def numbered(self) -> bool:
        """Whether any channel of the group has a number (else positions stand in for them)."""
        return self._group_numbers().numbered

    def _group_numbers(self) -> _Numbers:
        numbers = self._numbers
        if numbers is None or len(numbers.numbers) != len(self._channels):
            numbers = self._numbers = _Numbers(self._channels)
        return numbers


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
        max_digits: int = 5,  # channel numbers run past 9999 in long lists
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

    def note_request(self, channel: dict) -> None:
        """Record a tune the shell started itself (drawer pick, autotune, column click).

        It becomes the current target, as if this controller had asked for it, without
        emitting tuneRequested: the next step() moves on from it.
        """
        self._target = channel
        self._pending = [*self._pending, channel][-3:]

    def _request(self, channel: dict) -> None:
        self.note_request(channel)
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

    def cancel_digits(self) -> None:
        """Abandon number entry (Esc): stop the commit timer and clear the digits."""
        self._digit_timer.stop()
        self._digits = ""
        self.digitsChanged.emit("")

    def _commit_digits(self) -> None:
        text, self._digits = self._digits, ""
        channel = self.context.by_number(text) if text else None
        if channel is not None:
            if not ZapContext.same(channel, self._target):
                self._request(channel)
        elif text:
            self.numberNotFound.emit(text)
        self.digitsChanged.emit("")

    def previous_channel(self) -> Optional[dict]:
        """The channel tuned before the current one (None before the second tune)."""
        return self._previous

    def previous(self) -> None:
        dest = self._previous
        if dest is not None and ZapContext.same(dest, self._target):
            dest = self._tuned  # second press before tuned(): toggle back
        if dest is not None and not ZapContext.same(dest, self._target):
            self._request(dest)
