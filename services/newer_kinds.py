"""Providers of kinds older 2.x builds don't know (2.14: Plex), kept beside index.json.

Older builds read cache/provider/index.json whole and list every entry in it: one of a kind
they can't open would show as "PLEX" and answer "Unsupported provider: PLEX". So this build
writes those providers to index.more instead, each with the name of the provider it followed
(`split`), and reads them back into their places (`merged`): an older build never sees them,
and its own saves leave the file alone. A provider an older build removed or renamed meanwhile
leaves its neighbour's place to the end of the list; a name it gave meanwhile wins (the newer
kind's provider is made unique).

The file's name ends in neither ".json" nor ".jsonl": every build's cache pruning treats those
in that folder as a provider's cache. It holds sign-ins, so it is written as index.json is
(whole or not at all, this user's only). Its format is versioned: {"version": 1, "providers":
[{"after": name, "provider": entry}]}; a file of a later version is neither read nor written.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import orjson as json

from services.atomic_write import atomic_write, set_aside
from services.provider_changes import unique_name
from services.provider_types import PLEX

logger = logging.getLogger(__name__)

FILE_NAME = "index.more"
VERSION = 1
KINDS = (PLEX,)  # the kinds older builds don't know


def is_newer(provider: Any) -> bool:
    return isinstance(provider, dict) and str(provider.get("type") or "").upper() in KINDS


def split(providers: list) -> tuple[list, list[dict]]:
    """(what index.json holds, the newer kinds' entries with the name each follows)."""
    older: list = []
    newer: list[dict] = []
    before = ""
    for provider in providers:
        if is_newer(provider):
            newer.append({"after": before, "provider": provider})
        else:
            older.append(provider)
        if isinstance(provider, dict):
            before = str(provider.get("name") or "")
    return older, newer


def merged(older: list, newer: Any) -> list:
    """index.json's list with the newer kinds' providers back in their places: after the
    provider each followed (first when it followed none), at the end when that one went.
    Damaged entries are left out."""
    providers = list(older)
    renamed: dict[str, str] = {}  # a newer kind's provider renamed here: its old name -> new
    for entry in newer if isinstance(newer, list) else []:
        provider = entry.get("provider") if isinstance(entry, dict) else None
        if not isinstance(provider, dict) or not is_newer(provider):
            continue
        own = provider.get("name")
        if not isinstance(own, str) or not own:
            continue
        names = [str(p.get("name") or "") for p in providers if isinstance(p, dict)]
        name = unique_name(own, names)
        if name != own:
            provider = {**provider, "name": name}
            renamed[own] = name
        after = str(entry.get("after") or "")
        after = renamed.get(after, after)  # (it followed that one, renamed: review M10)
        if not after:
            at = _first_after_newer(providers)
        else:
            at = next(
                (
                    i + 1
                    for i, p in enumerate(providers)
                    if isinstance(p, dict) and p.get("name") == after
                ),
                len(providers),
            )
        providers.insert(at, provider)
    return providers


def _first_after_newer(providers: list) -> int:
    """Where a provider that was first goes: after those of the newer kinds already put first
    (their order kept)."""
    at = 0
    while at < len(providers) and is_newer(providers[at]):
        at += 1
    return at


class NewerKinds:
    """index.more in `directory`: read once, written with each save of the provider list."""

    def __init__(self, directory: str) -> None:
        self.path = os.path.join(directory, FILE_NAME)
        self._writable = True  # a later version's file is left as it is

    def read(self) -> list[dict]:
        """The entries kept ([] for none, or one that can't be read: set aside, logged)."""
        try:
            with open(self.path, "rb") as f:
                data = json.loads(f.read())
        except FileNotFoundError:
            return []
        except OSError as error:
            logger.warning("Couldn't read %s: %s", FILE_NAME, error.strerror or error)
            self._writable = False  # (it may be there: never written over unread)
            return []
        except json.JSONDecodeError:
            return self._unreadable()
        if not isinstance(data, dict) or not isinstance(data.get("version"), int):
            return self._unreadable()
        if data["version"] > VERSION:
            logger.warning("%s is a newer QiTV's: its providers are not listed", FILE_NAME)
            self._writable = False
            return []
        entries = data.get("providers")
        return entries if isinstance(entries, list) else []

    def _unreadable(self) -> list[dict]:
        aside = set_aside(self.path)
        if aside is None:
            self._writable = False
        logger.warning(
            "%s can't be read%s",
            FILE_NAME,
            f": it was kept as {os.path.basename(aside)}" if aside else "",
        )
        return []

    def write(self, newer: list[dict]) -> None:
        """Keep `newer` (its file removed when there is none). Raises OSError, also when the
        file can't be written over (a later version's, or one that couldn't be read): the
        change is then said not saved and put back (review M2), never said added and lost."""
        if not self._writable:
            if newer:
                raise OSError(f"{FILE_NAME} is a newer QiTV's or can't be read")
            return
        if not newer:
            if os.path.lexists(self.path):  # (none: nothing asked of the disk)
                try:
                    os.remove(self.path)
                except FileNotFoundError:
                    pass
            return
        serialized = json.dumps({"version": VERSION, "providers": newer}, option=json.OPT_INDENT_2)
        with atomic_write(self.path) as f:
            f.write(serialized)

    def held(self) -> Optional[bytes]:
        """The file as it is now (None: there is none, or it can't be read), to put back."""
        try:
            with open(self.path, "rb") as f:
                return f.read()
        except OSError:
            return None

    def put_back(self, held: Optional[bytes]) -> None:
        """The file as `held` was (removed when there was none): index.json couldn't be
        written after it (review M3), so the two say the same again. Only logged if refused."""
        if not self._writable:
            return
        try:
            if held is None:
                if os.path.lexists(self.path):
                    os.remove(self.path)
            else:
                with atomic_write(self.path) as f:
                    f.write(held)
        except OSError as error:
            logger.warning("Couldn't put %s back: %s", FILE_NAME, error.strerror or error)
