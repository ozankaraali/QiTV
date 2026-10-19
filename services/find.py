"""The channel list's find: live genres and channels at every provider (pure, and its worker).

The index is made in a worker from every provider's cached catalog (a catalog in memory is read
instead of its file), once per snapshot: each provider's genres with their channel counts, and
its channels' folded titles. The genres are few (a few thousand at four providers): they are
matched on the GUI thread as the user types. The channels (200,000 and more) are matched in
the worker.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from PySide6.QtCore import QObject, Signal

from services.catalog import ALL_GROUP_ID
from services.search import Hit, _title, iter_items
from services.text_fold import fold, fold_all

GENRE_LIMIT = 100  # genres shown, at most
CHANNEL_LIMIT = 200  # channels shown per provider, at most


@dataclass(frozen=True)
class FoundGenre:
    provider: str
    group_id: str  # the provider's own id
    title: str
    count: Optional[int]  # None: an STB portal's genre whose channels were never fetched


@dataclass(frozen=True)
class _Channels:
    provider: str
    folded: list[str]
    items: list[dict]


@dataclass(frozen=True)
class FindIndex:
    genres: list[tuple[str, FoundGenre]]  # (folded title, genre), the providers in order
    channels: list[_Channels]


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
        genres += [(fold(g.title), g) for g in live_genres(provider, live)]
        seen: set[str] = set()
        items: list[dict] = []
        for item in iter_items(live):
            ident = str(item.get("id") or item.get("cmd") or _title(item))
            if ident not in seen:
                seen.add(ident)
                items.append(item)
        channels.append(_Channels(provider, fold_all([_title(i) for i in items]), items))
    return FindIndex(genres, channels)


def _words(text: str) -> list[str]:
    return fold(text).split()


def find_genres(index: FindIndex, text: str, limit: int = GENRE_LIMIT) -> list[FoundGenre]:
    """The genres whose title has every word of `text`: those that start with the first word
    first, each part in the providers' order (on the GUI thread: a few thousand titles)."""
    words = _words(text)
    if not words:
        return []
    first: list[FoundGenre] = []
    rest: list[FoundGenre] = []
    for folded, genre in index.genres:
        if all(w in folded for w in words):
            (first if folded.startswith(words[0]) else rest).append(genre)
    return (first + rest)[:limit]


def find_channels(index: FindIndex, text: str, limit: int = CHANNEL_LIMIT) -> list[Hit]:
    """Each provider's channels whose title has every word of `text`, up to `limit` each:
    those that start with the first word first (in the worker: 200,000 titles)."""
    words = _words(text)
    if not words:
        return []
    head, others = words[0], words[1:]
    hits: list[Hit] = []
    for part in index.channels:
        folded = part.folded
        rows = [row for row, title in enumerate(folded) if head in title]
        if others:
            rows = [row for row in rows if all(w in folded[row] for w in others)]
        first = [row for row in rows if folded[row].startswith(head)]
        rest = [row for row in rows if not folded[row].startswith(head)]
        for row in (first + rest)[:limit]:
            item = part.items[row]
            hits.append(Hit(part.provider, "live", _title(item), item))
    return hits


class FindWorker(QObject):
    """JobRunner worker: makes the index unless given one (every provider's cached content,
    the catalogs' own in memory, else the disk), then finds the channels."""

    finished = Signal(object)  # (index, channel hits)
    error = Signal(str)

    def __init__(self, provider_manager, text: str, index=None, loaded=None):
        super().__init__()
        self._provider_manager = provider_manager
        self._text = text
        self._index = index
        self._loaded = loaded  # provider name -> content its catalog holds (read only)

    def run(self) -> None:
        index = self._index
        if index is None:
            caches = self._provider_manager.get_all_providers_cached_content(self._loaded or None)
            index = build_find_index(caches)
        self.finished.emit((index, find_channels(index, self._text)))
