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

    def test_delayed_link_and_resume_cannot_restart_a_closing_window(self):
        self.run_scenario("closing-playback")

    def test_windows_update_stops_recording_and_waits_for_native_shutdown(self):
        self.run_scenario("windows-update")

    def test_windows_update_launch_failure_is_reported_before_quitting(self):
        self.run_scenario("windows-update-error")


def run_child(scenario, playlist):
    # Keep QApplication, production imports, and crash-prone threads out of the
    # unittest runner. Every scenario uses only isolated configuration/fixtures.
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
    elif scenario in ("closing-playback", "windows-update", "windows-update-error"):
        from unittest.mock import patch

        from PySide6.QtCore import QProcess, Signal
        from PySide6.QtWidgets import QMainWindow, QMessageBox

        from channel_list import ChannelList
        from mpv_player import MpvPlayer
        import update_checker

        class Player(MpvPlayer):
            """Use a harmless stdin-driven child at the native MPV boundary."""

            def __init__(self):
                super().__init__(SimpleNamespace())
                self.starts = []
                self.quit_requested = False

            def _start(self):
                self.starts.append(self._pending.url)
                self._pending = None
                process = QProcess(self)
                self._process = process
                process.finished.connect(lambda code, status: self._finished(process, code, status))
                process.start(sys.executable, ["-c", "import sys; sys.stdin.read()"])

            def _begin_quit(self):
                # The test releases stdin separately, just as MPV can take time
                # to honor the IPC quit request while a recorder is stopping.
                self.quit_requested = True

        class Window(ChannelList):
            def __init__(self, player):
                # Omit catalog/UI construction; retain real playback and closure.
                QMainWindow.__init__(self)
                self.app = app
                self.player = player
                self._closing = False
                self._shutdown_complete = False
                self._bg_jobs = []
                self._provider_setup_running = False
                self._current_content_id = None
                self._pending_link_ctx = None
                self.content_type = "itv"
                self.provider_manager = SimpleNamespace(current_provider={"type": "STB"})
                self.config_manager = SimpleNamespace(
                    play_in_vlc=False,
                    play_in_mpv=False,
                    save_window_settings=lambda *_: None,
                )
                self.image_manager = SimpleNamespace(save_index=lambda: None)
                self.epg_manager = SimpleNamespace(save_index=lambda: None)
                self.content_refresh_timer = QTimer(self)
                self.refresh_on_air_timer = QTimer(self)

            def cancel_content_loading(self):
                pass

            def stop_image_loading(self):
                pass

            def unlock_ui_after_loading(self):
                pass

        player = Player()
        window = Window(player)
        shutdowns = []
        window.shutdownFinished.connect(lambda: shutdowns.append(True))
        window.show()
        app.setQuitOnLastWindowClosed(True)
        timed_out = []
        watchdog = QTimer()
        watchdog.setSingleShot(True)
        watchdog.timeout.connect(lambda: (timed_out.append(True), app.quit()))
        watchdog.start(10000)
        quits = []
        app.aboutToQuit.connect(lambda: quits.append(True))

        if scenario == "closing-playback":

            class LinkWorker(QObject):
                finished = Signal(dict)

                @Slot()
                def run(self):
                    release_link.wait(5)
                    self.finished.emit({"link": "https://example.invalid/late"})

            # Ordinary stop/replay remains allowed before application closure.
            window._play_content("https://example.invalid/first")
            wait_until(lambda: player._process.state() == QProcess.Running, "child did not start")
            player.stop()
            window._play_content_with_position("https://example.invalid/replay", 12000)
            case.assertEqual(player._pending.url, "https://example.invalid/replay")
            player.stop()
            player._process.closeWriteChannel()
            wait_until(lambda: not player.is_running(), "initial child did not exit")
            starts = list(player.starts)

            release_link = threading.Event()
            thread = QThread()
            worker = LinkWorker()
            worker.moveToThread(thread)
            thread.started.connect(worker.run)
            worker.finished.connect(window._on_link_created, Qt.QueuedConnection)
            worker.finished.connect(thread.quit)
            window._bg_jobs.append((thread, worker))
            ThreadCleanup(thread, worker).finished.connect(lambda _: window._bg_jobs.clear())
            thread.start()

            def close_during_link():
                window.close()
                case.assertTrue(window._closing)
                case.assertTrue(window.isVisible(), "window did not wait for the link worker")
                # Already queued resume/autoplay callbacks must not launch or
                # schedule another provider worker either.
                window._play_content_with_position("https://example.invalid/resume", 12000)
                window._play_content_with_resume_check("https://example.invalid/dialog", {})
                window.play_item({"cmd": "https://example.invalid/autoplay"})
                release_link.set()

            QTimer.singleShot(0, close_during_link)
            try:
                app.exec()
            finally:
                release_link.set()
            case.assertFalse(timed_out, "late playback prevented application shutdown")
            case.assertEqual(player.starts, starts, "closing launched a fresh player")
            case.assertIsNone(player._pending)
            case.assertFalse(window.isVisible())
        else:

            class Recorder(QThread):
                def __init__(self):
                    super().__init__()
                    self.stop_requested = threading.Event()

                def run(self):
                    self.stop_requested.wait(5)

                def request_stop(self):
                    self.stop_requested.set()

            gate = TeardownGate()
            recorder = Recorder()
            recorder.finished.connect(gate.block, Qt.DirectConnection)
            destroyed = threading.Event()
            recorder.destroyed.connect(lambda *_: destroyed.set())
            player._recorder = recorder
            ThreadCleanup(recorder).finished.connect(player._recording_finished)
            recorder.start()
            # Set up the independent native process without stopping recording.
            player._pending = SimpleNamespace(url="https://example.invalid/live")
            player._start()
            wait_until(lambda: player._process.state() == QProcess.Running, "child did not start")
            launches = []
            warnings = []
            quit_snapshots = []
            app.aboutToQuit.connect(lambda: quit_snapshots.append((list(launches), list(warnings))))

            def dismiss_failure():
                for widget in app.topLevelWidgets():
                    if isinstance(widget, QMessageBox):
                        warnings.append(widget.text())
                        widget.accept()

            def launch(command, **kwargs):
                case.assertFalse(has_pending_threads(), "installer raced native thread teardown")
                case.assertFalse(player.is_running(), "installer raced the old player process")
                launches.append(command)
                if scenario == "windows-update-error":
                    QTimer.singleShot(0, dismiss_failure)
                    raise OSError("fixture launch failure")

            def install():
                update_checker._perform_windows_update(
                    "downloaded.exe", "https://example.invalid/release"
                )
                case.assertTrue(recorder.stop_requested.is_set(), "update never stopped recording")
                case.assertTrue(player.quit_requested)
                gate.assert_pending(weakref.ref(recorder), weakref.ref(recorder))
                case.assertEqual(launches, [])
                gate.finish(destroyed)
                case.assertTrue(player.is_running())
                case.assertEqual(launches, [], "installer did not wait for the native child")
                player._process.closeWriteChannel()

            with (
                patch.object(update_checker.subprocess, "Popen", side_effect=launch),
                patch.object(update_checker.subprocess, "DETACHED_PROCESS", 8, create=True),
                patch.object(
                    update_checker.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, create=True
                ),
            ):
                QTimer.singleShot(0, install)
                try:
                    app.exec()
                finally:
                    gate.release.set()
                    recorder.stop_requested.set()
            case.assertFalse(timed_out, "update deadlocked during shutdown")
            expected = [["downloaded.exe", "--replace", sys.executable]]
            case.assertEqual(launches, expected)
            expected_warnings = (
                ["Failed to launch the update."] if scenario.endswith("-error") else []
            )
            case.assertEqual(quit_snapshots, [(expected, expected_warnings)])
        watchdog.stop()
        case.assertEqual(quits, [True])
        from PySide6.QtGui import QCloseEvent

        window.closeEvent(QCloseEvent())
        case.assertEqual(shutdowns, [True], "repeated close emitted shutdown completion again")
        case.assertFalse(has_pending_threads())
    else:
        raise ValueError(f"Unknown scenario: {scenario}")

    case.assertFalse(slot_errors, "\n".join(slot_errors))
    print(f"completed:{scenario}", flush=True)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--scenario":
        run_child(sys.argv[2], sys.argv[3])
    else:
        unittest.main()
