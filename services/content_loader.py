import logging
import os
import random
from urllib.parse import quote, urlencode

from PySide6.QtCore import QThread, Signal
import orjson as json

from services.lazy_import import lazy_module
from services.redact import safe_exc, safe_traceback

logger = logging.getLogger(__name__)
aiohttp = lazy_module("aiohttp")
asyncio = lazy_module("asyncio")

_HIDDEN = "…"


def _debug_header_value(name: str, value: object, raw: bool) -> str:
    """A header's value for QITV_DEBUG_CURL: Cookie and Authorization values are masked.

    The cookie carries the device (MAC, serial, device id), Authorization the portal's
    token. Their names stay, so the request still reads; QITV_DEBUG_CURL=raw shows them.
    """
    text = str(value)
    if raw:
        return text
    lowered = name.lower()
    if lowered == "cookie":
        parts = [part.strip() for part in text.split(";")]
        return "; ".join(
            f"{part.split('=', 1)[0]}={_HIDDEN}" if "=" in part else part for part in parts
        ).rstrip()
    if lowered in ("authorization", "proxy-authorization"):
        scheme = text.split(" ", 1)
        return f"{scheme[0]} {_HIDDEN}" if len(scheme) == 2 else _HIDDEN
    return text


def debug_curl(url: str, params: dict, headers: dict, timeout: float, mode: str) -> str:
    """A copy-paste curl command for a request (QITV_DEBUG_CURL; `mode` "raw" unmasks)."""
    query = urlencode(params, doseq=True, quote_via=quote)
    full_url = f"{url}?{query}" if query else url
    raw = mode.strip().lower() == "raw"

    def escape(text: object) -> str:
        return str(text).replace('"', '\\"')

    flags = " ".join(
        f'-H "{escape(k)}: {escape(_debug_header_value(k, v, raw))}"'
        for k, v in (headers or {}).items()
    )
    return f'curl -sS -m {timeout} {flags} "{full_url}"'


class ContentLoader(QThread):
    content_loaded = Signal(dict)
    progress_updated = Signal(int, int)
    error = Signal(str)

    def __init__(
        self,
        url,
        headers,
        content_type,
        category_id=None,
        parent_id=None,
        movie_id=None,
        season_id=None,
        period=None,
        ch_id=None,
        size=0,
        action="get_ordered_list",
        sortby="name",
        max_retries=2,
        timeout=5,
        verify_ssl=True,
    ):
        super().__init__()
        self.url = url
        self.headers = headers
        self.content_type = content_type
        self.category_id = category_id
        self.parent_id = parent_id
        self.movie_id = movie_id
        self.season_id = season_id
        self.action = action
        self.sortby = sortby
        self.period = period
        self.ch_id = ch_id
        self.size = size
        self.max_retries = max_retries
        self.timeout = timeout
        self.items = []
        self.counter_page_not_fetched = 0
        self.verify_ssl = verify_ssl

    async def fetch_page(self, session, page, max_retries=2, timeout=5):
        for attempt in range(max_retries):
            if self.isInterruptionRequested():
                raise asyncio.CancelledError()
            try:
                if attempt:
                    logger.debug(f"Retrying page {page}...")
                params = self.get_params(page)
                # For EPG debugging: a copy-paste-friendly curl with headers when enabled
                mode = os.getenv("QITV_DEBUG_CURL")
                if mode and self.action in ("get_epg_info", "get_short_epg") and page == 1:
                    try:
                        command = debug_curl(self.url, params, self.headers, timeout, mode)
                        logger.info("EPG curl: %s", command)
                    except Exception as e:
                        logger.debug("Could not format EPG curl command: %s", type(e).__name__)
                async with session.get(
                    self.url, headers=self.headers, params=params, timeout=timeout
                ) as response:
                    content = await response.read()
                    if response.status == 503 or not content:
                        logger.warning(f"Received error or empty response fetching page {page}")
                        if attempt == max_retries - 1:
                            self.counter_page_not_fetched += 1
                            return [], 0, 0
                        wait_time = (2**attempt) + random.uniform(0, 1)
                        logger.debug(f"Retrying in {wait_time:.2f} seconds...")
                        await asyncio.sleep(wait_time)
                        continue
                    response.raise_for_status()
                    result = json.loads(content)
                    # Special-case EPG actions that return a non-paginated structure
                    if self.action in ("get_short_epg", "get_epg_info"):
                        return (result.get("js"), 1, 1)
                    ret = result.get("js", {})
                    if not isinstance(ret, dict):
                        raise ValueError(f"Invalid response fetching page {page}")
                    return (
                        ret.get("data", []),
                        int(ret.get("total_items", 0)),
                        int(ret.get("max_page_items", 0)),
                    )
            except (
                aiohttp.ClientError,
                json.JSONDecodeError,
                asyncio.TimeoutError,
            ) as e:
                # aiohttp errors name the request URL: the type and host only.
                logger.warning("Error fetching page %s: %s", page, safe_exc(e))
                if attempt == max_retries - 1:
                    self.counter_page_not_fetched += 1
                    return [], 0, 0
                wait_time = (2**attempt) + random.uniform(0, 1)
                logger.debug(f"Retrying in {wait_time:.2f} seconds...")
                await asyncio.sleep(wait_time)
        return [], 0, 0

    def get_params(self, page):
        params = {
            "type": self.content_type,
            "action": self.action,
            "p": str(page),
            "JsHttpRequest": "1-xml",
        }
        if self.content_type == "itv":
            if self.action == "get_short_epg":
                params.update(
                    {
                        "ch_id": self.ch_id,
                        "size": self.size,
                    }
                )
                # remove unnecessary params
                params.pop("p")
            elif self.action == "get_epg_info":
                params.update(
                    {
                        "period": self.period,
                    }
                )
                # remove unnecessary params
                params.pop("p")
            else:
                params.update(
                    {
                        "genre": self.category_id if self.category_id else "*",
                        "force_ch_link_check": "",
                        "fav": "0",
                        "sortby": self.sortby,
                        "hd": "0",
                    }
                )
        elif self.content_type == "vod":
            params.update(
                {
                    "category": self.category_id if self.category_id else "*",
                    "sortby": self.sortby,
                }
            )
        elif self.content_type == "series":
            params.update(
                {
                    "category": self.category_id if self.category_id else "*",
                    "movie_id": self.movie_id if self.movie_id else "0",
                    "season_id": self.season_id if self.season_id else "0",
                    "episode_id": "0",
                    "sortby": self.sortby,
                }
            )
        return params

    async def load_content(self):
        semaphore = asyncio.Semaphore(10)  # Limit concurrent fetch_page calls

        connector = aiohttp.TCPConnector(ssl=self.verify_ssl)
        async with aiohttp.ClientSession(connector=connector) as session:
            # Fetch initial data to get total items and max page items
            page = 1
            page_items, total_items, max_page_items = await self.fetch_page(
                session, page, self.max_retries, self.timeout
            )
            # Paginated lists are concatenated; EPG actions return a single dict.
            if isinstance(page_items, list):
                self.items.extend(page_items)
            elif isinstance(page_items, dict):
                self.items.append(page_items)

            if max_page_items:
                pages = (total_items + max_page_items - 1) // max_page_items
            else:
                pages = 0

            self.progress_updated.emit(1, pages)

            async def fetch_with_semaphore(page_num):
                async with semaphore:
                    result = await self.fetch_page(
                        session, page_num, self.max_retries, self.timeout
                    )
                    return result[0]

            # Preserve provider page order, regardless of network completion order.
            # Only ten requests run at once; cancelled jobs never publish partial data.
            tasks = [
                asyncio.create_task(fetch_with_semaphore(page_num))
                for page_num in range(2, pages + 1)
            ]
            try:
                for i, task in enumerate(tasks, 2):
                    self.items.extend(await task)
                    self.progress_updated.emit(i, pages)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

            if self.isInterruptionRequested():
                return
            if self.counter_page_not_fetched:
                raise RuntimeError(f"Failed to fetch {self.counter_page_not_fetched} pages")

            # Emit all items once done
            self.content_loaded.emit(
                {
                    "page_count": pages,
                    "category_id": self.category_id,
                    "items": self.items,
                    "parent_id": self.parent_id,
                    "movie_id": self.movie_id,
                    "season_id": self.season_id,
                }
            )

    def run(self):
        try:
            asyncio.run(self.load_content())
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("Error in content loading: %s", safe_exc(e))
            logger.debug("Content loading failure:\n%s", safe_traceback(e))
            self.error.emit(str(e))
