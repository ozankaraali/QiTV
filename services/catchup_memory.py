"""Whether each channel's provider archive answered (2.3), for the next time it is asked for.

A channel may claim an archive and still have none (the provider answers 404, or a page). So
QiTV keeps, per channel (`services.live.tuning.channel_key()`: the provider and the channel,
hashed), when its archive last answered and which of its addresses did (`form`: the index in
`services.catchup.archive_urls`), or when it last did not. An archive that did not answer is
not asked for again for `FAILED_S` (three days): rewinding stops at the start of QiTV's own
buffer then. One that answered within `ANSWERED_S` (a day) opens without a probe. Network
failures and refusals teach nothing (they say nothing about the archive).

`CatchupMemory` keeps the entries in memory and in `catchup.json` beside the config as
`services.channel_store` keeps them; version 1 (a later build adds only optional fields). A
time in the future (the clock was set back) counts as expired.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from services.channel_store import ChannelStore

_LOG = logging.getLogger(__name__)

FILE_NAME = "catchup.json"
FAILED_S = 3 * 24 * 60 * 60  # an archive that did not answer is not asked again for this long
ANSWERED_S = 24 * 60 * 60  # one that answered opens without a probe for this long
MAX_ENTRIES = 2000
_VERSION = 1


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _entry(value: Any) -> Optional[dict[str, Any]]:
    """A stored entry as `remember()` writes it; None when it is not one."""
    if not isinstance(value, dict):
        return None
    answered, form, used = value.get("answered"), value.get("form", 0), value.get("used")
    if not isinstance(answered, bool) or not _number(used):
        return None
    if not isinstance(form, int) or isinstance(form, bool) or not 0 <= form < 8:
        return None
    entry: dict[str, Any] = {"answered": answered, "used": used}
    if answered and form:
        entry["form"] = form
    return entry


class CatchupMemory(ChannelStore):
    """`CatchupMemory(path)`; `path` None keeps what is learned for this run only."""

    WHAT = "catch-up memory"
    VERSION = _VERSION
    MAX_ENTRIES = MAX_ENTRIES
    LOG = _LOG
    _entry = staticmethod(_entry)

    def _fresh(self, key: str, answered: bool, lasts: float) -> Optional[dict[str, Any]]:
        entry = self._entries.get(key) if key else None
        if entry is None or entry["answered"] is not answered:
            return None
        return entry if 0 <= self._now() - entry["used"] < lasts else None

    def failed(self, key: str) -> bool:
        """The channel's archive did not answer within the last `FAILED_S`."""
        return self._fresh(key, False, FAILED_S) is not None

    def answered(self, key: str) -> Optional[int]:
        """The address form that answered within the last `ANSWERED_S`, else None."""
        entry = self._fresh(key, True, ANSWERED_S)
        return int(entry.get("form", 0)) if entry is not None else None

    def remember(self, key: str, answered: bool, form: int = 0) -> None:
        """The channel's archive answered (with the address `form`), or did not; write soon."""
        if not key:
            return
        entry: dict[str, Any] = {"answered": bool(answered), "used": self._now()}
        if answered and form:
            entry["form"] = int(form)
        if self._entries.get(key) == entry:
            return
        self._entries[key] = entry
        self._trim()
        self._save()

    def forget(self, key: str) -> None:
        if self._entries.pop(key, None) is not None:
            self._save()

    def _trim(self) -> None:
        excess = len(self._entries) - self._max
        if excess > 0:
            for key in sorted(self._entries, key=lambda k: self._entries[k]["used"])[:excess]:
                del self._entries[key]
