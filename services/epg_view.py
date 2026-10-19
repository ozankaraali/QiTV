"""Normalized EPG programmes for the TV shell: one shape for STB and XMLTV data (no Qt)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import logging
from typing import Any, Iterable, Optional

from services.epg_time import parse_stb, parse_xmltv, parse_xmltv_naive

logger = logging.getLogger(__name__)

# A guide entry this long or longer is a placeholder (the channel's name from 00:00 to 12:00,
# "Program" until the guide's end), not a programme: it loses to one starting with it.
PLACEHOLDER_SPAN = timedelta(hours=12)

# A provider's guide time offset (2.2, Settings → Providers): some providers' guides are off by
# whole hours, so every programme of theirs moves by it (−12…+12 hours in 30-minute steps; the
# key is optional, and older 2.x builds ignore it). It is applied where programmes' times are
# read (`normalize`, and EpgManager's window), so the grid, now/next, the banner and the
# time-shift bar's ticks all move together.
GUIDE_OFFSET_KEY = "guide_offset_minutes"
GUIDE_OFFSET_STEP = 30  # minutes
GUIDE_OFFSET_MAX = 12 * 60  # minutes, either way


def guide_offset_minutes(provider: Any) -> int:
    """The provider's guide time offset in minutes (0 when unset or not a valid offset)."""
    value = provider.get(GUIDE_OFFSET_KEY) if isinstance(provider, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return 0
    try:
        minutes = int(float(value))
    except ValueError, OverflowError:
        return 0
    if minutes % GUIDE_OFFSET_STEP or abs(minutes) > GUIDE_OFFSET_MAX:
        return 0  # a hand-edited value the setting cannot show: no offset
    return minutes


def guide_offset(provider: Any) -> timedelta:
    return timedelta(minutes=guide_offset_minutes(provider))


@dataclass(frozen=True)
class Programme:
    title: str
    start: datetime  # timezone-aware
    stop: datetime  # timezone-aware
    description: str = ""

    def is_on(self, now: datetime) -> bool:
        return self.start <= now < self.stop

    def progress(self, now: datetime) -> float:
        span = (self.stop - self.start).total_seconds()
        if span <= 0:
            return 0.0
        return min(1.0, max(0.0, (now - self.start).total_seconds() / span))


def _text(value: Any) -> str:
    """XMLTV text arrives as {"__text": ...}, a list of language variants, or a plain value."""
    if isinstance(value, list):
        for entry in value:
            text = _text(entry)
            if text:
                return text
        return ""
    if isinstance(value, dict):
        return str(value.get("__text") or "").strip()
    return str(value or "").strip()


def _aware(moment: datetime) -> datetime:
    # Naive times (STB portals, offset-less XMLTV) are local wall-clock times.
    return moment if moment.tzinfo else moment.astimezone()


def _parse_xmltv_time(value: Any) -> Optional[datetime]:
    text = str(value).strip()
    for parse in (parse_xmltv, parse_xmltv_naive):
        try:
            return _aware(parse(text))
        except ValueError:
            continue
    return None


def _parse_stb_time(value: Any) -> Optional[datetime]:
    try:
        return _aware(parse_stb(str(value).strip()))
    except ValueError:
        return None


def parse_programme(raw: dict) -> Optional[Programme]:
    if "@start" in raw:
        start, stop = _parse_xmltv_time(raw.get("@start")), _parse_xmltv_time(raw.get("@stop"))
        title, description = _text(raw.get("title")), _text(raw.get("desc"))
    elif "time" in raw:
        start, stop = _parse_stb_time(raw.get("time")), _parse_stb_time(raw.get("time_to"))
        title, description = _text(raw.get("name")), _text(raw.get("descr"))
    else:
        return None
    if start is None or stop is None or stop <= start:
        return None
    return Programme(title or "Untitled", start, stop, description)


def _piece(programme: Programme, start: datetime, stop: datetime) -> Programme:
    if (start, stop) == (programme.start, programme.stop):
        return programme
    return replace(programme, start=start, stop=stop)


def _show(
    shown: list[Programme], result: list[Programme], since: datetime, until: Optional[datetime]
) -> datetime:
    """Add to `result` what `shown` shows from `since` to `until` (None: until all of it has
    ended): the latest started of its programmes still on. Returns where that ends."""
    while shown and (until is None or since < until):
        top = shown[-1]
        stop = top.stop if until is None else min(top.stop, until)
        if since < stop:
            result.append(_piece(top, since, stop))
            since = stop
        if top.stop <= since:
            shown.pop()  # over (one under the top is dropped when it comes up)
    return since


def _rank(programme: Programme) -> tuple[bool, timedelta]:
    """Of programmes starting together, the highest ranked shows: a placeholder loses to any
    shorter one, and of the others the longer wins (the film over the hour's "Movie" block,
    the match over the league's name)."""
    span = programme.stop - programme.start
    return (True, span) if span < PLACEHOLDER_SPAN else (False, -span)


def timeline(programmes: Iterable[Programme]) -> list[Programme]:
    """One programme at a time, oldest first: what the guide's grid, its details bar, the info
    line and the banner all show.

    Some guides list several programmes at once for a channel (feeds merged by the provider,
    or a placeholder a day long beside the real programmes). The one that started last shows;
    of programmes starting together, the one _rank puts first (an exact tie: the one listed
    last). A programme shows until another starts, and again after that one when it outlasts
    it: no gap where the guide has a programme, never two at once. A programme cut so is
    shown in pieces with their own times; one that overlaps nothing is kept as it is.
    """
    # Programmes starting together go lowest ranked first, so the winner is on top of `shown`;
    # the sort is stable, so of an exact tie the one listed last is.
    ordered = sorted(programmes, key=lambda p: (p.start, _rank(p)))
    result: list[Programme] = []
    shown: list[Programme] = []  # started, the latest on top; ones under it may have ended
    since: Optional[datetime] = None  # the timeline is complete up to here
    for programme in ordered:
        if since is not None:
            since = _show(shown, result, since, programme.start)
        if since is None or since < programme.start:
            since = programme.start  # a gap: nothing was on
        shown.append(programme)
    if since is not None:
        _show(shown, result, since, None)
    return result


def shifted(programme: Programme, offset: timedelta) -> Programme:
    """`programme` moved by a guide time offset."""
    if not offset:
        return programme
    return replace(programme, start=programme.start + offset, stop=programme.stop + offset)


def normalize(
    raw_programmes: Optional[Iterable[dict]], offset: timedelta = timedelta(0)
) -> list[Programme]:
    """A channel's programmes as its timeline (see timeline), moved by the provider's guide
    time offset."""
    parsed = (parse_programme(raw) for raw in raw_programmes or [])
    return timeline(shifted(p, offset) for p in parsed if p is not None)


def now_next(
    programmes: list[Programme], now: datetime
) -> tuple[Optional[Programme], Optional[Programme]]:
    programmes = timeline(programmes)  # the grid's programme on now, whatever the list
    current = next((p for p in programmes if p.is_on(now)), None)
    boundary = current.stop if current else now
    upcoming = next((p for p in programmes if p.start >= boundary), None)
    return current, upcoming


def programmes_for_channel(
    epg_manager, channel: dict, now: datetime, limit: int = 8
) -> list[Programme]:
    """The channel's timeline (see timeline) without what has ended by `now` (the guide's grid
    passes its window's start), oldest first, moved by its provider's guide time offset (the
    manager's `guide_offset`); [] if the EPG is unavailable."""
    try:
        raw = epg_manager.get_programs_for_channel(channel)
        offset_of = getattr(epg_manager, "guide_offset", None)
        offset = offset_of() if callable(offset_of) else None
        if not isinstance(offset, timedelta):
            offset = timedelta(0)
    except Exception as e:  # the manager may be reloading on a worker thread
        logger.debug("EPG lookup failed for %s: %s", channel.get("name"), e)
        return []
    return [p for p in normalize(raw, offset) if p.stop > now][:limit]
