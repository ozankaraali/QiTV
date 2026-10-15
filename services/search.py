"""Search everything: folded multi-term matching over provider caches (pure)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator

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
        for group in contents.values():
            if isinstance(group, list):
                yield from (i for i in group if isinstance(i, dict))


# (provider, section, folded title, title, item), folded once per cache snapshot
SearchIndex = list[tuple[str, str, str, str, dict]]


def build_index(caches: list[tuple[str, dict]]) -> SearchIndex:
    index: SearchIndex = []
    for provider, cache in caches:
        for content_type, section in CONTENT_SECTIONS.items():
            seen: set[str] = set()
            for item in iter_items(cache.get(content_type)):
                title = _title(item)
                ident = str(item.get("id") or item.get("cmd") or title)
                if ident in seen:
                    continue
                seen.add(ident)
                index.append((provider, section, fold(title), title, item))
    return index


def search_index(
    index: SearchIndex, text: str, *, limit_per_section: int = LIMIT_PER_SECTION
) -> list[Hit]:
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


def search_caches(
    caches: list[tuple[str, dict]], text: str, *, limit_per_section: int = LIMIT_PER_SECTION
) -> list[Hit]:
    return search_index(build_index(caches), text, limit_per_section=limit_per_section)


class SearchWorker(QObject):
    """JobRunner worker: loads every provider's cached content (disk) unless given, then searches."""

    finished = Signal(object)  # (caches, index, hits)
    error = Signal(str)

    def __init__(self, provider_manager, text: str, caches=None, index=None):
        super().__init__()
        self._provider_manager = provider_manager
        self._text = text
        self._caches = caches
        self._index = index

    def run(self) -> None:
        caches = self._caches
        index = self._index
        if index is None:
            if caches is None:
                caches = self._provider_manager.get_all_providers_cached_content()
            index = build_index(caches)
        self.finished.emit((caches, index, search_index(index, self._text)))
