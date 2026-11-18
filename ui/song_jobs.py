"""A song's cover and album, read on threads of their own (spec §10, the audio screen), and the
cover written for the system's media panel (PanelCover).

As TVWindow's file check and the episode listing do: each start runs on a daemon thread, which
creates no Qt objects and starts no timers, and its answer comes on the GUI thread as plain
data, numbered by the caller's generation (a late answer for an older title is the caller's to
drop). The one exception is the cover's QImage, decoded and scaled on the thread as
services.image_loader does for logos (a QImage is safe there; the GUI thread makes the pixmap).
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import threading
from typing import Optional

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QImage

from services import album_listing
from services.image_loader import read_scaled
from services.song_facts import BRAND_COLOURS, dominant_colours

logger = logging.getLogger(__name__)

COVER_SIDE = 1024  # the cover is decoded at most this many pixels a side
COLOUR_SIDE = 32  # its colours are read from a copy this small
PANEL_SIDE = 512  # the media panel's cover file is at most this many pixels a side
PANEL_QUALITY = 90  # ... a JPEG at this quality (a PNG when it has see-through parts)


def cover_colours(image: Optional[QImage]) -> list[tuple[int, int, int]]:
    """The cover's dominant colours, from a COLOUR_SIDE square copy (QiTV's own without one)."""
    if image is None or image.isNull():
        return list(BRAND_COLOURS)
    small = image.scaled(
        COLOUR_SIDE,
        COLOUR_SIDE,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    ).convertToFormat(QImage.Format.Format_RGB888)
    width, height, stride = small.width(), small.height(), small.bytesPerLine()
    raw = bytes(small.constBits())[: stride * height]
    rgb = b"".join(raw[row * stride : row * stride + width * 3] for row in range(height))
    return dominant_colours(rgb, width, height)


class CoverJob(QObject):
    """The cover of a song, decoded on a daemon thread: `ready` (the caller's generation, the
    QImage or None for none, its colours) comes on the GUI thread."""

    ready = Signal(int, object, list)

    def start(self, generation: int, path: str, art_url: str) -> None:
        """Look for the cover of the song at `path`, `art_url` being libVLC's artwork URL for
        it (PlaybackSession.tags()): album_listing.cover_candidates, the first that decodes."""
        threading.Thread(
            target=self._read, args=(generation, path, art_url), name="qitv-cover", daemon=True
        ).start()

    def _read(self, generation: int, path: str, art_url: str) -> None:
        image: Optional[QImage] = None
        try:
            for candidate in album_listing.cover_candidates(path, art_url or ""):
                image = read_scaled(candidate, COVER_SIDE)
                if image is not None:
                    break
            colours = cover_colours(image)
        except Exception as error:  # never unanswered: the screen draws its own cover
            logger.warning("A song's cover could not be read (%s)", type(error).__name__)
            image, colours = None, list(BRAND_COLOURS)
        try:
            self.ready.emit(generation, image, colours)
        except RuntimeError:
            pass  # QiTV closed meanwhile


class AlbumJob(QObject):
    """The album of a song in its folder, listed on a daemon thread (album_listing.album_of):
    `listed` (the caller's generation, the album's Tracks in order, the song's own among them,
    or [] for a lone song) comes on the GUI thread, and `read` with it says whether the folder
    was read whole (album_listing.read_album: a listing cut short can't say what follows its
    last track). A newer start stops an older listing between two files; the older one is
    still answered, with what it had (and as cut short)."""

    listed = Signal(int, list)
    read = Signal(int, list, bool)  # listed's, and whether the folder was read whole

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        # The newest start's generation, in a plain list the threads read (never this QObject).
        self._newest: list[Optional[int]] = [None]

    def start(self, generation: int, path: str) -> None:
        self._newest[0] = generation
        threading.Thread(
            target=self._list, args=(generation, path, self._newest), name="qitv-album", daemon=True
        ).start()

    def _list(self, generation: int, path: str, newest: list[Optional[int]]) -> None:
        try:
            album, whole = album_listing.read_album(
                path,
                budget_s=album_listing.LIST_BUDGET_S,
                cap=album_listing.LIST_CAP,
                stop=lambda: newest[0] != generation,
            )
        except Exception as error:  # read_album never raises; never unanswered all the same
            logger.warning("A song's album could not be listed (%s)", type(error).__name__)
            album, whole = [], False
        try:
            self.read.emit(generation, album, whole)
            self.listed.emit(generation, album)
        except RuntimeError:
            pass  # QiTV closed meanwhile


def _see_through(image: QImage) -> bool:
    """Some of `image` is see-through (decoded covers all have an alpha channel: its pixels
    say)."""
    if not image.hasAlphaChannel():
        return False
    alpha = image.convertToFormat(QImage.Format.Format_Alpha8)
    width, height, stride = alpha.width(), alpha.height(), alpha.bytesPerLine()
    raw = bytes(alpha.constBits())[: stride * height]
    return any(min(raw[row * stride : row * stride + width]) < 255 for row in range(height))


class _CoverFiles:
    """The panel's cover files, as PanelCover's threads share them (never the QObject): one
    write at a time, the newest asked for only; the folder made by the first."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.newest = 0  # the newest write asked for
        self.folder = ""  # the private temporary folder ("": not made yet)
        self.closed = False

    def write(self, generation: int, image: QImage) -> str:
        """`image` as this write's file (its own name), the folder's only one: its path, or ""
        when a newer write was asked for meanwhile, QiTV closes, or it can't be written."""
        with self.lock:
            if self.closed or generation != self.newest:
                return ""
            path = ""
            try:
                if not self.folder:
                    self.folder = tempfile.mkdtemp(prefix="qitv-panel-")
                if image.width() > PANEL_SIDE or image.height() > PANEL_SIDE:
                    image = image.scaled(
                        PANEL_SIDE,
                        PANEL_SIDE,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                see_through = _see_through(image)  # a logo's: a JPEG would blacken it
                name = f"cover-{generation}.{'png' if see_through else 'jpg'}"
                target = os.path.join(self.folder, name)
                quality = -1 if see_through else PANEL_QUALITY
                if not image.save(target, "PNG" if see_through else "JPEG", quality):
                    raise OSError("not written")
                path = target
                for other in os.listdir(self.folder):
                    if other != name:
                        os.remove(os.path.join(self.folder, other))
            except OSError as error:
                logger.warning("The media panel's cover could not be written (%s)", error)
            if self.closed:  # QiTV closed while it wrote: nothing stays
                shutil.rmtree(self.folder, ignore_errors=True)
                return ""
            return path

    def close(self) -> None:
        self.closed = True
        if self.folder:
            shutil.rmtree(self.folder, ignore_errors=True)


class PanelCover(QObject):
    """The cover the system's media panel shows (TVWindow._show_now_playing): the audio
    screen's, as a small file (PANEL_SIDE) written on a daemon thread into a private temporary
    folder. One file at a time, each with a name of its own (a panel that keeps a picture by its
    file's name sees the new one); the folder goes when QiTV closes (close)."""

    written = Signal()  # the cover asked for last has its file now (path_for gives it)
    _ready = Signal(int, object, str)  # a write's generation, cover (cacheKey) and file

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._files = _CoverFiles()
        self._asked = 0  # the cacheKey of the cover asked for last (0: none)
        self._written = ""  # its file, once written
        self._ready.connect(self._on_ready)

    def path_for(self, image: Optional[QImage]) -> str:
        """`image`'s file, or "" (none, or not written yet: its write starts here; `written`
        says when it is). QiTV's drawn stand-in (None) has none: the panel shows QiTV's."""
        files = self._files
        if image is None or image.isNull() or files.closed:
            if self._asked:  # a write still to come is for nothing now
                self._asked, self._written = 0, ""
                files.newest += 1
            return ""
        key = image.cacheKey()
        if key == self._asked:
            return self._written
        # Another cover: its own write (an older one's file goes with it, so a cover asked
        # for again is written again).
        self._asked, self._written = key, ""
        files.newest += 1
        threading.Thread(
            target=self._write,
            args=(files, files.newest, key, image),
            name="qitv-panel-cover",
            daemon=True,
        ).start()
        return ""

    def _write(self, files: _CoverFiles, generation: int, key: int, image: QImage) -> None:
        path = files.write(generation, image)
        if not path:
            return
        try:
            self._ready.emit(generation, key, path)
        except RuntimeError:
            pass  # QiTV closed meanwhile

    def _on_ready(self, generation: int, key: int, path: str) -> None:
        files = self._files
        if files.closed or generation != files.newest or key != self._asked:
            return  # another cover was asked for since (its own file follows)
        self._written = path
        self.written.emit()

    def close(self) -> None:
        """QiTV closes: the folder goes with its file."""
        self._asked, self._written = 0, ""
        self._files.close()
