"""The Library screen: top nav, genre column, poster grid or series view (spec §5.4).

Mockup 05, option 1. 1 u = 1% of this screen's width. The screen only lays its parts out and
paints the search field; the LibraryController decides what they show and routes the keys.
"""

import logging

from PySide6.QtCore import QObject, QRectF, Qt, Signal
from PySide6.QtGui import QPainter, QPen, QPixmap
from PySide6.QtWidgets import QWidget

from services.i18n import tr
from ui.library.genre_list import GenreList
from ui.library.poster_grid import PosterGrid
from ui.library.series_view import SeriesView
from ui.library.top_nav import TopNav
from ui.overlays.painting import Units, draw_field_icon, elided, fill_round
from ui.text_edit import SELECTION_ALPHA, Dragging, TextEdit, paint_edit
from ui.theme import FOCUS_FILL_ALPHA, PALETTE, qcolor

logger = logging.getLogger(__name__)

MODES = ("browse", "series", "search")
DEFAULT_HOST_WIDTH = 1280
LIVE_TILE = "live-tile:"  # routes a grid URL to the channel logo cache
FIELD_FONT_U = 1.15
FOCUS_RING_PX = 1.5


class GridPosters(QObject):
    """The grid's image source: posters, and live channel logos from a separate cache.

    With a separate logo cache, a logo's grid URL is LIVE_TILE + its URL (`logo_key`), which
    routes its request and its pixmap to that cache. When one cache serves both, URLs pass
    through unchanged. The grid draws channel logos itself (PosterGrid.set_tile_provider).
    """

    logoReady = Signal(str)

    def __init__(self, posters, logos=None, parent=None) -> None:
        super().__init__(parent)
        self._posters = posters
        self._logos = logos if logos is not None else posters
        self._shared = self._logos is self._posters
        if posters is not None:
            posters.logoReady.connect(self.logoReady)
        if not self._shared and self._logos is not None:
            self._logos.logoReady.connect(self._on_logo_ready)

    def logo_key(self, url: str) -> str:
        """The grid URL of a channel logo."""
        return url if self._shared else LIVE_TILE + url

    def pixmap(self, url: str) -> QPixmap | None:
        if url.startswith(LIVE_TILE):
            logos = self._logos
            return logos.pixmap(url[len(LIVE_TILE) :]) if logos is not None else None
        return self._posters.pixmap(url) if self._posters is not None else None

    def failed(self, url: str) -> bool:
        """Whether a poster could not be loaded (a channel logo never counts: TMDB has none)."""
        failed = getattr(self._posters, "failed", None)
        return not url.startswith(LIVE_TILE) and callable(failed) and bool(failed(url))

    def request(self, urls: list[str], replace_pending: bool = False) -> None:
        if self._shared:
            both = [u[len(LIVE_TILE) :] if u.startswith(LIVE_TILE) else u for u in urls]
            if both and self._posters is not None:
                self._posters.request(list(dict.fromkeys(both)), replace_pending=replace_pending)
            return
        # Each cache is asked only for its own URLs, so one never drops the other's queue.
        posters = [u for u in urls if not u.startswith(LIVE_TILE)]
        logos = [u[len(LIVE_TILE) :] for u in urls if u.startswith(LIVE_TILE)]
        if posters and self._posters is not None:
            self._posters.request(posters, replace_pending=replace_pending)
        if logos and self._logos is not None:
            self._logos.request(logos, replace_pending=replace_pending)

    def _on_logo_ready(self, url: str) -> None:
        self.logoReady.emit(LIVE_TILE + url)


class SearchField(QWidget):
    """The Library's search field, in the top bar (2.9.3: it was a "Search" pill there and a
    field over the grid, so the genre filter looked like the place to type; the owner: "i kinda
    default to genre search instead of library search"). The top bar paints its look while the
    Library browses; this field shows over it while it searches: the query, its caret and
    selection (ui.text_edit). A click on it, or typing, searches."""

    clicked = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.NoFocus)
        self.setAccessibleName(tr("Search movies, series and channels"))  # as its placeholder
        self.setCursor(Qt.IBeamCursor)
        self.edit = TextEdit()
        self._drag = Dragging()
        self._focused = False
        self._caret = False

    @property
    def query(self) -> str:
        return self.edit.text

    def set_query(self, text: str) -> None:
        self.edit.set_text(text)
        self.setAccessibleDescription(text)  # a screen reader says what is searched
        self.update()

    def set_focused(self, focused: bool, *, caret: bool | None = None) -> None:
        """The keys' focus ring; `caret` (by default as `focused`): the keys type here."""
        self._focused = focused
        self._caret = focused if caret is None else caret
        self.update()

    def _units(self) -> Units:
        top = self.parentWidget()
        host = top.parentWidget() if top is not None else None
        width = host.width() if host is not None and host.width() > 0 else 0
        return Units(width or DEFAULT_HOST_WIDTH)

    def field_rect(self) -> QRectF:
        return QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
            if self.edit.text:
                shift = bool(event.modifiers() & Qt.ShiftModifier)
                self._drag.press(self.edit, event.position().x(), shift=shift)
            self.update()

    def mouseMoveEvent(self, event) -> None:
        if event.buttons() & Qt.LeftButton and self._drag.move(event.position().x()):
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        self._drag.release()

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self.edit.text:
            self._drag.double(self.edit, event.position().x())
            self.update()

    def paintEvent(self, event) -> None:
        units = self._units()
        u = units.u
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), qcolor(PALETTE.canvas))
        box = self.field_rect()
        radius = box.height() / 2
        if self._focused:
            fill_round(p, box, qcolor(PALETTE.accent, FOCUS_FILL_ALPHA), radius)
            p.setPen(QPen(qcolor(PALETTE.accent), FOCUS_RING_PX))
        else:
            p.setPen(qcolor(PALETTE.text, 0.15))
        p.setBrush(Qt.NoBrush)
        half = FOCUS_RING_PX / 2
        p.drawRoundedRect(box.adjusted(half, half, -half, -half), radius, radius)
        inner = box.adjusted(1.4 * u, 0, -1.4 * u, 0)
        p.setFont(units.font(FIELD_FONT_U))
        if self.edit.text or self._caret:
            tone = qcolor(PALETTE.text)
            inner = draw_field_icon(p, inner, tone)  # the magnifier the other finds draw
            paint_edit(
                p,
                inner,
                self.edit,
                tone,
                caret=self._caret,
                selection=qcolor(PALETTE.accent, SELECTION_ALPHA),
            )
        else:
            tone = qcolor(PALETTE.text, PALETTE.text_muted_alpha)
            inner = draw_field_icon(p, inner, tone)
            p.setPen(tone)
            # i18n: placeholder of the Library's search field
            text = tr("Search movies, series and channels")
            p.drawText(inner, Qt.AlignLeft | Qt.AlignVCenter, elided(p, text, inner.width()))
        p.end()


class LibraryScreen(QWidget):
    """Top nav across the top; the genre column on the left (browse mode only); the grid, or
    the series view, filling the rest. `logos` (channel logos for search results) defaults to
    `posters`."""

    def __init__(self, posters, logos=None, parent=None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.NoFocus)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self._mode = "browse"
        self.grid_posters = GridPosters(posters, logos, self)
        # Children size themselves from this screen's width (1 u = 1% of it).
        self.nav = TopNav(self)
        self.genres = GenreList(self)
        self.grid = PosterGrid(self.grid_posters, self)
        self.series = SeriesView(posters, self)
        self.search_field = SearchField(self.nav)
        self.nav.attach_search(self.search_field)  # the top bar places it on its search
        self.set_mode("browse")

    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"Unknown library mode: {mode!r}")
        self._mode = mode
        self.genres.setVisible(mode == "browse")
        self.grid.setVisible(mode != "series")
        self.series.setVisible(mode == "series")
        self.search_field.setVisible(mode == "search")
        self._layout()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._layout()

    def _layout(self) -> None:
        width, height = self.width(), self.height()
        top = self.nav.height()
        self.nav.setGeometry(0, 0, width, top)
        left = 0
        if self._mode == "browse":
            self.genres.setGeometry(0, top, self.genres.width(), max(0, height - top))
            left = self.genres.width()
        content = (left, top, max(0, width - left), max(0, height - top))
        self.grid.setGeometry(*content)
        self.series.setGeometry(*content)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), qcolor(PALETTE.canvas))
        p.end()
