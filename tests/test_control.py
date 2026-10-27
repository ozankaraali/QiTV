"""The control port (2.8.3): a developer's tools read and drive the running QiTV, on this
computer only, with the token only this user can read."""

import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from timer_fixtures import wait_until

from services import control


def request(port, command, token, args=None):
    """POST /command from another thread, as a tool does: (status, answer)."""
    url = f"http://127.0.0.1:{port}/{command}"
    req = urllib.request.Request(url, data=json.dumps({"args": args or {}}).encode(), method="POST")
    if token is not None:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as answer:
            return answer.status, json.loads(answer.read())
    except urllib.error.HTTPError as error:
        with error:
            return error.code, json.loads(error.read() or b"{}")


class SwitchTests(unittest.TestCase):
    def test_from_source_it_opens_unless_turned_off_a_build_only_when_asked(self):
        self.assertTrue(control.enabled({}, frozen=False))
        self.assertFalse(control.enabled({"QITV_CONTROL": "0"}, frozen=False))
        self.assertFalse(control.enabled({}, frozen=True))
        self.assertTrue(control.enabled({"QITV_CONTROL": "1"}, frozen=True))
        self.assertTrue(control.python_allowed(frozen=False))
        self.assertFalse(control.python_allowed(frozen=True))


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="qitv-control-"))
        self.log = self.directory / "qitv.log"
        self.log.write_text("a\nError shown: one\nb\nError shown: two\n", encoding="utf-8")
        self.threads = []

        def handle(command, args):
            self.threads.append(threading.current_thread() is threading.main_thread())
            if command == "status":
                return {"version": "test", "args": args}
            return {"error": f"unknown command: {command}"}

        self.server = control.ControlServer(handle, self.directory, self.log)
        self.assertIsNotNone(self.server.start())
        self.addCleanup(self.server.stop)

    def ask(self, command, token, args=None):
        """The request on a thread while this (the GUI) thread runs events."""
        got = []
        thread = threading.Thread(
            target=lambda: got.append(request(self.server.port, command, token, args))
        )
        thread.start()
        self.assertTrue(wait_until(lambda: bool(got)))
        thread.join(5)
        return got[0]

    def test_it_listens_on_this_computer_only_and_writes_a_private_file(self):
        self.assertEqual(self.server._server.server_address[0], "127.0.0.1")
        data = json.loads(self.server.file.read_text(encoding="utf-8"))
        self.assertEqual(
            (data["port"], data["token"], data["pid"]),
            (self.server.port, self.server.token, os.getpid()),
        )
        mode = stat.S_IMODE(self.server.file.stat().st_mode)
        self.assertEqual(mode & 0o077, 0)  # this user only

    def test_without_the_token_nothing_runs(self):
        for token in (None, "wrong"):
            with self.subTest(token=token):
                status, answer = self.ask("status", token)
                self.assertEqual(status, 401)
        self.assertEqual(self.threads, [])

    def test_a_command_runs_on_the_gui_thread_and_answers(self):
        status, answer = self.ask("status", self.server.token, {"x": 1})
        self.assertEqual((status, answer), (200, {"version": "test", "args": {"x": 1}}))
        self.assertEqual(self.threads, [True])
        status, answer = self.ask("nothing", self.server.token)
        self.assertEqual(status, 404)

    def test_the_log_is_read_with_a_filter(self):
        status, answer = self.ask("log", self.server.token, {"lines": 1, "grep": "Error shown"})
        self.assertEqual(answer, {"lines": ["Error shown: two"]})
        self.assertEqual(self.threads, [])  # read on the request's thread

    def test_stopping_removes_the_file(self):
        self.server.stop()
        self.assertFalse(self.server.file.exists())


if __name__ == "__main__":
    unittest.main()
