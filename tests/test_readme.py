"""README.md against the app: its key tables are the app's SHORTCUTS, its pictures exist, and
its licenses (and THIRD-PARTY-NOTICES.md's) are what the installed packages state."""

import importlib.metadata
from pathlib import Path
import re
import tomllib
import unittest

from scripts import make_demo_content, readme_keys
from ui.keymap import SHORTCUT_TITLES, SHORTCUTS

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
NOTICES = ROOT / "THIRD-PARTY-NOTICES.md"
PYPROJECT = ROOT / "pyproject.toml"
_MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)")
_HTML_IMAGE = re.compile(r"<img\b[^>]*\bsrc=\"([^\"]+)\"")


class ReadmeTests(unittest.TestCase):
    def setUp(self):
        self.text = README.read_text(encoding="utf-8")

    def test_the_key_tables_are_what_the_app_lists(self):
        # Regenerate with `uv run python scripts/readme_keys.py` and paste between the markers.
        self.assertEqual(readme_keys.readme_block(self.text), readme_keys.markdown())

    def test_every_screen_and_key_of_the_app_is_in_the_tables(self):
        block = readme_keys.readme_block(self.text)
        for context, rows in SHORTCUTS.items():
            self.assertIn(f"**{SHORTCUT_TITLES[context]}**", block)
            for keys, what in rows:
                self.assertIn(f"| {readme_keys.keys_cell(keys)} | {what} |", block)

    def test_keys_read_the_same_on_every_computer(self):
        self.assertEqual(readme_keys.key_name("Ctrl+F"), "Ctrl/⌘+F")
        self.assertEqual(readme_keys.key_name("Ctrl+P"), "Ctrl/⌘+P")
        self.assertEqual(readme_keys.key_name("Alt+P"), "Alt/⌥+P")
        self.assertEqual(readme_keys.key_name("Ctrl+Meta+F"), "⌃⌘F (Mac)")
        self.assertEqual(readme_keys.key_name("0…9"), "0–9")
        self.assertEqual(readme_keys.keys_cell("Up, Down"), "<kbd>↑</kbd> / <kbd>↓</kbd>")

    def test_every_picture_it_shows_is_in_the_repository(self):
        paths = _MARKDOWN_IMAGE.findall(self.text) + _HTML_IMAGE.findall(self.text)
        local = [path for path in paths if not path.startswith(("http://", "https://"))]
        self.assertTrue(local)
        for path in local:
            self.assertTrue((ROOT / path).is_file(), path)

    def test_it_shows_every_screenshot_the_demo_script_takes(self):
        for name in make_demo_content.SHOTS:
            self.assertIn(f"docs/screenshots/{name}", self.text)

    def test_each_python_library_has_the_license_its_package_states(self):
        notices = NOTICES.read_text(encoding="utf-8")
        for requirement in tomllib.loads(PYPROJECT.read_text())["project"]["dependencies"]:
            name = re.split(r"[\[<>=!~ ]", requirement, maxsplit=1)[0]
            try:
                metadata = importlib.metadata.metadata(name)
            except importlib.metadata.PackageNotFoundError:
                continue  # not installed here (another platform's): nothing to compare with
            row = f"| {metadata['Name']} | {stated_license(metadata)} |"
            self.assertIn(row, self.text)
            self.assertIn(row, notices)


class NoticesTests(unittest.TestCase):
    def test_every_license_text_it_names_is_in_the_repository(self):
        # The specs ship licenses/ whole: a text named here but missing is missing in the app.
        named = set(re.findall(r"`(licenses/[^`]+)`", NOTICES.read_text(encoding="utf-8")))
        self.assertTrue(any(path.startswith("licenses/python/") for path in named))
        for path in sorted(named):
            self.assertTrue((ROOT / path).is_file(), path)


def stated_license(metadata) -> str:
    """The license as a package's metadata states it: its expression, else its License
    field, else its license classifiers."""
    expression = (metadata.get("License-Expression") or "").strip()
    if expression:
        return expression
    text = (metadata.get("License") or "").strip()
    if text:
        return text
    classifiers = metadata.get_all("Classifier") or []
    return ", ".join(c.split(" :: ")[-1] for c in classifiers if c.startswith("License ::"))


if __name__ == "__main__":
    unittest.main()
