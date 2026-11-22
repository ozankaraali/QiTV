"""An open that waits for a stopped recording's stream (2.13.4), for any controller test case.

`StreamWait` goes before the case's class: its question card, a scheduled recording that holds
a provider's only stream (`hold_the_stream`), Stop recording on the question (`stop_it`) and
the recording's stream back (`lets_go`)."""

from PySide6.QtWidgets import QWidget

from ui.keymap import Action
from ui.overlays.question_card import QuestionCard

WAITING = "sched:w@1"  # the scheduled recording's REC sign name, its stream's budget purpose


class StreamWait:
    def overlays(self, column):
        overlays = super().overlays(column)  # type: ignore[misc]
        self.wait_host = QWidget()
        self.wait_host.resize(1280, 720)
        self.wait_card = QuestionCard(self.wait_host)
        overlays.question = self.wait_card
        return overlays

    def hold_the_stream(self, provider, channel):
        """A scheduled recording of `channel` holds `provider`'s one stream."""
        self.wait_ticket = self.controller.budget.request(provider, 1, purpose=WAITING, queue=False)
        self.assertIsNotNone(self.wait_ticket)
        self.wait_stopped = []
        self.controller.scheduled_now = lambda: (
            [(WAITING, channel["name"], 5.0, channel)] if self.wait_ticket is not None else []
        )
        self.controller.stop_scheduled = self.wait_stopped.append

    def stop_it(self):
        """Stop recording, on the question shown: what asked waits for its stream."""
        self.assertTrue(self.wait_card.isVisibleTo(self.wait_host))
        self.controller.handle(Action.SEEK_FWD)  # to Stop recording
        self.controller.handle(Action.OK)
        self.assertEqual(self.wait_stopped, [WAITING])

    def lets_go(self):
        """The recording's stream is back (its session closed)."""
        ticket, self.wait_ticket = self.wait_ticket, None
        self.controller.budget.release(ticket)
