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
* PiP must never wait behind the main stream. A caller passes ``queue=False``;
  when no slot is free, ``request`` returns :data:`DENIED` and queues nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from PySide6.QtCore import QObject, QTimer, Signal

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

    @staticmethod
    def limit_for(provider: dict[str, Any], xtream_max: Optional[int]) -> Optional[int]:
        """Streams allowed at once; None means unlimited."""
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
        entry = self._active.pop(ticket, None)
        self._pending_emit.discard(ticket)
        if entry is not None:
            self._promote(entry[0])

    def cancel(self, ticket: int) -> None:
        """Drop a queued request, or release a held one."""
        for provider, waiting in self._waiting.items():
            for w in waiting:
                if w.ticket == ticket:
                    waiting.remove(w)
                    return
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
            if head.limit is not None and self.in_use(provider_name) >= head.limit:
                break
            waiting.pop(0)
            self._take(head.ticket, provider_name, head.purpose)
        if not waiting:
            self._waiting.pop(provider_name, None)
