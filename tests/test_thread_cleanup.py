import gc
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import traceback
from types import SimpleNamespace
import unittest
import weakref


class ThreadCleanupTests(unittest.TestCase):
    def run_scenario(self, scenario):
        # The parent owns fixtures even if native Qt teardown crashes the child.
        with tempfile.TemporaryDirectory(prefix="qitv-thread-cleanup-") as directory:
            playlist = Path(directory) / "local.m3u"
            playlist.write_text(
                '#EXTM3U\n'
                '#EXTINF:-1 group-title="Local",First channel\n'
                'http://example.invalid/first\n'
                '#EXTINF:-1 group-title="Local",Second channel\n'
                'http://example.invalid/second\n',
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment.update(
                QT_QPA_PLATFORM="offscreen",
                HOME=directory,
                XDG_CONFIG_HOME=str(Path(directory) / "config"),
                XDG_CACHE_HOME=str(Path(directory) / "cache"),
            )
            try:
                result = subprocess.run(
                    [
                        sys.executable,
                        "-X",
                        "faulthandler",
                        str(Path(__file__).resolve()),
                        "--scenario",
                        scenario,
                        str(playlist),
                    ],
                    cwd=Path(__file__).resolve().parents[1],
                    env=environment,
                    capture_output=True,
                    text=True,
                    errors="replace",
                    timeout=60,
                )
            except subprocess.TimeoutExpired as error:
                self.fail(f"{scenario} hung: stdout={error.stdout!r}, stderr={error.stderr!r}")
            self.assertEqual(
                result.returncode,
                0,
                f"{scenario} failed ({result.returncode})\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            self.assertIn(f"completed:{scenario}", result.stdout)

    def test_cleanup_retains_wrappers_without_blocking_the_gui(self):
        """Native finished must not release wrappers or synchronously join on the GUI."""
        self.run_scenario("delayed-cleanup")

    def test_local_m3u_reloads_and_destroyed_consumer_finish_without_crashing(self):
        """Exercise the real content lifecycle, including loss of its QObject owner."""
        self.run_scenario("content-loading")

    def test_job_runner_teardown_survives_gui_object_churn(self):
        """Hundreds of jobs finishing while the GUI destroys connected QObjects must not hang.

        A worker deleted on its own thread made Qt call back into Python while holding a
        signal-slot mutex, deadlocking against the GUI thread holding the GIL.
        """
        self.run_scenario("jobrunner-churn")


def run_child(scenario, playlist):
    # Keep QApplication, production imports, and crash-prone threads out of the
    # unittest runner. No window, playback backend, or user config is needed.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from PySide6.QtCore import QEventLoop, QObject, Qt, QThread, QTimer, Slot
    from PySide6.QtWidgets import QApplication

    from services.thread_cleanup import ThreadCleanup, has_pending_threads

    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    case = unittest.TestCase()
    slot_errors = []
    sys.excepthook = lambda *error: slot_errors.append("".join(traceback.format_exception(*error)))

    def wait_until(predicate, description):
        loop = QEventLoop()
        poll = QTimer()
        deadline = QTimer()
        deadline.setSingleShot(True)

        def check():
            if slot_errors or predicate():
                loop.quit()

        poll.timeout.connect(check)
        deadline.timeout.connect(loop.quit)
        poll.start(1)
        deadline.start(5000)
        loop.exec()
        poll.stop()
        deadline.stop()
        case.assertFalse(slot_errors, "\n".join(slot_errors))
        case.assertTrue(predicate(), description)

    class TeardownGate:
        def __init__(self):
            self.entered = threading.Event()
            self.release = threading.Event()
            self.expired = threading.Event()
            self.heartbeats = 0
            self.timer = QTimer()
            self.timer.setInterval(10)
            self.timer.timeout.connect(self.heartbeat)
            self.timer.start()

        def block(self):
            # Connected directly to QThread.finished: it runs on the worker thread
            # inside native thread teardown, before the thread can be joined. Never
            # leave it blocked on a failing assertion or a GUI-side blocking wait.
            self.entered.set()
            if not self.release.wait(5):
                self.expired.set()

        def heartbeat(self):
            if self.entered.is_set() and not self.release.is_set():
                self.heartbeats += 1

        def assert_pending(self, thread_ref, worker_ref):
            wait_until(lambda: self.heartbeats >= 5, "GUI stalled during native teardown")
            case.assertFalse(self.expired.is_set(), "teardown gate timed out")
            case.assertTrue(has_pending_threads(), "unfinished native thread was released")
            gc.collect()
            case.assertIsNotNone(thread_ref(), "QThread wrapper lost while teardown was active")
            case.assertIsNotNone(worker_ref(), "worker wrapper lost while teardown was active")

        def finish(self, destroyed):
            self.release.set()
            self.timer.stop()
            wait_until(
                lambda: not has_pending_threads() and destroyed.is_set(),
                "native cleanup did not finish and destroy its QThread",
            )
            case.assertFalse(self.expired.is_set(), "teardown gate timed out")

    if scenario == "delayed-cleanup":

        class Worker(QObject):
            @Slot()
            def run(self):
                QThread.currentThread().quit()

        class Observer(QObject):
            def __init__(self):
                super().__init__()
                self.completions = []

            @Slot(object)
            def completed(self, thread):
                self.completions.append((thread.wait(0), QThread.currentThread() == app.thread()))

        gate = TeardownGate()
        observer = Observer()
        destroyed = threading.Event()
        worker_destroyed_on = []
        thread = QThread()
        worker = Worker()
        worker.moveToThread(thread)
        thread.finished.connect(gate.block, Qt.DirectConnection)
        worker.destroyed.connect(
            lambda *_: worker_destroyed_on.append(threading.get_ident()), Qt.DirectConnection
        )
        thread.destroyed.connect(lambda *_: destroyed.set())
        thread.started.connect(worker.run)
        cleanup = ThreadCleanup(thread, worker)
        cleanup.finished.connect(observer.completed)
        thread_ref, worker_ref = weakref.ref(thread), weakref.ref(worker)
        thread.start()
        del cleanup, thread, worker
        try:
            wait_until(gate.entered.is_set, "native thread teardown was never reached")
            gate.assert_pending(thread_ref, worker_ref)
            case.assertEqual(observer.completions, [], "completion preceded native join")
            case.assertEqual(worker_destroyed_on, [], "worker destroyed before the native join")
        finally:
            gate.finish(destroyed)
        case.assertEqual(observer.completions, [(True, True)])
        # Never destroyed on its own thread (that deadlocks against the GIL): the
        # cleanup releases it on the GUI thread once the thread has been joined.
        wait_until(lambda: worker_ref() is None, "worker was not released after the join")
        case.assertEqual(worker_destroyed_on, [threading.get_ident()])
    elif scenario == "content-loading":
        from services.catalog import ALL_GROUP_ID, CatalogService, WorkerFactory
        from services.workers import M3ULoaderWorker

        class Providers:
            """The ProviderManager surface CatalogService reads; the playlist is never fresh."""

            def __init__(self):
                self.providers = [{"name": "Local", "type": "M3UPLAYLIST", "url": playlist}]
                self.current_provider = self.providers[0]
                self.current_provider_content = {}
                self.headers = {}

            def is_content_stale(self, content_type):
                return True

            def mark_content_fresh(self, content_type):
                pass

            def save_provider(self):
                pass

        class GatedM3ULoaderWorker(M3ULoaderWorker):
            def run(self):
                # Hold native teardown open once the real worker has finished.
                QThread.currentThread().finished.connect(gate.block, Qt.DirectConnection)
                super().run()

        class Factory(WorkerFactory):
            """The production factory; the last load's worker holds its thread's teardown."""

            gated = False
            last = None

            def m3u(self, url, verify_ssl, prefer_https):
                if not self.gated:
                    return super().m3u(url, verify_ssl, prefer_https)
                self.last = GatedM3ULoaderWorker(url, verify_ssl=verify_ssl)
                return self.last

        config = SimpleNamespace(
            selected_provider_name="Local", prefer_https=False, ssl_verify=True, channel_epg=False
        )
        epg = SimpleNamespace(get_programs_for_channel=lambda channel, start, limit: [])
        factory = Factory()
        # Only the provider and EPG stand-ins are fakes: the worker factory, the JobRunner
        # threads, signal delivery and job retirement are production code.
        consumer = CatalogService(config, Providers(), epg, factory=factory)
        loads, names, errors, on_gui = [], [()], [], []

        def loaded(section):
            loads.append(section)
            names[0] = tuple(item["name"] for item in consumer.items(section, ALL_GROUP_ID))
            on_gui.append(QThread.currentThread() == app.thread())

        consumer.sectionReady.connect(loaded)
        consumer.sectionFailed.connect(lambda section, message: errors.append(message))
        for number in range(512):
            consumer.request_section("live")
            wait_until(
                lambda: len(loads) == number + 1 and not has_pending_threads(),
                f"local playlist load {number + 1} did not complete",
            )
        case.assertEqual(names[0], ("First channel", "Second channel"))
        case.assertTrue(all(on_gui))
        case.assertFalse(consumer.is_loading())
        case.assertEqual(errors, [])

        # Destroy the consumer (and its JobRunner) after its real result arrives but before
        # native cleanup can finish. Its job list must not be the sole owner.
        gate = TeardownGate()
        destroyed = threading.Event()
        consumer_destroyed = threading.Event()
        factory.gated = True
        consumer.destroyed.connect(lambda *_: consumer_destroyed.set())
        consumer.request_section("live")
        worker = factory.last
        factory.last = None
        thread = worker.thread()
        thread.destroyed.connect(lambda *_: destroyed.set())
        thread_ref, worker_ref = weakref.ref(thread), weakref.ref(worker)
        try:
            wait_until(
                lambda: gate.entered.is_set() and len(loads) == 513,
                "last playlist result did not reach deferred worker deletion",
            )
            consumer.deleteLater()
            del consumer, thread, worker
            gc.collect()
            wait_until(consumer_destroyed.is_set, "consumer was not destroyed")
            gate.assert_pending(thread_ref, worker_ref)
        finally:
            gate.finish(destroyed)
    elif scenario == "jobrunner-churn":
        import faulthandler

        from PySide6.QtCore import Signal
        from PySide6.QtTest import QTest

        from services.jobs import JobRunner

        # A deadlock exits with a stack dump instead of waiting for the parent's timeout.
        faulthandler.dump_traceback_later(45, exit=True)

        class Worker(QObject):
            finished = Signal(object)
            error = Signal(str)

            def run(self):
                self.finished.emit(1)

        class Churn(QObject):
            ping = Signal()

            @Slot()
            def pong(self):
                pass

        runner = JobRunner()
        delivered = []
        rounds, per_round = 60, 8
        for _ in range(rounds):
            for _ in range(per_round):
                runner.run_worker(Worker(), lambda _context, payload: delivered.append(payload))
            # GUI-thread churn: destroy connected Python-created QObjects while jobs end.
            churn = [Churn() for _ in range(40)]
            for sender, receiver in zip(churn, churn[1:]):
                sender.ping.connect(receiver.pong)
            for item in churn:
                item.deleteLater()
            del churn
            QTest.qWait(5)
        wait_until(
            lambda: runner.pending() == 0 and len(delivered) == rounds * per_round,
            "jobs did not all deliver and retire",
        )
        faulthandler.cancel_dump_traceback_later()
    else:
        raise ValueError(f"Unknown scenario: {scenario}")

    case.assertFalse(slot_errors, "\n".join(slot_errors))
    print(f"completed:{scenario}", flush=True)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--scenario":
        run_child(sys.argv[2], sys.argv[3])
    else:
        unittest.main()
