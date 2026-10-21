"""A phone's Library: a section's genres as the TV lists them (its shelves first), then a genre's
titles a page at a time. GUI thread only.

Each phone browses with its own LibraryModel, so the phone leaves the TV's Library where it is,
over the TV's cache: a genre the TV sorted is not sorted again (185,000 films take a second).
The find box searches the level shown, the whole of it. Genres travel as short keys and titles
as handles: nothing a phone receives names a stream, a link or a provider's own ids.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QTimer

from services.library import Genre, LibraryCache, LibraryModel, title_of
from services.text_fold import fold
from ui.remote_snapshots import Handles

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
    ) -> None:
        super().__init__(parent)
        self.model = LibraryModel(catalog, config, watch, self, cache=cache)
        self.handles = Handles()
        self._send = send
        self._keys: dict[str, str] = {}  # genre id → key
        self._ids: dict[str, str] = {}  # key → genre id
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

    def genres(self, section: str, page: int, find: str) -> None:
        self._section(section, page)
        self._answer_page(("genres", section, find), page)

    def titles(self, section: str, genre_key: str, page: int, find: str) -> None:
        """A genre's titles; `genre_key` "" is the section's first shelf (or the genre shown)."""
        self._section(section, page)
        genre = self._ids.get(genre_key)
        model = self.model
        if genre is not None and genre != model.genre_id and genre in self._listed():
            model.select_genre(genre)
        self._answer_page(("titles", section, model.genre_id, find), page)

    def _section(self, section: str, page: int) -> None:
        model = self.model
        if not self._asked or model.section != section or (self._failed and page == 0):
            self._asked, self._failed = True, False
            model.set_section(section)  # a failed load is asked for again

    def _answer_page(self, view: tuple, page: int) -> None:
        if view != self._view:
            self._view, self._anchor = view, page
        message, signature = self._answer(page)
        if page == self._anchor:
            self._shown = signature
        self._send(message)

    # ---- what the phone sees ---------------------------------------------------------------

    def _listed(self) -> set[str]:
        return {g.id for g in self.model.genres() if not g.header}

    def _key(self, genre_id: Optional[str]) -> str:
        if genre_id is None:
            return ""
        if genre_id not in self._keys:
            key = f"g{len(self._keys) + 1}"
            self._keys[genre_id], self._ids[key] = key, genre_id
        return self._keys[genre_id]

    def _find(self) -> str:
        return str(self._view[-1]) if self._view is not None else ""

    def _answer(self, page: int) -> tuple[dict, tuple]:
        if self._view is not None and self._view[0] == "genres":
            return self._genres_page(page)
        return self._titles_page(page)

    def _genres_page(self, page: int) -> tuple[dict, tuple]:
        model, find = self.model, self._find()
        genres = model.genres(find.strip())
        loading = not model.ready and not self._failed
        shown = genres[page * GENRES_PAGE : (page + 1) * GENRES_PAGE]
        message: dict = {
            "t": "genres",
            "section": model.section,
            "find": find,
            "page": page,
            "pages": _pages(len(genres), GENRES_PAGE),
            "rows": [self._genre(g) for g in shown],
            "loading": loading,
        }
        if self._failed and not genres:
            message["failed"] = True
        signature = (
            model.section,
            tuple((g.id, g.title, g.count) for g in shown),
            message["pages"],
            loading,
            self._failed,
        )
        return message, signature

    def _genre(self, genre: Genre) -> dict:
        title = genre.title[:TITLE_CHARS]
        if genre.header:
            return {"title": title, "header": True}
        shown: dict = {"id": self._key(genre.id), "title": title}
        if genre.count is not None:
            shown["count"] = genre.count
        return shown

    def _found(self, items: list[dict], find: str) -> list[dict]:
        needle = fold(find.strip())
        if not needle:
            return items
        titles = self.model.folded_titles()  # the shared cache's, beside the sorted list
        return [i for i, title in zip(items, titles) if needle in title]

    def _titles_page(self, page: int) -> tuple[dict, tuple]:
        model, find = self.model, self._find()
        items = model.items()
        found = self._found(items, find) if items is not None else []
        shown = found[page * LIBRARY_PAGE : (page + 1) * LIBRARY_PAGE]
        genre = model.genre_id
        rows = []
        for item in shown:
            kind = model.item_kind(item)
            row: dict[str, Any] = {
                "id": self.handles.issue("title", (item, kind, items)),
                "title": title_of(item)[:TITLE_CHARS],
                "sub": str(item.get("year") or item.get("series_name") or "")[:TITLE_CHARS],
            }
            progress = model.progress(item)
            if progress is not None:
                row["progress"] = round(progress, 3)
            rows.append(row)
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
        }
        if items is None and self._failed:
            message["failed"] = True
        signature = (
            model.section,
            genre,
            message["pages"],
            message["loading"],
            self._failed,
            tuple(title_of(i) for i in shown),
        )
        return message, signature

    # ---- loads landing later ---------------------------------------------------------------

    def _changed(self) -> None:
        if self._asked:
            self._settle.start()

    def _on_failed(self, _message: str) -> None:
        self._failed = True
        self._settle.start()

    def _push(self) -> None:
        message, signature = self._answer(self._anchor)
        if signature != self._shown:
            self._shown = signature
            self._send(message)
