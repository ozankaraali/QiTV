"""Network input for the live pipeline: raw MPEG-TS over HTTP(S), and HLS of TS or fragmented
MP4 segments.

A source yields the stream's bytes untouched plus ``Discontinuity`` markers; only fragmented
MP4 (CMAF) segments are remuxed, to TS (`services.live.fmp4`). HLS segments are fetched in
media-sequence order, AES-128 is decrypted as a stream, and media playlists are reloaded live.
Variant choice, redirect handling and error texts are ported from main's
``services/timeshift_source.py``; that version handed media to FFmpeg, which this pipeline
never uses.

Every request goes through one ``requests.Session`` per source, so cookies persist,
cross-origin redirects drop ``Authorization`` and the SSL setting applies on every hop and
nested resource. Connecting and each response's headers and first body byte may take 60 s;
after that, 10 s without a byte breaks the connection. ``close()`` may be called from any
thread, never blocks, and interrupts a TLS handshake, header read, body read or backoff
wait at once. A DNS lookup (``getaddrinfo``) is not bounded at all, and a TCP connect is
bounded only by the 60 s connect timeout per address. Credentials never reach error texts
or log lines: all ``SourceError`` messages are fixed strings. How ``close()`` wakes a
blocked call on each platform: `services.live.interruptible_http`. What a source reads can
be saved for replay: `services.live.capture`.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
import logging
import math
import os
import re
import threading
import time
from typing import Any, BinaryIO, Protocol, TypeVar
from urllib.parse import urljoin, urlsplit
from urllib.request import url2pathname

from Cryptodome.Cipher import AES
from Cryptodome.Util.Padding import unpad
import m3u8
import requests
from urllib3.exceptions import NameResolutionError, ReadTimeoutError

from services.live import interruptible_http
from services.live.capture import _Capture
from services.live.fmp4 import Fmp4Error, Fmp4Remuxer
from services.live.hls_audio import _LEAD_TICKS, AudioWeaver, video_start
from services.live.interruptible_http import _Closed, _Interrupter, _InterruptibleAdapter
from services.live.refusals import REFUSALS, STREAM_LIMIT, STREAM_REFUSED  # noqa: F401 (and here)
from services.live.ts import ContinuityJoiner, program_has_audio, pts_delta
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
# A separate audio playlist that does not list a video segment's audio yet: reloaded this many
# times, a target duration apart in all, before the segment plays silent.
_AUDIO_RELOADS = 4
# An audio segment found by its place in the playlist that starts this long after the video
# segment it is for (90 kHz): the one before it is taken too ...
_AUDIO_SLACK = 45000
_AUDIO_STEPS = 3  # ... and so on, this many segments back at most
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
_INIT_LIMIT = 4 * 1024 * 1024  # an fMP4 init segment (EXT-X-MAP): some kilobytes in practice
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
_BYTE_RANGES = "The HLS channel uses byte-range segments."
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
        if response is not None and not interruptible_http._INTERRUPT_OWNERS:
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


def _whole_segments(playlist: Any) -> Any:
    """A media playlist without the segment a low-latency (LL-HLS) playlist lists only as its
    parts, still being made: it has no URI of its own, and a later reload lists it whole."""
    playlist.segments[:] = [segment for segment in playlist.segments if segment.uri]
    return playlist


def _select_manifest(content, url):
    try:
        playlist = m3u8.loads(content.decode("utf-8-sig"), uri=url)
        if not playlist.is_variant:
            return _whole_segments(playlist)
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
    """Reject what this fetcher cannot play, before fetching any of it: byte ranges (of a
    segment or an init section), keys other than AES-128."""
    segments = playlist.segments
    maps = [segment.init_section for segment in segments if segment.init_section is not None]
    if any(segment.byterange for segment in segments) or any(
        init.byterange for init in [*maps, *(playlist.segment_map or ())]
    ):
        raise SourceError("unsupported", _BYTE_RANGES)
    for segment in segments:
        _resolve(base, segment.uri)
        if segment.init_section is not None:
            _resolve(base, segment.init_section.uri)
        method = _key_method(segment.key)
        if method == "AES-128":
            if not segment.key.uri:
                raise SourceError("unsupported", _UNDECRYPTABLE)
            _resolve(base, segment.key.uri)
        elif method != "NONE":
            raise SourceError("unsupported", _ENCRYPTION)


def _check_audio(playlist: Any, url: str) -> str | None:
    """Only the default AUDIO rendition matters: its playlist's URL when it is a separate one
    (2.2: woven into the variant's TS, `AudioWeaver`), else None. The variant may carry that
    audio too (some multi-track restream servers name each track's playlist and mux the first
    into the video's): its first segment decides (`HlsSource._with_audio`)."""
    audio = [item for item in playlist.media if item.type == "AUDIO"]
    if not audio:
        return None
    default = next((item for item in audio if item.default == "YES"), audio[0])
    dropped = [
        item.name or item.language or "unnamed"
        for item in audio
        if item.uri and item is not default
    ]
    if dropped:
        log.info("Alternate audio renditions are not time-shifted: %s.", ", ".join(dropped))
    return _resolve(url, default.uri) if default.uri else None


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


def _media_playlist(http: _Http, content: bytes, url: str) -> tuple[Any, str, str | None]:
    """Follow the best variant down to a checked media playlist and its final URL; and the
    separate default audio playlist a master on the way named (`_check_audio`), if any."""
    separate: str | None = None
    for _hop in range(_PLAYLIST_HOPS):
        playlist = _select_manifest(content, url)
        if not playlist.is_variant:
            _check_media(playlist, url)
            return playlist, url, separate
        separate = _check_audio(playlist, url) or separate
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

    Fragmented MP4 (CMAF) segments, a playlist with an init section (EXT-X-MAP), are fetched
    whole and remuxed to TS (`Fmp4Remuxer`), the init loaded whenever its URI changes: one
    encrypted (CENC, SAMPLE-AES) or of a codec not remuxed leaves the channel unsupported
    (direct play) at its first segment, as any segment that is not TS did before.

    A master that names a separate default audio playlist is followed to its variant. When the
    variant's first segment lists no audio of its own in its program tables, the separate
    playlist (MPEG-TS, packed ADTS or fMP4 segments, the last remuxed to TS first) is woven in
    (2.2, `_with_audio`): each video segment waits for the audio segment of the same media
    sequence number, and passes on as one TS with it (`AudioWeaver`); a missing audio segment
    leaves its video segment silent. An audio playlist that cannot be read, or whose fMP4
    init cannot be remuxed, leaves the channel unsupported, as before.

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
        separate_audio: str | None = None,
        capture: _Capture | None = None,
    ) -> None:
        self.user_agent = http.user_agent
        self._http = http
        self._capture = capture
        self._playlist = playlist
        # The master named a separate default audio playlist: the first segment shows whether
        # the variant carries audio of its own, or the separate one is woven in (`_with_audio`).
        self._audio_url = separate_audio
        self._audio_unknown = separate_audio is not None
        self._weaver: AudioWeaver | None = None
        self._audio_list: dict[int, Any] = {}  # the audio playlist's segments by sequence
        self._audio_base = ""
        self._audio_target = 0.0  # its target duration
        self._audio_next: int | None = None  # the audio segment to fetch next (None: find it)
        self._audio_ended = False  # it is finished (ENDLIST): no reload lists more
        # Fragmented MP4 remuxed to TS: the variant's, and the separate audio's (no PCR: its
        # PES are woven into the video's TS), each with the URI of the init it read last.
        self._remuxer = Fmp4Remuxer()
        self._init: str | None = None
        self._audio_remuxer = Fmp4Remuxer(pcr=False)
        self._audio_init: str | None = None
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
        fragmented = segment.init_section is not None
        try:
            if fragmented:
                self._init = self._load_init(self._remuxer, segment.init_section, base, self._init)
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
            self._capture.mark("segment", sequence)
        try:
            if fragmented:
                blocks = iter([self._remuxed(response, decryptor)])
            else:
                blocks = _checked_ts(self._blocks(response, decryptor, self._capture))
            if self._audio_unknown or self._weaver is not None:
                duration = float(segment.duration or 0)
                blocks = iter([self._with_audio(b"".join(blocks), sequence, duration)])
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

    def _remuxed(self, response: Any, decryptor: _Decryptor | None) -> bytes:
        """A whole fMP4 segment as TS (`Fmp4Remuxer`); captured as that TS, which the
        replay tool reads."""
        data = b"".join(self._blocks(response, decryptor, None))
        try:
            ts = self._remuxer.remux(data)
        except Fmp4Error as error:
            raise _BadSegment(str(error)) from None
        if self._capture is not None:
            self._capture.write(ts)
        return ts

    def _load_init(self, remuxer: Fmp4Remuxer, init: Any, base: str, loaded: str | None) -> str:
        """Read an fMP4 init section into `remuxer` unless its URI is `loaded` (the one read
        last); returns its URI. Raises `_BadSegment` when it cannot be remuxed."""
        uri = _resolve(base, init.uri)
        if uri == loaded:
            return uri
        data = self._http.fetch(lambda: self._fetch_small(uri, _INIT_LIMIT))
        try:
            remuxer.init(data)
        except Fmp4Error as error:
            raise _BadSegment(str(error)) from None
        return uri

    def _with_audio(self, video: bytes, sequence: int, duration: float) -> bytes:
        """A whole video segment with its sound: the first one's program tables show whether
        the variant carries audio of its own (2.1: some multi-track restream servers name a
        separate default audio playlist and mux it too), which leaves the separate playlist
        out; else that playlist is read (`_load_audio`: unreadable, or fMP4 audio whose init
        cannot be remuxed, the channel is unsupported) and from then on each segment's audio is
        woven in (2.2). Asked of the first whole segment only (a missing one is skipped as any
        other).

        The audio is matched by time, not by media sequence number (one packager numbers and
        cuts its renditions each its own way): the audio segments are fetched in order and
        queued until they reach the video segment's end (its first picture's time and EXTINF),
        and the video segment takes what is due before that (`AudioWeaver.weave_due`). The
        first audio segment, and the one after a break or one gone from the playlist, is found
        by its place from the playlist's end, as far back as the video segment's
        (`_audio_find`)."""
        if self._audio_unknown:
            if program_has_audio(video[:_AUDIO_LOOK]):
                self._audio_unknown = False
                log.info("The HLS variant carries audio of its own; its separate one is left out.")
                return video
            self._load_audio()
            first = next(iter(self._audio_list.values()), None)
            if first is not None and first.init_section is not None:
                try:
                    self._audio_init = self._load_init(
                        self._audio_remuxer, first.init_section, self._audio_base, None
                    )
                except _BadSegment:
                    raise _BadSegment(_SEPARATE_AUDIO) from None
            self._audio_unknown = False
            self._weaver = AudioWeaver()
            log.info("The HLS variant has no audio of its own; its separate playlist is woven in.")
        weaver = self._weaver
        assert weaver is not None
        start = video_start(video)
        if start is None:  # no picture's time to match: the audio of the same number
            audio = self._audio_segment(sequence)
            return weaver.weave(video, audio) if audio else video
        end = (start + round(duration * 90000)) % (1 << 33)
        if self._pending or self._audio_next is None:  # a break, or the first: found by time
            weaver.reset()
            self._audio_next = self._audio_find(start, duration)
        fetches = 2 + math.ceil(duration / max(1.0, self._audio_target))
        while self._audio_next is not None and fetches > 0:
            if weaver.reaches(end):
                break
            audio = self._audio_segment(self._audio_next)
            fetches -= 1
            if audio is None:
                if self._audio_list and self._audio_next < min(self._audio_list):
                    self._audio_next = self._audio_find(start, duration)  # fell behind it
                    continue
                break  # not listed yet: the next video segment asks again
            self._audio_next += 1
            if audio:
                weaver.add(audio)
        weaver.drop((start - _LEAD_TICKS) % (1 << 33))  # too late for VLC: left out
        return weaver.weave_due(video, end)

    def _audio_find(self, start: int, duration: float) -> int | None:
        """The audio segment to follow on from for a video segment that starts at `start`
        (`duration` long), queued; returns the one after it. Found by its place from the audio
        playlist's end, as far back as the video segment's from the video playlist's end, then
        up to `_AUDIO_STEPS` segments earlier while it starts later than the video segment (one
        packager's audio playlist runs 13 s ahead of its video's)."""
        weaver = self._weaver
        assert weaver is not None
        listed = sorted(self._audio_list)
        if not listed:
            return None
        back, chosen = self._rest + duration, listed[0]
        for sequence in reversed(listed):
            back -= float(self._audio_list[sequence].duration or 0)
            if back <= 0.001:
                chosen = sequence
                break
        audio = self._audio_segment(chosen)
        if not audio:
            return chosen + 1 if audio is not None else None
        found = [audio]
        times = weaver.peek(audio)
        earliest = chosen
        while (
            times is not None
            and pts_delta(start, times[0]) > _AUDIO_SLACK
            and earliest - 1 in self._audio_list
            and len(found) <= _AUDIO_STEPS
        ):
            earliest -= 1
            earlier = self._audio_segment(earliest)
            if not earlier:
                break
            found.insert(0, earlier)
            times = weaver.peek(earlier)
        for data in found:
            span = weaver.peek(data)
            if span is None or pts_delta(start, span[1]) >= 0:  # not over before the video
                weaver.add(data)
        return chosen + 1

    def _load_audio(self) -> None:
        """(Re)load the separate audio playlist; raise `_BadSegment` when it cannot be woven."""
        assert self._audio_url is not None
        url = self._audio_url
        try:
            content, final = self._http.fetch(lambda: _load_playlist(self._http, url))
            playlist = _whole_segments(m3u8.loads(content.decode("utf-8-sig"), uri=final))
        except _Closed:
            raise
        except Exception:
            raise _BadSegment(_SEPARATE_AUDIO) from None
        try:
            if playlist.is_variant:
                raise SourceError("unsupported")
            _check_media(playlist, final)
        except SourceError:
            raise _BadSegment(_SEPARATE_AUDIO) from None  # byte ranges, other keys: not read
        first = playlist.media_sequence or 0
        self._audio_list = {first + k: segment for k, segment in enumerate(playlist.segments)}
        self._audio_base = final
        self._audio_target = float(playlist.target_duration or 0)
        self._audio_ended = bool(playlist.is_endlist)

    def _audio_segment(self, sequence: int) -> bytes | None:
        """The audio segment of `sequence` (a live playlist that does not list it yet reloaded
        up to `_AUDIO_RELOADS` times), decrypted and as TS or packed audio; None when the
        playlist does not list it (not yet, or no more), b"" when it fails (the video plays
        without it)."""
        segment = self._audio_list.get(sequence)
        for attempt in range(_AUDIO_RELOADS):
            if segment is not None or self._audio_ended:
                break
            if self._audio_list and sequence < min(self._audio_list):
                break  # gone from its window already
            # At the live edge the audio playlist may list a segment a little after the video's
            # (another packager's): reloaded after a pause, up to a target duration in all.
            if attempt and self._http.wait(max(0.5, self._burst / _AUDIO_RELOADS)):
                raise _Closed()
            try:
                self._load_audio()
            except _BadSegment:
                return b""
            segment = self._audio_list.get(sequence)
        if segment is None:
            log.info("The HLS audio playlist does not list segment %d.", sequence)
            return None
        uri = _resolve(self._audio_base, segment.uri)
        try:
            decryptor = self._decryptor(segment.key, sequence, self._audio_base)
            response = self._http.fetch(lambda: self._http.get(uri, headers=_IDENTITY))
        except Exception as error:
            if _skippable(error) is None:
                raise
            return b""
        capture = self._capture.audio() if self._capture is not None else None
        if capture is not None:  # its own pair: the video's stays a playable TS
            capture.mark("segment", sequence)
        try:
            if segment.init_section is None:
                return b"".join(self._blocks(response, decryptor, capture))
            data = b"".join(self._blocks(response, decryptor, None))
            self._audio_init = self._load_init(
                self._audio_remuxer, segment.init_section, self._audio_base, self._audio_init
            )
            audio = self._audio_remuxer.remux(data)
            if capture is not None:  # as the TS it became: the replay weaves TS
                capture.write(audio)
            return audio
        except (_ReadError, _BadSegment, Fmp4Error) as error:
            log.info("An HLS audio segment could not be read (%s); it plays silent.", error)
            return b""
        except Exception as error:
            if _skippable(error) is None:
                raise
            return b""  # its init section is missing: silent, as a missing segment
        finally:
            self._http.release(response)

    def _skip(self, failure: SourceError, content: bool = False) -> None:
        """Skip a segment with a marker, or end the session: on bad content before any
        segment arrived whole (it is unsupported), and on a third failure in a row."""
        self._failures += 1
        self._fetching = 0.0  # it leaves the backlog: what it holds never comes
        if (content and not self._delivered) or self._failures >= _SEGMENT_FAILURES:
            raise failure
        log.info("Skipped an HLS segment (%s %s).", failure.kind, failure.status or "")
        self._mark()

    def _blocks(
        self, response: Any, decryptor: _Decryptor | None, capture: _Capture | None
    ) -> Iterator[bytes]:
        """A segment's bytes as read (decrypted), each saved in `capture` too, if any."""
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
        secret = self._fetch_small(uri, _KEY_LIMIT)
        if len(secret) != _BLOCK:
            raise _BadSegment(_UNDECRYPTABLE)
        return secret

    def _fetch_small(self, uri: str, limit: int) -> bytes:
        """A small resource (a key, an init section) whole, or just over `limit` bytes of it
        when it is longer (`_Http.read_all`)."""
        response = self._http.get(uri, headers=_IDENTITY)
        try:
            return self._http.read_all(response, b"", limit)
        finally:
            self._http.release(response)


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
