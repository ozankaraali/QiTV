"""A double-click reaches a running QiTV over DDE on Windows (services/dde_windows.py).

The command parser and the registry's ddeexec (the strict fake winreg of test_file_types) run
everywhere. So does the server's callback, through a fake DDEML that calls it as the real one
would (a ctypes callback of the same type). Only the live test talks to Windows' DDEML: a
client on another thread, as Explorer is another process, sends the command Explorer sends.
"""

import ctypes
import sys
import threading
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication
from test_file_types import HKCU_CLASSES, WINDOWS_EXE, FakeWinreg
from timer_fixtures import wait_until

from services import dde_windows, file_types
from services.dde_windows import COMMAND, SERVICE, TOPIC, DdeServer, parse_execute

PROGID = f"{HKCU_CLASSES}\\QiTV.Video"
DDEEXEC = f"{PROGID}\\shell\\open\\ddeexec"


class ParseExecuteTests(unittest.TestCase):
    def test_parse_execute(self):
        self.assertEqual(COMMAND, '[open("%1")]')
        one = "C:\\Dizi\\Bölüm 1.mkv"
        self.assertEqual(parse_execute(f'[open("{one}")]'), [one])
        self.assertEqual(parse_execute(COMMAND.replace("%1", one)), [one])
        # two commands (Explorer's multi-select may send them in one string), in order
        two = "D:\\Film (2019) [1080p].mkv"  # brackets inside the quotes are the name's
        self.assertEqual(parse_execute(f'[open("{one}")][open("{two}")]'), [one, two])
        self.assertEqual(parse_execute(f'[open("{two}")] \r\n [open("{one}")]'), [two, one])
        # a quote inside a path is doubled
        self.assertEqual(parse_execute('[open("C:\\say ""hi"".mkv")]'), ['C:\\say "hi".mkv'])
        # a network share's long path, unchanged
        unc = "\\\\server\\share\\" + "ç" * 300 + ".mkv"
        self.assertEqual(parse_execute(f'[open("{unc}")]'), [unc])
        # what Explorer sends after starting QiTV itself (ifexec), and other commands
        self.assertEqual(parse_execute("[noop]"), [])
        self.assertEqual(parse_execute(f'[noop][open("{one}")][print("{two}")]'), [one])
        # garbage
        for text in (
            "",
            "open",
            "\x00\x01\ufffd",
            "[open(C:\\x.mkv)]",  # no quotes: not our command
            '[open("C:\\x.mkv"',  # never closed
            '[open("C:\\x.mkv)]',
            '[open("")]',
            '[print("[open(""C:\\x.mkv"")]")]',  # an open inside another command's text
        ):
            with self.subTest(text=text):
                self.assertEqual(parse_execute(text), [])


class DdeExecRegistryTests(unittest.TestCase):
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

    def ddeexec(self):
        """The ddeexec key's default value and its three subkeys' (None: no such key)."""

        def default(path):
            values = self.reg.values(path)
            return None if values is None else values.get("")

        return {
            "ddeexec": default(DDEEXEC),
            "application": default(f"{DDEEXEC}\\Application"),
            "topic": default(f"{DDEEXEC}\\Topic"),
            "ifexec": default(f"{DDEEXEC}\\ifexec"),
        }

    def test_ddeexec_written_and_removed(self):
        self.assertTrue(file_types._windows.register("videos", WINDOWS_EXE))
        command = self.reg.values(f"{PROGID}\\shell\\open\\command")
        self.notified.clear()

        self.assertTrue(file_types.add_ddeexec(WINDOWS_EXE))
        # "How to Associate Verbs with DDE Commands": application, topic and ifexec are
        # subkeys of ddeexec, each with its default value
        self.assertEqual(
            self.ddeexec(),
            {"ddeexec": COMMAND, "application": SERVICE, "topic": TOPIC, "ifexec": "[noop]"},
        )
        self.assertEqual(self.reg.values(DDEEXEC), {"": COMMAND})  # nothing else on the key
        self.assertEqual(self.notified, [1])
        self.assertEqual(self.reg.other_roots, [])  # HKCU only

        self.assertTrue(file_types.add_ddeexec(WINDOWS_EXE))  # a stale one is simply rewritten
        self.assertEqual(self.ddeexec()["ddeexec"], COMMAND)
        self.notified.clear()

        self.assertTrue(file_types.remove_ddeexec())
        self.assertIsNone(self.reg.values(DDEEXEC))
        self.assertEqual(self.reg.values(f"{PROGID}\\shell\\open\\command"), command)
        self.assertEqual(self.notified, [1])
        self.notified.clear()
        self.assertTrue(file_types.remove_ddeexec())  # twice: nothing to do, nothing told
        self.assertEqual(self.notified, [])

    def test_ddeexec_only_where_qitv_is_registered(self):
        before = self.reg.tree()
        self.assertFalse(file_types.add_ddeexec(WINDOWS_EXE))  # never a ProgID of its own
        self.assertEqual(self.reg.tree(), before)
        self.assertTrue(file_types.remove_ddeexec())
        self.assertEqual(self.reg.tree(), before)
        self.assertEqual(self.notified, [])

    def test_unregister_removes_ddeexec(self):
        self.assertTrue(file_types._windows.register("videos", WINDOWS_EXE))
        self.assertTrue(file_types.add_ddeexec(WINDOWS_EXE))
        self.assertTrue(file_types._windows.unregister("videos", WINDOWS_EXE))
        self.assertIsNone(self.reg.values(PROGID))
        # Software\Classes and Software stay (Windows' own); nothing of QiTV's
        self.assertEqual(set(self.reg.paths()), {"Software", HKCU_CLASSES})
        # the quit's removal after Settings' unregister: nothing left to remove, nothing made
        self.assertTrue(file_types.remove_ddeexec())
        self.assertEqual(set(self.reg.paths()), {"Software", HKCU_CLASSES})

    def test_a_ddeexec_that_cannot_be_written_is_taken_back(self):
        self.assertTrue(file_types._windows.register("videos", WINDOWS_EXE))
        before = self.reg.tree()
        self.reg.fail_under = f"{DDEEXEC}\\Topic"
        with self.assertLogs("services.file_types", "WARNING"):
            self.assertFalse(file_types.add_ddeexec(WINDOWS_EXE))
        self.assertEqual(self.reg.tree(), before)

    def test_a_ddeexec_that_cannot_be_removed_says_so(self):
        self.assertTrue(file_types._windows.register("videos", WINDOWS_EXE))
        self.assertTrue(file_types.add_ddeexec(WINDOWS_EXE))
        self.reg.fail_under, self.reg.failing = DDEEXEC, {"delete"}
        with self.assertLogs("services.file_types", "WARNING"):
            self.assertFalse(file_types.remove_ddeexec())  # logged; never raised


# --- a fake DDEML -------------------------------------------------------------------------


class FakeDdeml:
    """user32's DDEML as the server uses it: it records the calls and calls the server's
    callback (a real ctypes callback) as a client's DdeConnect and DdeClientTransaction would.
    String handles are atoms: one per name, whatever its case."""

    INSTANCE = 7

    def __init__(self):
        self.calls = []
        self.callback = None
        self.flags = None
        self.atoms = {}  # casefolded name -> handle
        self.data = {}  # data handle -> bytes
        self.freed_data = []
        self.fail = set()  # names of calls that fail
        self.on_uninitialize = None
        self.uninitialize_answer = 1

    # the functions

    def DdeInitializeW(self, pid, callback, flags, reserved):
        self.calls.append("DdeInitializeW")
        if "DdeInitializeW" in self.fail:
            return 0x4006  # DMLERR_DLL_USAGE
        self.callback, self.flags = callback, flags
        pid.contents.value = self.INSTANCE
        return 0

    def DdeCreateStringHandleW(self, inst, text, code_page):
        self.calls.append(("DdeCreateStringHandleW", text, code_page))
        if "DdeCreateStringHandleW" in self.fail:
            return None
        return self.atom(text)

    def DdeNameService(self, inst, hsz1, hsz2, command):
        self.calls.append(("DdeNameService", hsz1, hsz2, command))
        return None if "DdeNameService" in self.fail else 1

    def DdeCmpStringHandles(self, hsz1, hsz2):
        return 0 if (hsz1 or 0) == (hsz2 or 0) else (-1 if (hsz1 or 0) < (hsz2 or 0) else 1)

    def DdeGetData(self, handle, dst, cb_max, offset):
        if "DdeGetData" in self.fail:
            raise OSError("DdeGetData")
        data = self.data[handle][offset:]
        if dst is None:
            return len(data)
        count = min(cb_max, len(data))
        ctypes.memmove(dst, data, count)
        return count

    def DdeFreeDataHandle(self, handle):
        self.freed_data.append(handle)
        return 1

    def DdeFreeStringHandle(self, inst, hsz):
        self.calls.append(("DdeFreeStringHandle", hsz))
        return 1

    def DdeUninitialize(self, inst):
        self.calls.append("DdeUninitialize")
        if self.on_uninitialize:
            self.on_uninitialize()
        return self.uninitialize_answer

    def DdeConnect(self, inst, service, topic, context):
        self.calls.append(("DdeConnect", service, topic))
        return None  # no server here

    def DdeGetLastError(self, inst):
        return 0x4009  # DMLERR_INVALIDPARAMETER

    # a client's

    def atom(self, text):
        return self.atoms.setdefault(text.casefold(), 0x100 + len(self.atoms))

    def connect(self, service, topic):
        """What the server answers to an XTYP_CONNECT for service/topic."""
        return self.callback(
            dde_windows.XTYP_CONNECT, 0, None, self.atom(topic), self.atom(service), None, 0, 0
        )

    def execute(self, data):
        """What the server answers to an XTYP_EXECUTE of `data` (bytes as DDEML gives them)."""
        handle = 0x900 + len(self.data)
        self.data[handle] = data
        return self.callback(
            dde_windows.XTYP_EXECUTE, 0, 0x50, self.atom(TOPIC), None, handle, 0, 0
        )


def utf16(text):
    return (text + "\0").encode("utf-16-le")


class DdeServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.ddeml = FakeDdeml()
        p = patch.object(dde_windows, "_user32", side_effect=lambda: self.ddeml)
        p.start()
        self.addCleanup(p.stop)
        self.server = DdeServer()
        self.addCleanup(self.server.stop)
        self.opened = []
        self.server.opened.connect(self.opened.append)

    def test_start_registers_qitv_system_with_the_unicode_functions(self):
        self.assertTrue(self.server.start())
        flags = self.ddeml.flags
        self.assertEqual(flags & dde_windows.APPCMD_FILTERINITS, dde_windows.APPCMD_FILTERINITS)
        self.assertEqual(flags & 0x3, dde_windows.APPCLASS_STANDARD)  # not a monitor
        for blocked in (0x1000, 0x2000, 0x8000):  # self-connections, connections, executes
            self.assertFalse(flags & blocked, hex(blocked))
        names = [c for c in self.ddeml.calls if c[0] == "DdeCreateStringHandleW"]
        self.assertEqual(
            names,
            [
                ("DdeCreateStringHandleW", SERVICE, dde_windows.CP_WINUNICODE),
                ("DdeCreateStringHandleW", TOPIC, dde_windows.CP_WINUNICODE),
            ],
        )
        service = self.ddeml.atom(SERVICE)
        self.assertIn(("DdeNameService", service, None, dde_windows.DNS_REGISTER), self.ddeml.calls)
        self.assertTrue(self.server.start())  # started already: nothing more
        self.assertEqual(self.ddeml.calls.count("DdeInitializeW"), 1)

    def test_connect_only_to_qitv_system(self):
        self.assertTrue(self.server.start())
        self.assertEqual(self.ddeml.connect("QiTV", "System"), 1)
        self.assertEqual(self.ddeml.connect("qitv", "SYSTEM"), 1)  # DDE ignores case
        self.assertFalse(self.ddeml.connect("Other", "System"))
        self.assertFalse(self.ddeml.connect("QiTV", "Topics"))

    def test_execute_reaches_opened_after_the_callback(self):
        self.assertTrue(self.server.start())
        path = "C:\\Dizi\\Bölüm 1.mkv"
        answer = self.ddeml.execute(utf16(COMMAND.replace("%1", path)))
        self.assertEqual(answer, dde_windows.DDE_FACK)
        self.assertEqual(self.ddeml.freed_data, [0x900])  # copied, then freed
        self.assertEqual(self.opened, [])  # nothing inside the callback
        self.assertTrue(wait_until(lambda: self.opened, 2000))
        self.assertEqual(self.opened, [[path]])

    def test_executes_before_the_event_loop_runs_arrive_together(self):
        self.assertTrue(self.server.start())
        first, second = "C:\\b.mkv", "\\\\server\\share\\a.mkv"
        # a block larger than its string (what follows the terminator is no part of it)
        self.assertEqual(
            self.ddeml.execute(utf16(f'[open("{first}")]') + "junk".encode("utf-16-le")),
            dde_windows.DDE_FACK,
        )
        self.assertEqual(self.ddeml.execute(utf16("[noop]")), dde_windows.DDE_FACK)
        self.assertEqual(self.ddeml.execute(utf16(f'[open("{second}")]')), dde_windows.DDE_FACK)
        self.assertTrue(wait_until(lambda: self.opened, 2000))
        wait_until(lambda: False, 50)
        self.assertEqual(self.opened, [[first, second]])

    def test_a_command_without_a_file_opens_nothing(self):
        self.assertTrue(self.server.start())
        self.assertEqual(self.ddeml.execute(utf16("[noop]")), dde_windows.DDE_FACK)
        self.assertEqual(self.ddeml.execute(b""), dde_windows.DDE_FACK)
        wait_until(lambda: False, 100)
        self.assertEqual(self.opened, [])

    def test_a_callback_that_fails_answers_not_processed(self):
        self.assertTrue(self.server.start())
        self.ddeml.fail.add("DdeGetData")
        with self.assertLogs("services.dde_windows", "WARNING"):
            answer = self.ddeml.execute(utf16('[open("C:\\x.mkv")]'))
        self.assertFalse(answer)  # DDE_FNOTPROCESSED
        self.assertEqual(self.ddeml.freed_data, [0x900])
        wait_until(lambda: False, 50)
        self.assertEqual(self.opened, [])

    def test_start_that_fails_returns_false_and_leaves_nothing(self):
        for failing in ("DdeInitializeW", "DdeCreateStringHandleW", "DdeNameService"):
            with self.subTest(failing=failing):
                self.ddeml = FakeDdeml()
                self.ddeml.fail.add(failing)
                server = DdeServer()
                with self.assertLogs("services.dde_windows", "WARNING"):
                    self.assertFalse(server.start())
                if failing != "DdeInitializeW":  # initialized: everything goes again
                    self.assertEqual(self.ddeml.calls[-1], "DdeUninitialize")
                else:
                    self.assertNotIn("DdeUninitialize", self.ddeml.calls)
                server.stop()  # nothing more
                self.assertLessEqual(self.ddeml.calls.count("DdeUninitialize"), 1)

    def test_no_ddeml_returns_false(self):
        with patch.object(dde_windows, "_user32", side_effect=OSError("no user32")):
            with self.assertLogs("services.dde_windows", "WARNING"):
                self.assertFalse(DdeServer().start())

    def test_a_call_that_raises_returns_false_and_leaves_nothing(self):
        # QiTV's start goes on whatever DDEML does (a ctypes ArgumentError, say)
        def raises(*args):
            raise ctypes.ArgumentError("argument 2: wrong type")

        for failing in ("DdeInitializeW", "DdeCreateStringHandleW", "DdeNameService"):
            with self.subTest(failing=failing):
                self.ddeml = FakeDdeml()
                setattr(self.ddeml, failing, raises)
                server = DdeServer()
                with self.assertLogs("services.dde_windows", "WARNING"):
                    self.assertFalse(server.start())
                initialized = failing != "DdeInitializeW"
                self.assertEqual(self.ddeml.calls.count("DdeUninitialize"), int(initialized))
                server.stop()
                self.assertEqual(self.ddeml.calls.count("DdeUninitialize"), int(initialized))

    def test_stop_unregisters_frees_and_uninitializes_once(self):
        self.server.stop()  # never started: nothing
        self.assertEqual(self.ddeml.calls, [])
        self.assertTrue(self.server.start())
        self.ddeml.calls.clear()
        self.server.stop()
        service, topic = self.ddeml.atom(SERVICE), self.ddeml.atom(TOPIC)
        self.assertEqual(
            self.ddeml.calls,
            [
                ("DdeNameService", service, None, dde_windows.DNS_UNREGISTER),
                ("DdeFreeStringHandle", service),
                ("DdeFreeStringHandle", topic),
                "DdeUninitialize",
            ],
        )
        self.server.stop()  # twice: nothing more
        self.assertEqual(self.ddeml.calls.count("DdeUninitialize"), 1)

    def test_a_callback_ddeml_kept_stays_alive(self):  # Ruling 28b
        self.assertTrue(self.server.start())
        first = self.ddeml.callback
        self.ddeml.uninitialize_answer = 0  # DdeUninitialize failed: DDEML may still call it
        with self.assertLogs("services.dde_windows", "WARNING"):
            self.server.stop()
        self.addCleanup(dde_windows._UNRELEASED.remove, first)
        self.assertIn(first, dde_windows._UNRELEASED)
        self.ddeml.uninitialize_answer = 1
        self.assertTrue(self.server.start())  # a new instance, with a callback of its own
        self.assertIsNot(self.ddeml.callback, first)
        self.assertIn(first, dde_windows._UNRELEASED)
        self.assertFalse(first(dde_windows.XTYP_CONNECT, 0, None, 1, 2, None, 0, 0))  # callable
        self.server.stop()
        self.assertNotIn(self.ddeml.callback, dde_windows._UNRELEASED)  # let go: not kept

    def test_the_client_never_connects_without_both_names(self):  # Ruling 28d
        self.ddeml.fail.add("DdeCreateStringHandleW")  # a 0 handle is any server or topic
        self.assertFalse(dde_windows.execute('[open("C:\\x.mkv")]'))
        self.assertNotIn("DdeConnect", [c[0] for c in self.ddeml.calls if isinstance(c, tuple)])
        self.assertEqual(self.ddeml.calls[-1], "DdeUninitialize")
        self.ddeml = FakeDdeml()
        self.assertFalse(dde_windows.execute('[open("C:\\x.mkv")]'))  # both, no server
        service, topic = self.ddeml.atom(SERVICE), self.ddeml.atom(TOPIC)
        self.assertIn(("DdeConnect", service, topic), self.ddeml.calls)

    def test_stop_never_raises(self):
        self.assertTrue(self.server.start())

        def broken(*args):
            raise OSError("gone")

        self.ddeml.DdeNameService = broken
        with self.assertLogs("services.dde_windows", "WARNING"):
            self.server.stop()


# --- main's part: served while QiTV runs registered ----------------------------------------


class Config:
    def __init__(self, file_types=None):
        self.file_types = dict(file_types or {})


class ServeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.reg = FakeWinreg()
        self.ddeml = FakeDdeml()
        patches = (
            patch.object(file_types, "_winreg", return_value=self.reg),
            patch.object(file_types, "_assoc_changed"),
            patch.object(dde_windows, "_user32", side_effect=lambda: self.ddeml),
            patch.object(sys, "platform", "win32"),
            patch.object(sys, "executable", WINDOWS_EXE),
        )
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.assertTrue(file_types._windows.register("videos", WINDOWS_EXE))
        self.registered = Config({"videos": WINDOWS_EXE})

    def writers(self):
        return [t for t in threading.enumerate() if t.name == dde_windows.WRITER_THREAD]

    def join_writers(self):
        for thread in self.writers():
            thread.join(5)

    def test_serve_writes_ddeexec_on_a_worker_and_quit_takes_it_back_first(self):
        written_on = []
        real_add = file_types.add_ddeexec
        with patch.object(
            file_types,
            "add_ddeexec",
            side_effect=lambda exe, wanted=None: (
                written_on.append(threading.current_thread()) or real_add(exe, wanted)
            ),
        ):
            serving = dde_windows.serve(self.registered, frozen=True)
            self.assertIsNotNone(serving)
            self.join_writers()
        self.assertEqual(len(written_on), 1)
        self.assertIsNot(written_on[0], threading.main_thread())  # off the GUI thread
        self.assertEqual(self.reg.values(DDEEXEC), {"": COMMAND})
        self.assertIn("DdeInitializeW", self.ddeml.calls)

        gone_before_stop = []
        self.ddeml.on_uninitialize = lambda: gone_before_stop.append(
            self.reg.values(DDEEXEC) is None
        )
        serving.close()
        self.assertEqual(gone_before_stop, [True])  # Explorer stops sending, then it stops
        self.assertIsNone(self.reg.values(DDEEXEC))
        self.assertIsNotNone(self.reg.values(f"{PROGID}\\shell\\open\\command"))
        serving.close()  # twice: nothing more
        self.assertEqual(self.ddeml.calls.count("DdeUninitialize"), 1)

    def test_an_updates_copy_points_explorer_at_the_qitv_it_replaced(self):
        # The update's copy runs from Downloads (sys.executable) once it replaced the installed
        # QiTV (registered as WINDOWS_EXE): its DDE owner names the installed one, and the
        # command Explorer starts QiTV with stays the installed QiTV's.
        downloads = "C:\\Users\\Ayşe\\Downloads\\qitv-windows.exe"
        self.addCleanup(file_types.use_installed, None)
        file_types.use_installed(WINDOWS_EXE)
        written = []
        real_add = file_types.add_ddeexec
        with (
            patch.object(sys, "executable", downloads),
            patch.object(
                file_types,
                "add_ddeexec",
                side_effect=lambda exe, wanted=None: written.append(exe) or real_add(exe, wanted),
            ),
            self.assertNoLogs("services.file_types", "INFO"),  # "names another QiTV": none
        ):
            serving = dde_windows.serve(self.registered, frozen=True)
            self.assertIsNotNone(serving)
            self.addCleanup(serving.close)
            self.join_writers()
        self.assertEqual(written, [WINDOWS_EXE])
        self.assertEqual(self.reg.values(DDEEXEC), {"": COMMAND})
        command = self.reg.values(f"{PROGID}\\shell\\open\\command")[""]
        self.assertIn(WINDOWS_EXE, command)
        self.assertNotIn("Downloads", command)
        file_types.use_installed(None)  # any other start: the exe it runs as
        with patch.object(sys, "executable", downloads):
            self.assertEqual(dde_windows.Serving(server=DdeServer())._exe, downloads)

    def test_opened_comes_from_the_server(self):
        serving = dde_windows.serve(self.registered, frozen=True)
        self.addCleanup(serving.close)
        got = []
        serving.server.opened.connect(got.append)
        self.ddeml.execute(utf16('[open("C:\\x\\a b.mkv")]'))
        self.assertTrue(wait_until(lambda: got, 2000))
        self.assertEqual(got, [["C:\\x\\a b.mkv"]])

    def test_nothing_from_source_or_elsewhere(self):
        before = self.reg.tree()
        cases = (
            (self.registered, False, "win32"),  # a run from source
            (self.registered, True, "linux"),
            (self.registered, True, "darwin"),
        )
        for config, frozen, system in cases:
            with self.subTest(system=system, frozen=frozen), patch.object(sys, "platform", system):
                self.assertIsNone(dde_windows.serve(config, frozen=frozen))
        self.join_writers()
        self.assertEqual(self.ddeml.calls, [])
        self.assertEqual(self.reg.tree(), before)

    def test_unregistered_the_owner_waits_without_serving(self):
        before = self.reg.tree()
        serving = dde_windows.serve(Config(), frozen=True)  # Settings may register it later
        self.assertIsNotNone(serving)
        self.assertFalse(serving.running)
        serving.close()  # it never served: the registry isn't touched
        self.join_writers()
        self.assertEqual(self.ddeml.calls, [])
        self.assertEqual(self.reg.tree(), before)

    def test_a_server_that_cannot_start_takes_a_stale_ddeexec_back(self):
        self.assertTrue(file_types.add_ddeexec(WINDOWS_EXE))  # a crash left it
        self.ddeml.fail.add("DdeInitializeW")
        with self.assertLogs("services.dde_windows", "WARNING"):
            serving = dde_windows.serve(self.registered, frozen=True)
        self.assertFalse(serving.running)
        self.join_writers()
        self.assertIsNone(self.reg.values(DDEEXEC))  # Explorer starts QiTV normally

    def test_follow_turns_it_on_once_and_off(self):  # Ruling 27: Settings' press
        serving = dde_windows.serve(Config(), frozen=True)
        self.addCleanup(serving.close)
        added = []

        def add(exe, wanted=None):
            added.append(exe)

        with patch.object(file_types, "add_ddeexec", side_effect=add):
            self.assertTrue(serving.follow(True))  # registered in Settings
            self.join_writers()
            self.assertTrue(serving.running)
            self.assertEqual(added, [WINDOWS_EXE])
            # again while it runs: no second server, the key written over (Ruling 28c)
            self.assertTrue(serving.follow(True))
            self.join_writers()
        self.assertEqual(self.ddeml.calls.count("DdeInitializeW"), 1)
        self.assertEqual(added, [WINDOWS_EXE, WINDOWS_EXE])
        self.assertFalse(serving.follow(False))  # unregistered: it stops
        self.assertFalse(serving.running)
        self.assertEqual(self.ddeml.calls.count("DdeUninitialize"), 1)
        with patch.object(file_types, "add_ddeexec", side_effect=add):
            self.assertTrue(serving.follow(True))  # registered again: the same server
            self.join_writers()
        self.assertEqual(added, [WINDOWS_EXE] * 3)
        serving.close()
        self.assertFalse(serving.follow(True))  # once quitting, never again
        self.assertEqual(self.ddeml.calls.count("DdeInitializeW"), 2)

    def test_the_key_waits_for_a_registration_on_its_way(self):
        # Settings' press, the start's refresh and the DDE key: one at a time
        written = threading.Event()
        with file_types._windows_lock:
            writer = threading.Thread(
                target=lambda: file_types.add_ddeexec(WINDOWS_EXE) and written.set()
            )
            writer.start()
            self.assertFalse(written.wait(0.2))
            self.assertIsNone(self.reg.values(DDEEXEC))
        writer.join(5)
        self.assertTrue(written.is_set())
        self.assertEqual(self.reg.values(DDEEXEC), {"": COMMAND})

    def test_a_second_close_does_nothing(self):  # aboutToQuit, then main()'s finally
        serving = dde_windows.serve(self.registered, frozen=True)
        self.join_writers()
        serving.close()
        self.assertIsNone(self.reg.values(DDEEXEC))
        with (
            patch.object(file_types, "remove_ddeexec") as remove,
            patch.object(file_types, "add_ddeexec") as add,
        ):
            serving.close()
            self.assertFalse(serving.follow(True))
        remove.assert_not_called()
        add.assert_not_called()
        self.assertEqual(self.ddeml.calls.count("DdeInitializeW"), 1)
        self.assertEqual(self.ddeml.calls.count("DdeUninitialize"), 1)

    def test_a_key_write_queued_behind_the_quit_writes_nothing(self):  # Ruling 28a
        serving = dde_windows.serve(Config(), frozen=True)
        with self.assertLogs("services.file_types", "INFO") as logs:
            with file_types._windows_lock:  # another change on its way
                self.assertTrue(serving.follow(True))  # its key write waits for the lock
                serving.close()  # the quit comes first (this thread may take the lock)
            self.join_writers()
        self.assertIn("isn't written", "\n".join(logs.output))
        self.assertIsNone(self.reg.values(DDEEXEC))  # never written after the removal

    def test_the_quit_waits_for_a_change_on_its_way_a_bounded_time(self):  # Ruling 28a
        serving = dde_windows.serve(self.registered, frozen=True)
        self.join_writers()
        self.assertEqual(self.reg.values(DDEEXEC), {"": COMMAND})
        held, release = threading.Event(), threading.Event()

        def hold():
            with file_types._windows_lock:
                held.set()
                release.wait(10)

        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        self.assertTrue(held.wait(5))
        try:
            with (
                patch.object(dde_windows, "QUIT_LOCK_S", 0.1),
                self.assertLogs("services.file_types", "WARNING"),
            ):
                serving.close()
        finally:
            release.set()
            holder.join(5)
        self.assertFalse(serving.running)  # it stopped all the same
        self.assertIn("DdeUninitialize", self.ddeml.calls)
        self.assertEqual(self.reg.values(DDEEXEC), {"": COMMAND})  # the next start writes it

    def test_close_never_raises(self):
        serving = dde_windows.serve(self.registered, frozen=True)
        self.join_writers()
        with (
            patch.object(file_types, "remove_ddeexec", side_effect=RuntimeError("registry")),
            self.assertLogs("services.dde_windows", "WARNING"),
        ):
            serving.close()
        self.assertIn("DdeUninitialize", self.ddeml.calls)  # it stopped all the same


# --- Windows' own DDEML ---------------------------------------------------------------------


@unittest.skipUnless(sys.platform == "win32", "DDEML is Windows'")
class LiveDdeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_live_execute_reaches_signal(self):
        server = DdeServer()
        self.assertTrue(server.start())
        self.addCleanup(server.stop)
        got = []
        server.opened.connect(got.append)
        path = "C:\\x\\a b.mkv"
        answers = []
        # Explorer's side: another thread's DDEML instance (its DdeConnect sends to this
        # thread's windows, which the event loop below pumps)
        client = threading.Thread(
            target=lambda: answers.append(dde_windows.execute(COMMAND.replace("%1", path))),
            daemon=True,
        )
        client.start()
        self.assertTrue(wait_until(lambda: got, 2000), f"client answered {answers}")
        # the client's disconnect waits for this thread's DDEML to answer: pumped, not joined
        self.assertTrue(wait_until(lambda: not client.is_alive(), 5000))
        self.assertEqual(got, [[path]])
        self.assertEqual(answers, [True])  # acknowledged (DDE_FACK)

    def test_live_paths_arrive_unchanged(self):
        server = DdeServer()
        self.assertTrue(server.start())
        self.addCleanup(server.stop)
        got = []
        server.opened.connect(got.extend)
        turkish = 'C:\\Dizi\\Bölüm 1 – "özel".mkv'  # its quotes go doubled
        long_unc = "\\\\sunucu\\paylaşım\\" + "ğüşıöç" * 50 + "\\Bölüm 2.mkv"  # > 260
        self.assertGreater(len(long_unc), 260)
        commands = [COMMAND.replace("%1", p.replace('"', '""')) for p in (turkish, long_unc)]
        answers = []
        client = threading.Thread(
            target=lambda: answers.extend(dde_windows.execute(c) for c in commands),
            daemon=True,
        )
        client.start()
        self.assertTrue(wait_until(lambda: len(got) == 2, 2000), f"client answered {answers}")
        self.assertTrue(wait_until(lambda: not client.is_alive(), 5000))
        self.assertEqual(got, [turkish, long_unc])  # code point for code point
        self.assertEqual(
            [p.encode("utf-16-le") for p in got],
            [p.encode("utf-16-le") for p in (turkish, long_unc)],
        )
        self.assertEqual(answers, [True, True])


if __name__ == "__main__":
    unittest.main()
