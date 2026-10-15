"""Smoke test of the TV window with real libVLC on the local fixture provider (spec §11).

    uv run python scripts/smoke_tv.py --out smoke-out [--seconds 6]

The app runs in a child process: an isolated QITV_CONFIG_DIR, the local two-channel M3U
fixture (tests/fixtures/local_provider.py), the real TVWindow and libVLC. The script drives it:
drawer → play → zap, then the overlay host against the video surface after a move and resize,
in and out of fullscreen and after a minimize, then a close and a clean exit.

Every check is written to <out>/smoke.json as {"name", "ok", "detail"}; any failed check makes
the exit code non-zero. Checks named "overlay: …" form the overlay gate (spec D4): a platform
that fails it gets the Qt-drawn frame fallback. Screenshots are the overlay host's grab()
composited over black (OS capture needs permissions), plus a libVLC snapshot of the video.
Nothing here touches the network: Python-level lookups and connections beyond this machine are
refused and recorded as a failure.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import faulthandler
import json
import logging
import os
from pathlib import Path
import platform
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Generator, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6 import __version__ as PYSIDE_VERSION  # noqa: E402
from PySide6.QtCore import QPoint, QRect, Qt, QTimer, qVersion  # noqa: E402
from PySide6.QtGui import QGuiApplication, QImage, QPainter  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
import qdarktheme  # noqa: E402

from config_manager import ConfigManager  # noqa: E402
from epg_manager import EpgManager  # noqa: E402
from image_manager import ImageManager  # noqa: E402
from provider_manager import ProviderManager  # noqa: E402
from services import playback  # noqa: E402
from services.playback import PlaybackSession, PlaybackState  # noqa: E402
from services.thread_cleanup import has_pending_threads  # noqa: E402
from tests.fixtures.local_provider import (  # noqa: E402
    channel_urls,
    seed_channels,
    write_local_provider,
)
from ui.tv_window import TVWindow  # noqa: E402

logger = logging.getLogger(__name__)

REPORT = "smoke.json"
SCREENSHOTS = ("01-drawer.png", "02-banner.png", "03-fullscreen.png")
VIDEO_SNAPSHOT = "02-video.png"
OVERLAY_PREFIX = "overlay: "
DEFAULT_SECONDS = 6.0  # the fixture clip's length
DRAWER_TIMEOUT_MS = 10_000
PLAY_TIMEOUT_MS = 15_000
MOVE_WAIT_MS = 100
STATE_WAIT_MS = 1_000  # after a fullscreen or minimize change
SETTLE_LIMIT_MS = 2_000  # more, while the OS still animates the window (the banner lasts 4 s)
STABLE_MS = 300
HIDE_TIMEOUT_MS = 3_000
POLL_MS = 50
EXIT_LIMIT_S = 10.0
APP_BUDGET_S = 120.0  # plus --seconds; a hung app is stopped after this


# ---- the report -------------------------------------------------------------------------------


class Report:
    """smoke.json, rewritten after every check so a crash still leaves what was seen."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = {"checks": []}

    def check(self, name: str, ok: bool, detail: Any = "") -> bool:
        self.data["checks"].append({"name": name, "ok": bool(ok), "detail": str(detail)})
        logger.log(
            logging.INFO if ok else logging.ERROR,
            "%s %s%s",
            "PASS" if ok else "FAIL",
            name,
            f": {detail}" if detail != "" else "",
        )
        self.save()
        return bool(ok)

    def save(self) -> None:
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)


def _summarize(data: dict[str, Any]) -> None:
    failed = [c["name"] for c in data["checks"] if not c["ok"]]
    overlay_failed = [name for name in failed if name.startswith(OVERLAY_PREFIX)]
    data["ok"] = not failed
    data["failed"] = failed
    data["overlay_gate"] = {"ok": not overlay_failed, "failed": overlay_failed}


# ---- the parent: runs the app and judges its exit ---------------------------------------------


def run_smoke(out: Path, seconds: float) -> int:
    out.mkdir(parents=True, exist_ok=True)
    for name in (REPORT, *SCREENSHOTS, VIDEO_SNAPSHOT):
        (out / name).unlink(missing_ok=True)
    budget = APP_BUDGET_S + seconds
    command = [
        sys.executable,
        "-X",
        "faulthandler",
        str(Path(__file__).resolve()),
        "--child",
        "--out",
        str(out),
        "--seconds",
        str(seconds),
    ]
    with tempfile.TemporaryDirectory(prefix="qitv-smoke-", ignore_cleanup_errors=True) as config:
        env = dict(os.environ, QITV_CONFIG_DIR=config)
        process = subprocess.Popen(command, cwd=ROOT, env=env)
        try:
            code: Optional[int] = process.wait(timeout=budget + 15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            code = None
        exited_at = time.time()

    report = Report(out / REPORT)
    try:
        report.data = json.loads((out / REPORT).read_text(encoding="utf-8"))
        report.data.setdefault("checks", [])
    except (OSError, ValueError) as e:
        report.check("the app writes its report", False, f"{e} (exit code {code})")
    report.check(
        "the app exits with code 0",
        code == 0,
        f"exit code {code}" if code is not None else f"stopped after {budget + 15:.0f} s",
    )
    close_at = report.data.get("close_requested_at")
    within = f"the app exits within {EXIT_LIMIT_S:.0f} s of the close"
    if not isinstance(close_at, (int, float)):
        report.check(within, False, "the window was never closed")
    elif code is None:
        report.check(within, False, "it never exited")
    else:
        elapsed = exited_at - close_at
        report.check(within, elapsed <= EXIT_LIMIT_S, f"{elapsed:.1f} s")
    _summarize(report.data)
    report.save()
    failed = report.data["failed"]
    logger.info(
        "Smoke %s: %d checks, %d failed%s; overlay gate %s. Report: %s",
        "passed" if not failed else "FAILED",
        len(report.data["checks"]),
        len(failed),
        f" ({'; '.join(failed)})" if failed else "",
        "passed" if report.data["overlay_gate"]["ok"] else "FAILED",
        out / REPORT,
    )
    return 0 if not failed else 1


# ---- the child: the app and its driver --------------------------------------------------------


def _is_local(host: Any) -> bool:
    if host is None:
        return True
    name = (host.decode() if isinstance(host, bytes) else str(host)).strip("[]").lower()
    return name in ("", "localhost", "::1") or name.startswith("127.")


def _refuse_network(attempts: list[str]) -> None:
    """Refuse Python-level name lookups and connections beyond this machine; record them."""
    real_connect = socket.socket.connect
    real_getaddrinfo = socket.getaddrinfo

    def refuse(target: str) -> None:
        attempts.append(target)
        raise OSError(f"the smoke test has no network access ({target})")

    def connect(sock: socket.socket, address: Any) -> None:
        inet = sock.family in (socket.AF_INET, socket.AF_INET6)
        if inet and not _is_local(address[0]):
            refuse(f"connect {address[0]}:{address[1]}")
        real_connect(sock, address)

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if not _is_local(host):
            refuse(f"lookup {host}")
        return real_getaddrinfo(host, *args, **kwargs)

    setattr(socket.socket, "connect", connect)
    setattr(socket, "getaddrinfo", getaddrinfo)


def _rect(rect: Any) -> list[int]:
    return [rect.x(), rect.y(), rect.width(), rect.height()]


def _cocoa_child_window_of(child: Any, parent: Any) -> bool:
    """[[child window] parentWindow] == [parent window]: macOS moves the overlay with the player."""
    objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc") or "libobjc.dylib")
    objc.sel_registerName.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]
    send = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(
        ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value or 0
    )

    def call(receiver: Optional[int], selector: bytes) -> Optional[int]:
        result: Optional[int] = send(receiver, objc.sel_registerName(selector))
        return result

    parent_window = call(int(parent.winId()), b"window")
    child_window = call(int(child.winId()), b"window")
    return bool(parent_window) and call(child_window, b"parentWindow") == parent_window


def run_app(out: Path, seconds: float) -> int:
    """Start the TV window over the fixture provider and drive it; returns app.exec()'s code."""
    faulthandler.dump_traceback_later(APP_BUDGET_S + seconds, exit=True)  # a hang shows where
    attempts: list[str] = []
    _refuse_network(attempts)
    report = Report(out / REPORT)
    report.data.update(seconds=seconds, screenshots={}, video={})

    config_dir = os.environ["QITV_CONFIG_DIR"]
    provider = write_local_provider(config_dir)
    app = QApplication([sys.argv[0]])
    config = ConfigManager()
    images = ImageManager(config, config.max_cache_image_size * 1024 * 1024)
    providers = ProviderManager(config)
    epg = EpgManager(config, providers)
    seed_channels(providers, provider)
    qdarktheme.setup_theme("dark", custom_colors={"primary": "#C96B43"})  # as main.py does

    screen = QGuiApplication.primaryScreen()
    vlc = playback.vlc
    report.data["environment"] = {
        "os": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "qt": qVersion(),
        "pyside": PYSIDE_VERSION,
        "qt_platform": QGuiApplication.platformName(),
        "libvlc": vlc.libvlc_get_version().decode() if vlc is not None else None,
        "screen": _rect(screen.geometry()) if screen else None,
        "device_pixel_ratio": screen.devicePixelRatio() if screen else None,
        "config_dir": config_dir,
    }
    report.save()
    logger.info("Environment: %s", report.data["environment"])

    window = TVWindow(app, config, providers, images, epg)
    driver = Driver(window, report, out, seconds)
    window.show()
    QTimer.singleShot(0, driver.start)
    code = app.exec()
    driver.after_exit(code, attempts)
    return code


class Driver:
    """Runs the scenario as a generator: each yield is the delay in ms before the next step."""

    def __init__(self, window: Any, report: Report, out: Path, seconds: float) -> None:
        self.window = window
        self.report = report
        self.out = out
        self.seconds = seconds
        self.finished = False
        self.problem = ""
        self._steps = self._scenario()

    # ---- stepping ---------------------------------------------------------------------------

    def start(self) -> None:
        self._run()

    def _run(self) -> None:
        try:
            delay = next(self._steps)
        except StopIteration:
            self.finished = True
            return
        except Exception as e:
            logger.exception("The smoke scenario stopped")
            self.problem = f"stopped: {e!r}"
            self._close()
            return
        QTimer.singleShot(delay, self._run)

    def _wait(self, predicate: Callable[[], bool], timeout_ms: int) -> Generator[int, None, bool]:
        deadline = time.monotonic() + timeout_ms / 1000
        while not predicate() and time.monotonic() < deadline:
            yield POLL_MS
        return bool(predicate())

    def _settle(self) -> Generator[int, None, None]:
        """Wait a second, then until the window stops changing (OS animations), at most 2 s."""
        yield STATE_WAIT_MS
        last, stable = self.window.geometry(), 0
        deadline = time.monotonic() + SETTLE_LIMIT_MS / 1000
        while stable < STABLE_MS and time.monotonic() < deadline:
            yield POLL_MS
            now = self.window.geometry()
            stable = stable + POLL_MS if now == last else 0
            last = now

    def _key(self, key: Any) -> None:
        QTest.keyClick(self.window, key)

    # ---- checks -----------------------------------------------------------------------------

    def _playing(self, url: str) -> Callable[[], bool]:
        session = self.window.session
        return lambda: session.url == url and session.state() is PlaybackState.PLAYING

    def _check_glued(self, when: str) -> None:
        host, surface = self.window.host, self.window.surface
        target = QRect(surface.mapToGlobal(QPoint(0, 0)), surface.size())
        self.report.check(
            f"{OVERLAY_PREFIX}host covers the video surface {when}",
            host.isVisible() and host.geometry() == target,
            f"host {_rect(host.geometry())} {'shown' if host.isVisible() else 'hidden'}, "
            f"surface {_rect(target)}, window {_rect(self.window.geometry())}",
        )
        if QGuiApplication.platformName() == "cocoa":
            self.report.check(
                f"{OVERLAY_PREFIX}host is a child window of the TV window {when}",
                _cocoa_child_window_of(host, self.window),
            )

    def _capture(self, name: str) -> None:
        """The overlay host's own rendering over black: no screen-capture permission needed."""
        pixmap = self.window.host.grab()
        image = QImage(pixmap.size(), QImage.Format.Format_ARGB32)
        image.setDevicePixelRatio(pixmap.devicePixelRatio())
        image.fill(0xFF000000)
        painter = QPainter(image)
        painter.drawPixmap(0, 0, pixmap)
        painter.end()
        self.report.data["screenshots"][name] = image.save(str(self.out / name))
        self.report.save()

    def _video_facts(self) -> dict[str, Any]:
        player = getattr(self.window.session, "_player", None)  # absent without libVLC
        if player is None:
            return {"vout": 0}
        facts: dict[str, Any] = {"vout": player.has_vout()}
        try:
            facts["size"] = list(player.video_get_size(0))
        except Exception as e:
            facts["size"] = repr(e)
        path = self.out / VIDEO_SNAPSHOT
        facts["snapshot"] = (
            VIDEO_SNAPSHOT
            if player.video_take_snapshot(0, str(path), 0, 0) == 0 and path.exists()
            else None
        )
        return facts

    # ---- the scenario -----------------------------------------------------------------------

    def _scenario(self) -> Generator[int, None, None]:
        w, report = self.window, self.report
        drawer, banner, session = w.overlays.drawer, w.overlays.banner, w.session
        first, second = channel_urls()
        report.check("libVLC is available", isinstance(session, PlaybackSession))
        w.raise_()
        w.activateWindow()

        # 1. The drawer opens at startup (no last channel) and the host covers the video.
        opened = yield from self._wait(drawer.isVisible, DRAWER_TIMEOUT_MS)
        report.check("the drawer opens at startup", opened)
        yield 200  # macOS attaches the host as a child window on the next tick
        self._check_glued("with the drawer open")
        self._capture(SCREENSHOTS[0])

        # 2. Activate the first channel: Return enters the group, Return plays Channel 1.
        self._key(Qt.Key.Key_Return)
        yield from self._wait(lambda: getattr(drawer.highlighted(), "layer", None) == "item", 2000)
        self._key(Qt.Key.Key_Return)
        played = yield from self._wait(self._playing(first), PLAY_TIMEOUT_MS)
        report.check("Channel 1 plays", played, f"{session.state()} {session.url}")

        # 3. Down zaps to Channel 2: the banner shows it and the zap context is on it.
        self._key(Qt.Key.Key_Down)
        played = yield from self._wait(self._playing(second), PLAY_TIMEOUT_MS)
        report.check("Channel 2 plays after Down", played, f"{session.state()} {session.url}")
        info = banner.info
        report.check(
            "the banner shows after the zap",
            banner.isVisible() and info is not None and info.name == "Channel 2",
            info.name if info is not None else "no banner info",
        )
        current = w.zap.context.current() or {}
        report.check(
            "the zap context is on Channel 2",
            current.get("name") == "Channel 2" and current.get("cmd") == second,
            current.get("name"),
        )
        self._capture(SCREENSHOTS[1])

        # Watch it play for --seconds: the clock must move (the fixture clip lasts 6 s).
        if self.seconds > 0:
            start, furthest = session.time_ms(), 0
            deadline = time.monotonic() + self.seconds
            snapshot_at = time.monotonic() + min(1.0, self.seconds / 2)
            while time.monotonic() < deadline:
                yield POLL_MS
                furthest = max(furthest, session.time_ms())
                if not report.data["video"] and time.monotonic() >= snapshot_at:
                    report.data["video"] = self._video_facts()
                    report.save()
            needed = min(1000, int(self.seconds * 500))
            report.check(
                "Channel 2's playback clock advances",
                furthest - start >= needed,
                f"from {start} ms to {furthest} ms in {self.seconds:g} s",
            )

        # 4. Move by (60, 40) and resize to 1100×620 with the banner up.
        self._key(Qt.Key.Key_I)
        pos = w.pos()
        w.move(pos.x() + 60, pos.y() + 40)
        w.resize(1100, 620)
        yield MOVE_WAIT_MS
        self._check_glued("after a move and resize")

        # 5. Fullscreen and back, the banner up through each change.
        self._key(Qt.Key.Key_I)
        self._key(Qt.Key.Key_F)
        yield from self._settle()
        report.check("the window enters fullscreen", w.isFullScreen(), _rect(w.geometry()))
        self._check_glued("in fullscreen")
        self._capture(SCREENSHOTS[2])
        self._key(Qt.Key.Key_I)
        self._key(Qt.Key.Key_F)
        yield from self._settle()
        report.check("the window leaves fullscreen", not w.isFullScreen(), _rect(w.geometry()))
        self._check_glued("after leaving fullscreen")

        # Minimize and restore: nothing floats over other apps meanwhile (Review Focus 1).
        w.showMinimized()
        yield from self._wait(lambda: not w.host.isVisible(), HIDE_TIMEOUT_MS)
        yield STATE_WAIT_MS  # let the OS finish minimizing before restoring
        report.check(
            f"{OVERLAY_PREFIX}host hides while the window is minimized",
            w.isMinimized() and not w.host.isVisible(),
            f"minimized {w.isMinimized()}, host {'shown' if w.host.isVisible() else 'hidden'}",
        )
        self._key(Qt.Key.Key_I)  # the banner waits for the window
        w.showNormal()
        w.activateWindow()
        yield from self._settle()
        self._check_glued("after a restore")

        # 6. Close; the parent judges the exit code and time.
        self._close()

    def _close(self) -> None:
        self.report.data["close_requested_at"] = time.time()
        self.report.save()
        self.window.close()

    def after_exit(self, code: int, attempts: list[str]) -> None:
        report = self.report
        close_at = report.data.get("close_requested_at")
        report.data["exec_code"] = code
        if isinstance(close_at, (int, float)):
            report.data["close_seconds"] = round(time.time() - close_at, 2)
        report.check(
            "the smoke scenario runs to the end",
            self.finished,
            self.problem or ("" if self.finished else "the app quit before the end"),
        )
        report.check("the window closes", not self.window.isVisible())
        report.check(
            f"{OVERLAY_PREFIX}host is gone after the close", not self.window.host.isVisible()
        )
        others = [t.name for t in threading.enumerate() if t is not threading.main_thread()]
        report.check("no background threads are pending", not has_pending_threads(), others)
        shots = report.data["screenshots"]
        report.check(
            "the screenshots are saved", all(shots.get(name) for name in SCREENSHOTS), shots
        )
        report.check("nothing touches the network", not attempts, attempts)


# ---- entry point ------------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", required=True, help="directory for smoke.json and screenshots")
    parser.add_argument(
        "--seconds",
        type=float,
        default=DEFAULT_SECONDS,
        help="how long to watch Channel 2 play after the zap (default: %(default)s)",
    )
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    out = Path(args.out).resolve()
    seconds = max(0.0, args.seconds)
    return run_app(out, seconds) if args.child else run_smoke(out, seconds)


if __name__ == "__main__":
    sys.exit(main())
