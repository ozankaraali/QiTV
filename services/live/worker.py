"""The live pipeline's helper process: every live session behind one loopback server.

`helper_main` is the `multiprocessing` spawn target. It is ported in spirit from main's
per-recording child (`services/timeshift.py`, `_record_process`): the provider URL and
headers arrive over the pipe, never in argv; fds 1 and 2 point at devnull because native
code and library loggers can print URLs (no logging handler is installed here either);
and a watchdog exits the process when its owner dies, which closes every upstream socket.

Commands (parent to helper):
- `("open", sid, url, headers, verify_ssl, ring, live_delay)`, where `ring` is
  `{"kind": "ram", "max_bytes": n}` or `{"kind": "disk", "root": path, "dir": name,
  "max_bytes": n}` (`root` defaults to the helper's own root);
- `("close", sid)`, always answered by `("closed", sid)`;
- `("shutdown",)`.

Messages (helper to parent): `("hello", base_url)`; `("state", sid, snapshot, first,
additions)`; `("ready", sid)` once the first segment is open (its state comes first);
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
waits for that thread: a DNS lookup or TCP connect cannot be interrupted, so a closed
session's thread may linger up to the 60 s connect timeout. Once cancelled it sends
nothing and touches no ring except to tear its own down.

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
_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")  # as the stream server requires
_INTERNAL = "The live buffer stopped unexpectedly."
_UNSUPPORTED = "The channel's stream cannot be time-shifted."
_NO_MEDIA = "The channel ended before any video could be buffered."
_NO_DATA = "The channel sent no data."
_DISK_FAILED = "The temporary playback buffer could not be created."

_Addition = tuple[int, float, float, bool]  # seq, start, end, discontinuity


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
                self._close_source()
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
        received = 0
        try:
            for item in source.chunks():
                self._check()
                if isinstance(item, Discontinuity):
                    segmenter.discontinuity()
                else:
                    received += len(item)
                    self._apply(segmenter.feed(item))
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
            if received:
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
            return RamRing()
        self._disk = True
        return ring

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
        fallback = RamRing()
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

    def _notify(self, force: bool = False) -> None:
        if self.cancelled:
            return
        now = time.monotonic()
        if not force and now - self._last_notify < _NOTIFY_INTERVAL:
            return
        self._last_notify = now
        ring, timeline = self._ring, self._timeline
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
            # On the segment's own PTS axis and the stall-free clock: no stall or jump inflates it.
            "live_edge": timeline.live_edge(self._segmenter.progress_pts, self._segmenter.wall),
            "bytes": ring.bytes,
            "ended": self._ended,
            # HLS: the target duration, which a reload may change; 0.0 for raw TS.
            "burst_s": self._source.burst_s if self._source is not None else 0.0,
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

    def _teardown(self) -> None:
        with self.lock:
            ring, self._ring = self._ring, None
        if ring is not None:
            _close_ring(ring)


class _Helper:
    """The command loop: owns the stream server, the sessions and the outbox.

    It never blocks except while waiting for the next command.
    """

    def __init__(self, conn: Connection, root: str) -> None:
        from services.live.server import StreamServer

        self.root = root
        self.server: StreamServer = StreamServer()
        self._conn = conn
        self._outbox = _Outbox(conn)
        self._sessions: dict[str, _Session] = {}

    def send(self, message: tuple[Any, ...], session: _Session | None = None) -> None:
        """Queue a message for the parent (dropped once `session` is cancelled)."""
        self._outbox.put(message, session)

    def run(self) -> None:
        """Serve commands until "shutdown" or until the owner closes the pipe."""
        self.send(("hello", self.server.start()))
        while True:
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
        if session is not None:
            session.cancel()
            self.server.remove_session(sid)  # ends that session's loopback responses
        self.send(("closed", sid))

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


def _exit_with_owner() -> None:
    parent = multiprocessing.parent_process()
    if parent is not None:
        wait_for_connection([parent.sentinel])
        # A crashed owner cannot close a stalled upstream connection. Close all
        # sockets now; the next helper safely reaps this owned stale session.
        os._exit(0)


def helper_main(conn: Connection, root: str) -> None:
    """Spawn entry point: arguments travel through multiprocessing IPC, not argv."""
    helper: _Helper | None = None
    try:
        # Python loggers can be silenced, but native libraries and urllib3 diagnostics may
        # write URL-bearing text straight to fd 1 or 2.
        with open(os.devnull, "wb") as discard:
            os.dup2(discard.fileno(), 1)
            os.dup2(discard.fileno(), 2)
        threading.Thread(target=_exit_with_owner, name="qitv-live-owner", daemon=True).start()
        helper = _Helper(conn, root)
        helper.run()
    except BaseException:
        # No traceback crosses IPC: it could disclose provider URLs.
        if helper is not None:
            helper.fail_all()
    finally:
        try:
            if helper is not None:
                helper.shutdown()
        finally:
            conn.close()
