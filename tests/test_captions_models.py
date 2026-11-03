"""AI captions' downloads, files and tier choice (services/captions/models.py, download.py,
tier.py), against a local HTTP server and temporary folders only."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
import hashlib
from http.server import BaseHTTPRequestHandler
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sys
import tarfile
import tempfile
import threading
import time
from typing import Any, Callable, Optional
import unittest
from unittest import mock
import zipfile

from PySide6.QtWidgets import QApplication

from services.captions import download, models, tier
from services.captions.download import CANCELLED, CaptionsDownload
from services.captions.models import Download, RemoteFile, Tier
from services.live.server import ThreadingLoopbackHTTPServer
from services.vlc_profile import DEFAULT_USER_AGENT


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def blob(size: int, seed: int = 1) -> bytes:
    """Bytes that differ along the file (a resumed file in the wrong place shows)."""
    out = bytearray()
    counter = 0
    while len(out) < size:
        out += hashlib.sha256(f"{seed}:{counter}".encode()).digest()
        counter += 1
    return bytes(out[:size])


class Route:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.cut_first: Optional[int] = None  # the first answer stops after this many bytes
        self.ignore_range = False
        self.pace_s = 0.0  # a pause between 64 KB pieces


class FileServer:
    """Serves files with Range answers, and records what was asked."""

    def __init__(self) -> None:
        self.routes: dict[str, Route] = {}
        self.requests: list[tuple[str, dict[str, str]]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: Any) -> None:
                pass

            def do_GET(self) -> None:
                owner.requests.append((self.path, dict(self.headers)))
                route = owner.routes.get(self.path)
                if route is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                owner.answer(self, route)

        self.server = ThreadingLoopbackHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self.thread.start()

    @staticmethod
    def answer(handler: BaseHTTPRequestHandler, route: Route) -> None:
        body = route.body
        start = 0
        asked = handler.headers.get("Range", "")
        if asked.startswith("bytes=") and not route.ignore_range:
            start = int(asked[6:].split("-", 1)[0])
            if start >= len(body):
                handler.send_response(416)
                handler.send_header("Content-Range", f"bytes */{len(body)}")
                handler.send_header("Content-Length", "0")
                handler.end_headers()
                return
            handler.send_response(206)
            handler.send_header("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
        else:
            handler.send_response(200)
        handler.send_header("Content-Length", str(len(body) - start))
        handler.send_header("Accept-Ranges", "bytes")
        handler.end_headers()
        rest = body[start:]
        if route.cut_first is not None:  # then the connection closes
            rest = rest[: route.cut_first]
            route.cut_first = None
            handler.close_connection = True
        try:
            for at in range(0, len(rest), 65536):
                handler.wfile.write(rest[at : at + 65536])
                if route.pace_s:
                    handler.wfile.flush()
                    time.sleep(route.pace_s)
        except OSError:  # the client went away (a cancel)
            handler.close_connection = True

    def add(self, path: str, body: bytes) -> Route:
        self.routes[path] = Route(body)
        return self.routes[path]

    def ranges(self, path: str) -> list[Optional[str]]:
        return [headers.get("Range") for asked, headers in self.requests if asked == path]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)


def tar_bz2(files: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:bz2") as tar:
        folders = sorted({str(PurePosixPath(name).parent) for name in files})
        for folder in folders:
            info = tarfile.TarInfo(folder)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            tar.addfile(info)
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("top/escape")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../outside"
        tar.addfile(link)
    return out.getvalue()


def wheel(files: dict[str, tuple[bytes, int]]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, (data, mode) in files.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (0o100000 | mode) << 16
            zf.writestr(info, data)
    return out.getvalue()


class _Recorder:
    def __init__(self, job: CaptionsDownload) -> None:
        self.progress: list[tuple[int, int]] = []
        self.preparing = 0
        self.finished = 0
        self.failed: list[str] = []
        job.progress.connect(lambda done, total: self.progress.append((done, total)))
        job.preparing.connect(self._prepared)
        job.finished.connect(self._finished)
        job.failed.connect(self.failed.append)

    def _prepared(self) -> None:
        self.preparing += 1

    def _finished(self) -> None:
        self.finished += 1

    def ended(self) -> bool:
        return bool(self.finished or self.failed)


class DownloadTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.server = FileServer()
        self.addCleanup(self.server.close)
        folder = tempfile.TemporaryDirectory(prefix="qitv-captions-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name) / "ai-captions"
        patch = mock.patch.object(download, "RETRY_WAIT_S", (0.0,))
        patch.start()
        self.addCleanup(patch.stop)

    def wait_until(self, done: Callable[[], bool], timeout: float = 20.0) -> None:
        deadline = time.monotonic() + timeout
        while not done():
            if time.monotonic() > deadline:
                self.fail("timed out")
            self.app.processEvents()
            time.sleep(0.005)
        for _ in range(3):
            self.app.processEvents()

    def remote(self, path: str, body: bytes, **fields: Any) -> RemoteFile:
        self.server.add("/" + path, body)
        return RemoteFile(f"{self.server.url}/{path}", path, len(body), sha(body), **fields)

    def run_plan(
        self, downloads: list[Download], fails: bool = False
    ) -> tuple[CaptionsDownload, _Recorder]:
        """Download `downloads`; `fails`: the failure is logged as a warning."""
        job = CaptionsDownload(root=self.root, plan=lambda _tier: downloads)
        seen = _Recorder(job)
        with self.assertLogs("services.captions", "WARNING") if fails else nullcontext():
            self.assertTrue(job.start("small"))
            self.wait_until(seen.ended)
        self.assertFalse(job.running())
        self.assertEqual(bool(seen.failed), fails)
        return job, seen


class TransferTests(DownloadTestCase):
    def test_a_dropped_transfer_resumes_with_a_range(self):
        body = blob(700_000)
        file = self.remote("model.onnx", body)
        self.server.routes["/model.onnx"].cut_first = 300_000
        _job, seen = self.run_plan([Download(self.root, file)])
        self.assertEqual(seen.failed, [])
        self.assertEqual(seen.finished, 1)
        self.assertEqual((self.root / "model.onnx").read_bytes(), body)
        self.assertFalse((self.root / "model.onnx.part").exists())
        # From what was written whole (requests drops a piece cut short: 256 KB here).
        first, again = self.server.ranges("/model.onnx")
        self.assertIsNone(first)
        assert again is not None
        resumed_at = int(again.removeprefix("bytes=").removesuffix("-"))
        self.assertTrue(0 < resumed_at <= 300_000, again)
        self.assertEqual(models.read_verified(self.root), {"model.onnx": sha(body)})
        self.assertEqual(seen.progress[-1], (len(body), len(body)))

    def test_a_part_left_by_an_earlier_run_resumes(self):
        body = blob(200_000, seed=2)
        file = self.remote("vad.onnx", body)
        self.root.mkdir(parents=True)
        (self.root / "vad.onnx.part").write_bytes(body[:12345])
        _job, seen = self.run_plan([Download(self.root, file)])
        self.assertEqual(seen.finished, 1)
        self.assertEqual(self.server.ranges("/vad.onnx"), ["bytes=12345-"])
        self.assertEqual((self.root / "vad.onnx").read_bytes(), body)

    def test_a_server_that_ignores_the_range_sends_it_all_again(self):
        body = blob(150_000, seed=3)
        file = self.remote("a.bin", body)
        self.server.routes["/a.bin"].ignore_range = True
        self.root.mkdir(parents=True)
        (self.root / "a.bin.part").write_bytes(body[:5000])
        _job, seen = self.run_plan([Download(self.root, file)])
        self.assertEqual(seen.finished, 1)
        self.assertEqual((self.root / "a.bin").read_bytes(), body)

    def test_a_sha256_mismatch_fails_and_removes_the_file(self):
        body = blob(100_000, seed=4)
        file = replace(self.remote("bad.onnx", body), sha256=sha(b"something else"))
        _job, seen = self.run_plan([Download(self.root, file)], fails=True)
        self.assertEqual(seen.finished, 0)
        self.assertEqual(len(seen.failed), 1)
        self.assertIn("sha256", seen.failed[0])
        self.assertNotEqual(seen.failed[0], CANCELLED)
        self.assertFalse((self.root / "bad.onnx").exists())
        self.assertFalse((self.root / "bad.onnx.part").exists())
        self.assertEqual(models.read_verified(self.root), {})

    def test_a_file_larger_than_pinned_fails_and_is_removed(self):
        body = blob(90_000, seed=5)
        file = replace(self.remote("long.bin", body), size=80_000)
        _job, seen = self.run_plan([Download(self.root, file)], fails=True)
        self.assertEqual(len(seen.failed), 1)
        self.assertFalse((self.root / "long.bin.part").exists())

    def test_a_missing_file_fails(self):
        file = RemoteFile(f"{self.server.url}/gone.onnx", "gone.onnx", 10, sha(b"x"))
        _job, seen = self.run_plan([Download(self.root, file)], fails=True)
        self.assertEqual(len(seen.failed), 1)
        self.assertIn("404", seen.failed[0])

    def test_cancel_keeps_the_part_and_the_next_start_resumes_it(self):
        body = blob(4_000_000, seed=6)
        file = self.remote("big.onnx", body)
        route = self.server.routes["/big.onnx"]
        route.pace_s = 0.01
        job = CaptionsDownload(root=self.root, plan=lambda _tier: [Download(self.root, file)])
        seen = _Recorder(job)
        self.assertTrue(job.start("small"))
        self.assertFalse(job.start("small"))  # one download at a time
        self.wait_until(lambda: bool(seen.progress) and seen.progress[-1][0] >= 600_000)
        job.cancel()
        self.wait_until(seen.ended)
        self.assertEqual(seen.failed, [CANCELLED])
        part = self.root / "big.onnx.part"
        kept = part.stat().st_size
        self.assertTrue(0 < kept < len(body))
        self.assertEqual(part.read_bytes(), body[:kept])
        self.assertFalse((self.root / "big.onnx").exists())

        route.pace_s = 0.0
        self.assertTrue(job.start("small"))
        self.wait_until(lambda: seen.finished == 1)
        self.assertEqual(self.server.ranges("/big.onnx")[-1], f"bytes={kept}-")
        self.assertEqual((self.root / "big.onnx").read_bytes(), body)

    def test_progress_comes_at_most_every_half_megabyte_and_ends_at_the_total(self):
        first = self.remote("one.bin", blob(1_500_000, seed=7))
        second = self.remote("two.bin", blob(1_100_000, seed=8))
        _job, seen = self.run_plan([Download(self.root, first), Download(self.root, second)])
        total = first.size + second.size
        self.assertEqual(seen.finished, 1)
        self.assertTrue(all(reported_total == total for _done, reported_total in seen.progress))
        self.assertEqual(seen.progress[-1], (total, total))
        self.assertLessEqual(len(seen.progress), total // download.PROGRESS_STEP + 4)
        dones = [done for done, _total in seen.progress]
        self.assertEqual(dones, sorted(dones))

    def test_a_size_past_two_gigabytes_reaches_the_gui(self):
        job = CaptionsDownload(root=self.root)
        seen = _Recorder(job)
        job.progress.emit(2_500_000_000, 2_404_230_105 + 30_000_000)
        self.assertEqual(seen.progress, [(2_500_000_000, 2_434_230_105)])

    def test_the_requests_carry_the_app_s_generic_user_agent_only(self):
        file = self.remote("ua.bin", blob(1000))
        self.run_plan([Download(self.root, file)])
        headers = self.server.requests[0][1]
        self.assertEqual(headers.get("User-Agent"), DEFAULT_USER_AGENT)
        self.assertFalse({"Cookie", "Authorization", "Referer"} & set(headers))

    def test_not_enough_disk_space_fails_before_downloading(self):
        file = self.remote("huge.bin", blob(1000))
        usage = mock.Mock(free=1000)
        with mock.patch.object(download.shutil, "disk_usage", return_value=usage):
            _job, seen = self.run_plan([Download(self.root, file)], fails=True)
        self.assertEqual(len(seen.failed), 1)
        self.assertEqual(self.server.requests, [])


class ExtractionTests(DownloadTestCase):
    def test_a_tar_bz2_unpacks_without_its_top_folder_and_samples(self):
        files = {
            "top/conv_frontend.onnx": blob(3000, seed=9),
            "top/tokenizer/vocab.json": b'{"a": 1}',
            "top/test_wavs/sample.wav": blob(500, seed=10),
        }
        archive = self.remote("model.tar.bz2", tar_bz2(files), extract=models.EXTRACT_TAR_BZ2)
        folder = self.root / "qwen3-asr-0.6b"
        contents = (("conv_frontend.onnx", 3000), ("tokenizer/vocab.json", 8))
        _job, seen = self.run_plan([Download(folder, archive, contents)])
        self.assertEqual(seen.failed, [])
        self.assertEqual(seen.preparing, 1)
        self.assertEqual(
            (folder / "conv_frontend.onnx").read_bytes(), files["top/conv_frontend.onnx"]
        )
        self.assertEqual((folder / "tokenizer" / "vocab.json").read_bytes(), b'{"a": 1}')
        self.assertFalse((folder / "test_wavs").exists())
        self.assertFalse((folder / "escape").exists())  # a link is not unpacked
        self.assertFalse((folder / "model.tar.bz2").exists())  # the archive goes once unpacked
        self.assertEqual(
            models.read_verified(self.root), {"qwen3-asr-0.6b/model.tar.bz2": archive.sha256}
        )

    def test_an_archive_without_the_expected_files_fails(self):
        archive = self.remote(
            "other.tar.bz2", tar_bz2({"top/x.onnx": b"x"}), extract=models.EXTRACT_TAR_BZ2
        )
        folder = self.root / "tier"
        _job, seen = self.run_plan(
            [Download(folder, archive, (("encoder.int8.onnx", 5),))], fails=True
        )
        self.assertEqual(len(seen.failed), 1)
        self.assertEqual(models.read_verified(self.root), {})

    def test_a_wheel_unpacks_as_pip_lays_out_site_packages(self):
        data = wheel(
            {
                "sherpa_onnx/__init__.py": (b"VALUE = 1\n", 0o644),
                "sherpa_onnx/lib/_native.so": (b"\x7fELF", 0o755),
                "sherpa_onnx-2.0.1.dist-info/RECORD": (b"", 0o644),
                "sherpa_onnx_core-2.0.1.data/data/Scripts/native.dll": (b"dll", 0o644),
                "sherpa_onnx_core-2.0.1.data/platlib/extra.py": (b"", 0o644),
                "../escape.txt": (b"no", 0o644),
            }
        )
        runtime = self.root / "runtime-2.0.1-cp314"
        file = self.remote("sherpa.whl", data, extract=models.EXTRACT_WHEEL)
        _job, seen = self.run_plan([Download(runtime, file, (("sherpa_onnx/__init__.py", None),))])
        self.assertEqual(seen.failed, [])
        self.assertEqual((runtime / "sherpa_onnx" / "__init__.py").read_bytes(), b"VALUE = 1\n")
        self.assertTrue((runtime / "sherpa_onnx" / "lib" / "_native.so").is_file())
        self.assertTrue((runtime / "sherpa_onnx-2.0.1.dist-info" / "RECORD").is_file())
        self.assertTrue((runtime / "extra.py").is_file())
        self.assertFalse((runtime / "sherpa_onnx_core-2.0.1.data").exists())
        self.assertFalse((self.root / "escape.txt").exists())
        self.assertFalse((runtime / "sherpa.whl").exists())
        if sys.platform != "win32":
            mode = (runtime / "sherpa_onnx" / "lib" / "_native.so").stat().st_mode
            self.assertTrue(mode & 0o100)

    def test_an_archive_that_arrived_before_is_checked_and_unpacked_without_a_request(self):
        data = wheel({"pkg/__init__.py": (b"", 0o644)})
        file = self.remote("pkg.whl", data, extract=models.EXTRACT_WHEEL)
        runtime = self.root / "runtime"
        runtime.mkdir(parents=True)
        (runtime / "pkg.whl").write_bytes(data)
        _job, seen = self.run_plan([Download(runtime, file, (("pkg/__init__.py", None),))])
        self.assertEqual(seen.failed, [])
        self.assertEqual(self.server.requests, [])
        self.assertTrue((runtime / "pkg" / "__init__.py").is_file())


class InstalledTierTests(DownloadTestCase):
    """A whole tier through models.missing(): runtime wheels, the VAD, the model."""

    def setUp(self) -> None:
        super().setUp()
        self.model = {
            "top/conv_frontend.onnx": blob(1000, seed=11),
            "top/encoder.int8.onnx": blob(2000, seed=12),
            "top/decoder.int8.onnx": blob(3000, seed=13),
            "top/tokenizer/vocab.json": b"{}",
        }
        archive = self.remote("model.tar.bz2", tar_bz2(self.model), extract=models.EXTRACT_TAR_BZ2)
        contents = tuple(
            (name.removeprefix("top/"), len(data)) for name, data in self.model.items()
        )
        tiny = Tier("small", "qwen3-asr-0.6b", (archive,), contents, "0.6B")
        main = self.remote(
            "main.whl",
            wheel({"sherpa_onnx/__init__.py": (b"", 0o644)}),
            extract=models.EXTRACT_WHEEL,
        )
        core = self.remote(
            "core.whl",
            wheel({"sherpa_onnx/lib/libcore.so": (b"lib", 0o755)}),
            extract=models.EXTRACT_WHEEL,
        )
        vad = self.remote("silero_vad.onnx", blob(400, seed=14))
        for patch in (
            mock.patch.dict(models.TIERS, {"small": tiny}),
            mock.patch.object(models, "VAD", vad),
            mock.patch.object(models, "runtime_files", return_value=(main, core)),
            mock.patch.object(models, "sherpa_importable", return_value=False),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def test_the_tier_downloads_whole_and_is_installed(self):
        self.assertFalse(models.installed("small", self.root))
        self.assertEqual(
            models.download_size("small", self.root),
            sum(d.file.size for d in models.needed("small", self.root)),
        )
        job = CaptionsDownload(root=self.root)
        seen = _Recorder(job)
        job.start("small")
        self.wait_until(seen.ended)
        self.assertEqual(seen.failed, [])
        self.assertTrue(models.installed("small", self.root))
        self.assertEqual(models.download_size("small", self.root), 0)
        paths = models.model_paths("small", self.root)
        self.assertEqual(paths.encoder.read_bytes(), self.model["top/encoder.int8.onnx"])
        self.assertTrue(paths.tokenizer.is_dir())
        self.assertEqual(paths.vad, self.root / "silero_vad.onnx")
        self.assertTrue(paths.vad.is_file())
        runtime = models.runtime_dir(self.root)
        assert runtime is not None
        self.assertTrue((runtime / "sherpa_onnx" / "lib" / "libcore.so").is_file())

        # Nothing left to fetch: a second start asks the server nothing.
        asked = len(self.server.requests)
        job.start("small")
        self.wait_until(lambda: seen.finished == 2)
        self.assertEqual(len(self.server.requests), asked)

    def test_what_is_on_disk_already_is_not_counted_again(self):
        # 2.9: a cancel while unpacking left the whole archive; the question still said 879 MB.
        downloads = models.needed("small", self.root)
        whole, part = downloads[0], downloads[1]
        whole.target.parent.mkdir(parents=True, exist_ok=True)
        whole.target.write_bytes(b"x" * whole.file.size)
        part.target.parent.mkdir(parents=True, exist_ok=True)
        part.target.with_name(part.target.name + ".part").write_bytes(b"x" * 3)
        everything = sum(d.file.size for d in downloads)
        self.assertEqual(models.download_size("small", self.root), everything - whole.file.size - 3)
        self.assertFalse(models.installed("small", self.root))  # still to be unpacked

    def test_installed_needs_every_file_at_its_size_and_its_record(self):
        job = CaptionsDownload(root=self.root)
        seen = _Recorder(job)
        job.start("small")
        self.wait_until(seen.ended)
        self.assertTrue(models.installed("small", self.root))

        encoder = models.model_paths("small", self.root).encoder
        encoder.write_bytes(b"cut")
        self.assertFalse(models.installed("small", self.root))
        self.assertEqual(
            [d.file.path for d in models.missing("small", self.root)], ["model.tar.bz2"]
        )
        encoder.write_bytes(self.model["top/encoder.int8.onnx"])
        self.assertTrue(models.installed("small", self.root))

        record = self.root / models.VERIFIED_FILE
        kept = json.loads(record.read_text())
        del kept["files"]["silero_vad.onnx"]
        record.write_text(json.dumps(kept))
        self.assertFalse(models.installed("small", self.root))
        self.assertEqual(
            [d.file.path for d in models.missing("small", self.root)], ["silero_vad.onnx"]
        )
        record.write_text("{not json")
        with self.assertLogs("services.captions", "WARNING"):
            self.assertFalse(models.installed("small", self.root))

    def test_space_used_and_delete_all(self):
        self.assertEqual(models.space_used(self.root), 0)
        job = CaptionsDownload(root=self.root)
        seen = _Recorder(job)
        job.start("small")
        self.wait_until(seen.ended)
        (self.root / "qwen3-asr-1.7b").mkdir()
        (self.root / "qwen3-asr-1.7b" / "decoder.int8.onnx.part").write_bytes(b"x" * 777)
        used = models.space_used(self.root)
        on_disk = sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())
        self.assertEqual(used, on_disk)
        self.assertGreater(used, sum(len(data) for data in self.model.values()) + 777)

        models.delete_all(self.root)
        self.assertFalse(self.root.exists())
        self.assertEqual(models.space_used(self.root), 0)
        self.assertFalse(models.installed("small", self.root))
        models.delete_all(self.root)  # nothing there: nothing happens

    def test_a_python_with_sherpa_skips_the_runtime(self):
        with mock.patch.object(models, "sherpa_importable", return_value=True):
            self.assertIsNone(models.runtime_dir(self.root))
            names = [d.file.path for d in models.needed("small", self.root)]
        self.assertEqual(names, ["silero_vad.onnx", "model.tar.bz2"])

    def test_without_a_runtime_for_this_computer_nothing_is_installed(self):
        with mock.patch.object(models, "runtime_files", side_effect=models.Unsupported("x")):
            self.assertFalse(models.installed("small", self.root))
            self.assertFalse(models.available())
            with self.assertRaises(models.Unsupported):
                models.missing("small", self.root)


class PinnedFilesTests(unittest.TestCase):
    def test_runtime_wheels_for_each_platform(self):
        cases = {
            ("darwin", "x86_64"): "macosx_10_15_x86_64",
            ("darwin", "arm64"): "macosx_11_0_arm64",
            ("win32", "AMD64"): "win_amd64",
            ("linux", "x86_64"): "manylinux2014_x86_64",
        }
        for (system, machine), tag in cases.items():
            with self.subTest(system=system, machine=machine):
                main, core = models.runtime_files(system, machine, "cp314")
                self.assertTrue(main.path.startswith("sherpa_onnx-2.0.1-cp314-cp314-"))
                self.assertTrue(core.path.startswith("sherpa_onnx_core-2.0.1-py3-none-"))
                for file in (main, core):
                    self.assertIn(tag, file.path)
                    self.assertEqual(file.extract, models.EXTRACT_WHEEL)
                    self.assertTrue(file.url.startswith("https://files.pythonhosted.org/"))
                    self.assertTrue(file.url.endswith("/" + file.path))
                    self.assertEqual(len(file.sha256), 64)
                    self.assertGreater(file.size, 1_000_000)

    def test_other_platforms_and_pythons_are_unsupported(self):
        for system, machine, python in (
            ("linux", "aarch64", "cp314"),
            ("win32", "ARM64", "cp314"),
            ("freebsd14", "amd64", "cp314"),
            ("darwin", "x86_64", "cp315"),
        ):
            with self.subTest(system=system, machine=machine, python=python):
                with self.assertRaises(models.Unsupported):
                    models.runtime_files(system, machine, python)

    def test_the_tiers_are_pinned_and_laid_out_alike(self):
        small, large = models.TIERS["small"], models.TIERS["large"]
        self.assertEqual(small.download_bytes, 878702423)
        self.assertEqual(small.files[0].extract, models.EXTRACT_TAR_BZ2)
        self.assertEqual(large.download_bytes, 2404230105)
        self.assertEqual(models.VAD.size, 643854)
        for info in (small, large):
            names = {path for path, _size in info.contents}
            self.assertTrue(
                {"conv_frontend.onnx", "encoder.int8.onnx", "decoder.int8.onnx"} <= names
            )
            self.assertTrue(all(name.startswith("tokenizer/") for name in names if "/" in name))
            for file in info.files:
                self.assertRegex(file.sha256, "^[0-9a-f]{64}$")
                self.assertTrue(file.url.startswith("https://"))
        root = Path("/captions")
        for name in ("small", "large"):
            paths = models.model_paths(name, root)
            folder = root / models.TIERS[name].folder
            self.assertEqual(paths.encoder, folder / "encoder.int8.onnx")
            self.assertEqual(paths.decoder, folder / "decoder.int8.onnx")
            self.assertEqual(paths.conv_frontend, folder / "conv_frontend.onnx")
            self.assertEqual(paths.tokenizer, folder / "tokenizer")
            self.assertEqual(paths.vad, root / "silero_vad.onnx")

    def test_the_folder_is_in_the_config_folder(self):
        with tempfile.TemporaryDirectory() as config:
            with mock.patch.dict(os.environ, {"QITV_CONFIG_DIR": config}):
                os.environ.pop("QITV_CAPTIONS_DIR", None)  # the tests' own folder (run_tests)
                self.assertEqual(models.captions_root(), Path(config) / "ai-captions")
            self.assertEqual(os.listdir(config), [])  # finding it makes nothing

    def test_the_runtime_folder_names_its_version_and_python(self):
        self.assertEqual(models.runtime_folder_name("cp314"), "runtime-2.0.1-cp314")


class TierChoiceTests(unittest.TestCase):
    GB = 1024**3

    def setUp(self) -> None:
        # Every value is given: no benchmark runs, no memory is read.
        for name in ("cpu_score", "machine_score", "total_ram_bytes", "is_arm_mac"):
            patch = mock.patch.object(tier, name, side_effect=AssertionError(name))
            patch.start()
            self.addCleanup(patch.stop)

    def choose(self, override: str = "auto", **machine: Any) -> str:
        return tier.choose_tier(override, **machine)

    def test_a_fast_machine_with_the_memory_gets_the_large_tier(self):
        self.assertEqual(self.choose(score=1.0, ram=16 * self.GB, arm_mac=False), "large")
        self.assertEqual(self.choose(score=0.9, ram=16 * self.GB, arm_mac=False), "large")
        self.assertEqual(self.choose(score=0.85, ram=64 * self.GB, arm_mac=False), "small")
        self.assertEqual(self.choose(score=4.0, ram=8 * self.GB, arm_mac=False), "small")

    def test_an_arm_mac_counts_as_fast_but_needs_the_memory_too(self):
        self.assertEqual(self.choose(score=0.5, ram=16 * self.GB, arm_mac=True), "large")
        self.assertEqual(self.choose(score=0.5, ram=8 * self.GB, arm_mac=True), "small")

    def test_a_12_gb_machine_counts_though_it_reports_a_little_less(self):
        reported = int(11.7 * self.GB)
        self.assertEqual(self.choose(score=3.0, ram=reported, arm_mac=False), "large")
        self.assertEqual(self.choose(score=3.0, ram=0, arm_mac=False), "small")  # unknown

    def test_the_viewer_s_choice_wins(self):
        self.assertEqual(self.choose("small", score=9.0, ram=64 * self.GB, arm_mac=True), "small")
        self.assertEqual(self.choose("large", score=0.1, ram=4 * self.GB, arm_mac=False), "large")


class MachineTests(unittest.TestCase):
    def test_the_score_is_relative_to_the_reference_machine(self):
        tier.cpu_score.cache_clear()
        self.addCleanup(tier.cpu_score.cache_clear)
        reference = tier.REFERENCE_ROUND_S
        with (
            mock.patch.object(tier.os, "cpu_count", return_value=8),
            mock.patch.object(tier, "single_thread_round_s", return_value=reference),
        ):
            self.assertAlmostEqual(tier.cpu_score(), 1.0)
        tier.cpu_score.cache_clear()
        with (
            mock.patch.object(tier.os, "cpu_count", return_value=16),
            mock.patch.object(tier, "single_thread_round_s", return_value=reference / 2),
        ):
            self.assertAlmostEqual(tier.cpu_score(), 3.0)  # the model takes 6 of the 16
        tier.cpu_score.cache_clear()
        with (
            mock.patch.object(tier.os, "cpu_count", return_value=24),
            mock.patch.object(tier, "single_thread_round_s", return_value=reference / 0.35),
        ):
            # Many slow threads: the model gets 6 of them, about half the reference's speed.
            self.assertAlmostEqual(tier.cpu_score(), 0.525)
        self.assertEqual([tier.model_threads(n) for n in (2, 4, 8, 12, 64)], [2, 2, 4, 6, 6])

    def test_the_best_score_measured_on_this_computer_is_kept(self):
        # 2.9: measured as a film opened, the reference scored 0.81 (1.0 quiet): the small model.
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        env = mock.patch.dict(os.environ, {"QITV_CAPTIONS_DIR": folder})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(tier.machine_score.cache_clear)

        def run(measured):
            tier.machine_score.cache_clear()
            with mock.patch.object(tier, "cpu_score", return_value=measured):
                return tier.machine_score()

        self.assertEqual(run(1.0), 1.0)
        self.assertEqual(run(0.81), 1.0)  # a busy moment: the quiet one stands
        self.assertEqual(run(1.2), 1.2)
        self.assertEqual(run(0.9), 1.2)
        with mock.patch.object(tier, "_this_computer", return_value="another"):
            self.assertEqual(run(0.5), 0.5)  # a profile copied elsewhere measures anew
        self.assertEqual(models.space_used(Path(folder)), 0)  # nothing downloaded to tell
        with open(os.path.join(folder, tier.SPEED_FILE), "w") as f:
            f.write("{not json")
        self.assertEqual(run(0.7), 0.7)

    def test_linux_memory_is_read_from_meminfo(self):
        with tempfile.NamedTemporaryFile("w", suffix=".meminfo", delete=False) as f:
            f.write("MemFree:  100 kB\nMemTotal:       16303848 kB\n")
        self.addCleanup(os.unlink, f.name)
        self.assertEqual(tier._linux_ram(f.name), 16303848 * 1024)

    def test_this_machine_s_memory_is_known(self):
        self.assertGreater(tier.total_ram_bytes(), 1024**3)


if __name__ == "__main__":
    unittest.main()
