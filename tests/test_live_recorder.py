"""Recording from the time-shift ring (2.3): the helper's recorder on synthetic rings, and the
helper's record commands on a paced local source (no second connection, a close keeps the
file)."""

import errno
import os
from pathlib import Path
import queue
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from PySide6.QtWidgets import QApplication
from http_fixtures import LocalSource
from ts_fixtures import make_stream

from services.live import engine as engine_module, recorder as recorder_module
from services.live.engine import LiveEngine
from services.live.recorder import ENDED, FAILED, LIMIT, SPACE, STOPPED, Recorder
from services.live.ring import RamRing
from tests.test_live_engine import URL_FOR_RING, wait_until
from tests.test_live_server import Writer, header, payload
from tests.test_live_worker import RAM, SECRET_PATH, InProcessHelper, paced_route

SEGMENT = 64 * 1024
MIB = 1024 * 1024


class Ended:
    """The session's input: running until `set()`."""

    def __init__(self):
        self.value = False

    def __call__(self):
        return self.value


class FullDisk:
    """A file that takes `room` bytes, then fails as a full disk does."""

    def __init__(self, path, room):
        self.handle = open(path, "xb", buffering=0)
        self.room = room

    def write(self, data):
        if self.room <= 0:
            raise OSError(errno.ENOSPC, "No space left on device")
        part = bytes(data[: self.room])
        self.room -= len(part)
        return self.handle.write(part)

    def close(self):
        self.handle.close()


class RecorderCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="qitv-recorder-test-")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name) / "Recordings"
        self.ring = RamRing(64 * SEGMENT)
        self.writer = Writer(self.ring)
        self.ended = Ended()
        self.done = threading.Event()
        self.ends = []

    def record(self, seq, *, ring_of=None, name="One 2026-10-19 21.30.ts", **kwargs):
        def on_done(recorder):
            self.ends.append((recorder.reason, recorder.path, recorder.written))
            self.done.set()

        recorder = Recorder(
            ring_of or (lambda: self.ring),
            self.ended,
            str(self.folder / name),
            seq,
            on_done,
            **kwargs,
        )
        recorder.start()
        self.addCleanup(recorder.join, 5)
        self.addCleanup(recorder.stop)
        return recorder

    def segments(self, count, size=SEGMENT):
        """`count` complete segments of `size` bytes, then a new open one."""
        for _ in range(count):
            self.writer.open()
            self.writer.append(size)
        self.writer.open()

    def finish(self, recorder, timeout=5):
        self.assertTrue(self.done.wait(timeout), "the recording did not end")
        self.assertEqual(len(self.ends), 1, "a recording ends once")
        return self.ends[0]

    def wait_written(self, recorder, size, timeout=5):
        deadline = time.monotonic() + timeout
        while recorder.written < size:
            if time.monotonic() > deadline:
                self.fail(f"only {recorder.written} of {size} bytes were written")
            time.sleep(0.01)


class RecorderTests(RecorderCase):
    def test_it_writes_from_a_segment_and_follows_the_open_one_until_stopped(self):
        self.segments(2)  # 0 and 1 closed, 2 open
        recorder = self.record(1)
        self.writer.append(1000)  # the open segment grows while it records
        self.writer.open()  # 3
        self.writer.append(5000)
        expected = self.writer.stream(1)  # 1 with its header, the later ones without
        self.wait_written(recorder, len(expected))
        recorder.stop()
        reason, path, written = self.finish(recorder)
        self.assertEqual(reason, STOPPED)
        self.assertEqual(Path(path).name, "One 2026-10-19 21.30.ts")
        self.assertEqual(Path(path).read_bytes(), expected)
        self.assertEqual(written, len(expected))

    def test_a_stop_writes_what_the_buffer_holds_up_to_its_live_edge(self):
        self.segments(20)
        with mock.patch.object(recorder_module, "_CHUNK", 1024):  # many reads to the edge
            recorder = self.record(0)
            recorder.stop()  # at once: it still catches up to the edge
            reason, path, written = self.finish(recorder)
        self.assertEqual(reason, STOPPED)
        self.assertEqual(Path(path).read_bytes(), self.writer.stream(0))

    def test_a_rewound_start_the_buffer_dropped_goes_on_at_its_oldest_segment(self):
        small = RamRing(3 * SEGMENT)
        self.ring, self.writer = small, Writer(small)
        self.segments(5)  # only the newest segments fit
        oldest = small.oldest_seq
        self.assertGreater(oldest, 0)
        recorder = self.record(0)
        expected = self.writer.stream(oldest)
        self.wait_written(recorder, len(expected))
        recorder.stop()
        reason, path, _ = self.finish(recorder)
        self.assertEqual(reason, STOPPED)
        self.assertEqual(Path(path).read_bytes(), expected)

    def test_the_end_of_the_input_ends_it_once_everything_is_written(self):
        self.segments(2)
        recorder = self.record(0)
        self.writer.append(3000)
        self.ring.complete(self.writer.seq)
        self.ended.value = True
        reason, path, written = self.finish(recorder)
        self.assertEqual(reason, ENDED)
        self.assertEqual(Path(path).read_bytes(), self.writer.stream(0))
        self.assertTrue(recorder.join(2))

    def test_the_time_limit_stops_it(self):
        self.segments(1)
        now = [100.0]
        recorder = self.record(0, max_s=4 * 3600.0, clock=lambda: now[0])
        self.wait_written(recorder, len(self.writer.stream(0)))
        now[0] += 4 * 3600.0
        self.writer.append(100)  # wakes it
        reason, path, written = self.finish(recorder)
        self.assertEqual(reason, LIMIT)
        self.assertGreater(written, 0)
        self.assertTrue(Path(path).is_file())

    def test_little_free_disk_space_stops_it_and_keeps_what_was_written(self):
        self.segments(3)
        free = [10**12]
        with mock.patch.object(recorder_module, "_SPACE_EVERY", SEGMENT):
            recorder = self.record(0, min_free=2 * 1024**3, free_bytes=lambda _path: free[0])
            self.wait_written(recorder, 2 * SEGMENT)
            free[0] = 1024**3
            self.writer.append(2 * SEGMENT)
            reason, path, written = self.finish(recorder)
        self.assertEqual(reason, SPACE)
        self.assertEqual(Path(path).stat().st_size, written)

    def test_no_file_is_made_when_the_disk_is_almost_full_at_the_start(self):
        self.segments(1)
        recorder = self.record(0, min_free=2 * 1024**3, free_bytes=lambda _path: 1024)
        self.assertEqual(self.finish(recorder), (SPACE, "", 0))
        self.assertEqual(list(self.folder.iterdir()), [])  # the empty file went

    def test_a_full_disk_stops_it_and_keeps_what_was_written(self):
        self.segments(3)
        with self.assertLogs("services.live.recorder", "WARNING"):  # its type only, no path
            recorder = self.record(0, open_new=lambda path: FullDisk(path, 70_000))
            reason, path, written = self.finish(recorder)
        self.assertEqual((reason, written), (SPACE, 70_000))
        self.assertEqual(Path(path).read_bytes(), self.writer.stream(0)[:70_000])

    def test_a_folder_that_cannot_be_made_fails_without_a_file(self):
        self.folder.parent.mkdir(exist_ok=True)
        self.folder.write_bytes(b"a file where the folder would be")
        self.segments(1)
        with self.assertLogs("services.live.recorder", "WARNING"):
            recorder = self.record(0)
            self.assertEqual(self.finish(recorder), (FAILED, "", 0))

    def test_an_existing_file_is_never_replaced(self):
        self.folder.mkdir(parents=True)
        taken = self.folder / "One 2026-10-19 21.30.ts"
        taken.write_bytes(b"an earlier recording")
        (self.folder / "One 2026-10-19 21.30 (2).ts").write_bytes(b"and another")
        self.segments(1)
        recorder = self.record(0)
        recorder.stop()
        _, path, _ = self.finish(recorder)
        self.assertEqual(Path(path).name, "One 2026-10-19 21.30 (3).ts")
        self.assertEqual(taken.read_bytes(), b"an earlier recording")

    def test_a_ring_replaced_after_a_disk_failure_is_followed(self):
        self.segments(1)  # 0 closed, 1 open
        current = [self.ring]
        recorder = self.record(0, ring_of=lambda: current[0])
        first = self.writer.stream(0)
        self.wait_written(recorder, len(first))
        # The session goes on in RAM from the next segment; the disk ring is closed.
        fallback = RamRing(64 * SEGMENT)
        current[0] = fallback
        self.ring.close()
        fallback.open_segment(3, header(3))
        fallback.append(payload(3, 0, 4000))
        expected = first + header(3) + payload(3, 0, 4000)
        self.wait_written(recorder, len(expected))
        recorder.stop()
        reason, path, _ = self.finish(recorder)
        self.assertEqual(reason, STOPPED)
        self.assertEqual(Path(path).read_bytes(), expected)

    def test_a_buffer_torn_down_ends_it_where_it_got_to(self):
        self.segments(1)
        current = [self.ring]
        recorder = self.record(0, ring_of=lambda: current[0])
        self.wait_written(recorder, len(self.writer.stream(0)))
        current[0] = None
        self.ring.close()
        reason, path, written = self.finish(recorder)
        self.assertEqual(reason, STOPPED)
        self.assertEqual(Path(path).stat().st_size, written)

    def test_a_session_without_a_buffer_records_nothing(self):
        recorder = self.record(0, ring_of=lambda: None)
        self.assertEqual(self.finish(recorder), (FAILED, "", 0))
        self.assertFalse(self.folder.exists())


class HelperRecordingTests(unittest.TestCase):
    """The helper's commands on a session that streams a paced local source."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="qitv-recording-test-")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.stop = threading.Event()
        self.hung_up = threading.Event()
        self.source = LocalSource(
            {SECRET_PATH: paced_route(make_stream(60), self.stop, self.hung_up)}
        )
        self.addCleanup(self.source.close)
        self.addCleanup(self.stop.set)
        self.helper = InProcessHelper(self)
        self.helper.parent.send(("open", "s", self.source.url + SECRET_PATH, {}, True, RAM, 6.0))
        self.helper.wait_for(lambda message: message == ("ready", "s"))

    def recorded(self, rid, timeout=10):
        return self.helper.wait_for(lambda m: m[0] == "recorded" and m[2] == rid, timeout)

    def assert_transport_stream(self, path):
        data = Path(path).read_bytes()
        self.assertGreater(len(data), 188 * 100)
        self.assertEqual(data[0], 0x47)
        self.assertEqual(data[188], 0x47)

    def test_a_recording_stops_and_keeps_its_file_without_another_connection(self):
        path = str(self.folder / "One.ts")
        self.helper.parent.send(("record_start", "s", "r1", path, 0, {}))
        time.sleep(1.0)
        self.helper.parent.send(("record_stop", "s"))
        message = self.recorded("r1")
        self.assertEqual(message[:5], ("recorded", "s", "r1", STOPPED, path))
        self.assertEqual(os.path.getsize(path), message[5])
        self.assert_transport_stream(path)
        self.assertEqual(self.source.connections, 1)  # the session's own, nothing more

    def test_a_channel_change_stops_the_recording_and_keeps_the_file(self):
        path = str(self.folder / "One.ts")
        self.helper.parent.send(("record_start", "s", "r1", path, 0, {}))
        time.sleep(0.6)
        self.helper.parent.send(("close", "s"))
        message = self.recorded("r1")  # also when it comes after "closed"
        self.assertEqual(message[3], STOPPED)
        self.assert_transport_stream(path)
        self.assertIn(("closed", "s"), self.helper.messages)
        self.assertTrue(self.hung_up.wait(3), "the session kept its upstream connection")

    def test_a_new_recording_may_start_while_the_last_one_finishes(self):
        first, second = str(self.folder / "a.ts"), str(self.folder / "b.ts")
        self.helper.parent.send(("record_start", "s", "r1", first, 0, {}))
        self.helper.parent.send(("record_start", "s", "r0", second, 0, {}))  # one at a time
        self.assertEqual(self.recorded("r0")[3:], (FAILED, "", 0))
        self.helper.parent.send(("record_stop", "s"))
        self.helper.parent.send(("record_start", "s", "r2", second, 0, {}))
        self.assertEqual(self.recorded("r1")[3], STOPPED)
        self.helper.parent.send(("record_stop", "s"))
        self.assertEqual(self.recorded("r2")[3], STOPPED)
        self.assertTrue(os.path.getsize(second) > 0)

    def test_its_limits_come_with_the_command(self):
        path = str(self.folder / "One.ts")
        self.helper.parent.send(("record_start", "s", "r1", path, 0, {"max_s": 0.5}))
        self.assertEqual(self.recorded("r1")[3], LIMIT)

    def test_an_unknown_session_and_a_malformed_command(self):
        self.helper.parent.send(("record_start", "gone", "r1", str(self.folder / "x.ts"), 0, {}))
        self.assertEqual(self.recorded("r1"), ("recorded", "gone", "r1", FAILED, "", 0))
        with self.assertLogs("services.live.worker", "WARNING"):
            self.helper.helper._dispatch(("record_start", "s", "r2", "", 0, {}))
            self.helper.helper._dispatch(("record_stop",))
        self.helper.helper._dispatch(("record_stop", "gone"))  # ignored
        self.assertFalse(any(m[0] == "recorded" and m[2] == "r2" for m in self.helper.messages))


class EngineRecordingTests(unittest.TestCase):
    """The GUI process's side: commands, their relay, and one end per recording."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.engine = LiveEngine(tempfile.gettempdir())  # never started: no helper
        self.addCleanup(self.engine.shutdown)
        self.ends = []
        self.engine.recordingEnded.connect(lambda *end: self.ends.append(end))
        self.sid = self.engine.open_session(
            URL_FOR_RING, headers={}, verify_ssl=True, disk=False, max_bytes=MIB
        )
        self.engine._commands.get_nowait()  # the open

    def start(self, path="/r/One.ts", seq=0):
        return self.engine.start_recording(self.sid, path, seq, max_s=60.0, min_free=0)

    def test_a_start_names_the_recording_and_its_limits(self):
        rid = self.engine.start_recording(self.sid, "/r/One.ts", 7, max_s=60.0, min_free=1024)
        self.assertTrue(rid)
        limits = {"max_s": 60.0, "min_free": 1024}
        self.assertEqual(
            self.engine._commands.get_nowait(),
            ("record_start", self.sid, rid, "/r/One.ts", 7, limits),
        )
        unknown = self.engine.start_recording("unknown", "/r/x.ts", 0, max_s=1, min_free=1)
        self.assertEqual(unknown, "")
        self.engine.stop_recording(self.sid)
        self.assertEqual(self.engine._commands.get_nowait(), ("record_stop", self.sid))

    def test_each_recording_ends_once_also_after_its_session_closed(self):
        rid = self.start()
        self.engine._on_message(("closed", self.sid))  # the session is gone ...
        end = (STOPPED, "/r/One (2).ts", 4096)
        self.engine._on_message(("recorded", self.sid, rid, *end))  # ... its recording ends
        self.engine._on_message(("recorded", self.sid, rid, *end))
        self.assertEqual(self.ends, [(rid, *end)])

    def test_a_helper_death_loses_what_it_had_not_said(self):
        rid = self.start()
        self.engine._on_message(("down",))
        self.assertEqual(self.ends, [(rid, "lost", "/r/One.ts", -1)])
        self.engine._on_message(("recorded", self.sid, rid, STOPPED, "/r/One.ts", 1))
        self.assertEqual(len(self.ends), 1)

    def test_nothing_follows_a_shutdown(self):
        self.start()
        self.engine.shutdown()
        self.engine._on_message(("down",))
        self.assertEqual(self.ends, [])

    def test_the_supervisor_sends_a_start_to_a_helper_that_holds_the_session(self):
        commands = queue.Queue()
        supervisor = engine_module._Supervisor(Path(tempfile.gettempdir()), commands)
        relayed, sent = [], []
        supervisor._relay = relayed.append
        command = ("open", "s1", URL_FOR_RING, {}, True, {"kind": "ram", "max_bytes": MIB}, 6.0)
        supervisor._records["s1"] = engine_module._Record(command=command, sent=True)
        start = ("record_start", "s1", "r1", "/r/One.ts", 0, {})
        other = ("record_start", "s2", "r2", "/r/x.ts", 0, {})  # no helper holds s2
        for queued in (start, ("record_stop", "s1"), other):
            commands.put(queued)
            supervisor._send_next(SimpleNamespace(send=sent.append))
        self.assertEqual(sent, [start, ("record_stop", "s1")])
        self.assertEqual(relayed, [("recorded", "s2", "r2", FAILED, "", 0)])
        # Its end is relayed whatever became of the session's record.
        del supervisor._records["s1"]
        supervisor._receive(("recorded", "s1", "r1", STOPPED, "/r/One.ts", 10))
        self.assertEqual(relayed[-1], ("recorded", "s1", "r1", STOPPED, "/r/One.ts", 10))

    def test_without_a_helper_a_start_fails_and_a_stop_is_nothing(self):
        commands = queue.Queue()
        supervisor = engine_module._Supervisor(Path(tempfile.gettempdir()), commands)
        relayed = []
        supervisor._relay = relayed.append
        commands.put(("record_start", "s1", "r1", "/r/One.ts", 0, {}))
        supervisor._serve_without_helper()
        commands.put(("record_stop", "s1"))
        supervisor._serve_without_helper()
        self.assertEqual(relayed, [("recorded", "s1", "r1", FAILED, "", 0)])


class EngineHelperRecordingTests(unittest.TestCase):
    """A real helper: the recording's end reaches the GUI after the session's close."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_a_recording_ends_with_its_file_once_the_channel_closes(self):
        temporary = tempfile.TemporaryDirectory(prefix="qitv-recording-engine-test-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        stop, hung_up = threading.Event(), threading.Event()
        source = LocalSource({SECRET_PATH: paced_route(make_stream(60), stop, hung_up)})
        self.addCleanup(source.close)
        self.addCleanup(stop.set)
        engine = LiveEngine(str(root / "timeshift"))
        self.addCleanup(engine.shutdown)
        ready, ends, closed = [], [], []
        engine.sessionReady.connect(ready.append)
        engine.sessionClosed.connect(closed.append)
        engine.recordingEnded.connect(lambda *end: ends.append(end))
        engine.start()
        sid = engine.open_session(
            source.url + SECRET_PATH, headers={}, verify_ssl=True, disk=False, max_bytes=64 * MIB
        )
        self.assertTrue(wait_until(lambda: ready, 60), "the session never got ready")
        path = str(root / "Recordings" / "One.ts")
        rid = engine.start_recording(sid, path, 0, max_s=60.0, min_free=0)

        def grown():
            return os.path.exists(path) and os.path.getsize(path) > 188 * 200

        # Short waits: a long qWait holds the interpreter, and the local source with it.
        self.assertTrue(wait_until(grown, 10), "the recording did not follow the stream")
        engine.close_session(sid)
        self.assertTrue(wait_until(lambda: ends and closed, 10), "no end came")
        self.assertEqual(ends, [(rid, STOPPED, path, os.path.getsize(path))])
        self.assertEqual(source.connections, 1)
