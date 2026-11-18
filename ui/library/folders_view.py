"""The Library's Folders tab (Plan 4, Task 12): the folders the viewer opened, each a tile, the
one opened last first (services.folder_shelf keeps them).

A light shelf, not a media library: nothing is scanned, no picture is decoded from a video,
nothing is fetched. A tile shows the folder's name, one line of what it holds ("S2 · E5 next"
for a series: the episode the start rule plays, local_playlist.where_it_starts; "Album · 12
videos") and its cover: its own cover, folder or front picture (song_facts.folder_cover), else
the audio screen's drawn stand-in with its name. The tab shows at once with each folder's name
and the kind kept on the shelf; then the folders are read on a worker, no song's tags among
them (`FolderLook`), and each tile fills in. A folder that is gone shows as missing, with
Remove; no dialog says so. A click on a tile plays its folder from where it stopped, as a
dropped folder plays (the router's read, the start rule, the hold); its × takes it off the
shelf (its files stay where they are), the last tile, Add folder, picks one as File → Open
Folder… does (a start from the tab, as a tile's play: Esc comes back to it), and Clear asks
first, on the app's question card.

Units: 1 u = 1% of this view's width (the Library screen's: the tab has no rail). Everything is
painted. The widget never takes keyboard focus: the Library controller routes keys to
`move_by`, `page`, `home`, `end`, `activate` and `remove_current` (Delete). ↑ from the top row
goes to Clear, ↑ from Clear out of the page (`upEdge`: the top bar).
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
import threading
from typing import Any, Callable, Optional, Sequence

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRectF, QSizeF, Qt, Signal
from PySide6.QtGui import (
    QFont,
    QFontMetricsF,
    QImage,
    QImageReader,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QWidget

from services.folder_shelf import ALBUM, SERIES, VIDEOS, FolderShelf, folder_kind
from services.i18n import N_, number, tr, tr_n, translate
from services.local_episodes import parse_episode
from services.local_playlist import (
    EMPTY,
    LOCAL,
    OTHER,
    UNREADABLE,
    PlaylistRead,
    read_folder,
    where_it_starts,
)
from services.open_paths import is_video
from services.song_facts import folder_cover
from ui.keymap import key_hint
from ui.library.top_nav import TAB_LABELS
from ui.overlays.audio_screen import name_upper, paint_stand_in
from ui.overlays.base import TooltipWidget
from ui.overlays.painting import Units, draw_icon, elided, fill_round, ui_units
from ui.theme import FOCUS_FILL_ALPHA, PALETTE, RADIUS_CARD, qcolor

logger = logging.getLogger(__name__)

DEFAULT_WIDTH = 1280
# The header as the Library grid's (the Recordings tab's tiles): its title and count, Clear at
# its right as the grid's sort chips are drawn; a line under it says what the tab is.
MARGIN_U = 2.2  # as the grid's side margin
HEADER_U = 5.0
TITLE_U = 1.7
COUNT_U = 1.0
COUNT_ALPHA = 0.5
SUMMARY_U = 1.0
HEADER_GAP_U = 1.6  # between the line and the tiles
CHIP_H_U = 2.4  # Clear
CHIP_FONT_U = 1.0
CHIP_PAD_U = 1.2
CHIP_ALPHA = 0.06
TILE_U = 13.0  # a tile's width, about (the row is shared out)
GAP_U = 1.4
NAME_U = 1.15
LINE_U = 1.0
TEXT_GAP_U = 0.7  # between the cover and the name
REMOVE_U = 2.4  # a tile's × (a disc at its cover's top right)
REMOVE_INSET_U = 0.6
OFFER_H_U = 2.8  # a missing folder's Remove, on its cover
WHEEL_STEP_U = 6.0
RING_PX = 1.5
HOVER_ALPHA = 0.1  # under the pointer, as the grid's chips
MISSING_SHADE = 0.62  # a missing folder's cover, under the canvas
# A cover is read at its tile's size on this screen (FoldersView.cover_px: its short side fills
# the tile, in the screen's pixels), never larger than this (a 5K screen's tile, about).
COVER_MAX_PX = 640
PAGE_ROWS = 2

TITLE_TEXT = TAB_LABELS["folders"]  # the tab's name is its title
# i18n: under the Folders tab's title
SUMMARY_TEXT = N_("The folders you opened. Each plays on from where you stopped.")
# i18n: the Folders tab's last tile: choose a folder to play (File → Open Folder…)
ADD_TEXT = N_("Add folder")
# i18n: a Folders tab tile's line: its folder is gone (moved, deleted, a drive unplugged)
MISSING_TEXT = N_("Missing")
# i18n: a Folders tab tile's line: its folder holds no video or music now
EMPTY_TEXT = N_("No videos or music")
# i18n: a Folders tab tile's line while its folder is read: it holds videos
VIDEOS_TEXT = N_("Videos")
# i18n: the question before the Folders tab's Clear takes every folder off it (its files stay)
CLEAR_QUESTION = N_("Take every folder off the Folders tab? Nothing is deleted from the disk.")


def _kind_text(kind: str) -> str:
    """A tile's line before its folder is read: the kind the shelf kept."""
    if kind == SERIES:
        # i18n: a Folders tab tile's line while its folder is read: one series' episodes
        return tr("Series", ctx="a folder")
    if kind == VIDEOS:
        return translate(VIDEOS_TEXT)
    return tr("Album")  # i18n: a Folders tab tile's line while its folder of songs is read


@dataclass(frozen=True)
class Tile:
    """A folder's tile: its `path`, `name` and `line`; `missing` when it is gone; its `cover`
    (None: the drawn stand-in); `read` once its folder was read (before: the shelf's kind)."""

    path: str
    name: str
    line: str
    missing: bool = False
    cover: Optional[QImage] = None
    read: bool = False


@dataclass(frozen=True)
class FolderLook:
    """What a folder holds, read on a worker: its list (PlaylistRead) and its cover."""

    read: PlaylistRead
    cover: Optional[QImage] = None


def look_at(path: str, cover_px: int = COVER_MAX_PX) -> FolderLook:
    """The folder at `path`, read as a dropped folder is (no song's tags: their count is
    enough), and its cover read with its short side `cover_px` at most (COVER_MAX_PX: the
    tile crops it square). Never raises; blocking disk I/O (call on a worker: a QImage is the
    only Qt object it makes)."""
    try:
        read = read_folder(path, cap=0)
    except Exception as error:  # it never raises; a tile is never left unanswered all the same
        logger.warning("A shelf folder could not be read (%s)", type(error).__name__)
        read = PlaylistRead(UNREADABLE, os.path.basename(path.rstrip("/\\")))
    side = max(1, min(int(cover_px), COVER_MAX_PX))
    cover = _cover(path, side) if read.kind in (LOCAL, EMPTY) else None
    return FolderLook(read, cover)


def _cover(path: str, side: int) -> Optional[QImage]:
    try:
        name = folder_cover(os.listdir(path))
        if name is None:
            return None
        reader = QImageReader(os.path.join(path, name))
        reader.setAutoTransform(True)
        size = reader.size()
        if size.isValid() and min(size.width(), size.height()) > side:
            expand = Qt.AspectRatioMode.KeepAspectRatioByExpanding
            reader.setScaledSize(size.scaled(side, side, expand))  # never made larger
        image = reader.read()
    except Exception as error:  # an odd picture: the stand-in
        logger.info("A shelf folder's cover could not be read (%s)", type(error).__name__)
        return None
    return None if image.isNull() else image


def tile_line(read: PlaylistRead, start: Optional[int]) -> str:
    """A read folder's line: the series' next episode (the entry at `start`, or the first),
    "Album · N songs" or "N videos"; "No videos or music" for an empty one."""
    entries = read.entries
    if read.kind != LOCAL or not entries:
        return translate(EMPTY_TEXT)
    kind = folder_kind(entries)
    if kind == ALBUM:
        count = len(entries)
        # i18n: a Folders tab tile's line: a folder of songs ({n}: how many)
        return tr_n("Album · {n} song", "Album · {n} songs", count, n=number(count))
    videos = [entry for entry in entries if is_video(entry)]
    if kind == SERIES:
        following = [entry for entry in entries[start or 0 :] if is_video(entry)] or videos
        episode = parse_episode(os.path.basename(following[0]))
        if episode is not None:
            # i18n: a Folders tab tile's line: a series' episode that plays next ({season},
            # {episode}: its numbers), "S2 · E5 next"
            return tr(
                "S{season} · E{episode} next",
                season=number(episode.season),
                episode=number(episode.first),
            )
    # i18n: a Folders tab tile's line: a folder of videos ({n}: how many)
    return tr_n("{n} video", "{n} videos", len(videos), n=number(len(videos)))


class _Wanted:
    """The read still wanted, a plain object the threads read (never a Qt one)."""

    serial = 0


class FolderLooks(QObject):
    """The shelf's folders read on a thread of their own, one after the other: `looked`
    (the read's number, a folder's path, its FolderLook) comes on the GUI thread for each. A
    newer read, or `stop()`, ends an older one between two folders."""

    looked = Signal(int, str, object)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._wanted = _Wanted()

    def look(self, serial: int, paths: Sequence[str], cover_px: int = COVER_MAX_PX) -> None:
        """Read `paths` (their covers `cover_px` on their short side, at most)."""
        self._wanted.serial = serial
        threading.Thread(
            target=self._run,
            args=(serial, list(paths), int(cover_px), self._wanted),
            name="qitv-folder-shelf",
            daemon=True,
        ).start()

    def stop(self) -> None:
        self._wanted.serial = -1

    def _run(self, serial: int, paths: list[str], cover_px: int, wanted: _Wanted) -> None:
        for path in paths:
            if wanted.serial != serial:
                return
            look = look_at(path, cover_px)
            try:
                self.looked.emit(serial, path, look)
            except RuntimeError:
                return  # QiTV closed meanwhile


@dataclass
class _Cell:
    """Where a tile (or Add folder) is drawn, in the page's coordinates (unscrolled)."""

    cover: QRectF
    name: QRectF
    line: QRectF

    def whole(self) -> QRectF:
        return self.cover.united(self.line)


class FoldersView(TooltipWidget):
    playRequested = Signal(str)  # a folder's path: it plays from where it stopped
    removeRequested = Signal(str)  # a folder's path: off the shelf
    addRequested = Signal()  # Add folder: File → Open Folder…
    clearRequested = Signal()  # Clear (the desk asks first)
    upEdge = Signal()  # ↑ from Clear: the top bar

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMouseTracking(True)
        self.setAccessibleName(translate(TITLE_TEXT))
        self._tiles: list[Tile] = []
        self._current = 0  # a cell: 0 … len(tiles) - 1 the tiles, len(tiles) Add folder; -1 Clear
        self._focused = False
        self._scroll = 0.0
        self._hover: Optional[tuple[str, int]] = None  # ("tile" | "remove" | "add" | "clear", i)
        self._pressed: Optional[tuple[str, int]] = None
        self._pixmaps: dict[tuple, QPixmap] = {}
        self._avoid: Optional[QWidget] = None  # the corner player, kept clear of (`avoid`)
        self._reserve = (0.0, 0.0)  # what it takes of the width, on the left and the right

    def avoid(self, widget: QWidget) -> None:
        """The tiles keep clear of `widget` (the Library's corner player) while it shows over
        them, as the Recordings page does: they are laid out beside it."""
        self._avoid = widget
        widget.installEventFilter(self)
        self._follow_avoid()

    def eventFilter(self, watched, event) -> bool:
        kinds = (QEvent.Type.Move, QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.Hide)
        if watched is self._avoid and event.type() in kinds:
            self._follow_avoid()
        return False

    def _follow_avoid(self) -> None:
        widget = self._avoid
        reserve = (0.0, 0.0)
        if widget is not None and widget.isVisible() and self.isVisible():
            top_left = self.mapFromGlobal(widget.mapToGlobal(QPoint(0, 0)))
            rect = QRectF(QPointF(top_left), QSizeF(widget.size()))
            if rect.intersects(QRectF(self.rect())):
                u = self._units().u
                margin, gap = MARGIN_U * u, GAP_U * u
                if rect.center().x() > self.width() / 2:
                    reserve = (0.0, max(0.0, self.width() - rect.left() + gap - margin))
                else:
                    reserve = (max(0.0, rect.right() + gap - margin), 0.0)
        if reserve != self._reserve:
            self._reserve = reserve
            self._pixmaps.clear()
            self._clamp_scroll()
            self.update()

    # ---- the tiles ----------------------------------------------------------------------------

    def set_tiles(self, tiles: Sequence[Tile]) -> None:
        """The shelf's folders, the newest first. The keys' focus stays on its folder, or on
        the tile in its place when it went."""
        before = self.current() if self._tiles else ""
        self._tiles = list(tiles)
        paths = [tile.path for tile in self._tiles]
        if not before:
            self._current = 0  # the tab's first folders: the newest
        elif before in paths:
            self._current = paths.index(before)
        elif before == "add":
            self._current = len(self._tiles)
        elif before != "clear":
            self._current = min(self._current, len(self._tiles))  # the one in its place
        if self._current < 0 and not self._tiles:
            self._current = 0  # no Clear without a folder
        self._pixmaps.clear()
        self._clamp_scroll()
        self._describe()
        self.update()

    def set_tile(self, tile: Tile) -> None:
        """One tile filled in (its folder was read)."""
        for index, shown in enumerate(self._tiles):
            if shown.path == tile.path:
                self._tiles[index] = tile
                self._pixmaps = {k: v for k, v in self._pixmaps.items() if k[0] != tile.path}
                self._describe()
                self.update()
                return

    def tiles(self) -> list[Tile]:
        return list(self._tiles)

    def cover_px(self) -> int:
        """A tile's cover side in this screen's pixels, as the tiles are laid out now: how
        large a cover is read (look_at)."""
        side = self._cells()[0].cover.width()
        return max(1, round(side * self.devicePixelRatioF()))

    # ---- the keys -----------------------------------------------------------------------------

    def set_focused(self, focused: bool) -> None:
        self._focused = focused
        self.update()

    def current(self) -> str:
        """What the keys' focus is on: a folder's path, "add" or "clear"."""
        if self._current < 0:
            return "clear"
        if self._current >= len(self._tiles):
            return "add"
        return self._tiles[self._current].path

    def move_by(self, dx: int, dy: int) -> None:
        last = len(self._tiles)  # Add folder's cell
        if self._current < 0:  # on Clear
            if dy < 0:
                self.upEdge.emit()
            elif dy > 0:
                self._go(0)
            return
        columns = self._columns()
        if dx:
            self._go(min(last, max(0, self._current + dx)))
        if dy < 0:
            if self._current < columns:
                if self._tiles:
                    self._go(-1)
                else:
                    self.upEdge.emit()
            else:
                self._go(self._current - columns)
        elif dy > 0:
            below = self._current + columns
            if below <= last:
                self._go(below)
            elif self._current // columns < last // columns:
                self._go(last)  # the row below is shorter: its last cell

    def page(self, delta: int) -> None:
        if self._current < 0:
            return
        step = self._columns() * PAGE_ROWS
        self._go(min(len(self._tiles), max(0, self._current + delta * step)))

    def home(self) -> None:
        self._go(0)

    def end(self) -> None:
        self._go(len(self._tiles))

    def activate(self) -> None:
        self._act(self.current())

    def remove_current(self) -> None:
        """Delete: the folder the focus is on goes off the shelf."""
        if 0 <= self._current < len(self._tiles):
            self.removeRequested.emit(self._tiles[self._current].path)

    def _go(self, cell: int) -> None:
        self._current = cell
        self._reveal()
        self._describe()
        self.update()

    def _act(self, target: str) -> None:
        if target == "clear":
            self.clearRequested.emit()
        elif target == "add":
            self.addRequested.emit()
        else:
            self.playRequested.emit(target)

    def _describe(self) -> None:
        """A screen reader says the tile the keys are on: its name and its line."""
        target = self.current()
        if target == "clear":
            text = tr("Clear")
        elif target == "add":
            text = translate(ADD_TEXT)
        else:
            tile = self._tiles[self._current]
            text = f"{tile.name}, {tile.line}"
        self.setAccessibleDescription(text)

    # ---- where things are ---------------------------------------------------------------------

    def _units(self) -> Units:
        return ui_units(self.width() or DEFAULT_WIDTH)

    def _area(self) -> tuple[float, float]:
        """The page's left edge and width for its title and tiles: inside its margins, beside
        the corner player (`avoid`)."""
        u = self._units().u
        left, right = self._reserve
        return MARGIN_U * u + left, max(1.0, self.width() - 2 * MARGIN_U * u - left - right)

    def _columns(self) -> int:
        u = self._units().u
        _left, room = self._area()
        return max(1, int((room + GAP_U * u) // ((TILE_U + GAP_U) * u)))

    def _text_heights(self, units: Units) -> tuple[float, float]:
        name = QFontMetricsF(units.font(NAME_U, QFont.Weight.DemiBold)).height()
        line = QFontMetricsF(units.font(LINE_U)).height()
        return name, line

    def _title_rect(self) -> QRectF:
        """The header: its title and count, and Clear at its right."""
        left, width = self._area()
        return QRectF(left, 0.0, width, HEADER_U * self._units().u)

    def _summary_rect(self) -> QRectF:
        units = self._units()
        title = self._title_rect()
        height = QFontMetricsF(units.font(SUMMARY_U)).height()
        return QRectF(title.left(), title.bottom() - 0.6 * units.u, title.width(), height)

    def _clear_page_rect(self) -> QRectF:
        if not self._tiles:
            return QRectF()
        units = self._units()
        u = units.u
        width = QFontMetricsF(units.font(CHIP_FONT_U)).horizontalAdvance(tr("Clear"))
        width += 2 * CHIP_PAD_U * u
        title = self._title_rect()
        height = CHIP_H_U * u
        return QRectF(title.right() - width, title.center().y() - height / 2, width, height)

    def _cells(self) -> list[_Cell]:
        """Each tile's cell, then Add folder's, in the page's coordinates."""
        units = self._units()
        u = units.u
        columns = self._columns()
        left, room = self._area()
        gap = GAP_U * u
        side = (room - (columns - 1) * gap) / columns
        name_h, line_h = self._text_heights(units)
        row_h = side + TEXT_GAP_U * u + name_h + line_h + gap
        top = self._summary_rect().bottom() + HEADER_GAP_U * u
        cells = []
        for index in range(len(self._tiles) + 1):
            row, column = divmod(index, columns)
            x = left + column * (side + gap)
            y = top + row * row_h
            cover = QRectF(x, y, side, side)
            name = QRectF(x, cover.bottom() + TEXT_GAP_U * u, side, name_h)
            line = QRectF(x, name.bottom(), side, line_h)
            cells.append(_Cell(cover, name, line))
        return cells

    def _remove_page_rect(self, cell: _Cell, tile: Tile) -> QRectF:
        u = self._units().u
        cover = cell.cover
        if tile.missing:  # the offer, in the middle of its cover
            font = self._units().font(LINE_U, QFont.Weight.DemiBold)
            width = QFontMetricsF(font).horizontalAdvance(tr("Remove")) + 2.4 * u
            height = OFFER_H_U * u
            return QRectF(
                cover.center().x() - width / 2, cover.center().y() - height / 2, width, height
            )
        size, inset = REMOVE_U * u, REMOVE_INSET_U * u
        return QRectF(cover.right() - inset - size, cover.top() + inset, size, size)

    def _shown(self, rect: QRectF) -> QRectF:
        return rect.translated(0, -self._scroll) if not rect.isEmpty() else rect

    def tile_rect(self, index: int) -> QRectF:
        """The tile of the folder at `index` (its cover, name and line), as shown."""
        cells = self._cells()
        if not 0 <= index < len(self._tiles):
            return QRectF()
        return self._shown(cells[index].whole())

    def remove_rect(self, index: int) -> QRectF:
        """The tile's Remove: its × (a missing folder's: the Remove on its cover)."""
        if not 0 <= index < len(self._tiles):
            return QRectF()
        return self._shown(self._remove_page_rect(self._cells()[index], self._tiles[index]))

    def add_rect(self) -> QRectF:
        return self._shown(self._cells()[-1].whole())

    def clear_rect(self) -> QRectF:
        return self._shown(self._clear_page_rect())

    def controls(self) -> dict[str, QRectF]:
        """The page's controls as shown, by name (the mouse audit's): "tiles" (the first
        folder's), "remove" (its ×), "add" and "clear"."""
        return {
            "tiles": self.tile_rect(0),
            "remove": self.remove_rect(0),
            "add": self.add_rect(),
            "clear": self.clear_rect(),
        }

    def _content_height(self) -> float:
        return self._cells()[-1].whole().bottom() + GAP_U * self._units().u

    def _clamp_scroll(self) -> None:
        self._scroll = max(0.0, min(self._scroll, self._content_height() - self.height()))

    def _reveal(self) -> None:
        if self._current < 0:
            self._scroll = 0.0
        else:
            cells = self._cells()
            rect = cells[min(self._current, len(cells) - 1)].whole()
            margin = GAP_U * self._units().u
            if rect.top() - margin < self._scroll:
                self._scroll = rect.top() - margin
            elif rect.bottom() + margin > self._scroll + self.height():
                self._scroll = rect.bottom() + margin - self.height()
        self._clamp_scroll()

    def _target_at(self, pos: QPointF) -> Optional[tuple[str, int]]:
        if self.clear_rect().contains(pos):
            return ("clear", -1)
        page = QPointF(pos.x(), pos.y() + self._scroll)
        cells = self._cells()
        for index, tile in enumerate(self._tiles):
            if self._remove_page_rect(cells[index], tile).contains(page):
                return ("remove", index)
            if cells[index].whole().contains(page):
                return ("tile", index)
        if cells[-1].whole().contains(page):
            return ("add", len(self._tiles))
        return None

    # ---- the mouse ----------------------------------------------------------------------------

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed = self._target_at(event.position())
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        pressed, self._pressed = self._pressed, None
        if pressed is None or self._target_at(event.position()) != pressed:
            return
        kind, index = pressed
        if kind == "remove":
            self.removeRequested.emit(self._tiles[index].path)
            return
        self._current = index if kind != "clear" else -1
        self._describe()
        self.update()
        self._act("clear" if kind == "clear" else "add" if kind == "add" else self.current())

    def mouseDoubleClickEvent(self, event) -> None:
        event.accept()  # a quick second click opens nothing twice

    def mouseMoveEvent(self, event) -> None:
        hover = self._target_at(event.position())
        if hover != self._hover:
            self._hover = hover
            self.setCursor(
                Qt.CursorShape.PointingHandCursor if hover else Qt.CursorShape.ArrowCursor
            )
            self.update()

    def leaveEvent(self, event) -> None:
        if self._hover is not None:
            self._hover = None
            self.update()
        super().leaveEvent(event)

    def wheelEvent(self, event) -> None:
        notches = event.angleDelta().y() / 120
        self._scroll -= notches * WHEEL_STEP_U * self._units().u
        self._clamp_scroll()
        self.update()
        event.accept()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._pixmaps.clear()
        self._follow_avoid()
        self._clamp_scroll()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._follow_avoid()

    def tooltip_at(self, pos: QPointF) -> Optional[str]:
        target = self._target_at(pos)
        if target is None:
            return ""
        kind, index = target
        if kind == "remove":
            # i18n: a tooltip: the Folders tab tile's ×: the folder goes off the shelf
            return key_hint(tr("Remove"), "Delete") if not self._tiles[index].missing else ""
        if kind == "tile":
            return self._tiles[index].path  # where it is (it stays on this computer)
        if kind == "add":
            return key_hint(translate(ADD_TEXT), "Ctrl+Shift+O")
        return ""

    # ---- painting -----------------------------------------------------------------------------

    def paintEvent(self, event) -> None:
        units = self._units()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.fillRect(self.rect(), qcolor(PALETTE.canvas))
        p.translate(0, -self._scroll)
        self._paint_header(p, units)
        cells = self._cells()
        visible = QRectF(0, self._scroll, self.width(), self.height())
        for index, tile in enumerate(self._tiles):
            if cells[index].whole().intersects(visible):
                self._paint_tile(p, units, index, tile, cells[index])
        if cells[-1].whole().intersects(visible):
            self._paint_add(p, units, cells[-1])
        p.end()

    def _paint_header(self, p: QPainter, units: Units) -> None:
        """As the Library grid's header: the title, its count, then Clear as its chips."""
        u = units.u
        title = self._title_rect()
        clear = self._clear_page_rect()
        right = clear.left() - 2 * u if not clear.isEmpty() else title.right()
        title_font = units.font(TITLE_U, QFont.Weight.Bold)
        baseline = title.center().y() + QFontMetricsF(title_font).capHeight() / 2
        p.setFont(title_font)
        p.setPen(qcolor(PALETTE.text))
        text = elided(p, translate(TITLE_TEXT), right - title.left())
        p.drawText(QPointF(title.left(), baseline), text)
        x = title.left() + p.fontMetrics().horizontalAdvance(text) + 0.8 * u
        if self._tiles and x < right:
            p.setFont(units.font(COUNT_U))
            p.setPen(qcolor(PALETTE.text, COUNT_ALPHA))
            p.drawText(QPointF(x, baseline), number(len(self._tiles)))
        p.setFont(units.font(SUMMARY_U))
        p.setPen(qcolor(PALETTE.text, PALETTE.text_muted_alpha))
        summary = self._summary_rect()
        p.drawText(
            summary,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            elided(p, translate(SUMMARY_TEXT), summary.width()),
        )
        if clear.isEmpty():
            return
        radius = clear.height() / 2
        focused = self._focused and self._current < 0
        hovered = self._hover == ("clear", -1)
        fill_round(p, clear, qcolor(PALETTE.text, HOVER_ALPHA if hovered else CHIP_ALPHA), radius)
        if focused:
            fill_round(p, clear, qcolor(PALETTE.accent, FOCUS_FILL_ALPHA), radius)
            p.setPen(QPen(qcolor(PALETTE.accent), RING_PX))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(clear.adjusted(0.75, 0.75, -0.75, -0.75), radius, radius)
        p.setFont(units.font(CHIP_FONT_U))
        p.setPen(qcolor(PALETTE.text) if focused or hovered else qcolor(PALETTE.text, 0.6))
        p.drawText(clear, Qt.AlignmentFlag.AlignCenter, tr("Clear"))

    def _paint_tile(self, p: QPainter, units: Units, index: int, tile: Tile, cell: _Cell) -> None:
        cover = cell.cover
        p.drawPixmap(cover.topLeft(), self._cover_pixmap(tile, cover.width()))
        focused = self._focused and self._current == index
        hovered = self._hover is not None and self._hover[1] == index
        if focused:
            p.setPen(QPen(qcolor(PALETTE.accent), RING_PX))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(cover.adjusted(-2, -2, 2, 2), RADIUS_CARD + 1, RADIUS_CARD + 1)
        remove = self._remove_page_rect(cell, tile)
        over = self._hover == ("remove", index)
        if tile.missing:
            fill_round(p, remove, qcolor(PALETTE.text, 0.26 if over else 0.18), remove.height() / 2)
            p.setFont(units.font(LINE_U, QFont.Weight.DemiBold))
            p.setPen(qcolor(PALETTE.text))
            p.drawText(remove, Qt.AlignmentFlag.AlignCenter, tr("Remove"))
        elif focused or hovered:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qcolor(PALETTE.accent) if over else qcolor(PALETTE.canvas, 0.72))
            p.drawEllipse(remove)
            mark = remove.width() * 0.42
            box = QRectF(remove.center().x() - mark / 2, remove.center().y() - mark / 2, mark, mark)
            draw_icon(p, "close", box, qcolor(PALETTE.canvas if over else PALETTE.text))
        lit = focused or hovered
        p.setFont(units.font(NAME_U, QFont.Weight.DemiBold))
        p.setPen(qcolor(PALETTE.text, 1.0 if lit else 0.92))
        p.drawText(
            cell.name,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            elided(p, tile.name, cell.name.width()),
        )
        p.setFont(units.font(LINE_U))
        tone = (
            qcolor(PALETTE.warn) if tile.missing else qcolor(PALETTE.text, PALETTE.text_muted_alpha)
        )
        p.setPen(tone)
        p.drawText(
            cell.line,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            elided(p, tile.line, cell.line.width()),
        )

    def _cover_pixmap(self, tile: Tile, side: float) -> QPixmap:
        """The tile's cover, its corners rounded: its picture (centre crop) or the stand-in;
        a missing folder's under the canvas. Kept per tile and size."""
        dpr = self.devicePixelRatioF()
        key = (tile.path, round(side * dpr), dpr, tile.missing, tile.cover is not None)
        pixmap = self._pixmaps.get(key)
        if pixmap is not None:
            return pixmap
        size = max(1, round(side * dpr))
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        p = QPainter(pixmap)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            rect = QRectF(0, 0, size, size)
            clip = QPainterPath()
            clip.addRoundedRect(rect, RADIUS_CARD * dpr, RADIUS_CARD * dpr)
            p.setClipPath(clip)
            image = tile.cover
            if image is None or image.isNull():
                paint_stand_in(p, rect, side, name_upper(tile.name))
            else:
                scaled = image.scaled(
                    size,
                    size,
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation,
                )
                p.drawImage(
                    QPointF((size - scaled.width()) / 2, (size - scaled.height()) / 2), scaled
                )
            if tile.missing:
                p.fillRect(rect, qcolor(PALETTE.canvas, MISSING_SHADE))
        finally:
            p.end()
        pixmap.setDevicePixelRatio(dpr)
        self._pixmaps[key] = pixmap
        return pixmap

    def _paint_add(self, p: QPainter, units: Units, cell: _Cell) -> None:
        cover = cell.cover
        focused = self._focused and self._current == len(self._tiles)
        hovered = self._hover is not None and self._hover[0] == "add"
        fill_round(p, cover, qcolor(PALETTE.text, 0.07 if hovered else 0.04), RADIUS_CARD)
        pen = QPen(qcolor(PALETTE.accent) if focused else qcolor(PALETTE.text, 0.28), RING_PX)
        pen.setStyle(Qt.PenStyle.SolidLine if focused else Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(cover.adjusted(0.75, 0.75, -0.75, -0.75), RADIUS_CARD, RADIUS_CARD)
        mark = cover.width() * 0.22
        box = QRectF(cover.center().x() - mark / 2, cover.center().y() - mark / 2, mark, mark)
        tone = qcolor(PALETTE.accent) if focused or hovered else qcolor(PALETTE.text, 0.7)
        draw_icon(p, "plus", box, tone)
        p.setFont(units.font(NAME_U, QFont.Weight.DemiBold))
        p.setPen(qcolor(PALETTE.text, 1.0 if focused or hovered else 0.92))
        p.drawText(
            cell.name,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            elided(p, translate(ADD_TEXT), cell.name.width()),
        )


class FoldersDesk(QObject):
    """The Folders tab's controller: the shelf (services.folder_shelf) on its view, each
    folder read on a worker when the tab opens (FolderLooks), what its tiles ask, and Clear's
    question on its own card (`card`, a QuestionCard on the Library page). `open_folder(path)`
    plays a folder as a dropped one plays; `add_folder()` picks one as File → Open Folder…
    does. `changed`: the shelf changed (the Library's tabs follow)."""

    changed = Signal()

    def __init__(
        self,
        shelf: FolderShelf,
        view: FoldersView,
        card: Any,
        watch: Any,
        *,
        open_folder: Callable[[str], None],
        add_folder: Callable[[], None],
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._shelf = shelf
        self._view = view
        self._card = card
        self._watch = watch
        self._open_folder = open_folder
        self._known: dict[str, FolderLook] = {}  # by path: the last look at each folder
        self._reading: set[str] = set()  # the paths the newest look reads still (M1)
        self._serial = 0
        self._shown = False
        self._asking = False
        self._looks = FolderLooks(self)
        self._looks.looked.connect(self._on_looked)
        shelf.subscribe(self._on_shelf_changed)
        view.playRequested.connect(self._play)
        view.removeRequested.connect(shelf.remove)
        view.addRequested.connect(add_folder)
        view.clearRequested.connect(self.ask_clear)
        card.answered.connect(self._on_answered)

    def count(self) -> int:
        return len(self._shelf)

    def shown(self) -> bool:
        return self._shown

    def show(self) -> None:
        """The tab opens: its folders at once, each read again (the lines follow what was
        watched since)."""
        self._shown = True
        self._serial += 1
        self._reading.clear()  # an older look's are no longer answered
        self._fill()
        self._look([entry["path"] for entry in self._shelf.folders()])

    def hide(self) -> None:
        """The tab is left (or the Library): reads under way are dropped, the question too."""
        self._shown = False
        self._serial += 1
        self._reading.clear()
        self._looks.stop()
        self.withdraw()

    def _look(self, paths: Sequence[str]) -> None:
        """Read `paths` on the looks' thread, their covers at the tiles' size on this screen."""
        if paths:
            self._reading.update(paths)
            self._looks.look(self._serial, paths, self._view.cover_px())

    def withdraw(self) -> None:
        if self._asking:
            self._asking = False
            self._card.withdraw()

    def answer_key(self, action: Any) -> bool:
        """A key while Clear's question shows: True when it is the card's."""
        return self._asking and bool(self._card.handle(action))

    def ask_clear(self) -> None:
        if not len(self._shelf):
            return
        self._asking = True
        self._card.ask(translate(CLEAR_QUESTION), tr("Clear"), tr("Cancel"))

    def _on_answered(self, yes: bool) -> None:
        if not self._asking:
            return  # the card's answer to another question
        self._asking = False
        if yes:
            self._shelf.clear()

    def _play(self, path: str) -> None:
        self._open_folder(path)

    def _on_shelf_changed(self) -> None:
        paths = [entry["path"] for entry in self._shelf.folders()]
        self._known = {path: look for path, look in self._known.items() if path in paths}
        if self._shown:
            self._fill()
            # a folder opened while the tab shows (not one still read: never twice at once)
            self._look([p for p in paths if p not in self._known and p not in self._reading])
            if not paths:
                self.withdraw()
        self.changed.emit()

    def _fill(self) -> None:
        tiles = [
            self._tile(entry, self._known.get(entry["path"])) for entry in self._shelf.folders()
        ]
        self._view.set_tiles(tiles)

    def _on_looked(self, serial: int, path: str, look: FolderLook) -> None:
        if serial != self._serial:
            return  # an older read's (the tab was left since)
        self._reading.discard(path)
        entry = next((e for e in self._shelf.folders() if e["path"] == path), None)
        if entry is None:
            return  # taken off meanwhile
        self._known[path] = look
        self._view.set_tile(self._tile(entry, look))

    def _tile(self, entry: dict, look: Optional[FolderLook]) -> Tile:
        path, name = str(entry["path"]), str(entry["name"])
        if look is None:
            return Tile(path, name, _kind_text(str(entry.get("kind"))))
        read = look.read
        if read.kind in (UNREADABLE, OTHER):
            return Tile(path, name, translate(MISSING_TEXT), missing=True, read=True)
        start = where_it_starts(self._watch, read.entries, read.sizes) if read.entries else None
        return Tile(path, name, tile_line(read, start), cover=look.cover, read=True)
