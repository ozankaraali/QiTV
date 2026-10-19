"""A file is written whole or not at all, and no temporary file is left either way."""

import os
from pathlib import Path
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
            self.assertEqual(
                written_dirs(config_dir), [config_dir, epg._cache_dir(), providers.provider_dir]
            )


if __name__ == "__main__":
    unittest.main()
