import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
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

    def test_claim_runs_before_tv_window_import(self):
        # A second start hands its file to the running QiTV before the UI's modules load
        # (seconds on Windows): `-X importtime` lists every module this start imported.
        from instance_fixtures import FIRST

        from services.single_instance import server_name

        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder, "config")
            film = Path(folder, "Dizi Bölüm 1.mkv")
            film.write_bytes(b"film")
            script = Path(folder, "first.py")
            script.write_text(FIRST, encoding="utf-8")
            env = dict(
                os.environ,
                PYTHONPATH=str(REPO),
                QT_QPA_PLATFORM="offscreen",
                QITV_CONFIG_DIR=str(config),
                QITV_SMOKE_MUTE="1",
            )
            first = subprocess.Popen(
                [sys.executable, str(script), server_name(str(config)), "files"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                env=env,
                cwd=REPO,
            )
            try:
                self.assertEqual(first.stdout.readline().strip(), "ready")
                second = subprocess.run(  # a failed hand-over would start QiTV: killed then
                    [sys.executable, "-X", "importtime", "main.py", str(film)],
                    cwd=REPO,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                opened = first.stdout.readline().strip()
            finally:
                first.kill()
                first.wait(15)
                first.stdout.close()
        self.assertEqual(second.returncode, 0, second.stderr[-2000:])
        self.assertIn("this start ends here", second.stderr)
        # spelled as given (never resolved: macOS's /var is /private/var, Windows' temp folder
        # a short name), as a hand-over or DDE names it
        self.assertEqual(opened, "opened " + json.dumps(str(film)))
        imported = {
            line.rsplit("|", 1)[1].strip()
            for line in second.stderr.splitlines()
            if line.startswith("import time:") and "|" in line
        }
        self.assertIn("ui.quit_reason", imported)  # the list is there
        self.assertIn("services.single_instance", imported)
        self.assertNotIn("ui.tv_window", imported)
        self.assertNotIn("qdarktheme", imported)

    def test_a_start_with_a_film_holds_what_would_open_over_it(self):
        # macOS's open event is kept before the claim (a second start hands it over); the
        # film is held for before the window shows, and the update question waits for it.
        body = (REPO / "main.py").read_text(encoding="utf-8").split("def main()", 1)[1]
        self.assertLess(body.index("app.processEvents()"), body.index("instance = claim("))
        self.assertIn("paths=paths + app.kept_opened()", body)
        self.assertEqual(body.count("app.take_opened()"), 1)
        show = body.index("window.show()")
        self.assertLess(body.index("opened = paths + app.take_opened()"), show)
        self.assertLess(body.index("app.filesOpened.connect("), show)
        self.assertLess(body.index("window.controller.hold_for_file(True)"), show)
        # held for a film, and for a file QiTV won't play (its message comes instead)
        self.assertLess(body.index("if holds_the_start(opened):"), show)
        # and, with no film, while its playlists are read (Ruling P4-3)
        self.assertLess(body.index("if reads_the_start(opened):"), show)
        self.assertLess(body.index("window.controller.hold_for_read()"), show)
        self.assertLess(show, body.index("window.open_paths(opened, source=LAUNCH)"))
        # the check starts once the film is over, and its question waits for any film opened
        # while the network answers (ui.update_dialogs._AfterTheFilm)
        self.assertIn(
            "window.when_idle(\n"
            "                lambda: check_for_updates(config_manager=config_manager, "
            "when_idle=window.when_idle)\n",
            body,
        )

    def test_the_tv_list_loads_while_the_window_is_built_behind_the_splash(self):
        # 2.13.1: the window begins its provider's setup as it is built (on a fair day the
        # list is there as it opens), unless no splash shows (the window shows at once, as
        # before) or the start opens files (the film first, as before).
        body = (REPO / "main.py").read_text(encoding="utf-8").split("def main()", 1)[1]
        early = body.index("load_early=with_splash and not (paths + app.kept_opened()),")
        self.assertLess(body.index("window = TVWindow("), early)
        self.assertLess(early, body.index("window.show()"))

    def test_the_settings_lock_goes_last(self):
        # Review M4: the window's close stops answering other starts (TVWindow); the lock stays
        # until the settings are written, so a start that waited for it reads the last save.
        body = (REPO / "main.py").read_text(encoding="utf-8").split("def main()", 1)[1]
        self.assertIn("window.instance = instance", body)
        last = body.rsplit("finally:", 1)[1]
        self.assertLess(last.index("config_manager.flush_saves()"), last.index("instance.close()"))
        self.assertLess(last.index("instance.close()"), last.index("log_file.close()"))

    def test_windows_hears_double_clicks_over_dde_while_it_runs(self):
        # Registered on Windows: a double-click reaches this QiTV over DDE once its window
        # shows, as another start's hand-over does; at quit Explorer is told first, before
        # anything else closes (services/dde_windows.py). The window keeps the run's one
        # server, which Settings turns on and off (Ruling 27).
        body = (REPO / "main.py").read_text(encoding="utf-8").split("def main()", 1)[1]
        serve = body.index("dde = window.dde = dde_windows.serve(config_manager)")
        self.assertLess(body.index("window.show()"), serve)
        self.assertLess(body.rindex('if sys.platform == "win32":', 0, serve), serve)
        self.assertIn(
            "dde.server.opened.connect(lambda files: window.open_paths(files, source=HANDOVER))",
            body,
        )
        last = body.rsplit("finally:", 1)[1]
        self.assertLess(last.index("dde.close()"), last.index("instance.close()"))
        self.assertLess(last.index("dde.close()"), last.index("config_manager.flush_saves()"))

    def test_a_windows_sign_out_still_takes_the_dde_key_back(self):
        # Qt ends a Windows session with aboutToQuit and the process is killed after it:
        # main()'s `finally` never runs then, so the key's removal hangs on aboutToQuit too
        # (Serving.close runs once; the `finally` stays the backstop). And DDE that fails to
        # come up is never a reason for QiTV not to start.
        body = (REPO / "main.py").read_text(encoding="utf-8").split("def main()", 1)[1]
        serve = body.index("dde = window.dde = dde_windows.serve(config_manager)")
        start, end = body.rindex("try:", 0, serve), body.index("if not_replaced", serve)
        block = body[start:end]
        self.assertLess(block.index("from services import dde_windows"), block.index("serve("))
        self.assertIn("except Exception:", block)
        self.assertIn("dde = window.dde = None", block)
        self.assertIn("app.aboutToQuit.connect(dde.close)", block)
        self.assertIn("dde.close()", body.rsplit("finally:", 1)[1])

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

    def test_an_updates_copy_registers_the_qitv_it_replaced(self):
        # Review I1: the update's copy wrote itself over the installed QiTV but runs on from
        # Downloads; what it registers (refresh_at_start, Settings, Explorer's DDE command)
        # names the installed one. Every other start registers where it runs (a QiTV the user
        # moved registers again where it is now).
        import main

        installed = "C:\\Users\\Ayşe\\QiTV\\qitv-windows.exe"
        downloads = "C:\\Users\\Ayşe\\Downloads\\qitv-windows.exe"
        update = [downloads, "--replace", installed]
        self.assertEqual(main.replaced_qitv(update, None), installed)  # it replaced it
        self.assertIsNone(main.replaced_qitv(update, (installed, downloads)))  # it couldn't
        self.assertIsNone(main.replaced_qitv(["qitv-windows.exe", "Dizi Bölüm 1.mkv"], None))
        self.assertIsNone(main.replaced_qitv([downloads, "--replace"], None))
        body = (REPO / "main.py").read_text(encoding="utf-8").split("def main()", 1)[1]
        self.assertIn("replaced = replaced_qitv(sys.argv, not_replaced)", body)
        chosen = body.index("file_types.use_installed(replaced)")
        self.assertLess(chosen, body.index("window = TVWindow("))  # before Settings can open
        self.assertLess(chosen, body.index("refresh_at_start(config_manager, window)"))
        self.assertLess(chosen, body.index("dde_windows.serve(config_manager)"))

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


class FakePyiSplash:
    """PyInstaller's pyi_splash as BootloaderSplash uses it: connected (`alive`) or not, and
    each close() with its thread; `fails` is raised from close()."""

    def __init__(self, alive=True, fails=None):
        self.alive, self.fails = alive, fails
        self.closes = []

    def is_alive(self):
        return self.alive

    def close(self):
        self.closes.append(threading.current_thread())
        if self.fails is not None:
            raise self.fails


PORT = {"_PYI_SPLASH_IPC": "51234"}


class BootloaderSplashTests(unittest.TestCase):
    """PyInstaller's bootloader splash (Windows and Linux one-file apps), which every start
    closes: once, from any thread, never raising into the start."""

    def take(self, environ, module):
        import main

        with mock.patch.dict(sys.modules, {"pyi_splash": module}):
            return main.BootloaderSplash(environ)

    def test_without_its_port_there_is_none_and_nothing_is_imported(self):
        # A run from source, the macOS app, a process the app spawned (port 0): pyi_splash,
        # which warns without its port, is never imported.
        for environ in ({}, {"_PYI_SPLASH_IPC": "0"}, {"_PYI_SPLASH_IPC": ""}):
            with self.subTest(environ=environ):
                module = mock.Mock()
                with self.assertNoLogs("qitv", "DEBUG"):  # nothing to say: none shows
                    bootloader = self.take(environ, module)
                    self.assertFalse(bootloader.shows)
                    bootloader.close("test")
                self.assertEqual(module.mock_calls, [])

    def test_a_splash_that_shows_closes_once_from_any_thread(self):
        module = FakePyiSplash()
        bootloader = self.take(PORT, module)
        self.assertTrue(bootloader.shows)
        threads = [threading.Thread(target=bootloader.close, args=("test",)) for _ in range(8)]
        with self.assertLogs("qitv", "INFO") as logs:
            for thread in threads:
                thread.start()
            bootloader.close("test")
            for thread in threads:
                thread.join()
        self.assertEqual(len(module.closes), 1)
        self.assertFalse(bootloader.shows)
        self.assertEqual(logs.output, ["INFO:qitv:PyInstaller's splash closed: test."])

    def test_a_splash_that_cannot_close_is_never_a_reason_not_to_start(self):
        # It may stay on top of QiTV for the whole run: the log says so.
        module = FakePyiSplash(fails=ConnectionResetError("gone"))
        bootloader = self.take(PORT, module)
        with self.assertLogs("qitv", "WARNING") as logs:
            bootloader.close("test")  # no exception
            bootloader.close("test")
        self.assertEqual(len(module.closes), 1)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("ConnectionResetError", logs.output[0])

    def test_a_port_with_no_splash_to_reach_says_so(self):
        # Its port is set, so the still shows, but QiTV can't reach it (its module missing, or
        # the connection refused): nothing can close it, and the log says so.
        for module in (FakePyiSplash(alive=False), None):
            with self.subTest(module=module):
                with self.assertLogs("qitv", "WARNING") as logs:
                    bootloader = self.take(PORT, module)
                self.assertEqual(len(logs.output), 1)
                self.assertFalse(bootloader.shows)
                bootloader.close("test")

    def test_an_unhandled_error_closes_it_after_its_log(self):
        # An exception that ends main() before its `finally` (or one a slot raised): the still
        # goes as it is logged, before a windowed build's error dialog shows under it.
        module = FakePyiSplash()
        bootloader = self.take(PORT, module)
        self.addCleanup(setattr, sys, "excepthook", sys.excepthook)
        logged = []
        sys.excepthook = lambda *exc: logged.append((exc[0], len(module.closes)))
        bootloader.close_on_crash()
        try:
            raise ValueError("boom")
        except ValueError:
            sys.excepthook(*sys.exc_info())
        self.assertEqual(logged, [(ValueError, 0)])  # logged first, then closed
        self.assertEqual(len(module.closes), 1)
        self.assertFalse(bootloader.shows)

        def failing_log(*_exc):
            raise RuntimeError("the log failed")

        again = FakePyiSplash()
        bootloader = self.take(PORT, again)
        sys.excepthook = failing_log
        bootloader.close_on_crash()
        with self.assertRaises(RuntimeError):
            sys.excepthook(ValueError, ValueError("boom"), None)
        self.assertEqual(len(again.closes), 1)  # closed all the same

    def test_an_unhandled_error_runs_a_close_after_its_log_and_never_raises_for_it(self):
        # The splash helper's: a windowed Windows build's error dialog keeps QiTV's process,
        # and so the splash's pipe, alive, with no other thread running meanwhile.
        import main

        self.addCleanup(setattr, sys, "excepthook", sys.excepthook)
        order = []
        sys.excepthook = lambda *exc: order.append("logged")
        main.close_on_crash(lambda: order.append("closed"))
        sys.excepthook(ValueError, ValueError("boom"), None)
        self.assertEqual(order, ["logged", "closed"])

        def broken():
            raise OSError("no such process")

        order.clear()
        sys.excepthook = lambda *exc: order.append("logged")
        main.close_on_crash(broken)
        sys.excepthook(ValueError, ValueError("boom"), None)  # the close's error stays quiet
        self.assertEqual(order, ["logged"])

    def test_the_splash_helper_ends_on_a_crash_from_its_start_on(self):
        # Between the splash's start and main()'s `try` come the heavy imports, the settings and
        # the managers: an error there ends the splash too, at once.
        body = (REPO / "main.py").read_text(encoding="utf-8").split("def main()", 1)[1]
        started = body.index("splash.start(lit=bootloader.shows)")
        crash = body.index("close_on_crash(splash.stop)")
        self.assertLess(started, crash)
        self.assertLess(crash, body.index("instance = claim("))
        self.assertLess(crash, body.index("from ui.tv_window import"))

    def test_with_none_showing_the_excepthook_is_left_alone(self):
        self.addCleanup(setattr, sys, "excepthook", sys.excepthook)
        hook = sys.excepthook
        self.take({}, mock.Mock()).close_on_crash()
        self.assertIs(sys.excepthook, hook)

    def test_main_imports_pyi_splash_by_name(self):
        # PyInstaller bundles pyi_splash only where an import statement names it: without it
        # the close does nothing, and the splash stays on top for the whole run.
        tree = ast.parse((REPO / "main.py").read_text(encoding="utf-8"))
        names = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        self.assertIn("pyi_splash", names)

    def test_every_start_closes_it(self):
        body = (REPO / "main.py").read_text(encoding="utf-8").split("def main()", 1)[1]
        take = body.index("bootloader = BootloaderSplash()")
        replace = body.index("not_replaced = handle_replace_flag()")
        self.assertLess(take, replace)
        # an unhandled error closes it too, once the log's own excepthook is in place
        crash = body.index("bootloader.close_on_crash()")
        self.assertLess(body.index("log_file.install()"), crash)
        self.assertLess(take, crash)
        self.assertLess(crash, replace)
        # no splash of QiTV's own (--replace, --smoke, no screen): at once, before an update's
        # copy waits up to 30 s for the old QiTV to quit
        self.assertIn('if not with_splash:\n        bootloader.close("', body[take:replace])
        # the helper's window stands over it, or the helper ended (shown or not)
        self.assertIn('on_shown=lambda: bootloader.close("', body)
        self.assertIn('on_end=lambda: bootloader.close("', body)
        started = body[body.index("splash = Splash(") : body.index("instance = claim(")]
        self.assertIn("if not (with_splash and splash.start(lit=bootloader.shows)):", started)
        self.assertIn('bootloader.close("', started.split("splash.start(lit=", 1)[1])
        # a start that handed over: right after the claim
        handed_over = body[body.index("if handed_over:") : body.index("return 0")]
        self.assertTrue(handed_over.split("\n", 1)[1].strip().startswith("bootloader.close("))
        # once the window is on screen (it would cover it; closed before, a splash not shown
        # yet would leave an empty screen until the window is drawn), and at the end
        show = body.index("window.show()")
        shows = body[body.index("splash.before_show(window)") : show]
        self.assertNotIn('bootloader.close("', shows)
        drawn = body.index('when_drawn(window, lambda: bootloader.close("')
        self.assertLess(show, drawn)
        self.assertLess(drawn, body.index("splash.open_into(window"))
        self.assertIn('bootloader.close("', body.rsplit("finally:", 1)[1])

    def test_the_smoke_test_closes_it_first(self):
        source = (REPO / "main.py").read_text(encoding="utf-8")
        block = source.split('if __name__ == "__main__":', 1)[1]
        smoke = block[block.index("if smoke is not None:") :]
        self.assertLess(smoke.index("BootloaderSplash().close("), smoke.index("smoke_main(smoke)"))


if __name__ == "__main__":
    unittest.main()
