"""Headless model behind the Movies/Series library: genres, sorting, letter index, details.

The library UI only paints what this model returns. It reads the CatalogService and never
touches the network or the disk itself, apart from ``config.save_config()``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import logging
import math
import re
from typing import Any, Callable, Optional, TypeVar, cast

from PySide6.QtCore import QObject, Signal

from services.navigation import item_key
from services.text_fold import fold
from services.watch_state import WatchState

logger = logging.getLogger(__name__)

T = TypeVar("T")

CONTINUE, NEW, MY_LIST = "@continue", "@new", "@mylist"
PINNED_TITLES = {CONTINUE: "⟲ Continue", NEW: "✦ New", MY_LIST: "★ My list"}
NEW_LIMIT = 48


@dataclass(frozen=True)
class Genre:
    id: str
    title: str
    count: Optional[int]
    pinned: bool = False


class Sort(str, Enum):
    ADDED = "added"
    AZ = "az"
    YEAR = "year"
    RATING = "rating"


SORT_LABELS = {
    Sort.ADDED: "Recently added",
    Sort.AZ: "A–Z",
    Sort.YEAR: "Year",
    Sort.RATING: "Rating",
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


# --- field parsing ------------------------------------------------------------------------
def _title(item: dict) -> str:
    return str(item.get("name") or item.get("title") or "")


def _added(item: dict) -> Optional[float]:
    """The unix time of ``added`` (a unix time or ``YYYY-MM-DD[ HH:MM:SS]``), else None."""
    raw = item.get("added")
    if raw is None or isinstance(raw, bool):
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    if not math.isfinite(value) or value <= 0:
        return None
    return value


def _year(item: dict) -> Optional[int]:
    for key in ("year", "releasedate", "releaseDate", "release_date"):
        match = re.search(r"\d{4}", str(item.get(key) or ""))
        if match:
            return int(match.group())
    match = re.search(r"\((\d{4})\)\s*$", _title(item))
    return int(match.group(1)) if match else None


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
        if secs not in (None, ""):
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
    return f"{hours}h {mins}m" if hours else f"{mins}m"


# --- sort keys (module level so tests can spy on them) -------------------------------------
_DIGITS = re.compile(r"(\d+)")


def _az_key(item: dict) -> list:
    parts = _DIGITS.split(fold(_title(item)))
    return [int(p) if i % 2 else p for i, p in enumerate(parts)]


def _added_key(item: dict) -> tuple:
    ts = _added(item)
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


def _letter(item: dict) -> str:
    first = fold(_title(item))[:1]
    return first if "a" <= first <= "z" else "#"


class LibraryModel(QObject):
    changed = Signal()
    failed = Signal(str)

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
        self._chosen_sort: Optional[Sort] = None
        self._memo: dict[tuple, Any] = {}
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
        self._ready = False
        self._genre = None
        self._memo.clear()
        self._catalog.request_section(section)
        self.changed.emit()

    def _current(self, section: str) -> bool:
        return (
            self._requested
            and section == self._section
            and self._catalog.current_provider_name() == self._provider
        )

    # --- catalog events ---------------------------------------------------------------
    def _on_provider_ready(self, _name: str) -> None:
        if self._requested:
            self.set_section(self._section)

    def _on_section_ready(self, section: str) -> None:
        if not self._current(section):
            return
        self._ready = True
        self._memo.clear()
        pinned = self._pinned_genres()
        groups = self._catalog.groups(self._section)
        if pinned:
            self._genre = pinned[0].id
        elif groups:
            self.select_genre(str(groups[0].id))
        self.changed.emit()

    def _on_section_failed(self, section: str, message: str) -> None:
        if self._current(section):
            self.failed.emit(message)

    def _on_group_ready(self, section: str, group_id: str) -> None:
        if self._current(section) and group_id == self._genre:
            self.changed.emit()
        elif self._current(section) and self._genre in PINNED_TITLES:
            self.changed.emit()  # New grows as genres load

    def _on_group_failed(self, section: str, group_id: str, message: str) -> None:
        if self._current(section) and group_id == self._genre:
            self.failed.emit(message)

    # --- fingerprints -----------------------------------------------------------------
    def _favorites(self) -> tuple:
        return tuple(self._config.favorites or [])

    def _history_entries(self) -> list[dict]:
        wanted = "episode" if self._section == "series" else "movie"
        provider = self._catalog.current_provider_name()
        return [
            h
            for h in (self._config.watch_history or [])
            if h.get("provider_name") == provider
            and h.get("item_type") == wanted
            and isinstance(h.get("item_data"), dict)
        ]

    def _fingerprint(self) -> tuple:
        positions = self._config.playback_positions or {}
        hist = []
        for entry in self._history_entries():
            cid = self._content_id(entry["item_data"], self._section == "series" and "episode")
            hist.append((cid, (positions.get(cid) or {}).get("position")))
        return (self._catalog.content_version(), self._favorites(), tuple(hist))

    def _memoized(self, key: tuple, build: Callable[[], T]) -> T:
        if key not in self._memo:
            if len(self._memo) > 64:
                self._memo.clear()
            self._memo[key] = build()
        return cast(T, self._memo[key])

    # --- content ids / kinds ----------------------------------------------------------
    def _content_id(self, item: dict, item_type=None) -> str:
        if not item_type:
            item_type = "serie" if self._section == "series" else "movie"
        return WatchState.content_id(
            self._catalog.current_provider_name(), item, item_type=item_type
        )

    def item_kind(self, item: dict) -> str:
        if self._section == "series":
            return "episode" if self._genre == CONTINUE else "series"
        return "movie"

    def _item_cid(self, item: dict) -> str:
        kind = self.item_kind(item)
        return self._content_id(item, {"episode": "episode", "series": "serie"}.get(kind, "movie"))

    # --- loaded items / pinned --------------------------------------------------------
    def _loaded_items(self) -> list[dict]:
        def build() -> list[dict]:
            seen: set[str] = set()
            out: list[dict] = []
            for group in self._catalog.groups(self._section):
                if group.count is None:
                    continue
                for item in self._catalog.items(self._section, str(group.id)) or []:
                    key = item_key(item)
                    if key not in seen:
                        seen.add(key)
                        out.append(item)
            return out

        return self._memoized(("loaded", self._section, self._catalog.content_version()), build)

    def _new_items(self) -> list[dict]:
        def build() -> list[dict]:
            dated = [(_added(i), i) for i in self._loaded_items()]
            dated = [(ts, i) for ts, i in dated if ts is not None]
            dated.sort(key=lambda pair: -pair[0])  # type: ignore[operator]
            return [i for _, i in dated[:NEW_LIMIT]]

        return self._memoized(("new", self._section, self._catalog.content_version()), build)

    def _continue_items(self) -> list[dict]:
        out: list[dict] = []
        seen: set[str] = set()
        kind = "episode" if self._section == "series" else "movie"
        for entry in self._history_entries():
            item = entry["item_data"]
            cid = self._content_id(item, kind)
            if cid in seen or self._watch.resume_position(cid) is None:
                continue
            seen.add(cid)
            out.append(item)
        return out

    def _my_list_items(self) -> list[dict]:
        favorites = set(self._favorites())
        return [i for i in self._loaded_items() if i.get("name") in favorites]

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
                out.append(Genre(gid, PINNED_TITLES[gid], len(items), True))
        return out

    # --- genres -----------------------------------------------------------------------
    def genres(self, filter_text: str = "") -> list[Genre]:
        if not self._requested:
            return []
        needle = fold(filter_text)

        def build() -> list[Genre]:
            found = self._pinned_genres() + [
                Genre(str(g.id), g.title, g.count) for g in self._catalog.groups(self._section)
            ]
            if needle:
                found = [g for g in found if needle in fold(g.title)]
            return found

        return list(self._memoized(("genres", self._section, needle, self._fingerprint()), build))

    def select_genre(self, genre_id: str) -> None:
        self._genre = genre_id
        if genre_id not in PINNED_TITLES and self._catalog.items(self._section, genre_id) is None:
            self._catalog.request_group(self._section, genre_id)
        self.changed.emit()

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
        has_added = self._memoized(key, lambda: any(_added(i) is not None for i in items))
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
            return sorted(raw, key=globals()[_SORT_KEYS[sort]])

        return list(self._memoized(key, build))

    def letter_index(self) -> dict[str, int]:
        if self._genre in PINNED_TITLES or self.sort != Sort.AZ:
            return {}
        items = self.items() or []
        index: dict[str, int] = {}
        for pos, item in enumerate(items):
            index.setdefault(_letter(item), pos)
        return index

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
        if not genre and self._genre is not None:
            genre = next((g.title for g in self.genres() if g.id == self._genre), "")
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
        )

    def is_favorite(self, item: dict) -> bool:
        return item.get("name") in self._favorites()

    def toggle_favorite(self, item: dict) -> bool:
        name = item.get("name")
        favorites = list(self._config.favorites or [])
        now = name not in favorites
        if now:
            favorites.append(name)
        else:
            favorites = [f for f in favorites if f != name]
        self._config.favorites = favorites
        try:
            self._config.save_config()
        except Exception as e:
            logger.warning("Couldn't save the favorites (%s)", type(e).__name__)
        self.changed.emit()
        return now
