"""The cycle collector runs on the GUI thread only (services.gc_guard).

CPython collects on whichever thread allocates past the threshold, so a worker thread freed
GUI QObjects held in reference cycles, and once hung against the GUI thread (a QObject's
destructor waits for the GIL while it holds a signal-slot mutex the GUI thread wants).
"""

import gc
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest import mock
import weakref

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QApplication
from timer_fixtures import wait_until

from services.gc_guard import FULL_EVERY, GcGuard


class Owner(QObject):
    """A Python-created QObject in a cycle (its __dict__ holds its own bound method) that owns
    a timer: only the collector frees it."""

    def __init__(self) -> None:
        super().__init__()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.again = self.tick

    def tick(self) -> None:
        pass


def allocate(kept: list, count: int = 200_000) -> None:
    """What a busy worker does (an import, a parse): it makes container objects and keeps them."""
    for i in range(count):
        kept.append((i, [i]))


class GcGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.guard = GcGuard()
        self.addCleanup(self.guard.stop)
        gc.collect()

    def drop_a_cycle(self) -> list[int]:
        """Drop an Owner; the list gets the thread that frees it."""
        freed_on: list[int] = []
        owner = Owner()
        self.ref = weakref.ref(owner, lambda _ref: freed_on.append(threading.get_ident()))
        del owner
        return freed_on

    def run_worker(self) -> list:
        kept: list = []
        worker = threading.Thread(target=allocate, args=(kept,))
        worker.start()
        worker.join()
        return kept

    def test_without_the_guard_a_worker_frees_a_gui_cycle(self):
        """Why the guard exists: the automatic collector frees it on the worker thread."""
        freed_on = self.drop_a_cycle()
        self.run_worker()
        self.assertEqual(len(freed_on), 1)
        self.assertNotEqual(freed_on[0], threading.get_ident())

    def test_with_the_guard_the_gui_thread_frees_it(self):
        self.guard.start()
        freed_on = self.drop_a_cycle()
        kept = self.run_worker()
        self.assertEqual(freed_on, [])  # the worker ran no collection
        self.assertTrue(wait_until(lambda: bool(freed_on)))  # the guard's timer did
        self.assertEqual(freed_on, [threading.get_ident()])
        del kept

    def test_start_turns_the_automatic_collector_off_and_stop_back_on(self):
        self.assertTrue(gc.isenabled())
        self.guard.start()
        self.assertFalse(gc.isenabled())
        self.guard.stop()
        self.assertTrue(gc.isenabled())

    def test_collects_by_the_collectors_own_thresholds(self):
        now = [1000.0]
        guard = GcGuard(clock=lambda: now[0])
        collected: list[int] = []
        due = [
            ((1999, 0, 0), -1),  # under threshold 0: nothing yet
            ((2001, 0, 0), 0),
            ((2001, 11, 0), 1),
            ((2001, 11, 11), 1),  # a full collection waits for FULL_EVERY
        ]
        with (
            mock.patch.object(gc, "get_threshold", return_value=(2000, 10, 10)),
            mock.patch.object(gc, "collect", side_effect=lambda g: collected.append(g) or 0),
        ):
            for count, generation in due:
                with mock.patch.object(gc, "get_count", return_value=count):
                    self.assertEqual(guard.collect(), generation)
            now[0] += FULL_EVERY
            with mock.patch.object(gc, "get_count", return_value=(2001, 11, 11)):
                self.assertEqual(guard.collect(), 2)
                self.assertEqual(guard.collect(), 1)  # and then not again before FULL_EVERY
        self.assertEqual(collected, [0, 1, 1, 2, 1])

    def test_gui_cycles_freed_while_a_worker_allocates_never_hang(self):
        """The hang itself: GUI QObjects in cycles, a worker allocating, menus being built."""
        root = Path(__file__).resolve().parents[1]
        path = os.pathsep.join([str(root), str(root / "tests")])
        try:
            done = subprocess.run(
                [sys.executable, "-X", "faulthandler", str(Path(__file__).resolve()), "--churn"],
                cwd=root,
                env=dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONPATH=path),
                capture_output=True,
                text=True,
                errors="replace",
                timeout=60,
            )
        except subprocess.TimeoutExpired as error:
            self.fail(f"hung: stdout={error.stdout!r}, stderr={error.stderr!r}")
        self.assertEqual(done.returncode, 0, f"stdout:\n{done.stdout}\nstderr:\n{done.stderr}")
        self.assertIn("completed", done.stdout)


def churn(seconds: float = 2.0) -> None:
    """Drop QObjects in cycles whose children receive signals, while a worker thread allocates
    and the GUI thread builds menus (QObject::connect inside Qt, the GIL held). Without the
    guard the worker's collection deletes them and hangs within a second or so."""
    import faulthandler
    import time

    from PySide6.QtCore import Signal
    from PySide6.QtWidgets import QMenuBar

    faulthandler.dump_traceback_later(30, exit=True)  # a deadlock exits with a stack dump
    app = QApplication([])
    guard = GcGuard(app)
    guard.start()

    class Sender(QObject):
        ping = Signal()

    senders = [Sender() for _ in range(8)]

    def owner(sender: Sender) -> None:
        dropped = Owner()
        child = QObject(dropped)
        sender.ping.connect(child.deleteLater)  # the child receives a Python sender's signal

    stop = threading.Event()

    def worker() -> None:
        kept: list = []
        while not stop.is_set():
            if len(kept) > 200_000:
                kept.clear()
            allocate(kept, 300)
            time.sleep(0.0005)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    bar = QMenuBar()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for sender in senders:
            owner(sender)
            bar.addMenu("Menu")
        if len(bar.actions()) > 200:
            bar.clear()
        app.processEvents()  # the guard's timer
    stop.set()
    thread.join()
    faulthandler.cancel_dump_traceback_later()
    print("completed", flush=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["--churn"]:
        churn()
    else:
        unittest.main()
