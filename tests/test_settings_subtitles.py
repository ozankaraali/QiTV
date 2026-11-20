"""Settings › Subtitles & AI captions in the drawer (plan 3): the speech model stepped, the space
AI captions' files take measured on a worker each time Settings opens, their deletion asked
first and done once nothing holds them, and OpenSubtitles' key and sign-in edited in place: the
key shows only its last four characters, the password only dots, and neither is said, given as
a tooltip, copied or logged. Over a real TV window; nothing reaches the network."""

import logging
import os
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from timer_fixtures import pause, wait_until

from services import _build_keys, settings_model as m
from services.config_manager import ConfigManager
from services.i18n import translate
from tests.test_settings_general import GeneralCase
from ui.captions_files import DELETE_QUESTION
from ui.captions_texts import size_text
from ui.keymap import Action
from ui.overlays.settings_card import CANCEL
from ui.overlays.settings_drawer import MASK, SECRET_DOTS
from ui.settings_subtitles import DELETE

KEY = "fictional0key0for0the0tests0WXYZ"
PASSWORD = "fictional-password"
MB_880 = 880_000_000


class FakeModels(SimpleNamespace):
    """services.captions.models' space_used and delete_all, over a number; the threads they
    ran on."""

    def space_used(self):
        self.threads.append(threading.current_thread())
        return self.size

    def delete_all(self):
        self.threads.append(threading.current_thread())
        self.deleted += 1
        self.size = 0


class FakeCaptions:
    """CaptionsController.release: off, then `then()` once nothing holds the files: later, as
    the real one polls for the captions process (and a cancelled download) to let go
    (`let_go`)."""

    def __init__(self):
        self.on = True
        self.released = 0
        self.waiting = []

    def release(self, then):
        self.on = False
        self.released += 1
        self.waiting.append(then)

    def let_go(self):
        waiting, self.waiting = self.waiting, []
        for then in waiting:
            then()


class Records(logging.Handler):
    """Every record logged while it listens, from any logger, at any level."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def text(self):
        return "\n".join(record.getMessage() for record in self.records)


class SubtitlesCase(GeneralCase):
    def setUp(self):
        super().setUp()
        self.models = FakeModels(size=0, deleted=0, threads=[])
        patch = mock.patch.dict(sys.modules, {"services.captions.models": self.models})
        patch.start()
        self.addCleanup(patch.stop)

    def open_at(self, section, key):
        """Settings at `section` once its rows are there (the files' rows come measured), the
        keys on its row `key`."""
        self.window.open_settings(section_key=section)
        self.assertTrue(wait_until(lambda: key in self.shown()), key)
        return super().open_at(section, key)

    def note(self, key):
        row = self.shown().get(key)
        return None if row is None else self.drawer._tail_text(row)

    def listen(self):
        records = Records()
        root = logging.getLogger()
        level = root.level
        root.addHandler(records)
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.setLevel, level)
        self.addCleanup(root.removeHandler, records)
        return records


class SubtitlesTests(SubtitlesCase):
    def test_speech_model_steps(self):
        context = m.Context(captions_space="Downloaded: 880 MB", captions_downloaded=True)
        section = {s.key: s for s in m.sections("Linux", (), context)}[m.SUBTITLES]
        self.assertEqual(
            [row.key for row in section.rows],
            [
                "ai_captions_model",
                m.CAPTIONS_FILES,
                m.DELETE_CAPTIONS,
                "opensubtitles_api_key",
                "opensubtitles_username",
                "opensubtitles_password",
                m.SIGN_OUT,
            ],
        )
        self.assertEqual(
            [row.text for row in section.rows],
            [
                "Speech model",
                "Downloaded files",
                "Delete downloaded files",
                "OpenSubtitles API key",
                "Username",
                "Password",
                "Sign out",
            ],
        )
        self.assertEqual(
            [row.kind for row in section.rows],
            [m.STEP, m.INFO, m.DANGER, m.TEXT, m.TEXT, m.TEXT, m.ACTION],
        )
        for row in section.rows:
            self.assertTrue(row.help, row.key)
            self.assertLessEqual(len(translate(row.help)), 90, row.key)
        # No files measured yet, or none of AI captions here: no Delete; none at all: no rows.
        measuring = {s.key: s for s in m.sections("Linux", (), m.Context(captions_space=""))}
        keys = [row.key for row in measuring[m.SUBTITLES].rows]
        self.assertIn(m.CAPTIONS_FILES, keys)
        self.assertNotIn(m.DELETE_CAPTIONS, keys)
        bare = {s.key: s for s in m.sections("Linux")}[m.SUBTITLES]
        self.assertNotIn(m.CAPTIONS_FILES, [row.key for row in bare.rows])
        # In the window: the old window's choices, a step applied and saved at once.
        row = self.open_at(m.SUBTITLES, "ai_captions_model")
        self.assertEqual(row.kind, m.STEP)
        self.assertEqual([c.value for c in row.choices], list(ConfigManager.AI_CAPTIONS_MODELS))
        self.assertEqual(
            [c.shown() for c in row.choices],
            ["Automatic", "Faster (0.6B)", "More accurate (1.7B)"],
        )
        self.assertEqual(
            self.drawer._help_text(row), "Automatic takes the more accurate one on a fast computer."
        )
        self.assertEqual(self.window.settings.value_text(row), "Automatic")
        self.assertFalse(self.window.settings.lit(row))
        self.key(Action.NAV_RIGHT)
        self.assertEqual(self.config.ai_captions_model, "small")
        self.key(Action.NAV_RIGHT)
        self.assertEqual(self.config.ai_captions_model, "large")
        self.assertTrue(self.window.settings.lit(row))
        self.config.save_config(wait=True)
        self.assertEqual(ConfigManager().ai_captions_model, "large")

    def test_the_key_row_says_whether_this_build_has_a_key(self):
        row = self.open_at(m.SUBTITLES, "opensubtitles_api_key")
        self.assertEqual(self.drawer._tail_text(row), "Paste your API key")  # none of its own
        with mock.patch.object(_build_keys, "OPENSUBTITLES_API_KEY", "built-in"):
            self.window.settings.refresh_sections()
            row = self.shown()["opensubtitles_api_key"]
            self.assertEqual(self.drawer._tail_text(row), "Optional: your own key")
            self.assertEqual(
                self.drawer._help_text(row),
                "This copy of QiTV has a key of its own: yours is optional, and is used instead"
                " when set.",
            )

    def test_space_is_measured_off_the_gui_thread(self):
        self.models.size = MB_880
        self.window.open_settings(section_key=m.SUBTITLES)
        self.assertTrue(wait_until(lambda: self.note(m.CAPTIONS_FILES) == "Downloaded: 880 MB"))
        self.assertEqual(self.shown()[m.CAPTIONS_FILES].kind, m.INFO)
        self.assertIn(m.DELETE_CAPTIONS, self.shown())
        self.assertTrue(self.models.threads)
        for thread in self.models.threads:
            self.assertIsNot(thread, threading.main_thread())
        # Opened again: measured again (a download may have come meanwhile), once.
        self.models.size = 0
        self.window.settings.close()
        self.assertTrue(wait_until(lambda: not self.window.settings.visible()))
        measured = len(self.models.threads)
        self.window.open_settings(section_key=m.SUBTITLES)
        self.assertTrue(wait_until(lambda: self.note(m.CAPTIONS_FILES) == "Nothing downloaded"))
        self.assertNotIn(m.DELETE_CAPTIONS, self.shown())  # nothing to delete
        self.assertEqual(len(self.models.threads), measured + 1)
        self.window.settings.refresh_sections()  # built again: not measured again
        self.assertTrue(wait_until(lambda: self.window.jobs.pending() == 0))
        self.assertEqual(len(self.models.threads), measured + 1)

    def test_delete_asks_and_releases(self):
        self.models.size = MB_880
        captions = FakeCaptions()
        with mock.patch.object(self.window, "captions", captions):
            row = self.open_at(m.SUBTITLES, m.DELETE_CAPTIONS)
            self.assertEqual(row.kind, m.DANGER)
            self.key(Action.NAV_ACTIVATE)  # asked first
            question = self.window.subtitle_rows.question
            self.assertTrue(self.window.settings.card_shown())
            self.assertEqual(question.title(), translate(DELETE_QUESTION, size=size_text(MB_880)))
            self.assertEqual(question.focus(), ("button", CANCEL))
            self.key(Action.NAV_ACTIVATE)  # Cancel: nothing goes
            self.assertFalse(self.window.settings.card_shown())
            self.assertEqual((captions.released, self.models.deleted), (0, 0))
            # Settings going with the question up deletes nothing either.
            self.key(Action.NAV_ACTIVATE)
            self.assertTrue(self.window.settings.card_shown())
            self.window.settings.close()
            self.assertTrue(wait_until(lambda: not self.window.settings.visible()))
            self.assertTrue(question.isHidden())
            self.assertEqual((captions.released, self.models.deleted), (0, 0))
            self.open_at(m.SUBTITLES, m.DELETE_CAPTIONS)
            self.key(Action.NAV_ACTIVATE)
            self.key(Action.NAV_RIGHT)
            self.assertEqual(question.focus(), ("button", DELETE))
            measured = len(self.models.threads)
            self.key(Action.NAV_ACTIVATE)
            self.assertNotIn(m.DELETE_CAPTIONS, self.shown())  # on its way
            self.assertEqual((captions.released, self.models.deleted), (1, 0))
            captions.let_go()
            self.assertTrue(wait_until(lambda: self.note(m.CAPTIONS_FILES) == "Nothing downloaded"))
        self.assertEqual((captions.released, self.models.deleted), (1, 1))
        self.assertFalse(captions.on)  # they stopped first
        self.assertIsNot(self.models.threads[measured], threading.main_thread())
        self.assertNotIn(m.DELETE_CAPTIONS, self.shown())
        self.assertEqual(self.drawer.path(), (m.SUBTITLES,))
        self.assertIsNotNone(self.drawer.focused_row())  # on its neighbour

    def test_settings_opened_while_a_delete_waits_keeps_it_on_its_way(self):
        # The captions process (or a cancelled download) lets go of the files only after a
        # while: Settings opened again meanwhile neither brings Delete back nor deletes twice.
        self.models.size = MB_880
        captions = FakeCaptions()
        with mock.patch.object(self.window, "captions", captions):
            self.open_at(m.SUBTITLES, m.DELETE_CAPTIONS)
            self.key(Action.NAV_ACTIVATE)
            self.key(Action.NAV_RIGHT)
            self.key(Action.NAV_ACTIVATE)  # Delete
            self.assertEqual(captions.released, 1)
            self.window.settings.close()
            self.assertTrue(wait_until(lambda: not self.window.settings.visible()))
            self.window.open_settings(section_key=m.SUBTITLES)
            self.assertTrue(wait_until(lambda: self.window.jobs.pending() == 0))
            pause(100)  # a measure's answer, had one been asked for, has landed
            self.assertNotIn(m.DELETE_CAPTIONS, self.shown())
            self.window.subtitle_rows.chosen(m.Row(m.DELETE_CAPTIONS, "", kind=m.DANGER))
            self.assertFalse(self.window.settings.card_shown())  # never asked again
            captions.let_go()
            self.assertTrue(wait_until(lambda: self.note(m.CAPTIONS_FILES) == "Nothing downloaded"))
        self.assertEqual((captions.released, self.models.deleted), (1, 1))
        self.assertNotIn(m.DELETE_CAPTIONS, self.shown())

    def test_secret_rows_mask_but_the_last_four(self):
        records = self.listen()
        self.config.opensubtitles_api_key = KEY
        self.config.opensubtitles_password = PASSWORD
        row = self.open_at(m.SUBTITLES, "opensubtitles_api_key")
        self.assertEqual((row.kind, row.secret, row.secret_tail), (m.TEXT, True, 4))
        self.drawer.grab()
        self.assertEqual(self.drawer._tail_text(row), MASK * SECRET_DOTS + "WXYZ")
        index = list(self.shown()).index(row.key)
        for point in (
            self.drawer._box("right", index).end.center(),
            self.drawer.title_rect(index).center(),
        ):
            self.assertNotIn("WXYZ", self.drawer.tooltip_at(point))
        self.assertNotIn("WXYZ", self.drawer._spoken())
        self.assertNotIn(MASK, self.drawer._spoken())
        password = self.shown()["opensubtitles_password"]
        self.assertEqual((password.secret, password.secret_tail), (True, 0))
        self.assertEqual(self.drawer._tail_text(password), MASK * len(PASSWORD))
        self.assertEqual(self.drawer._tail_text(self.shown()["opensubtitles_username"]), "")
        # Edited from the text itself, shown in dots; never copied out.
        self.key(Action.NAV_ACTIVATE)
        self.assertEqual(self.drawer._edit.text, KEY)
        self.drawer.grab()
        self.key(Action.SELECT_ALL)
        with mock.patch("ui.text_edit._copy") as copied:
            self.key(Action.COPY)
            self.key(Action.CUT)
        copied.assert_not_called()
        self.key(Action.NAV_TEXT, " fictional new key 1234 ")
        self.key(Action.NAV_ACTIVATE)
        self.assertEqual(self.config.opensubtitles_api_key, "fictionalnewkey1234")  # no spaces
        self.assertEqual(self.drawer._tail_text(row), MASK * SECRET_DOTS + "1234")
        # A key too short to hide most of: dots only, as many whatever its length.
        self.key(Action.NAV_ACTIVATE)
        self.key(Action.SELECT_ALL)
        self.key(Action.NAV_TEXT, "ab12")
        self.key(Action.NAV_ACTIVATE)
        self.assertEqual(self.drawer._tail_text(row), MASK * SECRET_DOTS)
        self.config.save_config(wait=True)
        self.assertEqual(ConfigManager().opensubtitles_api_key, "ab12")
        logged = records.text()
        for secret in ("WXYZ", "fictionalnewkey", "ab12", PASSWORD):
            self.assertNotIn(secret, logged)

    def test_sign_out_clears_the_login(self):
        self.config.opensubtitles_api_key = KEY
        self.config.opensubtitles_username = "harbour_viewer"
        self.config.opensubtitles_password = PASSWORD
        row = self.open_at(m.SUBTITLES, m.SIGN_OUT)
        self.assertEqual(row.kind, m.ACTION)
        self.assertEqual(self.drawer._help_text(row), "Clears the username and the password.")
        self.key(Action.NAV_ACTIVATE)
        self.assertEqual(
            (self.config.opensubtitles_username, self.config.opensubtitles_password), ("", "")
        )
        self.assertEqual(self.config.opensubtitles_api_key, KEY)  # the key stays
        shown = self.shown()
        self.assertEqual(self.drawer._tail_text(shown["opensubtitles_username"]), "")
        self.assertEqual(self.drawer._tail_text(shown["opensubtitles_password"]), "")
        self.config.save_config(wait=True)
        saved = ConfigManager()
        self.assertEqual((saved.opensubtitles_username, saved.opensubtitles_password), ("", ""))
        self.assertEqual(saved.opensubtitles_api_key, KEY)
        # Signed in again: the username kept without the spaces at its ends, as the old Save did.
        self.key(Action.NAV_UP)
        self.key(Action.NAV_UP)
        self.assertEqual(self.drawer.focused_row().key, "opensubtitles_username")
        self.key(Action.NAV_ACTIVATE)
        self.key(Action.NAV_TEXT, " harbour viewer ")
        self.key(Action.NAV_ACTIVATE)
        self.assertEqual(self.config.opensubtitles_username, "harbour viewer")


if __name__ == "__main__":
    unittest.main()
