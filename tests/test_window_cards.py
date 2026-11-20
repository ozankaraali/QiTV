"""QiTV's own questions and messages outside Settings (2.13.1): one at a time, on the page that
shows, once the window shows; each answers through its callback, Back with its own answer."""

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from timer_fixtures import elapse, wait_until

from services.i18n import translate
from ui import window_cards
from ui.keymap import Action
from ui.overlays.progress_card import BAR, ProgressCard
from ui.overlays.settings_card import CANCEL, OK, OK_TEXT, Button
from ui.window_cards import WindowCards

GO = "go"


class WindowCardsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = QWidget()
        self.window.resize(1280, 720)
        self.addCleanup(self.window.deleteLater)
        self.addCleanup(self.window.close)
        self.tv = QWidget(self.window)
        self.tv.resize(1280, 720)
        self.library = QWidget(self.window)
        self.library.resize(1280, 720)
        self.in_library = False
        self.closing = False
        self.prepared: list[str] = []
        self.cards = WindowCards(
            ProgressCard(self.tv),
            ProgressCard(self.library),
            window=self.window,
            library_shown=lambda: self.in_library,
            closing=lambda: self.closing,
            before_tv=lambda: self.prepared.append("tv"),
            parent=self.window,
        )
        self.answers: list[str] = []
        self.addCleanup(window_cards.install, None)

    def ask(self, title="Delete it?", *, default=CANCEL, back=CANCEL):
        buttons = [Button(CANCEL, "Cancel"), Button(GO, "Delete", danger=True)]
        self.cards.ask(title, "Its file goes.", buttons, default, self.answers.append, back=back)

    def show(self):
        self.window.show()
        elapse(0)

    def test_a_question_waits_for_the_window_then_shows_over_the_tv(self):
        self.ask()
        elapse(0)
        self.assertTrue(self.cards.tv_card.isHidden())  # the window has not shown yet
        self.assertFalse(self.cards.shown())
        self.show()
        card = self.cards.tv_card
        self.assertTrue(card.isVisible())
        self.assertTrue(self.cards.shown())
        self.assertEqual((card.title(), card.body()), ("Delete it?", "Its file goes."))
        self.assertEqual(card.focus(), ("button", CANCEL))  # its default first
        self.assertEqual(self.prepared, ["tv"])  # the mini player left, the ⋯ menu closed
        self.assertTrue(self.cards.library_card.isHidden())

    def test_one_question_at_a_time_the_next_from_the_event_loop(self):
        self.show()
        self.ask("First?")
        self.ask("Second?")
        card = self.cards.tv_card
        self.assertEqual(card.title(), "First?")
        card.handle(Action.NAV_RIGHT)
        card.handle(Action.NAV_ACTIVATE)
        self.assertEqual(self.answers, [GO])
        self.assertTrue(card.isHidden())  # never inside the answer
        elapse(0)
        self.assertEqual(card.title(), "Second?")
        self.assertTrue(card.isVisible())
        card.handle(Action.ESCAPE)
        self.assertEqual(self.answers, [GO, CANCEL])
        elapse(0)
        self.assertFalse(self.cards.shown())

    def test_back_and_the_dimmed_picture_give_the_questions_own_back_answer(self):
        self.show()
        for action in (Action.ESCAPE, Action.NAV_BACKSPACE, Action.PREVIOUS):
            with self.subTest(action=action):
                self.answers.clear()
                self.cards.ask(
                    "Quit?",
                    "",
                    [Button("keep", "Keep recording"), Button("quit", "Quit", danger=True)],
                    "keep",
                    self.answers.append,
                    back="keep",
                )
                self.assertEqual(self.cards.tv_card.focus(), ("button", "keep"))
                self.cards.tv_card.handle(Action.NAV_RIGHT)  # on Quit, then Back: kept
                self.cards.tv_card.handle(action)
                self.assertEqual(self.answers, ["keep"])
                elapse(0)
        self.answers.clear()
        self.ask(back=CANCEL)
        card = self.cards.tv_card
        QTest.mouseClick(card, Qt.MouseButton.LeftButton, pos=QPoint(4, 4))  # the dim
        self.assertEqual(self.answers, [CANCEL])
        elapse(QApplication.doubleClickInterval() + 80)

    def test_over_the_library_its_twin_shows_and_takes_the_keys(self):
        self.in_library = True
        self.show()
        self.assertFalse(self.cards.library_key(Action.NAV_RIGHT))  # nothing asked: not its
        self.ask()
        library = self.cards.library_card
        self.assertTrue(library.isVisible())
        self.assertTrue(self.cards.tv_card.isHidden())
        self.assertEqual(self.prepared, [])
        self.assertFalse(self.cards.library_key(Action.MUTE))  # the sound's keys go through
        self.assertTrue(self.cards.library_key(Action.NAV_RIGHT))
        self.assertEqual(library.focus(), ("button", GO))
        self.assertTrue(self.cards.library_key(Action.NAV_ACTIVATE))
        self.assertEqual(self.answers, [GO])
        self.assertFalse(self.cards.library_key(Action.NAV_ACTIVATE))  # answered: no more

    def test_a_question_goes_with_its_page_answered_once_as_back(self):
        self.in_library = True
        self.show()
        self.ask()
        self.in_library = False
        self.cards.page_changed("player")  # live TV came (the menu bar's Live TV)
        self.assertTrue(self.cards.library_card.isHidden())
        self.assertEqual(self.answers, [CANCEL])
        elapse(0)
        self.ask()  # over the TV now
        self.assertTrue(self.cards.tv_card.isVisible())
        self.in_library = True
        self.cards.page_changed("corner")
        self.assertTrue(self.cards.tv_card.isHidden())
        self.assertEqual(self.answers, [CANCEL, CANCEL])
        self.cards.page_changed("library")  # nothing more to answer
        self.assertEqual(self.answers, [CANCEL, CANCEL])

    def test_a_card_cancelled_by_its_host_answers_once(self):
        # TVController.cancel_cards (Settings, the Library or a stop coming): its Esc, then a
        # hide; one answer.
        self.show()
        self.ask()
        card = self.cards.tv_card
        card.handle(Action.ESCAPE)
        card.hide()
        self.assertEqual(self.answers, [CANCEL])
        self.cards.page_changed("library")
        self.assertEqual(self.answers, [CANCEL])

    def test_nothing_is_asked_once_the_window_closes(self):
        self.show()
        self.closing = True
        self.ask()
        elapse(0)
        self.assertFalse(self.cards.shown())
        self.assertTrue(self.cards.tv_card.isHidden())
        self.assertEqual(self.answers, [])

    def test_an_answer_while_the_window_closes_is_back(self):
        self.show()
        self.ask()
        self.cards.tv_card.handle(Action.NAV_RIGHT)
        self.closing = True
        self.cards.tv_card.handle(Action.NAV_ACTIVATE)
        self.assertEqual(self.answers, [CANCEL])

    def test_a_failing_answer_is_logged_and_the_next_question_comes(self):
        self.show()

        def broken(_key):
            raise RuntimeError("gone")

        self.cards.ask("First?", "", [Button(OK, "OK", accent=True)], OK, broken, back=OK)
        self.ask("Second?")
        with self.assertLogs("ui.window_cards", "ERROR"):
            self.cards.tv_card.handle(Action.NAV_ACTIVATE)
        elapse(0)
        self.assertEqual(self.cards.tv_card.title(), "Second?")

    def test_the_installed_windows_cards_ask_and_tell(self):
        self.show()
        window_cards.install(self.cards)
        window_cards.tell("No Updates", "You're running the latest version.")
        card = self.cards.tv_card
        self.assertTrue(card.isVisible())
        self.assertEqual(card.title(), "No Updates")
        self.assertEqual(
            [(b.key, b.text, b.accent) for b in card._buttons],
            [(OK, translate(OK_TEXT), True)],
        )
        self.assertEqual(card.focus(), ("button", OK))
        card.handle(Action.ESCAPE)
        self.assertTrue(card.isHidden())
        elapse(0)
        window_cards.ask("Go?", "", [Button(GO, "Go", accent=True)], GO, self.answers.append)
        card.handle(Action.ESCAPE)
        self.assertEqual(self.answers, [CANCEL])  # its back: Cancel, by default

    def test_with_no_window_nothing_is_asked(self):
        window_cards.install(None)
        with self.assertLogs("ui.window_cards", "INFO") as logs:
            window_cards.ask("Go?", "", [Button(GO, "Go")], GO, self.answers.append)
        self.assertEqual(self.answers, [])
        self.assertIn("no window", logs.output[0])
        window_cards.install(self.cards)
        self.cards.deleteLater()  # its window went
        elapse(0)
        with self.assertLogs("ui.window_cards", "INFO"):
            window_cards.ask("Go?", "", [Button(GO, "Go")], GO, self.answers.append)
        self.assertEqual(self.answers, [])

    def test_alternates_show_under_the_buttons_and_answer_too(self):
        self.show()
        mute = Button("mute", "Do not remind me about updates")
        self.cards.ask(
            "Update Available",
            "A new version is available!",
            [Button("skip", "Skip This Version"), Button(GO, "Download & Install", accent=True)],
            GO,
            self.answers.append,
            alternates=[mute],
        )
        card = self.cards.tv_card
        self.assertFalse(card.control_rect("alt", "mute").isEmpty())
        card.handle(Action.NAV_DOWN)
        self.assertEqual(card.focus(), ("alt", "mute"))
        card.handle(Action.NAV_ACTIVATE)
        self.assertEqual(self.answers, ["mute"])

    # ---- a progress card (an update's download) ----------------------------------------------

    def progress(self):
        return self.cards.progress(
            "Downloading Update",
            "Downloading update...",
            [Button("stop", "Cancel")],
            "stop",
            self.answers.append,
            back="hide",
        )

    def test_a_progress_card_moves_and_closes_unanswered(self):
        self.show()
        progress = self.progress()
        card = self.cards.tv_card
        self.assertTrue(card.isVisible())
        self.assertEqual(
            (card.title(), card.body()), ("Downloading Update", "Downloading update...")
        )
        self.assertEqual(card.progress(), 0.0)
        bar = card.control_rect(*BAR)
        self.assertFalse(bar.isEmpty())
        self.assertLess(bar.bottom(), card.control_rect("button", "stop").top())
        self.assertEqual(card.focus(), ("button", "stop"))
        progress.set(0.5, "Downloading update... 1.0 / 2.0 MB")
        self.assertEqual(card.progress(), 0.5)
        self.assertEqual(card.body(), "Downloading update... 1.0 / 2.0 MB")
        progress.set(1.5, "Downloading update... 2.0 / 2.0 MB")
        self.assertEqual(card.progress(), 1.0)  # never past its end
        self.ask("Update Now?")  # what comes after it waits for it
        self.assertEqual(card.title(), "Downloading Update")
        progress.close()  # done: it goes, answering nothing
        self.assertTrue(card.isHidden())
        self.assertEqual(self.answers, [])
        elapse(0)
        self.assertEqual(card.title(), "Update Now?")
        self.assertIsNone(card.progress())  # a question has no bar
        self.assertTrue(card.control_rect(*BAR).isEmpty())
        progress.set(0.7, "late")  # a closed one's news changes nothing
        progress.close()
        self.assertEqual(card.title(), "Update Now?")

    def test_progress_back_takes_the_card_away_and_its_cancel_stops(self):
        self.show()
        progress = self.progress()
        card = self.cards.tv_card
        card.handle(Action.ESCAPE)  # Back: only the card goes (the download goes on)
        self.assertEqual(self.answers, ["hide"])
        self.assertTrue(card.isHidden())
        progress.set(0.5, "Downloading update... 1.0 / 2.0 MB")
        self.assertTrue(card.isHidden())
        elapse(0)
        self.progress()
        card.handle(Action.NAV_ACTIVATE)  # its Cancel, focused
        self.assertEqual(self.answers, ["hide", "stop"])

    def test_a_progress_waiting_shows_where_it_got_to_and_over_the_library(self):
        self.in_library = True
        progress = self.progress()  # the window not shown yet
        progress.set(0.25, "Downloading update... 0.5 / 2.0 MB")
        self.show()
        card = self.cards.library_card
        self.assertTrue(card.isVisible())
        self.assertEqual(card.progress(), 0.25)
        self.assertEqual(card.body(), "Downloading update... 0.5 / 2.0 MB")
        self.assertTrue(self.cards.library_key(Action.NAV_BACKSPACE))
        self.assertEqual(self.answers, ["hide"])

    def test_a_progress_closed_before_it_showed_never_shows(self):
        progress = self.progress()
        progress.close()
        self.show()
        self.assertFalse(self.cards.shown())
        self.assertTrue(self.cards.tv_card.isHidden())

    def test_with_no_window_a_progress_shows_nothing(self):
        window_cards.install(None)
        with self.assertLogs("ui.window_cards", "INFO"):
            progress = window_cards.progress(
                "Downloading Update", "", [Button("stop", "Cancel")], "stop", self.answers.append
            )
        progress.set(0.5, "half")  # nothing to move, nothing raised
        progress.close()
        self.assertEqual(self.answers, [])


from tests.test_tv_window import WindowCase  # noqa: E402
from ui import update_dialogs  # noqa: E402
from ui.stream_question import KEEP_RECORDING, QUIT  # noqa: E402

QUESTION = "Channel 1 is recording. Quit and stop the recording?"


class WindowCardsWindowTests(WindowCase):
    """In the real window: an update's question over live TV has the keys (the drawer's: the
    arrows go between its answers, never seek or zap), the phone's too; over the Library its
    twin has them, and a recording's deletion is asked there."""

    def test_the_updates_quit_question_over_live_tv(self):
        self.show()
        drawer = self.wait_for_drawer()
        drawer.close_drawer()  # the player's keys: ← and → would seek, but for the card
        window = self.window
        answers: list[bool] = []
        update_dialogs.DIALOGS.confirm_stop_recordings(QUESTION, answers.append)
        card = window.window_cards.tv_card
        self.assertTrue(card.isVisible())
        self.assertEqual(card.body(), QUESTION)
        self.assertEqual(
            [(b.text, b.accent, b.danger) for b in card._buttons],
            [(translate(KEEP_RECORDING), False, False), (translate(QUIT), False, True)],
        )
        self.assertEqual(card.focus()[1], card._buttons[0].key)  # Keep recording first
        QTest.keyClick(window, Qt.Key.Key_Right)
        self.assertEqual(card.focus()[1], card._buttons[1].key)  # on Quit
        QTest.keyClick(window, Qt.Key.Key_Escape)  # Back: kept, Quit or not
        self.assertEqual(answers, [False])
        self.assertTrue(card.isHidden())
        elapse(50)
        self.assertTrue(drawer.isHidden())  # Esc was the card's alone; nothing came after it
        update_dialogs.DIALOGS.confirm_stop_recordings(QUESTION, answers.append)
        self.assertTrue(card.isVisible())
        window.remote_key(Qt.Key.Key_Right)  # the phone's arrows and OK
        window.remote_key(Qt.Key.Key_Return)
        self.assertEqual(answers, [False, True])
        elapse(0)
        self.assertFalse(window.window_cards.shown())

    def test_back_on_an_update_offer_is_not_now_never_skip(self):
        self.show()
        self.wait_for_drawer()
        info = {"version": "9.9.9", "url": "https://release.invalid/qitv", "download_url": ""}
        card = self.window.window_cards.tv_card
        with (
            mock.patch("services.update_checker.skip_version") as skip,
            mock.patch("services.update_checker.disable_update_reminders") as mute,
            mock.patch("services.system_open.open_url") as opened,
        ):
            for back in ("esc", "backspace", "dim"):
                with self.subTest(back=back):
                    update_dialogs.show_update_dialog(info, self.config, manual=False)
                    self.assertTrue(card.isVisible())
                    QTest.keyClick(self.window, Qt.Key.Key_Left)  # on Skip This Version
                    self.assertEqual(card.focus(), ("button", "skip"))
                    if back == "esc":
                        QTest.keyClick(self.window, Qt.Key.Key_Escape)
                    elif back == "backspace":
                        self.window.remote_key(Qt.Key.Key_Backspace)
                    else:
                        QTest.mouseClick(card, Qt.MouseButton.LeftButton, pos=QPoint(4, 4))
                        elapse(QApplication.doubleClickInterval() + 80)
                    self.assertTrue(card.isHidden())
                    elapse(0)
        skip.assert_not_called()  # skipping is a choice of its own, never Back
        mute.assert_not_called()
        opened.assert_not_called()
        self.assertEqual(self.config.skipped_update_version, "")

    def test_the_library_opening_answers_live_tvs_question_as_back(self):
        self.show()
        self.wait_for_drawer()
        answers: list[bool] = []
        update_dialogs.DIALOGS.confirm_quit_and_open(answers.append)
        self.assertTrue(self.window.window_cards.tv_card.isVisible())
        self.window.screens.open_recordings()  # the menu bar's View → Recordings
        self.assertEqual(answers, [False])  # Not Now
        self.assertTrue(self.window.window_cards.tv_card.isHidden())

    def test_over_the_library_and_a_recordings_deletion(self):
        self.show()
        self.wait_for_drawer()
        window = self.window
        window.screens.open_recordings()
        self.assertTrue(wait_until(window.screens.library_shown))
        cards = window.window_cards
        card = cards.library_card
        path = os.path.join(self.config_dir, "Channel 1.ts")
        with mock.patch.object(window.screens.recordings, "delete") as delete:
            window.screens.delete_recording({"path": path})
            self.assertTrue(card.isVisible())
            self.assertTrue(cards.tv_card.isHidden())
            self.assertIn("“Channel 1.ts”", card.body())
            self.assertEqual(
                [(b.key, b.danger) for b in card._buttons], [(CANCEL, False), ("delete", True)]
            )
            self.assertEqual(card.focus(), ("button", CANCEL))
            QTest.keyClick(window, Qt.Key.Key_Return)  # Cancel: kept
            delete.assert_not_called()
            window.screens.delete_recording({"path": path})
            QTest.keyClick(window, Qt.Key.Key_Right)
            QTest.keyClick(window, Qt.Key.Key_Return)
            delete.assert_called_once_with(path)
        elapse(0)
        update_dialogs.DIALOGS.up_to_date()  # an update's message shows there too
        self.assertTrue(card.isVisible())
        window.screens.back_to_tv()  # and goes with the Library
        self.assertTrue(card.isHidden())
        self.assertFalse(cards.shown())


if __name__ == "__main__":
    unittest.main()
