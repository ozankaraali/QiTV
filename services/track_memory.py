"""The sound and subtitle tracks the viewer chose for each film, series and live channel
(2.9.1), for the next time it plays.

The player keeps a choice only while its title plays (services.playback: chosen again in each
reopen of it, and by language in the next episode). Picked in the Sound or Subtitles panel, with
a key or from a phone remote, it is kept here too, and chosen again when the title opens another
day: a film by its TMDB id when it has one (a provider that numbers it anew keeps it), else by
the provider's number; a series once for all its episodes (their tracks are matched by
language); a live channel by `services.live.tuning.channel_key()`, as `sound_sync.json` keys
it. A subtitle file is never kept here (services.subtitle_picks keeps OpenSubtitles' picks):
only the media's own tracks, and subtitles turned off.

The file is `track_choices.json` beside the config, kept as `services.channel_store` keeps its
files, the titles hashed with their provider so that it never names one. Beyond `MAX_ENTRIES`
the choices made longest ago go first. Version 1: a later build adds only optional fields, so
this one keeps reading the file.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Mapping, Optional

from services.channel_store import ChannelStore

_LOG = logging.getLogger(__name__)

FILE_NAME = "track_choices.json"
MAX_ENTRIES = 5000
KINDS = ("audio", "spu")  # the sound's track, the subtitles' (-1: off)
_VERSION = 1
_NAME_MAX = 200


def _identity(item: Mapping[str, Any], fields: tuple[str, ...]) -> str:
    for field in fields:
        value = item.get(field)
        if value not in (None, "", 0, "0"):
            return f"{field}:{value}"
    return ""


def title_key(
    provider: str, item: Mapping[str, Any], *, episode: bool, context: Optional[dict] = None
) -> str:
    """A film's or a series' key under `provider` ("" when the item names nothing): an episode's
    is its series' (every episode shares the series' choice)."""
    if episode:
        context = context or {}
        series = context.get("series") if isinstance(context.get("series"), dict) else None
        owner = _identity(series, ("tmdb", "series_id", "id", "name")) if series else ""
        if not owner:
            owner = _identity(item, ("series_id",))
        return _hashed(provider, f"series:{owner}") if owner else ""
    identity = _identity(item, ("tmdb", "stream_id", "id", "cmd", "name"))
    return _hashed(provider, f"film:{identity}") if identity else ""


def _hashed(provider: str, identity: str) -> str:
    return hashlib.sha256(f"{provider}\0tracks\0{identity}".encode("utf-8")).hexdigest()[:32]


def _track(value: Any) -> Optional[tuple[int, str]]:
    if not isinstance(value, list) or len(value) != 2:
        return None
    track, name = value
    if not isinstance(track, int) or isinstance(track, bool) or not isinstance(name, str):
        return None
    return track, name[:_NAME_MAX]


def _entry(value: Any) -> Optional[dict[str, Any]]:
    """A stored entry as `choose()` writes it; None when it is not one."""
    if not isinstance(value, dict):
        return None
    when = value.get("set")
    if not isinstance(when, (int, float)) or isinstance(when, bool):
        return None
    entry: dict[str, Any] = {"set": when}
    for kind in KINDS:
        track = _track(value.get(kind))
        if track is not None:
            entry[kind] = list(track)
    return entry if len(entry) > 1 else None


class TrackMemory(ChannelStore):
    """`TrackMemory(path)`; `path` None keeps the choices for this run only."""

    WHAT = "track choices"
    VERSION = _VERSION
    MAX_ENTRIES = MAX_ENTRIES
    LOG = _LOG
    _entry = staticmethod(_entry)

    def chosen(self, key: str) -> dict[str, tuple[int, str]]:
        """The tracks chosen for `key`, by kind ("audio", "spu"): (id, libVLC's name)."""
        entry = self._entries.get(key) if key else None
        if entry is None:
            return {}
        return {kind: (int(entry[kind][0]), str(entry[kind][1])) for kind in KINDS if kind in entry}

    def choose(self, key: str, kind: str, track: int, name: str) -> None:
        """The viewer chose `track` (named `name`; -1: none) of `kind` for `key`: kept, and
        written soon."""
        if not key or kind not in KINDS:
            return
        entry = dict(self._entries.get(key) or {})
        value = [int(track), str(name or "")[:_NAME_MAX]]
        if entry.get(kind) == value:
            return
        entry[kind] = value
        entry["set"] = self._now()
        self._entries[key] = entry
        self._trim()
        self._save()

    def _trim(self) -> None:
        """Beyond the cap, the choices made longest ago go first."""
        excess = len(self._entries) - self._max
        if excess > 0:
            for key in sorted(self._entries, key=lambda k: self._entries[k]["set"])[:excess]:
                del self._entries[key]
