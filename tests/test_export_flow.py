import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from services.exporter import MSG_CANCELLED, MSG_NOTHING_LOADED, ExportKind
from ui.export_flow import BUSY_TEXT, PROGRESS_MS, ExportFlow, default_path

CHANNELS = [{"id": 1, "name": "One", "cmd": "http://tv/1"}, {"id": 2, "name": "Two"}]
ASK = "ui.export_flow.QFileDialog.getSaveFileName"


class HeldRunner:
    """Keeps each worker without running it: the export stays running until the test ends it."""

    def __init__(self):
        self.jobs = []

    def run_worker(self, worker, on_finished, on_error=None, context=None):
        self.jobs.append((worker, on_finished, on_error))
        return worker


class ExportFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.manager = SimpleNamespace(
            current_provider={"type": "M3UPLAYLIST", "name": "Home"},
            current_provider_content={"itv": {"contents": list(CHANNELS)}},
            headers={},
        )
        self.config = SimpleNamespace(ssl_verify=True, prefer_https=False)
        self.runner = HeldRunner()
        self.toasts = []
        self.flow = ExportFlow(
            catalog=SimpleNamespace(provider_manager=self.manager),
            config=self.config,
            runner=self.runner,
            notify=lambda text, kind, ms: self.toasts.append((text, kind, ms)),
        )
        self.addCleanup(self.flow.shutdown)

    def start(self, path="/exports/home.m3u", kind=ExportKind.CACHED):
        with mock.patch(ASK, return_value=(path, "")) as ask:
            self.flow.export(kind, "live")
        return ask

    def test_the_default_path_is_in_downloads_and_safe_for_a_file_name(self):
        path = Path(default_path("News/Sports: HD*", ExportKind.ALL_LIVE))
        self.assertEqual(path.parent, Path.home() / "Downloads")
        self.assertEqual(path.name, "News_Sports_ HD-all_live.m3u")
        self.assertEqual(Path(default_path("", ExportKind.CACHED)).name, "QiTV-cached.m3u")

    def test_a_reason_is_shown_and_no_path_asked(self):
        self.manager.current_provider_content = {}
        ask = self.start()
        ask.assert_not_called()
        self.assertEqual(self.toasts, [(MSG_NOTHING_LOADED, "info", 3000)])
        self.assertFalse(self.flow.running())

    def test_no_path_no_export(self):
        self.start(path="")
        self.assertEqual(self.runner.jobs, [])
        self.assertFalse(self.flow.running())

    def test_one_export_at_a_time(self):
        self.start()
        self.assertTrue(self.flow.running())
        ask = self.start()
        ask.assert_not_called()
        self.assertEqual(self.toasts[-1], (BUSY_TEXT, "info", 3000))
        self.assertEqual(len(self.runner.jobs), 1)
        worker, on_finished, _on_error = self.runner.jobs[0]
        on_finished(None, 2)
        self.assertFalse(self.flow.running())
        self.assertEqual(self.toasts[-1], ("Exported 2 items to home.m3u", "info", 3000))

    def test_progress_is_a_lasting_toast(self):
        self.start()
        self.assertEqual(self.toasts[-1], ("Exporting…", "info", PROGRESS_MS))
        worker = self.runner.jobs[0][0]
        worker.progress.emit(3, 10, "Lantern Street")
        QApplication.processEvents()  # queued to the GUI thread
        self.assertEqual(self.toasts[-1], ("Exporting… 3/10", "info", PROGRESS_MS))

    def test_cancel_stops_it_once_and_says_so(self):
        self.start()
        worker, _on_finished, on_error = self.runner.jobs[0]
        self.assertTrue(self.flow.cancel())
        self.assertTrue(worker._cancel.is_set())
        self.assertFalse(self.flow.cancel())  # already cancelling: Esc does its usual job
        worker.progress.emit(4, 10, "late")
        QApplication.processEvents()
        self.assertNotIn(("Exporting… 4/10", "info", PROGRESS_MS), self.toasts)
        self.assertTrue(self.flow.running())  # until the worker has stopped
        on_error(None, MSG_CANCELLED)
        self.assertEqual(self.toasts[-1], (MSG_CANCELLED, "info", 3000))
        self.assertFalse(self.flow.running())
        self.assertFalse(self.flow.cancel())

    def test_a_failure_is_an_error_toast(self):
        self.start()
        self.runner.jobs[0][2](None, "Export failed: couldn’t write the file")
        self.assertEqual(self.toasts[-1][1], "error")

    def test_the_worker_reads_a_copy_of_the_content(self):
        self.start()
        worker = self.runner.jobs[0][0]
        self.manager.current_provider_content["itv"]["contents"].append({"id": 3})
        self.assertEqual(worker._content["itv"]["contents"], CHANNELS)

    def test_shutdown_cancels_a_running_export(self):
        self.start()
        worker = self.runner.jobs[0][0]
        self.flow.shutdown()
        self.assertTrue(worker._cancel.is_set())
        self.runner.jobs[0][1](None, 2)
        self.assertNotIn("Exported", self.toasts[-1][0])  # nothing shown while closing


if __name__ == "__main__":
    unittest.main()
