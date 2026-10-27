"""A local control port (2.8.3): a developer's tools read and drive the running QiTV.

It listens on 127.0.0.1 only, on a free port, and answers only requests that carry the token
it writes to `control.json` beside QiTV's log (`log_file.log_dir()`), a file only this user can
read, removed when QiTV quits. Nothing is announced on the network. A run from the source code
has it (QITV_CONTROL=0 turns it off); a downloaded build only with QITV_CONTROL=1, and never runs
Python sent to it.

A request is GET or POST /<command> with an `Authorization: Bearer <token>` header and, for a
POST, a JSON body {"args": {...}}; the answer is JSON. `log` is read here; every other command
runs on the GUI thread (`handle(command, args)`), which the request's thread waits for.
"""

from __future__ import annotations

import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import secrets
import sys
import threading
from typing import Any, Callable, Mapping, Optional

from PySide6.QtCore import QObject, Qt, Signal, Slot

logger = logging.getLogger(__name__)

FILE_NAME = "control.json"
ENV = "QITV_CONTROL"
WAIT_S = 30.0  # the longest a command may take on the GUI thread
LOG_LINES = 200  # `log`'s default number of lines, at most `LOG_MAX`
LOG_MAX = 5000

Handle = Callable[[str, dict], dict]


def enabled(env: Optional[Mapping[str, str]] = None, frozen: Optional[bool] = None) -> bool:
    """Whether this run opens the control port: from source unless QITV_CONTROL=0, a
    downloaded build only with QITV_CONTROL=1."""
    env = os.environ if env is None else env
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    setting = env.get(ENV, "")
    return setting == "1" or (not frozen and setting != "0")


def python_allowed(frozen: Optional[bool] = None) -> bool:
    """Whether Python sent to the port runs in QiTV: from source only."""
    return not (bool(getattr(sys, "frozen", False)) if frozen is None else frozen)


class _Call:
    def __init__(self, command: str, args: dict) -> None:
        self.command, self.args = command, args
        self.done = threading.Event()
        self.result: dict = {}


class _GuiBridge(QObject):
    """Made on the GUI thread: a request's thread emits `asked`, the GUI thread runs it."""

    asked = Signal(object)

    def __init__(self, handle: Handle) -> None:
        super().__init__()
        self._handle = handle
        self.asked.connect(self._run, Qt.ConnectionType.QueuedConnection)

    @Slot(object)
    def _run(self, call: _Call) -> None:
        try:
            call.result = self._handle(call.command, call.args)
        except Exception as error:  # a command's failure is its answer, never QiTV's
            call.result = {"error": f"{type(error).__name__}: {error}"}
        finally:
            call.done.set()


class ControlServer:
    """`handle(command, args)` runs on the GUI thread; `directory` holds `control.json` and
    `log_path` is the log `log` reads."""

    def __init__(self, handle: Handle, directory: Path, log_path: Optional[Path] = None) -> None:
        self._bridge = _GuiBridge(handle)
        self._directory = Path(directory)
        self._log_path = log_path
        self.token = secrets.token_urlsafe(32)
        self.port = 0
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def file(self) -> Path:
        return self._directory / FILE_NAME

    def start(self) -> Optional[int]:
        """Listen and write `control.json`: the port, or None when it could not open."""
        try:
            self._server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self))
        except OSError as error:
            logger.warning("The control port could not open (%s).", type(error).__name__)
            return None
        self._server.daemon_threads = True
        self.port = int(self._server.server_address[1])
        try:
            self._write_file()
        except OSError as error:
            logger.warning(
                "The control port's file could not be written (%s).", type(error).__name__
            )
            self._server.server_close()
            self._server = None
            return None
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="qitv-control", daemon=True
        )
        self._thread.start()
        logger.info("Control port open on this computer only (port %d).", self.port)
        return self.port

    def stop(self) -> None:
        """Stop listening and remove `control.json` (when it is still this run's)."""
        server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()
        try:
            if json.loads(self.file.read_text(encoding="utf-8")).get("token") == self.token:
                self.file.unlink()
        except OSError, ValueError, AttributeError:
            pass

    def _write_file(self) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        data = {"port": self.port, "token": self.token, "pid": os.getpid()}
        descriptor = os.open(self.file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as out:
            json.dump(data, out)
        os.chmod(self.file, 0o600)  # an older file of another mode

    def authorized(self, header: str) -> bool:
        return hmac.compare_digest(header.encode(), f"Bearer {self.token}".encode())

    def call(self, command: str, args: dict) -> tuple[int, dict]:
        """Run `command` on the GUI thread and wait for its answer (never from that thread)."""
        if command == "log":
            return 200, self.log_tail(args)
        call = _Call(command, args)
        self._bridge.asked.emit(call)
        if not call.done.wait(WAIT_S):
            return 504, {"error": f"QiTV did not answer within {WAIT_S:.0f} s"}
        error = str(call.result.get("error") or "")
        return (404 if error.startswith("unknown command") else 200), call.result

    def log_tail(self, args: dict) -> dict:
        """The log's last `lines` lines (those holding `grep`, when given), as written:
        addresses and query strings already left out."""
        if self._log_path is None or not self._log_path.exists():
            return {"lines": []}
        try:
            wanted = max(1, min(LOG_MAX, int(args.get("lines") or LOG_LINES)))
        except TypeError, ValueError:
            wanted = LOG_LINES
        needle = str(args.get("grep") or "")
        text = self._log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        if needle:
            text = [line for line in text if needle in line]
        return {"lines": text[-wanted:]}


def _handler(server: ControlServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass  # the requests themselves go nowhere

        def do_GET(self) -> None:  # noqa: N802 (http.server's names)
            self._serve({})

        def do_POST(self) -> None:  # noqa: N802
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                self._reply(400, {"error": "the body is not JSON"})
                return
            args = body.get("args") if isinstance(body, dict) else None
            self._serve(args if isinstance(args, dict) else {})

        def _serve(self, args: dict) -> None:
            if not server.authorized(self.headers.get("Authorization", "")):
                self._reply(401, {"error": "a token is needed (control.json)"})
                return
            command = self.path.split("?", 1)[0].strip("/")
            status, answer = server.call(command, args)
            self._reply(status, answer)

        def _reply(self, status: int, answer: dict) -> None:
            data = json.dumps(answer, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler
