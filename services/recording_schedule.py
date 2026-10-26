"""Scheduled recordings (2.8): what is to be recorded, when each is due, what became of it.

A recording is scheduled from the guide: one programme (`add_once`: its channel, title, start
and stop) or a series (`add_series`: every airing of a title on a channel, as the channel's
guide lists them). Each records from PRE_S before its start to POST_S after its stop
(`window`). The scheduler (`ui.recording_scheduler`) asks `due(now, guide)` every few seconds;
a job it starts is marked (`started`) and its end noted (`finished`: recorded, missed or failed,
with why), the last OUTCOMES of them kept: the Library names a recording by its programme
(`title_of`) and lists what was missed. A programme whose end passed while QiTV was closed is
noted missed once QiTV starts again (`missed_before`).

The schedule is a small file beside the config, {"version": 1, "items": [...], "done": [...],
"outcomes": [...]}, written on a thread of its own (`write`); a file of another version, or a
broken one, reads as empty (an older QiTV ignores the file).
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
import threading
import time
from typing import Any, Callable, Optional
import uuid

from services.atomic_write import atomic_write
from services.epg_view import Programme

logger = logging.getLogger(__name__)

FILE_NAME = "recording_schedule.json"
VERSION = 1
PRE_S = 60  # recording starts a minute early
POST_S = 180  # and goes on three minutes late (guides run late)
OUTCOMES = 50  # the last outcomes kept
DONE = 400  # the series airings already taken, kept to never record one twice
ONCE, SERIES = "once", "series"
RECORDED, MISSED, FAILED = "recorded", "missed", "failed"
_CHANNEL_KEYS = ("id", "name", "number", "cmd", "xmltv_id", "_provider", "user_agent", "referrer")


@dataclass(frozen=True)
class Job:
    """One recording to make now: `item`'s (its id), of `channel`, from `start` to `stop`
    (unix seconds, the programme's own; `window` adds the margins)."""

    item_id: str
    title: str
    channel: dict
    provider: str
    start: float
    stop: float

    @property
    def key(self) -> str:
        return f"{self.item_id}@{int(self.start)}"


def write_later(path: str, data: dict) -> None:
    threading.Thread(target=write_now, args=(path, data), name="schedule-save", daemon=True).start()


def write_now(path: str, data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with atomic_write(path) as out:
            out.write(json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"))
    except OSError as e:
        logger.warning("The recording schedule could not be saved: %s", type(e).__name__)


def _channel_copy(channel: dict) -> dict:
    return {key: channel[key] for key in _CHANNEL_KEYS if channel.get(key) not in (None, "")}


def _same_channel(a: dict, b: dict) -> bool:
    if a.get("_provider") != b.get("_provider"):
        return False
    if a.get("id") not in (None, "") and b.get("id") not in (None, ""):
        return str(a.get("id")) == str(b.get("id"))
    return str(a.get("name") or "") == str(b.get("name") or "")


def _fold(title: str) -> str:
    return " ".join(str(title or "").casefold().split())


def window(item_or_job: Any) -> tuple[float, float]:
    """When a recording runs: PRE_S before its start to POST_S after its stop."""
    if isinstance(item_or_job, Job):
        return item_or_job.start - PRE_S, item_or_job.stop + POST_S
    return float(item_or_job["start"]) - PRE_S, float(item_or_job["stop"]) + POST_S


class Schedule:
    def __init__(self, path: str, write: Callable[[str, dict], None] = write_later) -> None:
        self._path = path
        self._write = write
        self._items: list[dict] = []
        self._done: list[str] = []  # job keys started (series airings never twice)
        self._outcomes: list[dict] = []
        self._load()

    # ---- the file ---------------------------------------------------------------------------

    def _load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as source:
                data = json.load(source)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            logger.warning("The recording schedule could not be read: %s", type(e).__name__)
            return
        if not isinstance(data, dict) or data.get("version") != VERSION:
            return
        self._items = [i for i in data.get("items") or [] if isinstance(i, dict) and i.get("id")]
        self._done = [str(k) for k in data.get("done") or []][-DONE:]
        self._outcomes = [o for o in data.get("outcomes") or [] if isinstance(o, dict)][-OUTCOMES:]

    def _save(self) -> None:
        data = {
            "version": VERSION,
            "items": self._items,
            "done": self._done[-DONE:],
            "outcomes": self._outcomes[-OUTCOMES:],
        }
        self._write(self._path, json.loads(json.dumps(data)))  # a copy for the writing thread

    # ---- what is scheduled -----------------------------------------------------------------

    def items(self) -> list[dict]:
        return [dict(item) for item in self._items]

    def outcomes(self) -> list[dict]:
        """What became of recent recordings, newest first."""
        return [dict(o) for o in reversed(self._outcomes)]

    def providers(self) -> set[str]:
        return {str(item.get("provider") or "") for item in self._items} - {""}

    def add_once(self, channel: dict, programme: Programme) -> dict:
        existing = self._once_of(channel, programme)
        if existing is not None:
            return dict(existing)
        item = {
            "id": uuid.uuid4().hex[:12],
            "kind": ONCE,
            "provider": str(channel.get("_provider") or ""),
            "channel": _channel_copy(channel),
            "title": programme.title,
            "start": programme.start.timestamp(),
            "stop": programme.stop.timestamp(),
            "created": time.time(),
        }
        self._items.append(item)
        self._save()
        return dict(item)

    def add_series(self, channel: dict, title: str) -> dict:
        for item in self._items:
            if (
                item.get("kind") == SERIES
                and _same_channel(item.get("channel") or {}, channel)
                and _fold(item.get("title", "")) == _fold(title)
            ):
                return dict(item)
        item = {
            "id": uuid.uuid4().hex[:12],
            "kind": SERIES,
            "provider": str(channel.get("_provider") or ""),
            "channel": _channel_copy(channel),
            "title": title,
            "created": time.time(),
        }
        self._items.append(item)
        self._save()
        return dict(item)

    def cancel(self, item_id: str) -> None:
        before = len(self._items)
        self._items = [i for i in self._items if i.get("id") != item_id]
        if len(self._items) != before:
            self._save()

    def _once_of(self, channel: dict, programme: Programme) -> Optional[dict]:
        start = programme.start.timestamp()
        for item in self._items:
            if (
                item.get("kind") == ONCE
                and _same_channel(item.get("channel") or {}, channel)
                and abs(float(item.get("start") or 0) - start) < 60
            ):
                return item
        return None

    def scheduled_item(self, channel: dict, programme: Programme) -> Optional[dict]:
        """The schedule's item that records `programme` (once, or its series), else None."""
        once = self._once_of(channel, programme)
        if once is not None:
            return dict(once)
        for item in self._items:
            if (
                item.get("kind") == SERIES
                and _same_channel(item.get("channel") or {}, channel)
                and _fold(item.get("title", "")) == _fold(programme.title)
            ):
                return dict(item)
        return None

    def is_scheduled(self, channel: dict, programme: Programme) -> bool:
        return self.scheduled_item(channel, programme) is not None

    # ---- what is due -----------------------------------------------------------------------

    def due(self, now: float, guide: Callable[[dict, float], list[Programme]]) -> list[Job]:
        """The recordings to start now: in their window and not started. `guide(channel,
        since)` lists a series' channel's programmes from `since` on."""
        jobs: list[Job] = []
        for item in self._items:
            channel = item.get("channel") or {}
            if item.get("kind") == ONCE:
                job = Job(
                    item["id"],
                    str(item.get("title") or ""),
                    channel,
                    str(item.get("provider") or ""),
                    float(item.get("start") or 0),
                    float(item.get("stop") or 0),
                )
                candidates = [job]
            else:
                candidates = [
                    Job(
                        item["id"],
                        programme.title,
                        channel,
                        str(item.get("provider") or ""),
                        programme.start.timestamp(),
                        programme.stop.timestamp(),
                    )
                    for programme in guide(channel, now - POST_S - 6 * 3600)
                    if _fold(programme.title) == _fold(item.get("title", ""))
                ]
            for job in candidates:
                start, stop = window(job)
                if start <= now < stop and job.key not in self._done:
                    jobs.append(job)
        return jobs

    def started(self, job: Job) -> None:
        self._done.append(job.key)
        self._done = self._done[-DONE:]
        self._save()

    def finished(self, job: Job, state: str, *, path: str = "", why: str = "") -> None:
        """A job ended: its outcome kept; a one-time item leaves the schedule."""
        self._items = [
            i for i in self._items if not (i.get("id") == job.item_id and i.get("kind") == ONCE)
        ]
        self._note(job.title, job.channel, job.start, state, path=path, why=why)
        self._save()

    def _note(self, title: str, channel: dict, start: float, state: str, **extra: str) -> None:
        outcome = {
            "title": title,
            "channel": str(channel.get("name") or ""),
            "start": start,
            "state": state,
            "at": time.time(),
        }
        outcome.update({k: v for k, v in extra.items() if v})
        self._outcomes.append(outcome)
        self._outcomes = self._outcomes[-OUTCOMES:]

    def missed_before(self, now: float) -> list[dict]:
        """The one-time recordings whose window ended before `now` without being started
        (QiTV was closed): noted missed and taken off; what was noted."""
        missed = []
        kept = []
        for item in self._items:
            if item.get("kind") == ONCE and window(item)[1] <= now:
                key = f"{item['id']}@{int(float(item.get('start') or 0))}"
                if key not in self._done:
                    missed.append(item)
                continue
            kept.append(item)
        if len(kept) == len(self._items):
            return []
        self._items = kept
        for item in missed:
            self._note(
                str(item.get("title") or ""),
                item.get("channel") or {},
                float(item.get("start") or 0),
                MISSED,
                why="closed",
            )
        self._save()
        return [dict(item) for item in missed]

    def title_of(self, path: str) -> str:
        """The programme a recording's file holds (a scheduled one's), else ""."""
        for outcome in reversed(self._outcomes):
            if outcome.get("path") == path:
                return str(outcome.get("title") or "")
        return ""
