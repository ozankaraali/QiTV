"""XMLTV and STB portal times, parsed as datetime.strptime parses them, only faster (no Qt).

strptime looks the locale up and runs a regular expression on every call, about 20 µs on a
mid-range machine. A guide lookup parses each programme's start and stop, and painting the
drawer, the zap column or the guide looks up every visible channel, so strptime alone made
those paints slow. The fixed-width forms guide data uses are read here by slicing; any other
text goes to strptime, so every value gives strptime's result or raises its error.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

XMLTV_FORMAT = "%Y%m%d%H%M%S %z"
XMLTV_NAIVE_FORMAT = "%Y%m%d%H%M%S"
STB_FORMAT = "%Y-%m-%d %H:%M:%S"

_ZONES: dict[str, timezone] = {}  # "+HHMM" -> its zone; at most 2 × 24 × 60 valid offsets


def _digits(text: str) -> bool:
    return text.isascii() and text.isdecimal()


def _stamp(text: str, tz: timezone | None) -> datetime:
    """The datetime of text starting "YYYYmmddHHMMSS"; ValueError for a field out of range."""
    return datetime(
        int(text[0:4]),
        int(text[4:6]),
        int(text[6:8]),
        int(text[8:10]),
        int(text[10:12]),
        int(text[12:14]),
        tzinfo=tz,
    )


def _zone(offset: str) -> timezone:
    """The zone of an offset, "+HHMM" or "-HHMM"; ValueError for minutes over 59 or a day."""
    zone = _ZONES.get(offset)
    if zone is None:
        minutes = int(offset[3:5])
        if minutes > 59:
            raise ValueError(offset)
        delta = timedelta(hours=int(offset[1:3]), minutes=minutes)
        zone = timezone(-delta if offset[0] == "-" else delta)
        _ZONES[offset] = zone
    return zone


def parse_xmltv(text: str) -> datetime:
    """datetime.strptime(text, XMLTV_FORMAT): "20261015203000 +0200" → an aware datetime."""
    if (
        len(text) == 20
        and text[14] == " "
        and text[15] in "+-"
        and _digits(text[:14])
        and _digits(text[16:])
    ):
        try:
            return _stamp(text, _zone(text[15:]))
        except ValueError:
            pass  # strptime decides (it may read the digits another way, or raise)
    return datetime.strptime(text, XMLTV_FORMAT)


def parse_xmltv_naive(text: str) -> datetime:
    """datetime.strptime(text, XMLTV_NAIVE_FORMAT): "20261015203000" → a naive datetime."""
    if len(text) == 14 and _digits(text):
        try:
            return _stamp(text, None)
        except ValueError:
            pass
    return datetime.strptime(text, XMLTV_NAIVE_FORMAT)


_XMLTV_PARSERS = {XMLTV_FORMAT: parse_xmltv, XMLTV_NAIVE_FORMAT: parse_xmltv_naive}


def parse_xmltv_as(text: str, fmt: str) -> datetime:
    """datetime.strptime(text, fmt) for fmt XMLTV_FORMAT or XMLTV_NAIVE_FORMAT."""
    return _XMLTV_PARSERS[fmt](text)


def parse_stb(text: str) -> datetime:
    """datetime.strptime(text, STB_FORMAT): "2026-10-15 20:30:00" → a naive datetime."""
    if (
        len(text) == 19
        and text[4] == "-"
        and text[7] == "-"
        and text[10] == " "
        and text[13] == ":"
        and text[16] == ":"
        and _digits(text[0:4] + text[5:7] + text[8:10] + text[11:13] + text[14:16] + text[17:19])
    ):
        try:
            return datetime(
                int(text[0:4]),
                int(text[5:7]),
                int(text[8:10]),
                int(text[11:13]),
                int(text[14:16]),
                int(text[17:19]),
            )
        except ValueError:
            pass
    return datetime.strptime(text, STB_FORMAT)
