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
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.request import urlopen

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from gc_fixtures import collect_on_the_gui_thread
from http_fixtures import LocalSource
from process_fixtures import still_running, terminate
from ts_fixtures import make_stream

from services.live import engine as engine_module
from services.live.engine import LiveEngine
from services.live.ring import read_index
from services.live.timeline import START_KEY, START_NONE, START_OPEN, Span
from services.thread_cleanup import has_pending_threads

REPO = Path(__file__).resolve().parents[1]
SECRET_PATH = "/user/secret/live.ts"
INTERNAL = "The live buffer stopped unexpectedly."
UNAVAILABLE = "The live buffer is not available."
STATE_KEYS = {
    "oldest",
    "newest",
    "open",
    "start",
    "end",
    "live_edge",
    "bytes",
    "ended",
    "burst_s",
    "received",
    "hls",
    "fmp4",  # HLS of fragmented MP4 (the Picture panel's statistics name the source)
    "breaks",
    "caught_up",
    "backlog_s",
    "finite",
    "open_quality",
    "video",
    "answered",
    "reconnecting",
    "parts",  # 2.6: a kept buffer's runs, each with its time of day
    "secure",  # 2.7.1: its source's address is HTTPS
}
HEADERS = {"User-Agent": "QiTV-test"}
MIB = 1024 * 1024
PAYLOAD = make_stream(120)
URL_FOR_RING = "http://provider.test/live/1.ts"
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
# The owner plays a disk session, then leaves like TVWindow's _exit_now: os._exit, with no
# atexit handler and no engine shutdown.
OWNER_EXIT = """
import multiprocessing, os, sys, time
from PySide6.QtCore import QCoreApplication
from services.live.engine import LiveEngine
app = QCoreApplication([])
engine = LiveEngine(sys.argv[2])
ready = []
engine.sessionReady.connect(ready.append)
engine.start()
engine.open_session(sys.argv[1], headers={}, verify_ssl=True, disk=True, max_bytes=1 << 28)
deadline = time.monotonic() + 60
while not ready and time.monotonic() < deadline:
    app.processEvents()
    time.sleep(0.01)
print(multiprocessing.active_children()[0].pid if ready else "", flush=True)
os._exit(3)
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
    prefixes = (".qitv-timeshift-", ".qitv-buffer-")  # 2.6: v2 folders
    return sorted(path.name for path in root.iterdir() if path.name.startswith(prefixes))


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


class RamFallbackSizeTests(unittest.TestCase):
    """After a disk failure a session goes on in memory of timeshift_ram_mib."""

    def test_a_disk_session_names_its_memory_fallback_size(self):
        import queue

        from services.live import engine as engine_module

        commands: queue.Queue = queue.Queue()
        engine = engine_module.LiveEngine.__new__(engine_module.LiveEngine)
        engine._shut, engine._sessions, engine._commands = False, {}, commands
        engine.open_session(
            URL_FOR_RING,
            headers={},
            verify_ssl=True,
            disk=True,
            max_bytes=4096 * MIB,
            ram_bytes=512 * MIB,
        )
        ring = commands.get_nowait()[5]
        self.assertEqual((ring["kind"], ring["ram_bytes"]), ("disk", 512 * MIB))

    def test_a_reopen_after_a_disk_failure_uses_that_size(self):
        import queue

        from services.live import engine as engine_module

        supervisor = engine_module._Supervisor(Path(tempfile.gettempdir()), queue.Queue())
        supervisor._relay = lambda message: None
        ring = {"kind": "disk", "dir": "d", "max_bytes": 4096 * MIB, "ram_bytes": 512 * MIB}
        command = ("open", "s1", URL_FOR_RING, {}, True, ring, 6.0)
        supervisor._records["s1"] = engine_module._Record(command=command, sent=True)
        supervisor._receive(("error", "s1", "disk", "The disk buffer failed."))
        self.assertEqual(
            supervisor._records["s1"].command[5], {"kind": "ram", "max_bytes": 512 * MIB}
        )


class StartTargetTests(unittest.TestCase):
    """2.1: the session's target reaches the helper, so an HLS source starts far
    enough back for a start at it; a reopen keeps it."""

    def test_the_open_command_carries_the_target_and_extra(self):
        import queue

        from services.live import engine as engine_module

        commands: queue.Queue = queue.Queue()
        engine = engine_module.LiveEngine.__new__(engine_module.LiveEngine)
        engine._shut, engine._sessions, engine._commands = False, {}, commands
        engine.open_session(URL_FOR_RING, headers={}, verify_ssl=True, disk=False, max_bytes=MIB)
        self.assertEqual(len(commands.get_nowait()), 7)  # 2.0's command: live_delay only
        engine.open_session(
            URL_FOR_RING,
            headers={},
            verify_ssl=True,
            disk=True,
            max_bytes=MIB,
            target=2.5,
            extra=3.0,
        )
        command = commands.get_nowait()
        self.assertEqual(command[6:], (6.0, {"target": 2.5, "extra": 3.0}))
        supervisor = engine_module._Supervisor(Path(tempfile.gettempdir()), queue.Queue())
        supervisor._relay = lambda message: None
        supervisor._records["s1"] = engine_module._Record(command=command, sent=True)
        supervisor._receive(("error", "s1", "disk", "The disk buffer failed."))
        self.assertEqual(supervisor._records["s1"].command[6:], command[6:])

    def test_a_window_open_carries_its_options(self):
        # 2.7: a server part's start and stop travel in the start dict.
        import queue

        from services.live import engine as engine_module

        commands: queue.Queue = queue.Queue()
        engine = engine_module.LiveEngine.__new__(engine_module.LiveEngine)
        engine._shut, engine._sessions, engine._commands = False, {}, commands
        window = {"back_s": 60.0, "until_seq": 108, "ahead_s": 60.0}
        engine.open_session(
            URL_FOR_RING, headers={}, verify_ssl=True, disk=False, max_bytes=MIB, window=window
        )
        self.assertEqual(commands.get_nowait()[7], {"window": window})
        engine.open_session(
            URL_FOR_RING,
            headers={},
            verify_ssl=True,
            disk=False,
            max_bytes=MIB,
            target=2.5,
            window=window,
        )
        self.assertEqual(commands.get_nowait()[7], {"target": 2.5, "extra": 0.0, "window": window})

    def test_a_server_part_is_not_reopened_after_a_helper_death(self):
        import queue

        from services.live import engine as engine_module

        supervisor = engine_module._Supervisor(Path(tempfile.gettempdir()), queue.Queue())
        ring = {"kind": "ram", "max_bytes": 64 * MIB}
        part = ("open", "p", URL_FOR_RING, {}, True, ring, 6.0, {"window": {"back_s": 9.0}})
        live = ("open", "l", URL_FOR_RING, {}, True, ring, 6.0)
        for sid, command in (("p", part), ("l", live)):
            supervisor._records[sid] = engine_module._Record(command=command, sent=True)
        relayed, sent = [], []
        supervisor._relay = relayed.append
        supervisor._report_lost()
        supervisor._conn = SimpleNamespace(send=sent.append)
        supervisor._receive(("hello", "http://127.0.0.1:1/t"))
        self.assertEqual([command[1] for command in sent if command[0] == "open"], ["l"])
        self.assertIn(("error", "p", "internal", engine_module._INTERNAL), relayed)

    def test_set_until_reaches_the_helper_and_never_closes(self):
        # 2.7: a server part's stop follows the live buffer's start; the supervisor's
        # fallback for an unknown command is a close, so this one has its own branch.
        import queue

        from services.live import engine as engine_module

        commands: queue.Queue = queue.Queue()
        engine = engine_module.LiveEngine.__new__(engine_module.LiveEngine)
        engine._shut, engine._sessions, engine._commands = False, {}, commands
        sid = engine.open_session(
            URL_FOR_RING, headers={}, verify_ssl=True, disk=False, max_bytes=MIB, window={}
        )
        opened = commands.get_nowait()
        engine.set_until(sid, 110)
        until = commands.get_nowait()
        self.assertEqual(until, ("until", sid, 110))
        supervisor = engine_module._Supervisor(Path(tempfile.gettempdir()), queue.Queue())
        supervisor._records[sid] = engine_module._Record(command=opened, sent=True)
        supervisor._commands.put(until)
        sent = []
        supervisor._send_next(SimpleNamespace(send=sent.append))
        self.assertEqual(sent, [until])
        self.assertFalse(supervisor._records[sid].closing)


class ResizeTests(unittest.TestCase):
    """2.2: the multiview's tiles grow and shrink their RAM buffers; a reopen keeps the size."""

    def test_a_resize_reaches_the_helper_and_a_reopen_keeps_it(self):
        import queue

        from services.live import engine as engine_module

        commands: queue.Queue = queue.Queue()
        engine = engine_module.LiveEngine.__new__(engine_module.LiveEngine)
        engine._shut, engine._sessions, engine._commands = False, {}, commands
        sid = engine.open_session(
            URL_FOR_RING, headers={}, verify_ssl=True, disk=False, max_bytes=256 * MIB, target=2.5
        )
        opened = commands.get_nowait()
        engine.resize_session(sid, 64 * MIB)
        engine.resize_session("unknown", 64 * MIB)  # not a session of this engine
        engine.resize_session(sid, 0)
        self.assertEqual(commands.get_nowait(), ("resize", sid, 64 * MIB))
        self.assertTrue(commands.empty())
        supervisor = engine_module._Supervisor(Path(tempfile.gettempdir()), commands)
        sent = []
        connection = SimpleNamespace(send=sent.append)
        supervisor._records[sid] = engine_module._Record(command=opened, sent=True)
        commands.put(("resize", sid, 64 * MIB))
        supervisor._send_next(connection)
        self.assertEqual(sent, [("resize", sid, 64 * MIB)])
        record = supervisor._records[sid]
        self.assertEqual(record.command[5], {"kind": "ram", "max_bytes": 64 * MIB})
        self.assertEqual(record.command[6:], opened[6:])  # the target stays
        record.sent = False  # the helper died: a reopen opens with the new size
        commands.put(("resize", sid, 128 * MIB))
        supervisor._send_next(connection)
        self.assertEqual(len(sent), 1)
        self.assertEqual(record.command[5]["max_bytes"], 128 * MIB)

    def test_a_disk_session_keeps_the_users_size(self):
        import queue

        from services.live import engine as engine_module

        commands: queue.Queue = queue.Queue()
        supervisor = engine_module._Supervisor(Path(tempfile.gettempdir()), commands)
        ring = {"kind": "disk", "dir": "d", "max_bytes": 4096 * MIB}
        command = ("open", "s1", URL_FOR_RING, {}, True, ring, 6.0)
        supervisor._records["s1"] = engine_module._Record(command=command, sent=True)
        sent = []
        commands.put(("resize", "s1", 64 * MIB))
        supervisor._send_next(SimpleNamespace(send=sent.append))
        self.assertEqual((sent, supervisor._records["s1"].command), ([], command))


class ParkResumeTests(unittest.TestCase):
    """2.6: a parked session keeps its view; the supervisor never reopens it by itself."""

    def engine(self):
        import queue

        commands: queue.Queue = queue.Queue()
        engine = LiveEngine.__new__(LiveEngine)
        LiveEngine.__init__(engine, tempfile.gettempdir())
        engine._commands = commands
        return engine, commands

    def test_park_and_resume_commands(self):
        engine, commands = self.engine()
        sid = engine.open_session(
            URL_FOR_RING, headers={}, verify_ssl=True, disk=False, max_bytes=256 * MIB, target=2.5
        )
        commands.get_nowait()
        engine.park_session(sid)
        engine.park_session("unknown")
        engine.resume_session(
            sid, URL_FOR_RING, headers={"User-Agent": "a"}, verify_ssl=False, target=3.0
        )
        self.assertEqual(commands.get_nowait(), ("park", sid))
        self.assertEqual(
            commands.get_nowait(),
            (
                "resume",
                sid,
                URL_FOR_RING,
                {"User-Agent": "a"},
                False,
                6.0,
                {"target": 3.0, "extra": 0.0},
            ),
        )
        self.assertTrue(commands.empty())

    def test_https_first_reaches_the_helper_on_an_open_and_a_resume(self):
        # 2.7.1: a provider preferring HTTPS has its stream asked over HTTPS first.
        engine, commands = self.engine()
        sid = engine.open_session(
            URL_FOR_RING, headers={}, verify_ssl=True, disk=False, max_bytes=MIB, https_first=True
        )
        self.assertEqual(commands.get_nowait()[7], {"https": True})
        engine.resume_session(sid, URL_FOR_RING, headers={}, verify_ssl=True, https_first=True)
        self.assertEqual(commands.get_nowait()[5:], (6.0, {"https": True}))
        engine.open_session(URL_FOR_RING, headers={}, verify_ssl=True, disk=False, max_bytes=MIB)
        self.assertEqual(len(commands.get_nowait()), 7)  # without it, as before

    def test_a_size_cap_reaches_the_helper_on_an_open_and_a_resume(self):
        # 2.8, the live ⚙: the user's cap for an HLS master's variants.
        engine, commands = self.engine()
        sid = engine.open_session(
            URL_FOR_RING, headers={}, verify_ssl=True, disk=False, max_bytes=MIB, max_height=720
        )
        self.assertEqual(commands.get_nowait()[7], {"max_height": 720})
        engine.resume_session(sid, URL_FOR_RING, headers={}, verify_ssl=True, max_height=720)
        self.assertEqual(commands.get_nowait()[5:], (6.0, {"max_height": 720}))
        engine.open_session(
            URL_FOR_RING, headers={}, verify_ssl=True, disk=False, max_bytes=MIB, max_height=0
        )
        self.assertEqual(len(commands.get_nowait()), 7)  # no cap: as before

    def test_a_resume_that_joins_says_so(self):  # 2.8 review I1: a rewound viewer's
        engine, commands = self.engine()
        sid = engine.open_session(
            URL_FOR_RING, headers={}, verify_ssl=True, disk=False, max_bytes=MIB
        )
        commands.get_nowait()
        engine.resume_session(sid, URL_FOR_RING, headers={}, verify_ssl=True, join=True)
        self.assertEqual(commands.get_nowait()[5:], (6.0, {"join": True}))

    def test_it_says_which_files_its_recordings_write(self):  # 2.8 final review 8
        # The Library keeps a file being written from deletion; the phone reads one on as it
        # grows. Read from other threads: a set that is replaced, never changed.
        engine, _ = self.engine()
        sid = engine.open_session(
            URL_FOR_RING, headers={}, verify_ssl=True, disk=False, max_bytes=MIB
        )
        self.assertEqual(engine.writing, frozenset())
        rid = engine.start_recording(sid, "/rec/a.ts", 0, max_s=60.0, min_free=0)
        before = engine.writing
        self.assertEqual(before, frozenset({"/rec/a.ts"}))
        engine._on_message(("recorded", sid, rid, "stopped", "/rec/a.ts", 1000))
        self.assertEqual(engine.writing, frozenset())
        self.assertEqual(before, frozenset({"/rec/a.ts"}))  # what a reader had stays whole
        rid = engine.start_recording(sid, "/rec/b.ts", 0, max_s=60.0, min_free=0)
        engine._lose_recordings()  # the helper died
        self.assertEqual(engine.writing, frozenset())

    def test_parked_and_resumed_keep_the_view(self):
        engine, _ = self.engine()
        engine._sessions["s"] = engine_module._View()
        engine._sessions["s"].spans.append(Span(0, 0.0, 2.0, False))
        parked, resumed = [], []
        engine.sessionParked.connect(parked.append)
        engine.sessionResumed.connect(resumed.append)
        engine._on_message(("parked", "s"))
        engine._on_message(("resumed", "s"))
        self.assertEqual((parked, resumed), (["s"], ["s"]))
        self.assertEqual(len(engine.spans("s")), 1)

    def test_a_parked_session_is_not_reopened_after_a_helper_death(self):
        import queue

        commands: queue.Queue = queue.Queue()
        supervisor = engine_module._Supervisor(Path(tempfile.gettempdir()), commands)
        ring = {"kind": "ram", "max_bytes": 64 * MIB}
        command = ("open", "s1", URL_FOR_RING, {}, True, ring, 6.0)
        supervisor._records["s1"] = engine_module._Record(command=command, sent=True)
        sent, relayed = [], []
        supervisor._relay = relayed.append
        commands.put(("park", "s1"))
        supervisor._send_next(SimpleNamespace(send=sent.append))
        self.assertEqual(sent, [("park", "s1")])
        supervisor._receive(("parked", "s1"))
        self.assertTrue(supervisor._records["s1"].over)
        self.assertIn(("parked", "s1"), relayed)

    def test_a_resume_reopens_with_its_own_source_after_a_helper_death(self):
        import queue

        commands: queue.Queue = queue.Queue()
        supervisor = engine_module._Supervisor(Path(tempfile.gettempdir()), commands)
        ring = {"kind": "ram", "max_bytes": 64 * MIB}
        command = ("open", "s1", URL_FOR_RING, {}, True, ring, 6.0)
        record = supervisor._records["s1"] = engine_module._Record(command, sent=True, over=True)
        sent, relayed = [], []
        supervisor._relay = relayed.append
        start = {"target": 3.0, "extra": 0.0}
        commands.put(("resume", "s1", "http://other.test/1.ts", {"A": "b"}, False, 4.0, start))
        supervisor._send_next(SimpleNamespace(send=sent.append))
        self.assertEqual(sent[0][0], "resume")
        self.assertFalse(record.over)
        self.assertEqual(
            record.command,
            ("open", "s1", "http://other.test/1.ts", {"A": "b"}, False, ring, 4.0, start),
        )

    def test_a_resumed_buffers_disk_failure_keeps_its_folder_for_a_new_helper(self):
        # 2.8 (review of 2.6): the helper parks a buffer with kept history with its disk ring
        # as it stands; rewritten to memory, a helper's death left it orphaned ("unkept").
        import queue

        temporary = tempfile.TemporaryDirectory(prefix="qitv-engine-test-")
        self.addCleanup(temporary.cleanup)
        commands: queue.Queue = queue.Queue()
        supervisor = engine_module._Supervisor(Path(temporary.name), commands)
        ring = {"kind": "disk", "dir": engine_module._session_dir(), "max_bytes": 64 * MIB}
        (Path(temporary.name) / ring["dir"]).mkdir()
        command = ("open", "s1", URL_FOR_RING, {}, True, ring, 6.0)
        record = engine_module._Record(command, sent=True, over=True, parked=True)
        supervisor._records["s1"] = record
        supervisor._relay = lambda message: None
        commands.put(("resume", "s1", URL_FOR_RING, {}, True, 6.0))
        supervisor._send_next(SimpleNamespace(send=lambda command: None))
        supervisor._receive(("error", "s1", "disk", "The disk buffer failed."))
        supervisor._receive(("parked", "s1"))
        self.assertEqual(record.command[5], ring)
        record.sent = False  # the helper died
        sent = []
        supervisor._send_open(SimpleNamespace(send=sent.append), record)
        self.assertEqual(sent, [("adopt", "s1", ring["dir"], 64 * MIB)])

    def test_a_fresh_sessions_disk_failure_goes_on_in_memory(self):
        import queue

        supervisor = engine_module._Supervisor(Path(tempfile.gettempdir()), queue.Queue())
        ring = {"kind": "disk", "dir": engine_module._session_dir(), "max_bytes": 64 * MIB}
        command = ("open", "s1", URL_FOR_RING, {}, True, ring, 6.0)
        record = supervisor._records["s1"] = engine_module._Record(command, sent=True)
        supervisor._relay = lambda message: None
        supervisor._receive(("error", "s1", "disk", "The disk buffer failed."))
        self.assertEqual(record.command[5]["kind"], "ram")

    def test_a_new_disk_amount_reaches_the_helper_and_a_reopen(self):
        # 2.8: it applies at once, not only at the next disk open.
        engine, commands = self.engine()
        engine.set_disk_amount(512 * MIB)
        self.assertEqual(commands.get_nowait(), ("disk_amount", 512 * MIB))
        supervisor = engine_module._Supervisor(Path(tempfile.gettempdir()), commands)
        disk = {"kind": "disk", "dir": engine_module._session_dir(), "max_bytes": 1024 * MIB}
        ram = {"kind": "ram", "max_bytes": 64 * MIB}
        for sid, ring in (("d", disk), ("r", ram)):
            command = ("open", sid, URL_FOR_RING, {}, True, ring, 6.0)
            supervisor._records[sid] = engine_module._Record(command, sent=True)
        sent = []
        commands.put(("disk_amount", 512 * MIB))
        supervisor._send_next(SimpleNamespace(send=sent.append))
        self.assertEqual(sent, [])  # no helper is up: its next disk open brings it
        supervisor._up = True
        commands.put(("disk_amount", 512 * MIB))
        supervisor._send_next(SimpleNamespace(send=sent.append))
        self.assertEqual(sent, [("disk_amount", 512 * MIB)])
        self.assertEqual(supervisor._records["d"].command[5]["max_bytes"], 512 * MIB)
        self.assertEqual(supervisor._records["r"].command[5], ram)

    def test_a_resume_without_a_helper_fails(self):
        import queue

        commands: queue.Queue = queue.Queue()
        supervisor = engine_module._Supervisor(Path(tempfile.gettempdir()), commands)
        ring = {"kind": "ram", "max_bytes": 64 * MIB}
        command = ("open", "s1", URL_FOR_RING, {}, True, ring, 6.0)
        supervisor._records["s1"] = engine_module._Record(command, sent=False, over=True)
        relayed = []
        supervisor._relay = relayed.append
        commands.put(("resume", "s1", URL_FOR_RING, {}, True, 6.0))
        supervisor._send_next(SimpleNamespace(send=relayed.append))
        self.assertEqual(relayed[-1][:3], ("error", "s1", "crashed"))


class KeptFolderTests(unittest.TestCase):
    """2.6: disk buffers go in v2 folders, labelled; a helper's death leaves them for the next."""

    def test_new_disk_buffers_are_v2_folders(self):
        self.assertTrue(engine_module._session_dir().startswith(".qitv-buffer-"))

    def test_keep_and_where_reach_the_helper(self):
        import queue

        commands: queue.Queue = queue.Queue()
        engine = LiveEngine.__new__(LiveEngine)
        LiveEngine.__init__(engine, tempfile.gettempdir())
        engine._commands = commands
        sid = engine.open_session(
            URL_FOR_RING, headers={}, verify_ssl=True, disk=True, max_bytes=MIB
        )
        opened = commands.get_nowait()
        engine.keep_session(sid, {"channel": "k1"})
        engine.note_where(sid, {"position": 12.0, "paused": True})
        engine.keep_session("unknown", {"channel": "k2"})
        self.assertEqual(commands.get_nowait(), ("keep", sid, {"channel": "k1"}))
        self.assertEqual(commands.get_nowait(), ("where", sid, {"position": 12.0, "paused": True}))
        self.assertTrue(commands.empty())
        supervisor = engine_module._Supervisor(Path(tempfile.gettempdir()), commands)
        supervisor._records[sid] = engine_module._Record(command=opened, sent=True)
        sent = []
        commands.put(("keep", sid, {"channel": "k1"}))
        supervisor._send_next(SimpleNamespace(send=sent.append))
        self.assertEqual(sent, [("keep", sid, {"channel": "k1"})])

    def test_a_helpers_death_leaves_labelled_folders_only(self):
        import queue

        from services.live.ring import DiskRing

        with tempfile.TemporaryDirectory() as root:
            kept, plain = engine_module._session_dir(), engine_module._session_dir()
            for name in (kept, plain):
                ring = DiskRing(Path(root), name, MIB)
                if name == kept:
                    ring.note_label({"channel": "k1"})
                ring.close(keep=True)  # as a helper that died left it
            supervisor = engine_module._Supervisor(Path(root), queue.Queue())
            supervisor._dirs = {kept, plain}
            supervisor._clean_dirs()
            self.assertTrue((Path(root) / kept).exists())
            self.assertFalse((Path(root) / plain).exists())  # nobody could take it back
            self.assertEqual(supervisor._dirs, set())


class AdoptKeptTests(unittest.TestCase):
    """2.6: at the start the supervisor takes kept folders back, and sweeps what cannot be."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="qitv-kept-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def folder(self, *, label=True, age=0.0):
        from services.live import ring as ring_module
        from services.live.ring import DiskRing

        name = engine_module._session_dir()
        with patch.object(ring_module, "_wall_time", lambda: time.time() - age):
            ring = DiskRing(self.root, name, MIB)
            ring.open_segment(0, b"h" * 376)
            ring.append(b"d" * 1000)
            ring.complete(0)
            ring.note_segment(0, 0.0, 2.0, False, "key")
        if label:
            ring.note_label({"channel": "k1"})
        ring.close(keep=True)
        return name

    def supervisor(self):
        import queue

        supervisor = engine_module._Supervisor(self.root, queue.Queue())
        relayed = []
        supervisor._relay = relayed.append
        return supervisor, relayed

    def test_fresh_labelled_folders_are_taken_back_the_rest_swept(self):
        from services.live.ring import STALE_S

        kept = self.folder()
        stale = self.folder(age=STALE_S + 60)
        plain = self.folder(label=False)
        supervisor, relayed = self.supervisor()
        supervisor._adopt_kept(64 * MIB)
        adopting = [m for m in relayed if m[0] == "adopting"]
        self.assertEqual(len(adopting), 1)
        record = supervisor._records[adopting[0][1]]
        self.assertTrue(record.parked)
        self.assertEqual(record.command[5]["dir"], kept)
        self.assertTrue((self.root / kept).exists())
        self.assertFalse((self.root / stale).exists())
        self.assertFalse((self.root / plain).exists())
        sent = []
        supervisor._send_open(SimpleNamespace(send=sent.append), record)
        self.assertEqual(sent, [("adopt", adopting[0][1], kept, 64 * MIB)])

    def test_a_folder_another_qitv_holds_is_left_alone(self):
        from test_ring import hold_session_lock

        kept = self.folder()
        context = multiprocessing.get_context("spawn")
        ready, release = context.Event(), context.Event()
        holder = context.Process(
            target=hold_session_lock, args=(str(self.root / kept / "session.lock"), ready, release)
        )
        holder.start()
        self.addCleanup(holder.join, 10)
        self.addCleanup(release.set)
        self.assertTrue(ready.wait(20))
        supervisor, relayed = self.supervisor()
        supervisor._adopt_kept(64 * MIB)
        self.assertEqual(relayed, [])
        self.assertTrue((self.root / kept / "segment-0.ts").exists())

    def test_the_scan_never_takes_a_folder_another_qitv_is_making(self):
        # 2.8 (review of 2.6): its maker holds the manager lock until the folder is locked
        # and indexed; the scan holds it too, so it never sweeps a folder half made.
        from test_ring import hold_session_lock

        name = engine_module._session_dir()
        (self.root / name).mkdir()
        (self.root / name / "owner").write_text("QiTV time-shift buffer v2\n", encoding="ascii")
        context = multiprocessing.get_context("spawn")
        ready, release = context.Event(), context.Event()
        manager = str(self.root / ".qitv-timeshift-manager.lock")
        holder = context.Process(target=hold_session_lock, args=(manager, ready, release))
        holder.start()
        self.addCleanup(holder.join, 10)
        self.addCleanup(release.set)
        self.assertTrue(ready.wait(20))
        supervisor, relayed = self.supervisor()
        with patch.object(engine_module, "_MANAGER_WAIT_S", 0.2, create=True):
            supervisor._adopt_kept(64 * MIB)
            supervisor._drop_kept()
        self.assertTrue((self.root / name / "owner").exists())
        self.assertEqual(relayed, [])
        release.set()
        holder.join(10)
        supervisor._drop_kept()  # once it is free, as before
        self.assertFalse((self.root / name).exists())

    def test_kept_buffers_after_time_shift_went_off(self):
        # 2.8 (review of 2.6): the supervisor without a helper failed on them; the disk
        # buffer turned off still sweeps what no helper holds (it needs none).
        kept = self.folder()
        supervisor, relayed = self.supervisor()
        supervisor._dead = True
        for command in (("adopt_kept", 64 * MIB), ("drop_kept",), ("disk_amount", MIB)):
            supervisor._commands.put(command)
            supervisor._serve_without_helper()
        self.assertEqual(relayed, [])
        self.assertFalse((self.root / kept).exists())

    def test_disk_buffer_off_sweeps_every_kept_folder(self):
        kept = self.folder()
        supervisor, _ = self.supervisor()
        supervisor._drop_kept()
        self.assertFalse((self.root / kept).exists())

    def test_a_parked_session_is_taken_back_after_a_helper_death(self):
        supervisor, _ = self.supervisor()
        ring = {"kind": "disk", "dir": engine_module._session_dir(), "max_bytes": 64 * MIB}
        (self.root / ring["dir"]).mkdir()  # left for the next helper
        command = ("open", "s1", URL_FOR_RING, {}, True, ring, 6.0)
        record = supervisor._records["s1"] = engine_module._Record(command, sent=True)
        supervisor._receive(("parked", "s1"))
        self.assertTrue(record.parked)
        record.sent = False  # the helper died
        sent = []
        supervisor._send_open(SimpleNamespace(send=sent.append), record)
        self.assertEqual(sent, [("adopt", "s1", ring["dir"], 64 * MIB)])

    def test_the_engine_tells_what_was_kept_at_once(self):
        # Review B I2: from the scan, before the helper took it back, so a startup tune of
        # that channel finds it.
        engine = LiveEngine.__new__(LiveEngine)
        LiveEngine.__init__(engine, tempfile.gettempdir())
        found = []
        engine.keptFound.connect(lambda sid, label, where: found.append((sid, label, where)))
        label, where = {"channel": "k1"}, {"position": 1.0, "paused": True}
        engine._on_message(("adopting", "z", label, where))
        self.assertEqual(found, [("z", label, where)])
        self.assertTrue(engine.holds("z"))
        engine._on_message(("state", "z", {"oldest": 0}, 0, [(0, 0.0, 2.0, False, "key")]))
        engine._on_message(("parked", "z"))
        engine._on_message(("kept", "z", label, where))
        self.assertEqual(len(found), 1)  # told once
        self.assertEqual(len(engine.spans("z")), 1)
        engine._on_message(("unkept", "z"))
        self.assertEqual(engine.spans("z"), [])
        self.assertFalse(engine.holds("z"))

    def test_a_buffer_that_cannot_be_taken_back_is_told_lost(self):
        # 2.8 (review of 2.6): the kept buffers' registry lets it go ("unkept").
        engine = LiveEngine.__new__(LiveEngine)
        LiveEngine.__init__(engine, tempfile.gettempdir())
        lost, failed = [], []
        engine.keptLost.connect(lost.append)
        engine.sessionFailed.connect(lambda sid, kind, message: failed.append((sid, kind)))
        engine._on_message(("adopting", "z", {"channel": "k1"}, {}))
        engine._on_message(("unkept", "z"))
        self.assertEqual(lost, ["z"])
        self.assertEqual(failed, [("z", "crashed")])

    def test_keep_and_where_before_the_helper_is_up_follow_its_open(self):
        # Review B I1: a startup tune opens before the helper's hello.
        supervisor, _ = self.supervisor()
        ring = {"kind": "disk", "dir": engine_module._session_dir(), "max_bytes": MIB}
        opened = ("open", "s1", URL_FOR_RING, {}, True, ring, 6.0)
        conn = SimpleNamespace(send=lambda command: None)
        for command in (
            opened,
            ("keep", "s1", {"channel": "k1"}),
            ("where", "s1", {"position": 1.0, "paused": False}),
        ):
            supervisor._commands.put(command)
            supervisor._send_next(conn)
        sent = []
        supervisor._up = True
        supervisor._send_open(SimpleNamespace(send=sent.append), supervisor._records["s1"])
        self.assertEqual([c[0] for c in sent], ["open", "keep", "where"])

    def test_a_resume_before_the_take_back_follows_it(self):
        # Review B I2: the startup tune takes the kept buffer back before the helper is up.
        kept = self.folder()
        supervisor, relayed = self.supervisor()
        supervisor._adopt_kept(64 * MIB)
        [(_, sid, label, where)] = [m for m in relayed if m[0] == "adopting"]
        self.assertEqual(label, {"channel": "k1"})
        supervisor._commands.put(("resume", sid, URL_FOR_RING, {}, True, 6.0))
        supervisor._send_next(SimpleNamespace(send=lambda command: None))
        self.assertNotIn(("error", sid, "crashed", engine_module._INTERNAL), relayed)
        sent = []
        supervisor._up = True
        supervisor._send_open(SimpleNamespace(send=sent.append), supervisor._records[sid])
        self.assertEqual([c[0] for c in sent], ["adopt", "resume"])
        self.assertEqual(sent[0][2], kept)

    def test_of_two_folders_of_one_channel_the_newest_is_taken_back(self):
        # Review B I3
        older = self.folder(age=600.0)
        newer = self.folder()
        supervisor, relayed = self.supervisor()
        supervisor._adopt_kept(64 * MIB)
        [(_, sid, _, _)] = [m for m in relayed if m[0] == "adopting"]
        self.assertEqual(supervisor._records[sid].command[5]["dir"], newer)
        self.assertFalse((self.root / older).exists())

    def test_a_helpers_death_keeps_parked_folders_only(self):
        # Review B I3: a live session reopens in a fresh folder; its old one would be an orphan.
        live, parked = self.folder(), self.folder()
        supervisor, _ = self.supervisor()
        for sid, name, is_parked in (("a", live, False), ("b", parked, True)):
            ring = {"kind": "disk", "dir": name, "max_bytes": MIB}
            command = ("open", sid, URL_FOR_RING, {}, True, ring, 6.0)
            supervisor._records[sid] = engine_module._Record(command, parked=is_parked)
            supervisor._dirs.add(name)
        supervisor._clean_dirs(died=True)
        self.assertFalse((self.root / live).exists())
        self.assertTrue((self.root / parked).exists())


class HelperArgsTests(unittest.TestCase):  # 2.8.2: the detailed log reaches the helper
    def test_the_helper_starts_with_the_logs_detail(self):
        from services import log_file
        from services.live import engine as engine_module

        self.addCleanup(log_file.set_detailed, False)
        self.assertEqual(engine_module._helper_args("c", Path("/r")), ("c", "/r", False))
        log_file.set_detailed(True)
        self.assertEqual(engine_module._helper_args("c", Path("/r")), ("c", "/r", True))


class FailureReasonTests(unittest.TestCase):
    """2.1: why a channel plays directly is logged, by kind and HTTP status only."""

    def test_an_http_failure_is_relayed_and_logged_with_its_status(self):
        import queue

        from services.live import engine as engine_module

        supervisor = engine_module._Supervisor(Path(tempfile.gettempdir()), queue.Queue())
        relayed = []
        supervisor._relay = relayed.append
        command = ("open", "s1", URL_FOR_RING, {}, True, {"kind": "ram", "max_bytes": MIB}, 6.0)
        supervisor._records["s1"] = engine_module._Record(command=command, sent=True)
        failure = ("error", "s1", "http", "The channel could not be reached over HTTP.", 503)
        with self.assertLogs("services.live.engine", "INFO") as logs:
            supervisor._receive(failure)
        self.assertEqual(relayed, [failure])
        self.assertTrue(supervisor._records["s1"].failed)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("http", logs.output[0])
        self.assertIn("503", logs.output[0])
        self.assertNotIn("provider.test", logs.output[0])

    def test_a_failure_without_a_status_is_logged_by_its_kind(self):
        import queue

        from services.live import engine as engine_module

        supervisor = engine_module._Supervisor(Path(tempfile.gettempdir()), queue.Queue())
        relayed = []
        supervisor._relay = relayed.append
        command = ("open", "s1", URL_FOR_RING, {}, True, {"kind": "ram", "max_bytes": MIB}, 6.0)
        supervisor._records["s1"] = engine_module._Record(command=command, sent=True)
        failure = ("error", "s1", "network", "The channel sent no data.")
        with self.assertLogs("services.live.engine", "INFO") as logs:
            supervisor._receive(failure)
        self.assertEqual(relayed, [failure])
        self.assertIn("network", logs.output[0])

    def test_a_helper_log_line_is_logged_by_the_app(self):  # 2.8.1
        import logging
        import queue

        from services.live import engine as engine_module

        supervisor = engine_module._Supervisor(Path(tempfile.gettempdir()), queue.Queue())
        line = "The channel did not open over HTTPS (tls); opening it over HTTP."
        with self.assertLogs("services.live.helper", "INFO") as logs:
            supervisor._receive(("log", logging.INFO, "services.live.source", line))
            supervisor._receive(("log", logging.INFO, "urllib3", "http://secret.test/"))
            supervisor._receive(("log", "INFO", "services.live.source", line))  # malformed
        self.assertEqual(logs.output, [f"INFO:services.live.helper.source:{line}"])

    def test_a_failure_says_how_it_failed(self):  # 2.8.1: refused, unanswered, broken off
        import queue

        from services.live import engine as engine_module

        supervisor = engine_module._Supervisor(Path(tempfile.gettempdir()), queue.Queue())
        supervisor._relay = lambda message: None
        command = ("open", "s1", URL_FOR_RING, {}, True, {"kind": "ram", "max_bytes": MIB}, 6.0)
        supervisor._records["s1"] = engine_module._Record(command=command, sent=True)
        failure = ("error", "s1", "network", "The channel's server refused the connection.")
        with self.assertLogs("services.live.engine", "INFO") as logs:
            supervisor._receive(failure)
        self.assertEqual(
            logs.output,
            [
                "INFO:services.live.engine:A live session's input failed for good: network "
                "(The channel's server refused the connection.)."
            ],
        )


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


def silent_helper(conn, root, detailed_log=False):
    """A helper that never says hello."""
    time.sleep(60)


def crashing_session_helper(conn, root, detailed_log=False):
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


def stubborn_helper(conn, root, detailed_log=False):
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
        engine.sessionParked.connect(lambda sid: self._add("parked", sid), direct)  # 2.6

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


class SpanMessageTests(unittest.TestCase):
    """In process, without a helper: the spans the state messages rebuild."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_spans_carry_the_start_quality_and_additions_without_one_start_on_a_key(self):
        engine = LiveEngine(tempfile.gettempdir())  # never started: no helper, no buffer
        self.addCleanup(engine.shutdown)
        sid = engine.open_session(
            URL_FOR_RING, headers={}, verify_ssl=True, disk=False, max_bytes=MIB
        )
        additions = [
            (0, 0.0, 2.0, False, START_OPEN),
            (1, 2.0, 12.0, False, START_NONE),
            (2, 12.0, 14.0, True),  # as an older helper sent it
        ]
        engine._on_message(("state", sid, {"oldest": 0}, 0, additions))
        engine._on_message(("state", sid, {"oldest": 1}, 1, [(3, 14.0, 16.0, False, START_KEY)]))
        self.assertEqual(
            engine.spans(sid),
            [
                Span(1, 2.0, 12.0, False, START_NONE),
                Span(2, 12.0, 14.0, True, START_KEY),
                Span(3, 14.0, 16.0, False, START_KEY),
            ],
        )

    def test_an_addition_with_a_media_sequence_becomes_a_span(self):
        # 2.7: a sixth field, the HLS media sequence the segment begins; five mean None.
        engine = LiveEngine(tempfile.gettempdir())  # never started: no helper, no buffer
        self.addCleanup(engine.shutdown)
        sid = engine.open_session(
            URL_FOR_RING, headers={}, verify_ssl=True, disk=False, max_bytes=MIB
        )
        additions = [(0, 0.0, 2.0, False, START_KEY, 7), (1, 2.0, 4.0, False, START_KEY)]
        engine._on_message(("state", sid, {"oldest": 0}, 0, additions))
        self.assertEqual([span.media_seq for span in engine.spans(sid)], [7, None])


class LiveEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        collect_on_the_gui_thread(cls)  # the supervisor thread never runs the collector

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

    def test_where_and_park_reach_the_helper_before_it_goes(self):
        # Review B C1: the app notes where the user was and parks at quit, then shuts the
        # engine down in the same turn: those must reach the helper first.
        engine, signals = self.engine()
        signals.wait_for(lambda event: event == ("available", True), timeout=60)
        source = self.source()
        sid = self.open(engine, source.url + SECRET_PATH, disk=True)
        engine.keep_session(sid, {"channel": "k1"})
        signals.wait_for(lambda e: e[0] == "updated" and e[1] == sid and len(e[3]) >= 2)
        engine.note_where(sid, {"position": 2.0, "paused": True})
        engine.park_session(sid)
        engine.shutdown()
        [name] = session_dirs(self.root)
        index = read_index(self.root / name)
        self.assertEqual(index.where, {"position": 2.0, "paused": True})
        self.assertEqual(index.label, {"channel": "k1"})

    def test_the_helpers_own_lines_reach_the_apps_log(self):  # 2.8.1, end to end
        engine, signals = self.engine()
        signals.wait_for(lambda event: event == ("available", True), timeout=60)
        source = self.source()
        with self.assertLogs("services.live.helper", "INFO") as logs:
            sid = engine.open_session(
                source.url + SECRET_PATH,
                headers={},
                verify_ssl=True,
                disk=False,
                max_bytes=MIB,
                https_first=True,  # a plain server: the helper says HTTPS did not open
            )
            signals.wait_for(lambda event: event == ("ready", sid), timeout=60)
        self.assertTrue(any("did not open over HTTPS" in line for line in logs.output), logs)
        self.assertFalse(any(SECRET_PATH in line for line in logs.output))

    def test_a_session_opened_before_the_helper_is_up_is_labelled(self):
        # Review B I1, end to end: the startup tune opens before the helper's hello.
        engine, signals = self.engine()
        source = self.source()
        sid = self.open(engine, source.url + SECRET_PATH, disk=True)
        engine.keep_session(sid, {"channel": "k1"})
        self.assertFalse(engine.is_available())  # still starting
        signals.wait_for(lambda event: event == ("ready", sid), timeout=60)
        [name] = session_dirs(self.root)
        self.assertTrue(wait_until(lambda: (read_index(self.root / name) or None) is not None, 5))
        self.assertTrue(
            wait_until(lambda: read_index(self.root / name).label == {"channel": "k1"}, 5)
        )

    def test_a_kept_buffer_survives_a_restart(self):
        # 2.6, end to end through two real helpers: one keeps a labelled disk buffer as the
        # engine shuts down, the next engine takes it back with its spans and where it was.
        engine, signals = self.engine()
        signals.wait_for(lambda event: event == ("available", True), timeout=60)
        source = self.source({SECRET_PATH: (200, {}, make_stream(8))})
        sid = self.open(engine, source.url + SECRET_PATH, disk=True)
        engine.keep_session(sid, {"channel": "k1"})
        signals.wait_for(lambda event: event == ("ended", sid))
        spans = [(s.seq, s.start, s.end) for s in engine.spans(sid)]
        engine.note_where(sid, {"position": 2.0, "paused": True})
        engine.park_session(sid)
        signals.wait_for(lambda event: event == ("parked", sid))
        engine.shutdown()
        self.assertEqual(len(session_dirs(self.root)), 1)  # kept for the next start

        second, signals = self.engine()
        found = []
        second.keptFound.connect(lambda *args: found.append(args))
        second.adopt_kept(256 * MIB)
        self.assertTrue(wait_until(lambda: found, 30), "nothing was taken back")
        [(kept, label, where)] = found
        self.assertEqual((label, where), ({"channel": "k1"}, {"position": 2.0, "paused": True}))
        # Told at once, from the scan; its spans follow once the helper took it back.
        self.assertTrue(wait_until(lambda: second.spans(kept), 30), "its spans never came")
        self.assertEqual([(s.seq, s.start, s.end) for s in second.spans(kept)], spans)
        second.close_session(kept)
        self.assertTrue(wait_until(lambda: session_dirs(self.root) == [], 10))

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
        self.assertEqual({span.start_quality for span in spans}, {START_KEY})  # IDR starts
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
                self.assertEqual(still_running([helper_pid], 3), [], "the orphan kept running")
            finally:
                if owner.poll() is None:
                    owner.kill()
                    owner.wait(5)
                terminate(still_running([helper_pid], 0))
                owner.stdout.close()

    def test_an_owner_leaving_with_os_exit_leaves_no_helper_or_buffer(self):
        # TVWindow's _exit_now ends the GUI with os._exit when its close gives up waiting.
        source = self.source()
        owner = subprocess.Popen(
            [sys.executable, "-c", OWNER_EXIT, source.url + SECRET_PATH, str(self.root)],
            cwd=REPO,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        helper_pid = None
        try:
            line = owner.stdout.readline().strip()
            self.assertTrue(line, "the owner's session never became ready")
            helper_pid = int(line)
            self.assertEqual(owner.wait(10), 3)
            self.assertEqual(still_running([helper_pid], 3), [], "the helper outlived its owner")
            self.assertTrue(self.hung_up.wait(2), "the helper kept its upstream connection")
            self.assertEqual(session_dirs(self.root), [], "the disk buffer was left behind")
        finally:
            if owner.poll() is None:
                owner.kill()
                owner.wait(5)
            terminate(still_running([helper_pid], 0))
            owner.stdout.close()


if __name__ == "__main__":
    unittest.main()
