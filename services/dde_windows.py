"""Windows: a double-clicked video or song reaches a running QiTV over DDE, with no second
process (a second start unpacks the whole single-file app first: 6.7 s on CI before it could
hand over).

While a QiTV registered for videos or music runs, each registered kind's ProgID has a `ddeexec`
on its open verb (file_types.add_ddeexec), from the start or from the moment Settings registers
it, until Settings unregisters the last kind or QiTV quits. Explorer then connects to the
service `SERVICE` on `TOPIC` and executes `COMMAND` with `%1` replaced by the file. When no
QiTV runs (the key goes), Windows starts QiTV with the file on its command line, which plays
it. A crash leaves the key: Explorer, finding no server, starts QiTV with the file all the same
and then sends `[noop]`, which opens nothing; that start writes the key again.

The server uses DDEML (user32) through ctypes, with its Unicode functions. DDEML posts its
messages to the thread that initialized it, so the server lives on the GUI thread: Qt's event
loop dispatches them. Its callback only copies the command and schedules `opened` for the
event loop; the files go on to the window as another start's hand-over does.

Microsoft's pages: DdeInitializeW, DdeCreateStringHandleW, DdeNameService, DdeCmpStringHandles,
DdeGetData, DdeFreeDataHandle, DdeFreeStringHandle, DdeUninitialize, DdeConnect,
DdeClientTransaction, DdeDisconnect, DdeCallback (PFNCALLBACK), XTYP_CONNECT, XTYP_EXECUTE;
the numbers are the Windows SDK's ddeml.h.
"""

from __future__ import annotations

import ctypes
from ctypes import POINTER, c_int, c_size_t, c_uint, c_uint32, c_void_p, c_wchar_p
import logging
import re
import sys
import threading
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from services import file_types
from services.file_types import Status

logger = logging.getLogger(__name__)

SERVICE = "QiTV"
TOPIC = "System"
COMMAND = '[open("%1")]'  # Explorer puts the file's path for %1
WRITER_THREAD = "qitv-dde-key"
QUIT_LOCK_S = 2.0  # at quit, the longest wait for a file types change on its way (it takes ms)
CLIENT_TIMEOUT_MS = 2000

# ddeml.h
APPCLASS_STANDARD = 0x00000000
APPCMD_CLIENTONLY = 0x00000010
APPCMD_FILTERINITS = 0x00000020  # XTYP_CONNECT only for the service registered
CBF_FAIL_ADVISES = 0x00004000
CBF_FAIL_POKES = 0x00010000
CBF_FAIL_REQUESTS = 0x00020000
CBF_SKIP_ALLNOTIFICATIONS = 0x003C0000
XTYP_CONNECT = 0x0060 | 0x1000 | 0x0002  # | XCLASS_BOOL | XTYPF_NOBLOCK
XTYP_EXECUTE = 0x0050 | 0x4000  # | XCLASS_FLAGS
DDE_FACK = 0x8000
DDE_FNOTPROCESSED = 0x0000
DNS_REGISTER = 0x0001
DNS_UNREGISTER = 0x0002
CP_WINUNICODE = 1200
DMLERR_NO_ERROR = 0
# A server: connections and executes reach the callback; advise loops, pokes, requests and
# notifications are answered by DDEML itself.
SERVER_FLAGS = (
    APPCLASS_STANDARD
    | APPCMD_FILTERINITS
    | CBF_FAIL_ADVISES
    | CBF_FAIL_POKES
    | CBF_FAIL_REQUESTS
    | CBF_SKIP_ALLNOTIFICATIONS
)

# Windows' calling convention (C's elsewhere, where the tests call the callback themselves).
WINFUNCTYPE: Any = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
# HDDEDATA DdeCallback(UINT, UINT, HCONV, HSZ, HSZ, HDDEDATA, ULONG_PTR, ULONG_PTR): the
# handles are pointer-sized, an instance id (DWORD) 32 bits.
CALLBACK = WINFUNCTYPE(
    c_void_p, c_uint, c_uint, c_void_p, c_void_p, c_void_p, c_void_p, c_size_t, c_size_t
)
DWORD = c_uint32
BOOL = c_int
HANDLE = c_void_p  # HSZ, HCONV, HDDEDATA
# The callbacks of instances DDEML didn't let go (DdeUninitialize failed): it may still call
# them, so they are kept for the run.
_UNRELEASED: list[Any] = []

_OPEN = re.compile(r'\[\s*open\s*\(\s*"((?:[^"]|"")*)"\s*\)\s*\]', re.IGNORECASE)


def parse_execute(text: str) -> list[str]:
    """The paths in one DDE execute string: one or more `[open("<path>")]` commands, in
    order, with a quote doubled inside a path undoubled. Anything else (`[noop]`, another
    command, text that isn't a command) is passed over."""
    paths = []
    at = 0
    while (start := text.find("[", at)) >= 0:
        match = _OPEN.match(text, start)
        if match:
            path = match.group(1).replace('""', '"')
            if path:
                paths.append(path)
            at = match.end()
        else:
            at = _past_command(text, start)
    return paths


def _past_command(text: str, start: int) -> int:
    """Just past the `]` that closes the command at `start`, outside quotes; else the end."""
    quoted = False
    for i in range(start + 1, len(text)):
        char = text[i]
        if char == '"':
            quoted = not quoted
        elif char == "]" and not quoted:
            return i + 1
    return len(text)


def _command_text(raw: bytes) -> str:
    """An execute's data as text: UTF-16 (the server's DDEML is the Unicode one), up to its
    terminator (the block may be larger). A path's lone surrogate stays as it is."""
    text = raw[: len(raw) - len(raw) % 2].decode("utf-16-le", "surrogatepass")
    return text.split("\0", 1)[0]


def _user32() -> Any:
    """user32's DDEML functions, typed (their own copy: no other module's types change).
    Windows only; the tests put a fake DDEML here."""
    lib = ctypes.WinDLL("user32")
    signatures: dict[str, tuple[Any, list[Any]]] = {
        "DdeInitializeW": (c_uint, [POINTER(DWORD), CALLBACK, DWORD, DWORD]),
        "DdeCreateStringHandleW": (HANDLE, [DWORD, c_wchar_p, c_int]),
        "DdeFreeStringHandle": (BOOL, [DWORD, HANDLE]),
        "DdeNameService": (HANDLE, [DWORD, HANDLE, HANDLE, c_uint]),
        "DdeCmpStringHandles": (c_int, [HANDLE, HANDLE]),
        "DdeGetData": (DWORD, [HANDLE, c_void_p, DWORD, DWORD]),
        "DdeFreeDataHandle": (BOOL, [HANDLE]),
        "DdeUninitialize": (BOOL, [DWORD]),
        "DdeGetLastError": (c_uint, [DWORD]),
        "DdeConnect": (HANDLE, [DWORD, HANDLE, HANDLE, c_void_p]),
        "DdeClientTransaction": (
            HANDLE,
            [c_void_p, DWORD, HANDLE, HANDLE, c_uint, c_uint, DWORD, POINTER(DWORD)],
        ),
        "DdeDisconnect": (BOOL, [HANDLE]),
    }
    for name, (restype, argtypes) in signatures.items():
        function = getattr(lib, name)
        function.restype, function.argtypes = restype, argtypes
    return lib


class DdeServer(QObject):
    """QiTV's DDE server (`SERVICE` on `TOPIC`): each `[open("…")]` executed comes out as
    `opened(paths)` from the event loop. `start` and `stop` on the GUI thread."""

    opened = Signal(list)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._lib: Any = None
        self._callback: Any = None  # DDEML calls it until uninitialized: kept here
        self._inst = 0
        self._service: Optional[int] = None
        self._topic: Optional[int] = None
        self._registered = False
        self._pending: list[str] = []  # commands copied in the callback, for the event loop

    @property
    def running(self) -> bool:
        return bool(self._inst)

    def start(self) -> bool:
        """Serve; False (logged) when DDEML refused or failed any step, and QiTV works as
        without (it never raises: QiTV's start goes on)."""
        if self._inst:
            return True
        try:
            return self._start()
        except Exception:
            logger.warning("No DDE: a double-click starts QiTV again", exc_info=True)
            self.stop()
            return False

    def _start(self) -> bool:
        lib = _user32()
        callback = CALLBACK(self._on_transaction)
        inst = DWORD(0)
        error = lib.DdeInitializeW(ctypes.pointer(inst), callback, SERVER_FLAGS, 0)
        if error != DMLERR_NO_ERROR:
            logger.warning("DDE didn't start (DDEML error 0x%x)", error)
            return False
        self._lib, self._callback, self._inst = lib, callback, inst.value
        self._service = lib.DdeCreateStringHandleW(self._inst, SERVICE, CP_WINUNICODE)
        self._topic = lib.DdeCreateStringHandleW(self._inst, TOPIC, CP_WINUNICODE)
        if self._service and self._topic:
            self._registered = bool(
                lib.DdeNameService(self._inst, self._service, None, DNS_REGISTER)
            )
        if not self._registered:
            logger.warning(
                "QiTV's DDE service wasn't offered (DDEML error 0x%x)",
                lib.DdeGetLastError(self._inst),
            )
            self.stop()
            return False
        logger.info("Double-clicked videos reach QiTV over DDE")
        return True

    def stop(self) -> None:
        """The service unregistered and DDEML let go, each step whatever the others did; again,
        nothing. It never raises."""
        inst, self._inst = self._inst, 0
        if not inst:
            return
        lib, service, topic = self._lib, self._service, self._topic

        def attempt(what: str, call: Callable[[], object]) -> bool:
            try:
                return bool(call())
            except Exception as e:
                logger.warning("DDE couldn't %s (%s)", what, type(e).__name__)
                return False

        if self._registered:
            attempt("unregister", lambda: lib.DdeNameService(inst, service, None, DNS_UNREGISTER))
        for handle in (service, topic):
            if handle:
                attempt("free a name", lambda: lib.DdeFreeStringHandle(inst, handle))
        if not attempt("let go", lambda: lib.DdeUninitialize(inst)):
            logger.warning("DDEML didn't let QiTV's DDE go: its callback is kept for the run")
            _UNRELEASED.append(self._callback)
        self._callback = None  # DDEML calls it no more, or it is kept above
        self._service = self._topic = None
        self._registered = False
        self._pending = []

    # DDEML's, on the GUI thread: nothing but copying and scheduling, and an answer at once

    def _on_transaction(
        self,
        kind: int,
        fmt: int,
        conversation: Optional[int],
        hsz1: Optional[int],
        hsz2: Optional[int],
        data: Optional[int],
        data1: int,
        data2: int,
    ) -> int:
        try:
            if kind == XTYP_CONNECT:  # hsz1 the topic, hsz2 the service
                lib = self._lib
                ours = (
                    lib.DdeCmpStringHandles(hsz1, self._topic) == 0
                    and lib.DdeCmpStringHandles(hsz2, self._service) == 0
                )
                return 1 if ours else 0
            if kind == XTYP_EXECUTE:
                self._take(data)
                return DDE_FACK
        except Exception:
            logger.warning("A DDE transaction failed (0x%x)", kind, exc_info=True)
            return DDE_FNOTPROCESSED
        return 0

    def _take(self, data: Optional[int]) -> None:
        """Copy an execute's command (the data handle lasts only for the callback; the
        XTYP_EXECUTE page has the server free it) and schedule it for the event loop."""
        lib = self._lib
        try:
            size = lib.DdeGetData(data, None, 0, 0) if data else 0
            raw = b""
            if size:
                buffer = ctypes.create_string_buffer(size)
                copied = lib.DdeGetData(data, buffer, size, 0)
                raw = buffer.raw[:copied]
        finally:
            if data:
                lib.DdeFreeDataHandle(data)
        if not self._pending:
            QTimer.singleShot(0, self, self._flush)
        self._pending.append(_command_text(raw))

    def _flush(self) -> None:
        """The commands copied since the last time, as one `opened` (a multi-select's files
        together: the window picks among them as among one start's)."""
        commands, self._pending = self._pending, []
        if not commands:
            return
        paths = [path for command in commands for path in parse_execute(command)]
        if not paths:
            logger.info("A DDE command named no file to open")
            return
        logger.info("%d file(s) came over DDE", len(paths))
        self.opened.emit(paths)


class Serving:
    """The run's one DDE server and the ddeexec that points Explorer at it, following the
    registration: on while QiTV is registered for any kind (from the start, or once Settings
    registered one), off once Settings unregistered the last. On the GUI thread. `exe`: the QiTV the
    registration's command names, by default this one's (file_types.own_executable: after a
    Windows update, the installed QiTV the copy in Downloads wrote itself over)."""

    def __init__(self, server: Optional[DdeServer] = None, exe: str = "") -> None:
        self.server = server if server is not None else DdeServer()
        self._exe = exe or file_types.own_executable()
        self._served = False  # it served once: the quit takes the ddeexec back
        self._closed = False

    @property
    def running(self) -> bool:
        return self.server.running

    def follow(self, registered: bool) -> bool:
        """Registered (for any kind): the server starts unless it runs (never a second one),
        and the ddeexec is written on a worker each time, in every registered kind's ProgID
        (written over: a registration may have come meanwhile); a server that can't start
        takes a stale ddeexec back instead. Not registered for any: the server stops
        (unregistering took each ddeexec with its ProgID). True while it serves."""
        if self._closed:
            return False
        if not registered:
            self.server.stop()
            return False
        if not self.server.start():  # True at once when it runs
            _on_worker(file_types.remove_ddeexec)
            return False
        self._served = True
        exe = self._exe
        _on_worker(lambda: file_types.add_ddeexec(exe, wanted=lambda: not self._closed))
        return True

    def close(self) -> None:
        """At quit (aboutToQuit, which a Windows sign-out or shutdown still sends; main()'s
        `finally` too), on the GUI thread: the ddeexec goes first, so Explorer starts QiTV
        again, then the server stops. A key write still queued then writes nothing; one on its
        way is waited for, QUIT_LOCK_S at most. Once; it never raises."""
        if self._closed:
            return
        self._closed = True
        if self._served:
            try:
                file_types.remove_ddeexec(timeout=QUIT_LOCK_S)
            except Exception as e:
                logger.warning(
                    "QiTV's DDE key stays (%s): the next start writes it", type(e).__name__
                )
        self.server.stop()


def serve(config: Any, frozen: Optional[bool] = None) -> Optional[Serving]:
    """On Windows, where QiTV can register (file_types.status: never from source or a
    temporary place), the run's DDE owner, serving already when QiTV is registered for any
    kind. None elsewhere."""
    if sys.platform != "win32":
        return None
    states = [file_types.status(kind, config, frozen) for kind in file_types.KINDS]
    if any(state not in (Status.REGISTERED, Status.NOT_REGISTERED) for state in states):
        return None
    serving = Serving()
    serving.follow(Status.REGISTERED in states)
    return serving


def _on_worker(work: Callable[[], object]) -> threading.Thread:
    def body() -> None:
        try:
            work()
        except Exception:
            logger.warning("Telling Explorer about QiTV's DDE failed", exc_info=True)

    thread = threading.Thread(target=body, name=WRITER_THREAD, daemon=True)
    thread.start()
    return thread


def execute(command: str, timeout_ms: int = CLIENT_TIMEOUT_MS) -> bool:
    """Send `command` to a running QiTV as Explorer does (DdeConnect to `SERVICE`/`TOPIC`, a
    synchronous XTYP_EXECUTE): True once it was acknowledged. Windows only, from a thread that
    serves no DDE itself; for the tests and CI's measurement."""
    lib = _user32()
    callback = CALLBACK(lambda *args: 0)  # a client's: nothing comes to it
    inst = DWORD(0)
    if lib.DdeInitializeW(ctypes.pointer(inst), callback, APPCMD_CLIENTONLY, 0):
        return False
    service = lib.DdeCreateStringHandleW(inst.value, SERVICE, CP_WINUNICODE)
    topic = lib.DdeCreateStringHandleW(inst.value, TOPIC, CP_WINUNICODE)
    try:
        if not (service and topic):  # a 0 handle connects to any server, or any topic
            return False
        conversation = lib.DdeConnect(inst.value, service, topic, None)
        if not conversation:
            logger.info("No QiTV answered over DDE (0x%x)", lib.DdeGetLastError(inst.value))
            return False
        try:
            data = (command + "\0").encode("utf-16-le")
            buffer = ctypes.create_string_buffer(data, len(data))
            result = DWORD(0)
            answer = lib.DdeClientTransaction(
                buffer,
                len(data),
                conversation,
                None,
                0,
                XTYP_EXECUTE,
                timeout_ms,
                ctypes.pointer(result),
            )
            return bool(answer)
        finally:
            lib.DdeDisconnect(conversation)
    finally:
        for handle in (service, topic):
            if handle:
                lib.DdeFreeStringHandle(inst.value, handle)
        if not lib.DdeUninitialize(inst.value):
            _UNRELEASED.append(callback)  # DDEML may still call it
