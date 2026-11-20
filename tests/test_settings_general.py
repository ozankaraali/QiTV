"""Settings › General, Files and Advanced in the drawer (plan 3): the old Settings window's
General tab, row by row, each step applied at once through its hook (the update check, TMDB,
Secure DNS, the detailed log, the global SSL check's providers), the file types told on a
worker and the image cache cleared on one once asked. Over a real TV window; nothing reaches
the network or the system's file types."""

import os
from pathlib import Path
import threading
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import requests
from timer_fixtures import wait_until

from services import file_types, i18n, settings_model as m, update_checker
from services.config_manager import ConfigManager
from services.file_types import RegisterResult, Status
from services.i18n import translate
from services.provider_manager import ProviderManager
from services.settings_texts import CLEAR_CACHE_QUESTION
from tests import test_tv_window as base
from tests.fixtures.local_provider import PROVIDER_NAME
from ui import file_types_settings as row_module
from ui.file_types_settings import FAILED_HINT, MAC_HINT, MUSIC_LABEL, VIDEOS_LABEL
from ui.keymap import Action
from ui.overlays.settings_card import CANCEL
from ui.overlays.settings_drawer import RESTART_NOTE
from ui.settings_general import (
    CLEAR,
    CLEARED_TEXT,
    DEFAULT_APPS,
    ELSEWHERE_TEXT,
    IN_QITV_TEXT,
    MUSIC,
    VIDEOS,
)

EXE = "/opt/QiTV/qitv"


def recorded(unavailable=()):
    """file_types.status as a packaged QiTV answers it, from the record; `unavailable` kinds
    cannot be registered."""

    def status(kind, config, frozen=None):
        if kind in unavailable:
            return Status.UNAVAILABLE
        return Status.REGISTERED if kind in config.file_types else Status.NOT_REGISTERED

    return status


class GeneralCase(base.WindowCase):
    def setUp(self):
        super().setUp()
        patch = mock.patch("ui.tv_window.OptionsDialog")  # never a modal window here
        patch.start()
        self.addCleanup(patch.stop)
        self.show()
        self.wait_for_drawer()

    @property
    def drawer(self):
        return self.window.settings_drawer

    def key(self, action, payload=""):
        self.assertTrue(self.window.settings.handle(action, payload), action)

    def open_at(self, section, key):
        """Settings at `section`, the keys on its row `key`."""
        self.window.open_settings(section_key=section)
        self.key(Action.NAV_RIGHT)
        for _ in range(len(self.drawer._right_rows()) + 1):
            row = self.drawer.focused_row()
            if row is not None and row.key == key:
                return row
            self.key(Action.NAV_DOWN)
        self.fail(f"no row {key}")

    def shown(self):
        return {row.key: row for row in self.drawer._right_rows()}


class GeneralTests(GeneralCase):
    def test_general_rows_and_language_restart_note(self):
        languages = (("en", "English"), ("de", "Deutsch"), ("tr", "Türkçe"))
        context = m.Context(languages=languages, system_language="Deutsch", tmdb_built_in=True)
        by_key = {s.key: s for s in m.sections("Linux", (), context)}
        general = by_key[m.GENERAL]
        self.assertEqual(
            [row.key for row in general.rows],
            ["language", "check_updates", "channel_logos", "tmdb_details", "tmdb_token"],
        )
        self.assertEqual(
            [row.text for row in general.rows],
            [
                "Language",
                "Allow Check for Updates",
                "Enable Channel Logos",
                "Film and series details from TMDB",
                "TMDB token",
            ],
        )
        language = general.rows[0]
        self.assertEqual((language.kind, language.restart, language.default), (m.STEP, True, None))
        self.assertEqual([c.value for c in language.choices], [None, "en", "de", "tr"])
        self.assertEqual(
            [c.shown() for c in language.choices],
            ["System default (Deutsch)", "English", "Deutsch", "Türkçe"],
        )
        token = general.rows[-1]
        self.assertEqual((token.kind, token.secret), (m.TEXT, True))
        self.assertEqual(token.placeholder, "Built in; or paste your own")
        source = {s.key: s for s in m.sections("Linux", (), m.Context(tmdb_built_in=False))}
        self.assertEqual(source[m.GENERAL].rows[-1].placeholder, "Your TMDB Read Access Token")
        for key in (m.GENERAL, m.FILES, m.ADVANCED):  # no longer the Settings window's
            self.assertEqual(by_key[key].legacy_tab, "", key)
            self.assertNotIn(key, m.LEGACY_TABS)
        self.assertEqual(
            [row.key for row in by_key[m.ADVANCED].rows],
            [
                "ssl_verify",
                "secure_dns",
                "detailed_log",
                "max_cache_image_size",
                m.CLEAR_IMAGE_CACHE,
            ],
        )
        for section in (general, by_key[m.ADVANCED]):
            for row in section.rows:
                self.assertTrue(row.help, row.key)
                self.assertLessEqual(len(row.help), 90, row.key)
                if row.restart:
                    self.assertNotIn("restart", row.help.lower(), row.key)
        # In the window: the languages QiTV has texts for, the system's first; the restart note
        # under the row; a step written at once, for the next start.
        row = self.open_at(m.GENERAL, "language")
        available = i18n.available_languages()
        self.assertEqual([c.value for c in row.choices], [None] + [code for code, _ in available])
        system = dict(available).get(i18n.system_language(), i18n.SOURCE_NAME)
        self.assertEqual(
            row.choices[0].shown(), translate("System default ({language})", language=system)
        )
        self.assertTrue(row.restart)
        self.assertIn(translate(RESTART_NOTE), self.drawer._help_text(row))
        self.assertIsNone(self.config.language)
        self.key(Action.NAV_RIGHT)
        self.assertEqual(self.config.language, available[0][0])
        self.config.save_config(wait=True)
        self.assertEqual(ConfigManager().language, available[0][0])

    def test_turning_update_checks_on_starts_a_check_off_the_gui_thread(self):
        self.assertFalse(self.config.check_updates)  # the fixture's config: off
        asked = []

        def offline(url, **kwargs):
            asked.append(threading.current_thread())
            raise requests.ConnectionError("offline")

        with (
            mock.patch.object(update_checker.requests, "get", side_effect=offline),
            self.assertLogs("services.update_checker", "WARNING"),
        ):
            self.open_at(m.GENERAL, "check_updates")
            self.key(Action.NAV_RIGHT)  # on: one check, as at a start
            self.assertTrue(self.config.check_updates)
            self.assertTrue(wait_until(lambda: asked and not update_checker._update_jobs))
            self.key(Action.NAV_RIGHT)  # off: none
            self.assertFalse(self.config.check_updates)
        self.assertEqual(len(asked), 1)
        self.assertIsNot(asked[0], threading.main_thread())
        self.assertEqual(update_checker._update_jobs, [])

    def test_tmdb_hook(self):
        tmdb = self.window.screens.tmdb
        with mock.patch.object(tmdb, "settings_changed") as changed:
            self.open_at(m.GENERAL, "tmdb_details")
            self.key(Action.NAV_RIGHT)
            self.assertFalse(self.config.tmdb_details)
            changed.assert_called_once_with()
            self.key(Action.NAV_DOWN)
            row = self.drawer.focused_row()
            self.assertEqual(row.key, "tmdb_token")
            self.key(Action.NAV_ACTIVATE)  # edited in place
            self.key(Action.NAV_TEXT, " eyJ.made up ")
            self.key(Action.NAV_ACTIVATE)
        self.assertEqual(self.config.tmdb_token, "eyJ.madeup")  # kept without its spaces
        self.assertEqual(changed.call_count, 2)  # a token of one's own may turn TMDB on
        self.assertNotIn("eyJ", self.drawer.tooltip_at(self.drawer.title_rect(4).center()))
        self.assertNotIn("eyJ", self.drawer._spoken())


class FileTypesTests(GeneralCase):
    def registering(self, result=RegisterResult(True, True)):
        """A register that records as the service does, and the threads it ran on."""
        self.threads = []

        def register(kind, config, *args, **kwargs):
            self.threads.append(threading.current_thread())
            if result:
                config.file_types = {**config.file_types, kind: EXE}
            return result

        return mock.patch.object(file_types, "register", side_effect=register)

    def join(self):
        for thread in threading.enumerate():
            if thread.name in (row_module.PRESS_THREAD, row_module.DEFAULT_APPS_THREAD):
                thread.join(5)

    def test_file_type_rows_show_status_and_register_on_a_worker(self):
        config = self.config
        saved = []
        save = config.save_config

        def saving(**kwargs):
            saved.append(threading.current_thread())
            return save(**kwargs)

        with (
            mock.patch.object(file_types, "status", side_effect=recorded({"music"})),
            mock.patch.object(row_module, "_system", return_value="darwin"),
            self.registering(),
        ):
            row = self.open_at(m.FILES, VIDEOS)
            self.assertEqual(list(self.shown()), [VIDEOS])  # music can't be: no row for it
            self.assertEqual((row.kind, row.text), (m.ACTION, VIDEOS_LABEL))
            self.assertEqual(row.note, translate(ELSEWHERE_TEXT))
            self.assertEqual(row.help, MAC_HINT)
            with mock.patch.object(config, "save_config", side_effect=saving):
                self.key(Action.NAV_ACTIVATE)
                self.assertTrue(
                    wait_until(lambda: self.shown()[VIDEOS].note == translate(IN_QITV_TEXT))
                )
            self.join()
        self.assertEqual(config.file_types, {"videos": EXE})
        self.assertEqual(len(self.threads), 1)
        self.assertIsNot(self.threads[0], threading.main_thread())
        self.assertEqual(saved, [threading.main_thread()])  # saved on the GUI thread
        self.assertEqual(self.drawer.focused_row().key, VIDEOS)  # the place kept

    def test_music_too_and_windows_default_apps(self):
        self.config.file_types = {"videos": EXE}
        with (
            mock.patch.object(file_types, "status", side_effect=recorded()),
            mock.patch.object(row_module, "_system", return_value="win32"),
            mock.patch.object(file_types, "open_default_apps") as default_apps,
            self.registering(),
        ):
            self.open_at(m.FILES, MUSIC)
            self.assertEqual(list(self.shown()), [VIDEOS, MUSIC, DEFAULT_APPS])
            self.assertEqual(self.shown()[MUSIC].text, MUSIC_LABEL)
            self.assertEqual(self.shown()[VIDEOS].note, translate(IN_QITV_TEXT))
            self.assertEqual(self.shown()[MUSIC].note, translate(ELSEWHERE_TEXT))
            self.key(Action.NAV_ACTIVATE)  # registered: Windows' Default Apps asks to confirm
            self.assertTrue(wait_until(lambda: self.shown()[MUSIC].note == translate(IN_QITV_TEXT)))
            self.join()
            self.assertEqual(default_apps.call_count, 1)
            self.key(Action.NAV_DOWN)
            self.assertEqual(self.drawer.focused_row().key, DEFAULT_APPS)
            self.key(Action.NAV_ACTIVATE)  # and the row opens it again
            self.join()
            self.assertEqual(default_apps.call_count, 2)
        # Unavailable (a run from source): Files says so, without rows.
        with mock.patch.object(file_types, "status", return_value=Status.UNAVAILABLE):
            self.window.open_settings(section_key=m.ADVANCED)
            self.window.open_settings(section_key=m.FILES)
        self.assertEqual(self.drawer._right_rows(), ())
        files = next(s for s in self.window.settings._sections if s.key == m.FILES)
        self.assertTrue(files.empty_text)

    def test_a_failed_press_is_said_and_changes_nothing(self):
        with (
            mock.patch.object(file_types, "status", side_effect=recorded()),
            mock.patch.object(row_module, "_system", return_value="linux"),
            self.registering(RegisterResult(False)),
        ):
            self.open_at(m.FILES, VIDEOS)
            self.key(Action.NAV_ACTIVATE)
            self.assertTrue(wait_until(lambda: (translate(FAILED_HINT), "error") in self.toasts))
            self.join()
            self.assertEqual(self.shown()[VIDEOS].note, translate(ELSEWHERE_TEXT))
        self.assertEqual(self.config.file_types, {})


class SslTests(GeneralCase):
    seed_fixture = True

    def provider_list(self):
        fixture = {
            "type": "M3UPLAYLIST",
            "name": PROVIDER_NAME,
            "url": str(Path(self.config_dir) / "fixture.m3u"),
        }
        return [
            fixture,
            {"type": "M3UPLAYLIST", "name": "Inherits", "url": "http://lists.example/tv.m3u"},
            {
                "type": "XTREAM",
                "name": "Own",
                "url": "http://panel.example:8080",
                "username": "demo",
                "password": "secret",
                "ssl_verify": True,
            },
        ]

    def test_global_ssl_change_refreshes_inheriting_providers(self):
        invalidated = []
        original = ProviderManager.invalidate_provider_cache

        def record(manager, name):
            invalidated.append(name)
            return original(manager, name)

        with (
            mock.patch.object(
                ProviderManager, "invalidate_provider_cache", autospec=True, side_effect=record
            ),
            mock.patch.object(self.window, "set_provider") as set_provider,
        ):
            self.open_at(m.ADVANCED, "ssl_verify")
            self.key(Action.NAV_RIGHT)
            self.assertFalse(self.config.ssl_verify)
            # Only the providers that follow the global check connect anew: the others' lists
            # go, the one in use is set up again with it.
            self.assertEqual(invalidated, ["Inherits"])
            set_provider.assert_called_once_with(force_update=True)
            # The one in use with a check of its own keeps its connection.
            fixture = next(
                p for p in self.window.provider_manager.providers if p["name"] == PROVIDER_NAME
            )
            fixture["ssl_verify"] = True
            self.key(Action.NAV_RIGHT)
            self.assertTrue(self.config.ssl_verify)
            self.assertEqual(invalidated, ["Inherits", "Inherits"])
            set_provider.assert_called_once_with(force_update=True)


class AdvancedTests(GeneralCase):
    def test_secure_dns_and_detailed_log_hooks(self):
        engine = mock.Mock()
        with (
            mock.patch.object(self.window, "engine", engine),
            mock.patch("services.secure_dns.enable") as enable,
            mock.patch("services.log_file.set_detailed") as detailed,
        ):
            self.open_at(m.ADVANCED, "secure_dns")
            self.key(Action.NAV_RIGHT)
            self.assertTrue(self.config.secure_dns)
            enable.assert_called_once_with(True)
            engine.set_secure_dns.assert_called_once_with(True)
            self.key(Action.NAV_DOWN)
            self.assertEqual(self.drawer.focused_row().key, "detailed_log")
            self.key(Action.NAV_RIGHT)
            self.assertTrue(self.config.detailed_log)
            detailed.assert_called_once_with(True)
            engine.set_detailed_log.assert_called_once_with(True)
            self.key(Action.NAV_RIGHT)
            detailed.assert_called_with(False)
            engine.set_detailed_log.assert_called_with(False)

    def test_clear_image_cache_asks_and_clears_on_a_worker(self):
        cleared = []
        manager = self.window.image_manager
        with mock.patch.object(
            manager, "clear_cache", side_effect=lambda: cleared.append(threading.current_thread())
        ):
            row = self.open_at(m.ADVANCED, m.CLEAR_IMAGE_CACHE)
            self.assertEqual(row.kind, m.DANGER)
            self.key(Action.NAV_ACTIVATE)  # asked first
            question = self.window.general_rows.question
            self.assertTrue(self.window.settings.card_shown())
            self.assertEqual(question.title(), translate(CLEAR_CACHE_QUESTION))
            self.assertEqual(question.focus(), ("button", CANCEL))  # the old window's No
            self.key(Action.NAV_ACTIVATE)  # Cancel: nothing goes
            self.assertFalse(self.window.settings.card_shown())
            self.assertEqual(cleared, [])
            self.key(Action.NAV_ACTIVATE)
            self.key(Action.NAV_RIGHT)
            self.assertEqual(question.focus(), ("button", CLEAR))
            self.key(Action.NAV_ACTIVATE)
            self.assertTrue(wait_until(lambda: (translate(CLEARED_TEXT), "info") in self.toasts))
        self.assertEqual(len(cleared), 1)
        self.assertIsNot(cleared[0], threading.main_thread())
        self.assertEqual(self.drawer.path(), (m.ADVANCED,))

    def test_image_cache_size_round_trips(self):
        row = self.open_at(m.ADVANCED, "max_cache_image_size")
        self.assertTrue(row.restart)
        self.assertEqual([c.value for c in row.choices], [50, 100, 200, 500, 1000, 2000])
        self.assertEqual(self.window.settings.value_text(row), "100")  # the config's default
        self.assertFalse(self.window.settings.lit(row))
        self.key(Action.NAV_RIGHT)
        self.assertEqual(self.config.max_cache_image_size, 200)
        self.config.save_config(wait=True)
        self.assertEqual(ConfigManager().max_cache_image_size, 200)
        # A size the old window took that is none of the steps shows as it is, and steps on.
        odd = {s.key: s for s in m.sections("Linux", (), m.Context(image_cache_mb=150))}
        sized = next(r for r in odd[m.ADVANCED].rows if r.key == "max_cache_image_size")
        self.assertEqual([c.value for c in sized.choices], [50, 100, 150, 200, 500, 1000, 2000])
        self.assertEqual(m.choice_of(sized, 150).shown(), "150")
        self.assertEqual(m.step(sized, 150, 1), 200)


if __name__ == "__main__":
    unittest.main()
