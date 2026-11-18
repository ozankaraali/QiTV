"""A song's cover and album, read on threads of their own (spec §10, the audio screen), and the
cover written for the system's media panel (PanelCover).

As TVWindow's file check and the episode listing do: each start runs on a daemon thread, which
creates no Qt objects and starts no timers, and its answer comes on the GUI thread as plain
data, numbered by the caller's generation (a late answer for an older title is the caller's to
drop). The one exception is the cover's QImage, decoded and scaled on the thread as
services.image_loader does for logos (a QImage is safe there; the GUI thread makes the pixmap).
"""

from __future__ import annotations

from collections.abc import Sequence
import hashlib
import logging
import os
import shutil
import tempfile
import threading
import time
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
PANEL_PREFIX = "qitv-panel-"  # its private temporary folder's name starts so
STALE_PANEL_S = 24 * 3600  # a folder of an earlier QiTV's this old is swept (one that crashed)


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
    still answered, with what it had (and as cut short).

    A playlist's songs (start_files) are read the same way, by name: the files it names only,
    in its order, never their folders; `read` answers with their Tracks (the first LIST_CAP,
    within LIST_BUDGET_S) and whether every one was read."""

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

    def start_files(self, generation: int, files: Sequence[tuple[str, Optional[int]]]) -> None:
        """Read the songs `files` names ((path, size or None), a playlist's, in its order)."""
        self._newest[0] = generation
        threading.Thread(
            target=self._read_files,
            args=(generation, list(files), self._newest),
            name="qitv-songs",
            daemon=True,
        ).start()

    def _read_files(
        self,
        generation: int,
        files: list[tuple[str, Optional[int]]],
        newest: list[Optional[int]],
    ) -> None:
        try:
            tracks = album_listing.read_tracks(
                files,
                budget_s=album_listing.LIST_BUDGET_S,
                cap=album_listing.LIST_CAP,
                stop=lambda: newest[0] != generation,
            )
        except Exception as error:  # read_tracks never raises; never unanswered all the same
            logger.warning("A playlist's songs could not be read (%s)", type(error).__name__)
            tracks = []
        try:
            self.read.emit(generation, tracks, len(tracks) == len(files))
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


def _content_key(image: QImage) -> str:
    """A name for `image`'s pixels: the same picture (an album's next track's cover, a logo
    drawn again) is the same file."""
    pixels = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    digest = hashlib.sha1(f"{pixels.width()}x{pixels.height()}".encode())
    digest.update(bytes(pixels.constBits())[: pixels.bytesPerLine() * pixels.height()])
    return digest.hexdigest()[:16]


def _sweep_stale(root: str, now: float) -> None:
    """Earlier QiTVs' panel folders (one that crashed keeps its own) older than a day."""
    try:
        entries = list(os.scandir(root))
    except OSError:
        return
    for entry in entries:
        try:
            if (
                entry.name.startswith(PANEL_PREFIX)
                and entry.is_dir(follow_symlinks=False)
                and now - entry.stat(follow_symlinks=False).st_mtime > STALE_PANEL_S
            ):
                shutil.rmtree(entry.path, ignore_errors=True)
        except OSError:
            continue


class _CoverFiles:
    """The panel's cover files, as PanelCover's threads share them (never the QObject): one
    write at a time, the newest asked for only; the folder made by the first (which sweeps
    earlier QiTVs' stale ones first)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.newest = 0  # the newest write asked for
        self.folder = ""  # the private temporary folder ("": not made yet)
        self.closed = False

    def write(self, generation: int, image: QImage, keep: str) -> Optional[tuple[str, bytes]]:
        """`image` as a file named by its pixels, and that file's bytes; the folder keeps it
        and `keep` (the file the panel shows until this one lands) only. None when a newer
        write was asked for meanwhile, QiTV closes, or it can't be written."""
        with self.lock:
            if self.closed or generation != self.newest:
                return None
            try:
                if not self.folder:
                    root = tempfile.gettempdir()
                    _sweep_stale(root, time.time())
                    self.folder = tempfile.mkdtemp(prefix=PANEL_PREFIX, dir=root)
                if image.width() > PANEL_SIDE or image.height() > PANEL_SIDE:
                    image = image.scaled(
                        PANEL_SIDE,
                        PANEL_SIDE,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                see_through = _see_through(image)  # a logo's: a JPEG would blacken it
                name = f"cover-{_content_key(image)}.{'png' if see_through else 'jpg'}"
                target = os.path.join(self.folder, name)
                if not os.path.isfile(target):  # the same picture: its file as it is
                    quality = -1 if see_through else PANEL_QUALITY
                    if not image.save(target, "PNG" if see_through else "JPEG", quality):
                        raise OSError("not written")
                with open(target, "rb") as file:
                    data = file.read()
                self._remove_but(name, os.path.basename(keep))
                return target, data
            except OSError as error:
                logger.warning("The media panel's cover could not be written (%s)", error)
                return None

    def keep_only(self, generation: int, path: str) -> None:
        """The panel shows `path` now: the folder's other file goes (unless a newer write was
        asked for since: that one sweeps)."""
        with self.lock:
            if self.closed or generation != self.newest:
                return
            try:
                self._remove_but(os.path.basename(path))
            except OSError as error:
                logger.warning("An old media panel cover stays (%s)", error)

    def _remove_but(self, *names: str) -> None:
        for other in os.listdir(self.folder):
            if other not in names:
                os.remove(os.path.join(self.folder, other))

    def close(self) -> None:
        with self.lock:  # (a write under way finishes first: at most one small file)
            self.closed = True
            if self.folder:
                shutil.rmtree(self.folder, ignore_errors=True)


class PanelCover(QObject):
    """The cover the system's media panel shows (TVWindow._show_now_playing): the audio
    screen's, as a small file (PANEL_SIDE) written on a daemon thread into a private temporary
    folder, with its bytes (macOS makes its picture of them). The file is named by its pixels:
    an album's next track, whose cover is the same picture, keeps it and nothing is sent again;
    another picture is a file of its own (a panel that keeps a picture by its file's name sees
    the new one), the last one shown until it lands. The folder goes when QiTV closes."""

    written = Signal()  # the cover asked for last has its file now (file_for gives it)
    _ready = Signal(int, object, str, object)  # a write's generation, cover key, file, bytes

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._files = _CoverFiles()
        self._asked = 0  # the cacheKey of the cover asked for last (0: none)
        self._written: tuple[str, bytes] = ("", b"")  # the last one's file and bytes
        self._ready.connect(self._on_ready)

    def file_for(self, image: Optional[QImage]) -> tuple[str, bytes]:
        """`image`'s file and its bytes: until it is written (its write starts here; `written`
        says when it is), the last cover's. QiTV's drawn stand-in (None) has none: the panel
        shows QiTV's."""
        files = self._files
        if image is None or image.isNull() or files.closed:
            if self._asked or self._written[0]:  # a write still to come is for nothing now
                self._asked, self._written = 0, ("", b"")
                files.newest += 1
            return ("", b"")
        key = image.cacheKey()
        if key != self._asked:
            self._asked = key
            files.newest += 1
            threading.Thread(
                target=self._write,
                # A copy of the screen's picture (shared until either changes it).
                args=(files, files.newest, key, QImage(image), self._written[0]),
                name="qitv-panel-cover",
                daemon=True,
            ).start()
        return self._written

    def _write(
        self, files: _CoverFiles, generation: int, key: int, image: QImage, keep: str
    ) -> None:
        written = files.write(generation, image, keep)
        if written is None:
            return
        try:
            self._ready.emit(generation, key, *written)
        except RuntimeError:
            pass  # QiTV closed meanwhile

    def _on_ready(self, generation: int, key: int, path: str, data: bytes) -> None:
        files = self._files
        if files.closed or generation != files.newest or key != self._asked:
            return  # another cover was asked for since (its own file follows)
        self._written = (path, data)
        threading.Thread(
            target=files.keep_only, args=(generation, path), name="qitv-panel-cover", daemon=True
        ).start()
        self.written.emit()

    def close(self) -> None:
        """QiTV closes: the folder goes with its file."""
        self._asked, self._written = 0, ("", b"")
        self._files.close()
