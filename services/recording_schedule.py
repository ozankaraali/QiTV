"""Scheduled recordings (2.8): what is to be recorded, when each is due, what became of it.

A recording is scheduled from the guide: one programme (`add_once`: its channel, title, start
and stop) or a series (`add_series`: every airing of a title on a channel, as the channel's
guide lists them); or at a time, which needs no guide (`add_timer`: a channel from one time of
this computer's clock to another on chosen weekdays; once at a time is a one-time programme
`add_once` is given). Each records from PRE_S before its start to POST_S after its stop
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
from datetime import date, datetime, time as clock_time, timedelta
import json
import logging
import os
import threading
import time
from typing import Any, Callable, Iterable, Mapping, Optional
from urllib.parse import quote
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
ONCE, SERIES, TIMER = "once", "series", "timer"
DAY_MINUTES = 24 * 60  # a timer's longest, and its start's bound (minutes after midnight)
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


_pending: dict[str, dict] = {}  # the newest data to write, by file
_writing = threading.Condition()
_writer: Optional[threading.Thread] = None


def write_later(path: str, data: dict) -> None:
    """`data` written to `path` off the GUI thread, by one writer: saves land in order and
    only the newest of those waiting is written."""
    global _writer
    with _writing:
        _pending[path] = data
        if _writer is None:
            _writer = threading.Thread(target=_write_pending, name="schedule-save", daemon=True)
            _writer.start()


def _write_pending() -> None:
    global _writer
    while True:
        with _writing:
            if not _pending:
                _writer = None
                _writing.notify_all()
                return
            path = next(iter(_pending))
            data = _pending.pop(path)
        write_now(path, data)


def flush_writes(timeout: float = 5.0) -> None:
    """Waits until the saves under way are written (QiTV closing, tests)."""
    with _writing:
        _writing.wait_for(lambda: _writer is None, timeout)


def write_now(path: str, data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with atomic_write(path) as out:
            out.write(json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"))
    except OSError as e:
        logger.warning("The recording schedule could not be saved: %s", type(e).__name__)


# An Xtream channel's address holds its account; the file keeps these in its place (2.8).
_USER, _PASSWORD = "{username}", "{password}"
_USER_Q, _PASSWORD_Q = "{username:q}", "{password:q}"


def _account(account: Optional[Mapping[str, Any]]) -> tuple[str, str]:
    if not account:
        return "", ""
    return str(account.get("username") or ""), str(account.get("password") or "")


def _channel_copy(channel: dict, account: Optional[Mapping[str, Any]] = None) -> dict:
    """What the file keeps of a channel; an address that holds the provider's account
    (`account`: its username and password, an Xtream panel's) keeps it out (`with_account`
    puts the provider's own back when it records)."""
    copy = {key: channel[key] for key in _CHANNEL_KEYS if channel.get(key) not in (None, "")}
    user, password = _account(account)
    cmd = copy.get("cmd")
    if user and password and isinstance(cmd, str):
        cmd = cmd.replace(f"/{user}/{password}/", f"/{_USER}/{_PASSWORD}/")
        quoted = f"/{quote(user, safe='')}/{quote(password, safe='')}/"
        copy["cmd"] = cmd.replace(quoted, f"/{_USER_Q}/{_PASSWORD_Q}/")
    return copy


def with_account(channel: dict, account: Optional[Mapping[str, Any]]) -> dict:
    """A kept channel with its provider's account (as the provider has it now) back in its
    address (2.8; `_channel_copy`)."""
    cmd = channel.get("cmd")
    if not isinstance(cmd, str) or "{" not in cmd:
        return dict(channel)
    user, password = _account(account)
    cmd = cmd.replace(_USER_Q, quote(user, safe="")).replace(_PASSWORD_Q, quote(password, safe=""))
    return dict(channel, cmd=cmd.replace(_USER, user).replace(_PASSWORD, password))


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _whole(value: Any, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _valid(item: Any) -> bool:
    """An item as the schedule writes it (a hand-edited file's others are left out, 2.8)."""
    if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
        return False
    if not isinstance(item.get("channel"), dict) or not isinstance(item.get("title"), str):
        return False
    if item.get("kind") == SERIES:
        return True
    if item.get("kind") == TIMER:
        days = item.get("days")
        return (
            _whole(item.get("begin"), 0, DAY_MINUTES - 1)
            and _whole(item.get("minutes"), 1, DAY_MINUTES)
            and isinstance(days, list)
            and bool(days)
            and all(_whole(day, 0, 6) for day in days)
        )
    return item.get("kind") == ONCE and _number(item.get("start")) and _number(item.get("stop"))


def slot_text(begin: int, minutes: int) -> str:
    """A timer's time, as "21:00–22:30" (`begin`: minutes after midnight)."""
    end = (begin + minutes) % DAY_MINUTES
    return f"{begin // 60:02d}:{begin % 60:02d}–{end // 60:02d}:{end % 60:02d}"


def _local(day: date, minutes: int) -> float:
    """`minutes` after `day`'s midnight on this computer's clock, in Unix seconds."""
    hour, minute = divmod(minutes, 60)
    return datetime.combine(day, clock_time(hour, minute)).astimezone().timestamp()


def _timer_starts(item: dict, now: float) -> list[float]:
    """The starts of a timer's slots that may run at `now`: yesterday's (one past midnight)
    and today's, on its days."""
    today = datetime.fromtimestamp(now).date()
    days = set(item["days"])
    return [
        _local(day, item["begin"])
        for day in (today - timedelta(days=1), today)
        if day.weekday() in days
    ]


def same_channel(a: dict, b: dict) -> bool:
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
    def __init__(
        self,
        path: str,
        write: Callable[[str, dict], None] = write_later,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = path
        self._write = write
        self._clock = clock  # now, in Unix seconds (a programme that ended is not added)
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
        items, done, outcomes = data.get("items"), data.get("done"), data.get("outcomes")
        self._items = [i for i in (items if isinstance(items, list) else []) if _valid(i)]
        self._done = [str(k) for k in (done if isinstance(done, list) else [])][-DONE:]
        kept = outcomes if isinstance(outcomes, list) else []
        self._outcomes = [o for o in kept if isinstance(o, dict)][-OUTCOMES:]

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

    def add_once(
        self, channel: dict, programme: Programme, *, account: Optional[Mapping] = None
    ) -> Optional[dict]:
        """Record `programme` at its time; None when it ended already (2.8: a phone's stale
        timetable). `account`: the provider's (an Xtream panel's), kept out of the file."""
        if programme.stop.timestamp() <= self._clock():
            return None
        existing = self._once_of(channel, programme)
        if existing is not None:
            return dict(existing)
        item = {
            "id": uuid.uuid4().hex[:12],
            "kind": ONCE,
            "provider": str(channel.get("_provider") or ""),
            "channel": _channel_copy(channel, account),
            "title": programme.title,
            "start": programme.start.timestamp(),
            "stop": programme.stop.timestamp(),
            "created": time.time(),
        }
        self._items.append(item)
        self._save()
        return dict(item)

    def add_series(self, channel: dict, title: str, *, account: Optional[Mapping] = None) -> dict:
        for item in self._items:
            if (
                item.get("kind") == SERIES
                and same_channel(item.get("channel") or {}, channel)
                and _fold(item.get("title", "")) == _fold(title)
            ):
                return dict(item)
        item = {
            "id": uuid.uuid4().hex[:12],
            "kind": SERIES,
            "provider": str(channel.get("_provider") or ""),
            "channel": _channel_copy(channel, account),
            "title": title,
            "created": time.time(),
        }
        self._items.append(item)
        self._save()
        return dict(item)

    def add_timer(
        self,
        channel: dict,
        *,
        begin: int,
        minutes: int,
        days: Iterable[int],
        title: str = "",
        account: Optional[Mapping] = None,
    ) -> dict:
        """Record `channel` on the weekdays `days` (Monday 0) from `begin` (minutes after
        midnight, this computer's clock) for `minutes`, until cancelled: no guide needed.
        `title` names it (its time when empty); the same timer again is the one kept."""
        chosen = sorted({int(day) for day in days})
        for item in self._items:
            if (
                item.get("kind") == TIMER
                and same_channel(item.get("channel") or {}, channel)
                and (item.get("begin"), item.get("minutes"), item.get("days"))
                == (begin, minutes, chosen)
            ):
                return dict(item)
        item = {
            "id": uuid.uuid4().hex[:12],
            "kind": TIMER,
            "provider": str(channel.get("_provider") or ""),
            "channel": _channel_copy(channel, account),
            "title": title.strip(),
            "begin": int(begin),
            "minutes": int(minutes),
            "days": chosen,
            "created": time.time(),
        }
        if not _valid(item):
            raise ValueError("a timer needs a time of day, a length up to a day, and a weekday")
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
                and same_channel(item.get("channel") or {}, channel)
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
                and same_channel(item.get("channel") or {}, channel)
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
            elif item.get("kind") == TIMER:
                title = str(item.get("title") or "") or slot_text(item["begin"], item["minutes"])
                provider = str(item.get("provider") or "")
                candidates = [
                    Job(item["id"], title, channel, provider, start, start + item["minutes"] * 60)
                    for start in _timer_starts(item, now)
                ]
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
                    # In its window now ends after now - POST_S (2.8: asked from there, not
                    # hours back: 48 short programmes never reached now).
                    for programme in guide(channel, now - POST_S)
                    if _fold(programme.title) == _fold(item.get("title", ""))
                ]
            for job in candidates:
                start, stop = window(job)
                if start <= now < stop and job.key not in self._done:
                    jobs.append(job)
        return jobs

    def started(self, job: Job) -> None:
        if job.key not in self._done:
            self._done.append(job.key)
            self._done = self._done[-DONE:]
            self._save()

    def unstarted(self, job: Job) -> None:
        """`job` is due again (its recording was cut short while it airs)."""
        if job.key in self._done:
            self._done = [key for key in self._done if key != job.key]
            self._save()

    def noted(self, job: Job, state: str, *, path: str = "", why: str = "") -> None:
        """An outcome kept without ending the job (a part recorded, the rest to come)."""
        self._note(job.title, job.channel, job.start, state, path=path, why=why)
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
