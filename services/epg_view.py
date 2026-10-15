"""Normalized EPG programmes for the TV shell: one shape for STB and XMLTV data (no Qt)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
from typing import Any, Iterable, Optional

from services.epg_time import parse_stb, parse_xmltv, parse_xmltv_naive

logger = logging.getLogger(__name__)


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


def normalize(raw_programmes: Optional[Iterable[dict]]) -> list[Programme]:
    parsed = [p for p in (parse_programme(raw) for raw in raw_programmes or []) if p is not None]
    parsed.sort(key=lambda p: p.start)
    return parsed


def now_next(
    programmes: list[Programme], now: datetime
) -> tuple[Optional[Programme], Optional[Programme]]:
    current = next((p for p in programmes if p.is_on(now)), None)
    boundary = current.stop if current else now
    upcoming = next((p for p in programmes if p.start >= boundary), None)
    return current, upcoming


def programmes_for_channel(
    epg_manager, channel: dict, now: datetime, limit: int = 8
) -> list[Programme]:
    """Programmes that have not ended yet, oldest first; [] if the EPG is unavailable."""
    try:
        raw = epg_manager.get_programs_for_channel(channel, None, 0)
    except Exception as e:  # the manager may be reloading on a worker thread
        logger.debug("EPG lookup failed for %s: %s", channel.get("name"), e)
        return []
    return [p for p in normalize(raw) if p.stop > now][:limit]
