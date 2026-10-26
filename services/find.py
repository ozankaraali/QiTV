"""The channel list's find: live genres and channels at every provider (pure, and its worker).

The index is made in a worker from every provider's cached catalog (a catalog in memory is read
instead of its file), once per snapshot: each provider's genres with their channel counts, and
its channels' titles, folded, with anything but letters and digits made a space. Each find
runs in the worker too, and ranks what it finds:

1. how well it matches: every word typed is a whole word of the title, else every one starts a
   word of it, else it is only somewhere in it ("moss 1" puts "Moss 1 (1440p)" before
   "Moss 2 (1440p)");
2. its provider: the one the list shows, then those the user watched a channel of most
   recently, then the providers' order in Settings (`Preferences`);
3. a channel the user starred or watched lately;
4. the provider's own order.

At most GENRE_LIMIT genres are kept, and CHANNEL_LIMIT channels of each provider, once ranked.
"""

from __future__ import annotations

from array import array
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
import logging
import time
from typing import Any, Callable, Optional, TypeVar

from PySide6.QtCore import QObject, Signal

from services.catalog import ALL_GROUP_ID
from services.navigation import item_key
from services.provider_ids import ALL_PROVIDERS
from services.search import _title, iter_items
from services.text_fold import fold, fold_all

logger = logging.getLogger(__name__)

GENRE_LIMIT = 100  # genres shown, at most
CHANNEL_LIMIT = 200  # channels shown per provider, at most
WATCHED_LIMIT = 20  # the channels watched lately that rank first (as many as ⟲ Recent lists)
_LINE = ord("\n")
_NO_GENRE = 0xFFFF  # (a row's genre, in `_Channels.genre_of`)


@dataclass(frozen=True)
class FoundGenre:
    provider: str
    group_id: str  # the provider's own id
    title: str
    count: Optional[int]  # None: an STB portal's genre whose channels were never fetched


@dataclass(frozen=True)
class FoundChannel:
    provider: str
    title: str
    item: dict
    group_id: str  # the first of its provider's genres that lists it ("" when none does)


_Found = TypeVar("_Found", FoundGenre, FoundChannel)


@dataclass(frozen=True)
class _Channels:
    provider: str
    spaced: list[str]  # each title as `words()` makes it, a space before and after
    items: list[dict]
    genres: list[str]  # the provider's genre ids, as listed
    genre_of: array  # each row's first genre (an index into `genres`; _NO_GENRE: none)


@dataclass(frozen=True)
class FindIndex:
    genres: list[tuple[str, FoundGenre]]  # (spaced title, genre), the providers in order
    channels: list[_Channels]


@dataclass(frozen=True)
class Preferences:
    """What ranks the user's own first (plain data, made on the GUI thread)."""

    providers: tuple[str, ...] = ()  # the order of their results; others after, as listed
    watched: frozenset[tuple[str, str]] = frozenset()  # (provider, item key) watched lately
    favorite: Optional[Callable[[dict], bool]] = field(default=None, compare=False)


def preferences_for(
    providers: list[str],
    browsed: str,
    history: list[dict],
    favorite: Optional[Callable[[dict], bool]] = None,
) -> Preferences:
    """The provider the list shows first (none when it shows them all), then those of the
    channels watched most recently, then the rest as listed; the channels watched lately; the
    stars (`favorites.matcher`). On the GUI thread: plain data for the worker."""
    order = [browsed] if browsed in providers else []
    watched: set[tuple[str, str]] = set()
    for entry in history or []:
        provider = entry.get("provider_name")
        if entry.get("item_type") != "channel" or provider not in providers:
            continue
        if provider not in order:
            order.append(provider)
        if len(watched) < WATCHED_LIMIT:
            watched.add((provider, item_key(entry.get("item_data") or {})))
    return Preferences(tuple(order), frozenset(watched), favorite)


class _Gaps(dict):
    """A str.translate table: anything but a letter, a digit or a line break is a space."""

    def __missing__(self, code: int) -> int:
        kept = code if code == _LINE or chr(code).isalnum() else 32
        self[code] = kept
        return kept


_GAPS = _Gaps()


def words(text: str) -> list[str]:
    """The words of `text` as the find reads them: folded runs of letters and digits."""
    return fold(text).translate(_GAPS).split()


def _spaced(titles: list[str]) -> list[str]:
    """Each title's words, a space between them and around them (" tr spor kanali ")."""
    folded = fold_all(titles)
    joined = "\n".join(folded).translate(_GAPS).split("\n")
    if len(joined) != len(folded):  # a title with a line break of its own
        joined = [title.translate(_GAPS).replace("\n", " ") for title in folded]
    return [f" {' '.join(title.split())} " for title in joined]


def matches(title: str, typed: list[str]) -> bool:
    """Whether a title has every word typed (`words()`), whole or in part."""
    spaced = f" {' '.join(words(title))} "
    return all(w in spaced for w in typed)


def live_genres(provider: str, content: Any) -> list[FoundGenre]:
    """A provider's live genres and their channel counts, as its catalog lists them (the "All"
    group and empty genres aside)."""
    if not isinstance(content, dict):
        return []
    contents = content.get("contents")
    sorted_map = content.get("sorted_channels")
    genres = []
    for category in content.get("categories") or []:
        if not isinstance(category, dict):
            continue
        group_id = str(category.get("id", ""))
        if group_id == ALL_GROUP_ID:
            continue
        title = str(category.get("title") or category.get("name") or group_id)
        count: Optional[int] = None
        if isinstance(sorted_map, dict):
            count = len(sorted_map.get(group_id, []))
        elif isinstance(contents, dict):
            loaded = contents.get(group_id)
            count = len(loaded) if isinstance(loaded, list) else None
        if count == 0:
            continue
        genres.append(FoundGenre(provider, group_id, title, count))
    return genres


def build_find_index(caches: list[tuple[str, dict]]) -> FindIndex:
    """The index of every provider's live genres and channels (in the worker)."""
    genres: list[tuple[str, FoundGenre]] = []
    channels: list[_Channels] = []
    for provider, cache in caches:
        live = cache.get("itv")
        found = live_genres(provider, live)
        genres += zip(_spaced([g.title for g in found]), found)
        rows: dict[str, int] = {}
        items: list[dict] = []
        for item in iter_items(live):
            ident = _ident(item)
            if ident not in rows:
                rows[ident] = len(items)
                items.append(item)
        genre_of = _genre_rows(live, found, rows, len(items))
        ids = [g.group_id for g in found]
        channels.append(
            _Channels(provider, _spaced([_title(i) for i in items]), items, ids, genre_of)
        )
    return FindIndex(genres, channels)


def _ident(item: dict) -> str:
    return str(item.get("id") or item.get("cmd") or _title(item))


def _genre_rows(live: Any, genres: list[FoundGenre], rows: dict[str, int], count: int) -> array:
    """Each channel's first genre (an index into `genres`), for a found one to play in it."""
    genre_of = array("H", [_NO_GENRE]) * count
    if not isinstance(live, dict) or len(genres) >= _NO_GENRE:
        return genre_of
    contents, sorted_map = live.get("contents"), live.get("sorted_channels")
    for at, genre in enumerate(genres):
        if isinstance(sorted_map, dict) and isinstance(contents, list):
            members = [
                contents[i] for i in sorted_map.get(genre.group_id, []) if 0 <= i < len(contents)
            ]
        elif isinstance(contents, dict):
            members = contents.get(genre.group_id) or []
        else:
            return genre_of
        for item in members:
            row = rows.get(_ident(item)) if isinstance(item, dict) else None
            if row is not None and genre_of[row] == _NO_GENRE:
                genre_of[row] = at
    return genre_of


def _tiers(spaced: list[str], rows: list[int], typed: list[str]) -> tuple[list[int], ...]:
    """`rows` (those with every word typed) by how well they match: every word whole, every
    word the start of one, the rest; each in the list's order."""
    whole, starts = list(rows), list(rows)
    for w in typed:
        whole = [r for r in whole if f" {w} " in spaced[r]]
        starts = [r for r in starts if f" {w}" in spaced[r]]
    first = set(whole)
    second = [r for r in starts if r not in first]
    taken = first.union(second)
    return whole, second, [r for r in rows if r not in taken]


def _provider_rank(preferences: Preferences, providers: list[str]) -> dict[str, int]:
    order = list(preferences.providers) + [p for p in providers if p not in preferences.providers]
    return {name: at for at, name in enumerate(order)}


def find_genres(
    index: FindIndex, text: str, preferences: Preferences = Preferences(), limit: int = GENRE_LIMIT
) -> list[FoundGenre]:
    """The genres whose title has every word typed, ranked (see the module), `limit` at most."""
    typed = words(text)
    if not typed:
        return []
    spaced = [s for s, _genre in index.genres]
    rows = [r for r, s in enumerate(spaced) if all(w in s for w in typed)]
    rank = _provider_rank(preferences, [part.provider for part in index.channels])
    ranked = []
    for tier, members in enumerate(_tiers(spaced, rows, typed)):
        ranked += [(tier, rank.get(index.genres[r][1].provider, len(rank)), r) for r in members]
    ranked.sort()
    return [index.genres[r][1] for _tier, _provider, r in ranked[:limit]]


def find_channels(
    index: FindIndex,
    text: str,
    preferences: Preferences = Preferences(),
    limit: int = CHANNEL_LIMIT,
) -> list[FoundChannel]:
    """Each provider's channels whose title has every word typed, `limit` at most of each,
    ranked (see the module)."""
    typed = words(text)
    if not typed:
        return []
    rank = _provider_rank(preferences, [part.provider for part in index.channels])
    favorite, watched = preferences.favorite, preferences.watched
    ranked: list[tuple[int, int, int, FoundChannel]] = []
    for part in index.channels:
        spaced, items, provider = part.spaced, part.items, part.provider

        def liked(row: int) -> bool:
            item = items[row]
            if favorite is not None and favorite(item):
                return True
            return bool(watched) and (provider, item_key(item)) in watched

        rows = [r for r, s in enumerate(spaced) if typed[0] in s]
        for w in typed[1:]:
            rows = [r for r in rows if w in spaced[r]]
        taken: list[tuple[int, int]] = []
        for tier, members in enumerate(_tiers(spaced, rows, typed)):
            room = limit - len(taken)
            if room <= 0:
                break
            if favorite is not None or watched:
                first = [r for r in members if liked(r)]
                if first:
                    picked = set(first)
                    members = first + [r for r in members if r not in picked]
            taken += [(tier, r) for r in members[:room]]
        at = rank.get(provider, len(rank))
        for position, (tier, r) in enumerate(taken):
            genre = part.genre_of[r]
            group = part.genres[genre] if genre != _NO_GENRE else ""
            hit = FoundChannel(provider, _title(items[r]), items[r], group)
            ranked.append((tier, at, position, hit))
    ranked.sort(key=lambda entry: entry[:3])
    return [hit for _tier, _provider, _position, hit in ranked]


def by_scope(
    found: Sequence[_Found], provider: str, own: Collection[tuple[str, str]] = ()
) -> list[_Found]:
    """What a find found (genres or channels, ranked) widening as the lists do, phone and
    desktop alike: the open group's channels first (`own`: their providers and item keys),
    then those of the provider the list shows (`provider`; every one for ALL_PROVIDERS or
    ""), then every provider's; each keeps its rank."""

    def scope(hit: _Found) -> int:
        item = getattr(hit, "item", None)
        if own and isinstance(item, dict) and (hit.provider, item_key(item)) in own:
            return 0
        if provider in ("", ALL_PROVIDERS) or hit.provider == provider:
            return 1
        return 2

    return sorted(found, key=scope)  # (stable: each keeps its rank)


class FindWorker(QObject):
    """JobRunner worker: makes the index unless given one (every provider's cached content,
    the catalogs' own in memory, else the disk), then finds and ranks the genres and the
    channels."""

    finished = Signal(object)  # (index, genres, channel hits)
    error = Signal(str)

    def __init__(
        self,
        provider_manager,
        text: str,
        index=None,
        loaded=None,
        preferences: Preferences = Preferences(),
    ):
        super().__init__()
        self._provider_manager = provider_manager
        self._text = text
        self._index = index
        self._loaded = loaded  # provider name -> content its catalog holds (read only)
        self._preferences = preferences

    def run(self) -> None:
        index = self._index
        if index is None:
            caches = self._provider_manager.get_all_providers_cached_content(self._loaded or None)
            index = build_find_index(caches)
        started = time.perf_counter()
        genres = find_genres(index, self._text, self._preferences)
        channels = find_channels(index, self._text, self._preferences)
        logger.debug("A find took %.0f ms", (time.perf_counter() - started) * 1000)
        self.finished.emit((index, genres, channels))
