"""A phone's Library: a section's genres as the TV lists them (its shelves first), then a genre's
titles a page at a time. GUI thread only.

Each phone browses with its own LibraryModel, so the phone leaves the TV's Library where it is,
over the TV's cache: a genre the TV sorted is not sorted again (a large one takes a second).
The find box searches the level shown, the whole of it, and goes on with the desktop Library's
search (services.search through a SearchSession the phones share): every provider's films or
series from their saved lists, read in a worker, the shown provider's first; after the genres
it matches, or after the genre's own titles. Genres travel as short keys and titles
as handles: nothing a phone receives names a stream, a link or a provider's own ids.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QTimer

from services.library import Genre, LibraryCache, LibraryModel, title_of
from services.navigation import item_key
from services.provider_ids import ALL_PROVIDERS, provider_of
from services.search import Hit, search_index
from services.text_fold import fold
from ui.remote_snapshots import Handles, stable_key

GENRES_PAGE = 100
LIBRARY_PAGE = 50
TITLE_CHARS = 80
SETTLE_MS = 250  # loads arriving together (several providers) answer once


def _pages(count: int, size: int) -> int:
    return max(1, -(-count // size))


class PhoneLibrary(QObject):
    """`send(message)` reaches this phone. Each ask answers at once; a load that lands later
    sends the view's first page again when what it shows changed."""

    def __init__(
        self,
        catalog: Any,
        config: Any,
        watch: Any,
        send: Callable[[dict], None],
        parent=None,
        cache: Optional[LibraryCache] = None,
        searcher: Any = None,
        recordings: Any = None,
    ) -> None:
        super().__init__(parent)
        self.model = LibraryModel(catalog, config, watch, self, cache=cache, recordings=recordings)
        self._recordings = recordings
        self._catalog = catalog
        # The desktop Library's search, shared: what a find reaches past the level shown.
        self._searcher = searcher
        if searcher is not None:
            searcher.changed.connect(self._changed)  # what it found arrived
        self._claiming = False  # answering the phone's ask: its find may take the shared search
        self.handles = Handles()
        self._send = send
        self._ids: dict[str, str] = {}  # key → genre id, the genres seen so far
        self._wanted: Optional[str] = None  # a genre asked for before its section listed it
        self._asked = False
        self._failed = False
        self._view: Optional[tuple] = None  # ("genres", find) or ("titles", find)
        self._anchor = 0  # the page the view opened on: the one sent again
        self._shown: Optional[tuple] = None  # what that page showed, as last sent
        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.setInterval(SETTLE_MS)
        self._settle.timeout.connect(self._push)
        self.model.changed.connect(self._changed)
        self.model.failed.connect(self._on_failed)

    # ---- the phone's asks ------------------------------------------------------------------

    def genres(self, section: str, page: int, find: str, n: int = -1) -> None:
        if section == "movies" and self._recordings is not None:
            self._recordings.refresh()  # the folder read again (2.8): a new recording shows
        self._section(section, page)
        self._wanted = None
        self._answer_page(("genres", section, find), page, n)

    def titles(self, section: str, genre_key: str, page: int, find: str, n: int = -1) -> None:
        """A genre's titles; `genre_key` "" is the section's first shelf (or the genre shown).
        A key the section doesn't list yet (its lists still on their way) opens once it does."""
        self._section(section, page)
        self._wanted = genre_key or None
        self._choose_wanted()
        self._answer_page(("titles", section, self.model.genre_id, find), page, n)

    def _choose_wanted(self) -> None:
        if self._wanted is None:
            return
        genre = self._genre_of(self._wanted)
        if genre is None:
            return  # not listed (yet)
        self._wanted = None
        if genre != self.model.genre_id:
            self.model.select_genre(genre)

    def _genre_of(self, key: str) -> Optional[str]:
        listed = self._listed()
        if key not in self._ids:
            for each in listed:
                self._ids[stable_key(each)] = each
        found = self._ids.get(key)
        return found if found in listed else None

    def _section(self, section: str, page: int) -> None:
        model = self.model
        if not self._asked or model.section != section or (self._failed and page == 0):
            self._asked, self._failed = True, False
            model.set_section(section)  # a failed load is asked for again

    def _answer_page(self, view: tuple, page: int, n: int = -1) -> None:
        if view != self._view:
            self._view, self._anchor = view, page
        self._claiming = True
        try:
            message, signature = self._answer(page)
        finally:
            self._claiming = False
        if page == self._anchor:
            self._shown = signature
        if n >= 0:
            message["n"] = n  # the ask it answers; a later push carries none
        self._send(message)

    def finding(self) -> bool:
        """Whether what this Library shows is a find (the shared search stays while one is)."""
        return self._view is not None and bool(str(self._view[-1]).strip())

    # ---- what the phone sees ---------------------------------------------------------------

    def _listed(self) -> set[str]:
        return {g.id for g in self.model.genres() if not g.header}

    def _key(self, genre_id: Optional[str]) -> str:
        if genre_id is None:
            return ""
        key = stable_key(genre_id)
        self._ids[key] = genre_id
        return key

    def _find(self) -> str:
        return str(self._view[-1]) if self._view is not None else ""

    def _answer(self, page: int) -> tuple[dict, tuple]:
        if self._view is not None and self._view[0] == "genres":
            return self._genres_page(page)
        return self._titles_page(page)

    def _genres_page(self, page: int) -> tuple[dict, tuple]:
        """The genres; with a find, the genres it matches on the first page and the section's
        titles it finds (the desktop search's rules) a page at a time after them."""
        model, find = self.model, self._find()
        genres = model.genres(find.strip())
        hits, searching = self._everywhere(find)
        if hits:
            shown = genres[:GENRES_PAGE] if page == 0 else []
            pages = _pages(len(hits), LIBRARY_PAGE)
        else:
            shown = genres[page * GENRES_PAGE : (page + 1) * GENRES_PAGE]
            pages = _pages(len(genres), GENRES_PAGE)
        found = [hit.item for hit in hits[page * LIBRARY_PAGE : (page + 1) * LIBRARY_PAGE]]
        loading = not model.ready and not self._failed
        message: dict = {
            "t": "genres",
            "section": model.section,
            "find": find,
            "page": page,
            "pages": pages,
            "rows": [self._genre(g) for g in shown],
            "titles": [self._found_row(item) for item in found],
            "found": len(hits),
            "loading": loading,
            "searching": searching,  # every provider's, still on its way
        }
        if self._failed and not genres:
            message["failed"] = True
        signature = (
            model.section,
            tuple((g.id, g.title, g.count) for g in shown),
            tuple(title_of(i) for i in found),
            pages,
            loading,
            searching,
            self._failed,
        )
        return message, signature

    def _found_row(self, item: dict) -> dict:
        """A title the find found in the section: a film or a series whatever genre the model
        is on (on Continue in Series its titles are episodes)."""
        if self.model.section == "series":
            return self._title_row(item, None, "series")
        return self._title_row(item, None, "movie")

    def _title_row(
        self, item: dict, genre_items: Optional[list], kind: Optional[str] = None
    ) -> dict:
        model = self.model
        kind = kind or model.item_kind(item)
        recorded = item.get("description") if kind == "recording" else ""  # when, how big (2.8)
        row: dict[str, Any] = {
            "id": self.handles.issue("title", (item, kind, genre_items)),
            "title": title_of(item)[:TITLE_CHARS],
            "sub": str(item.get("year") or item.get("series_name") or recorded or "")[:TITLE_CHARS],
        }
        progress = model.progress(item) if kind == model.item_kind(item) else None
        if progress is not None:  # (a series found on Continue has none of its own)
            row["progress"] = round(progress, 3)
        if kind in ("movie", "episode"):  # it can play on the phone (a recording: on the TV)
            row["plays"] = True
        return row

    def _genre(self, genre: Genre) -> dict:
        title = genre.title[:TITLE_CHARS]
        if genre.header:
            return {"title": title, "header": True}
        shown: dict = {"id": self._key(genre.id), "title": title}
        if genre.count is not None:
            shown["count"] = genre.count
        return shown

    def _found(self, items: list[dict], find: str) -> list[dict]:
        words = fold(find).split()  # every word, in any order: as the desktop search
        if not words:
            return items
        titles = self.model.folded_titles()  # the shared cache's, beside the sorted list
        return [i for i, title in zip(items, titles) if all(w in title for w in words)]

    def _titles_page(self, page: int) -> tuple[dict, tuple]:
        model, find = self.model, self._find()
        items = model.items()
        found = self._found(items, find) if items is not None else []
        own = len(found)  # the genre's own; the others the find finds follow them
        searching = False
        if items is not None and find.strip():
            listed = {(provider_of(i), item_key(i)) for i in items}
            hits, searching = self._everywhere(find)
            found = found + [
                h.item for h in hits if (provider_of(h.item), item_key(h.item)) not in listed
            ]
        start = page * LIBRARY_PAGE
        shown = found[start : start + LIBRARY_PAGE]
        genre = model.genre_id
        rows = [
            self._title_row(item, items) if at < own else self._found_row(item)
            for at, item in enumerate(shown, start)
        ]
        title = next((g.title for g in model.genres() if g.id == genre), "")
        message: dict = {
            "t": "library",
            "section": model.section,
            "genre": self._key(genre),
            "title": title[:TITLE_CHARS],
            "count": len(found),
            "find": find,
            "page": page,
            "pages": _pages(len(found), LIBRARY_PAGE),
            "items": rows,
            "loading": items is None and not self._failed,
            "searching": searching,
        }
        if items is None and self._failed:
            message["failed"] = True
        signature = (
            model.section,
            genre,
            message["pages"],
            message["loading"],
            searching,
            self._failed,
            tuple(title_of(i) for i in shown),
        )
        return message, signature

    # ---- past the level shown: the desktop Library's search --------------------------------

    def _everywhere(self, find: str) -> tuple[list[Hit], bool]:
        """The desktop search's films or series (the section shown) at every provider for
        `find`, the shown provider's first, and whether they are still on their way: meanwhile
        the shown provider's own, found here at once."""
        if not find.strip():
            return [], False
        local = search_index(self.model.search_index(), find)  # the shown provider's, at once
        searcher = self._searcher
        if searcher is None:
            return local, False
        if searcher.query != find:
            if not self._claiming:  # another search holds it (the phone's Search, say)
                return local, False
            searcher.set_query(find)
        if searcher.searching or searcher.hits is None:
            return local, True  # every provider's follow
        words = fold(find).split()
        section = self.model.section
        hits = [
            hit
            for hit in searcher.hits or []
            if hit.section == section and all(w in fold(hit.title) for w in words)
        ]
        shown = _shown(self._catalog)
        if shown and shown != ALL_PROVIDERS:
            hits.sort(key=lambda hit: hit.provider != shown)  # stable: each keeps its order
        return hits, bool(searcher.searching)

    # ---- loads landing later ---------------------------------------------------------------

    def _changed(self) -> None:
        if self._asked:
            self._settle.start()

    def _on_failed(self, _message: str) -> None:
        self._failed = True
        self._settle.start()

    def _push(self) -> None:
        if self._view is not None and self._view[0] == "titles" and self._wanted:
            self._choose_wanted()  # the genre asked for is listed now
            self._view = (*self._view[:2], self.model.genre_id, self._view[3])
        message, signature = self._answer(self._anchor)
        if signature != self._shown:
            self._shown = signature
            self._send(message)


def _shown(catalog: Any) -> str:
    """The provider `catalog` shows (ALL_PROVIDERS for all of them)."""
    source = getattr(catalog, "source", None)
    return str(source() if callable(source) else catalog.current_provider_name())
