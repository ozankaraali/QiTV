from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import requests

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from services import thread_cleanup
from services.exporter import (
    MSG_NOTHING_LOADED,
    ExportKind,
    ExportWorker,
    collect_items,
    export_available,
    write_items,
)
from services.jobs import JobRunner


def live_content(n=3):
    return {
        "itv": {
            "contents": {
                "1": [
                    {"id": str(i), "name": f"Ch {i}", "cmd": f"http://cdn.test/{i}.m3u8"}
                    for i in range(n)
                ]
                + [{"id": "0", "name": "dup", "cmd": "x"}]
            }
        }
    }


class FakePortal:
    """STB portal: 2 series x 2 seasons x 2 episodes; series in ``broken`` answer 500."""

    def __init__(self, broken=(), on_request=None, stall=0.0, malformed=False):
        self.stall = stall
        self.malformed = malformed
        self.broken = set(broken)
        self.on_request = on_request
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a):
                pass

            def do_GET(self):
                parts = urlsplit(self.path)
                q = {k: v[0] for k, v in parse_qs(parts.query).items()}
                owner.requests.append((parts.path, q, dict(self.headers)))
                if owner.stall:
                    time.sleep(owner.stall)
                if owner.on_request:
                    owner.on_request()
                sid = q.get("movie_id")
                if parts.path != "/server/load.php" or sid in owner.broken:
                    self.send_response(500)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if owner.malformed:
                    body = json.dumps({"js": {"data": None}}).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                season = q.get("season_id")
                if season == "0":
                    data = [{"id": f"{sid}-s{n}", "name": f"Season {n}"} for n in (1, 2)]
                else:
                    data = [
                        {
                            "id": f"{season}-e{n}",
                            "name": f"Ep {n}",
                            "cmd": f"http://cdn.test/{season}/{n}",
                        }
                        for n in (1, 2)
                    ]
                body = json.dumps({"js": {"data": data}}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_port}/c/"
        # A daemon: a run interrupted before close() must still exit.
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)


def series_content():
    return {
        "series": {
            "contents": {
                "9": [
                    {"id": "a", "category_id": "9", "name": "Show A"},
                    {"id": "b", "category_id": "9", "name": "Show B"},
                ]
            }
        }
    }


class ExporterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "out.m3u")

    def worker(self, kind, provider, content, section, cancel=None):
        return ExportWorker(
            kind,
            provider,
            content,
            section,
            self.path,
            verify_ssl=True,
            prefer_https=False,
            headers={"User-Agent": "t"},
            cancel=cancel or threading.Event(),
        )

    def run_worker(self, worker):
        out = {"progress": [], "finished": None, "error": None}
        worker.progress.connect(lambda *a: out["progress"].append(a))
        worker.finished.connect(lambda v: out.__setitem__("finished", v))
        worker.error.connect(lambda m: out.__setitem__("error", m))
        worker.run()
        return out

    def read(self):
        with open(self.path, encoding="utf-8") as f:
            return f.read()

    def test_cached_m3u_one_extinf_per_item(self):
        provider = {"type": "M3U", "url": "x"}
        out = self.run_worker(self.worker(ExportKind.CACHED, provider, live_content(), "live"))
        self.assertEqual(out["finished"], 3)
        self.assertEqual(self.read().count("#EXTINF"), 3)
        self.assertEqual(out["progress"], [(1, 1, "Writing…")])

    def test_write_items_never_takes_provider_as_list(self):
        provider = {"type": "M3U"}
        self.assertEqual(write_items(provider, [{"name": "a", "cmd": "u"}], self.path), 1)
        self.assertIn("#EXTINF", self.read())

    def test_cached_stb_uses_portal_base(self):
        provider = {"type": "STB", "url": "http://portal.test:8080/c/", "mac": "00:1A:79:00:00:01"}
        content = {"itv": [{"id": "1", "name": "A", "cmd": "ffmpeg http://localhost/ch/77_x"}]}
        self.run_worker(self.worker(ExportKind.CACHED, provider, content, "live"))
        text = self.read()
        self.assertIn("http://portal.test:8080/play/live.php?mac=00:1A:79:00:00:01&stream=77", text)
        self.assertNotIn("//play", text)

    def test_collect_items_dedupes(self):
        items = collect_items(live_content(), "live")
        self.assertEqual([i["name"] for i in items], ["Ch 0", "Ch 1", "Ch 2"])

    def test_availability(self):
        m3u, stb = {"type": "M3U"}, {"type": "STB"}
        self.assertEqual(
            export_available(ExportKind.ALL_LIVE, m3u, "live", {}), "Load Live TV first."
        )
        self.assertIsNone(export_available(ExportKind.ALL_LIVE, m3u, "live", live_content()))
        self.assertEqual(
            export_available(ExportKind.CACHED, m3u, "movies", live_content()),
            "Nothing is loaded in this section yet.",
        )
        self.assertIsNone(export_available(ExportKind.CACHED, m3u, "live", live_content()))
        self.assertIsNone(export_available(ExportKind.COMPLETE_SERIES, stb, "series"))
        reason = "Complete export is only available for STB series. Use “Cached content” instead."
        self.assertEqual(
            export_available(ExportKind.COMPLETE_SERIES, {"type": "XTREAM"}, "series"), reason
        )
        self.assertEqual(export_available(ExportKind.COMPLETE_SERIES, stb, "movies"), reason)

    def test_complete_series_xtream_says_it_is_for_stb_series_only(self):
        out = self.run_worker(
            self.worker(ExportKind.COMPLETE_SERIES, {"type": "XTREAM"}, series_content(), "series")
        )
        self.assertIn("only available for STB series", out["error"])
        self.assertFalse(os.path.exists(self.path))

    def test_complete_series_against_portal(self):
        portal = FakePortal()
        self.addCleanup(portal.close)
        provider = {"type": "STB", "url": portal.url, "mac": "00:1A:79:00:00:02"}
        out = self.run_worker(
            self.worker(ExportKind.COMPLETE_SERIES, provider, series_content(), "series")
        )
        self.assertEqual(out["finished"], 8)
        self.assertEqual(self.read().count("#EXTINF"), 8)
        self.assertEqual(out["progress"][-1][:2], (2, 2))

    def test_cancel_during_first_series(self):
        cancel = threading.Event()
        portal = FakePortal(on_request=cancel.set)
        self.addCleanup(portal.close)
        provider = {"type": "STB", "url": portal.url, "mac": "m"}
        out = self.run_worker(
            self.worker(ExportKind.COMPLETE_SERIES, provider, series_content(), "series", cancel)
        )
        self.assertEqual(out["error"], "Export cancelled")
        self.assertIsNone(out["finished"])
        self.assertFalse(os.path.exists(self.path))

    def test_failing_series_is_skipped(self):
        portal = FakePortal(broken={"a"})
        self.addCleanup(portal.close)
        provider = {"type": "STB", "url": portal.url, "mac": "m"}
        out = self.run_worker(
            self.worker(ExportKind.COMPLETE_SERIES, provider, series_content(), "series")
        )
        self.assertEqual(out["finished"], 4)
        self.assertEqual(self.read().count("#EXTINF"), 4)

    def test_portal_receives_expected_params_and_headers(self):
        portal = FakePortal()
        self.addCleanup(portal.close)
        provider = {"type": "STB", "url": portal.url, "mac": "m"}
        self.run_worker(
            self.worker(ExportKind.COMPLETE_SERIES, provider, series_content(), "series")
        )
        path, q, headers = portal.requests[0]
        self.assertEqual(path, "/server/load.php")
        self.assertEqual(
            q,
            {
                "type": "series",
                "action": "get_ordered_list",
                "category_id": "9",
                "movie_id": "a",
                "season_id": "0",
                "sortby": "name",
                "JsHttpRequest": "1-xml",
            },
        )
        self.assertEqual(headers.get("User-Agent"), "t")
        self.assertTrue(any(r[1]["sortby"] == "added" for r in portal.requests))

    def test_provider_https_preference_overrides_global(self):
        portal = FakePortal()
        self.addCleanup(portal.close)
        # the provider says no HTTPS: the global preference must not upgrade the fetch
        provider = {"type": "STB", "url": portal.url, "mac": "m", "prefer_https": False}
        w = self.worker(ExportKind.COMPLETE_SERIES, provider, series_content(), "series")
        w._prefer_https = True
        self.assertEqual(self.run_worker(w)["finished"], 8)
        # global preference applies when the provider is silent: http portal is not reached
        provider = {"type": "STB", "url": portal.url, "mac": "m"}
        w = self.worker(ExportKind.COMPLETE_SERIES, provider, series_content(), "series")
        w._prefer_https = True
        before = len(portal.requests)
        out = self.run_worker(w)
        self.assertEqual(len(portal.requests), before)
        self.assertEqual(out["error"], "Couldn\u2019t reach the provider to fetch episodes.")

    def test_verify_flag_is_passed(self):
        provider = {"type": "STB", "url": "http://portal.test/c/", "mac": "m"}
        w = self.worker(ExportKind.COMPLETE_SERIES, provider, series_content(), "series")
        w._verify_ssl = False
        with mock.patch("requests.Session.get", side_effect=requests.ConnectionError()) as get:
            self.run_worker(w)
        self.assertIs(get.call_args.kwargs["verify"], False)
        self.assertEqual(get.call_args.kwargs["timeout"], (10, 30))

    def test_write_failure_is_an_error(self):
        self.path = os.path.join(self.dir.name, "no-such-dir", "out.m3u")
        out = self.run_worker(
            self.worker(ExportKind.CACHED, {"type": "M3U"}, live_content(), "live")
        )
        self.assertIsNone(out["finished"])
        self.assertEqual(out["error"], "Export failed: couldn\u2019t write the file")

    def test_write_items_counts_written_and_tolerates_none_cmd(self):
        items = [{"name": "a", "cmd": "u"}, {"name": "b", "cmd": None}, {"name": "c"}]
        self.assertEqual(write_items({"type": "M3U"}, items, self.path), 1)
        self.assertEqual(
            write_items({"type": "STB", "url": "http://p.test/c/"}, items, self.path), 1
        )

    def test_the_fake_portal_never_keeps_the_test_process_alive(self):
        # A run interrupted before close() must still exit (as tests/http_fixtures.LocalSource).
        portal = FakePortal()
        self.addCleanup(portal.close)
        self.assertTrue(portal.thread.daemon)

    def test_malformed_portal_data_is_skipped(self):
        portal = FakePortal(malformed=True)
        self.addCleanup(portal.close)
        provider = {"type": "STB", "url": portal.url, "mac": "m"}
        out = self.run_worker(
            self.worker(ExportKind.COMPLETE_SERIES, provider, series_content(), "series")
        )
        self.assertEqual(out["error"], "No episodes found to export.")

    def test_all_requests_failing_reports_unreachable(self):
        portal = FakePortal(broken={"a", "b"})
        self.addCleanup(portal.close)
        provider = {"type": "STB", "url": portal.url, "mac": "m"}
        out = self.run_worker(
            self.worker(ExportKind.COMPLETE_SERIES, provider, series_content(), "series")
        )
        self.assertEqual(out["error"], "Couldn\u2019t reach the provider to fetch episodes.")

    def test_cancel_aborts_a_stalled_request(self):
        portal = FakePortal(stall=5.0)
        self.addCleanup(portal.close)
        provider = {"type": "STB", "url": portal.url, "mac": "m"}
        cancel = threading.Event()
        threading.Timer(0.3, cancel.set).start()
        started = time.monotonic()
        out = self.run_worker(
            self.worker(ExportKind.COMPLETE_SERIES, provider, series_content(), "series", cancel)
        )
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(out["error"], "Export cancelled")
        self.assertEqual(out["progress"][0], (0, 2, "Show A"))
        self.assertFalse(os.path.exists(self.path))

    def test_unexpected_exception_message_is_type_only(self):
        w = self.worker(ExportKind.CACHED, {"type": "M3U"}, live_content(), "live")
        with mock.patch(
            "services.exporter.write_items",
            side_effect=RuntimeError("http://host/get.php?username=u&password=secret"),
        ):
            out = self.run_worker(w)
        self.assertEqual(out["error"], "Export failed: RuntimeError")

    def test_real_job_runner(self):
        provider = {"type": "M3U"}
        got = []
        runner = JobRunner()
        runner.run_worker(
            self.worker(ExportKind.ALL_LIVE, provider, live_content(), "live"),
            lambda ctx, payload: got.append(payload),
            lambda ctx, msg: got.append(msg),
        )
        for _ in range(100):
            if got and not thread_cleanup.has_pending_threads():
                break
            QTest.qWait(50)
        QTest.qWait(50)
        self.assertEqual(got, [3])
        self.assertFalse(thread_cleanup.has_pending_threads())


if __name__ == "__main__":
    unittest.main()


class AvailabilityCostTests(unittest.TestCase):
    """export_available runs on the GUI thread: it must not build the export's item list."""

    def test_it_stops_at_the_first_loaded_item(self):
        items = [{"id": i, "name": f"Title {i}"} for i in range(1000)]
        content = {"vod": {"contents": {"1": items}}}
        with mock.patch("services.exporter.collect_items", side_effect=AssertionError):
            self.assertIsNone(
                export_available(ExportKind.CACHED, {"type": "XTREAM"}, "movies", content)
            )
            self.assertIsNone(
                export_available(ExportKind.ALL_LIVE, {"type": "STB"}, "movies", {"itv": [{}]})
            )
            self.assertEqual(
                export_available(ExportKind.CACHED, {"type": "XTREAM"}, "series", content),
                MSG_NOTHING_LOADED,
            )
