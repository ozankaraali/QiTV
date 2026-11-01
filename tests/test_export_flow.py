import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from services.exporter import MSG_CANCELLED, MSG_LOAD_LIVE, MSG_NOTHING_LOADED, ExportKind
from ui.export_flow import (
    BUSY_TEXT,
    CANCEL_TEXT,
    HOLDS_SIGN_IN,
    MAY_HOLD_SIGN_IN,
    PROGRESS_MS,
    RESULT_MS,
    ExportFlow,
    default_path,
)

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
        self.config = SimpleNamespace(ssl_verify=True)
        self.runner = HeldRunner()
        self.toasts = []
        self.buttons = []  # each message's button: (its words, what a click does) or None

        def notify(text, kind, ms, action="", on_action=None):
            self.toasts.append((text, kind, ms))
            self.buttons.append((action, on_action) if on_action is not None else None)

        self.flow = ExportFlow(
            catalog=SimpleNamespace(provider_manager=self.manager),
            config=self.config,
            runner=self.runner,
            notify=notify,
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
        self.assertEqual(
            self.toasts[-1], (f"Exported 2 items to home.m3u {MAY_HOLD_SIGN_IN}", "info", RESULT_MS)
        )

    def test_the_dialog_and_the_result_name_the_provider_in_all_providers(self):
        # All providers lists every provider; the export is one provider's.
        self.flow._catalog = SimpleNamespace(
            provider_manager=self.manager, all_providers=lambda: True
        )
        ask = self.start()
        self.assertEqual(ask.call_args[0][1], "Export Home's channels")
        self.runner.jobs[0][1](None, 2)
        self.assertEqual(
            self.toasts[-1][0], f"Exported 2 items from Home to home.m3u {MAY_HOLD_SIGN_IN}"
        )

    def test_all_live_from_a_library_without_channels_exports_the_channel_lists(self):
        # The Library shows a provider of films only; the channel list's has channels.
        films = SimpleNamespace(
            current_provider={"type": "XTREAM", "name": "Films"},
            current_provider_content={"vod": {"contents": [{"id": 9, "name": "A film"}]}},
            headers={},
        )
        library = SimpleNamespace(provider_manager=films)
        with mock.patch(ASK, return_value=("/exports/home.m3u", "")) as ask:
            self.flow.export(ExportKind.ALL_LIVE, "movies", library)
        self.assertEqual(ask.call_args[0][1], "Export Home's channels")  # whose, named
        self.assertTrue(ask.call_args[0][2].endswith("Home-all_live.m3u"))
        self.runner.jobs[0][1](None, 2)
        self.assertEqual(
            self.toasts[-1],
            (
                "Exported 2 items from Home to home.m3u "
                "The file may hold your sign-in: keep it private.",
                "info",
                RESULT_MS,
            ),
        )

    def test_all_live_says_plainly_when_the_provider_has_no_channels(self):
        films = SimpleNamespace(
            current_provider={"type": "XTREAM", "name": "Films"},
            current_provider_content={"itv": {"categories": [], "contents": {}}},
            headers={},
        )
        library = SimpleNamespace(provider_manager=films)
        self.manager.current_provider_content = {}  # the channel list has none loaded either
        with mock.patch(ASK) as ask:
            self.flow.export(ExportKind.ALL_LIVE, "movies", library)
        ask.assert_not_called()
        self.assertEqual(self.toasts, [("Films has no live channels.", "info", 3000)])
        films.current_provider_content = {}  # not loaded yet: Live TV loads them
        with mock.patch(ASK) as ask:
            self.flow.export(ExportKind.ALL_LIVE, "movies", library)
        ask.assert_not_called()
        self.assertEqual(self.toasts[-1], (MSG_LOAD_LIVE, "info", 3000))

    def test_one_provider_shown_keeps_the_plain_dialog(self):
        ask = self.start()
        self.assertEqual(ask.call_args[0][1], "Export")

    def test_the_result_says_the_file_holds_the_sign_in(self):  # a privacy review
        for kind, note in (("XTREAM", HOLDS_SIGN_IN), ("STB", HOLDS_SIGN_IN)):
            with self.subTest(kind=kind):
                self.manager.current_provider = {"type": kind, "name": "Home"}
                self.start()
                self.runner.jobs[-1][1](None, 2)
                self.assertEqual(self.toasts[-1][0], f"Exported 2 items to home.m3u {note}")
        self.start()
        self.runner.jobs[-1][1](None, 0)  # no link written: nothing to say of it
        self.assertEqual(self.toasts[-1], ("Exported 0 items to home.m3u", "info", 3000))

    def test_a_running_export_has_a_cancel_button(self):
        # Esc is no SHORTCUTS key: a mouse alone stops a long series crawl too.
        self.start()
        self.assertEqual(self.buttons[-1][0], CANCEL_TEXT)
        worker = self.runner.jobs[0][0]
        worker.progress.emit(3, 10, "Lantern Street")
        QApplication.processEvents()
        action, on_action = self.buttons[-1]
        self.assertEqual((self.toasts[-1][0], action), ("Exporting… 3/10", CANCEL_TEXT))
        self.assertTrue(on_action())  # the button is the export's cancel
        self.assertTrue(worker._cancel.is_set())
        self.assertEqual(self.toasts[-1], ("Exporting… 3/10", "info", PROGRESS_MS))
        self.assertIsNone(self.buttons[-1])  # cancelling: no button to press again

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
