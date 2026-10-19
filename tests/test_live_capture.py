"""QITV_CAPTURE_DIR: a live source saves what it reads from the provider, with arrival times.

Every server is local (tests/http_fixtures.py) and the media synthetic (tests/ts_fixtures.py).
"""

import hashlib
import io
import logging
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_live_source import FAST_RETRIES, KEY, SourceTestCase, drain, encrypt, leaf

from services.live import source as source_module
from services.live.source import CAPTURE_ENV, Discontinuity, open_source

NAME = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{8}$")


class _Logs:
    """The source module's log records (level, message) while in the block."""

    def __enter__(self):
        self.records = []
        logs = self

        class Collect(logging.Handler):
            def emit(self, record):
                logs.records.append((record.levelno, record.getMessage()))

        self._logger = logging.getLogger("services.live.source")
        self._handler = Collect(logging.DEBUG)
        self._level = self._logger.level
        self._logger.addHandler(self._handler)
        self._logger.setLevel(logging.DEBUG)
        return self.records

    def __exit__(self, *_exc):
        self._logger.removeHandler(self._handler)
        self._logger.setLevel(self._level)


def captures(directory):
    """The capture names (no suffix) in `directory`, sorted."""
    return sorted(path.stem for path in Path(directory).glob("*.ts"))


def read_times(path):
    """(reads, comments): the `<cumulative bytes> <seconds>` lines as (int, float), and the
    other lines as (word, seconds, after how many reads), as the replay tools tolerate them."""
    reads, comments = [], []
    for line in Path(path).read_text(encoding="ascii").splitlines():
        parts = line.split()
        if len(parts) == 2:
            reads.append((int(parts[0]), float(parts[1])))
            continue
        assert parts[0] == "#" and len(parts) == 3, line
        assert re.fullmatch(r"\d+\.\d{3}", parts[2]), line
        comments.append((parts[1], float(parts[2]), len(reads)))
    return reads, comments


def stream(url):
    """The source opened for `url`, and everything it yielded (`drain`)."""
    source = open_source(url, headers={}, verify_ssl=True, stop=threading.Event())
    return source, drain(source)


class CaptureFileTests(unittest.TestCase):
    """The tee itself, with arrival times given."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="qitv-capture-")
        self.addCleanup(self.directory.cleanup)
        self.root = self.directory.name

    def capture(self, url="http://host.invalid/user/secret/1.ts"):
        capture = source_module._Capture(self.root, url)
        self.addCleanup(capture.close)
        return capture

    def test_bytes_and_times_are_written_exactly(self):
        url = "http://host.invalid/user/secret/1.ts"
        capture = self.capture(url)
        opened = capture._opened
        with _Logs() as logs:
            capture.write(b"abc", opened + 0.25)
            capture.write(b"", opened + 0.5)  # nothing read: no line
            capture.write(b"defg", opened + 1.0)
            capture.write(b"h", opened + 61.2346)
            capture.close()
            capture.write(b"after", opened + 62.0)  # closed: ignored
        (name,) = captures(self.root)
        self.assertRegex(name, NAME)
        self.assertTrue(name.endswith("-" + hashlib.sha256(url.encode()).hexdigest()[:8]))
        self.assertEqual((Path(self.root) / f"{name}.ts").read_bytes(), b"abcdefgh")
        self.assertEqual(
            (Path(self.root) / f"{name}.times").read_text(), "3 0.250\n7 1.000\n8 61.235\n"
        )
        self.assertEqual(
            logs, [(logging.INFO, f"Capturing the live stream to {name}.ts and .times.")]
        )

    def test_a_mark_is_a_comment_line_with_its_seconds(self):
        capture = self.capture()
        capture.write(b"abc", capture._opened)
        capture.mark("reconnect")
        capture.close()
        (name,) = captures(self.root)
        reads, comments = read_times(Path(self.root) / f"{name}.times")
        self.assertEqual(reads, [(3, 0.0)])
        self.assertEqual([(word, after) for word, _at, after in comments], [("reconnect", 1)])

    def test_nothing_is_made_before_the_first_bytes(self):
        capture = self.capture()
        capture.close()
        self.assertEqual(os.listdir(self.root), [])

    def test_a_second_open_in_the_same_second_gets_its_own_pair(self):
        first, second = self.capture(), self.capture()
        second.name = first.name
        first.write(b"one", first._opened)
        second.write(b"two", second._opened)
        first.close()
        second.close()
        self.assertEqual(captures(self.root), [first.name, first.name + "-2"])
        self.assertEqual((Path(self.root) / f"{first.name}-2.ts").read_bytes(), b"two")

    def test_the_directory_is_made_when_missing(self):
        nested = os.path.join(self.root, "a", "b")
        capture = source_module._Capture(nested, "http://host.invalid/1.ts")
        capture.write(b"abc")
        capture.close()
        self.assertEqual(len(captures(nested)), 1)

    def test_the_size_cap_stops_the_capture_once(self):
        capture = self.capture()
        opened = capture._opened
        with patch.object(source_module, "_CAPTURE_MAX_BYTES", 25), _Logs() as logs:
            for index in range(5):
                capture.write(b"0123456789", opened + index)
        (name,) = captures(self.root)
        self.assertEqual((Path(self.root) / f"{name}.ts").read_bytes(), b"0123456789" * 2)
        self.assertEqual(read_times(Path(self.root) / f"{name}.times")[0], [(10, 0.0), (20, 1.0)])
        stops = [message for _level, message in logs if "stopped" in message]
        self.assertEqual(stops, [f"The live stream capture {name} stopped at 2 GiB."])

    def test_the_time_cap_stops_the_capture(self):
        capture = self.capture()
        opened = capture._opened
        with _Logs() as logs:
            capture.write(b"abc", opened + 3 * 3600 - 0.5)
            capture.write(b"def", opened + 3 * 3600)
            capture.write(b"ghi", opened + 3 * 3600 + 1)
        (name,) = captures(self.root)
        self.assertEqual((Path(self.root) / f"{name}.ts").read_bytes(), b"abc")
        stops = [message for _level, message in logs if "stopped" in message]
        self.assertEqual(stops, [f"The live stream capture {name} stopped after 3 hours."])

    def test_the_disk_keeps_a_gibibyte_free(self):
        floor = source_module._CAPTURE_FREE_FLOOR
        usage = shutil_usage(free=floor + source_module._CAPTURE_CHECK_BYTES)
        capture = self.capture()
        with patch.object(source_module.shutil, "disk_usage", return_value=usage), _Logs() as logs:
            capture.write(b"abc")
            capture.write(b"def")
        self.assertEqual(os.listdir(self.root), [])
        self.assertEqual(
            [level for level, message in logs if "less than 1 GiB free" in message],
            [logging.WARNING],
        )

    def test_the_free_space_is_looked_up_again_as_the_capture_grows(self):
        plenty = shutil_usage(free=1 << 50)
        low = shutil_usage(free=source_module._CAPTURE_FREE_FLOOR)
        capture = self.capture()
        with (
            patch.object(source_module, "_CAPTURE_CHECK_BYTES", 10),
            patch.object(source_module.shutil, "disk_usage", side_effect=[plenty, plenty, low]),
            _Logs() as logs,
        ):
            for _ in range(5):
                capture.write(b"0123456")
        (name,) = captures(self.root)
        # Looked up at the start, after 14 bytes and after 28: the last stops it.
        self.assertEqual((Path(self.root) / f"{name}.ts").read_bytes(), b"0123456" * 4)
        self.assertEqual(
            [message for level, message in logs if level == logging.WARNING],
            [f"The live stream capture {name} stopped with less than 1 GiB free on its disk."],
        )

    def test_a_write_error_logs_one_warning_without_the_path_and_stops(self):
        capture = self.capture()
        writes = failing_open(after=1)
        with patch.object(source_module, "open", writes.open, create=True), _Logs() as logs:
            capture.write(b"abc")
            capture.write(b"def")  # fails
            capture.write(b"ghi")  # stopped: never tried
            capture.mark("reconnect")
        self.assertEqual(writes.attempts, 2)
        warnings = [message for level, message in logs if level == logging.WARNING]
        self.assertEqual(
            warnings,
            [
                f"The live stream capture {capture.name} stopped: it could not be written "
                "(OSError)."
            ],
        )
        self.assertNotIn(self.root, " ".join(message for _level, message in logs))


def shutil_usage(free):
    return SimpleNamespace(total=1 << 50, used=(1 << 50) - free, free=free)


class failing_open:
    """`open` for the capture's media file, whose writes fail with a path in the error after
    `after` of them; any other file opens as usual."""

    def __init__(self, after):
        self.after = after
        self.attempts = 0

    def open(self, path, mode="r", *args, **kwargs):
        if "b" not in mode:
            return open(path, mode, *args, **kwargs)
        real = open(path, mode, *args, **kwargs)
        owner = self

        class Failing(io.RawIOBase):
            def writable(self):
                return True

            def write(self, data):
                owner.attempts += 1
                if owner.attempts > owner.after:
                    raise OSError(28, "No space left on device", path)
                return real.write(data)

            def flush(self):
                real.flush()

            def close(self):
                if not self.closed:
                    super().close()  # flushes
                    real.close()

        return Failing()


class CapturedSourceTests(SourceTestCase):
    """Sources opened with QITV_CAPTURE_DIR set, over local servers."""

    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory(prefix="qitv-capture-")
        self.addCleanup(directory.cleanup)
        self.root = directory.name
        environment = patch.dict(os.environ, {CAPTURE_ENV: self.root})
        environment.start()
        self.addCleanup(environment.stop)

    def only_capture(self):
        (name,) = captures(self.root)
        self.assertRegex(name, NAME)
        base = Path(self.root) / name
        return base.with_suffix(".ts").read_bytes(), read_times(base.with_suffix(".times"))

    def broken_live(self):
        """A raw TS channel whose first connection breaks off halfway; and that half."""
        half = len(self.video) // 2 // 188 * 188

        def live(_request, count):
            if count == 1:

                def broken(request):
                    request.wfile.write(self.video[:half])

                return 200, {"Content-Length": str(len(self.video))}, broken
            return 200, {}, self.video[half:]

        return self.server({"/user/secret/live.ts": live}), half

    def test_raw_ts_is_captured_as_read_and_a_reconnect_goes_on_in_the_same_pair(self):
        server, half = self.broken_live()
        started = time.monotonic()
        with patch.object(source_module, "_RECONNECT_DELAYS", FAST_RETRIES), _Logs() as logs:
            _source, parts = stream(server.url + "/user/secret/live.ts")
        took = time.monotonic() - started
        self.assertEqual(parts, [self.video[:half], Discontinuity(), self.video[half:]])
        data, (reads, comments) = self.only_capture()
        self.assertEqual(data, self.video)
        ends = [end for end, _at in reads]
        self.assertEqual(ends, sorted(ends))
        self.assertEqual(ends[-1], len(self.video))
        times = [at for _end, at in reads]
        self.assertEqual(times, sorted(times))
        self.assertLessEqual(times[-1], took + 0.001)
        (reconnect,) = comments
        self.assertEqual(reconnect[0], "reconnect")
        self.assertEqual(ends[reconnect[2] - 1], half)  # after the first connection's bytes
        self.assertGreaterEqual(reconnect[1], times[reconnect[2] - 1])
        name = captures(self.root)[0]
        self.assertIn((logging.INFO, f"Capturing the live stream to {name}.ts and .times."), logs)
        for _level, message in logs:
            self.assertNotIn("secret", message)
            self.assertNotIn("127.0.0.1", message)

    def test_hls_segments_are_captured_unjoined_with_their_boundaries(self):
        text = (
            "#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4,\na.ts\n"
            "#EXT-X-DISCONTINUITY\n#EXTINF:4,\nb.ts\n#EXT-X-ENDLIST\n"
        )
        server = self.server(
            {
                "/channel.m3u8": (200, {}, text.encode()),
                "/a.ts": (200, {}, self.video),
                "/b.ts": (200, {}, self.next_video),
            }
        )
        stream(server.url + "/channel.m3u8")
        data, (reads, comments) = self.only_capture()
        # The segments as they came: the joiner's continuity counters are not in them.
        self.assertEqual(data, self.video + self.next_video)
        self.assertEqual(reads[-1][0], len(data))
        ends = [0] + [end for end, _at in reads]
        boundaries = [(word, ends[after]) for word, _at, after in comments]
        self.assertEqual(
            boundaries,
            [("segment", 0), ("discontinuity", len(self.video)), ("segment", len(self.video))],
        )

    def test_an_encrypted_hls_segment_is_captured_decrypted(self):
        playlist = leaf(
            "encrypted.ts",
            extra='#EXT-X-KEY:METHOD=AES-128,URI="channel.key",IV=0x' + "0" * 32 + "\n",
        )
        server = self.server(
            {
                "/channel.m3u8": (200, {}, playlist),
                "/encrypted.ts": (200, {}, encrypt(self.video, bytes(16))),
                "/channel.key": (200, {}, KEY),
            }
        )
        stream(server.url + "/channel.m3u8")
        data, (reads, _comments) = self.only_capture()
        self.assertEqual(data, self.video)
        self.assertEqual(reads[-1][0], len(self.video))

    def test_unset_captures_nothing(self):
        server = self.server({"/live.ts": (200, {}, self.video)})
        for value in (None, ""):
            with self.subTest(value=value):
                with patch.dict(os.environ):
                    if value is None:
                        del os.environ[CAPTURE_ENV]
                    else:
                        os.environ[CAPTURE_ENV] = value
                    source, parts = stream(server.url + "/live.ts")
                self.assertIsNone(source._capture)
                self.assertEqual(parts, [self.video])
        self.assertEqual(os.listdir(self.root), [])

    def test_a_write_error_leaves_the_stream_whole(self):
        server, half = self.broken_live()
        writes = failing_open(after=1)
        with (
            patch.object(source_module, "_RECONNECT_DELAYS", FAST_RETRIES),
            patch.object(source_module, "open", writes.open, create=True),
            _Logs() as logs,
        ):
            _source, parts = stream(server.url + "/user/secret/live.ts")
        self.assertEqual(parts, [self.video[:half], Discontinuity(), self.video[half:]])
        self.assertEqual(writes.attempts, 2)  # the failed write was the last one tried
        warnings = [message for level, message in logs if level == logging.WARNING]
        self.assertEqual(len(warnings), 1)
        self.assertIn("could not be written (OSError)", warnings[0])
        self.assertNotIn(self.root, warnings[0])

    def test_a_directory_that_cannot_be_made_leaves_the_stream_whole(self):
        blocker = Path(self.root) / "file"
        blocker.write_bytes(b"")
        server = self.server({"/live.ts": (200, {}, self.video)})
        with patch.dict(os.environ, {CAPTURE_ENV: str(blocker / "captures")}), _Logs() as logs:
            _source, parts = stream(server.url + "/live.ts")
        self.assertEqual(parts, [self.video])
        warnings = [message for level, message in logs if level == logging.WARNING]
        self.assertEqual(len(warnings), 1)
        self.assertNotIn(self.root, warnings[0])

    def test_the_size_cap_leaves_the_stream_whole(self):
        server = self.server({"/live.ts": (200, {}, self.video)})
        cap = len(self.video) // 2
        with patch.object(source_module, "_CAPTURE_MAX_BYTES", cap), _Logs() as logs:
            _source, parts = stream(server.url + "/live.ts")
        self.assertEqual(parts, [self.video])
        stops = [message for _level, message in logs if "stopped at 2 GiB" in message]
        self.assertEqual(len(stops), 1)
        for name in captures(self.root):  # none when the first read was larger than the cap
            data = (Path(self.root) / f"{name}.ts").read_bytes()
            reads, _comments = read_times(Path(self.root) / f"{name}.times")
            self.assertLessEqual(len(data), cap)
            self.assertEqual(data, self.video[: len(data)])
            self.assertEqual(reads[-1][0], len(data))


if __name__ == "__main__":
    unittest.main()
