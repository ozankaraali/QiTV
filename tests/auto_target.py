"""2.0.2's Auto latency, the reference the learned target's tests measure it by (a helper, not
a test module). Since 2.1 stage 4b the session steers Auto by the learned target
(`services.live.learned_target`)."""

from __future__ import annotations

import math
from typing import Optional

from services.live.stay_live import _AUTO_DOWN, _AUTO_QUIET_S, TARGETS

AUTO_UP = 1.5  # a starvation or a stall raised the channel's target this much
AUTO_MAX_S = 60.0  # a remembered extra beyond this is not one Auto set: start afresh


class AutoTarget:
    """One channel's Auto latency as 2.0.2 had it: what it adds to the target in force.

    The target in force is Normal's, or the source's burst when that is larger, as under any
    latency; Auto adds `extra` on top: 0 at first, or where the channel's last tune left it.
    Each starvation of VLC at the live edge, and each stall of the source, adds `AUTO_UP`,
    so the target in force moves at once (on long HLS segments too), up to max(Stable's +
    4 s, burst + 2 s) in all; every `_AUTO_QUIET_S` of playing without one takes
    `_AUTO_DOWN` off, down to 0. The caller says what counts.
    """

    def __init__(self, extra: Optional[float] = None) -> None:
        self.extra = 0.0
        if extra is not None and math.isfinite(extra) and 0.0 < extra <= AUTO_MAX_S:
            self.extra = extra
        self._quiet = 0.0

    @staticmethod
    def _base(burst: float) -> float:
        return max(TARGETS["normal"], burst)

    @staticmethod
    def _cap(burst: float) -> float:
        return max(TARGETS["stable"] + 4.0, burst + 2.0)

    def target(self, burst: float) -> float:
        """The target in force for a source with this burst (0.0: steady), with the extra."""
        base = self._base(burst)
        return max(base, min(base + self.extra, self._cap(burst)))

    @property
    def adjusted(self) -> bool:
        """The channel needed more than the target in force."""
        return self.extra > 0.0

    def trouble(self, burst: float) -> bool:
        """A starvation or a stall: 1.5 s more, up to the cap; True when the extra changed."""
        self._quiet = 0.0
        room = self._cap(burst) - self._base(burst)  # the most Auto may add on this source
        raised = max(self.extra, min(self.extra + AUTO_UP, room))  # never lowered here
        changed, self.extra = raised != self.extra, raised
        return changed

    def quiet(self, seconds: float) -> bool:
        """`seconds` more of playing without trouble; True when the extra was lowered."""
        self._quiet += seconds
        if self._quiet < _AUTO_QUIET_S - 1e-9:
            return False
        self._quiet = 0.0
        lowered = max(0.0, self.extra - _AUTO_DOWN)
        changed, self.extra = lowered != self.extra, lowered
        return changed
