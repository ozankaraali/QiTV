import os
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from job_fixtures import ImmediateRunner

from services.jobs import Cancelled, JobRunner, cancel_requested
from tests.timer_fixtures import wait_until

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

    def on_result(self, context, payload):
        self.results.append(payload)
        self.threads.append(threading.get_ident())

    def on_error(self, context, message):
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


class JobRunnerStressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_fast_workers_each_deliver_exactly_once_with_their_context(self):
        runner = JobRunner()
        delivered = []

        def on_result(context, payload):
            delivered.append(("ok", context))

        def on_error(context, message):
            delivered.append(("err", context))

        for i in range(50):
            runner.run_worker(_Worker("fail" if i % 5 == 0 else "ok"), on_result, on_error, i)
        for _ in range(300):
            if len(delivered) >= 50 and runner.pending() == 0:
                break
            QTest.qWait(10)
        self.assertEqual(sorted(c for _, c in delivered), list(range(50)))
        self.assertEqual(runner.pending(), 0)
        self.assertEqual(len(runner._relays), 0)


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


class JobCancellationTests(unittest.TestCase):
    """Quitting cancels the catalog's running jobs: an XMLTV download or an STB crawl stops
    at once instead of holding the close for up to 30 s."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def run_and_cancel(self, worker, under_way, *, within_s=2.0):
        """Start `worker`, cancel the runner once `under_way()`; the job's outcome."""
        runner = JobRunner()
        outcome = []
        runner.run_worker(
            worker,
            lambda _c, payload: outcome.append(("ok", payload)),
            lambda _c, message: outcome.append(("error", message)),
        )
        self.assertTrue(wait_until(under_way, 20_000))  # e.g. a first import of aiohttp
        self.assertEqual(outcome, [])  # still running
        started = time.monotonic()
        runner.cancel_all()
        self.assertTrue(wait_until(lambda: outcome and runner.pending() == 0, 10_000))
        self.assertLess(time.monotonic() - started, within_s, outcome)
        return outcome[0]

    def test_cancel_requested_is_false_outside_a_cancelled_job(self):
        self.assertFalse(cancel_requested())

    def test_a_cancelled_job_that_checks_stops(self):
        polls = []

        class Polling(QObject):
            finished = Signal(object)
            error = Signal(str)

            def run(self):
                polls.append(1)
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    if cancel_requested():
                        raise Cancelled()
                    time.sleep(0.01)
                self.finished.emit("done")

        kind, _ = self.run_and_cancel(Polling(), lambda: polls)
        self.assertEqual(kind, "error")

    def test_an_xmltv_download_stops_and_marks_nothing_unavailable(self):
        from services.epg_manager import EpgManager, _cache_key
        from services.multikeydict import MultiKeyDict
        from services.workers import EpgLoadWorker

        directory = tempfile.TemporaryDirectory(prefix="qitv-cancel-")
        self.addCleanup(directory.cleanup)
        url = "http://epg.test/guide.xml"
        config = SimpleNamespace(
            get_config_dir=lambda: directory.name,
            epg_source="URL",
            epg_url=url,
            ssl_verify=True,
            xmltv_channel_map=MultiKeyDict(),
        )
        manager = EpgManager(config, SimpleNamespace(current_provider={"type": "M3U"}))

        sent = []

        def endless(_size):
            yield b'<?xml version="1.0"?><tv>'
            while True:  # a slow guide server: a chunk now and then, never the end
                time.sleep(0.02)
                sent.append(1)
                yield b"<!-- " + b"x" * 1000 + b" -->"

        response = SimpleNamespace(
            status_code=200, headers={}, iter_content=endless, close=lambda: None
        )
        with mock.patch("services.epg_manager.requests.get", return_value=response):
            worker = EpgLoadWorker(manager, force_refresh=True)
            kind, _ = self.run_and_cancel(worker, lambda: len(sent) > 5)
        self.assertEqual(kind, "error")
        self.assertNotIn(_cache_key(url), manager.index)  # tried again next time
        self.assertEqual(os.listdir(os.path.join(directory.name, "cache", "epg")), [])

    def test_an_stb_listing_stops_while_the_portal_keeps_it_waiting(self):
        from services.workers import StbListWorker
        from tests.http_fixtures import LocalSource

        class EveryPath(dict):
            def get(self, _path, _default=None):
                return silent

        asked = threading.Event()

        def silent(handler, _count):
            asked.set()
            source.stop.wait(30)  # the portal never answers (until the test ends)
            return 200, {}, b"{}"

        source = LocalSource(EveryPath())
        self.addCleanup(source.close)
        worker = StbListWorker(
            url=f"{source.url}/server/load.php",
            headers={},
            content_type="vod",
            category_id="1",
            timeout=20,
        )
        kind, _ = self.run_and_cancel(worker, asked.is_set)
        self.assertEqual(kind, "error")


if __name__ == "__main__":
    unittest.main()
