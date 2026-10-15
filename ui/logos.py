"""GUI-thread image caches (channel logos, library posters).

Batches URL downloads and builds bounded QPixmaps on the GUI thread.
"""

from collections import OrderedDict
from functools import partial
import logging

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QGuiApplication, QPixmap

from image_loader import ImageLoader
from services.thread_cleanup import ThreadCleanup

logger = logging.getLogger(__name__)


class ImageCache(QObject):
    """Bounded LRU pixmap cache fed by batched ImageLoader downloads.

    Subclasses set the limits. Full-size images are never kept: every stored pixmap is
    scaled so its long side is at most MAX_SIDE logical pixels (times devicePixelRatio).
    """

    MAX_SIDE = 192  # logical pixels; scaled by devicePixelRatio when stored
    MAX_ENTRIES = 2000
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
        pixmap = QPixmap(path)  # GUI thread only
        if pixmap.isNull():
            logger.info("Unreadable cached image for %s", url)
            return
        self._store(url, self._bounded(pixmap))
        self.logoReady.emit(url)

    def _bounded(self, pixmap: QPixmap) -> QPixmap:
        app = QGuiApplication.instance()
        dpr = app.devicePixelRatio() if isinstance(app, QGuiApplication) else 1.0
        limit = max(1, round(self.MAX_SIDE * dpr))
        if max(pixmap.width(), pixmap.height()) <= limit:
            return pixmap
        return pixmap.scaled(limit, limit, Qt.KeepAspectRatio, Qt.SmoothTransformation)

    def _store(self, url: str, pixmap: QPixmap) -> None:
        self._pixmaps[url] = pixmap
        self._pixmaps.move_to_end(url)
        while len(self._pixmaps) > self.MAX_ENTRIES:
            evicted, _ = self._pixmaps.popitem(last=False)
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
    MAX_SIDE = 192
    MAX_ENTRIES = 2000
    ICONIFIED = True

    def _enabled(self) -> bool:
        return bool(self._config.channel_logos)


class PosterCache(ImageCache):
    """Library posters: always on, larger than logos, fewer kept."""

    MAX_SIDE = 360
    MAX_ENTRIES = 400
    ICONIFIED = False
