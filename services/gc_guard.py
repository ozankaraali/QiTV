"""Python's cycle collector runs on the GUI thread only.

CPython runs its collector on whichever thread allocates past the collector's threshold. A
reference cycle that holds a QObject made on the GUI thread (a controller and the timers it
owns) is then freed on that thread, and Qt objects are deleted off their own thread. Deleting a
Python-created QObject makes Qt call back into Python (disconnectNotify, metaObject: they need
the GIL) while it holds a signal-slot mutex; a GUI thread that holds the GIL and wants the
same mutex (any connect) then waits for it forever. The live engine's supervisor thread hung
the multiview tests so under load (see ThreadCleanup for the same deadlock with a worker).

`GcGuard.start()` turns the automatic collector off and runs it from a timer on the GUI
thread instead, by the collector's own thresholds: the youngest generation once its count
passes threshold 0, an older one as its count passes its threshold, and a full collection at
most every `FULL_EVERY` seconds (the catalog is frozen out of it, see Catalogs._freeze_heap).
No other thread runs the collector after that; nothing calls `gc.collect()` off the GUI thread.

The trade-off: while the GUI thread is blocked, cycles made on worker threads wait for it. The
GUI thread never blocks on network or disk (AGENTS.md), so that wait is short; a modal dialog's
nested event loop still runs the timer.
"""

from __future__ import annotations

import gc
import logging
import time
from typing import Callable, Optional

from PySide6.QtCore import QObject, QTimer, Slot

log = logging.getLogger(__name__)

INTERVAL_MS = 500
FULL_EVERY = 300.0  # seconds: at most one full collection in this time


class GcGuard(QObject):
    """Collects garbage cycles on the thread it lives on (the GUI thread), and only there."""

    def __init__(
        self, parent: Optional[QObject] = None, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        super().__init__(parent)
        self._clock = clock
        self._last_full = clock()
        self._was_enabled = False
        self._timer = QTimer(self)
        self._timer.setInterval(INTERVAL_MS)
        self._timer.timeout.connect(self.collect)

    def start(self) -> None:
        """The automatic collector off; this thread's timer collects from now on."""
        if self._timer.isActive():
            return
        self._was_enabled = gc.isenabled()
        gc.disable()
        self._timer.start()

    def stop(self) -> None:
        """The automatic collector again, as before `start()` (tests)."""
        if not self._timer.isActive():
            return
        self._timer.stop()
        if self._was_enabled:
            gc.enable()

    @Slot()
    def collect(self) -> int:
        """The collection the automatic collector would have run by now: the generation
        collected, or -1 when none was due."""
        if self._timer.isActive() and gc.isenabled():  # turned back on meanwhile: off again
            log.warning("The automatic garbage collector was turned on again; turning it off.")
            gc.disable()
        count0, count1, count2 = gc.get_count()
        threshold0, threshold1, threshold2 = gc.get_threshold()
        if threshold0 <= 0 or count0 <= threshold0:  # threshold 0 turns collection off
            return -1
        generation = 0
        if count1 > threshold1:
            generation = 1
            now = self._clock()
            if count2 > threshold2 and now - self._last_full >= FULL_EVERY:
                generation = 2
                self._last_full = now
        gc.collect(generation)
        return generation
