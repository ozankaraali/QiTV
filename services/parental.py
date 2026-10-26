"""Parental controls (2.8): a PIN, and the channel groups, channels and Library genres it hides.

With a PIN set, groups (a live group, a Library genre) and single channels can be hidden. While
QiTV is locked (always, unless unlocked with the PIN for UNLOCK_S) they are left out of every
list that reads the providers through a browsed catalog (the channel list, the guide, zapping,
the Library, the phone's lists) and of the searches. What they protect never enters the history,
the Recent list or Continue, locked or not.

The config keeps {"version": 1, "salt", "pin" (PBKDF2-SHA256 of the PIN), "hidden": [{"key",
"kind", "section", "provider", "title"}]}; older builds ignore the key. The PIN itself is never
stored or logged.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import time
from typing import Any, Callable, Iterable

from services.navigation import item_key
from services.provider_ids import provider_of

logger = logging.getLogger(__name__)

VERSION = 1
UNLOCK_S = 3600
ROUNDS = 120_000
SEP = "\x1f"
_ADULT = re.compile(
    r"(?<![a-z0-9])(xxx|adults?|porn\w*|erotic\w*|18\s*\+|\+\s*18)(?![a-z0-9])", re.I
)


def looks_adult(title: str) -> bool:
    """Whether a group's name says it is for adults (XXX, Adult, 18+, +18, …)."""
    return bool(_ADULT.search(str(title or "")))


def group_key(section: str, provider: str, group_id: str) -> str:
    return SEP.join(("group", section, provider, str(group_id)))


def channel_key(section: str, item: dict) -> str:
    return SEP.join(("item", section, provider_of(item) or "", item_key(item)))


def _hash(pin: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, ROUNDS).hex()


class Parental:
    """`config` keeps the PIN and the hidden list (`parental`); `clock` measures the unlock."""

    def __init__(self, config: Any, clock: Callable[[], float] = time.monotonic) -> None:
        self._config = config
        self._clock = clock
        self._unlocked_until = 0.0
        self.version = 0  # changes whenever what is hidden now changes (lists rebuild)
        self._groups: set[str] = set()
        self._items: set[str] = set()
        self._load()

    # ---- the stored part --------------------------------------------------------------------

    def _data(self) -> dict:
        data = getattr(self._config, "parental", None)
        if isinstance(data, dict) and data.get("version") == VERSION:
            return data
        return {"version": VERSION, "hidden": []}

    def _load(self) -> None:
        hidden = self._data().get("hidden") or []
        self._groups = {
            h["key"] for h in hidden if isinstance(h, dict) and h.get("kind") == "group"
        }
        self._items = {h["key"] for h in hidden if isinstance(h, dict) and h.get("kind") == "item"}

    def _save(self, data: dict) -> None:
        self._config.parental = data
        try:
            self._config.save_config()
        except Exception as e:  # the setting stays for this run
            logger.warning("Parental controls could not be saved: %s", type(e).__name__)
        self._load()
        self.version += 1

    # ---- the PIN ----------------------------------------------------------------------------

    def has_pin(self) -> bool:
        return bool(self._data().get("pin"))

    def set_pin(self, pin: str) -> None:
        salt = os.urandom(16)
        data = dict(self._data())
        data.update(salt=salt.hex(), pin=_hash(pin, salt))
        self._unlocked_until = 0.0
        self._save(data)

    def check(self, pin: str) -> bool:
        data = self._data()
        stored, salt = str(data.get("pin") or ""), str(data.get("salt") or "")
        if not stored or not salt:
            return False
        try:
            return hmac.compare_digest(_hash(pin, bytes.fromhex(salt)), stored)
        except ValueError:
            return False

    def clear_pin(self, pin: str) -> bool:
        """The PIN goes (and with it the hiding) when `pin` is right."""
        if not self.check(pin):
            return False
        self._save({"version": VERSION, "hidden": []})
        return True

    # ---- locked or not ----------------------------------------------------------------------

    def locked(self) -> bool:
        return self.has_pin() and self._clock() >= self._unlocked_until

    def unlock(self, pin: str, seconds: float = UNLOCK_S) -> bool:
        if not self.check(pin):
            return False
        self._unlocked_until = self._clock() + seconds
        self.version += 1
        return True

    def lock(self) -> None:
        if self._unlocked_until:
            self._unlocked_until = 0.0
            self.version += 1

    def active(self) -> bool:
        """Whether lists leave anything out now (a PIN, something hidden, locked)."""
        return bool(self._groups or self._items) and self.locked()

    def state_version(self) -> int:
        """`version`, plus the lock running out by itself (lists rebuild then too)."""
        return self.version * 2 + (1 if self.active() else 0)

    # ---- what is hidden ---------------------------------------------------------------------

    def hidden(self) -> list[dict]:
        return [dict(h) for h in self._data().get("hidden") or [] if isinstance(h, dict)]

    def _add(self, entry: dict) -> bool:
        if not self.has_pin():
            return False
        data = dict(self._data())
        hidden = [h for h in data.get("hidden") or [] if h.get("key") != entry["key"]]
        data["hidden"] = hidden + [entry]
        self._save(data)
        return True

    def hide_group(self, section: str, provider: str, group_id: str, title: str = "") -> bool:
        key = group_key(section, provider, group_id)
        entry = {"key": key, "kind": "group", "section": section, "provider": provider}
        return self._add({**entry, "title": title or str(group_id)})

    def hide_item(self, section: str, item: dict) -> bool:
        entry = {"key": channel_key(section, item), "kind": "item", "section": section}
        entry["provider"] = provider_of(item) or ""
        return self._add({**entry, "title": str(item.get("name") or "")})

    def show_again(self, key: str) -> None:
        data = dict(self._data())
        data["hidden"] = [h for h in data.get("hidden") or [] if h.get("key") != key]
        self._save(data)

    def hides_group(self, section: str, provider: str, group_id: str) -> bool:
        return self.active() and group_key(section, provider, group_id) in self._groups

    def protects(self, section: str, item: dict) -> bool:
        """Whether `item` is hidden (in a hidden group, or alone), locked or not."""
        if not self.has_pin() or not (self._groups or self._items):
            return False
        provider = provider_of(item) or ""
        if channel_key(section, item) in self._items:
            return True
        for field in ("tv_genre_id", "category_id"):
            group = item.get(field)
            if group not in (None, "") and group_key(section, provider, str(group)) in self._groups:
                return True
        return False

    def hides_item(self, section: str, item: dict) -> bool:
        return self.active() and self.protects(section, item)

    def visible(self, section: str, items: Iterable[dict]) -> list[dict]:
        """`items` without what is hidden now (the list as it is when nothing is)."""
        items = list(items)
        if not self.active():
            return items
        return [item for item in items if not self.protects(section, item)]


_installed: list[Parental] = []


def install(guard: "Parental | None") -> None:
    """The window's parental controls, for what reads the providers' lists outside a browsed
    catalog (the searches): None takes them away (tests, a closing window)."""
    _installed[:] = [guard] if guard is not None else []


def installed() -> "Parental | None":
    return _installed[0] if _installed else None


def visible_hits(hits: Iterable[Any]) -> list[Any]:
    """Search hits (`.section`, `.item`) without what parental controls hide now."""
    hits = list(hits)
    guard = installed()
    if guard is None or not guard.active():
        return hits
    return [hit for hit in hits if not guard.hides_item(hit.section, hit.item)]


def adult_groups(groups: Iterable[Any]) -> list[Any]:
    """The groups whose names look adult (offered for hiding when the PIN is first set)."""
    return [g for g in groups if not getattr(g, "header", False) and looks_adult(g.title)]
