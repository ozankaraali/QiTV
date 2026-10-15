import ast
from pathlib import Path
import subprocess
import sys
import unittest

REPO = Path(__file__).resolve().parent.parent
PROBE = """
import importlib, sys
importlib.import_module("main")
heavy = sorted(
    name
    for name in sys.modules
    if name.split(".")[0] in ("PySide6", "shiboken6", "qdarktheme", "vlc", "ui", "services")
)
print(heavy)
"""


class MainModuleTests(unittest.TestCase):
    def test_importing_main_loads_no_qt_or_ui(self):
        # Helper processes started with multiprocessing's "spawn" import main again.
        result = subprocess.run(
            [sys.executable, "-c", PROBE], cwd=REPO, capture_output=True, text=True, timeout=60
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "[]")

    def test_freeze_support_comes_first_when_run_as_a_script(self):
        source = (REPO / "main.py").read_text(encoding="utf-8")
        block = source.split('if __name__ == "__main__":', 1)[1]
        first = next(
            line.strip()
            for line in block.splitlines()
            if line.strip() and not line.strip().startswith("#")
        )
        self.assertEqual(first, "multiprocessing.freeze_support()")

    def test_the_frozen_apps_bundle_the_live_helper(self):
        # The helper process is spawned from the bundle; its source imports these lazily.
        wanted = {"services.live.worker", "m3u8", "Cryptodome.Cipher.AES"}
        for name in ("qitv-macos.spec", "qitv-windows.spec", "qitv-linux.spec"):
            tree = ast.parse((REPO / name).read_text(encoding="utf-8"))
            analysis = next(
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Analysis"
            )
            hidden = next(k.value for k in analysis.keywords if k.arg == "hiddenimports")
            self.assertLessEqual(wanted, set(ast.literal_eval(hidden)), name)


if __name__ == "__main__":
    unittest.main()
