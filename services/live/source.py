"""Network input for the live pipeline: raw MPEG-TS over HTTP(S), and HLS of TS segments.

A source yields the stream's bytes untouched (no remux) plus ``Discontinuity`` markers. HLS
segments are fetched in media-sequence order, AES-128 is decrypted as a stream, and media
playlists are reloaded live. Variant choice, redirect handling and error texts are ported
from main's ``services/timeshift_source.py``; that version handed media to FFmpeg, which
this pipeline never uses.

Every request goes through one ``requests.Session`` per source, so cookies persist,
cross-origin redirects drop ``Authorization`` and the SSL setting applies on every hop and
nested resource. Connecting and each response's headers and first body byte may take 60 s;
after that, 10 s without a byte breaks the connection. ``close()`` may be called from any
thread, never blocks, and interrupts a TLS handshake, header read, body read or backoff
wait at once. A DNS lookup (``getaddrinfo``) is not bounded at all, and a TCP connect is
bounded only by the 60 s connect timeout per address. Credentials never reach error texts
or log lines: all ``SourceError`` messages are fixed strings.

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
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
import hashlib
import logging
import os
import re
import shutil
import socket
import ssl
import sys
import threading
import time
from typing import Any, BinaryIO, Protocol, TextIO, TypeVar, cast
from urllib.parse import urljoin, urlsplit
from urllib.request import url2pathname
import weakref

from Cryptodome.Cipher import AES
from Cryptodome.Util.Padding import unpad
import m3u8
import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import NameResolutionError, ReadTimeoutError
from urllib3.util.ssl_ import create_urllib3_context, resolve_cert_reqs, resolve_ssl_version

from services.live.refusals import REFUSALS, STREAM_LIMIT, STREAM_REFUSED  # noqa: F401 (and here)
from services.live.ts import ContinuityJoiner, program_has_audio
from services.vlc_profile import DEFAULT_USER_AGENT

log = logging.getLogger(__name__)

_TIMEOUT = 60.0  # connect, and a response's headers and first body byte
# Once a body has sent bytes, this long without one is a broken connection: a provider that
# stops sending but keeps the connection open is skipped (an HLS segment or playlist) ...
_STALL_TIMEOUT = 10.0
# ... or reconnected (raw TS), later: one provider's raw TS pauses for up to ~14 s (measured:
# 3.7-14 s every 30-60 s) and then goes on in the same connection with its timestamps
# continuous. A reconnect within the pause started 2-4 s back (the ring held them twice) or
# at live (the delayed seconds were never seen).
_TS_STALL_TIMEOUT = 20.0
_MANIFEST_LIMIT = 2 * 1024 * 1024
_READ_SIZE = 64 * 1024
_SNIFF_SIZE = 10
_PLAYLIST_TYPES = (
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "audio/mpegurl",
    "audio/x-mpegurl",
)
# A variant whose master names a separate default audio playlist: how much of its first
# segment may pass before its program tables must have shown audio of its own.
_AUDIO_LOOK = 64 * 1024
_PACKET = 188
_BLOCK = 16
_RECONNECT_DELAYS = (0.5, 1.0, 2.0, 4.0, 8.0, 8.0)
# A first answer that failed in passing is asked for again after the first this many reconnect
# delays (3.5 s in all, inside the GUI's 6 s ready timeout).
_OPEN_RETRIES = 3
# Spent reconnect tries come back only once a new connection has carried this much.
_HEALTHY_SECONDS = 2.0
_HEALTHY_BYTES = 256 * 1024
_STOP_POLL = 0.1
_PLAYLIST_HOPS = 4
_KEY_CACHE = 16
_KEY_LIMIT = 1024
# A live playlist without a new segment for this long is frozen.
_FROZEN_TIMEOUT = 60.0
# An older playlist copy that overlaps recent segments is waited out this many targets.
_STALE_TARGETS = 3
_RECENT_SEGMENTS = 10
_SEGMENT_FAILURES = 3
_SKIPPABLE_STATUS = (404, 410)
# Answers that mean the provider's stream limit is reached (spec 6.9), never retried; any other
# 4xx counts when its body says so (the first _LIMIT_BODY_BYTES of it are read). A plain 403
# may be one too, or a channel this account may not watch: STREAM_REFUSED says it softer.
_LIMIT_STATUS = (429, 458, 509)
_LIMIT_BODY_BYTES = 4096
_LIMIT_WORDS = re.compile(
    rb"connections?[\s_-]*limit|limit[\s_-]*of[\s_-]*connections|max(?:imum)?[\s_-]*connections"
    rb"|too[\s_-]*many[\s_-]*connections",
    re.IGNORECASE,
)
_DEFAULT_PORTS = {"http": 80, "https": 443}
_IDENTITY = {"Accept-Encoding": "identity"}
_WINDOWS = sys.platform == "win32"
# Interrupt plain sockets through their owning objects and let TLS sockets wait in slices,
# instead of shutting down duplicates (see the module docstring).
_INTERRUPT_OWNERS = _WINDOWS
_TLS_SLICE = 0.25
# A directory where every opened source saves what it reads from the provider, with arrival
# times (`_Capture`). For debugging only: the files hold the provider's stream. Unset (the
# default), nothing is saved and nothing is paid.
CAPTURE_ENV = "QITV_CAPTURE_DIR"
_CAPTURE_MAX_BYTES = 2 << 30  # a capture stops after 2 GiB ...
_CAPTURE_MAX_SECONDS = 3 * 3600.0  # ... or 3 hours
_CAPTURE_FREE_FLOOR = 1 << 30  # and never leaves the disk less than 1 GiB free
_CAPTURE_CHECK_BYTES = 32 << 20  # the free space is looked up again after this much
_CAPTURE_BUFFER = 1 << 20
_CAPTURE_FLUSH_S = 1.0  # a read this long after the last flush flushes both files

_OPEN_FAILED = "The channel could not be opened for time-shift."
_HTTP_FAILED = "The channel could not be reached over HTTP."
_TLS_FAILED = "The channel's TLS certificate could not be verified."
_TIMED_OUT = "The channel did not respond before the connection timed out."
_RECEIVE_FAILED = "The channel connection failed while receiving media."
_STOPPED = "The channel connection was stopped."
_TOO_LARGE = "The channel's HLS playlist is too large."
_NO_VIDEO = "The HLS channel has no compatible video quality."
_INVALID_PLAYLIST = "The channel supplied an invalid HLS playlist."
_NON_HTTP = "The HLS channel references a non-HTTP resource."
_FRAGMENTED = "The HLS channel uses fragmented MP4 or byte-range segments."
_SEPARATE_AUDIO = "The HLS channel carries its audio in a separate playlist."
_ENCRYPTION = "The HLS channel uses an unsupported encryption method."
_UNDECRYPTABLE = "The HLS channel could not be decrypted."
_NOT_TS = "The HLS channel's segments are not MPEG-TS."
_FROZEN = "The channel's HLS playlist stopped advancing."

_DEFAULT_MESSAGES = {
    "unsupported": _OPEN_FAILED,
    "network": _HTTP_FAILED,
    "tls": _TLS_FAILED,
    "timeout": _TIMED_OUT,
    "http": _HTTP_FAILED,
}

_T = TypeVar("_T")


class SourceError(Exception):
    """A source failure whose kind and message are safe to show: never a URL or credential.

    ``kind`` is ``"unsupported"`` (play directly instead), ``"network"``, ``"tls"``,
    ``"timeout"`` or ``"http"``; ``status`` is the HTTP status of an ``"http"`` failure.
    """

    def __init__(self, kind: str, message: str = "", status: int | None = None) -> None:
        if kind not in _DEFAULT_MESSAGES:
            raise ValueError(f"unknown source error kind {kind!r}")
        super().__init__(kind, message or _DEFAULT_MESSAGES[kind], status)
        self.kind = kind
        self.status = status

    def __str__(self) -> str:
        return str(self.args[1])


@dataclass(frozen=True)
class Discontinuity:
    """A break in the byte stream: an HLS discontinuity, a sequence gap or a reconnect."""


@dataclass(frozen=True)
class Reconnecting:
    """Raw TS only: the connection broke (it ended, or sent nothing for `_TS_STALL_TIMEOUT`)
    and a new one is being opened; a `Discontinuity` follows once it delivers. Until then the
    source waits on the provider, as during a pause it waits out."""


class CaughtUp:
    """HLS only: the source delivered every segment its playlist listed, so what follows comes
    as the provider publishes it. Yielded after each such batch; the helper then reports its
    live edge at once (its state messages are throttled, and the end of a batch would wait for
    the next one) and that the backlog fetched at the start has landed."""


Chunk = bytes | Discontinuity | CaughtUp | Reconnecting


class Source(Protocol):
    user_agent: str
    hls: bool  # an HLS source (stay-live keeps such a stream at 1x; 2.0.2)

    @property
    def burst_s(self) -> float:
        """How many seconds of media typically arrive at once: an HLS segment, or 0."""
        ...

    def chunks(self) -> Iterator[Chunk]: ...

    def close(self) -> None: ...

    def wait_closed(self, timeout: float) -> bool:
        """After ``close()``: wait up to ``timeout`` until the upstream connection is closed
        (on Windows a TLS reader closes it within one 0.25 s slice); True once it is."""
        ...


class _Closed(Exception):
    """The source was closed or stopped; iteration ends quietly."""


class _ReadError(Exception):
    """A response body broke off; ``kind`` is ``"timeout"`` or ``"network"``."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class _NotPlaylist(Exception):
    """A playlist URL answered with something else: unsupported at open, retried on reload."""


class _Exhausted(Exception):
    """A request kept failing through the whole reconnect schedule."""


class _BadSegment(Exception):
    """One segment's content is unusable (not TS, undecryptable, a bad key or IV)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _scheme(url: str) -> str:
    # A Windows drive letter ("C:\\...") is a path, not the URL scheme "c".
    return "" if os.path.splitdrive(url)[0] else urlsplit(url).scheme.lower()


def _origin(url: str) -> tuple[str, str, int | None]:
    """Scheme, host and port (defaults filled in); raises ValueError for a malformed URL."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    return scheme, (parts.hostname or "").lower(), parts.port or _DEFAULT_PORTS.get(scheme)


def _identity(url: str) -> str:
    """A segment's name without rotating tokens: the last path component, no query."""
    return urlsplit(url).path.rsplit("/", 1)[-1]


def _status(error: requests.exceptions.HTTPError) -> int | None:
    return error.response.status_code if error.response is not None else None


def _refusal(response: Any) -> str | None:
    """The refusal an error answer means (one of REFUSALS), if it is one.

    A 4xx body is read (bounded) only to look for limit words: it never reaches a log.
    """
    status = int(getattr(response, "status_code", 0) or 0)
    if status in _LIMIT_STATUS:
        return STREAM_LIMIT
    if not 400 <= status < 500:
        return None
    if _names_limit(response):
        return STREAM_LIMIT
    return STREAM_REFUSED if status == 403 else None


def _names_limit(response: Any) -> bool:
    """Whether an error answer's body (its first `_LIMIT_BODY_BYTES`) names the connection
    limit. The body never reaches a log."""
    try:
        body = response.raw.read1(_LIMIT_BODY_BYTES, decode_content=True) or b""
    except Exception:
        body = b""
    return bool(_LIMIT_WORDS.search(body))


class _LimitAnswer(requests.exceptions.HTTPError):
    """A 5xx answer whose body names the connection limit (2.1 review I1): it is no limit
    refusal (the channel plays directly, as before), but the open never asks it again."""


def _dns_failed(error: Exception) -> bool:
    """Whether a connection failed because the host does not resolve: nothing warms up."""
    reason = getattr(error.args[0], "reason", None) if error.args else None
    return isinstance(reason, NameResolutionError)


def _sized(response: Any) -> bool:
    """Whether the body declared its length: only then is its end the end of the input."""
    headers = response.headers  # case-insensitive names
    framing = str(headers.get("Transfer-Encoding", "")).lower()
    return "Content-Length" in headers and "chunked" not in framing  # chunked overrides it


def _close_redirect_body(response, **_kwargs):
    # Requests consumes redirect bodies even with stream=True (and while preparing
    # Response.next with redirects disabled). Close before its redirect resolver
    # runs; headers remain available for cookies, authentication, and Location.
    if response.is_redirect:
        response.close()


def _quietly(action: Callable[[], object], what: str) -> None:
    try:
        action()
    except Exception as error:
        log.debug("Closing the %s failed (%s).", what, type(error).__name__)


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


def _retryable(error: Exception) -> bool:
    if isinstance(error, (_ReadError, _NotPlaylist)):
        return True
    if isinstance(error, requests.exceptions.SSLError):
        return False
    if isinstance(error, requests.exceptions.HTTPError):
        status = _status(error) or 0
        return status >= 500 or status == 408  # never 429: retrying deepens a rate limit
    return isinstance(error, (requests.exceptions.ConnectionError, requests.exceptions.Timeout))


def _translate(error: BaseException) -> SourceError:
    """A fresh, context-free error: nothing from a URL-bearing exception survives."""
    if isinstance(error, SourceError):
        return SourceError(error.kind, str(error), error.status)
    if isinstance(error, requests.exceptions.SSLError):
        return SourceError("tls")
    if isinstance(error, requests.exceptions.Timeout):
        return SourceError("timeout")
    if isinstance(error, _ReadError) and error.kind == "timeout":
        return SourceError("timeout")
    if isinstance(error, _ReadError):
        return SourceError("network", _RECEIVE_FAILED)
    if isinstance(error, _Exhausted):
        return SourceError("network", _RECEIVE_FAILED)
    if isinstance(error, _NotPlaylist):
        return SourceError("unsupported", _INVALID_PLAYLIST)
    if isinstance(error, _BadSegment):
        return SourceError("unsupported", error.message)
    if isinstance(error, requests.exceptions.HTTPError):
        return SourceError("http", status=_status(error))
    if isinstance(error, requests.exceptions.RequestException):
        return SourceError("network")
    log.warning("A live source failed unexpectedly (%s).", type(error).__name__)
    return SourceError("unsupported")


class _Backoff:
    """The reconnect schedule; the caller refills it once data flows again."""

    def __init__(self) -> None:
        self.used = 0

    def next_delay(self) -> float | None:
        if self.used >= len(_RECONNECT_DELAYS):
            return None
        self.used += 1
        return _RECONNECT_DELAYS[self.used - 1]

    def reset(self) -> None:
        self.used = 0


class _Http:
    """One Session per source, interruptible from any thread by ``close()`` or ``stop``."""

    def __init__(self, headers: Mapping[str, str], verify_ssl: bool, stop: threading.Event) -> None:
        self._interrupter = _Interrupter()
        self._lock = threading.Lock()
        self._closed_event = threading.Event()
        self._response: Any = None
        self._flowing: Any = None  # the latest response whose body sent bytes
        self._released = threading.Event()  # close() has closed the response and session
        self._owner: int | None = None
        self._verify_ssl = verify_ssl
        self._stop = stop
        self._stall_timeout = _STALL_TIMEOUT
        self.closed = False
        provided = {name.lower(): value for name, value in headers.items() if value}
        self.user_agent = provided.get("user-agent") or DEFAULT_USER_AGENT
        referer = provided.get("referer")
        self.session = requests.Session()
        adapter = _InterruptibleAdapter(self._interrupter)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self.session.hooks["response"].append(_close_redirect_body)
        self.session.headers["User-Agent"] = self.user_agent
        if referer:  # an M3U channel's http-referrer (2.1): every request, as VLC sends it
            self.session.headers["Referer"] = referer
        threading.Thread(target=self._watch, name="qitv-live-source-stop", daemon=True).start()

    def _watch(self) -> None:
        while not self._stop.wait(_STOP_POLL):
            if self.closed:
                return
        self.close()

    def get(
        self, url: str, headers: Mapping[str, str] | None = None, *, read_5xx: bool = False
    ) -> Any:
        """Send one GET; the response stays registered so ``close()`` can interrupt it.

        With ``read_5xx`` (the open), a 5xx answer's body is read for limit words too: one
        naming the limit raises `_LimitAnswer`, which is never asked again."""
        if urlsplit(url).scheme.lower() not in ("http", "https"):
            raise SourceError("unsupported", _NON_HTTP)
        if self.closed:
            raise _Closed()
        self._owner = threading.get_ident()
        try:
            response = self.session.get(
                url,
                stream=True,
                timeout=(_TIMEOUT, _TIMEOUT),
                verify=self._verify_ssl,
                headers=dict(headers or {}),
            )
        except Exception:
            if self.closed:
                raise _Closed() from None
            raise
        with self._lock:
            closed = self.closed
            if not closed:
                self._response = response
        if closed:
            _quietly(response.close, "response")
            raise _Closed()
        try:
            response.raise_for_status()
        except requests.exceptions.HTTPError as error:
            refusal = _refusal(response)
            status = _status(error) or 0
            limited = not refusal and read_5xx and status >= 500 and _names_limit(response)
            self.release(response)
            if refusal:
                raise SourceError("http", refusal, _status(error)) from None
            if limited:
                raise _LimitAnswer(response=response) from None
            raise
        except BaseException:
            self.release(response)
            raise
        return response

    def stall_after(self, seconds: float) -> None:
        """Break a body that sent bytes after `seconds` without one, the flowing one's too.
        Call it on the reading thread (it may touch that response's socket)."""
        self._stall_timeout = seconds
        if self._flowing is not None:
            _shorten_read_timeout(self._flowing, seconds)

    def release(self, response: Any) -> None:
        with self._lock:
            if self._response is not response:
                return  # close() took it over
            self._response = None
        _quietly(response.close, "response")

    def read(self, response: Any) -> bytes:
        self._owner = threading.get_ident()
        try:
            data = response.raw.read1(_READ_SIZE, decode_content=True)
        except Exception as error:
            if self.closed:
                raise _Closed() from None
            timeout = isinstance(error, (ReadTimeoutError, TimeoutError))
            raise _ReadError("timeout" if timeout else "network") from None
        if self.closed:
            raise _Closed()
        if data and response is not self._flowing:
            self._flowing = response
            _shorten_read_timeout(response, self._stall_timeout)
        return data or b""

    def read_prefix(self, response: Any) -> bytes:
        prefix = b""
        while len(prefix) < _SNIFF_SIZE:
            block = self.read(response)
            if not block:
                break
            prefix += block
        return prefix

    def read_all(self, response: Any, prefix: bytes, limit: int) -> bytes:
        """The whole body, or just over ``limit`` bytes of it when it is longer."""
        data = bytearray(prefix)
        while len(data) <= limit:
            block = self.read(response)
            if not block:
                break
            data += block
        return bytes(data)

    def wait(self, seconds: float) -> bool:
        """Sleep, waking early (True) when the source closes."""
        return self._closed_event.wait(max(0.0, seconds))

    def retry(self, attempt: Callable[[], _T], backoff: _Backoff | None = None) -> _T:
        """Repeat ``attempt`` after each remaining reconnect delay; then give up."""
        if backoff is None:
            backoff = _Backoff()
        while (delay := backoff.next_delay()) is not None:
            if self.wait(delay):
                raise _Closed()
            try:
                return attempt()
            except Exception as error:
                if self.closed:
                    raise _Closed() from None
                if not _retryable(error):
                    raise
                log.info(
                    "Live source retry %d of %d failed (%s).",
                    backoff.used,
                    len(_RECONNECT_DELAYS),
                    type(error).__name__,
                )
        raise _Exhausted()

    def fetch(self, attempt: Callable[[], _T]) -> _T:
        """``attempt`` now, retried with the reconnect delays after a transient failure."""
        try:
            return attempt()
        except Exception as error:
            if self.closed:
                raise _Closed() from None
            if not _retryable(error):
                raise
            log.info("A live source request failed (%s); retrying.", type(error).__name__)
        return self.retry(attempt)

    def close(self) -> None:
        with self._lock:
            if self.closed:
                return
            self.closed = True
            response, self._response = self._response, None
            owner = self._owner
        self._closed_event.set()
        self._interrupter.interrupt()
        if response is not None and not _INTERRUPT_OWNERS:
            # A close-delimited body's socket has no live duplicate (http.client closed its
            # connection after the headers). Never used with owners: TLS sockets are not
            # touched from here, and plain owners were already interrupted above.
            _quietly(response.raw.shutdown, "response socket")
        if owner in (None, threading.get_ident()):
            self._release_all(response)
        else:
            # The reader may hold the response's buffer lock until its read wakes up.
            threading.Thread(
                target=self._release_all,
                args=(response,),
                name="qitv-live-source-close",
                daemon=True,
            ).start()

    def wait_closed(self, timeout: float) -> bool:
        return self._released.wait(timeout)

    def _release_all(self, response: Any) -> None:
        try:
            if response is not None:
                _quietly(response.close, "response")
            _quietly(self.session.close, "session")
        finally:
            self._released.set()


def _shorten_read_timeout(response: Any, seconds: float) -> None:
    """Media flowed: from now on a silence of `seconds` breaks the connection.

    Called on the reading thread, the only one that may touch a TLS socket. On Windows a
    sliced TLS socket keeps the value as its deadline over the slices. A response from a
    pool this module did not build (a SOCKS proxy) keeps the full read timeout.
    """
    sock = getattr(response.raw, "qitv_socket", None)
    if sock is None:
        return
    try:
        sock.settimeout(min(seconds, _TIMEOUT))
    except OSError as error:  # closed under the reader: its next read fails anyway
        log.debug("A read timeout could not be shortened (%s).", type(error).__name__)


def _private(path: str, flags: int) -> int:
    """`open`'s opener for a capture file: readable and writable by its owner only."""
    return os.open(path, flags, 0o600)


class _Capture:
    """What one opened source reads from the provider, saved under ``QITV_CAPTURE_DIR`` so a
    glitch can be replayed exactly: by the replay tool (``scripts/replay_capture.py``), or
    the ``.ts`` file alone in VLC or mpv.

    ``<stamp>-<hash>.ts`` holds the bytes as they were read, before any parsing, joining or
    segmenting: a raw TS body from its first byte on, and an HLS channel's media segments one
    after another in arrival order (an AES-128 segment decrypted, as it reaches the joiner;
    no playlist). ``<stamp>-<hash>.times`` is text with one ``<cumulative bytes> <seconds>``
    line per read: the bytes in the ``.ts`` file once that read was saved, and the seconds
    since the open began, with 3 decimals. Lines starting with ``#`` are comments, which a
    reader skips: ``# reconnect <seconds>`` once a raw TS stream's new connection answered
    (its bytes go on in the same pair, as they go on in the stream the helper reads), and for
    HLS ``# segment <seconds>`` as each segment answers, after ``# discontinuity <seconds>``
    when a break comes before it. The stamp is the local time at the open and the hash 8 hex
    digits of the channel URL's SHA-256: no address or credential reaches a file or a log.

    The files are made at the first bytes, readable by their owner only (and the directory,
    when it is made here), and written in the reading thread, buffered. Both are flushed by
    a comment line, by a read that comes a second or more after the last flush (so also the
    first read after a stall), and when the source closes: `flush` from the closing thread,
    then the reading thread's own close (the helper ends with ``os._exit``, which flushes
    nothing). A capture stops after 2 GiB or 3 hours, or where the disk would keep less than
    1 GiB free; a failure to write logs one warning and stops it. Playback goes on unchanged
    either way.
    """

    def __init__(self, directory: str, url: str) -> None:
        self._opened = time.monotonic()
        digest = hashlib.sha256(url.encode("utf-8", "surrogatepass")).hexdigest()[:8]
        self.name = f"{time.strftime('%Y%m%d-%H%M%S')}-{digest}"
        self._directory = directory
        self._lock = threading.Lock()  # the reading thread, and `flush` from a closing one
        self._files: tuple[BinaryIO, TextIO] | None = None
        self._written = 0
        self._checked = 0  # what had been written when the free space was last looked up
        self._flushed = self._opened
        self._stopped = False

    @classmethod
    def from_environment(cls, url: str) -> _Capture | None:
        directory = os.environ.get(CAPTURE_ENV, "")
        return cls(directory, url) if directory else None

    def write(self, block: bytes, at: float | None = None) -> None:
        """One read's bytes, which arrived at ``at`` (`time.monotonic`; now by default)."""
        if self._stopped or not block:
            return
        now = time.monotonic() if at is None else at
        with self._lock:
            try:
                self._write(block, now)
            except Exception as error:
                self._failed(error)

    def mark(self, event: str) -> None:
        """A ``# <event> <seconds>`` line in the times file, now; both files are flushed."""
        if self._stopped:
            return
        now = time.monotonic()
        with self._lock:
            try:
                files = self._open()
                if files is not None:
                    files[1].write(f"# {event} {now - self._opened:.3f}\n")
                    self._flush(now)
            except Exception as error:
                self._failed(error)

    def flush(self) -> None:
        """From any thread (the source's ``close`` and ``wait_closed``): what is buffered goes
        to disk. Never waits: while the reading thread is writing, it is left to that thread,
        which closes the files as the source ends."""
        if self._stopped or not self._lock.acquire(blocking=False):
            return
        try:
            if self._files is not None:
                self._flush(time.monotonic())
        except Exception as error:
            self._failed(error)
        finally:
            self._lock.release()

    def close(self) -> None:
        """In the reading thread, as the source ends: both files are flushed and closed."""
        with self._lock:
            self._close()

    def _write(self, block: bytes, now: float) -> None:
        seconds = now - self._opened
        if seconds >= _CAPTURE_MAX_SECONDS:
            self._stop(logging.INFO, "after 3 hours")
            return
        if self._written + len(block) > _CAPTURE_MAX_BYTES:
            self._stop(logging.INFO, "at 2 GiB")
            return
        files = self._open()
        if files is None:
            return
        data, times = files
        data.write(block)
        self._written += len(block)
        times.write(f"{self._written} {seconds:.3f}\n")
        if now - self._flushed >= _CAPTURE_FLUSH_S:
            self._flush(now)

    def _flush(self, now: float) -> None:
        if self._files is not None:
            for file in self._files:  # the bytes first: no line counts bytes still unsaved
                file.flush()
        self._flushed = now

    def _close(self) -> None:
        self._stopped = True
        files, self._files = self._files, None
        if files is not None:
            for file in files:
                _quietly(file.close, "capture")

    def _open(self) -> tuple[BinaryIO, TextIO] | None:
        """The files, made at the first call; None once the disk is too full."""
        if self._files is None:
            os.makedirs(self._directory, mode=0o700, exist_ok=True)
            if not self._room():
                return None
            self._files = self._create()
            log.info("Capturing the live stream to %s.ts and .times.", self.name)
        elif self._written - self._checked >= _CAPTURE_CHECK_BYTES and not self._room():
            return None
        return self._files

    def _create(self) -> tuple[BinaryIO, TextIO]:
        """A new pair: a second open in the same second of the same channel adds -2, -3..."""
        for attempt in range(1, 100):
            name = self.name if attempt == 1 else f"{self.name}-{attempt}"
            path = os.path.join(self._directory, name)
            try:
                data = open(path + ".ts", "xb", buffering=_CAPTURE_BUFFER, opener=_private)
            except FileExistsError:
                continue
            try:
                times = open(path + ".times", "x", encoding="ascii", newline="\n", opener=_private)
            except BaseException:
                data.close()
                raise
            self.name = name
            return data, times
        raise FileExistsError()

    def _room(self) -> bool:
        """Whether the disk keeps 1 GiB free until the next look, even with the buffer."""
        self._checked = self._written
        free = shutil.disk_usage(self._directory).free
        if free >= _CAPTURE_FREE_FLOOR + _CAPTURE_CHECK_BYTES + _CAPTURE_BUFFER:
            return True
        self._stop(logging.WARNING, "with less than 1 GiB free on its disk")
        return False

    def _stop(self, level: int, why: str) -> None:
        self._close()
        log.log(level, "The live stream capture %s stopped %s.", self.name, why)

    def _failed(self, error: Exception) -> None:
        # An OSError's text names the path: only its type is told.
        self._close()
        log.warning(
            "The live stream capture %s stopped: it could not be written (%s).",
            self.name,
            type(error).__name__,
        )


def _guarded(
    http: _Http, inner: Iterator[Chunk], capture: _Capture | None = None
) -> Iterator[Chunk]:
    """End quietly when closed; turn every other failure into a context-free SourceError.
    The capture, if any, is closed with the source, in the reading thread."""
    failure: SourceError | None = None
    try:
        yield from inner
    except _Closed:
        return
    except Exception as error:
        if http.closed:
            return
        failure = _translate(error)
    finally:
        try:
            http.close()
        finally:
            if capture is not None:
                capture.close()
    if failure is not None:
        raise failure


class TsSource:
    """Raw MPEG-TS from one HTTP(S) response; reconnects after the connection breaks.

    Only a body with a Content-Length ends the input when complete; every other end
    reconnects while the backoff schedule allows.
    """

    hls = False

    def __init__(
        self,
        http: _Http,
        url: str,
        response: Any,
        prefix: bytes,
        *,
        capture: _Capture | None = None,
    ) -> None:
        self.user_agent = http.user_agent
        http.stall_after(_TS_STALL_TIMEOUT)
        self._http = http
        self._url = url
        self._response = response
        self._prefix = prefix
        self._prefix_at = time.monotonic()  # the open has just read it
        self._capture = capture

    @property
    def burst_s(self) -> float:
        return 0.0

    def chunks(self) -> Iterator[Chunk]:
        return _guarded(self._http, self._run(), self._capture)

    def close(self) -> None:
        self._http.close()
        if self._capture is not None:
            self._capture.flush()

    def wait_closed(self, timeout: float) -> bool:
        if self._capture is not None:
            self._capture.flush()
        return self._http.wait_closed(timeout)

    def _run(self) -> Iterator[bytes | Discontinuity | Reconnecting]:
        http, response, prefix, capture = self._http, self._response, self._prefix, self._capture
        self._response, self._prefix = None, b""
        if response is None:
            return
        backoff = _Backoff()
        connected, received, reconnected = time.monotonic(), 0, False
        if prefix:
            if capture is not None:
                capture.write(prefix, self._prefix_at)
            yield prefix
        while True:
            try:
                while block := http.read(response):
                    if capture is not None:
                        capture.write(block)
                    if reconnected:
                        reconnected = False
                        yield Discontinuity()
                    received += len(block)
                    if (
                        received >= _HEALTHY_BYTES
                        and time.monotonic() - connected >= _HEALTHY_SECONDS
                    ):
                        backoff.reset()
                    yield block
                if _sized(response):
                    return  # a finite body reached its declared end
                # Any other end of a live body is a disconnect, also a clean one: a FIN, or
                # a chunked body's last chunk (nginx when the channel's upstream restarts).
                log.info("The channel closed its stream; reconnecting.")
            except _ReadError as error:
                log.info("The channel connection broke off (%s); reconnecting.", error.kind)
            finally:
                http.release(response)
            yield Reconnecting()  # before the backoff: the helper says so at once
            response = http.retry(lambda: http.get(self._url), backoff)
            if capture is not None:
                capture.mark("reconnect")
            connected, received, reconnected = time.monotonic(), 0, True


class LocalTsSource(TsSource):
    """A TsSource over a local file (tests only): no HTTP and no reconnects."""

    def __init__(self, file: BinaryIO) -> None:  # deliberately not TsSource.__init__
        self.user_agent = DEFAULT_USER_AGENT
        self._lock = threading.Lock()
        self._file: BinaryIO | None = file

    def chunks(self) -> Iterator[bytes | Discontinuity]:
        try:
            while True:
                with self._lock:
                    if self._file is None:
                        return
                    block = self._file.read(_READ_SIZE)
                if not block:
                    return
                yield block
        finally:
            self.close()

    def close(self) -> None:
        with self._lock:
            file, self._file = self._file, None
        if file is not None:
            file.close()

    def wait_closed(self, timeout: float) -> bool:
        return True  # no connection: close() is immediate


def _video_compatible(variant):
    codecs = variant.stream_info.codecs
    if not codecs:
        # Many IPTV masters omit CODECS. The recorder checks the actual streams.
        return True
    video = ("avc1", "avc3", "hvc1", "hev1", "h264", "hevc", "mp2v", "mpeg2")
    for codec in codecs.lower().split(","):
        codec = codec.strip()
        if codec.split(".", 1)[0] in video or codec.startswith(
            ("mp4v.60", "mp4v.61", "mp4v.62", "mp4v.63", "mp4v.64", "mp4v.65")
        ):
            return True
    return False


def _select_manifest(content, url):
    try:
        playlist = m3u8.loads(content.decode("utf-8-sig"), uri=url)
        if not playlist.is_variant:
            return playlist
        candidates = [item for item in playlist.playlists if _video_compatible(item)]
        if not candidates:
            raise SourceError("unsupported", _NO_VIDEO)
        selected = max(candidates, key=lambda item: item.stream_info.bandwidth or 0)
        groups = {
            "AUDIO": selected.stream_info.audio,
            "SUBTITLES": selected.stream_info.subtitles,
            "CLOSED-CAPTIONS": selected.stream_info.closed_captions,
            "VIDEO": selected.stream_info.video,
        }
        media = [
            item
            for item in playlist.media
            if groups.get(item.type) is not None and groups[item.type] == item.group_id
        ]
        # Alternate camera angles must not create competing video streams either.
        videos = [item for item in media if item.type == "VIDEO"]
        if videos:
            video = next((item for item in videos if item.default == "YES"), videos[0])
            media = [item for item in media if item.type != "VIDEO" or item is video]
        playlist.playlists[:] = [selected]
        playlist.media[:] = media
        selected.media = media
        playlist.iframe_playlists.clear()
        playlist.image_playlists.clear()
        # Steering could reintroduce discarded pathways/qualities.
        playlist.content_steering = None
        return playlist
    except SourceError:
        raise
    except Exception:
        raise SourceError("unsupported", _INVALID_PLAYLIST) from None


def _is_playlist(prefix: bytes) -> bool:
    return prefix.lstrip(b"\xef\xbb\xbf\r\n\t ").startswith(b"#EXTM3U")


def _resolve(base: str, uri: str | None) -> str:
    """``uri`` relative to the playlist's final (redirected) URL; only HTTP(S) is allowed."""
    try:
        target = urljoin(base, uri or "")
        scheme = _origin(target)[0]
    except ValueError:
        scheme = ""
        target = ""
    if not target:
        raise SourceError("unsupported", _INVALID_PLAYLIST)
    if scheme not in ("http", "https"):
        raise SourceError("unsupported", _NON_HTTP)
    return target


def _key_method(key: Any) -> str:
    return str(key.method or "NONE").upper() if key is not None else "NONE"


def _check_media(playlist: Any, base: str) -> None:
    """Reject what a TS-concatenating fetcher cannot play, before fetching any of it."""
    segments = playlist.segments
    if playlist.segment_map or any(
        segment.init_section is not None or segment.byterange for segment in segments
    ):
        raise SourceError("unsupported", _FRAGMENTED)
    for segment in segments:
        _resolve(base, segment.uri)
        method = _key_method(segment.key)
        if method == "AES-128":
            if not segment.key.uri:
                raise SourceError("unsupported", _UNDECRYPTABLE)
            _resolve(base, segment.key.uri)
        elif method != "NONE":
            raise SourceError("unsupported", _ENCRYPTION)


def _check_audio(playlist: Any) -> bool:
    """Only the default AUDIO rendition matters: whether it is a separate playlist (True), which
    a TS-only fetcher cannot mux. The variant may carry that audio too (some multi-track restream
    servers name each track's playlist and mux the first into the video's): its first segment
    decides (`HlsSource._own_audio`)."""
    audio = [item for item in playlist.media if item.type == "AUDIO"]
    if not audio:
        return False
    default = next((item for item in audio if item.default == "YES"), audio[0])
    dropped = [item.name or item.language or "unnamed" for item in audio if item.uri]
    if dropped:
        log.info("Alternate audio renditions are not time-shifted: %s.", ", ".join(dropped))
    return bool(default.uri)


def _sniff(http: _Http, response: Any) -> tuple[bytes, bytes | None]:
    """The first bytes, and the whole playlist when the body starts with ``#EXTM3U``."""
    prefix = http.read_prefix(response)
    if not _is_playlist(prefix):
        return prefix, None
    content = http.read_all(response, prefix, _MANIFEST_LIMIT)
    if len(content) > _MANIFEST_LIMIT:
        raise SourceError("unsupported", _TOO_LARGE)
    return prefix, content


def _load_playlist(http: _Http, url: str) -> tuple[bytes, str]:
    response = http.get(url)
    try:
        _prefix, content = _sniff(http, response)
        final = str(response.url)
    finally:
        http.release(response)
    if content is None:
        raise _NotPlaylist()
    return content, final


def _media_playlist(http: _Http, content: bytes, url: str) -> tuple[Any, str, bool]:
    """Follow the best variant down to a checked media playlist and its final URL; and
    whether a master on the way named a separate default audio playlist (`_check_audio`)."""
    separate = False
    for _hop in range(_PLAYLIST_HOPS):
        playlist = _select_manifest(content, url)
        if not playlist.is_variant:
            _check_media(playlist, url)
            return playlist, url, separate
        separate = _check_audio(playlist) or separate
        content, url = _load_playlist(http, _resolve(url, playlist.playlists[0].uri))
    raise SourceError("unsupported", _INVALID_PLAYLIST)


def _iv(key: Any, sequence: int) -> bytes:
    if not key.iv:
        return sequence.to_bytes(_BLOCK, "big")
    try:
        value = int(key.iv, 16)
    except ValueError:
        value = -1
    if not 0 <= value < 1 << 128:
        raise _BadSegment(_UNDECRYPTABLE)
    return value.to_bytes(_BLOCK, "big")


class _Decryptor:
    """AES-128-CBC as a stream: the last block waits for the segment's end, then is unpadded."""

    def __init__(self, key: bytes, iv: bytes) -> None:
        self._cipher = AES.new(key, AES.MODE_CBC, iv=iv)
        self._held = b""

    def update(self, data: bytes) -> bytes:
        data = self._held + data
        hold = len(data) % _BLOCK + _BLOCK
        if len(data) <= hold:
            self._held = data
            return b""
        self._held = data[-hold:]
        return self._cipher.decrypt(data[:-hold])

    def finish(self) -> bytes:
        if len(self._held) != _BLOCK:
            raise _BadSegment(_UNDECRYPTABLE)
        try:
            return unpad(self._cipher.decrypt(self._held), _BLOCK)
        except ValueError:
            pass
        raise _BadSegment(_UNDECRYPTABLE)


def _skippable(error: Exception) -> tuple[SourceError, bool] | None:
    """How a segment's key or request failure is skipped (failure, is-content), if it can be.

    A missing segment or key (404/410), a request that used up its reconnect schedule and
    damaged content are skippable; anything else ends the session.
    """
    if isinstance(error, _BadSegment):
        return SourceError("unsupported", error.message), True
    if isinstance(error, _Exhausted):
        return SourceError("network", _RECEIVE_FAILED), False
    if isinstance(error, requests.exceptions.HTTPError):
        status = _status(error)
        if status in _SKIPPABLE_STATUS:
            return SourceError("http", status=status), False
    return None


def _require_ts(data: bytes) -> None:
    if data[0] != 0x47 or (len(data) > _PACKET and data[_PACKET] != 0x47):
        raise _BadSegment(_NOT_TS)


def _checked_ts(blocks: Iterator[bytes]) -> Iterator[bytes]:
    """Pass blocks on once the segment's first bytes have TS sync at offsets 0 and 188."""
    head = b""
    checked = False
    for block in blocks:
        if not block:
            continue
        if checked:
            yield block
            continue
        head += block
        if len(head) > _PACKET:
            _require_ts(head)
            checked = True
            yield head
    if head and not checked:
        _require_ts(head)
        yield head


class HlsSource:
    """HLS of MPEG-TS segments: live reloads, AES-128 and discontinuity markers.

    Live failures are survived where a player would survive them:
    - a missing (404/410), gap-tagged, damaged or persistently failing segment (or key) is
      skipped with a marker; three failures in a row end the session;
    - a stale playlist copy is waited out; a media-sequence restart, or falling behind the
      window, resumes at the live start;
    - reloads that bring no new segment for 60 s (download time excluded) time out.

    A master that names a separate default audio playlist is followed to its variant; the
    variant's first segment then passes only when its program tables list audio of its own
    (`_own_audio`), else the channel is unsupported (nothing here muxes the separate audio).

    Segments are joined with each PID's continuity counter running on (`ContinuityJoiner`):
    some CDNs package every segment on its own, its counters restarting at 0, and VLC took
    each join for a break on every PID, losing its clock (2.0.2). At a discontinuity marker
    the counters run on too, and the first PCR after it carries the discontinuity indicator:
    the bytes tell VLC the time base changed (the marker itself only reaches the segmenter).
    """

    hls = True

    def __init__(
        self,
        http: _Http,
        playlist: Any,
        url: str,
        live_delay: float,
        target: float | None = None,
        extra: float = 0.0,
        *,
        separate_audio: bool = False,
        capture: _Capture | None = None,
    ) -> None:
        self.user_agent = http.user_agent
        self._http = http
        self._capture = capture
        self._playlist = playlist
        # The master named a separate default audio playlist: the first segment must show the
        # variant's own audio (`_own_audio`) before any of it passes.
        self._audio_unknown = separate_audio
        self._url = url
        self._live_delay = live_delay
        self._target = target  # the session's target (None: start at live_delay only)
        self._extra = extra  # what the session adds to its target (Auto's learned extra)
        self._loaded = time.monotonic()
        self._burst = float(playlist.target_duration or 0)
        self._keys: dict[str, bytes] = {}
        self._recent: deque[str] = deque(maxlen=_RECENT_SEGMENTS)
        self._discontinuity_sequence = playlist.discontinuity_sequence
        self._failures = 0
        self._started = False  # bytes were yielded
        self._delivered = False  # a segment arrived whole
        self._pending = False  # a Discontinuity is due before the next bytes
        self._joiner = ContinuityJoiner()  # the segments' counters run on across joins
        self._serial = 0  # segments started so far
        self._fetching = 0.0  # the EXTINF of the segment being fetched (0.0: none)
        self._rest = 0.0  # the EXTINF of the listed segments after it
        self._last_whole: tuple[int, float] | None = None  # (serial, EXTINF) of the last whole
        self._finite = bool(playlist.is_endlist)  # the playlist is finished (ENDLIST)

    @property
    def burst_s(self) -> float:
        """The media playlist's target duration, updated on every reload."""
        return self._burst

    @property
    def segment_serial(self) -> int:
        """Counts the segments started: it changes as the next segment's first bytes come."""
        return self._serial

    @property
    def segment_s(self) -> float:
        """The EXTINF seconds of the segment being fetched; 0.0 once every listed one came."""
        return self._fetching

    @property
    def last_whole(self) -> tuple[int, float] | None:
        """(`segment_serial`, EXTINF) of the last segment that arrived whole: the helper
        measures the media/EXTINF ratio of those (2.1 stage 3: a wrong EXTINF moved the
        start). A segment that failed (404, skipped) is never one."""
        return self._last_whole

    @property
    def finite(self) -> bool:
        """The playlist is finished (ENDLIST): a clip, played from its beginning."""
        return self._finite

    @property
    def rest_s(self) -> float:
        """The EXTINF seconds of the listed segments after the one being fetched: with what
        that one still holds, the media to come before the source has caught up (2.1 stage 3:
        the helper predicts the live edge then, so a tune need not wait for all of it)."""
        return self._rest

    def chunks(self) -> Iterator[Chunk]:
        return _guarded(self._http, self._run(), self._capture)

    def close(self) -> None:
        self._http.close()
        if self._capture is not None:
            self._capture.flush()

    def wait_closed(self, timeout: float) -> bool:
        if self._capture is not None:
            self._capture.flush()
        return self._http.wait_closed(timeout)

    def _mark(self) -> None:
        # A break before the first segment needs no marker.
        self._pending = self._pending or self._started

    def _start(self, playlist: Any) -> int:
        """The first segment to fetch: `_depth` seconds from a live playlist's end."""
        if playlist.is_endlist:
            return 0
        depth = self._depth(playlist)
        total = 0.0
        for index in range(len(playlist.segments) - 1, -1, -1):
            total += float(playlist.segments[index].duration or 0)
            if total >= depth:
                return index
        return 0

    def _depth(self, playlist: Any) -> float:
        """How far back from a live playlist's end the start goes (2.1 stage 3).

        Without the session's target, ``live_delay``. With it, the target (the source's
        segment duration, when that is larger, under any latency) and one and a half segments
        more (a start at the target is half a segment further back than it, the target being
        the mean lag over a batch, and one segment more is the room to choose), and at least
        ``live_delay`` and the playlist's HOLD-BACK, or three target durations without one
        (the HLS spec's advice); plus the session's extra. A sparse-PCR channel's 10 s
        segments fetched from 6 s before the end gave one segment, and VLC ran dry before
        the next landed.
        """
        if self._target is None:
            return self._live_delay
        duration = float(playlist.target_duration or 0)
        control = playlist.server_control
        hold_back = float(control.hold_back or 0) if control is not None else 0.0
        advised = hold_back if hold_back > 0 else 3 * duration
        room = max(self._target, duration) + 1.5 * duration
        return max(self._live_delay, advised, room) + max(0.0, self._extra)

    def _restarted(self, playlist: Any, base: str, stalled: float) -> bool:
        """For a playlist whose newest segment is older than the last one fetched: whether
        the stream restarted (True) or a lagging server sent a stale copy (False).

        Segments are compared by name without rotating tokens (path prefix, query). An
        empty copy, or one with a lower discontinuity sequence, is stale; ``stalled`` is the
        time since the last reload that brought new segments finished downloading them.
        """
        if not playlist.segments:
            return False
        names = {_identity(_resolve(base, segment.uri)) for segment in playlist.segments}
        if names.isdisjoint(self._recent):
            return True
        if (playlist.discontinuity_sequence or 0) > (self._discontinuity_sequence or 0):
            return True
        return stalled >= _STALE_TARGETS * float(playlist.target_duration or 1)

    def _run(self) -> Iterator[Chunk]:
        http, playlist, url = self._http, self._playlist, self._url
        loaded = progress = self._loaded
        next_sequence: int | None = None
        while True:
            first = playlist.media_sequence or 0
            newest = first + len(playlist.segments) - 1
            stale = False
            if next_sequence is None:
                next_sequence = first + self._start(playlist)
            elif first > next_sequence:
                # Fell behind the window: rejoin at the live start, not the oldest segment.
                log.info("The HLS playlist moved past %d segments.", first - next_sequence)
                self._mark()
                next_sequence = first + self._start(playlist)
            elif newest < next_sequence - 1:
                stale = not self._restarted(playlist, url, loaded - progress)
                if not stale:
                    log.info("The HLS media sequence restarted.")
                    self._mark()
                    next_sequence = first + self._start(playlist)
            if not stale:
                self._discontinuity_sequence = playlist.discontinuity_sequence
            fresh = newest >= next_sequence
            self._rest = sum(
                float(segment.duration or 0)
                for index, segment in enumerate(playlist.segments)
                if first + index >= next_sequence and not segment.gap_tag
            )
            # Frozen time runs between reloads only, never while segments download.
            if not fresh and loaded - progress >= _FROZEN_TIMEOUT:
                raise SourceError("timeout", _FROZEN)
            for index, segment in enumerate(playlist.segments):
                sequence = first + index
                if sequence < next_sequence:
                    continue
                next_sequence = sequence + 1
                if segment.discontinuity:
                    self._mark()
                if segment.gap_tag:
                    log.info("Skipped an HLS segment tagged as a gap.")
                    self._mark()
                    continue
                self._fetching = float(segment.duration or 0)
                self._rest = max(0.0, self._rest - self._fetching)
                self._serial += 1
                yield from self._segment(segment, sequence, url)
            self._fetching = 0.0
            if fresh:
                progress = time.monotonic()
                yield CaughtUp()  # what follows comes as the provider publishes it
            if playlist.is_endlist:
                return
            interval = max(1.0, float(playlist.target_duration or 0) / 2)
            if http.wait(interval - (time.monotonic() - loaded)):
                raise _Closed()
            loaded = time.monotonic()
            content, final = http.fetch(lambda: _load_playlist(http, url))
            playlist, url, _separate = _media_playlist(http, content, final)
            self._burst = float(playlist.target_duration or 0)
            self._finite = bool(playlist.is_endlist)

    def _segment(self, segment: Any, sequence: int, base: str) -> Iterator[bytes | Discontinuity]:
        uri = _resolve(base, segment.uri)
        self._recent.append(_identity(uri))
        skipped: tuple[SourceError, bool] | None = None
        try:
            decryptor = self._decryptor(segment.key, sequence, base)
            response = self._http.fetch(lambda: self._http.get(uri, headers=_IDENTITY))
        except Exception as error:
            skipped = _skippable(error)
            if skipped is None:
                raise
        if skipped is not None:
            self._skip(*skipped)
            return
        failure: SourceError | None = None
        content = False
        joiner = self._joiner
        joiner.segment()
        if self._capture is not None:
            if self._pending:
                self._capture.mark("discontinuity")
            self._capture.mark("segment")
        try:
            blocks = _checked_ts(self._blocks(response, decryptor))
            if self._audio_unknown:
                blocks = self._own_audio(blocks)
            for block in blocks:
                if self._pending:  # only ever right before bytes that really arrive
                    self._pending = False
                    joiner.discontinuity()
                    yield Discontinuity()
                self._started = True
                if block := joiner.feed(block):
                    yield block
            if tail := joiner.end():  # a partial packet at the segment's end, as it came
                yield tail
        except _ReadError as error:
            failure = _translate(error)
        except _BadSegment as error:
            failure, content = SourceError("unsupported", error.message), True
        finally:
            self._http.release(response)
        if failure is None:
            self._failures = 0
            self._delivered = True
            self._last_whole = (self._serial, float(segment.duration or 0))
        else:
            self._skip(failure, content)

    def _own_audio(self, blocks: Iterator[bytes]) -> Iterator[bytes]:
        """A segment's blocks, held until its program tables show whether the variant carries
        audio of its own (2.1: public channels VLC played were refused for their master alone,
        which named a separate default audio playlist the variant also muxed). Without such
        audio by `_AUDIO_LOOK` bytes (or the segment's end), the channel is unsupported: a
        TS-only fetcher cannot mux the separate playlist. Asked of the first whole segment
        only (a missing one is skipped as any other, and the next one asked)."""
        head = b""
        audio: bool | None = None
        for block in blocks:
            head += block
            audio = program_has_audio(head)
            if audio is not None or len(head) >= _AUDIO_LOOK:
                break
        if not audio:
            raise _BadSegment(_SEPARATE_AUDIO)
        self._audio_unknown = False
        log.info("The HLS variant carries audio of its own; its separate playlist is left out.")
        yield head
        yield from blocks

    def _skip(self, failure: SourceError, content: bool = False) -> None:
        """Skip a segment with a marker, or end the session: on bad content before any
        segment arrived whole (it is unsupported), and on a third failure in a row."""
        self._failures += 1
        self._fetching = 0.0  # it leaves the backlog: what it holds never comes
        if (content and not self._delivered) or self._failures >= _SEGMENT_FAILURES:
            raise failure
        log.info("Skipped an HLS segment (%s %s).", failure.kind, failure.status or "")
        self._mark()

    def _blocks(self, response: Any, decryptor: _Decryptor | None) -> Iterator[bytes]:
        capture = self._capture
        while block := self._http.read(response):
            if decryptor:
                block = decryptor.update(block)
            if capture is not None:
                capture.write(block)
            yield block
        if decryptor:
            block = decryptor.finish()
            if capture is not None:
                capture.write(block)
            yield block

    def _decryptor(self, key: Any, sequence: int, base: str) -> _Decryptor | None:
        if _key_method(key) == "NONE":
            return None
        uri = _resolve(base, key.uri)
        secret = self._keys.get(uri)
        if secret is None:
            secret = self._http.fetch(lambda: self._fetch_key(uri))
            if len(self._keys) >= _KEY_CACHE:
                self._keys.pop(next(iter(self._keys)))
            self._keys[uri] = secret
        return _Decryptor(secret, _iv(key, sequence))

    def _fetch_key(self, uri: str) -> bytes:
        response = self._http.get(uri, headers=_IDENTITY)
        try:
            secret = self._http.read_all(response, b"", _KEY_LIMIT)
        finally:
            self._http.release(response)
        if len(secret) != _BLOCK:
            raise _BadSegment(_UNDECRYPTABLE)
        return secret


def _open_local(url: str, scheme: str) -> Source:
    path = url2pathname(urlsplit(url).path) if scheme == "file" else url
    try:
        return LocalTsSource(open(path, "rb"))
    except OSError:
        pass  # the error text would name the path
    raise SourceError("unsupported")


def _names_a_playlist(url: str, response: Any) -> bool:
    """Whether an answer says, before its body, that it is an HLS playlist: by its content
    type, or by the path of the URL asked or reached."""
    kind = str(response.headers.get("Content-Type", "")).split(";", 1)[0].strip().lower()
    if kind in _PLAYLIST_TYPES:
        return True
    paths = (urlsplit(url).path, urlsplit(str(response.url)).path)
    return any(path.lower().endswith((".m3u8", ".m3u")) for path in paths)


def _answer(
    http: _Http, url: str, on_answer: Callable[[bool], None] | None
) -> tuple[Any, bytes, bytes | None]:
    """The entry URL's response, its first bytes and, for a playlist, all of it. As its
    headers come, ``on_answer(True)`` hears that the channel answered, unless the answer names
    an HLS playlist (`_names_a_playlist`): an on-demand raw-TS restream may hold its body back
    for seconds, while an HLS channel is waited for longer only once segment bytes flow. A body
    that fails, or ends before its first byte (a broken one), or turns out a playlist takes the
    answer back (``on_answer(False)``)."""
    response = http.get(url, read_5xx=True)
    report = on_answer if not _names_a_playlist(url, response) else None
    try:
        if report is not None:
            report(True)
        prefix, content = _sniff(http, response)
        if not prefix:
            raise _ReadError("network")
    except BaseException:
        http.release(response)
        if report is not None:
            report(False)  # taken back: asked again, or the end (review M2)
        raise
    if report is not None and content is not None:
        report(False)  # a playlist after all: no cold raw-TS restream (review M4)
    return response, prefix, content


def _first_answer(
    http: _Http, url: str, on_answer: Callable[[bool], None] | None
) -> tuple[Any, bytes, bytes | None]:
    """`_answer`, asked for again after each of the first `_OPEN_RETRIES` reconnect delays
    while it fails in passing (a 5xx or 408, a broken or refused connection, a body that ends
    before its first byte): an on-demand restream may refuse the request that starts it and
    serve the next, which VLC's own retry finds. Then the last failure stands as it is (an
    "http" one keeps its status). At once: an answer that means it (a 4xx, a limit, a 5xx whose
    body names the limit, TLS), a timeout (the full 60 s were waited already) and a host that
    does not resolve. A limit refusal met on asking again leaves the first failure standing
    (the panel counted the first answer; the channel plays directly, as before 2.1)."""
    delays = iter(_RECONNECT_DELAYS[:_OPEN_RETRIES])
    first: Exception | None = None
    while True:
        try:
            return _answer(http, url, on_answer)
        except Exception as error:
            if http.closed:
                raise _Closed() from None
            if first is not None and isinstance(error, SourceError) and str(error) in REFUSALS:
                # A panel that counted the first answer refuses the next for its limit: the
                # first failure stands, so the channel plays directly as before (review M1).
                raise first from None
            first = first or error
            timed_out = isinstance(error, requests.exceptions.Timeout) or (
                isinstance(error, _ReadError) and error.kind == "timeout"
            )
            passing = _retryable(error) and not timed_out and not isinstance(error, _LimitAnswer)
            if isinstance(error, requests.exceptions.ConnectionError) and _dns_failed(error):
                passing = False
            delay = next(delays, None) if passing else None
            if delay is None:
                raise
            log.info("The channel's first answer failed (%s); asking again.", type(error).__name__)
        if http.wait(delay):
            raise _Closed()


def _open_http(
    http: _Http,
    url: str,
    live_delay: float,
    target: float | None,
    extra: float,
    on_answer: Callable[[bool], None] | None = None,
    capture: _Capture | None = None,
) -> Source:
    response, prefix, content = _first_answer(http, url, on_answer)
    if content is None:
        return TsSource(http, url, response, prefix, capture=capture)  # it now owns the response
    try:
        final = str(response.url)
    finally:
        http.release(response)
    playlist, base, separate = _media_playlist(http, content, final)
    return HlsSource(
        http, playlist, base, live_delay, target, extra, separate_audio=separate, capture=capture
    )


def open_source(
    url: str,
    *,
    headers: Mapping[str, str],
    verify_ssl: bool,
    stop: threading.Event,
    live_delay: float = 6.0,
    target: float | None = None,
    extra: float = 0.0,
    allow_local: bool = False,
    on_answer: Callable[[bool], None] | None = None,
) -> Source:
    """Open ``url``, sniff it, and return a TS or HLS source.

    One request, unless the first answer fails in passing: a 5xx or 408, a broken or refused
    connection, or a body that ends before its first byte is asked for again after each of the
    first three reconnect delays (`_first_answer`). ``on_answer(True)`` is called as an
    answer's headers come, before its first byte, which an on-demand raw-TS restream may hold
    back for seconds (not for an answer that names an HLS playlist by its content type or
    path); ``on_answer(False)`` takes it back once its body failed or turned out a playlist.

    Provider ``headers``: only ``User-Agent`` (default ``VLC/3.0.20``) and ``Referer`` are
    used, and each goes with every request: playlists and segments, on any origin, and their
    redirect hops (2.1: an M3U channel's http-referrer, which a CDN's hotlink check may read,
    as VLC sends it). Every other provider header (Host, Range, Connection, Content-Length,
    Cookie, Authorization, ...) is dropped.

    A live HLS session starts at the first segment from which the playlist's remaining
    EXTINF durations add up to at least ``live_delay`` seconds (at least one segment); given
    the session's ``target`` (and the ``extra`` it adds, Auto's), that target and one and a
    half segments more, and at least HOLD-BACK or three target durations
    (`HlsSource._depth`). After each batch of listed segments it yields `CaughtUp`.
    Local paths and ``file://`` URLs are refused unless ``allow_local`` (tests only).

    Blocks while connecting. Setting ``stop`` (polled every 0.1 s) or calling ``close()`` on
    the result from another thread interrupts a TLS handshake, read or wait; a DNS lookup
    is not bounded and a TCP connect ends within the 60 s timeout per address. An
    interrupted open raises ``SourceError("network")``; an interrupted ``chunks()`` ends.

    With ``QITV_CAPTURE_DIR`` set, the source also saves what it reads there (`_Capture`).
    """
    try:
        scheme = _scheme(url)
        if scheme in ("http", "https"):
            _origin(url)  # validates the host and port
    except ValueError:
        scheme = "malformed"
    if scheme in ("", "file") and allow_local:
        return _open_local(url, scheme)
    if scheme not in ("http", "https"):
        raise SourceError("unsupported")
    http = _Http(headers, verify_ssl, stop)
    capture = _Capture.from_environment(url)
    try:
        return _open_http(http, url, live_delay, target, extra, on_answer, capture)
    except Exception as error:
        failure = SourceError("network", _STOPPED) if http.closed else _translate(error)
    except BaseException:
        http.close()
        raise
    http.close()
    raise failure
