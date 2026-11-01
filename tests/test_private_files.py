"""The sign-in's files are this user's only, whatever mode an older QiTV or the umask left."""

import os
from pathlib import Path
import stat
import tempfile
import unittest

from services import private_files


def mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


@unittest.skipIf(os.name == "nt", "POSIX modes")
class TightenTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="qitv-modes-")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.settings, self.logs = root / "qitv", root / "logs"
        self.provider = self.settings / "cache" / "provider"
        self.provider.mkdir(parents=True)
        self.logs.mkdir()
        self.files = [
            self.settings / "config.json",
            self.settings / "live_tuning.json",
            self.provider / "index.json",
            self.provider / ("a" * 64 + ".json"),  # a provider's lists, their links
            self.logs / "qitv.log",
            self.logs / "qitv.log.1",
        ]
        for path in self.files:
            path.write_text("{}", encoding="utf-8")
            os.chmod(path, 0o644)
        for folder in (self.settings, self.provider, self.logs):
            os.chmod(folder, 0o755)

    def test_files_and_folders_become_this_users_only(self):
        changed = private_files.tighten(self.settings, self.logs, system="Linux")
        for path in self.files:
            self.assertEqual(mode(path), 0o600, path.name)
        for folder in (self.settings, self.provider, self.logs):
            self.assertEqual(mode(folder), 0o700, folder.name)
        self.assertEqual(changed, len(self.files) + 3)
        self.assertEqual(private_files.tighten(self.settings, self.logs, system="Linux"), 0)

    def test_no_bit_is_ever_added_and_what_is_missing_is_skipped(self):
        os.chmod(self.files[0], 0o400)  # read-only, the user's choice
        (self.settings / "live_tuning.json").unlink()
        private_files.tighten(self.settings, None, system="Linux")
        self.assertEqual(mode(self.files[0]), 0o400)
        self.assertEqual(mode(self.logs), 0o755)  # no log folder given

    def test_a_portable_settings_folder_is_the_programs_and_stays(self):
        (self.settings / private_files.PORTABLE_FLAG).write_text("", encoding="utf-8")
        private_files.tighten(self.settings, self.logs, system="Linux")
        self.assertEqual(mode(self.settings), 0o755)
        self.assertEqual(mode(self.settings / "config.json"), 0o600)

    def test_windows_is_left_as_it_is(self):
        self.assertEqual(private_files.tighten(self.settings, self.logs, system="Windows"), 0)
        self.assertEqual(mode(self.settings / "config.json"), 0o644)


if __name__ == "__main__":
    unittest.main()
