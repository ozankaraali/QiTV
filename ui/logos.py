"""GUI-thread image caches (channel logos, library posters).

Batches URL downloads; the loader thread decodes and scales each image into a QImage, and
the GUI thread only wraps it in a QPixmap, kept within a byte budget.
"""

from collections import OrderedDict
from functools import partial
import logging

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QGuiApplication, QImage, QPixmap

from services.image_loader import ImageLoader, read_scaled
from services.redact import redact_url
from services.thread_cleanup import ThreadCleanup

logger = logging.getLogger(__name__)

MIB = 1024 * 1024


def _cost(pixmap: QPixmap) -> int:
    """The bytes a pixmap holds (32-bit pixels)."""
    return pixmap.width() * pixmap.height() * 4


class ImageCache(QObject):
    """LRU pixmap cache within MAX_BYTES, fed by batched ImageLoader downloads.

    Subclasses set the limits. Full-size images are never kept: every stored pixmap is
    scaled so its long side is at most MAX_SIDE logical pixels (times devicePixelRatio),
    on the loader thread.
    """

    MAX_SIDE = 192  # logical pixels; scaled by devicePixelRatio when stored
    MAX_BYTES = 48 * MIB
    ICONIFIED = True
    MAX_RETRIES = 2

    # Emitted with the URL once its pixmap is available. The name is kept for every
    # cache (posters included) so consumers connect the same way.
    logoReady = Signal(str)

    def __init__(self, image_manager, config_manager, parent=None) -> None:
        super().__init__(parent)
        self._image_manager = image_manager
        self._config = config_manager
        self._pixmaps: OrderedDict[str, QPixmap] = OrderedDict()
        self._bytes = 0
        self._retries: dict[str, int] = {}
        self._closed = False
        self._seen: set[str] = set()
        self._queue: list[str] = []
        self._loader: ImageLoader | None = None
        self._batch: list[str] = []
        self._generation = 0

    def _enabled(self) -> bool:
        return True

    def pixmap(self, url: str) -> QPixmap | None:
        pixmap = self._pixmaps.get(url)
        if pixmap is not None:
            self._pixmaps.move_to_end(url)
        return pixmap

    def failed(self, url: str) -> bool:
        """Whether `url` could not be loaded (once at least) and has not loaded since."""
        return self._retries.get(url, 0) > 0 and url not in self._pixmaps

    def size_bytes(self) -> int:
        """The bytes the stored pixmaps hold (at most MAX_BYTES, but for one larger image)."""
        return self._bytes

    def shutdown(self) -> None:
        """Stop loading; late results are ignored."""
        self._closed = True
        self._queue.clear()
        if self._loader is not None:
            self._loader.cancel()

    def request(self, urls: list[str], replace_pending: bool = False) -> None:
        """Queue `urls` for loading.

        With `replace_pending`, URLs still waiting in the queue are dropped first (the batch
        in flight keeps loading), so a view that scrolled on does not wait behind rows it
        has already left. Dropped URLs may be requested again later.
        """
        if self._closed or not self._enabled():
            return
        if replace_pending:
            for url in self._queue:
                self._seen.discard(url)
            self._queue.clear()
        for url in urls:
            if url and url not in self._seen:
                self._seen.add(url)
                self._queue.append(url)
        self._start_next()

    def _start_next(self) -> None:
        if self._loader is not None or not self._queue:
            return
        self._batch, self._queue = self._queue, []
        loader = ImageLoader(
            list(self._batch),
            self._image_manager,
            iconified=self.ICONIFIED,
            verify_ssl=self._config.ssl_verify,
            decode_side=self._side(),
        )
        self._loader = loader
        self._generation += 1
        loader.progress_updated.connect(
            partial(self._on_progress, generation=self._generation), Qt.QueuedConnection
        )
        ThreadCleanup(loader).finished.connect(self._on_cleanup)
        loader.start()

    def _on_progress(
        self, current: int, total: int, data: dict, generation: int | None = None
    ) -> None:
        if self._closed or not data:
            return
        # Bound at connect time, so a superseded batch is dropped without sender().
        if generation is not None and generation != self._generation:
            return
        rank = data.get("rank")
        path = data.get("cache_path")
        if not isinstance(rank, int) or not 0 <= rank < len(self._batch) or not path:
            return
        url = self._batch[rank]
        if "image" in data:
            image = data["image"]  # decoded and scaled on the loader thread
        else:
            image = read_scaled(path, self._side())  # a loader that does not decode
        if not isinstance(image, QImage) or image.isNull():
            logger.info("Unreadable cached image for %s", redact_url(url))
            return
        self._store(url, QPixmap.fromImage(image))  # GUI thread only
        self.logoReady.emit(url)

    def _side(self) -> int:
        """MAX_SIDE in device pixels."""
        app = QGuiApplication.instance()
        dpr = app.devicePixelRatio() if isinstance(app, QGuiApplication) else 1.0
        return max(1, round(self.MAX_SIDE * dpr))

    def _store(self, url: str, pixmap: QPixmap) -> None:
        old = self._pixmaps.pop(url, None)
        if old is not None:
            self._bytes -= _cost(old)
        self._pixmaps[url] = pixmap
        self._bytes += _cost(pixmap)
        while self._bytes > self.MAX_BYTES and len(self._pixmaps) > 1:
            evicted, gone = self._pixmaps.popitem(last=False)
            self._bytes -= _cost(gone)
            self._seen.discard(evicted)  # may be requested again

    def _on_cleanup(self, _thread) -> None:
        for url in self._batch:
            if url not in self._pixmaps and url in self._seen:
                tries = self._retries.get(url, 0) + 1
                self._retries[url] = tries
                if tries <= self.MAX_RETRIES:
                    self._seen.discard(url)  # failed: allow a later retry
        self._loader = None
        self._batch = []
        if not self._closed:
            self._start_next()


class LogoCache(ImageCache):
    """Channel logos at the size the banner paints them (8u of a ~1600 px host).

    One size for every use: rows (about 4u) scale down from it when they paint, so each
    logo is decoded and kept once. A logo costs at most 64 KiB (256 KiB at 2× DPR), so
    48 MiB holds 750 (190 at 2×) even when square; most logos are wider than tall.
    """

    MAX_SIDE = 128
    MAX_BYTES = 48 * MIB
    ICONIFIED = True

    def _enabled(self) -> bool:
        return bool(self._config.channel_logos)


class PosterCache(ImageCache):
    """Library posters: always on, larger than logos (a 2:3 poster is ~1.4 MB at 2× DPR)."""

    MAX_SIDE = 360
    MAX_BYTES = 96 * MIB
    ICONIFIED = False
