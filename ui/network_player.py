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
None for an item its provider makes the link of. It asks `whose(item)` whose stream a link is
each time it plays, from any path (⟲ Recent, a tile, the picture in picture, the phone's
Watch), and so does ⟲ Recent as it lists it: never the stamp it was kept with (an account
configured after its link played owns it from then on). A kept link chosen in the channel list's
Network streams, or found by its find, plays by its id (`play_kept`): its address and kind as
kept. Channel up/down while a kept link plays steps to the next or previous kept link (`step`),
in Network streams' order as the steps began, round from the last to the first. `rows()` are
those links as the list's rows (a film's with where it stopped), and `find_items()` as the
find's channels, plain dicts without their addresses. A link hidden with "Hide (parental
controls)" is hidden as a channel is while locked: its row goes, the steps pass it, and the
controller plays it nowhere (`hides_link`: by its id, whoever's stream it is now).

A link that showed its first picture is kept (`keep`, `opened`); one that did not play, or
went for good, is kept with why (`failed`, in plain words). Its kind changes only as this
player handed it over: a kept film played another way (as a channel of ⟲ Recent or a tile)
stays a film. Settings › Providers › Network streams changes them too (ui.settings_links):
`rename`, `set_url` (a fresh token, or another stream that replaces the link's: its id stays,
so its name and ⟲ Recent's row follow, and Open Link of that address finds it; its resume
point stays for a fresh token only, through the controller's `forget_stopped`),
`keep_new` and `remove`. A link removed is not kept again by what plays it next in this run
(the stream still playing, ⟲ Recent), only by Open Link. The store's file is read once on
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
    LIVE,
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
        self._removed: set[str] = set()  # links removed: not kept again but by Open Link
        # Channel up/down's order of the kept links, as the steps began, and the link the last
        # step played (`step`). The links that failed before they played in this run (a
        # site's page that gave no stream) are stepped past, from the link that plays on
        # meanwhile (the run's anchor: the one its steps left from).
        self._steps: list[str] = []
        self._stepped = ""
        self._anchor = ""
        self._step_played = False  # the last step's link played (its failures after are not)
        self._passed: set[str] = set()
        controller.set_links(self.url_of)
        controller.set_link_owner(self.whose)
        controller.set_link_player(self.play_kept)
        controller.set_link_stepper(self.step)
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
        entry = self._of_stream(stream_id(url))
        key = entry.id if entry is not None else stream_id(url)
        self._removed.discard(key)  # opened again: kept again once it plays
        if entry is not None and entry.url != url:
            self.set_url(key, url)  # the newest address handed over is the one it plays
        name = entry.name if entry is not None else default_name(url)
        self._hand_over(key, name, url, kind)

    def _of_stream(self, key: str) -> Optional[Entry]:
        """The link whose stream `key` names (a stream_id): kept under it, else one whose
        address was replaced with that stream's in Settings (kept under its own id)."""
        entry = self.store.get(key)
        if entry is not None:
            return entry
        return next((e for e in self.store.entries() if stream_id(e.url) == key), None)

    def play_kept(self, key: str, zapped: bool = False) -> bool:
        """The link kept under `key` plays (its row in the channel list, or the find's), its
        address and kind as kept; False when none is kept under it. `zapped`: a channel
        up/down's step to it (`step`), as a zap's (an outside player waits for the steps to
        settle)."""
        entry = self.store.get(key)
        if entry is None:
            return False
        self._hand_over(entry.id, entry.name, entry.url, entry.kind, zapped)
        return True

    def step(self, item: dict, delta: int) -> bool:
        """Channel up (+1) or down (−1) while `item` plays: when it is a kept link, the next
        or previous kept link plays, as `play_kept` plays it (a film as a film, an account's
        link as its stream), round from the last to the first as channels go; with it alone,
        nothing plays. False for anything else (a provider's channel: the channels' zap). The
        order is Network streams' as the steps began: a link that plays moves to its top, never
        under the viewer's steps. A link that failed before it played in this run of steps (a
        site's page whose site gave no stream) is stepped past, either way, from the link that
        plays on: each is asked once a run, until a step plays."""
        key = item.get("id") if isinstance(item, dict) else None
        if not isinstance(key, str) or self.store.get(key) is None:
            return False
        kept = [e.id for e in self.store.entries() if e.id == key or not self._hidden(e)]
        going_on = key == self._stepped or (key == self._anchor and bool(self._passed))
        if not going_on or set(self._steps) != set(kept):
            self._steps = kept  # a new run of steps, or the links changed meanwhile
            self._passed = set()
        at, count = self._steps.index(key), len(self._steps)
        way = 1 if delta > 0 else -1
        ahead = [self._steps[(at + way * n) % count] for n in range(1, count)]
        then = next((k for k in ahead if k not in self._passed), None)
        if then is None and self._passed:
            # Every other link failed in this run: they are asked in turn again (a press
            # never does nothing while another link is kept).
            self._passed = set()
            then = ahead[0] if ahead else None
        if then is not None:
            self._anchor, self._stepped, self._step_played = key, then, False
            self.play_kept(then, zapped=True)
        return True

    def _hand_over(self, key: str, name: str, url: str, kind: str, zapped: bool = False) -> None:
        """The controller plays the link `url` as `key`'s: as a channel or a film, the stream
        of the account whose host and user name it has (else Network streams'); `zapped`, as
        a zap step's."""
        owner = self._owner(url) or NETWORK_PROVIDER
        item = {"id": key, "name": name, "cmd": url, "_provider": owner}
        self._handed[key] = kind
        whose = "its account's" if owner != NETWORK_PROVIDER else "no provider's"
        logger.info("Open Link: a %s link plays (%s stream)", kind.lower(), whose)
        self._controller.play_link(item, "movies" if kind == FILM else "live", zapped=zapped)

    def _hidden(self, entry: Entry) -> bool:
        """Whether parental controls hide the kept link now (locked; the controller's)."""
        item = {"id": entry.id, "name": entry.name, "cmd": entry.url}
        return bool(self._controller.hides_link({**item, "_provider": NETWORK_PROVIDER}))

    def rows(self) -> list[LinkRow]:
        """The kept links as the channel list's rows (and Settings'), newest opened first: a
        film's with where it stopped (as it resumes), one that failed with why; none that
        parental controls hide now."""
        rows = []
        for entry in self.store.entries():
            if self._hidden(entry):
                continue
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

    def whose(self, item: dict) -> Optional[str]:
        """Whose stream the link `item` is now: the configured account whose host and user name
        its address has, else Network streams'; None for an item that is no link. Asked as it
        plays, never read from the stamp it was kept with."""
        url = self.url_of(item)
        return None if url is None else (self._owner(url) or NETWORK_PROVIDER)

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

    def _dropped(self, item: dict, url: str) -> bool:
        """Whether `item` is a link removed in Settings (still playing, or played again from
        ⟲ Recent): it is not kept again; only Open Link keeps it again."""
        key = item.get("id")
        if isinstance(key, str) and self.store.get(key) is not None:
            return False
        return (isinstance(key, str) and key in self._removed) or stream_id(url) in self._removed

    def _on_played(self, item: object, kind: str, url: str) -> None:
        if not isValid(self) or not isinstance(item, dict) or self._dropped(item, url):
            return
        entry = self._kept(item, kind, url)
        self.store.opened(entry.id, self._now())
        if entry.id == self._stepped:
            self._step_played = True
            self._passed = set()  # a step played: the links passed are asked in turn again
        self._changed()

    def _on_failed(self, item: object, kind: str, url: str, why: str) -> None:
        if not isValid(self) or not isinstance(item, dict) or self._dropped(item, url):
            return
        entry = self._kept(item, kind, url)
        self.store.failed(entry.id, why)
        if entry.id == self._stepped and not self._step_played:
            self._passed.add(entry.id)  # it never played: the run's next steps pass it
        logger.info("A link of Network streams did not play: %s", why)
        self._changed()

    # ---- Settings' changes (ui.settings_links) ------------------------------------------------

    def rename(self, key: str, name: str) -> None:
        """The link kept under `key` is listed as `name` from now on."""
        if self.store.get(key) is not None:
            self.store.rename(key, name)
            self._changed()

    def set_url(self, key: str, url: str, kind: str = "") -> None:
        """The link kept under `key` plays `url` from now on: its stream's fresh token, or
        another stream that replaces its own, of `kind` (LIVE or FILM: as that address says;
        "" keeps the link's). Its id stays, with its name and ⟲ Recent's row, and why it
        failed last time goes: that was the old address's. Its resume point stays for the
        stream it plays now only: another stream's film (its first one again, after a
        Replace) starts from its beginning."""
        entry = self.store.get(key)
        if entry is None:
            return
        another = stream_id(url) != stream_id(entry.url)  # (its first stream again too)
        self.store.set_url(key, url)
        if kind in (LIVE, FILM):
            entry.kind = kind
        self.store.failed(key, "")
        if another:
            self._controller.forget_stopped({"id": key, "cmd": url, "_provider": NETWORK_PROVIDER})
        self._changed()

    def keep_new(self, url: str, kind: str) -> Entry:
        """`url` kept as a link of its own (Settings' Keep as a new link), named after its
        address, not played yet; the same stream's kept link takes the address instead."""
        entry = self.store.keep(url, "", kind)
        self._removed.discard(entry.id)
        self._changed()
        return entry

    def remove(self, key: str) -> None:
        """The link kept under `key` goes (its resume point stays with the watch state)."""
        if self.store.get(key) is None:
            return
        self.store.remove(key)
        self._handed.pop(key, None)
        self._removed.add(key)
        self._changed()

    def _changed(self) -> None:
        """The links changed: the lists follow, and the store is written on a worker."""
        self.changed.emit()
        self._save()
