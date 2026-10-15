"""Network input for the live pipeline: raw MPEG-TS over HTTP(S), and HLS of TS segments.

A source yields the stream's bytes untouched (no remux) plus ``Discontinuity`` markers. HLS
segments are fetched in media-sequence order, AES-128 is decrypted as a stream, and media
playlists are reloaded live. Variant choice, redirect handling and error texts are ported
from main's ``services/timeshift_source.py``; that version handed media to FFmpeg, which
this pipeline never uses.

Every request goes through one ``requests.Session`` per source, so cookies persist,
cross-origin redirects drop ``Authorization`` and the SSL setting applies on every hop and
nested resource. ``close()`` may be called from any thread and interrupts a TLS handshake,
header read, body read or backoff wait at once; a DNS lookup or TCP connect in progress is
bounded only by the 60 s connect timeout. Credentials never reach error texts or log lines:
all ``SourceError`` messages are fixed strings.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
import logging
import os
import socket
import threading
import time
from typing import Any, BinaryIO, Protocol, TypeVar, cast
from urllib.parse import urljoin, urlsplit
from urllib.request import url2pathname

from Cryptodome.Cipher import AES
from Cryptodome.Util.Padding import unpad
import m3u8
import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import ReadTimeoutError

from services.vlc_profile import DEFAULT_USER_AGENT

log = logging.getLogger(__name__)

_TIMEOUT = 60.0
_MANIFEST_LIMIT = 2 * 1024 * 1024
_READ_SIZE = 64 * 1024
_SNIFF_SIZE = 10
_PACKET = 188
_BLOCK = 16
_RECONNECT_DELAYS = (0.5, 1.0, 2.0, 4.0, 8.0, 8.0)
_STOP_POLL = 0.1
# Like FFmpeg (main's player), a live playlist starts three segments from its end.
_LIVE_START_SEGMENTS = 3
_PLAYLIST_HOPS = 4
_KEY_CACHE = 16
_KEY_LIMIT = 1024
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
_FRAGMENTED = "The HLS channel uses fragmented MP4 or byte-range segments."
_SEPARATE_AUDIO = "The HLS channel carries its audio in a separate playlist."
_ENCRYPTION = "The HLS channel uses an unsupported encryption method."
_UNDECRYPTABLE = "The HLS channel could not be decrypted."
_NOT_TS = "The HLS channel's segments are not MPEG-TS."

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
    ``"timeout"`` or ``"http"``.
    """

    def __init__(self, kind: str, message: str = "") -> None:
        if kind not in _DEFAULT_MESSAGES:
            raise ValueError(f"unknown source error kind {kind!r}")
        super().__init__(kind, message or _DEFAULT_MESSAGES[kind])
        self.kind = kind

    def __str__(self) -> str:
        return str(self.args[1])


@dataclass(frozen=True)
class Discontinuity:
    """A break in the byte stream: an HLS discontinuity, a sequence gap or a reconnect."""


class Source(Protocol):
    user_agent: str

    def chunks(self) -> Iterator[bytes | Discontinuity]: ...

    def close(self) -> None: ...


class _Closed(Exception):
    """The source was closed or stopped; iteration ends quietly."""


class _ReadError(Exception):
    """A response body broke off; ``kind`` is ``"timeout"`` or ``"network"``."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class _NotPlaylist(Exception):
    """A playlist URL answered with something else: unsupported at open, retried on reload."""


def _scheme(url: str) -> str:
    # A Windows drive letter ("C:\\...") is a path, not the URL scheme "c".
    return "" if os.path.splitdrive(url)[0] else urlsplit(url).scheme.lower()


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


class _Interrupter:
    """A duplicate descriptor per live connection, so another thread can shut it down.

    The duplicate shares the kernel socket: shutting it down wakes a TLS handshake, header
    read or body read blocked on the original descriptor. Duplicates are only closed under
    the lock, so a recycled descriptor number is never touched.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._handles: dict[int, socket.socket] = {}
        self._interrupted = False

    def track(self, connection: object, sock: socket.socket) -> None:
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


def _interruptible_pools(interrupter: _Interrupter) -> dict[str, type[HTTPConnectionPool]]:
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

        return cast(
            "type[HTTPConnection]",
            type(base.__name__, (base,), {"_new_conn": new_conn, "close": close}),
        )

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
        status = error.response.status_code if error.response is not None else 0
        return status >= 500 or status in (408, 429)
    return isinstance(error, (requests.exceptions.ConnectionError, requests.exceptions.Timeout))


def _translate(error: BaseException) -> SourceError:
    """A fresh, context-free error: nothing from a URL-bearing exception survives."""
    if isinstance(error, SourceError):
        return SourceError(error.kind, str(error))
    if isinstance(error, requests.exceptions.SSLError):
        return SourceError("tls")
    if isinstance(error, requests.exceptions.Timeout):
        return SourceError("timeout")
    if isinstance(error, _ReadError) and error.kind == "timeout":
        return SourceError("timeout")
    if isinstance(error, _ReadError):
        return SourceError("network", _RECEIVE_FAILED)
    if isinstance(error, _NotPlaylist):
        return SourceError("unsupported", _INVALID_PLAYLIST)
    if isinstance(error, requests.exceptions.HTTPError):
        return SourceError("http")
    if isinstance(error, requests.exceptions.RequestException):
        return SourceError("network")
    log.warning("A live source failed unexpectedly (%s).", type(error).__name__)
    return SourceError("unsupported")


class _Http:
    """One Session per source, interruptible from any thread by ``close()`` or ``stop``."""

    def __init__(self, headers: Mapping[str, str], verify_ssl: bool, stop: threading.Event):
        self._interrupter = _Interrupter()
        self._lock = threading.Lock()
        self._closed_event = threading.Event()
        self._response: Any = None
        self._verify_ssl = verify_ssl
        self._stop = stop
        self.closed = False
        agent = next(
            (value for name, value in headers.items() if name.lower() == "user-agent" and value),
            DEFAULT_USER_AGENT,
        )
        self.user_agent = agent
        self.session = requests.Session()
        adapter = _InterruptibleAdapter(self._interrupter)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self.session.hooks["response"].append(_close_redirect_body)
        self.session.headers.update(headers)
        self.session.headers["User-Agent"] = agent
        threading.Thread(target=self._watch, name="qitv-live-source-stop", daemon=True).start()

    def _watch(self) -> None:
        while not self._stop.wait(_STOP_POLL):
            if self.closed:
                return
        self.close()

    def get(self, url: str, headers: Mapping[str, str] | None = None) -> Any:
        """Send one GET; the response stays registered so ``close()`` can interrupt it."""
        if urlsplit(url).scheme.lower() not in ("http", "https"):
            raise SourceError("unsupported", _NON_HTTP)
        if self.closed:
            raise _Closed()
        try:
            response = self.session.get(
                url,
                stream=True,
                timeout=(_TIMEOUT, _TIMEOUT),
                verify=self._verify_ssl,
                headers=headers,
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
        except BaseException:
            self.release(response)
            raise
        return response

    def release(self, response: Any) -> None:
        with self._lock:
            if self._response is not response:
                return  # close() took it over
            self._response = None
        _quietly(response.close, "response")

    def read(self, response: Any) -> bytes:
        try:
            data = response.raw.read1(_READ_SIZE, decode_content=True)
        except Exception as error:
            if self.closed:
                raise _Closed() from None
            timeout = isinstance(error, (ReadTimeoutError, TimeoutError))
            raise _ReadError("timeout" if timeout else "network") from None
        if self.closed:
            raise _Closed()
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

    def retry(self, attempt: Callable[[], _T]) -> _T:
        """Repeat ``attempt`` after each reconnect delay; give up after the last one."""
        for number, delay in enumerate(_RECONNECT_DELAYS, 1):
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
                    number,
                    len(_RECONNECT_DELAYS),
                    type(error).__name__,
                )
        raise SourceError("network", _RECEIVE_FAILED)

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
        self._closed_event.set()
        self._interrupter.interrupt()
        if response is not None:
            # Shut the socket first: closing a response whose body another thread is
            # reading would otherwise wait for that read to finish.
            _quietly(response.raw.shutdown, "response socket")
            _quietly(response.close, "response")
        _quietly(self.session.close, "session")


def _guarded(
    http: _Http, inner: Iterator[bytes | Discontinuity]
) -> Iterator[bytes | Discontinuity]:
    """End quietly when closed; turn every other failure into a context-free SourceError."""
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
        http.close()
    if failure is not None:
        raise failure


class TsSource:
    """Raw MPEG-TS from one HTTP(S) response; reconnects after the connection breaks."""

    def __init__(self, http: _Http, url: str, response: Any, prefix: bytes) -> None:
        self.user_agent = http.user_agent
        self._http = http
        self._url = url
        self._response = response
        self._prefix = prefix

    def chunks(self) -> Iterator[bytes | Discontinuity]:
        return _guarded(self._http, self._run())

    def close(self) -> None:
        self._http.close()

    def _run(self) -> Iterator[bytes | Discontinuity]:
        http, response, prefix = self._http, self._response, self._prefix
        self._response, self._prefix = None, b""
        if response is None:
            return
        if prefix:
            yield prefix
        while True:
            try:
                while block := http.read(response):
                    yield block
                return  # a finite stream ended
            except _ReadError as error:
                log.info("The channel connection broke off (%s); reconnecting.", error.kind)
            finally:
                http.release(response)
            response = http.retry(lambda: http.get(self._url))
            yield Discontinuity()


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
    target = urljoin(base, uri or "")
    if urlsplit(target).scheme.lower() not in ("http", "https"):
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


def _load_playlist(http: _Http, url: str) -> tuple[bytes, str]:
    response = http.get(url)
    try:
        prefix = http.read_prefix(response)
        if not _is_playlist(prefix):
            raise _NotPlaylist()
        content = http.read_all(response, prefix, _MANIFEST_LIMIT)
        final = str(response.url)
    finally:
        http.release(response)
    if len(content) > _MANIFEST_LIMIT:
        raise SourceError("unsupported", _TOO_LARGE)
    return content, final


def _media_playlist(http: _Http, content: bytes, url: str) -> tuple[Any, str]:
    """Follow the best variant down to a checked media playlist and its final URL."""
    for _hop in range(_PLAYLIST_HOPS):
        playlist = _select_manifest(content, url)
        if not playlist.is_variant:
            _check_media(playlist, url)
            return playlist, url
        # A TS-only fetcher cannot mux a separate audio playlist into the video.
        if any(item.type == "AUDIO" and item.uri for item in playlist.media):
            raise SourceError("unsupported", _SEPARATE_AUDIO)
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
        raise SourceError("unsupported", _UNDECRYPTABLE)
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
            raise SourceError("unsupported", _UNDECRYPTABLE)
        try:
            return unpad(self._cipher.decrypt(self._held), _BLOCK)
        except ValueError:
            pass
        raise SourceError("unsupported", _UNDECRYPTABLE)


def _require_ts(data: bytes) -> None:
    if data[0] != 0x47 or (len(data) > _PACKET and data[_PACKET] != 0x47):
        raise SourceError("unsupported", _NOT_TS)


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
    """HLS of MPEG-TS segments: live reloads, AES-128 and discontinuity markers."""

    def __init__(self, http: _Http, playlist: Any, url: str) -> None:
        self.user_agent = http.user_agent
        self._http = http
        self._playlist = playlist
        self._url = url
        self._loaded = time.monotonic()
        self._keys: dict[str, bytes] = {}
        self._started = False
        self._pending = False

    def chunks(self) -> Iterator[bytes | Discontinuity]:
        return _guarded(self._http, self._run())

    def close(self) -> None:
        self._http.close()

    def _mark(self) -> None:
        # A break before the first segment needs no marker.
        self._pending = self._pending or self._started

    def _run(self) -> Iterator[bytes | Discontinuity]:
        http, playlist, url = self._http, self._playlist, self._url
        loaded = self._loaded
        next_sequence: int | None = None
        while True:
            first = playlist.media_sequence or 0
            if next_sequence is None:
                skip = 0
                if not playlist.is_endlist:
                    skip = max(0, len(playlist.segments) - _LIVE_START_SEGMENTS)
                next_sequence = first + skip
            elif first > next_sequence:
                log.info("The HLS playlist skipped %d segments.", first - next_sequence)
                self._mark()
                next_sequence = first
            for index, segment in enumerate(playlist.segments):
                sequence = first + index
                if sequence < next_sequence:
                    continue
                if segment.discontinuity:
                    self._mark()
                yield from self._segment(segment, sequence, url)
                next_sequence = sequence + 1
            if playlist.is_endlist:
                return
            interval = max(1.0, float(playlist.target_duration or 0) / 2)
            if http.wait(interval - (time.monotonic() - loaded)):
                raise _Closed()
            loaded = time.monotonic()
            content, final = http.fetch(lambda: _load_playlist(http, url))
            playlist, url = _media_playlist(http, content, final)

    def _segment(self, segment: Any, sequence: int, base: str) -> Iterator[bytes | Discontinuity]:
        uri = _resolve(base, segment.uri)
        decryptor = self._decryptor(segment.key, sequence, base)
        response = self._http.fetch(lambda: self._http.get(uri, headers=_IDENTITY))
        try:
            if self._pending:
                self._pending = False
                yield Discontinuity()
            for block in _checked_ts(self._blocks(response, decryptor)):
                self._started = True
                yield block
        except _ReadError as error:
            log.info("An HLS segment broke off (%s); continuing with the next.", error.kind)
            self._mark()
        finally:
            self._http.release(response)

    def _blocks(self, response: Any, decryptor: _Decryptor | None) -> Iterator[bytes]:
        while block := self._http.read(response):
            yield decryptor.update(block) if decryptor else block
        if decryptor:
            yield decryptor.finish()

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
            raise SourceError("unsupported", _UNDECRYPTABLE)
        return secret


def _open_local(url: str, scheme: str) -> Source:
    path = url2pathname(urlsplit(url).path) if scheme == "file" else url
    try:
        return LocalTsSource(open(path, "rb"))
    except OSError:
        pass  # the error text would name the path
    raise SourceError("unsupported")


def _open_http(http: _Http, url: str) -> Source:
    response = http.get(url)
    try:
        prefix = http.read_prefix(response)
        if not _is_playlist(prefix):
            return TsSource(http, url, response, prefix)  # it now owns the response
        content = http.read_all(response, prefix, _MANIFEST_LIMIT)
        final = str(response.url)
    except BaseException:
        http.release(response)
        raise
    http.release(response)
    if len(content) > _MANIFEST_LIMIT:
        raise SourceError("unsupported", _TOO_LARGE)
    playlist, base = _media_playlist(http, content, final)
    return HlsSource(http, playlist, base)


def open_source(
    url: str, *, headers: Mapping[str, str], verify_ssl: bool, stop: threading.Event
) -> Source:
    """Open ``url`` (one request), sniff it, and return a TS or HLS source.

    Blocks while connecting. Setting ``stop`` (polled every 0.1 s) or calling ``close()`` on
    the result from another thread interrupts a handshake, read or wait; DNS lookups and TCP
    connects end within the 60 s connect timeout. An interrupted open raises
    ``SourceError("network")``; an interrupted ``chunks()`` simply ends.
    """
    scheme = _scheme(url)
    if scheme in ("", "file"):
        return _open_local(url, scheme)
    if scheme not in ("http", "https"):
        raise SourceError("unsupported")
    http = _Http(headers, verify_ssl, stop)
    try:
        return _open_http(http, url)
    except Exception as error:
        failure = SourceError("network", _STOPPED) if http.closed else _translate(error)
    except BaseException:
        http.close()
        raise
    http.close()
    raise failure
