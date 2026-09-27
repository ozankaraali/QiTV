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


def run_child(scenario, playlist):
    # Keep QApplication, production imports, and crash-prone threads out of the
    # unittest runner. No ChannelList, playback backend, or user config is needed.
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
            # QObject deferred destruction runs after QThread.finished, but is
            # still inside native thread teardown. Never leave it blocked on a
            # failing assertion or a GUI-side blocking wait regression.
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
        thread = QThread()
        worker = Worker()
        worker.moveToThread(thread)
        worker.destroyed.connect(gate.block, Qt.DirectConnection)
        thread.destroyed.connect(lambda *_: destroyed.set())
        thread.started.connect(worker.run)
        cleanup = ThreadCleanup(thread, worker)
        cleanup.finished.connect(observer.completed)
        thread_ref, worker_ref = weakref.ref(thread), weakref.ref(worker)
        thread.start()
        del cleanup, thread, worker
        try:
            wait_until(gate.entered.is_set, "worker deferred deletion was never reached")
            gate.assert_pending(thread_ref, worker_ref)
            case.assertEqual(observer.completions, [], "completion preceded native join")
        finally:
            gate.finish(destroyed)
        case.assertEqual(observer.completions, [(True, True)])
    elif scenario == "content-loading":
        from mixins.content_loading_mixin import ContentLoadingMixin
        from workers import M3ULoaderWorker

        class Consumer(QObject, ContentLoadingMixin):
            def __init__(self):
                super().__init__()
                self._bg_jobs = []
                self.content_loader = None
                self.config_manager = SimpleNamespace(ssl_verify=True, prefer_https=False)
                self.provider_manager = SimpleNamespace(current_provider={})
                self.loads = 0
                self.names = ()
                self.results_on_gui = True
                self.errors = []

            # Only view/render hooks are replaced; worker creation, signal
            # delivery, cancellation, and job retirement are production code.
            def cancel_content_render(self):
                pass

            def update_ui_on_loading(self, loading):
                self.loading = loading

            def statusBar(self):
                return SimpleNamespace(
                    showMessage=lambda message, duration: self.errors.append(message)
                )

            def _on_catalog_loaded(self, payload):
                self.loads += 1
                self.names = tuple(item["name"] for item in payload["contents"])
                self.results_on_gui &= QThread.currentThread() == app.thread()

        consumer = Consumer()
        for number in range(512):
            consumer.load_m3u_playlist(playlist)
            wait_until(
                lambda: consumer.loads == number + 1 and not has_pending_threads(),
                f"local playlist load {number + 1} did not complete",
            )
        case.assertEqual(consumer.names, ("First channel", "Second channel"))
        case.assertTrue(consumer.results_on_gui)
        case.assertFalse(consumer.loading)
        case.assertEqual(consumer.errors, [])

        # Close the consumer after its real result arrives but before native
        # cleanup can finish. The consumer's job list must not be the sole owner.
        gate = TeardownGate()
        destroyed = threading.Event()
        consumer_destroyed = threading.Event()
        worker = M3ULoaderWorker(playlist)
        worker.destroyed.connect(gate.block, Qt.DirectConnection)
        consumer.destroyed.connect(lambda *_: consumer_destroyed.set())
        consumer._start_content_worker(worker, consumer._on_catalog_loaded)
        thread = worker.thread()
        thread.destroyed.connect(lambda *_: destroyed.set())
        thread_ref, worker_ref = weakref.ref(thread), weakref.ref(worker)
        try:
            wait_until(
                lambda: gate.entered.is_set() and consumer.loads == 513,
                "last playlist result did not reach deferred worker deletion",
            )
            consumer.deleteLater()
            del consumer, thread, worker
            gc.collect()
            wait_until(consumer_destroyed.is_set, "consumer was not destroyed")
            gate.assert_pending(thread_ref, worker_ref)
        finally:
            gate.finish(destroyed)
    else:
        raise ValueError(f"Unknown scenario: {scenario}")

    case.assertFalse(slot_errors, "\n".join(slot_errors))
    print(f"completed:{scenario}", flush=True)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--scenario":
        run_child(sys.argv[2], sys.argv[3])
    else:
        unittest.main()
