"""2.15: a phone's Watch of a site's channel (Kick, Twitch) that isn't live now waits for it.

Before, it failed. Now the phone says "This channel isn't live now · It plays as soon as it's
live", and QiTV asks the site again as the large picture does (ui/live_controls.py's
OFFLINE_ASK_S and its jitter): after at least 30 s, one ask at a time, the next only once the
last answered, and none while every phone waiting is away (its page closed: asked again once one
is back). A wait is the channel's: the phones waiting for it (by device: a phone that slept comes
back as another session) share its asks. The phones hold no stream meanwhile. Once the site has
a stream, it plays on one phone, the one that asked last (one screen at a time); the others are
told another screen took over. A phone's wait ends when it asks for something else, hands back
to the TV, leaves for good or is unpaired; the channel's when its site says something else than
"not live", and when the TV plays something else (one screen at a time).

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
    """One channel waited for: the channel (a Target), the group it was picked in, the phones
    waiting for it (the last to ask last), and what the TV showed as the wait began (`tv`:
    the TV playing something else since ends it)."""

    target: Any
    group: Optional[tuple[str, list]] = field(default=None, repr=False)
    devices: list[str] = field(default_factory=list)
    tv: Any = field(default=None, repr=False)
    asking: bool = False  # an ask is on its way: the next waits for its answer
    timer: Optional[QTimer] = field(default=None, repr=False)


# `ask(wait)`: ask the site again for `wait`'s channel; False when it could not be asked now
# (every phone waiting is away), and the wait goes on to the next time.
Ask = Callable[[SiteWait], bool]


class SiteWaits(QObject):
    """The channels phones wait for to be live."""

    def __init__(self, ask: Ask, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._ask = ask
        self._waits: list[SiteWait] = []

    def waits(self) -> list[SiteWait]:
        return list(self._waits)

    def wait_of(self, device: str) -> Optional[SiteWait]:
        """The wait phone `device` is one of (None: it waits for nothing)."""
        return next((wait for wait in self._waits if device in wait.devices), None)

    def waiting(self, device: str) -> bool:
        return self.wait_of(device) is not None

    def current(self, wait: SiteWait) -> bool:
        return any(wait is other for other in self._waits)

    def failed(
        self,
        device: str,
        target: Any,
        group: Optional[tuple[str, list]],
        why: str,
        tv: Any = None,
    ) -> Optional[str]:
        """Why phone `device`'s own ask for `target` (its Watch) failed (as it is kept:
        English): "new" when it begins to wait and the channel's wait begins with it (asked
        again after the calm wait), "joined" when it begins to wait for a channel other phones
        wait for already (the channel's asks and an ask on its way are left as they are), "on"
        when it waited for it already (the site still says so, or never answered), None for
        anything else (its wait, if any, ends: the reason is said as ever)."""
        mine = self.wait_of(device)
        channel = self._of_channel(target)
        waits_on = mine is not None and mine is channel
        if not (is_offline(why) or (waits_on and why in (TIMED_OUT, FAILED))):
            self.end(device)
            return None
        if mine is not None and mine is not channel:
            self.end(device)
        if waits_on:
            return "on"
        if channel is not None:
            channel.devices.append(device)  # the last to ask: the one it plays on once live
            return "joined"
        channel = SiteWait(target, group, devices=[device], tv=tv)
        self._waits.append(channel)
        delay = _next_ask()
        self._arm(channel, delay)  # (it was just asked: the next after the calm wait)
        logger.info(
            "%s is not live now (a phone's); asking its site again in %.0f s", target.title, delay
        )
        return "new"

    def answered(self, wait: SiteWait, why: str) -> Optional[bool]:
        """The channel's ask again got no stream: False when it waits on (the site says it
        isn't live, or never answered), None for anything else: the wait ends (the caller
        tells its phones). An answer to a wait that ended meanwhile: False, nothing changes."""
        if not self.current(wait):
            return False
        if is_offline(why) or why in (TIMED_OUT, FAILED):
            wait.asking = False
            self._arm(wait, _next_ask())
            return False
        self._drop(wait)
        return None

    def ask_later(self, wait: SiteWait) -> None:
        """The channel's ask came to nothing (its stream was not taken): asked again after the
        calm wait, not the lost answer's."""
        if self.current(wait):
            wait.asking = False
            self._arm(wait, _next_ask())

    def take(self, wait: SiteWait) -> Optional[list[str]]:
        """The site gave the channel a stream: its phones (the last to ask last), and the wait
        is over; None when it ended meanwhile (the stream is not handed on)."""
        if not self.current(wait):
            return None
        devices = list(wait.devices)
        self._drop(wait)
        return devices

    def end(self, device: str) -> None:
        """Phone `device` waits no more; a channel no phone waits for is asked for no more."""
        wait = self.wait_of(device)
        if wait is None:
            return
        wait.devices.remove(device)
        if not wait.devices:
            self._drop(wait)

    def end_wait(self, wait: SiteWait) -> list[str]:
        """The channel is waited for no more (the TV took over): its phones."""
        if not self.current(wait):
            return []
        devices = list(wait.devices)
        self._drop(wait)
        return devices

    def end_all(self) -> None:
        for wait in list(self._waits):
            self._drop(wait)

    def keep_only(self, devices: set[str]) -> None:
        """The paired phones are `devices`: those unpaired wait no more."""
        for wait in list(self._waits):
            for device in list(wait.devices):
                if device not in devices:
                    self.end(device)

    def _of_channel(self, target: Any) -> Optional[SiteWait]:
        return next(
            (wait for wait in self._waits if ZapContext.same(wait.target.item, target.item)),
            None,
        )

    def _drop(self, wait: SiteWait) -> None:
        self._waits = [other for other in self._waits if other is not wait]
        if wait.timer is not None:
            wait.timer.stop()
            wait.timer.deleteLater()
            wait.timer = None

    def _arm(self, wait: SiteWait, seconds: float) -> None:
        if wait.timer is None:
            wait.timer = QTimer(self)
            wait.timer.setSingleShot(True)
            wait.timer.timeout.connect(lambda wait=wait: self._due(wait))
        wait.timer.start(round(seconds * 1000))

    def _due(self, wait: SiteWait) -> None:
        if not self.current(wait):
            return
        wait.asking = True
        self._arm(wait, LOST_S)  # its answer arms the next ask; none by then: asked again
        try:
            asked = self._ask(wait)
        except Exception as error:  # a phone's wait must never take QiTV down
            logger.error("A phone's wait for a channel: asking failed (%s)", type(error).__name__)
            asked = False
        if not asked and self.current(wait) and wait.asking:
            wait.asking = False
            self._arm(wait, _next_ask())  # every phone away: asked once one is back, next turn


def _next_ask() -> float:
    """The wait before the next ask: as the large picture's, with its jitter (QiTVs that waited
    together never ask together)."""
    return live_controls.OFFLINE_ASK_S + random.uniform(0.0, live_controls.OFFLINE_JITTER_S)
