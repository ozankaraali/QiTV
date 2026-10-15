"""GUI-thread logo cache: batches URL downloads and builds QPixmaps on the GUI thread."""

import logging

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QPixmap

from image_loader import ImageLoader
from services.thread_cleanup import ThreadCleanup

logger = logging.getLogger(__name__)


class LogoCache(QObject):
    logoReady = Signal(str)

    def __init__(self, image_manager, config_manager, parent=None) -> None:
        super().__init__(parent)
        self._image_manager = image_manager
        self._config = config_manager
        self._pixmaps: dict[str, QPixmap] = {}
        self._seen: set[str] = set()
        self._queue: list[str] = []
        self._loader: ImageLoader | None = None
        self._batch: list[str] = []

    def pixmap(self, url: str) -> QPixmap | None:
        return self._pixmaps.get(url)

    def request(self, urls: list[str]) -> None:
        if not self._config.channel_logos:
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
        loader.progress_updated.connect(
            lambda cur, tot, data, ld=loader: self._on_progress(ld, cur, tot, data),
            Qt.QueuedConnection,
        )
        ThreadCleanup(loader).finished.connect(self._on_cleanup)
        loader.start()

    def _on_progress(self, loader, current: int, total: int, data: dict) -> None:
        if loader is not self._loader or not data:
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
        self._pixmaps[url] = pixmap
        self.logoReady.emit(url)

    def _on_cleanup(self, _thread) -> None:
        self._loader = None
        self._batch = []
        self._start_next()
