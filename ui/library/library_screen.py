"""The Library screen: top nav, genre column, poster grid or series view (spec §5.4).

Mockup 05, option 1. 1 u = 1% of this screen's width. The screen only lays its parts out and
paints the search field; the LibraryController decides what they show and routes the keys.
"""

from collections import OrderedDict
import logging

from PySide6.QtCore import QObject, QRectF, Qt, Signal
from PySide6.QtGui import QPainter, QPen, QPixmap
from PySide6.QtWidgets import QWidget

from ui.library.genre_list import GenreList
from ui.library.poster_grid import HEADER_H_U, PAD_U, POSTER_ASPECT, PosterGrid
from ui.library.series_view import SeriesView
from ui.library.top_nav import TopNav
from ui.overlays.painting import Units, elided, fill_round
from ui.theme import FOCUS_FILL_ALPHA, PALETTE, qcolor

logger = logging.getLogger(__name__)

MODES = ("browse", "series", "search")
DEFAULT_HOST_WIDTH = 1280
LIVE_TILE = "live-tile:"  # a poster URL drawn as the channel logo centred on a dark tile
TILE_W = 320  # pixels; tall enough to stay sharp when the grid scales it to a poster
TILE_LOGO_W = 0.7  # the logo fits in this share of the tile's width…
TILE_LOGO_H = 0.4  # …and of its height
MAX_TILES = 300
FIELD_W_U = 46.0
FIELD_H_U = 3.2
FIELD_RADIUS_U = 0.8
FIELD_FONT_U = 1.15
COUNT_FONT_U = 1.0
FOCUS_RING_PX = 1.5
SEARCH_PLACEHOLDER = "⌕ Search movies, series and channels"


class GridPosters(QObject):
    """The grid's image source: posters, and live channel logos centred on a dark tile.

    A URL that starts with LIVE_TILE names a channel logo; the logo cache loads it and the
    tile is composed here, on the GUI thread, when the grid paints it. Anything else is a
    poster. `logos` defaults to `posters`.
    """

    logoReady = Signal(str)

    def __init__(self, posters, logos=None, parent=None) -> None:
        super().__init__(parent)
        self._posters = posters
        self._logos = logos if logos is not None else posters
        self._tiles: OrderedDict[str, QPixmap] = OrderedDict()
        if posters is not None:
            posters.logoReady.connect(self.logoReady)
        if self._logos is not None:
            self._logos.logoReady.connect(self._on_logo_ready)

    def pixmap(self, url: str) -> QPixmap | None:
        if not url.startswith(LIVE_TILE):
            return self._posters.pixmap(url) if self._posters is not None else None
        logo_url = url[len(LIVE_TILE) :]
        tile = self._tiles.get(logo_url)
        if tile is not None:
            self._tiles.move_to_end(logo_url)
            return tile
        logo = self._logos.pixmap(logo_url) if self._logos is not None else None
        if logo is None or logo.isNull():
            return None
        tile = _logo_tile(logo)
        self._tiles[logo_url] = tile
        while len(self._tiles) > MAX_TILES:
            self._tiles.popitem(last=False)
        return tile

    def request(self, urls: list[str], replace_pending: bool = False) -> None:
        if self._logos is self._posters:
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
        self._tiles.pop(url, None)
        self.logoReady.emit(LIVE_TILE + url)


def _logo_tile(logo: QPixmap) -> QPixmap:
    """The logo centred on a raised-panel tile with the poster's aspect ratio."""
    tile = QPixmap(TILE_W, round(TILE_W * POSTER_ASPECT))
    tile.fill(qcolor(PALETTE.panel_raised))
    scaled = logo.scaled(
        round(tile.width() * TILE_LOGO_W),
        round(tile.height() * TILE_LOGO_H),
        Qt.KeepAspectRatio,
        Qt.SmoothTransformation,
    )
    p = QPainter(tile)
    p.drawPixmap(
        (tile.width() - scaled.width()) // 2, (tile.height() - scaled.height()) // 2, scaled
    )
    p.end()
    return tile


class SearchField(QWidget):
    """The search field painted over the grid's header in search mode."""

    clicked = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.NoFocus)
        self.query = ""
        self.count_text = ""
        self._focused = False

    def set_query(self, text: str) -> None:
        self.query = text
        self.update()

    def set_count_text(self, text: str) -> None:
        self.count_text = text
        self.update()

    def set_focused(self, focused: bool) -> None:
        self._focused = focused
        self.update()

    def _units(self) -> Units:
        parent = self.parentWidget()
        width = parent.width() if parent is not None and parent.width() > 0 else 0
        return Units(width or DEFAULT_HOST_WIDTH)

    def field_rect(self) -> QRectF:
        u = self._units().u
        pad = PAD_U * u
        width = min(FIELD_W_U * u, self.width() - 2 * pad)
        return QRectF(pad, (self.height() - FIELD_H_U * u) / 2, max(0.0, width), FIELD_H_U * u)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit()

    def paintEvent(self, event) -> None:
        units = self._units()
        u = units.u
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), qcolor(PALETTE.canvas))
        box = self.field_rect()
        radius = FIELD_RADIUS_U * u
        if self._focused:
            fill_round(p, box, qcolor(PALETTE.accent, FOCUS_FILL_ALPHA), radius)
            p.setPen(QPen(qcolor(PALETTE.accent), FOCUS_RING_PX))
        else:
            p.setPen(qcolor(PALETTE.text, 0.12))
        p.setBrush(Qt.NoBrush)
        half = FOCUS_RING_PX / 2
        p.drawRoundedRect(box.adjusted(half, half, -half, -half), radius, radius)
        inner = box.adjusted(1.2 * u, 0, -1.2 * u, 0)
        p.setFont(units.font(FIELD_FONT_U))
        if self.query or self._focused:
            p.setPen(qcolor(PALETTE.text))
            text = "⌕ " + self.query + ("▏" if self._focused else "")
        else:
            p.setPen(qcolor(PALETTE.text, 0.45))
            text = SEARCH_PLACEHOLDER
        p.drawText(inner, Qt.AlignLeft | Qt.AlignVCenter, elided(p, text, inner.width()))
        if self.count_text:
            p.setFont(units.font(COUNT_FONT_U))
            p.setPen(qcolor(PALETTE.text, 0.5))
            left = box.right() + 2 * u
            right = self.width() - PAD_U * u
            p.drawText(
                QRectF(left, 0, max(0.0, right - left), self.height()),
                Qt.AlignRight | Qt.AlignVCenter,
                elided(p, self.count_text, right - left),
            )
        p.end()


class LibraryScreen(QWidget):
    """Top nav across the top; the genre column on the left (not in search mode); the grid,
    or the series view, filling the rest. `logos` (channel logos for search results) defaults
    to `posters`."""

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
        self.search_field = SearchField(self)
        self.set_mode("browse")

    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"Unknown library mode: {mode!r}")
        self._mode = mode
        self.genres.setVisible(mode != "search")
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
        if self._mode != "search":
            self.genres.setGeometry(0, top, self.genres.width(), max(0, height - top))
            left = self.genres.width()
        content = (left, top, max(0, width - left), max(0, height - top))
        self.grid.setGeometry(*content)
        self.series.setGeometry(*content)
        header = Units(width or DEFAULT_HOST_WIDTH).px(HEADER_H_U)
        self.search_field.setGeometry(left, top, content[2], header)
        self.search_field.raise_()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), qcolor(PALETTE.canvas))
        p.end()
