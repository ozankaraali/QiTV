"""QiTV registered with the system for videos, per user (services/file_types.py).

Windows' registry is a strict fake (a key with subkeys can't be deleted, missing ones raise,
writes need a handle opened for writing, names compare without case); Linux writes into
throwaway XDG folders, with xdg-mime and update-desktop-database faked on PATH (the real
xdg-mime, where installed, answers one query).
"""

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from services import file_types
from services.config_manager import ConfigManager
from services.file_types import Status
from services.open_paths import VIDEO_EXTENSIONS

HKCU_CLASSES = "Software\\Classes"
CAPABILITIES = "Software\\QiTV\\Capabilities"
WINDOWS_EXE = "C:\\Users\\Ayşe\\Apps\\Qi TV\\qitv-windows.exe"
ICON = Path(__file__).resolve().parent.parent / "assets" / "qitv.png"


# --- a strict fake winreg -------------------------------------------------------------------


class _Node:
    def __init__(self, name):
        self.name = name
        self.keys = {}  # lower-case name -> _Node
        self.values = {}  # lower-case name -> (name, type, data)


class _Handle:
    def __init__(self, node, path, access):
        self.node = node
        self.path = path
        self.access = access
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.Close()

    def Close(self):
        self.closed = True


class FakeWinreg:
    HKEY_CURRENT_USER = "HKEY_CURRENT_USER"
    HKEY_LOCAL_MACHINE = "HKEY_LOCAL_MACHINE"
    HKEY_CLASSES_ROOT = "HKEY_CLASSES_ROOT"
    KEY_QUERY_VALUE = 0x0001
    KEY_SET_VALUE = 0x0002
    KEY_CREATE_SUB_KEY = 0x0004
    KEY_ENUMERATE_SUB_KEYS = 0x0008
    KEY_READ = 0x20019
    KEY_WRITE = 0x20006
    KEY_ALL_ACCESS = 0xF003F
    REG_NONE = 0
    REG_SZ = 1
    REG_EXPAND_SZ = 2
    REG_DWORD = 4

    def __init__(self):
        self.root = _Node("HKEY_CURRENT_USER")
        self.other_roots = []  # any root but HKCU that was asked for
        self.fail_under = None  # SetValueEx below this path raises PermissionError

    # the module's functions

    def CreateKeyEx(self, key, sub_key, reserved=0, access=KEY_WRITE):
        node, path = self._walk(key, sub_key, create=True)
        return _Handle(node, path, access)

    def CreateKey(self, key, sub_key):
        return self.CreateKeyEx(key, sub_key, 0, self.KEY_ALL_ACCESS)

    def OpenKey(self, key, sub_key, reserved=0, access=KEY_READ):
        node, path = self._walk(key, sub_key, create=False)
        return _Handle(node, path, access)

    OpenKeyEx = OpenKey

    def CloseKey(self, handle):
        handle.Close()

    def SetValueEx(self, handle, name, reserved, kind, value):
        self._allowed(handle, self.KEY_SET_VALUE)
        if self.fail_under and handle.path.lower().startswith(self.fail_under.lower()):
            raise PermissionError(5, "Access is denied")
        if kind in (self.REG_SZ, self.REG_EXPAND_SZ) and not isinstance(value, str):
            raise TypeError("a string value takes a str")
        name = name or ""
        handle.node.values[name.lower()] = (name, kind, value)

    def QueryValueEx(self, handle, name):
        self._allowed(handle, self.KEY_QUERY_VALUE)
        found = handle.node.values.get((name or "").lower())
        if found is None:
            raise FileNotFoundError(2, "The system cannot find the file specified")
        return found[2], found[1]

    def DeleteValue(self, handle, name):
        self._allowed(handle, self.KEY_SET_VALUE)
        if handle.node.values.pop((name or "").lower(), None) is None:
            raise FileNotFoundError(2, "The system cannot find the file specified")

    def DeleteKey(self, key, sub_key):
        parent_path, _, leaf = sub_key.rpartition("\\")
        parent, _ = self._walk(key, parent_path, create=False)
        node = parent.keys.get(leaf.lower())
        if node is None:
            raise FileNotFoundError(2, "The system cannot find the file specified")
        if node.keys:
            raise PermissionError(5, "Access is denied")
        del parent.keys[leaf.lower()]

    def EnumKey(self, handle, index):
        self._allowed(handle, self.KEY_ENUMERATE_SUB_KEYS)
        names = [n.name for n in handle.node.keys.values()]
        if index >= len(names):
            raise OSError(259, "No more data is available")
        return names[index]

    def EnumValue(self, handle, index):
        self._allowed(handle, self.KEY_QUERY_VALUE)
        values = list(handle.node.values.values())
        if index >= len(values):
            raise OSError(259, "No more data is available")
        name, kind, data = values[index]
        return name, data, kind

    def QueryInfoKey(self, handle):
        self._allowed(handle, self.KEY_QUERY_VALUE)
        return len(handle.node.keys), len(handle.node.values), 0

    # the test's own

    def set(self, path, name, value, kind=REG_SZ):
        with self.CreateKeyEx(self.HKEY_CURRENT_USER, path, 0, self.KEY_ALL_ACCESS) as key:
            self.SetValueEx(key, name, 0, kind, value)

    def values(self, path):
        """{name: data} of the key at `path`, or None when there is no such key."""
        try:
            node, _ = self._walk(self.HKEY_CURRENT_USER, path, create=False)
        except FileNotFoundError:
            return None
        return {name: data for name, _kind, data in node.values.values()}

    def tree(self, node=None):
        node = node or self.root
        return {
            "values": {name: (kind, data) for name, kind, data in node.values.values()},
            "keys": {child.name: self.tree(child) for child in node.keys.values()},
        }

    def paths(self, node=None, prefix=""):
        """Every key's path."""
        node = node or self.root
        found = []
        for child in node.keys.values():
            path = f"{prefix}\\{child.name}" if prefix else child.name
            found.append(path)
            found.extend(self.paths(child, path))
        return found

    def _walk(self, key, sub_key, create):
        if isinstance(key, _Handle):
            if key.closed:
                raise OSError(6, "The handle is invalid")
            node, path = key.node, key.path
        elif key == self.HKEY_CURRENT_USER:
            node, path = self.root, ""
        else:
            self.other_roots.append(key)
            raise PermissionError(5, "only HKEY_CURRENT_USER in these tests")
        for part in [p for p in (sub_key or "").split("\\") if p]:
            child = node.keys.get(part.lower())
            if child is None:
                if not create:
                    raise FileNotFoundError(2, "The system cannot find the file specified")
                child = node.keys[part.lower()] = _Node(part)
            node = child
            path = f"{path}\\{child.name}" if path else child.name
        return node, path

    def _allowed(self, handle, right):
        if handle.closed:
            raise OSError(6, "The handle is invalid")
        if handle.access & right != right:
            raise PermissionError(5, "Access is denied")


class WindowsBackendTests(unittest.TestCase):
    def setUp(self):
        self.reg = FakeWinreg()
        self.notified = []
        patches = (
            patch.object(file_types, "_winreg", return_value=self.reg),
            patch.object(file_types, "_assoc_changed", side_effect=lambda: self.notified.append(1)),
        )
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def seed_other_apps(self):
        """What other apps (and Windows) keep in the same keys."""
        self.reg.set(f"{HKCU_CLASSES}\\.mkv", "", "OtherPlayer.mkv")
        self.reg.set(f"{HKCU_CLASSES}\\.mkv\\OpenWithProgids", "OtherPlayer.mkv", "")
        self.reg.set(f"{HKCU_CLASSES}\\OtherPlayer.mkv\\shell\\open\\command", "", "other %1")
        self.reg.set("Software\\RegisteredApplications", "OtherPlayer", "Software\\Other\\Caps")
        self.reg.set("Software\\QiTV\\Something", "kept", "yes")
        # an Open with → Browse… for this very exe, which Windows wrote itself
        self.reg.set(
            f"{HKCU_CLASSES}\\Applications\\qitv-windows.exe\\shell\\open\\command",
            "",
            f'"{WINDOWS_EXE}" "%1"',
        )

    def test_windows_register_writes_exactly(self):
        self.reg.set(f"{HKCU_CLASSES}\\.mkv", "", "OtherPlayer.mkv")
        self.assertTrue(file_types._windows.register("videos", WINDOWS_EXE))

        progid = f"{HKCU_CLASSES}\\QiTV.Video"
        self.assertEqual(self.reg.values(f"{progid}\\DefaultIcon"), {"": f'"{WINDOWS_EXE}",0'})
        self.assertEqual(
            self.reg.values(f"{progid}\\shell\\open\\command"), {"": f'"{WINDOWS_EXE}" "%1"'}
        )
        for ext in VIDEO_EXTENSIONS:  # ts too: Open With
            self.assertEqual(
                self.reg.values(f"{HKCU_CLASSES}\\.{ext}\\OpenWithProgids"), {"QiTV.Video": ""}
            )
        self.assertEqual(self.reg.values(f"{HKCU_CLASSES}\\.mkv"), {"": "OtherPlayer.mkv"})
        self.assertEqual(
            self.reg.values(f"{HKCU_CLASSES}\\Applications\\qitv-windows.exe\\SupportedTypes"),
            {f".{ext}": "" for ext in VIDEO_EXTENSIONS},
        )
        capabilities = self.reg.values(CAPABILITIES)
        self.assertEqual(capabilities["ApplicationName"], "QiTV")
        self.assertTrue(capabilities["ApplicationDescription"])
        associations = self.reg.values(f"{CAPABILITIES}\\FileAssociations")
        self.assertEqual(
            associations, {f".{ext}": "QiTV.Video" for ext in file_types.DEFAULT_VIDEO}
        )
        self.assertNotIn(".ts", associations)
        self.assertEqual(
            self.reg.values("Software\\RegisteredApplications"), {"QiTV": CAPABILITIES}
        )
        self.assertEqual(self.notified, [1])
        self.assertEqual(self.reg.other_roots, [])  # HKCU only

        # nothing else was written: these keys, their parents, nothing more
        written = {
            "Software",
            HKCU_CLASSES,
            progid,
            f"{progid}\\DefaultIcon",
            f"{progid}\\shell",
            f"{progid}\\shell\\open",
            f"{progid}\\shell\\open\\command",
            f"{HKCU_CLASSES}\\Applications",
            f"{HKCU_CLASSES}\\Applications\\qitv-windows.exe",
            f"{HKCU_CLASSES}\\Applications\\qitv-windows.exe\\SupportedTypes",
            "Software\\QiTV",
            CAPABILITIES,
            f"{CAPABILITIES}\\FileAssociations",
            "Software\\RegisteredApplications",
        }
        for ext in VIDEO_EXTENSIONS:
            written |= {f"{HKCU_CLASSES}\\.{ext}", f"{HKCU_CLASSES}\\.{ext}\\OpenWithProgids"}
        self.assertEqual(set(self.reg.paths()), written)
        self.assertNotIn("MultiSelectModel", json.dumps(self.reg.tree()))

    def test_windows_unregister_removes_only_ours(self):
        self.seed_other_apps()
        before = self.reg.tree()
        self.assertTrue(file_types._windows.register("videos", WINDOWS_EXE))
        self.assertNotEqual(self.reg.tree(), before)
        self.notified.clear()

        self.assertTrue(file_types._windows.unregister("videos", WINDOWS_EXE))
        self.assertEqual(self.reg.tree(), before)
        self.assertEqual(self.notified, [1])

    def test_windows_unregister_on_an_empty_registry_leaves_it_empty(self):
        self.assertTrue(file_types._windows.register("videos", WINDOWS_EXE))
        self.assertTrue(file_types._windows.unregister("videos", WINDOWS_EXE))
        # Software\Classes and Software stay (Windows' own); nothing of QiTV's
        self.assertEqual(set(self.reg.paths()), {"Software", HKCU_CLASSES})
        self.assertTrue(file_types._windows.unregister("videos", WINDOWS_EXE))  # twice: fine

    def test_windows_register_that_fails_midway_leaves_nothing(self):
        self.seed_other_apps()
        before = self.reg.tree()
        self.reg.fail_under = "Software\\RegisteredApplications"
        self.assertFalse(file_types._windows.register("videos", WINDOWS_EXE))
        self.reg.fail_under = None
        self.assertEqual(self.reg.tree(), before)

    def test_windows_renamed_exe_moves_its_supported_types(self):
        old = "C:\\Tools\\qitv-windows.exe"
        new = "C:\\Tools\\qitv-windows (1).exe"
        self.assertTrue(file_types._windows.register("videos", old))
        self.assertTrue(file_types._windows.register("videos", new, defaults=False, previous=old))
        apps = f"{HKCU_CLASSES}\\Applications"
        self.assertIsNone(self.reg.values(f"{apps}\\qitv-windows.exe"))
        supported = self.reg.values(f"{apps}\\qitv-windows (1).exe\\SupportedTypes")
        self.assertEqual(len(supported), len(VIDEO_EXTENSIONS))
        self.assertEqual(
            self.reg.values(f"{HKCU_CLASSES}\\QiTV.Video\\shell\\open\\command"),
            {"": f'"{new}" "%1"'},
        )

    def test_open_default_apps_falls_back_to_the_plain_page(self):
        opened = []

        def startfile(uri):
            opened.append(uri)
            if "?" in uri:
                raise OSError("no such page")

        with (
            patch.object(sys, "platform", "win32"),
            patch.object(os, "startfile", startfile, create=True),
        ):
            file_types.open_default_apps()
        self.assertEqual(
            opened, ["ms-settings:defaultapps?registeredAppUser=QiTV", "ms-settings:defaultapps"]
        )


# --- Linux in throwaway XDG folders ---------------------------------------------------------

FAKE_TOOL = """#!/bin/sh
echo "${0##*/} $*" >> "$QITV_FAKE_LOG"
exit "${QITV_FAKE_EXIT:-0}"
"""


def _keyfile_unescape(value):
    """A desktop file's string value as read: \\s, \\n, \\t, \\r and \\\\ undone."""
    out, i = [], 0
    while i < len(value):
        if value[i] == "\\" and i + 1 < len(value):
            out.append({"s": " ", "n": "\n", "t": "\t", "r": "\r", "\\": "\\"}[value[i + 1]])
            i += 2
        else:
            out.append(value[i])
            i += 1
    return "".join(out)


def _exec_argv(exec_value):
    """Exec's arguments as the Desktop Entry Specification reads them: string escapes undone,
    then the quoting (a quoted argument's \\", \\`, \\$ and \\\\), then %% made %. (shlex
    leaves \\$ and \\` inside double quotes as they are.)"""
    command, args, i = _keyfile_unescape(exec_value), [], 0
    while i < len(command):
        if command[i] == " ":
            i += 1
        elif command[i] == '"':
            i, arg = i + 1, []
            while command[i] != '"':
                if command[i] == "\\":
                    escaped = command[i + 1]
                    assert escaped in '"`$\\', f"a stray backslash in {command!r}"
                    arg.append(escaped)
                    i += 2
                else:
                    arg.append(command[i])
                    i += 1
            args.append("".join(arg))
            i += 1
        else:
            end = command.find(" ", i)
            end = len(command) if end < 0 else end
            args.append(command[i:end])
            i = end
    return [a.replace("%%", "%") for a in args]


GIO = shutil.which("gio")


class LinuxBackendTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.data = self.base / "data"
        self.config = self.base / "config"
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.log = self.base / "tools.log"
        env = patch.dict(
            os.environ,
            {
                "XDG_DATA_HOME": str(self.data),
                "XDG_CONFIG_HOME": str(self.config),
                "PATH": str(self.bin),
                "QITV_FAKE_LOG": str(self.log),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        self.desktop = self.data / "applications" / "qitv-open.desktop"
        self.icon = self.data / "icons" / "hicolor" / "256x256" / "apps" / "qitv.png"

    def fake_tools(self, *names):
        for name in names:
            tool = self.bin / name
            tool.write_text(FAKE_TOOL)
            tool.chmod(0o755)

    def logged(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def entry(self):
        """The desktop file's [Desktop Entry] keys."""
        lines = self.desktop.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], "[Desktop Entry]")
        return dict(line.split("=", 1) for line in lines[1:] if line)

    def test_linux_register_in_temp_xdg(self):
        self.fake_tools("xdg-mime", "update-desktop-database")
        exe = "/home/ayşe/Qi TV/qitv-linux"
        self.assertTrue(file_types._linux.register("videos", exe))

        entry = self.entry()
        self.assertEqual(entry["Type"], "Application")
        self.assertEqual(entry["Name"], "QiTV")
        self.assertEqual(entry["Exec"], f'"{exe}" %F')
        self.assertEqual(entry["Icon"], "qitv")
        self.assertEqual(entry["Categories"], "AudioVideo;Video;Player;")
        mimes = entry["MimeType"].split(";")
        self.assertEqual(mimes[-1], "")  # ends in a semicolon
        self.assertIn("video/mp2t", mimes)
        self.assertEqual(set(mimes[:-1]), set(file_types.mime_types("videos")))
        self.assertEqual(self.icon.read_bytes(), ICON.read_bytes())

        log = self.logged()
        self.assertEqual(log[0], f"update-desktop-database {self.desktop.parent}")
        defaults = [line.split() for line in log[1:]]
        self.assertTrue(
            all(d[:3] == ["xdg-mime", "default", "qitv-open.desktop"] for d in defaults)
        )
        defaulted = {d[3] for d in defaults}
        self.assertEqual(defaulted, set(file_types.default_mime_types("videos")))
        self.assertNotIn("video/mp2t", defaulted)
        self.assertIn("video/x-matroska", defaulted)

        # what xdg-mime would have written, beside another app's and the user's own lines
        mimeapps = self.config / "mimeapps.list"
        mimeapps.parent.mkdir(parents=True, exist_ok=True)
        mimeapps.write_text(
            "# mine\n"
            "[Default Applications]\n"
            "video/x-matroska=qitv-open.desktop;other.desktop;\n"
            "video/mp4=qitv-open.desktop;\n"
            "image/png=viewer.desktop;\n"
            "\n"
            "[Added Associations]\n"
            "video/x-matroska=other.desktop;qitv-open.desktop;\n"
            "video/webm=qitv-open.desktop\n"
            "[Removed Associations]\n"
            "video/ogg=qitv-open.desktop;\n"
            "text/plain=editor.desktop;\n"
        )
        mimeapps.chmod(0o644)
        self.log.unlink()

        self.assertTrue(file_types._linux.unregister("videos", exe))
        self.assertFalse(self.desktop.exists())
        self.assertFalse(self.icon.exists())
        self.assertEqual(
            mimeapps.read_text(),
            "# mine\n"
            "[Default Applications]\n"
            "video/x-matroska=other.desktop;\n"
            "image/png=viewer.desktop;\n"
            "\n"
            "[Added Associations]\n"
            "video/x-matroska=other.desktop;\n"
            "[Removed Associations]\n"
            "text/plain=editor.desktop;\n",
        )
        self.assertNotIn("qitv", mimeapps.read_text())
        self.assertEqual(stat.S_IMODE(mimeapps.stat().st_mode), 0o644)
        self.assertEqual(self.logged(), [f"update-desktop-database {self.desktop.parent}"])

    def test_unregister_edits_a_linked_list_through_the_link_and_the_old_place_too(self):
        dotfiles = self.base / "dotfiles"
        dotfiles.mkdir()
        target = dotfiles / "mimeapps.list"
        target.write_text("[Default Applications]\nvideo/mp4=qitv-open.desktop;a.desktop;\n")
        self.config.mkdir()
        (self.config / "mimeapps.list").symlink_to(target)
        old_place = self.data / "applications" / "mimeapps.list"  # xdg-utils before 1.1
        self.assertTrue(file_types._linux.register("videos", "/opt/qitv/qitv-linux"))
        old_place.write_text("[Added Associations]\nvideo/webm=qitv-open.desktop;\n")

        self.assertTrue(file_types._linux.unregister("videos", "/opt/qitv/qitv-linux"))
        self.assertTrue((self.config / "mimeapps.list").is_symlink())
        self.assertEqual(target.read_text(), "[Default Applications]\nvideo/mp4=a.desktop;\n")
        self.assertEqual(old_place.read_text(), "[Added Associations]\n")

    def test_exec_quotes_the_path_and_escapes_twice(self):
        cases = {
            "/home/user/Qi TV/qitv-linux": '"/home/user/Qi TV/qitv-linux" %F',
            "/home/user/$apps/qitv-linux": '"/home/user/\\\\$apps/qitv-linux" %F',
            "/opt/100%/qitv-linux": '"/opt/100%%/qitv-linux" %F',
            '/opt/say "hi"/qitv-linux': '"/opt/say \\\\"hi\\\\"/qitv-linux" %F',
            "/opt/back\\slash/qitv-linux": '"/opt/back\\\\\\\\slash/qitv-linux" %F',
            "/opt/tick`s/qitv-linux": '"/opt/tick\\\\`s/qitv-linux" %F',
        }
        for exe, expected in cases.items():
            with self.subTest(exe=exe):
                self.assertTrue(file_types._linux.register("videos", exe))
                exec_value = self.entry()["Exec"]
                self.assertEqual(exec_value, expected)
                self.assertEqual(_exec_argv(exec_value), [exe, "%F"])

    @unittest.skipUnless(GIO, "GLib's gio is not installed here")
    def test_glib_starts_the_path_written_with_the_files(self):
        # GLib's own launcher reads the file: a folder with a space, $, `, \ and " in its name
        folder = self.base / 'Qi $TV `x` \\ "y"'
        folder.mkdir()
        argv_log = self.base / "argv"
        exe = folder / "qitv-linux"
        exe.write_text(
            f'#!/bin/sh\nfor a in "$0" "$@"; do printf "%s\\n" "$a"; done > "{argv_log}"\n'
        )
        exe.chmod(0o755)
        film = self.base / "Dizi Bölüm $1.mkv"
        film.touch()
        self.assertTrue(file_types._linux.register("videos", str(exe)))
        subprocess.run([GIO, "launch", str(self.desktop), str(film)], timeout=10, check=True)
        for _ in range(50):
            if argv_log.exists() and argv_log.read_text().count("\n") == 2:
                break
            time.sleep(0.1)  # the launched script runs on its own
        self.assertEqual(argv_log.read_text(encoding="utf-8").splitlines(), [str(exe), str(film)])

    def test_missing_tools_are_skipped(self):
        with self.assertLogs("services.file_types", "INFO") as logs:
            self.assertTrue(file_types._linux.register("videos", "/opt/qitv/qitv-linux"))
        self.assertTrue(self.desktop.exists())
        self.assertTrue(any("xdg-mime" in line for line in logs.output))
        self.assertTrue(file_types._linux.unregister("videos", "/opt/qitv/qitv-linux"))
        self.assertFalse(self.desktop.exists())

    def test_xdg_mime_failing_for_every_type_takes_it_all_back(self):
        self.fake_tools("xdg-mime", "update-desktop-database")
        with patch.dict(os.environ, {"QITV_FAKE_EXIT": "4"}):
            self.assertFalse(file_types._linux.register("videos", "/opt/qitv/qitv-linux"))
        self.assertFalse(self.desktop.exists())
        self.assertFalse(self.icon.exists())

    def test_refresh_rewrites_the_file_without_taking_defaults_again(self):
        self.fake_tools("xdg-mime", "update-desktop-database")
        self.assertTrue(
            file_types._linux.register(
                "videos", "/b/qitv-linux", defaults=False, previous="/a/qitv-linux"
            )
        )
        self.assertEqual(self.entry()["Exec"], '"/b/qitv-linux" %F')
        self.assertFalse([line for line in self.logged() if line.startswith("xdg-mime")])

    def test_an_icon_qitv_did_not_put_there_stays(self):
        # scripts/install-linux.sh puts the same logo in the same place
        self.icon.parent.mkdir(parents=True)
        self.icon.write_bytes(b"the installer's")
        self.assertTrue(file_types._linux.register("videos", "/opt/qitv/qitv-linux"))
        self.assertTrue(file_types._linux.register("videos", "/opt/qitv/qitv-linux"))
        self.assertTrue(file_types._linux.unregister("videos", "/opt/qitv/qitv-linux"))
        self.assertEqual(self.icon.read_bytes(), b"the installer's")

    @unittest.skipUnless(shutil.which("xdg-mime"), "xdg-mime is not installed here")
    def test_linux_real_xdg_mime_when_present(self):
        real_path = os.pathsep.join([os.path.dirname(shutil.which("xdg-mime")), "/usr/bin", "/bin"])
        quiet = {"XDG_CURRENT_DESKTOP": "", "DESKTOP_SESSION": "", "KDE_FULL_SESSION": ""}
        with patch.dict(os.environ, {"PATH": real_path, **quiet}):

            def query():
                return subprocess.run(
                    ["xdg-mime", "query", "default", "video/x-matroska"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                ).stdout.strip()

            self.assertTrue(file_types._linux.register("videos", "/opt/qitv/qitv-linux"))
            self.assertEqual(query(), "qitv-open.desktop")
            self.assertTrue(file_types._linux.unregister("videos", "/opt/qitv/qitv-linux"))
            self.assertNotEqual(query(), "qitv-open.desktop")


# --- the service --------------------------------------------------------------------------


class RecordingBackend:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    def register(self, kind, exe, **options):
        self.calls.append(("register", kind, exe, options))
        return self.ok

    def unregister(self, kind, exe):
        self.calls.append(("unregister", kind, exe))
        return self.ok


class Config:
    def __init__(self, file_types=None):
        self.file_types = file_types or {}


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.backend = RecordingBackend()
        p = patch.object(file_types, "_backend", side_effect=lambda: self.backend)
        p.start()
        self.addCleanup(p.stop)

    def test_kinds_and_types(self):
        self.assertEqual(file_types.KINDS, ("videos",))
        self.assertEqual(file_types.DEFAULT_VIDEO, VIDEO_EXTENSIONS - {"ts"})
        self.assertEqual(file_types.MAC_DEFAULT_VIDEO, ("mkv", "mp4", "mov", "avi", "webm"))
        for ext in VIDEO_EXTENSIONS:
            self.assertTrue(file_types.MIME_TYPES[ext], ext)
        # .m2ts and .mts are video/mp2t too: like .ts, Open With only on Linux
        self.assertNotIn("video/mp2t", file_types.default_mime_types("videos"))
        self.assertIn("video/mp2t", file_types.mime_types("videos"))
        for both in ("video/vnd.avi", "video/x-msvideo", "video/mp4", "video/x-m4v"):
            self.assertIn(both, file_types.default_mime_types("videos"))

    def test_temporary_locations(self):
        temporary = [
            os.path.join(tempfile.gettempdir(), "qitv", "qitv-linux"),
            "C:\\Users\\x\\AppData\\Local\\Temp\\Temp1_qitv.zip\\qitv-windows.exe",
            "c:\\users\\X\\appdata\\local\\temp\\7zO12\\qitv-windows.exe",
            "/private/var/folders/ab/cd/T/AppTranslocation/1A2B/d/QiTV.app/Contents/MacOS/QiTV",
            "C:\\Users\\x\\Downloads\\qitv.zip\\qitv-windows.exe",
            "C:\\Users\\x\\Downloads\\QiTV.ZIP\\inner\\qitv-windows.exe",
            "/tmp/qitv-linux",
        ]
        lasting = [
            "/opt/qitv/qitv-linux",
            "C:\\Tools\\QiTV\\qitv-windows.exe",
            "C:\\Users\\x\\Downloads\\qitv-windows.exe",
            "C:\\zipper\\qitv-windows.exe",
            "/home/user/Templates/qitv-linux",
            "/home/user/tmpfiles/qitv-linux",
            "",
        ]
        no_io = AssertionError("temporary_location touched the disk")
        with (
            patch.object(tempfile, "gettempdir", side_effect=no_io),
            patch.object(os, "stat", side_effect=no_io),
            patch.object(os, "lstat", side_effect=no_io),
            patch.object(os.path, "realpath", side_effect=no_io),
        ):
            for exe in temporary:
                self.assertTrue(file_types.temporary_location(exe), exe)
            for exe in lasting:
                self.assertFalse(file_types.temporary_location(exe), exe)
            with patch.dict(os.environ, {"LOCALAPPDATA": "D:\\Profiles\\x\\Local"}):
                self.assertTrue(
                    file_types.temporary_location("D:\\Profiles\\x\\Local\\Temp\\a\\qitv.exe")
                )
                self.assertFalse(
                    file_types.temporary_location("D:\\Profiles\\x\\Local\\Programs\\qitv.exe")
                )

    def test_source_run_never_registers(self):
        config = Config({"videos": "/a/qitv"})
        with patch.object(sys, "executable", "/opt/qitv/qitv-linux"):
            self.assertIs(file_types.status("videos", config, frozen=False), Status.UNAVAILABLE)
        self.assertFalse(file_types.register("videos", config, "/b/qitv", frozen=False))
        file_types.refresh(config, "/b/qitv", frozen=False)
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(config.file_types, {"videos": "/a/qitv"})

    def test_status_follows_the_record(self):
        config = Config()
        with patch.object(sys, "executable", "/opt/qitv/qitv-linux"):
            self.assertIs(file_types.status("videos", config, frozen=True), Status.NOT_REGISTERED)
            self.assertTrue(file_types.register("videos", config, "/opt/qitv/qitv-linux", True))
            self.assertIs(file_types.status("videos", config, frozen=True), Status.REGISTERED)
        with patch.object(sys, "executable", "/tmp/qitv-linux"):
            self.assertIs(file_types.status("videos", config, frozen=True), Status.TEMPORARY)
        self.backend = None  # a system without a backend (macOS until its own)
        self.assertIs(file_types.status("videos", config, frozen=True), Status.UNAVAILABLE)

    def test_register_records_and_unregister_forgets(self):
        config = Config()
        self.assertTrue(file_types.register("videos", config, "/opt/qitv/qitv-linux", True))
        self.assertEqual(config.file_types, {"videos": "/opt/qitv/qitv-linux"})
        self.assertEqual(self.backend.calls, [("register", "videos", "/opt/qitv/qitv-linux", {})])
        self.backend.calls.clear()
        with patch.object(sys, "executable", "/elsewhere/qitv-linux"):
            self.assertTrue(file_types.unregister("videos", config))
        # what was registered is what's removed
        self.assertEqual(self.backend.calls, [("unregister", "videos", "/opt/qitv/qitv-linux")])
        self.assertEqual(config.file_types, {})

    def test_a_failure_changes_no_record(self):
        config = Config()
        self.backend.ok = False
        self.assertFalse(file_types.register("videos", config, "/opt/qitv/qitv-linux", True))
        self.assertEqual(config.file_types, {})
        config.file_types = {"videos": "/opt/qitv/qitv-linux"}
        self.assertFalse(file_types.unregister("videos", config))
        self.assertEqual(config.file_types, {"videos": "/opt/qitv/qitv-linux"})

    def test_a_backend_that_raises_answers_false(self):
        config = Config()
        self.backend.register = lambda *a, **k: 1 / 0
        with self.assertLogs("services.file_types", "WARNING"):
            self.assertFalse(file_types.register("videos", config, "/opt/qitv/qitv-linux", True))
        self.assertEqual(config.file_types, {})

    def test_a_temporary_exe_is_never_registered(self):
        config = Config()
        self.assertFalse(file_types.register("videos", config, "/tmp/x/qitv-linux", True))
        self.assertEqual(self.backend.calls, [])

    def test_refresh_moves_registration(self):
        config = Config({"videos": "/a/qitv"})
        file_types.refresh(config, "/b/qitv", frozen=True)
        self.assertEqual(
            self.backend.calls,
            [("register", "videos", "/b/qitv", {"defaults": False, "previous": "/a/qitv"})],
        )
        self.assertEqual(config.file_types, {"videos": "/b/qitv"})

        self.backend.calls.clear()
        file_types.refresh(config, "/b/qitv", frozen=True)  # the same path: nothing to do
        self.assertEqual(self.backend.calls, [])

        file_types.refresh(config, os.path.join(tempfile.gettempdir(), "qitv"), frozen=True)
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(config.file_types, {"videos": "/b/qitv"})

        self.backend.ok = False
        file_types.refresh(config, "/c/qitv", frozen=True)
        self.assertEqual(config.file_types, {"videos": "/b/qitv"})  # tried again next start

        file_types.refresh(Config(), "/c/qitv", frozen=True)  # nothing registered: nothing
        self.assertEqual(len(self.backend.calls), 1)

    def test_the_record_reaches_config_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text("{}", encoding="utf-8")
            with patch.object(ConfigManager, "_get_config_path", return_value=str(path)):
                config = ConfigManager()
            self.assertTrue(file_types.register("videos", config, "/opt/qitv/qitv-linux", True))
            config.save_config(wait=True)
            written = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(written["file_types"], {"videos": "/opt/qitv/qitv-linux"})
            # another config without the key shares nothing with this one
            other = Path(directory) / "other.json"
            other.write_text("{}", encoding="utf-8")
            with patch.object(ConfigManager, "_get_config_path", return_value=str(other)):
                self.assertEqual(ConfigManager().file_types, {})
            self.assertTrue(file_types.unregister("videos", config))
            config.save_config(wait=True)
            written = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(written["file_types"], {})


if __name__ == "__main__":
    unittest.main()
