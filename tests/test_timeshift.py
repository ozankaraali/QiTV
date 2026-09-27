from contextlib import contextmanager
from fractions import Fraction
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import multiprocessing
import os
from pathlib import Path
import secrets
import signal
import socket
from socketserver import BaseRequestHandler, ThreadingTCPServer
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urljoin
from urllib.request import urlopen

from PySide6.QtCore import Qt
import av

from services.timeshift import (
    _OWNER,
    TimeshiftRecorder,
    _BoundedWriter,
    _Failure,
    _RecordingClock,
    _RecordingEngine,
    _Segment,
)


def make_transport_stream(seconds=16, audio_codec="mp2", container="mpegts"):
    """Tiny real timestamped GOPs and two distinguishable audio tracks."""
    destination = io.BytesIO()
    with av.open(destination, "w", format=container) as output:
        video = output.add_stream("mpeg2video", rate=25)
        video.width = 96
        video.height = 64
        video.pix_fmt = "yuv420p"
        video.codec_context.gop_size = 50
        video.codec_context.max_b_frames = 0
        audio = []
        for language in ("eng", "fra"):
            stream = output.add_stream(audio_codec, rate=48000)
            stream.layout = "mono"
            stream.bit_rate = 64000
            stream.metadata["language"] = language
            audio.append(stream)
        sample = 0
        for index in range(seconds * 25):
            frame = av.VideoFrame(96, 64, "yuv420p")
            for plane, value in zip(frame.planes, (16 + index % 200, 100, 140)):
                plane.update(bytes([value]) * plane.buffer_size)
            frame.pts = index
            frame.time_base = Fraction(1, 25)
            output.mux(video.encode(frame))
            while sample < (index + 1) * 48000 // 25:
                for stream in audio:
                    sound = av.AudioFrame(format="s16", layout="mono", samples=1152)
                    sound.planes[0].update(bytes(sound.planes[0].buffer_size))
                    sound.sample_rate = 48000
                    sound.pts = sample
                    sound.time_base = Fraction(1, 48000)
                    output.mux(stream.encode(sound))
                sample += 1152
        output.mux(video.encode(None))
        for stream in audio:
            output.mux(stream.encode(None))
    return destination.getvalue()


class SourceServer:
    def __init__(self, payload, *, stall=False, delay=0.004):
        self.payload = payload
        self.requests = []
        self.connected = threading.Event()
        self.stop = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format, *args):
                pass

            def do_GET(self):
                owner.requests.append((self.path, self.headers.get("Range")))
                self.send_response(200)
                self.send_header("Content-Type", "video/mp2t")
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Connection", "close")
                # No length or range support: genuinely non-seekable HTTP TS.
                self.end_headers()
                owner.connected.set()
                if stall:
                    owner.stop.wait(60)
                    return
                try:
                    for offset in range(0, len(owner.payload), 188 * 16):
                        if owner.stop.wait(delay):
                            break
                        chunk = owner.payload[offset : offset + 188 * 16]
                        self.wfile.write(f"{len(chunk):x}\r\n".encode("ascii"))
                        self.wfile.write(chunk)
                        self.wfile.write(b"\r\n")
                    self.wfile.write(b"0\r\n\r\n")
                except BrokenPipeError, ConnectionResetError:
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}
        )
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/user/secret/live.ts"

    def close(self):
        self.stop.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)


class NativeSourceServer:
    """Real native FFmpeg TCP open/read that never reaches EOF by itself."""

    def __init__(self, payload=b""):
        self.stop = threading.Event()
        self.connected = threading.Event()
        owner = self

        class Handler(BaseRequestHandler):
            def handle(self):
                owner.connected.set()
                try:
                    self.request.sendall(payload)
                    owner.stop.wait(60)
                except OSError:
                    pass

        self.server = ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}
        )
        self.thread.start()
        self.url = f"tcp://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.stop.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)


class TimeshiftTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = make_transport_stream()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="qitv-timeshift-test-")
        self.directory = Path(self.temporary.name)
        self.recorders = []
        self.servers = []

    def tearDown(self):
        for recorder in self.recorders:
            recorder.request_stop()
        for recorder in self.recorders:
            self.assertTrue(recorder.wait(7000), "recorder did not stop within its timeout")
        for server in self.servers:
            server.close()
        self.temporary.cleanup()

    def source(self, **options):
        server = SourceServer(self.payload, **options)
        self.servers.append(server)
        return server

    def recorder(self, url, *, cap=2 * 1024 * 1024):
        recorder = TimeshiftRecorder(url, directory=str(self.directory), max_bytes=cap)
        recorder.errors = []
        recorder.snapshots = []
        recorder.errorOccurred.connect(recorder.errors.append, Qt.DirectConnection)
        recorder.updated.connect(recorder.snapshots.append, Qt.DirectConnection)
        self.recorders.append(recorder)
        recorder.start()
        return recorder

    def engine(self):
        return _RecordingEngine(
            "unused",
            session_directory=self.directory / (".qitv-timeshift-" + secrets.token_hex(16)),
            max_bytes=12 * 1024,
            stop=threading.Event(),
            send=lambda message: None,
        )

    def wait_for(self, predicate, message, seconds=8):
        deadline = time.monotonic() + seconds
        while not predicate() and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(predicate(), message)

    def stop_recorder(self, recorder):
        recorder.request_stop()
        self.assertTrue(recorder.wait(7000))
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_nonseekable_ingestion_eviction_manifest_tracks_and_seek_clamping(self):
        source = self.source()
        cap = len(self.payload) // 2
        recorder = self.recorder(source.url, cap=cap)
        self.wait_for(lambda: recorder.playback() or recorder.errors, "buffer not ready")
        self.assertFalse(recorder.errors)
        initial = recorder._snapshot()
        self.assertGreaterEqual(initial["end"] - initial["start"], 4)
        initial_url, initial_base, _ = recorder.playback(-100)
        # Do not consume any segments: recording must advance while paused.
        self.wait_for(lambda: recorder._snapshot()["ended"] or recorder.errors, "EOF not reached")
        self.assertFalse(recorder.errors)
        state = recorder._snapshot()
        self.assertGreater(state["end"], initial["end"])
        self.assertGreater(state["start"], initial_base)
        self.assertGreater(state["bytes"], 0)
        self.assertLessEqual(max(item["bytes"] for item in recorder.snapshots), cap)
        self.assertEqual(source.requests, [("/user/secret/live.ts", None)])
        oldest_url, base, offset = recorder.playback(-100)
        self.assertEqual((base, offset), (state["start"], 0))
        _, base, offset = recorder.playback(1e9)
        self.assertAlmostEqual(base + offset, state["end"] - 0.05)
        _, base, offset = recorder.playback()
        self.assertAlmostEqual(base + offset, state["end"] - 3)
        _, base, offset = recorder.playback(state["start"] + 0.75)
        self.assertAlmostEqual(base + offset, state["start"] + 0.75)
        with urlopen(oldest_url, timeout=2) as response:
            manifest = response.read().decode("ascii")
        self.assertIn("#EXT-X-ENDLIST", manifest)
        self.assertNotIn("secret", manifest)
        media_paths = [line for line in manifest.splitlines() if not line.startswith("#")]
        self.assertEqual(len(media_paths), len(recorder._completed))
        with av.open(oldest_url, options={"live_start_index": "0"}, timeout=(3, 3)) as media:
            self.assertEqual(len(media.streams.video), 1)
            self.assertEqual(len(media.streams.audio), 2)
            self.assertEqual(
                {stream.metadata.get("language") for stream in media.streams.audio},
                {"eng", "fra"},
            )
            timestamps = [
                float(packet.pts * packet.time_base)
                for packet in media.demux(video=0)
                if packet.pts is not None
            ]
        self.assertGreater(timestamps[-1], timestamps[0] + 3)
        # Every advertised file begins on a real video keyframe.
        for path in media_paths:
            with urlopen(urljoin(oldest_url, path), timeout=2) as response:
                data = response.read()
            with av.open(io.BytesIO(data), format="mpegts") as media:
                first = next(packet for packet in media.demux(video=0) if packet.size)
                self.assertTrue(first.is_keyframe)
        # A stale selection advances to the remaining ring, not deleted files.
        with urlopen(initial_url, timeout=2) as response:
            self.assertEqual(response.read().decode("ascii"), manifest)
        with self.assertRaises(HTTPError) as error:
            token = oldest_url.split("/")[3]
            urlopen(oldest_url.replace(token, "invalid"), timeout=2)
        self.assertEqual(error.exception.code, 404)
        error.exception.close()
        self.stop_recorder(recorder)

    def test_mux_clock_repairs_collisions_without_drift_or_cross_track_offsets(self):
        with av.open(io.BytesIO(self.payload)) as media:
            for time_base in (Fraction(1, 90000), Fraction(1, 28224000)):
                with self.subTest(time_base=time_base):
                    clock = _RecordingClock(media.streams.video[0].index)

                    def normalize(timestamp, stream):
                        packet = av.Packet(b"audio")
                        packet.stream = stream
                        packet.time_base = time_base
                        packet.pts = packet.dts = int(Fraction(timestamp, 90000) / time_base)
                        packet.duration = int(Fraction(1920, 90000) / time_base)
                        clock.apply(packet)
                        self.assertEqual(packet.time_base, Fraction(1, 90000))
                        self.assertEqual(packet.pts, packet.dts)
                        self.assertEqual(packet.duration, 1920)
                        return packet.dts

                    first, second = media.streams.audio
                    self.assertEqual(normalize(-90000, first), -90000)
                    self.assertEqual(normalize(90000, first), 90000)
                    self.assertEqual(normalize(90000, first), 90001)
                    self.assertEqual(normalize(90000, first), 90002)
                    self.assertEqual(normalize(91920, first), 91920)
                    self.assertEqual(normalize(90000, second), 90000)

    def test_repeated_aac_timestamps_preserve_frames_and_audio_tracks(self):
        payload = make_transport_stream(seconds=4, audio_codec="aac")

        @contextmanager
        def source(repeated):
            with av.open(io.BytesIO(payload)) as media:

                class TimestampSource:
                    streams = media.streams

                    def demux(self, streams):
                        counts = {}
                        previous = {}
                        for packet in media.demux(streams):
                            index = packet.stream.index
                            if repeated and packet.size and packet.stream.type == "audio":
                                counts[index] = counts.get(index, 0) + 1
                                if counts[index] == 8:
                                    packet.duration = 0
                                    previous[index] = packet.pts, packet.dts
                                elif counts[index] == 9:
                                    packet.pts, packet.dts = previous[index]
                            yield packet

                yield TimestampSource()

        def recorded_frames(repeated):
            recorder = self.engine()
            recorder._max_bytes = 2 * 1024 * 1024
            recorder._prepare_directory()
            try:
                with patch("services.timeshift.open_source", return_value=source(repeated)):
                    recorder._record(av, [])
                content = b"".join(
                    segment.path.read_bytes() for segment in recorder._segments.values()
                )
                frames = {}
                with av.open(io.BytesIO(content)) as media:
                    for packet in media.demux():
                        key = packet.stream.type, packet.stream.metadata.get("language")
                        for frame in packet.decode():
                            frames[key] = frames.get(key, 0) + (
                                frame.samples if isinstance(frame, av.AudioFrame) else 1
                            )
                return frames
            finally:
                recorder._stop.set()
                recorder.run()

        expected = recorded_frames(False)
        self.assertEqual(expected[("video", None)], 100)
        self.assertGreater(expected[("audio", "eng")], 180000)
        self.assertEqual(expected[("audio", "eng")], expected[("audio", "fra")])
        self.assertEqual(recorded_frames(True), expected)

    def test_cap_counts_open_segment_disk_blocks_and_evicted_reader_lease(self):
        recorder = self.engine()
        recorder._prepare_directory()
        unit = recorder._unit
        recorder._max_bytes = 3 * unit

        def new_writer(sequence):
            segment = _Segment(
                sequence,
                sequence * 4 * 3600,
                sequence * 4 * 3600,
                recorder._directory / f"segment-{sequence}.ts",
            )
            recorder._segments[sequence] = segment
            return segment, _BoundedWriter(recorder, segment)

        first, writer = new_writer(0)
        writer.write(bytes(unit))
        writer.close()
        recorder._complete_segment(first, 2)
        lease = recorder._lease(0)
        self.assertIs(lease, first)
        second, writer = new_writer(1)
        writer.write(bytes(unit))
        writer.close()
        recorder._complete_segment(second, second.start + 2)
        self.assertFalse(first.retired, "Age alone must not evict history that fits in the cache")
        self.assertEqual(recorder._snapshot()["start"], 0)
        current, writer = new_writer(2)
        try:
            writer.write(bytes(unit))
            writer.write(b"x")
            self.assertTrue(first.retired)
            self.assertTrue(first.path.exists(), "leased bytes disappeared before reader closed")
            self.assertFalse(second.path.exists())
            self.assertEqual(recorder._allocated, 3 * unit)
            self.assertEqual(recorder._snapshot()["bytes"], 2 * unit + 1)
            recorder._release(lease)
            self.assertFalse(first.path.exists())
            self.assertEqual(recorder._snapshot()["bytes"], unit + 1)
            before = current.path.stat().st_size
            with self.assertRaises(_Failure):
                writer.write(bytes(3 * unit))
            self.assertEqual(current.path.stat().st_size, before)
            self.assertLessEqual(recorder._allocated, recorder._max_bytes)
        finally:
            writer.close()
            recorder._stop.set()
            recorder.run()
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_stop_interrupts_partial_local_http_headers_and_removes_buffer(self):
        recorder = self.recorder(self.source(delay=0).url)
        self.wait_for(lambda: recorder._snapshot()["ended"] or recorder.errors, "EOF not reached")
        self.assertFalse(recorder.errors)
        url, _, _ = recorder.playback()
        port = int(url.split(":")[2].split("/")[0])
        with socket.create_connection(("127.0.0.1", port), timeout=2) as stalled:
            stalled.sendall(b"GET / HTTP/1.1\r\nIncomplete:")
            time.sleep(0.03)
            recorder.request_stop()
            self.assertTrue(recorder.wait(2000), "local header reader prevented stop")
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_provider_stall_cancellation_is_bounded_and_credentials_are_not_emitted(self):
        source = self.source(stall=True)
        recorder = self.recorder(source.url)
        self.assertTrue(source.connected.wait(5))
        pid = recorder._child_pid
        self.assertIn(pid, [child.pid for child in multiprocessing.active_children()])
        if os.name != "nt":
            command = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                capture_output=True,
                check=True,
                timeout=2,
            ).stdout.decode()
            self.assertNotIn("secret", command)
            self.assertNotIn(source.url, command)
        recorder.request_stop()
        self.assertTrue(recorder.wait(2000), "upstream open prevented cancellation")
        self.assertEqual(recorder.errors, [])
        self.assertIsNone(recorder.playback())
        self.assertNotIn(pid, [child.pid for child in multiprocessing.active_children()])
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_owner_death_interrupts_stalled_native_source(self):
        # A multiprocessing daemon flag only handles orderly owner exit.
        # An abruptly killed GUI must not leave its network recorder running.
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.settimeout(8)
            url = f"tcp://127.0.0.1:{listener.getsockname()[1]}"
            code = """
import sys, time
from services.timeshift import TimeshiftRecorder
recorder = TimeshiftRecorder(sys.argv[1], directory=sys.argv[2], max_bytes=1048576)
recorder.start()
while recorder._child_pid is None:
    time.sleep(.01)
print(recorder._child_pid, flush=True)
time.sleep(60)
"""
            owner = subprocess.Popen(
                [sys.executable, "-c", code, url, str(self.directory)],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            child_pid = None
            closed = False
            try:
                with listener.accept()[0] as peer:
                    child_pid = int(owner.stdout.readline())
                    owner.kill()
                    owner.wait(5)
                    peer.settimeout(3)
                    self.assertEqual(peer.recv(1), b"", "orphaned recorder retained its socket")
                    closed = True
            finally:
                if owner.poll() is None:
                    owner.kill()
                    owner.wait(5)
                if child_pid is not None and not closed:
                    try:
                        os.kill(child_pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                owner.stdout.close()

    def test_native_open_and_demux_stalls_outlive_old_timeouts_but_stop_promptly(self):
        for payload in (b"", self.payload):
            with self.subTest(stage="demux" if payload else "open"):
                source = NativeSourceServer(payload)
                self.servers.append(source)
                recorder = self.recorder(source.url)
                self.assertTrue(source.connected.wait(5))
                if payload:
                    self.wait_for(
                        lambda: recorder.playback() or recorder.errors,
                        "native source did not reach playback",
                    )
                    self.assertFalse(recorder.errors)
                    # Native demux now waits for additional media, beyond the
                    # previous two-second read timeout.
                    time.sleep(2.5)
                else:
                    # No bytes exist to probe. av.open remains inside native
                    # code beyond both previous open/read timeout values.
                    time.sleep(5.5)
                self.assertTrue(recorder.isRunning())
                self.assertFalse(recorder.errors)
                pid = recorder._child_pid
                recorder.request_stop()
                self.assertIsNone(recorder.playback())
                self.assertTrue(recorder.wait(2000), "native I/O prevented cancellation")
                self.assertEqual(recorder.errors, [])
                self.assertNotIn(pid, [child.pid for child in multiprocessing.active_children()])
                self.assertEqual(list(self.directory.iterdir()), [])

    def test_stopping_stalled_child_preserves_playing_sibling(self):
        active = self.recorder(self.source(delay=0).url)
        self.wait_for(
            lambda: active._snapshot()["ended"] or active.errors,
            "active sibling did not finish ingesting",
        )
        self.assertFalse(active.errors)
        url, _, _ = active.playback()
        stalled_source = self.source(stall=True)
        stalled = self.recorder(stalled_source.url)
        self.assertTrue(stalled_source.connected.wait(5))
        stalled.request_stop()
        self.assertTrue(stalled.wait(2000))
        self.assertFalse(stalled.errors)
        self.assertFalse(stalled._directory.exists())
        self.assertTrue(active._directory.exists())
        with urlopen(url, timeout=2) as response:
            self.assertIn(b"#EXT-X-ENDLIST", response.read())
        self.assertIn(active._child_pid, [child.pid for child in multiprocessing.active_children()])
        self.stop_recorder(active)

    def test_oversize_gop_fails_before_exceeding_cap_and_cleans_disk(self):
        recorder = self.recorder(self.source(delay=0).url, cap=4096)
        self.assertTrue(recorder.wait(7000))
        self.assertTrue(recorder.errors)
        self.assertNotIn("secret", " ".join(recorder.errors))
        self.assertLessEqual(max(item["bytes"] for item in recorder.snapshots), 4096)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_short_finite_source_is_playable_and_unsupported_audio_is_rejected(self):
        path = self.directory / "short.ts"
        path.write_bytes(make_transport_stream(seconds=2))
        recorder = self.recorder(str(path))
        self.wait_for(lambda: recorder._snapshot()["ended"] or recorder.errors, "short EOF missing")
        self.assertFalse(recorder.errors)
        self.assertIsNotNone(recorder.playback())
        recorder.request_stop()
        self.assertTrue(recorder.wait(3000))
        path.unlink()
        path = self.directory / "unsupported.mkv"
        path.write_bytes(
            make_transport_stream(seconds=2, audio_codec="pcm_s16le", container="matroska")
        )
        unsupported = self.recorder(str(path))
        self.assertTrue(unsupported.wait(7000))
        self.assertEqual(len(unsupported.errors), 1)
        self.assertIsNone(unsupported.playback())
        path.unlink()
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_stale_owned_sessions_only_and_active_sibling_lock_survives(self):
        stale = self.directory / (".qitv-timeshift-" + "a" * 32)
        stale.mkdir()
        (stale / "owner").write_text(_OWNER, encoding="ascii")
        (stale / "segment-0.ts").write_bytes(b"old recording")
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                "import os,sys; from PySide6.QtCore import QLockFile; "
                "lock=QLockFile(sys.argv[1]); assert lock.tryLock(0); os._exit(0)",
                str(stale / "session.lock"),
            ],
            capture_output=True,
            timeout=5,
        )
        self.assertEqual(child.returncode, 0, child.stderr.decode())
        unrelated = self.directory / (".qitv-timeshift-" + "b" * 32)
        unrelated.mkdir()
        (unrelated / "owner").write_text("not owned", encoding="ascii")
        outside = self.directory / "user-data"
        outside.mkdir()
        (outside / "precious").write_bytes(b"keep")
        link = self.directory / (".qitv-timeshift-" + "c" * 32)
        if os.name != "nt":
            link.symlink_to(outside, target_is_directory=True)
        active = self.engine()
        active._prepare_directory()
        active_directory = active._directory
        second = self.engine()
        second._prepare_directory()
        try:
            self.assertFalse(stale.exists())
            self.assertTrue(active_directory.exists())
            self.assertTrue((active_directory / "session.lock").exists())
            self.assertEqual((unrelated / "owner").read_text(), "not owned")
            self.assertEqual((outside / "precious").read_bytes(), b"keep")
            if os.name != "nt":
                self.assertTrue(link.is_symlink())
        finally:
            for recorder in (active, second):
                recorder._stop.set()
                recorder.run()
        self.assertFalse(active_directory.exists())


if __name__ == "__main__":
    unittest.main()
