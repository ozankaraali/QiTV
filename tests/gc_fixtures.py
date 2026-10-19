"""The app's garbage-collection policy in the tests.

main() collects garbage cycles on the GUI thread only (services.gc_guard): a collection on
the live engine's supervisor thread deleted GUI objects there and hung the multiview tests
under load. The test runner (scripts/run_tests.py) runs every test module under the same
policy once the module has made its QApplication (`CollectingResult`); test classes whose
objects meet such threads (a window, a live engine) also start it themselves
(`collect_on_the_gui_thread`), for a run of their module alone.
"""

import gc
import sys
import unittest

__all__ = ["CollectingResult", "collect_on_the_gui_thread"]


def collect_on_the_gui_thread(case: type[unittest.TestCase]) -> None:
    """From setUpClass, with the QApplication made: a GcGuard for the class, stopped (the
    automatic collector back on) at its class cleanup."""
    from services.gc_guard import GcGuard

    guard = GcGuard()
    guard.start()
    case.addClassCleanup(guard.stop)


class CollectingResult(unittest.TextTestResult):
    """The runner's results: from the first test that finds a QApplication made, a GcGuard
    collects garbage cycles on the GUI thread (by its timer while a test runs the event
    loop, and after each test, by the collector's own thresholds), as main() does. A module
    whose tests turn the collector on and off themselves sets `AUTOMATIC_GC = True`."""

    _guard = None  # the process's GcGuard, once started
    _automatic = False  # a module asked for the automatic collector

    def startTest(self, test: unittest.TestCase) -> None:
        self._follow(test)
        super().startTest(test)

    def stopTest(self, test: unittest.TestCase) -> None:
        super().stopTest(test)
        guard = CollectingResult._guard
        if guard is not None:
            gc.disable()  # a test that turned it on again (and off at its end) leaves it off
            guard.collect()
        else:
            self._follow(test)  # a module that makes its QApplication in setUp

    @staticmethod
    def _follow(test: unittest.TestCase) -> None:
        if CollectingResult._guard is not None or CollectingResult._automatic:
            return
        if getattr(sys.modules.get(type(test).__module__), "AUTOMATIC_GC", False):
            CollectingResult._automatic = True  # its process keeps the automatic collector
            return
        core = sys.modules.get("PySide6.QtCore")
        if core is None or core.QCoreApplication.instance() is None:
            return  # no Qt, or no application yet: nothing to guard
        from services.gc_guard import GcGuard

        guard = GcGuard()  # no parent: it outlives an application a module deletes
        guard.start()
        CollectingResult._guard = guard
