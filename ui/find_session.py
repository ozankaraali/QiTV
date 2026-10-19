"""The channel list's find: genres and channels at every provider (see services.find).

The first find makes the index in a worker; it is kept until the catalogs' content changes
(`signature`), then made again at the next find. With an index, each letter typed shows the
matching genres at once (matched here) and narrows the channels already found; the channels
for the new text follow from a worker after a short pause. One worker runs at a time: a text
typed meanwhile waits, and only the latest one runs.
"""

from __future__ import annotations

from dataclasses import replace
import logging
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from services.find import FindIndex, FindWorker, FoundGenre, find_genres
from services.provider_ids import with_provider
from services.search import Hit
from services.text_fold import fold

logger = logging.getLogger(__name__)

DEBOUNCE_MS = 150


def _matches(title: str, words: list[str]) -> bool:
    folded = fold(title)
    return all(w in folded for w in words)


class FindSession(QObject):
    changed = Signal()  # the genres, the channels or `searching` changed

    def __init__(
        self,
        provider_manager,
        jobs,
        *,
        loaded: Optional[Callable[[], dict]] = None,
        signature: Optional[Callable[[], Any]] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._provider_manager = provider_manager
        self._jobs = jobs
        self._loaded = loaded  # the content the catalogs hold: found without a re-read
        self._signature = signature  # changes when the catalogs' content may have
        self._query = ""
        self._index: Optional[FindIndex] = None
        self._made_for: Any = None  # the signature the index was made for
        self._snapshot = 0
        self._generation = 0
        self._running = False
        self._queued = False
        self._closed = False
        self.genres: list[FoundGenre] = []
        self.channels: list[Hit] = []  # each item stamped with its provider
        self.searching = False  # the channels for the text are on their way
        self.failed = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(DEBOUNCE_MS)
        self._timer.timeout.connect(self._start)

    @property
    def query(self) -> str:
        return self._query

    def set_query(self, text: str) -> None:
        if not text.strip():
            self.clear()
            return
        self._query = text
        if self._index is not None and self._signature is not None:
            if self._signature() != self._made_for:
                self.drop_snapshot()
        words = fold(text).split()
        if self._index is not None:
            self.genres = find_genres(self._index, text)
        else:  # the index is on its way: what was found narrows meanwhile
            self.genres = [g for g in self.genres if _matches(g.title, words)]
        self.channels = [h for h in self.channels if _matches(h.title, words)]
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
        self.changed.emit()

    def drop_snapshot(self) -> None:
        """The next find reads the catalogs and folds the titles again."""
        self._snapshot += 1
        self._index = None

    def shutdown(self) -> None:
        self._closed = True
        self._timer.stop()
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
        worker = FindWorker(self._provider_manager, self._query, index=self._index, loaded=loaded)
        context = (self._generation, self._snapshot, signature, self._query)
        self._jobs.run_worker(worker, self._on_done, self._on_error, context=context)

    def _on_done(self, context: tuple, payload) -> None:
        generation, snapshot, signature, text = context
        self._running = False
        index, hits = payload
        if snapshot == self._snapshot and self._index is None:
            self._index, self._made_for = index, signature  # titles fold once per snapshot
        if self._closed:
            return
        if self._queued:
            self._queued = False
            self._start()  # a newer text: these channels would only flash
        elif generation == self._generation and text == self._query:
            self.genres = find_genres(index, text)
            self.channels = [replace(h, item=with_provider(h.item, h.provider)) for h in hits]
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
