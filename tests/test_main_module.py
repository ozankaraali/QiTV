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

    def test_the_smoke_test_runs_only_with_its_flag_and_variable(self):
        # The packaged apps' CI smoke test: release users never start it by accident.
        import main

        argv = ["qitv", "--smoke", "--out", "smoke-frozen", "--pipeline"]
        self.assertEqual(
            main.smoke_arguments(argv, {"QITV_SMOKE": "1"}), ["--out", "smoke-frozen", "--pipeline"]
        )
        self.assertIsNone(main.smoke_arguments(argv, {}))
        self.assertIsNone(main.smoke_arguments(argv, {"QITV_SMOKE": "0"}))
        self.assertIsNone(
            main.smoke_arguments(["qitv", "--out", "x", "--smoke"], {"QITV_SMOKE": "1"})
        )
        self.assertIsNone(main.smoke_arguments(["qitv"], {"QITV_SMOKE": "1"}))

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

    def test_the_linux_app_bundles_vlc_plugins_where_its_libvlccore_looks(self):
        # libVLC 3 on Linux loads plugins from <libvlccore's directory>/vlc/plugins, and the
        # bundle's libvlccore sits at its top: plugins anywhere else leave libvlc_new NULL.
        tree = ast.parse((REPO / "qitv-linux.spec").read_text(encoding="utf-8"))
        analysis = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Analysis"
        )
        binaries = next(k.value for k in analysis.keywords if k.arg == "binaries")
        destinations = [
            item.elts[1].value
            for item in binaries.elts
            if isinstance(item, ast.Tuple) and "plugins" in ast.unparse(item.elts[0])
        ]
        self.assertEqual(destinations, ["vlc/plugins"])

    def test_the_bundled_libvlc_is_chosen_before_anything_imports_vlc(self):
        source = (REPO / "main.py").read_text(encoding="utf-8")
        block = source.split('if __name__ == "__main__":', 1)[1]
        self.assertLess(block.index("freeze_support()"), block.index("use_bundled_vlc()"))
        self.assertLess(block.index("use_bundled_vlc()"), block.index("smoke_main"))
        self.assertLess(block.index("use_bundled_vlc()"), block.index("main()"))

    def test_the_mac_app_is_one_directory_with_vlc_plugins_where_its_libvlccore_looks(self):
        # libvlccore on macOS loads plugins from <its directory>/vlc/plugins; one directory,
        # so that macOS validates the plugins once, not after every launch's unpacking.
        tree = ast.parse((REPO / "qitv-macos.spec").read_text(encoding="utf-8"))
        calls = {
            getattr(node.func, "id", ""): node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
        }
        binaries = next(k.value for k in calls["Analysis"].keywords if k.arg == "binaries")
        destinations = {
            ast.literal_eval(item.elts[1])
            for item in binaries.elts
            if isinstance(item, ast.Tuple) and "plugins" in ast.unparse(item.elts[0])
        }
        self.assertEqual(destinations, {"vlc/plugins"})
        exclude = next(k.value for k in calls["EXE"].keywords if k.arg == "exclude_binaries")
        self.assertIs(ast.literal_eval(exclude), True)
        self.assertEqual(getattr(calls["BUNDLE"].args[0], "id", ""), "coll")
        self.assertIn("COLLECT", calls)


if __name__ == "__main__":
    unittest.main()
