"""A file is written whole or not at all, and no temporary file is left either way."""

import os
from pathlib import Path
import tempfile
import unittest

from services.atomic_write import atomic_write


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


if __name__ == "__main__":
    unittest.main()
