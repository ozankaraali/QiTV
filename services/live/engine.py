"""The GUI process's side of the live pipeline: supervise the helper process.

`LiveEngine` lives on the GUI thread. A `_Supervisor` QThread spawns the helper
(`services.live.worker.helper_main`), sends it the commands the GUI queued, reads its
messages and relays them to the GUI thread, where they become the engine's signals and the
span lists rebuilt from the state deltas. No multiprocessing primitive is touched from the
GUI thread: it only puts commands on a `queue.Queue` and sets a `threading.Event`.

Ported from main's `TimeshiftRecorder` (services/timeshift.py): the spawn context and its
`Pipe` (looked up on the context object), credentials that travel over IPC and never in
argv, the receive loop that treats `EOFError` or `BrokenPipeError` from either `poll()` or
`recv()` as the helper's exit, the failed flag that keeps a failure the helper reported from
being reported again, and the join -> terminate -> kill escalation with 0.5 s graces.

Helper death (spec: restart once, then fall back): every session it held whose input was
still running gets one `sessionFailed(sid, "internal", ...)` (none when that session had
already ended or reported a final error), its spans are dropped, every buffer directory
handed out is removed, and the helper is restarted. A helper that sends no `hello` within
30 s counts as dead too, and so does one that cannot be started at all. On the new helper's
`hello`, the sessions the GUI has not closed are reopened with the same arguments: a fresh
directory if the old one could not be removed, RAM if the disk already failed. Then `available(True)` follows. `is_available()` is
False in between, without a signal. A second death emits `available(False)` for the rest of
the run, and every later open fails at once.

A session whose input is over (it ended, or its source failed for good) is not reopened, so
a restart never opens a second provider connection behind the GUI's back. It stays known
until `close_session`; its buffer is gone after a death, but it hears nothing about it.

Contract per sid: after `sessionEnded`, or after a final `sessionFailed` (any kind except
"disk" and "internal"), only `sessionClosed` follows. "internal" always means a reopen
follows on `available(True)`, unless `available(False)` comes instead.
`sessionFailed(sid, "disk", ...)` is the one other non-final failure: the session goes on
in RAM.

Known limits:
- Commands are sent with a blocking write, one small command at a time between reads. Only
  a helper whose command loop hangs while several KiB of commands are unread could block
  the supervisor; the helper's own sends never block (Task 7's writer thread).
- The supervisor QThread stays registered with `ThreadCleanup` for the whole run, so
  `has_pending_threads()` is True until `shutdown()`. Call `shutdown()` before waiting for
  pending threads (window close, self-update).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
import logging
import multiprocessing
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
import os
from pathlib import Path
import queue
import secrets
import threading
import time
from typing import Any

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt, QThread, Signal, Slot

from services.live.ring import cleanup_session_dir
from services.live.timeline import Span
from services.live.worker import helper_main
from services.thread_cleanup import ThreadCleanup

_LOG = logging.getLogger(__name__)
_POLL = 0.05
_IDLE = 0.25  # how long a supervisor without a helper waits for a command
_STOP_GRACE = 0.5
_TERMINATE_GRACE = 0.5
_RESTARTS = 1
_HELLO_TIMEOUT = 30.0  # a helper that never says hello counts as dead
_RAM_BYTES = 128 * 1024 * 1024  # what the helper itself falls back to when the disk fails
_SHUTDOWN_WAIT_MS = 1800  # the escalation takes at most 1.5 s
_SESSION_PREFIX = ".qitv-timeshift-"
_INTERNAL = "The live buffer stopped unexpectedly."
_UNAVAILABLE = "The live buffer is not available."

_Command = tuple[Any, ...]


def _session_dir() -> str:
    return _SESSION_PREFIX + secrets.token_hex(16)


@dataclass
class _Record:
    """The supervisor's view of a session: what reopens it and what it reported."""

    command: _Command  # ("open", sid, url, headers, verify_ssl, ring, live_delay)
    sent: bool = False  # the current helper has the session
    failed: bool = False  # a final failure was reported for the current or last helper
    over: bool = False  # its input ended or its source failed for good: never reopened
    closing: bool = False  # "close" was sent; "closed" is due


@dataclass
class _View:
    """The GUI thread's view of a session."""

    spans: deque[Span] = field(default_factory=deque)
    snapshot: dict[str, Any] = field(default_factory=dict)
    closing: bool = False


class _Supervisor(QThread):
    """Owns the helper process and its pipe; everything here runs on this thread."""

    message = Signal(object)

    def __init__(self, root: Path, commands: queue.Queue[_Command | None]) -> None:
        super().__init__()
        self._root = root
        self._commands = commands
        self._stop = threading.Event()
        self._process: BaseProcess | None = None
        self._conn: Connection | None = None
        self._up = False  # the current helper said hello
        self._spawned = 0.0  # when the current helper was started
        self._deaths = 0
        self._dead = False  # time-shift is off for this run
        self._records: dict[str, _Record] = {}
        self._dirs: set[str] = set()  # every buffer directory handed to a helper

    def request_stop(self) -> None:
        # Never signal a multiprocessing primitive from Qt: this thread stops the helper.
        self._stop.set()
        self._commands.put(None)  # wakes a supervisor that has no helper

    def run(self) -> None:
        started = False
        try:
            while not self._stop.is_set():
                try:
                    if not started:
                        started = True
                        self._start_helper()
                    elif self._process is None:
                        self._serve_without_helper()
                    else:
                        self._step()
                except Exception as error:
                    # A supervisor defect: no traceback (its locals hold provider URLs).
                    _LOG.error("The live helper supervisor failed (%s).", type(error).__name__)
                    self._stop_helper()
                    if not self._dead:
                        self._report_lost()
                        self._give_up()
        finally:
            self._stop_helper()

    # ---- with a helper ----------------------------------------------------------------
    def _step(self) -> None:
        conn, process = self._conn, self._process
        assert conn is not None and process is not None
        try:
            # One command at a time, then read. The helper's sends go through its outbox
            # thread and never block its command loop, but keeping up with them bounds the
            # pipe's backlog.
            self._send_next(conn)
            if conn.poll(0 if not self._commands.empty() else _POLL):
                self._receive(conn.recv())
                return
            alive = process.is_alive()
        except EOFError, OSError:
            # Windows can report the helper's exit from poll's PeekNamedPipe, before recv
            # has an opportunity to translate it into EOF.
            alive = False
        if alive and not self._up and time.monotonic() - self._spawned >= _HELLO_TIMEOUT:
            _LOG.warning("The live helper did not start in time.")
            alive = False
        if not alive:
            self._helper_died()

    def _send_next(self, conn: Connection) -> None:
        try:
            command = self._commands.get_nowait()
        except queue.Empty:
            return
        if command is None:
            return
        name, sid = command[0], command[1]
        if name == "open":
            opened = self._records[sid] = _Record(command)
            if self._up:
                self._send_open(conn, opened)  # otherwise on the helper's hello
            return
        record = self._records.get(sid)
        if record is None:
            self._relay(("closed", sid))
        elif record.sent:
            if not record.closing:
                record.closing = True
                conn.send(("close", sid))
        else:  # it never reached a helper
            del self._records[sid]
            self._relay(("closed", sid))

    def _send_open(self, conn: Connection, record: _Record) -> None:
        command = record.command
        ring = command[5]
        if ring["kind"] == "disk":
            if os.path.lexists(self._root / ring["dir"]):
                # A dead helper's directory could not be removed: reopen in a fresh one.
                ring = dict(ring, dir=_session_dir())
                record.command = command = (*command[:5], ring, *command[6:])
            self._dirs.add(ring["dir"])
        conn.send(command)
        record.sent = True
        record.failed = False

    def _receive(self, message: Any) -> None:
        if not isinstance(message, tuple) or len(message) < 2:
            return
        name = message[0]
        if name == "hello":
            self._up = True
            assert self._conn is not None
            for waiting in list(self._records.values()):
                if not waiting.sent and not waiting.over:
                    self._send_open(self._conn, waiting)
            self._relay(("hello", message[1]))
            return
        sid = message[1]
        record = self._records.get(sid) if isinstance(sid, str) else None
        if record is None:
            return
        if name == "closed":
            if record.closing:
                del self._records[sid]
                self._relay(("closed", sid))
                self._prune_dirs()
            return
        if record.closing:
            return  # the GUI closed it: only "closed" is still of interest
        if name == "error":
            if len(message) != 4:
                return
            if message[2] == "disk":
                # The only non-final error: the helper went on in RAM, and so does a reopen.
                ram = {"kind": "ram", "max_bytes": _RAM_BYTES}
                record.command = (*record.command[:5], ram, *record.command[6:])
            else:
                record.failed = True
                if message[2] != "internal":
                    record.over = True  # the source gave up: a reopen would just retry it
        elif name == "ended":
            record.over = True
        elif name not in ("state", "ready"):
            return
        self._relay(message)

    def _prune_dirs(self) -> None:
        """Forget the directories of closed sessions once their helper removed them."""
        in_use = {record.command[5].get("dir") for record in self._records.values()}
        for name in self._dirs - in_use:
            if not os.path.lexists(self._root / name):
                self._dirs.discard(name)

    def _drain(self) -> None:
        """Receive what a dead helper sent before it went: its failures then count."""
        conn = self._conn
        assert conn is not None
        try:
            while conn.poll(0):
                self._receive(conn.recv())
        except EOFError, OSError:
            pass

    # ---- helper lifetime --------------------------------------------------------------
    def _start_helper(self) -> None:
        """Spawn a helper; a failed start counts as a death."""
        while not self._stop.is_set() and not self._spawn():
            if not self._count_death():
                return

    def _spawn(self) -> bool:
        context = multiprocessing.get_context("spawn")
        conn = child = None
        try:
            # Through the context object, so a test can stand in Windows pipe semantics.
            conn, child = context.Pipe(duplex=True)
            # The URL and headers travel over this pipe later, never in argv.
            process = context.Process(
                target=helper_main,
                args=(child, str(self._root)),
                name="qitv-live",
                daemon=True,
            )
            process.start()
        except Exception as error:  # e.g. a PicklingError in a frozen build: a failed start
            _LOG.warning("The live helper could not be started (%s).", type(error).__name__)
            for end in (conn, child):
                if end is not None:
                    end.close()
            return False
        child.close()  # the helper's exit now shows up here as EOF
        self._process, self._conn, self._up = process, conn, False
        self._spawned = time.monotonic()
        return True

    def _helper_died(self) -> None:
        self._drain()
        process, conn = self._process, self._conn
        assert process is not None and conn is not None
        self._process = self._conn = None
        self._report_lost()
        if self._reap(process, conn, _STOP_GRACE):
            self._clean_dirs()
        if self._count_death():
            self._start_helper()

    def _report_lost(self) -> None:
        """The helper is gone: its sessions failed (once) and their buffers with it."""
        self._up = False
        self._relay(("down",))
        for sid, record in list(self._records.items()):
            if record.closing:
                del self._records[sid]
                self._relay(("closed", sid))
            elif record.sent:
                record.sent = False
                self._relay(("reset", sid))
                # Not when the helper reported it already or the input is over: an ended
                # session hears only `sessionClosed`, though its buffer is gone too.
                if not record.failed and not record.over:
                    record.failed = True
                    self._relay(("error", sid, "internal", _INTERNAL))

    def _count_death(self) -> bool:
        """True when one more helper may be started."""
        self._deaths += 1
        if self._deaths <= _RESTARTS:
            _LOG.warning("The live helper stopped unexpectedly; restarting it.")
            return True
        _LOG.warning("The live helper stopped again; time-shift is off for this run.")
        self._give_up()
        return False

    def _give_up(self) -> None:
        self._dead = True
        for sid, record in self._records.items():
            # Nothing was reported yet (it never reached a helper), and its input is not over.
            if not record.failed and not record.over:
                self._relay(("error", sid, "internal", _UNAVAILABLE))
        self._records.clear()
        self._relay(("unavailable",))

    def _serve_without_helper(self) -> None:
        try:
            command = self._commands.get(timeout=_IDLE)
        except queue.Empty:
            return
        if command is None:
            return
        if command[0] == "open":
            self._relay(("error", command[1], "internal", _UNAVAILABLE))
        else:
            self._relay(("closed", command[1]))

    def _stop_helper(self) -> None:
        process, conn = self._process, self._conn
        self._process = self._conn = None
        exited = True
        if process is not None and conn is not None:
            try:
                conn.send(("shutdown",))
            except OSError, ValueError:
                pass
            deadline = time.monotonic() + _STOP_GRACE
            # Keep reading while the helper stops, so none of its sends can block it.
            while process.is_alive() and (remaining := deadline - time.monotonic()) > 0:
                try:
                    if conn.poll(min(remaining, _POLL)):
                        conn.recv()
                except EOFError, OSError:
                    process.join(max(0.0, deadline - time.monotonic()))
                    break
            exited = self._reap(process, conn, 0)
        if exited:
            self._clean_dirs()

    def _reap(self, process: BaseProcess, conn: Connection, grace: float) -> bool:
        """Join, then terminate, then kill (ported escalation); True once it exited."""
        process.join(grace)
        if process.is_alive():
            process.terminate()
            process.join(_TERMINATE_GRACE)
        if process.is_alive():
            process.kill()
            process.join(_TERMINATE_GRACE)
        exited = not process.is_alive()
        conn.close()
        if exited:
            process.close()
        else:
            _LOG.warning("The live helper could not be stopped.")
        return exited

    def _clean_dirs(self) -> None:
        """Only after the helper exited: a live owner's directory is left untouched."""
        for name in sorted(self._dirs):
            try:
                removed = cleanup_session_dir(self._root, name)
            except OSError as error:
                _LOG.warning(
                    "A temporary buffer directory could not be removed (%s).",
                    type(error).__name__,
                )
                continue
            if removed:
                self._dirs.discard(name)

    def _relay(self, message: tuple[Any, ...]) -> None:
        if not self._stop.is_set():  # nothing is reported once the engine shuts down
            self.message.emit(message)


class LiveEngine(QObject):
    """Live sessions in the helper process, driven from the GUI thread.

    Signals are delivered on the GUI thread. `sessionFailed(sid, kind, message)` carries a
    fixed message; kind "disk" is a notice (the session continues in RAM) and
    "internal" (a helper death) is followed by a reopen on `available(True)`, or by
    `available(False)`; every other kind ends the session's input. After that or
    `sessionEnded`, only `sessionClosed` follows. `sessionClosed` answers `close_session`.
    """

    available = Signal(bool)
    sessionReady = Signal(str)
    sessionUpdated = Signal(str, dict)
    sessionFailed = Signal(str, str, str)
    sessionEnded = Signal(str)
    sessionClosed = Signal(str)

    def __init__(self, root_dir: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._root = Path(root_dir)
        self._commands: queue.Queue[_Command | None] = queue.Queue()
        self._sessions: dict[str, _View] = {}
        self._base_url = ""
        self._available = False
        self._started = False
        self._shut = False
        self._supervisor: _Supervisor | None = None
        self._cleanup: ThreadCleanup | None = None

    def start(self) -> None:
        """Spawn the helper in the background; `available(True)` follows its hello."""
        if self._started or self._shut:
            return
        self._started = True
        supervisor = _Supervisor(self._root, self._commands)
        supervisor.message.connect(self._on_message, Qt.ConnectionType.QueuedConnection)
        cleanup = ThreadCleanup(supervisor)
        cleanup.finished.connect(self._on_retired)
        self._supervisor, self._cleanup = supervisor, cleanup
        supervisor.start()

    def is_available(self) -> bool:
        return self._available

    def open_session(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        verify_ssl: bool,
        disk: bool,
        max_bytes: int,
        live_delay: float = 6.0,
    ) -> str:
        sid = secrets.token_hex(8)
        if self._shut:
            return sid
        ring: dict[str, Any] = {"kind": "ram", "max_bytes": int(max_bytes)}
        if disk:
            ring = {"kind": "disk", "dir": _session_dir(), "max_bytes": int(max_bytes)}
        self._sessions[sid] = _View()
        command = ("open", sid, url, dict(headers), bool(verify_ssl), ring, float(live_delay))
        self._commands.put(command)
        return sid

    def close_session(self, sid: str) -> None:
        view = self._sessions.get(sid)
        if view is None or view.closing or self._shut:
            return
        view.closing = True
        self._commands.put(("close", sid))

    def spans(self, sid: str) -> list[Span]:
        view = self._sessions.get(sid)
        return list(view.spans) if view is not None else []

    def snapshot(self, sid: str) -> dict[str, Any]:
        view = self._sessions.get(sid)
        return dict(view.snapshot) if view is not None else {}

    def stream_url(self, sid: str, seq: int) -> str:
        """The loopback URL streaming from segment `seq`; "" while no helper is up."""
        if not self._base_url:
            return ""
        return f"{self._base_url}/{sid}/stream/{seq}.ts"

    def shutdown(self) -> None:
        """Stop the helper (bounded) and remove its buffers; no signal follows."""
        if self._shut:
            return
        self._shut = True
        self._available = False
        self._base_url = ""
        self._sessions.clear()
        supervisor, cleanup = self._supervisor, self._cleanup
        if supervisor is None:
            return
        supervisor.request_stop()
        if not supervisor.wait(_SHUTDOWN_WAIT_MS):
            _LOG.warning("The live helper is still stopping.")
            return
        if cleanup is not None:
            # Run ThreadCleanup's queued check now, so no pending thread outlives shutdown.
            QCoreApplication.sendPostedEvents(cleanup, QEvent.Type.MetaCall)

    @Slot(object)
    def _on_retired(self, _thread: object) -> None:
        self._supervisor = self._cleanup = None

    @Slot(object)
    def _on_message(self, message: tuple[Any, ...]) -> None:
        if self._shut:
            return
        name = message[0]
        if name == "hello":
            self._base_url, self._available = message[1], True
            self.available.emit(True)
            return
        if name in ("down", "unavailable"):
            self._base_url, self._available = "", False
            if name == "unavailable":
                self.available.emit(False)
            return
        sid = message[1]
        view = self._sessions.get(sid)
        if view is None:
            return
        if name == "closed":
            del self._sessions[sid]
            self.sessionClosed.emit(sid)
        elif view.closing:
            return
        elif name == "state":
            _, _, snapshot, first, additions = message
            # Ported from main: drop spans older than `first`, append the additions.
            spans = view.spans
            if first is None:
                spans.clear()
            else:
                while spans and spans[0].seq < first:
                    spans.popleft()
                spans.extend(Span(*item) for item in additions if item[0] >= first)
            view.snapshot = dict(snapshot)
            self.sessionUpdated.emit(sid, dict(snapshot))
        elif name == "reset":  # the helper holding this buffer died
            view.spans.clear()
            view.snapshot = {}
        elif name == "ready":
            self.sessionReady.emit(sid)
        elif name == "error":
            self.sessionFailed.emit(sid, message[2], message[3])
        elif name == "ended":
            self.sessionEnded.emit(sid)
