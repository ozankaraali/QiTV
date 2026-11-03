"""The GUI's side of the captions process (2.9, AI captions): start it, talk to it, hear it.

Like the time-shift helper (services.live.engine): a "spawn" child (frozen builds stop it at
main.py's freeze_support), a duplex Pipe, and a supervisor QThread that sends what the GUI
queued and relays what the child says as a Qt signal, queued to the GUI thread: the GUI never
waits on the pipe. A child that dies is reported (`failed`), not restarted: the controller
decides.
"""

from __future__ import annotations

import logging
import multiprocessing
import queue
from typing import Any, Optional

from PySide6.QtCore import QObject, Qt, QThread, Signal

logger = logging.getLogger(__name__)

_POLL_S = 0.2
_QUIT_GRACE_S = 3.0


class _Supervisor(QThread):
    message = Signal(int, object)  # its serial, what the child said

    def __init__(
        self, conn: Any, process: Any, outbox: "queue.Queue[Optional[tuple]]", serial: int = 0
    ) -> None:
        super().__init__()
        self._serial = serial
        self._conn = conn
        self._process = process
        self._outbox = outbox

    def run(self) -> None:
        quitting = False
        while True:
            try:
                while True:
                    item = self._outbox.get_nowait()
                    if item is None:
                        quitting = True
                        self._send(("quit",))
                        break
                    self._send(item)
            except queue.Empty:
                pass
            if quitting:
                break
            try:
                if self._conn.poll(_POLL_S):
                    self.message.emit(self._serial, self._conn.recv())
                    continue
            except EOFError, OSError:
                self.message.emit(self._serial, ("failed", "gone"))
                break
            if not self._process.is_alive():
                self.message.emit(self._serial, ("failed", "gone"))
                break
        self._process.join(_QUIT_GRACE_S)
        if self._process.is_alive():
            self._process.kill()
            self._process.join(1.0)
        try:
            self._conn.close()
        except OSError:
            pass

    def _send(self, item: tuple) -> None:
        try:
            self._conn.send(item)
        except OSError, ValueError:
            pass


class CaptionsProcess(QObject):
    """The captions process while it runs; `start()` again after `stop()` for a new one."""

    ready = Signal()
    failed = Signal(str)
    caption = Signal(int, float, float, str)  # run, start, end, text
    progress = Signal(int, float, float)  # run, newest second decoded, real-time factor
    resync = Signal(int, float)  # run, newest second: read again from there
    ended = Signal(int, str)  # run, reason
    detected = Signal(int, str)  # run, the language the pieces agreed on (none was given)
    piece = Signal(int, float, float, float)  # run, start, end, speech share (no model loaded)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._supervisor: Optional[_Supervisor] = None
        self._leaving: Optional[_Supervisor] = None  # stopped, its child still ending
        self._serial = 0  # the running child's: a stopped one's last words are not passed on
        self._outbox: "queue.Queue[Optional[tuple]]" = queue.Queue()

    def running(self) -> bool:
        return self._supervisor is not None

    def busy(self) -> bool:
        """A child runs or is still ending (its files are open)."""
        return self._supervisor is not None or self._leaving is not None

    def start(self, load: dict, *, detailed_log: bool = False) -> None:
        """Start the child and have it load the model (`load`: the paths and threads)."""
        if self._supervisor is not None:
            return
        from services.captions.worker import captions_main

        context = multiprocessing.get_context("spawn")
        conn, child = context.Pipe(duplex=True)
        process = context.Process(
            target=captions_main, args=(child, detailed_log), name="qitv-captions", daemon=True
        )
        process.start()
        child.close()
        self._outbox = queue.Queue()
        self._serial += 1
        supervisor = _Supervisor(conn, process, self._outbox, self._serial)
        supervisor.message.connect(self._on_message, Qt.ConnectionType.QueuedConnection)
        supervisor.finished.connect(supervisor.deleteLater)
        supervisor.finished.connect(lambda: self._forget(supervisor))
        self._supervisor = supervisor
        supervisor.start()
        self.send("load", load)

    def send(self, *message: Any) -> None:
        if self._supervisor is not None:
            self._outbox.put(tuple(message))

    def stop(self) -> None:
        """Ask the child to quit; the supervisor waits for it off the GUI thread."""
        supervisor, self._supervisor = self._supervisor, None
        if supervisor is not None:
            self._leaving = supervisor
            self._outbox.put(None)

    def wait(self, ms: int) -> None:
        """Tests and the app's close: wait for the last child to end."""
        supervisor = self._supervisor or self._leaving
        if supervisor is not None:
            supervisor.wait(ms)

    def _forget(self, supervisor: _Supervisor) -> None:
        if self._leaving is supervisor:
            self._leaving = None

    def _on_message(self, serial: int, message: Any) -> None:
        kind = message[0]
        if kind != "log" and (self._supervisor is None or serial != self._serial):
            # A stopped child's (2.9: Automatic's step down starts the other model at once):
            # its "gone" would forget the new one, its "ready" would be the new one's.
            return
        if kind == "caption":
            self.caption.emit(int(message[1]), float(message[2]), float(message[3]), message[4])
        elif kind == "progress":
            self.progress.emit(int(message[1]), float(message[2]), float(message[3]))
        elif kind == "detected":
            self.detected.emit(int(message[1]), str(message[2]))
        elif kind == "piece":
            self.piece.emit(
                int(message[1]), float(message[2]), float(message[3]), float(message[4])
            )
        elif kind == "ready":
            self.ready.emit()
        elif kind == "resync":
            self.resync.emit(int(message[1]), float(message[2]))
        elif kind == "ended":
            self.ended.emit(int(message[1]), str(message[2]))
        elif kind == "failed":
            if message[1] == "gone":
                self._supervisor = None
            self.failed.emit(str(message[1]))
        elif kind == "log":
            logger.log(int(message[1]), "captions: %s", message[2])
