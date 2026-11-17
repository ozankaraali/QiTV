"""One QiTV per settings folder (2.8.12): a second start hands over to the first and quits. A
first that can't be reached (a socket left by a crash) is not waited for; one reached while it
still starts is, up to START_WAIT_MS, and one that never answers is taken over after that. A
start with files hands them over too; a first of 2.10, which only knows `show`, still comes
forward. The first QiTV's stand-in (FIRST) is in instance_fixtures."""

import json
import os
from pathlib import Path
import socket as pysocket
import subprocess
import sys
import tempfile
import time
import unicodedata
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, Qt, QTimer
from PySide6.QtNetwork import QLocalServer
from PySide6.QtWidgets import QApplication
from instance_fixtures import FIRST

from services import single_instance
from services.single_instance import InstanceServer, claim, hand_over, server_name

REPO = Path(__file__).resolve().parents[1]
app = QApplication.instance() or QApplication([])

# A second start, in a process of its own: hand_over(name, paths=<JSON list>), then one JSON
# line with what it returned, how long the call took and what it logged.
ASK = r"""
import json, logging, sys, time
from services.single_instance import hand_over
lines = []
class Keep(logging.Handler):
    def emit(self, record):
        lines.append(record.getMessage())
log = logging.getLogger("services.single_instance")
log.addHandler(Keep())
log.setLevel(logging.DEBUG)
started = time.monotonic()
ok = hand_over(sys.argv[1], paths=json.loads(sys.argv[2]))
ms = (time.monotonic() - started) * 1000
print(json.dumps({"ok": ok, "ms": ms, "log": lines}), flush=True)
"""

# A start that doesn't follow the protocol: "show", then a line over MAX_LINE and "done"
# ("long"), or nothing more ("quiet"); then one JSON line with what came back.
RAW = r"""
import json, sys, time
from PySide6.QtNetwork import QLocalSocket
from services.single_instance import DONE, MAX_LINE, SHOW
socket = QLocalSocket()
socket.connectToServer(sys.argv[1])
socket.waitForConnected(3000)
def read(seconds):
    data, end = b"", time.monotonic() + seconds
    while b"\n" not in data:
        left = int((end - time.monotonic()) * 1000)
        if left <= 0 or not socket.waitForReadyRead(left):
            break
        data += bytes(socket.readAll().data())
    if socket.bytesAvailable():
        data += bytes(socket.readAll().data())
    return data
socket.write(SHOW)
socket.flush()
shown = read(3)
started = time.monotonic()
if sys.argv[2] == "long":
    line = b"open file:///"
    line += b"a" * (MAX_LINE + 10 - len(line))
    socket.write(line + b"\n" + DONE)  # it may fail half-way: the first hung up
    socket.flush()
    end = time.monotonic() + 3
    while socket.bytesToWrite() > 0:
        left = int((end - time.monotonic()) * 1000)
        if left <= 0 or not socket.waitForBytesWritten(left):
            break
answer = read(5)
closed = socket.state() != QLocalSocket.LocalSocketState.ConnectedState
ms = (time.monotonic() - started) * 1000
print(json.dumps({"shown": shown.decode(), "answer": answer.decode(errors="replace"),
                  "closed": closed, "ms": ms}), flush=True)
"""

# A start of a multi-select, in a process of its own: it says "ready" once Qt is imported,
# reads the shared moment (wall-clock seconds) from its stdin, and at that instant claims the
# settings folder (argv[1]) with its file (argv[2]). One that listens serves until argv[3] files
# were handed to it (RACE_BOUND_S at most), as a starting QiTV's event loop would. Then one JSON
# line: whether it handed over (and how long its claim took), whether it listened, and the
# files handed to it.
RACE = r"""
import json, sys, time
from PySide6.QtCore import QCoreApplication, QTimer
from services.single_instance import claim
app = QCoreApplication([])
folder, path, expect, bound = sys.argv[1], sys.argv[2], int(sys.argv[3]), float(sys.argv[4])
print("ready", flush=True)
moment = float(sys.stdin.readline())
time.sleep(max(0.0, moment - time.time() - 0.02))
while time.time() < moment:
    pass  # the last few milliseconds spun: every start claims at the same instant
began = time.monotonic()
server = claim(folder, paths=[path])
claim_s = time.monotonic() - began
if server is None:
    print(json.dumps({"handed_over": True, "claim_s": claim_s}), flush=True)
    sys.exit(0)
opened = []
def on_opened(paths):
    opened.extend(paths)
    if len(opened) >= expect:
        QTimer.singleShot(100, app.quit)
server.opened.connect(on_opened)
listening = server.listening()
QTimer.singleShot(0 if expect <= 0 else int(bound * 1000), app.quit)
app.exec()
server.close()
print(json.dumps({"handed_over": False, "listening": listening, "opened": opened}), flush=True)
"""
LATE_S = 22.0  # the late holder answers this long after the starts claimed (> START_WAIT_MS)
RACE_STARTS = 6
RACE_BOUND_S = 60.0  # a listening start ends sooner once every other start's file came

# A QiTV of this version holding the settings folder's lock (argv[1]): it listens (but in
# "unlistened") and says "ready", then stays without an event loop for argv[3] seconds, a cold
# start under load, or until its stdin ends. "slow" waits instead until its stdin says "serve",
# then serves, printing `opened <path as JSON>` for each file handed to it, until its stdin
# ends (8 s at most). Each quits as QiTV does, letting go of its lock itself ("quits" and
# "unlistened" without ever answering): Windows lets go of a killed process's lock only when
# it gets round to it, so the tests end a holder through its stdin, never by a kill.
# "closes" closes its server (a start connected to it is hung up on), holds the lock 2 s more,
# prints `released <wall-clock time>` as it lets go, and lives on 3 s; "crashes" ends without
# letting go (os._exit: the system lets go).
HOLDER = r"""
import json, os, sys, threading, time
from PySide6.QtCore import QCoreApplication, QTimer
from services.single_instance import InstanceServer, _InstanceLock, _Lock, server_name
app = QCoreApplication([])
folder, mode, delay = sys.argv[1], sys.argv[2], float(sys.argv[3])
lock = _InstanceLock(folder)
assert lock.acquire() is _Lock.OURS
server = InstanceServer(server_name(folder), lock=lock)
if mode != "unlistened":
    assert server.listen()
done = threading.Event()  # the test is done with it: its stdin ended
serve = threading.Event()
def read():
    for line in sys.stdin:
        if line.strip() == "serve":
            serve.set()
    done.set()
    serve.set()
threading.Thread(target=read, daemon=True).start()
print("ready", flush=True)
if mode == "slow":
    serve.wait(delay)
else:
    done.wait(delay)
if mode == "crashes":
    os._exit(0)
if mode == "closes":
    server._server.close()  # the server only: the lock stays held
    done.wait(2)
    released_at = time.time()  # taken before it lets go: no start can listen sooner
    lock.release()
    print("released " + repr(released_at), flush=True)
    done.wait(3)
if mode == "slow" and not done.is_set():
    def on_opened(paths):
        for path in paths:
            print("opened " + json.dumps(path), flush=True)
    server.opened.connect(on_opened)
    watch = QTimer()
    watch.timeout.connect(lambda: done.is_set() and app.quit())
    watch.start(100)
    QTimer.singleShot(8000, app.quit)
    app.exec()
server.close()  # the server, then its lock
sys.exit(0)
"""

# What a start logs when the first didn't take its files.
NOT_TAKEN = ("older", "didn't say")

# Windows' extended-length and device spellings, as ntpath writes them: they go raw on every
# system (Ruling 29: QUrl dropped \\?\UNC\'s prefix on Windows).
LONG_DIR = "\\".join(["Sezon 1 Bölümler uzun klasör adı"] * 9)
EXTENDED = [
    "\\\\?\\C:\\" + LONG_DIR + "\\Dizi Bölüm 1.mkv",  # over 260 characters
    "\\\\?\\UNC\\SERVER\\Share\\Dizi Bölüm 1.mkv",
    "\\\\.\\C:\\Videos\\a%20b #1 ;x=y&[z]{w}~'.mkv",
    "\\\\?\\C:\\Videos\\lone \ud800 surrogate.mkv",  # a name Windows allows
]


class NameTests(unittest.TestCase):
    def test_one_folder_one_name_however_it_is_spelled(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(server_name(folder), server_name(folder + os.sep))
            self.assertEqual(server_name(folder), server_name(os.path.join(folder, ".")))
            other = os.path.join(folder, "other")
            self.assertNotEqual(server_name(folder), server_name(other))
            self.assertLess(len(server_name(folder)), 40)  # a Unix socket path is short

    def test_the_settings_folder_is_found_without_touching_it(self):
        import main

        with tempfile.TemporaryDirectory() as root:
            folder = os.path.join(root, "profile")
            with mock.patch.dict(os.environ, {"QITV_CONFIG_DIR": folder}):
                self.assertEqual(main.settings_dir(), folder)
            self.assertFalse(os.path.exists(folder))

    def test_an_updates_copy_waits_on_the_old_qitv_only_while_it_runs(self):
        # Ruling 32 for an update: once its old file was replaced, the old QiTV has quit, and
        # the copy claims as any start does (it hands over to a start that took the lock in
        # the meantime); only when the old one never let go of its file does it wait on it.
        import main

        replaced, not_replaced = None, ("C:\\QiTV\\qitv.exe", "C:\\Downloads\\qitv.exe")
        update = ["qitv.exe", "--replace", "C:\\QiTV\\qitv.exe"]
        self.assertFalse(main.old_qitv_still_runs(update, replaced))
        self.assertTrue(main.old_qitv_still_runs(update, not_replaced))
        self.assertFalse(main.old_qitv_still_runs(["qitv.exe", "film.mkv"], None))

    def test_main_claims_only_in_main(self):
        source = (REPO / "main.py").read_text(encoding="utf-8")
        claim_at = source.index("instance = claim(")
        self.assertGreater(claim_at, source.index("def main()"))
        self.assertLess(claim_at, source.index("ConfigManager()"))  # before any config write
        self.assertIn("replacing=old_qitv_still_runs(sys.argv, not_replaced)", source)
        # The smoke test and the helper processes branch off before main().
        self.assertLess(source.index("smoke_main(smoke)"), source.rindex("sys.exit(main())"))


class ClaimTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = folder.name
        self.name = server_name(self.folder)

    def listening(self, server):
        self.assertIsNotNone(server)
        self.addCleanup(self.dispose, server)
        self.assertTrue(server.listening())

    @staticmethod
    def dispose(server):
        # Closed and deleted by Qt here, not by the runner's garbage collection after the
        # test (a socket still connected then was deleted outright: a crash on CI's Macs).
        server.close()
        server.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def first(self, mode="answer"):
        script = Path(self.folder) / "first.py"
        script.write_text(FIRST, encoding="utf-8")
        env = dict(os.environ, PYTHONPATH=str(REPO), QT_QPA_PLATFORM="offscreen")
        process = subprocess.Popen(
            [sys.executable, str(script), self.name, mode],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=env,
            cwd=str(REPO),
        )
        self.addCleanup(process.stdout.close)
        self.addCleanup(process.wait, 15)
        self.addCleanup(process.kill)
        self.assertEqual(process.stdout.readline().strip(), "ready")
        return process

    def test_the_first_start_listens(self):
        started = time.monotonic()
        self.listening(claim(self.folder))
        self.assertLess(time.monotonic() - started, 1.5)  # nobody to wait for
        self.assertFalse(hand_over(server_name(self.folder + "-other"), 300))

    def test_a_second_start_hands_over_and_quits(self):
        first = self.first()
        self.assertIsNone(claim(self.folder))
        self.assertEqual(first.stdout.readline().strip(), "activated")

    def test_a_first_that_never_answers_is_not_waited_for(self):
        self.first("hang")
        started = time.monotonic()
        with (
            mock.patch.object(single_instance, "ANSWER_MS", 400),
            mock.patch.object(single_instance, "START_WAIT_MS", 800),
        ):
            server = claim(self.folder)
        waited = time.monotonic() - started
        self.assertGreaterEqual(waited, 0.75)  # reached: given START_WAIT_MS to answer
        self.assertLess(waited, 3)  # and no longer
        self.listening(server)  # the name is taken over

    def test_a_first_still_starting_is_waited_for(self):
        # Ruling 8: a multi-select starts one QiTV per file at once, and the first may listen
        # seconds before its event loop runs. A start that reached it waits for its answer
        # instead of taking the name over (two QiTVs on one settings folder).
        first = self.first("slow")
        p1 = self.turkish_path()
        started = time.monotonic()
        with self.assertLogs("services.single_instance", "INFO") as logs:
            self.assertIsNone(claim(self.folder, paths=[p1]))
        self.assertGreater(time.monotonic() - started, single_instance.ANSWER_MS / 1000)
        self.assertEqual(first.stdout.readline().strip(), "opened " + json.dumps(p1))
        self.assertTrue([line for line in logs.output if "it was starting" in line], logs.output)

    @unittest.skipIf(os.name == "nt", "Windows pipes leave nothing behind")
    def test_a_socket_left_by_a_crash_is_taken_over(self):
        probe = QLocalServer()
        self.assertTrue(probe.listen(self.name))
        path = probe.fullServerName()
        probe.close()
        stale = pysocket.socket(pysocket.AF_UNIX, pysocket.SOCK_STREAM)
        stale.bind(path)  # a socket file nobody listens on, as a crash leaves it
        stale.close()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        started = time.monotonic()
        self.listening(claim(self.folder))
        self.assertLess(time.monotonic() - started, 1.5)  # not reached: not waited for

    def race(self, files, expect, meanwhile=None):
        """One start per file, released at one instant once every one has said it is ready
        (however long a loaded machine takes to import Qt); `meanwhile(moment)` runs while they
        claim; what each said."""
        env = dict(os.environ, PYTHONPATH=str(REPO), QT_QPA_PLATFORM="offscreen")
        starts = [
            subprocess.Popen(
                [sys.executable, "-c", RACE, self.folder, path, str(expect), str(RACE_BOUND_S)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                env=env,
                cwd=str(REPO),
            )
            for path in files
        ]
        for start in starts:
            self.addCleanup(self.end, start)
        for start in starts:
            self.assertEqual(start.stdout.readline().strip(), "ready")
        moment = time.time() + 0.3
        for start in starts:
            start.stdin.write(f"{moment}\n")
            start.stdin.flush()
        if meanwhile is not None:
            meanwhile(moment)
        said = []
        for start in starts:
            out, _ = start.communicate(timeout=RACE_BOUND_S + 30)
            said.append(json.loads(out.strip().splitlines()[-1]))
        return said

    def test_starts_at_the_same_moment_leave_one_qitv(self):
        # Ruling 31: a multi-select starts one QiTV per file at the same instant. Exactly one
        # listens; every other start hands its file to it and ends.
        files = [os.path.join(self.folder, f"Bölüm {i}.mkv") for i in range(RACE_STARTS)]
        said = self.race(files, expect=RACE_STARTS - 1)
        stayed = [s for s in said if not s["handed_over"]]
        self.assertEqual(len(stayed), 1, said)
        self.assertTrue(stayed[0]["listening"])
        # The files of the starts that handed over all reached it.
        others = sorted(f for f, s in zip(files, said) if s["handed_over"])
        self.assertEqual(sorted(stayed[0]["opened"]), others)
        self.assertLockFree()  # the one that listened let go of it as it closed

    def test_a_windows_updates_copy_never_hands_over(self):
        with (
            mock.patch.object(single_instance, "hand_over") as handed,
            mock.patch.object(single_instance, "_ask") as asked,
        ):
            self.listening(claim(self.folder, replacing=True))
        handed.assert_not_called()
        asked.assert_not_called()

    def test_a_windows_updates_copy_listens_once_the_old_qitv_lets_go(self):
        old = single_instance._InstanceLock(self.folder)  # the QiTV being replaced, quitting
        self.assertIs(old.acquire(), single_instance._Lock.OURS)
        with mock.patch.object(single_instance, "RETRY_MS", 50):
            server = claim(self.folder, replacing=True)
            self.addCleanup(self.dispose, server)
            self.pump(lambda: False, 0.4)
            self.assertFalse(server.listening())  # the old one still holds its settings
            old.release()
            self.pump(server.listening, 5)
        self.assertTrue(server.listening())
        other = single_instance._InstanceLock(self.folder)
        self.assertIs(other.acquire(), single_instance._Lock.HELD)  # the new copy's now

    def test_a_windows_updates_copy_listens_all_the_same_after_its_bound(self):
        old = single_instance._InstanceLock(self.folder)  # an old QiTV that never quits
        self.assertIs(old.acquire(), single_instance._Lock.OURS)
        self.addCleanup(old.release)
        with (
            mock.patch.object(single_instance, "RETRY_MS", 50),
            mock.patch.object(single_instance, "RETRIES", 4),
            self.assertLogs("services.single_instance", "WARNING") as logs,
        ):
            server = claim(self.folder, replacing=True)
            self.addCleanup(self.dispose, server)
            self.pump(server.listening, 5)
        self.assertTrue(server.listening())
        self.assertTrue([line for line in logs.output if "listening anyway" in line], logs.output)

    def test_a_qitv_that_cant_listen_lets_the_lock_go(self):
        with mock.patch.object(InstanceServer, "listen", return_value=False):
            server = claim(self.folder)
        self.addCleanup(self.dispose, server)
        other = single_instance._InstanceLock(self.folder)
        self.assertIs(other.acquire(), single_instance._Lock.OURS)  # no start waits for it
        other.release()

    def test_the_qitv_that_listens_holds_the_lock_until_it_closes(self):
        server = claim(self.folder)
        self.listening(server)
        other = single_instance._InstanceLock(self.folder)
        self.assertIs(other.acquire(), single_instance._Lock.HELD)
        server.close()
        self.assertIs(other.acquire(), single_instance._Lock.OURS)
        other.release()
        # The file stays: removed, two starts could each lock a file of their own.
        self.assertTrue(os.path.exists(os.path.join(self.folder, single_instance.LOCK_FILE)))

    def fresh_folder(self):
        """A settings folder of its own for a subcase (nothing an earlier one held)."""
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = folder.name
        self.name = server_name(self.folder)

    def holder(self, mode, delay):
        env = dict(os.environ, PYTHONPATH=str(REPO), QT_QPA_PLATFORM="offscreen")
        process = subprocess.Popen(
            [sys.executable, "-c", HOLDER, self.folder, mode, str(delay)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=env,
            cwd=str(REPO),
        )
        self.addCleanup(self.stop_holder, process)  # before its folder goes
        self.assertEqual(process.stdout.readline().strip(), "ready")
        return process

    def stop_holder(self, process):
        """It quits through its stdin, letting go of its lock itself, and is waited for (a kill
        only when it didn't within 15 s)."""
        if process.stdin and not process.stdin.closed:
            process.stdin.close()
        try:
            process.wait(15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(15)
        if process.stdout and not process.stdout.closed:
            process.stdout.close()

    def assertLockFree(self):
        """Nothing holds the settings folder's lock any more: no test leaves it held."""
        lock = single_instance._InstanceLock(self.folder)
        self.assertIs(lock.acquire(), single_instance._Lock.OURS)
        lock.release()

    def test_a_late_lock_holder_gets_every_start(self):
        # Ruling 32: a holder whose event loop runs only LATE_S after the starts claimed (past
        # START_WAIT_MS; CI's Macs took 38 s with 8 starts at once) gets every start's file;
        # none takes over.
        holder = self.holder("slow", 60)

        def serve_late(moment):
            time.sleep(max(0.0, moment + LATE_S - time.time()))
            holder.stdin.write("serve\n")
            holder.stdin.flush()

        files = [os.path.join(self.folder, f"Geç {i}.mkv") for i in range(3)]
        said = self.race(files, expect=0, meanwhile=serve_late)  # one that listened would show
        self.assertEqual([s["handed_over"] for s in said], [True] * 3, said)
        for s in said:  # each waited past START_WAIT_MS: before Ruling 32 it took over then
            self.assertGreater(s["claim_s"], single_instance.START_WAIT_MS / 1000, said)
        got = sorted(json.loads(holder.stdout.readline().split(" ", 1)[1]) for _ in files)
        self.assertEqual(got, sorted(files))
        self.stop_holder(holder)
        self.assertEqual(holder.returncode, 0)
        self.assertLockFree()

    def test_a_lock_holder_that_never_answers_ends_the_start(self):
        # Reached but never answering ("quiet"), or never listening (this process's own lock):
        # after HELD_WAIT_MS the start ends without listening, never beside its holder.
        for case in ("quiet", "unlistened"):
            with self.subTest(case=case):
                self.fresh_folder()
                process, held = None, None
                if case == "quiet":
                    process = self.holder("quits", 30)
                else:
                    held = single_instance._InstanceLock(self.folder)
                    self.assertIs(held.acquire(), single_instance._Lock.OURS)
                    self.addCleanup(held.release)  # before its folder goes, whatever fails
                started = time.monotonic()
                with (
                    mock.patch.object(single_instance, "HELD_WAIT_MS", 1500),
                    self.assertLogs("services.single_instance", "WARNING") as logs,
                ):
                    self.assertIsNone(claim(self.folder, paths=[self.turkish_path()]))
                waited = time.monotonic() - started
                self.assertGreaterEqual(waited, 1.4)
                self.assertLess(waited, 5)
                ends = "holds them but doesn't answer; this start ends"
                self.assertTrue([line for line in logs.output if ends in line], logs.output)
                if held is not None:
                    held.release()
                if process is not None:
                    self.stop_holder(process)
                    self.assertEqual(process.returncode, 0)
                self.assertLockFree()

    def test_only_the_lock_ends_the_wait(self):
        # A lock that reads HELD, then UNAVAILABLE (no descriptor left, a lock call failing a
        # moment) is no sign its holder went: the start waits on, and ends without listening.
        states = iter([single_instance._Lock.HELD])
        with (
            mock.patch.object(
                single_instance._InstanceLock,
                "acquire",
                autospec=True,
                side_effect=lambda lock: next(states, single_instance._Lock.UNAVAILABLE),
            ),
            mock.patch.object(single_instance, "HELD_WAIT_MS", 800),
            self.assertLogs("services.single_instance", "WARNING") as logs,
        ):
            started = time.monotonic()
            self.assertIsNone(claim(self.folder, paths=[self.turkish_path()]))
        self.assertGreaterEqual(time.monotonic() - started, 0.75)  # the whole wait
        ends = "holds them but doesn't answer; this start ends"
        self.assertTrue([line for line in logs.output if ends in line], logs.output)
        self.assertFalse(hand_over(self.name, 300))  # nobody listens: it never did

    def test_an_updates_copy_hands_over_to_a_start_that_took_the_lock(self):
        # A double-click during an update may take the lock between the old QiTV letting go
        # and the update's copy (its old file replaced: it claims as any start does). The copy
        # hands over to it and ends: never a second QiTV beside it.
        holder = self.holder("slow", 0)  # serves at once
        started = time.monotonic()
        with self.assertLogs("services.single_instance", "INFO") as logs:
            self.assertIsNone(claim(self.folder, replacing=False))
        self.assertLess(time.monotonic() - started, 5)
        self.assertTrue([line for line in logs.output if "brought to the front" in line])
        self.stop_holder(holder)
        self.assertEqual(holder.returncode, 0)
        self.assertLockFree()

    def test_a_lock_holder_that_ends_mid_wait_passes_the_lock_on(self):
        # Its holder quits or crashes while a start waits: the start takes the lock and runs.
        # "closes": a quitting QiTV hangs up before it lets go of the lock; the start waits on,
        # and takes the lock once it is let go, while that process still lives.
        for mode in ("quits", "unlistened", "closes", "crashes"):
            with self.subTest(mode=mode):
                if mode == "crashes" and os.name == "nt":
                    # Windows lets go of a dead process's lock "depending upon available system
                    # resources": QiTV waits for it (HELD_WAIT_MS), a test can't bound it.
                    self.skipTest("Windows lets go of a crashed process's lock in its own time")
                self.fresh_folder()
                holder = self.holder(mode, 2)
                started = time.monotonic()
                server = claim(self.folder)
                listened_at = time.time()
                self.assertGreaterEqual(time.monotonic() - started, 1.5)  # it waited for it
                self.listening(server)
                if mode == "closes":
                    # Hung up on 2 s before the lock went: it waited on, and listened only once
                    # the lock was let go, the holder still alive.
                    self.assertIsNone(holder.poll())
                    released = holder.stdout.readline().split()
                    self.assertEqual(released[0], "released")
                    self.assertGreaterEqual(listened_at, float(released[1]))
                    self.assertGreaterEqual(time.monotonic() - started, 3.5)
                self.assertEqual(holder.wait(10), 0)
                other = single_instance._InstanceLock(self.folder)
                self.assertIs(other.acquire(), single_instance._Lock.HELD)  # the start's now
                server.close()  # the server, then its lock
                self.assertLockFree()

    def test_the_server_answers_and_says_so(self):
        server = InstanceServer(self.name)
        self.assertTrue(server.listen())
        self.listening(server)
        seen = []
        server.activated.connect(lambda: seen.append(1))
        script = (
            "import sys\nfrom services.single_instance import hand_over\n"
            "sys.exit(0 if hand_over(sys.argv[1]) else 1)\n"
        )
        env = dict(os.environ, PYTHONPATH=str(REPO), QT_QPA_PLATFORM="offscreen")
        asking = subprocess.Popen([sys.executable, "-c", script, self.name], env=env, cwd=str(REPO))
        deadline = time.monotonic() + 10
        while asking.poll() is None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        self.assertEqual(asking.wait(5), 0)
        self.assertEqual(seen, [1])

    # ---- files handed over ------------------------------------------------------------------

    def turkish_path(self):
        return os.path.join(self.folder, "Dizi Bölüm 1.mkv")

    def long_path(self):
        """300 characters, with spaces (and a Turkish letter) in every part."""
        head = self.folder
        while len(head) < 230:
            head = os.path.join(head, "Sezon 1 Bölümler")
        stem = ("Uzun bir bölüm adı " * 10)[: 300 - len(head) - len(os.sep) - len(".mkv")]
        path = os.path.join(head, stem + ".mkv")
        self.assertEqual(len(path), 300)
        return path

    def serve(self):
        server = InstanceServer(self.name)
        self.assertTrue(server.listen())
        self.listening(server)
        return server

    def child(self, script, *args):
        env = dict(os.environ, PYTHONPATH=str(REPO), QT_QPA_PLATFORM="offscreen")
        process = subprocess.Popen(
            [sys.executable, "-c", script, self.name, *args],
            stdout=subprocess.PIPE,
            text=True,
            env=env,
            cwd=str(REPO),
        )
        self.addCleanup(self.end, process)
        return process

    @staticmethod
    def end(process):
        if process.poll() is None:
            process.kill()
        process.wait(15)
        if process.stdout and not process.stdout.closed:
            process.stdout.close()

    def ask(self, paths):
        return self.child(ASK, json.dumps(paths))

    @staticmethod
    def pump(until, seconds=15.0):
        """This process's events (the server in it answers) until `until()` or `seconds`."""
        deadline = time.monotonic() + seconds
        while not until() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)

    def said(self, process):
        """The child's JSON line, once it has ended (this process answering meanwhile)."""
        self.pump(lambda: process.poll() is not None)
        out, _ = process.communicate(timeout=5)
        lines = out.strip().splitlines()
        self.assertTrue(lines, "the child said nothing")
        return json.loads(lines[-1])

    def assertTaken(self, said):
        self.assertTrue(said["ok"])
        self.assertLess(said["ms"], single_instance.ANSWER_MS)
        self.assertFalse([line for line in said["log"] if any(n in line for n in NOT_TAKEN)])

    def test_files_reach_the_first(self):
        first = self.first("files")
        p1, p2 = self.turkish_path(), self.long_path()
        with self.assertLogs("services.single_instance", "INFO") as logs:
            self.assertIsNone(claim(self.folder, paths=[p1, p2]))
        got = [first.stdout.readline().strip() for _ in range(2)]
        self.assertEqual([line.split(" ", 1)[0] for line in got], ["opened", "opened"])
        self.assertEqual([json.loads(line.split(" ", 1)[1]) for line in got], [p1, p2])
        self.assertFalse([line for line in logs.output if any(n in line for n in NOT_TAKEN)])

    def test_extended_paths_reach_the_first_as_they_are(self):
        # Ruling 29: \\?\ and \\.\ paths (ntpath's spelling; any system carries them) arrive
        # byte for byte, through the protocol as a real hand-over sends them.
        first = self.first("files")
        # (Not the lone surrogate's: the line carries it, Qt's signal after it doesn't, as before.)
        paths = EXTENDED[:3]
        with self.assertLogs("services.single_instance", "INFO") as logs:
            self.assertIsNone(claim(self.folder, paths=paths))
        got = [first.stdout.readline().strip() for _ in paths]
        self.assertEqual([json.loads(line.split(" ", 1)[1]) for line in got], paths)
        self.assertFalse([line for line in logs.output if any(n in line for n in NOT_TAKEN)])

    def test_a_path_too_long_to_hand_over_is_left_out(self):
        first = self.first("files")
        p1 = self.turkish_path()
        huge = os.path.join(self.folder, "a" * single_instance.MAX_LINE + ".mkv")
        with self.assertLogs("services.single_instance", "INFO") as logs:
            self.assertIsNone(claim(self.folder, paths=[huge, p1]))
        self.assertEqual(first.stdout.readline().strip(), "opened " + json.dumps(p1))
        self.assertTrue([line for line in logs.output if "too long to hand over" in line])

    def test_old_first_still_comes_forward(self):
        first = self.first("old")
        started = time.monotonic()
        with self.assertLogs("services.single_instance", "INFO") as logs:
            self.assertTrue(hand_over(self.name, paths=[self.turkish_path()]))
        self.assertLess(time.monotonic() - started, (single_instance.ANSWER_MS + 500) / 1000)
        older = "The running QiTV is older: it came to the front without the file."
        self.assertTrue([line for line in logs.output if older in line], logs.output)
        out, _ = first.communicate(timeout=10)
        self.assertEqual(out.split(), ["activated"])  # once

    def test_show_alone_unchanged(self):
        spy = QLocalServer()
        self.assertTrue(spy.listen(self.name))
        sockets, seen, ended = [], bytearray(), []

        def on_read(socket):
            seen.extend(bytes(socket.readAll().data()))
            if bytes(seen) == single_instance.SHOW:
                # Answered, and the connection kept, as a QiTV that takes files does.
                socket.write(single_instance.SHOWN)
                socket.flush()

        def on_connection():
            socket = spy.nextPendingConnection()
            sockets.append(socket)
            socket.readyRead.connect(lambda: on_read(socket))
            socket.disconnected.connect(lambda: ended.append(1))

        spy.newConnection.connect(on_connection)
        self.addCleanup(self.dispose_spy, spy, sockets)
        asking = self.ask([])
        said = self.said(asking)
        self.pump(lambda: bool(ended), 5)
        self.assertTrue(said["ok"])
        self.assertLess(said["ms"], single_instance.ANSWER_MS)
        self.assertEqual(bytes(seen), single_instance.SHOW)

    @staticmethod
    def dispose_spy(spy, sockets):
        for socket in sockets:
            socket.abort()
            socket.deleteLater()
        spy.close()
        spy.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_line_over_cap_is_dropped(self):
        server = self.serve()
        opened, activated = [], []
        server.opened.connect(opened.append)
        server.activated.connect(lambda: activated.append(1))
        with self.assertLogs("services.single_instance", "INFO") as logs:
            said = self.said(self.child(RAW, "long"))
        self.assertTrue([line for line in logs.output if "too long" in line], logs.output)
        self.assertEqual(said["shown"], "shown\n")
        self.assertNotIn("opened", said["answer"])
        self.assertTrue(said["closed"])
        self.assertLess(said["ms"], single_instance.ANSWER_MS)  # let go for the line, not quiet
        self.assertEqual(opened, [])
        # Another start isn't held up by it.
        p1 = self.turkish_path()
        self.assertTaken(self.said(self.ask([p1])))
        self.assertEqual(opened, [[p1]])
        self.assertEqual(activated, [1, 1])

    def test_a_start_that_goes_quiet_is_let_go(self):
        server = self.serve()
        opened = []
        server.opened.connect(opened.append)
        with mock.patch.object(single_instance, "ANSWER_MS", 300):
            with self.assertLogs("services.single_instance", "INFO") as logs:
                said = self.said(self.child(RAW, "quiet"))
        self.assertTrue([line for line in logs.output if "said nothing more" in line])
        self.assertEqual(said["shown"], "shown\n")
        self.assertEqual(said["answer"], "")
        self.assertTrue(said["closed"])
        self.assertLess(said["ms"], 2000)  # let go after 300 ms of silence, not 5 s
        self.assertEqual(opened, [])

    def test_answers_during_a_modal_loop(self):
        # Review Focus 5: a modal dialog (Settings) runs its own event loop inside the main
        # one's; the hand-over is answered there, and the files arrive there too.
        server = self.serve()
        p1 = self.turkish_path()
        outer, nested = QEventLoop(), QEventLoop()
        seen = []
        server.opened.connect(lambda paths: seen.append((paths, nested.isRunning())))
        asking = self.ask([p1])

        def modal():
            watch = QTimer()
            watch.timeout.connect(lambda: asking.poll() is not None and nested.quit())
            watch.start(20)
            QTimer.singleShot(15000, nested, nested.quit)
            nested.exec()
            watch.stop()
            outer.quit()

        QTimer.singleShot(0, outer, modal)
        QTimer.singleShot(20000, outer, outer.quit)
        outer.exec()
        self.assertTaken(self.said(asking))
        self.assertEqual(seen, [([p1], True)])


class FileUrlTests(unittest.TestCase):
    """An `open` line carries a path byte for byte (Review Focus 1): a file URL, or the path
    itself (raw) for a Windows path with the extended-length or device prefix."""

    def cases(self):
        if os.name == "nt":
            long_dir = "\\".join(["Sezon 1 Bölümler uzun klasör adı"] * 9)
            return [
                "C:\\Videos\\Dizi Bölüm 1.mkv",
                "\\\\SERVER\\Share\\Dizi Bölüm 1.mkv",
                "\\\\?\\C:\\" + long_dir + "\\Dizi Bölüm 1.mkv",  # over 260 characters
                "\\\\?\\UNC\\SERVER\\Share\\Dizi Bölüm 1.mkv",
                "C:\\Videos\\a%20b #1 ;x=y&[z]{w}~'.mkv",
            ]
        return [
            "/v/Dizi Bölüm 1.mkv",
            unicodedata.normalize("NFD", "/v/Dizi Bölüm 1.mkv"),  # as a Mac may name it
            "/v/a%20b #1 ?x;y=z&[a]{b}~'.mkv",
            "/v/new\nline.mkv",
            "/v/back\\slash.mkv",
            "//SERVER/Share/Dizi Bölüm 1.mkv",  # the server's name keeps its case
            "/" + "/".join(["Sezon 1 Bölümler uzun klasör adı"] * 9) + "/Dizi Bölüm 1.mkv",
        ]

    def test_paths_cross_byte_for_byte(self):
        for path in self.cases():
            with self.subTest(path=path):
                payload = single_instance._open_payload(path)
                self.assertNotIn(b"\n", payload)
                extended = path.startswith(("\\\\?\\", "\\\\.\\"))
                form = single_instance.RAW if extended else b"file:"
                self.assertTrue(payload.startswith(form), payload)
                self.assertEqual(single_instance._opened_path(payload), path)

    def test_extended_paths_go_raw_on_every_system(self):
        for path in EXTENDED:
            with self.subTest(path=path):
                payload = single_instance._open_payload(path)
                self.assertTrue(payload.startswith(single_instance.RAW), payload)
                payload.decode("ascii")  # percent-encoded: no space, newline or other byte
                self.assertNotIn(b" ", payload)
                self.assertEqual(single_instance._opened_path(payload), path)

    def test_a_raw_path_that_doesnt_decode_is_skipped(self):
        for payload in (b"raw:", b"raw:%FF.mkv", b"raw:\xff", b"raw:C%3A%5Ca%00b.mkv"):
            with self.subTest(payload=payload):
                self.assertEqual(single_instance._opened_path(payload), "")

    def test_a_relative_path_is_made_absolute(self):
        url = single_instance._file_url("Dizi Bölüm 1.mkv")
        self.assertEqual(single_instance._local_path(url), os.path.abspath("Dizi Bölüm 1.mkv"))

    def test_other_urls_are_not_files(self):
        for url in (b"http://host/a.mkv", b"file:", b"", b"garbage \xff"):
            with self.subTest(url=url):
                self.assertEqual(single_instance._local_path(url), "")
                self.assertEqual(single_instance._opened_path(url), "")


from tests.test_tv_window import WindowCase  # noqa: E402


class ComeToFrontTests(WindowCase):
    def test_a_minimized_window_comes_back_as_it_was(self):
        self.show()
        self.window.setWindowState(Qt.WindowState.WindowMaximized)
        self.window.setWindowState(self.window.windowState() | Qt.WindowState.WindowMinimized)
        self.window.come_to_front()
        state = self.window.windowState()
        self.assertFalse(state & Qt.WindowState.WindowMinimized)
        self.assertTrue(state & Qt.WindowState.WindowMaximized)
        self.assertTrue(self.window.isVisible())


if __name__ == "__main__":
    unittest.main()
