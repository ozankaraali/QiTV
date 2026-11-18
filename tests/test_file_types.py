"""QiTV registered with the system for videos and for music, per user (services/file_types.py).
Each backend's tests run for both kinds; the *KindsTests check that the two stay independent
and that playlists are Open With only.

Windows' registry is a strict fake (a key with subkeys can't be deleted, missing ones raise,
writes need a handle opened for writing, names compare without case); Linux writes into
throwaway XDG folders, with xdg-mime and update-desktop-database faked on PATH (the real
xdg-mime, where installed, answers one query); macOS's Launch Services is a fake that records
its calls (the real one is never loaded: CI runs these tests on macOS too).
"""

import ctypes
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from services import file_types, open_paths
from services.config_manager import ConfigManager
from services.file_types import Status
from services.open_paths import MUSIC_EXTENSIONS, VIDEO_EXTENSIONS

HKCU_CLASSES = "Software\\Classes"
CAPABILITIES = "Software\\QiTV\\Capabilities"
WINDOWS_EXE = "C:\\Users\\Ayşe\\Apps\\Qi TV\\qitv-windows.exe"
ICON = Path(__file__).resolve().parent.parent / "assets" / "qitv.png"
MAC_APP = "/Applications/QiTV.app"
PLAYLISTS = frozenset({"m3u", "m3u8"})  # Open With with either kind, never the default
PROGIDS = {"videos": "QiTV.Video", "music": "QiTV.Audio"}
PLAYLIST_MIMES = {"audio/x-mpegurl", "application/vnd.apple.mpegurl"}

# the real loader, for the one test that reads the C types it gives (with ctypes faked)
_REAL_CORE_SERVICES = file_types._core_services


def setUpModule():
    # Never the real Launch Services: on a Mac (CI runs these tests on macOS) a call would
    # change that machine's default apps. The macOS tests put their fake over this.
    guard = patch.object(
        file_types,
        "_core_services",
        side_effect=AssertionError("a test reached the real CoreServices"),
    )
    guard.start()
    unittest.addModuleCleanup(guard.stop)


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
        # below this path, the operations in `failing` raise PermissionError: "set"
        # (SetValueEx), "open" (OpenKey) and "delete" (DeleteValue, DeleteKey)
        self.fail_under = None
        self.failing = {"set"}

    # the module's functions

    def CreateKeyEx(self, key, sub_key, reserved=0, access=KEY_WRITE):
        node, path = self._walk(key, sub_key, create=True)
        return _Handle(node, path, access)

    def CreateKey(self, key, sub_key):
        return self.CreateKeyEx(key, sub_key, 0, self.KEY_ALL_ACCESS)

    def OpenKey(self, key, sub_key, reserved=0, access=KEY_READ):
        node, path = self._walk(key, sub_key, create=False)
        self._fail("open", path)
        return _Handle(node, path, access)

    OpenKeyEx = OpenKey

    def CloseKey(self, handle):
        handle.Close()

    def SetValueEx(self, handle, name, reserved, kind, value):
        self._allowed(handle, self.KEY_SET_VALUE)
        self._fail("set", handle.path)
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
        self._fail("delete", handle.path)
        if handle.node.values.pop((name or "").lower(), None) is None:
            raise FileNotFoundError(2, "The system cannot find the file specified")

    def DeleteKey(self, key, sub_key):
        self._fail("delete", sub_key)
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

    def _fail(self, operation, path):
        under = self.fail_under
        if under and operation in self.failing and path.lower().startswith(under.lower()):
            raise PermissionError(5, "Access is denied")


class _FakeRegistry(unittest.TestCase):
    def setUp(self):
        self.reg = FakeWinreg()
        self.notified: list[int] = []
        patches = (
            patch.object(file_types, "_winreg", return_value=self.reg),
            patch.object(file_types, "_assoc_changed", side_effect=lambda: self.notified.append(1)),
        )
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def seed_other_apps(self, seeded="mkv"):
        """What other apps (and Windows) keep in the same keys."""
        self.reg.set(f"{HKCU_CLASSES}\\.{seeded}", "", f"OtherPlayer.{seeded}")
        self.reg.set(f"{HKCU_CLASSES}\\.{seeded}\\OpenWithProgids", f"OtherPlayer.{seeded}", "")
        self.reg.set(f"{HKCU_CLASSES}\\OtherPlayer.{seeded}\\shell\\open\\command", "", "other %1")
        self.reg.set("Software\\RegisteredApplications", "OtherPlayer", "Software\\Other\\Caps")
        self.reg.set("Software\\QiTV\\Something", "kept", "yes")
        # an Open with → Browse… for this very exe, which Windows wrote itself
        self.reg.set(
            f"{HKCU_CLASSES}\\Applications\\qitv-windows.exe\\shell\\open\\command",
            "",
            f'"{WINDOWS_EXE}" "%1"',
        )


class WindowsBackendTests(_FakeRegistry):
    """One kind's registration (videos here, music in the subclass)."""

    KIND = "videos"
    PROGID = "QiTV.Video"
    TYPES = VIDEO_EXTENSIONS
    DEFAULTS = VIDEO_EXTENSIONS - {"ts"}  # .ts is Open With only
    SEEDED = "mkv"  # a default type another app holds too, mid-way through the sorted types

    @property
    def every(self):
        return self.TYPES | PLAYLISTS

    def seed_other_apps(self, seeded=None):
        super().seed_other_apps(seeded or self.SEEDED)

    def test_windows_register_writes_exactly(self):
        seeded = self.SEEDED
        self.reg.set(f"{HKCU_CLASSES}\\.{seeded}", "", f"OtherPlayer.{seeded}")
        self.assertTrue(file_types._windows.register(self.KIND, WINDOWS_EXE))

        progid = f"{HKCU_CLASSES}\\{self.PROGID}"
        self.assertEqual(self.reg.values(f"{progid}\\DefaultIcon"), {"": f'"{WINDOWS_EXE}",0'})
        self.assertEqual(
            self.reg.values(f"{progid}\\shell\\open\\command"), {"": f'"{WINDOWS_EXE}" "%1"'}
        )
        for ext in self.every:  # ts and the playlists too: Open With
            self.assertEqual(
                self.reg.values(f"{HKCU_CLASSES}\\.{ext}\\OpenWithProgids"), {self.PROGID: ""}
            )
        self.assertEqual(
            self.reg.values(f"{HKCU_CLASSES}\\.{seeded}"), {"": f"OtherPlayer.{seeded}"}
        )
        self.assertEqual(
            self.reg.values(f"{HKCU_CLASSES}\\Applications\\qitv-windows.exe\\SupportedTypes"),
            {f".{ext}": "" for ext in self.every},
        )
        capabilities = self.reg.values(CAPABILITIES)
        self.assertEqual(capabilities["ApplicationName"], "QiTV")
        self.assertTrue(capabilities["ApplicationDescription"])
        associations = self.reg.values(f"{CAPABILITIES}\\FileAssociations")
        self.assertEqual(associations, {f".{ext}": self.PROGID for ext in self.DEFAULTS})
        for ext in self.every - self.DEFAULTS:
            self.assertNotIn(f".{ext}", associations)
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
        for ext in self.every:
            written |= {f"{HKCU_CLASSES}\\.{ext}", f"{HKCU_CLASSES}\\.{ext}\\OpenWithProgids"}
        self.assertEqual(set(self.reg.paths()), written)
        self.assertNotIn("MultiSelectModel", json.dumps(self.reg.tree()))

    def test_windows_unregister_removes_only_ours(self):
        self.seed_other_apps()
        before = self.reg.tree()
        self.assertTrue(file_types._windows.register(self.KIND, WINDOWS_EXE))
        self.assertNotEqual(self.reg.tree(), before)
        self.notified.clear()

        self.assertTrue(file_types._windows.unregister(self.KIND, WINDOWS_EXE))
        self.assertEqual(self.reg.tree(), before)
        self.assertEqual(self.notified, [1])

    def test_windows_unregister_on_an_empty_registry_leaves_it_empty(self):
        self.assertTrue(file_types._windows.register(self.KIND, WINDOWS_EXE))
        self.assertTrue(file_types._windows.unregister(self.KIND, WINDOWS_EXE))
        # Software\Classes and Software stay (Windows' own); nothing of QiTV's
        self.assertEqual(set(self.reg.paths()), {"Software", HKCU_CLASSES})
        self.assertTrue(file_types._windows.unregister(self.KIND, WINDOWS_EXE))  # twice: fine

    def test_windows_register_that_fails_midway_leaves_nothing(self):
        self.seed_other_apps()
        before = self.reg.tree()
        self.reg.fail_under = "Software\\RegisteredApplications"
        self.assertFalse(file_types._windows.register(self.KIND, WINDOWS_EXE))
        self.reg.fail_under = None
        self.assertEqual(self.reg.tree(), before)

    def test_windows_renamed_exe_moves_its_supported_types(self):
        old = "C:\\Tools\\qitv-windows.exe"
        new = "C:\\Tools\\qitv-windows (1).exe"
        self.assertTrue(file_types._windows.register(self.KIND, old))
        self.assertTrue(file_types._windows.register(self.KIND, new, defaults=False, previous=old))
        apps = f"{HKCU_CLASSES}\\Applications"
        self.assertIsNone(self.reg.values(f"{apps}\\qitv-windows.exe"))
        supported = self.reg.values(f"{apps}\\qitv-windows (1).exe\\SupportedTypes")
        self.assertEqual(len(supported), len(self.every))
        self.assertEqual(
            self.reg.values(f"{HKCU_CLASSES}\\{self.PROGID}\\shell\\open\\command"),
            {"": f'"{new}" "%1"'},
        )

    def test_windows_unregister_keeps_going_past_a_key_it_cannot_change(self):
        self.seed_other_apps()
        before = self.reg.tree()
        self.assertTrue(file_types._windows.register(self.KIND, WINDOWS_EXE))
        # the seeded type sits mid-way through the types (sorted): its key can't be opened or
        # deleted
        seeded = self.SEEDED
        self.reg.fail_under = f"{HKCU_CLASSES}\\.{seeded}"
        self.reg.failing = {"open", "delete"}
        with self.assertLogs("services.file_types", "WARNING"):
            self.assertFalse(file_types._windows.unregister(self.KIND, WINDOWS_EXE))
        self.assertEqual(
            self.reg.values(f"{HKCU_CLASSES}\\.{seeded}\\OpenWithProgids"),
            {f"OtherPlayer.{seeded}": "", self.PROGID: ""},
        )
        # everything else of QiTV's went, the types after the seeded one too
        self.assertIsNone(self.reg.values(f"{HKCU_CLASSES}\\{self.PROGID}"))
        for ext in self.every - {seeded}:
            self.assertIsNone(self.reg.values(f"{HKCU_CLASSES}\\.{ext}"), ext)
        self.assertIsNone(
            self.reg.values(f"{HKCU_CLASSES}\\Applications\\qitv-windows.exe\\SupportedTypes")
        )
        self.assertIsNone(self.reg.values(CAPABILITIES))
        self.assertEqual(
            self.reg.values("Software\\RegisteredApplications"),
            {"OtherPlayer": "Software\\Other\\Caps"},
        )
        self.assertEqual(self.notified, [1, 1])

        self.reg.fail_under = None  # tried again: the rest goes
        self.assertTrue(file_types._windows.unregister(self.KIND, WINDOWS_EXE))
        self.assertEqual(self.reg.tree(), before)

    def test_a_first_register_that_fails_takes_back_what_it_could(self):
        self.seed_other_apps()
        before = self.reg.tree()
        self.reg.fail_under = "Software\\RegisteredApplications"
        self.reg.failing = {"set", "open"}  # the take-back can't open it either
        with self.assertLogs("services.file_types", "WARNING"):
            self.assertFalse(file_types._windows.register(self.KIND, WINDOWS_EXE))
        self.reg.fail_under = None
        self.assertEqual(self.reg.tree(), before)

    def test_register_again_that_fails_keeps_the_working_registration(self):
        config = Config({self.KIND: WINDOWS_EXE})
        self.seed_other_apps()
        self.assertTrue(file_types._windows.register(self.KIND, WINDOWS_EXE))
        working = self.reg.tree()
        self.reg.fail_under = "Software\\RegisteredApplications"
        with patch.object(file_types, "_backend", return_value=file_types._windows):
            with self.assertLogs("services.file_types", "WARNING"):
                self.assertFalse(file_types.register(self.KIND, config, WINDOWS_EXE, frozen=True))
        self.reg.fail_under = None
        self.assertEqual(self.reg.tree(), working)
        self.assertEqual(config.file_types, {self.KIND: WINDOWS_EXE})

    def test_a_renamed_exe_that_fails_leaves_only_the_working_entry(self):
        old = "C:\\Tools\\qitv-windows.exe"
        new = "C:\\Tools\\qitv-windows-2.10.2.exe"
        self.assertTrue(file_types._windows.register(self.KIND, old))
        self.reg.fail_under = "Software\\RegisteredApplications"
        with self.assertLogs("services.file_types", "WARNING"):
            self.assertFalse(file_types._windows.register(self.KIND, new, previous=old))
        self.reg.fail_under = None
        apps = f"{HKCU_CLASSES}\\Applications"
        self.assertIsNone(self.reg.values(f"{apps}\\qitv-windows-2.10.2.exe"))  # none of it
        self.assertEqual(
            len(self.reg.values(f"{apps}\\qitv-windows.exe\\SupportedTypes")),
            len(self.every),
        )

    def test_a_moved_exe_that_fails_keeps_the_entry_it_shares(self):
        # the same file name: Applications\<name> is the working registration's too
        old = "C:\\Tools\\qitv-windows.exe"
        new = "D:\\Apps\\QITV-WINDOWS.EXE"
        self.assertTrue(file_types._windows.register(self.KIND, old))
        self.reg.fail_under = "Software\\RegisteredApplications"
        with self.assertLogs("services.file_types", "WARNING"):
            self.assertFalse(file_types._windows.register(self.KIND, new, previous=old))
        self.reg.fail_under = None
        apps = f"{HKCU_CLASSES}\\Applications"
        supported = self.reg.values(f"{apps}\\qitv-windows.exe\\SupportedTypes")
        self.assertEqual(len(supported), len(self.every))

    def test_register_from_a_renamed_exe_moves_its_supported_types(self):
        old = "C:\\Tools\\qitv-windows.exe"
        new = "C:\\Tools\\qitv-windows-2.10.2.exe"
        config = Config({self.KIND: old})
        self.assertTrue(file_types._windows.register(self.KIND, old))
        apps = f"{HKCU_CLASSES}\\Applications"
        with patch.object(file_types, "_backend", return_value=file_types._windows):
            # a failure keeps the old name's entry: it's what still works
            self.reg.fail_under = "Software\\RegisteredApplications"
            with self.assertLogs("services.file_types", "WARNING"):
                self.assertFalse(file_types.register(self.KIND, config, new, frozen=True))
            self.assertTrue(self.reg.values(f"{apps}\\qitv-windows.exe\\SupportedTypes"))
            self.assertIsNone(self.reg.values(f"{apps}\\qitv-windows-2.10.2.exe"))
            self.reg.fail_under = None

            self.assertTrue(file_types.register(self.KIND, config, new, frozen=True))
        self.assertIsNone(self.reg.values(f"{apps}\\qitv-windows.exe"))
        self.assertEqual(
            len(self.reg.values(f"{apps}\\qitv-windows-2.10.2.exe\\SupportedTypes")),
            len(self.every),
        )
        self.assertEqual(config.file_types, {self.KIND: new})

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


class MusicWindowsBackendTests(WindowsBackendTests):
    KIND = "music"
    PROGID = "QiTV.Audio"
    TYPES = MUSIC_EXTENSIONS
    DEFAULTS = MUSIC_EXTENSIONS  # every music type may become QiTV's
    SEEDED = "mp3"


class WindowsKindsTests(_FakeRegistry):
    """Videos and music on one registry: each comes and goes without touching the other."""

    def register(self, kind, exe=WINDOWS_EXE):
        self.assertTrue(file_types._windows.register(kind, exe))

    def unregister(self, kind, exe=WINDOWS_EXE):
        self.assertTrue(file_types._windows.unregister(kind, exe))

    def fresh_registry(self):
        """An empty registry from now on (the one setUp gave stays patched under it)."""
        self.reg = FakeWinreg()
        p = patch.object(file_types, "_winreg", return_value=self.reg)
        p.start()
        self.addCleanup(p.stop)

    def test_rows_are_independent(self):
        for first, second in (("videos", "music"), ("music", "videos")):
            with self.subTest(first=first):
                self.fresh_registry()
                self.seed_other_apps("mkv")
                self.seed_other_apps("mp3")
                before = self.reg.tree()
                self.register(first)
                only_first = self.reg.tree()
                self.register(second)
                for kind in (first, second):
                    command = f"{HKCU_CLASSES}\\{PROGIDS[kind]}\\shell\\open\\command"
                    self.assertEqual(self.reg.values(command), {"": f'"{WINDOWS_EXE}" "%1"'})
                associations = self.reg.values(f"{CAPABILITIES}\\FileAssociations")
                self.assertEqual(associations[".mkv"], "QiTV.Video")
                self.assertEqual(associations[".mp3"], "QiTV.Audio")
                self.unregister(second)  # the first stays exactly as it was
                self.assertEqual(self.reg.tree(), only_first)
                self.assertEqual(
                    self.reg.values("Software\\RegisteredApplications")["QiTV"], CAPABILITIES
                )
                self.unregister(first)  # the last one: QiTV leaves Default Programs
                self.assertEqual(self.reg.tree(), before)

        # both registered, the first one taken back: what's left is the second alone
        alone = FakeWinreg()
        with patch.object(file_types, "_winreg", return_value=alone):
            self.register("music")
        self.fresh_registry()
        self.register("videos")
        self.register("music")
        self.unregister("videos")
        self.assertEqual(self.reg.tree(), alone.tree())

    def test_playlists_are_open_with_only(self):
        self.register("videos")
        self.register("music")
        supported = f"{HKCU_CLASSES}\\Applications\\qitv-windows.exe\\SupportedTypes"
        associations = self.reg.values(f"{CAPABILITIES}\\FileAssociations")
        for ext in PLAYLISTS:
            self.assertEqual(
                self.reg.values(f"{HKCU_CLASSES}\\.{ext}\\OpenWithProgids"),
                {"QiTV.Video": "", "QiTV.Audio": ""},
            )
            self.assertIn(f".{ext}", self.reg.values(supported))
            self.assertNotIn(f".{ext}", associations)  # never offered as the default
        self.unregister("videos")  # music still lists them
        for ext in PLAYLISTS:
            self.assertEqual(
                self.reg.values(f"{HKCU_CLASSES}\\.{ext}\\OpenWithProgids"), {"QiTV.Audio": ""}
            )
            self.assertIn(f".{ext}", self.reg.values(supported))
        self.unregister("music")
        for ext in PLAYLISTS:
            self.assertIsNone(self.reg.values(f"{HKCU_CLASSES}\\.{ext}"))

    def test_a_renamed_exe_keeps_the_playlists_the_other_kind_still_lists(self):
        old = "C:\\Tools\\qitv-windows.exe"
        new = "C:\\Tools\\qitv-windows-2.10.2.exe"
        self.register("videos", old)
        self.register("music", old)
        apps = f"{HKCU_CLASSES}\\Applications"
        # a start's refresh moves one kind, then the other
        self.assertTrue(file_types._windows.register("videos", new, defaults=False, previous=old))
        # music still opens with the old name, and so do the playlists
        old_types = self.reg.values(f"{apps}\\qitv-windows.exe\\SupportedTypes")
        self.assertEqual(set(old_types), {f".{ext}" for ext in MUSIC_EXTENSIONS | PLAYLISTS})
        self.assertTrue(file_types._windows.register("music", new, defaults=False, previous=old))
        self.assertIsNone(self.reg.values(f"{apps}\\qitv-windows.exe"))
        self.assertEqual(
            set(self.reg.values(f"{apps}\\qitv-windows-2.10.2.exe\\SupportedTypes")),
            {f".{ext}" for ext in VIDEO_EXTENSIONS | MUSIC_EXTENSIONS | PLAYLISTS},
        )

    def test_a_first_register_that_fails_keeps_the_other_kind(self):
        self.register("videos")
        working = self.reg.tree()
        self.reg.fail_under = "Software\\RegisteredApplications"
        with self.assertLogs("services.file_types", "WARNING"):
            self.assertFalse(file_types._windows.register("music", WINDOWS_EXE))
        self.reg.fail_under = None
        self.assertEqual(self.reg.tree(), working)


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


# GLib's launcher and xdg-mime read desktop files on Linux only (the unit tests also run on
# macOS in CI, where a gio has no desktop files to launch).
LINUX = sys.platform.startswith("linux")
GIO = shutil.which("gio") if LINUX else None
XDG_MIME = shutil.which("xdg-mime") if LINUX else None


@unittest.skipIf(os.name == "nt", "the fake tools are sh scripts")
class _XdgFolders(unittest.TestCase):
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


class LinuxBackendTests(_XdgFolders):
    """One kind's registration (videos here, music in the subclass)."""

    KIND = "videos"
    OPEN_WITH_MIME = "video/mp2t"  # .ts's: in Open With, never made the default
    DEFAULT_MIME = "video/x-matroska"
    # the fixture's types: a default other apps share, one QiTV alone has, one in Added
    # Associations only, one the user removed
    SHARED, OWN, ADDED, REMOVED = "video/x-matroska", "video/mp4", "video/webm", "video/ogg"

    def test_linux_register_in_temp_xdg(self):
        self.fake_tools("xdg-mime", "update-desktop-database")
        exe = "/home/ayşe/Qi TV/qitv-linux"
        result = file_types._linux.register(self.KIND, exe)
        self.assertTrue(result)
        self.assertTrue(result.defaults)

        entry = self.entry()
        self.assertEqual(entry["Type"], "Application")
        self.assertEqual(entry["Name"], "QiTV")
        self.assertEqual(entry["Exec"], f'"{exe}" %F')
        self.assertEqual(entry["Icon"], "qitv")
        self.assertEqual(entry["Categories"], "AudioVideo;Audio;Video;Player;")  # music too
        self.assertEqual(entry["Comment"], "Video, music and IPTV player")
        mimes = entry["MimeType"].split(";")
        self.assertEqual(mimes[-1], "")  # ends in a semicolon
        self.assertIn(self.OPEN_WITH_MIME, mimes)
        self.assertLessEqual(PLAYLIST_MIMES, set(mimes))  # playlists: Open With
        self.assertEqual(set(mimes[:-1]), set(file_types.mime_types(self.KIND)))
        self.assertEqual(self.icon.read_bytes(), ICON.read_bytes())

        log = self.logged()
        self.assertEqual(log[0], f"update-desktop-database {self.desktop.parent}")
        defaults = [line.split() for line in log[1:]]
        self.assertTrue(
            all(d[:3] == ["xdg-mime", "default", "qitv-open.desktop"] for d in defaults)
        )
        defaulted = {d[3] for d in defaults}
        self.assertEqual(defaulted, set(file_types.default_mime_types(self.KIND)))
        self.assertNotIn(self.OPEN_WITH_MIME, defaulted)
        self.assertFalse(PLAYLIST_MIMES & defaulted)
        self.assertIn(self.DEFAULT_MIME, defaulted)

        # what xdg-mime would have written, beside another app's and the user's own lines
        mimeapps = self.config / "mimeapps.list"
        mimeapps.parent.mkdir(parents=True, exist_ok=True)
        shared, own, added, removed = self.SHARED, self.OWN, self.ADDED, self.REMOVED
        mimeapps.write_text(
            "# mine\n"
            "[Default Applications]\n"
            f"{shared}=qitv-open.desktop;other.desktop;\n"
            f"{own}=qitv-open.desktop;\n"
            "image/png=viewer.desktop;\n"
            "\n"
            "[Added Associations]\n"
            f"{shared}=other.desktop;qitv-open.desktop;\n"
            f"{added}=qitv-open.desktop\n"
            "[Removed Associations]\n"
            f"{removed}=qitv-open.desktop;\n"
            "text/plain=editor.desktop;\n"
        )
        mimeapps.chmod(0o644)
        self.log.unlink()

        self.assertTrue(file_types._linux.unregister(self.KIND, exe))
        self.assertFalse(self.desktop.exists())
        self.assertFalse(self.icon.exists())
        self.assertEqual(
            mimeapps.read_text(),
            "# mine\n"
            "[Default Applications]\n"
            f"{shared}=other.desktop;\n"
            "image/png=viewer.desktop;\n"
            "\n"
            "[Added Associations]\n"
            f"{shared}=other.desktop;\n"
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
        target.write_text(f"[Default Applications]\n{self.OWN}=qitv-open.desktop;a.desktop;\n")
        self.config.mkdir()
        (self.config / "mimeapps.list").symlink_to(target)
        old_place = self.data / "applications" / "mimeapps.list"  # xdg-utils before 1.1
        self.assertTrue(file_types._linux.register(self.KIND, "/opt/qitv/qitv-linux"))
        old_place.write_text(f"[Added Associations]\n{self.ADDED}=qitv-open.desktop;\n")

        self.assertTrue(file_types._linux.unregister(self.KIND, "/opt/qitv/qitv-linux"))
        self.assertTrue((self.config / "mimeapps.list").is_symlink())
        self.assertEqual(target.read_text(), f"[Default Applications]\n{self.OWN}=a.desktop;\n")
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
                self.assertTrue(file_types._linux.register(self.KIND, exe))
                exec_value = self.entry()["Exec"]
                self.assertEqual(exec_value, expected)
                self.assertEqual(_exec_argv(exec_value), [exe, "%F"])

    @unittest.skipUnless(GIO, "GLib's gio is not installed here (or this isn't Linux)")
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
        self.assertTrue(file_types._linux.register(self.KIND, str(exe)))
        assert GIO  # skipped without it
        subprocess.run([GIO, "launch", str(self.desktop), str(film)], timeout=10, check=True)
        for _ in range(50):
            if argv_log.exists() and argv_log.read_text().count("\n") == 2:
                break
            time.sleep(0.1)  # the launched script runs on its own
        self.assertEqual(argv_log.read_text(encoding="utf-8").splitlines(), [str(exe), str(film)])

    def test_missing_tools_are_skipped(self):
        with self.assertLogs("services.file_types", "INFO") as logs:
            result = file_types._linux.register(self.KIND, "/opt/qitv/qitv-linux")
        self.assertTrue(result)
        self.assertFalse(result.defaults)  # Open With only: the hint says to choose it by hand
        self.assertTrue(self.desktop.exists())
        self.assertTrue(any("xdg-mime" in line for line in logs.output))
        self.assertTrue(file_types._linux.unregister(self.KIND, "/opt/qitv/qitv-linux"))
        self.assertFalse(self.desktop.exists())

    def test_xdg_mime_failing_for_every_type_keeps_open_with(self):
        self.fake_tools("xdg-mime", "update-desktop-database")
        with patch.dict(os.environ, {"QITV_FAKE_EXIT": "4"}):
            result = file_types._linux.register(self.KIND, "/opt/qitv/qitv-linux")
        self.assertTrue(result)
        self.assertFalse(result.defaults)
        self.assertEqual(self.entry()["Exec"], '"/opt/qitv/qitv-linux" %F')
        self.assertTrue(self.icon.exists())

    def test_a_failed_rewrite_keeps_the_working_desktop_file(self):
        exe = "/opt/qitv/qitv-linux"
        self.assertTrue(file_types._linux.register(self.KIND, exe))
        working = self.desktop.read_bytes()
        failing = patch.object(
            file_types._LinuxBackend, "_write_desktop", side_effect=PermissionError(13, "denied")
        )
        with failing, self.assertLogs("services.file_types", "WARNING"):
            self.assertFalse(file_types._linux.register(self.KIND, exe, previous=exe))
        self.assertEqual(self.desktop.read_bytes(), working)
        self.assertTrue(self.icon.exists())

        # a first registration that fails takes back what it wrote
        self.assertTrue(file_types._linux.unregister(self.KIND, exe))
        with failing, self.assertLogs("services.file_types", "WARNING"):
            self.assertFalse(file_types._linux.register(self.KIND, exe))
        self.assertFalse(self.desktop.exists())
        self.assertFalse(self.icon.exists())

    def test_refresh_rewrites_the_file_without_taking_defaults_again(self):
        self.fake_tools("xdg-mime", "update-desktop-database")
        self.assertTrue(
            file_types._linux.register(
                self.KIND, "/b/qitv-linux", defaults=False, previous="/a/qitv-linux"
            )
        )
        self.assertEqual(self.entry()["Exec"], '"/b/qitv-linux" %F')
        self.assertFalse([line for line in self.logged() if line.startswith("xdg-mime")])

    def test_an_icon_qitv_did_not_put_there_stays(self):
        # scripts/install-linux.sh puts the same logo in the same place
        self.icon.parent.mkdir(parents=True)
        self.icon.write_bytes(b"the installer's")
        self.assertTrue(file_types._linux.register(self.KIND, "/opt/qitv/qitv-linux"))
        self.assertTrue(file_types._linux.register(self.KIND, "/opt/qitv/qitv-linux"))
        self.assertTrue(file_types._linux.unregister(self.KIND, "/opt/qitv/qitv-linux"))
        self.assertEqual(self.icon.read_bytes(), b"the installer's")

    @unittest.skipUnless(XDG_MIME, "xdg-mime is not installed here (or this isn't Linux)")
    def test_linux_real_xdg_mime_when_present(self):
        assert XDG_MIME  # skipped without it
        self.config.mkdir()  # a user's ~/.config is there; xdg-mime may not create it
        real_path = os.pathsep.join([os.path.dirname(XDG_MIME), "/usr/bin", "/bin"])
        quiet = {"XDG_CURRENT_DESKTOP": "", "DESKTOP_SESSION": "", "KDE_FULL_SESSION": ""}
        with patch.dict(os.environ, {"PATH": real_path, **quiet}):

            def query():
                return subprocess.run(
                    ["xdg-mime", "query", "default", self.DEFAULT_MIME],
                    capture_output=True,
                    text=True,
                    timeout=10,
                ).stdout.strip()

            self.assertTrue(file_types._linux.register(self.KIND, "/opt/qitv/qitv-linux"))
            self.assertEqual(query(), "qitv-open.desktop")
            self.assertTrue(file_types._linux.unregister(self.KIND, "/opt/qitv/qitv-linux"))
            self.assertNotEqual(query(), "qitv-open.desktop")


class MusicLinuxBackendTests(LinuxBackendTests):
    KIND = "music"
    OPEN_WITH_MIME = "audio/x-mpegurl"  # a playlist's: in Open With, never made the default
    DEFAULT_MIME = "audio/mpeg"
    SHARED, OWN, ADDED, REMOVED = "audio/mpeg", "audio/flac", "audio/ogg", "audio/vnd.wave"


class LinuxKindsTests(_XdgFolders):
    """Videos and music share QiTV's one desktop file: its MimeType holds what's registered,
    and each kind comes and goes without touching the other."""

    EXE = "/opt/qitv/qitv-linux"

    def setUp(self):
        super().setUp()
        self.fake_tools("xdg-mime", "update-desktop-database")

    def mimes(self):
        return [m for m in self.entry()["MimeType"].split(";") if m]

    def defaulted(self):
        """The MIME types xdg-mime was asked to give QiTV since the log was last cleared."""
        return {line.split()[3] for line in self.logged() if line.startswith("xdg-mime default")}

    def test_rows_are_independent(self):
        for first, second in (("videos", "music"), ("music", "videos")):
            with self.subTest(first=first):
                self.assertTrue(file_types._linux.register(first, self.EXE))
                only_first = self.desktop.read_bytes()
                self.log.unlink()
                self.assertTrue(file_types._linux.register(second, self.EXE))
                # one file holds both, and the second press defaults its own types only
                both = set(file_types.mime_types(first)) | set(file_types.mime_types(second))
                self.assertEqual(set(self.mimes()), both)
                self.assertEqual(len(self.mimes()), len(both))
                self.assertEqual(self.defaulted(), set(file_types.default_mime_types(second)))
                self.assertEqual(self.entry()["Exec"], f'"{self.EXE}" %F')

                # what xdg-mime wrote for both, and a playlist the user chose QiTV for
                kept = sorted(file_types.default_mime_types(first))
                gone = sorted(file_types.default_mime_types(second))
                mimeapps = self.config / "mimeapps.list"
                mimeapps.parent.mkdir(parents=True, exist_ok=True)
                mimeapps.write_text(
                    "[Default Applications]\n"
                    + "".join(f"{m}=qitv-open.desktop;\n" for m in kept + gone)
                    + "[Added Associations]\naudio/x-mpegurl=qitv-open.desktop;other.desktop;\n"
                )
                self.log.unlink()

                self.assertTrue(file_types._linux.unregister(second, self.EXE))
                self.assertEqual(self.desktop.read_bytes(), only_first)  # as the first wrote it
                self.assertTrue(self.icon.exists())
                self.assertEqual(
                    mimeapps.read_text(),
                    "[Default Applications]\n"
                    + "".join(f"{m}=qitv-open.desktop;\n" for m in kept)
                    + "[Added Associations]\naudio/x-mpegurl=qitv-open.desktop;other.desktop;\n",
                )
                self.assertEqual(self.logged(), [f"update-desktop-database {self.desktop.parent}"])

                self.assertTrue(file_types._linux.unregister(first, self.EXE))  # the last one
                self.assertFalse(self.desktop.exists())
                self.assertFalse(self.icon.exists())
                self.assertNotIn("qitv", mimeapps.read_text())
                mimeapps.unlink()
                self.log.unlink()

    def test_unregistering_one_kind_keeps_the_others_exec(self):
        # a QiTV registered for videos from one place, music from another: the file opens the
        # last one registered, and stopping videos doesn't change which QiTV music opens
        self.assertTrue(file_types._linux.register("videos", "/a/qitv-linux"))
        self.assertTrue(file_types._linux.register("music", "/b/qitv-linux"))
        self.assertTrue(file_types._linux.unregister("videos", "/a/qitv-linux"))
        self.assertEqual(self.entry()["Exec"], '"/b/qitv-linux" %F')
        self.assertEqual(set(self.mimes()), set(file_types.mime_types("music")))

    def test_a_first_register_that_fails_keeps_the_other_kind(self):
        self.assertTrue(file_types._linux.register("videos", self.EXE))
        working = self.desktop.read_bytes()
        failing = patch.object(
            file_types._LinuxBackend, "_write_desktop", side_effect=PermissionError(13, "denied")
        )
        with failing, self.assertLogs("services.file_types", "WARNING"):
            self.assertFalse(file_types._linux.register("music", self.EXE))
        self.assertEqual(self.desktop.read_bytes(), working)
        self.assertTrue(self.icon.exists())

    def test_playlists_are_open_with_only(self):
        self.assertTrue(file_types._linux.register("videos", self.EXE))
        self.assertTrue(file_types._linux.register("music", self.EXE))
        self.assertLessEqual(PLAYLIST_MIMES, set(self.mimes()))
        self.assertFalse(PLAYLIST_MIMES & self.defaulted())  # never made the default
        for kind in file_types.KINDS:
            self.assertFalse(PLAYLIST_MIMES & set(file_types.default_mime_types(kind)), kind)
        self.assertTrue(file_types._linux.unregister("videos", self.EXE))
        self.assertLessEqual(PLAYLIST_MIMES, set(self.mimes()))  # music still lists them
        self.assertTrue(file_types._linux.unregister("music", self.EXE))
        self.assertFalse(self.desktop.exists())


# --- macOS with a fake Launch Services ------------------------------------------------------

# Apple's headers: CFString.h (kCFStringEncodingUTF8) and LaunchServices' LSConstants.h
# (kLSRolesAll: every role, as Finder's Change All); the values are written out here, not
# taken from the module.
UTF8 = 0x08000100
ROLES_ALL = 0xFFFFFFFF
BUNDLE_ID = "com.ozankaraali.QiTV"
FIVE = [
    "org.matroska.mkv",
    "public.mpeg-4",
    "com.apple.quicktime-movie",
    "public.avi",
    "org.webmproject.webm",
]
# music's short list: mp3, m4a (macOS's own m4a type, not its MPEG-4 audio parent) and flac
THREE = ["public.mp3", "com.apple.m4a-audio", "org.xiph.flac"]


class FakeCoreServices:
    """CoreFoundation's CFString calls and Launch Services' default-handler call: each string
    is a numbered reference that must be released once, and every call is recorded."""

    def __init__(self, answers=None):
        self.answers = answers or {}  # UTI (or "*" for every one) -> OSStatus; 0 otherwise
        self.null_for = set()  # texts CFStringCreateWithCString answers NULL for
        self.strings = {}  # reference -> text
        self.released = []
        self.calls = []

    def CFStringCreateWithCString(self, alloc, c_str, encoding):
        assert alloc is None, alloc
        assert encoding == UTF8, hex(encoding)
        text = c_str.decode("utf-8")
        if text in self.null_for:
            return None
        ref = 0x7F0000001000 + 16 * len(self.strings)
        self.strings[ref] = text
        return ref

    def CFRelease(self, ref):
        assert ref in self.strings, f"CFRelease({ref!r}) of no string"
        assert ref not in self.released, "released twice"
        self.released.append(ref)

    def LSSetDefaultRoleHandlerForContentType(self, content_type, role, handler):
        for ref in (content_type, handler):
            assert ref in self.strings and ref not in self.released, "a string used freed"
        assert 0 <= role <= 0xFFFFFFFF, role  # an LSRolesMask is a UInt32
        uti = self.strings[content_type]
        self.calls.append((uti, role, self.strings[handler]))
        return self.answers.get(uti, self.answers.get("*", 0))

    def all_released(self):
        return sorted(self.released) == sorted(self.strings)


class _FakeLaunchServices(unittest.TestCase):
    def use(self, fake):
        p = patch.object(file_types, "_core_services", return_value=fake)
        p.start()
        self.addCleanup(p.stop)
        return fake


class MacBackendTests(_FakeLaunchServices):
    """One kind's short list (videos here, music in the subclass)."""

    KIND = "videos"
    SHORT_LIST = "MAC_DEFAULT_VIDEO"
    EXTENSIONS: tuple[str, ...] = ("mkv", "mp4", "mov", "avi", "webm")
    UTIS = FIVE
    REFUSED = "org.matroska.mkv"  # one type Launch Services refuses
    NO_STRING = "public.avi"  # one type macOS makes no CFString of

    def test_mac_register_calls_ls_for_short_list(self):
        fake = self.use(FakeCoreServices())
        result = file_types._mac.register(self.KIND, MAC_APP)
        self.assertTrue(result)
        self.assertTrue(result.defaults)
        self.assertEqual(fake.calls, [(uti, ROLES_ALL, BUNDLE_ID) for uti in self.UTIS])
        short_list = getattr(file_types, self.SHORT_LIST)
        self.assertEqual(short_list, self.EXTENSIONS)
        self.assertEqual([file_types.MAC_UTIS[ext][0] for ext in short_list], self.UTIS)
        self.assertTrue(fake.all_released())

    def test_mac_failure_returns_false(self):
        fake = self.use(FakeCoreServices({"*": -54}))
        with self.assertLogs("services.file_types", "WARNING") as logs:
            result = file_types._mac.register(self.KIND, MAC_APP)
        self.assertFalse(result)
        self.assertFalse(result.defaults)
        self.assertEqual(len(fake.calls), len(self.UTIS))
        self.assertTrue(all("-54" in line for line in logs.output), logs.output)
        self.assertTrue(fake.all_released())

    def test_mac_one_refusal_keeps_the_others(self):
        fake = self.use(FakeCoreServices({self.REFUSED: -54}))
        with self.assertLogs("services.file_types", "WARNING") as logs:
            result = file_types._mac.register(self.KIND, MAC_APP)
        self.assertTrue(result)
        self.assertTrue(result.defaults)
        self.assertEqual([call[0] for call in fake.calls], self.UTIS)
        self.assertEqual(len(logs.output), 1)
        self.assertIn(self.REFUSED, logs.output[0])
        self.assertTrue(fake.all_released())

    def test_mac_a_string_macos_cannot_make(self):
        fake = self.use(FakeCoreServices())
        fake.null_for = {BUNDLE_ID}
        with self.assertLogs("services.file_types", "WARNING"):
            self.assertFalse(file_types._mac.register(self.KIND, MAC_APP))
        self.assertEqual(fake.calls, [])
        self.assertTrue(fake.all_released())  # no CFRelease of NULL

        fake = self.use(FakeCoreServices())
        fake.null_for = {self.NO_STRING}
        with self.assertLogs("services.file_types", "WARNING"):
            self.assertTrue(file_types._mac.register(self.KIND, MAC_APP))
        self.assertEqual(
            [call[0] for call in fake.calls], [u for u in self.UTIS if u != self.NO_STRING]
        )
        self.assertTrue(fake.all_released())

    def test_mac_refresh_and_unregister_call_nothing(self):
        # the bundle declares the types itself; Launch Services knows QiTV by its bundle id
        result = file_types._mac.register(self.KIND, MAC_APP, defaults=False, previous="/x.app")
        self.assertTrue(result)
        with self.assertLogs("services.file_types", "INFO"):
            self.assertTrue(file_types._mac.unregister(self.KIND, MAC_APP))
        # the module's guard raises on any load: nothing was loaded

    def test_mac_backend_on_macos(self):
        with patch.object(sys, "platform", "darwin"):
            self.assertIs(file_types._backend(), file_types._mac)

    def test_mac_loader_gives_the_c_types(self):
        loaded = {}

        class Library:
            def __init__(self, path):
                loaded[path] = self

            def __getattr__(self, name):
                function = SimpleNamespace(argtypes=None, restype="unset")
                setattr(self, name, function)
                return function

        with patch.object(ctypes, "CDLL", Library):
            lib = _REAL_CORE_SERVICES()
        cf = loaded.pop("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        cs = loaded.pop("/System/Library/Frameworks/CoreServices.framework/CoreServices")
        self.assertEqual(loaded, {})
        self.assertIs(lib.CFStringCreateWithCString, cf.CFStringCreateWithCString)
        self.assertIs(lib.CFRelease, cf.CFRelease)
        self.assertIs(
            lib.LSSetDefaultRoleHandlerForContentType, cs.LSSetDefaultRoleHandlerForContentType
        )
        # a CFStringRef is a pointer: read as a C int it would be cut to 32 bits
        create = lib.CFStringCreateWithCString
        self.assertEqual(create.argtypes, [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32])
        self.assertIs(create.restype, ctypes.c_void_p)
        self.assertEqual(lib.CFRelease.argtypes, [ctypes.c_void_p])
        self.assertIsNone(lib.CFRelease.restype)
        set_default = lib.LSSetDefaultRoleHandlerForContentType
        self.assertEqual(set_default.argtypes, [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p])
        self.assertIs(set_default.restype, ctypes.c_int32)  # OSStatus

    def test_mac_records_the_app_and_its_place(self):
        kind = self.KIND
        backend = RecordingBackend()
        config = Config()
        inside = f"{MAC_APP}/Contents/MacOS/QiTV"
        with (
            patch.object(sys, "platform", "darwin"),
            patch.object(file_types, "_backend", return_value=backend),
            patch.object(sys, "executable", inside),
        ):
            self.assertTrue(file_types.register(kind, config, inside, frozen=True))
            self.assertEqual(config.file_types, {kind: MAC_APP})
            self.assertIs(file_types.status(kind, config, frozen=True), Status.REGISTERED)
            file_types.refresh(config, inside, frozen=True)  # the same app: nothing to do
            self.assertEqual(backend.calls, [("register", kind, MAC_APP, {"previous": None})])

            moved = "/Users/ayşe/Applications/QiTV.app"
            file_types.refresh(config, f"{moved}/Contents/MacOS/QiTV", frozen=True)
            self.assertEqual(config.file_types, {kind: moved})
            self.assertEqual(
                backend.calls[-1],
                ("register", kind, moved, {"defaults": False, "previous": MAC_APP}),
            )
            config.file_types = {}
            self.assertTrue(file_types.unregister(kind, config, frozen=True))
            self.assertEqual(backend.calls[-1], ("unregister", kind, MAC_APP))

            dmg = "/Volumes/QiTV/QiTV.app/Contents/MacOS/QiTV"
            with patch.object(sys, "executable", dmg):
                self.assertIs(file_types.status(kind, config, frozen=True), Status.TEMPORARY)
            self.assertFalse(file_types.register(kind, config, dmg, frozen=True))


class MusicMacBackendTests(MacBackendTests):
    KIND = "music"
    SHORT_LIST = "MAC_DEFAULT_MUSIC"
    EXTENSIONS = ("mp3", "m4a", "flac")
    UTIS = THREE
    REFUSED = "org.xiph.flac"
    NO_STRING = "com.apple.m4a-audio"


class MacKindsTests(_FakeLaunchServices):
    def test_rows_are_independent(self):
        fake = self.use(FakeCoreServices())
        self.assertTrue(file_types._mac.register("videos", MAC_APP))
        self.assertTrue(file_types._mac.register("music", MAC_APP))
        # each press asks for its own short list only (macOS 26.4+ asks once for each)
        self.assertEqual([call[0] for call in fake.calls], FIVE + THREE)
        with self.assertLogs("services.file_types", "INFO"):
            self.assertTrue(file_types._mac.unregister("music", MAC_APP))
        self.assertEqual(len(fake.calls), len(FIVE + THREE))  # nothing taken back
        self.assertTrue(fake.all_released())

    def test_playlists_are_open_with_only(self):
        fake = self.use(FakeCoreServices())
        for kind in file_types.KINDS:
            self.assertTrue(file_types._mac.register(kind, MAC_APP))
        self.assertEqual(
            {file_types.MAC_UTIS[ext][0] for ext in PLAYLISTS}, {"public.m3u-playlist"}
        )
        self.assertNotIn("public.m3u-playlist", [call[0] for call in fake.calls])


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


class _RecordedBackend(unittest.TestCase):
    def setUp(self):
        self.backend: RecordingBackend | None = RecordingBackend()  # None: no backend
        p = patch.object(file_types, "_backend", side_effect=lambda: self.backend)
        p.start()
        self.addCleanup(p.stop)


class TypesTests(_RecordedBackend):
    def test_kinds_and_types(self):
        self.assertEqual(file_types.KINDS, ("videos", "music"))
        self.assertEqual(file_types.DEFAULT_VIDEO, VIDEO_EXTENSIONS - {"ts"})
        self.assertEqual(file_types.DEFAULT_MUSIC, MUSIC_EXTENSIONS)
        self.assertEqual(file_types.MAC_DEFAULT_VIDEO, ("mkv", "mp4", "mov", "avi", "webm"))
        self.assertEqual(file_types.MAC_DEFAULT_MUSIC, ("mp3", "m4a", "flac"))
        for ext in VIDEO_EXTENSIONS | MUSIC_EXTENSIONS | PLAYLISTS:
            self.assertTrue(file_types.MIME_TYPES[ext], ext)
            self.assertTrue(file_types.MAC_UTIS[ext], ext)
        # .m2ts and .mts are video/mp2t too: like .ts, Open With only on Linux
        self.assertNotIn("video/mp2t", file_types.default_mime_types("videos"))
        self.assertIn("video/mp2t", file_types.mime_types("videos"))
        for both in ("video/vnd.avi", "video/x-msvideo", "video/mp4", "video/x-m4v"):
            self.assertIn(both, file_types.default_mime_types("videos"))
        # today's name and the one older databases use (shared-mime-info before 2.3: x-wav);
        # an Ogg file is told apart by what it holds
        music = file_types.default_mime_types("music")
        for both in ("audio/vnd.wave", "audio/x-wav", "audio/ogg", "audio/x-vorbis+ogg"):
            self.assertIn(both, music)
        for mime in ("audio/mpeg", "audio/mp4", "audio/aac", "audio/flac", "audio/x-opus+ogg"):
            self.assertIn(mime, music)

    def test_aiff_is_registered_as_music(self):
        # 2.11.1: shared-mime-info names AIFF (.aif, .aiff) audio/x-aiff and AIFF-C (.aifc,
        # .aiffc) audio/x-aifc; macOS 13 declares (exports) public.aiff-audio for .aif and
        # .aiff, a kind of public.aifc-audio, which is .aifc. Windows registers the
        # extensions themselves (MusicWindowsBackendTests).
        for ext, mime, uti in (
            ("aif", "audio/x-aiff", "public.aiff-audio"),
            ("aiff", "audio/x-aiff", "public.aiff-audio"),
            ("aifc", "audio/x-aifc", "public.aifc-audio"),
        ):
            with self.subTest(ext=ext):
                self.assertEqual(file_types.MIME_TYPES[ext], (mime,))
                self.assertEqual(file_types.MAC_UTIS[ext], (uti,))
                every, chosen = file_types._kind_types("music")
                self.assertIn(ext, every)
                self.assertIn(ext, chosen)  # it may become QiTV's, as every music type
                self.assertIn(mime, file_types.default_mime_types("music"))
                self.assertNotIn(ext, file_types._kind_types("videos")[0])
        self.assertEqual(file_types.MAC_DEFAULT_MUSIC, ("mp3", "m4a", "flac"))  # as before

    def test_the_other_audio_formats_are_never_registered(self):
        # They play when handed over, dropped or opened (open_paths), but QiTV never claims
        # them: nothing of theirs is written on any system.
        for ext in open_paths.OTHER_MUSIC_EXTENSIONS:
            with self.subTest(ext=ext):
                self.assertNotIn(ext, file_types.MIME_TYPES)
                self.assertNotIn(ext, file_types.MAC_UTIS)
                for kind in file_types.KINDS:
                    self.assertNotIn(ext, file_types._kind_types(kind)[0])

    def test_each_kind_has_its_own_types(self):
        # one desktop file holds both kinds: a type of one never belongs to the other (a
        # playlist's are both kinds', and tell neither apart)
        videos = set(file_types.mime_types("videos")) - PLAYLIST_MIMES
        music = set(file_types.mime_types("music")) - PLAYLIST_MIMES
        self.assertFalse(videos & music)
        self.assertTrue(all(m.startswith("video/") for m in videos), videos)
        self.assertTrue(all(m.startswith("audio/") for m in music), music)
        self.assertEqual(set(file_types.PROGIDS), set(file_types.KINDS))
        self.assertEqual(file_types.PROGIDS, PROGIDS)

    def test_playlists_are_open_with_only(self):
        for kind in file_types.KINDS:
            with self.subTest(kind=kind):
                every, chosen = file_types._kind_types(kind)
                self.assertLessEqual(PLAYLISTS, every)  # in Open With with either kind
                self.assertFalse(PLAYLISTS & chosen)  # never offered as the default
                self.assertLessEqual(PLAYLIST_MIMES, set(file_types.mime_types(kind)))
                self.assertFalse(PLAYLIST_MIMES & set(file_types.default_mime_types(kind)))
                short_list = file_types._MAC_DEFAULTS[kind]
                self.assertFalse(PLAYLISTS & set(short_list))

    def test_what_the_music_types_also_cover_plays(self):
        # the system hands QiTV these too: macOS's types for mp3, aac and wav and the Ogg
        # import cover them, and so do Linux's MIME types (shared-mime-info's globs); an
        # audiobook's .m4b is audio/x-m4b there, a kind of audio/mp4
        for ext in ("mpga", "adts", "wave", "bwf", "oga", "spx", "m4b", "aiffc"):
            self.assertTrue(open_paths.is_music(f"/x/song.{ext}"), ext)
        self.assertNotIn("m4b", open_paths.MUSIC_EXTENSIONS)  # registered for: as before

    def test_temporary_locations(self):
        temporary = [
            os.path.join(tempfile.gettempdir(), "qitv", "qitv-linux"),
            "C:\\Users\\x\\AppData\\Local\\Temp\\Temp1_qitv.zip\\qitv-windows.exe",
            "c:\\users\\X\\appdata\\local\\temp\\7zO12\\qitv-windows.exe",
            "/private/var/folders/ab/cd/T/AppTranslocation/1A2B/d/QiTV.app/Contents/MacOS/QiTV",
            "/private/var/folders/ab/cd/T/AppTranslocation/1A2B/d/QiTV.app",  # the bundle
            # run from the mounted disk image (scripts/make_dmg.py puts it at the root)
            "/Volumes/QiTV/QiTV.app/Contents/MacOS/QiTV",
            "/Volumes/QiTV 1/QiTV.app",
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
            "/Applications/QiTV.app/Contents/MacOS/QiTV",
            "/Applications/QiTV.app",
            "/Users/ayşe/Applications/QiTV.app/Contents/MacOS/QiTV",
            # a disk of the user's own, with QiTV in a folder there
            "/Volumes/Macintosh HD/Applications/QiTV.app/Contents/MacOS/QiTV",
            "/Volumes/Archive/Apps/QiTV.app",
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


class ServiceTests(_RecordedBackend):
    """One kind's record (videos here, music in the subclass)."""

    KIND = "videos"

    def test_source_run_never_registers(self):
        config = Config({self.KIND: "/a/qitv"})
        with patch.object(sys, "executable", "/opt/qitv/qitv-linux"):
            self.assertIs(file_types.status(self.KIND, config, frozen=False), Status.UNAVAILABLE)
        self.assertFalse(file_types.register(self.KIND, config, "/b/qitv", frozen=False))
        file_types.refresh(config, "/b/qitv", frozen=False)
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(config.file_types, {self.KIND: "/a/qitv"})

    def test_source_run_never_unregisters(self):
        # a run from source may share the packaged app's settings: its registration stays
        config = Config({self.KIND: "/a/qitv"})
        with self.assertLogs("services.file_types", "INFO"):
            self.assertFalse(file_types.unregister(self.KIND, config, frozen=False))
        self.assertFalse(file_types.unregister(self.KIND, config))  # this test run is one
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(config.file_types, {self.KIND: "/a/qitv"})

    def test_status_follows_the_record(self):
        config = Config()
        with patch.object(sys, "executable", "/opt/qitv/qitv-linux"):
            self.assertIs(file_types.status(self.KIND, config, frozen=True), Status.NOT_REGISTERED)
            self.assertTrue(file_types.register(self.KIND, config, "/opt/qitv/qitv-linux", True))
            self.assertIs(file_types.status(self.KIND, config, frozen=True), Status.REGISTERED)
        with patch.object(sys, "executable", "/tmp/qitv-linux"):
            self.assertIs(file_types.status(self.KIND, config, frozen=True), Status.TEMPORARY)
        self.backend = None  # a system without a backend
        self.assertIs(file_types.status(self.KIND, config, frozen=True), Status.UNAVAILABLE)

    def test_register_records_and_unregister_forgets(self):
        config = Config()
        self.assertTrue(file_types.register(self.KIND, config, "/opt/qitv/qitv-linux", True))
        self.assertEqual(config.file_types, {self.KIND: "/opt/qitv/qitv-linux"})
        self.assertEqual(
            self.backend.calls,
            [("register", self.KIND, "/opt/qitv/qitv-linux", {"previous": None})],
        )
        self.backend.calls.clear()
        with patch.object(sys, "executable", "/elsewhere/qitv-linux"):
            self.assertTrue(file_types.unregister(self.KIND, config, frozen=True))
        # what was registered is what's removed
        self.assertEqual(self.backend.calls, [("unregister", self.KIND, "/opt/qitv/qitv-linux")])
        self.assertEqual(config.file_types, {})

    def test_register_again_tells_the_backend_what_was_registered(self):
        config = Config({self.KIND: "/a/qitv"})
        self.assertTrue(file_types.register(self.KIND, config, "/b/qitv", True))
        self.assertEqual(
            self.backend.calls, [("register", self.KIND, "/b/qitv", {"previous": "/a/qitv"})]
        )
        self.assertEqual(config.file_types, {self.KIND: "/b/qitv"})

    def test_register_answers_whether_the_defaults_were_taken(self):
        config = Config()
        result = file_types.register(self.KIND, config, "/opt/qitv/qitv-linux", True)
        self.assertEqual(result, file_types.RegisterResult(ok=True, defaults=True))

        # Open With only (Linux without xdg-mime): registered, and recorded, without defaults
        self.backend.ok = file_types.RegisterResult(ok=True, defaults=False)
        config = Config()
        result = file_types.register(self.KIND, config, "/opt/qitv/qitv-linux", True)
        self.assertTrue(result)
        self.assertFalse(result.defaults)
        self.assertEqual(config.file_types, {self.KIND: "/opt/qitv/qitv-linux"})

        # every refusal is a falsy result without defaults
        self.backend.ok = False
        refusals = [
            file_types.register(self.KIND, Config(), "/opt/qitv/qitv-linux", True),
            file_types.register(self.KIND, Config(), "/opt/qitv/qitv-linux", False),
            file_types.register(self.KIND, Config(), "/tmp/qitv-linux", True),
        ]
        self.backend = None
        refusals.append(file_types.register(self.KIND, Config(), "/opt/qitv/qitv-linux", True))
        for refused in refusals:
            self.assertIsInstance(refused, file_types.RegisterResult)
            self.assertFalse(refused)
            self.assertFalse(refused.defaults)

    def test_a_failure_changes_no_record(self):
        config = Config()
        self.backend.ok = False
        self.assertFalse(file_types.register(self.KIND, config, "/opt/qitv/qitv-linux", True))
        self.assertEqual(config.file_types, {})
        config.file_types = {self.KIND: "/opt/qitv/qitv-linux"}
        self.assertFalse(file_types.unregister(self.KIND, config, frozen=True))
        self.assertEqual(config.file_types, {self.KIND: "/opt/qitv/qitv-linux"})

    def test_a_backend_that_raises_answers_false(self):
        config = Config()
        raising = patch.object(self.backend, "register", side_effect=ZeroDivisionError)
        with raising, self.assertLogs("services.file_types", "WARNING"):
            result = file_types.register(self.KIND, config, "/opt/qitv/qitv-linux", True)
        self.assertFalse(result)
        self.assertFalse(result.defaults)
        self.assertEqual(config.file_types, {})

    def test_a_temporary_exe_is_never_registered(self):
        config = Config()
        self.assertFalse(file_types.register(self.KIND, config, "/tmp/x/qitv-linux", True))
        self.assertEqual(self.backend.calls, [])

    def test_refresh_moves_registration(self):
        config = Config({self.KIND: "/a/qitv"})
        file_types.refresh(config, "/b/qitv", frozen=True)
        self.assertEqual(
            self.backend.calls,
            [("register", self.KIND, "/b/qitv", {"defaults": False, "previous": "/a/qitv"})],
        )
        self.assertEqual(config.file_types, {self.KIND: "/b/qitv"})

        self.backend.calls.clear()
        file_types.refresh(config, "/b/qitv", frozen=True)  # the same path: nothing to do
        self.assertEqual(self.backend.calls, [])

        file_types.refresh(config, os.path.join(tempfile.gettempdir(), "qitv"), frozen=True)
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(config.file_types, {self.KIND: "/b/qitv"})

        self.backend.ok = False
        file_types.refresh(config, "/c/qitv", frozen=True)
        self.assertEqual(config.file_types, {self.KIND: "/b/qitv"})  # tried again next start

        file_types.refresh(Config(), "/c/qitv", frozen=True)  # nothing registered: nothing
        self.assertEqual(len(self.backend.calls), 1)

    def test_an_updates_copy_registers_the_qitv_it_replaced(self):
        # Windows' self-update runs Downloads\qitv-windows.exe --replace <installed>: the copy
        # writes itself over the installed QiTV and runs on from Downloads. It registers the
        # installed one (main: use_installed), at start and from Settings alike.
        installed = "C:\\Users\\Ayşe\\QiTV\\qitv-windows.exe"
        downloads = "C:\\Users\\Ayşe\\Downloads\\qitv-windows.exe"
        self.addCleanup(file_types.use_installed, None)
        config = Config({self.KIND: installed})
        with patch.object(sys, "executable", downloads):
            file_types.use_installed(installed)
            self.assertEqual(file_types.own_executable(), installed)
            file_types.refresh(config, frozen=True)
            self.assertEqual(self.backend.calls, [])  # registered there already: nothing moves
            self.assertEqual(config.file_types, {self.KIND: installed})
            # one an earlier update moved to Downloads comes back to the installed QiTV
            config.file_types = {self.KIND: downloads}
            file_types.refresh(config, frozen=True)
            self.assertEqual(config.file_types, {self.KIND: installed})
            self.assertEqual(self.backend.calls[-1][:3], ("register", self.KIND, installed))
            # Settings' press in that run, and its unregister with nothing recorded
            config.file_types = {}
            self.assertTrue(file_types.register(self.KIND, config, frozen=True))
            self.assertEqual(config.file_types, {self.KIND: installed})
            self.assertIs(file_types.status(self.KIND, config, frozen=True), Status.REGISTERED)
            config.file_types = {}
            self.assertTrue(file_types.unregister(self.KIND, config, frozen=True))
            self.assertEqual(self.backend.calls[-1], ("unregister", self.KIND, installed))

    def test_a_qitv_the_user_moved_registers_where_it_runs_now(self):
        # Any other start (no update, or one whose replace failed: main passes None) is QiTV
        # where it runs: moved by the user, it registers there again.
        moved = "D:\\Programlar\\QiTV\\qitv-windows.exe"
        config = Config({self.KIND: "C:\\Users\\Ayşe\\QiTV\\qitv-windows.exe"})
        self.addCleanup(file_types.use_installed, None)
        file_types.use_installed(None)
        with patch.object(sys, "executable", moved):
            self.assertEqual(file_types.own_executable(), moved)
            file_types.refresh(config, frozen=True)
        self.assertEqual(config.file_types, {self.KIND: moved})
        self.assertEqual(self.backend.calls[-1][:3], ("register", self.KIND, moved))

    def test_the_record_reaches_config_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text("{}", encoding="utf-8")
            with patch.object(ConfigManager, "_get_config_path", return_value=str(path)):
                config = ConfigManager()
            self.assertTrue(file_types.register(self.KIND, config, "/opt/qitv/qitv-linux", True))
            config.save_config(wait=True)
            written = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(written["file_types"], {self.KIND: "/opt/qitv/qitv-linux"})
            # another config without the key shares nothing with this one
            other = Path(directory) / "other.json"
            other.write_text("{}", encoding="utf-8")
            with patch.object(ConfigManager, "_get_config_path", return_value=str(other)):
                self.assertEqual(ConfigManager().file_types, {})
            self.assertTrue(file_types.unregister(self.KIND, config, frozen=True))
            config.save_config(wait=True)
            written = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(written["file_types"], {})


class MusicServiceTests(ServiceTests):
    KIND = "music"


class KindsServiceTests(_RecordedBackend):
    """The record holds each kind on its own: one row's press never changes the other's."""

    EXE = "/opt/qitv/qitv-linux"

    def test_rows_are_independent(self):
        config = Config()
        with patch.object(sys, "executable", self.EXE):
            self.assertTrue(file_types.register("music", config, self.EXE, True))
            self.assertIs(file_types.status("music", config, frozen=True), Status.REGISTERED)
            self.assertIs(file_types.status("videos", config, frozen=True), Status.NOT_REGISTERED)
            self.assertTrue(file_types.register("videos", config, self.EXE, True))
            self.assertTrue(file_types.unregister("music", config, frozen=True))
            self.assertEqual(config.file_types, {"videos": self.EXE})
            self.assertIs(file_types.status("videos", config, frozen=True), Status.REGISTERED)
            self.assertIs(file_types.status("music", config, frozen=True), Status.NOT_REGISTERED)
        self.assertEqual(
            [call[:2] for call in self.backend.calls],
            [("register", "music"), ("register", "videos"), ("unregister", "music")],
        )

    def test_two_presses_at_once_keep_both(self):
        # Settings' two rows answer on workers of their own: a press while the other's runs
        # records on what the first wrote, never on what it read before
        config = Config()
        gate, inside = threading.Event(), threading.Event()
        register = self.backend.register

        def slow(kind, exe, **options):
            if kind == "videos":
                inside.set()
                gate.wait(5)
            return register(kind, exe, **options)

        presses = [
            threading.Thread(target=file_types.register, args=(kind, config, self.EXE, True))
            for kind in ("videos", "music")
        ]
        with patch.object(self.backend, "register", side_effect=slow):
            presses[0].start()
            self.assertTrue(inside.wait(5))
            presses[1].start()
            presses[1].join(0.3)  # it waits for the first (or, unserialized, is done)
            gate.set()
            for press in presses:
                press.join(5)
        self.assertEqual(config.file_types, {"videos": self.EXE, "music": self.EXE})

    def test_refresh_moves_every_kind_registered(self):
        config = Config({"videos": "/a/qitv", "music": "/a/qitv"})
        file_types.refresh(config, "/b/qitv", frozen=True)
        self.assertEqual(config.file_types, {"videos": "/b/qitv", "music": "/b/qitv"})
        moved = {"defaults": False, "previous": "/a/qitv"}
        self.assertEqual(
            self.backend.calls,
            [("register", "videos", "/b/qitv", moved), ("register", "music", "/b/qitv", moved)],
        )

    def test_an_updates_copy_registers_each_kind_for_the_qitv_it_replaced(self):
        installed = "C:\\Users\\Ayşe\\QiTV\\qitv-windows.exe"
        downloads = "C:\\Users\\Ayşe\\Downloads\\qitv-windows.exe"
        self.addCleanup(file_types.use_installed, None)
        config = Config({"videos": downloads, "music": downloads})  # an earlier update's
        with patch.object(sys, "executable", downloads):
            file_types.use_installed(installed)
            file_types.refresh(config, frozen=True)
        self.assertEqual(config.file_types, {"videos": installed, "music": installed})
        self.assertEqual(
            [call[:3] for call in self.backend.calls],
            [("register", "videos", installed), ("register", "music", installed)],
        )

    def test_an_unknown_record_is_left_alone(self):
        # a newer QiTV's kind, in a config this one reads: kept, never registered or removed
        config = Config({"photos": "/a/qitv", "music": "/a/qitv"})
        file_types.refresh(config, "/b/qitv", frozen=True)
        self.assertEqual(config.file_types, {"photos": "/a/qitv", "music": "/b/qitv"})
        with self.assertRaises(ValueError):
            file_types.status("photos", config, frozen=True)


if __name__ == "__main__":
    unittest.main()
