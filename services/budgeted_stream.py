"""One live stream's slot in the provider's connection budget (spec §6.9).

Providers count connections, and many allow one or two, so the old stream must be released
before a new one opens. A `BudgetedStream` holds one `LiveSession`'s budget ticket and keeps
that order for it (the main stream's `LiveTuner` and the PiP slot's `SessionManager` each
own one):
- `request()` asks for a ticket for the next start; the budget always grants later, and
  `granted` follows for the latest request only;
- `start()` starts the live session on that ticket; if it raises, the ticket goes back;
- `stop()` stops a running session and gives its ticket back once `LiveSession.released`
  says its upstream is closed (also when that comes inside `stop()`), or after
  `RELEASE_CAP_MS`; `released` follows then. A ticket that never started goes back at once.

`released` from a stop this object did not make (or after its cap) is ignored.

A `HeldSlot` is the slot of a stream that plays outside a `LiveSession`: the main player's
direct channel, movie or episode. It is taken at once, whatever the limit (the player's own
release gate already orders its streams), so that other consumers (Phase 4's PiP) see the
slot taken; its owner gives it back once the player let go of that stream.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from services.connection_budget import DENIED, ConnectionBudget
from services.live.live_session import LiveSession

logger = logging.getLogger(__name__)

# LiveSession caps its own waits for the release at 2 s; this backstop also covers a stop
# that emits nothing (`LiveSession.stop()` on a session someone else had already stopped).
RELEASE_CAP_MS = 2500


class BudgetedStream(QObject):
    granted = Signal()  # the latest request's ticket was granted: it may start
    released = Signal()  # a stop's upstream is closed (or the cap passed): its ticket is back

    def __init__(
        self,
        live: LiveSession,
        budget: ConnectionBudget,
        purpose: str,
        *,
        cap_ms: int = RELEASE_CAP_MS,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.live = live
        self.budget = budget
        self.purpose = purpose
        self.ticket: Optional[int] = None  # the pending or running stream's
        self.disk = False  # the running stream keeps its time-shift on disk
        self._granted = False
        self._running = False  # live.start() was called and not stopped since
        self._releasing: list[int] = []  # given back once the stopped upstream is closed
        self._waiting = False  # live.stop() was called; released() has not come
        self._cap = QTimer(self, singleShot=True, interval=cap_ms)
        self._cap.timeout.connect(self._on_cap)
        budget.granted.connect(self._on_granted)
        live.released.connect(self._on_released)

    def request(self, provider: str, limit: Optional[int], *, queue: bool = True) -> int:
        """Ask for a ticket for the next start, in place of any held one.

        Returns it, or `DENIED` (holding none) when `queue` is False and no slot is free.
        """
        self.stop()
        ticket = self.budget.request(provider, limit, purpose=self.purpose, queue=queue)
        if ticket != DENIED:
            self.ticket = ticket
        return ticket

    def set_purpose(self, purpose: str) -> None:
        """Take another role (a PiP swap): the held and the releasing tickets follow."""
        self.purpose = purpose
        for ticket in (self.ticket, *self._releasing):
            if ticket is not None:
                self.budget.relabel(ticket, purpose)

    def is_granted(self) -> bool:
        return self._granted

    def running(self) -> bool:
        """`start()` was called and the stream not stopped since."""
        return self._running

    def waiting(self) -> bool:
        """A stopped stream's upstream may still be open: `released` has not come yet."""
        return self._waiting

    def start(self, *args: Any, **kwargs: Any) -> None:
        """Start the live session on the held ticket; if it raises, the ticket goes back."""
        self._running = True
        self.disk = bool(kwargs.get("disk"))
        try:
            self.live.start(*args, **kwargs)
        except Exception:
            self._running = self._granted = self.disk = False
            ticket, self.ticket = self.ticket, None
            if ticket is not None:
                self.budget.release(ticket)
            try:
                self.live.stop()
            except Exception:
                logger.debug("Stopping the live session after a failed start failed too")
            raise

    def stop(self) -> None:
        """Stop the stream; a held ticket goes back once its upstream is closed."""
        ticket, self.ticket = self.ticket, None
        self._granted = self.disk = False
        if self._running:
            self._running = False
            if ticket is not None:
                self._releasing.append(ticket)
            self._waiting = True
            self._cap.start()
            self.live.stop()  # released() follows, perhaps at once
        elif ticket is not None:
            self.budget.release(ticket)  # never started: nothing upstream to wait for

    def _on_granted(self, ticket: int) -> None:
        if ticket == self.ticket and not self._running:
            self._granted = True
            self.granted.emit()

    def _on_released(self) -> None:
        if not self._waiting:
            return  # a stop this object did not make
        self._waiting = False
        self._cap.stop()
        tickets, self._releasing = self._releasing, []
        for ticket in tickets:
            self.budget.release(ticket)
        self.released.emit()

    def _on_cap(self) -> None:
        if self._waiting:
            logger.warning(
                "The %s stream did not report its upstream closed; going on.", self.purpose
            )
            self._on_released()


class HeldSlot:
    """A budget ticket for a stream that plays outside a `LiveSession` (see the docstring)."""

    def __init__(self, budget: ConnectionBudget, purpose: str) -> None:
        self.budget = budget
        self.purpose = purpose
        self.ticket: Optional[int] = None

    def take(self, provider: str) -> None:
        """Hold a ticket for `provider` in place of any held one; never queued."""
        self.give_back()
        self.ticket = self.budget.request(provider, None, purpose=self.purpose)

    def set_purpose(self, purpose: str) -> None:
        self.purpose = purpose
        if self.ticket is not None:
            self.budget.relabel(self.ticket, purpose)

    def give_back(self) -> None:
        ticket, self.ticket = self.ticket, None
        if ticket is not None:
            self.budget.release(ticket)
