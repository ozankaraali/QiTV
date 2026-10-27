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

Before its first helper, the supervisor removes the buffer directories that a crashed run
left in the root (live sessions of another instance keep their locks and stay).

Helper death (spec: restart once, then fall back): `is_available()` turns False (no
signal), then every session it held whose input was still running gets one
`sessionFailed(sid, "internal", ...)` (none when that session had already ended or reported
a final error), its spans are dropped, every buffer directory handed out is removed, and the
helper is restarted. A helper that sends no `hello` within 30 s counts as dead too, and so
does one that cannot be started at all. On the new helper's `hello`, the sessions the GUI has
not closed are reopened with the same arguments: a fresh directory if the old one could not
be removed, RAM if the disk already failed. Then `available(True)` follows. A second death,
or a dead helper that cannot be stopped (it may still hold its upstream connections), emits
`available(False)` for the rest of the run, and every later open fails at once.

A session whose input is over (it ended, or it failed for good) is not reopened, so a
restart never opens a second provider connection behind the GUI's back. It stays known
until `close_session`; its buffer is gone after a death, but it hears nothing about it.

`sessionFailed(sid, kind, message)` carries a fixed message (a final failure is logged by its
kind and the HTTP status the helper sends after the message, 2.1: why a channel plays
directly); each kind has one meaning:
- "disk": a notice, the only non-final kind. The disk buffer failed; the session goes on in
  RAM, and so does a reopen. (2.8: one resumed once, a kept buffer, parks with its disk buffer
  as it stands instead, and a new helper takes that back after a death.)
- "unsupported", "network", "tls", "timeout", "http": the input ended for good (the stream
  cannot be time-shifted, or the source gave up). Final; the buffer stays playable.
- "crashed": the helper failed this session unexpectedly while staying up. Final.
- "internal": only from the engine, only for a helper death, and only after it went down
  (`is_available()` is already False). The session is reopened under the same sid once
  `available(True)` follows; otherwise `available(False)` follows.
- "unavailable": time-shift is off for this run, after `available(False)` was emitted: an
  open after that, or a session that had not reached a helper yet. Final.
After `sessionEnded` or a final failure, only `sessionClosed` follows; `sessionClosed` always
answers `close_session`.

Recording (2.2): `start_recording(sid, path, seq)` has the helper write the session's buffer
to a file from segment `seq` on, following the live edge (no second connection: the helper's
recorder reads the ring), until `stop_recording(sid)`, the session's close, its input's end or
a limit. It returns the recording's id, and each is answered by one `recordingEnded(rid,
reason, path, written)`, before or after the session's `sessionClosed` (in no set order: a close
never waits for its recording): `reason` is the helper's
(`services.live.recorder.REASONS`), or the engine's "lost" when the helper died (or time-shift
went off) before it said; then `path` is the partial file left behind as the helper last told
it ("" while it had made none) and `written` its size then (-1 when not known). A start for a
session no helper holds now is answered "failed". Nothing follows `shutdown()`.

Known limits:
- Commands are sent with a blocking write, one small command at a time between reads. Only
  a helper whose command loop hangs while several KiB of commands are unread could block
  the supervisor; the helper's own sends never block (a writer thread sends them).
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

from PySide6.QtCore import QCoreApplication, QEvent, QLockFile, QObject, Qt, QThread, Signal, Slot

from services.live.ring import (
    STALE_S,
    Index,
    cleanup_session_dir,
    lock_manager,
    read_index,
    remove_stale_sessions,
)
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
_RAM_BYTES = 128 * 1024 * 1024  # a disk ring's memory fallback when it names none
_SHUTDOWN_WAIT_MS = 1800  # the escalation takes at most 1.5 s
_SESSION_PREFIX = ".qitv-buffer-"  # 2.6: a v2 folder, kept past the helper (services.live.ring)
# 2.8: how long a scan of the kept folders waits for the buffers' manager lock (another QiTV's
# helper holds it while it makes a folder, briefly)
_MANAGER_WAIT_S = 2.0
_INTERNAL = "The live buffer stopped unexpectedly."
_UNAVAILABLE = "The live buffer is not available."
_FAILED, _LOST = "failed", "lost"  # a recording that could not start, or whose helper died
# "internal" and "unavailable" are the engine's own kinds (see the module docstring).

_Command = tuple[Any, ...]


def _session_dir() -> str:
    return _SESSION_PREFIX + secrets.token_hex(16)


def _fallback_bytes(ring: dict[str, Any]) -> int:
    """The memory a disk ring goes on in when its disk fails (as the helper sizes it)."""
    size = ring.get("ram_bytes")
    return size if isinstance(size, int) and size > 0 else _RAM_BYTES


def _window_part(command: _Command) -> bool:
    """A server part's open (2.7): its start dict names a window."""
    return len(command) > 7 and isinstance(command[7], dict) and "window" in command[7]


@dataclass
class _Record:
    """The supervisor's view of a session: what reopens it and what it reported."""

    command: _Command  # ("open", sid, url, headers, verify_ssl, ring, live_delay, start)
    sent: bool = False  # the current helper has the session
    failed: bool = False  # a final failure was reported for the current or last helper
    over: bool = False  # its input ended or its source failed for good: never reopened
    closing: bool = False  # "close" was sent; "closed" is due
    # 2.6: parked (its upstream closed, its buffer kept): a new helper takes its folder back
    # ("adopt") instead of opening it again; also a kept folder found at the start.
    parked: bool = False
    label: dict | None = None  # 2.6: "keep", also sent after a (re)open or a take-back
    where: dict | None = None  # 2.6: "where", likewise
    resume: _Command | None = None  # 2.6: a resume that came before its take-back was sent
    # 2.8: resumed once (a kept buffer): the helper parks it with its disk ring as it stands
    # when the disk fails, never going on in RAM, so its record keeps the disk spec.
    history: bool = False


@dataclass
class _View:
    """The GUI thread's view of a session."""

    spans: deque[Span] = field(default_factory=deque)
    snapshot: dict[str, Any] = field(default_factory=dict)
    closing: bool = False


def _helper_args(child: Any, root: Path) -> tuple[Any, str, bool]:
    """A helper's start arguments: its pipe, its buffers' folder and whether it forwards its
    debug lines too (2.8.2: while the detailed log is on, QiTV's own loggers are at DEBUG)."""
    return child, str(root), logging.getLogger("services").isEnabledFor(logging.DEBUG)


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
                        self._sweep()
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
        if command[0] == "adopt_kept":  # 2.6
            self._adopt_kept(int(command[1]))
            return
        if command[0] == "drop_kept":
            self._drop_kept()
            return
        if command[0] == "disk_amount":  # 2.8
            self._disk_amount(int(command[1]))
            if self._up:
                conn.send(command)  # otherwise its next disk open brings it
            return
        name, sid = command[0], command[1]
        if name == "open":
            opened = self._records[sid] = _Record(command)
            if self._up:
                self._send_open(conn, opened)  # otherwise on the helper's hello
            return
        record = self._records.get(sid)
        if name == "resize":
            if record is None or record.closing or record.command[5].get("kind") != "ram":
                return  # a disk session keeps the user's size; a closed one is gone
            ring = record.command[5]
            # A reopen after a helper's death takes the new size too.
            resized = dict(ring, max_bytes=command[2])
            record.command = (*record.command[:5], resized, *record.command[6:])
            if record.sent:
                conn.send(command)
            return
        if name == "park":  # 2.6: a parked session is never reopened after a helper death
            if record is not None and record.sent and not record.closing:
                record.over = True
                conn.send(command)
            elif record is not None and not record.closing:
                record.over = True
                self._relay(("parked", sid))  # no helper holds an upstream for it
            return
        if name == "resume":
            if record is None or record.closing:
                return
            if not record.sent and record.parked:  # its take-back waits for the helper
                record.resume = command
                return
            if not record.sent:  # its buffer went with a helper: nothing to resume
                self._relay(("error", sid, "crashed", _INTERNAL))
                return
            self._forward_resume(conn, record, command)
            return
        if name in ("keep", "where"):  # 2.6: a disk buffer's label, where the user was
            if record is None or record.closing:
                return
            if name == "keep":
                record.label = dict(command[2])
            else:
                record.where = dict(command[2])
            if record.sent:  # else after its open (`_send_notes`)
                conn.send(command)
            return
        if name == "until":  # 2.7: a server part's stop (never the close fallback below)
            if record is not None and record.sent and not record.closing:
                conn.send(command)
            return
        if name in ("record_start", "record_stop"):
            if record is not None and record.sent and not record.closing:
                conn.send(command)
            elif name == "record_start":  # no helper holds it now: nothing can be recorded
                self._relay(("recorded", sid, command[2], _FAILED, "", 0))
            return
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
        if record.parked:  # 2.6: its folder is taken back, no upstream opens
            sid = command[1]
            if ring.get("kind") != "disk" or not os.path.lexists(self._root / ring["dir"]):
                del self._records[sid]  # a memory buffer went with the helper
                self._relay(("unkept", sid))
                return
            self._dirs.add(ring["dir"])
            conn.send(("adopt", sid, ring["dir"], int(ring["max_bytes"])))
            record.sent = True
            record.failed = False
            self._send_notes(conn, record)
            resume, record.resume = record.resume, None
            if resume is not None:  # the tune that took it back came first
                self._forward_resume(conn, record, resume)
            return
        if ring["kind"] == "disk":
            if os.path.lexists(self._root / ring["dir"]):
                # A dead helper's directory could not be removed: reopen in a fresh one.
                ring = dict(ring, dir=_session_dir())
                record.command = command = (*command[:5], ring, *command[6:])
            self._dirs.add(ring["dir"])
        conn.send(command)
        record.sent = True
        record.failed = False
        self._send_notes(conn, record)

    def _send_notes(self, conn: Connection, record: _Record) -> None:
        """2.6: a disk session's label and where the user was, after its (re)open."""
        sid = record.command[1]
        if record.label is not None:
            conn.send(("keep", sid, record.label))
        if record.where is not None:
            conn.send(("where", sid, record.where))

    def _forward_resume(self, conn: Connection, record: _Record, command: _Command) -> None:
        """Send a resume; a helper's death after it reopens with the new source."""
        _, sid, url, headers, verify_ssl, live_delay, *start = command
        ring = record.command[5]
        record.command = ("open", sid, url, headers, verify_ssl, ring, live_delay, *start)
        record.over = record.failed = record.parked = False
        record.history = True
        conn.send(command)

    def _receive(self, message: Any) -> None:
        if not isinstance(message, tuple) or len(message) < 2:
            return
        name = message[0]
        if name == "hello":
            self._up = True
            assert self._conn is not None
            for waiting in list(self._records.values()):
                if not waiting.sent and (not waiting.over or waiting.parked):
                    self._send_open(self._conn, waiting)
            self._relay(("hello", message[1]))
            return
        if name == "log":  # 2.8.1: the helper's own line, into the app's log
            if (
                len(message) == 4
                and type(message[1]) is int
                and isinstance(message[2], str)
                and message[2].startswith("services.live.")
                and isinstance(message[3], str)
            ):
                short = message[2].rsplit(".", 1)[-1]
                logging.getLogger(f"services.live.helper.{short}").log(
                    message[1], "%s", message[3][:500]
                )
            return
        sid = message[1]
        if name in ("recorded", "recording"):
            # A recording outlives its session's close: relayed whatever the record says.
            if isinstance(sid, str) and len(message) == (6 if name == "recorded" else 5):
                self._relay(message)
            return
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
            if len(message) not in (4, 5):  # an HTTP failure's status last (2.1)
                return
            if message[2] == "disk" and not record.history:
                # The only non-final error: the helper went on in RAM, and so does a reopen.
                ram = {"kind": "ram", "max_bytes": _fallback_bytes(record.command[5])}
                record.command = (*record.command[:5], ram, *record.command[6:])
            elif message[2] == "disk":
                pass  # 2.8: a kept buffer's: parked with its disk ring, which a death keeps
            else:
                # Final, "crashed" included: a reopen would only retry what failed.
                record.failed = record.over = True
                # Why the channel will play directly: its kind and HTTP status, never a URL;
                # 2.8.1: and how it failed (the helper's fixed texts: refused, unanswered, …).
                status = message[4] if len(message) == 5 else None
                text = message[3] if isinstance(message[3], str) and message[2] != "http" else ""
                _LOG.info(
                    "A live session's input failed for good: %s%s%s.",
                    message[2],
                    f", HTTP {status}" if isinstance(status, int) else "",
                    f" ({text[:200].rstrip('.')}.)" if text else "",
                )
        elif name == "ended":
            record.over = True
        elif name == "parked":
            record.over = record.parked = True
        elif name == "resumed":
            record.over = record.parked = False
        elif name == "unkept":  # 2.6: a kept folder could not be taken back
            del self._records[sid]
            ring = record.command[5]
            if ring.get("dir"):
                self._dirs.discard(ring["dir"])
        elif name == "kept":
            if len(message) != 4:
                return
        elif name not in ("state", "ready"):
            return
        self._relay(message)

    # ---- 2.6: kept folders ----------------------------------------------------------------
    def _disk_amount(self, max_bytes: int) -> None:
        """2.8: the user's disk amount changed: a reopen after a helper's death takes it too."""
        for record in self._records.values():
            ring = record.command[5]
            if ring.get("kind") == "disk":
                resized = dict(ring, max_bytes=max_bytes)
                record.command = (*record.command[:5], resized, *record.command[6:])

    def _scanning(self) -> Any:
        """The buffers' manager lock for a scan of the kept folders (2.8): another QiTV's
        helper holds it while it makes a folder, so the scan never takes one half made (no
        lock, no index yet) for one to remove. None (nothing scanned now) while it stays
        held, or without a root."""
        if not self._root.is_dir():
            return None
        manager = lock_manager(self._root, _MANAGER_WAIT_S)
        if manager is None:
            _LOG.info("The kept buffers were not looked at: another QiTV is making one.")
        return manager

    def _adopt_kept(self, max_bytes: int) -> None:
        """Take back the kept folders no helper holds (at the start, and when a new engine
        starts): a fresh labelled one becomes a parked session ("adopting" says its id first);
        one over a day old, unlabelled or unreadable is removed; one another process holds is
        left alone. Under the manager lock (2.8). Runs on this thread: never the GUI's."""
        manager = self._scanning()
        if manager is None:
            return
        try:
            self._adopt_kept_locked(max_bytes)
        finally:
            manager.unlock()

    def _adopt_kept_locked(self, max_bytes: int) -> None:
        now = time.time()
        found: dict[str, tuple[float, str, Index]] = {}  # channel -> its newest folder
        for name in self._kept_names():
            directory = self._root / name
            if not self._lockable(directory):
                continue  # another QiTV holds it
            index = read_index(directory)
            newest = max((r["w"] for r in index.segments), default=0.0) if index else 0.0
            channel = index.label.get("channel") if index and index.label else None
            if index is None or not isinstance(channel, str) or newest < now - STALE_S:
                self._remove(name)
                continue
            other = found.get(channel)
            if other is not None:  # one channel, two folders (a helper's death): the newer
                older = name if other[0] >= newest else other[1]
                self._remove(older)
                if older == other[1]:
                    found[channel] = (newest, name, index)
                continue
            found[channel] = (newest, name, index)
        for _newest, name, index in found.values():
            sid = secrets.token_hex(8)
            ring = {"kind": "disk", "dir": name, "max_bytes": int(max_bytes)}
            command: _Command = ("open", sid, "", {}, True, ring, 6.0)
            record = self._records[sid] = _Record(command, over=True, parked=True)
            # Told at once, with whose and where (a startup tune of it finds it).
            self._relay(("adopting", sid, index.label or {}, index.where or {}))
            if self._up and self._conn is not None:
                self._send_open(self._conn, record)

    def _drop_kept(self) -> None:
        """The disk buffer is off: every kept folder no helper holds goes (under the manager
        lock, 2.8; it needs no helper)."""
        manager = self._scanning()
        if manager is None:
            return
        try:
            for name in self._kept_names():
                self._remove(name)
        finally:
            manager.unlock()

    def _kept_names(self) -> list[str]:
        if not self._root.is_dir():
            return []
        held = set(self._dirs)
        try:
            return sorted(
                p.name
                for p in self._root.iterdir()
                if p.name.startswith(_SESSION_PREFIX) and p.name not in held
            )
        except OSError:
            return []

    @staticmethod
    def _lockable(directory: Path) -> bool:
        lock = QLockFile(str(directory / "session.lock"))
        lock.setStaleLockTime(0)
        if not lock.tryLock(0):
            return False
        lock.unlock()
        return True

    def _remove(self, name: str) -> None:
        try:
            cleanup_session_dir(self._root, name)
        except OSError as error:
            _LOG.warning("A kept buffer could not be removed (%s).", type(error).__name__)

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
    def _sweep(self) -> None:
        """Once, before the first helper: remove the buffers a crashed run left behind."""
        try:
            remove_stale_sessions(self._root)
        except OSError as error:
            _LOG.warning("Old temporary buffers could not be removed (%s).", type(error).__name__)

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
                args=_helper_args(child, self._root),
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
        if not self._reap(process, conn, _STOP_GRACE):
            # It may still hold its upstream connections: a reopen would add a second one.
            _LOG.warning("The dead live helper is still running; time-shift is off for this run.")
            self._give_up()
            return
        self._clean_dirs(died=True)
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
                if _window_part(record.command):
                    record.over = True  # 2.7: a server part is opened anew by its session

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
        self._relay(("unavailable",))  # first: "unavailable" failures follow available(False)
        for sid, record in self._records.items():
            # Nothing was reported yet (it never reached a helper), and its input is not over.
            if not record.failed and not record.over:
                self._relay(("error", sid, "unavailable", _UNAVAILABLE))
        self._records.clear()

    def _serve_without_helper(self) -> None:
        try:
            command = self._commands.get(timeout=_IDLE)
        except queue.Empty:
            return
        if command is None:
            return
        if command[0] == "drop_kept":
            self._drop_kept()  # 2.8: the disk buffer turned off needs no helper
        elif command[0] in ("adopt_kept", "disk_amount"):
            return  # 2.8: no helper takes anything back or holds a disk ring
        elif command[0] == "open":
            # Time-shift is off: available(False) was emitted already, and no reopen follows.
            self._relay(("error", command[1], "unavailable", _UNAVAILABLE))
        elif command[0] == "record_start":
            self._relay(("recorded", command[1], command[2], _FAILED, "", 0))
        elif command[0] != "record_stop":
            self._relay(("closed", command[1]))

    def _stop_helper(self) -> None:
        process, conn = self._process, self._conn
        self._process = self._conn = None
        exited = True
        if process is not None and conn is not None:
            try:
                self._flush_notes(conn)  # 2.6: where the user was, parks, closes: first
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

    def _flush_notes(self, conn: Connection) -> None:
        """At a stop, the queued notes, parks and closes go to the helper before "shutdown"
        (2.6: the app notes where the user was and parks as it quits, in the same turn); an
        open, a resume or anything else that would reach a provider is dropped."""
        while True:
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                return
            if command is None or command[0] not in ("keep", "where", "park", "close"):
                continue
            record = self._records.get(command[1])
            if record is not None and record.sent and not record.closing:
                if command[0] == "close":
                    record.closing = True
                conn.send(command)

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

    def _clean_dirs(self, *, died: bool = False) -> None:
        """Only after the helper exited: a live owner's directory is left untouched. A kept
        buffer's (v2) stays for the next helper (2.6): every labelled one at a stop; after a
        death only a parked session's, which the next helper takes back (a live one opens
        again in a fresh folder, as before 2.6)."""
        parked = {r.command[5].get("dir") for r in self._records.values() if r.parked}
        for name in sorted(self._dirs):
            if name.startswith(_SESSION_PREFIX) and (not died or name in parked):
                index = read_index(self._root / name)
                if index is not None and index.label is not None:
                    self._dirs.discard(name)  # kept for the next helper
                    continue
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
    fixed message, and each kind has one meaning (module docstring): "disk" is a notice (the
    session continues in RAM); "internal" is a helper death, after `is_available()` turned
    False, and is followed by a reopen on `available(True)` or by `available(False)`; every
    other kind ("unsupported", "network", "tls", "timeout", "http", "crashed",
    "unavailable") is final. After a final failure or `sessionEnded`, only `sessionClosed`
    follows. `sessionClosed` answers `close_session`.
    """

    available = Signal(bool)
    sessionReady = Signal(str)
    sessionUpdated = Signal(str, dict)
    sessionFailed = Signal(str, str, str)
    sessionEnded = Signal(str)
    sessionClosed = Signal(str)
    # (rid, reason, path, written): a recording ended (see the module docstring)
    recordingEnded = Signal(str, str, str, int)
    sessionParked = Signal(str)  # 2.6: its upstream is closed, its buffer kept and served
    # 2.6: a kept folder taken back as a parked session: (sid, label, where)
    keptFound = Signal(str, dict, dict)
    sessionResumed = Signal(str)  # 2.6: a resumed session's new run has its first segment
    # 2.8: a kept (parked) session could not be taken back by a new helper: it is gone (its
    # `sessionFailed` follows); the kept buffers' registry lets it go
    keptLost = Signal(str)

    def __init__(self, root_dir: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._root = Path(root_dir)
        self._commands: queue.Queue[_Command | None] = queue.Queue()
        self._sessions: dict[str, _View] = {}
        # rid -> (its file, its size) as the helper last told them ("", -1 before), until it
        # ends
        self._recordings: dict[str, tuple[str, int]] = {}
        self._writes: dict[str, str] = {}  # rid -> the file it writes, from its start (2.8)
        # The files recordings write now (2.8): the Library keeps them from deletion, a phone
        # reads one on as it grows. Replaced, never changed: other threads read it.
        self.writing: frozenset[str] = frozenset()
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

    @property
    def root_dir(self) -> Path:
        """Where the disk buffers live (the helper's root)."""
        return self._root

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
        ram_bytes: int | None = None,
        target: float | None = None,
        extra: float = 0.0,
        window: Mapping[str, Any] | None = None,
        https_first: bool = False,
        max_height: int = 0,
    ) -> str:
        """`ram_bytes`: a disk session's memory if its disk fails (_RAM_BYTES when None).
        `target` and `extra`: the target the session steers to and what it adds to it (Auto's
        learned extra), so an HLS source starts far enough back for it (2.1); without
        `target`, `live_delay` seconds back. `window` (2.7): a server part of the HLS
        playlist, `{"back_s", "until_seq", "ahead_s"}` (`services.live.source.HlsSource`);
        never reopened after a helper's death. `https_first` (2.7.1): an http:// address is
        asked over HTTPS first (`open_source`). `max_height` (2.8, the live ⚙): an HLS master
        plays its best variant of at most that many lines (0: its best)."""
        sid = secrets.token_hex(8)
        if self._shut:
            return sid
        ring: dict[str, Any] = {"kind": "ram", "max_bytes": int(max_bytes)}
        if disk:
            ring = {"kind": "disk", "dir": _session_dir(), "max_bytes": int(max_bytes)}
            if ram_bytes is not None:
                ring["ram_bytes"] = int(ram_bytes)
        self._sessions[sid] = _View()
        command: _Command = (
            "open",
            sid,
            url,
            dict(headers),
            bool(verify_ssl),
            ring,
            float(live_delay),
        )
        start: dict[str, Any] = {}
        if target is not None:
            start.update(target=float(target), extra=float(extra))
        if window is not None:
            start["window"] = dict(window)
        if https_first:
            start["https"] = True
        if max_height > 0:
            start["max_height"] = int(max_height)
        if start:
            command = (*command, start)
        self._commands.put(command)
        return sid

    def close_session(self, sid: str) -> None:
        view = self._sessions.get(sid)
        if view is None or view.closing or self._shut:
            return
        view.closing = True
        self._commands.put(("close", sid))

    def park_session(self, sid: str) -> None:
        """Close the session's upstream and keep its buffer (2.6); `sessionParked` follows.
        Its spans and state stay until it is closed."""
        view = self._sessions.get(sid)
        if view is None or view.closing or self._shut:
            return
        self._commands.put(("park", sid))

    def resume_session(
        self,
        sid: str,
        url: str,
        *,
        headers: Mapping[str, str],
        verify_ssl: bool,
        live_delay: float = 6.0,
        target: float | None = None,
        extra: float = 0.0,
        https_first: bool = False,
        max_height: int = 0,
        join: bool = False,
    ) -> None:
        """A new upstream for a parked session (2.6): `sessionResumed` once its first
        segment came, else `sessionFailed` (it stays parked). The arguments as for
        `open_session`; `join` (2.8): an HLS run goes on after the earlier run's last segment
        (a rewound viewer's: nothing plays twice where they join)."""
        view = self._sessions.get(sid)
        if view is None or view.closing or self._shut:
            return
        command: _Command = ("resume", sid, url, dict(headers), bool(verify_ssl), float(live_delay))
        start: dict[str, Any] = {}
        if target is not None:
            start.update(target=float(target), extra=float(extra))
        if https_first:
            start["https"] = True
        if max_height > 0:
            start["max_height"] = int(max_height)
        if join:
            start["join"] = True
        if start:
            command = (*command, start)
        self._commands.put(command)

    def holds(self, sid: str) -> bool:
        """The engine knows session `sid` (2.6: a kept one still being taken back too)."""
        return sid in self._sessions

    def adopt_kept(self, max_bytes: int) -> None:
        """Take back the kept buffers a helper left (2.6): each comes as `keptFound`."""
        if not self._shut:
            self._commands.put(("adopt_kept", int(max_bytes)))

    def drop_kept(self) -> None:
        """Remove the kept buffers no session holds (2.6: the disk buffer was turned off)."""
        if not self._shut:
            self._commands.put(("drop_kept",))

    def set_disk_amount(self, max_bytes: int) -> None:
        """The user's disk amount changed (2.8): every disk buffer holds it at once (a kept one
        gives way first), not only from the next disk open."""
        if not self._shut and max_bytes > 0:
            self._commands.put(("disk_amount", int(max_bytes)))

    def set_until(self, sid: str, seq: int) -> None:
        """Move a server part's stop to media sequence `seq` (2.7: the live buffer's start
        moved, eviction): its input ends before it."""
        view = self._sessions.get(sid)
        if view is not None and not view.closing and not self._shut:
            self._commands.put(("until", sid, int(seq)))

    def keep_session(self, sid: str, label: Mapping[str, Any]) -> None:
        """Label a disk session's buffer (2.6: the channel's key), so a later helper knows
        whose it is."""
        view = self._sessions.get(sid)
        if view is not None and not view.closing and not self._shut:
            self._commands.put(("keep", sid, dict(label)))

    def note_where(self, sid: str, where: Mapping[str, Any]) -> None:
        """Note where the user was in a disk session's buffer (2.6), for a later helper."""
        view = self._sessions.get(sid)
        if view is not None and not view.closing and not self._shut:
            self._commands.put(("where", sid, dict(where)))

    def resize_session(self, sid: str, max_bytes: int) -> None:
        """A RAM session holds up to `max_bytes` from now on (the multiview: the focused
        tile's buffer grows, the others' shrink), also when a helper restart reopens it. A
        disk session keeps the user's size."""
        view = self._sessions.get(sid)
        if view is None or view.closing or self._shut or max_bytes <= 0:
            return
        self._commands.put(("resize", sid, int(max_bytes)))

    def start_recording(self, sid: str, path: str, seq: int, *, max_s: float, min_free: int) -> str:
        """Record session `sid` to a new file at `path` from segment `seq` on (the module
        docstring); `max_s` and `min_free` stop it after that long or once the disk holding
        it has less free. The recording's id; "" (nothing asked, nothing follows) for a
        session this engine does not hold open. Stop the session's recording first: the
        helper records one at a time per session."""
        view = self._sessions.get(sid)
        if view is None or view.closing or self._shut:
            return ""
        rid = secrets.token_hex(6)
        self._recordings[rid] = ("", -1)
        self._writes[rid] = path
        self.writing = frozenset(self._writes.values())
        limits = {"max_s": float(max_s), "min_free": int(min_free)}
        self._commands.put(("record_start", sid, rid, path, int(seq), limits))
        return rid

    def stop_recording(self, sid: str) -> None:
        """End session `sid`'s recording at the live edge; `recordingEnded` follows."""
        if sid in self._sessions and not self._shut:
            self._commands.put(("record_stop", sid))

    def spans(self, sid: str) -> list[Span]:
        """The session's closed segments, oldest first; each says how cleanly playback starts
        at it (`Span.start_quality`)."""
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
        self._recordings.clear()  # the helper's shutdown ends them; no signal follows
        self._wrote(*self._writes)
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

    def _wrote(self, *rids: str) -> None:
        """Recordings `rids` write no more."""
        for rid in rids:
            self._writes.pop(rid, None)
        self.writing = frozenset(self._writes.values())

    def _lose_recordings(self) -> None:
        recordings, self._recordings = self._recordings, {}
        self._wrote(*recordings)
        for rid, (path, written) in recordings.items():
            self.recordingEnded.emit(rid, _LOST, path, written)

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
            self._lose_recordings()  # what the helper had not said ended with it
            if name == "unavailable":
                self.available.emit(False)
            return
        sid = message[1]
        if name == "adopting":  # 2.6: a kept folder is taken back as this session
            self._sessions[sid] = _View()
            if len(message) == 4:  # told at once: a startup tune of it finds it
                self.keptFound.emit(sid, dict(message[2] or {}), dict(message[3] or {}))
            return
        if name == "recording":
            _, _, rid, path, written = message
            if rid in self._recordings:
                self._recordings[rid] = (str(path), int(written))
            return
        if name == "recorded":
            _, _, rid, reason, path, written = message
            if rid in self._recordings:
                del self._recordings[rid]
                self._wrote(str(rid))
                self.recordingEnded.emit(str(rid), str(reason), str(path), int(written))
            return
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
            # Ported from main: drop spans older than `first`, append the additions. An
            # addition without a start quality (four fields) starts on a keyframe, as before.
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
        elif name == "parked":
            self.sessionParked.emit(sid)
        elif name == "kept":
            pass  # taken back (it was told at "adopting")
        elif name == "unkept":
            del self._sessions[sid]
            self.keptLost.emit(sid)  # 2.8: the kept buffers' registry lets it go
            self.sessionFailed.emit(sid, "crashed", _INTERNAL)  # a tune of it starts afresh
        elif name == "resumed":
            self.sessionResumed.emit(sid)
        elif name == "error":
            self.sessionFailed.emit(sid, message[2], message[3])
        elif name == "ended":
            self.sessionEnded.emit(sid)
