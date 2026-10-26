"""CI writes a release build's OpenSubtitles key and TMDB token into services/_build_keys.py
from its secrets; without a secret its key stays empty, and no key is ever printed."""

import contextlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest

from scripts import write_build_keys

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "main.yml"
KEY = "fictionalbuildkey0123456789abcdef"
TOKEN = "eyJfictional.token-part_two.three"


class WriteBuildKeysTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.target = Path(directory.name) / "_build_keys.py"
        self.target.write_text(write_build_keys.TARGET.read_text(encoding="utf-8"))

    def run_script(self, env):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            write_build_keys.main(env, self.target)
        return out.getvalue()

    def loaded(self):
        spec = importlib.util.spec_from_file_location("written_keys", self.target)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def loaded_key(self):
        return self.loaded().OPENSUBTITLES_API_KEY

    def test_the_secret_is_written_and_never_printed(self):
        said = self.run_script({"QITV_OPENSUBTITLES_KEY": f" {KEY}\n"})
        self.assertEqual(self.loaded_key(), KEY)
        self.assertNotIn(KEY, said)
        self.assertIn("written", said)
        self.assertIn('"""', self.target.read_text())  # its docstring stays

    def test_without_the_secret_the_key_stays_empty(self):
        for env in ({}, {"QITV_OPENSUBTITLES_KEY": ""}, {"QITV_OPENSUBTITLES_KEY": "a b"}):
            with self.subTest(env=env):
                said = self.run_script(env)
                self.assertEqual(self.loaded_key(), "")
                self.assertIn("none", said)

    def test_the_tmdb_token_is_written_and_never_printed(self):
        said = self.run_script({"QITV_TMDB_TOKEN": TOKEN, "QITV_OPENSUBTITLES_KEY": KEY})
        self.assertEqual((self.loaded().TMDB_API_TOKEN, self.loaded_key()), (TOKEN, KEY))
        self.assertNotIn(TOKEN, said)
        self.assertIn("TMDB token: written", said)

    def test_without_its_secret_the_tmdb_token_stays_empty(self):
        said = self.run_script({"QITV_OPENSUBTITLES_KEY": KEY})
        self.assertEqual(self.loaded().TMDB_API_TOKEN, "")
        self.assertIn("TMDB token: none", said)

    def test_the_repositorys_file_has_one_key_line_and_no_key(self):
        source = write_build_keys.TARGET.read_text(encoding="utf-8")
        self.assertEqual(write_build_keys.render(source, ""), source)
        self.assertEqual(write_build_keys.render(source, "", "TMDB_API_TOKEN"), source)

    def test_every_packaging_job_writes_the_keys_before_pyinstaller(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        steps = workflow.split("      - name: ")
        packaging = [i for i, step in enumerate(steps) if "uv run pyinstaller" in step]
        self.assertEqual(len(packaging), 4)
        for index in packaging:
            writes = steps[index - 1]
            self.assertIn("scripts/write_build_keys.py", writes)
            self.assertIn("QITV_OPENSUBTITLES_KEY: ${{ secrets.QITV_OPENSUBTITLES_KEY }}", writes)
            self.assertIn("QITV_TMDB_TOKEN: ${{ secrets.QITV_TMDB_TOKEN }}", writes)
        self.assertNotIn("git commit", workflow)
        self.assertNotIn("git push", workflow)


if __name__ == "__main__":
    unittest.main()
