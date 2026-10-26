"""TMDB in the Library: looks up the title in focus and the posterless ones on screen, and
keeps what TMDB said for this run (services/tmdb.py matches titles and keeps the disk cache).

The title in focus is asked for once it has kept the focus a moment (FOCUS_DELAY_MS), ahead
of the others; the posterless titles on screen as soon as the view settles, BATCH at a time,
one batch in flight. A new screen's titles replace the ones still waiting. Nothing is asked
while TMDB is off in Settings or without a token; a token TMDB refused is not used again, a
"slow down" pauses every lookup for as long as TMDB asks, and a title that could not be asked
(no connection) waits RETRY_S before another try. The lookups run on the job runner; the GUI
only reads what came back.
"""

from __future__ import annotations

from collections import OrderedDict
import logging
import time
from typing import Any, Callable, Iterable, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from services import i18n
from services.tmdb import (
    MOVIE,
    TV,
    Info,
    Looked,
    LookupWorker,
    TmdbCache,
    TmdbClient,
    Wanted,
    api_token,
    fill_gaps,
    tmdb_language,
    wanted_for,
)

logger = logging.getLogger(__name__)

FOCUS_DELAY_MS = 400
BATCH = 4
RETRY_S = 5 * 60
MEMO_SIZE = 4096
KINDS = {"movie": MOVIE, "series": TV}  # the Library's kinds TMDB knows


class TmdbFill(QObject):
    """`changed` says TMDB told something new: the strip and the posters repaint."""

    changed = Signal()

    def __init__(
        self,
        *,
        config: Any,
        jobs: Any,
        cache_path: str,
        language: Optional[str] = None,
        http: Any = None,
        now: Callable[[], float] = time.monotonic,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._jobs = jobs
        self._cache = TmdbCache(cache_path)
        self._language = tmdb_language(language if language is not None else i18n.language())
        self._http = http
        self._now = now
        self._known: dict[str, Optional[Info]] = {}
        self._failed: dict[str, float] = {}  # key -> when it could not be asked
        self._asking: set[str] = set()  # keys in the batch in flight
        self._focus: Optional[Wanted] = None
        self._waiting: list[Wanted] = []  # the posterless titles on screen, in their order
        self._running = False
        self._paused_until = 0.0
        self._refused = ""  # the token TMDB refused
        self._closed = False
        self._memo: OrderedDict[int, tuple[dict, str, Optional[Wanted]]] = OrderedDict()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(FOCUS_DELAY_MS)
        self._timer.timeout.connect(self.run_now)

    # ---- what the Library reads -----------------------------------------------------------

    def enabled(self) -> bool:
        """TMDB is on in Settings and there is a token it hasn't refused."""
        token = self._token()
        return (
            bool(getattr(self._config, "tmdb_details", False))
            and bool(token)
            and (token != self._refused)
        )

    def info(self, item: dict, kind: str) -> Optional[Info]:
        """What TMDB said of a title of the Library's `kind` ("movie", "series"), if known."""
        if not self.enabled():
            return None
        wanted = self._wanted(item, kind)
        return self._known.get(wanted.key()) if wanted is not None else None

    def fill(self, item: dict, kind: str) -> dict:
        """`item` with TMDB's details in its gaps (the item itself when TMDB knows nothing)."""
        return fill_gaps(item, self.info(item, kind))

    def poster_url(self, item: dict, kind: str) -> str:
        """TMDB's poster of a title ("" when unknown)."""
        info = self.info(item, kind)
        return info.poster_url() if info is not None else ""

    # ---- what the Library asks for --------------------------------------------------------

    def focus(self, item: Optional[dict], kind: str) -> None:
        """The title in focus: asked for once it keeps the focus a moment."""
        wanted = self._wanted(item, kind) if item is not None else None
        if self._closed or wanted is None or not self._should_ask(wanted):
            self._focus = None
            return
        self._focus = wanted
        self._timer.start()

    def want_posters(self, titles: Iterable[tuple[dict, str]]) -> None:
        """The posterless titles on screen, (item, kind) each: they replace the ones still
        waiting, and are asked for now."""
        if self._closed:
            return
        waiting: dict[str, Wanted] = {}
        for item, kind in titles:
            wanted = self._wanted(item, kind)
            if wanted is not None and self._should_ask(wanted):
                waiting.setdefault(wanted.key(), wanted)
        self._waiting = list(waiting.values())
        self.run_now()

    def run_now(self) -> None:
        """Start the next batch, unless one is in flight (it starts the next when done)."""
        self._timer.stop()
        if self._closed or self._running or not self.enabled():
            return
        wait = self._paused_until - self._now()
        if wait > 0:
            self._timer.start(int(wait * 1000) + 1)
            return
        batch: list[Wanted] = []
        candidates = ([self._focus] if self._focus is not None else []) + self._waiting
        self._focus = None
        for wanted in candidates:
            if len(batch) == BATCH:
                break
            if self._should_ask(wanted) and all(w.key() != wanted.key() for w in batch):
                batch.append(wanted)
        taken = {w.key() for w in batch}
        self._waiting = [w for w in self._waiting if w.key() not in taken and self._should_ask(w)]
        if not batch:
            return
        self._running = True
        self._asking = taken
        client = TmdbClient(self._token(), self._language, http=self._http)
        self._jobs.run_worker(
            LookupWorker(client, self._cache, batch), self._on_looked, self._on_failed, batch
        )

    def shutdown(self) -> None:
        """The window is closing: nothing more is asked, and late answers are dropped."""
        self._closed = True
        self._timer.stop()
        self._waiting.clear()
        self._focus = None

    # ---- answers --------------------------------------------------------------------------

    def _on_looked(self, batch: list[Wanted], looked: object) -> None:
        self._running = False
        self._asking = set()
        if self._closed or not isinstance(looked, Looked):
            return
        now = self._now()
        if looked.bad_token:
            self._refused = self._token()
            logger.warning("TMDB refused the token: its details stay off until it changes")
        elif looked.busy_s:
            self._paused_until = now + looked.busy_s
            logger.info("TMDB asked to slow down: pausing for %.0f s", looked.busy_s)
        else:
            for wanted in batch:
                if wanted.key() not in looked.found:
                    self._failed[wanted.key()] = now
        self._known.update(looked.found)
        if looked.found:
            self.changed.emit()
        self.run_now()

    def _on_failed(self, batch: list[Wanted], _message: str) -> None:
        self._running = False
        self._asking = set()
        now = self._now()
        for wanted in batch:
            self._failed[wanted.key()] = now  # not straight back: the worker itself failed

    # ---- helpers --------------------------------------------------------------------------

    def _token(self) -> str:
        return api_token(str(getattr(self._config, "tmdb_token", "") or ""))

    def _should_ask(self, wanted: Wanted) -> bool:
        key = wanted.key()
        if key in self._known or key in self._asking:
            return False
        failed = self._failed.get(key)
        return failed is None or self._now() - failed > RETRY_S

    def _wanted(self, item: Any, kind: str) -> Optional[Wanted]:
        """What to look up for `item` (memoised by the dict: the grid asks on every paint)."""
        if not isinstance(item, dict):
            return None
        memo = self._memo.get(id(item))
        if memo is not None and memo[0] is item and memo[1] == kind:
            self._memo.move_to_end(id(item))
            return memo[2]
        tmdb_kind = KINDS.get(kind)
        wanted = wanted_for(item, tmdb_kind) if tmdb_kind is not None else None
        self._memo[id(item)] = (item, kind, wanted)  # the item is held: its id stays its own
        while len(self._memo) > MEMO_SIZE:
            self._memo.popitem(last=False)
        return wanted
