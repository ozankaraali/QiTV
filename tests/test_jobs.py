import os
import threading
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from services.jobs import ImmediateRunner, JobRunner


class _Worker(QObject):
    finished = Signal(object)
    error = Signal(str)

    def __init__(self, fail=False):
        super().__init__()
        self.fail = fail

    def run(self):
        if self.fail:
            self.error.emit("boom")
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


class JobRunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_worker_runs_off_thread_and_result_arrives_on_gui_thread(self):
        runner, receiver = JobRunner(), _Receiver()
        runner.run_worker(_Worker(), receiver.on_result, receiver.on_error)
        for _ in range(200):
            if receiver.results and runner.pending() == 0:
                break
            QTest.qWait(10)
        self.assertEqual(len(receiver.results), 1)
        self.assertNotEqual(receiver.results[0]["thread"], threading.get_ident())
        self.assertEqual(receiver.threads, [threading.get_ident()])
        self.assertEqual(runner.pending(), 0)

    def test_errors_are_delivered(self):
        runner, receiver = JobRunner(), _Receiver()
        runner.run_worker(_Worker(fail=True), receiver.on_result, receiver.on_error)
        for _ in range(200):
            if receiver.errors and runner.pending() == 0:
                break
            QTest.qWait(10)
        self.assertEqual(receiver.errors, ["boom"])

    def test_immediate_runner_is_synchronous(self):
        receiver = _Receiver()
        ImmediateRunner().run_worker(_Worker(), receiver.on_result, receiver.on_error)
        self.assertEqual(len(receiver.results), 1)
        self.assertEqual(receiver.results[0]["thread"], threading.get_ident())


if __name__ == "__main__":
    unittest.main()
