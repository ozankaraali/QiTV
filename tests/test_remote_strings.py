"""The phone remote page's words come from QiTV's translations (services/remote/page_strings.py):
every text the page shows has a key there, its English matches the page's own fallback, and
each language translates it."""

import html
import json
from pathlib import Path
import re
import unittest

from services.remote.page_strings import PAGE_STRINGS, page_hello

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "services" / "remote" / "web"
LANGUAGES = ("tr", "de", "es", "fr")


def page_texts() -> dict[str, str]:
    """Every data-t / data-tp key of index.html with the English it shows."""
    page = (WEB / "index.html").read_text(encoding="utf-8")
    found: dict[str, str] = {}
    for key, text in re.findall(r'data-t="([a-z_]+)"[^>]*>([^<]*)<', page):
        found.setdefault(key, html.unescape(text))
    for text, key in re.findall(r'placeholder="([^"]*)" data-tp="([a-z_]+)"', page):
        found.setdefault(key, html.unescape(text))
    return found


def script_texts(name: str, table: str) -> dict[str, str]:
    """A JS file's table of English fallbacks: `const <table> = { key: "text", ... };`."""
    source = (WEB / name).read_text(encoding="utf-8")
    block = re.search(rf"const {table} = \{{(.*?)\n\}};", source, re.S)
    assert block is not None, f"{table} not found in {name}"
    return {
        k: json.loads(v)
        for k, v in re.findall(r'\n\s+([a-z_]+): ("(?:[^"\\]|\\.)*"),', block.group(1))
    }


class PageStringTests(unittest.TestCase):
    def test_every_page_text_has_a_key(self):
        shown = {
            **page_texts(),
            **script_texts("app.js", "FALLBACK"),
            **script_texts("screens.js", "words"),
        }
        self.assertTrue(shown)
        for key, english in shown.items():
            with self.subTest(key):
                self.assertIn(key, PAGE_STRINGS)
                self.assertEqual(PAGE_STRINGS[key], english)

    def test_every_key_is_used(self):
        shown = {
            **page_texts(),
            **script_texts("app.js", "FALLBACK"),
            **script_texts("screens.js", "words"),
        }
        self.assertEqual(set(PAGE_STRINGS) - set(shown), set())

    def test_every_page_string_translated_everywhere(self):
        for lang in LANGUAGES:
            catalog = json.loads((ROOT / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))
            for key, english in PAGE_STRINGS.items():
                with self.subTest(lang=lang, key=key):
                    self.assertTrue(catalog.get(english), f"{lang} lacks {english!r}")

    def test_hello_carries_strings(self):
        hello = page_hello()
        self.assertEqual(set(hello["strings"]), set(PAGE_STRINGS))
        self.assertTrue(hello["lang"])

    def test_the_hub_says_them(self):
        from services.remote.hub import RemoteHub

        hub = RemoteHub(lambda event: None, WEB)
        self.assertEqual(set(hub._core.hello()["strings"]), set(PAGE_STRINGS))


if __name__ == "__main__":
    unittest.main()
