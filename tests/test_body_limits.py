"""A hostile or broken server's answer is read up to a generous limit, never whole: a playlist,
a Player API or portal list, a logo, a guide (2.8.12 review)."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import tempfile
import threading
import unittest
from unittest import mock

import requests

from services import workers
from services.body_limits import TOO_LARGE_MESSAGE, TooLarge, capped, read_capped
from services.catalog import friendly_error

PLAYLIST = b"#EXTM3U\n" + b"".join(
    b'#EXTINF:-1 group-title="News",Ch %d\nhttp://h.test/%d.ts\n' % (n, n) for n in range(40)
)


class _Server(BaseHTTPRequestHandler):
    body = b""
    chunked = False  # no Content-Length: the size is found as it comes

    def log_message(self, *args):
        pass

    def do_GET(self):
        try:
            self._answer(self.server.answer(self.path))
        except BrokenPipeError, ConnectionResetError:
            pass  # the reader stopped at its limit and closed

    def _answer(self, body):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        if self.server.chunked:
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for start in range(0, len(body), 100):
                part = body[start : start + 100]
                self.wfile.write(f"{len(part):x}\r\n".encode() + part + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
        else:
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


class BodyLimitTests(unittest.TestCase):
    def serve(self, answer, chunked=False):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Server)
        server.answer, server.chunked = answer, chunked
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def run_worker(self, worker):
        got = {}
        worker.finished.connect(lambda result: got.setdefault("finished", result))
        worker.error.connect(lambda message: got.setdefault("error", message))
        worker.run()
        return got

    def test_a_body_is_read_up_to_its_limit(self):
        for chunked in (False, True):
            with self.subTest(chunked=chunked):
                base = self.serve(lambda path: PLAYLIST, chunked)
                response = requests.get(base, hooks=capped(len(PLAYLIST)), timeout=5)
                self.assertEqual(response.content, PLAYLIST)  # read as before
                with self.assertRaises(TooLarge), self.assertLogs("services.body_limits"):
                    requests.get(base, hooks=capped(len(PLAYLIST) - 1), timeout=5)

    def test_a_streamed_answer_is_read_capped_too(self):
        base = self.serve(lambda path: PLAYLIST, chunked=True)
        with requests.get(base, stream=True, timeout=5) as response:
            with self.assertRaises(TooLarge), self.assertLogs("services.body_limits"):
                read_capped(response, 100)

    def test_a_playlist_too_large_fails_with_a_clear_message(self):
        base = self.serve(lambda path: PLAYLIST, chunked=True)
        with (
            mock.patch.object(workers, "LIST_MAX", 200),
            self.assertLogs("services.body_limits", "WARNING"),
        ):
            got = self.run_worker(workers.M3ULoaderWorker(f"{base}/list.m3u"))
        self.assertEqual(got, {"error": TOO_LARGE_MESSAGE})
        self.assertEqual(friendly_error(got["error"]), TOO_LARGE_MESSAGE)  # shown as it is
        got = self.run_worker(workers.M3ULoaderWorker(f"{base}/list.m3u"))
        self.assertEqual(len(got["finished"]["contents"]), 40)  # within the limit: as before

    def test_a_local_playlist_too_large_fails_so_too(self):
        with tempfile.NamedTemporaryFile("wb", suffix=".m3u", delete=False) as file:
            file.write(PLAYLIST)
        self.addCleanup(os.remove, file.name)
        with mock.patch.object(workers, "LIST_MAX", 200):
            got = self.run_worker(workers.M3ULoaderWorker(file.name))
        self.assertEqual(got, {"error": TOO_LARGE_MESSAGE})

    def test_an_xtream_list_too_large_fails_with_a_clear_message(self):
        streams = [{"stream_id": n, "name": f"Ch {n}", "category_id": "1"} for n in range(50)]

        def answer(path):
            if "action=get_live_categories" in path:
                return json.dumps([{"category_id": "1", "category_name": "News"}]).encode()
            if "action=get_live_streams" in path:
                return json.dumps(streams).encode()
            return json.dumps({"user_info": {"auth": 1}, "server_info": {}}).encode()

        base = self.serve(answer)
        with (
            mock.patch.object(workers, "LIST_MAX", 1000),
            mock.patch.dict(workers._https_bases, clear=True),
            self.assertLogs("services.body_limits", "WARNING"),
        ):
            got = self.run_worker(workers.XtreamLoaderWorker(base, "u", "p", "itv"))
        self.assertEqual(got, {"error": TOO_LARGE_MESSAGE})


if __name__ == "__main__":
    unittest.main()
