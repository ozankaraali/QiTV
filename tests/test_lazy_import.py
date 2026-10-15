import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import unittest

from services import lazy_import
from services.lazy_import import deferred_modules, lazy_module, load_lazy_modules

ROOT = Path(__file__).resolve().parents[1]
# What main() imports before the window shows (main.py imports these inside main()).
STARTUP_IMPORTS = """
from PySide6 import QtGui
from PySide6.QtWidgets import QApplication
import qdarktheme
from services.config_manager import ConfigManager, get_app_version
from services.epg_manager import EpgManager
from services.image_manager import ImageManager
from services.provider_manager import ProviderManager
from services.sleep_manager import allow_sleep, prevent_sleep
from ui.tv_window import TVWindow
from ui.update_dialogs import check_for_updates
"""
# Loaded when a background job first needs them, not before the window shows.
NETWORK_MODULES = ["requests", "aiohttp", "urllib3", "Cryptodome", "m3u8", "urlobject"]


class LazyModuleTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        sys.path.insert(0, str(self.root))
        self.addCleanup(sys.path.remove, str(self.root))

    def make_module(self, name, body):
        (self.root / f"{name}.py").write_text(body, encoding="utf-8")
        self.addCleanup(sys.modules.pop, name, None)

    def test_the_module_runs_on_first_attribute_read(self):
        self.make_module("qitv_lazy_probe", "import sys\nsys.qitv_lazy_runs = 1\nX = 1\n")
        self.addCleanup(lambda: vars(sys).pop("qitv_lazy_runs", None))
        module = lazy_module("qitv_lazy_probe")
        self.assertFalse(hasattr(sys, "qitv_lazy_runs"))
        self.assertIs(sys.modules["qitv_lazy_probe"], module)
        self.assertEqual(module.X, 1)
        self.assertEqual(sys.qitv_lazy_runs, 1)

    def test_deferred_modules_load_together_on_request(self):
        self.make_module("qitv_lazy_a", "import sys\nsys.qitv_lazy_a = 1\n")
        self.make_module("qitv_lazy_b", "import sys\nsys.qitv_lazy_b = 1\n")
        self.addCleanup(lambda: [vars(sys).pop(k, None) for k in ("qitv_lazy_a", "qitv_lazy_b")])
        self.addCleanup(
            lambda: [
                lazy_import._lazy.remove(n)
                for n in ("qitv_lazy_a", "qitv_lazy_b")
                if n in lazy_import._lazy
            ]
        )
        lazy_module("qitv_lazy_a")
        lazy_module("qitv_lazy_b")
        self.assertEqual(deferred_modules()[-2:], ["qitv_lazy_a", "qitv_lazy_b"])
        self.assertFalse(hasattr(sys, "qitv_lazy_a"))
        load_lazy_modules()
        self.assertEqual((sys.qitv_lazy_a, sys.qitv_lazy_b), (1, 1))

    def test_an_imported_module_is_returned_as_it_is(self):
        self.assertIs(lazy_module("json"), json)

    def test_a_missing_module_raises_at_once(self):
        with self.assertRaises(ModuleNotFoundError):
            lazy_module("qitv_no_such_module_anywhere")

    def test_threads_reading_it_first_at_once_run_it_once(self):
        self.make_module(
            "qitv_lazy_slow",
            "import sys, time\n"
            "sys.qitv_lazy_runs = getattr(sys, 'qitv_lazy_runs', 0) + 1\n"
            "time.sleep(0.2)\nVALUE = 42\n",
        )
        self.addCleanup(lambda: vars(sys).pop("qitv_lazy_runs", None))
        module = lazy_module("qitv_lazy_slow")
        seen = []
        threads = [threading.Thread(target=lambda: seen.append(module.VALUE)) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual(seen, [42] * 8)
        self.assertEqual(sys.qitv_lazy_runs, 1)


class PackagingTests(unittest.TestCase):
    def test_every_lazily_loaded_module_is_bundled(self):
        # PyInstaller finds modules through import statements; lazy_module() has none.
        names = set()
        for path in [*ROOT.glob("services/**/*.py"), *ROOT.glob("ui/**/*.py")]:
            names.update(re.findall(r'lazy_module\("([\w.]+)"\)', path.read_text("utf-8")))
        self.assertTrue(names)
        for spec in ROOT.glob("qitv-*.spec"):
            text = spec.read_text("utf-8")
            hidden = text[
                text.index("hiddenimports=[") : text.index("]", text.index("hiddenimports=["))
            ]
            for name in sorted(names):
                self.assertIn(f"'{name}'", hidden, f"{spec.name} misses {name}")


class StartupImportTests(unittest.TestCase):
    def test_the_window_shows_before_the_network_libraries_load(self):
        code = STARTUP_IMPORTS + (
            "import json, sys\n"
            f"print(json.dumps([m for m in {NETWORK_MODULES!r} if m in sys.modules"
            " and type(sys.modules[m]).__name__ != '_LazyModule']))\n"
        )
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout.strip().splitlines()[-1]), [])


if __name__ == "__main__":
    unittest.main()
