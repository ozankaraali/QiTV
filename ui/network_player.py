"""A link opened with File → Open Link… plays, is kept and resumes (plan 4: Network streams).

`play(url, kind)` hands the TV controller an item for the link, `{"id", "name", "cmd",
"_provider"}`: its stable id (`network_streams.stream_id`: a fresh token keeps it), the name it
was kept under (else one made of its address), the address, and whose stream it is. That is
the configured Xtream account whose host and user name the link has (`owner`, the window's),
so it counts against that account's stream limit; any other link is Network streams'
(`NETWORK_PROVIDER`), with no limit, as a playlist's channel. A live link plays as a channel
(through the time-shift pipeline when it can) and lands in ⟲ Recent; a film plays as a film,
its resume point kept under Network streams by its id.

The controller asks `url_of(item)` for a link's address as it plays now (a link of ⟲ Recent
too): the kept entry's, whose address may have been replaced since, else the item's own;
None for an item its provider makes the link of. A kept link chosen in the channel list's
Network streams, or found by its find, plays by its id (`play_kept`): its address and kind as
kept. `rows()` are those links as the list's rows (a film's with where it stopped), and
`find_items()` as the find's channels, plain dicts without their addresses.

A link that showed its first picture is kept (`keep`, `opened`); one that did not play, or
went for good, is kept with why (`failed`, in plain words). Its kind changes only as this
player handed it over: a kept film played another way (as a channel of ⟲ Recent or a tile)
stays a film. The store's file is read once on
a worker (`start`), and each change is written on a worker, one write at a time with the
newest state; nothing is written before the file was read (what played meanwhile is merged
over it), and a file that could not be read is set aside before the first write. The links
are never logged.
"""

from __future__ import annotations

import logging
from pathlib import Path
import time
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, Signal
from shiboken6 import isValid

from services import network_streams
from services.navigation import LinkRow
from services.network_streams import (
    FILM,
    Entry,
    NetworkStreams,
    default_name,
    is_link,
    stream_id,
)
from services.provider_ids import NETWORK_PROVIDER

logger = logging.getLogger(__name__)


class _ReadJob(QObject):
    """The store's file read on the runner's thread: its bytes (None: there is none)."""

    finished = Signal(object)
    error = Signal(str)

    def __init__(self, path: Path) -> None:
        super().__init__()
        self._path = path

    def run(self) -> None:
        try:
            data = network_streams.read_bytes(self._path)
        except OSError as e:
            logger.warning("Couldn't read the kept network streams (%s)", type(e).__name__)
            self.error.emit(type(e).__name__)
            return
        self.finished.emit(data)


class _WriteJob(QObject):
    """The store written on the runner's thread (the unreadable file set aside first)."""

    finished = Signal(object)
    error = Signal(str)

    def __init__(self, path: Path, data: bytes, set_aside: bool) -> None:
        super().__init__()
        self._path, self._data, self._set_aside = path, data, set_aside

    def run(self) -> None:
        try:
            network_streams.save(self._path, self._data, set_aside=self._set_aside)
        except OSError as e:
            logger.warning("Couldn't keep the network streams (%s)", type(e).__name__)
            self.error.emit(type(e).__name__)
            return
        self.finished.emit(None)


class NetworkPlayer(QObject):
    """Plays the links of `store` through `controller` (a TVController) and keeps them.
    `jobs` runs the file's read and writes (the window's JobRunner); `owner(url)` names the
    configured account whose stream a link is ("" for none); `now()` is the time a link
    played (Unix seconds)."""

    changed = Signal()  # the kept links changed, or were read

    def __init__(
        self,
        store: NetworkStreams,
        jobs: Any,
        controller: Any,
        *,
        owner: Callable[[str], str],
        now: Callable[[], float] = time.time,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.store = store
        self._jobs = jobs
        self._controller = controller
        self._owner = owner
        self._now = now
        self._loading = self._loaded = False
        self._writing = False  # a write on its way: the next waits for it
        self._dirty = False  # changed since the last write began
        self._set_aside = False  # the file could not be read: moved aside before the write
        self._handed: dict[str, str] = {}  # each link's kind as this player last played it
        controller.set_links(self.url_of)
        controller.set_link_player(self.play_kept)
        controller.linkPlayed.connect(self._on_played)
        controller.linkFailed.connect(self._on_failed)

    # ---- the file ---------------------------------------------------------------------------

    def start(self) -> None:
        """Read the store's file on a worker (once): its links are merged in when it comes."""
        if self._loading or self._loaded:
            return
        self._loading = True
        self._jobs.run_worker(_ReadJob(self.store.path), self._on_read, self._on_read_failed)

    def loaded(self) -> bool:
        """The store's file was read (or found missing, or unreadable)."""
        return self._loaded

    def _on_read(self, _context: object, data: object) -> None:
        if not isValid(self):
            return
        try:
            if isinstance(data, bytes) and not self.store.merge(data):
                self._set_aside = True  # what the file held is kept beside it
        except Exception as e:
            logger.warning("The kept network streams could not be read (%s)", type(e).__name__)
            self._set_aside = True  # kept beside the new one, never written over
        finally:
            self._read_done()  # read or not, what plays from now on is kept

    def _on_read_failed(self, _context: object, _message: str) -> None:
        if isValid(self):
            self._set_aside = True  # there, but unreadable: kept beside the new one
            self._read_done()

    def _read_done(self) -> None:
        self._loading, self._loaded = False, True
        self.changed.emit()
        if self._dirty:
            self._save()

    def _save(self) -> None:
        """Write the store on a worker: once the file was read, one write at a time, the
        newest state each."""
        if not self._loaded or self._writing:
            self._dirty = True
            return
        self._dirty = False
        self._writing = True
        aside, self._set_aside = self._set_aside, False
        job = _WriteJob(self.store.path, self.store.save_bytes(), aside)
        self._jobs.run_worker(job, self._on_written, self._on_write_failed, context=aside)

    def _on_written(self, _context: object, _payload: object) -> None:
        if not isValid(self):
            return
        self._writing = False
        if self._dirty:
            self._save()

    def _on_write_failed(self, aside: object, _message: str) -> None:
        """The write failed (logged): an unreadable file it was to set aside still is, and the
        next change writes everything (never at once again: a disk that fails stays quiet)."""
        if not isValid(self):
            return
        self._writing = False
        if aside:
            self._set_aside = True
        if self._dirty:
            self._save()

    # ---- playing ------------------------------------------------------------------------------

    def play(self, url: str, kind: str) -> None:
        """Play the link `url`, `kind` LIVE or FILM (Open Link's). A kept link's address
        becomes this one (a fresh token): ⟲ Recent plays it from now on."""
        url = url.strip()
        if not url:
            return
        key = stream_id(url)
        entry = self.store.get(key)
        if entry is not None and entry.url != url:
            self.store.set_url(key, url)  # the newest address handed over is the one it plays
            self.changed.emit()
            self._save()
        name = entry.name if entry is not None else default_name(url)
        self._hand_over(key, name, url, kind)

    def play_kept(self, key: str) -> bool:
        """The link kept under `key` plays (its row in the channel list, or the find's), its
        address and kind as kept; False when none is kept under it."""
        entry = self.store.get(key)
        if entry is None:
            return False
        self._hand_over(entry.id, entry.name, entry.url, entry.kind)
        return True

    def _hand_over(self, key: str, name: str, url: str, kind: str) -> None:
        """The controller plays the link `url` as `key`'s: as a channel or a film, the stream
        of the account whose host and user name it has (else Network streams')."""
        owner = self._owner(url) or NETWORK_PROVIDER
        item = {"id": key, "name": name, "cmd": url, "_provider": owner}
        self._handed[key] = kind
        whose = "its account's" if owner != NETWORK_PROVIDER else "no provider's"
        logger.info("Open Link: a %s link plays (%s stream)", kind.lower(), whose)
        self._controller.play_link(item, "movies" if kind == FILM else "live")

    def rows(self) -> list[LinkRow]:
        """The kept links as the channel list's rows, newest opened first: a film's with
        where it stopped (as it resumes), one that failed with why."""
        rows = []
        for entry in self.store.entries():
            stopped = None
            if entry.kind == FILM:
                item = {"id": entry.id, "cmd": entry.url, "_provider": NETWORK_PROVIDER}
                stopped = self._controller.stopped_at(item)
            rows.append(LinkRow(entry.id, entry.name, entry.kind, entry.failed, stopped))
        return rows

    def find_items(self) -> list[dict]:
        """The kept links as the channel list's find reads them (plain data for its worker):
        their ids and names, Network streams' (never their addresses)."""
        return [
            {"id": e.id, "name": e.name, "_provider": NETWORK_PROVIDER}
            for e in self.store.entries()
        ]

    def url_of(self, item: dict) -> Optional[str]:
        """A link's address as it plays now: its kept entry's (by its id: a token replaced
        since), else its own; None for an item its provider makes the link of."""
        key = item.get("id")
        entry = self.store.get(key) if isinstance(key, str) else None
        if entry is not None:
            return entry.url
        if is_link(item):
            return str(item.get("cmd") or "").strip() or None
        return None

    def _kept(self, item: dict, kind: str, url: str) -> Entry:
        """The entry of the link `item` that played `url` as `kind`: kept now if it was not (or
        its address updated, and its kind when this player played it so), else the one its
        id names (its address replaced since)."""
        key = item.get("id")
        entry = self.store.get(key) if isinstance(key, str) else None
        if entry is not None and entry.id != stream_id(url):
            return entry
        kept = entry or self.store.get(stream_id(url))
        if kept is not None and self._handed.get(kept.id) != kind:
            kind = kept.kind  # played another way (⟲ Recent, a tile): what it was kept as
        return self.store.keep(url, str(item.get("name") or ""), kind)

    def _on_played(self, item: object, kind: str, url: str) -> None:
        if not isValid(self) or not isinstance(item, dict):
            return
        entry = self._kept(item, kind, url)
        self.store.opened(entry.id, self._now())
        self.changed.emit()
        self._save()

    def _on_failed(self, item: object, kind: str, url: str, why: str) -> None:
        if not isValid(self) or not isinstance(item, dict):
            return
        entry = self._kept(item, kind, url)
        self.store.failed(entry.id, why)
        logger.info("A link of Network streams did not play: %s", why)
        self.changed.emit()
        self._save()
