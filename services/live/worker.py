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
- `("open", sid, url, headers, verify_ssl, ring, live_delay)`, where `ring` is
  `{"kind": "ram", "max_bytes": n}` or `{"kind": "disk", "root": path, "dir": name,
  "max_bytes": n}` (`root` defaults to the helper's own root);
- `("close", sid)`, always answered by `("closed", sid)`, once the session's upstream
  connection is closed but at most 0.5 s later;
- `("shutdown",)`.

Messages (helper to parent): `("hello", base_url)`; `("state", sid, snapshot, first,
additions)`, also before ready while bytes arrive (`received`) and nothing is playable yet;
`("ready", sid)` once the first segment is open (its state comes first);
`("error", sid, kind, message)`; `("ended", sid)`; `("closed", sid)`. Error messages are
fixed strings. `kind`, one meaning each:
- a `SourceError` kind ("unsupported", "network", "tls", "timeout", "http"; "unsupported"
  also for input the segmenter or the RAM ring cannot take): the input ended for good;
- "crashed": this session failed unexpectedly while the helper stays up (also the reply to
  a malformed or duplicate "open", and, with sid None, to a malformed command);
- "disk": the disk ring failed and the session goes on in RAM, the only non-final error.
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

from services.live.timeline import Timeline
from services.live.ts import Data, Event, Opened, Segmenter, Unsupported

if TYPE_CHECKING:
    from services.live.ring import Ring
    from services.live.server import StreamServer
    from services.live.source import Source

_LOG = logging.getLogger(__name__)
_POLL = 0.1
_NOTIFY_INTERVAL = 0.25
# The parent terminates the helper 0.5 s after "shutdown"; both waits are usually instant.
_SHUTDOWN_GRACE = 0.25  # for session threads to remove their disk buffers
_FLUSH_GRACE = 0.25  # for the writer to deliver what is queued
_RELEASE_WAIT = 0.5  # for a closed session to close its upstream before "closed" is sent
_OWNER_GRACE = 1.0  # after the owner died: for the shutdown to remove the disk buffers
_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")  # as the stream server requires
_INTERNAL = "The live buffer stopped unexpectedly."
_UNSUPPORTED = "The channel's stream cannot be time-shifted."
_NO_MEDIA = "The channel ended before any video could be buffered."
_NO_DATA = "The channel sent no data."
_DISK_FAILED = "The temporary playback buffer could not be created."

_Addition = tuple[int, float, float, bool]  # seq, start, end, discontinuity

# BurstMeter (see its docstring):
_BURST_GAP_S = 1.0  # this long without a byte ends a burst
_BURST_SHARE = 0.5  # a burst's bytes arrive within this share of the time until the next
_FIRST_BURST_S = 1.5  # a burst this long counts at once, before a second one confirms it
# The connect burst counts for its period (up to the next burst) when that is longer than its
# media, but at most this many times its media and this many seconds:
_CONNECT_PERIOD_RATIO = 3.0
_CONNECT_PERIOD_MAX_S = 10.0  # the owner's cadence: 10 s bursts (the gap read 8.5 s)
_BURST_SPREAD = 1.5  # two bursts in a row are alike when neither is larger by more than this
_BURST_RUN = 3  # the estimate is the largest of this many latest bursts
_STEADY_BURSTS = 3.0  # this many estimates' worth of seconds without a gap: steady again
_STEADY_UNCONFIRMED = 1.0  # the same for an estimate from one burst no other has confirmed


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
    estimate of about its period (the owner's channel: 2-4 s at connect, then 10 s every 10 s),
    while a steady source that paused after connect (4 s, then 12 s without a byte, then real
    time) is taken for 10 s bursts for 10 s (before 2.0.1's review: for 12 s ones for 36 s).

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


class _Cancelled(Exception):
    """The session was closed: unwind without sending anything or writing to a ring."""


def _close_ring(ring: Ring) -> None:
    try:
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
    ) -> None:
        self.sid = sid
        self.stop = threading.Event()
        # Set under `lock` by the command loop; read under the outbox lock as each message
        # is queued, so nothing from this session follows its "closed".
        self.cancelled = False
        self.lock = threading.Lock()
        self.released = threading.Event()  # the session holds no upstream connection anymore
        self._helper = helper
        self._url = url
        self._headers = headers
        self._verify_ssl = verify_ssl
        self._ring_spec = ring
        self._live_delay = live_delay
        self._source: Source | None = None
        self._ring: Ring | None = None
        self._disk = False
        self._skipping = False  # after a disk failure: drop data until the next segment
        self._ready = False
        self._ended = False
        self._segmenter = Segmenter()
        self._timeline = Timeline()
        self._bursts = BurstMeter()  # raw TS that arrives in bursts (HLS reports its own)
        self._received = 0  # bytes from the source, those before the first keyframe too
        self._pending: list[_Addition] = []
        self._last_notify = 0.0
        self.thread = threading.Thread(target=self._run, name="qitv-live-session", daemon=True)

    def cancel(self) -> None:
        """Called by the command loop; never blocks and never joins the session thread."""
        with self.lock:
            self.cancelled = True
            source = self._source
        self.stop.set()  # also interrupts an open in progress and a disk-lock wait
        if source is not None:
            source.close()

    # ---- session thread ---------------------------------------------------------------
    def _run(self) -> None:
        try:
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
                    self.released.set()
            # Keep serving the buffer until the parent closes the session.
            self.stop.wait()
        finally:
            self._teardown()

    def _ingest(self) -> None:
        from services.live.ring import RingError
        from services.live.source import Discontinuity, SourceError, open_source

        try:
            source = open_source(
                self._url,
                headers=self._headers,
                verify_ssl=self._verify_ssl,
                stop=self.stop,
                live_delay=self._live_delay,
            )
        except SourceError as error:
            self._send(("error", self.sid, error.kind, str(error)))
            return
        with self.lock:
            cancelled = self.cancelled
            if not cancelled:
                self._source = source
        if cancelled:
            source.close()
            raise _Cancelled()
        self._adopt(self._make_ring())
        segmenter = self._segmenter
        try:
            for item in source.chunks():
                self._check()
                if isinstance(item, Discontinuity):
                    segmenter.discontinuity()
                else:
                    self._received += len(item)
                    self._apply(segmenter.feed(item))
                    self._measure()
                    if not self._ready:
                        self._notify()  # bytes flow, nothing playable yet: say so
            self._check()
            self._apply(segmenter.finish())
        except SourceError as error:
            self._fail(error.kind, str(error))
            return
        except Unsupported:
            self._fail("unsupported", _UNSUPPORTED)
            return
        except RingError as error:
            # Only the RAM ring raises here (a disk failure falls back to RAM): one
            # keyframe interval does not fit, so play the channel directly instead.
            self._fail("unsupported", str(error))
            return
        if not self._ready:
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
                self._helper.server.add_session(self.sid, ring, self._is_ended)
        if cancelled:
            _close_ring(ring)
            raise _Cancelled()
        if previous is not None:
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
                timeline.on_opened(event.seq, event.first_pts, event.wall, event.discontinuity)
                self._skipping = False
                seq, header = event.seq, event.header
                self._write(lambda ring: ring.open_segment(seq, header), new_segment=True)
                if not self._ready:
                    self._ready = True
                    self._notify(force=True)  # the parent starts playback at "oldest"
                    self._send(("ready", self.sid))
            else:
                if not self._skipping:
                    closing = event.seq
                    self._write(lambda ring: ring.complete(closing))
                span = timeline.on_closed(event.seq, event.next_first_pts, event.wall)
                self._pending.append((span.seq, span.start, span.end, span.discontinuity))
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
        self._disk_failed(message)
        fallback = RamRing(self._fallback_bytes())
        self._adopt(fallback)  # readers of the disk ring are cut off; it is removed
        if new_segment:
            action(fallback)  # the segment starts afresh in RAM
        else:
            self._skipping = True  # the open segment lost its start: wait for the next

    def _fail(self, kind: str, message: str) -> None:
        """Report a failure that ends the input; the buffered part stays playable."""
        if self._ring is not None:
            self._check()  # a cancelled session never touches its ring again
            try:
                self._apply(self._segmenter.finish())
            except _Cancelled:
                raise
            except Exception as error:
                _LOG.info("The last live segment was dropped (%s).", type(error).__name__)
            self._ended = True  # readers stop at the end of the buffer instead of waiting
            self._notify(force=True)
        self._send(("error", self.sid, kind, message))

    def _live_edge(self) -> float:
        # On the segment's own PTS axis and the stall-free clock: no stall or jump inflates it.
        return self._timeline.live_edge(self._segmenter.progress_pts, self._segmenter.wall)

    def _measure(self) -> None:
        """Feed the burst meter; a new estimate is reported at once (it moves the parent's
        target and stall bound)."""
        before = self._bursts.burst_s
        self._bursts.add(time.monotonic(), self._live_edge())
        if self._bursts.burst_s != before:
            self._notify(force=True)

    def _notify(self, force: bool = False) -> None:
        if self.cancelled:
            return
        now = time.monotonic()
        if not force and now - self._last_notify < _NOTIFY_INTERVAL:
            return
        self._last_notify = now
        ring, timeline, source = self._ring, self._timeline, self._source
        if ring is None:
            return
        oldest = ring.oldest_seq
        # Spans of evicted segments leave the timeline (all of them while the ring is empty).
        evicted = oldest - 1 if oldest is not None else timeline.newest_seq
        first = timeline.oldest_seq
        if first is not None and evicted is not None and first <= evicted:
            timeline.on_evicted(evicted)
        snapshot = {
            "oldest": oldest,
            "newest": ring.newest_seq,
            "open": ring.open_seq,
            "start": timeline.start,
            "end": timeline.end,
            "live_edge": self._live_edge(),
            "bytes": ring.bytes,
            "ended": self._ended,
            # HLS: the target duration, which a reload may change; raw TS: what the meter
            # measured (0.0 for a steady source). The larger, should both apply.
            "burst_s": max(source.burst_s if source is not None else 0.0, self._bursts.burst_s),
            # Before ready: bytes arrived, though nothing is playable yet (no keyframe).
            "received": self._received,
            # An HLS source: the parent keeps its player at 1x (VLC crawls once its rate
            # was changed on such a stream; 2.0.2).
            "hls": bool(source is not None and source.hls),
        }
        additions, self._pending = self._pending, []
        # Only newly completed segments cross IPC (ported from main's `_notify`): the
        # parent drops spans older than `first` and appends the additions, so a long
        # ring still costs a constant-size message.
        self._send(("state", self.sid, snapshot, timeline.oldest_seq, additions))

    def _check(self) -> None:
        if self.cancelled:
            raise _Cancelled()

    def _is_ended(self) -> bool:
        return self._ended

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
            ring, self._ring = self._ring, None
        if ring is not None:
            _close_ring(ring)


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

    def send(self, message: tuple[Any, ...], session: _Session | None = None) -> None:
        """Queue a message for the parent (dropped once `session` is cancelled)."""
        self._outbox.put(message, session)

    def run(self) -> None:
        """Serve commands until "shutdown", or until the owner closes the pipe or dies."""
        self.send(("hello", self.server.start()))
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
                if len(command) != (7 if name == "open" else 2) or not isinstance(command[1], str):
                    # A plain reply: a malformed session id is never echoed back.
                    self.send(("error", None, "crashed", _INTERNAL))
                elif name == "open":
                    self._open(*command[1:])
                else:
                    self._close(command[1])
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
    ) -> None:
        if not _SESSION_ID.fullmatch(sid) or sid in self._sessions:
            self.send(("error", sid, "crashed", _INTERNAL))
            return
        session = _Session(self, sid, url, headers, verify_ssl, ring, live_delay)
        self._sessions[sid] = session
        session.thread.start()

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
        sessions = list(self._sessions.values())
        self._sessions.clear()
        for session in sessions:
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
