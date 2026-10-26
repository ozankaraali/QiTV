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

The side lane also asks an STB portal for the link of a programme's recording in its archive
(catch-up, `request_archive`): its answer, or its failure, goes to its asker, which drops
one that comes too late itself; the lanes never supersede it.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import logging
from typing import Any, Callable, Optional

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
    # A live channel's provider archive from this moment (Unix seconds; catch-up, 2.2), to
    # `archive_until` (a past programme's end) when given; None: live.
    archive_at: Optional[float] = None
    archive_until: Optional[float] = None
    # 2.8: the player's quality menu: a media server's version or conversion (its Quality),
    # and where the title goes on from (ms) when its quality changed while it played.
    quality: Any = None
    start_ms: Optional[int] = None

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


def _quality(target: Target) -> dict:
    """The link request's quality argument: only when the quality menu chose one (2.8)."""
    return {"quality": target.quality} if target.quality is not None else {}


@dataclass
class _SideJob:
    target: Target
    then: Callable[[str], None] = field(repr=False)  # takes the link
    generation: Optional[int]  # the choice it plays for; None: a copy, never superseded
    programme: str = ""  # an STB archive's programme: the link asked is its recording's
    # Takes the failure's message (an archive link's asker); None: sideFailed says it.
    failed: Optional[Callable[[str], None]] = field(default=None, repr=False)


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
        """Ask for `target`'s link; while one is in flight it becomes the queued target. One
        the catalog holds for its provider's setup asks nothing of a portal yet: it is
        dropped (`forget`) and the newer choice is asked at once."""
        if self._closed:
            return
        inflight = self._inflight
        if inflight is not None:
            if not self._held(inflight[0]):
                self.queued = target  # replaces any older queued target
                return
            forget = getattr(self.catalog, "forget", None)
            if forget is not None:
                forget(inflight[0])
            self._inflight = None
        self.queued = None  # the target sent now is the newest choice
        request_id = self.catalog.request_link(
            target.item,
            section=target.section,
            is_episode=target.section == "series",
            **_quality(target),
        )
        self._inflight = (request_id, target, self._generation)

    def _held(self, request_id: int) -> bool:
        """Whether the catalog holds the request for its provider's setup (see `holds`)."""
        holds = getattr(self.catalog, "holds", None)
        return holds is not None and bool(holds(request_id))

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
        elif self._side_pending(target):
            return  # asked again for the very same target (the PiP after a setup): one is enough
        self._side_queue.append(_SideJob(target, then, generation))
        self._next_side()

    def request_archive(
        self,
        target: Target,
        programme: str,
        then: Callable[[str], None],
        failed: Callable[[str], None],
    ) -> None:
        """Ask, after the side requests before it, for the link of programme `programme`'s
        recording in the archive of `target`'s STB portal (catch-up); `then` takes the link,
        `failed` the failure's message (also when its provider's setup fails meanwhile:
        "")."""
        if self._closed:
            return
        self._side_queue.append(_SideJob(target, then, None, programme, failed))
        self._next_side()

    def _side_pending(self, target: Target) -> bool:
        jobs = [self._side[1]] if self._side is not None else []
        return any(job.target is target for job in [*jobs, *self._side_queue])

    def _next_side(self) -> None:
        if self._closed or self._side is not None:
            return
        job = self._next_current_job()
        if job is None:
            return
        section = job.target.section
        if job.programme:
            request_id = self.catalog.request_archive_link(job.target.item, job.programme)
        else:
            request_id = self.catalog.request_link(
                job.target.item,
                section=section,
                is_episode=section == "series",
                **_quality(job.target),
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

    def provider_ready(self, name: str, owner: Callable[[dict], str]) -> Optional[Target]:
        """Provider `name` was set up (`owner` names an item's provider): what its catalog
        was asked before the setup gets no answer. The tune in flight is forgotten and
        returned (the shell asks again for the newest choice); the side request in flight is
        asked again before those waiting behind it, which all stay, in order. Requests the
        catalog held for the setup (`holds`) are answered now and stay; other providers'
        requests are not touched."""

        def dropped_by_setup(request_id: int, item: dict) -> bool:
            return owner(item) == name and not self._held(request_id)

        dropped = None
        inflight = self._inflight
        if inflight is not None and dropped_by_setup(inflight[0], inflight[1].item):
            dropped = inflight[1]
            self._inflight = None
        side = self._side
        if side is not None and dropped_by_setup(side[0], side[1].target.item):
            self._side = None
            self._side_queue.appendleft(side[1])
        self._next_side()
        return dropped

    def provider_failed(
        self, name: str, owner: Callable[[dict], str]
    ) -> tuple[Optional[Target], bool]:
        """Provider `name`'s setup failed: none of its requests is answered, and asking for
        those waiting in line would sign it in again. All of them are forgotten; other
        providers' requests go on. Returns the tune in flight that was dropped (if `name`'s)
        and whether side requests were (a menu's links, the PiP's), so the shell says so."""
        dropped = None
        inflight = self._inflight
        if inflight is not None and owner(inflight[1].item) == name:
            dropped = inflight[1]
            self._inflight = None
        lost = False
        dropped_jobs: list[_SideJob] = []
        side = self._side
        if side is not None and owner(side[1].target.item) == name:
            self._side = None
            dropped_jobs.append(side[1])
        kept: deque[_SideJob] = deque()
        for job in self._side_queue:
            if owner(job.target.item) != name:
                kept.append(job)
            else:
                dropped_jobs.append(job)
        self._side_queue = kept
        for job in dropped_jobs:
            if job.failed is not None:
                self._tell_failed(job, "")  # its asker says it
            elif not self._superseded(job):
                lost = True
        self._next_side()
        return dropped, lost

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
            try:
                if job.failed is not None:
                    self._tell_failed(job, message)
                else:
                    self.sideFailed.emit(job.target, message)
            finally:
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
            if job.failed is not None:
                self._tell_failed(job, "")
            else:
                self.sideFailed.emit(job.target, str(e))
        finally:
            self._next_side()

    def _tell_failed(self, job: _SideJob, message: str) -> None:
        """An archive link's failure, to its asker (whose own failure is logged here)."""
        if job.failed is None:
            return
        try:
            job.failed(message)
        except Exception as e:
            logger.warning(
                "Could not report the archive link of %s (%s)", job.target.title, type(e).__name__
            )
            logger.debug("%s", safe_traceback(e))
