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
taken, and on Windows two servers may listen on one pipe. So a QiTV holds an OS lock on
LOCK_FILE in the settings folder (`_InstanceLock`) while it listens; the system lets go of it
when its process ends, a crash too. While a live process holds it, a start never becomes a
second QiTV (Ruling 32): it asks the holder until it answers (a cold start under load took
38 s on CI before its event loop ran), HELD_WAIT_MS at most, and then ends without starting.
Only a name no process holds the lock for is taken over: a crash's socket, or an older QiTV
(it takes no lock) that hangs START_WAIT_MS.

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
import errno
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
RETRY_MS = 250  # a Windows update's copy tries the old QiTV's lock again until it lets go
LOCK_FILE = "qitv-instance.lock"  # in the settings folder: held by the QiTV that listens
HELD_WAIT_MS = 120000  # a start that finds the lock held asks its holder that long, then ends
CLAIM_RETRY_S = 0.05  # between asks while the holder doesn't listen yet
RETRIES = 120  # RETRY_MS each: about 30 s, then it listens all the same


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


def _ask(
    name: str, timeout_ms: int, paths: Sequence[str], answer_ms: Optional[int] = None
) -> _Asked:
    """`hand_over`, saying why a QiTV that didn't come wasn't reached or didn't answer. One
    reached is waited for `answer_ms` (START_WAIT_MS unless given), or until it hangs up."""
    socket = QLocalSocket()
    try:
        socket.connectToServer(name)
        if not socket.waitForConnected(timeout_ms):
            return _Asked.UNREACHABLE
        _allow_foreground()
        asked = time.monotonic()
        socket.write(SHOW)
        socket.flush()
        wait_ms = START_WAIT_MS if answer_ms is None else answer_ms
        if _read_line(socket, asked + wait_ms / 1000) != SHOWN:
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
        try:
            state = _lock(fd)
        except BaseException:
            os.close(fd)  # never a descriptor left open (Windows couldn't remove the folder)
            raise
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
        except OSError as error:
            # _locking with _LK_NBLCK sets EACCES for a byte another holds (EDEADLOCK is
            # _LK_LOCK's after its retries). Anything else is no lock: a start that read it as
            # held would wait HELD_WAIT_MS and never start.
            if error.errno == errno.EACCES:
                return _Lock.HELD
            logger.warning("QiTV's start lock isn't supported here (%s).", type(error).__name__)
            return _Lock.UNAVAILABLE
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
        self._closed = False

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
        """A Windows update's new copy whose old QiTV still runs: listen once the old one let go
        of the settings' lock (it quits), from the event loop every RETRY_MS, RETRIES times at
        most, then all the same (an old version that hangs). A listen is no sign the old one is
        gone: Qt lets two listen on one pipe."""
        if self._closed:
            return
        lock = self._lock
        state = lock.acquire() if lock is not None else _Lock.UNAVAILABLE
        if state is _Lock.HELD:
            if self._tries < RETRIES:
                self._tries += 1
                QTimer.singleShot(RETRY_MS, self, self.listen_later)
                return
            logger.warning("The QiTV this one replaces still holds its settings: listening anyway.")
        if lock is not None and not lock.held:
            self._lock = None
        self._listen_or_let_go()

    def _listen_or_let_go(self) -> bool:
        """Listen; one that can't lets the lock go (a start waiting for its answer would wait
        in vain)."""
        if self.listen():
            return True
        logger.warning("Couldn't watch for another start of QiTV with these settings.")
        if self._lock is not None:
            self._lock.release()
            self._lock = None
        return False

    def listening(self) -> bool:
        return self._server.isListening()

    def close(self) -> None:
        """Stops listening and drops a start still connected, so nothing of it is left for
        the garbage collector to delete outright (Qt warns, and may crash, on a socket so
        deleted)."""
        self._closed = True  # a listen_later still due does nothing
        self._server.close()
        for socket in self._server.findChildren(QLocalSocket):
            socket.abort()
            socket.deleteLater()
        for start in self._starts.values():
            start.quiet.stop()
        self._starts.clear()
        # The server first, then the lock: a start waiting for this QiTV's answer sees it hang
        # up, finds the lock held a moment longer and waits on, then takes the lock once it
        # is let go here (it never takes over while the lock is held).
        if self._lock is not None:
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
    """This QiTV's claim on its settings folder. None: quit now. Another QiTV of that folder
    answered and came to the front, and was handed `paths`; or one holds the folder's lock but
    didn't answer within HELD_WAIT_MS (its files are lost: better than a second QiTV). Else
    the listening server, kept while QiTV runs.

    `replacing`: a Windows update's new copy whose old QiTV still runs (its file couldn't be
    replaced: main.old_qitv_still_runs). It never hands over (the old one is quitting, or
    hangs), and listens once the old one lets go of its lock, about 30 s at most. Once the old
    file was replaced the old QiTV has quit, and the copy claims as any start does: a start
    that took the lock meanwhile (a double-click) gets the copy's files, and the copy ends."""
    name = server_name(config_dir)
    lock = _InstanceLock(config_dir)
    if replacing:
        server = InstanceServer(name, lock=lock)
        server.listen_later()
        return server
    if lock.acquire() is _Lock.HELD:
        # A QiTV of these settings runs, or starts at this moment (a multi-select): it is
        # asked until it answers, and never gets a second QiTV beside it.
        asked, state = _wait_for_holder(name, paths, lock)
        if asked is _Asked.ANSWERED:
            logger.info("Another QiTV runs with these settings: it was brought to the front.")
            return None
        if state is not _Lock.OURS:
            logger.warning(
                "Another QiTV with these settings holds them but doesn't answer; this start ends."
            )
            return None
        # its holder quit or crashed meanwhile: this start is the QiTV now
    else:
        # No QiTV of this version runs: an older one may (it takes no lock), or a crash left
        # its socket. One that answers gets the files; one that hangs START_WAIT_MS, and a
        # crash's socket, are taken over.
        if _ask(name, ANSWER_MS, paths) is _Asked.ANSWERED:
            lock.release()
            logger.info("Another QiTV runs with these settings: it was brought to the front.")
            return None
    server = InstanceServer(name, lock=lock if lock.held else None)
    server._listen_or_let_go()
    return server


def _wait_for_holder(name: str, paths: Sequence[str], lock: _InstanceLock) -> tuple[_Asked, _Lock]:
    """While a live process holds the lock: ask it, HELD_WAIT_MS at most, every CLAIM_RETRY_S
    while it doesn't listen yet; one reached is waited for until it answers, hangs up or the
    time runs out (one connection: no `show` piles up for it). ANSWERED once it came, with the
    lock still HELD; OURS once this start got the lock (its holder quit or crashed); HELD when
    the time ran out."""
    deadline = time.monotonic() + HELD_WAIT_MS / 1000
    asked = _Asked.UNREACHABLE
    while (left := _left_ms(deadline)) > 0:
        asked = _ask(name, min(ANSWER_MS, left), paths, answer_ms=left)
        if asked is _Asked.ANSWERED:
            return asked, _Lock.HELD
        # Only the lock itself ends the wait: one that reads UNAVAILABLE now (no descriptor
        # left, a lock call failing for a moment) is no sign its holder went.
        if lock.acquire() is _Lock.OURS:
            return asked, _Lock.OURS
        time.sleep(CLAIM_RETRY_S)
    return asked, _Lock.HELD
