"""The TV shell's link requests: the tune lane and the side lane (spec §6.9).

STB portals mint a link per request and providers limit connections, so requests are
serialised. The tune lane holds what is to play: at most one request is in flight, and a
choice made meanwhile replaces the single queued target. The side lane (the drawer's context
menu) asks one link at a time beside it, and a link request opens no stream. LinkLanes is the
only listener of the catalog's linkReady/linkFailed: every result goes to the lane whose
request id it answers.

A choice generation orders what the user chose. `supersede()` starts a new one: the shell
calls it for a tune or a movie, and a side request that will play (an external player) makes
its own and drops the queued tune. A side play whose generation is no longer the newest when
its link lands is dropped (one still waiting in line is never even asked for), and so is a
tune in flight that it overtook (`tuneDropped`); a copied link is never superseded.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import logging
from typing import Callable, Optional

from PySide6.QtCore import QObject, Signal

from services.navigation import episode_key
from services.redact import safe_traceback
from services.zapping import ZapContext

logger = logging.getLogger(__name__)


@dataclass
class Target:
    """Something to play once its link resolves."""

    item: dict
    section: str  # "live", "movies" or "series" (an episode)
    title: str
    group_key: str = ""
    source: str = ""  # the TV page's source when it was picked (a provider or ALL_PROVIDERS)
    subtitle: str = ""
    zapped: bool = False  # chosen by a zap step (↑/↓, digits, previous), not picked
    retried: bool = False  # its link was made again after a playback failure (STB tokens)
    context: Optional[dict] = None  # a movie's or episode's play context (see play_vod)
    start_over: bool = False  # from the start, not where it stopped
    local: bool = False  # a file opened from the disk: no provider's link, slot or history

    @property
    def live(self) -> bool:
        return self.section == "live"

    @property
    def item_type(self) -> str:
        """The watch history's kind ("channel", "episode", "movie"), play_vod's for VOD."""
        return {"live": "channel", "series": "episode"}.get(self.section, "movie")

    def same_as(self, other: Target) -> bool:
        if self.section != other.section or not ZapContext.same(self.item, other.item):
            return False
        # STB episodes share the season's cmd and id; the episode number tells them apart.
        return self.section != "series" or episode_key(self.item) == episode_key(other.item)


@dataclass
class _SideJob:
    target: Target
    then: Callable[[str], None] = field(repr=False)  # takes the link
    generation: Optional[int]  # the choice it plays for; None: a copy, never superseded


class LinkLanes(QObject):
    tuneReady = Signal(object, str)  # Target, link
    tuneFailed = Signal(object, str)  # Target, message
    tuneDropped = Signal(object)  # Target: overtaken by a side play, its result is dropped
    sideFailed = Signal(object, str)  # Target, message (scrub before showing)

    def __init__(self, catalog, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.catalog = catalog
        self.queued: Optional[Target] = None  # the newest choice while a request is in flight
        self._inflight: Optional[tuple[int, Target, int]] = None  # id, target, generation
        self._side: Optional[tuple[int, _SideJob]] = None
        self._side_queue: deque[_SideJob] = deque()
        self._generation = 0
        self._closed = False
        catalog.linkReady.connect(self._on_ready)
        catalog.linkFailed.connect(self._on_failed)

    # ---- choices ------------------------------------------------------------------------------

    def supersede(self) -> int:
        """A newer choice: side plays asked for before it no longer play."""
        self._generation += 1
        return self._generation

    # ---- the tune lane ------------------------------------------------------------------------

    def request(self, target: Target) -> None:
        """Ask for `target`'s link; while one is in flight it becomes the queued target."""
        if self._closed:
            return
        if self._inflight is not None:
            self.queued = target  # replaces any older queued target
            return
        self.queued = None  # the target sent now is the newest choice
        request_id = self.catalog.request_link(
            target.item, section=target.section, is_episode=target.section == "series"
        )
        self._inflight = (request_id, target, self._generation)

    def in_flight(self) -> Optional[Target]:
        return self._inflight[1] if self._inflight is not None else None

    def pending(self) -> Optional[Target]:
        """The newest choice whose link has not come: the queued one, else the one in flight."""
        return self.queued or self.in_flight()

    def busy(self) -> bool:
        return self._inflight is not None or self.queued is not None

    def take_queued(self) -> Optional[Target]:
        queued, self.queued = self.queued, None
        return queued

    def drop_tune(self) -> None:
        self._inflight = None
        self.queued = None

    # ---- the side lane ------------------------------------------------------------------------

    def request_side(self, target: Target, then: Callable[[str], None], *, play: bool) -> None:
        """Ask for `target`'s link after the side requests before it; `then` takes it.

        `play`: the link opens a player, a new choice that a newer one supersedes.
        """
        if self._closed:
            return
        generation = None
        if play:
            generation = self.supersede()
            self.queued = None  # an older choice: the tune in flight then lands stale
        self._side_queue.append(_SideJob(target, then, generation))
        self._next_side()

    def _next_side(self) -> None:
        if self._closed or self._side is not None:
            return
        job = self._next_current_job()
        if job is None:
            return
        section = job.target.section
        request_id = self.catalog.request_link(
            job.target.item, section=section, is_episode=section == "series"
        )
        self._side = (request_id, job)

    def _next_current_job(self) -> Optional[_SideJob]:
        """The next side job worth a request: superseded plays are dropped unasked."""
        while self._side_queue:
            job = self._side_queue.popleft()
            if not self._superseded(job):
                return job
            logger.debug("Not asking for the link of %s: a newer choice plays", job.target.title)
        return None

    def _superseded(self, job: _SideJob) -> bool:
        return job.generation is not None and job.generation != self._generation

    # ---- both ---------------------------------------------------------------------------------

    def drop(self) -> None:
        """Forget every pending request (a provider setup drops their results anyway)."""
        self.drop_tune()
        self._side = None
        self._side_queue.clear()

    def drop_provider(self, name: str, owner: Callable[[dict], str]) -> Optional[Target]:
        """Forget the requests of provider `name` (`owner` names an item's provider), whose
        setup dropped their results; other providers' requests stay, and so do those the
        catalog held for the setup (`holds`: they are answered now). Returns the tune in
        flight that was dropped, if it was `name`'s."""
        holds = getattr(self.catalog, "holds", None)

        def dropped_by_setup(request_id: int, item: dict) -> bool:
            return owner(item) == name and not (holds is not None and holds(request_id))

        dropped = None
        inflight = self._inflight
        if inflight is not None and dropped_by_setup(inflight[0], inflight[1].item):
            dropped = inflight[1]
            self._inflight = None
        side = self._side
        if side is not None and dropped_by_setup(side[0], side[1].target.item):
            self._side = None
        self._side_queue = deque(j for j in self._side_queue if owner(j.target.item) != name)
        self._next_side()
        return dropped

    def close(self) -> None:
        self._closed = True
        self.drop()

    def _take_side(self, request_id: int) -> Optional[_SideJob]:
        if self._closed or self._side is None or self._side[0] != request_id:
            return None
        job = self._side[1]
        self._side = None
        return job

    def _take_tune(self, request_id: int) -> Optional[Target]:
        """The target of the tune request `request_id`; None when it is not ours or stale."""
        inflight = self._inflight
        if self._closed or inflight is None or inflight[0] != request_id:
            logger.debug("Ignoring the result of link request %s", request_id)
            return None
        self._inflight = None
        _request_id, target, generation = inflight
        if generation != self._generation and self.queued is None:
            # Overtaken by a side play (a newer tune would be queued): it must not open.
            logger.debug("Dropping the link for %s: a newer choice plays", target.title)
            self.tuneDropped.emit(target)
            return None
        return target

    def _on_ready(self, request_id: int, url: str) -> None:
        job = self._take_side(request_id)
        if job is not None:
            self._finish_side(job, url)
            return
        target = self._take_tune(request_id)
        if target is not None:
            self.tuneReady.emit(target, url)

    def _on_failed(self, request_id: int, message: str) -> None:
        job = self._take_side(request_id)
        if job is not None:
            self.sideFailed.emit(job.target, message)
            self._next_side()
            return
        target = self._take_tune(request_id)
        if target is not None:
            self.tuneFailed.emit(target, message)

    def _finish_side(self, job: _SideJob, url: str) -> None:
        try:
            if self._superseded(job):
                logger.debug("Dropping the link for %s: a newer choice plays", job.target.title)
                return
            job.then(url)
        except Exception as e:
            # The message can carry the link: logs get the type and the frames only.
            logger.warning("Could not use the link of %s (%s)", job.target.title, type(e).__name__)
            logger.debug("%s", safe_traceback(e))
            self.sideFailed.emit(job.target, str(e))
        finally:
            self._next_side()
