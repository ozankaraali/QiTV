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
A recording the stream or the helper cuts short is kept, and the rest goes to another file. The
next programme on a channel that records hands over: it needs no stream of its own; half a
minute into it the earlier file ends, and the next starts from the helper's buffer a minute
before its start. One that ends within a recording on its channel (a programme inside an
evening's timer, the same programme scheduled twice) is that recording's: it is noted in its
file once it ends (the file keeps the longer one's name), or is due again on its own when that
recording stops before it ended. Due together, the longest begins first. A one-time recording
made longer while it records goes on to its new end: the scheduler stops every recording at
its window's end (the helper's own limit is only a day's). What records shows on the REC sign
(`recording_now`), which stops it (`stop_now`).

QiTV keeps the computer awake while it runs; a sleeping computer or a closed lid misses them.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
import logging
import time
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from services import recording, recording_schedule as rs
from services.connection_budget import DENIED
from services.i18n import N_, translate
from services.live.recorder import FAILED, LIMIT, MAX_SECONDS, MIN_FREE_BYTES, STOPPED
from services.provider_api import known_http_only
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
# i18n: a toast: QiTV's time-shift helper is not running, so a scheduled recording can't start
NO_HELPER = N_("the time-shift helper is not running")


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
    ) -> None:
        super().__init__(parent)
        self.schedule = schedule
        self._engine: Any = None
        self._catalogs = catalogs
        self._budget = budget
        self._config = config
        self._toast = toast
        self._clock = clock
        self._runs: list[_Run] = []
        self._tries: dict[str, _Try] = {}
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
            if not run.state and not run.user_stopped:
                self._tries[run.job.key] = _Try(run.job, now, rs.FAILED, translate(NO_HELPER))
            run.sid = ""
            self._release(run)
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
                self._said(attempt.job, attempt.state, attempt.why)
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
                SCHEDULED_PREFIX + run.job.key,
                str(run.job.channel.get("name") or ""),
                run.since,
                dict(run.job.channel),
            )
            for run in self._runs
            if run.rid and not run.ended and not run.stopping
        ]

    def stop_now(self, name: str) -> None:
        """The REC sign's Stop recording: this airing's recording stops and is kept (a series
        records its next airing)."""
        for run in list(self._runs):
            if SCHEDULED_PREFIX + run.job.key == name and not run.stopping:
                run.user_stopped = True
                self._stop(run)

    def providers(self) -> set[str]:
        """The providers the schedule records from (their guides stay loaded for series)."""
        return self.schedule.providers()

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
        same = next(
            (
                r
                for r in self._runs
                if r.sid
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
        """A stream of the provider first (none free: tried again later), then the link."""
        job = run.job
        try:
            entry = self._catalogs.provider(job.provider)
        except KeyError:
            self._retry(run, rs.FAILED, "provider")
            return
        limit = self._budget.limit_for(entry, self._catalogs.max_connections(job.provider))
        run.ticket = self._budget.request(
            job.provider, limit, purpose=f"rec:{job.item_id}", queue=False
        )
        if run.ticket == DENIED:
            self._retry(run, rs.MISSED, "stream")
            return
        # The provider's account back in the channel's address (the file keeps it out).
        channel = rs.with_account(job.channel, entry)
        run.link_id = self._catalogs.request_link(channel, section="live")

    def _hand_over_when_due(self, run: _Run, now: float) -> None:
        earlier = run.waiting_for
        if earlier is None:
            return
        if earlier not in self._runs or earlier.ended or not earlier.sid:
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
        https = url.startswith("http://") and not known_http_only(url) and not xtream
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
        run.since = time.monotonic()
        self._toast(translate(STARTED_TEXT, title=run.job.title, channel=name), "info")
        self.changed.emit()

    def _on_recorded(self, rid: str, reason: str, path: str, written: int) -> None:
        run = self._run_by(rid=rid)
        if run is None or run.ended:
            return
        run.ended = True
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
            shown = STOPPED if reason == LIMIT else reason  # its window's end, not a 24 h limit
            text, kind = recording.ended_text(shown, path, written)
            self._toast(text, kind)
        following = run.handing
        if following is not None and following in self._runs and run.sid and not cut:
            # The next programme takes the session (and its stream) over.
            following.sid, following.ticket = run.sid, run.ticket
            following.waiting_for = None
            run.sid, run.ticket = "", DENIED
            self._release(run)
            self._record(following, rs.window(following.job)[0])
            return
        if following is not None:
            following.waiting_for = None
            if following in self._runs:
                self._attempt(following)
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
            self._toast(translate(FAILED_TEXT, title=job.title, why=why), "error")

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
