"""One QiTV per settings folder (2.8.12).

Two QiTVs on one settings folder would each follow the recording schedule (one programme
recorded twice), each count a provider's streams by itself (two streams where it allows one)
and save the settings over each other. So a QiTV listens on a local socket named after its
settings folder (QITV_CONFIG_DIR, or the system's), and a second start of QiTV with that
folder hands over to it (`claim`): the first comes to the front, the second quits. A first
that can't be reached within ANSWER_MS (a crash left its socket behind) is not waited for: the
new one starts as usual and takes the name over. One that was reached may still be starting,
its event loop not running yet (a multi-select starts one QiTV per file at once): its answer
is waited for up to START_WAIT_MS, and only one that hangs that long is taken over.

Starts at the same instant (Ruling 31) all find nobody listening, and a listen is no proof of
being alone: Qt's Unix server, with UserAccessOption, renames its socket over a name already
taken, and on Windows two servers may listen on one pipe. So the QiTV that listens first takes
an OS lock on LOCK_FILE in the settings folder (`_InstanceLock`); a start that finds it held
asks again until its holder answers (CLAIM_WAIT_S at most), and takes over only one that never
listens or hangs. The system lets go of the lock when its process ends, a crash too.

The hand-over: the new start sends `show` and the first answers `shown` once it came to the
front. A start with files (a video double-clicked while QiTV runs) then sends one
`open <file URL>` line for each (`open raw:<path>` for a Windows path a URL would change), and
`done`; the first answers `opened` and opens them. A QiTV
of 2.10 or before hangs up after `shown`: it came to the front without the files, and the
new start quits all the same (never a second QiTV on one settings folder).

A run with a settings folder of its own (the smoke test, the tests, a second profile) has a
name of its own. The packaged app's --smoke and the time-shift helper (a multiprocessing
child) never reach main(), so they never ask. A Windows update's new copy (--replace) starts
while the old one quits: it never hands over.
"""

from __future__ import annotations

from collections.abc import Sequence
import enum
import hashlib
import logging
import os
import sys
import time
from typing import Optional
from urllib.parse import quote, unquote_to_bytes

from PySide6.QtCore import QByteArray, QDir, QObject, QTimer, QUrl, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

logger = logging.getLogger(__name__)

ANSWER_MS = 2000  # to reach the first QiTV, and for it to take the files once it came forward
START_WAIT_MS = 20000  # a first QiTV reached while it still starts is waited for that long
SHOW = b"show\n"  # asked: come to the front
SHOWN = b"shown\n"  # answered
OPEN = b"open "  # and a file URL (percent-encoded: no newline in it), then a newline
RAW = b"raw:"  # after OPEN: the path itself, its UTF-8 percent-encoded (`_open_payload`)
DONE = b"done\n"  # no more files
OPENED = b"opened\n"  # answered: the files were taken
MAX_LINE = 65536  # a longer line (its newline not counted) drops its connection
RETRY_MS = 1000  # a Windows update's copy listens again until the old QiTV is gone
LOCK_FILE = "qitv-instance.lock"  # in the settings folder: held by the QiTV that listens
CLAIM_WAIT_S = 5.0  # a start that finds the lock held asks its holder that long (it listens in ms)
CLAIM_RETRY_S = 0.05
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


def _too_long(line: bytes) -> bool:
    """A line neither side sends nor reads (its newline not counted)."""
    return len(line) > MAX_LINE


def _file_url(path: str) -> bytes:
    """A path as an `open` line's file URL, read back by `_local_path` byte for byte."""
    if not os.path.isabs(path):
        path = os.path.abspath(path)  # the first QiTV runs in another folder
    slashed = QDir.fromNativeSeparators(path)
    if slashed.startswith("//"):
        # \\server\share\…: QUrl would make the server's name the URL's host and lower-case
        # it. The whole path goes into the URL's path behind an empty host. (A \\?\… path
        # never comes here: `_open_payload` sends it raw.)
        url = QUrl()
        url.setScheme("file")
        url.setHost("")
        url.setPath(slashed, QUrl.ParsingMode.DecodedMode)
    else:
        url = QUrl.fromLocalFile(path)
    return bytes(url.toEncoded().data())


# Windows' extended-length (\\?\) and device (\\.\) spellings: QUrl drops the prefix
# (\\?\UNC\server\share\… came back \\server\share\…), so these paths go raw.
_UNCHANGED_PREFIXES = ("\\\\?\\", "\\\\.\\")


def _open_payload(path: str) -> bytes:
    """What follows `open ` for `path`, read back by `_opened_path` byte for byte: its file URL,
    or for a Windows path with the extended-length or device prefix the path itself (RAW, its
    UTF-8 percent-encoded, lone surrogates too). Such a path is absolute as it is."""
    if path.startswith(_UNCHANGED_PREFIXES):
        encoded = quote(path.encode("utf-8", "surrogatepass"), safe="")
        return RAW + encoded.encode("ascii")
    return _file_url(path)


def _opened_path(payload: bytes) -> str:
    """The path in an `open` line (either of `_open_payload`'s forms); "" for a URL that isn't
    a local file's, and for a raw path that doesn't decode or holds a NUL (it is skipped)."""
    if not payload.startswith(RAW):
        return _local_path(payload)
    try:
        path = unquote_to_bytes(payload[len(RAW) :].decode("ascii")).decode(
            "utf-8", "surrogatepass"
        )
    except UnicodeDecodeError:
        return ""
    return "" if "\0" in path else path


def _local_path(url: bytes) -> str:
    """The local file's path in an `open` line's URL, spelled as this system does (Windows:
    backslashes, as a launch argument has them); "" for any other URL."""
    parsed = QUrl.fromEncoded(QByteArray(url))
    if not parsed.isValid() or not parsed.isLocalFile():
        return ""
    return QDir.toNativeSeparators(parsed.toLocalFile())


def _left_ms(deadline: float) -> int:
    return int((deadline - time.monotonic()) * 1000)


def _read_line(socket: QLocalSocket, deadline: float) -> bytes:
    """The answer up to its newline, or what came of it by `deadline` or the hang-up."""
    answer = b""
    while True:
        if socket.bytesAvailable() > 0:
            answer += bytes(socket.readAll().data())
        if b"\n" in answer:
            return answer
        left = _left_ms(deadline)
        if left <= 0 or not socket.waitForReadyRead(left):
            if socket.bytesAvailable() > 0:  # it came with the hang-up
                answer += bytes(socket.readAll().data())
            return answer


def _send_files(socket: QLocalSocket, paths: Sequence[str], timeout_ms: int) -> None:
    """After `shown`: the files and `done`; the first's answer says whether it took them."""
    lines = []
    for path in paths:
        if not path:
            continue
        line = OPEN + _open_payload(path)
        if _too_long(line):
            logger.warning("A file's path is too long to hand over (%d characters).", len(path))
            continue
        lines.append(line + b"\n")
    deadline = time.monotonic() + timeout_ms / 1000
    if socket.state() == QLocalSocket.LocalSocketState.ConnectedState:
        socket.write(b"".join(lines) + DONE)
        socket.flush()
        while socket.bytesToWrite() > 0:
            left = _left_ms(deadline)
            if left <= 0 or not socket.waitForBytesWritten(left):
                break
    answer = _read_line(socket, deadline)
    if answer == OPENED:
        logger.info("The running QiTV took the files (%d).", len(lines))
    elif socket.state() != QLocalSocket.LocalSocketState.ConnectedState:
        logger.info("The running QiTV is older: it came to the front without the file.")
    else:
        logger.warning("The running QiTV didn't say it took the files.")


class _Asked(enum.Enum):
    ANSWERED = "answered"  # it came to the front (an older QiTV, which takes no files, too)
    UNREACHABLE = "unreachable"  # nobody listens on the name (yet), or a crash left it
    SILENT = "silent"  # reached, but no answer within START_WAIT_MS (it hangs or went away)


def hand_over(name: str, timeout_ms: int = ANSWER_MS, paths: Sequence[str] = ()) -> bool:
    """Ask the QiTV listening on `name` to come to the front and open `paths`; True once it
    said it came (an older QiTV, which takes no files, too: this start quits all the same).

    One that can't be reached within `timeout_ms` is not waited for. One that was reached may
    still be starting (no event loop yet): its answer is waited for up to START_WAIT_MS."""
    return _ask(name, timeout_ms, paths) is _Asked.ANSWERED


def _ask(name: str, timeout_ms: int, paths: Sequence[str]) -> _Asked:
    """`hand_over`, saying why a QiTV that didn't come wasn't reached or didn't answer."""
    socket = QLocalSocket()
    try:
        socket.connectToServer(name)
        if not socket.waitForConnected(timeout_ms):
            return _Asked.UNREACHABLE
        _allow_foreground()
        asked = time.monotonic()
        socket.write(SHOW)
        socket.flush()
        if _read_line(socket, asked + START_WAIT_MS / 1000) != SHOWN:
            if socket.state() == QLocalSocket.LocalSocketState.ConnectedState:
                logger.warning("A QiTV with these settings was reached but didn't answer.")
            return _Asked.SILENT
        waited_ms = int((time.monotonic() - asked) * 1000)
        if waited_ms > timeout_ms:
            logger.info("The running QiTV answered after %d ms: it was starting.", waited_ms)
        if paths:
            try:
                _send_files(socket, paths, timeout_ms)
            except Exception as error:  # it came forward: never a second QiTV for a file
                logger.warning("Handing the files over failed (%s).", type(error).__name__)
        return _Asked.ANSWERED
    finally:
        socket.abort()


class _Lock(enum.Enum):
    OURS = "ours"
    HELD = "held"  # by another QiTV of this settings folder, listening or about to
    UNAVAILABLE = "unavailable"  # the folder or its file system can't lock: started without


class _InstanceLock:
    """An exclusive OS lock on LOCK_FILE in a settings folder (flock on Unix, a byte locked
    with msvcrt on Windows): one QiTV of the folder listens at a time. The system lets go when
    the process ends, a crash too, so nothing stale is ever left to judge. The file itself
    stays: removing it would let two starts each lock a file of their own."""

    def __init__(self, config_dir: str) -> None:
        self.path = os.path.join(config_dir, LOCK_FILE)
        self._fd: Optional[int] = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> _Lock:
        if self._fd is not None:
            return _Lock.OURS
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)  # never inherited
        except OSError as error:
            logger.warning("QiTV's start lock can't be opened (%s).", type(error).__name__)
            return _Lock.UNAVAILABLE
        state = _lock(fd)
        if state is _Lock.OURS:
            self._fd = fd
        else:
            os.close(fd)
        return state

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        if sys.platform == "win32":
            import msvcrt

            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass  # closing lets go all the same
        try:
            os.close(fd)  # Unix: the flock goes with the file's last descriptor
        except OSError:
            pass


def _lock(fd: int) -> _Lock:
    """Lock `fd`'s file without waiting."""
    if sys.platform == "win32":
        import msvcrt

        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            # Held (EACCES), as any failure is read: a lock misread as free would let two
            # starts listen, one misread as held only waits CLAIM_WAIT_S.
            return _Lock.HELD
        return _Lock.OURS
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return _Lock.HELD
    except OSError as error:  # a file system without locks (ENOLCK, EOPNOTSUPP)
        logger.warning("QiTV's start lock isn't supported here (%s).", type(error).__name__)
        return _Lock.UNAVAILABLE
    return _Lock.OURS


class _Start:
    """A start that asks (one connection): its unread bytes, whether it was answered, its
    files, and the timer that lets it go when it says nothing more."""

    def __init__(self, quiet: QTimer) -> None:
        self.pending = b""
        self.shown = False
        self.paths: list[str] = []
        self.quiet = quiet


class InstanceServer(QObject):
    """This QiTV's socket: `activated` when another start asks it to come to the front,
    `opened` with the files a start handed over."""

    activated = Signal()
    opened = Signal(list)

    def __init__(
        self,
        name: str,
        parent: Optional[QObject] = None,
        *,
        lock: Optional[_InstanceLock] = None,
    ) -> None:
        super().__init__(parent)
        self.name = name
        self._lock = lock  # held while it listens; let go at close
        self._server = QLocalServer(self)
        # Unix: only this user's processes may connect (the socket file's mode).
        self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self._server.newConnection.connect(self._on_connection)
        self._starts: dict[int, _Start] = {}
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
        for start in self._starts.values():
            start.quiet.stop()
        self._starts.clear()
        if self._lock is not None:  # after the server: the next QiTV may listen now
            self._lock.release()

    def _on_connection(self) -> None:
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            if socket is None:
                return
            key = id(socket)
            quiet = QTimer(socket)
            quiet.setSingleShot(True)
            quiet.timeout.connect(lambda s=socket, k=key: self._went_quiet(s, k))
            self._starts[key] = _Start(quiet)
            socket.readyRead.connect(lambda s=socket, k=key: self._on_read(s, k))
            socket.disconnected.connect(lambda k=key: self._forget(k))
            socket.disconnected.connect(socket.deleteLater)
            if socket.bytesAvailable():
                self._on_read(socket, key)

    def _forget(self, key: int) -> None:
        start = self._starts.pop(key, None)
        if start is not None:
            start.quiet.stop()

    def _let_go(self, socket: QLocalSocket, key: int) -> None:
        self._forget(key)
        socket.abort()

    def _went_quiet(self, socket: QLocalSocket, key: int) -> None:
        if key in self._starts:
            logger.info("A start that came forward said nothing more: it was let go.")
            self._let_go(socket, key)

    def _on_read(self, socket: QLocalSocket, key: int) -> None:
        start = self._starts.get(key)
        if start is None:
            return  # answered, or let go: nothing more is read
        start.pending += bytes(socket.readAll().data())
        while self._starts.get(key) is start:
            line, newline, rest = start.pending.partition(b"\n")
            if _too_long(line):
                logger.warning("A start sent a line too long: it was let go.")
                self._let_go(socket, key)
                return
            if not newline:
                break
            start.pending = rest  # kept before the line is acted on (a slot may spin a loop)
            self._on_line(socket, key, start, line)
        if self._starts.get(key) is start and start.shown:
            start.quiet.start(ANSWER_MS)  # silent that long after `shown`: let go

    def _on_line(self, socket: QLocalSocket, key: int, start: _Start, line: bytes) -> None:
        if not start.shown:
            if line + b"\n" != SHOW:  # not a QiTV asking: hung up on, unanswered
                self._forget(key)
                socket.disconnectFromServer()
                return
            start.shown = True
            logger.info("QiTV was started again with these settings: this window comes forward.")
            self.activated.emit()
            socket.write(SHOWN)
            socket.flush()
        elif line.startswith(OPEN):
            path = _opened_path(line.removeprefix(OPEN))
            if path:
                start.paths.append(path)
            else:
                logger.debug("A start handed over a URL that isn't a local file's.")
        elif line + b"\n" == DONE:
            self._forget(key)
            # Answered before the files are opened: however long that takes, the start that
            # handed them over isn't kept waiting.
            socket.write(OPENED)
            socket.flush()
            socket.disconnectFromServer()
            if start.paths:
                count = len(start.paths)
                logger.info("A start handed over %d file(s): this window opens them.", count)
                self.opened.emit(start.paths)
        # Any other line is a later QiTV's: not understood, left unanswered.


def claim(
    config_dir: str, *, replacing: bool = False, paths: Sequence[str] = ()
) -> Optional[InstanceServer]:
    """This QiTV's claim on its settings folder. None: another QiTV of that folder answered
    and came to the front, and was handed `paths` (quit now). Else the listening server,
    kept while QiTV runs. `replacing` (a Windows update's new copy) never hands over: it
    listens once the old QiTV is gone."""
    name = server_name(config_dir)
    if replacing:
        server = InstanceServer(name)
        server.listen_later()
        return server
    asked = _ask(name, ANSWER_MS, paths)
    lock = _InstanceLock(config_dir)
    state: Optional[_Lock] = None
    if asked is _Asked.UNREACHABLE:
        state = lock.acquire()
        if state is _Lock.HELD:
            # Another start of these settings, at the same moment (a multi-select), listens in
            # a moment: it is asked until it answers.
            logger.info("Another QiTV with these settings is starting: it is asked.")
            asked, state = _ask_the_holder(name, paths, lock)
    if asked is _Asked.ANSWERED:
        logger.info("Another QiTV runs with these settings: it was brought to the front.")
        return None
    if state is None:  # one was reached but hangs: taken over, as always; the lock if free
        state = lock.acquire()
    if state is _Lock.HELD:
        logger.warning("The QiTV holding these settings doesn't answer: this one takes over.")
    server = InstanceServer(name, lock=lock if lock.held else None)
    if not server.listen():
        logger.warning("Couldn't watch for another start of QiTV with these settings.")
    return server


def _ask_the_holder(name: str, paths: Sequence[str], lock: _InstanceLock) -> tuple[_Asked, _Lock]:
    """While another start holds the lock: ask it every CLAIM_RETRY_S, CLAIM_WAIT_S at most
    (it listens milliseconds after locking). What it answered, with the lock still HELD (SILENT:
    reached, but it hangs; UNREACHABLE: it never listened); or UNREACHABLE with the lock's new
    state once it came free (its holder quit or crashed meanwhile)."""
    deadline = time.monotonic() + CLAIM_WAIT_S
    while time.monotonic() < deadline:
        time.sleep(CLAIM_RETRY_S)
        asked = _ask(name, ANSWER_MS, paths)
        if asked is not _Asked.UNREACHABLE:
            return asked, _Lock.HELD
        state = lock.acquire()
        if state is not _Lock.HELD:
            return _Asked.UNREACHABLE, state
    return _Asked.UNREACHABLE, _Lock.HELD
    return _Asked.UNREACHABLE
