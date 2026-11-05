"""The Recordings tab's find in the guide (2.9.7): programmes still to come whose title holds
what was typed, to record.

The sources are lists of channels, the nearest first (the home guide's channels, then the
provider's, then every provider's: the owner's rule, a find widens); a channel already looked
at is not looked at again, and a programme is found once. The guide is read on the GUI thread
as the guide overlay reads it, `SLICE` channels a turn of the event loop, so nothing waits; a
new query (or `cancel`) drops the one under way. What a slice finds comes as `found`, the end
as `finished`.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from services.epg_view import Programme

logger = logging.getLogger(__name__)

SLICE = 40  # channels looked at per turn
LIMIT = 48  # programmes asked per channel (about a day of them)
MOST = 200  # programmes found at most


def fold(text: str) -> str:
    return " ".join(text.casefold().split())


class GuideSearch(QObject):
    found = Signal(list)  # [(channel, Programme)], a slice's
    finished = Signal()

    def __init__(
        self,
        programmes: Callable[[dict, datetime, int], list[Programme]],
        now: Callable[[], datetime],
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._programmes = programmes
        self._now = now
        self._queue: list[dict] = []
        self._query = ""
        self._seen_channels: set[str] = set()
        self._seen: set[tuple[str, float]] = set()
        self._count = 0
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(0)
        self._timer.timeout.connect(self._step)

    def start(self, query: str, sources: list[list[dict]], days: int = 7) -> None:
        self.cancel()
        self._query = fold(query)
        self._horizon = self._now() + timedelta(days=days)
        self._queue = []
        self._seen_channels = set()
        for source in sources:
            for channel in source:
                key = _key(channel)
                if key not in self._seen_channels:
                    self._seen_channels.add(key)
                    self._queue.append(channel)
        self._seen = set()
        self._count = 0
        if not self._query:
            return
        self._timer.start()  # its first slice on the next turn: a newer query replaces it

    def cancel(self) -> None:
        self._timer.stop()
        self._queue = []

    def running(self) -> bool:
        return bool(self._queue) or self._timer.isActive()

    def _step(self) -> None:
        now = self._now()
        batch, self._queue = self._queue[:SLICE], self._queue[SLICE:]
        hits: list[tuple[dict, Programme]] = []
        for channel in batch:
            try:
                programmes = self._programmes(channel, now, LIMIT)
            except Exception as e:  # a channel whose guide fails: nothing found there
                logger.debug("A channel's guide could not be read: %s", type(e).__name__)
                continue
            for programme in programmes:
                if programme.stop <= now or programme.start > self._horizon:
                    continue
                if self._query not in fold(programme.title):
                    continue
                key = (_key(channel), programme.start.timestamp())
                if key in self._seen:
                    continue
                self._seen.add(key)
                hits.append((channel, programme))
        if hits:
            room = MOST - self._count
            hits = hits[:room]
            self._count += len(hits)
            self.found.emit(hits)
        if self._queue and self._count < MOST:
            self._timer.start()
        else:
            self._queue = []
            self.finished.emit()


def _key(channel: dict[str, Any]) -> str:
    return f"{channel.get('_provider') or ''}|{channel.get('id') or channel.get('name') or ''}"
