"""LiveEngine: the GUI process supervising the live helper (real spawn, loopback sources).

Ports main's supervisor tests (tests/test_timeshift.py): a pipe that reports the helper's
exit from poll() as BrokenPipeError (Windows) must not repeat a failure the helper already
reported, a stop is bounded and leaves no process or buffer behind, credentials stay out of
the helper's command line, and owner death closes the upstream socket. New here: helper
death is noticed within a second, its buffers are removed, it restarts once with its
sessions reopened, and a second death turns time-shift off for the run.
"""

import multiprocessing
import operator
import os
from pathlib import Path
import pickle
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import urlopen

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from http_fixtures import LocalSource
from ts_fixtures import make_stream

from services.live import engine as engine_module
from services.live.engine import LiveEngine
from services.live.timeline import Span
from services.thread_cleanup import has_pending_threads

REPO = Path(__file__).resolve().parents[1]
SECRET_PATH = "/user/secret/live.ts"
INTERNAL = "The live buffer stopped unexpectedly."
UNAVAILABLE = "The live buffer is not available."
STATE_KEYS = {"oldest", "newest", "open", "start", "end", "live_edge", "bytes", "ended", "burst_s"}
HEADERS = {"User-Agent": "QiTV-test"}
MIB = 1024 * 1024
PAYLOAD = make_stream(120)
# The owner opens a session against a raw listener and is then killed by the test.
OWNER = """
import multiprocessing, sys, time
from PySide6.QtCore import QCoreApplication
from services.live.engine import LiveEngine
app = QCoreApplication([])
engine = LiveEngine(sys.argv[2])
engine.start()
engine.open_session(sys.argv[1], headers={}, verify_ssl=True, disk=False, max_bytes=1 << 24)
while not multiprocessing.active_children():
    time.sleep(0.01)
print(multiprocessing.active_children()[0].pid, flush=True)
time.sleep(60)
"""


def wait_until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            return False
        QTest.qWait(10)
    return True


def helpers():
    return multiprocessing.active_children()


def kill(process):
    if os.name == "nt":
        process.terminate()
    else:
        os.kill(process.pid, signal.SIGKILL)


def session_dirs(root):
    if not root.is_dir():
        return []
    return sorted(path.name for path in root.iterdir() if path.name.startswith(".qitv-timeshift-"))


def paced_body(payload, stop, hung_up):
    """A close-delimited body streamed a few times faster than real time."""

    def body(request):
        try:
            for offset in range(0, len(payload), 188 * 20):
                if stop.wait(0.02):
                    return
                request.wfile.write(payload[offset : offset + 188 * 20])
        except OSError:
            hung_up.set()

    return body


def paced_route(payload, stop, hung_up):
    """Answers every request (reconnects included) with a fresh paced body."""
    return lambda _request, _count: (200, {}, paced_body(payload, stop, hung_up))


class StalledUpstream:
    """Accepts one HTTP request, sends part of a response header and waits for EOF."""

    def __init__(self):
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{self.listener.getsockname()[1]}{SECRET_PATH}"
        self.requested = threading.Event()
        self.hung_up = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        try:
            client, _address = self.listener.accept()
        except OSError:
            return
        with client:
            client.settimeout(30)
            try:
                data = b""
                while b"\r\n\r\n" not in data:
                    received = client.recv(4096)
                    if not received:
                        return
                    data += received
                self.requested.set()
                client.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: video/mp2t\r\n")
                while client.recv(4096):
                    pass
            except ConnectionError:
                pass  # Windows reports a reset or an abort where Unix reports EOF
            except OSError:
                return
            self.hung_up.set()

    def close(self):
        self.listener.close()


class Unreadable:
    """Pickles here, but unpickling it in the helper raises (neither EOFError nor OSError)."""

    def __reduce__(self):
        return (operator.truediv, (1, 0))


def silent_helper(conn, root):
    """A helper that never says hello."""
    time.sleep(60)


def crashing_session_helper(conn, root):
    """The real helper, except that a session whose URL ends in "/crash" fails unexpectedly
    (its helper stays up)."""
    from services.live import worker

    ingest = worker._Session._ingest

    def crash(session):
        if session._url.endswith("/crash"):
            raise RuntimeError("an unexpected session failure")
        ingest(session)

    worker._Session._ingest = crash
    worker.helper_main(conn, root)


def stubborn_helper(conn, root):
    """A helper that holds a real disk buffer and ignores "shutdown" and SIGTERM."""
    from services.live.ring import DiskRing

    if os.name != "nt":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    conn.send(("hello", "http://127.0.0.1:9/unused"))
    rings = []
    while True:
        try:
            command = conn.recv()
        except EOFError:
            time.sleep(60)
            continue
        if command[0] == "open" and command[5]["kind"] == "disk":
            rings.append(DiskRing(Path(root), command[5]["dir"], command[5]["max_bytes"]))
            conn.send(("ready", command[1]))


class Signals:
    """Every engine signal in emission order, when it came, and from which thread."""

    def __init__(self, engine):
        self.engine = engine
        self.events = []
        self.times = []
        self.up = []  # engine.is_available() as each signal arrived
        self.off_thread = []
        # Direct connections record the emitting thread, which must be the GUI thread.
        direct = Qt.ConnectionType.DirectConnection
        engine.available.connect(lambda ok: self._add("available", ok), direct)
        engine.sessionReady.connect(lambda sid: self._add("ready", sid), direct)
        engine.sessionUpdated.connect(
            lambda sid, snapshot: self._add("updated", sid, snapshot, engine.spans(sid)), direct
        )
        engine.sessionFailed.connect(
            lambda sid, kind, text: self._add("failed", sid, kind, text), direct
        )
        engine.sessionEnded.connect(lambda sid: self._add("ended", sid), direct)
        engine.sessionClosed.connect(lambda sid: self._add("closed", sid), direct)

    def _add(self, *event):
        if threading.current_thread() is not threading.main_thread():
            self.off_thread.append(event)
        self.events.append(event)
        self.times.append(time.monotonic())
        self.up.append(self.engine.is_available())

    def wait_for(self, predicate, timeout=20.0, start=0):
        """The index of the first matching event at or after `start`."""
        deadline = time.monotonic() + timeout
        index = start
        while True:
            while index < len(self.events):
                if predicate(self.events[index]):
                    return index
                index += 1
            if time.monotonic() >= deadline:
                raise AssertionError(f"no matching engine signal among {self.events!r}")
            QTest.qWait(10)

    def of(self, kind, sid=None, start=0):
        return [
            event
            for event in self.events[start:]
            if event[0] == kind and (sid is None or event[1] == sid)
        ]


class LiveEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="qitv-live-engine-test-")
        self.addCleanup(temporary.cleanup)
        self.temporary = Path(temporary.name)
        self.root = self.temporary / "timeshift"
        self.stop = threading.Event()

    def source(self, routes=None):
        self.hung_up = threading.Event()
        server = LocalSource(routes or {SECRET_PATH: paced_route(PAYLOAD, self.stop, self.hung_up)})
        self.addCleanup(server.close)
        self.addCleanup(self.stop.set)  # runs first: paced bodies stop writing
        return server

    def stalled(self):
        upstream = StalledUpstream()
        self.addCleanup(upstream.close)
        return upstream

    def engine(self, root=None):
        engine = LiveEngine(str(root or self.root))
        signals = Signals(engine)
        self.addCleanup(self.reap_survivors)  # runs last
        self.addCleanup(self.retire, engine)
        engine.start()
        return engine, signals

    def retire(self, engine):
        engine.shutdown()
        self.assertTrue(
            wait_until(lambda: not has_pending_threads(), 5), "the supervisor stayed pending"
        )
        self.assertEqual(helpers(), [])

    def reap_survivors(self):
        # A helper the engine failed to stop would hang multiprocessing's exit handler.
        for child in helpers():
            kill(child)
            child.join(5)

    def open(self, engine, url, *, disk=False):
        return engine.open_session(
            url, headers=HEADERS, verify_ssl=True, disk=disk, max_bytes=256 * MIB
        )

    def test_a_session_is_served_with_spans_rebuilt_from_the_deltas(self):
        engine, signals = self.engine()
        self.assertFalse(engine.is_available())
        signals.wait_for(lambda event: event == ("available", True), timeout=60)
        self.assertTrue(engine.is_available())
        source = self.source()
        sid = self.open(engine, source.url + SECRET_PATH)
        self.assertRegex(sid, r"^[0-9a-f]{16}$")
        ready = signals.wait_for(lambda event: event == ("ready", sid))
        signals.wait_for(
            lambda event: event[:2] == ("updated", sid) and len(event[3]) >= 3, start=ready
        )
        counts = [len(event[3]) for event in signals.of("updated", sid)]
        self.assertEqual(counts[0], 0, "the state before ready has no complete segment")
        self.assertEqual(counts, sorted(counts), "spans shrank without eviction")
        spans = engine.spans(sid)
        self.assertTrue(all(isinstance(span, Span) for span in spans))
        self.assertEqual([span.seq for span in spans], list(range(len(spans))))
        for before, after in zip(spans, spans[1:]):
            self.assertAlmostEqual(before.end, after.start)
        snapshot = engine.snapshot(sid)
        self.assertEqual(set(snapshot), STATE_KEYS)
        self.assertEqual(snapshot["oldest"], 0)
        self.assertEqual(signals.of("updated", sid)[-1][2], snapshot)

        url = engine.stream_url(sid, spans[0].seq)
        self.assertRegex(url, rf"^http://127\.0\.0\.1:[0-9]+/[0-9a-f]{{32}}/{sid}/stream/0\.ts$")
        with urlopen(url, timeout=10) as response:
            data = response.read(188 * 64)
        self.assertEqual(len(data), 188 * 64)
        self.assertEqual((data[0], ((data[1] & 0x1F) << 8) | data[2]), (0x47, 0), "PAT first")

        engine.close_session(sid)
        closed = signals.wait_for(lambda event: event == ("closed", sid))
        self.assertTrue(self.hung_up.wait(3), "the closed session kept its upstream")
        QTest.qWait(300)
        self.assertEqual([event for event in signals.events[closed + 1 :] if event[1] == sid], [])
        self.assertEqual(engine.spans(sid), [])
        self.assertEqual(signals.of("failed"), [])
        self.assertEqual(source.counts[SECRET_PATH], 1)
        self.assertEqual(source.requests[0][1].get("User-Agent"), "QiTV-test")
        self.assertEqual(signals.off_thread, [], "signals must arrive on the GUI thread")
        self.assertNotIn("secret", repr(signals.events))

    def test_helper_death_restarts_once_and_a_second_death_turns_time_shift_off(self):
        cleaned = []
        cleanup = engine_module.cleanup_session_dir

        def spy(root, name):
            removed = cleanup(root, name)
            cleaned.append((Path(root), name, removed))
            return removed

        self.enterContext(patch.object(engine_module, "cleanup_session_dir", spy))
        engine, signals = self.engine()
        signals.wait_for(lambda event: event == ("available", True), timeout=60)
        source = self.source()
        sid = self.open(engine, source.url + SECRET_PATH, disk=True)
        signals.wait_for(lambda event: event == ("ready", sid))
        [directory] = session_dirs(self.root)
        [first] = helpers()
        first_pid = first.pid

        with self.assertLogs("services.live.engine", "WARNING") as logs:
            mark = len(signals.events)
            killed = time.monotonic()
            kill(first)
            failed = signals.wait_for(
                lambda event: event == ("failed", sid, "internal", INTERNAL), start=mark
            )
            self.assertLess(signals.times[failed] - killed, 1.0, "helper death noticed late")
            self.assertTrue(wait_until(lambda: (self.root, directory, True) in cleaned))
            self.assertFalse(engine.is_available())
            # The restarted helper reopens the session with the same arguments.
            signals.wait_for(lambda event: event == ("available", True), start=failed)
            signals.wait_for(lambda event: event == ("ready", sid), start=failed)
            signals.wait_for(lambda event: event[:2] == ("updated", sid) and event[3], start=failed)
        self.assertTrue(engine.is_available())
        [second] = helpers()
        self.assertNotEqual(second.pid, first_pid)
        self.assertEqual(source.counts[SECRET_PATH], 2)
        self.assertEqual(source.requests[-1][1].get("User-Agent"), "QiTV-test")
        self.assertEqual(engine.spans(sid)[0].seq, 0, "the dead helper's spans were kept")
        self.assertEqual(session_dirs(self.root), [directory])
        for line in logs.output:
            self.assertNotIn("secret", line)
            self.assertNotIn(source.url, line)

        mark = len(signals.events)
        with self.assertLogs("services.live.engine", "WARNING") as logs:
            kill(second)
            unavailable = signals.wait_for(lambda event: event == ("available", False), start=mark)
        self.assertIn("time-shift is off for this run", logs.output[-1])
        self.assertEqual(signals.of("failed", start=mark), [("failed", sid, "internal", INTERNAL)])
        self.assertFalse(engine.is_available())
        self.assertEqual(session_dirs(self.root), [], "the dead helper's buffer was left behind")
        self.assertEqual(engine.spans(sid), [])
        self.assertEqual(engine.stream_url(sid, 0), "")
        QTest.qWait(500)
        self.assertEqual(helpers(), [], "a second restart was attempted")
        self.assertEqual(signals.of("available", start=unavailable + 1), [])

        engine.close_session(sid)
        signals.wait_for(lambda event: event == ("closed", sid), start=unavailable)
        late = self.open(engine, source.url + SECRET_PATH)
        # Final: no reopen follows; available(False) already came.
        signals.wait_for(lambda event: event == ("failed", late, "unavailable", UNAVAILABLE))
        self.assertEqual(len(signals.of("failed", sid)), 2, "one failure per helper death")
        self.assertEqual(signals.off_thread, [])

    def test_a_closed_pipe_does_not_repeat_a_failure_the_helper_reported(self):
        # Ported from main's test_closed_recording_pipe_does_not_repeat_reported_source_failure.
        context = multiprocessing.get_context("spawn")
        native_pipe = context.Pipe
        closed = threading.Event()
        crash = threading.Event()

        def windows_eof_pipe(*args, **kwargs):
            receiver, sender = native_pipe(*args, **kwargs)
            native_poll, native_recv, native_send = receiver.poll, receiver.recv, receiver.send
            pending = []

            def poll(timeout=0):
                try:
                    if not native_poll(timeout):
                        return False
                    pending.append(native_recv())
                except EOFError, BrokenPipeError:
                    # Windows PeekNamedPipe reports a closed peer from poll,
                    # before recv can translate it into EOFError.
                    closed.set()
                    raise BrokenPipeError("helper pipe closed") from None
                return True

            def send(command):
                if crash.is_set():
                    crash.clear()
                    command = Unreadable()  # the helper's command loop fails on it
                native_send(command)

            receiver.poll = poll
            receiver.recv = pending.pop
            receiver.send = send
            return receiver, sender

        source = self.source()
        with patch.object(context, "Pipe", windows_eof_pipe):
            engine, signals = self.engine()
            signals.wait_for(lambda event: event == ("available", True), timeout=60)
            sid = self.open(engine, source.url + SECRET_PATH)
            signals.wait_for(lambda event: event == ("ready", sid))
            mark = len(signals.events)
            with self.assertLogs("services.live.engine", "WARNING"):
                crash.set()
                other = self.open(engine, source.url + SECRET_PATH)
                # The helper reports its failure for the open session, then closes the pipe.
                failed = signals.wait_for(
                    lambda event: event == ("failed", sid, "internal", INTERNAL), start=mark
                )
                restarted = signals.wait_for(
                    lambda event: event == ("available", True), start=failed
                )
            for session in (sid, other):
                signals.wait_for(lambda event, s=session: event == ("ready", s), start=restarted)
            QTest.qWait(300)
        self.assertTrue(closed.is_set(), "the helper pipe never reached its closed-peer boundary")
        self.assertEqual(signals.of("failed", sid), [("failed", sid, "internal", INTERNAL)])
        self.assertEqual(signals.of("failed", other), [("failed", other, "internal", INTERNAL)])
        # The command loop's crash is a helper death: "down" (not available) comes first.
        for index, event in enumerate(signals.events):
            if event[0] == "failed":
                self.assertFalse(signals.up[index], f"{event} arrived while still available")

    def test_the_first_start_removes_buffers_a_crashed_run_left(self):
        # Up to 16 GiB could stay behind after a crash, forever if disk time-shift is then
        # turned off: the engine sweeps once before its first helper.
        stale = self.root / (".qitv-timeshift-" + "a" * 32)
        stale.mkdir(parents=True)
        (stale / "owner").write_text("QiTV temporary time-shift v1\n", encoding="ascii")
        (stale / "segment-0.ts").write_bytes(b"left over")
        engine, signals = self.engine()
        signals.wait_for(lambda event: event == ("available", True), timeout=60)
        self.assertFalse(stale.exists())

    def test_a_dead_helper_that_cannot_be_stopped_turns_time_shift_off(self):
        # A helper stuck in I/O (a network drive) may outlive a kill with its upstream
        # connections open: restarting and reopening would add a second one per session.
        reap = engine_module._Supervisor._reap

        def unstoppable(supervisor, process, conn, grace):
            reap(supervisor, process, conn, grace)  # it does stop here, but reports it did not
            return False

        self.enterContext(patch.object(engine_module._Supervisor, "_reap", unstoppable))
        engine, signals = self.engine()
        signals.wait_for(lambda event: event == ("available", True), timeout=60)
        source = self.source()
        sid = self.open(engine, source.url + SECRET_PATH)
        signals.wait_for(lambda event: event == ("ready", sid))
        mark = len(signals.events)
        with self.assertLogs("services.live.engine", "WARNING") as logs:
            kill(helpers()[0])
            off = signals.wait_for(lambda event: event == ("available", False), start=mark)
        self.assertIn("time-shift is off for this run", logs.output[-1])
        self.assertEqual(signals.of("failed", start=mark), [("failed", sid, "internal", INTERNAL)])
        QTest.qWait(500)
        self.assertEqual(helpers(), [], "a helper was started again")
        self.assertEqual(signals.of("available", start=off + 1), [])
        self.assertEqual(source.counts[SECRET_PATH], 1, "the session was reopened")
        engine.close_session(sid)
        signals.wait_for(lambda event: event == ("closed", sid), start=off)

    def test_a_session_crash_is_final_and_never_reopened(self):
        self.enterContext(patch.object(engine_module, "helper_main", crashing_session_helper))
        engine, signals = self.engine()
        signals.wait_for(lambda event: event == ("available", True), timeout=60)
        source = self.source()
        live = self.open(engine, source.url + SECRET_PATH)
        crashed = self.open(engine, source.url + "/user/secret/crash")
        failed = signals.wait_for(lambda event: event[:2] == ("failed", crashed))
        self.assertEqual(signals.events[failed], ("failed", crashed, "crashed", INTERNAL))
        self.assertTrue(signals.up[failed], "the helper stayed up")
        signals.wait_for(lambda event: event == ("ready", live))
        mark = len(signals.events)
        with self.assertLogs("services.live.engine", "WARNING"):
            kill(helpers()[0])
            restarted = signals.wait_for(lambda event: event == ("available", True), start=mark)
        signals.wait_for(lambda event: event == ("ready", live), start=restarted)
        QTest.qWait(500)  # time for an unwanted reopen to crash again
        # A reopen would crash once more: the session heard of its failure once, and of the
        # helper's death not at all.
        self.assertEqual(signals.of("failed", crashed), [("failed", crashed, "crashed", INTERNAL)])
        self.assertEqual(signals.of("failed", live), [("failed", live, "internal", INTERNAL)])
        engine.close_session(crashed)
        signals.wait_for(lambda event: event == ("closed", crashed), start=mark)

    def test_shutdown_is_bounded_and_leaves_no_helper_or_buffer(self):
        engine, signals = self.engine()
        signals.wait_for(lambda event: event == ("available", True), timeout=60)
        source = self.source()
        stalled = self.stalled()
        sessions = [
            self.open(engine, source.url + SECRET_PATH, disk=True),
            self.open(engine, source.url + SECRET_PATH),
        ]
        stalled_sid = self.open(engine, stalled.url, disk=True)
        for sid in sessions:
            signals.wait_for(lambda event, sid=sid: event == ("ready", sid))
        self.assertTrue(stalled.requested.wait(10))
        self.assertEqual(len(session_dirs(self.root)), 1)
        mark = len(signals.events)

        started = time.monotonic()
        engine.shutdown()
        self.assertLess(time.monotonic() - started, 2.0, "shutdown was not bounded")
        self.assertEqual(multiprocessing.active_children(), [])
        self.assertFalse(has_pending_threads(), "the supervisor thread was not retired")
        self.assertEqual(list(self.root.iterdir()), [], "a session buffer was left behind")
        self.assertFalse(engine.is_available())
        self.assertTrue(stalled.hung_up.wait(2), "the stalled upstream stayed open")
        self.assertTrue(self.hung_up.wait(2), "an upstream stayed open")
        QTest.qWait(300)
        self.assertEqual(signals.events[mark:], [], "signals after shutdown")
        self.assertEqual(engine.stream_url(stalled_sid, 0), "")
        engine.shutdown()  # a second call changes nothing

    def test_shutdown_kills_a_helper_that_ignores_it_and_removes_its_buffer(self):
        with patch.object(engine_module, "helper_main", stubborn_helper):
            engine, signals = self.engine()
            signals.wait_for(lambda event: event == ("available", True), timeout=60)
        sid = self.open(engine, "http://127.0.0.1:9/unused", disk=True)
        signals.wait_for(lambda event: event == ("ready", sid))
        self.assertEqual(len(session_dirs(self.root)), 1)
        started = time.monotonic()
        engine.shutdown()
        self.assertLess(time.monotonic() - started, 2.0, "the escalation was not bounded")
        self.assertEqual(multiprocessing.active_children(), [])
        self.assertEqual(list(self.root.iterdir()), [], "the killed helper's buffer was left")
        self.assertFalse(has_pending_threads())

    def test_requests_before_the_helper_is_up_and_a_disk_notice_that_is_not_final(self):
        # A file where the buffer directory should be: the helper's disk ring fails and
        # the session goes on in RAM, which is a notice, not the session's failure.
        blocked = self.temporary / "not-a-directory"
        blocked.write_bytes(b"")
        engine, signals = self.engine(blocked)
        source = self.source()
        sid = self.open(engine, source.url + SECRET_PATH, disk=True)
        early = self.open(engine, source.url + SECRET_PATH)
        engine.close_session(early)  # closed before it reached the helper
        engine.close_session(early)  # a second close changes nothing
        signals.wait_for(lambda event: event == ("closed", early))
        notice = signals.wait_for(lambda event: event[:3] == ("failed", sid, "disk"), timeout=60)
        ready = signals.wait_for(lambda event: event == ("ready", sid))
        self.assertLess(notice, ready, "the disk notice comes before ready")
        self.assertEqual([event for event in signals.events if early in event], [("closed", early)])

        mark = len(signals.events)
        with self.assertLogs("services.live.engine", "WARNING"):
            kill(helpers()[0])
            failed = signals.wait_for(
                lambda event: event == ("failed", sid, "internal", INTERNAL), start=mark
            )
            # The restarted helper reopens the session in RAM: no second disk notice.
            signals.wait_for(lambda event: event == ("ready", sid), start=failed)
        self.assertEqual(
            signals.of("failed", sid, start=mark), [("failed", sid, "internal", INTERNAL)]
        )

    def test_a_restart_reopens_only_sessions_whose_input_was_still_running(self):
        def stream_then_refuse(_request, count):
            # Streams, then refuses the reconnect: an "http" failure after ready.
            if count == 1:
                return 200, {}, paced_body(make_stream(6), self.stop, threading.Event())
            return 404, {}, b""

        paths = {
            "live": SECRET_PATH,
            "http": "/user/secret/refused.ts",
            "unsupported": "/user/secret/page.ts",
            "ended": "/user/secret/finite.ts",
        }
        source = self.source(
            {
                paths["live"]: paced_route(PAYLOAD, self.stop, threading.Event()),
                paths["http"]: stream_then_refuse,
                paths["unsupported"]: (200, {}, b"<html>not a transport stream</html>" * 2000),
                paths["ended"]: (200, {}, make_stream(4)),
            }
        )
        engine, signals = self.engine()
        signals.wait_for(lambda event: event == ("available", True), timeout=60)
        sids = {name: self.open(engine, source.url + path) for name, path in paths.items()}
        signals.wait_for(lambda event: event == ("ready", sids["http"]))
        signals.wait_for(lambda event: event[:3] == ("failed", sids["http"], "http"))
        signals.wait_for(lambda event: event[:3] == ("failed", sids["unsupported"], "unsupported"))
        signals.wait_for(lambda event: event == ("ended", sids["ended"]))
        signals.wait_for(lambda event: event == ("ready", sids["live"]))
        requests = dict(source.counts)
        self.assertEqual(requests[paths["http"]], 2)

        mark = len(signals.events)
        with self.assertLogs("services.live.engine", "WARNING"):
            kill(helpers()[0])
            restarted = signals.wait_for(lambda event: event == ("available", True), start=mark)
        signals.wait_for(lambda event: event == ("ready", sids["live"]), start=restarted)
        QTest.qWait(1000)  # time for an unwanted reopen to reach the source
        for name in ("http", "unsupported", "ended"):
            self.assertEqual(source.counts[paths[name]], requests[paths[name]], name)
            self.assertEqual(signals.of("ready", sids[name], start=mark), [], name)
        self.assertEqual(source.counts[paths["live"]], requests[paths["live"]] + 1)
        # Only the live session gets a failure: after `ended` or a final failure only
        # `sessionClosed` follows, even though the buffer is gone with the helper.
        self.assertEqual(
            signals.of("failed", start=mark), [("failed", sids["live"], "internal", INTERNAL)]
        )
        # A second death gives up; the sessions whose input is over hear nothing more.
        mark = len(signals.events)
        with self.assertLogs("services.live.engine", "WARNING"):
            kill(helpers()[0])
            signals.wait_for(lambda event: event == ("available", False), start=mark)
        self.assertEqual(
            signals.of("failed", start=mark), [("failed", sids["live"], "internal", INTERNAL)]
        )
        for sid in sids.values():  # every session is still answered by close
            engine.close_session(sid)
        for sid in sids.values():
            signals.wait_for(lambda event, sid=sid: event == ("closed", sid), start=mark)

    def test_a_helper_that_cannot_start_turns_time_shift_off(self):
        failure = pickle.PicklingError("frozen build")
        with patch("multiprocessing.context.SpawnProcess.start", side_effect=failure):
            with self.assertLogs("services.live.engine", "WARNING") as logs:
                engine, signals = self.engine()
                signals.wait_for(lambda event: event == ("available", False))
        started = [line for line in logs.output if "could not be started (PicklingError)" in line]
        self.assertEqual(len(started), 2, "one start and one restart")
        self.assertFalse(engine.is_available())
        source_url = "http://127.0.0.1:9" + SECRET_PATH
        sid = self.open(engine, source_url)
        signals.wait_for(lambda event: event == ("failed", sid, "unavailable", UNAVAILABLE))
        engine.close_session(sid)
        signals.wait_for(lambda event: event == ("closed", sid))
        self.assertEqual(helpers(), [])
        self.assertNotIn(source_url, repr(signals.events))

    def test_a_helper_that_never_says_hello_counts_as_dead(self):
        with (
            patch.object(engine_module, "helper_main", silent_helper),
            patch.object(engine_module, "_HELLO_TIMEOUT", 1.0),
            self.assertLogs("services.live.engine", "WARNING") as logs,
        ):
            engine, signals = self.engine()
            # It never reaches a helper: when time-shift goes off, it fails for good.
            early = self.open(engine, "http://127.0.0.1:9" + SECRET_PATH)
            off = signals.wait_for(lambda event: event == ("available", False), timeout=30)
        failed = signals.wait_for(lambda event: event[:2] == ("failed", early))
        self.assertLess(off, failed, "available(False) comes first")
        self.assertEqual(signals.events[failed], ("failed", early, "unavailable", UNAVAILABLE))
        late = [line for line in logs.output if "did not start in time" in line]
        self.assertEqual(len(late), 2, "one start and one restart")
        self.assertEqual(helpers(), [])

    def test_a_closed_disk_session_is_forgotten_once_its_buffer_is_gone(self):
        cleaned = []
        cleanup = engine_module.cleanup_session_dir

        def spy(root, name):
            cleaned.append(name)
            return cleanup(root, name)

        self.enterContext(patch.object(engine_module, "cleanup_session_dir", spy))
        engine, signals = self.engine()
        signals.wait_for(lambda event: event == ("available", True), timeout=60)
        source = self.source()
        first = self.open(engine, source.url + SECRET_PATH, disk=True)
        signals.wait_for(lambda event: event == ("ready", first))
        [first_dir] = session_dirs(self.root)
        engine.close_session(first)
        signals.wait_for(lambda event: event == ("closed", first))
        self.assertTrue(wait_until(lambda: session_dirs(self.root) == []))
        second = self.open(engine, source.url + SECRET_PATH, disk=True)
        signals.wait_for(lambda event: event == ("ready", second))
        engine.close_session(second)  # its "closed" prunes the first directory
        signals.wait_for(lambda event: event == ("closed", second))
        engine.shutdown()
        self.assertNotIn(first_dir, cleaned, "a removed directory was still tracked")
        self.assertEqual(session_dirs(self.root), [])

    @unittest.skipIf(os.name == "nt", "reads the command line with ps")
    def test_credentials_never_reach_the_helper_command_line(self):
        stalled = self.stalled()
        engine, signals = self.engine()
        sid = self.open(engine, stalled.url)
        self.assertTrue(stalled.requested.wait(60))
        [helper] = helpers()
        command = subprocess.run(
            ["ps", "-o", "command=", "-p", str(helper.pid)],
            capture_output=True,
            check=True,
            timeout=5,
        ).stdout.decode()
        self.assertIn("multiprocessing", command)
        self.assertNotIn("secret", command)
        self.assertNotIn(stalled.url, command)
        engine.close_session(sid)
        signals.wait_for(lambda event: event == ("closed", sid))
        self.assertTrue(stalled.hung_up.wait(2))

    def test_owner_death_makes_the_helper_close_its_upstream(self):
        # Ported from main's test_owner_death_interrupts_stalled_native_source.
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.settimeout(60)
            url = f"http://127.0.0.1:{listener.getsockname()[1]}{SECRET_PATH}"
            owner = subprocess.Popen(
                [sys.executable, "-c", OWNER, url, str(self.root)],
                cwd=REPO,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            helper_pid = None
            closed = False
            try:
                with listener.accept()[0] as peer:
                    request = b""
                    while b"\r\n\r\n" not in request:
                        received = peer.recv(4096)
                        self.assertTrue(received, "the helper hung up before its request")
                        request += received
                    helper_pid = int(owner.stdout.readline())
                    owner.kill()
                    owner.wait(5)
                    peer.settimeout(3)
                    try:
                        remaining = peer.recv(1)
                    except ConnectionResetError:
                        # Windows reports a reset when the helper's socket closes.
                        remaining = b""
                    self.assertEqual(remaining, b"", "an orphaned helper kept its upstream")
                    closed = True
            finally:
                if owner.poll() is None:
                    owner.kill()
                    owner.wait(5)
                if helper_pid is not None and not closed:
                    try:
                        os.kill(helper_pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                owner.stdout.close()


if __name__ == "__main__":
    unittest.main()
