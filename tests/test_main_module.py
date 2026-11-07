import ast
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

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

    def test_windows_knows_every_version_as_the_same_app(self):
        # The taskbar groups windows and keeps a pinned icon by this id: with the version in
        # it, a pinned QiTV became a second icon after each update.
        import main
        from services.config_manager import get_app_version

        self.assertEqual(main.APP_USER_MODEL_ID, "com.ozankaraali.qitv")
        self.assertNotIn(get_app_version(), main.APP_USER_MODEL_ID)
        source = (REPO / "main.py").read_text(encoding="utf-8")
        self.assertIn("SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)", source)

    def test_the_new_version_replaces_the_old_once_it_quit(self):
        # Windows keeps a running program's file locked: the move is tried again until the
        # old QiTV has quit, and the old file is never half-written meanwhile.
        import main

        with tempfile.TemporaryDirectory() as folder:
            old, new = Path(folder, "qitv.exe"), Path(folder, "qitv-windows-9.9.9.exe")
            old.write_bytes(b"old")
            new.write_bytes(b"new")
            replace = os.replace
            attempts = []

            def locked_twice(source, target):
                attempts.append(Path(target).read_bytes())
                if len(attempts) < 3:
                    raise PermissionError(13, "The file is in use")
                replace(source, target)

            slept = []
            with mock.patch.object(main.os, "replace", side_effect=locked_twice):
                self.assertTrue(main.replace_executable(str(old), str(new), sleep=slept.append))
            self.assertEqual(attempts, [b"old", b"old", b"old"])
            self.assertEqual(slept, [main.REPLACE_RETRY_S] * 2)
            self.assertEqual(old.read_bytes(), b"new")
            self.assertEqual(
                sorted(p.name for p in Path(folder).iterdir()), sorted([old.name, new.name])
            )

    def test_a_replacement_that_never_can_says_where_the_new_version_is(self):
        import main

        with tempfile.TemporaryDirectory() as folder:
            old, new = Path(folder, "qitv.exe"), Path(folder, "qitv-windows-9.9.9.exe")
            old.write_bytes(b"old")
            new.write_bytes(b"new")
            now = [0.0]

            def sleep(seconds):
                now[0] += seconds

            with (
                mock.patch.object(main.os, "replace", side_effect=PermissionError(13, "in use")),
                self.assertLogs("qitv", "ERROR"),
            ):
                done = main.replace_executable(
                    str(old), str(new), sleep=sleep, clock=lambda: now[0]
                )
            self.assertFalse(done)
            self.assertGreaterEqual(now[0], main.REPLACE_WAIT_S)  # it waited that long
            self.assertEqual(old.read_bytes(), b"old")
            self.assertEqual(
                sorted(p.name for p in Path(folder).iterdir()), sorted([old.name, new.name])
            )

            argv = ["qitv-windows-9.9.9.exe", "--replace", str(old)]
            failed = main.handle_replace_flag(argv, str(new), replace=lambda old, new: False)
            self.assertEqual(failed, (str(old), str(new)))
            self.assertIsNone(main.handle_replace_flag(argv, str(new), replace=lambda o, n: True))
            self.assertIsNone(main.handle_replace_flag(["qitv.exe"], str(new)))

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

    def test_the_trusted_authorities_are_chosen_before_anything_connects(self):
        # aiohttp makes its verifying TLS context when it is imported, from what is set then.
        source = (REPO / "main.py").read_text(encoding="utf-8")
        block = source.split('if __name__ == "__main__":', 1)[1]
        self.assertLess(block.index("freeze_support()"), block.index("use_bundled_authorities()"))
        self.assertLess(block.index("use_bundled_authorities()"), block.index("smoke_main"))
        self.assertLess(block.index("use_bundled_authorities()"), block.index("main()"))

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

    def test_the_mac_app_says_why_it_uses_the_home_network(self):  # 2.8 review M12
        # Newer macOS asks before an app reaches the home network or browses Bonjour for a
        # service type: casting looks for _googlecast._tcp; a media server and the phone
        # remote are reached at home.
        tree = ast.parse((REPO / "qitv-macos.spec").read_text(encoding="utf-8"))
        bundle = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "BUNDLE"
        )
        plist = next(k.value for k in bundle.keywords if k.arg == "info_plist")
        keys = {ast.literal_eval(key): value for key, value in zip(plist.keys, plist.values)}
        self.assertTrue(ast.literal_eval(keys["NSLocalNetworkUsageDescription"]))
        self.assertIn("_googlecast._tcp", ast.literal_eval(keys["NSBonjourServices"]))
        # 2.8.2: macOS asked for the microphone as QiTV's sound started with a Bluetooth
        # speaker on; QiTV never records: the question says so.
        microphone = ast.literal_eval(keys["NSMicrophoneUsageDescription"])
        self.assertIn("never records", microphone)
        self.assertIn("Bluetooth", microphone)


if __name__ == "__main__":
    unittest.main()
