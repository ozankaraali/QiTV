"""A provider that keeps only the newest stream (2.6, Record as a PVR): the recording wins.

Some providers do not say how many streams they allow, and when a second one opens they close
the older. A recording would lose its stream to a zap. So, while a channel records at a
provider whose limit is unknown, the guard notes each stream that starts there; when the
recording's stream breaks (reconnecting, or failed) within `DROP_WINDOW_S` of another one of
that provider starting, that newer stream is stopped, the provider counts as allowing as many
streams as were open before it for the rest of the run (`ConnectionBudget.learn`, so every
later open is refused as over the limit or asks first), and a toast says why. The recording
reconnects by itself. A newer stream that records too is never stopped for an older one.
"""

from __future__ import annotations

from functools import partial
import logging
import time
from typing import Callable, Optional

from PySide6.QtCore import QObject

from services.connection_budget import ConnectionBudget
from services.i18n import N_, translate
from services.sessions import Slot, records

logger = logging.getLogger(__name__)

DROP_WINDOW_S = 10.0  # a break this soon after another stream started is that stream's doing
# i18n: a toast: the provider closed the recording's stream when another one opened;
# {channel} is the recording channel
DROPPED = N_("Your provider allows no other stream while {channel} records")


class RecordGuard(QObject):
    def __init__(
        self,
        budget: ConnectionBudget,
        slots: Callable[[], list[Slot]],
        *,
        limit_of: Callable[[Slot], Optional[int]],
        stop: Callable[[Slot], None],
        toast: Callable[[str], None],
        clock: Callable[[], float] = time.monotonic,
        parent: Optional[QObject] = None,
    ) -> None:
        """`slots()` every player, `limit_of(slot)` the limit of the provider of what it plays
        (None: unknown), `stop(slot)` stops a player's stream (the shell's, by its role)."""
        super().__init__(parent)
        self._budget = budget
        self._slots = slots
        self._limit_of = limit_of
        self._stop = stop
        self._toast = toast
        self._clock = clock
        self._starts: list[tuple[str, str, float]] = []  # (provider, purpose, when) of late
        budget.granted.connect(self._on_granted)

    def watch_slot(self, slot: Slot) -> None:
        """Follow a player's stream: each player's at wiring, a made one's when added."""
        slot.live.timelineChanged.connect(partial(self._on_timeline, slot))
        slot.live.failed.connect(lambda _message, s=slot: self._on_break(s))

    def _on_granted(self, ticket: int) -> None:
        entry = self._budget.entry(ticket)
        if entry is None:
            return
        now = self._clock()
        self._starts = [s for s in self._starts if now - s[2] <= DROP_WINDOW_S]
        self._starts.append((entry[0], entry[1], now))

    def _on_timeline(self, slot: Slot, state: dict) -> None:
        if state.get("reconnecting"):
            self._on_break(slot)

    def _on_break(self, slot: Slot) -> None:
        if not records(slot):
            return
        own = _purpose(slot)
        stream = getattr(slot.tuner, "stream", None)
        provider = self._budget.provider_of(getattr(stream, "ticket", None))
        if provider is None or self._limit_of(slot) is not None:
            return  # a known limit: the budget never opened a stream too many
        now = self._clock()
        newer = [
            purpose
            for name, purpose, when in self._starts
            if name == provider and purpose != own and now - when <= DROP_WINDOW_S
        ]
        if not newer:
            return
        culprit = next((s for s in self._slots() if _purpose(s) == newer[-1]), None)
        if culprit is None or records(culprit):
            return
        self._starts = [s for s in self._starts if s[0] != provider]
        allowed = max(1, len(self._budget.holders(provider)) - 1)
        self._budget.learn(provider, allowed)
        logger.info(
            "A provider closed a recording's stream as another opened: it allows %d now",
            allowed,
        )
        self._stop(culprit)
        channel = str((slot.channel or {}).get("name") or "")
        self._toast(translate(DROPPED, channel=channel))


def _purpose(slot: Slot) -> str:
    return str(getattr(slot.tuner, "purpose", slot.name))
