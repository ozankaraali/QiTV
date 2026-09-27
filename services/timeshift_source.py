"""Single-request HTTP ingestion and native, reloadable HLS source selection."""

import base64
from contextlib import ExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import secrets
import sys
import threading
from urllib.parse import urljoin, urlsplit

import certifi
import m3u8
import requests

_TIMEOUT = 60.0
_MANIFEST_LIMIT = 2 * 1024 * 1024
_NETWORK_PROTOCOLS = "http,https,tls,tcp,udp,rtp,crypto,httpproxy,rtmp,rtmps,rtmpt,rtmpts,srt"


class SourceError(RuntimeError):
    """A source failure whose explanation is safe to display without redaction."""


class _HTTPReader(io.RawIOBase):
    """Nonseekable, short-reading transport; never reopen the HTTP response."""

    def __init__(self, response):
        super().__init__()
        self.name = response.url
        self._response = response
        response.raw.decode_content = True

    def readable(self):
        return True

    def readinto(self, buffer):
        try:
            data = self._response.raw.read1(len(buffer), decode_content=True)
        except Exception:
            # urllib3 also raises its own exceptions, not just requests exceptions.
            raise SourceError("The channel connection failed while receiving media.") from None
        buffer[: len(data)] = data
        return len(data)

    def close(self):
        try:
            self._response.close()
        finally:
            super().close()


class _ReplayReader(io.RawIOBase):
    def __init__(self, prefix, stream):
        super().__init__()
        self.name = stream.name
        self._prefix = prefix
        self._stream = stream

    def readable(self):
        return True

    def readinto(self, buffer):
        if self._prefix:
            count = min(len(buffer), len(self._prefix))
            buffer[:count] = self._prefix[:count]
            self._prefix = self._prefix[count:]
            return count
        return self._stream.readinto1(buffer)


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
            raise SourceError("The HLS channel has no compatible video quality.")
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
        raise SourceError("The channel supplied an invalid HLS playlist.") from None


class _HLSBridge:
    """Verified upstream HTTP with native HLS decryption and playlist reloads.

    FFmpeg inherits HLS protocol options from its initial AVIOContext, not from
    a Python file's options. Worse, its inherited option list omits tls_verify.
    Thus every remote HLS resource uses requests; native FFmpeg sees only a
    private loopback HTTP endpoint. No media is cached here.
    """

    def __init__(self, session, verify_ssl, url, content):
        self._session = session
        self._verify_ssl = verify_ssl
        self._token = secrets.token_urlsafe(24)
        self._lock = threading.Lock()
        self._session_lock = threading.Lock()
        self.required_audio = 0
        self.required_subtitles = 0
        self._responses = set()
        self._stopped = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def setup(self):
                super().setup()
                self.connection.settimeout(5)

            def do_GET(self):
                try:
                    pieces = urlsplit(self.path).path.split("/")
                    if len(pieces) != 4 or not secrets.compare_digest(pieces[1], owner._token):
                        self.send_error(404)
                        return
                    target = base64.urlsafe_b64decode(pieces[2].encode("ascii")).decode("utf-8")
                    if urlsplit(target).scheme.lower() not in ("http", "https"):
                        self.send_error(403)
                        return
                    owner._serve(self, target)
                except BrokenPipeError, ConnectionResetError, TimeoutError:
                    pass
                except Exception:
                    # Never echo upstream URLs, credentials, TLS details, or paths.
                    self.close_connection = True
                    try:
                        self.send_error(502, "Channel resource unavailable")
                    except OSError:
                        pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._server.block_on_close = False
        try:
            self._initial: tuple[str, bytes] | None = (url, self._manifest(content, url))
        except Exception:
            self._server.server_close()
            raise
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self._thread.start()
        self.url = self._url(url)

    def _url(self, url):
        if urlsplit(url).scheme.lower() not in ("http", "https"):
            raise SourceError("The HLS channel references a non-HTTP resource.")
        encoded = base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii")
        # The extension is only advisory: upstream resources need not have suffixes.
        return f"http://127.0.0.1:{self._server.server_port}/{self._token}/{encoded}/resource"

    def _manifest(self, content, url):
        playlist = _select_manifest(content, url)
        if playlist.is_variant:
            self.required_audio = max(
                self.required_audio, sum(item.type == "AUDIO" for item in playlist.media)
            )
            self.required_subtitles = max(
                self.required_subtitles, sum(item.type == "SUBTITLES" for item in playlist.media)
            )
        resources = [
            *playlist.playlists,
            *playlist.media,
            *playlist.keys,
            *playlist.session_keys,
            *playlist.segments,
            *playlist.segment_map,
            *playlist.rendition_reports,
            *playlist.session_data,
            playlist.preload_hint,
        ]
        for segment in playlist.segments:
            resources.append(segment.init_section)
            resources.extend(segment.parts)
        seen = set()
        for item in resources:
            if item is None or id(item) in seen:
                continue
            seen.add(id(item))
            if getattr(item, "uri", None):
                item.uri = self._url(urljoin(url, item.uri))
        playlist.content_steering = None
        return playlist.dumps().encode("utf-8")

    def _serve(self, handler, target):
        with self._lock:
            initial = self._initial
            if initial and initial[0] == target:
                self._initial = None
            else:
                initial = None
        if initial:
            self._send_manifest(handler, initial[1])
            return
        headers = {"User-Agent": "libmpv", "Accept-Encoding": "identity"}
        if handler.headers.get("Range"):
            headers["Range"] = handler.headers["Range"]
        # Session preparation/cookie updates are serialized; response bodies stream
        # concurrently without the lock, so language tracks cannot block each other.
        with self._session_lock:
            if self._stopped.is_set():
                return
            response = self._session.get(
                target,
                stream=True,
                headers=headers,
                timeout=(_TIMEOUT, _TIMEOUT),
                verify=self._verify_ssl,
            )
        with self._lock:
            if self._stopped.is_set():
                response.close()
                return
            self._responses.add(response)
        try:
            with response, io.BufferedReader(_HTTPReader(response)) as stream:
                response.raise_for_status()
                prefix = stream.read(10)
                if prefix.lstrip(b"\xef\xbb\xbf\r\n\t ").startswith(b"#EXTM3U"):
                    content = prefix + stream.read(_MANIFEST_LIMIT + 1 - len(prefix))
                    if len(content) > _MANIFEST_LIMIT:
                        raise SourceError("The channel's HLS playlist is too large.")
                    self._send_manifest(handler, self._manifest(content, response.url))
                    return
                handler.send_response(response.status_code)
                handler.send_header("Connection", "close")
                if response.headers.get("Content-Range"):
                    handler.send_header("Content-Range", response.headers["Content-Range"])
                handler.end_headers()
                handler.wfile.write(prefix)
                while not self._stopped.is_set():
                    block = stream.read1(64 * 1024)
                    if not block:
                        break
                    handler.wfile.write(block)
                handler.close_connection = True
        finally:
            with self._lock:
                self._responses.discard(response)

    @staticmethod
    def _send_manifest(handler, content):
        handler.send_response(200)
        handler.send_header("Content-Type", "application/vnd.apple.mpegurl")
        handler.send_header("Content-Length", str(len(content)))
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.write(content)
        handler.close_connection = True

    def close(self):
        self._stopped.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(1)
        # The process supervisor bounds an upstream read/handshake that refuses to
        # return. No thread, session, or credential survives that child process.
        with self._lock:
            responses = tuple(self._responses)
        for response in responses:
            response.close()


def _native_options(verify_ssl, *, network):
    options = {
        "user_agent": "libmpv",
        "rw_timeout": str(int(_TIMEOUT * 1_000_000)),
        "tls_verify": "1" if verify_ssl else "0",
    }
    if network:
        options["protocol_whitelist"] = _NETWORK_PROTOCOLS
    # SecureTransport opens ca_file through FFmpeg's protocol whitelist, requiring
    # unsafe file access. Its native trust store is the correct macOS default.
    if sys.platform != "darwin":
        options["ca_file"] = certifi.where()
    return options


@contextmanager
def open_source(url: str, *, verify_ssl: bool = True):
    """Yield a real PyAV input while owning every HTTP and native resource.

    Only acquisition failures are translated here. Exceptions raised by the
    recorder inside the context retain their original type and meaning.
    """
    import av

    with ExitStack() as resources:
        try:
            scheme = urlsplit(url).scheme.lower()
            network = bool(scheme and scheme != "file")
            options = _native_options(verify_ssl, network=network)
            source: str | _ReplayReader = url
            source_format = None
            bridge = None
            if scheme in ("http", "https"):
                session = resources.enter_context(requests.Session())
                response = resources.enter_context(
                    session.get(
                        url,
                        stream=True,
                        timeout=(_TIMEOUT, _TIMEOUT),
                        verify=verify_ssl,
                        headers={"User-Agent": "libmpv"},
                    )
                )
                response.raise_for_status()
                stream = resources.enter_context(io.BufferedReader(_HTTPReader(response)))
                prefix = stream.read(10)
                if prefix.lstrip(b"\xef\xbb\xbf\r\n\t ").startswith(b"#EXTM3U"):
                    content = prefix + stream.read(_MANIFEST_LIMIT + 1 - len(prefix))
                    if len(content) > _MANIFEST_LIMIT:
                        raise SourceError("The channel's HLS playlist is too large.")
                    bridge = _HLSBridge(session, verify_ssl, response.url, content)
                    resources.callback(bridge.close)
                    stream.close()
                    source = bridge.url
                    source_format = "hls"
                    options.update(
                        {
                            "protocol_whitelist": "http,tcp,crypto",
                            "allowed_extensions": "ALL",
                            "extension_picky": "0",
                            "http_persistent": "0",
                        }
                    )
                else:
                    source = resources.enter_context(_ReplayReader(prefix, stream))
            container = av.open(
                source,
                format=source_format,
                options=options,
                timeout=(_TIMEOUT, _TIMEOUT),
            )
            resources.callback(container.close)
            if bridge is not None and (
                len(container.streams.audio) < bridge.required_audio
                or len(container.streams.subtitles) < bridge.required_subtitles
            ):
                raise SourceError("An HLS language track could not be opened for time-shift.")
        except SourceError:
            raise
        except requests.exceptions.SSLError:
            raise SourceError("The channel's TLS certificate could not be verified.") from None
        except requests.exceptions.Timeout:
            raise SourceError(
                "The channel did not respond before the connection timed out."
            ) from None
        except requests.exceptions.RequestException:
            raise SourceError("The channel could not be reached over HTTP.") from None
        except Exception:
            raise SourceError("The channel could not be opened for time-shift.") from None
        yield container
