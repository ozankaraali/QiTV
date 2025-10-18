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
    ):
        super().__init__()
        self.image_urls = image_urls
        self.image_manager = image_manager
        self.iconified = iconified

    async def fetch_image(self, session, image_rank, image_url):
        try:
            # Use ImageManager to get QIcon or QPixmap
            image = await self.image_manager.get_image_from_url(session, image_url, self.iconified)
            if image:
                if self.iconified:
                    return {"rank": image_rank, "icon": image}
                else:
                    return {"pixmap": image}
        except Exception as e:
            logger.warning(f"Error fetching image {image_url}: {e}")
            raise
        return None

    async def decode_base64_image(self, image_rank, image_str):
        try:
            # Use ImageManager to get QIcon or QPixmap
            image = await self.image_manager.get_image_from_base64(image_str, self.iconified)
            if image:
                if self.iconified:
                    return {"rank": image_rank, "icon": image}
                else:
                    return {"pixmap": image}
        except Exception as e:
            logger.warning(f"Error decoding base64 image : {e}")
            raise
        return None

    async def load_images(self):
        async with aiohttp.ClientSession() as session:
            tasks = []
            for image_rank, url in enumerate(self.image_urls):
                if url:
                    if url.startswith(("http://", "https://")):
                        tasks.append(self.fetch_image(session, image_rank, url))
                    elif url.startswith("data:image"):
                        tasks.append(self.decode_base64_image(image_rank, url))
            image_count = len(tasks)

            for i, task in enumerate(asyncio.as_completed(tasks), 1):
                try:
                    image_item = await task
                except Exception as e:
                    image_item = None
                    logger.warning(f"Error processing image task: {e}")
                finally:
                    self.progress_updated.emit(i, image_count, image_item)

    def run(self):
        try:
            asyncio.run(self.load_images())
        except Exception as e:
            logger.warning(f"Error in image loading: {e}")
