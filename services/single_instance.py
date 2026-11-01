"""One QiTV per settings folder (2.8.12).

Two QiTVs on one settings folder would each follow the recording schedule (one programme
recorded twice), each count a provider's streams by itself (two streams where it allows one)
and save the settings over each other. So a QiTV listens on a local socket named after its
settings folder (QITV_CONFIG_DIR, or the system's), and a second start of QiTV with that
folder hands over to it (`claim`): the first comes to the front, the second quits. A first
that doesn't answer within ANSWER_MS (it hangs, or a crash left its socket behind) is not
waited for: the new one starts as usual and takes the name over.

A run with a settings folder of its own (the smoke test, the tests, a second profile) has a
name of its own. The packaged app's --smoke and the time-shift helper (a multiprocessing
child) never reach main(), so they never ask. A Windows update's new copy (--replace) starts
while the old one quits: it never hands over.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
import time
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

logger = logging.getLogger(__name__)

ANSWER_MS = 2000  # a first QiTV that doesn't answer within this is not waited for
SHOW = b"show\n"  # asked: come to the front
SHOWN = b"shown\n"  # answered
RETRY_MS = 1000  # a Windows update's copy listens again until the old QiTV is gone
RETRIES = 30


def server_name(config_dir: str) -> str:
    """The local socket's name for a settings folder: short (a Unix socket's path is) and the
    same however the folder is spelled."""
    folder = os.path.normcase(os.path.realpath(os.path.abspath(config_dir)))
    return "qitv-" + hashlib.sha256(folder.encode("utf-8")).hexdigest()[:24]


def _allow_foreground() -> None:
    """Windows: the QiTV asked may take the foreground (this start was given it)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY: any process
    except (AttributeError, OSError) as e:
        logger.debug("AllowSetForegroundWindow failed: %s", type(e).__name__)


def hand_over(name: str, timeout_ms: int = ANSWER_MS) -> bool:
    """Ask the QiTV listening on `name` to come to the front; True once it said it did."""
    socket = QLocalSocket()
    deadline = time.monotonic() + timeout_ms / 1000
    try:
        socket.connectToServer(name)
        if not socket.waitForConnected(timeout_ms):
            return False
        _allow_foreground()
        socket.write(SHOW)
        socket.flush()
        answer = b""
        while not answer.endswith(b"\n"):
            left = int((deadline - time.monotonic()) * 1000)
            if left <= 0 or not socket.waitForReadyRead(left):
                return False
            answer += bytes(socket.readAll().data())
        return answer == SHOWN
    finally:
        socket.abort()


class InstanceServer(QObject):
    """This QiTV's socket: `activated` when another start asks it to come to the front."""

    activated = Signal()

    def __init__(self, name: str, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.name = name
        self._server = QLocalServer(self)
        # Unix: only this user's processes may connect (the socket file's mode).
        self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self._server.newConnection.connect(self._on_connection)
        self._read: dict[int, bytes] = {}
        self._tries = 0

    def listen(self, *, take_over: bool = True) -> bool:
        """Listen on the name; one left by a crashed or hung QiTV is taken over (Unix: its
        socket file is removed)."""
        if self._server.isListening() or self._server.listen(self.name):
            return True
        if not take_over:
            return False
        QLocalServer.removeServer(self.name)
        return self._server.listen(self.name)

    def listen_later(self) -> None:
        """Listen once the QiTV holding the name has gone (a Windows update's new copy)."""
        if self.listen(take_over=False) or self._tries >= RETRIES:
            if not self._server.isListening():
                self.listen()
            return
        self._tries += 1
        QTimer.singleShot(RETRY_MS, self, self.listen_later)

    def listening(self) -> bool:
        return self._server.isListening()

    def close(self) -> None:
        """Stops listening and drops a start still connected, so nothing of it is left for
        the garbage collector to delete outright (Qt warns, and may crash, on a socket so
        deleted)."""
        self._server.close()
        for socket in self._server.findChildren(QLocalSocket):
            socket.abort()
            socket.deleteLater()
        self._read.clear()

    def _on_connection(self) -> None:
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            if socket is None:
                return
            key = id(socket)
            self._read[key] = b""
            socket.readyRead.connect(lambda s=socket, k=key: self._on_read(s, k))
            socket.disconnected.connect(lambda k=key: self._read.pop(k, None))
            socket.disconnected.connect(socket.deleteLater)
            if socket.bytesAvailable():
                self._on_read(socket, key)

    def _on_read(self, socket: QLocalSocket, key: int) -> None:
        data = self._read.get(key, b"") + bytes(socket.readAll().data())
        if b"\n" not in data:
            self._read[key] = data[-64:]
            return
        self._read.pop(key, None)
        if data.startswith(SHOW):
            logger.info("QiTV was started again with these settings: this window comes forward.")
            self.activated.emit()
            socket.write(SHOWN)
            socket.flush()
        socket.disconnectFromServer()


def claim(config_dir: str, *, replacing: bool = False) -> Optional[InstanceServer]:
    """This QiTV's claim on its settings folder. None: another QiTV of that folder answered
    and came to the front (quit now). Else the listening server, kept while QiTV runs.
    `replacing` (a Windows update's new copy) never hands over: it listens once the old
    QiTV is gone."""
    name = server_name(config_dir)
    if not replacing and hand_over(name, ANSWER_MS):
        logger.info("Another QiTV runs with these settings: it was brought to the front.")
        return None
    server = InstanceServer(name)
    if replacing:
        server.listen_later()
    elif not server.listen():
        logger.warning("Couldn't watch for another start of QiTV with these settings.")
    return server
