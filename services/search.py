"""Search everything: folded multi-term matching over provider caches (pure)."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any, overload

from PySide6.QtCore import QObject, Signal

from services.text_fold import fold

CONTENT_SECTIONS = {"itv": "live", "vod": "movies", "series": "series"}
MIN_CHARS = 2
LIMIT_PER_SECTION = 300


@dataclass(frozen=True)
class Hit:
    provider: str
    section: str
    title: str
    item: dict


def _title(item: dict) -> str:
    value = item.get("name") or item.get("title") or ""
    return value if isinstance(value, str) else str(value)


def iter_items(content: Any) -> Iterator[dict]:
    if isinstance(content, list):
        yield from (i for i in content if isinstance(i, dict))
        return
    if not isinstance(content, dict):
        return
    contents = content.get("contents")
    if isinstance(contents, list):
        yield from (i for i in contents if isinstance(i, dict))
    elif isinstance(contents, dict):
        for group in list(contents.values()):  # (the GUI thread may add a group meanwhile)
            if isinstance(group, list):
                yield from (i for i in group if isinstance(i, dict))


# (provider, section, folded title, title, item)
Row = tuple[str, str, str, str, dict]


class SearchIndex(Sequence[Row]):
    """The rows search_index looks through, folded once per cache snapshot, kept in columns.

    A tuple a title (400,000 with every provider's films, series and channels) is an object
    the cycle collector walks at each full collection for as long as the index is kept: a
    full pass went from 52 to 171 ms. Five lists are five objects."""

    __slots__ = ("providers", "sections", "folded", "titles", "items")

    def __init__(self, rows: Iterable[Row] = ()) -> None:
        self.providers: list[str] = []
        self.sections: list[str] = []
        self.folded: list[str] = []
        self.titles: list[str] = []
        self.items: list[dict] = []
        for row in rows:
            self.append(*row)

    def append(self, provider: str, section: str, folded: str, title: str, item: dict) -> None:
        self.providers.append(provider)
        self.sections.append(section)
        self.folded.append(folded)
        self.titles.append(title)
        self.items.append(item)

    def __len__(self) -> int:
        return len(self.items)

    def __iter__(self) -> Iterator[Row]:
        return zip(self.providers, self.sections, self.folded, self.titles, self.items)

    @overload
    def __getitem__(self, at: int) -> Row: ...

    @overload
    def __getitem__(self, at: slice) -> list[Row]: ...

    def __getitem__(self, at: int | slice) -> Row | list[Row]:
        if isinstance(at, slice):
            return [self[i] for i in range(*at.indices(len(self)))]
        return (
            self.providers[at],
            self.sections[at],
            self.folded[at],
            self.titles[at],
            self.items[at],
        )

    def __eq__(self, other: object) -> bool:
        if isinstance(other, (SearchIndex, list, tuple)):
            return list(self) == list(other)
        return NotImplemented

    __hash__ = None  # type: ignore[assignment]  # (equal to a list of its rows: no hash)


def build_index(caches: list[tuple[str, dict]]) -> SearchIndex:
    index = SearchIndex()
    for provider, cache in caches:
        for content_type, section in CONTENT_SECTIONS.items():
            seen: set[str] = set()
            for item in iter_items(cache.get(content_type)):
                title = _title(item)
                ident = str(item.get("id") or item.get("cmd") or title)
                if ident in seen:
                    continue
                seen.add(ident)
                index.append(provider, section, fold(title), title, item)
    return index


def search_index(
    index: Iterable[Row], text: str, *, limit_per_section: int = LIMIT_PER_SECTION
) -> list[Hit]:
    """The hits of `text` in `index` (a SearchIndex, or its rows as tuples)."""
    words = fold(text).split()
    if sum(len(w) for w in words) < MIN_CHARS:
        return []
    first_word = words[0]
    hits: list[Hit] = []
    first: list[Hit] = []
    rest: list[Hit] = []
    current: tuple[str, str] = ("", "")

    def flush() -> None:
        hits.extend((first + rest)[:limit_per_section])
        first.clear()
        rest.clear()

    for provider, section, folded, title, item in index:
        if (provider, section) != current:
            flush()
            current = (provider, section)
        if len(first) >= limit_per_section or not all(w in folded for w in words):
            continue
        (first if folded.startswith(first_word) else rest).append(
            Hit(provider, section, title, item)
        )
    flush()
    return hits


class SearchWorker(QObject):
    """JobRunner worker: gathers every provider's cached content (a catalog's own in memory, else
    the disk) unless given, then searches."""

    finished = Signal(object)  # (caches, index, hits)
    error = Signal(str)

    def __init__(self, provider_manager, text: str, caches=None, index=None, loaded=None):
        super().__init__()
        self._provider_manager = provider_manager
        self._text = text
        self._caches = caches
        self._index = index
        self._loaded = loaded  # provider name -> content its catalog holds (read only)

    def run(self) -> None:
        caches = self._caches
        index = self._index
        if index is None:
            if caches is None:
                if self._loaded:
                    caches = self._provider_manager.get_all_providers_cached_content(self._loaded)
                else:
                    caches = self._provider_manager.get_all_providers_cached_content()
            index = build_index(caches)
        self.finished.emit((caches, index, search_index(index, self._text)))
