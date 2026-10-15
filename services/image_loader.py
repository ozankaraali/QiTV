import logging

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QImage, QImageReader

from services.lazy_import import lazy_module

logger = logging.getLogger(__name__)
aiohttp = lazy_module("aiohttp")
asyncio = lazy_module("asyncio")


def read_scaled(path: str, max_side: int) -> QImage | None:
    """The image at `path`, scaled so its long side is at most `max_side` pixels.

    A QImage, not a QPixmap: safe on a worker thread. None when the file is unreadable.
    """
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    size = reader.size()
    if size.isValid() and max(size.width(), size.height()) > max_side:
        reader.setScaledSize(size.scaled(max_side, max_side, Qt.AspectRatioMode.KeepAspectRatio))
    image = reader.read()
    if image.isNull():
        return None
    if max(image.width(), image.height()) > max_side:  # a format that ignored the scaled size
        image = image.scaled(
            max_side,
            max_side,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    return image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)


class ImageLoader(QThread):
    """Caches images on disk; with `decode_side`, also decodes them for the GUI.

    Each result dict then carries "image": a QImage scaled to `decode_side` pixels on this
    thread, or None when the file could not be decoded. The GUI only wraps it in a QPixmap.
    """

    progress_updated = Signal(int, int, dict)

    def __init__(
        self,
        image_urls,
        image_manager,
        iconified=False,
        verify_ssl=True,
        refresh_cache=False,
        decode_side: int | None = None,
    ):
        super().__init__()
        self.image_urls = image_urls
        self.image_manager = image_manager
        self.iconified = iconified
        self.verify_ssl = verify_ssl
        self.refresh_cache = refresh_cache
        self.decode_side = decode_side
        self._loop: asyncio.AbstractEventLoop | None = None
        self._load_task: asyncio.Task | None = None

    def cancel(self):
        """Interrupt pending I/O without blocking the GUI thread."""
        self.requestInterruption()
        loop = self._loop
        task = self._load_task
        if loop is not None and task is not None:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                # The loop can finish between reading it and posting cancellation.
                pass

    async def fetch_image(self, session, image_rank, image_url):
        try:
            # Cache image on disk in worker thread, avoid creating GUI objects here
            cache_path = await self.image_manager.cache_image_from_url(
                session, image_url, self.iconified
            )
            if cache_path:
                return self._result(image_rank, cache_path)
        except Exception as e:
            logger.warning(f"Error fetching image {image_url}: {e}")
            raise
        return None

    async def decode_base64_image(self, image_rank, image_str):
        # Cache decoded image on disk in worker thread; the manager logs decode/write
        # failures and returns None for them.
        cache_path = await self.image_manager.cache_image_from_base64(image_str, self.iconified)
        if cache_path:
            return self._result(image_rank, cache_path)
        return None

    def _result(self, image_rank, cache_path) -> dict:
        result = {"rank": image_rank, "cache_path": cache_path, "iconified": self.iconified}
        if self.decode_side:
            result["image"] = read_scaled(cache_path, self.decode_side)  # on this thread
        return result

    async def load_images(self):
        self._loop = asyncio.get_running_loop()
        self._load_task = asyncio.current_task()
        tasks = []
        invalidated: set[str] | None = set() if self.refresh_cache else None
        try:
            if self.isInterruptionRequested():
                return
            connector = aiohttp.TCPConnector(ssl=self.verify_ssl)
            async with aiohttp.ClientSession(connector=connector) as session:
                try:
                    for image_rank, url in enumerate(self.image_urls):
                        if self.isInterruptionRequested():
                            return
                        if not url:
                            continue
                        if invalidated is not None and url not in invalidated:
                            self.image_manager.remove_icon_from_cache(url)
                            invalidated.add(url)
                        if url.startswith(("http://", "https://")):
                            coroutine = self.fetch_image(session, image_rank, url)
                        elif url.startswith("data:image"):
                            coroutine = self.decode_base64_image(image_rank, url)
                        else:
                            continue
                        tasks.append(asyncio.create_task(coroutine))
                    image_count = len(tasks)

                    for current, completion in enumerate(asyncio.as_completed(tasks), 1):
                        try:
                            image_item = await completion
                        except Exception as e:
                            image_item = None
                            logger.info(f"Image task failed: {e}")
                        if self.isInterruptionRequested():
                            return
                        self.progress_updated.emit(current, image_count, image_item or {})
                finally:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    if tasks:
                        await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self._load_task = None
            self._loop = None

    def run(self):
        try:
            asyncio.run(self.load_images())
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"Error in image loading: {e}")
