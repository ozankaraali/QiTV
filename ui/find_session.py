"""The channel list's find: genres and channels at every provider (see services.find).

The first find makes the index in a worker. It holds the catalogs' own channels, so it goes
when their content changes (`content_changed`, the hub's signals; a find on screen then runs
again) and when the find has been idle for IDLE_DROP_MS; the next find makes it again. Each
letter typed narrows what was found at
once, here; the genres and channels for the new text, ranked, follow from a worker after a
short pause (`preferences`, read here, rank the user's own first). One worker runs at a time:
a text typed meanwhile waits, and only the latest one runs.
"""

from __future__ import annotations

from dataclasses import replace
import logging
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from services import parental
from services.find import (
    FindIndex,
    FindWorker,
    FoundChannel,
    FoundGenre,
    Preferences,
    matches,
    words,
)
from services.provider_ids import with_provider

logger = logging.getLogger(__name__)

DEBOUNCE_MS = 150
IDLE_DROP_MS = 5 * 60 * 1000  # an ended find's index (200,000 channels: ~100 MB) goes then


class FindSession(QObject):
    changed = Signal()  # the genres, the channels or `searching` changed

    def __init__(
        self,
        provider_manager,
        jobs,
        *,
        loaded: Optional[Callable[[], dict]] = None,
        signature: Optional[Callable[[], Any]] = None,
        preferences: Optional[Callable[[], Preferences]] = None,
        idle_ms: int = IDLE_DROP_MS,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._provider_manager = provider_manager
        self._jobs = jobs
        self._loaded = loaded  # the content the catalogs hold: found without a re-read
        self._signature = signature  # changes when the catalogs' content may have
        self._preferences = preferences  # the browsed provider, what was watched, the stars
        self._query = ""
        self._index: Optional[FindIndex] = None
        self._made_for: Any = None  # the signature the index was made for
        self._snapshot = 0
        self._generation = 0
        self._running = False
        self._queued = False
        self._closed = False
        self.genres: list[FoundGenre] = []
        self.channels: list[FoundChannel] = []  # each item stamped with its provider
        self.searching = False  # the genres and channels for the text are on their way
        self.failed = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(DEBOUNCE_MS)
        self._timer.timeout.connect(self._start)
        self._idle = QTimer(self)
        self._idle.setSingleShot(True)
        self._idle.setInterval(idle_ms)
        self._idle.timeout.connect(self._on_idle)

    @property
    def query(self) -> str:
        return self._query

    def set_query(self, text: str) -> None:
        if not text.strip():
            self.clear()
            return
        self._query = text
        self._idle.stop()
        if self._index is not None and self._signature is not None:
            if self._signature() != self._made_for:
                self.drop_snapshot()
        typed = words(text)  # what was found narrows at once; the worker ranks anew
        self.genres = [g for g in self.genres if matches(g.title, typed)]
        self.channels = [h for h in self.channels if matches(h.title, typed)]
        self.searching = True
        self.failed = False
        self._timer.start()  # restarts the pause
        self.changed.emit()

    def clear(self) -> None:
        self._timer.stop()
        self._generation += 1  # a running find no longer counts
        self._queued = False
        self._query = ""
        self.genres, self.channels = [], []
        self.searching = self.failed = False
        self._idle.start()  # an index unused for a while goes
        self.changed.emit()

    def drop_snapshot(self) -> None:
        """The next find reads the catalogs and folds the titles again."""
        self._snapshot += 1
        self._index = None

    def content_changed(self, _provider: str = "") -> None:
        """A catalog's content or the providers changed (the hub's contentChanged and
        providersChanged): an index made before goes, and a find on screen runs again."""
        if self._index is None and not self._running:
            return  # none made, none on its way: the next find reads the content as it is
        if self._index is not None and self._signature is not None:
            if self._signature() == self._made_for:
                return  # (a section reported ready again, unchanged)
        self.refresh()  # (one being made from the old content is never kept)

    def refresh(self) -> None:
        """Find again from the catalogs as they are now (what is shown stays meanwhile)."""
        self.drop_snapshot()
        if self._query.strip():
            self._timer.start()

    def group_of(self, item: dict) -> str:
        """The genre a found channel (as `channels` holds it) is first listed in, or ""."""
        return next((c.group_id for c in self.channels if c.item is item), "")

    def _on_idle(self) -> None:
        if not self._query.strip():
            self.drop_snapshot()

    def shutdown(self) -> None:
        self._closed = True
        self._timer.stop()
        self._idle.stop()
        self._generation += 1
        self._queued = False

    def _start(self) -> None:
        if self._closed or not self._query.strip():
            return
        if self._running:
            self._queued = True  # only the latest text runs next
            return
        self._generation += 1
        self._queued = False
        self._running = True
        loaded = signature = None
        if self._index is None:
            loaded = self._loaded() if self._loaded is not None else None
            signature = self._signature() if self._signature is not None else None
        preferences = self._preferences() if self._preferences is not None else Preferences()
        worker = FindWorker(
            self._provider_manager,
            self._query,
            index=self._index,
            loaded=loaded,
            preferences=preferences,
        )
        context = (self._generation, self._snapshot, signature, self._query)
        self._jobs.run_worker(worker, self._on_done, self._on_error, context=context)

    def _on_done(self, context: tuple, payload) -> None:
        generation, snapshot, signature, text = context
        self._running = False
        index, genres, hits = payload
        if snapshot == self._snapshot and self._index is None:
            self._index, self._made_for = index, signature  # titles fold once per snapshot
        if self._closed:
            return
        if self._queued:
            self._queued = False
            self._start()  # a newer text: these would only flash
        elif generation == self._generation and text == self._query:
            guard = parental.installed()  # 2.8: not what parental controls hide now
            if guard is not None and guard.active():
                genres = [
                    g for g in genres if not guard.hides_group("live", g.provider, str(g.group_id))
                ]
            self.genres = list(genres)
            stamped = [replace(h, item=with_provider(h.item, h.provider)) for h in hits]
            self.channels = parental.visible_hits(stamped)
            self.searching = False
            self.changed.emit()

    def _on_error(self, context: tuple, message: str) -> None:
        generation, _snapshot, _signature, text = context
        self._running = False
        logger.warning("Find failed (%s)", message)
        if self._closed:
            return
        if self._queued:
            self._queued = False
            self._start()
        elif generation == self._generation and text == self._query:
            self.searching = False
            self.failed = True
            self.changed.emit()
