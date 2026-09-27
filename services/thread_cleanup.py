"""Keep Python/Qt wrappers alive until native thread teardown has completed."""

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot

_pending: set["ThreadCleanup"] = set()


def has_pending_threads() -> bool:
    return bool(_pending)


class ThreadCleanup(QObject):
    """Register on the GUI thread before starting a QThread.

    QThread.finished precedes deferred QObject deletion and native teardown.
    Releasing either wrapper then can race Shiboken's disconnectNotify calls.
    Only report completion once a nonblocking native join succeeds.
    """

    finished = Signal(object)

    def __init__(self, thread: QThread, worker: QObject | None = None) -> None:
        super().__init__()
        self._thread = thread
        self._worker = worker
        self._timer = QTimer(self)
        self._timer.setInterval(10)
        self._timer.timeout.connect(self._check_finished)
        if worker is not None and worker is not thread:
            thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self._check_finished, Qt.QueuedConnection)
        # Not owned by the consumer: closing a dialog must not release a job.
        _pending.add(self)

    @Slot()
    def _check_finished(self) -> None:
        if not self._thread.wait(0):
            self._timer.start()
            return
        self._worker = None
        self._timer.stop()
        _pending.remove(self)
        self.finished.emit(self._thread)
        self._thread.deleteLater()
        self.deleteLater()
