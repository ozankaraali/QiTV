"""2.15: a phone's Watch of a site's channel (Kick, Twitch) that isn't live now waits for it.

Before, it failed. Now the phone says "This channel isn't live now · It plays as soon as it's
live", and QiTV asks the site again as the large picture does (ui/live_controls.py's
OFFLINE_ASK_S and its jitter): after at least 30 s, one ask at a time, the next only once the
last answered, and none while the phone is away (its page closed: asked again once it is back).
The phone holds no stream meanwhile. Once the site has a stream, the phone is handed it as any
Watch is. A wait is the phone's (its device's: a phone that slept comes back as another
session), and ends when it asks for something else, hands back to the TV, leaves for good or is
unpaired, and when its site says something else than "not live" (said as ever).

GUI thread only: the timers are made here, never on a worker.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import random
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QTimer

from services.network_streams import FAILED, TIMED_OUT, is_offline
from services.zapping import ZapContext
from ui import live_controls

logger = logging.getLogger(__name__)

LOST_S = 90.0  # an ask with no answer this long (dropped on its way) is made again


@dataclass(eq=False)
class SiteWait:
    """What one phone waits for: the channel (a Target) and the group it was picked in."""

    target: Any
    group: Optional[tuple[str, list]] = field(default=None, repr=False)
    asking: bool = False  # an ask is on its way: the next waits for its answer


# `ask(device, wait)`: ask the site again for `wait`'s channel for phone `device`; False when it
# could not be asked now (the phone is away), and the wait goes on to the next time.
Ask = Callable[[str, SiteWait], bool]


class SiteWaits(QObject):
    """The phones that wait for a site's channel to be live, by device."""

    def __init__(self, ask: Ask, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._ask = ask
        self._waits: dict[str, SiteWait] = {}
        self._timers: dict[str, QTimer] = {}

    def waiting(self, device: str) -> bool:
        return device in self._waits

    def wait_of(self, device: str) -> Optional[SiteWait]:
        return self._waits.get(device)

    def failed(
        self, device: str, target: Any, group: Optional[tuple[str, list]], why: str
    ) -> Optional[bool]:
        """Why phone `device`'s ask for `target` failed (as it is kept: English): True when
        its wait begins (the site says it isn't live now), False when it waits on (asked again
        while waiting, the site still says so, or never answered: as the large picture waits
        on), None for anything else (its wait, if any, ends: the reason is said as ever)."""
        wait = self._waits.get(device)
        same = wait is not None and ZapContext.same(wait.target.item, target.item)
        if not (is_offline(why) or (same and why in (TIMED_OUT, FAILED))):
            self.end(device)
            return None
        if not same:
            self.end(device)
            wait = SiteWait(target, group)
            self._waits[device] = wait
        assert wait is not None
        wait.asking = False
        delay = _next_ask()
        logger.info(
            "%s is not live now (a phone's); asking its site again in %.0f s", target.title, delay
        )
        self._arm(device, delay)
        return not same

    def take(self, device: str, wait: SiteWait) -> bool:
        """The site gave `wait`'s channel a stream: True (and the wait is over) while it is
        still the phone's wait; False when it ended meanwhile (the stream is not handed on)."""
        if self._waits.get(device) is not wait:
            return False
        self.end(device)
        return True

    def end(self, device: str) -> None:
        """Phone `device` waits no more (nothing is asked for it again)."""
        self._waits.pop(device, None)
        timer = self._timers.pop(device, None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()

    def end_all(self) -> None:
        for device in list(self._waits):
            self.end(device)

    def keep_only(self, devices: set[str]) -> None:
        """The paired phones are `devices`: those unpaired wait no more."""
        for device in list(self._waits):
            if device not in devices:
                self.end(device)

    def _arm(self, device: str, seconds: float) -> None:
        timer = self._timers.get(device)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda device=device: self._due(device))
            self._timers[device] = timer
        timer.start(round(seconds * 1000))

    def _due(self, device: str) -> None:
        wait = self._waits.get(device)
        if wait is None:
            return
        wait.asking = True
        self._arm(device, LOST_S)  # its answer arms the next ask; none by then: asked again
        try:
            asked = self._ask(device, wait)
        except Exception as error:  # a phone's wait must never take QiTV down
            logger.error("A phone's wait for a channel: asking failed (%s)", type(error).__name__)
            asked = False
        if not asked and self._waits.get(device) is wait:
            wait.asking = False
            self._arm(device, _next_ask())  # away: asked once it is back, at the next turn


def _next_ask() -> float:
    """The wait before the next ask: as the large picture's, with its jitter (QiTVs that waited
    together never ask together)."""
    return live_controls.OFFLINE_ASK_S + random.uniform(0.0, live_controls.OFFLINE_JITTER_S)
