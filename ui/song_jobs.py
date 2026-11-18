"""A song's cover and album, read on threads of their own (spec §10, the audio screen).

As TVWindow's file check and the episode listing do: each start runs on a daemon thread, which
creates no Qt objects and starts no timers, and its answer comes on the GUI thread as plain
data, numbered by the caller's generation (a late answer for an older title is the caller's to
drop). The one exception is the cover's QImage, decoded and scaled on the thread as
services.image_loader does for logos (a QImage is safe there; the GUI thread makes the pixmap).
"""

from __future__ import annotations

import logging
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
