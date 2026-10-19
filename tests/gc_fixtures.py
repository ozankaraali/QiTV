"""The app's garbage-collection policy for a test class (not a test module).

main() collects garbage cycles on the GUI thread only (services.gc_guard): a collection on
the live engine's supervisor thread deleted GUI objects there and hung the multiview tests
under load. Test classes whose objects meet such threads (a window, a live engine) run under
the same policy.
"""

import unittest

from services.gc_guard import GcGuard

__all__ = ["collect_on_the_gui_thread"]


def collect_on_the_gui_thread(case: type[unittest.TestCase]) -> None:
    """From setUpClass, with the QApplication made: a GcGuard for the class, stopped (the
    automatic collector back on) at its class cleanup."""
    guard = GcGuard()
    guard.start()
    case.addClassCleanup(guard.stop)
