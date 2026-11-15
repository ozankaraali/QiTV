"""A live channel that is still watched comes back by itself once its stream does.

A TV keeps the channel on: when the picture goes, it shows that it is trying, and the picture
comes back with the signal. QiTV does the same for a live channel whose stream ended or failed
after it played: it opens the stream again after a growing wait, `REVIVE_S` (about 1 s, 3 s,
10 s, 30 s, 60 s and 120 s, then every 5 minutes), with no last try while the channel stays
chosen and QiTV open. A stream that plays `HEALTHY_S` again starts the waits anew. Each open
goes in the same player, and so in its budget slot: the provider never sees a second stream
(`services.connection_budget`). A refusal for the provider's limit is never asked again.

`Revive` counts one stream's tries; the time-shift helper's source (`services.live.source`)
waits the same schedule's longer steps once its own quick reconnects are spent.
"""

from __future__ import annotations

from collections.abc import Callable
import time
from typing import Optional

REVIVE_S = (1.0, 3.0, 10.0, 30.0, 60.0, 120.0, 300.0)  # the waits; then the last, each time
HEALTHY_S = 60.0  # a stream that played this long since it last opened starts the waits anew


def revive_wait(tries: int) -> float:
    """The wait before try `tries` + 1 (0: the first): the last of `REVIVE_S` from there on."""
    return REVIVE_S[min(max(0, tries), len(REVIVE_S) - 1)]


def fmt_wait(seconds: float) -> str:
    """A wait as a countdown shows it: "0:25", "2:00", "5:00" (whole seconds, rounded up)."""
    whole = max(0, int(-(-max(0.0, seconds) // 1)))
    return f"{whole // 60}:{whole % 60:02d}"


class Revive:
    """One live stream's tries to open again since it last played `HEALTHY_S` (`REVIVE_S`).

    `playing()` as it plays, `stopped()` as it ends or fails (a stream that played long enough
    since it last started playing has its waits back), then `next_wait()` for the wait before
    the next open, which counts it.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self.tries = 0
        self._playing_since: Optional[float] = None

    def playing(self) -> None:
        self._playing_since = self._clock()

    def played_well(self) -> bool:
        """It has played `HEALTHY_S` since it last started playing."""
        since = self._playing_since
        return since is not None and self._clock() - since >= HEALTHY_S

    def stopped(self) -> None:
        if self.played_well():
            self.tries = 0
        self._playing_since = None

    def next_wait(self) -> float:
        wait = revive_wait(self.tries)
        self.tries += 1
        return wait

    def reset(self) -> None:
        self.tries = 0
        self._playing_since = None
