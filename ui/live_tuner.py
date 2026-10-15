"""Opens the TV shell's provider streams one at a time (spec §6.9).

Before anything opens, the stream before it is released: providers count connections, and
many allow one or two. "Released" means:
- the time-shift pipeline's `LiveSession.released()`: its upstream is closed and, after
  direct play, libVLC let go of the provider stream (LiveSession bounds both waits);
- the player's own direct play (a movie, an episode, a channel the pipeline cannot read):
  `PlaybackSession.inputEnded`, since libVLC ends its input on a worker after `stop()`.
Each wait is capped (RELEASE_CAP_MS; the LiveSession one a little later, as a backstop to
its own caps). Only the newest open waits: a newer one replaces it.

A pipeline tune also takes a slot of the provider's connection budget (purpose "main") and
starts once the slot is granted and the old stream is released; the old slot goes back on
that release. With a limit of one, the new request waits at the head of the queue, so the
freed slot goes to it and to no other consumer. A pause or Go live pressed during that
wait applies once the tune starts.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from services.connection_budget import ConnectionBudget
from services.live.live_session import LiveSession
from services.playback import Session

logger = logging.getLogger(__name__)

RELEASE_CAP_MS = 2000  # the longest wait for libVLC to let go of a direct stream
LIVE_RELEASE_CAP_MS = RELEASE_CAP_MS + 500  # LiveSession caps its own waits at 2 s
PURPOSE = "main"  # the budget purpose of the main live stream (Phase 4 adds "pip")
RAM_RING_MIB = 128  # the time-shift buffer without consent to use the disk (spec §8)


@dataclass
class LiveStart:
    """What a pipeline tune opens: the provider URL and how to ask for it."""

    url: str
    user_agent: str
    verify_ssl: bool


class LiveTuner(QObject):
    startFailed = Signal(str)  # LiveSession.start() raised: its message (scrub before showing)

    def __init__(
        self,
        session: Session,
        config: Any,
        live: Optional[LiveSession] = None,
        budget: Optional[ConnectionBudget] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.session = session
        self.config = config  # live_latency, timeshift_enabled, timeshift_max_mib
        self.live = live
        self.budget = budget if budget is not None else ConnectionBudget(self)
        self.timeline: Optional[dict] = None  # the running tune's last timelineChanged state
        self._pending: Optional[LiveStart] = None  # a tune waiting for its slot or the release
        self._ticket: Optional[int] = None  # the slot of the pending or running tune
        self._granted = False
        self._running = False  # live.start() was called and not stopped since
        self._latency: Optional[str] = None  # what the running tune started with
        self._pause = False  # pressed while the pending tune waited: applied at its start
        self._releasing: list[int] = []  # slots given back once the old upstream is closed
        self._live_wait = False  # live.stop() was called; released() has not come
        self._input_wait = False  # a direct play was stopped; inputEnded has not come
        self._direct = False  # the player plays a provider stream an open() asked for
        self._action: Optional[Callable[[], None]] = None  # an open waiting for the release
        self._action_direct = False
        self._live_cap = QTimer(self, singleShot=True, interval=LIVE_RELEASE_CAP_MS)
        self._live_cap.timeout.connect(self._on_live_cap)
        self._input_cap = QTimer(self, singleShot=True, interval=RELEASE_CAP_MS)
        self._input_cap.timeout.connect(self._on_input_cap)
        self.budget.granted.connect(self._on_granted)
        session.inputEnded.connect(self._on_input_ended)
        if live is not None:
            live.released.connect(self._on_released)
            live.timelineChanged.connect(self._on_timeline)

    # ---- opening ------------------------------------------------------------------------------

    def open(self, action: Callable[[], None], *, direct: bool = False) -> None:
        """Stop what plays, then run `action` once it is released: now when nothing waits.

        `direct`: `action` plays a provider URL in the session, whose input the next open
        then waits for.
        """
        self.stop()
        self._action, self._action_direct = action, direct
        self._proceed()

    def tune(self, start: LiveStart, provider: str, limit: Optional[int]) -> None:
        """Stop what plays and start `start` through the pipeline with a budget slot."""
        assert self.live is not None
        self.stop()
        self._pending = start
        self._ticket = self.budget.request(provider, limit, purpose=PURPOSE)

    def refused(self, provider: str, limit: Optional[int]) -> bool:
        """Whether other streams (Phase 4: picture in picture) take the whole limit."""
        if limit is None:
            return False
        others = [purpose for purpose in self.budget.holders(provider) if purpose != PURPOSE]
        return limit <= 0 or len(others) >= limit

    def stop(self) -> None:
        """Stop what plays or waits to; a held slot goes back once its upstream is closed."""
        self._action = None
        self._pending = None
        self._granted = self._pause = False
        self.timeline = None
        ticket, self._ticket = self._ticket, None
        if self._running and self.live is not None:
            self._running = False
            if ticket is not None:
                self._releasing.append(ticket)
            self._live_wait = True
            self._live_cap.start()
            self.live.stop()  # released() follows, at once when nothing is left open
        elif ticket is not None:
            self.budget.release(ticket)  # never started: nothing upstream to wait for
        if self._direct:
            self._direct = False
            self.session.stop()
            if self.session.holds_input():
                self._input_wait = True
                self._input_cap.start()

    def shutdown(self) -> None:
        direct = self._direct
        self.stop()
        if not direct:
            self.session.stop()  # whatever else the player holds

    def _proceed(self) -> None:
        if self._live_wait or self._input_wait:
            return
        if self._action is not None:
            action, self._action = self._action, None
            self._direct = self._action_direct
            action()
        elif self._pending is not None and self._granted:
            self._start()

    def _start(self) -> None:
        pending, live = self._pending, self.live
        if pending is None or live is None:
            return
        self._pending = None
        config = self.config
        disk = bool(config.timeshift_enabled)
        mib = config.timeshift_max_mib if disk else RAM_RING_MIB
        self._latency = config.live_latency
        self._running = True
        try:
            live.start(
                pending.url,
                headers={"User-Agent": pending.user_agent},
                verify_ssl=pending.verify_ssl,
                latency=config.live_latency,
                user_agent=pending.user_agent,
                disk=disk,
                max_bytes=mib * 1024 * 1024,
            )
        except Exception as error:
            logger.warning("Could not start the live channel (%s)", type(error).__name__)
            self._running = self._pause = False
            ticket, self._ticket = self._ticket, None
            if ticket is not None:
                self.budget.release(ticket)
            try:
                live.stop()
            except Exception:
                logger.debug("Stopping the live session after a failed start failed too")
            self.startFailed.emit(str(error))
            return
        if self._pause:
            self._pause = False
            live.toggle_pause()  # LiveSession keeps a pause for the stream that opens next

    # ---- events -------------------------------------------------------------------------------

    def _on_granted(self, ticket: int) -> None:
        if ticket == self._ticket and self._pending is not None:
            self._granted = True
            self._proceed()

    def _on_released(self) -> None:
        self._live_wait = False
        self._live_cap.stop()
        tickets, self._releasing = self._releasing, []
        for ticket in tickets:
            self.budget.release(ticket)
        self._proceed()

    def _on_live_cap(self) -> None:
        if self._live_wait:
            logger.warning("The live channel did not report its stream closed; going on.")
            self._on_released()

    def _on_input_ended(self) -> None:
        if self._input_wait:
            self._input_wait = False
            self._input_cap.stop()
            self._proceed()

    def _on_input_cap(self) -> None:
        if self._input_wait:
            logger.warning("VLC did not let go of the stream in time; going on without it.")
            self._on_input_ended()

    def _on_timeline(self, state: dict) -> None:
        if self._running:
            self.timeline = state

    # ---- controls and state -------------------------------------------------------------------

    def running(self) -> bool:
        """A pipeline tune has started (it may be opening, time-shifting or playing direct)."""
        return self._running

    def opening(self) -> bool:
        """Something will play but does not yet: it waits for a slot, a release or the pipeline."""
        if self._action is not None or self._pending is not None:
            return True
        return self._running and self.live is not None and not self.live.mode

    def pipeline(self) -> bool:
        """The running tune plays from the time-shift buffer."""
        return self._running and self.live is not None and self.live.is_pipeline()

    def toggle_pause(self) -> None:
        if self._running and self.live is not None:
            self.live.toggle_pause()  # plays on direct channels too
        elif self._pending is not None:
            self._pause = not self._pause
        else:
            self.session.toggle_pause()

    def go_live(self) -> bool:
        """Go live on the running tune; a pending one starts live and unpaused anyway."""
        if self._running and self.live is not None:
            self.live.go_live()
            return True
        if self._pending is not None:
            self._pause = False
            return True
        return False

    def set_latency(self, latency: str) -> None:
        """A new live latency reaches the running tune at once."""
        if self._running and self.live is not None and latency != self._latency:
            self._latency = latency
            self.live.set_latency(latency)
