"""Keep Python/Qt wrappers alive until native thread teardown has completed."""

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot

_pending: set["ThreadCleanup"] = set()


def has_pending_threads() -> bool:
    return bool(_pending)


class ThreadCleanup(QObject):
    """Register on the GUI thread before starting a QThread.

    QThread.finished precedes native teardown. Releasing either wrapper then can race
    Shiboken's disconnectNotify calls, so completion is only reported once a nonblocking
    native join succeeds.

    The worker is never deleted on its own thread: destroying a Python-created QObject
    there makes Qt call back into Python (disconnectNotify needs the GIL) while it holds
    a signal-slot mutex, which deadlocks against a GUI thread that holds the GIL and
    destroys an object guarded by the same mutex. The worker is kept referenced until the
    join and then released here, on the GUI thread.
    """

    finished = Signal(object)

    def __init__(self, thread: QThread, worker: QObject | None = None) -> None:
        super().__init__()
        self._thread = thread
        self._worker = worker if worker is not thread else None
        self._timer = QTimer(self)
        self._timer.setInterval(10)
        self._timer.timeout.connect(self._check_finished)
        thread.finished.connect(self._check_finished, Qt.QueuedConnection)
        # Not owned by the consumer: closing a dialog must not release a job.
        _pending.add(self)

    @Slot()
    def _check_finished(self) -> None:
        if not self._thread.wait(0):
            self._timer.start()
            return
        self._timer.stop()
        _pending.remove(self)
        self.finished.emit(self._thread)
        # The thread is joined: dropping the last reference destroys the worker here, on
        # the GUI thread, after consumers have released their own references.
        self._worker = None
        self._thread.deleteLater()
        self.deleteLater()
