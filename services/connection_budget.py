"""Per-provider simultaneous-stream budget (spec 6.9).

Many IPTV providers allow only one or two concurrent streams. Callers ask for a
slot with :meth:`ConnectionBudget.request` and give it back with
:meth:`release` once the old stream is fully closed, so the old stream is
always released before a new one opens.

Design notes:

* GUI-thread only. No threads or timers of its own.
* ``granted`` is always emitted *deferred* (``QTimer.singleShot(0, self, ...)``),
  even when a slot is free. The caller only learns its ticket when ``request``
  returns, so a synchronous emit would be missed by a handler that compares the
  ticket with a value assigned from the return value. The slot is counted as in
  use from the moment of the request, so the budget is never over-committed in
  between. A ticket cancelled or released before the deferred emit never fires.
* Only the newest queued request per ``(provider, purpose)`` is kept: this is
  exactly what zapping needs.
* Grant order across purposes is FIFO by each purpose's newest request (a
  replacing request moves behind the others). Each waiter keeps the ``limit``
  it was requested with (a snapshot); the head of the queue decides whether a
  slot is free.
* PiP must never wait behind the main stream. A caller passes ``queue=False``;
  when no slot is free, ``request`` returns :data:`DENIED` and queues nothing.
* A limit can be learned for the run (2.6: a provider that keeps only the newest stream
  while a recording holds one): ``learn`` lowers it, never raises it; ``limit_for`` and
  ``request`` apply it (``cap``), so every caller sees it. ``free`` says whether a purpose
  could take a stream beside the others now.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from PySide6.QtCore import QObject, QTimer, Signal

# Returned (never a valid ticket) by request(queue=False) when no slot is free.
# release()/cancel() ignore it, so callers may pass it back unchecked.
DENIED = -1  # returned by request(queue=False) when no slot is free


@dataclass
class _Waiter:
    ticket: int
    purpose: str
    limit: Optional[int]


class ConnectionBudget(QObject):
    granted = Signal(int)  # ticket

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._next = 1
        self._active: dict[int, tuple[str, str]] = {}  # ticket -> (provider, purpose)
        self._waiting: dict[str, list[_Waiter]] = {}  # provider -> FIFO
        self._pending_emit: set[int] = set()
        self._learned: dict[str, int] = {}  # provider -> a limit learned for this run (2.6)

    def learn(self, provider_name: str, limit: int) -> None:
        """For the rest of the run, `provider_name` allows `limit` streams at most."""
        if limit > 0:
            known = self._learned.get(provider_name)
            self._learned[provider_name] = limit if known is None else min(known, limit)

    def cap(self, provider_name: str, limit: Optional[int]) -> Optional[int]:
        """`limit` under what was learned for `provider_name` (None: unlimited)."""
        learned = self._learned.get(provider_name)
        if learned is None:
            return limit
        return learned if limit is None else min(limit, learned)

    def free(self, provider_name: str, limit: Optional[int], *, purpose: str) -> bool:
        """`purpose` could take a stream of `provider_name` beside the others' now."""
        capped = self.cap(provider_name, limit)
        if capped is None:
            return True
        others = [p for p in self.holders(provider_name) if p != purpose]
        return len(others) < capped

    def limit_for(self, provider: dict[str, Any], xtream_max: Optional[int]) -> Optional[int]:
        """Streams allowed at once, under what was learned this run; None means unlimited."""
        return self.cap(str(provider.get("name", "")), self._given_limit(provider, xtream_max))

    @staticmethod
    def _given_limit(provider: dict[str, Any], xtream_max: Optional[int]) -> Optional[int]:
        """Streams allowed at once as the provider's settings say; None means unlimited."""
        try:
            override = int(provider.get("max_streams") or 0)
        except TypeError, ValueError:
            override = 0
        if override > 0:
            return override
        ptype = str(provider.get("type", ""))
        if ptype == "XTREAM":
            if xtream_max is not None and xtream_max > 0:
                return xtream_max
            return None
        if ptype == "STB":
            return 1
        return None

    def in_use(self, provider_name: str) -> int:
        return sum(1 for prov, _ in self._active.values() if prov == provider_name)

    def holders(self, provider_name: str) -> list[str]:
        return [p for prov, p in self._active.values() if prov == provider_name]

    def provider_of(self, ticket: Optional[int]) -> Optional[str]:
        """The provider a held ticket counts against (None: not held, or no ticket)."""
        entry = self._active.get(ticket) if ticket is not None else None
        return entry[0] if entry is not None else None

    def request(
        self,
        provider_name: str,
        limit: Optional[int],
        *,
        purpose: str,
        queue: bool = True,
    ) -> int:
        ticket = self._next
        self._next += 1
        limit = self.cap(provider_name, limit)
        free = limit is None or self.in_use(provider_name) < limit
        if free:
            self._take(ticket, provider_name, purpose)
            return ticket
        if not queue:
            return DENIED
        waiting = self._waiting.setdefault(provider_name, [])
        waiting[:] = [w for w in waiting if w.purpose != purpose]
        waiting.append(_Waiter(ticket, purpose, limit))
        return ticket

    def release(self, ticket: int) -> None:
        """Give a slot back, or drop the request if it is still queued.

        Unknown tickets and ``DENIED`` are ignored.
        """
        entry = self._active.pop(ticket, None)
        if entry is None:
            self.cancel(ticket)
            return
        self._pending_emit.discard(ticket)
        self._promote(entry[0])

    def relabel(self, ticket: int, purpose: str) -> None:
        """Give a held or queued ticket another purpose (a PiP swap exchanges the roles).

        Its place is kept. Unknown tickets and ``DENIED`` are ignored.
        """
        entry = self._active.get(ticket)
        if entry is not None:
            self._active[ticket] = (entry[0], purpose)
            return
        for waiting in self._waiting.values():
            for w in waiting:
                if w.ticket == ticket:
                    w.purpose = purpose
                    return

    def cancel(self, ticket: int) -> None:
        """Drop a queued request, or release a held one. ``DENIED`` is ignored."""
        if ticket == DENIED:
            return
        for provider in list(self._waiting):
            waiting = self._waiting[provider]
            for w in waiting:
                if w.ticket == ticket:
                    waiting.remove(w)
                    if not waiting:
                        del self._waiting[provider]
                    return
        if ticket in self._active:
            self.release(ticket)

    def _take(self, ticket: int, provider_name: str, purpose: str) -> None:
        self._active[ticket] = (provider_name, purpose)
        self._pending_emit.add(ticket)
        QTimer.singleShot(0, self, lambda t=ticket: self._emit_granted(t))

    def _emit_granted(self, ticket: int) -> None:
        if ticket in self._pending_emit:
            self._pending_emit.discard(ticket)
            self.granted.emit(ticket)

    def _promote(self, provider_name: str) -> None:
        waiting = self._waiting.get(provider_name, [])
        while waiting:
            head = waiting[0]
            limit = self.cap(provider_name, head.limit)
            if limit is not None and self.in_use(provider_name) >= limit:
                break
            waiting.pop(0)
            self._take(head.ticket, provider_name, head.purpose)
        if not waiting:
            self._waiting.pop(provider_name, None)
