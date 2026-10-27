"""QiTV's log on disk (2.7.2): a packaged app has no terminal, so its log is also kept in a
small rotating file, every line masked, with native crashes' stacks beside it."""

import faulthandler
import logging
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from services import log_file


class LogDirTests(unittest.TestCase):
    def test_each_systems_place_for_logs(self):
        home = Path("/home/u")
        self.assertEqual(log_file.log_dir("Darwin", {}, home), home / "Library" / "Logs" / "QiTV")
        self.assertEqual(
            log_file.log_dir("Windows", {"LOCALAPPDATA": "C:/L"}, home),
            Path("C:/L") / "QiTV" / "Logs",
        )
        self.assertEqual(
            log_file.log_dir("Linux", {"XDG_STATE_HOME": "/s"}, home), Path("/s") / "qitv"
        )
        self.assertEqual(log_file.log_dir("Linux", {}, home), home / ".local" / "state" / "qitv")

    def test_a_config_of_its_own_keeps_its_log_beside_it(self):
        env = {"QITV_CONFIG_DIR": "/tmp/smoke"}
        self.assertEqual(log_file.log_dir("Darwin", env, Path("/h")), Path("/tmp/smoke/logs"))


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="qitv-log-"))
        root = logging.getLogger()
        before = list(root.handlers)
        hook = sys.excepthook
        self.addCleanup(setattr, sys, "excepthook", hook)
        self.addCleanup(faulthandler.disable)
        self.addCleanup(self.drop_handlers, before)
        self.addCleanup(log_file.close)
        level = root.level
        self.addCleanup(root.setLevel, level)
        root.setLevel(logging.INFO)

    @staticmethod
    def drop_handlers(before):
        root = logging.getLogger()
        for handler in [h for h in root.handlers if h not in before]:
            root.removeHandler(handler)
            handler.close()

    def text(self) -> str:
        for handler in logging.getLogger().handlers:
            handler.flush()
        return (self.directory / log_file.LOG_NAME).read_text(encoding="utf-8")

    def test_lines_go_to_the_file_with_addresses_masked(self):
        self.assertEqual(log_file.install(self.directory), self.directory / log_file.LOG_NAME)
        logging.getLogger("services.anything").info(
            "Retrying %s", "http://panel.test:8080/player_api.php?username=u&password=secret"
        )
        text = self.text()
        self.assertIn("[INFO] services.anything: Retrying", text)
        for secret in ("secret", "panel.test", "username="):
            self.assertNotIn(secret, text)

    def test_the_detailed_log_adds_qitvs_own_debug_lines(self):  # 2.8.2, Settings
        log_file.install(self.directory)
        self.addCleanup(log_file.set_detailed, False)
        logging.getLogger("services.anything").debug("hidden while not detailed")
        log_file.set_detailed(True)
        logging.getLogger("services.anything").debug("Tuning %s", "http://p.test/x?token=t")
        logging.getLogger("ui.anything").debug("a screen's line")
        logging.getLogger("urllib3.connectionpool").debug("http://p.test/x?token=t")  # not ours
        text = self.text()
        self.assertNotIn("hidden while not detailed", text)
        self.assertIn("[DEBUG] services.anything: Tuning", text)
        self.assertIn("[DEBUG] ui.anything: a screen's line", text)
        self.assertNotIn("urllib3", text)
        self.assertNotIn("token=", text)  # masked, as every line
        handler = next(h for h in logging.getLogger().handlers if hasattr(h, "maxBytes"))
        self.assertEqual(handler.maxBytes, log_file.DETAILED_MAX_BYTES)  # keeps more
        log_file.set_detailed(False)
        logging.getLogger("services.anything").debug("hidden again")
        self.assertNotIn("hidden again", self.text())
        self.assertEqual(handler.maxBytes, log_file.MAX_BYTES)

    def test_a_native_crash_dumps_its_stacks_beside_the_log(self):
        log_file.install(self.directory)
        self.assertTrue(faulthandler.is_enabled())
        self.assertTrue((self.directory / log_file.CRASH_NAME).exists())

    def test_an_unhandled_exception_is_logged_without_its_message(self):
        log_file.install(self.directory)
        try:
            raise ValueError("http://panel.test/get.php?username=u&password=secret")
        except ValueError:
            sys.excepthook(*sys.exc_info())
        text = self.text()
        self.assertIn("Unhandled ValueError", text)
        self.assertIn("test_an_unhandled_exception_is_logged_without_its_message", text)
        self.assertNotIn("secret", text)

    def test_a_place_that_cannot_be_written_leaves_the_console_log(self):
        with patch.object(Path, "mkdir", side_effect=PermissionError(13, "denied")):
            with self.assertLogs("services.log_file", "WARNING"):
                self.assertIsNone(log_file.install(self.directory / "no"))


if __name__ == "__main__":
    unittest.main()
