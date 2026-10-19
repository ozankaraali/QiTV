"""A job runner for tests (a helper, not a test module)."""

from typing import Any, Callable, Optional

from PySide6.QtCore import QObject

from services.jobs import _WorkerGuard


class ImmediateRunner:
    """Runs a worker synchronously on the calling thread with direct signals, guarded as
    JobRunner guards it (one terminal signal, a raise reported as the generic failure)."""

    def run_worker(
        self,
        worker: QObject,
        on_finished: Callable,
        on_error: Optional[Callable] = None,
        context: Any = None,
    ) -> QObject:
        worker.finished.connect(lambda payload: on_finished(context, payload))
        if on_error is not None:
            worker.error.connect(lambda message: on_error(context, message))
        _WorkerGuard(worker).run()
        return worker

    def pending(self) -> int:
        return 0
