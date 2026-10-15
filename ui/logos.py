"""GUI-thread logo cache: batches URL downloads and builds QPixmaps on the GUI thread."""

from collections import OrderedDict
import logging

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QGuiApplication, QPixmap

from image_loader import ImageLoader
from services.thread_cleanup import ThreadCleanup

logger = logging.getLogger(__name__)

MAX_LOGO_SIDE = 192  # logical pixels; scaled by devicePixelRatio when stored
MAX_ENTRIES = 2000
MAX_RETRIES = 2


class LogoCache(QObject):
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

    def request(self, urls: list[str]) -> None:
        if self._closed or not self._config.channel_logos:
            return
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
            iconified=True,
            verify_ssl=self._config.ssl_verify,
        )
        self._loader = loader
        loader.progress_updated.connect(self._on_progress, Qt.QueuedConnection)
        ThreadCleanup(loader).finished.connect(self._on_cleanup)
        loader.start()

    def _on_progress(self, current: int, total: int, data: dict) -> None:
        if self._closed or not data:
            return
        sender = self.sender()  # None when invoked directly
        if sender is not None and sender is not self._loader:
            return
        rank = data.get("rank")
        path = data.get("cache_path")
        if not isinstance(rank, int) or not 0 <= rank < len(self._batch) or not path:
            return
        url = self._batch[rank]
        pixmap = QPixmap(path)  # GUI thread only
        if pixmap.isNull():
            logger.info("Unreadable cached logo for %s", url)
            return
        self._store(url, self._bounded(pixmap))
        self.logoReady.emit(url)

    @staticmethod
    def _bounded(pixmap: QPixmap) -> QPixmap:
        app = QGuiApplication.instance()
        dpr = app.devicePixelRatio() if isinstance(app, QGuiApplication) else 1.0
        limit = max(1, round(MAX_LOGO_SIDE * dpr))
        if max(pixmap.width(), pixmap.height()) <= limit:
            return pixmap
        return pixmap.scaled(limit, limit, Qt.KeepAspectRatio, Qt.SmoothTransformation)

    def _store(self, url: str, pixmap: QPixmap) -> None:
        self._pixmaps[url] = pixmap
        self._pixmaps.move_to_end(url)
        while len(self._pixmaps) > MAX_ENTRIES:
            evicted, _ = self._pixmaps.popitem(last=False)
            self._seen.discard(evicted)  # may be requested again

    def _on_cleanup(self, _thread) -> None:
        for url in self._batch:
            if url not in self._pixmaps and url in self._seen:
                tries = self._retries.get(url, 0) + 1
                self._retries[url] = tries
                if tries <= MAX_RETRIES:
                    self._seen.discard(url)  # failed: allow a later retry
        self._loader = None
        self._batch = []
        if not self._closed:
            self._start_next()
