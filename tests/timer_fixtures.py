"""Waiting for Qt timers in tests without racing the wall clock (not a test module).

``QTest.qWait(ms)`` stops processing events once ``ms`` of wall-clock time has passed. On a
loaded CI runner the product's timer can still be pending at that moment, so a test that waits
"interval plus a margin" fails at random. These helpers wait on the event loop instead.

Qt fires due timers in deadline order, so a probe timer started after a product timer with a
longer interval fires after it, however late the process gets to run. A coarse timer (the
QTimer default) may slip by 5% of its interval, so give the probe at least that much margin.
(That order holds for Qt's own timer list, on macOS and Linux. Windows runs timers natively; see
the note in ``timeline``.)
"""

from collections.abc import Callable
import time
from typing import Any

from PySide6.QtCore import QCoreApplication, Qt, QTimer
from PySide6.QtTest import QTest

__all__ = ["elapse", "timeline", "wait_until"]

_TIMEOUT_MS = 10_000


def timeline(*steps: tuple[int, Callable[[], Any]]) -> list[Any]:
    """Run each ``(ms, action)`` step ``ms`` after now, in timer order; return their results.

    Every step's timer starts now, so the steps keep their spacing however late each one runs.
    An action runs as its timer fires, before any timer due after it (a stalled process can
    find several due at once): use one to act before a product timer runs out, or to read a
    state that a later product timer would change.
    """
    results: dict[int, Any] = {}
    timers = []
    for k, (_ms, action) in enumerate(steps):
        timer = QTimer()
        timer.setSingleShot(True)
        timer.setTimerType(Qt.TimerType.PreciseTimer)
        timer.timeout.connect(lambda k=k, action=action: results.__setitem__(k, action()))
        timers.append(timer)
    for timer, (ms, _action) in zip(timers, steps):
        timer.start(ms)
    longest = max((ms for ms, _action in steps), default=0)
    if not wait_until(lambda: len(results) == len(steps), longest + _TIMEOUT_MS):
        raise AssertionError(f"timers of {[ms for ms, _action in steps]} ms did not all fire")
    # Windows delivers a precise timer as a posted event but a coarse one (the QTimer default)
    # as a low-priority WM_TIMER message, so a due product timer can trail the last probe there.
    QCoreApplication.processEvents()
    return [results[k] for k in range(len(steps))]


def elapse(ms: int, then: Callable[[], Any] = lambda: None) -> Any:
    """Run the event loop until a timer started now for ``ms`` has fired; return ``then()``.

    Every product timer due before it has fired (and its slots have run) by then. ``then`` runs
    as this timer fires, before any timer due after it.
    """
    return timeline((ms, then))[0]


def wait_until(predicate: Callable[[], object], timeout_ms: int = _TIMEOUT_MS) -> bool:
    """Process events until ``predicate()`` is true; False if ``timeout_ms`` passes first."""
    deadline = time.monotonic() + timeout_ms / 1000
    while not predicate():
        if time.monotonic() > deadline:
            return False
        QTest.qWait(5)
    return True
