"""Headless model behind the Movies/Series library: genres, sorting, letter index, details.

The library UI only paints what this model returns. It reads the CatalogService and never
touches the network or the disk itself, apart from ``config.save_config()``.
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

from services import favorites
from services.catalog import ALL_GROUP_ID, shown_label
from services.i18n import N_, tr, translate
from services.provider_ids import (
    ALL_PROVIDERS,
    PROVIDER_KEY,
    provider_of,
    split_group_id,
    with_provider,
)
from services.text_fold import fold
from services.watch_state import WatchState

logger = logging.getLogger(__name__)

T = TypeVar("T")

CONTINUE, NEW, MY_LIST = "@continue", "@new", "@mylist"
# The shelves' names, shown with translate() (the genres carry them translated).
PINNED_TITLES = {
    CONTINUE: N_("Continue"),  # i18n: Library shelf (genre column): titles to continue watching
    NEW: N_("New"),  # i18n: Library shelf (genre column): the newest titles
    MY_LIST: N_("My list"),  # i18n: Library shelf (genre column): the titles starred with ★
}
# Provider groups that are no genre: every title ("All"), and the loaders' group for titles
# without a category ("Unknown Category").
_NOT_GENRES = frozenset({ALL_GROUP_ID, "None"})
NEW_LIMIT = 48
MIN_YEAR = 1800
MEMO_SIZE = 64
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
        value = float(text)
    except ValueError:
        return _iso_time(text)
    if not math.isfinite(value) or value <= 0:
        return None
    return value / 1000 if value > 1e11 else value


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


class LibraryModel(QObject):
    changed = Signal()
    failed = Signal(str)
    notice = Signal(str)

    def __init__(self, catalog, config, watch: WatchState, parent=None):
        super().__init__(parent)
        self._catalog = catalog
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
        self._memo: OrderedDict[tuple, Any] = OrderedDict()
        # Parsed `added` times for the current content version, by item identity (the
        # item is kept too, so its id cannot be reused while the entry lives).
        self._added_times: dict[int, tuple[dict, Optional[float]]] = {}
        self._added_version: Optional[int] = None
        catalog.sectionReady.connect(self._on_section_ready)
        catalog.sectionFailed.connect(self._on_section_failed)
        catalog.groupReady.connect(self._on_group_ready)
        catalog.groupFailed.connect(self._on_group_failed)
        catalog.providerReady.connect(self._on_provider_ready)

    # --- state ------------------------------------------------------------------------
    @property
    def section(self) -> str:
        return self._section

    @property
    def genre_id(self) -> Optional[str]:
        return self._genre

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
        self._memo.clear()
        # A section the provider lacks (an M3U playlist's movies) is ready and empty.
        self._available = section in self._catalog.sections()
        self._ready = not self._available
        if self._available:
            self._catalog.request_section(section)
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
        """Leave a pinned genre that has emptied for the first available one."""
        if self._genre not in PINNED_TITLES or self._pinned_items(self._genre):
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
        if self._mixed():  # every provider still in the list, newest first
            names = set(self._catalog.provider_names())
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
        return (self._catalog.content_version(), self._favorites(), tuple(hist))

    def _memoized(self, key: tuple, build: Callable[[], T]) -> T:
        if key in self._memo:
            self._memo.move_to_end(key)
        else:
            self._memo[key] = build()
            while len(self._memo) > MEMO_SIZE:  # least recently used first
                self._memo.popitem(last=False)
        return cast(T, self._memo[key])

    def _added_lookup(self) -> Callable[[dict], Optional[float]]:
        """`_added(item)`, parsed once per content version (look up a batch with one call)."""
        version = self._catalog.content_version()
        if version != self._added_version:
            self._added_times = {}
            self._added_version = version
        times = self._added_times

        def lookup(item: dict) -> Optional[float]:
            entry = times.get(id(item))
            if entry is None or entry[0] is not item:
                entry = times[id(item)] = (item, _added(item))
            return entry[1]

        return lookup

    # --- content ids / kinds ----------------------------------------------------------
    def _mixed(self) -> bool:
        """Whether the Library shows every provider (All providers)."""
        return bool(self._catalog.all_providers())

    def _content_id(self, item: dict, item_type=None) -> str:
        if not item_type:
            item_type = "serie" if self._section == "series" else "movie"
        provider = provider_of(item) or self._catalog.current_provider_name()
        return WatchState.content_id(provider, item, item_type=item_type)

    def item_kind(self, item: dict) -> str:
        if self._section == "series":
            return "episode" if self._genre == CONTINUE else "series"
        return "movie"

    def _item_cid(self, item: dict) -> str:
        kind = self.item_kind(item)
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

    def _loaded_items(self) -> list[dict]:
        if not self._available:
            return []

        def build() -> list[dict]:
            missing = object()
            seen: set[tuple[str, str]] = set()
            same: set[int] = set()  # a genre lists the dicts All does: told apart by identity
            out: list[dict] = []
            groups = self._catalog.groups(self._section)
            if self._all_lists_everything(groups):
                groups = groups[:1]  # the genres only repeat what All lists
            for group in groups:
                if group.count is None:  # not fetched yet; a provider's heading
                    continue
                for item in self._catalog.items(self._section, str(group.id)) or []:
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

        return self._memoized(("loaded", self._section, self._catalog.content_version()), build)

    def _new_items(self) -> list[dict]:
        def build() -> list[dict]:
            added = self._added_lookup()
            dated = [(added(i), i) for i in self._loaded_items()]
            newest = heapq.nlargest(
                NEW_LIMIT, ((ts, i) for ts, i in dated if ts is not None), key=lambda p: p[0]
            )
            return [i for _, i in newest]

        return self._memoized(("new", self._section, self._catalog.content_version()), build)

    def _continue_items(self) -> list[dict]:
        out: list[dict] = []
        seen: set[str] = set()
        for entry in self._history_entries():
            cid = self._entry_content_id(entry)
            if cid in seen or self._watch.resume_position(cid) is None:
                continue
            seen.add(cid)
            item = entry["item_data"]
            # Stamped with the provider that played it (older history has no stamp), so it
            # plays, stars and asks for its link there, whatever page is on another provider.
            out.append(with_provider(item, entry["provider_name"]))
        return out

    def _my_list_items(self) -> list[dict]:
        is_kept = favorites.matcher(self._config)
        if is_kept is None:
            return []
        return [i for i in self._loaded_items() if is_kept(i)]

    def _pinned_items(self, genre_id: str) -> list[dict]:
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
        for gid in (CONTINUE, NEW, MY_LIST):
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
        key = ("raw", self._section, self._genre, self._catalog.content_version())
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

        def build() -> list[dict]:
            if sort == Sort.ADDED:  # the parsed times are shared with "New"
                added = self._added_lookup()
                return sorted(raw, key=lambda i: _added_order(added(i)))
            return sorted(raw, key=globals()[_SORT_KEYS[sort]])

        return list(self._memoized(key, build))

    def letter_index(self) -> dict[str, int]:
        if self._genre in PINNED_TITLES or self.sort != Sort.AZ:
            return {}
        items = self.items() or []
        key = ("letters", self._section, self._genre, self.sort, self._catalog.content_version())

        def build() -> dict[str, int]:
            index: dict[str, int] = {}
            for pos, item in enumerate(items):
                index.setdefault(_letter(item), pos)
            return index

        return dict(self._memoized(key, build))

    # --- details / favorites ----------------------------------------------------------
    def progress(self, item: dict) -> Optional[float]:
        resume = self._watch.resume_position(self._item_cid(item))
        if resume is None:
            return None
        saved = (self._config.playback_positions or {}).get(self._item_cid(item)) or {}
        duration = int(saved.get("duration") or 0)
        return min(1.0, resume / duration) if duration > 0 else None

    def details(self, item: dict) -> Details:
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
            resume_ms=self._watch.resume_position(self._item_cid(item)),
            progress=self.progress(item),
            favorite=self.is_favorite(item),
            kind=self.item_kind(item),
            provider=provider_of(item) if self._mixed() else "",
        )

    def is_favorite(self, item: dict) -> bool:
        return favorites.is_favorite(self._config, item)

    def toggle_favorite(self, item: dict) -> bool:
        now = favorites.toggle(self._config, item)
        if not favorites.save(self._config):
            self.notice.emit(translate(SAVE_FAILED_NOTICE))
        self._ensure_selection()
        self.changed.emit()
        return now
