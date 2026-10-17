import unittest

from services.pending_action import LOAD, RUN, WAIT, Pending, PendingAction


def act(provider, name="x", section=None):
    return Pending(provider, name, section)


class PendingActionTests(unittest.TestCase):
    def setUp(self):
        self.pending = PendingAction()

    def test_a_ready_provider_runs_at_once(self):
        self.assertEqual(self.pending.activate(act("P"), ready=True), RUN)
        self.assertIsNone(self.pending.action)

    def test_a_provider_not_ready_waits_and_runs_once_when_ready(self):
        a = act("Q")
        self.assertEqual(self.pending.activate(a, ready=False), WAIT)
        self.assertIs(self.pending.action, a)
        self.assertEqual(self.pending.on_ready("Q"), (RUN, a))
        self.assertIsNone(self.pending.action)
        self.assertEqual(self.pending.on_ready("Q"), (None, None))  # runs once

    def test_the_latest_activation_replaces_the_waiting_one(self):
        self.pending.activate(act("Q", "first"), ready=False)
        second = act("R", "second")
        self.assertEqual(self.pending.activate(second, ready=False), WAIT)
        self.assertEqual(self.pending.on_ready("Q"), (None, None))
        self.assertEqual(self.pending.on_ready("R"), (RUN, second))

    def test_another_provider_becoming_ready_leaves_it_waiting(self):
        a = act("Q")
        self.pending.activate(a, ready=False)
        self.assertEqual(self.pending.on_ready("P"), (None, None))
        self.assertIs(self.pending.action, a)
        self.assertEqual(self.pending.on_ready("Q"), (RUN, a))

    def test_a_ready_activation_drops_the_waiting_one(self):
        self.pending.activate(act("Q"), ready=False)
        self.assertEqual(self.pending.activate(act("P"), ready=True), RUN)
        self.assertEqual(self.pending.on_ready("Q"), (None, None))

    def test_cancel_forgets_the_action(self):
        self.pending.activate(act("Q"), ready=False)
        self.pending.cancel()
        self.assertIsNone(self.pending.action)
        self.assertEqual(self.pending.on_ready("Q"), (None, None))

    def test_failure_drops_the_action_of_that_provider(self):
        a = act("Q")
        self.pending.activate(a, ready=False)
        self.assertIsNone(self.pending.on_failed("R"))
        self.assertIs(self.pending.action, a)
        self.assertIs(self.pending.on_failed("Q"), a)
        self.assertIsNone(self.pending.action)
        self.assertEqual(self.pending.activate(act("Q"), ready=False), WAIT)

    def test_a_section_is_loaded_before_running(self):
        a = act("Q", section="series")
        self.pending.activate(a, ready=False)
        self.assertEqual(self.pending.on_ready("Q"), (LOAD, a))
        self.assertEqual(self.pending.on_ready("Q"), (None, None))  # a repeat keeps waiting
        self.assertIs(self.pending.action, a)
        self.assertIsNone(self.pending.on_section_ready("Q", "movies"))
        self.assertIsNone(self.pending.on_section_ready("P", "series"))
        self.assertIs(self.pending.on_section_ready("Q", "series"), a)
        self.assertIsNone(self.pending.action)

    def test_a_ready_provider_loads_a_section_it_lacks(self):
        a = act("Q", section="series")
        self.assertEqual(self.pending.activate(a, ready=True, loaded=False), LOAD)
        self.assertIs(self.pending.on_section_ready("Q", "series"), a)

    def test_a_ready_provider_with_its_section_runs_at_once(self):
        self.assertEqual(
            self.pending.activate(act("Q", section="series"), ready=True, loaded=True), RUN
        )

    def test_a_failed_section_drops_the_action(self):
        a = act("Q", section="series")
        self.pending.activate(a, ready=False)
        self.pending.on_ready("Q")
        self.assertIsNone(self.pending.on_section_failed("Q", "movies"))
        self.assertIsNone(self.pending.on_section_failed("P", "series"))
        self.assertIs(self.pending.on_section_failed("Q", "series"), a)
        self.assertIsNone(self.pending.on_section_ready("Q", "series"))


if __name__ == "__main__":
    unittest.main()
