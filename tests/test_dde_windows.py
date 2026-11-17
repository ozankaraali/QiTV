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
        return 1

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
            side_effect=lambda exe: written_on.append(threading.current_thread()) or real_add(exe),
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

    def test_opened_comes_from_the_server(self):
        serving = dde_windows.serve(self.registered, frozen=True)
        self.addCleanup(serving.close)
        got = []
        serving.server.opened.connect(got.append)
        self.ddeml.execute(utf16('[open("C:\\x\\a b.mkv")]'))
        self.assertTrue(wait_until(lambda: got, 2000))
        self.assertEqual(got, [["C:\\x\\a b.mkv"]])

    def test_nothing_unregistered_from_source_or_elsewhere(self):
        before = self.reg.tree()
        cases = (
            (Config(), True, "win32"),  # not registered
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

    def test_a_server_that_cannot_start_takes_a_stale_ddeexec_back(self):
        self.assertTrue(file_types.add_ddeexec(WINDOWS_EXE))  # a crash left it
        self.ddeml.fail.add("DdeInitializeW")
        with self.assertLogs("services.dde_windows", "WARNING"):
            self.assertIsNone(dde_windows.serve(self.registered, frozen=True))
        self.join_writers()
        self.assertIsNone(self.reg.values(DDEEXEC))  # Explorer starts QiTV normally

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


if __name__ == "__main__":
    unittest.main()
