import unittest

from services.pending_action import LOAD, RUN, SWITCH, WAIT, Pending, PendingAction


def act(provider, name="x", section=None):
    return Pending(provider, name, section)


class PendingActionTests(unittest.TestCase):
    def setUp(self):
        self.pending = PendingAction()

    def test_the_current_provider_runs_at_once(self):
        self.assertEqual(self.pending.activate(act("P"), current="P", loading=False), RUN)
        self.assertIsNone(self.pending.action)

    def test_another_provider_switches_and_runs_when_ready(self):
        a = act("Q")
        self.assertEqual(self.pending.activate(a, current="P", loading=False), SWITCH)
        self.assertEqual(self.pending.target, "Q")
        self.assertEqual(self.pending.on_ready("Q"), (RUN, a))
        self.assertIsNone(self.pending.action)
        self.assertIsNone(self.pending.target)
        self.assertEqual(self.pending.on_ready("Q"), (None, None))  # runs once

    def test_activations_during_a_switch_wait_and_switch_only_for_a_new_target(self):
        self.pending.activate(act("Q", "first"), current="P", loading=False)
        # the provider manager already names Q while its setup runs
        second = act("Q", "second")
        self.assertEqual(self.pending.activate(second, current="Q", loading=True), WAIT)
        self.assertIs(self.pending.action, second)
        back = act("P", "back")
        self.assertEqual(self.pending.activate(back, current="Q", loading=True), SWITCH)
        self.assertEqual(self.pending.target, "P")
        self.assertEqual(self.pending.on_ready("P"), (RUN, back))

    def test_loading_started_elsewhere_holds_the_current_provider_too(self):
        a = act("P")
        self.assertEqual(self.pending.activate(a, current="P", loading=True), SWITCH)
        self.assertEqual(self.pending.on_ready("P"), (RUN, a))

    def test_a_different_provider_becoming_ready_drops_it(self):
        self.pending.activate(act("Q"), current="P", loading=False)
        self.assertEqual(self.pending.on_ready("R"), (None, None))
        self.assertIsNone(self.pending.action)
        self.assertIsNone(self.pending.target)
        self.assertEqual(self.pending.on_ready("Q"), (None, None))

    def test_a_source_switch_elsewhere_is_a_target(self):
        self.pending.switching("Q")
        self.assertEqual(self.pending.activate(act("Q"), current="P", loading=True), WAIT)

    def test_cancel_keeps_the_switch_target(self):
        self.pending.activate(act("Q"), current="P", loading=False)
        self.pending.cancel()
        self.assertIsNone(self.pending.action)
        self.assertEqual(self.pending.target, "Q")
        self.assertEqual(self.pending.activate(act("Q"), current="P", loading=True), WAIT)
        self.assertEqual(self.pending.on_ready("Q")[0], RUN)

    def test_failure_drops_the_action_of_that_provider(self):
        a = act("Q")
        self.pending.activate(a, current="P", loading=False)
        self.assertIsNone(self.pending.on_failed("R"))
        self.assertIs(self.pending.on_failed("Q"), a)
        self.assertIsNone(self.pending.action)
        self.assertIsNone(self.pending.target)
        self.assertEqual(self.pending.activate(act("Q"), current="P", loading=False), SWITCH)

    def test_a_section_is_loaded_before_running(self):
        a = act("Q", section="series")
        self.pending.activate(a, current="P", loading=False)
        self.assertEqual(self.pending.on_ready("Q"), (LOAD, a))
        self.assertEqual(self.pending.on_ready("Q"), (None, None))  # a repeat keeps waiting
        self.assertIs(self.pending.action, a)
        self.assertIsNone(self.pending.on_section_ready("movies", current="Q"))
        self.assertIsNone(self.pending.on_section_ready("series", current="P"))
        self.assertIs(self.pending.on_section_ready("series", current="Q"), a)
        self.assertIsNone(self.pending.action)

    def test_a_failed_section_drops_the_action(self):
        a = act("Q", section="series")
        self.pending.activate(a, current="P", loading=False)
        self.pending.on_ready("Q")
        self.assertIsNone(self.pending.on_section_failed("movies"))
        self.assertIs(self.pending.on_section_failed("series"), a)
        self.assertIsNone(self.pending.on_section_ready("series", current="Q"))

    def test_another_provider_ready_while_loading_a_section_drops_it(self):
        self.pending.activate(act("Q", section="series"), current="P", loading=False)
        self.pending.on_ready("Q")
        self.assertEqual(self.pending.on_ready("R"), (None, None))
        self.assertIsNone(self.pending.action)


if __name__ == "__main__":
    unittest.main()
