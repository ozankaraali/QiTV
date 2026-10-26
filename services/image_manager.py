import base64
from collections import OrderedDict
from datetime import datetime
import hashlib
import logging
import os
import random
import threading
import time

import orjson as json

from services.lazy_import import lazy_module
from services.redact import redact_url, safe_exc

logger = logging.getLogger(__name__)
aiohttp = lazy_module("aiohttp")
asyncio = lazy_module("asyncio")

# 2.8: TMDB's images (services/tmdb.py) are fetched again once as old as the details kept with
# them: TMDB's terms allow six months at most. A file's time is its download's.
TMDB_IMAGES = "https://image.tmdb.org/"
TMDB_IMAGE_MAX_AGE_S = 30 * 24 * 60 * 60


def _too_old(url: str, path: str) -> bool:
    if not url.startswith(TMDB_IMAGES):
        return False
    try:
        return time.time() - os.path.getmtime(path) > TMDB_IMAGE_MAX_AGE_S
    except OSError:
        return False


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
        # ImageLoader threads share this manager; guard every cache mutation/iteration.
        self._lock = threading.RLock()
        self._load_index()

    async def cache_image_from_url(self, session, url, iconified, max_retries=2, timeout=5):
        """Download image bytes and store to cache on disk only.
        Returns absolute cache file path or None.
        Safe to call from worker threads (no GUI objects created).
        """
        ext = "png" if iconified else "jpg"
        url_hash = self._hash_string(url + ext)
        cache_path = os.path.join(self.cache_dir, f"{url_hash}.{ext}")

        # If already cached on disk, update metadata and return
        if (
            os.path.exists(cache_path)
            and not _too_old(url, cache_path)
            and self._touch_cache_entry(url_hash, cache_path)
        ):
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
                    if not 200 <= response.status < 300:
                        # A refusal's or an error's page is no image: never cached as one.
                        logger.info("Image refused (%d): %s", response.status, redact_url(url))
                        return None

                    # Persist bytes. We skip pre-scaling here to avoid GUI usage off-thread.
                    try:
                        with open(cache_path, "wb") as f:
                            f.write(content)
                        self._touch_cache_entry(url_hash, cache_path)
                        self._manage_cache_size(protect=url_hash)
                        return cache_path
                    except OSError as e:
                        logger.warning(f"Error writing image to cache: {e}")
                        return None
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.info("Error fetching image: %s", safe_exc(e))  # e names the URL
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

        if os.path.exists(cache_path) and self._touch_cache_entry(image_hash, cache_path):
            return cache_path

        try:
            base64_data = image_str.split(",", 1)[1]
            image_data = base64.b64decode(base64_data)
            with open(cache_path, "wb") as f:
                f.write(image_data)
            self._touch_cache_entry(image_hash, cache_path)
            self._manage_cache_size(protect=image_hash)
            return cache_path
        except Exception as e:
            logger.warning(f"Error decoding/saving base64 image: {e}")
            return None

    def _touch_cache_entry(self, key_hash, cache_path) -> bool:
        """Record an access; False if the file vanished (e.g. evicted by another thread)."""
        try:
            size = os.path.getsize(cache_path)
        except OSError:
            with self._lock:
                entry = self.cache.pop(key_hash, None)
                self.current_cache_size -= (entry or {}).get("size", 0)
            return False
        with self._lock:
            entry = self.cache.get(key_hash) or {}
            self.current_cache_size += size - entry.get("size", 0)
            entry["size"] = size
            entry["ext"] = os.path.splitext(cache_path)[1].lstrip(".")
            entry["last_access"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.cache[key_hash] = entry
            self.cache.move_to_end(key_hash)  # maintain LRU order
        return True

    def clear_cache(self):
        with self._lock:
            for filename in os.listdir(self.cache_dir):
                file_path = os.path.join(self.cache_dir, filename)
                if os.path.isfile(file_path):
                    os.remove(file_path)
            self.cache.clear()
            self.current_cache_size = 0
        # Save empty cache index
        self.save_index()

    def _entry_paths(self, key_hash, entry):
        """Files an entry may own; entries from older indexes did not record their extension."""
        exts = [entry["ext"]] if entry and entry.get("ext") else ["png", "jpg"]
        return [os.path.join(self.cache_dir, f"{key_hash}.{ext}") for ext in exts]

    def discard(self, cache_path: str) -> None:
        """Forget a cached file that does not decode (an error page an older QiTV cached as an
        image, say), so the next load asks for it again."""
        key = os.path.splitext(os.path.basename(cache_path))[0]
        with self._lock:
            entry = self.cache.pop(key, None)
            self.current_cache_size -= (entry or {}).get("size", 0)
        try:
            os.remove(cache_path)
        except FileNotFoundError:
            pass

    def remove_icon_from_cache(self, url):
        url_hash = self._hash_string(url + "png")
        with self._lock:
            entry = self.cache.pop(url_hash, None)
            self.current_cache_size -= (entry or {}).get("size", 0)
        path = os.path.join(self.cache_dir, f"{url_hash}.png")
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

    def _hash_string(self, url):
        return hashlib.sha256(url.encode("utf-8")).hexdigest()

    def _load_index(self):
        with self._lock:
            self.cache.clear()
            if os.path.exists(self.index_file):
                with open(self.index_file, "r", encoding="utf-8") as f:
                    try:
                        data = json.loads(f.read())
                        self.cache = OrderedDict(data) if data else OrderedDict()
                    except (json.JSONDecodeError, IOError) as e:
                        logger.warning(f"Error loading index file: {e}")

            # Index files that exist on disk but not in the index (metadata only)
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for filename in os.listdir(self.cache_dir):
                image_hash, ext = os.path.splitext(filename)
                if ext in (".png", ".jpg") and not self.cache.get(image_hash):
                    self.cache[image_hash] = {
                        "size": os.path.getsize(os.path.join(self.cache_dir, filename)),
                        "ext": ext.lstrip("."),
                        "last_access": now,
                    }

            self.current_cache_size = sum(
                entry.get("size", 0) for entry in self.cache.values() if entry
            )

    def save_index(self):
        with self._lock:
            snapshot = json.dumps(dict(self.cache), option=json.OPT_INDENT_2).decode("utf-8")
        with open(self.index_file, "w", encoding="utf-8") as f:
            f.write(snapshot)

    def _manage_cache_size(self, protect=None):
        # Evict least recently used entries; never the one (`protect`) being returned.
        doomed = []
        with self._lock:
            while self.current_cache_size > self.max_cache_size:
                victim = next((k for k in self.cache if k != protect), None)
                if victim is None:
                    break
                data = self.cache.pop(victim)
                self.current_cache_size -= (data or {}).get("size", 0)
                doomed.extend(self._entry_paths(victim, data))
        for path in doomed:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
