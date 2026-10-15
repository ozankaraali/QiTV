import os
import threading
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from services.jobs import ImmediateRunner, JobRunner

GENERIC_FAILURE = "The background task failed"
NO_RESULT = "The background task ended without a result"


class _Worker(QObject):
    finished = Signal(object)
    error = Signal(str)

    def __init__(self, mode="ok"):
        super().__init__()
        self.mode = mode

    def run(self):
        if self.mode == "fail":
            self.error.emit("boom")
        elif self.mode == "raise":
            raise RuntimeError("internal detail")
        elif self.mode == "silent":
            return
        else:
            self.finished.emit({"thread": threading.get_ident()})


class _Receiver(QObject):
    def __init__(self):
        super().__init__()
        self.results, self.errors, self.threads = [], [], []

    def on_result(self, payload):
        self.results.append(payload)
        self.threads.append(threading.get_ident())

    def on_error(self, message):
        self.errors.append(message)


def _wait_for(runner, receiver):
    for _ in range(200):
        if (receiver.results or receiver.errors) and runner.pending() == 0:
            return
        QTest.qWait(10)


class JobRunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_worker_runs_off_thread_and_result_arrives_on_gui_thread(self):
        runner, receiver = JobRunner(), _Receiver()
        runner.run_worker(_Worker(), receiver.on_result, receiver.on_error)
        _wait_for(runner, receiver)
        self.assertEqual(len(receiver.results), 1)
        self.assertNotEqual(receiver.results[0]["thread"], threading.get_ident())
        self.assertEqual(receiver.threads, [threading.get_ident()])
        self.assertEqual(runner.pending(), 0)

    def test_errors_are_delivered(self):
        runner, receiver = JobRunner(), _Receiver()
        runner.run_worker(_Worker("fail"), receiver.on_result, receiver.on_error)
        _wait_for(runner, receiver)
        self.assertEqual(receiver.errors, ["boom"])

    def test_raising_worker_reports_generic_error_and_releases_thread(self):
        runner, receiver = JobRunner(), _Receiver()
        with self.assertLogs("services.jobs", "ERROR"):
            runner.run_worker(_Worker("raise"), receiver.on_result, receiver.on_error)
            _wait_for(runner, receiver)
        self.assertEqual(receiver.errors, [GENERIC_FAILURE])
        self.assertEqual(receiver.results, [])
        self.assertEqual(runner.pending(), 0)

    def test_silent_worker_reports_no_result_and_releases_thread(self):
        runner, receiver = JobRunner(), _Receiver()
        runner.run_worker(_Worker("silent"), receiver.on_result, receiver.on_error)
        _wait_for(runner, receiver)
        self.assertEqual(receiver.errors, [NO_RESULT])
        self.assertEqual(receiver.results, [])
        self.assertEqual(runner.pending(), 0)


class ImmediateRunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_immediate_runner_is_synchronous(self):
        receiver = _Receiver()
        ImmediateRunner().run_worker(_Worker(), receiver.on_result, receiver.on_error)
        self.assertEqual(len(receiver.results), 1)
        self.assertEqual(receiver.results[0]["thread"], threading.get_ident())

    def test_immediate_raising_worker_reports_generic_error(self):
        receiver = _Receiver()
        with self.assertLogs("services.jobs", "ERROR"):
            ImmediateRunner().run_worker(_Worker("raise"), receiver.on_result, receiver.on_error)
        self.assertEqual(receiver.errors, [GENERIC_FAILURE])
        self.assertEqual(receiver.results, [])

    def test_immediate_silent_worker_reports_no_result(self):
        receiver = _Receiver()
        ImmediateRunner().run_worker(_Worker("silent"), receiver.on_result, receiver.on_error)
        self.assertEqual(receiver.errors, [NO_RESULT])
        self.assertEqual(receiver.results, [])


if __name__ == "__main__":
    unittest.main()
