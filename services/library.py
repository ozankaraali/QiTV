"""Headless model behind the Movies/Series library: genres, sorting, letter index, details.

The library UI only paints what this model returns. It reads the CatalogService and never
touches the network or the disk itself, apart from ``config.save_config()``.

With a JobRunner, a large section's lists (its titles each once, the newest, a genre's sorted
list, what a find finds) are worked out by a worker on plain data and kept on the GUI thread
when they arrive; meanwhile the lists worked out before answer (or nothing yet: the section
is not ready, a genre's titles are None), and `changed` follows the answer.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import heapq
import logging
import math
import re
from typing import Any, Callable, Optional, TypeVar, cast

from PySide6.QtCore import QObject, Signal

from services import favorites, parental
from services.catalog import ALL_GROUP_ID, shown_label
from services.i18n import N_, tr, translate
from services.provider_ids import (
    ALL_PROVIDERS,
    PROVIDER_KEY,
    provider_of,
    split_group_id,
    with_provider,
)
from services.search import Hit, search_index
from services.text_fold import fold
from services.watch_state import WatchState

logger = logging.getLogger(__name__)

T = TypeVar("T")

CONTINUE, NEW, MY_LIST = "@continue", "@new", "@mylist"
RECORDINGS = "@recordings"  # 2.8: the Recordings folder's files, in Movies whatever the provider
# The shelves' names, shown with translate() (the genres carry them translated).
PINNED_TITLES = {
    CONTINUE: N_("Continue"),  # i18n: Library shelf (genre column): titles to continue watching
    NEW: N_("New"),  # i18n: Library shelf (genre column): the newest titles
    MY_LIST: N_("My list"),  # i18n: Library shelf (genre column): the titles starred with ★
    RECORDINGS: N_("Recordings"),  # i18n: Library shelf (genre column): what QiTV recorded
}
# Provider groups that are no genre: every title ("All"), and the loaders' group for titles
# without a category ("Unknown Category").
_NOT_GENRES = frozenset({ALL_GROUP_ID, "None"})
NEW_LIMIT = 48
MIN_YEAR = 1800
MEMO_SIZE = 64
# From this many titles, a section's titles (each once), its newest, a genre's sorted list and
# what a find finds are worked out by a worker (with a JobRunner): the GUI thread waits on none
# of it (185,000 films take seconds). A smaller list takes milliseconds, worked out at once.
BACKGROUND_FROM = 5000
LATEST_KEPT = 16  # the lists answering while their next content version is worked out
FINDS_KEPT = 16  # the finds a cache keeps (a phone deleting a letter has the last at once)
SAVE_FAILED_NOTICE = N_("Couldn't save your list; it will be lost when QiTV closes")


@dataclass(frozen=True)
class Genre:
    id: str
    title: str
    count: Optional[int]
    pinned: bool = False
    header: bool = False  # a provider's heading above its genres (All providers): not a genre


class Sort(str, Enum):
    ADDED = "added"
    AZ = "az"
    YEAR = "year"
    RATING = "rating"


# The sort chips above the poster grid, shown with translate().
SORT_LABELS = {
    Sort.ADDED: N_("Recently added"),  # i18n: sort chip above the Library's posters
    Sort.AZ: N_("A–Z"),  # i18n: sort chip (alphabetical order); keep it short
    Sort.YEAR: N_("Year"),  # i18n: sort chip (by release year)
    Sort.RATING: N_("Rating"),  # i18n: sort chip (by rating)
}


@dataclass(frozen=True)
class Details:
    title: str
    year: str
    duration: str
    genre: str
    rating: str
    plot: str
    resume_ms: Optional[int]
    progress: Optional[float]
    favorite: bool
    kind: str  # "movie" | "series" | "episode"
    provider: str = ""  # the title's provider, named in the strip while providers are mixed
    cast: str = ""  # its leading actors, as one line ("Ana, Bo")


# --- field parsing ------------------------------------------------------------------------
def title_of(item: dict) -> str:
    """The display title of a catalog item: its name, else its title."""
    return str(item.get("name") or item.get("title") or "")


_title = title_of  # the earlier private name


def _added(item: dict) -> Optional[float]:
    """The unix time of ``added`` (a unix time or ``YYYY-MM-DD[ HH:MM:SS]``), else None."""
    raw = item.get("added")
    if (
        raw.__class__ is str and raw.isdecimal() and len(raw) <= 13
    ):  # a unix time as text, the usual case
        value = int(raw)
        if value <= 0:
            return None
        return value / 1000 if value > 1e11 else float(value)
    if raw is None or isinstance(raw, bool):
        return None
    text = str(raw).strip()
    if not text:
        return None
    if "-" in text[1:] or ":" in text:  # a date: skip the (raising) number attempt
        return _iso_time(text)
    try:
        number = float(text)
    except ValueError:
        return _iso_time(text)
    if not math.isfinite(number) or number <= 0:
        return None
    return number / 1000 if number > 1e11 else number


def _iso_time(text: str) -> Optional[float]:
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _year(item: dict) -> Optional[int]:
    for key in ("year", "releasedate", "releaseDate", "release_date"):
        match = re.search(r"\d{4}", str(item.get(key) or ""))
        if match and int(match.group()) >= MIN_YEAR:
            return int(match.group())
    match = re.search(r"\((\d{4})\)\s*$", _title(item))
    return int(match.group(1)) if match and int(match.group(1)) >= MIN_YEAR else None


def _rating(item: dict) -> Optional[float]:
    for key, scale in (("rating", 1.0), ("rating_imdb", 1.0), ("rating_5based", 2.0)):
        try:
            value = float(item.get(key) or 0) * scale
        except TypeError, ValueError:
            continue
        if math.isfinite(value) and value > 0:
            return value
    return None


def _minutes(item: dict) -> Optional[int]:
    """The running time in whole minutes, else None."""
    secs = item.get("duration_secs")
    try:
        if secs not in (None, "") and float(secs) > 0:
            return int(float(secs) // 60)
    except TypeError, ValueError:
        pass
    raw = str(item.get("duration") or "").strip()
    if ":" in raw:
        try:
            parts = [int(float(p)) for p in raw.split(":")]
        except ValueError:
            return None
        seconds = 0
        for part in parts:
            seconds = seconds * 60 + part
        return seconds // 60
    for value in (raw, str(item.get("time") or "").strip()):
        try:
            if value:
                return int(float(value))
        except ValueError:
            continue
    return None


def _format_duration(minutes: Optional[int]) -> str:
    if not minutes or minutes <= 0:
        return ""
    hours, mins = divmod(minutes, 60)
    if hours:
        return tr("{h}h {m}m", h=hours, m=mins)  # i18n: a running time, abbreviated (1h 5m)
    return tr("{m}m", m=mins)  # i18n: a running time in minutes, abbreviated (45m)


# --- sort keys (module level so tests can spy on them) -------------------------------------
_DIGITS = re.compile(r"(\d+)")


def _az_key(item: dict) -> list:
    parts = _DIGITS.split(fold(_title(item)))
    return [int(p) if i % 2 else p for i, p in enumerate(parts)]


def _added_key(item: dict) -> tuple:
    return _added_order(_added(item))


def _added_order(ts: Optional[float]) -> tuple:
    return (1, 0.0) if ts is None else (0, -ts)


def _year_key(item: dict) -> tuple:
    year = _year(item)
    return (1, 0) if year is None else (0, -year)


def _rating_key(item: dict) -> tuple:
    rating = _rating(item)
    return (1, 0.0) if rating is None else (0, -rating)


_SORT_KEYS = {
    Sort.ADDED: "_added_key",
    Sort.AZ: "_az_key",
    Sort.YEAR: "_year_key",
    Sort.RATING: "_rating_key",
}


_LETTER_PREFIX = 8  # characters folded to find a title's first letter


def letter_of(title: str) -> str:
    """The A–Z rail letter of a title: its folded first character when a–z, else '#'.

    Only the start of the title is folded: folding works character by character (accents
    are dropped, the base letters stay in order), so the first folded character of a prefix
    is the title's, unless the prefix folds to nothing.
    """
    first = fold(title[:_LETTER_PREFIX])[:1] or fold(title)[:1]
    return first if "a" <= first <= "z" else "#"


def _letter(item: dict) -> str:
    return letter_of(title_of(item))


# --- a section's work, on plain data (a worker's or the GUI thread's: the same answers) ----
def _dedupe(lists: list[list[dict]]) -> list[dict]:
    """The titles of `lists` (the genres' lists), each once."""
    missing = object()
    seen: set[tuple[str, str]] = set()
    same: set[int] = set()  # a genre lists the dicts All does: told apart by identity
    out: list[dict] = []
    for items in lists:
        for item in items:
            if id(item) in same:
                continue
            same.add(id(item))
            # (provider_of(item), navigation.item_key(item)) inlined: 40,000 titles a time
            ident = item.get("id", missing)
            if ident is missing:
                ident = item.get("cmd", item.get("name", ""))
            key = (item.get(PROVIDER_KEY) or "", str(ident))
            if key not in seen:
                seen.add(key)
                out.append(item)
    return out


def _newest(items: list[dict], added: Callable[[dict], Optional[float]]) -> list[dict]:
    dated = [(added(i), i) for i in items]
    newest = heapq.nlargest(
        NEW_LIMIT, ((ts, i) for ts, i in dated if ts is not None), key=lambda p: p[0]
    )
    return [i for _, i in newest]


def _sorted(
    items: list[dict], sort: Sort, added: Optional[Callable[[dict], Optional[float]]] = None
) -> list[dict]:
    if sort == Sort.ADDED:  # on the GUI thread, the parsed times shared with "New" (`added`)
        parsed = added or _added
        return sorted(items, key=lambda i: _added_order(parsed(i)))
    return sorted(items, key=globals()[_SORT_KEYS[sort]])


def _letters(items: list[dict]) -> dict[str, int]:
    index: dict[str, int] = {}
    for pos, item in enumerate(items):
        index.setdefault(_letter(item), pos)
    return index


def _folded(items: list[dict]) -> list[str]:
    return [fold(title_of(i)) for i in items]


def _indexed(items: list[dict], section: str) -> list[tuple[str, str, str, str, dict]]:
    """`items` as services.search indexes them (each title folded)."""
    return [("", section, fold(title_of(i)), title_of(i), i) for i in items]


def _having(items: list[dict], folded: list[str], words: list[str]) -> list[dict]:
    return [i for i, title in zip(items, folded) if all(w in title for w in words)]


# What a worker works out: a tuple, in the order of the keys it is for.
def _section_work(lists: list[list[dict]]) -> tuple:
    loaded = _dedupe(lists)
    return loaded, _newest(loaded, _added)


def _sort_work(items: list[dict], sort: Sort, letters: bool) -> tuple:
    ordered = _sorted(items, sort)
    return (ordered, _letters(ordered)) if letters else (ordered,)


def _letters_work(items: list[dict]) -> tuple:
    return (_letters(items),)


def _folded_work(items: list[dict]) -> tuple:
    return (_folded(items),)


def _find_work(index: list, text: str) -> tuple:
    return (search_index(index, text),)


def _index_find_work(items: list[dict], section: str, text: str) -> tuple:
    index = _indexed(items, section)
    return index, search_index(index, text)


def _found_work(items: list[dict], folded: list[str], words: list[str]) -> tuple:
    return (_having(items, folded, words),)


def _fold_found_work(items: list[dict], words: list[str]) -> tuple:
    folded = _folded(items)
    return folded, _having(items, folded, words)


class LibraryWork(QObject):
    """JobRunner worker: `work(*inputs)` on plain data (the catalog's title dicts, strings),
    its answers by the cache keys they are for."""

    finished = Signal(object)  # {key: answer}
    error = Signal(str)

    def __init__(self, keys: tuple, work: Callable[..., tuple], inputs: tuple) -> None:
        super().__init__()
        self._keys, self._work, self._inputs = keys, work, inputs

    def run(self) -> None:
        self.finished.emit(dict(zip(self._keys, self._work(*self._inputs))))


def history_episode(history, provider: str, series: dict) -> Optional[dict]:
    """The episode of the newest watch-history entry for `series`, when it is known.

    `history` is newest first (ConfigManager.watch_history). STB episodes copy the series
    dict, so their id is the series id, and carry `season_id`; an entry may also name its
    `series_id`. Xtream episodes carry neither, so their series is unknown.
    """
    series_id = str(series.get("id"))
    for entry in history or []:
        if not isinstance(entry, dict):
            continue
        episode = entry.get("item_data")
        if (
            entry.get("provider_name") != provider
            or entry.get("item_type") != "episode"
            or not isinstance(episode, dict)
        ):
            continue
        owner = episode.get("series_id")
        if owner is None and episode.get("season_id") is not None:
            owner = episode.get("id")
        if owner is not None and str(owner) == series_id:
            return episode
    return None


def history_season(history, provider: str, series: dict, seasons: list[dict]) -> Optional[dict]:
    """The season (one of `seasons`) of `history_episode`, when it is known."""
    episode = history_episode(history, provider, series)
    if episode is None:
        return None
    season_id, number = episode.get("season_id"), episode.get("season")
    for season in seasons:
        if season_id is not None and str(season.get("id")) == str(season_id):
            return season
    for season in seasons:
        if number not in (None, "") and str(season.get("number")) == str(number):
            return season
    return None


class LibraryCache(QObject):
    """What Library models work out from one catalog (a genre's sorted list, the shelves, the
    parsed dates), by provider and content version: the TV's Library and the phone remote's
    copies of it share one, so a large provider's list is sorted once for all of them.

    With a JobRunner (`jobs`), a large section's work is done by workers: the keys on their
    way are `pending`, and `landed` names those whose answers arrived (or failed: those are
    worked out on the GUI thread from then on)."""

    landed = Signal(object)  # a set of keys

    def __init__(self, jobs: Any = None) -> None:
        super().__init__()
        self.memo: OrderedDict[tuple, Any] = OrderedDict()
        # Parsed `added` times for one content version, by item identity (the item is kept
        # too, so its id cannot be reused while the entry lives).
        self.added_times: dict[int, tuple[dict, Optional[float]]] = {}
        self.added_version: Optional[int] = None
        self.jobs = jobs
        self.pending: set[tuple] = set()
        self.failed: set[tuple] = set()
        # Each list's last answer, by its key without the content version: what answers while
        # the next version's is worked out, with what parental controls hid when it was worked
        # out (`lock_state()`): one from another state answers nothing (it may hold titles
        # hidden now).
        self.latest: OrderedDict[tuple, tuple[Any, Any]] = OrderedDict()
        self.lock_state: Callable[[], Any] = lambda: None
        self.finds: OrderedDict[tuple, Any] = OrderedDict()  # what the finds found, by find
        # Counts the answers that were not the final ones (an earlier version's, or nothing
        # yet): what is built on one is not kept.
        self.interim = 0

    def start(self, keys: tuple, work: Callable[..., tuple], inputs: tuple) -> None:
        self.pending.update(keys)
        worker = LibraryWork(keys, work, inputs)
        self.jobs.run_worker(worker, self._arrived, self._failed, context=(keys, self.lock_state()))

    def keep(self, key: tuple, value: Any, state: Any) -> None:
        """`value` for the memo's `key` (its content version last), the latest of its list
        (worked out under the lock state `state`)."""
        _kept(self.memo, key, value, MEMO_SIZE)
        _kept(self.latest, key[:-1], (state, value), LATEST_KEPT)

    def stale(self, key: tuple, default: Any) -> Any:
        """The latest list of the memo's `key` (an earlier content version's) if it was worked
        out under today's lock state, else `default`."""
        state, value = self.latest.get(key[:-1], (None, default))
        return value if key[:-1] in self.latest and state == self.lock_state() else default

    def found(self, key: tuple, value: Any) -> None:
        _kept(self.finds, key, value, FINDS_KEPT)

    def _arrived(self, context: tuple, answers: dict) -> None:
        keys, state = context
        self.pending.difference_update(keys)
        for key, value in answers.items():
            if key[1] in _FINDS:
                self.found(key, value)
            else:
                self.keep(key, value, state)
        self.landed.emit(set(keys))

    def _failed(self, context: tuple, message: str) -> None:
        keys, _state = context
        self.pending.difference_update(keys)
        self.failed.update(keys)
        logger.warning("The Library's %s could not be worked out (%s)", keys[0][1], message)
        self.landed.emit(set(keys))


_FINDS = frozenset({"find", "found"})  # the kinds of key kept in LibraryCache.finds


def _kept(store: OrderedDict, key: tuple, value: Any, size: int) -> None:
    store[key] = value
    store.move_to_end(key)
    while len(store) > size:  # least recently used first
        store.popitem(last=False)


class LibraryModel(QObject):
    changed = Signal()
    failed = Signal(str)
    notice = Signal(str)

    def __init__(
        self,
        catalog,
        config,
        watch: WatchState,
        parent=None,
        cache: Optional[LibraryCache] = None,
        recordings=None,
        jobs=None,
    ):
        super().__init__(parent)
        self._catalog = catalog
        # 2.8: the Recordings folder's titles (services.recordings_list.RecordingsList), the
        # Recordings shelf of Movies whatever the provider (one without films too).
        self._recordings = recordings
        self._config = config
        self._watch = watch
        self._section = "movies"
        self._provider = ""
        self._requested = False
        self._ready = False
        self._genre: Optional[str] = None
        self._pinned_sig: tuple = ()
        self._chosen_sort: Optional[Sort] = None
        self._available = True  # the provider offers the section
        # 2.8: what fills a title's gaps for its strip (TMDB: ui/tmdb_fill.py), as
        # filler(item, kind) -> the item, or a copy with more fields; none by default.
        self._filler: Optional[Callable[[dict, str], dict]] = None
        # A JobRunner (`jobs`, or the shared cache's) works out a large section's lists.
        self.cache = cache if cache is not None else LibraryCache(jobs)
        self.cache.lock_state = self._lock_state
        self.cache.landed.connect(self._on_landed)
        self._waiting: set[tuple] = set()  # the keys on their way this model answered without
        self._deferred: Optional[str] = None  # a section ready once its titles are worked out
        catalog.sectionReady.connect(self._on_section_ready)
        catalog.sectionFailed.connect(self._on_section_failed)
        catalog.groupReady.connect(self._on_group_ready)
        catalog.groupFailed.connect(self._on_group_failed)
        catalog.providerReady.connect(self._on_provider_ready)
        if recordings is not None:
            recordings.changed.connect(self._on_recordings_changed)

    # --- state ------------------------------------------------------------------------
    @property
    def _memo(self) -> OrderedDict[tuple, Any]:
        return self.cache.memo

    @property
    def section(self) -> str:
        return self._section

    @property
    def genre_id(self) -> Optional[str]:
        return self._genre

    @property
    def available(self) -> bool:
        """Whether the section has anything to show: the provider offers it, or (Movies) there
        are recordings."""
        return self._available

    @property
    def ready(self) -> bool:
        """Whether the section's lists arrived (or the provider has no such section)."""
        return self._requested and self._ready

    @property
    def sort(self) -> Sort:
        available = self.available_sorts()
        if self._chosen_sort is not None and self._chosen_sort in available:
            return self._chosen_sort
        return Sort.ADDED if Sort.ADDED in available else Sort.AZ

    def set_sort(self, sort: Sort) -> None:
        if sort != self._chosen_sort:
            self._chosen_sort = sort
            self.changed.emit()

    def set_section(self, section: str) -> None:
        self._section = section
        self._provider = self._catalog.current_provider_name()
        self._requested = True
        self._genre = None
        self._deferred = None
        # The cache stays: its entries name their provider, section and content version.
        # A section the provider lacks (an M3U playlist's movies) is ready and empty.
        offered = section in self._catalog.sections()
        self._available = offered or self._has_recordings(section)
        self._ready = not offered
        if offered:
            self._catalog.request_section(section)
        elif self._available:
            self._genre = RECORDINGS  # only recordings: nothing to wait for
        self.changed.emit()

    def _current(self, section: str) -> bool:
        return (
            self._requested
            and section == self._section
            and self._catalog.current_provider_name() == self._provider
        )

    # --- catalog events ---------------------------------------------------------------
    def _on_provider_ready(self, name: str) -> None:
        if not self._requested:
            return
        if self._mixed() and name != ALL_PROVIDERS:
            # One provider of the mix is ready: its lists join the others, the genre stays.
            self._catalog.request_section(self._section)
            self.changed.emit()
            return
        if (
            name == self._provider
            and self._genre is not None
            and self._section in self._catalog.sections()
        ):
            # The source shown is ready again (a refresh, a new sign-in, a title of it chosen
            # while it set up): its lists come again and the genre being read stays.
            self._catalog.request_section(self._section)
            self.changed.emit()
            return
        self.set_section(self._section)  # another source: from its first genre

    def _on_section_ready(self, section: str) -> None:
        """The section's lists arrived: the first genre when none is chosen yet; afterwards
        (a stale list reloaded, the section asked for again, another provider's lists in the
        mix) the genre being read stays while it is listed."""
        if not self._current(section):
            return
        if self._working():
            self._deferred = section  # ready once a worker has worked out its titles
            return
        self._ready = True  # memo keys carry the content version: nothing to clear
        pinned = self._pinned_genres()
        groups = [g for g in self._catalog.groups(self._section) if not g.header]
        genre = self._genre
        if genre is not None and self._genre_listed(groups):
            if genre not in PINNED_TITLES:
                self._select(genre)  # a portal's genre loads on demand: asked again if gone
        elif pinned:
            self._genre = pinned[0].id
        elif groups:
            self._select(str(groups[0].id))
        else:
            self._genre = None
        self._pinned_sig = self._pinned_signature()
        self.changed.emit()

    def lists_recordings(self) -> bool:
        """Whether the Recordings folder has titles (Movies then shows whatever the provider)."""
        return bool(self._recording_items())

    def _has_recordings(self, section: str) -> bool:
        return section == "movies" and bool(self._recording_items())

    def _on_recordings_changed(self) -> None:
        """The Recordings folder changed: its shelf follows (and, on a provider without films,
        whether Movies has anything at all)."""
        if not self._requested:
            return
        if self._section not in self._catalog.sections():
            self._available = self._has_recordings(self._section)
            self._ready = True
            self._genre = RECORDINGS if self._available else None
        else:
            self._ensure_selection()
        self.changed.emit()

    def _on_section_failed(self, section: str, message: str) -> None:
        if self._current(section):
            self.failed.emit(message)

    def _on_group_ready(self, section: str, group_id: str) -> None:
        if not self._current(section):
            return
        signature = self._pinned_signature()
        grew = signature != self._pinned_sig  # New grows as genres load
        self._pinned_sig = signature
        moved = self._ensure_selection()
        if grew or moved or group_id == self._genre:
            self.changed.emit()

    def _genre_listed(self, groups: list) -> bool:
        """Whether the genre chosen is still one of the genres listed (groups: no headings)."""
        genre = self._genre
        if genre is None:
            return False
        if genre in PINNED_TITLES:
            return bool(self._pinned_items(genre))
        return any(str(g.id) == genre for g in groups)

    def _pinned_signature(self) -> tuple:
        return tuple((g.id, g.count) for g in self._pinned_genres())

    def _ensure_selection(self) -> bool:
        """Leave a pinned genre that has emptied for the first available one (not one whose
        titles are on their way from a worker)."""
        if self._genre not in PINNED_TITLES:
            return False
        items, final = self._final(lambda: self._pinned_items(cast(str, self._genre)))
        if items or not final:
            return False
        available = [g for g in self.genres() if not g.header]
        if available:
            self._select(available[0].id)
        else:
            self._genre = None
        return True

    def _on_group_failed(self, section: str, group_id: str, message: str) -> None:
        if self._current(section) and group_id == self._genre:
            self.failed.emit(message)

    # --- fingerprints -----------------------------------------------------------------
    def _favorites(self) -> tuple:
        return favorites.fingerprint(self._config)

    def _history_entries(self) -> list[dict]:
        wanted = "episode" if self._section == "series" else "movie"
        if self._mixed():  # the providers mixed (2.8.1: those ticked), newest first
            names = set(self._catalog.mixed_names())
        else:
            names = {self._catalog.current_provider_name()}
        return [
            h
            for h in (self._config.watch_history or [])
            if h.get("provider_name") in names
            and h.get("item_type") == wanted
            and isinstance(h.get("item_data"), dict)
        ]

    def _entry_content_id(self, entry: dict) -> str:
        """The resume key of a watch-history entry (at the provider that played it)."""
        kind = "episode" if self._section == "series" else "movie"
        return WatchState.content_id(entry["provider_name"], entry["item_data"], item_type=kind)

    def _fingerprint(self) -> tuple:
        positions = self._config.playback_positions or {}
        hist = []
        for entry in self._history_entries():
            cid = self._entry_content_id(entry)
            hist.append((cid, (positions.get(cid) or {}).get("position")))
        shelf = self._recordings.version if self._recordings is not None else 0
        return (self._catalog.content_version(), self._favorites(), tuple(hist), shelf)

    def _key(self, key: tuple) -> tuple:
        """A cache entry's key: `key` at the provider shown (a shared cache serves several)."""
        return (self._catalog.current_provider_name(),) + key

    def _memoized(self, key: tuple, build: Callable[[], T]) -> T:
        key = self._key(key)
        if key in self._memo:
            self._memo.move_to_end(key)
        else:
            interim = self.cache.interim
            value = build()
            if self.cache.interim != interim:
                return value  # built on what a worker still works out: not kept
            _kept(self._memo, key, value, MEMO_SIZE)
        return cast(T, self._memo[key])

    # --- a worker's work --------------------------------------------------------------
    def _background(self, size: int, *keys: tuple) -> bool:
        """Whether a list of `size` titles is worked out by a worker (for these full keys)."""
        cache = self.cache
        return cache.jobs is not None and size >= BACKGROUND_FROM and cache.failed.isdisjoint(keys)

    def _later(
        self,
        keys: tuple,
        work: Callable[..., tuple],
        inputs: Callable[[], tuple],
        interims: tuple,
        stale: bool = True,
    ) -> tuple:
        """The full `keys` worked out by a worker, `work(*inputs())` (started unless on its
        way; `changed` follows its answer): meanwhile each one's list as worked out for an
        earlier content version (`stale`), else its interim."""
        cache = self.cache
        self._waiting.update(keys)
        if cache.pending.isdisjoint(keys):
            cache.start(keys, work, inputs())
        cache.interim += 1
        if not stale:
            return interims
        return tuple(cache.stale(key, interim) for key, interim in zip(keys, interims))

    def _lock_state(self) -> Any:
        """What parental controls hide now, as the catalog says (None: it says nothing)."""
        state = getattr(self._catalog, "parental_state", None)
        return state() if callable(state) else None

    def _final(self, ask: Callable[[], T]) -> tuple[T, bool]:
        """`ask()`, and whether its answer is the final one (no worker's still on its way)."""
        interim = self.cache.interim
        answer = ask()
        return answer, self.cache.interim == interim

    def _working(self) -> bool:
        """Whether the section's titles are on their way from a worker (asked for when not)."""
        return not self._final(self._new_items)[1]

    def _on_landed(self, keys: set) -> None:
        if self._waiting.isdisjoint(keys):
            return
        self._waiting -= keys
        section, self._deferred = self._deferred, None
        if section is not None:
            self._on_section_ready(section)  # its titles are in: the section is ready
            return
        self._ensure_selection()
        self.changed.emit()

    def _added_lookup(self) -> Callable[[dict], Optional[float]]:
        """`_added(item)`, parsed once per content version (look up a batch with one call)."""
        version, cache = self._catalog.content_version(), self.cache
        if version != cache.added_version:
            cache.added_times = {}
            cache.added_version = version
        times = cache.added_times

        def lookup(item: dict) -> Optional[float]:
            entry = times.get(id(item))
            if entry is None or entry[0] is not item:
                entry = times[id(item)] = (item, _added(item))
            return entry[1]

        return lookup

    # --- content ids / kinds ----------------------------------------------------------
    def _mixed(self) -> bool:
        """Whether the Library mixes providers (All providers, or those ticked)."""
        return bool(self._catalog.all_providers())

    def _content_id(self, item: dict, item_type=None) -> str:
        if not item_type:
            item_type = "serie" if self._section == "series" else "movie"
        provider = provider_of(item) or self._catalog.current_provider_name()
        return WatchState.content_id(provider, item, item_type=item_type)

    def item_kind(self, item: dict) -> str:
        if item.get("_recording"):
            return "recording"
        if item.get("_scheduled"):
            return "scheduled"  # 2.8: a recording still to come (its right-click cancels it)
        if self._section == "series":
            return "episode" if self._genre == CONTINUE else "series"
        return "movie"

    def _item_cid(self, item: dict) -> str:
        kind = self.item_kind(item)
        if kind in ("recording", "scheduled"):
            return str(item.get("id") or "")  # "recording:<file name>": no provider's
        return self._content_id(item, {"episode": "episode", "series": "serie"}.get(kind, "movie"))

    def identity(self, item: dict) -> str:
        """A title's lasting identity (its provider, id and kind): the same title once its
        list is loaded again, in new dicts."""
        return self._item_cid(item)

    # --- loaded items / pinned --------------------------------------------------------
    def _all_lists_everything(self, groups: list) -> bool:
        """Whether the All genre holds every title: every provider lists All (an STB portal
        gives its titles genre by genre, so its genres add to it)."""
        if not groups or groups[0].id != ALL_GROUP_ID or groups[0].count is None:
            return False
        if self._catalog.items(self._section, ALL_GROUP_ID) is None:
            return False
        try:
            names = [g.provider for g in groups if g.header]
            if names:
                return all(
                    str(self._catalog.provider_entry(n).get("type", "")).upper() != "STB"
                    for n in names
                )
            return str(self._catalog.provider_type()).upper() != "STB"
        except AttributeError, KeyError:
            return False

    def _listed_groups(self) -> list:
        """The genres whose lists hold the section's titles (All alone when it holds them)."""
        groups = self._catalog.groups(self._section)
        if self._all_lists_everything(groups):
            groups = groups[:1]  # the genres only repeat what All lists
        return [g for g in groups if g.count is not None]  # not fetched yet; a heading

    def _section_titles(self, kind: str) -> list[dict]:
        """The section's titles, each once ("loaded"), or the newest of them ("new"), worked
        out once per content version: by a worker for a large section."""
        if not self._available:
            return []
        section, version = self._section, self._catalog.content_version()
        keys = tuple(self._key((each, section, version)) for each in ("loaded", "new"))
        wanted = keys[0] if kind == "loaded" else keys[1]
        if wanted in self._memo:
            self._memo.move_to_end(wanted)
            return cast(list, self._memo[wanted])
        on_its_way = not self.cache.pending.isdisjoint(keys)
        groups = [] if on_its_way else self._listed_groups()  # (not gathered again meanwhile)

        def lists() -> list[list[dict]]:
            return [self._catalog.items(section, str(g.id)) or [] for g in groups]

        if on_its_way or self._background(sum(g.count for g in groups), *keys):
            found = self._later(keys, _section_work, lambda: (lists(),), ([], []))
            return cast(list, found[0] if kind == "loaded" else found[1])
        if kind == "loaded":
            return self._memoized(("loaded", section, version), lambda: _dedupe(lists()))
        loaded = self._section_titles("loaded")
        return self._memoized(
            ("new", section, version), lambda: _newest(loaded, self._added_lookup())
        )

    def _loaded_items(self) -> list[dict]:
        return self._section_titles("loaded")

    def _new_items(self) -> list[dict]:
        return self._section_titles("new")

    def _continue_items(self) -> list[dict]:
        guard = parental.installed()
        out: list[dict] = []
        seen: set[str] = set()
        for entry in self._history_entries():
            cid = self._entry_content_id(entry)
            if cid in seen or self._watch.resume_position(cid) is None:
                continue
            seen.add(cid)
            item = entry["item_data"]
            if guard is not None and guard.hides_item(
                self._section, with_provider(item, entry["provider_name"])
            ):
                continue  # 2.8: hidden by parental controls while locked
            # Stamped with the provider that played it (older history has no stamp), so it
            # plays, stars and asks for its link there, whatever page is on another provider.
            out.append(with_provider(item, entry["provider_name"]))
        return out

    def _my_list_items(self) -> list[dict]:
        is_kept = favorites.matcher(self._config)
        if is_kept is None:
            return []
        return [i for i in self._loaded_items() if is_kept(i)]

    def _recording_items(self) -> list[dict]:
        return self._recordings.items() if self._recordings is not None else []

    def _pinned_items(self, genre_id: str) -> list[dict]:
        if genre_id == RECORDINGS:
            if self._section != "movies":
                return []
            guard = parental.installed()  # 2.8: a hidden channel's recordings, while locked
            items = self._recording_items()
            if guard is None or not guard.active():
                return items
            return [i for i in items if not guard.hides_recording(str(i.get("_channel") or ""))]
        fp = self._fingerprint()
        return self._memoized(
            ("pinned", self._section, genre_id, fp),
            lambda: {
                CONTINUE: self._continue_items,
                NEW: self._new_items,
                MY_LIST: self._my_list_items,
            }[genre_id](),
        )

    def _pinned_genres(self) -> list[Genre]:
        out = []
        for gid in (CONTINUE, NEW, MY_LIST, RECORDINGS):
            items = self._pinned_items(gid)
            if items:
                out.append(Genre(gid, translate(PINNED_TITLES[gid]), len(items), True))
        return out

    # --- genres -----------------------------------------------------------------------
    def genres(self, filter_text: str = "") -> list[Genre]:
        if not self._requested or not self._available:
            return []
        needle = fold(filter_text)

        def build() -> list[Genre]:
            # A genre's title as shown: the loaders' own ("All") in the active language.
            found = self._pinned_genres() + [
                Genre(str(g.id), shown_label(g.title), g.count, header=g.header)
                for g in self._catalog.groups(self._section)
            ]
            if needle:
                found = self._matching(found, needle)
            return found

        return list(self._memoized(("genres", self._section, needle, self._fingerprint()), build))

    @staticmethod
    def _matching(genres: list[Genre], needle: str) -> list[Genre]:
        """The genres whose title has `needle`, each provider's heading only above a match."""
        found: list[Genre] = []
        heading: Optional[Genre] = None
        for genre in genres:
            if genre.header:
                heading = genre
            elif needle in fold(genre.title):
                if heading is not None:
                    found.append(heading)
                    heading = None
                found.append(genre)
        return found

    def select_genre(self, genre_id: str) -> None:
        self._select(genre_id)
        self.changed.emit()

    def _select(self, genre_id: str) -> None:
        self._genre = genre_id
        if genre_id not in PINNED_TITLES and self._catalog.items(self._section, genre_id) is None:
            self._catalog.request_group(self._section, genre_id)

    # --- items / sorting --------------------------------------------------------------
    def _genre_items(self) -> Optional[list[dict]]:
        if self._genre is None:
            return [] if self._ready else None
        if self._genre in PINNED_TITLES:
            return self._pinned_items(self._genre)
        items: Optional[list[dict]] = self._catalog.items(self._section, self._genre)
        return items

    def _raw_items(self) -> Optional[list[dict]]:
        if not self._requested:
            return None
        if self._genre in PINNED_TITLES:
            return self._genre_items()
        key = self._key(("raw", self._section, self._genre, self._catalog.content_version()))
        if key in self._memo:
            return cast(Optional[list[dict]], self._memo[key])
        items = self._genre_items()
        if items is not None:
            self._memo[key] = items
        return items

    def available_sorts(self) -> list[Sort]:
        items = self._raw_items()
        if items is None:
            return list(Sort)
        key = ("has_added", self._section, self._genre, self._catalog.content_version())
        added = self._added_lookup()
        has_added = self._memoized(key, lambda: any(added(i) is not None for i in items))
        return [s for s in Sort if s != Sort.ADDED or has_added]

    def items(self) -> Optional[list[dict]]:
        raw = self._raw_items()
        if raw is None:
            return None
        if self._genre in PINNED_TITLES:
            return list(raw)
        sort = self.sort
        key = ("items", self._section, self._genre, sort, self._catalog.content_version())
        full = self._key(key)
        if full not in self._memo and self._background(len(raw), full):
            # A large genre: sorted by a worker (with its letters for A–Z); meanwhile the list
            # as sorted before (an earlier content version), else None (on its way).
            keys = (full, self._key(("letters",) + key[1:])) if sort == Sort.AZ else (full,)
            found = self._later(keys, _sort_work, lambda: (raw, sort, sort == Sort.AZ), (None, {}))
            return None if found[0] is None else list(found[0])

        def build() -> list[dict]:
            return _sorted(raw, sort, self._added_lookup() if sort == Sort.ADDED else None)

        return list(self._memoized(key, build))

    def search_index(self) -> list[tuple[str, str, str, str, dict]]:
        """The section's titles as services.search indexes them (each title folded), worked out
        once per catalog version: the phone remote finds titles with the desktop's rules."""
        loaded = self._loaded_items()
        key = ("search", self._section, self._catalog.content_version())
        return list(self._memoized(key, lambda: _indexed(loaded, self._section)))

    def find_titles(self, text: str) -> Optional[list[Hit]]:
        """The section's titles `text` finds, as services.search finds them; None while a
        worker finds them in a large section (`changed` follows)."""
        loaded, final = self._final(self._loaded_items)
        if not final:
            return None
        version = self._catalog.content_version()
        index_key = self._key(("search", self._section, version))
        key = (index_key[0], "find", self._section, version, text)
        cache = self.cache
        if key in cache.finds:
            cache.finds.move_to_end(key)
            return list(cache.finds[key])
        if not self._background(len(loaded), index_key, key):
            hits = search_index(self.search_index(), text)
            cache.found(key, hits)
            return list(hits)
        index = cache.memo.get(index_key)
        if index is not None:
            self._later((key,), _find_work, lambda: (index, text), (None,), stale=False)
        else:  # the index first: a find on its way already makes it, else this one does
            work = (self._section, text)
            keys = (index_key, key)
            self._later(keys, _index_find_work, lambda: (loaded, *work), ([], None), stale=False)
        return None

    def folded_titles(self) -> list[str]:
        """The titles of items(), folded for a find, in its order; worked out once per list
        (by a worker for a large one: [] meanwhile)."""
        items, final = self._final(lambda: self.items() or [])
        if self._genre in PINNED_TITLES:  # a shelf changes with the history, not the version
            return [fold(title_of(i)) for i in items]
        if not final:
            return []
        key = ("folded", self._section, self._genre, self.sort, self._catalog.content_version())
        full = self._key(key)
        if full not in self._memo and self._background(len(items), full):
            return list(self._later((full,), _folded_work, lambda: (items,), ([],), False)[0])
        return list(self._memoized(key, lambda: _folded(items)))

    def find_in_genre(self, text: str) -> Optional[list[dict]]:
        """items() whose folded titles have every word of `text`, in their order; None while
        on their way (a large genre's from a worker: `changed` follows)."""
        items, final = self._final(self.items)
        words = fold(text).split()
        if items is None or not words:
            return items
        if not final:
            return None  # the genre's titles come first
        if self._genre in PINNED_TITLES:  # a shelf changes with the stars, not the version
            return _having(items, self.folded_titles(), words)
        version = self._catalog.content_version()
        folded_key = self._key(("folded", self._section, self._genre, self.sort, version))
        key = (folded_key[0], "found", *folded_key[2:], text)
        cache = self.cache
        if key in cache.finds:
            cache.finds.move_to_end(key)
            return list(cache.finds[key])
        if not self._background(len(items), folded_key, key):
            found = _having(items, self.folded_titles(), words)
            cache.found(key, found)
            return list(found)
        folded = cache.memo.get(folded_key)
        if folded is not None:
            self._later((key,), _found_work, lambda: (items, folded, words), (None,), False)
        else:
            keys = (folded_key, key)
            self._later(keys, _fold_found_work, lambda: (items, words), ([], None), False)
        return None

    def letter_index(self) -> dict[str, int]:
        if self._genre in PINNED_TITLES or self.sort != Sort.AZ:
            return {}
        items, final = self._final(lambda: self.items() or [])
        key = ("letters", self._section, self._genre, self.sort, self._catalog.content_version())
        full = self._key(key)
        if full not in self._memo:
            if not final:  # the sorted list on its way brings them: an earlier one's meanwhile
                self.cache.interim += 1
                return dict(self.cache.stale(full, {})) if items else {}
            if self._background(len(items), full):
                return dict(self._later((full,), _letters_work, lambda: (items,), ({},), False)[0])
        return dict(self._memoized(key, lambda: _letters(items)))

    # --- details / favorites ----------------------------------------------------------
    def progress(self, item: dict) -> Optional[float]:
        resume = self._watch.resume_position(self._item_cid(item))
        if resume is None:
            return None
        saved = (self._config.playback_positions or {}).get(self._item_cid(item)) or {}
        duration = int(saved.get("duration") or 0)
        return min(1.0, resume / duration) if duration > 0 else None

    def set_filler(self, filler: Optional[Callable[[dict, str], dict]]) -> None:
        """`filler(item, kind)` fills a title's gaps in its details (None: none does)."""
        self._filler = filler

    def filled(self, item: dict, kind: Optional[str] = None) -> dict:
        """`item` with its gaps filled (the item itself without a filler); `kind` is the
        title's ("movie", "series"…), the shown section's by default."""
        if self._filler is None:
            return item
        try:
            return self._filler(item, kind or self.item_kind(item))
        except Exception:
            logger.exception("The details filler failed")
            return item

    def details(self, item: dict, kind: Optional[str] = None) -> Details:
        """The strip's details of a title; `kind` as `filled` takes it."""
        kind = kind or self.item_kind(item)
        resume_ms = self._watch.resume_position(self._item_cid(item))
        progress, favorite = self.progress(item), self.is_favorite(item)
        item = self.filled(item, kind)
        year = _year(item)
        rating = _rating(item)
        genre = str(item.get("genre") or item.get("genres_str") or "")
        current = self._genre
        # Else the genre the title is listed under; a shelf (Continue, New, My list) or a
        # group such as "All" names none (a provider's own too, under its heading).
        own = split_group_id(current)[1] if current is not None else None
        if not genre and current not in PINNED_TITLES and own not in _NOT_GENRES:
            genre = next((g.title for g in self.genres() if g.id == current), "")
        return Details(
            title=_title(item),
            year="" if year is None else str(year),
            duration=_format_duration(_minutes(item)),
            genre=genre,
            rating="" if rating is None else f"{rating:.1f}",
            plot=str(item.get("plot") or item.get("description") or ""),
            resume_ms=resume_ms,
            progress=progress,
            favorite=favorite,
            kind=kind,
            provider=provider_of(item) if self._mixed() else "",
            cast=str(item.get("cast") or item.get("actors") or ""),
        )

    def is_favorite(self, item: dict) -> bool:
        if item.get("_recording") or item.get("_scheduled"):
            return False  # a file of the disk: no provider's title to star
        return favorites.is_favorite(self._config, item)

    def toggle_favorite(self, item: dict) -> bool:
        now = favorites.toggle(self._config, item)
        if not favorites.save(self._config):
            self.notice.emit(translate(SAVE_FAILED_NOTICE))
        self._ensure_selection()
        self.changed.emit()
        return now
