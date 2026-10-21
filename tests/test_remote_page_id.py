"""Which phone page a QiTV carries (services/remote/page_id.py): a fingerprint of its files, which
QiTV's hello names so the relay can hand a phone that QiTV's own page."""

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from services.remote.hub import web_dir
from services.remote.page_id import page_id


class PageIdTests(unittest.TestCase):
    def folder(self, files):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: [p.unlink() for p in root.rglob("*") if p.is_file()])
        for name, text in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text)
        return root

    def test_the_same_files_the_same_id(self):
        a = self.folder({"index.html": "x", "vendor/crypto.js": "y"})
        b = self.folder({"vendor/crypto.js": "y", "index.html": "x"})
        self.assertEqual(page_id(a), page_id(b))
        self.assertRegex(page_id(a), r"^[0-9a-f]{16}$")

    def test_any_change_another_id(self):
        base = page_id(self.folder({"index.html": "x", "app.js": "y"}))
        for files in (
            {"index.html": "x", "app.js": "z"},  # a file's content
            {"index.html": "x", "app2.js": "y"},  # a file's name
            {"index.html": "x", "app.js": "y", "more.js": ""},  # a file more
            {"index.htmlx": "", "app.js": "y"},  # the bytes moved between name and content
        ):
            with self.subTest(files=files):
                self.assertNotEqual(page_id(self.folder(files)), base)

    def test_windows_line_endings_are_the_same_page(self):
        # A Windows checkout (git's autocrlf) gives the page's text files CRLF line ends; the
        # relay's copy has LF: one page, one id, or Windows QiTVs would never meet theirs.
        lf = self.folder({"index.html": "<p>\nx\n", "app.js": "a;\nb;\n"})
        crlf = Path(tempfile.mkdtemp())
        for name in ("index.html", "app.js"):
            (crlf / name).write_bytes((lf / name).read_bytes().replace(b"\n", b"\r\n"))
        self.assertEqual(page_id(crlf), page_id(lf))

    def test_what_the_relay_adds_and_hidden_files_do_not_count(self):
        files = {"index.html": "x"}
        base = page_id(self.folder(files))
        extra = {**files, "pages.json": "{}", ".DS_Store": "z", "p/abc/index.html": "old"}
        self.assertEqual(page_id(self.folder(extra)), base)

    def test_run_as_a_script_it_prints_the_id(self):
        script = Path(__file__).resolve().parents[1] / "services" / "remote" / "page_id.py"
        out = subprocess.run(
            [sys.executable, str(script), str(web_dir())],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(out.stdout.strip(), page_id(web_dir()))


if __name__ == "__main__":
    unittest.main()
