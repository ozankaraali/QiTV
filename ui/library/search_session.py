"""Search everything for the Library: debounce, one search at a time, snapshot reuse.

Each query change restarts a debounce. Then a SearchWorker runs through the JobRunner. Only
one runs at a time: a query typed meanwhile waits, and only the latest one runs, with the
index the first search built. The first search loads every provider's cached catalog and folds
its titles; the caches and the folded index are kept (a snapshot) until `drop_snapshot()`.
Every search carries a generation, so `clear()` and `shutdown()` make a running one count
for nothing.
"""

from __future__ import annotations

from dataclasses import replace
import itertools
import logging
from typing import Callable, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from services.library import Details, LibraryModel
from services.provider_ids import with_provider
from services.search import MIN_CHARS, Hit, SearchWorker
from services.text_fold import fold
from services.watch_state import WatchState
from ui.library.poster_grid import poster_url

logger = logging.getLogger(__name__)

DEBOUNCE_MS = 250
SEARCH_PROMPT = "Type to search every provider's movies, series and channels"
SEARCHING_TEXT = "Searching…"
FAILED_TEXT = "Couldn't search — type to try again"
SECTION_LABELS = {"live": "Live TV", "movies": "Movies", "series": "Series"}


def searchable(text: str) -> bool:
    """True when `text` has enough letters for search_index to look (MIN_CHARS)."""
    return sum(len(word) for word in fold(text).split()) >= MIN_CHARS


class SearchSession(QObject):
    changed = Signal()  # the hits, or what to show instead of them, changed

    def __init__(
        self, provider_manager, jobs, model: LibraryModel, watch: WatchState, parent=None
    ) -> None:
        super().__init__(parent)
        self._provider_manager = provider_manager
        self._jobs = jobs
        self._model = model
        self._watch = watch
        self._closed = False
        self._query = ""
        self._generation = 0
        self._snapshot = 0
        self._caches: Optional[list] = None
        self._index: Optional[list] = None
        self._running = False
        self._queued = False  # a newer query waits for the running search
        self._hits: Optional[list[Hit]] = None  # None: nothing searched (the prompt)
        self._failed = False
        self._sections: list[tuple[str, list[dict]]] = []
        self._flat: list[Hit] = []
        self._hit_of: dict[int, Hit] = {}
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(DEBOUNCE_MS)
        self._timer.timeout.connect(self._start)

    # ---- state --------------------------------------------------------------------------------

    @property
    def query(self) -> str:
        return self._query

    @property
    def hits(self) -> Optional[list[Hit]]:
        return self._hits

    def sections(self) -> list[tuple[str, list[dict]]]:
        """The grid sections: "{provider} · {Live TV|Movies|Series} ({n})" and their items."""
        return list(self._sections)

    def hit_at(self, index: int) -> Optional[Hit]:
        return self._flat[index] if 0 <= index < len(self._flat) else None

    def hit_for(self, item: dict) -> Optional[Hit]:
        return self._hit_of.get(id(item))

    def message(self) -> Optional[str]:
        """What the grid shows instead of hits, else None."""
        if self._failed:
            return FAILED_TEXT
        if self._hits is None:
            searching = self._running and searchable(self._query)
            return SEARCHING_TEXT if searching else SEARCH_PROMPT
        if not self._hits:
            return f"Nothing matches “{self._query.strip()}”"
        return None

    def count_text(self) -> str:
        if self._hits is None or self._failed:
            return ""
        n = len(self._flat)
        return "No results" if not n else f"{n:,} result" + ("" if n == 1 else "s")

    # ---- queries ------------------------------------------------------------------------------

    def set_query(self, text: str) -> None:
        self._query = text
        self._timer.start()  # restarts the debounce

    def run_now(self) -> None:
        """Run a debounced query at once."""
        if self._timer.isActive():
            self._timer.stop()
            self._start()

    def rerun(self) -> None:
        """Search the current query again (after `drop_snapshot`)."""
        self._timer.stop()
        if searchable(self._query):
            self._start()

    def clear(self) -> None:
        self._timer.stop()
        self._generation += 1  # a running search no longer counts
        self._queued = False
        self._query = ""
        self._set_hits(None)

    def drop_snapshot(self) -> None:
        """The next search reads the cached catalogs and folds the titles again."""
        self._snapshot += 1
        self._caches = self._index = None

    def shutdown(self) -> None:
        self._closed = True
        self._timer.stop()
        self._generation += 1
        self._queued = False

    # ---- running ------------------------------------------------------------------------------

    def _start(self) -> None:
        if self._closed:
            return
        if self._running:
            self._queued = True  # only the latest query runs next
            return
        self._generation += 1
        self._queued = False
        text = self._query
        if not searchable(text):
            self._set_hits(None)
            return
        self._running = True
        if self._hits is None:
            self.changed.emit()  # "Searching…"
        worker = SearchWorker(self._provider_manager, text, caches=self._caches, index=self._index)
        self._jobs.run_worker(
            worker, self._on_done, self._on_error, context=(self._generation, self._snapshot)
        )

    def _on_done(self, context: tuple[int, int], payload) -> None:
        generation, snapshot = context
        self._running = False
        caches, index, hits = payload
        if snapshot == self._snapshot and self._index is None:
            self._caches, self._index = caches, index  # titles are folded once per snapshot
        if self._closed:
            return
        if self._queued:
            self._queued = False
            self._start()  # a newer query: these hits would only flash
        elif generation == self._generation:
            self._set_hits(list(hits))

    def _on_error(self, context: tuple[int, int], message: str) -> None:
        generation, _snapshot = context
        self._running = False
        logger.warning("Search failed (%s)", message)
        if self._closed:
            return
        if self._queued:
            self._queued = False
            self._start()
        elif generation == self._generation:
            self._set_hits([], failed=True)

    def _set_hits(self, hits: Optional[list[Hit]], *, failed: bool = False) -> None:
        if hits:  # a hit's title is its provider's, wherever it is played from
            hits = [replace(h, item=with_provider(h.item, h.provider)) for h in hits]
        self._hits = hits
        self._failed = failed
        sections: list[tuple[str, list[dict]]] = []
        flat: list[Hit] = []
        for (provider, section), group in itertools.groupby(
            hits or [], key=lambda h: (h.provider, h.section)
        ):
            found = list(group)
            label = SECTION_LABELS.get(section, section)
            sections.append((f"{provider} · {label} ({len(found):,})", [h.item for h in found]))
            flat.extend(found)
        self._sections = sections
        self._flat = flat
        self._hit_of = {id(h.item): h for h in flat}
        self.changed.emit()

    # ---- painting helpers ---------------------------------------------------------------------

    def poster_url(self, item: dict, logo_key: Callable[[str], str]) -> str:
        """A live hit's channel logo (as `logo_key` names it for the grid), else its poster."""
        hit = self.hit_for(item)
        if hit is not None and hit.section == "live":
            logo = str(item.get("logo") or "")
            return logo_key(logo) if logo else ""
        return poster_url(item)

    def is_channel(self, item: dict) -> bool:
        hit = self.hit_for(item)
        return hit is not None and hit.section == "live"

    def details(self, item: dict) -> Details:
        """The strip of a hit: the model's parsing, with the hit's own kind, genre and resume."""
        hit = self.hit_for(item)
        section = hit.section if hit is not None else "movies"
        resume = None
        if hit is not None and section == "movies":
            cid = WatchState.content_id(hit.provider, item, item_type="movie")
            resume = self._watch.resume_position(cid)
        return replace(
            self._model.details(item),
            genre=str(item.get("genre") or item.get("genres_str") or ""),
            kind="series" if section == "series" else "movie",
            resume_ms=resume,
            progress=None,
        )
