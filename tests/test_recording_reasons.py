"""Why a scheduled recording failed is said in the user's language: a short reason for each
code the scheduler, the helper or the catalog gives, never the code or an English message."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from services import i18n, recording_schedule as rs
from services.catalog import NO_STREAM_MESSAGE
from services.live.refusals import STREAM_LIMIT
from tests import test_recording_scheduler as base
from ui.recording_scheduler import FAILURE_REASONS, NO_HELPER, failure_reason


class FailureReasonTests(unittest.TestCase):
    def setUp(self):
        self.assertEqual(i18n.set_language("tr"), "tr")
        self.addCleanup(i18n.set_language, "en")

    def test_each_code_is_a_translated_reason(self):
        for code, text in FAILURE_REASONS.items():
            with self.subTest(code=code):
                shown = failure_reason(code)
                self.assertEqual(shown, i18n.translate(text))
                self.assertNotEqual(shown, text)  # Turkish has it
                self.assertNotEqual(shown, code)

    def test_the_catalogs_and_the_providers_own_words_are_translated(self):
        for text in (NO_STREAM_MESSAGE, STREAM_LIMIT):
            with self.subTest(text=text):
                self.assertEqual(failure_reason(text), i18n.translate(text))
                self.assertNotEqual(failure_reason(text), text)
        helper = i18n.translate(NO_HELPER)  # said translated already
        self.assertEqual(failure_reason(helper), helper)

    def test_an_english_message_or_an_errors_text_is_the_stream_failing(self):
        for text in ("The channel could not be reached over HTTP.", "Read timed out (x.invalid)"):
            with self.subTest(text=text):
                self.assertEqual(failure_reason(text), failure_reason("failed"))


class ToastTests(unittest.TestCase):
    """The toast at the end of a programme that could not be recorded."""

    def setUp(self):
        base.SchedulerTests.setUp(self)  # type: ignore[arg-type]
        self.assertEqual(i18n.set_language("tr"), "tr")
        self.addCleanup(i18n.set_language, "en")

    def test_a_link_that_never_came_is_said_in_the_users_language(self):
        self.schedule.add_once(base.CHANNEL, base.programme("The News", 30))
        self.scheduler.tick()
        self.catalogs.linkFailed.emit(1, "")
        self.at(63, 1)
        (outcome,) = self.schedule.outcomes()
        self.assertEqual((outcome["state"], outcome["why"]), (rs.FAILED, "link"))  # the code
        [(text, kind)] = [toast for toast in self.toasts if toast[1] == "error"]
        self.assertIn(failure_reason("link"), text)
        self.assertNotIn("(link)", text)

    pump = base.SchedulerTests.pump
    at = base.SchedulerTests.at


if __name__ == "__main__":
    unittest.main()
