"""One QiTV per settings folder (2.8.12): a second start hands over to the first and quits; a
first that never answers (hung, or a socket left by a crash) is not waited for."""

import os
from pathlib import Path
import socket as pysocket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtNetwork import QLocalServer
from PySide6.QtWidgets import QApplication

from services import single_instance
from services.single_instance import InstanceServer, claim, hand_over, server_name

REPO = Path(__file__).resolve().parents[1]
app = QApplication.instance() or QApplication([])

# The first QiTV, in a process of its own: it listens, says "ready", and once asked to come
# to the front says "activated" and ends. With "hang" it listens and never answers.
FIRST = """
import sys, time
from PySide6.QtCore import QCoreApplication, QTimer
from services.single_instance import InstanceServer
app = QCoreApplication([])
server = InstanceServer(sys.argv[1])
assert server.listen()
print("ready", flush=True)
if sys.argv[2] == "hang":
    time.sleep(8)
    sys.exit(0)
server.activated.connect(lambda: print("activated", flush=True))
server.activated.connect(lambda: QTimer.singleShot(200, app.quit))
QTimer.singleShot(8000, app.quit)
app.exec()
"""


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
        self.addCleanup(server.close)
        self.assertTrue(server.listening())

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
