"""History, last-watched, last live channel, resume points (spec §4.1 PlaybackSession
bookkeeping)."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Callable, Optional

from PySide6.QtCore import QTimer

from services.redact import safe_message

logger = logging.getLogger(__name__)

RESUME_MIN_MS = 5000
RESUME_MAX_RATIO = 0.90


class WatchState:
    """Watch bookkeeping in the config; `save_delay_ms` defers the save of a play.

    With a delay (the TV shell), a play marks the state dirty and a timer saves it once the
    tuning settles, so a burst of zaps writes config.json once, and never on the GUI thread
    at every tune. That save tries once: a locked file (Windows) is retried for up to 250 ms
    only by flush(), which saves at once (on close). Without a delay every play is saved
    immediately.
    """

    def __init__(
        self,
        config,
        *,
        flush_interval_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
        save_delay_ms: int = 0,
    ):
        self.config = config
        self._flush_interval_s = flush_interval_s
        self._clock = clock
        self._last_flush = clock()
        self._dirty = False
        self.save_delay_ms = save_delay_ms
        self._save_timer: Optional[QTimer] = None

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
        self._save_soon()

    def forget(self, matches: Callable[[dict], bool]) -> int:
        """Drop the history entries `matches` picks (a channel taken out of ⟲ Recent, or all
        of them); how many went. Saved as a play is: the entries left keep their format.
        """
        history = self.config.watch_history or []
        kept = [entry for entry in history if not (isinstance(entry, dict) and matches(entry))]
        removed = len(history) - len(kept)
        if removed:
            self.config.watch_history = kept
            self._save_soon()
        return removed

    def _save_soon(self) -> None:
        """Mark the state changed and save it: at once, or once the zapping settles."""
        self._dirty = True
        if self.save_delay_ms <= 0:
            self._save()
            return
        if self._save_timer is None:
            self._save_timer = QTimer(singleShot=True, interval=self.save_delay_ms)
            self._save_timer.timeout.connect(self._save_settled)
        self._save_timer.start()  # restarted: one save once the zapping settles

    def resume_position(self, cid: str) -> Optional[int]:
        # The duration needed for the 90% rule lives in the stored entry.
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

    def flush(self) -> None:
        self._save()

    def _save_settled(self) -> None:
        """The zapping settled: save once, never waiting for a locked file (flush() will)."""
        self._save(retry=False)

    def _save(self, *, retry: bool = True) -> None:
        """Write the config; a zap or a close never fails because the file is locked."""
        if self._save_timer is not None:
            self._save_timer.stop()  # this save covers a pending play
        self._last_flush = self._clock()  # a failed write is tried again after the interval
        try:
            if retry:
                self.config.save_config()
            else:
                self.config.save_config(retry=False)
        except Exception as e:
            logger.warning("Couldn't save the watch state: %s", safe_message(e))
            self._dirty = True
            return
        self._dirty = False
