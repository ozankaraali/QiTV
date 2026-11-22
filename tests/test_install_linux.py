"""scripts/install-linux.sh writes the desktop entry's Exec= line for the path QiTV is installed at,
whatever characters the path holds (a sed replacement read & | and \\ in it as its own)."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "install-linux.sh"
TEMPLATE = REPO / "assets" / "qitv.desktop"
# Each in a folder name of its own: those sed and the Exec key read as their own, and plain ones.
NAMES = {
    "ampersand": "This & That",
    "pipe": "a|b",
    "backslash": "back\\slash",
    "backtick": "back`tick",
    "dollar": "pay$HOME",
    "percent": "100%u",
    "quote": 'say "hi"',
    "apostrophe": "it's",
    "space": "My Apps",
    "plain": "apps",
}


def _key_string(value: str) -> str:
    """A desktop entry's string value as read: its escapes (\\s \\n \\t \\r \\\\) undone."""
    out, i = [], 0
    escapes = {"s": " ", "n": "\n", "t": "\t", "r": "\r", "\\": "\\"}
    while i < len(value):
        if value[i] == "\\" and i + 1 < len(value):
            if value[i + 1] not in escapes:
                raise ValueError(f"invalid escape in {value!r}")  # GLib refuses the key
            out.append(escapes[value[i + 1]])
            i += 2
        else:
            out.append(value[i])
            i += 1
    return "".join(out)


def exec_argv(value: str) -> list[str]:
    """The program and arguments an Exec= value starts, read as the Desktop Entry spec (and
    GLib) read it: the string's escapes, then %% as %, then the quoting (in double quotes,
    a backslash escapes " ` $ and itself)."""
    text = _key_string(value)
    if "%" in text.replace("%%", ""):
        raise ValueError(f"a field code in {value!r}")
    text = text.replace("%%", "%")
    args: list[str] = []
    arg: list[str] = []
    started = quoted = False
    i = 0
    while i < len(text):
        c = text[i]
        if quoted:
            if c == "\\" and i + 1 < len(text) and text[i + 1] in '"`$\\':
                arg.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                quoted = False
            else:
                arg.append(c)
        elif c == '"':
            quoted = started = True
        elif c == " ":
            if started:
                args.append("".join(arg))
                arg, started = [], False
        elif c in "\t\n'\\><~|&;$*?#()`":
            raise ValueError(f"{c!r} not quoted in {value!r}")  # reserved: quoted or wrong
        else:
            arg.append(c)
            started = True
        i += 1
    if quoted:
        raise ValueError(f"a quote left open in {value!r}")
    if started:
        args.append("".join(arg))
    return args


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("bash"), "Linux's script")
class InstallLinuxTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="qitv-install-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def install(self, home: Path, script: Path, *args: str) -> list[str]:
        """The script run with `home` as HOME; the Exec= line's program and arguments."""
        home.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "HOME": str(home)}
        done = subprocess.run(
            ["bash", str(script), *args], env=env, capture_output=True, text=True, timeout=60
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        entry = home / ".local" / "share" / "applications" / "qitv.desktop"
        lines = entry.read_text(encoding="utf-8").splitlines()
        template = TEMPLATE.read_text(encoding="utf-8").splitlines()
        self.assertEqual(  # every other line as the template has it
            [line for line in lines if not line.startswith("Exec=")],
            [line for line in template if not line.startswith("Exec=")],
        )
        execs = [line[len("Exec=") :] for line in lines if line.startswith("Exec=")]
        self.assertEqual(len(execs), 1, lines)
        return exec_argv(execs[0])

    def test_the_release_binarys_exec_line_names_where_it_was_installed(self):
        binary = self.root / "download" / "qitv-linux"
        binary.parent.mkdir()
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        for case, name in NAMES.items():
            with self.subTest(case):
                home = self.root / "homes" / case / name
                argv = self.install(home, SCRIPT, str(binary))
                self.assertEqual(argv, [str(home / ".local" / "bin" / "qitv-linux")])

    def test_a_checkouts_exec_line_names_its_python_and_main_py(self):
        for case, name in NAMES.items():
            with self.subTest(case):
                checkout = self.root / "checkouts" / case / name
                (checkout / "scripts").mkdir(parents=True)
                (checkout / "assets").mkdir()
                (checkout / "venv" / "bin").mkdir(parents=True)
                shutil.copy(SCRIPT, checkout / "scripts" / SCRIPT.name)
                shutil.copy(TEMPLATE, checkout / "assets" / TEMPLATE.name)
                shutil.copy(REPO / "assets" / "qitv.png", checkout / "assets" / "qitv.png")
                (checkout / "venv" / "bin" / "python").write_text("", encoding="utf-8")
                (checkout / "main.py").write_text("", encoding="utf-8")
                argv = self.install(self.root / "home", checkout / "scripts" / SCRIPT.name)
                self.assertEqual(
                    argv, [str(checkout / "venv" / "bin" / "python"), str(checkout / "main.py")]
                )


if __name__ == "__main__":
    unittest.main()
