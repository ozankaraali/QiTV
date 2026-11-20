"""The provider check off the GUI thread, for Settings' Verify and the drawer's cards.

`ProviderCheck.start` hands a copy of the provider to a worker on a JobRunner thread, which
runs services.provider_check.check there; the answer comes back on the GUI thread as
`answered(request id, Outcome)`. Only the latest request answers: one started before it is
dropped, so a slow answer never lands on a form that has moved on.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QObject, Signal
from shiboken6 import isValid

from services import provider_check
from services.jobs import JobRunner


class ProviderCheckJob(QObject):
    """One check, run on the runner's thread: it emits the Outcome, plain data."""

    finished = Signal(object)
    error = Signal(str)

    def __init__(self, provider: dict, ssl_verify: bool, password: str = "") -> None:
        super().__init__()
        self._provider = provider
        self._ssl_verify = ssl_verify
        self._password = password  # held here only, for the sign-in

    def run(self) -> None:
        self.finished.emit(provider_check.check(self._provider, self._ssl_verify, self._password))


class ProviderCheck(QObject):
    """Checks a provider on a worker thread; `answered(request_id, outcome)` gives the answer
    to the latest `start` only."""

    answered = Signal(int, object)

    def __init__(self, runner: Any = None, parent: Optional[QObject] = None) -> None:
        """`runner` runs the jobs (a JobRunner of its own by default); pass one that outlives
        this object, as the TV window's, so a check on its way ends after a close."""
        super().__init__(parent)
        self._runner = runner if runner is not None else JobRunner(self)
        self._request = 0

    def start(self, provider: dict, ssl_verify: bool, password: str = "") -> int:
        """Check `provider` (copied: the caller may change its own); the request's id."""
        self._request += 1
        job = ProviderCheckJob(dict(provider), bool(ssl_verify), password)
        self._runner.run_worker(job, self._on_answer, self._on_error, context=self._request)
        return self._request

    def _on_answer(self, request_id: int, outcome: provider_check.Outcome) -> None:
        if not isValid(self) or request_id != self._request:
            return  # its asker is gone, or a later check was started
        self.answered.emit(request_id, outcome)

    def _on_error(self, request_id: int, _message: str) -> None:
        self._on_answer(request_id, provider_check.Outcome(False))
