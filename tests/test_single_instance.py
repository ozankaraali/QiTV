"""One QiTV per settings folder (2.8.12): a second start hands over to the first and quits; a
first that never answers (hung, or a socket left by a crash) is not waited for. A start with
files hands them over too; a first of 2.10, which only knows `show`, still comes forward."""

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

from services import single_instance
from services.single_instance import InstanceServer, claim, hand_over, server_name

REPO = Path(__file__).resolve().parents[1]
app = QApplication.instance() or QApplication([])

# The first QiTV, in a process of its own: it listens, says "ready", and once asked to come
# to the front says "activated" and ends. With "hang" it listens and never answers. With
# "files" it prints `opened <path as JSON>` for each file a start handed over, and ends. With
# "old" it is QiTV 2.10's server: it answers "shown" to "show" and hangs up at once.
FIRST = r"""
import json, sys, time
from PySide6.QtCore import QCoreApplication, QTimer
app = QCoreApplication([])
mode = sys.argv[2]
if mode == "old":
    from PySide6.QtNetwork import QLocalServer
    server = QLocalServer()
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
    read = {}
    def on_read(socket):
        data = read.get(id(socket), b"") + bytes(socket.readAll().data())
        if b"\n" not in data:
            read[id(socket)] = data[-64:]
            return
        read.pop(id(socket), None)
        if data.startswith(b"show\n"):
            print("activated", flush=True)
            QTimer.singleShot(300, app.quit)
            socket.write(b"shown\n")
            socket.flush()
        socket.disconnectFromServer()
    def on_connection():
        while server.hasPendingConnections():
            socket = server.nextPendingConnection()
            socket.readyRead.connect(lambda s=socket: on_read(s))
            socket.disconnected.connect(socket.deleteLater)
            if socket.bytesAvailable():
                on_read(socket)
    server.newConnection.connect(on_connection)
    assert server.listen(sys.argv[1])
else:
    from services.single_instance import InstanceServer
    server = InstanceServer(sys.argv[1])
    assert server.listen()
print("ready", flush=True)
if mode == "hang":
    time.sleep(8)
    sys.exit(0)
if mode == "files":
    def on_opened(paths):
        for path in paths:
            print("opened " + json.dumps(path), flush=True)
        QTimer.singleShot(200, app.quit)
    server.opened.connect(on_opened)
elif mode == "answer":
    server.activated.connect(lambda: print("activated", flush=True))
    server.activated.connect(lambda: QTimer.singleShot(200, app.quit))
QTimer.singleShot(8000, app.quit)
app.exec()
"""

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

# What a start logs when the first didn't take its files.
NOT_TAKEN = ("older", "didn't say")


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

    def test_main_claims_only_in_main(self):
        source = (REPO / "main.py").read_text(encoding="utf-8")
        claim_at = source.index("instance = claim(")
        self.assertGreater(claim_at, source.index("def main()"))
        self.assertLess(claim_at, source.index("ConfigManager()"))  # before any config write
        self.assertIn('replacing="--replace" in sys.argv', source)
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
        with mock.patch.object(single_instance, "ANSWER_MS", 400):
            server = claim(self.folder)
        self.assertLess(time.monotonic() - started, 3)
        self.listening(server)  # the name is taken over

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
        self.listening(claim(self.folder))

    def test_a_windows_updates_copy_never_hands_over(self):
        with mock.patch.object(single_instance, "hand_over") as asked:
            self.listening(claim(self.folder, replacing=True))
        asked.assert_not_called()

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
    """An `open` line's URL carries a path byte for byte (Review Focus 1)."""

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
                url = single_instance._file_url(path)
                self.assertNotIn(b"\n", url)
                self.assertTrue(url.startswith(b"file:"))
                self.assertEqual(single_instance._local_path(url), path)

    def test_a_relative_path_is_made_absolute(self):
        url = single_instance._file_url("Dizi Bölüm 1.mkv")
        self.assertEqual(single_instance._local_path(url), os.path.abspath("Dizi Bölüm 1.mkv"))

    def test_other_urls_are_not_files(self):
        for url in (b"http://host/a.mkv", b"file:", b"", b"garbage \xff"):
            with self.subTest(url=url):
                self.assertEqual(single_instance._local_path(url), "")


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
