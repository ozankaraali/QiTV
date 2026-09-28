"""Private, bounded, temporary MPEG-TS ring for the internal player.

The QThread supervises an isolated recording process; native source I/O never
holds up cancellation. Playback exposes only completed segments on loopback.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from fractions import Fraction
from http.server import BaseHTTPRequestHandler
import math
import multiprocessing
from multiprocessing.connection import wait as wait_for_connection
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import threading
import time

from PySide6.QtCore import QLockFile, QThread, Signal

from services.timeshift_source import LoopbackHTTPServer, SourceError, open_source

_SESSION_PREFIX = ".qitv-timeshift-"
_SESSION_PATTERN = re.compile(r"\.qitv-timeshift-[0-9a-f]{32}")
_MEDIA_PATTERN = re.compile(r"segment-[0-9]+\.ts")
_OWNER = "QiTV temporary time-shift v1\n"
_RESERVE_BYTES = 64 * 1024 * 1024
_SEGMENT_SECONDS = 2.0
_IO_TIMEOUT = 2.0
_READ_SIZE = 64 * 1024
_STOP_GRACE = 0.5
_TERMINATE_GRACE = 0.5
_MPEGTS_TIME_BASE = Fraction(1, 90000)
_VIDEO_CODECS = frozenset({"h264", "hevc", "mpeg2video"})
_AUDIO_CODECS = frozenset({"aac", "mp2", "mp3", "ac3", "eac3", "dts"})
_SUBTITLE_CODECS = frozenset({"dvb_subtitle", "dvb_teletext"})


class _Failure(Exception):
    """An explanation that never contains provider data or filesystem paths."""


class _Stopped(Exception):
    pass


@dataclass
class _ClockEpoch:
    offset: int = 0


@dataclass
class _StreamClock:
    epoch: _ClockEpoch
    raw_dts: int
    dts: int
    duration: int
    presentation_end: int
    has_dts: bool


class _RecordingClock:
    """Map source clock epochs onto one continuous 90-kHz recording timeline."""

    def __init__(self, video_index: int) -> None:
        self._video_index = video_index
        self._epoch = _ClockEpoch()
        self._streams: dict[int, _StreamClock] = {}
        self._video_next: int | None = None

    def apply(self, packet) -> None:
        if packet.time_base != _MPEGTS_TIME_BASE:
            scale = packet.time_base / _MPEGTS_TIME_BASE

            def rescale(value):
                if value is None:
                    return None
                numerator = value * scale.numerator
                rounded = (abs(numerator) + scale.denominator // 2) // scale.denominator
                return rounded if numerator >= 0 else -rounded

            packet.pts = rescale(packet.pts)
            packet.dts = rescale(packet.dts)
            packet.duration = rescale(packet.duration)
            packet.time_base = _MPEGTS_TIME_BASE

        index = packet.stream.index
        raw = packet.dts if packet.dts is not None else packet.pts
        assert raw is not None
        last = self._streams.get(index)
        duration = packet.duration or (last.duration if last else 0)
        if not duration and last and raw > last.raw_dts:
            duration = raw - last.raw_dts
        duration = max(1, duration)
        epoch = last.epoch if last else self._epoch
        reference = last.dts + last.duration if last else self._video_next
        if reference is not None and self._video_next is not None:
            reference = max(reference, self._video_next)

        # Demux interleave can deliver old audio after the first new video
        # packet, or new audio before video. Join the shared epoch only when
        # its clock is closer; never shift still-arriving old-epoch packets.
        if reference is not None and epoch is not self._epoch:
            if abs(raw + self._epoch.offset - reference) < abs(raw + epoch.offset - reference):
                epoch = self._epoch
        if last and packet.stream.type in ("video", "audio"):
            regressed = (
                packet.dts is not None
                and last.has_dts
                and last.raw_dts - raw > max(last.duration, duration)
            )
            if index == self._video_index and packet.is_keyframe and packet.pts is not None:
                regressed |= packet.pts + last.epoch.offset < last.presentation_end
            if regressed and epoch is last.epoch:
                epoch = _ClockEpoch(last.dts + last.duration - raw)
                self._epoch = epoch

        if last and index == self._video_index and epoch is not last.epoch:
            # Video may have decode reordering or a changed reorder delay.
            # Refine the shared offset, not independent offsets for each
            # language. Tracks already in this epoch follow this refinement.
            epoch.offset = max(epoch.offset, last.dts + last.duration - raw)
            if packet.pts is not None:
                epoch.offset = max(epoch.offset, last.presentation_end - packet.pts)
        if packet.pts is not None:
            packet.pts += epoch.offset
        if packet.dts is not None:
            packet.dts += epoch.offset
            if last and packet.dts <= last.dts:
                # Repeated AAC stamps/minor jitter are not clock resets.
                # Keep every frame and its presentation/decode relationship.
                correction = last.dts + 1 - packet.dts
                packet.dts += correction
                if packet.pts is not None:
                    packet.pts += correction

        dts = packet.dts if packet.dts is not None else packet.pts
        assert dts is not None
        presentation_end = (packet.pts if packet.pts is not None else dts) + duration
        if last:
            last.epoch = epoch
            last.raw_dts = raw
            last.dts = dts
            last.duration = duration
            last.presentation_end = max(last.presentation_end, presentation_end)
            last.has_dts = packet.dts is not None
        else:
            self._streams[index] = _StreamClock(
                epoch, raw, dts, duration, presentation_end, packet.dts is not None
            )
        if index == self._video_index:
            self._video_next = dts + duration


@dataclass
class _Segment:
    sequence: int
    start: float
    end: float
    path: Path
    size: int = 0
    allocated: int = 0
    readers: int = 0
    retired: bool = False


def _allocation_unit(directory: Path) -> int:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        sectors = wintypes.DWORD()
        bytes_per_sector = wintypes.DWORD()
        free = wintypes.DWORD()
        total = wintypes.DWORD()
        root = str(directory.resolve().anchor)
        if not ctypes.windll.kernel32.GetDiskFreeSpaceW(
            root,
            ctypes.byref(sectors),
            ctypes.byref(bytes_per_sector),
            ctypes.byref(free),
            ctypes.byref(total),
        ):
            raise _Failure("Cannot determine the temporary disk allocation size.")
        return sectors.value * bytes_per_sector.value
    return max(512, os.statvfs(directory).f_frsize)


def _owned_files(directory: Path, *, require_marker: bool = True) -> list[Path] | None:
    """Do not follow symlinks or remove anything outside our tiny file format."""
    try:
        files = list(directory.iterdir())
        if any(
            item.is_symlink()
            or not item.is_file()
            or not (item.name in {"owner", "session.lock"} or _MEDIA_PATTERN.fullmatch(item.name))
            for item in files
        ):
            return None
        if not require_marker:
            return files
        marker = directory / "owner"
        # Text-mode writes use CRLF on Windows; read_text normalizes both endings.
        if marker.stat().st_size not in (len(_OWNER), len(_OWNER) + 1):
            return None
        if marker.read_text(encoding="ascii") != _OWNER:
            return None
        return files
    except OSError, UnicodeError:
        return None


def _remove_stale_sessions(root: Path) -> None:
    for directory in root.iterdir():
        if (
            not _SESSION_PATTERN.fullmatch(directory.name)
            or directory.is_symlink()
            or not directory.is_dir()
            or _owned_files(directory) is None
        ):
            continue
        lock = QLockFile(str(directory / "session.lock"))
        # Age alone never makes a running session stale.
        lock.setStaleLockTime(0)
        if not lock.tryLock(0):
            continue
        try:
            files = _owned_files(directory)
            if files is not None:
                for item in files:
                    if item.name != "session.lock":
                        item.unlink()
        finally:
            lock.unlock()
        if files is not None:
            directory.rmdir()


class _BoundedWriter:
    """Unbuffered AVIO sink: reserve disk blocks before every physical write."""

    def __init__(self, recorder: _RecordingEngine, segment: _Segment) -> None:
        self.recorder = recorder
        self.segment = segment
        self.file = segment.path.open("xb", buffering=0)

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def write(self, data: bytes) -> int:
        recorder = self.recorder
        if recorder._stop.is_set():
            raise _Stopped()
        with recorder._disk_lock:
            # AVIO may replace a Python callback exception with a generic
            # FFmpeg error while flushing. Preserve the safe disk failure.
            if recorder._write_failure:
                raise _Failure(recorder._write_failure)
            try:
                recorder._reserve_write(self.segment, len(data))
                written = self.file.write(data)
                if written is None or written != len(data):
                    raise _Failure("The temporary disk could not complete a buffer write.")
            except (_Failure, OSError) as error:
                recorder._write_failure = (
                    str(error)
                    if isinstance(error, _Failure)
                    else "The temporary disk could not complete a buffer write."
                )
                raise
            with recorder._state_lock:
                self.segment.size += written
                recorder._bytes += written
        recorder._notify()
        return written

    def close(self) -> None:
        self.file.close()


class _PlaylistServer(LoopbackHTTPServer):
    allow_reuse_address = False

    def __init__(self, recorder: _RecordingEngine) -> None:
        self.recorder = recorder
        self._sockets: set[socket.socket] = set()
        self._socket_lock = threading.Lock()
        super().__init__(("127.0.0.1", 0), _PlaylistHandler)
        self._thread = threading.Thread(
            target=self.serve_forever,
            kwargs={"poll_interval": 0.1},
            name="qitv-timeshift-http",
        )

    def get_request(self):
        client, address = super().get_request()
        if self.recorder._stop.is_set():
            client.close()
            raise OSError("server stopping")
        client.settimeout(_IO_TIMEOUT)
        with self._socket_lock:
            self._sockets.add(client)
        return client, address

    def close_request(self, request):
        with self._socket_lock:
            self._sockets.discard(request)
        super().close_request(request)

    def handle_error(self, request, client_address):
        # HTTP errors must not print credential-bearing request URLs.
        pass

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        # Interrupt partial request headers and blocked writes before joining.
        with self._socket_lock:
            for client in self._sockets:
                try:
                    client.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        if self._thread.is_alive():
            self.shutdown()
            self._thread.join()
        self.server_close()


class _PlaylistHandler(BaseHTTPRequestHandler):
    server: _PlaylistServer

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        self._serve(False)

    def do_HEAD(self):
        self._serve(True)

    def _headers(self, status: int, content_type: str, length: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()

    def _serve(self, head: bool) -> None:
        recorder = self.server.recorder
        prefix = f"/{recorder._token}/"
        if not self.path.startswith(prefix) or recorder._stop.is_set():
            self._headers(404, "text/plain", 0)
            return
        route = self.path[len(prefix) :]
        match = re.fullmatch(r"playlist/([0-9]{1,20})\.m3u8", route)
        if match:
            manifest = recorder._manifest(int(match[1]))
            if manifest is None:
                self._headers(503, "text/plain", 0)
                return
            self._headers(200, "application/vnd.apple.mpegurl", len(manifest))
            if not head:
                self.wfile.write(manifest)
            return
        match = re.fullmatch(r"segment/([0-9]{1,20})\.ts", route)
        segment = recorder._lease(int(match[1])) if match else None
        if segment is None:
            self._headers(404, "text/plain", 0)
            return
        try:
            self._headers(200, "video/mp2t", segment.size)
            if head:
                return
            offset = 0
            while offset < segment.size and not recorder._stop.is_set():
                # No open file spans a socket write. Leases remain charged to
                # the cap, and Windows never sees unlink of an open reader.
                with recorder._disk_lock:
                    with segment.path.open("rb") as source:
                        source.seek(offset)
                        data = source.read(min(_READ_SIZE, segment.size - offset))
                if not data:
                    break
                self.wfile.write(data)
                offset += len(data)
        finally:
            recorder._release(segment)


class _RecordingEngine:
    """Child-owned storage, remuxer and loopback server; no Qt event loop."""

    def __init__(
        self,
        url: str,
        *,
        session_directory: Path,
        max_bytes: int,
        verify_ssl: bool = True,
        stop,
        send,
    ) -> None:
        self._url = url
        self._session_directory = session_directory
        self._root = session_directory.parent
        self._max_bytes = max_bytes
        self._verify_ssl = verify_ssl
        self._stop = stop
        self._send = send
        self._state_lock = threading.Lock()
        self._disk_lock = threading.Lock()
        self._segments: dict[int, _Segment] = {}
        self._completed: deque[_Segment] = deque()
        self._pending_segments: list[tuple[int, float, float]] = []
        self._bytes = 0
        self._allocated = 0
        self._unit = 4096
        self._ended = False
        self._token = secrets.token_urlsafe(32)
        self._base_url = ""
        self._directory: Path | None = None
        self._session_lock: QLockFile | None = None
        self._write_failure: str | None = None
        self._last_notify = 0.0
        self._ready = False

    def _snapshot(self) -> dict:
        with self._state_lock:
            return {
                "start": self._completed[0].start if self._completed else 0.0,
                "end": self._completed[-1].end if self._completed else 0.0,
                "bytes": self._bytes,
                "ended": self._ended,
            }

    def _notify(self, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last_notify >= 0.25:
            self._last_notify = now
            with self._state_lock:
                additions = self._pending_segments
                self._pending_segments = []
                first = self._completed[0].sequence if self._completed else None
                state = {
                    "start": self._completed[0].start if self._completed else 0.0,
                    "end": self._completed[-1].end if self._completed else 0.0,
                    "bytes": self._bytes,
                    "ended": self._ended,
                }
                message = ("state", state, self._base_url, self._ready, first, additions)
            # Only newly completed segments cross IPC. Byte-only updates and
            # retirement cutoffs remain constant-size even for a long ring.
            self._send(message)

    def _prepare_directory(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        manager = QLockFile(str(self._root / ".qitv-timeshift-manager.lock"))
        manager.setStaleLockTime(0)
        deadline = time.monotonic() + _IO_TIMEOUT
        while not manager.tryLock(0):
            if self._stop.wait(0.05):
                raise _Stopped()
            if time.monotonic() >= deadline:
                raise _Failure("The temporary buffer directory is busy.")
        try:
            _remove_stale_sessions(self._root)
            directory = self._session_directory
            directory.mkdir(mode=0o700)
            self._directory = directory
            (directory / "owner").write_text(_OWNER, encoding="ascii")
            lock = QLockFile(str(directory / "session.lock"))
            lock.setStaleLockTime(0)
            if not lock.tryLock(0):
                raise _Failure("Cannot lock the temporary playback buffer.")
            self._session_lock = lock
            self._unit = _allocation_unit(directory)
            if shutil.disk_usage(directory).free < _RESERVE_BYTES:
                raise _Failure("Not enough free disk space for the temporary buffer.")
        finally:
            manager.unlock()

    def _delete_locked(self, segment: _Segment) -> None:
        # Caller owns disk lock. Metadata is removed before unlink so no new
        # reader can acquire a lease between the two operations.
        with self._state_lock:
            self._segments.pop(segment.sequence, None)
        segment.path.unlink()
        with self._state_lock:
            self._bytes -= segment.size
            self._allocated -= segment.allocated

    def _retire_oldest_locked(self) -> None:
        with self._state_lock:
            segment = self._completed.popleft()
            segment.retired = True
            delete = not segment.readers
        if delete:
            self._delete_locked(segment)

    def _reserve_write(self, segment: _Segment, length: int) -> None:
        new_size = segment.size + length
        charge = ((new_size + self._unit - 1) // self._unit) * self._unit
        additional = charge - segment.allocated
        while self._allocated + additional > self._max_bytes and self._completed:
            self._retire_oldest_locked()
        if self._allocated + additional > self._max_bytes:
            raise _Failure(
                "The disk buffer limit cannot fit one keyframe interval or an active read."
            )
        if additional and shutil.disk_usage(segment.path.parent).free < (
            _RESERVE_BYTES + additional
        ):
            raise _Failure("Recording stopped to preserve free disk space.")
        with self._state_lock:
            self._allocated += additional
            segment.allocated = charge

    def _lease(self, sequence: int) -> _Segment | None:
        with self._state_lock:
            segment = self._segments.get(sequence)
            if segment is None or segment.retired or segment not in self._completed:
                return None
            segment.readers += 1
            return segment

    def _release(self, segment: _Segment) -> None:
        with self._disk_lock:
            with self._state_lock:
                segment.readers -= 1
                delete = segment.retired and not segment.readers
            if delete:
                self._delete_locked(segment)

    def _manifest(self, first_sequence: int) -> bytes | None:
        with self._state_lock:
            segments = [item for item in self._completed if item.sequence >= first_sequence]
            if not segments:
                return None
            duration = max(1, math.ceil(max(item.end - item.start for item in segments)))
            lines = [
                "#EXTM3U",
                "#EXT-X-VERSION:3",
                f"#EXT-X-TARGETDURATION:{duration}",
                f"#EXT-X-MEDIA-SEQUENCE:{segments[0].sequence}",
                "#EXT-X-INDEPENDENT-SEGMENTS",
            ]
            for item in segments:
                lines.extend(
                    [
                        f"#EXTINF:{item.end - item.start:.6f},",
                        f"../segment/{item.sequence}.ts",
                    ]
                )
            if self._ended:
                lines.append("#EXT-X-ENDLIST")
        return ("\n".join(lines) + "\n").encode("ascii")

    def _open_segment(self, av, sequence: int, start: float, streams):
        assert self._directory is not None
        segment = _Segment(sequence, start, start, self._directory / f"segment-{sequence}.ts")
        with self._state_lock:
            self._segments[sequence] = segment
        writer = _BoundedWriter(self, segment)
        try:
            output = av.open(
                writer,
                mode="w",
                format="mpegts",
                options={
                    "mpegts_flags": "+resend_headers+initial_discontinuity",
                    "mpegts_copyts": "1",
                    "avoid_negative_ts": "disabled",
                    "flush_packets": "1",
                    "max_interleave_delta": "1000000",
                },
            )
            # Remux decoder parameters verbatim; no encoder is needed for a copy.
            mapping = {
                stream.index: output.add_stream_from_template(stream, opaque=True)
                for stream in streams
            }
            for stream in streams:
                mapping[stream.index].metadata.update(stream.metadata)
            return segment, writer, output, mapping
        except BaseException:
            writer.close()
            raise

    def _complete_segment(self, segment: _Segment, end: float) -> None:
        if not segment.size or end <= segment.start:
            raise _Failure("The source did not provide a usable timestamped segment.")
        with self._state_lock:
            segment.end = end
            self._completed.append(segment)
            self._pending_segments.append((segment.sequence, segment.start, segment.end))
            if segment.end - self._completed[0].start >= 4.0:
                self._ready = True
        self._notify(True)

    def _record(self, av, logs: list) -> None:
        with open_source(self._url, verify_ssl=self._verify_ssl) as source:
            videos = list(source.streams.video)
            audios = list(source.streams.audio)
            subtitles = list(source.streams.subtitles)
            if len(videos) != 1 or videos[0].codec_context.name not in _VIDEO_CODECS:
                raise _Failure("Time-shift requires one H.264, HEVC, or MPEG-2 video stream.")
            if any(stream.codec_context.name not in _AUDIO_CODECS for stream in audios):
                raise _Failure(
                    "An audio track cannot be remuxed safely to the temporary MPEG-TS buffer."
                )
            if any(stream.codec_context.name not in _SUBTITLE_CODECS for stream in subtitles):
                raise _Failure("An embedded subtitle track cannot be remuxed safely to MPEG-TS.")
            # Timed metadata/data (not selectable media) are deliberately omitted.
            streams = [videos[0], *audios, *subtitles]
            video_index = videos[0].index
            origin = None
            latest = 0.0
            sequence = 0
            clock = _RecordingClock(video_index)
            segment = writer = output = None
            mapping = {}
            try:
                for packet in source.demux(streams):
                    logs.clear()
                    if self._stop.is_set():
                        raise _Stopped()
                    if not packet.size:
                        continue
                    index = packet.stream.index
                    timestamp = packet.pts if packet.pts is not None else packet.dts
                    if timestamp is None or packet.time_base is None:
                        raise _Failure("The source has no usable media timestamps.")
                    if origin is None and (index != video_index or not packet.is_keyframe):
                        continue
                    clock.apply(packet)
                    timestamp = packet.pts if packet.pts is not None else packet.dts
                    seconds = float(timestamp * packet.time_base)
                    if origin is None:
                        origin = seconds
                        segment, writer, output, mapping = self._open_segment(
                            av, sequence, 0.0, streams
                        )
                    relative = seconds - origin
                    if relative < 0 and index != video_index:
                        continue
                    assert segment is not None and output is not None
                    if index == video_index and packet.is_keyframe:
                        if relative - segment.start >= _SEGMENT_SECONDS:
                            output.close()
                            output = None
                            writer.close()
                            writer = None
                            self._complete_segment(segment, relative)
                            sequence += 1
                            segment, writer, output, mapping = self._open_segment(
                                av, sequence, relative, streams
                            )
                    duration = float((packet.duration or 0) * packet.time_base)
                    latest = max(latest, relative + duration)
                    packet.stream = mapping[index]
                    output.mux(packet)
                if self._stop.is_set():
                    raise _Stopped()
                if output is None or segment is None:
                    raise _Failure("The source ended before a video keyframe was available.")
                output.close()
                output = None
                writer.close()
                writer = None
                self._complete_segment(segment, latest)
            finally:
                # Closing a muxer can flush bytes. The bounded writer remains
                # installed during close, including after errors/cancellation.
                try:
                    if output is not None:
                        output.close()
                finally:
                    if writer is not None:
                        writer.close()
        with self._state_lock:
            self._ended = True
        self._notify(True)

    def run(self) -> None:
        import av

        server = None
        try:
            if self._stop.is_set():
                return
            if self._max_bytes <= 0:
                raise _Failure("The temporary buffer size must be positive.")
            self._prepare_directory()
            server = _PlaylistServer(self)
            with self._state_lock:
                self._base_url = f"http://127.0.0.1:{server.server_port}/{self._token}"
            server.start()
            # Native diagnostics can contain the complete provider URL. Capture
            # on this worker and discard frequently; never log source errors.
            with av.logging.Capture(local=True) as logs:
                self._record(av, logs)
            self._stop.wait()
        except _Stopped:
            pass
        except (_Failure, SourceError) as error:
            if not self._stop.is_set():
                self._send(("error", str(error)))
        except av.FFmpegError:
            if not self._stop.is_set():
                self._send(
                    (
                        "error",
                        self._write_failure
                        or "The source could not be read or remuxed; it may be unavailable, "
                        "timed out, or incompatible with MPEG-TS.",
                    )
                )
        except OSError, ValueError, RuntimeError:
            if not self._stop.is_set():
                self._send(
                    ("error", "The temporary playback buffer could not be created or written.")
                )
        finally:
            self._stop.set()
            if server is not None:
                server.stop()
            with self._state_lock:
                self._base_url = ""
                self._completed.clear()
                self._segments.clear()
            try:
                if self._directory is not None:
                    # This process created the directory, even if writing its
                    # ownership marker failed because the disk filled up.
                    files = _owned_files(self._directory, require_marker=False)
                    if files is not None:
                        for item in files:
                            if item.name != "session.lock":
                                item.unlink()
                    if self._session_lock is not None:
                        self._session_lock.unlock()
                    if files is not None:
                        self._directory.rmdir()
            except OSError:
                self._send(("error", "Some temporary buffer files could not be removed."))
            finally:
                with self._state_lock:
                    self._bytes = 0
                    self._allocated = 0
                self._notify(True)


def _exit_with_owner() -> None:
    parent = multiprocessing.parent_process()
    if parent is not None:
        wait_for_connection([parent.sentinel])
        # A crashed owner cannot supervise a blocked FFmpeg call. Close all
        # sockets now; the next recorder safely reaps this owned stale session.
        os._exit(0)


def _record_process(url, session_directory, max_bytes, verify_ssl, stop, sender):
    """Spawn entry point: arguments travel through multiprocessing IPC, not argv."""
    try:
        # Capture/discarding Python FFmpeg logs is not enough: native libraries
        # may bypass logging and write URL-bearing diagnostics directly to fd 2.
        with open(os.devnull, "wb") as discard:
            os.dup2(discard.fileno(), 1)
            os.dup2(discard.fileno(), 2)
        threading.Thread(target=_exit_with_owner, daemon=True).start()
        engine = _RecordingEngine(
            url,
            session_directory=session_directory,
            max_bytes=max_bytes,
            verify_ssl=verify_ssl,
            stop=stop,
            send=sender.send,
        )
        engine.run()
    except BaseException:
        # Neither multiprocessing tracebacks nor native failures may disclose
        # provider URLs. The supervisor also detects an unexpected process exit.
        try:
            sender.send(("error", "The temporary recording process stopped unexpectedly."))
        except OSError, EOFError:
            pass
    finally:
        sender.close()


class TimeshiftRecorder(QThread):
    """Supervise one recording process without blocking GUI selection or stop.

    ``updated`` contains completed ``start``/``end`` seconds, all media bytes
    (including open writes and leased evictions), and ``ended``. Source I/O,
    filesystem work, IPC and child reaping never run on the GUI thread.
    """

    updated = Signal(object)
    errorOccurred = Signal(str)

    def __init__(
        self,
        url: str,
        *,
        directory: str,
        max_bytes: int,
        verify_ssl: bool = True,
    ) -> None:
        super().__init__()
        self._url = url
        self._directory = Path(directory) / f"{_SESSION_PREFIX}{secrets.token_hex(16)}"
        self._max_bytes = max_bytes
        self._verify_ssl = verify_ssl
        self._stop = threading.Event()
        self._state_lock = threading.Lock()
        self._completed: deque[tuple[int, float, float]] = deque()
        self._state = {"start": 0.0, "end": 0.0, "bytes": 0, "ended": False}
        self._base_url = ""
        self._ready = False
        self._child_pid: int | None = None

    def request_stop(self) -> None:
        # In particular, never signal a multiprocessing semaphore from Qt.
        self._stop.set()

    def playback(self, target: float | None = None) -> tuple[str, float, float] | None:
        with self._state_lock:
            if self._stop.is_set() or not self._completed or not self._base_url:
                return None
            if not self._ready and not self._state["ended"]:
                return None
            start, end = self._state["start"], self._state["end"]
            if target is None or not math.isfinite(target):
                target = end - 3.0
            target = min(max(target, start), max(start, end - 0.05))
            segment = self._completed[0]
            for candidate in reversed(self._completed):
                if candidate[1] <= target:
                    segment = candidate
                    break
            sequence, base, _ = segment
            return f"{self._base_url}/playlist/{sequence}.m3u8", base, target - base

    def _snapshot(self) -> dict:
        with self._state_lock:
            return self._state.copy()

    def _receive(self, message) -> bool:
        if message[0] == "error":
            if not self._stop.is_set():
                self.errorOccurred.emit(message[1])
            return True
        _, state, base_url, ready, first, additions = message
        with self._state_lock:
            if first is None:
                self._completed.clear()
            else:
                while self._completed and self._completed[0][0] < first:
                    self._completed.popleft()
                self._completed.extend(item for item in additions if item[0] >= first)
            self._state = state
            self._base_url = base_url
            self._ready = ready
        self.updated.emit(state)
        return False

    def _cleanup_session(self) -> None:
        # Called only after the child has exited. Its sockets and file handles
        # are then closed even if native open/demux never returned.
        directory = self._directory
        if directory.is_symlink():
            raise OSError("temporary session was replaced")
        if directory.exists():
            files = _owned_files(directory, require_marker=False)
            if files is None:
                raise OSError("temporary session contains unowned files")
            for item in files:
                item.unlink(missing_ok=True)
            directory.rmdir()
        # A forced stop may have interrupted directory preparation while the
        # child held the manager lock. QLockFile only reaps it if its owner died;
        # an active sibling's manager is never unlocked or removed.
        manager_path = directory.parent / ".qitv-timeshift-manager.lock"
        if manager_path.exists():
            manager = QLockFile(str(manager_path))
            manager.setStaleLockTime(0)
            if manager.tryLock(0):
                manager.unlock()

    def run(self) -> None:
        receiver = sender = process = None
        child_stop = None
        started = False
        failed = False
        try:
            if self._stop.is_set():
                return
            context = multiprocessing.get_context("spawn")
            receiver, sender = context.Pipe(duplex=False)
            child_stop = context.Event()
            process = context.Process(
                target=_record_process,
                args=(
                    self._url,
                    self._directory,
                    self._max_bytes,
                    self._verify_ssl,
                    child_stop,
                    sender,
                ),
                name="qitv-timeshift",
                daemon=True,
            )
            process.start()
            started = True
            self._child_pid = process.pid
            sender.close()
            deadline = None
            while True:
                if self._stop.is_set() and deadline is None:
                    child_stop.set()
                    deadline = time.monotonic() + _STOP_GRACE
                if deadline is not None and time.monotonic() >= deadline:
                    break
                try:
                    if receiver.poll(0.05):
                        failed = self._receive(receiver.recv()) or failed
                    elif not process.is_alive():
                        break
                except EOFError, BrokenPipeError:
                    # Windows can report peer closure from poll's PeekNamedPipe,
                    # before recv has an opportunity to translate it into EOF.
                    break
        except OSError, EOFError, ValueError, RuntimeError:
            if not self._stop.is_set():
                failed = True
                self.errorOccurred.emit("The temporary recording process could not be started.")
        finally:
            exited = True
            if started:
                # Native calls may ignore source timeouts and cooperative stop.
                # Termination happens here, never on the GUI thread.
                if process.is_alive():
                    child_stop.set()
                    process.join(_STOP_GRACE if not self._stop.is_set() else 0)
                if process.is_alive():
                    process.terminate()
                    process.join(_TERMINATE_GRACE)
                if process.is_alive():
                    process.kill()
                    process.join(_TERMINATE_GRACE)
                exited = not process.is_alive()
                if not exited:
                    self.errorOccurred.emit("The temporary recording process could not be stopped.")
                elif not self._stop.is_set() and not failed:
                    self.errorOccurred.emit("The temporary recording process stopped unexpectedly.")
            for connection in (receiver, sender):
                if connection is not None:
                    connection.close()
            if process is not None and exited:
                process.close()
            if exited:
                try:
                    self._cleanup_session()
                except OSError:
                    self.errorOccurred.emit("Some temporary buffer files could not be removed.")
            self._stop.set()
            with self._state_lock:
                self._base_url = ""
                self._completed.clear()
                self._state = {"start": 0.0, "end": 0.0, "bytes": 0, "ended": False}
            self.updated.emit(self._snapshot())
