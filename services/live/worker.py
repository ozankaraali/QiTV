"""The live pipeline's helper process: every live session behind one loopback server.

`helper_main` is the `multiprocessing` spawn target. It is ported in spirit from main's
per-recording child (`services/timeshift.py`, `_record_process`): the provider URL and
headers arrive over the pipe, never in argv; fds 1 and 2 point at devnull because native
code and library loggers can print URLs (no logging handler is installed here either);
and a watchdog stops the helper when its owner dies.

The process ends with `os._exit` on every path (after "shutdown", once the owner closed
the pipe or died, after a failure), once its shutdown closed the upstream connections and
removed the disk buffers; after the owner died, within `_OWNER_GRACE` even when that
shutdown is stuck. So nothing keeps a helper running behind the GUI's back with provider
connections: neither a thread that is not a daemon (any imported module's, or one started
by a parent's unguarded `__main__` module, which spawn runs again in the child) nor the
interpreter's own teardown.

Commands (parent to helper):
- `("open", sid, url, headers, verify_ssl, ring, live_delay[, start])`, where `ring` is
  `{"kind": "ram", "max_bytes": n}` or `{"kind": "disk", "root": path, "dir": name,
  "max_bytes": n}` (`root` defaults to the helper's own root) and `start` (2.1) is
  `{"target": seconds, "extra": seconds}`: the session's target and what it adds to it, so an
  HLS source starts far enough back for a start at that target (`HlsSource._depth`);
- `("close", sid)`, always answered by `("closed", sid)`, once the session's upstream
  connection is closed but at most 0.5 s later;
- `("resize", sid, max_bytes)` (2.2, the multiview): a RAM session holds up to `max_bytes`
  from now on, its open ring at once (a smaller size evicts the oldest segments) and a ring
  made later too; a disk session's size stays the user's. Unanswered; an unknown sid is
  ignored;
- `("park", sid)` (2.6, kept buffers): the session's upstream closes and its buffer stays:
  what the pipeline holds goes into the ring and the open segment completes; ring, timeline,
  segmenter and clock joiner are kept, and the server serves the ring, readers ending at its
  end as after an ended input. Answered by `("parked", sid)` once the upstream connection is
  closed (an unknown sid at once). Parking a session whose input already ended answers at once;
- `("resume", sid, url, headers, verify_ssl, live_delay[, start])` (2.6): a new upstream for a
  parked session. Its first segment begins a new part of the timeline (`Timeline.begin_part`,
  with the time of day) and is a junction: the clock joiner re-bases the new run onto the old
  run's last picture, as for a restart inside one connection, so the timeline and VLC go
  straight across the gap; the counters run on, the segment numbers too, and the ring marks
  the segment so a reader following on into it reads its tables (`Ring.open_segment`).
  Answered by `("resumed", sid)` at that segment. A resume that fails reports its error and
  parks again (`("parked", sid)`): nothing of the buffer goes. A resume of a session that is
  not parked is ignored;
- `("keep", sid, label)` and `("where", sid, where)` (2.6): a disk buffer's label (the
  channel's key) and where the user was in it, written to its folder's index (a v2 folder,
  `services.live.ring`); unanswered, ignored for a memory buffer. A helper going down
  ("shutdown", its owner gone) keeps v2 folders for the next helper; "close" removes them;
- `("adopt", sid, dir_name, max_bytes)` (2.6): take a kept folder back (a v2 folder a helper
  that went down kept, `services.live.ring.DiskRing.adopt`) as parked session `sid`: its ring,
  timeline and parts as they were, its segmenter and clock joiner primed with its newest
  segment so a resume goes on after it. Answered by a state holding every span, then
  `("parked", sid)` and `("kept", sid, label, where)`; `("unkept", sid)` when it cannot be
  taken back (gone, in use, unreadable);
- `("record_start", sid, rid, path, seq, limits)` (2.2): recording `rid` (the parent's name for
  it) writes the session's ring to a new file at `path` (a unique name next to it when that
  exists) from segment `seq` on, following the live edge, until "record_stop", the session's
  close or the helper's shutdown, the end of its input, or `limits` (`{"max_s": seconds,
  "min_free": bytes}`, both optional) stop it (`services.live.recorder`). It opens no
  connection. One recording per session at a time (another start is answered as failed);
  a stopped one may still be finishing. Answered by "recorded";
- `("record_stop", sid)`: the session's recording ends at the live edge; an unknown sid, or
  one that records nothing, is ignored;
- `("shutdown",)`.

Messages (helper to parent): `("hello", base_url)`; `("state", sid, snapshot, first,
additions)`, also before ready while bytes arrive (`received`) and nothing is playable yet,
and once a raw-TS source answered (`answered`, 2.1: its headers came, before its first byte;
taken back once its body failed or turned out a playlist),
each addition a newly closed segment's `(seq, start, end, discontinuity, start_quality)`
(`Span`'s fields; an engine from before 2.1 would not take the fifth, but the helper and the
engine always ship in one build, and the engine still takes four);
`("ready", sid)` once the first segment is open (its state comes first);
`("recording", sid, rid, path, written)` once recording `rid` made its file and every few
seconds while it grows (2.2), and `("recorded", sid, rid, reason, path, written)` once it ended
(`reason` is one of `recorder.REASONS`; `path` the file written, "" when none was). After a
"close" they come before or after the session's "closed", in no set order: the close never
waits for its recording (the next channel opens on "closed"), and the session's teardown lets
the recording reach the live edge meanwhile. A recording outlives its session's cancel, and
these messages are never dropped for it;
`("parked", sid)` and `("resumed", sid)` (2.6, see "park" and "resume");
`("error", sid, kind, message[, status])`, an HTTP failure's status last (2.1: the parent
logs why a channel played directly); `("ended", sid)`; `("closed", sid)`. Error messages are
fixed strings. `kind`, one meaning each:
- a `SourceError` kind ("unsupported", "network", "tls", "timeout", "http"; "unsupported"
  also for input the segmenter or the RAM ring cannot take): the input ended for good;
- "crashed": this session failed unexpectedly while the helper stays up (also the reply to
  a malformed or duplicate "open", and, with sid None, to a malformed command);
- "disk": the disk ring failed and the session goes on in RAM, the only non-final error;
  a session that was resumed (2.6) parks instead, its disk ring kept as it stands.
The helper never sends "internal": that is the parent's report of the helper's death. When
the command loop itself fails, the sessions are cancelled without a message and the helper
exits. After a final error or the end of the input the buffer stays playable until "close".

Messages leave through one queue and one writer thread, the only caller of `conn.send`:
the pipe buffers a few KiB, and a parent that stops reading for a while must never block
the command loop or a session. Queued states of one session merge (see `_Outbox`).

Each session runs source, segmenter, ring and timeline on its own thread. `close` never
blocks the command loop: a short-lived thread sends "closed" once the session's thread has
released its source (on Windows a TLS reader notices the close within one 0.25 s slice and
only then closes its socket), waiting at most 0.5 s. It never waits for a DNS lookup or a
TCP connect, which cannot be interrupted: a closed session's thread may linger up to the
60 s connect timeout. Once cancelled it sends nothing and touches no ring except to tear
its own down.

This module stays import-light so that spawn starts fast: the source (requests), the
ring and the server (Qt's QLockFile) are imported only inside the helper.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Sequence
import contextlib
import logging
import multiprocessing
from multiprocessing.connection import Connection, wait as wait_for_connection
import os
from pathlib import Path
import re
import threading
import time
from typing import TYPE_CHECKING, Any

from services.live.timeline import StartQuality, Timeline
from services.live.ts import (
    AudioRelabeler,
    ClockJoiner,
    Data,
    Event,
    LoopTrimmer,
    Opened,
    ReconnectTrimmer,
    RepeatTrimmer,
    Segmenter,
    Unsupported,
)

if TYPE_CHECKING:
    from services.live.recorder import Recorder
    from services.live.ring import DiskBudget, Ring
    from services.live.server import StreamServer
    from services.live.source import Source

_LOG = logging.getLogger(__name__)
_wall_time = time.time  # a part's time of day (2.6); tests replace the module's clocks
_POLL = 0.1
_EXPIRE_EVERY_S = 60.0  # 2.6: how often a day-old segment is looked for
_NOTIFY_WAIT_S = 0.5  # a kept session's state after its ring lost segments: waited for that long
_NOTIFY_INTERVAL = 0.25
# A live edge this far past the one last reported is reported at once, throttle or not: a burst
# lands in one read, and the throttle held it back until the next read (up to a pause later).
_EDGE_STEP_S = 0.5
# Raw TS: how often a restart the trimmer holds is judged while the source sends nothing.
_HOLD_TICK_S = 0.5
# The parent terminates the helper 0.5 s after "shutdown"; both waits are usually instant.
_SHUTDOWN_GRACE = 0.25  # for session threads to remove their disk buffers
_FLUSH_GRACE = 0.25  # for the writer to deliver what is queued
_RELEASE_WAIT = 0.5  # for a closed session to close its upstream before "closed" is sent
_OWNER_GRACE = 1.0  # after the owner died: for the shutdown to remove the disk buffers
_RECORD_GRACE = 2.0  # at a session's teardown: for its recording to reach the live edge
_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")  # as the stream server requires
_INTERNAL = "The live buffer stopped unexpectedly."
_UNSUPPORTED = "The channel's stream cannot be time-shifted."
_NO_MEDIA = "The channel ended before any video could be buffered."
_NO_DATA = "The channel sent no data."
_DISK_FAILED = "The temporary playback buffer could not be created."

# seq, start, end, discontinuity, start_quality
_Addition = tuple[int, float, float, bool, StartQuality]
# PTS jumps the timeline left out, in the state while the ring holds them (2.1; and
# `_BREAK_SLACK_S` after), at
# most this many (a memory bound: a flaky source that reconnects every few seconds)
_BREAKS_KEPT = 64
# A break stays this long after the ring's start passed it (2.1): a player still
# playing evicted media it read before may reach it yet (a sparse-PCR clock's 2 s step and a
# second more).
_BREAK_SLACK_S = 3.0

# BurstMeter (see its docstring):
_BURST_GAP_S = 1.0  # this long without a byte ends a burst
_BURST_SHARE = 0.5  # a burst's bytes arrive within this share of the time until the next
_FIRST_BURST_S = 1.5  # a burst this long counts at once, before a second one confirms it
# The connect burst counts for its period (up to the next burst) when that is longer than its
# media, but at most this many times its media and this many seconds:
_CONNECT_PERIOD_RATIO = 3.0
_CONNECT_PERIOD_MAX_S = 10.0  # a measured channel's cadence: 10 s bursts (gap read 8.5 s)
_BURST_SPREAD = 1.5  # two bursts in a row are alike when neither is larger by more than this
_BURST_RUN = 3  # the estimate is the largest of this many latest bursts
_STEADY_BURSTS = 3.0  # this many estimates' worth of seconds without a gap: steady again
_STEADY_UNCONFIRMED = 1.0  # the same for an estimate from one burst no other has confirmed
# An HLS backlog's EXTINF is scaled by the media/EXTINF ratio measured, kept within these:
_RATIO_MIN = 0.5
_RATIO_MAX = 2.0


class BurstMeter:
    """How a source delivers its media: steadily (`burst_s` 0.0), or in bursts.

    Some providers' raw MPEG-TS restreams send ~10 s of media at once every ~10 s, as HLS
    sends a segment at a time. `add()` takes each chunk's arrival time and the media position
    after it (the live edge, in seconds; it starts at 0.0).

    A burst ends where `_BURST_GAP_S` passes without a byte, and is measured when the next
    one begins: its period is the time from its first byte to the next burst's first byte.
    It counts only when its bytes arrived within `_BURST_SHARE` of that period (mostly gap),
    so the stretch before a hiccup on a steady stream, which takes about as long to deliver
    as to play, is no burst (it clears the run of bursts), also when a backlog at connect
    came first. It counts for its media, but at most its period: at real time no more plays
    until the next burst lands, so anything beyond that was a backlog. That keeps the
    provider's backlog at connect (12 s, then 10 s every 10 s) or after a reconnect out of
    the estimate without treating the first burst apart.

    The first clear burst (at least `_FIRST_BURST_S`) is the estimate at once, so the target,
    the mean lag and the stall bound follow from the second burst on, before stay-live could
    eat the buffer catching up with a lag that is only the sawtooth. When that is the burst at
    connect and it brought less than its period (2-4 s, then the first 10 s burst ~9 s later),
    the gap says more about the provider's cadence than the media: the estimate is the period,
    though at most `_CONNECT_PERIOD_RATIO` times the media and `_CONNECT_PERIOD_MAX_S`, since
    a steady source that paused after connect (an on-demand restream starting up) looks the
    same; for such a source the bounds keep the slow-down that builds a burst's room short.
    It holds only until bursts are alike: the run counts the media, as for any burst. Once two bursts in a row
    are alike (within `_BURST_SPREAD`), the estimate is the largest of the last `_BURST_RUN`
    alike ones: a burst larger than the estimate would starve the player before the next one
    lands (and count as a stall), one smaller costs nothing. A burst unlike the one before
    starts a new run and leaves the estimate as it was until two are alike again, which
    corrects a first burst that was not typical. Once `_STEADY_BURSTS` estimates' worth of
    seconds pass without a gap, the source is steady again (0.0); for an estimate from a single
    burst that no second one has confirmed yet, once `_STEADY_UNCONFIRMED` estimate's worth has.
    A bursty source shows its next gap within half a period of a burst's first byte, inside an
    estimate of about its period (a measured channel: 2-4 s at connect, then 10 s every 10 s),
    while a steady source that paused after connect (4 s, then 12 s without a byte, then real
    time) is taken for 10 s bursts for 10 s (before 2.0.1: for 12 s ones for 36 s).

    Pure logic: no clock of its own.
    """

    def __init__(self) -> None:
        self._estimate = 0.0
        self._confirmed = False  # the estimate came from alike bursts, not one alone
        self._run: deque[float] = deque(maxlen=_BURST_RUN)
        self._last_wall: float | None = None  # the latest arrival
        self._connect = True  # the burst that ends next is the first since the start
        self._start_wall = 0.0  # the current burst's first arrival
        self._start_media = 0.0  # the media position before it
        self._media = 0.0  # the media position after the latest arrival

    @property
    def burst_s(self) -> float:
        return self._estimate

    def add(self, wall: float, media: float) -> None:
        last = self._last_wall
        if last is None or wall - last >= _BURST_GAP_S:
            if last is not None:
                self._ended(
                    self._media - self._start_media,
                    last - self._start_wall,
                    wall - self._start_wall,
                )
            self._start_wall, self._start_media = wall, self._media
        self._last_wall, self._media = wall, media
        if self._estimate:
            steady = _STEADY_BURSTS if self._confirmed else _STEADY_UNCONFIRMED
            if wall - self._start_wall >= steady * self._estimate:
                self._estimate = 0.0  # no gap for a while: steady after all
                self._confirmed = False
                self._run.clear()

    def _ended(self, media: float, took: float, period: float) -> None:
        """A burst of `media` seconds ended: its bytes arrived over `took` seconds, and the
        next burst began `period` seconds after it did."""
        if media <= 0 or took > _BURST_SHARE * period:
            self._run.clear()  # about as long to deliver as to play: no burst
            self._connect = False
            return
        size = min(media, period)  # more than plays until the next one: a backlog
        run = self._run
        if run and max(size, run[-1]) > _BURST_SPREAD * min(size, run[-1]):
            run.clear()  # unlike the one before: a new run starts
        run.append(size)
        connect, self._connect = self._connect, False
        if len(run) >= 2:
            self._estimate = max(run)
            self._confirmed = True
        elif not self._estimate and size >= _FIRST_BURST_S:
            # The first clear burst, until others confirm or correct it.
            if connect and period > size:
                bound = min(_CONNECT_PERIOD_RATIO * size, _CONNECT_PERIOD_MAX_S)
                size = max(size, min(period, bound))
            self._estimate = size


class _Parking(Exception):
    """The session parks at once (a resumed session's disk ring failed: 2.6)."""


class _Cancelled(Exception):
    """The session was closed: unwind without sending anything or writing to a ring."""


def _error(sid: str, kind: str, text: str, status: int | None) -> tuple[Any, ...]:
    """An "error" message; an HTTP failure's status follows its text (the GUI logs it)."""
    message = ("error", sid, kind, text)
    return message if status is None else (*message, status)


def _close_ring(ring: Ring, *, keep: bool = False) -> None:
    try:
        if keep:
            ring.close(keep=True)
        else:
            ring.close()
    except Exception as error:  # RingError: some disk buffer files could not be removed
        _LOG.warning("A live buffer was not removed completely (%s).", type(error).__name__)


def _merge(older: tuple[Any, ...], newer: tuple[Any, ...]) -> tuple[Any, ...]:
    """One state message with the effect of `older` followed by `newer` on the parent.

    The parent drops spans before `first` and appends the additions at or after it; seqs
    only grow, so applying the newer `first` to both lists of additions is equivalent. A
    seq kept twice keeps its newer span, so a merged message is bounded by the ring.
    """
    _, sid, snapshot, first, additions = newer
    kept: dict[int, _Addition] = {}
    if first is not None:
        for item in (*older[4], *additions):
            if item[0] >= first:
                kept[item[0]] = item
    return ("state", sid, snapshot, first, list(kept.values()))


def _sid_of(message: tuple[Any, ...]) -> str | None:
    """The session a message is about; None for "hello" and for a malformed id."""
    return message[1] if len(message) > 1 and isinstance(message[1], str) else None


class _Outbox:
    """The pipe's only writer: messages queue in FIFO order and never block their sender.

    Queued states of one session merge into one (`_merge`), so a parent that stops reading
    cannot make the queue grow without bound. Any other message of that session ends the
    merge, so it keeps its order relative to the states before and after it.
    """

    def __init__(self, conn: Connection) -> None:
        self._conn = conn
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        # One-item lists, so that a queued state can be merged in place.
        self._queue: deque[list[tuple[Any, ...]]] = deque()
        self._states: dict[str, list[tuple[Any, ...]]] = {}  # sid -> its mergeable state
        self._closing = False
        self._broken = False
        self._thread = threading.Thread(target=self._write, name="qitv-live-send", daemon=True)
        self._thread.start()

    def put(self, message: tuple[Any, ...], session: _Session | None = None) -> None:
        """Queue `message`; dropped once `session` is cancelled. Never blocks."""
        with self._wake:
            # Checked under this lock: `close` cancels a session before it queues
            # "closed", so nothing the session queues can follow its "closed".
            if self._closing or self._broken or (session is not None and session.cancelled):
                return
            sid = _sid_of(message)
            if message[0] == "state" and sid is not None:
                entry = self._states.get(sid)
                if entry is not None:
                    entry[0] = _merge(entry[0], message)
                    return
                entry = self._states[sid] = [message]
            else:
                if sid is not None:
                    self._states.pop(sid, None)  # later states queue behind this message
                entry = [message]
            self._queue.append(entry)
            self._wake.notify()

    def close(self, timeout: float) -> None:
        """Queue nothing more and deliver what is queued, waiting at most `timeout`."""
        with self._wake:
            self._closing = True
            self._wake.notify()
        self._thread.join(timeout)

    def _write(self) -> None:
        while True:
            with self._wake:
                while not self._queue and not self._closing:
                    self._wake.wait()
                if not self._queue:
                    return
                entry = self._queue.popleft()
                message = entry[0]
                try:
                    sid = _sid_of(message)
                    if sid is not None and self._states.get(sid) is entry:
                        del self._states[sid]  # a later state queues anew
                except Exception as error:  # one bad message must never stop the writer
                    _LOG.debug("A live helper message was malformed (%s).", type(error).__name__)
            try:
                self._conn.send(message)
            except OSError:
                with self._wake:
                    self._broken = True  # the owner is gone; the command loop sees EOF
                    self._queue.clear()
                    self._states.clear()
                return
            except Exception as error:  # nothing was written: the pickling failed
                _LOG.warning("A live helper message was dropped (%s).", type(error).__name__)


class _Session:
    """One live session: source -> segmenter -> ring and timeline, on its own thread."""

    def __init__(
        self,
        helper: _Helper,
        sid: str,
        url: str,
        headers: dict[str, str],
        verify_ssl: bool,
        ring: dict[str, Any],
        live_delay: float,
        start: dict[str, float] | None = None,
    ) -> None:
        self.sid = sid
        self.stop = threading.Event()
        # Ends the source's open and its reads: a cancel, or a park (2.6) that keeps the rest.
        self._interrupt = threading.Event()
        self._wake = threading.Event()  # a resume came, or the session ends
        # Set under `lock` by the command loop; read under the outbox lock as each message
        # is queued, so nothing from this session follows its "closed".
        self.cancelled = False
        self.lock = threading.Lock()
        # Raw TS: held by whatever moves bytes down the pipeline (the session thread with each
        # item of the source, the hold ticker while the source is silent: `_hold_ticker`).
        self._moving = threading.RLock()
        # What a tick failed with: the session thread raises it with its next item (`_take`).
        self._tick_failed: BaseException | None = None
        self.released = threading.Event()  # the session holds no upstream connection anymore
        self._helper = helper
        self._url = url
        self._headers = headers
        self._verify_ssl = verify_ssl
        self._ring_spec = ring
        self._live_delay = live_delay
        self._start = start or {}
        self._source: Source | None = None
        self._ring: Ring | None = None
        self._disk = False
        self._skipping = False  # after a disk failure: drop data until the next segment
        self._ready = False
        self._ended = False
        self._segmenter = Segmenter()
        # Renames an audio stream the PMT misnames, on the bytes' way to the segmenter.
        self._relabeler = AudioRelabeler()
        # Raw TS only (`_ingest`): drops what a reconnect sends again in the old timebase.
        self._rejoiner: ReconnectTrimmer | None = None
        # Raw TS only: drops what a restart inside the connection sends twice.
        self._trimmer: RepeatTrimmer | None = None
        # Raw TS only: drops what a provider replays while its own source is down.
        self._loop_trimmer: LoopTrimmer | None = None
        # Re-bases a restarted run's timestamps (raw TS) and a clock that leaps ahead (raw TS
        # and HLS) so the clock runs on.
        self._clock_joiner: ClockJoiner | None = None
        self._timeline = Timeline()
        self._bursts = BurstMeter()  # raw TS that arrives in bursts (HLS reports its own)
        self._received = 0  # bytes from the source, those before the first keyframe too
        self._answered = False  # the source's response headers came (bytes may not have yet)
        self._caught_up = False  # HLS: every segment its playlist listed was delivered once
        self._reconnecting = False  # raw TS: the connection broke, a new one is being opened
        self._segment = (0, 0.0)  # HLS: the segment being fetched (serial), the edge it began at
        self._measured = (0.0, 0.0)  # HLS: media and EXTINF seconds of the whole segments
        self._pending: list[_Addition] = []
        # (timeline position, seconds left out) of the latest PTS jumps the segmenter dropped
        self._breaks: deque[tuple[float, float]] = deque(maxlen=_BREAKS_KEPT)
        self._last_notify = 0.0
        self._heard_edge = 0.0  # the live edge the latest state reported
        # 2.6, kept buffers: a park asked (the run ends quietly), done (readers end at the
        # ring's end), and the resume waiting for the session thread.
        self._parking = False
        self._parked = False
        self._resume_args: tuple[str, dict[str, str], bool, float, dict[str, float]] | None = None
        self._new_part = True  # the next segment opened begins a part of the timeline
        self._junction_next = False  # the clock joiner's next piece begins a resumed run
        self._open_junction = False  # the ring's next segment begins a resumed run
        self._resuming = False  # "resumed" goes with the resumed run's first segment
        self._kept_history = False  # resumed once: a disk failure parks, never drops it
        self._clock_hls: bool | None = None  # the kind of source the clock joiner is for
        self._label: dict | None = None  # 2.6: "keep" (written once a disk ring exists)
        self._where: dict | None = None  # 2.6: "where"
        self.keep_on_close = False  # 2.6: the helper goes down: a v2 folder stays
        self.adopting: tuple[str, int] | None = None  # 2.6: (folder, size) to take back
        self._recorder: Recorder | None = None  # 2.2: writes the ring to a file (`record`)
        self._finishing: set[Recorder] = set()  # stopped, still writing up to the live edge
        self.thread = threading.Thread(target=self._run, name="qitv-live-session", daemon=True)

    def record(self, rid: str, path: str, seq: int, limits: dict[str, Any]) -> None:
        """Called by the command loop ("record_start"): never blocks; the recorder's thread
        makes the file. Answered by "recorded" once it ends (at once when it cannot start: the
        session records already, or it was closed)."""
        from services.live.recorder import FAILED, Recorder

        kwargs: dict[str, Any] = {}
        if isinstance(limits.get("max_s"), (int, float)):
            kwargs["max_s"] = float(limits["max_s"])
        if isinstance(limits.get("min_free"), int):
            kwargs["min_free"] = limits["min_free"]
        recorder = Recorder(
            lambda: self._ring,
            self._is_ended,
            path,
            seq,
            self._recorded,
            on_progress=self._recording,
            **kwargs,
        )
        recorder.name = rid
        with self.lock:
            busy = self.cancelled or self._recorder is not None
            if not busy:
                self._recorder = recorder
        if busy:
            self._helper.send(("recorded", self.sid, rid, FAILED, "", 0))
            return
        recorder.start()

    def stop_recording(self) -> None:
        """Called by the command loop ("record_stop"); never blocks. The recording writes on
        up to the live edge meanwhile, and a new one may start at once."""
        with self.lock:
            recorder, self._recorder = self._recorder, None
            if recorder is not None:
                self._finishing.add(recorder)
        if recorder is not None:
            recorder.stop()

    def _recording(self, recorder: Recorder) -> None:
        """A recording's file and its size so far (its thread): the parent names the file
        should the helper die."""
        message = ("recording", self.sid, recorder.name, recorder.path, recorder.written)
        self._helper.send(message)

    def _recorded(self, recorder: Recorder) -> None:
        """A recording's end (its own thread): the parent hears it, before or after "closed"."""
        with self.lock:
            if self._recorder is recorder:
                self._recorder = None
            self._finishing.discard(recorder)
        end = (recorder.reason, recorder.path, recorder.written)
        self._helper.send(("recorded", self.sid, recorder.name, *end))

    def resize(self, max_bytes: int) -> None:
        """Called by the command loop: a RAM session's size from now on (see "resize"). A
        disk session's, or one that fell back to RAM after its disk failed, stays."""
        from services.live.ring import RamRing, RingError

        with self.lock:
            if self.cancelled or self._ring_spec.get("kind") != "ram":
                return
            self._ring_spec = dict(self._ring_spec, max_bytes=max_bytes)  # a ring made later
            ring = self._ring
        if isinstance(ring, RamRing):
            try:
                ring.set_max_bytes(max_bytes)
            except RingError:
                _LOG.warning("A live buffer could not change its size.")

    def cancel(self) -> None:
        """Called by the command loop; never blocks and never joins the session thread."""
        with self.lock:
            self.cancelled = True
            source = self._source
        self.stop.set()  # also interrupts a disk-lock wait
        self._interrupt.set()  # and an open in progress
        self._wake.set()
        if source is not None:
            source.close()

    def park(self) -> None:
        """Called by the command loop ("park"); never blocks. The session thread answers
        "parked" once its upstream is closed; a session that reads nothing answers now."""
        with self.lock:
            if self.cancelled:
                return
            source = self._source
            self._resume_args = None  # a resume that has not begun never begins
            reading = not self.released.is_set()
            if reading:
                self._parking = True
            else:
                self._parked = True
        if not reading:
            if self._ring is not None:
                self._helper.ring_parked(self._ring, True)
            self._send(("parked", self.sid))
            return
        self._interrupt.set()
        if source is not None:
            source.close()

    def resume(
        self,
        url: str,
        headers: dict[str, str],
        verify_ssl: bool,
        live_delay: float,
        start: dict[str, float] | None,
    ) -> None:
        """Called by the command loop ("resume"); never blocks: the session thread opens the
        new upstream. Ignored unless the session reads nothing (parked, or its input ended)."""
        with self.lock:
            # Parked, its input ended, or a park still on its way (another player took the
            # buffer back before "parked" came): the resume follows the park.
            taking_back = self.adopting is not None and not self.released.is_set()
            ready = self.released.is_set() or self._parking or taking_back
            accepted = not self.cancelled and ready and self._resume_args is None
            if accepted:
                self._resume_args = (url, headers, verify_ssl, live_delay, start or {})
        if not accepted:
            _LOG.warning("A live session that still reads was asked to resume; ignored.")
            return
        self._wake.set()

    # ---- session thread ---------------------------------------------------------------
    def _run(self) -> None:
        try:
            if self.adopting is not None and not (self._take_back() and self._await_resume()):
                return
            while True:
                try:
                    self._ingest()
                except _Cancelled:
                    pass
                except BaseException as error:
                    _LOG.warning("A live session failed unexpectedly (%s).", type(error).__name__)
                    with contextlib.suppress(_Cancelled):
                        self._fail("crashed", _INTERNAL)
                finally:
                    try:
                        self._close_source()
                    finally:
                        with self.lock:
                            parked = self._parking
                            if parked:
                                self._parking, self._parked = False, True
                            self.released.set()
                if parked or self._resume_failed():
                    if self._ring is not None:
                        self._helper.ring_parked(self._ring, True)  # it gives way first
                    self._send(("parked", self.sid))
                # Keep serving the buffer until the parent closes the session or resumes it.
                if not self._await_resume():
                    return
        finally:
            self._teardown()

    def _take_back(self) -> bool:
        """Take a kept folder back as a parked session (see "adopt"); False (it ends, having
        said "unkept") when it cannot be."""
        from services.live.parts import Part
        from services.live.ring import DiskRing
        from services.live.timeline import Span

        assert self.adopting is not None
        directory, max_bytes = self.adopting
        root = Path(self._helper.root)
        try:
            ring, index = DiskRing.adopt(root, directory, max_bytes)
        except Exception as error:  # RingError, OSError: gone, in use, unreadable
            _LOG.info(
                "A kept time-shift buffer could not be taken back (%s).", type(error).__name__
            )
            self._helper.unkept(self)
            return False
        records = index.segments
        spans = [
            Span(r["seg"], float(r["s"]), float(r["e"]), bool(r.get("d")), r.get("q") or "key")
            for r in records
        ]
        self._timeline.restore(
            spans, [Part(p["part"], float(p["s"]), float(p["w"])) for p in index.parts]
        )
        # The newest segment primes the segmenter (its program) and the clock joiner (the last
        # picture's timestamps, the counters), so a resume re-bases onto it as after a park.
        newest = records[-1]["seg"]
        segmenter = self._segmenter
        joiner = ClockJoiner(
            program=lambda: (segmenter.video_pid, segmenter.video_type), clock=time.monotonic
        )
        try:
            last = (root / directory / f"segment-{newest}.ts").read_bytes()
            segmenter.feed(last)
            segmenter.finish()
            joiner.feed(last)
            joiner.finish()
        except Exception as error:  # a resume then starts its clock afresh
            _LOG.info(
                "A kept buffer's newest segment could not be read (%s).", type(error).__name__
            )
        segmenter.dropped.clear()
        segmenter.seed(newest + 1)
        self._clock_joiner, self._clock_hls = joiner, None
        self._label, self._where = index.label, index.where
        self._kept_history = self._disk = True
        self._ring_spec = {"kind": "disk", "dir": directory, "max_bytes": max_bytes}
        self._pending = [(s.seq, s.start, s.end, s.discontinuity, s.start_quality) for s in spans]
        self._helper.disk_budget(max_bytes).register(ring, parked=True)
        try:
            self._adopt(ring)
        except _Cancelled:  # the helper goes down meanwhile: the folder stays
            return False
        with self.lock:
            self._parked = True
            self.released.set()  # no upstream
        self._notify(force=True)
        self._send(("parked", self.sid))
        self._send(("kept", self.sid, index.label or {}, index.where or {}))
        return True

    def _resume_failed(self) -> bool:
        """A resumed run ended without its first segment: the session is parked again."""
        with self.lock:
            failed = self._resuming
            if failed:
                self._resuming = False
                self._parked = True
        return failed

    def _await_resume(self) -> bool:
        """Wait for a resume (True, its run prepared) or the session's end (False)."""
        while True:
            self._wake.wait()
            with self.lock:
                if self.cancelled:
                    return False
                args, self._resume_args = self._resume_args, None
                self._wake.clear()
                if args is not None:  # from here a park interrupts the resumed run
                    self._parked = self._parking = self._ended = False
                    self.released.clear()
                    self._interrupt.clear()
            if args is not None:
                self._begin_run(*args)
                return True

    def _begin_run(
        self,
        url: str,
        headers: dict[str, str],
        verify_ssl: bool,
        live_delay: float,
        start: dict[str, float],
    ) -> None:
        """A resumed run's start: the ring, the timeline, the segmenter and the clock joiner
        stay; what belongs to one connection starts afresh (see "resume")."""
        self._url, self._headers, self._verify_ssl = url, headers, verify_ssl
        self._live_delay, self._start = live_delay, start
        with self.lock:
            self._source = None
        ring = self._ring
        if ring is not None:
            ring.clear_failure()  # its disk is tried anew
            self._helper.ring_parked(ring, False)
        self._kept_history = self._resuming = True
        self._new_part = self._open_junction = self._junction_next = True
        self._received, self._answered, self._caught_up = 0, False, False
        self._reconnecting = self._skipping = False
        self._segment, self._measured = (0, 0.0), (0.0, 0.0)
        self._bursts = BurstMeter()
        self._rejoiner = self._trimmer = self._loop_trimmer = None
        self._relabeler.reconnect()
        with self._moving:
            self._discontinuity()  # the segmenter frames the new input afresh

    def _ingest(self) -> None:
        from services.live.ring import RingError
        from services.live.source import SourceError, open_source

        target = self._start.get("target")
        try:
            source = open_source(
                self._url,
                headers=self._headers,
                verify_ssl=self._verify_ssl,
                stop=self._interrupt,
                live_delay=self._live_delay,
                target=float(target) if isinstance(target, (int, float)) else None,
                extra=float(self._start.get("extra") or 0.0),
                on_answer=self._on_answer,
            )
        except SourceError as error:
            if not self._parking:  # a park interrupted the open: nothing to say
                self._send(_error(self.sid, error.kind, str(error), error.status))
            return
        with self.lock:
            cancelled = self.cancelled
            if not cancelled:
                self._source = source
        if cancelled:
            source.close()
            raise _Cancelled()
        if self._ring is None:  # a resumed run goes on in its ring
            self._adopt(self._make_ring())
        segmenter = self._segmenter
        # A resumed run of the same kind keeps the clock joiner: it re-bases the new run onto
        # the old run's last picture (2.6). Another kind starts afresh.
        kept_clock = self._clock_joiner if self._clock_hls in (None, source.hls) else None
        self._clock_hls = source.hls

        def program() -> tuple[int | None, int | None]:
            return segmenter.video_pid, segmenter.video_type

        trimmer = rejoiner = None
        if not source.hls:  # HLS has its own discontinuities and no such restarts
            rejoiner = self._rejoiner = ReconnectTrimmer(program=program)
            # Its clock is the arrival a forward jump is measured against.
            self._clock_joiner = kept_clock or ClockJoiner(program=program, clock=time.monotonic)
            # The clock joiner runs the counters on at every junction; without one, the trimmer
            # does where it drops.
            trimmer = self._trimmer = RepeatTrimmer(
                program=program, clock=time.monotonic, join_counters=self._clock_joiner is None
            )
            self._loop_trimmer = LoopTrimmer(program=program, clock=time.monotonic)
        else:
            # A clock that leaps ahead inside a segment is re-based too: each segment's EXTINF
            # is its arrival (`_follow`).
            self._clock_joiner = kept_clock or ClockJoiner(program=program)
        ticking = self._hold_ticker(trimmer) if trimmer is not None else None
        try:
            try:
                for item in source.chunks():
                    with self._moving:
                        self._take(item, source, trimmer, rejoiner)
            finally:
                if ticking is not None:
                    ticking.set()
            with self._moving:
                self._raise_tick_failure()
                self._check()
                if trimmer is not None and rejoiner is not None:
                    self._pass_on(trimmer.feed(rejoiner.finish()))
                    self._pass_on(trimmer.finish())
                self._clock_end(reconnect=False)
                self._finish_segmenter()
        except _Parking:
            return  # its ring stays as it stands (`_write`)
        except SourceError as error:
            if self._parking:  # the park closed the source
                self._drain()
                self._notify(force=True)
                return
            self._fail(error.kind, str(error), error.status)
            return
        except Unsupported:
            self._fail("unsupported", _UNSUPPORTED)
            return
        except RingError as error:
            # Only the RAM ring raises here (a disk failure falls back to RAM): one
            # keyframe interval does not fit, so play the channel directly instead.
            self._fail("unsupported", str(error))
            return
        if self._parking:
            self._notify(force=True)  # what it held is in the ring: parked, not ended
            return
        if not self._ready or self._resuming:
            if self._received:
                self._fail("unsupported", _NO_MEDIA)
            else:
                self._fail("network", _NO_DATA)
            return
        self._ended = True
        self._notify(force=True)
        self._send(("ended", self.sid))

    def _make_ring(self) -> Ring:
        from services.live.ring import DiskRing, RamRing, RingError

        spec = self._ring_spec
        if spec["kind"] == "ram":
            return RamRing(spec["max_bytes"])
        if spec["kind"] != "disk":
            raise ValueError("unknown ring kind")
        self._check()
        root = Path(spec.get("root") or self._helper.root)
        try:
            ring: Ring = DiskRing(root, spec["dir"], spec["max_bytes"], stop=self.stop)
            self._helper.disk_budget(spec["max_bytes"]).register(ring)
            if self._label is not None:
                ring.note_label(self._label)
            if self._where is not None:
                ring.note_where(self._where)
        except (RingError, OSError) as error:
            self._check()
            # An OSError's text names the path; ring errors are fixed strings.
            self._disk_failed(str(error) if isinstance(error, RingError) else _DISK_FAILED)
            return RamRing(self._fallback_bytes())
        self._disk = True
        return ring

    def _fallback_bytes(self) -> int:
        """The memory a failed disk ring goes on in: the spec's ram_bytes (the user's memory
        per stream), else RamRing's default."""
        from services.live.ring import RamRing

        size = self._ring_spec.get("ram_bytes")
        if isinstance(size, int) and size > 0:
            return size
        return RamRing.DEFAULT_MAX_BYTES

    def _adopt(self, ring: Ring) -> None:
        """Serve `ring` from now on; the previous ring (if any) is closed."""
        with self.lock:
            cancelled = self.cancelled
            previous = self._ring
            if not cancelled:
                # Under the lock: `close` removes the session from the server after
                # setting `cancelled`, so a cancelled session is never added back.
                self._ring = ring
                # 2.6: a recording's file serves what the ring let go of while it records.
                from services.live.recorder import RecordedReads

                reads = RecordedReads(ring, lambda: self._recorder)
                self._helper.server.add_session(self.sid, reads, self._is_ended)
        if cancelled:
            self._helper.forget_ring(ring)
            # 2.6: a helper going down keeps a labelled buffer (one being taken back too).
            _close_ring(ring, keep=self.keep_on_close and self._label is not None)
            raise _Cancelled()
        if previous is not None:
            self._helper.forget_ring(previous)
            _close_ring(previous)

    def _disk_failed(self, message: str) -> None:
        """Spec §9: report the disk failure and keep the session running in RAM."""
        self._disk = False
        self._send(("error", self.sid, "disk", message))

    def _apply(self, events: Sequence[Event]) -> None:
        timeline = self._timeline
        for event in events:
            self._check()
            if isinstance(event, Data):
                if not self._skipping:
                    payload = event.payload
                    self._write(lambda ring: ring.append(payload))
                self._notify()
            elif isinstance(event, Opened):
                began = self._new_part
                if began:  # a run's first segment: the part's time of day (2.6)
                    self._new_part = False
                    timeline.begin_part(_wall_time())
                timeline.on_opened(event.seq, event.first_pts, event.wall, event.discontinuity)
                if began and timeline.parts() and self._ring is not None:
                    part = timeline.parts()[-1]
                    self._ring.note_part(part.first_seq, part.start, part.wall)
                self._skipping = False
                seq, header = event.seq, event.header
                junction, self._open_junction = self._open_junction, False
                self._write(
                    lambda ring: ring.open_segment(seq, header, junction=junction),
                    new_segment=True,
                )
                if self._resuming:
                    with self.lock:
                        self._resuming = False
                    self._ready = True
                    self._notify(force=True)
                    self._send(("resumed", self.sid))
                elif not self._ready:
                    self._ready = True
                    self._notify(force=True)  # the parent starts playback at "oldest"
                    self._send(("ready", self.sid))
            else:
                if not self._skipping:
                    closing = event.seq
                    self._write(lambda ring: ring.complete(closing))
                    if self._disk:
                        self._helper.trim_disk(self)  # 2.6: every disk buffer's total
                span = timeline.on_closed(
                    event.seq, event.next_first_pts, event.wall, event.start_quality
                )
                if not self._skipping and self._ring is not None:
                    self._ring.note_segment(  # 2.6: the index a later helper reads
                        span.seq, span.start, span.end, span.discontinuity, span.start_quality
                    )
                self._pending.append(
                    (span.seq, span.start, span.end, span.discontinuity, span.start_quality)
                )
                self._notify(force=True)
        self._take_breaks()

    def _take_breaks(self) -> None:
        """Where the segmenter left a PTS jump out of the timeline, and how much (the player's
        clock jumps by it there, while the media does not), reported at once."""
        dropped = self._segmenter.dropped
        if not dropped:
            return
        timeline = self._timeline
        starts = {span.seq: span.start for span in timeline.spans()}
        for seq, offset, seconds in dropped:
            # A closed segment's start, else the open one's (where the closed ones end).
            self._breaks.append((starts.get(seq, timeline.end) + (offset or 0.0), seconds))
        dropped.clear()
        self._notify(force=True)

    def _write(self, action: Callable[[Ring], None], *, new_segment: bool = False) -> None:
        from services.live.ring import RamRing, RingError

        ring = self._ring
        assert ring is not None
        try:
            action(ring)
            return
        except RingError as error:
            if not self._disk:
                raise
            message = str(error)
        self._check()
        if self._kept_history:
            # 2.6: a kept history is never given up for a RAM ring, which would remove it:
            # the session parks with the disk ring as it stands, still served. The segment it
            # could not write goes, leaving no span; the next resume writes again.
            self._send(("error", self.sid, "disk", message))
            ring.drop_open()
            self._timeline.drop_open()
            self._segmenter.abandon()
            self._new_part = self._resuming  # a run with nothing written begins none
            with self.lock:
                self._parking = True
            self._interrupt.set()
            raise _Parking()
        self._disk_failed(message)
        fallback = RamRing(self._fallback_bytes())
        self._adopt(fallback)  # readers of the disk ring are cut off; it is removed
        if new_segment:
            action(fallback)  # the segment starts afresh in RAM
        else:
            self._skipping = True  # the open segment lost its start: wait for the next

    def _on_answer(self, answered: bool) -> None:
        """The source's response headers came, before its first byte (2.1): an on-demand
        raw-TS restream may hold its body back for seconds (VLC's second request then finds it
        warm), so the parent waits for it as for bytes that are not playable yet. Taken back
        (False) once that body failed, before the open asks again, or turned out a playlist (an
        HLS channel waits longer only once segment bytes flow). Reported at once, before any
        ring exists."""
        if answered != self._answered:
            self._answered = answered
            self._notify(force=True)

    def _take(
        self,
        item: Any,
        source: Source,
        trimmer: RepeatTrimmer | None,
        rejoiner: ReconnectTrimmer | None,
    ) -> None:
        """One item of the source down the pipeline: bytes, or a marker."""
        from services.live.source import CaughtUp, Discontinuity, Reconnecting

        self._raise_tick_failure()
        self._check()
        if isinstance(item, Reconnecting):
            self._reconnecting = True
            self._notify(force=True)
            return
        self._reconnecting = False
        if isinstance(item, Discontinuity):
            self._relabeler.reconnect()
            if trimmer is not None and rejoiner is not None:
                # A reconnect: what they hold passes on untouched.
                self._pass_on(trimmer.feed(rejoiner.reconnect()))
                self._pass_on(trimmer.discontinuity())
            self._clock_end(reconnect=True)  # (HLS: a discontinuity)
            self._discontinuity()
        elif isinstance(item, CaughtUp):
            # The batch's end, which the throttle may have held back, and the start's
            # backlog has landed: the parent chooses where to start on this.
            self._caught_up = True
            self._notify(force=True)
        else:
            self._follow(source)  # before its bytes: where an HLS segment begins
            self._received += len(item)
            if trimmer is None or rejoiner is None:
                self._rebase([item])
            else:
                self._pass_on(trimmer.feed(rejoiner.feed(item)))
            self._measure()
            if not self._ready:
                self._notify()  # bytes flow, nothing playable yet: say so

    def _hold_ticker(self, trimmer: RepeatTrimmer) -> threading.Event:
        """Raw TS: while the session thread waits on a silent source (it reads with a 20 s
        stall timeout), a restart the trimmer holds is still judged every `_HOLD_TICK_S`
        (`RepeatTrimmer.tick`), so a pause in its burst lets it go then, not with the next
        read: VLC plays its cushion meanwhile, and what was held would be missing from it.
        Each tick takes the pipeline as the session thread does (`_moving`), only while
        something is held; returns the event that stops it. A tick that fails (a ring that
        cannot take a segment, say) ends the ticker: what it failed with is logged and the
        session thread raises it with its next item, as if that item had met it, so the
        session ends the way it would have (`_fail`)."""
        stop = threading.Event()

        def tick() -> None:
            while not stop.wait(_HOLD_TICK_S):
                if not trimmer.holding:
                    continue  # nothing held: the pipeline is left to the session thread
                with self._moving:
                    if stop.is_set() or self.cancelled or not trimmer.holding:
                        continue
                    try:
                        self._pass_on(trimmer.tick())
                        self._measure()
                    except _Cancelled:
                        return
                    except Exception as error:
                        _LOG.warning(
                            "A held restart could not be passed on (%s); the session ends.",
                            type(error).__name__,
                        )
                        self._tick_failed = error
                        return

        threading.Thread(target=tick, name=f"qitv-live-hold-{self.sid}", daemon=True).start()
        return stop

    def _raise_tick_failure(self) -> None:
        """What a hold tick failed with, raised in the session thread (`_hold_ticker`)."""
        failed, self._tick_failed = self._tick_failed, None
        if failed is not None:
            raise failed

    def _fail(self, kind: str, message: str, status: int | None = None) -> None:
        """Report a failure that ends the input; the buffered part stays playable."""
        with self._moving:
            self._end_input(kind, message, status)

    def _end_input(self, kind: str, message: str, status: int | None) -> None:
        if self._ring is not None:
            self._drain()
            if self._resuming:
                self._parked = True  # a failed resume keeps what was kept (2.6)
            else:
                self._ended = True  # readers stop at the end of the buffer instead of waiting
            self._notify(force=True)
        self._send(_error(self.sid, kind, message, status))

    def _drain(self) -> None:
        """The input ends: what the pipeline holds goes into the ring, the open segment
        completes."""
        if self._ring is None:
            return
        with self._moving:
            self._check()  # a cancelled session never touches its ring again
            try:
                if self._trimmer is not None:
                    if self._rejoiner is not None:
                        self._pass_on(self._trimmer.feed(self._rejoiner.finish()))
                    self._pass_on(self._trimmer.finish())
                self._clock_end(reconnect=False)
                self._finish_segmenter()
            except _Cancelled:
                raise
            except _Parking:
                pass  # the ring could not take it: it stays as it stands
            except Exception as error:
                _LOG.info("The last live segment was dropped (%s).", type(error).__name__)

    def _pass_on(self, pieces: list[bytes]) -> None:
        """The trimmer's output, through the loop trimmer and the clock joiner, to the
        segmenter. Between two of the trimmer's pieces a restarted run starts (and between two
        of the loop trimmer's, the new content after a loop it dropped): the clock joiner
        re-bases it so its timestamps run on, and between two of its pieces (or, without one,
        the trimmer's) the segmenter cuts the new run a segment of its own."""
        loop = self._loop_trimmer
        self._rebase(pieces if loop is None else loop.feed(pieces))

    def _rebase(self, pieces: list[bytes]) -> None:
        """Pieces through the clock joiner to the segmenter (see `_pass_on`; HLS: its bytes,
        one piece at a time)."""
        clock = self._clock_joiner
        for k, piece in enumerate(pieces):
            junction = k > 0 or self._junction_next  # a resumed run's first piece too (2.6)
            self._junction_next = False
            parts = [piece] if clock is None else clock.feed(piece, junction=junction)
            for j, part in enumerate(parts):
                if (j if clock is not None else k) > 0:
                    self._discontinuity()
                if part:
                    self._to_segmenter(part)

    def _clock_end(self, reconnect: bool) -> None:
        """The loop trimmer's and the clock joiner's input ends (a reconnect or an HLS
        discontinuity follows, or the end): what they hold."""
        loop = self._loop_trimmer
        if loop is not None:
            self._rebase(loop.reconnect() if reconnect else loop.finish())
        clock = self._clock_joiner
        if clock is not None:
            tail = clock.discontinuity() if reconnect else clock.finish()
            if tail:
                self._to_segmenter(tail)

    def _to_segmenter(self, data: bytes) -> None:
        """Bytes for the segmenter, through the audio relabeler."""
        self._apply(self._segmenter.feed(self._relabeler.feed(data)))

    def _discontinuity(self) -> None:
        """The segmenter's next cut is a discontinuity; what the relabeler holds goes first."""
        held = self._relabeler.flush()
        if held:
            self._apply(self._segmenter.feed(held))
        self._segmenter.discontinuity()

    def _finish_segmenter(self) -> None:
        held = self._relabeler.flush()
        if held:
            self._apply(self._segmenter.feed(held))
        self._apply(self._segmenter.finish())

    def _follow(self, source: Source) -> None:
        """HLS: note the live edge where each segment's first bytes begin (`_backlog`), and
        measure the one before if it arrived whole: its media against its EXTINF. Not the
        first that brought media (it began at edge 0.0): the timeline starts at its first
        keyframe, so whatever came before that would read as missing media. Its EXTINF is the
        clock joiner's arrival (`ClockJoiner.arrive`)."""
        serial = getattr(source, "segment_serial", 0)
        if serial == self._segment[0]:
            return
        fetching = getattr(source, "segment_s", None)
        if self._clock_joiner is not None and isinstance(fetching, float):
            self._clock_joiner.arrive(fetching)
        edge = self._live_edge()
        previous, began = self._segment
        whole = getattr(source, "last_whole", None)
        measurable = began > 0.0 and edge > began
        if isinstance(whole, tuple) and whole[0] == previous and whole[1] > 0 and measurable:
            media, extinf = self._measured
            self._measured = (media + edge - began, extinf + whole[1])
        self._segment = (serial, edge)

    def _backlog(self, source: Source | None) -> float | None:
        """HLS: the media its playlist listed still to come: what the segment being fetched
        holds beyond the live edge and the listed ones after it, by their EXTINF scaled by
        the media/EXTINF ratio of the whole segments so far (2.1: EXTINF 10 on 8 s
        segments moved the start). None until one was measured; 0.0 for a finished playlist
        (it plays from its beginning) and for raw TS."""
        fetching = getattr(source, "segment_s", None)
        rest = getattr(source, "rest_s", None)
        if not isinstance(fetching, float) or not isinstance(rest, float):
            return 0.0
        if getattr(source, "finite", False) is True:
            return 0.0
        media, extinf = self._measured
        if extinf <= 0:
            return None if fetching + rest > 0 else 0.0
        ratio = min(max(media / extinf, _RATIO_MIN), _RATIO_MAX)
        return max(0.0, self._segment[1] + fetching * ratio - self._live_edge()) + rest * ratio

    def _live_edge(self) -> float:
        # On the segment's own PTS axis and the stall-free clock: no stall or jump inflates it.
        return self._timeline.live_edge(self._segmenter.progress_pts, self._segmenter.wall)

    def _measure(self) -> None:
        """Feed the burst meter; a new estimate is reported at once (it moves the parent's
        target and stall bound), and so is a live edge `_EDGE_STEP_S` past the last reported."""
        before = self._bursts.burst_s
        edge = self._live_edge()
        self._bursts.add(time.monotonic(), edge)
        if self._bursts.burst_s != before or edge - self._heard_edge >= _EDGE_STEP_S:
            self._notify(force=True)

    def _notify(self, force: bool = False) -> None:
        if self.cancelled:
            return
        now = time.monotonic()
        if not force and now - self._last_notify < _NOTIFY_INTERVAL:
            return
        self._last_notify = now
        ring, timeline, source = self._ring, self._timeline, self._source
        # No ring yet while the source opens: only its answer can be news then.
        oldest = ring.oldest_seq if ring is not None else None
        recorder = self._recorder
        if recorder is not None and oldest is not None:
            # 2.6: while it records, what it wrote stays on the timeline (its file serves it).
            written = recorder.first_written()
            if written is not None and written < oldest:
                oldest = written
        if ring is not None:
            # Spans of evicted segments leave the timeline (all of them while it is empty).
            evicted = oldest - 1 if oldest is not None else timeline.newest_seq
            first = timeline.oldest_seq
            if first is not None and evicted is not None and first <= evicted:
                timeline.on_evicted(evicted)
        breaks = self._breaks
        edge = self._live_edge()
        while breaks and breaks[0][0] < timeline.start - _BREAK_SLACK_S:
            breaks.popleft()  # its media went a while ago: no player can reach it any more
        snapshot = {
            "oldest": oldest,
            "newest": ring.newest_seq if ring is not None else None,
            "open": ring.open_seq if ring is not None else None,
            "start": timeline.start,
            "end": timeline.end,
            "live_edge": edge,
            "bytes": ring.bytes if ring is not None else 0,
            "ended": self._ended,
            # HLS: the target duration, which a reload may change; raw TS: what the meter
            # measured (0.0 for a steady source). The larger, should both apply.
            "burst_s": max(source.burst_s if source is not None else 0.0, self._bursts.burst_s),
            # Before ready: bytes arrived, though nothing is playable yet (no keyframe).
            "received": self._received,
            # An HLS source: the parent keeps its player at 1x (VLC crawls once its rate
            # was changed on such a stream; 2.0.2).
            "hls": bool(source is not None and source.hls),
            # HLS of fragmented MP4 segments, remuxed to TS (2.3: the statistics say so).
            "fmp4": bool(source is not None and getattr(source, "fmp4", False) is True),
            # PTS jumps the timeline left out: (timeline position, seconds), the latest last.
            "breaks": list(self._breaks),
            # HLS: every segment its playlist listed has arrived, once at least (the backlog
            # fetched at the start has landed). Never for raw TS, whose end is not known.
            "caught_up": self._caught_up,
            # HLS: the live edge once every listed segment arrived is this much further (2.1:
            # a tune need not wait for all of its backlog); 0.0 for raw TS.
            "backlog_s": self._backlog(source),
            # HLS: the playlist is finished (ENDLIST): a clip, played from its beginning.
            "finite": bool(source is not None and getattr(source, "finite", False) is True),
            # How cleanly playback starts at the open segment ("key", "open", "none"; None:
            # none is open), as `start_quality` tells it for the closed ones.
            "open_quality": self._segmenter.open_quality,
            # The program has a video PID (2.1): an audio-only stream's starts are
            # all "none", which is no reason to say anything.
            "video": self._segmenter.video_pid is not None,
            # The source answered (2.1): its headers came, though no byte may have yet.
            "answered": self._answered,
            # Raw TS: the connection broke and a new one is being opened (a silent but open
            # connection is waited out first: a stall is not a reconnect).
            "reconnecting": self._reconnecting,
            # 2.6: the buffer's parts, [first_seq, start, wall] each (`services.live.parts`).
            "parts": [[p.first_seq, p.start, p.wall] for p in timeline.parts()],
        }
        self._heard_edge = edge
        additions, self._pending = self._pending, []
        # Only newly completed segments cross IPC (ported from main's `_notify`): the
        # parent drops spans older than `first` and appends the additions, so a long
        # ring still costs a constant-size message.
        self._send(("state", self.sid, snapshot, timeline.oldest_seq, additions))

    def _check(self) -> None:
        if self.cancelled:
            raise _Cancelled()

    def _is_ended(self) -> bool:
        """Readers end at the ring's end: the input ended, or the session is parked."""
        return self._ended or self._parked

    def _send(self, message: tuple[Any, ...]) -> None:
        self._helper.send(message, self)

    def _close_source(self) -> None:
        with self.lock:
            source = self._source
        if source is not None:
            source.close()  # a finished or failed session holds no upstream connection
            if not source.wait_closed(_RELEASE_WAIT):
                _LOG.info("A live upstream connection is still closing.")

    def _teardown(self) -> None:
        with self.lock:
            recorders = [r for r in (self._recorder, *self._finishing) if r is not None]
        # A recording writes what the ring holds up to its live edge before the ring goes.
        deadline = time.monotonic() + _RECORD_GRACE
        for recorder in recorders:
            recorder.stop()
        for recorder in recorders:
            if not recorder.join(max(0.0, deadline - time.monotonic())):
                _LOG.info("A recording is still finishing as its buffer closes.")
        with self.lock:
            ring, self._ring = self._ring, None
        if ring is not None:
            self._helper.forget_ring(ring)
            # 2.6: a helper going down keeps a labelled buffer (one a later helper can give
            # back to its channel); any other goes as before.
            _close_ring(ring, keep=self.keep_on_close and self._label is not None)

    def note(self, kind: str, value: dict) -> None:
        """Called by the command loop ("keep", "where"): kept, and written to the ring's index
        (now when it exists, else once it is made)."""
        if kind == "keep":
            self._label = dict(value)
        else:
            self._where = dict(value)
        ring = self._ring
        if ring is not None:
            try:
                (ring.note_label if kind == "keep" else ring.note_where)(dict(value))
            except Exception as error:  # an index is a convenience: never the session's end
                _LOG.warning("A kept buffer's note was not written (%s).", type(error).__name__)

    def expire(self, before: float) -> None:
        """2.6: segments completed before `before` go; the parent hears it."""
        ring = self._ring
        if ring is not None and ring.retire_before(before):
            self.notify_soon()

    def notify_soon(self) -> None:
        """Another thread changed the ring (the disk amount, the day's limit): the state says
        so once the pipeline is free, skipped when it stays busy (its own next state will)."""
        if self.cancelled or not self._moving.acquire(timeout=_NOTIFY_WAIT_S):
            return
        try:
            self._notify(force=True)
        finally:
            self._moving.release()


class _Helper:
    """The command loop: owns the stream server, the sessions and the outbox.

    It never blocks except while waiting for the next command.
    """

    def __init__(
        self, conn: Connection, root: str, owner_gone: threading.Event | None = None
    ) -> None:
        from services.live.server import StreamServer

        self.root = root
        self.server: StreamServer = StreamServer()
        self._conn = conn
        # Set by the watchdog: the owner died, though the pipe may still be open (another
        # process can hold a copy of its end).
        self._owner_gone = owner_gone or threading.Event()
        self._outbox = _Outbox(conn)
        self._sessions: dict[str, _Session] = {}
        self._budget: DiskBudget | None = None  # 2.6: the disk rings' shared amount
        self._expiry = threading.Event()  # set: the expiry ticker ends

    # ---- 2.6: the disk amount and the day's limit ----------------------------------------
    def disk_budget(self, max_bytes: int) -> DiskBudget:
        """The disk rings' shared amount: the newest disk session's (the user's setting)."""
        from services.live.ring import DiskBudget

        if self._budget is None:
            self._budget = DiskBudget(max_bytes)
        else:
            self._budget.max_bytes = max_bytes
        return self._budget

    def ring_parked(self, ring: Ring, parked: bool) -> None:
        if self._budget is not None:
            self._budget.set_parked(ring, parked)

    def forget_ring(self, ring: Ring) -> None:
        if self._budget is not None:
            self._budget.unregister(ring)

    def trim_disk(self, writer: _Session) -> None:
        """Fit every disk ring into the amount; kept sessions that lost segments say so."""
        if self._budget is None:
            return
        changed = self._budget.trim()
        for session in list(self._sessions.values()):
            if session is not writer and session._ring in changed:
                session.notify_soon()

    def expire(self) -> None:
        """Segments over a day old go, from every session's ring (2.6)."""
        from services.live import ring as ring_module

        before = ring_module._wall_time() - ring_module.STALE_S
        for session in list(self._sessions.values()):
            session.expire(before)

    def _expire_ticker(self) -> None:
        while not self._expiry.wait(_EXPIRE_EVERY_S):
            try:
                self.expire()
            except Exception as error:  # the next tick tries again
                _LOG.warning("Old time-shift segments could not go (%s).", type(error).__name__)

    def send(self, message: tuple[Any, ...], session: _Session | None = None) -> None:
        """Queue a message for the parent (dropped once `session` is cancelled)."""
        self._outbox.put(message, session)

    def run(self) -> None:
        """Serve commands until "shutdown", or until the owner closes the pipe or dies."""
        self.send(("hello", self.server.start()))
        threading.Thread(target=self._expire_ticker, name="qitv-live-expiry", daemon=True).start()
        while not self._owner_gone.is_set():
            try:
                if not self._conn.poll(_POLL):
                    continue
                command = self._conn.recv()
            except EOFError, OSError:
                # The owner closed its end; Windows can report that from poll() as
                # BrokenPipeError before recv() could turn it into EOFError.
                return
            if not self._dispatch(command):
                return

    def _dispatch(self, command: Any) -> bool:
        """Run one command; False ends the command loop."""
        name = command[0] if isinstance(command, tuple) and command else None
        try:
            if name in ("open", "close"):
                sizes = (7, 8) if name == "open" else (2,)
                if len(command) not in sizes or not isinstance(command[1], str):
                    # A plain reply: a malformed session id is never echoed back.
                    self.send(("error", None, "crashed", _INTERNAL))
                elif name == "open":
                    self._open(*command[1:])
                else:
                    self._close(command[1])
            elif name == "resize":
                size = command[2] if len(command) == 3 else None
                if not isinstance(command[1], str) or type(size) is not int or size <= 0:
                    raise ValueError("a resize needs a session and a positive size")
                session = self._sessions.get(command[1])
                if session is not None:
                    session.resize(size)
            elif name == "park":
                if len(command) != 2 or not isinstance(command[1], str):
                    raise ValueError("a park needs a session")
                self._park(command[1])
            elif name == "resume":
                if len(command) not in (6, 7) or not isinstance(command[1], str):
                    raise ValueError("a resume needs a session and its source")
                self._resume(*command[1:])
            elif name == "adopt":
                if (
                    len(command) != 4
                    or not all(isinstance(command[k], str) for k in (1, 2))
                    or type(command[3]) is not int
                ):
                    raise ValueError("an adopt needs a session, a folder and a size")
                self._adopt(command[1], command[2], command[3])
            elif name in ("keep", "where"):
                if len(command) != 3 or not isinstance(command[1], str):
                    raise ValueError("a note needs a session")
                if not isinstance(command[2], dict):
                    raise TypeError("a note is a mapping")
                session = self._sessions.get(command[1])
                if session is not None:
                    session.note(name, command[2])
            elif name == "record_start":
                self._record(command)
            elif name == "record_stop":
                if len(command) != 2 or not isinstance(command[1], str):
                    raise ValueError("a recording's stop needs a session")
                session = self._sessions.get(command[1])
                if session is not None:
                    session.stop_recording()
            elif name == "shutdown":
                return False
            else:
                _LOG.warning("The live helper ignored a malformed command.")
        except (TypeError, ValueError) as error:
            _LOG.warning("The live helper ignored a malformed command (%s).", type(error).__name__)
        return True

    def _open(
        self,
        sid: str,
        url: str,
        headers: dict[str, str],
        verify_ssl: bool,
        ring: dict[str, Any],
        live_delay: float,
        start: dict[str, float] | None = None,
    ) -> None:
        if not _SESSION_ID.fullmatch(sid) or sid in self._sessions:
            self.send(("error", sid, "crashed", _INTERNAL))
            return
        if start is not None and not isinstance(start, dict):
            raise TypeError("start must be a mapping")
        session = _Session(self, sid, url, headers, verify_ssl, ring, live_delay, start)
        self._sessions[sid] = session
        session.thread.start()

    def _adopt(self, sid: str, directory: str, max_bytes: int) -> None:
        if not _SESSION_ID.fullmatch(sid) or sid in self._sessions:
            self.send(("unkept", sid))
            return
        spec = {"kind": "disk", "dir": directory, "max_bytes": max_bytes}
        session = _Session(self, sid, "", {}, True, spec, 6.0)
        session.adopting = (directory, max_bytes)
        self._sessions[sid] = session
        session.thread.start()

    def unkept(self, session: _Session) -> None:
        """A kept folder could not be taken back (its session thread): it is no session."""
        if self._sessions.get(session.sid) is session:
            del self._sessions[session.sid]
        self.send(("unkept", session.sid))

    def _park(self, sid: str) -> None:
        session = self._sessions.get(sid)
        if session is None:
            self.send(("parked", sid))
            return
        session.park()

    def _resume(
        self,
        sid: str,
        url: str,
        headers: dict[str, str],
        verify_ssl: bool,
        live_delay: float,
        start: dict[str, float] | None = None,
    ) -> None:
        if start is not None and not isinstance(start, dict):
            raise TypeError("start must be a mapping")
        session = self._sessions.get(sid)
        if session is None:
            self.send(("error", sid, "crashed", _INTERNAL))
            return
        session.resume(url, headers, verify_ssl, live_delay, start)

    def _record(self, command: tuple[Any, ...]) -> None:
        """("record_start", sid, rid, path, seq, limits): the session records, else
        "recorded" says it failed at once."""
        from services.live.recorder import FAILED

        if (
            len(command) != 6
            or not all(isinstance(command[k], str) and command[k] for k in (1, 2, 3))
            or type(command[4]) is not int
            or not isinstance(command[5], dict)
        ):
            raise ValueError("a recording needs a session, a name, a path, a segment, limits")
        sid, rid, path, seq, limits = command[1:]
        session = self._sessions.get(sid)
        if session is None:
            self.send(("recorded", sid, rid, FAILED, "", 0))
            return
        session.record(rid, path, seq, limits)

    def _close(self, sid: str) -> None:
        session = self._sessions.pop(sid, None)
        if session is None:
            self.send(("closed", sid))
            return
        session.cancel()
        self.server.remove_session(sid)  # ends that session's loopback responses
        if session.released.is_set():
            self.send(("closed", sid))
            return
        threading.Thread(
            target=self._closed_once_released, args=(session,), name="qitv-live-close", daemon=True
        ).start()

    def _closed_once_released(self, session: _Session) -> None:
        """The budget opens the next stream on "closed": first let this one close (spec §4.2)."""
        session.released.wait(_RELEASE_WAIT)
        self.send(("closed", session.sid))

    def fail_all(self) -> None:
        """The command loop failed: cancel every session, silently, before exiting.

        The helper is going down: its parent reports that ("down", then "internal" for each
        session still reading), and a failure sent from here would arrive before "down".
        """
        for session in self._sessions.values():
            session.cancel()

    def shutdown(self) -> None:
        self._expiry.set()
        sessions = list(self._sessions.values())
        self._sessions.clear()
        for session in sessions:
            session.keep_on_close = True  # 2.6: kept buffers stay for the next helper
            session.cancel()
            self.server.remove_session(session.sid)
        self.server.stop()
        # Give the session threads a moment to remove their disk buffers; one stuck in
        # DNS or a TCP connect is left behind (the parent cleans its directories).
        deadline = time.monotonic() + _SHUTDOWN_GRACE
        for session in sessions:
            session.thread.join(max(0.0, deadline - time.monotonic()))
        self._outbox.close(_FLUSH_GRACE)


def _exit_with_owner(owner_gone: threading.Event) -> None:
    parent = multiprocessing.parent_process()
    if parent is not None:
        wait_for_connection([parent.sentinel])
        owner_gone.set()  # the command loop stops; its shutdown removes the disk buffers
        time.sleep(_OWNER_GRACE)
        # A crashed owner cannot close a stalled upstream connection, and the shutdown
        # leaves one stuck in DNS or a TCP connect: exiting closes every socket. The next
        # helper safely reaps a buffer left behind.
        os._exit(0)


def _raise_open_files() -> None:
    """Room for many open files (2.6: each kept buffer holds its folder's lock): the soft limit
    up to 4096, within the hard one (a Mac app started from the Finder gets 256)."""
    try:
        import resource
    except ImportError:  # Windows has no such limit
        return
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        wanted = 4096 if hard == resource.RLIM_INFINITY else min(4096, hard)
        if soft != resource.RLIM_INFINITY and soft < wanted:
            resource.setrlimit(resource.RLIMIT_NOFILE, (wanted, hard))
    except (OSError, ValueError) as error:
        _LOG.info("The open files limit stays as it is (%s).", type(error).__name__)


def helper_main(conn: Connection, root: str) -> None:
    """Spawn entry point: arguments travel through multiprocessing IPC, not argv.

    It never returns: the process ends with `os._exit` once the helper has shut down.
    """
    helper: _Helper | None = None
    owner_gone = threading.Event()
    try:
        # Python loggers can be silenced, but native libraries and urllib3 diagnostics may
        # write URL-bearing text straight to fd 1 or 2.
        with open(os.devnull, "wb") as discard:
            os.dup2(discard.fileno(), 1)
            os.dup2(discard.fileno(), 2)
        threading.Thread(
            target=_exit_with_owner, args=(owner_gone,), name="qitv-live-owner", daemon=True
        ).start()
        _raise_open_files()
        helper = _Helper(conn, root, owner_gone)
        helper.run()
    except BaseException:
        # No traceback crosses IPC: it could disclose provider URLs.
        if helper is not None:
            helper.fail_all()
    finally:
        try:
            if helper is not None:
                helper.shutdown()  # sessions stopped, buffers removed, the server closed
        finally:
            # Not the interpreter's exit: it waits for every thread that is not a daemon,
            # which could keep this process and its upstream sockets alive for good.
            os._exit(0)
