import asyncio
import base64
from collections import OrderedDict
from datetime import datetime
import hashlib
from io import BytesIO
import logging
import os
import random

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QPixmap
import aiohttp
import orjson as json

logger = logging.getLogger(__name__)


class ImageManager:
    def __init__(
        self, config_manager, max_cache_size=50 * 1024 * 1024
    ):  # Default max cache size: 50 MB
        self.cache_dir = os.path.join(config_manager.get_config_dir(), "cache", "image")
        os.makedirs(self.cache_dir, exist_ok=True)
        self.index_file = os.path.join(self.cache_dir, "index.json")
        self.cache = (
            OrderedDict()
        )  # cache is an ordered dict where last accessed items are at the end
        self.max_cache_size = max_cache_size
        self.current_cache_size = 0
        self._load_index()

    async def get_image_from_base64(self, image_str, iconified):
        image_type = "qicon" if iconified else "qpixmap"
        ext = "png" if iconified else "jpg"
        image_hash = self._hash_string(image_str + ext)

        # Check cache first
        cached_image, found = self._get_cached(image_hash, ext, image_type)
        if found:
            return cached_image

        # Check if file exists on disk but not in memory cache
        cache_path = os.path.join(self.cache_dir, f"{image_hash}.{ext}")
        if os.path.exists(cache_path):
            image = QPixmap(cache_path, "PNG" if iconified else "JPG")
            if iconified:
                image = QIcon(image)
            self._store_cached(image_hash, image_type, image, os.path.getsize(cache_path))
            return image

        # Extract and decode base64 data from the image string
        base64_data = image_str.split(",", 1)[1]
        image_data = base64.b64decode(base64_data)
        image = QPixmap()
        if image.loadFromData(image_data):
            if iconified:
                image = image.scaled(64, 64, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            else:
                image = image.scaled(300, 400, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            if image.save(cache_path, "PNG" if iconified else "JPG"):
                if iconified:
                    image = QIcon(image)
                self._store_cached(image_hash, image_type, image, os.path.getsize(cache_path))
                return image
        self.cache[image_hash] = None
        return None

    async def get_image_from_url(self, session, url, iconified, max_retries=2, timeout=5):
        image_type = "qicon" if iconified else "qpixmap"
        ext = "png" if iconified else "jpg"
        url_hash = self._hash_string(url + ext)

        # Check cache first
        cached_image, found = self._get_cached(url_hash, ext, image_type)
        if found:
            return cached_image

        # Check if file exists on disk but not in memory cache
        cache_path = os.path.join(self.cache_dir, f"{url_hash}.{ext}")
        if os.path.exists(cache_path):
            image = QPixmap(cache_path, "PNG" if iconified else "JPG")
            if iconified:
                image = QIcon(image)
            self._store_cached(url_hash, image_type, image, os.path.getsize(cache_path))
            return image

        for attempt in range(max_retries):
            try:
                async with session.get(url, timeout=timeout) as response:
                    content = await response.read()
                    if response.status == 503 or not content:
                        if attempt == max_retries - 1:
                            continue
                        wait_time = (2**attempt) + random.uniform(0, 1)
                        logger.debug(
                            f"Received error or empty response. Retrying in {wait_time:.2f} seconds..."
                        )
                        await asyncio.sleep(wait_time)
                        continue

                    # check if content type is image
                    if response.headers.get("content-type", "").startswith("image/"):
                        image_data = BytesIO(content)
                        image = QPixmap()
                        if image.loadFromData(image_data.read()):
                            if iconified:
                                image = image.scaled(
                                    64, 64, Qt.KeepAspectRatio, Qt.SmoothTransformation
                                )
                            else:
                                image = image.scaled(
                                    300,
                                    400,
                                    Qt.KeepAspectRatio,
                                    Qt.SmoothTransformation,
                                )
                            if image.save(cache_path, "PNG" if iconified else "JPG"):
                                if iconified:
                                    image = QIcon(image)
                                self._store_cached(
                                    url_hash, image_type, image, os.path.getsize(cache_path)
                                )
                                return image
                    self.cache[url_hash] = None
                    return None
            except (
                aiohttp.ClientError,
                asyncio.TimeoutError,
            ) as e:
                logger.info(f"Error fetching image: {e}")
                if attempt == max_retries - 1:
                    self.cache[url_hash] = None
                    return None
                wait_time = (2**attempt) + random.uniform(0, 1)
                logger.debug(f"Retrying in {wait_time:.2f} seconds...")
                await asyncio.sleep(wait_time)

        self.cache[url_hash] = None
        return None

    async def cache_image_from_url(self, session, url, iconified, max_retries=2, timeout=5):
        """Download image bytes and store to cache on disk only.
        Returns absolute cache file path or None.
        Safe to call from worker threads (no GUI objects created).
        """
        ext = "png" if iconified else "jpg"
        url_hash = self._hash_string(url + ext)
        cache_path = os.path.join(self.cache_dir, f"{url_hash}.{ext}")

        # If already cached on disk, update metadata and return
        if os.path.exists(cache_path):
            self._touch_cache_entry(url_hash, cache_path)
            return cache_path

        # Download and write to disk
        for attempt in range(max_retries):
            try:
                async with session.get(url, timeout=timeout) as response:
                    content = await response.read()
                    if response.status == 503 or not content:
                        if attempt == max_retries - 1:
                            break
                        wait_time = (2**attempt) + random.uniform(0, 1)
                        logger.debug(
                            f"Received error or empty response. Retrying in {wait_time:.2f} seconds..."
                        )
                        await asyncio.sleep(wait_time)
                        continue

                    # Persist bytes. We skip pre-scaling here to avoid GUI usage off-thread.
                    try:
                        with open(cache_path, "wb") as f:
                            f.write(content)
                        self._touch_cache_entry(url_hash, cache_path)
                        self._manage_cache_size()
                        return cache_path
                    except OSError as e:
                        logger.warning(f"Error writing image to cache: {e}")
                        return None
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.info(f"Error fetching image: {e}")
                if attempt == max_retries - 1:
                    break
                wait_time = (2**attempt) + random.uniform(0, 1)
                logger.debug(f"Retrying in {wait_time:.2f} seconds...")
                await asyncio.sleep(wait_time)
        return None

    async def cache_image_from_base64(self, image_str, iconified):
        """Decode base64 image and store to cache on disk only. Returns path or None."""
        ext = "png" if iconified else "jpg"
        image_hash = self._hash_string(image_str + ext)
        cache_path = os.path.join(self.cache_dir, f"{image_hash}.{ext}")

        if os.path.exists(cache_path):
            self._touch_cache_entry(image_hash, cache_path)
            return cache_path

        try:
            base64_data = image_str.split(",", 1)[1]
            image_data = base64.b64decode(base64_data)
            with open(cache_path, "wb") as f:
                f.write(image_data)
            self._touch_cache_entry(image_hash, cache_path)
            self._manage_cache_size()
            return cache_path
        except Exception as e:
            logger.warning(f"Error decoding/saving base64 image: {e}")
            return None

    def _touch_cache_entry(self, key_hash, cache_path):
        # Update cache metadata without creating GUI objects
        size = os.path.getsize(cache_path)
        entry = self.cache.get(key_hash, {}) or {}
        entry["size"] = size
        entry["last_access"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.cache[key_hash] = entry
        # maintain LRU order
        try:
            self.cache.move_to_end(key_hash)
        except Exception:
            pass

    def clear_cache(self):
        for filename in os.listdir(self.cache_dir):
            file_path = os.path.join(self.cache_dir, filename)
            if os.path.isfile(file_path):
                os.remove(file_path)
        self.cache.clear()
        self.current_cache_size = 0
        # Save empty cache index
        self.save_index()

    def remove_icon_from_cache(self, url):
        ext = "png"
        url_hash = self._hash_string(url + ext)
        if url_hash in self.cache:
            cache_path = os.path.join(self.cache_dir, f"{url_hash}.{ext}")
            if os.path.exists(cache_path):
                file_size = os.path.getsize(cache_path)
                os.remove(cache_path)
                self.current_cache_size -= file_size
            self.cache.pop(url_hash)

    def _hash_string(self, url):
        return hashlib.sha256(url.encode("utf-8")).hexdigest()

    def _get_cached(self, cache_hash, ext, image_type):
        """Look up cache entry, update LRU order, and return cached image or None.

        Returns a tuple (image_or_none, found_in_cache). If found_in_cache is True
        but image_or_none is None, the entry was a negative cache hit (previously failed).
        If found_in_cache is False, the caller should attempt to fetch/decode the image.
        """
        if cache_hash not in self.cache:
            return None, False

        entry = self.cache[cache_hash]
        if entry is None:
            # Negative cache hit - previously failed to load
            return None, True

        # Update LRU metadata
        entry["last_access"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.cache.move_to_end(cache_hash)

        cache_path = os.path.join(self.cache_dir, f"{cache_hash}.{ext}")
        if not os.path.exists(cache_path):
            # File was deleted externally; remove stale entry
            self.current_cache_size -= entry.get("size", 0)
            self.cache.pop(cache_hash)
            return None, False

        # Return cached image object if already loaded
        if image_type in entry:
            return entry[image_type], True

        # Load from disk and cache the object
        image = QPixmap(cache_path, "PNG" if ext == "png" else "JPG")
        if ext == "png":
            image = QIcon(image)
        entry[image_type] = image
        return image, True

    def _store_cached(self, cache_hash, image_type, image, file_size):
        """Store an image in the cache with LRU tracking and size management."""
        self.cache[cache_hash] = {
            image_type: image,
            "size": file_size,
            "last_access": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.current_cache_size += file_size
        self.cache.move_to_end(cache_hash)
        self._manage_cache_size()

    def _load_index(self):
        self.cache.clear()
        if os.path.exists(self.index_file):
            with open(self.index_file, "r", encoding="utf-8") as f:
                try:
                    data = json.loads(f.read())
                    self.cache = OrderedDict(data) if data else OrderedDict()
                except (json.JSONDecodeError, IOError) as e:
                    logger.warning(f"Error loading index file: {e}")

        # Add missing keys to the cache
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for filename in os.listdir(self.cache_dir):
            if filename.endswith(".png") or filename.endswith(".jpg"):
                image_hash = filename.split(".")[0]
                if image_hash not in self.cache:
                    iconified = filename.endswith(".png")
                    image_type = "qicon" if iconified else "qpixmap"
                    cache_path = os.path.join(self.cache_dir, filename)
                    image = QPixmap(cache_path, "PNG" if iconified else "JPG")
                    if iconified:
                        image = QIcon(image)
                    self.cache[image_hash] = {
                        image_type: image,
                        "size": os.path.getsize(cache_path),
                        "last_access": now,
                    }

        self.current_cache_size = sum(entry["size"] for entry in self.cache.values() if entry)

    def save_index(self):
        index_data = {
            url: (
                {k: v for k, v in data.items() if k not in ["qicon", "qpixmap"]} if data else None
            )
            for url, data in self.cache.items()
        }
        with open(self.index_file, "w", encoding="utf-8") as f:
            f.write(json.dumps(index_data, option=json.OPT_INDENT_2).decode("utf-8"))

    def _manage_cache_size(self):
        # Remove oldest accessed items until cache size is within limits
        while self.current_cache_size > self.max_cache_size:
            # Pick the oldest accessed item (reminder: cache is an ordered dict where last accessed items are at the end)
            oldest_hash, oldest_data = self.cache.popitem(last=False)
            ext = "png" if "qicon" in oldest_data else "jpg"
            cache_path = os.path.join(self.cache_dir, f"{oldest_hash}.{ext}")
            if os.path.exists(cache_path):
                file_size = os.path.getsize(cache_path)
                os.remove(cache_path)
                self.current_cache_size -= file_size
