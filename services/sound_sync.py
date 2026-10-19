"""How far the viewer moved each live channel's sound against its picture (2.2).

On some channels the sound drifts out of step with the picture over time (other players do
the same: the stream's own clocks are clean). The viewer nudges it in steps of `STEP_MS`, up
to `LIMIT_MS` either way (J and K, or the player's Sound panel), and QiTV keeps it for the
channel's next tune: by `services.live.tuning.channel_key()`, the provider and the channel
hashed as `live_tuning.json` keys them, in a small file of its own, `sound_sync.json` beside
the config. `live_tuning.json` forgets what a channel taught once 500 others taught something
since; a sync stays until the viewer sets it back to zero. Only beyond `MAX_ENTRIES` channels
do the oldest go first. Films and episodes are not kept here (the player keeps theirs while
they play).

`SoundSync` keeps the entries in memory and in the file as `LiveTuning` does (see
`services.channel_store`). Version 1: a later build adds only optional fields, so this one
keeps reading the file.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from services.channel_store import ChannelStore

_LOG = logging.getLogger(__name__)

FILE_NAME = "sound_sync.json"
STEP_MS = 50  # one nudge (J, K, the panel's − and +)
LIMIT_MS = 5000  # the most either way
MAX_ENTRIES = 5000
_VERSION = 1


def clamp(ms: int) -> int:
    """`ms` within ±LIMIT_MS."""
    return max(-LIMIT_MS, min(LIMIT_MS, int(ms)))


def _entry(value: Any) -> Optional[dict[str, Any]]:
    """A stored entry as `set_audio_ms()` writes it; None when it is not one."""
    if not isinstance(value, dict):
        return None
    ms, when = value.get("audio_ms"), value.get("set")
    if not isinstance(ms, int) or isinstance(ms, bool) or not ms or abs(ms) > LIMIT_MS:
        return None
    if not isinstance(when, (int, float)) or isinstance(when, bool):
        return None
    return {"audio_ms": ms, "set": when}


class SoundSync(ChannelStore):
    """`SoundSync(path)`; `path` None keeps the syncs for this run only."""

    WHAT = "sound sync"
    VERSION = _VERSION
    MAX_ENTRIES = MAX_ENTRIES
    LOG = _LOG
    _entry = staticmethod(_entry)

    def audio_ms(self, key: str) -> int:
        """The channel's sound delay in milliseconds (later when positive); 0: in sync."""
        entry = self._entries.get(key) if key else None
        return int(entry["audio_ms"]) if entry is not None else 0

    def set_audio_ms(self, key: str, ms: int) -> None:
        """Keep the channel's sound delay (0 forgets it); write it soon."""
        if not key:
            return
        ms = clamp(ms)
        if not ms:
            if self._entries.pop(key, None) is None:
                return
        else:
            if self.audio_ms(key) == ms:
                return
            self._entries[key] = {"audio_ms": ms, "set": self._now()}
            self._trim()
        self._save()

    def _trim(self) -> None:
        """Beyond the cap, the syncs set longest ago go first."""
        excess = len(self._entries) - self._max
        if excess > 0:
            for key in sorted(self._entries, key=lambda k: self._entries[k]["set"])[:excess]:
                del self._entries[key]
