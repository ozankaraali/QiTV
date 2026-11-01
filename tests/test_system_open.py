"""Web pages and folders open with the system's program (services/system_open.py): a frozen
Linux build starts xdg-open itself, without its bundle's library path, and never waits."""

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QUrl

from services import system_open

BUNDLE = {
    "PATH": "/usr/bin",
    "LD_LIBRARY_PATH": "/tmp/_MEI1",
    "LD_LIBRARY_PATH_ORIG": "/opt/lib",
    "VLC_PLUGIN_PATH": "/tmp/_MEI1/vlc/plugins",
}


class SystemOpenTests(unittest.TestCase):
    def open(self, url, *, system="Linux", frozen=True, popen=None):
        """`open_url(url)` on `system`; (its answer, Popen's mock, openUrl's mock)."""
        with (
            mock.patch.object(system_open.platform, "system", return_value=system),
            mock.patch.object(sys, "frozen", frozen, create=True),
            mock.patch.dict(os.environ, BUNDLE, clear=True),
            mock.patch.object(system_open.subprocess, "Popen", popen or mock.MagicMock()) as run,
            mock.patch.object(
                system_open.QDesktopServices, "openUrl", return_value=True
            ) as open_url,
        ):
            return system_open.open_url(url), run, open_url

    def test_a_frozen_linux_app_starts_xdg_open_with_the_systems_libraries(self):
        opened, popen, open_url = self.open("https://example.invalid/page")
        self.assertTrue(opened)
        open_url.assert_not_called()
        popen.assert_called_once()
        self.assertEqual(popen.call_args.args[0], ["xdg-open", "https://example.invalid/page"])
        env = popen.call_args.kwargs["env"]
        self.assertEqual(env["LD_LIBRARY_PATH"], "/opt/lib")  # from before QiTV started
        self.assertNotIn("VLC_PLUGIN_PATH", env)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])  # never waited for

    def test_a_folder_opens_by_its_path_and_one_not_there_says_so(self):
        with tempfile.TemporaryDirectory() as folder:
            opened, popen, _open_url = self.open(QUrl.fromLocalFile(folder))
            self.assertTrue(opened)
            self.assertEqual(popen.call_args.args[0], ["xdg-open", folder])
            missing = str(Path(folder, "Recordings"))
            opened, popen, _open_url = self.open(QUrl.fromLocalFile(missing))
            self.assertFalse(opened)
            popen.assert_not_called()

    def test_open_folder_is_a_local_url(self):
        with tempfile.TemporaryDirectory() as folder:
            with (
                mock.patch.object(system_open.platform, "system", return_value="Darwin"),
                mock.patch.object(
                    system_open.QDesktopServices, "openUrl", return_value=True
                ) as open_url,
            ):
                self.assertTrue(system_open.open_folder(folder))
            self.assertEqual(open_url.call_args.args[0].toLocalFile(), folder)

    def test_without_xdg_open_qt_opens_it(self):
        opened, _popen, open_url = self.open(
            "https://example.invalid/page", popen=mock.MagicMock(side_effect=FileNotFoundError)
        )
        self.assertTrue(opened)
        self.assertEqual(open_url.call_args.args[0].toString(), "https://example.invalid/page")

    def test_elsewhere_and_from_source_qt_opens_it(self):
        for system, frozen in (("Linux", False), ("Windows", True), ("Darwin", True)):
            with self.subTest(system=system, frozen=frozen):
                opened, popen, open_url = self.open(
                    "https://example.invalid/page", system=system, frozen=frozen
                )
                self.assertTrue(opened)
                popen.assert_not_called()
                self.assertEqual(
                    open_url.call_args.args[0].toString(), "https://example.invalid/page"
                )


class CallersTests(unittest.TestCase):
    """Every page and folder QiTV opens goes through system_open."""

    def test_no_caller_opens_a_url_on_its_own(self):
        root = Path(__file__).resolve().parent.parent
        for path in [*root.glob("ui/**/*.py"), *root.glob("services/**/*.py")]:
            if path.name == "system_open.py":
                continue
            source = path.read_text(encoding="utf-8")
            for call in ("QDesktopServices.openUrl", "webbrowser.open", '"xdg-open"'):
                self.assertNotIn(call, source, f"{path.relative_to(root)}: {call}")


if __name__ == "__main__":
    unittest.main()
