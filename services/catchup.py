"""Provider catch-up (2.3): a channel's archive of past programmes, played from the provider.

Many providers keep the last days of some channels. QiTV's own time-shift buffer holds only
the minutes it recorded since the tune; further back, the provider's archive plays instead.
This module knows which channels have one, builds the archive's address for a moment, and
judges what a probe of that address read. No Qt.

What a channel's archive is, as the loaders keep it on the catalog item, and only on a channel
that has one (a catalog can hold 200,000 items; older builds ignore the keys):
- `archive_days`: its depth in days (Xtream: `tv_archive_duration` where `tv_archive` is 1; M3U:
  `catchup-days`, else `timeshift` or `tvg-rec`);
- M3U only: `catchup`, its type (default, append, shift, flussonic or xc), and
  `catchup_source`, the template the type fills in, when the playlist gives one. A playlist's
  `#EXTM3U` line may set them for every channel.
A catalog saved before 2.3 has none of them: its channels have no archive until it refreshes.

Addresses (`archive_urls`):
- Xtream: `{base}/timeshift/{user}/{pass}/{minutes}/{YYYY-MM-DD:HH-MM}/{id}.ts`, then the
  `{base}/streaming/timeshift.php?username=…&password=…&stream=…&start=…&duration=…` form. The
  start is a whole minute in the panel's time zone (`provider_zone`: Player API server_info's
  `timezone`, else the offset its `time_now` shows against `timestamp_now`, else UTC).
- M3U, by its type: default (the template is the address), append (the template goes after
  the channel's address), shift (`utc=` and `lutc=` go after it), flussonic
  (`…/index-{start}-{duration}.m3u8`, `…/timeshift_abs-{start}.ts`) and xc (the Xtream form,
  built from the channel's address, in UTC: a playlist names no time zone). Templates take the
  common placeholders, as the public documentation of catch-up playlists names them:
  {utc} ${start} {start}, {utcend} ${end} {end}, {lutc} ${now} {now} ${timestamp}
  {timestamp} (Unix seconds); {duration} ${duration} (seconds), {duration:60} (divided);
  {offset} ${offset} {offset:N} (now less the start); {Y} {m} {d} {H} {M} {S} (the start in
  UTC); and a format after a time's name, {utc:Y-m-d:H-M} or ${start:YmdHMS}.

The probe (`probe`, run on a worker; `judge` decides) reads the first 16 KB of an address and
closes it: MPEG-TS packets or an HLS playlist mean the archive plays (`PLAYS`); 404, 410 or
400, an empty answer, or an HTML, JSON or text page mean there is nothing (`NONE`); a refusal
that may be the provider's connection limit (401, 403, 407, 429, 458) is `REFUSED`; anything
else, a network failure included, proves nothing (`UNKNOWN`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from enum import Enum
import logging
import math
import re
import time
from typing import Any, Callable, Mapping, Optional
from urllib.parse import quote, urlsplit, urlunsplit

from services.lazy_import import lazy_module

logger = logging.getLogger(__name__)
requests = lazy_module("requests")

ARCHIVE_KEY = "archive_days"  # days of archive, on a channel that has one (not STB's "archive")
TYPE_KEY = "catchup"  # M3U: its catch-up type
SOURCE_KEY = "catchup_source"  # M3U: the template its type fills in
XTREAM = "xtream"  # an Xtream panel's channel: its archive's type
TYPES = ("default", "append", "shift", "flussonic", "xc")
MAX_DAYS = 31  # a longer archive reads as this long
MAX_MINUTES = 6 * 60  # the longest stretch one address asks for
PROBE_BYTES = 16 * 1024
PROBE_TIMEOUT_S = 8.0
_TS_PACKET = 188
# The names playlists give the types, as QiTV keeps them.
_TYPE_NAMES = {
    "default": "default",
    "append": "append",
    "shift": "shift",
    "flussonic": "flussonic",
    "flussonic-hls": "flussonic",
    "flussonic-ts": "flussonic",
    "fs": "flussonic",
    "xc": "xc",
    "xtream": "xc",
    "xtreamcodes": "xc",
}


@dataclass(frozen=True)
class Archive:
    """A channel's archive: how far back it goes and how its address is made."""

    days: int
    kind: str  # XTREAM, or an M3U type (TYPES)
    source: str = ""  # the M3U template its type fills in

    @property
    def seconds(self) -> float:
        return self.days * 86400.0


def _days(value: Any) -> int:
    """A count of days in a provider's field ("3", 3, "3.0"); 0 when it is none."""
    try:
        days = int(float(str(value).strip()))
    except TypeError, ValueError:
        return 0
    return max(0, min(days, MAX_DAYS))


def xtream_archive_fields(stream: Mapping[str, Any]) -> dict[str, Any]:
    """The archive keys of a Player API live stream: `archive_days` with its days where
    `tv_archive` is 1 (a panel that gives no days keeps one), else none."""
    if _days(stream.get("tv_archive")) != 1:
        return {}
    return {ARCHIVE_KEY: _days(stream.get("tv_archive_duration")) or 1}


def m3u_archive_fields(
    attributes: Mapping[str, str], defaults: Optional[Mapping[str, str]] = None
) -> dict[str, Any]:
    """The archive keys of an M3U entry's attributes (`defaults`: its `#EXTM3U` line's).

    A type the playlist names needs what it fills in: default and append a template. Days alone
    (`timeshift`, `tvg-rec`) read as the shift type. Without either, there is no archive.
    """
    defaults = defaults or {}

    def value(*names: str) -> str:
        for source in (attributes, defaults):
            for name in names:
                text = str(source.get(name) or "").strip()
                if text:
                    return text
        return ""

    named = value("catchup", "catchup-type").lower()
    source = value("catchup-source")
    days = _days(value("catchup-days", "timeshift", "tvg-rec"))
    if named:
        kind = _TYPE_NAMES.get(named, "")
        if not kind:
            return {}  # a type QiTV cannot make an address for (or "0", "disabled")
    elif source:
        kind = "default"
    elif days:
        kind = "shift"
    else:
        return {}
    if kind in ("default", "append") and not source:
        return {}
    fields: dict[str, Any] = {ARCHIVE_KEY: days or 1, TYPE_KEY: kind}
    if source:
        fields[SOURCE_KEY] = source
    return fields


def archive_of(item: Mapping[str, Any]) -> Optional[Archive]:
    """The channel's archive as the loaders kept it on the item; None without one."""
    days = item.get(ARCHIVE_KEY)
    if not isinstance(days, int) or isinstance(days, bool) or days <= 0:
        return None
    kind = item.get(TYPE_KEY)
    if kind is None:
        return Archive(min(days, MAX_DAYS), XTREAM)
    source = item.get(SOURCE_KEY) or ""
    if kind not in TYPES or not isinstance(source, str):
        return None
    if kind in ("default", "append") and not source:
        return None
    return Archive(min(days, MAX_DAYS), kind, source)


# ---- the panel's time zone -----------------------------------------------------------------


def xtream_zone_fields(server_info: Any) -> dict[str, Any]:
    """The time zone keys of a Player API sign-in's server_info: `timezone` (its IANA name)
    and `tz_offset` (the minutes its `time_now` runs ahead of UTC at `timestamp_now`), each
    when it is there."""
    if not isinstance(server_info, Mapping):
        return {}
    fields: dict[str, Any] = {}
    name = server_info.get("timezone")
    if isinstance(name, str) and name.strip():
        fields["timezone"] = name.strip()
    offset = _offset_minutes(server_info.get("time_now"), server_info.get("timestamp_now"))
    if offset is not None:
        fields["tz_offset"] = offset
    return fields


def _offset_minutes(time_now: Any, timestamp_now: Any) -> Optional[int]:
    """The panel's local time against UTC, to the quarter hour; None when it can't be told."""
    try:
        local = datetime.strptime(str(time_now).strip(), "%Y-%m-%d %H:%M:%S")
        stamp = float(str(timestamp_now).strip())
    except TypeError, ValueError:
        return None
    utc = datetime.fromtimestamp(stamp, timezone.utc).replace(tzinfo=None)
    minutes = (local - utc).total_seconds() / 60.0
    if not math.isfinite(minutes) or abs(minutes) > 15 * 60:
        return None
    return int(round(minutes / 15.0) * 15)


def provider_zone(name: Any = None, offset: Any = None) -> tzinfo:
    """The panel's time zone: by its name when this system knows it, else its offset, else UTC."""
    if isinstance(name, str) and name.strip():
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(name.strip())
        except Exception:  # unknown here (ZoneInfoNotFoundError, a bad key)
            logger.debug("Unknown panel time zone; using its offset")
    if isinstance(offset, int) and not isinstance(offset, bool) and abs(offset) <= 15 * 60:
        return timezone(timedelta(minutes=offset))
    return timezone.utc


# ---- addresses -----------------------------------------------------------------------------


@dataclass(frozen=True)
class _XtreamStream:
    base: str
    username: str
    password: str
    stream_id: str
    ext: str
    query: str = ""


_DIGITS = re.compile(r"^\d+$")


def _xtream_stream(cmd: str, username: str = "", password: str = "") -> Optional[_XtreamStream]:
    """An Xtream channel's address `{base}/live/{user}/{pass}/{id}.{ext}` (or the playlists'
    `{base}/{user}/{pass}/{id}`) taken apart; the account's own name and password find a
    password with a slash in it."""
    url = str(cmd or "").strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return None
    if username and password:
        for marker in (f"/live/{username}/{password}/", f"/{username}/{password}/"):
            at = parts.path.find(marker)
            if at >= 0:
                last = parts.path[at + len(marker) :]
                found = _stream_file(last)
                if found is not None:
                    base = urlunsplit((parts.scheme, parts.netloc, parts.path[:at], "", ""))
                    return _XtreamStream(base, username, password, *found, parts.query)
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) < 3:
        return None
    found = _stream_file(segments[-1])
    if found is None:
        return None
    user, secret = segments[-3], segments[-2]
    head = segments[:-3]
    if head and head[-1].lower() == "live":
        head = head[:-1]
    path = "/" + "/".join(head) if head else ""
    base = urlunsplit((parts.scheme, parts.netloc, path, "", ""))
    return _XtreamStream(base, user, secret, *found, parts.query)


def _stream_file(last: str) -> Optional[tuple[str, str]]:
    """A stream's last path part, "123.ts" or "123": its id and extension (ts by default)."""
    stem, dot, ext = last.partition(".")
    if "/" in last or not _DIGITS.match(stem):
        return None
    ext = ext.lower() if dot else ""
    return stem, ext if ext in ("ts", "m3u8") else "ts"


def _minutes(duration_s: float) -> int:
    return max(1, min(MAX_MINUTES, math.ceil(max(0.0, duration_s) / 60.0)))


def xtream_archive_urls(
    cmd: str,
    start: datetime,
    duration_s: float,
    zone: tzinfo,
    *,
    username: str = "",
    password: str = "",
) -> list[str]:
    """An Xtream channel's archive from `start` (its whole minute) for `duration_s`: the
    `/timeshift/` address, then the `timeshift.php` one; [] when `cmd` is no Xtream stream."""
    stream = _xtream_stream(cmd, username, password)
    if stream is None:
        return []
    local = start.astimezone(zone).strftime("%Y-%m-%d:%H-%M")
    minutes = _minutes(duration_s)
    path = (
        f"{stream.base}/timeshift/{stream.username}/{stream.password}/{minutes}/{local}/"
        f"{stream.stream_id}.{stream.ext}"
    )
    query = "&".join(
        (
            f"username={quote(stream.username, safe='')}",
            f"password={quote(stream.password, safe='')}",
            f"stream={stream.stream_id}",
            f"start={quote(local, safe=':-')}",
            f"duration={minutes}",
        )
    )
    return [path, f"{stream.base}/streaming/timeshift.php?{query}"]


_PLACEHOLDER = re.compile(r"\$?\{(\w+)(?::([^{}]*))?\}")
_TIMES = {
    "utc": "start",
    "start": "start",
    "utcend": "end",
    "end": "end",
    "lutc": "now",
    "now": "now",
    "timestamp": "now",
}
_PARTS = {"Y": "%Y", "m": "%m", "d": "%d", "H": "%H", "M": "%M", "S": "%S"}


def expand(template: str, start: datetime, end: datetime, now: datetime) -> str:
    """A catch-up template with its placeholders filled in (see the module docstring); one it
    does not know stays as it is."""
    moments = {"start": start, "end": end, "now": now}
    duration = max(0, int((end - start).total_seconds()))
    offset = max(0, int((now - start).total_seconds()))

    def fill(match: re.Match[str]) -> str:
        whole, name, arg = match.group(0), match.group(1), match.group(2)
        if name in _PARTS and arg is None:
            return start.astimezone(timezone.utc).strftime(_PARTS[name])
        key = name.lower()
        if key in _TIMES:
            moment = moments[_TIMES[key]]
            if arg is None:
                return str(int(moment.timestamp()))
            return _formatted(moment, arg)
        if key in ("duration", "offset"):
            seconds = duration if key == "duration" else offset
            if arg is None:
                return str(seconds)
            try:
                divisor = int(arg)
            except ValueError:
                return whole
            return str(seconds // divisor) if divisor > 0 else whole
        return whole

    return _PLACEHOLDER.sub(fill, template)


def _formatted(moment: datetime, pattern: str) -> str:
    """`moment` in UTC written as `pattern` says: Y m d H M S are its parts, all else stays."""
    utc = moment.astimezone(timezone.utc)
    return "".join(utc.strftime(_PARTS[c]) if c in _PARTS else c for c in pattern)


def _flussonic(cmd: str, start: datetime, duration_s: float) -> Optional[str]:
    """A Flussonic stream's archive: `index-{start}-{duration}.m3u8` for its HLS playlist,
    `timeshift_abs-{start}.ts` for its MPEG-TS (`…/mpegts`) or its bare name."""
    try:
        parts = urlsplit(cmd)
    except ValueError:
        return None
    if not parts.netloc:
        return None
    stamp, length = int(start.timestamp()), max(1, int(duration_s))
    head, _, last = parts.path.rpartition("/")
    if last.endswith(".m3u8"):
        path = f"{head}/{last[: -len('.m3u8')]}-{stamp}-{length}.m3u8"
    elif last == "mpegts" or last.endswith(".ts"):
        path = f"{head}/timeshift_abs-{stamp}.ts"
    else:
        path = f"{parts.path.rstrip('/')}/timeshift_abs-{stamp}.ts"
    return urlunsplit(parts._replace(path=path))


def m3u_archive_url(
    archive: Archive, cmd: str, start: datetime, duration_s: float, now: datetime
) -> Optional[str]:
    """An M3U channel's archive from `start` for `duration_s`, as its type makes it; None
    when its address does not fit the type."""
    end = start + timedelta(seconds=max(0.0, duration_s))
    kind = archive.kind
    if kind == "default":
        return expand(archive.source, start, end, now)
    if kind == "append":
        return f"{cmd}{expand(archive.source, start, end, now)}"
    if kind == "shift":
        sep = "&" if "?" in cmd else "?"
        return f"{cmd}{sep}utc={int(start.timestamp())}&lutc={int(now.timestamp())}"
    if kind == "flussonic":
        return _flussonic(cmd, start, duration_s)
    if kind == "xc":
        urls = xtream_archive_urls(cmd, start, duration_s, timezone.utc)
        return urls[0] if urls else None
    return None


def archive_urls(
    archive: Archive,
    cmd: str,
    start: datetime,
    duration_s: float,
    now: datetime,
    *,
    zone: tzinfo = timezone.utc,
    username: str = "",
    password: str = "",
) -> list[str]:
    """The addresses to try for the channel's archive from `start`, best first ([]: none)."""
    if archive.kind == XTREAM:
        return xtream_archive_urls(
            cmd, start, duration_s, zone, username=username, password=password
        )
    url = m3u_archive_url(archive, cmd, start, duration_s, now)
    return [url] if url and url.lower().startswith(("http://", "https://")) else []


def address_start(archive: Archive, start: datetime) -> datetime:
    """Where an address for `start` really starts: an Xtream one at the whole minute."""
    if archive.kind in (XTREAM, "xc"):
        return start.replace(second=0, microsecond=0)
    return start.replace(microsecond=0)


# ---- the probe -----------------------------------------------------------------------------


class Answer(str, Enum):
    PLAYS = "plays"  # media came: the archive plays
    NONE = "none"  # the provider said there is nothing there
    REFUSED = "refused"  # refused, maybe for its connection limit: proves nothing
    UNKNOWN = "unknown"  # no clear answer (a network failure, a server error)


_NONE_STATUS = (400, 404, 410)
_REFUSED_STATUS = (401, 403, 407, 429, 458)
_PAGES = ("text/html", "application/json", "text/plain", "application/xml", "text/xml")
_MEDIA = ("video/", "audio/", "application/octet-stream", "application/vnd.apple.mpegurl")


def _ts_sync(head: bytes) -> bool:
    """Two MPEG-TS packets in a row within the first packet's length."""
    for at in range(min(_TS_PACKET, len(head) - _TS_PACKET)):
        if head[at] == 0x47 and head[at + _TS_PACKET] == 0x47:
            return True
    return False


def judge(status: int, content_type: str, head: bytes) -> Answer:
    """What a probe's answer (`status`, its Content-Type and its first bytes) says."""
    if status in _NONE_STATUS:
        return Answer.NONE
    if status in _REFUSED_STATUS:
        return Answer.REFUSED
    if status not in (200, 206):
        return Answer.UNKNOWN
    kind = (content_type or "").split(";", 1)[0].strip().lower()
    if head.lstrip(b"\xef\xbb\xbf \r\n\t").startswith(b"#EXTM3U") or _ts_sync(head):
        return Answer.PLAYS
    if not head or kind.startswith(_PAGES):
        return Answer.NONE
    if kind.startswith(_MEDIA) or "mpegurl" in kind:
        return Answer.PLAYS
    return Answer.UNKNOWN


def probe(
    url: str,
    *,
    headers: Optional[Mapping[str, str]] = None,
    verify: bool = True,
    timeout: float = PROBE_TIMEOUT_S,
    get: Optional[Callable[..., Any]] = None,
    clock: Callable[[], float] = time.monotonic,
) -> Answer:
    """Read the first `PROBE_BYTES` of `url` and judge them (blocking: a worker's). A range
    the server refuses (416) is asked again without one."""
    get = get or requests.get
    asked = dict(headers or {})
    deadline = clock() + timeout
    for ranged in (True, False):
        sent = dict(asked, Range=f"bytes=0-{PROBE_BYTES - 1}") if ranged else asked
        try:
            response = get(url, headers=sent, stream=True, timeout=timeout, verify=verify)
        except Exception as e:  # requests' errors, or anything its adapters raise
            logger.info("The archive probe failed: %s", type(e).__name__)
            return Answer.UNKNOWN
        try:
            if response.status_code == 416 and ranged:
                continue
            head = b""
            try:
                for chunk in response.iter_content(4096):
                    head += chunk
                    if len(head) >= PROBE_BYTES or clock() > deadline:
                        break
            except Exception as e:
                logger.info("The archive probe's read failed: %s", type(e).__name__)
                if not head:
                    return Answer.UNKNOWN
            content_type = response.headers.get("Content-Type", "") or ""
            return judge(response.status_code, content_type, head[:PROBE_BYTES])
        finally:
            response.close()
    return Answer.UNKNOWN
