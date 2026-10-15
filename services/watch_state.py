"""History, last-watched, last live channel, resume points (spec §4.1 PlaybackSession bookkeeping)."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

RESUME_MIN_MS = 5000
RESUME_MAX_RATIO = 0.90


class WatchState:
    def __init__(
        self,
        config,
        *,
        flush_interval_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.config = config
        self._flush_interval_s = flush_interval_s
        self._clock = clock
        self._last_flush = clock()
        self._dirty = False

    @staticmethod
    def content_id(provider_name: str, item: dict, *, item_type: str) -> str:
        key = item.get("id") or item.get("stream_id") or item.get("series_id")
        if key is None:
            key = item.get("cmd") or item.get("name")
        if key is None:
            blob = json.dumps(item, sort_keys=True, default=str).encode("utf-8")
            key = hashlib.sha1(blob).hexdigest()
        cid = f"{provider_name}:{key}"
        if item_type == "episode":
            season = item.get("season") or item.get("season_id") or ""
            episode = item.get("series") or item.get("episode_num") or item.get("number") or ""
            cid += f":s{season}:e{episode}"
        return cid

    def record_play(
        self, provider_name: str, item: dict, *, item_type: str, link: str, group_id=None
    ) -> None:
        self.config.add_to_watch_history(
            item,
            item_type,
            link,
            provider_name,
            content_id=self.content_id(provider_name, item, item_type=item_type),
        )
        self.config.last_watched = {
            "item_data": item,
            "item_type": item_type,
            "link": link,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "provider_name": provider_name,
        }
        if item_type == "channel":
            self.config.last_live_channel = {
                "provider_name": provider_name,
                "group_id": "" if group_id is None else str(group_id),
                "item": item,
            }
        self._save()

    def resume_position(self, cid: str) -> Optional[int]:
        # ConfigManager.get_playback_position returns only the position (int); the
        # duration needed for the 90% rule lives in the stored entry.
        saved = (getattr(self.config, "playback_positions", None) or {}).get(cid) or {}
        position, duration = int(saved.get("position") or 0), int(saved.get("duration") or 0)
        if position <= RESUME_MIN_MS:
            return None
        if duration > 0 and position / duration >= RESUME_MAX_RATIO:
            return None
        return position

    def update_position(self, cid: str, position_ms: int, duration_ms: int) -> None:
        self.config.save_playback_position(cid, position_ms, duration_ms)
        self._dirty = True
        if self._clock() - self._last_flush >= self._flush_interval_s:
            self.flush()

    def clear_position(self, cid: str) -> None:
        self.config.clear_playback_position(cid)
        self.flush()

    def flush(self) -> None:
        self._save()

    def _save(self) -> None:
        """Write the config; a zap or a close never fails because the file is locked."""
        self._last_flush = self._clock()  # a failed write is tried again after the interval
        try:
            self.config.save_config()
        except Exception as e:
            logger.warning("Couldn't save the watch state (%s)", type(e).__name__)
            self._dirty = True
            return
        self._dirty = False
