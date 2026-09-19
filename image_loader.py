import asyncio
import logging

from PySide6.QtCore import QThread, Signal
import aiohttp

logger = logging.getLogger(__name__)


class ImageLoader(QThread):
    progress_updated = Signal(int, int, dict)

    def __init__(
        self,
        image_urls,
        image_manager,
        iconified=False,
        verify_ssl=True,
        refresh_cache=False,
    ):
        super().__init__()
        self.image_urls = image_urls
        self.image_manager = image_manager
        self.iconified = iconified
        self.verify_ssl = verify_ssl
        self.refresh_cache = refresh_cache
        self._loop = None
        self._load_task = None

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
                return {"rank": image_rank, "cache_path": cache_path, "iconified": self.iconified}
        except Exception as e:
            logger.warning(f"Error fetching image {image_url}: {e}")
            raise
        return None

    async def decode_base64_image(self, image_rank, image_str):
        try:
            # Cache decoded image on disk in worker thread
            cache_path = await self.image_manager.cache_image_from_base64(image_str, self.iconified)
            if cache_path:
                return {"rank": image_rank, "cache_path": cache_path, "iconified": self.iconified}
        except Exception as e:
            logger.warning(f"Error decoding base64 image : {e}")
            raise
        return None

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

                    for current, task in enumerate(asyncio.as_completed(tasks), 1):
                        try:
                            image_item = await task
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
