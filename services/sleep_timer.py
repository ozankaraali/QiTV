"""The sleep timer (2.3): playback stops after a while the viewer chose (no widgets).

`start(seconds)` counts down on a monotonic clock, checked each `tick_ms` on the GUI thread:
`changed` each tick and whenever it starts or stops (the Picture panel shows the time left),
`warning` once when `WARN_S` or less are left (a start with less warns at its first tick), then
`expired` at the end, when it stops by itself. `cancel()` stops it, unsaid. It is never kept
across a restart. What expiry does (stop playback, let the computer sleep) is the shell's.
"""

from __future__ import annotations

import time
from typing import Callable, Optional

from PySide6.QtCore import QObject, QTimer, Signal

WARN_S = 60.0  # "Playback stops in 1:00": a minute to keep watching
PRESETS_MIN = (15, 30, 60, 90)  # the Picture panel's choices, beside Off and the programme's end


class SleepTimer(QObject):
    changed = Signal()
    warning = Signal()
    expired = Signal()

    def __init__(
        self,
        parent: Optional[QObject] = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        tick_ms: int = 1000,
        warn_s: float = WARN_S,
    ) -> None:
        super().__init__(parent)
        self._clock = clock
        self._warn_s = warn_s
        self._deadline: Optional[float] = None
        self._warned = False
        self._tick = QTimer(self, interval=tick_ms)
        self._tick.timeout.connect(self._on_tick)

    def start(self, seconds: float) -> None:
        """Stop playback `seconds` from now (a timer running already starts again)."""
        self._deadline = self._clock() + max(0.0, float(seconds))
        self._warned = False
        self._tick.start()
        self.changed.emit()

    def cancel(self) -> None:
        if self._deadline is None:
            return
        self._stop()
        self.changed.emit()

    def running(self) -> bool:
        return self._deadline is not None

    def warned(self) -> bool:
        """The warning was given for this countdown (and it still runs)."""
        return self._warned and self._deadline is not None

    def remaining(self) -> float:
        """Seconds left (0.0 when it does not run)."""
        if self._deadline is None:
            return 0.0
        return max(0.0, self._deadline - self._clock())

    def _stop(self) -> None:
        self._deadline = None
        self._warned = False
        self._tick.stop()

    def _on_tick(self) -> None:
        if self._deadline is None:
            self._tick.stop()
            return
        left = self.remaining()
        if left <= 0.0:
            self._stop()
            self.changed.emit()
            self.expired.emit()
            return
        if not self._warned and left <= self._warn_s:
            self._warned = True
            self.warning.emit()
        self.changed.emit()


def clock_text(seconds: float) -> str:
    """ "28:41", or from an hour "1:28:41" (rounded up: a countdown never shows 0:00 early)."""
    total = max(0, int(-(-seconds // 1)))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"
