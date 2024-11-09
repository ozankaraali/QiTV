import random
import aiohttp
import asyncio
import orjson as json
from PySide6.QtCore import QThread, Signal

class ContentLoader(QThread):
    content_loaded = Signal(dict)
    progress_updated = Signal(int, int)

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
        self.period= period
        self.ch_id = ch_id
        self.size = size
        self.items = []

    async def fetch_page(self, session, page, max_retries=2, timeout=5):
        for attempt in range(max_retries):
            try:
                params = self.get_params(page)
                async with session.get(
                    self.url, headers=self.headers, params=params, timeout=timeout
                ) as response:
                    content = await response.read()
                    if response.status == 503 or not content:
                        wait_time = (2**attempt) + random.uniform(0, 1)
                        print(
                            f"Received error or empty response. Retrying in {wait_time:.2f} seconds..."
                        )
                        await asyncio.sleep(wait_time)
                        continue
                    result = json.loads(content)
                    if self.action == "get_short_epg":
                        return (
                            result["js"],
                            1,
                            1,
                        )

                    return (
                        result["js"]["data"],
                        int(result["js"].get("total_items", 1)),
                        int(result["js"].get("max_page_items", 1)),
                    )
            except (
                aiohttp.ClientError,
                json.JSONDecodeError,
                asyncio.TimeoutError,
            ) as e:
                print(f"Error fetching page {page}: {e}")
                if attempt == max_retries - 1:
                    raise
                wait_time = (2**attempt) + random.uniform(0, 1)
                print(f"Retrying in {wait_time:.2f} seconds...")
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
        async with aiohttp.ClientSession() as session:
            # Fetch initial data to get total items and max page items
            page = 1
            page_items, total_items, max_page_items = await self.fetch_page(
                session, page
            )
            # if page_items is list, extend items
            if isinstance(page_items, list):
                self.items.extend(page_items)
            # if page_items is dict, extend items
            elif isinstance(page_items, dict):
                self.items.append(page_items)

            if max_page_items:
                pages = (total_items + max_page_items - 1) // max_page_items
            else:
                pages = 0

            self.progress_updated.emit(1, pages)
    
            tasks = []
            for page_num in range(2, pages + 1):
                tasks.append(self.fetch_page(session, page_num))
    
            for i, task in enumerate(asyncio.as_completed(tasks), 2):
                page_items, _, _ = await task
                self.items.extend(page_items)
                self.progress_updated.emit(i, pages)

            # Emit all items once done
            self.content_loaded.emit(
                {
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
        except Exception as e:
            print(f"Error in content loading: {e}")
