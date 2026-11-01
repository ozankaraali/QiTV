"""A file is written whole or not at all, and no temporary file is left either way."""

import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from main import written_dirs
from services.atomic_write import (
    STALE_AFTER_S,
    atomic_write,
    set_aside,
    sweep_in_background,
    sweep_stale_temporaries,
)


class AtomicWriteTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "settings.json"
        self.path.write_bytes(b"old")

    def test_the_file_is_replaced_once_written(self):
        with atomic_write(str(self.path)) as f:
            f.write(b"new")
            seen = os.listdir(self.path.parent)
            self.assertEqual(self.path.read_bytes(), b"old")  # until it is whole
        self.assertEqual(self.path.read_bytes(), b"new")
        self.assertEqual(os.listdir(self.path.parent), ["settings.json"])
        [temporary] = [name for name in seen if name != "settings.json"]
        self.assertTrue(temporary.startswith(".settings-") and temporary.endswith(".tmp"))

    def test_a_failed_write_leaves_the_file_as_it_was(self):
        with self.assertRaises(RuntimeError):
            with atomic_write(str(self.path)) as f:
                f.write(b"half")
                raise RuntimeError("disk full")
        self.assertEqual(self.path.read_bytes(), b"old")
        self.assertEqual(os.listdir(self.path.parent), ["settings.json"])

    def test_a_replace_that_fails_or_skips_leaves_no_temporary_file(self):
        def locked(temporary, path):
            raise PermissionError(13, "locked")

        with self.assertRaises(PermissionError):
            with atomic_write(str(self.path), locked) as f:
                f.write(b"new")
        with atomic_write(str(self.path), lambda temporary, path: None) as f:
            f.write(b"newer")  # a newer write is queued: this one is not swapped in
        self.assertEqual(self.path.read_bytes(), b"old")
        self.assertEqual(os.listdir(self.path.parent), ["settings.json"])


class SweepTests(unittest.TestCase):
    """A write killed midway leaves its temporary file: a day later, start-up removes it."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.now = time.time()

    def make(self, name, age_s, directory=None):
        path = (directory or self.dir) / name
        path.write_bytes(b"x")
        os.utime(path, (self.now - age_s, self.now - age_s))
        return path

    def test_only_temporary_files_a_day_old_go(self):
        day = STALE_AFTER_S
        stale = [
            self.make(".config-k2j9x_0a.tmp", day + 60),
            self.make(".index-abcdefgh.tmp", 3 * day),
            self.make(".subtitle_picks-0000_zzz.tmp", day + 1),  # the stores' own writer
        ]
        kept = [
            self.make(".config-k2j9x_0b.tmp", day - 60),  # perhaps a write under way
            self.make("config.json", 3 * day),
            self.make("notes.tmp", 3 * day),  # not hidden
            self.make(".config.tmp", 3 * day),  # not a temporary file's name
            self.make(".config-ABCDEFGH.tmp", 3 * day),
            self.make(".config-abcdefgh.tmp.bak", 3 * day),
        ]
        folder = self.dir / ".cache-abcdefgh.tmp"  # a directory so named stays
        folder.mkdir()
        os.utime(folder, (self.now - 3 * day, self.now - 3 * day))
        self.assertEqual(sweep_stale_temporaries([str(self.dir)], now=self.now), 3)
        self.assertFalse(any(path.exists() for path in stale))
        self.assertTrue(all(path.exists() for path in kept))
        self.assertTrue(folder.is_dir())

    def test_a_missing_directory_and_an_unremovable_file_are_passed_over(self):
        old = self.make(".config-abcdefgh.tmp", 2 * STALE_AFTER_S)
        missing = str(self.dir / "cache" / "epg")
        with mock.patch("services.atomic_write.os.remove", side_effect=PermissionError(13, "x")):
            self.assertEqual(sweep_stale_temporaries([missing, str(self.dir)], now=self.now), 0)
        self.assertTrue(old.exists())
        self.assertEqual(sweep_stale_temporaries([missing, str(self.dir)], now=self.now), 1)
        self.assertFalse(old.exists())

    def test_start_up_sweeps_on_a_thread_of_its_own(self):
        epg = self.dir / "cache" / "epg"
        epg.mkdir(parents=True)
        old = self.make(".index-abcdefgh.tmp", 2 * STALE_AFTER_S, epg)
        threads = []
        real = sweep_stale_temporaries

        def sweep(directories):
            threads.append(threading.current_thread())
            return real(directories)

        with mock.patch("services.atomic_write.sweep_stale_temporaries", sweep):
            thread = sweep_in_background(written_dirs(str(self.dir)))
            thread.join(5)
        self.assertEqual(threads, [thread])
        self.assertIsNot(thread, threading.main_thread())
        self.assertFalse(old.exists())

    def test_the_swept_directories_are_where_qitv_writes(self):
        from services.config_manager import ConfigManager
        from services.epg_manager import EpgManager
        from services.provider_manager import ProviderManager

        with mock.patch.dict(os.environ, {"QITV_CONFIG_DIR": str(self.dir)}):
            config = ConfigManager()
            providers = ProviderManager(config)
            self.addCleanup(providers._cache_writer.shutdown, wait=True)
            epg = EpgManager(config, SimpleNamespace(current_provider={}))
            config_dir = config.get_config_dir()  # the config, and the channel stores beside it
            cache = os.path.join(config_dir, "cache")  # 2.8: TMDB's cache is written there
            self.assertEqual(
                written_dirs(config_dir),
                [config_dir, cache, epg._cache_dir(), providers.provider_dir],
            )

    def test_each_downloaded_subtitles_folder_is_swept_too(self):  # 2.8
        subtitles = self.dir / "subtitles"
        folder = subtitles / "12345"
        folder.mkdir(parents=True)
        old = self.make(".Film.en-abcdefgh.tmp", 2 * STALE_AFTER_S, folder)
        kept = self.make("Film.en.srt", 2 * STALE_AFTER_S, folder)
        thread = sweep_in_background([], nested=[str(subtitles)])
        thread.join(5)
        self.assertFalse(old.exists())
        self.assertTrue(kept.exists())


class UnreadableFileTests(unittest.TestCase):
    """A settings file cut short (a crash mid-save) is kept aside, never written over: the
    defaults that replace it lose nothing, and the providers' caches stay."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        patcher = mock.patch.dict(os.environ, {"QITV_CONFIG_DIR": str(self.dir)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def providers(self, config):
        from services.provider_manager import ProviderManager

        manager = ProviderManager(config)
        self.addCleanup(manager._cache_writer.shutdown, wait=True)
        return manager

    def aside(self, name):
        return sorted(self.dir.rglob(f"{name}.broken-*"))

    def test_a_provider_list_cut_short_is_kept_aside_with_every_cache(self):
        from services.config_manager import ConfigManager

        config = ConfigManager()
        first = self.providers(config)
        first.providers = [
            {"name": "A", "type": "XTREAM", "url": "http://a.invalid", "password": "p"},
            {"name": "B", "type": "M3UPLAYLIST", "url": "http://b.invalid/list.m3u"},
        ]
        first.save_providers()
        caches = [Path(first._provider_cache_name(name)) for name in ("A", "B")]
        for cache in caches:
            cache.write_bytes(b'{"itv": {}}')
        index = Path(first.index_file)
        cut = index.read_bytes()[:60]
        index.write_bytes(cut)

        manager = self.providers(config)

        self.assertEqual(manager.providers, manager.default_providers())
        [aside] = self.aside("index.json")
        self.assertEqual(aside.read_bytes(), cut)
        self.assertEqual(json.loads(index.read_bytes()), manager.default_providers())
        manager.save_providers()  # this session's saves prune nothing either
        self.assertTrue(all(cache.exists() for cache in caches))

    def test_the_provider_list_is_written_whole_or_not_at_all(self):
        from services.config_manager import ConfigManager

        manager = self.providers(ConfigManager())
        before = Path(manager.index_file).read_bytes()
        manager.providers = manager.providers + [{"name": "A", "type": "M3UPLAYLIST"}]
        with mock.patch("services.atomic_write.os.replace", side_effect=OSError(28, "full")):
            with self.assertRaises(OSError):
                manager.save_providers()
        self.assertEqual(Path(manager.index_file).read_bytes(), before)

    def test_damaged_settings_are_kept_aside_before_the_defaults_are_written(self):
        from services.config_manager import ConfigManager

        damaged = b'{"selected_provider_name": "A", "favor'
        path = self.dir / "config.json"
        path.write_bytes(damaged)

        config = ConfigManager()

        [aside] = self.aside("config.json")
        self.assertEqual(aside.read_bytes(), damaged)
        written = json.loads(path.read_bytes())
        self.assertEqual(written["selected_provider_name"], config.selected_provider_name)

    @unittest.skipIf(os.name == "nt", "POSIX file modes")
    def test_the_provider_list_and_the_settings_are_the_users_alone(self):
        from services.config_manager import ConfigManager

        config = ConfigManager()
        manager = self.providers(config)
        for path in (config.config_path, manager.index_file):  # written by an older build
            os.chmod(path, 0o644)
        config.save_config()
        manager.save_providers()
        for path in (config.config_path, manager.index_file):
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600, path)

    def test_two_set_aside_in_one_second_are_both_kept(self):
        path = self.dir / "index.json"
        path.write_bytes(b"one")
        first = set_aside(str(path), now=1_000_000.0)
        path.write_bytes(b"two")
        second = set_aside(str(path), now=1_000_000.0)
        self.assertNotEqual(first, second)
        self.assertEqual(Path(first).read_bytes(), b"one")
        self.assertEqual(Path(second).read_bytes(), b"two")
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
