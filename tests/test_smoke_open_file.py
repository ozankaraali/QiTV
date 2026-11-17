"""scripts/smoke_open_file.py's registration for a run (--dde, CI's Windows runner): once tried,
it is taken back at the end whatever it answered, raised, or whatever the run did after it.
Nothing here touches a registry or starts QiTV: the backend is a fake and QiTV's part a mock."""

import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import smoke_open_file
from services import file_types
from services.file_types import RegisterResult

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "smoke.mp4"


class FakeBackend:
    """Records each call; `answer()` is what registering does (it may raise)."""

    def __init__(self, answer):
        self._answer = answer
        self.calls: list[tuple[str, str, str]] = []

    def register(self, kind, exe):
        self.calls.append(("register", kind, exe))
        return self._answer()

    def unregister(self, kind, exe):
        self.calls.append(("unregister", kind, exe))
        return True


def raises():
    raise OSError("access denied after a part was written")


class RegistrationTakenBackTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        self.exe = self.base / "qitv-windows.exe"
        self.exe.write_bytes(b"")

    def run_smoke(self, answer, dde=True, one_qitv=None):
        self.backend = backend = FakeBackend(answer)
        with (
            mock.patch.object(smoke_open_file, "WINDOWS", True),
            mock.patch.object(file_types, "_windows", backend),
            mock.patch.object(smoke_open_file.OpenFileSmoke, "one_qitv", one_qitv or mock.Mock()),
            mock.patch.dict(os.environ),  # run() sets QiTV's variables for its starts
            contextlib.redirect_stdout(io.StringIO()),
        ):
            result = smoke_open_file.run(str(self.exe), self.base / "out", str(FIXTURE), dde=dde)
        return backend, result

    def test_a_registration_that_failed_is_taken_back(self):
        backend, result = self.run_smoke(lambda: RegisterResult(False))
        self.assertEqual(
            [call[0] for call in backend.calls], ["register", "unregister"], backend.calls
        )
        self.assertFalse(result["ok"])
        self.assertFalse(result["dde_registered"])
        self.assertTrue(result["dde_unregistered"])

    def test_a_registration_that_raised_is_taken_back_and_the_run_goes_on(self):
        one_qitv = mock.Mock()
        backend, result = self.run_smoke(raises, one_qitv=one_qitv)
        self.assertEqual([call[0] for call in backend.calls], ["register", "unregister"])
        self.assertIn("OSError", " ".join(result["failures"]))
        one_qitv.assert_called_once()
        self.assertFalse(one_qitv.call_args.args[2])  # no DDE check without a registration

    def test_a_registration_is_taken_back_when_the_run_raises(self):
        one_qitv = mock.Mock(side_effect=RuntimeError("the run broke"))
        with self.assertRaises(RuntimeError):
            self.run_smoke(lambda: RegisterResult(True, True), one_qitv=one_qitv)
        self.assertTrue(one_qitv.call_args.args[2])
        self.assertEqual([call[0] for call in self.backend.calls], ["register", "unregister"])

    def test_a_registration_that_worked_is_taken_back(self):
        backend, result = self.run_smoke(lambda: RegisterResult(True, True))
        self.assertEqual(
            backend.calls,
            [("register", "videos", str(self.exe)), ("unregister", "videos", str(self.exe))],
        )
        self.assertTrue(result["ok"], result["failures"])

    def test_without_dde_nothing_is_registered(self):
        backend, result = self.run_smoke(lambda: RegisterResult(True, True), dde=False)
        self.assertEqual(backend.calls, [])
        self.assertNotIn("dde_registered", result)


if __name__ == "__main__":
    unittest.main()
