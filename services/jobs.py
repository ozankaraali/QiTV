"""Run QObject workers off the GUI thread; deliver their results on it."""

import logging
from typing import Callable, Optional

from PySide6.QtCore import QObject, Qt, QThread

from services.thread_cleanup import ThreadCleanup

logger = logging.getLogger(__name__)

FAILED_MESSAGE = "The background task failed"
NO_RESULT_MESSAGE = "The background task ended without a result"


class _WorkerGuard(QObject):
    """Runs a worker and guarantees one terminal signal (finished or error) and a quit.

    Lives on the worker thread (or the caller's thread for ImmediateRunner), so the
    terminal-signal flag is updated on the same thread that runs the worker.
    """

    def __init__(self, worker: QObject, thread: Optional[QThread] = None):
        super().__init__()
        self._worker = worker
        self._thread = thread
        self._terminal = False
        worker.finished.connect(self._mark_terminal)
        worker.error.connect(self._mark_terminal)

    def _mark_terminal(self, *_args: object) -> None:
        self._terminal = True

    def run(self) -> None:
        try:
            self._worker.run()
        except Exception:
            logger.exception("Background task raised")
            if not self._terminal:
                self._worker.error.emit(FAILED_MESSAGE)
        else:
            if not self._terminal:
                self._worker.error.emit(NO_RESULT_MESSAGE)
        finally:
            if self._thread is not None:
                self._thread.quit()


class JobRunner(QObject):
    """Owns background jobs until their native threads are joined (see ThreadCleanup).

    Callbacks must be bound methods of QObjects that live on the GUI thread: they are
    connected queued, so they always run there.
    """

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._jobs: dict[QThread, _WorkerGuard] = {}

    def run_worker(
        self,
        worker: QObject,
        on_finished: Callable,
        on_error: Optional[Callable] = None,
    ) -> QObject:
        thread = QThread()
        guard = _WorkerGuard(worker, thread)
        guard.moveToThread(thread)
        worker.moveToThread(thread)
        thread.started.connect(guard.run)
        worker.finished.connect(on_finished, Qt.QueuedConnection)
        if on_error is not None:
            worker.error.connect(on_error, Qt.QueuedConnection)
        self._jobs[thread] = guard
        ThreadCleanup(thread, worker).finished.connect(self._retire)
        thread.start()
        return worker

    def _retire(self, thread: QThread) -> None:
        self._jobs.pop(thread, None)

    def pending(self) -> int:
        return len(self._jobs)


class ImmediateRunner:
    """Test double: runs a worker synchronously on the calling thread with direct signals."""

    def run_worker(
        self,
        worker: QObject,
        on_finished: Callable,
        on_error: Optional[Callable] = None,
    ) -> QObject:
        worker.finished.connect(on_finished)
        if on_error is not None:
            worker.error.connect(on_error)
        _WorkerGuard(worker).run()
        return worker

    def pending(self) -> int:
        return 0
