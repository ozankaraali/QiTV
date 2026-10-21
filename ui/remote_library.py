"""A phone's Library: Movies and Series by genre, a page at a time, as the TV lists them.

Each phone browses with its own LibraryModel (the TV's shelves, genres and order), so scrolling on
the phone leaves the TV's Library where it is. Genres travel as short keys and titles as handles:
nothing a phone receives names a stream, a link or a provider's own ids. GUI thread only.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QTimer

from services.library import Genre, LibraryModel, title_of
from ui.remote_snapshots import Handles, fit_entries

LIBRARY_PAGE = 50
TITLE_CHARS = 80
SETTLE_MS = 250  # loads arriving together (several providers) answer once


class PhoneLibrary(QObject):
    """`send(message)` reaches this phone; `ask` answers at once, a load that lands later
    pushes the first page again when what it shows changed."""

    def __init__(
        self, catalog: Any, config: Any, watch: Any, send: Callable[[dict], None], parent=None
    ) -> None:
        super().__init__(parent)
        self.model = LibraryModel(catalog, config, watch, self)
        self.handles = Handles()
        self._send = send
        self._keys: dict[str, str] = {}  # genre id → key
        self._ids: dict[str, str] = {}  # key → genre id
        self._asked = False
        self._failed = False
        self._shown: Optional[tuple] = None  # what the first page showed, as last sent
        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.setInterval(SETTLE_MS)
        self._settle.timeout.connect(self._push)
        self.model.changed.connect(self._changed)
        self.model.failed.connect(self._on_failed)

    def ask(self, section: str, genre_key: str, page: int) -> None:
        model = self.model
        if not self._asked or model.section != section or (self._failed and page == 0):
            self._asked, self._failed = True, False
            model.set_section(section)  # a failed load is asked for again
        genre = self._ids.get(genre_key)
        if genre is not None and genre != model.genre_id and genre in self._listed():
            model.select_genre(genre)
        view = self._view(page)
        if page == 0:
            self._shown = _signature(view)
        self._send(self._message(view))

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

    def _view(self, page: int) -> dict:
        model = self.model
        items = model.items()
        shown = items or []
        return {
            "section": model.section,
            "genre": model.genre_id,
            "genres": model.genres() if page == 0 else None,
            "page": page,
            "pages": max(1, -(-len(shown) // LIBRARY_PAGE)),
            "items": shown[page * LIBRARY_PAGE : (page + 1) * LIBRARY_PAGE],
            "all": items,
            "loading": items is None and not self._failed,
            "failed": items is None and self._failed,
        }

    def _message(self, view: dict) -> dict:
        model = self.model
        rows = []
        for item in view["items"]:
            kind = model.item_kind(item)
            row: dict[str, Any] = {
                "id": self.handles.issue("title", (item, kind, view["all"])),
                "title": title_of(item)[:TITLE_CHARS],
                "sub": str(item.get("year") or item.get("series_name") or "")[:TITLE_CHARS],
            }
            progress = model.progress(item)
            if progress is not None:
                row["progress"] = round(progress, 3)
            rows.append(row)
        message: dict = {
            "t": "library",
            "section": view["section"],
            "genre": self._key(view["genre"]),
            "page": view["page"],
            "pages": view["pages"],
            "items": rows,
            "loading": view["loading"],
        }
        if view["failed"]:
            message["failed"] = True
        if view["genres"] is not None:
            genres = [self._genre(g) for g in view["genres"]]
            message["genres"] = fit_entries(message, genres, message["genre"])
        return message

    def _genre(self, genre: Genre) -> dict:
        title = genre.title[:TITLE_CHARS]
        if genre.header:
            return {"title": title, "header": True}
        shown: dict = {"id": self._key(genre.id), "title": title}
        if genre.count is not None:
            shown["count"] = genre.count
        return shown

    # ---- loads landing later ---------------------------------------------------------------

    def _changed(self) -> None:
        if self._asked:
            self._settle.start()

    def _on_failed(self, _message: str) -> None:
        self._failed = True
        self._settle.start()

    def _push(self) -> None:
        view = self._view(0)
        signature = _signature(view)
        if signature != self._shown:
            self._shown = signature
            self._send(self._message(view))


def _signature(view: dict) -> tuple:
    genres = tuple((g.id, g.title, g.count) for g in view["genres"] or ())
    titles = tuple(title_of(item) for item in view["items"])
    flags = (view["section"], view["genre"], view["pages"], view["loading"], view["failed"])
    return flags + (genres, titles)
