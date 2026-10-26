"""Scheduled recordings (2.8): the programmes the schedule says are due are recorded with no
player, by the time-shift helper.

Every TICK_MS the scheduler asks the schedule what is due (`services.recording_schedule`). A due
job asks the channel's provider for its link (waiting for its setup), takes one of the
provider's streams without waiting for one (none free: it is missed, and said), opens the
channel's stream in the helper (in memory: nothing on screen, nothing heard) and records it from
the first segment the helper has, until its window ends (or its own limit: the window). At its
end the session closes and the stream goes back once it is closed; the outcome is noted (the
Library names the file by its programme) and said in a toast. A stream that fails for good ends
the recording with what it has.

QiTV keeps the computer awake while it runs; a sleeping computer or a closed lid misses them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import logging
import time
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from services import recording, recording_schedule as rs
from services.connection_budget import DENIED
from services.i18n import N_, translate
from services.live.recorder import ENDED, LIMIT, MIN_FREE_BYTES, STOPPED
from services.provider_api import known_http_only
from services.vlc_profile import DEFAULT_USER_AGENT

logger = logging.getLogger(__name__)

TICK_MS = 20_000
RAM_BYTES = 64 * 1024 * 1024  # the helper's buffer for a recording: it only passes through
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
    stopping: bool = False
    ended: bool = False
    state: str = ""
    why: str = ""
    extra: dict = field(default_factory=dict)


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
        self._engine = engine
        self._catalogs = catalogs
        self._budget = budget
        self._config = config
        self._toast = toast
        self._clock = clock
        self._runs: list[_Run] = []
        self._closed = False
        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self.tick)
        catalogs.linkReady.connect(self._on_link)
        catalogs.linkFailed.connect(self._on_link_failed)
        engine.sessionReady.connect(self._on_ready)
        engine.sessionFailed.connect(self._on_failed)
        engine.sessionEnded.connect(lambda sid: self._on_failed(sid, "ended", ""))
        engine.sessionClosed.connect(self._on_closed)
        engine.recordingEnded.connect(self._on_recorded)

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
        for run in list(self._runs):  # a window that ended stops its recording
            if not run.stopping and now >= rs.window(run.job)[1]:
                self._stop(run)
        busy = {run.job.key for run in self._runs}
        for job in self.schedule.due(now, self._guide):
            if job.key not in busy:
                self._begin(job)

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

    def providers(self) -> set[str]:
        """The providers the schedule records from (their guides stay loaded for series)."""
        return self.schedule.providers()

    def cancel(self, item_id: str) -> None:
        """A scheduled recording is no longer wanted: off the schedule, and stopped (what it
        recorded is kept) when it records now."""
        self.schedule.cancel(item_id)
        for run in list(self._runs):
            if run.job.item_id == item_id and not run.stopping:
                self._stop(run)
        self.changed.emit()

    def shutdown(self) -> None:
        """QiTV closes: the recordings stop and keep what they have."""
        self._timer.stop()
        for run in list(self._runs):
            if run.sid and not run.stopping:
                self._stop(run)
        self._closed = True

    # ---- one recording ----------------------------------------------------------------------

    def _begin(self, job: rs.Job) -> None:
        self.schedule.started(job)
        run = _Run(job)
        self._runs.append(run)
        channel = dict(job.channel)
        run.link_id = self._catalogs.request_link(channel, section="live")
        logger.info("A scheduled recording starts.")

    def _run_by(self, **wanted: Any) -> Optional[_Run]:
        for run in self._runs:
            if all(getattr(run, key) == value for key, value in wanted.items()):
                return run
        return None

    def _on_link_failed(self, link_id: int, message: str) -> None:
        run = self._run_by(link_id=link_id)
        if run is not None and not run.sid:
            self._end(run, rs.FAILED, why=message or "link")

    def _on_link(self, link_id: int, url: str) -> None:
        run = self._run_by(link_id=link_id)
        if run is None or run.sid:
            return
        job = run.job
        try:
            entry = self._catalogs.provider(job.provider)
        except KeyError:
            self._end(run, rs.FAILED, why="provider")
            return
        limit = self._budget.limit_for(entry, self._catalogs.max_connections(job.provider))
        run.ticket = self._budget.request(
            job.provider, limit, purpose=f"rec:{job.item_id}", queue=False
        )
        if run.ticket == DENIED:
            self._end(run, rs.MISSED, why="stream")
            return
        if not self._engine.is_available():
            self._engine.start()
            self._end(run, rs.FAILED, why=translate(NO_HELPER))
            return
        channel = job.channel
        headers = {"User-Agent": str(channel.get("user_agent") or DEFAULT_USER_AGENT)}
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
        if run is None or run.rid:
            return
        spans = self._engine.spans(sid)
        seq = spans[0].seq if spans else 0
        name = str(run.job.channel.get("name") or run.job.title)
        run.path = recording.path_for(self._config, name, datetime.now().astimezone())
        remaining = max(60.0, rs.window(run.job)[1] - self._clock())
        run.rid = self._engine.start_recording(
            sid, run.path, seq, max_s=remaining, min_free=MIN_FREE_BYTES
        )
        if not run.rid:
            self._close(run, rs.FAILED, why="helper")
            return
        self._toast(translate(STARTED_TEXT, title=run.job.title, channel=name), "info")

    def _on_recorded(self, rid: str, reason: str, path: str, written: int) -> None:
        run = self._run_by(rid=rid)
        if run is None or run.ended:
            return
        run.ended = True
        if written > 0 and reason in (ENDED, STOPPED, LIMIT):
            self._close(run, rs.RECORDED, path=path)
        elif written > 0:
            self._close(run, rs.FAILED, path=path, why=reason)
        else:
            self._close(run, rs.FAILED, why=reason or "empty")
        text, kind = recording.ended_text(reason, path, written)
        self._toast(text, kind)

    def _on_failed(self, sid: str, kind: str, message: str) -> None:
        if kind == "restarting":
            return  # the helper reopens it
        run = self._run_by(sid=sid)
        if run is None or run.stopping:
            return
        if run.rid and not run.ended:
            # The recording ends with what it has: its end (recordingEnded) says so.
            run.stopping = True
            self._engine.stop_recording(sid)
            return
        self._close(run, rs.FAILED, why=message or kind)

    def _stop(self, run: _Run) -> None:
        """Its window ended (or QiTV closes): the recording stops; its end follows."""
        run.stopping = True
        if run.sid and run.rid and not run.ended:
            self._engine.stop_recording(run.sid)
        elif run.sid:
            self._close(run, rs.FAILED, why="no picture")
        else:
            self._end(run, rs.FAILED, why="link")

    def _close(self, run: _Run, state: str, **extra: str) -> None:
        """Its session closes (the stream goes back once it is closed)."""
        if run.state:
            return
        run.state = state
        run.extra = extra
        self.schedule.finished(run.job, state, **extra)
        if run.sid:
            self._engine.close_session(run.sid)
        else:
            self._release(run)

    def _on_closed(self, sid: str) -> None:
        run = self._run_by(sid=sid)
        if run is not None:
            self._release(run)

    def _end(self, run: _Run, state: str, why: str = "") -> None:
        """It ends before any session opened: said, and its stream (if taken) goes back."""
        if run.state:
            return
        run.state = state
        self.schedule.finished(run.job, state, why=why)
        name = str(run.job.channel.get("name") or "")
        if state == rs.MISSED:
            self._toast(translate(NO_STREAM_TEXT, title=run.job.title, channel=name), "error")
        else:
            self._toast(translate(FAILED_TEXT, title=run.job.title, why=why), "error")
        self._release(run)

    def _release(self, run: _Run) -> None:
        if run.ticket != DENIED:
            self._budget.release(run.ticket)
            run.ticket = DENIED
        if run in self._runs:
            self._runs.remove(run)
            self.changed.emit()
