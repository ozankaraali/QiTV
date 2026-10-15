"""Opens the TV shell's provider streams one at a time (spec §6.9).

Before anything opens, the stream before it is released: providers count connections, and
many allow one or two. "Released" means:
- the time-shift pipeline's `LiveSession.released()`: its upstream is closed and, after
  direct play, libVLC let go of the provider stream (LiveSession bounds both waits);
- the player's own direct play (a movie, an episode, a channel the pipeline cannot read):
  `PlaybackSession.inputEnded`, since libVLC ends its input on a worker after `stop()`.
Each wait is capped (RELEASE_CAP_MS; the LiveSession one by `BudgetedStream`, a little
later, as a backstop to its own caps). Only the newest open waits: a newer one replaces it.

A pipeline tune also takes a slot of the provider's connection budget (purpose "main") and
starts once the slot is granted and the old stream is released; the old slot goes back on
that release. A `BudgetedStream` holds that slot and waits for the release (the PiP slot's
`SessionManager` uses one too). With a limit of one, the new request waits at the head of
the queue, so the freed slot goes to it and to no other consumer. A pause or Go live pressed
during that wait applies once the tune starts.

A direct play (`open(direct=True)`: a channel the pipeline cannot read, a movie, an
episode) holds a "main" slot too (a `HeldSlot`, the selected provider's), from its start
until libVLC let go of its stream, so a PiP sees the slot taken. It never waits for one:
this gate already orders the main player's streams.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from services.budgeted_stream import BudgetedStream, HeldSlot
from services.connection_budget import ConnectionBudget
from services.live.live_session import LiveSession
from services.playback import Session

logger = logging.getLogger(__name__)

RELEASE_CAP_MS = 2000  # the longest wait for libVLC to let go of a direct stream
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
        self.config = config  # live_latency, timeshift_*, selected_provider_name (its slots)
        self.live = live
        self.budget = budget if budget is not None else ConnectionBudget(self)
        self.timeline: Optional[dict] = None  # the running tune's last timelineChanged state
        self._pending: Optional[LiveStart] = None  # a tune waiting for its slot or the release
        # The pipeline tune's budget slot, its start and the wait for its release.
        self._stream: Optional[BudgetedStream] = None
        self._latency: Optional[str] = None  # what the running tune started with
        self._pause = False  # pressed while the pending tune waited: applied at its start
        self._input_wait = False  # a direct play was stopped; inputEnded has not come
        self._direct = False  # the player plays a provider stream an open() asked for
        self._held = HeldSlot(self.budget, PURPOSE)  # that stream's slot, until VLC let go
        self._action: Optional[Callable[[], None]] = None  # an open waiting for the release
        self._action_direct = False
        self._input_cap = QTimer(self, singleShot=True, interval=RELEASE_CAP_MS)
        self._input_cap.timeout.connect(self._on_input_cap)
        session.inputEnded.connect(self._on_input_ended)
        if live is not None:
            self._stream = BudgetedStream(live, self.budget, PURPOSE, parent=self)
            self._stream.granted.connect(self._on_granted)
            self._stream.released.connect(self._on_released)
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
        assert self._stream is not None
        self.stop()
        self._pending = start
        self._stream.request(provider, limit)

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
        self._pause = False
        self.timeline = None
        if self._stream is not None:
            self._stream.stop()  # released() follows, at once when nothing is left open
        if self._direct:
            self._direct = False
            self.session.stop()
            if self.session.holds_input():
                self._input_wait = True
                self._input_cap.start()  # the slot goes back with the input
            else:
                self._held.give_back()

    def shutdown(self) -> None:
        direct = self._direct
        self.stop()
        if not direct:
            self.session.stop()  # whatever else the player holds

    def _proceed(self) -> None:
        stream = self._stream
        if self._input_wait or (stream is not None and stream.waiting()):
            return
        if self._action is not None:
            action, self._action = self._action, None
            self._direct = self._action_direct
            if self._direct:
                self._held.take(str(getattr(self.config, "selected_provider_name", "") or ""))
            action()
        elif self._pending is not None and stream is not None and stream.is_granted():
            self._start()

    def _start(self) -> None:
        pending, stream = self._pending, self._stream
        if pending is None or stream is None:
            return
        self._pending = None
        config = self.config
        disk = bool(config.timeshift_enabled)
        mib = config.timeshift_max_mib if disk else RAM_RING_MIB
        self._latency = config.live_latency
        try:
            stream.start(
                pending.url,
                headers={"User-Agent": pending.user_agent},
                verify_ssl=pending.verify_ssl,
                latency=config.live_latency,
                user_agent=pending.user_agent,
                disk=disk,
                max_bytes=mib * 1024 * 1024,
            )
        except Exception as error:  # the stream gave its slot back and stopped the session
            logger.warning("Could not start the live channel (%s)", type(error).__name__)
            self._pause = False
            self.startFailed.emit(str(error))
            return
        if self._pause:
            self._pause = False
            stream.live.toggle_pause()  # LiveSession keeps a pause for the stream that opens next

    # ---- events -------------------------------------------------------------------------------

    def _on_granted(self) -> None:
        if self._pending is not None:
            self._proceed()

    def _on_released(self) -> None:
        self._proceed()

    def _on_input_ended(self) -> None:
        if self._input_wait:
            self._input_wait = False
            self._input_cap.stop()
            self._held.give_back()
            self._proceed()

    def _on_input_cap(self) -> None:
        if self._input_wait:
            logger.warning("VLC did not let go of the stream in time; going on without it.")
            self._on_input_ended()

    def _on_timeline(self, state: dict) -> None:
        if self.running():
            self.timeline = state

    # ---- controls and state -------------------------------------------------------------------

    def running(self) -> bool:
        """A pipeline tune has started (it may be opening, time-shifting or playing direct)."""
        return self._stream is not None and self._stream.running()

    def opening(self) -> bool:
        """Something will play but does not yet: it waits for a slot, a release or the pipeline."""
        if self._action is not None or self._pending is not None:
            return True
        return self.running() and self.live is not None and not self.live.mode

    def pipeline(self) -> bool:
        """The running tune plays from the time-shift buffer."""
        return self.running() and self.live is not None and self.live.is_pipeline()

    def toggle_pause(self) -> None:
        if self.running() and self.live is not None:
            self.live.toggle_pause()  # plays on direct channels too
        elif self._pending is not None:
            self._pause = not self._pause
        else:
            self.session.toggle_pause()

    def go_live(self) -> bool:
        """Go live on the running tune; a pending one starts live and unpaused anyway."""
        if self.running() and self.live is not None:
            self.live.go_live()
            return True
        if self._pending is not None:
            self._pause = False
            return True
        return False

    def set_latency(self, latency: str) -> None:
        """A new live latency reaches the running tune at once."""
        if self.running() and self.live is not None and latency != self._latency:
            self._latency = latency
            self.live.set_latency(latency)
