"""Interrupting a live source's HTTP(S) connections from another thread (moved from
services/live/source.py as it was; `_Http` builds its session on `_InterruptibleAdapter`).

Platforms: on macOS and Linux, ``close()`` shuts down a duplicate descriptor of each live
connection, which wakes any call blocked on it, and never closes a descriptor from another
thread. On Windows a ``shutdown()`` wakes nothing blocked in Winsock (on a duplicate or on
the original handle); only closing the handle the call waits on does. There ``close()``
closes the owning plain socket objects, so the reader's own later close is a no-op. TLS
sockets are never touched from another thread (OpenSSL keeps using the raw handle value,
which a new socket may already own): they wait in slices of at most 0.25 s and stop on
their own once the source is closed.
"""

from __future__ import annotations

import _socket
from collections.abc import Callable
import logging
import socket
import ssl
import sys
import threading
import time
from typing import Any, TypeVar, cast
import weakref

from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.util.ssl_ import create_urllib3_context, resolve_cert_reqs, resolve_ssl_version

log = logging.getLogger(__name__)

_WINDOWS = sys.platform == "win32"
# Interrupt plain sockets through their owning objects and let TLS sockets wait in slices,
# instead of shutting down duplicates (see the module docstring).
_INTERRUPT_OWNERS = _WINDOWS
_TLS_SLICE = 0.25

_T = TypeVar("_T")


class _Closed(Exception):
    """The source was closed or stopped; iteration ends quietly."""


def _shutdown(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass  # already closed or never connected: nothing left to interrupt


def _abort(sock: socket.socket) -> None:
    """Interrupt a call blocked on ``sock`` (an owning plain socket) from another thread.

    On Windows only, the handle is then closed: nothing else wakes a blocked Winsock call.
    The C-level close marks the object's descriptor invalid under the GIL before releasing
    the handle, so the reader's later calls fail and its own close does nothing. POSIX
    never closes here: on macOS a close right after the shutdown can swallow its wakeup.
    """
    try:
        _socket.socket.shutdown(sock, socket.SHUT_RDWR)
    except OSError:
        pass  # not connected or already closed
    if not _WINDOWS:
        return
    try:
        _socket.socket.close(sock)
    except OSError:
        pass  # the peer already reset it; the handle is released either way


class _Interrupter:
    """Lets another thread interrupt every live connection of one source.

    macOS and Linux: a duplicate descriptor per connection. It shares the kernel socket,
    so shutting it down wakes a TLS handshake, header read or body read blocked on the
    original descriptor. Duplicates are only closed under the lock, so a recycled
    descriptor number is never touched.

    Windows: the owning plain socket objects (held weakly). Only closing a handle wakes a
    call blocked on it there; see ``_abort``. TLS sockets are left to ``_sliced_tls_socket``.
    (Tests force this mode elsewhere, where the shutdown alone wakes a plain reader.)
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._handles: dict[int, socket.socket] = {}
        self._owners: weakref.WeakSet[socket.socket] = weakref.WeakSet()
        self._interrupted = False

    @property
    def interrupted(self) -> bool:
        return self._interrupted

    def adopt(self, sock: socket.socket) -> None:
        """Windows: a plain socket object that owns its handle."""
        with self._lock:
            if not self._interrupted:
                self._owners.add(sock)
                return
        _abort(sock)  # the source closed while this connection was being made

    def track(self, connection: object, sock: socket.socket) -> None:
        if _INTERRUPT_OWNERS:
            # An HTTPS connection's socket is about to become a TLS socket's handle, which
            # must not be closed from another thread; its TLS socket waits in slices.
            if not isinstance(connection, HTTPSConnection):
                self.adopt(sock)
            return
        with self._lock:
            if not self._interrupted:
                self._forget_locked(connection)
                try:
                    self._handles[id(connection)] = sock.dup()
                except OSError as error:
                    log.debug("A connection cannot be interrupted (%s).", type(error).__name__)
                return
        _shutdown(sock)  # the source closed while this connection was being made

    def forget(self, connection: object) -> None:
        with self._lock:
            self._forget_locked(connection)

    def _forget_locked(self, connection: object) -> None:
        handle = self._handles.pop(id(connection), None)
        if handle is not None:
            handle.close()

    def interrupt(self) -> None:
        with self._lock:
            self._interrupted = True
            for handle in self._handles.values():
                _shutdown(handle)
                handle.close()
            self._handles.clear()
            for owner in list(self._owners):
                _abort(owner)


def _tls_context(connection: Any, socket_class: type[ssl.SSLSocket]) -> ssl.SSLContext:
    """The context urllib3 builds for a connection without one, plus ``socket_class``."""
    context = create_urllib3_context(
        ssl_version=resolve_ssl_version(connection.ssl_version),
        ssl_minimum_version=connection.ssl_minimum_version,
        ssl_maximum_version=connection.ssl_maximum_version,
        cert_reqs=resolve_cert_reqs(connection.cert_reqs),
    )
    # urllib3 loads the system roots itself only into a context it built (requests always
    # passes its CA bundle when verifying, so this matters only without verification).
    if not (connection.ca_certs or connection.ca_cert_dir or connection.ca_cert_data):
        context.load_default_certs()
    context.sslsocket_class = socket_class
    return context


def _sliced_tls_socket(interrupter: _Interrupter) -> type[ssl.SSLSocket]:
    """Windows TLS sockets that wait in slices and stop by themselves once interrupted.

    The real socket timeout is at most ``_TLS_SLICE``; the caller's timeout is kept as a
    deadline over the retries. Retrying after an ``_ssl`` timeout is safe: it is raised
    only while OpenSSL waits for the network (WANT_READ or WANT_WRITE), and CPython
    allows a write to be retried from a moved buffer. Only that timeout, which has no
    errno, is retried: a Winsock WSAETIMEDOUT is sticky and raised at once.
    """

    def waiting(self: Any, operation: Callable[[], _T]) -> _T:
        timeout = self.gettimeout()
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if interrupter.interrupted:
                raise _Closed()
            try:
                return operation()
            except TimeoutError as error:
                if error.errno is not None:
                    raise
                if deadline is not None and time.monotonic() >= deadline:
                    raise

    def settimeout(self: Any, value: float | None) -> None:
        self._logical_timeout = value
        if value is None:
            ssl.SSLSocket.settimeout(self, _TLS_SLICE)
        else:
            ssl.SSLSocket.settimeout(self, min(float(value), _TLS_SLICE))

    def gettimeout(self: Any) -> float | None:
        if "_logical_timeout" not in self.__dict__:
            return ssl.SSLSocket.gettimeout(self)
        timeout: float | None = self._logical_timeout
        return timeout

    def setblocking(self: Any, flag: bool) -> None:
        self.settimeout(None if flag else 0.0)

    def read(self: Any, len: int = 1024, buffer: Any = None) -> Any:  # also recv, recv_into
        return waiting(self, lambda: ssl.SSLSocket.read(self, len, buffer))

    def send(self: Any, data: Any, flags: int = 0) -> int:  # also sendall
        return waiting(self, lambda: ssl.SSLSocket.send(self, data, flags))

    def do_handshake(self: Any, block: bool = False) -> None:
        waiting(self, lambda: ssl.SSLSocket.do_handshake(self, block))

    namespace = {
        "settimeout": settimeout,
        "gettimeout": gettimeout,
        "setblocking": setblocking,
        "read": read,
        "send": send,
        "do_handshake": do_handshake,
    }
    return cast("type[ssl.SSLSocket]", type("SSLSocket", (ssl.SSLSocket,), namespace))


def _interruptible_pools(interrupter: _Interrupter) -> dict[str, type[HTTPConnectionPool]]:
    tls_socket = _sliced_tls_socket(interrupter)

    def connection_class(base: type[HTTPConnection]) -> type[HTTPConnection]:
        def new_conn(self: HTTPConnection) -> socket.socket:
            sock = base._new_conn(self)
            interrupter.track(self, sock)
            return sock

        def close(self: HTTPConnection) -> None:
            try:
                base.close(self)
            finally:
                interrupter.forget(self)

        def connect(self: Any) -> None:
            # Windows TLS sockets must wait in slices; the TLS socket exists only inside
            # urllib3's wrap_socket: hand urllib3 its usual context with a sliced class.
            if not (_INTERRUPT_OWNERS and isinstance(self, HTTPSConnection)):
                base.connect(self)
                return
            if self.ssl_context is not None:
                base.connect(self)  # a caller's own context: left as it is
                return
            self.ssl_context = _tls_context(self, tls_socket)
            try:
                base.connect(self)
            finally:
                self.ssl_context = None

        def getresponse(self: HTTPConnection) -> Any:
            sock = self.sock  # http.client drops it at once for a close-delimited body
            response = base.getresponse(self)
            response.qitv_socket = sock  # the reader shortens its timeout (`_Http.read`)
            return response

        namespace = {
            "_new_conn": new_conn,
            "close": close,
            "connect": connect,
            "getresponse": getresponse,
        }
        return cast("type[HTTPConnection]", type(base.__name__, (base,), namespace))

    def pool_class(base: type[HTTPConnectionPool], connection: type[HTTPConnection]) -> Any:
        return type(base.__name__, (base,), {"ConnectionCls": connection_class(connection)})

    return {
        "http": pool_class(HTTPConnectionPool, HTTPConnection),
        "https": pool_class(HTTPSConnectionPool, HTTPSConnection),
    }


class _InterruptibleAdapter(HTTPAdapter):
    def __init__(self, interrupter: _Interrupter) -> None:
        self._pool_classes = _interruptible_pools(interrupter)
        super().__init__()

    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = self._pool_classes

    def proxy_manager_for(self, proxy: str, **proxy_kwargs: Any) -> Any:
        manager = super().proxy_manager_for(proxy, **proxy_kwargs)
        if not proxy.lower().startswith("socks"):  # SOCKS pools bring their own connections
            manager.pool_classes_by_scheme = self._pool_classes
        return manager
