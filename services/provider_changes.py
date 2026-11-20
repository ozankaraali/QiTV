"""One provider change at a time (Settings in the drawer, where each change applies at once).

The old Settings window edited a copy of the provider list and wrote it whole at Save. Here each
change (a provider added, one field or a few changed, a rename, a removal) is written alone, by
the same rules, so the list stays as older 2.x builds read it:

- `max_streams` only when the provider had the key or the value changed (Automatic, 0, is the
  key's absence; a key there keeps its explicit 0);
- `guide_offset_minutes` only when not 0 (0 removes the key);
- `ssl_verify` only when the provider had the key or the value differs from the global setting;
- a media server's password never (it signs in once; its token is kept instead).

Each write saves the list (`ProviderManager.save_providers`, which also lets go of the cache
of a name no longer listed). A write the disk refuses (OSError) changes nothing: the list goes
back to what it was, as the disk still holds it, and the error is the caller's to say. What the
TV window does after one is `ui.provider_actions`'.

The list object stays the one the providers' own managers share; a changed provider is listed
as a new entry, so whoever holds the one before keeps it as it was (as the old Save's copy).
Pure (no Qt): how a provider is named, and how a dropped one is compared, are here too.
"""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass, field
import os
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urlsplit

from services.epg_view import GUIDE_OFFSET_KEY
from services.provider_types import is_media_server

if TYPE_CHECKING:
    from services.provider_manager import ProviderManager

PLAYLIST_TYPES = ("M3UPLAYLIST", "M3USTREAM")
_MISSING = object()


class NameTaken(ValueError):
    """Another provider has the name (in any case): QiTV finds a provider by its name."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


@dataclass(frozen=True)
class Change:
    """What one update changed: the stored `fields` that differ now, and whether the
    connection did (`ProviderManager.provider_cache_identity`: its lists come again)."""

    identity_changed: bool = False
    fields: set[str] = field(default_factory=set)


class ProviderChanges:
    """The provider list of `manager`, changed one change at a time and saved after each.
    `global_ssl_verify` is the global Verify SSL setting as it is now."""

    def __init__(self, manager: ProviderManager, global_ssl_verify: Callable[[], bool]) -> None:
        self._manager = manager
        self._global_ssl_verify = global_ssl_verify

    @property
    def _providers(self) -> list[Any]:
        return self._manager.providers

    def names(self) -> list[str]:
        """The providers' names, in the list's order."""
        return [str(p.get("name") or "") for p in self._providers if isinstance(p, dict)]

    def get(self, name: str) -> dict:
        """Provider `name`'s entry (a copy; KeyError when none has the name)."""
        return deepcopy(self._providers[self._index(name)])

    def add(self, entry: dict) -> str:
        """List `entry` as a new provider, named as given or after its address (a playlist
        file's name), made unique ("name (2)"); its name in the list. ValueError when it has
        neither a name nor an address."""
        provider = self._written({}, entry)
        typed = str(provider.get("name") or "")
        name = typed if typed.strip() else self._default_name(provider)
        if not name:
            raise ValueError("A provider needs a name or an address")
        name = provider["name"] = unique_name(name, self.names())
        listed = list(self._providers)
        self._providers.append(provider)
        self._save(listed)
        return name

    def update(self, name: str, changes: dict) -> Change:
        """Change provider `name`'s fields by `changes` (not its name: `rename`); saved when
        anything changed. KeyError when no provider has the name."""
        index = self._index(name)
        changes = dict(changes)
        if changes.pop("name", name) != name:
            raise ValueError("A provider is renamed with rename()")
        before = self._providers[index]
        after = self._written(before, changes)
        fields = {
            key
            for key in before.keys() | after.keys()
            if before.get(key, _MISSING) != after.get(key, _MISSING)
        }
        if not fields:
            return Change()
        verify = self._global_ssl_verify()
        identity = self._manager.provider_cache_identity
        changed = identity(before, ssl_verify=verify) != identity(after, ssl_verify=verify)
        listed = list(self._providers)
        self._providers[index] = after
        self._save(listed)
        return Change(identity_changed=changed, fields=fields)

    def rename(self, old: str, new: str) -> str:
        """Rename provider `old` to `new` (blank: after its address); its name now. NameTaken
        when another provider has the name in any case; KeyError when none has `old`."""
        index = self._index(old)
        entry = self._providers[index]
        others = [n for i, n in enumerate(self._names_by_index()) if i != index]
        if not new.strip():
            new = self._default_name(entry, others) or old
        if new == old:
            return old
        folded = new.casefold()
        if any(other.casefold() == folded for other in others):
            raise NameTaken(new)
        listed = list(self._providers)
        self._providers[index] = {**deepcopy(entry), "name": new}
        self._save(listed)
        return new

    def remove(self, name: str) -> None:
        """Take provider `name` off the list (KeyError when none has the name)."""
        index = self._index(name)
        listed = list(self._providers)
        del self._providers[index]
        self._save(listed)

    def _save(self, listed: list) -> None:
        """Write the list; one the disk refuses (OSError) is put back as `listed`, the list as
        it was before the change (the same list object, which the providers' own managers
        share): memory stays what the disk holds, and the error goes on to the caller."""
        try:
            self._manager.save_providers()
        except OSError:
            self._providers[:] = listed
            raise

    # ---- the writing rules ----------------------------------------------------------------------

    def _written(self, before: dict, changes: dict) -> dict:
        """`before` with `changes` as the old Settings window wrote them (see the module)."""
        after = deepcopy(before)  # nothing shared with the entry listed before (as the old copy)
        for key, value in changes.items():
            if key == "max_streams":
                streams = _int(value)
                if "max_streams" in before or streams != _int(before.get("max_streams")):
                    after[key] = streams
            elif key == GUIDE_OFFSET_KEY:
                minutes = _int(value)
                if minutes:
                    after[key] = minutes
                else:
                    after.pop(key, None)
            elif key == "ssl_verify":
                verify = bool(value)
                if "ssl_verify" in before or verify != self._global_ssl_verify():
                    after[key] = verify
            else:
                after[key] = deepcopy(value)
        if is_media_server(after.get("type")):
            after.pop("password", None)  # its sign-in's token is kept instead (2.8)
        return after

    def _default_name(self, provider: dict, taken: Iterable[str] = ()) -> str:
        """An unnamed provider's name: its address (a playlist file's name, without its
        folder), never another provider's; "" without an address."""
        url = str(provider.get("url") or "").strip()
        if not url:
            return ""
        name = url
        if provider.get("type") in PLAYLIST_TYPES and os.path.isabs(url):
            name = os.path.splitext(os.path.basename(url))[0] or url
        return unique_name(name, taken)

    def _names_by_index(self) -> list[str]:
        return [str(p.get("name") or "") if isinstance(p, dict) else "" for p in self._providers]

    def _index(self, name: str) -> int:
        for index, provider in enumerate(self._providers):
            if isinstance(provider, dict) and provider.get("name") == name:
                return index
        raise KeyError(name)


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except TypeError, ValueError, OverflowError:
        return 0  # a hand-edited bad value means none (Automatic, no offset)


# ---- names and comparisons (also ui.provider_drop's) ----------------------------------------------


def same_provider(a: dict, b: dict) -> bool:
    """Whether two provider entries are one provider: its type, address and login."""

    def key(entry: dict) -> tuple[str, str, str]:
        return (
            str(entry.get("type", "")).upper(),
            normal_address(str(entry.get("url", "") or "")),
            str(entry.get("username", "") or ""),
        )

    return key(a) == key(b)


def normal_address(address: str) -> str:
    """A provider's address as compared: a web address without its scheme (http and https
    reach one server), its host in lower case, no default port, no trailing slash; a file's
    path as the system compares paths."""
    address = address.strip()
    if "://" not in address:
        return os.path.normcase(os.path.normpath(address)) if address else ""
    try:
        parts = urlsplit(address)
        port = parts.port
    except ValueError:
        return address.rstrip("/")
    if parts.scheme.lower() not in ("http", "https"):
        return address.rstrip("/")
    host = (parts.hostname or "").lower()
    if port is not None and port not in (80, 443):
        host = f"{host}:{port}"
    path = parts.path.rstrip("/")
    return f"//{host}{path}" + (f"?{parts.query}" if parts.query else "")


def unique_name(name: str, taken: Iterable[str]) -> str:
    """`name`, or "name (2)", "name (3)" … when the list already has it (in any case: QiTV
    finds a provider by its name, so two may never share one)."""
    used = {str(t).casefold() for t in taken if t is not None}
    candidate, number = name, 2
    while candidate.casefold() in used:
        candidate = f"{name} ({number})"
        number += 1
    return candidate
