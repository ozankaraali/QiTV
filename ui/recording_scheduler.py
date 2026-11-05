"""Scheduled recordings (2.8): the programmes the schedule says are due are recorded with no
player, by the time-shift helper.

Every TICK_MS the scheduler asks the schedule what is due (`services.recording_schedule`). A due
job takes one of its provider's streams without waiting for one, then asks for the channel's
link (an STB portal mints one per request: none is asked without a stream), opens the channel's
stream in the helper (in memory: nothing on screen, nothing heard) and records it from the first
segment the helper has, until its window ends. At its end the session closes and the stream
goes back once it is closed; the outcome is noted (the Library names the file by its programme)
and said in a toast.

A try that fails before it records (no stream free, no link, the stream failing) is tried again
every RETRY_S while the programme airs; it is said missed or failed only once its window ended.
No stream free is said at once too, the first time (the user may free one: what they watch, a
phone's, an outside player's hold one). A one-time programme whose window passed with no try
while QiTV ran (the computer slept) is said missed then, not at the next start.
A recording the stream or the helper cuts short is kept, and the rest goes to another file. The
next programme on a channel that records hands over: it needs no stream of its own; half a
minute into it the earlier file ends, and the next starts from the helper's buffer a minute
before its start. One that ends within a recording on its channel (a programme inside an
evening's timer, the same programme scheduled twice) is that recording's: it is noted in its
file once it ends (the file keeps the longer one's name), or is due again on its own when that
recording stops before it ended. Due together, the longest begins first. A one-time recording
made longer while it records goes on to its new end: the scheduler stops every recording at
its window's end (the helper's own limit is only a day's). What records shows on the REC sign
(`recording_now`), which stops it (`stop_now`); its stream's budget purpose is that name, so the
question before a recording is stopped for a stream counts it (`ui.stream_question`).

QiTV keeps the computer awake while it runs; a sleeping computer or a closed lid misses them.

Recording what plays (2.9.7): a programme due on a channel a player shows from its buffer (the
big picture, the small one, a tile, a player recording in the background) records on that
player, as R does, from the programme's start as far back as its buffer goes: no stream of its
own, so a provider that allows one stream records the channel watched. The player says REC and
counts as a recording (the stream question, Quit); leaving its channel sets it aside to record
on, and the scheduler stops it at its window's end. Its end is the player's to say. One that
ends early while it airs (its stream failed) is tried again from where it ended.

Watching what records (2.9.7): a player that tunes a channel recording on schedule (the small
picture, the big one, the REC sign's Go back to, the Recordings page's Watch) borrows the
recording's session (`lend`, `services.live.live_session.Borrowed`): it plays that buffer, and
the provider sees no second stream. A recording that ends while it is watched keeps its session
(and its stream's budget slot, under the viewer's name) open for the viewer, until it is given
back; the next programme recording on that channel meanwhile records on it.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
import logging
import math
import time
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from services import recording, recording_schedule as rs
from services.catalog import EMPTY_LINK_MESSAGE, NO_STREAM_MESSAGE, PROVIDER_LOADING_MESSAGE
from services.connection_budget import DENIED
from services.i18n import N_, translate
from services.live.live_session import Borrowed
from services.live.recorder import (
    ENDED,
    FAILED,
    LIMIT,
    MAX_SECONDS,
    MIN_FREE_BYTES,
    SPACE,
    STOPPED,
)
from services.live.refusals import STREAM_LIMIT, STREAM_REFUSED
from services.provider_api import known_http_only, portal_http_only
from services.vlc_profile import user_agent_for

logger = logging.getLogger(__name__)

TICK_MS = 20_000
RETRY_S = 60.0  # a try that failed is made again after this
HANDOVER_S = 30.0  # into the next programme on the channel, the earlier file ends
MIN_LEFT_S = 60.0  # less of its window left: a recording cut short is not tried again
RAM_BYTES = 192 * 1024 * 1024  # the helper's buffer: a handover reaches 90 s back (15 Mbit/s)
SCHEDULED_PREFIX = "sched:"  # the REC sign's name of a scheduled recording
# i18n: a toast: a scheduled recording began ({title}: the programme, {channel}: its channel)
STARTED_TEXT = N_("● Recording “{title}” on {channel}")
# i18n: a toast: a scheduled recording could not start: the provider allows no more streams now
NO_STREAM_TEXT = N_("“{title}” was not recorded: no stream of {channel}'s provider was free.")
# i18n: a toast: a scheduled recording could not start or stopped early ({why}: the reason)
FAILED_TEXT = N_("“{title}” could not be recorded ({why}).")
# i18n: a toast at start: scheduled recordings whose time passed while QiTV was closed
MISSED_TEXT = N_("Missed while QiTV was closed: {titles}")
# i18n: a toast: scheduled recordings whose time passed while QiTV ran but the computer slept
MISSED_ASLEEP_TEXT = N_("Missed while the computer was asleep: {titles}")
# {title}: the programme, {channel}: its channel; it is tried again while it airs.
# i18n: a toast: a scheduled recording is due, but its provider's streams are all in use
NO_STREAM_YET_TEXT = N_(
    "“{title}” can't start recording: no stream of {channel}'s provider is free. "
    "QiTV tries again every minute while it airs."
)
# i18n: a toast: QiTV's time-shift helper is not running, so a scheduled recording can't start
NO_HELPER = N_("the time-shift helper is not running")

LOST = "lost"  # a recording's end when the helper went (services.live.engine)

# Why a scheduled recording failed, as FAILED_TEXT's {why} says it: by the code the scheduler,
# the helper (a session's failure kind, the recorder's end) or the catalog gives.
# i18n: why a scheduled recording failed, in "“{title}” could not be recorded ({why})."
_HELPER_STOPPED = N_("the time-shift helper stopped")
# i18n: why a scheduled recording failed, in "“{title}” could not be recorded ({why})."
_UNREACHABLE = N_("the channel could not be reached")
# i18n: why a scheduled recording failed, in "“{title}” could not be recorded ({why})."
_STREAM_FAILED = N_("the channel's stream failed")
FAILURE_REASONS = {
    # i18n: why a scheduled recording failed: its provider was removed from the list
    "provider": N_("its provider is no longer in your list"),
    # i18n: why a scheduled recording failed: the provider gave no address for the channel
    "link": N_("the provider gave no address for the channel"),
    # i18n: why a scheduled recording failed: the channel opened, but no picture came
    "no picture": N_("no picture came from the channel"),
    # i18n: why a scheduled recording failed: nothing arrived to be written to the file
    "empty": N_("nothing came to record"),
    # i18n: why a scheduled recording failed: no stream of its provider was free
    "stream": N_("no stream of its provider was free"),
    # i18n: why a scheduled recording failed: the channel can't be recorded by QiTV's buffer
    "unsupported": N_("the channel can't be recorded"),
    # i18n: why a scheduled recording failed: the channel's certificate was not trusted
    "tls": N_("the channel's certificate could not be verified"),
    # i18n: why a scheduled recording failed: the channel's server did not answer in time
    "timeout": N_("the channel did not answer in time"),
    # i18n: why a scheduled recording failed: the channel's stream ended
    "ended": N_("the channel's stream ended"),
    "network": _UNREACHABLE,
    "http": _UNREACHABLE,
    "helper": _HELPER_STOPPED,
    "crashed": _HELPER_STOPPED,
    "internal": _HELPER_STOPPED,
    "unavailable": _HELPER_STOPPED,
    "failed": _STREAM_FAILED,
}
# What else reaches {why} and says it well: the catalog's link failures and the provider's
# refusals (N_ texts), and the helper not running (said translated already).
_REASON_TEXTS = (
    PROVIDER_LOADING_MESSAGE,
    NO_STREAM_MESSAGE,
    EMPTY_LINK_MESSAGE,
    STREAM_LIMIT,
    STREAM_REFUSED,
)


def failure_reason(why: str) -> str:
    """A scheduled recording's failure, translated, as FAILED_TEXT's {why}: a code's short
    reason; a catalog's or a refusal's own text; anything else (a helper's English message,
    an exception's text) as the channel's stream failing: it is in the log already."""
    reason = FAILURE_REASONS.get(why)
    if reason is not None:
        return translate(reason)
    if why in _REASON_TEXTS:
        return translate(why)
    if why == translate(NO_HELPER):
        return why
    return translate(_STREAM_FAILED)


@dataclass
class _Run:
    job: rs.Job
    link_id: int = 0
    ticket: int = DENIED
    sid: str = ""
    rid: str = ""
    path: str = ""
    since: float = 0.0  # when it began recording (time.monotonic, as the REC sign counts)
    stopping: bool = False
    ended: bool = False
    state: str = ""  # its outcome, once noted
    cut: str = ""  # why the stream or the helper cut it short
    user_stopped: bool = False  # the REC sign's Stop, a cancel, QiTV closing: not tried again
    retry: Optional[tuple[str, str]] = None  # (state, why): tried again once its session closed
    waiting_for: Optional[_Run] = None  # the earlier programme on its channel, to hand over
    handing: Optional[_Run] = None  # the next programme it hands its session to
    covers: list[rs.Job] = field(default_factory=list)  # the jobs within it, in its file
    serving: bool = False  # 2.9.7: its recording ended; its session stays for a viewer
    host: Any = None  # 2.9.7: the player's LiveSession it records on (no session of its own)
    host_name: str = ""  # ... that player's name ("main", "pip", "b1" …)


@dataclass
class _Try:
    job: rs.Job
    at: float  # tried again from then
    state: str  # what it is if its window ends first (missed: no stream; failed: why)
    why: str


class RecordingScheduler(QObject):
    changed = Signal()  # the schedule or what records changed (the Library lists them)

    def __init__(
        self,
        *,
        schedule: rs.Schedule,
        engine: Any,
        catalogs: Any,
        budget: Any,
        config: Any,
        toast: Callable[..., None],
        clock: Callable[[], float] = time.time,
        parent: Optional[QObject] = None,
        index: Any = None,
    ) -> None:
        """`index` (2.9.7, services.recordings_index): each recording's programme, channel and
        logo are noted there as it starts."""
        super().__init__(parent)
        self.schedule = schedule
        self._index = index
        self._engine: Any = None
        self._catalogs = catalogs
        self._budget = budget
        self._config = config
        self._toast = toast
        self._clock = clock
        self._runs: list[_Run] = []
        self._tries: dict[str, _Try] = {}
        # 2.9.7: the budget purposes of the players watching a session (`lend`), one each
        self._lent: dict[str, list[str]] = {}
        self._said_busy: set[str] = set()  # jobs told once that no stream was free
        self._cut_at: dict[str, float] = {}  # 2.9.7: when a job's recording on a player broke
        # 2.9.7: the players showing a channel: (name, provider, channel, LiveSession); the
        # window's (none: every recording takes a stream of its own).
        self.players: Callable[[], list[tuple[str, str, dict, Any]]] = list
        self._closed = False
        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self.tick)
        catalogs.linkReady.connect(self._on_link)
        catalogs.linkFailed.connect(self._on_link_failed)
        self.use_engine(engine)

    def use_engine(self, engine: Any) -> None:
        """The time-shift helper's engine to record with (a new one after the helper was
        replaced: `engine_gone` first)."""
        old = self._engine
        if old is not None:
            for signal, slot in self._wiring(old):
                try:
                    signal.disconnect(slot)
                except RuntimeError, TypeError:
                    pass
        self._engine = engine
        for signal, slot in self._wiring(engine):
            signal.connect(slot)

    def _wiring(self, engine: Any) -> list[tuple[Any, Callable]]:
        return [
            (engine.sessionReady, self._on_ready),
            (engine.sessionFailed, self._on_failed),
            (engine.sessionEnded, self._on_ended),
            (engine.sessionClosed, self._on_closed),
            (engine.recordingEnded, self._on_recorded),
        ]

    def engine_gone(self) -> None:
        """The helper was shut down: its sessions went with it and no end follows. Their
        streams go back; what they recorded is kept, and the rest is tried again (with the next
        engine, `use_engine`)."""
        now = self._clock()
        for run in list(self._runs):
            if run.rid and not run.ended and not run.state:
                self.schedule.noted(run.job, rs.FAILED, path=run.path, why="helper")
                self.schedule.unstarted(run.job)
                self._cut_at[run.job.key] = now  # 2.9.7: on a player, the rest from here
            if not run.state and not run.user_stopped:
                self._tries[run.job.key] = _Try(run.job, now, rs.FAILED, translate(NO_HELPER))
            run.sid = ""
            self._release(run)
        self._lent.clear()  # the players' sessions went with the helper too
        self.changed.emit()

    # ---- the clock --------------------------------------------------------------------------

    def start(self) -> None:
        """QiTV started: what was missed meanwhile is said, then the schedule is followed."""
        missed = self.schedule.missed_before(self._clock())
        if missed:
            titles = ", ".join(str(m.get("title") or "") for m in missed)
            self._toast(translate(MISSED_TEXT, titles=titles), "error")
        self._timer.start()
        self.tick()

    def tick(self) -> None:
        if self._closed:
            return
        now = self._clock()
        for run in self._runs:  # a one-time recording made longer goes on to its new end
            item = self.schedule.item(run.job.item_id)
            if item is not None and item.get("kind") == rs.ONCE:
                stop = float(item.get("stop") or 0)
                if stop > run.job.stop and not run.stopping:
                    run.job = replace(run.job, stop=stop)
        for run in list(self._runs):  # a window that ended stops its recording
            if not run.stopping and now >= rs.window(run.job)[1]:
                self._stop(run)
        for run in list(self._runs):
            if run.waiting_for is not None:
                self._hand_over_when_due(run, now)
        for key, attempt in list(self._tries.items()):
            if now >= rs.window(attempt.job)[1]:
                del self._tries[key]
                self._cut_at.pop(key, None)
                self._said_busy.discard(key)
                self._said(attempt.job, attempt.state, attempt.why)
        self._missed_meanwhile(now)
        busy = {job.key for run in self._runs for job in (run.job, *run.covers)}
        due = self.schedule.due(now, self._guide)
        for job in sorted(due, key=lambda job: (job.start, -job.stop)):  # the longest first
            if job.key in busy:
                continue
            earlier = self._tries.get(job.key)
            if earlier is not None and now < earlier.at:
                continue
            busy.add(job.key)
            holder = self._holder(job)
            if holder is not None:  # within a recording on its channel: in that file
                holder.covers.append(job)
                self.schedule.started(job)
                self._tries.pop(job.key, None)
                continue
            self._begin(job)

    def _missed_meanwhile(self, now: float) -> None:
        """One-time programmes whose window passed while QiTV ran without a try (the computer
        slept through it): said missed now, not as closed at the next start."""
        busy = {job.key for run in self._runs for job in (run.job, *run.covers)}
        missed = self.schedule.missed_before(now, why="asleep", busy=busy | set(self._tries))
        if missed:
            titles = ", ".join(str(m.get("title") or "") for m in missed)
            self._toast(translate(MISSED_ASLEEP_TEXT, titles=titles), "error")

    def _holder(self, job: rs.Job) -> Optional[_Run]:
        """A recording (or one about to) on `job`'s channel that goes on at least as long."""
        for run in self._runs:
            if (
                not run.stopping
                and run.handing is None
                and run.job.provider == job.provider
                and rs.same_channel(run.job.channel, job.channel)
                and rs.window(job)[1] <= rs.window(run.job)[1]
            ):
                return run
        return None

    def _settle(self, run: _Run, written: int, path: str, why: str = "") -> None:
        """`run`'s recording ended (or never began): the jobs within it whose window ended are
        noted in its file (before it, so the file keeps its name); the others are due again,
        on their own."""
        now = self._clock()
        covers, run.covers = run.covers, []
        for job in covers:
            if now < rs.window(job)[1]:
                self.schedule.unstarted(job)
            elif written > 0:
                self.schedule.finished(job, rs.RECORDED, path=path)
            else:
                self.schedule.finished(job, rs.FAILED, why=why or "empty")

    def _guide(self, channel: dict, since: float) -> list:
        try:
            moment = datetime.fromtimestamp(since).astimezone()
            return list(self._catalogs.programmes(channel, moment, 48))
        except Exception as e:  # a guide that fails records nothing of a series now
            logger.debug("A series' guide could not be read: %s", type(e).__name__)
            return []

    def running(self) -> list[tuple[str, str]]:
        """The scheduled recordings recording now: (programme, channel)."""
        return [
            (run.job.title, str(run.job.channel.get("name") or ""))
            for run in self._runs
            if run.rid and not run.ended
        ]

    def recording_now(self) -> list[tuple[str, str, float, dict]]:
        """What records now, for the REC sign: (its name for `stop_now`, the channel's name,
        since when (time.monotonic), the channel)."""
        return [
            (
                _purpose(run.job),
                str(run.job.channel.get("name") or ""),
                run.since,
                dict(run.job.channel),
            )
            for run in self._runs
            if run.rid and not run.ended and not run.stopping and run.host is None
        ]

    def hosted_players(self) -> set[str]:
        """2.9.7: the players a scheduled recording records on now (the Recordings page lists
        those as the programme, not twice)."""
        return {
            run.host_name
            for run in self._runs
            if run.host is not None and run.rid and not run.ended
        }

    def recording_details(self) -> list[tuple[str, str, str, float, float, str]]:
        """2.9.7: what records now, for the Recordings page: (its name for `stop_now`, the
        programme, the channel's name, since when (time.monotonic), its window's end (unix),
        its schedule item's id)."""
        return [
            (
                _purpose(run.job),
                run.job.title,
                str(run.job.channel.get("name") or ""),
                run.since,
                rs.window(run.job)[1],
                run.job.item_id,
            )
            for run in self._runs
            if run.rid and not run.ended and not run.stopping
        ]

    def stop_now(self, name: str) -> None:
        """The REC sign's Stop recording: this airing's recording stops and is kept (a series
        records its next airing)."""
        for run in list(self._runs):
            if _purpose(run.job) == name and not run.stopping:
                run.user_stopped = True
                self._stop(run)

    def providers(self) -> set[str]:
        """The providers the schedule records from (their guides stay loaded for series)."""
        return self.schedule.providers()

    def rename_providers(self, renames: Any) -> None:
        """A renamed provider (`services.provider_rename.Renames`): the schedule records from
        its new name, and so do the recordings under way and those to try again."""
        self.schedule.rename_providers(renames)

        def moved(job: rs.Job) -> rs.Job:
            if job.provider not in renames.names:
                return job
            return replace(
                job, provider=renames.of(job.provider), channel=renames.item(job.channel)
            )

        for run in self._runs:
            run.job = moved(run.job)
            run.covers = [moved(job) for job in run.covers]
        for attempt in self._tries.values():
            attempt.job = moved(attempt.job)

    def waiting(self) -> list[tuple[rs.Job, str]]:
        """2.9.7: the scheduled recordings due now that wait to be tried again, and why the
        last try failed ("stream": no stream was free), the soonest to end first."""
        trying = {run.job.key for run in self._runs}  # tried again now: not waiting
        tries = [t for key, t in self._tries.items() if key not in trying]
        tries.sort(key=lambda attempt: rs.window(attempt.job)[1])
        return [(attempt.job, attempt.why) for attempt in tries]

    def try_now(self, key: str) -> None:
        """2.9.7: the Recordings page's Try now: the job `key` waits no more."""
        attempt = self._tries.get(key)
        if attempt is not None and not self._closed:
            attempt.at = self._clock()
            self.tick()
            self.changed.emit()

    def skip(self, key: str) -> None:
        """2.9.7: the Recordings page's Skip: the job `key` is not recorded and waits no more
        (nothing is noted: it was the user's choice). A one-time one leaves the schedule; a
        timer or a series records its next airing."""
        attempt = self._tries.pop(key, None)
        if attempt is None:
            return
        self._said_busy.discard(key)
        job = attempt.job
        item = self.schedule.item(job.item_id)
        if item is not None and item.get("kind") == rs.ONCE:
            self.schedule.cancel(job.item_id)
        else:
            self.schedule.started(job)  # this airing is never due again
        self.changed.emit()

    def edit(self, item_id: str, **fields: Any) -> Optional[dict]:
        """2.9.7: the Recordings page's Edit (`Schedule.edit`'s fields): a try under way of
        the item is dropped (it is due at its new time); what records now is never edited (the
        page offers no Edit for it)."""
        item = self.schedule.edit(item_id, **fields)
        if item is None:
            return None
        for key, attempt in list(self._tries.items()):
            if attempt.job.item_id == item_id:
                del self._tries[key]
                self._said_busy.discard(key)
        self.changed.emit()
        self.tick()  # one on now starts at once
        return item

    # ---- 2.9.7: watching what records ----------------------------------------------------------

    def lendable(self, channel: dict, provider: str) -> bool:
        """A recording (or a session kept for a viewer) reads `channel` of `provider` now: a
        player can borrow it (`lend`) instead of opening a stream of its own."""
        return self._lendable_run(channel, provider) is not None

    def lend(self, channel: dict, provider: str, purpose: str) -> Optional[Borrowed]:
        """The session that reads `channel` of `provider`, for the player whose budget
        purpose is `purpose` to watch; None when none does. It stays open until given back."""
        run = self._lendable_run(channel, provider)
        if run is None or self._closed:
            return None
        sid = run.sid
        self._lent.setdefault(sid, []).append(purpose)
        if run.serving and run.ticket != DENIED:
            self._budget.relabel(run.ticket, purpose)  # its stream is the viewer's alone now
        given = [False]

        def give_back() -> None:
            if not given[0]:
                given[0] = True
                self._give_back(sid, purpose)

        return Borrowed(sid, give_back, recording=lambda: self._records_on(sid))

    def _lendable_run(self, channel: dict, provider: str) -> Optional[_Run]:
        for run in self._runs:
            recording = bool(run.rid) and not run.ended and not run.stopping
            if (
                run.sid
                and (recording or run.serving)
                and run.job.provider == provider
                and _same_channel(run.job.channel, channel)
            ):
                return run
        return None

    def _records_on(self, sid: str) -> bool:
        return any(run.sid == sid and run.rid and not run.ended for run in self._runs)

    def _give_back(self, sid: str, purpose: str) -> None:
        """The player of budget purpose `purpose` let go of `sid`: a session kept for its
        viewers goes on under the name of one still watching, and closes once none is (its
        stream goes back with its close; nothing borrows or records on it meanwhile)."""
        left = self._lent.get(sid, [])
        if purpose in left:
            left.remove(purpose)
        if left:
            for run in self._runs:
                if run.serving and run.sid == sid and run.ticket != DENIED:
                    self._budget.relabel(run.ticket, left[-1])
            return
        self._lent.pop(sid, None)
        for run in self._runs:
            if run.serving and run.sid == sid:
                run.serving = False  # closing: neither lent nor recorded on again
                self._engine.close_session(sid)  # `_on_closed` releases it
                return

    def _serve(self, run: _Run) -> None:
        """`run`'s recording ended while a player watches its session: it stays open for
        them, its stream under their name."""
        run.serving = run.stopping = True
        watching = self._lent.get(run.sid)
        if watching and run.ticket != DENIED:
            self._budget.relabel(run.ticket, watching[-1])
        self.changed.emit()

    def cancel(self, item_id: str) -> None:
        """A scheduled recording is no longer wanted: off the schedule, and stopped (what it
        recorded is kept) when it records now."""
        self.schedule.cancel(item_id)
        for key, attempt in list(self._tries.items()):
            if attempt.job.item_id == item_id:
                del self._tries[key]
        for run in self._runs:  # one held by another's recording is noted no more
            run.covers = [job for job in run.covers if job.item_id != item_id]
        for run in list(self._runs):
            if run.job.item_id == item_id and not run.stopping:
                run.user_stopped = True
                self._stop(run)
        self.changed.emit()

    def shutdown(self) -> None:
        """QiTV closes: the recordings stop and keep what they have. One recording now is
        noted at once (its end may come after QiTV is gone: the Library still names it), and
        stays to record the rest should QiTV open again while it airs."""
        self._timer.stop()
        for run in list(self._runs):
            if run.serving and run.sid:
                self._engine.close_session(run.sid)  # 2.9.7: kept for a viewer: it goes too
                continue
            run.user_stopped = True
            recording = bool(run.rid and run.path and not run.ended)
            self._settle(run, 1 if recording else 0, run.path)  # the rest of each, on reopening
            if not run.stopping:
                self._stop(run)
            if recording and not run.ended:  # its end did not come at once
                run.ended = True
                self.schedule.noted(run.job, rs.RECORDED, path=run.path)
                self.schedule.unstarted(run.job)
        self._closed = True

    # ---- one recording ----------------------------------------------------------------------

    def _begin(self, job: rs.Job) -> None:
        run = _Run(job)
        served = next(
            (
                r
                for r in self._runs
                if r.serving
                and r.sid
                and r.job.provider == job.provider
                and rs.same_channel(r.job.channel, job.channel)
            ),
            None,
        )
        if served is not None:  # 2.9.7: a session kept for a viewer: it records on that
            run.sid, run.ticket = served.sid, served.ticket
            if run.ticket != DENIED:
                self._budget.relabel(run.ticket, _purpose(job))
            served.sid, served.ticket = "", DENIED
            self._runs.remove(served)
            self._runs.append(run)
            self._record(run, rs.window(job)[0])
            self.changed.emit()
            return
        same = next(
            (
                r
                for r in self._runs
                if (r.sid or r.host is not None)
                and not r.stopping
                and r.handing is None
                and r.waiting_for is None
                and r.job.provider == job.provider
                and rs.same_channel(r.job.channel, job.channel)
            ),
            None,
        )
        self._runs.append(run)
        if same is not None:  # the next programme on a channel that records: no new stream
            run.waiting_for = same
        else:
            self._attempt(run)
        self.changed.emit()

    def _attempt(self, run: _Run) -> None:
        """A player showing the channel records it (2.9.7); else a stream of the provider
        first (none free: tried again later), then the link."""
        if self._record_on_player(run):
            return
        job = run.job
        try:
            entry = self._catalogs.provider(job.provider)
        except KeyError:
            self._retry(run, rs.FAILED, "provider")
            return
        limit = self._budget.limit_for(entry, self._catalogs.max_connections(job.provider))
        run.ticket = self._budget.request(job.provider, limit, purpose=_purpose(job), queue=False)
        if run.ticket == DENIED:
            if job.key not in self._said_busy:  # said once, at once: it may be freed
                self._said_busy.add(job.key)
                name = str(job.channel.get("name") or "")
                self._toast(translate(NO_STREAM_YET_TEXT, title=job.title, channel=name), "error")
            self._retry(run, rs.MISSED, "stream")
            return
        # The provider's account back in the channel's address (the file keeps it out).
        channel = rs.with_account(job.channel, entry)
        run.link_id = self._catalogs.request_link(channel, section="live")

    def _player_for(self, job: rs.Job) -> Optional[tuple[str, Any]]:
        """2.9.7: a player that shows `job`'s channel from its buffer and records nothing:
        (its name, its LiveSession); None when none does."""
        try:
            players = list(self.players())
        except Exception as e:  # a player being made or closed: a stream of its own then
            logger.debug("The players could not be read: %s", type(e).__name__)
            return None
        for name, provider, channel, live in players:
            if provider != job.provider or not channel:
                continue
            if not _same_channel(job.channel, channel):
                continue
            if live.recordable() and not live.recording():
                return name, live
        return None

    def _record_on_player(self, run: _Run) -> bool:
        """2.9.7: `run` records on a player that shows its channel (the module docstring):
        False when none does, or it could not start there."""
        found = self._player_for(run.job)
        if found is None:
            return False
        host_name, live = found
        job = run.job
        name = str(job.channel.get("name") or job.title)
        path = recording.path_for(self._config, name, datetime.now().astimezone())
        since = max(rs.window(job)[0], self._cut_at.get(job.key, -math.inf))
        if not live.start_recording(path, since=since):
            return False
        rid = live.recording_id()
        if not rid:
            return False
        run.host, run.host_name, run.rid, run.path = live, host_name, rid, path
        self._note(run)
        self.schedule.started(job)
        self._tries.pop(job.key, None)
        self._said_busy.discard(job.key)
        run.since = time.monotonic()
        logger.info(
            "A scheduled recording records on the %s player, which shows its channel: no"
            " stream of its own.",
            host_name,
        )
        self._toast(translate(STARTED_TEXT, title=job.title, channel=name), "info")
        self.changed.emit()
        return True

    def _hand_over_when_due(self, run: _Run, now: float) -> None:
        earlier = run.waiting_for
        if earlier is None:
            return
        if earlier not in self._runs or earlier.ended or not (earlier.sid or earlier.host):
            run.waiting_for = None  # the earlier one ended meanwhile: this one starts alone
            self._attempt(run)
            return
        if earlier.handing is not run and (
            now >= run.job.start + HANDOVER_S or now >= rs.window(earlier.job)[1]
        ):
            earlier.handing = run
            if not earlier.stopping:
                self._stop(earlier)

    def _retry(self, run: _Run, state: str, why: str) -> None:
        """A try failed before it recorded: its stream goes back; tried again in RETRY_S
        while it airs (said only if its window ends first)."""
        if not run.user_stopped:
            self._tries[run.job.key] = _Try(run.job, self._clock() + RETRY_S, state, why)
        self._settle(run, 0, "", why)
        self._release(run)

    def _run_by(self, **wanted: Any) -> Optional[_Run]:
        for run in self._runs:
            if all(getattr(run, key) == value for key, value in wanted.items()):
                return run
        return None

    def _on_link_failed(self, link_id: int, message: str) -> None:
        run = self._run_by(link_id=link_id)
        if run is not None and not run.sid and not run.stopping:
            self._retry(run, rs.FAILED, message or "link")

    def _on_link(self, link_id: int, url: str) -> None:
        run = self._run_by(link_id=link_id)
        if run is None or run.sid or run.stopping:
            return
        job = run.job
        try:
            entry = self._catalogs.provider(job.provider)
        except KeyError:
            self._retry(run, rs.FAILED, "provider")
            return
        if not self._engine.is_available():
            self._engine.start()
            self._retry(run, rs.FAILED, translate(NO_HELPER))
            return
        channel = job.channel
        headers = {"User-Agent": user_agent_for(channel, str(entry.get("type") or ""))}
        if channel.get("referrer"):
            headers["Referer"] = str(channel["referrer"])
        xtream = str(entry.get("type") or "").upper() == "XTREAM"
        plain = xtream or portal_http_only(entry)  # its sign-in found no HTTPS (2.8.1)
        https = url.startswith("http://") and not known_http_only(url) and not plain
        run.sid = self._engine.open_session(
            url,
            headers=headers,
            verify_ssl=bool(entry.get("ssl_verify", getattr(self._config, "ssl_verify", True))),
            disk=False,
            max_bytes=RAM_BYTES,
            https_first=https,
        )

    def _on_ready(self, sid: str) -> None:
        run = self._run_by(sid=sid)
        if run is None or run.rid or run.stopping:
            return
        self._record(run, None)

    def _record(self, run: _Run, since: Optional[float]) -> None:
        """Records on `run`'s session: from the first segment the helper has, or (a handover)
        from the one at `since` (unix seconds), as far back as the helper's buffer goes."""
        spans = self._engine.spans(run.sid)
        seq = spans[0].seq if spans else 0
        now = self._clock()
        if since is not None and spans:
            target = spans[-1].end - max(0.0, now - since)
            seq = next((s.seq for s in reversed(spans) if s.start <= target), spans[0].seq)
        name = str(run.job.channel.get("name") or run.job.title)
        run.path = recording.path_for(self._config, name, datetime.now().astimezone())
        # Stopped at its window's end by `tick` (which may move, made longer): the helper's
        # own limit is only the day's.
        run.rid = self._engine.start_recording(
            run.sid, run.path, seq, max_s=MAX_SECONDS, min_free=MIN_FREE_BYTES
        )
        if not run.rid:
            run.retry = (rs.FAILED, "helper")
            self._engine.close_session(run.sid)
            return
        self.schedule.started(run.job)
        self._tries.pop(run.job.key, None)
        self._said_busy.discard(run.job.key)
        run.since = time.monotonic()
        self._note(run)
        self._toast(translate(STARTED_TEXT, title=run.job.title, channel=name), "info")
        self.changed.emit()

    def _note(self, run: _Run) -> None:
        """2.9.7: what `run`'s file holds, in the recordings index."""
        if self._index is None or not run.path:
            return
        job, channel = run.job, run.job.channel
        self._index.note(
            run.path,
            title=job.title,
            channel=str(channel.get("name") or ""),
            provider=job.provider,
            channel_id=str(channel.get("id") or ""),
            logo=str(channel.get("logo") or ""),
            start=job.start,
            stop=job.stop,
        )

    def _on_recorded(self, rid: str, reason: str, path: str, written: int) -> None:
        run = self._run_by(rid=rid)
        if run is None or run.ended:
            return
        run.ended = True
        if self._index is not None and path and run.path and path != run.path:
            self._index.moved(run.path, path)  # 2.9.7: the helper saved it under another name
        if run.host is not None and not (run.stopping or run.user_stopped or run.cut):
            # 2.9.7: a player's recording that ended unasked. One whose player still plays its
            # buffer was stopped there (R): this airing is done. Else its stream broke (it
            # ended, the player fell back to direct play or was refused, the helper went): the
            # rest is recorded again, as a session's own break is.
            if reason == STOPPED and run.host.recordable():
                run.user_stopped = True
            elif reason != SPACE:
                run.cut = {ENDED: "ended", LOST: "helper"}.get(reason, FAILED)
        cut = run.cut or (reason if reason == FAILED else "")
        self._settle(run, written, path, cut or reason)
        left = rs.window(run.job)[1] - self._clock()
        if cut and left > MIN_LEFT_S and not run.user_stopped:
            # Cut short while it airs: what it has is kept, the rest goes to another file.
            if written > 0:
                self.schedule.noted(run.job, rs.FAILED, path=path, why=cut)
            self.schedule.unstarted(run.job)
            run.retry = (rs.FAILED, cut)
        else:
            if written > 0 and not cut:
                self._finish(run, rs.RECORDED, path=path)
            elif written > 0:
                self._finish(run, rs.FAILED, path=path, why=cut)
            else:
                self._finish(run, rs.FAILED, why=cut or reason or "empty")
            if run.host is None:  # 2.9.7: a player's recording: the player says how it ended
                shown = STOPPED if reason == LIMIT else reason  # its window's end, not 24 h
                text, kind = recording.ended_text(shown, path, written)
                self._toast(text, kind)
        following = run.handing
        if following is not None and following in self._runs and run.sid and not cut:
            # The next programme takes the session (and its stream) over.
            following.sid, following.ticket = run.sid, run.ticket
            self._budget.relabel(following.ticket, _purpose(following.job))  # its name now
            following.waiting_for = None
            run.sid, run.ticket = "", DENIED
            self._release(run)
            self._record(following, rs.window(following.job)[0])
            return
        if following is not None:
            following.waiting_for = None
            if following in self._runs:
                self._attempt(following)
        if run.host is not None:  # 2.9.7: the player's session stays the player's
            if run.retry is not None and not run.user_stopped:
                self._cut_at[run.job.key] = self._clock()  # the rest, from where it broke
                state, why = run.retry
                self._tries[run.job.key] = _Try(run.job, self._clock() + RETRY_S, state, why)
            self._release(run)
            return
        if run.sid and self._lent.get(run.sid):
            self._serve(run)  # 2.9.7: a player watches it: it stays open for them
            return
        self._engine.close_session(run.sid)

    def _on_failed(self, sid: str, kind: str, message: str) -> None:
        if kind == "restarting":
            return  # the helper reopens it
        run = self._run_by(sid=sid)
        if run is None or run.stopping:
            return
        if run.rid and not run.ended:
            # The recording ends with what it has: its end (recordingEnded) says so.
            run.cut = message or kind
            run.stopping = True
            self._engine.stop_recording(sid)
            return
        run.retry = (rs.FAILED, message or kind)  # not recording yet: tried again
        self._engine.close_session(sid)

    def _on_ended(self, sid: str) -> None:
        self._on_failed(sid, "ended", "")

    def _stop(self, run: _Run) -> None:
        """Its window ended (or the REC sign's Stop, a cancel, QiTV closing): the recording
        stops; its end follows."""
        run.stopping = True
        if run.host is not None and not run.ended:  # 2.9.7: on a player
            if run.host.recording_id() == run.rid:
                run.host.stop_recording()
            return  # its end follows (recordingEnded), with the jobs within it
        if run.sid and run.rid and not run.ended:
            self._engine.stop_recording(run.sid)
            return  # its end follows (recordingEnded), with the jobs within it
        self._settle(run, 0, "", "no picture" if run.sid else "link")
        if run.sid:
            self._finish(run, rs.FAILED, why="no picture")
            self._engine.close_session(run.sid)
        else:  # its stream or its link is on the way, or it waits for a handover
            if not run.user_stopped:
                self._said(run.job, rs.FAILED, "link")
            self._release(run)

    def _finish(self, run: _Run, state: str, **extra: str) -> None:
        if run.state:
            return
        run.state = state
        self.schedule.finished(run.job, state, **extra)

    def _said(self, job: rs.Job, state: str, why: str) -> None:
        """A programme whose window ended without a recording: noted, and said."""
        self.schedule.finished(job, state, why=why)
        name = str(job.channel.get("name") or "")
        if state == rs.MISSED:
            self._toast(translate(NO_STREAM_TEXT, title=job.title, channel=name), "error")
        else:
            self._toast(translate(FAILED_TEXT, title=job.title, why=failure_reason(why)), "error")

    def _on_closed(self, sid: str) -> None:
        run = self._run_by(sid=sid)
        if run is None:
            return
        if run.retry is not None and not run.user_stopped:
            state, why = run.retry
            self._tries[run.job.key] = _Try(run.job, self._clock() + RETRY_S, state, why)
        self._release(run)

    def _release(self, run: _Run) -> None:
        self._settle(run, 0, "")  # none left behind (they are due again, or noted)
        if run.ticket != DENIED:
            self._budget.release(run.ticket)
            run.ticket = DENIED
        if run in self._runs:
            self._runs.remove(run)
            self.changed.emit()


def _same_channel(recorded: dict, channel: dict) -> bool:
    """2.9.7: a recording's channel (as the schedule keeps it) is `channel` (a list's)
    (`recording_schedule.is_listed`)."""
    return rs.is_listed(recorded, channel)


def _purpose(job: rs.Job) -> str:
    """The budget purpose of `job`'s stream: its REC sign name (`recording_now`)."""
    return SCHEDULED_PREFIX + job.key
