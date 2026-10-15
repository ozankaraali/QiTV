"""Run QObject workers off the GUI thread; deliver their results on it."""

from typing import Callable, Optional

from PySide6.QtCore import QObject, Qt, QThread

from services.thread_cleanup import ThreadCleanup


class JobRunner(QObject):
    """Owns background jobs until their native threads are joined (see ThreadCleanup).

    Callbacks must be bound methods of QObjects that live on the GUI thread: they are
    connected queued, so they always run there.
    """

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._jobs: dict[QThread, QObject] = {}

    def run_worker(
        self,
        worker: QObject,
        on_finished: Callable,
        on_error: Optional[Callable] = None,
    ) -> QObject:
        thread = QThread()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(on_finished, Qt.QueuedConnection)
        if on_error is not None:
            worker.error.connect(on_error, Qt.QueuedConnection)
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)
        self._jobs[thread] = worker
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
        worker.run()
        return worker

    def pending(self) -> int:
        return 0
