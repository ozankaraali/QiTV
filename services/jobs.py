"""Run QObject workers off the GUI thread; deliver their results on it."""

import logging
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, Qt, QThread, Slot

from services.redact import safe_exc, safe_traceback
from services.thread_cleanup import ThreadCleanup

logger = logging.getLogger(__name__)

FAILED_MESSAGE = "The background task failed"
NO_RESULT_MESSAGE = "The background task ended without a result"


class Cancelled(Exception):
    """A job stopped early because its runner cancelled it (JobRunner.cancel_all)."""


def cancel_requested() -> bool:
    """True inside a job whose runner asked its jobs to stop (the window is closing).

    Long jobs (an XMLTV download, an STB crawl) check it between steps and raise Cancelled.
    Always False on the GUI thread.
    """
    return QThread.currentThread().isInterruptionRequested()


class _WorkerGuard(QObject):
    """Runs a worker and guarantees one terminal signal (finished or error) and a quit.

    Lives on the worker thread (or the caller's thread, in the tests' ImmediateRunner), so
    the terminal-signal flag is updated on the same thread that runs the worker.
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
        except Exception as e:
            # requests exceptions repeat the URL, credentials and all: the text and the
            # traceback's message never reach the log.
            logger.error(
                "Background task %s raised %s", type(self._worker).__name__, type(e).__name__
            )
            logger.debug("Background task failure:\n%s", safe_traceback(e))
            if not self._terminal:
                self._worker.error.emit(FAILED_MESSAGE)
        else:
            if not self._terminal:
                self._worker.error.emit(NO_RESULT_MESSAGE)
        finally:
            if self._thread is not None:
                self._thread.quit()


class _Relay(QObject):
    """Delivers one job's terminal signal on the GUI thread, with the job's context.

    Created on the calling (GUI) thread. The runner keeps it alive until it has
    delivered, so a queued call is never discarded by garbage collection.
    """

    def __init__(self, context: Any, on_finished: Callable, on_error: Optional[Callable], done):
        super().__init__()
        self._context = context
        self._on_finished = on_finished
        self._on_error = on_error
        self._done = done
        self._delivered = False

    def _deliver(self, callback: Optional[Callable], value: object) -> None:
        if self._delivered:
            return
        self._delivered = True
        try:
            if callback is not None:
                callback(self._context, value)
        except Exception as e:
            # The message can name a provider URL: the type and the frames only.
            logger.error("Job callback raised %s:\n%s", safe_exc(e), safe_traceback(e))
        finally:
            self._done(self)

    @Slot(object)
    def finished(self, payload: object) -> None:
        self._deliver(self._on_finished, payload)

    @Slot(str)
    def error(self, message: str) -> None:
        self._deliver(self._on_error, message)


class JobRunner(QObject):
    """Owns background jobs until their native threads are joined (see ThreadCleanup).

    Callbacks are invoked as ``on_finished(context, payload)`` / ``on_error(context,
    message)`` on the GUI thread, exactly once per job.
    """

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._jobs: dict[QThread, _WorkerGuard] = {}
        self._relays: set[_Relay] = set()

    def run_worker(
        self,
        worker: QObject,
        on_finished: Callable,
        on_error: Optional[Callable] = None,
        context: Any = None,
    ) -> QObject:
        relay = _Relay(context, on_finished, on_error, self._relays.discard)
        self._relays.add(relay)
        thread = QThread()
        guard = _WorkerGuard(worker, thread)
        guard.moveToThread(thread)
        worker.moveToThread(thread)
        thread.started.connect(guard.run)
        worker.finished.connect(relay.finished, Qt.QueuedConnection)
        worker.error.connect(relay.error, Qt.QueuedConnection)
        self._jobs[thread] = guard
        ThreadCleanup(thread, worker).finished.connect(self._retire)
        thread.start()
        return worker

    def _retire(self, thread: QThread) -> None:
        self._jobs.pop(thread, None)

    def cancel_all(self) -> None:
        """Ask every running job to stop (see cancel_requested); each still reports once."""
        for thread in list(self._jobs):
            thread.requestInterruption()

    def pending(self) -> int:
        return len(self._jobs)
