"""Smoke test of the TV window with real libVLC on the local fixture provider (spec §11).

    uv run python scripts/smoke_tv.py --out smoke-out [--seconds 3]

The app runs in a child process: an isolated QITV_CONFIG_DIR, the local two-channel M3U
fixture (tests/fixtures/local_provider.py), the real TVWindow and libVLC. The script drives it:
drawer → play → zap → the transport over the whole host, then the overlay host against the
video surface after a move and resize, in and out of fullscreen and after a minimize, then a
close and a clean exit.

Every check is written to <out>/smoke.json as {"name", "ok", "detail"}; any failed check makes
the exit code non-zero. The overlay gate (spec D4) passes only when every expected "overlay: …"
check and every window change they rely on is present and passed; a platform that fails it
gets the Qt-drawn frame fallback. Screenshots are the overlay host's grab() composited over
black (OS capture needs permissions), plus a libVLC snapshot of the video. Nothing here touches
the network: Python-level lookups and connections beyond this machine are refused and recorded
as a failure.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import faulthandler
import functools
import glob
import json
import logging
import os
from pathlib import Path
import platform
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Generator, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6 import __version__ as PYSIDE_VERSION  # noqa: E402
from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, Qt, QTimer, qVersion  # noqa: E402
from PySide6.QtGui import QGuiApplication, QImage, QMouseEvent, QPainter  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
import qdarktheme  # noqa: E402

from config_manager import ConfigManager  # noqa: E402
from epg_manager import EpgManager  # noqa: E402
from image_manager import ImageManager  # noqa: E402
from provider_manager import ProviderManager  # noqa: E402
from services import playback, thread_cleanup  # noqa: E402
from services.playback import VLC_LOG_ENV, PlaybackSession, PlaybackState  # noqa: E402
from tests.fixtures.local_provider import (  # noqa: E402
    channel_urls,
    seed_channels,
    write_local_provider,
)
from ui.tv_window import TVWindow, fit_to_area, frame_margins  # noqa: E402

logger = logging.getLogger(__name__)

REPORT = "smoke.json"
SCREENSHOTS = ("01-drawer.png", "02-banner.png", "02-transport.png", "03-fullscreen.png")
TRANSPORT_SCREENSHOT = SCREENSHOTS[2]
TRANSPORT_TIMEOUT_MS = 2_000
VIDEO_SNAPSHOT = "02-video.png"
VLC_LOG = "libvlc.log"  # libVLC's verbose log of the run (local fixture URLs only)
DISPLAY_OPENED = 'using vout display module "'  # libVLC opened a video display (vout_display)
HANG_STACK = "hang-stack.txt"
DEFAULT_SECONDS = 3.0  # below the fixture clip's 6 s
MIN_SECONDS = 1.0
NATIVE_PLATFORMS = ("cocoa", "windows", "xcb")  # VLC draws into the surface (TVWindow binds it)
DRAWER_TIMEOUT_MS = 10_000
PLAY_TIMEOUT_MS = 15_000
MOVE_WAIT_MS = 100
STATE_WAIT_MS = 1_000  # at least, after a fullscreen change or a restore
STATE_TIMEOUT_MS = 5_000  # for the window to reach fullscreen, leave it, minimize or restore
SETTLE_LIMIT_MS = 2_000  # more, while the OS still animates the window
STABLE_MS = 300
QUIET_MS = 500  # no overlay shown or hidden for this long before a window change
QUIET_TIMEOUT_MS = 10_000
POLL_MS = 50
EXIT_LIMIT_S = 10.0
HANG_DUMP_S = 60.0  # plus --seconds: the child dumps its Python stacks
KILL_GRACE_S = 15.0  # then the parent records a native stack and stops it

OVERLAY_PREFIX = "overlay: "
GLUE_STEPS = (
    "with the drawer open",
    "after a move and resize",
    "in fullscreen",
    "after leaving fullscreen",
    "after a restore",
)
MINIMIZED_CHECK = f"{OVERLAY_PREFIX}host hides while the window is minimized"
CLOSED_CHECK = f"{OVERLAY_PREFIX}host is gone after the close"
# Without these window changes the overlay checks after them prove nothing.
WINDOW_CHANGES = (
    "the window moves and resizes",
    "the window enters fullscreen",
    "the window leaves fullscreen",
    "the window minimizes",
    "the window restores",
)
SCENARIO_CHECK = "the smoke scenario runs to the end"


def glue_check(when: str) -> str:
    return f"{OVERLAY_PREFIX}host covers the video surface {when}"


def child_window_check(when: str) -> str:
    return f"{OVERLAY_PREFIX}host is a child window of the TV window {when}"


def gate_checks(cocoa: bool) -> list[str]:
    """Every check the overlay gate needs, by name."""
    names = list(WINDOW_CHANGES)
    for when in GLUE_STEPS:
        names.append(glue_check(when))
        if cocoa:
            names.append(child_window_check(when))
    return names + [MINIMIZED_CHECK, CLOSED_CHECK]


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

    def step(self, name: str) -> None:
        """Mark where the scenario is, so a hang or crash says where it stopped."""
        self.data["step"] = name
        logger.info("Step: %s", name)
        self.save()

    def names(self) -> set[str]:
        return {c["name"] for c in self.data["checks"]}

    def save(self) -> None:
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)


def _summarize(data: dict[str, Any]) -> None:
    outcome = {c["name"]: c["ok"] for c in data["checks"]}
    failed = [name for name, ok in outcome.items() if not ok]
    qt_platform = (data.get("environment") or {}).get("qt_platform")
    if qt_platform is None:  # the app stopped before reporting it
        qt_platform = "cocoa" if sys.platform == "darwin" else ""
    expected = gate_checks(qt_platform == "cocoa")
    gate_failed = [n for n in failed if n in expected or n.startswith(OVERLAY_PREFIX)]
    missing = [n for n in expected if n not in outcome]
    data["overlay_gate"] = {
        "ok": not gate_failed and not missing,
        "failed": gate_failed,
        "missing": missing,
    }
    data["failed"] = failed
    data["ok"] = not failed and data["overlay_gate"]["ok"]


# ---- the parent: runs the app and judges its exit ---------------------------------------------


def _native_stack(pid: int, out: Path) -> Optional[str]:
    """A native stack of the hung app before it is stopped (macOS `sample`, Linux /proc)."""
    path = out / HANG_STACK
    try:
        if sys.platform == "darwin":
            subprocess.run(
                ["sample", str(pid), "3", "-file", str(path)], capture_output=True, timeout=60
            )
        elif sys.platform.startswith("linux"):
            stacks = []
            for task in sorted(glob.glob(f"/proc/{pid}/task/*")):
                try:
                    stacks.append(f"== {task}\n{Path(task, 'stack').read_text()}")
                except OSError:
                    pass  # kernel stacks are often root-only
            if stacks:
                path.write_text("\n".join(stacks), encoding="utf-8")
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("Could not record a native stack of the app: %s", e)
    return path.name if path.exists() else None


def run_smoke(out: Path, seconds: float) -> int:
    out.mkdir(parents=True, exist_ok=True)
    for name in (REPORT, *SCREENSHOTS, VIDEO_SNAPSHOT, HANG_STACK, VLC_LOG):
        (out / name).unlink(missing_ok=True)
    limit = HANG_DUMP_S + seconds + KILL_GRACE_S
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
    hang_stack = None
    with tempfile.TemporaryDirectory(prefix="qitv-smoke-", ignore_cleanup_errors=True) as config:
        env = dict(os.environ, QITV_CONFIG_DIR=config)
        env[VLC_LOG_ENV] = str(out / VLC_LOG)
        process = subprocess.Popen(command, cwd=ROOT, env=env)
        try:
            code: Optional[int] = process.wait(timeout=limit)
        except subprocess.TimeoutExpired:
            hang_stack = _native_stack(process.pid, out)
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
    report.data["hang_stack"] = hang_stack
    stopped = f"stopped after {limit:.0f} s" if code is None else f"exit code {code}"
    if SCENARIO_CHECK not in report.names():  # the app died or hung on the way
        report.check(SCENARIO_CHECK, False, f"{stopped} during: {report.data.get('step')}")
    report.check("the app exits with code 0", code == 0, stopped)
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
    gate = report.data["overlay_gate"]
    logger.info(
        "Smoke %s: %d checks, %d failed%s; overlay gate %s%s. Report: %s",
        "passed" if report.data["ok"] else "FAILED",
        len(report.data["checks"]),
        len(report.data["failed"]),
        f" ({'; '.join(report.data['failed'])})" if report.data["failed"] else "",
        "passed" if gate["ok"] else "FAILED",
        f" (missing: {'; '.join(gate['missing'])})" if gate["missing"] else "",
        out / REPORT,
    )
    return 0 if report.data["ok"] else 1


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


def _no_display_reason(log: Path) -> Optional[str]:
    """Why libVLC showed no video, when its log shows it never opened a video display.

    None when it did open one (no video then is a real failure) or when there is no log.
    """
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not text or DISPLAY_OPENED in text:
        return None
    lines = [
        line.strip()[:160]
        for line in text.splitlines()
        if any(word in line for word in ("vout", "video output", "display", "OpenGL", "CGL"))
        and ("error" in line or "fail" in line.lower() or "cannot" in line or "no " in line)
    ]
    return "libVLC never opened a video display: " + (" | ".join(lines[:5]) or "no error logged")


def _rect(rect: Any) -> list[int]:
    return [rect.x(), rect.y(), rect.width(), rect.height()]


@functools.cache
def _objc() -> Any:
    objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc") or "libobjc.dylib")
    objc.sel_registerName.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]
    return objc


def _send(receiver: Optional[int], selector: bytes, restype: Any = ctypes.c_void_p) -> Any:
    objc = _objc()
    send = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p)(
        ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value or 0
    )
    return send(receiver, objc.sel_registerName(selector))


def _ns_window(widget: Any) -> Optional[int]:
    result: Optional[int] = _send(int(widget.winId()), b"window")
    return result


def _cocoa_child_window_of(child: Any, parent: Any) -> bool:
    """[[child window] parentWindow] == [parent window]: macOS moves the overlay with the player."""
    parent_window = _ns_window(parent)
    return bool(parent_window) and _send(_ns_window(child), b"parentWindow") == parent_window


def _cocoa_miniaturized(widget: Any) -> bool:
    return bool(_send(_ns_window(widget), b"isMiniaturized", ctypes.c_bool))


class Resyncs:
    """Records each overlay show or hide: each one re-syncs the host to the surface.

    Overlay.setVisible() calls host.overlay_changed(), which moves the host over the surface.
    One between a window change and its check would hide a host that failed to follow.
    """

    def __init__(self, host: Any) -> None:
        self.calls: list[tuple[float, list[str]]] = []
        original = host.overlay_changed

        def overlay_changed() -> None:
            shown = [type(w).__name__ for w in host.visible_overlays()]
            self.calls.append((time.monotonic(), shown))
            original()

        host.overlay_changed = overlay_changed  # Overlay.setVisible looks it up on the host

    def last(self) -> float:
        return self.calls[-1][0] if self.calls else 0.0

    def since(self, moment: float) -> list[str]:
        return [f"{at - moment:+.2f}s {shown}" for at, shown in self.calls if at >= moment]


def run_app(out: Path, seconds: float) -> int:
    """Start the TV window over the fixture provider and drive it; returns app.exec()'s code."""
    faulthandler.dump_traceback_later(HANG_DUMP_S + seconds)  # a hang shows where; keeps going
    attempts: list[str] = []
    _refuse_network(attempts)
    report = Report(out / REPORT)
    report.data.update(seconds=seconds, screenshots={}, video={})
    report.step("start the app")

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
    report.step("after the app quit")
    driver.after_exit(code, attempts)
    return code


class Driver:
    """Runs the scenario as a generator: each yield is the delay in ms before the next step."""

    def __init__(self, window: Any, report: Report, out: Path, seconds: float) -> None:
        self.window = window
        self.report = report
        self.out = out
        self.seconds = seconds
        self.cocoa = QGuiApplication.platformName() == "cocoa"
        self.resyncs = Resyncs(window.host)
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
            self.problem = f"stopped during {self.report.data.get('step')}: {e!r}"
            self._close()
            return
        QTimer.singleShot(delay, self._run)

    def _wait(self, predicate: Callable[[], bool], timeout_ms: int) -> Generator[int, None, bool]:
        deadline = time.monotonic() + timeout_ms / 1000
        while not predicate() and time.monotonic() < deadline:
            yield POLL_MS
        return bool(predicate())

    def _settle(self, since: float) -> Generator[int, None, None]:
        """At least a second after `since`, then until the window holds still (≤ 2 s more)."""
        remaining = since + STATE_WAIT_MS / 1000 - time.monotonic()
        if remaining > 0:
            yield int(remaining * 1000)
        last, stable = self.window.geometry(), 0
        deadline = time.monotonic() + SETTLE_LIMIT_MS / 1000
        while stable < STABLE_MS and time.monotonic() < deadline:
            yield POLL_MS
            now = self.window.geometry()
            stable = stable + POLL_MS if now == last else 0
            last = now

    def _quiet(self) -> Generator[int, None, bool]:
        """Until only the drawer is up and no overlay showed or hid for half a second."""
        host, drawer = self.window.host, self.window.overlays.drawer

        def quiet() -> bool:
            idle = time.monotonic() - self.resyncs.last() >= QUIET_MS / 1000
            return idle and host.visible_overlays() == [drawer]

        return (yield from self._wait(quiet, QUIET_TIMEOUT_MS))

    def _key(self, key: Any) -> None:
        QTest.keyClick(self.window, key)

    def _move_mouse(self) -> None:
        """A mouse move over the middle of the video, sent to the surface (the real pointer
        stays where it is)."""
        surface = self.window.surface
        local = QPointF(surface.width() / 2, surface.height() / 2)
        event = QMouseEvent(
            QEvent.Type.MouseMove,
            local,
            QPointF(surface.mapToGlobal(local)),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(surface, event)

    def _fitted(self, rect: QRect) -> QRect:
        """Where TVWindow returns a window from fullscreen: `rect` inside the work area."""
        w = self.window
        return fit_to_area(rect, frame_margins(w), w.screen().availableGeometry())

    def _native_minimized(self) -> Optional[bool]:
        return _cocoa_miniaturized(self.window) if self.cocoa else None

    def _states(self) -> str:
        w = self.window
        native = self._native_minimized()
        return (
            f"window {_rect(w.geometry())}, screen {_rect(w.screen().geometry())}, "
            f"fullscreen {w.isFullScreen()}, minimized {w.isMinimized()}"
            + (f", miniaturized {native}" if native is not None else "")
        )

    # ---- checks -----------------------------------------------------------------------------

    def _playing(self, url: str) -> Callable[[], bool]:
        session = self.window.session
        return lambda: session.url == url and session.state() is PlaybackState.PLAYING

    def _check_glued(self, when: str, since: Optional[float] = None, quiet: bool = True) -> None:
        """The host sits exactly over the surface, and no overlay re-synced it since `since`."""
        host, surface = self.window.host, self.window.surface
        target = QRect(surface.mapToGlobal(QPoint(0, 0)), surface.size())
        resyncs = self.resyncs.since(since) if since is not None else []
        detail = (
            f"host {_rect(host.geometry())} {'shown' if host.isVisible() else 'hidden'}, "
            f"surface {_rect(target)}, window {_rect(self.window.geometry())}"
        )
        if not quiet:
            detail += "; overlays never settled before the window change"
        if resyncs:
            detail += f"; overlays showed or hid after the window change: {resyncs}"
        self.report.check(
            glue_check(when),
            quiet and not resyncs and host.isVisible() and host.geometry() == target,
            detail,
        )
        if self.cocoa:
            self.report.check(child_window_check(when), _cocoa_child_window_of(host, self.window))

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

    def _player(self) -> Any:
        """libVLC's player, or None without libVLC or while a worker stops its input.

        PlaybackSession ends inputs on a worker that holds the player's input lock; has_vout
        and the other calls below take it too, so they must wait until the job is done.
        """
        session = self.window.session
        if getattr(session, "_job", None) is not None:
            return None
        return getattr(session, "_player", None)

    def _video_facts(self) -> Optional[dict[str, Any]]:
        """The video output's facts; None while a worker still owns the player."""
        if getattr(self.window.session, "_player", None) is None:  # no libVLC
            return {"vout": 0}
        player = self._player()
        if player is None:
            return None
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
        report.step("1. the drawer opens at startup")
        opened = yield from self._wait(drawer.isVisible, DRAWER_TIMEOUT_MS)
        report.check("the drawer opens at startup", opened)
        yield 200  # macOS attaches the host as a child window on the next tick
        self._check_glued(GLUE_STEPS[0])
        self._capture(SCREENSHOTS[0])

        # 2. Activate the first channel: Return enters the group, Return plays Channel 1.
        report.step("2. play Channel 1")
        self._key(Qt.Key.Key_Return)
        yield from self._wait(lambda: getattr(drawer.highlighted(), "layer", None) == "item", 2000)
        self._key(Qt.Key.Key_Return)
        played = yield from self._wait(self._playing(first), PLAY_TIMEOUT_MS)
        report.check("Channel 1 plays", played, f"{session.state()} {session.url}")

        # 3. Down zaps to Channel 2: the banner shows it and the zap context is on it.
        report.step("3. zap to Channel 2")
        player = self._player()  # Channel 1's video output as the zap leaves it
        report.data["video_channel1"] = {
            "vout": player.has_vout() if player is not None else None,
            "state": session.state().value,
        }
        report.save()
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

        # Watch it play for --seconds: the clock moves and frames reach the surface.
        report.step("watch Channel 2")
        start, furthest = session.time_ms(), 0
        deadline = time.monotonic() + self.seconds
        facts_at = time.monotonic() + min(1.0, self.seconds / 2)
        while time.monotonic() < deadline:
            yield POLL_MS
            furthest = max(furthest, session.time_ms())
            if not report.data["video"] and time.monotonic() >= facts_at:
                facts = self._video_facts()
                if facts is not None:
                    report.data["video"] = facts
                    report.save()
        needed = min(1000, int(self.seconds * 500))
        report.check(
            "Channel 2's playback clock advances",
            furthest - start >= needed,
            f"from {start} ms to {furthest} ms in {self.seconds:g} s",
        )
        if QGuiApplication.platformName() in NATIVE_PLATFORMS:
            video = report.data["video"]
            shown, detail = video.get("vout", 0) >= 1, video
            if not shown and self.cocoa and os.environ.get("CI"):
                # CI's macOS VMs may have no OpenGL for libVLC's display: if libVLC never
                # opened one in this run, Channel 1 included, the clock above is the proof
                # of playback and the video output is only recorded.
                reason = _no_display_reason(self.out / VLC_LOG)
                if reason is not None:
                    video["informational"] = reason
                    shown, detail = True, f"informational on CI macOS: {video}"
            report.check("VLC shows the video in the surface", shown, detail)

        # The transport: a mouse move over the playing video shows it over the whole host.
        report.step("show the transport")
        transport = w.overlays.transport
        self._move_mouse()
        shown = yield from self._wait(transport.isVisible, TRANSPORT_TIMEOUT_MS)
        yield 200  # its first paint and the host's mask
        host_rect = w.host.rect()
        report.check(
            "the transport covers the overlay host",
            shown and w.host.isVisible() and transport.geometry() == host_rect,
            f"transport {_rect(transport.geometry())} "
            f"{'shown' if transport.isVisible() else 'hidden'}, host {_rect(host_rect)}",
        )
        self._capture(TRANSPORT_SCREENSHOT)

        # The window checks need overlays that hold still: pause (the clip would end and
        # toast) and open the drawer, which stays up until closed, then let the rest settle.
        report.step("pause and open the drawer")
        self._key(Qt.Key.Key_Space)
        self._key(Qt.Key.Key_Return)

        # 4. Move by (60, 40) and resize to 1100×620.
        report.step("4. move and resize")
        quiet = yield from self._quiet()
        before = w.geometry()
        changed_at = time.monotonic()
        w.move(w.pos().x() + 60, w.pos().y() + 40)
        w.resize(1100, 620)
        yield MOVE_WAIT_MS
        after = w.geometry()
        report.check(  # small screens may clamp the size: it only has to change
            WINDOW_CHANGES[0],
            after.topLeft() != before.topLeft() and after.size() != before.size(),
            f"{_rect(before)} -> {_rect(after)}",
        )
        self._check_glued(GLUE_STEPS[1], changed_at, quiet)

        # 5. Fullscreen and back. The drawer is open, so F would type: use the window action.
        report.step("5. enter fullscreen")
        quiet = yield from self._quiet()
        normal = w.geometry()
        changed_at = time.monotonic()
        w.toggle_fullscreen()
        entered = yield from self._wait(
            lambda: w.isFullScreen() and w.geometry() == w.screen().geometry(), STATE_TIMEOUT_MS
        )
        yield from self._settle(changed_at)
        report.check(WINDOW_CHANGES[1], entered, self._states())
        self._check_glued(GLUE_STEPS[2], changed_at, quiet)
        self._capture(SCREENSHOTS[3])

        report.step("5. leave fullscreen")
        quiet = yield from self._quiet()
        changed_at = time.monotonic()
        w.toggle_fullscreen()

        def back() -> bool:  # the window from before, kept inside the screen's work area
            return not w.isFullScreen() and w.geometry() == self._fitted(normal)

        left = yield from self._wait(back, STATE_TIMEOUT_MS)
        yield from self._settle(changed_at)
        report.check(
            WINDOW_CHANGES[2],
            left and back(),
            f"{self._states()}, before {_rect(normal)}, expected {_rect(self._fitted(normal))}",
        )
        self._check_glued(GLUE_STEPS[3], changed_at, quiet)

        # Minimize and restore: nothing floats over other apps meanwhile (Review Focus 1).
        report.step("minimize")
        quiet = yield from self._quiet()
        w.showMinimized()
        minimized = yield from self._wait(
            lambda: w.isMinimized() and self._native_minimized() is not False, STATE_TIMEOUT_MS
        )
        report.check(WINDOW_CHANGES[3], minimized, self._states())
        report.check(
            MINIMIZED_CHECK,
            not w.host.isVisible(),
            f"host {'shown' if w.host.isVisible() else 'hidden'}",
        )

        report.step("restore")
        changed_at = time.monotonic()
        w.showNormal()
        w.activateWindow()
        restored = yield from self._wait(
            lambda: not w.isMinimized() and not self._native_minimized() and w.host.isVisible(),
            STATE_TIMEOUT_MS,
        )
        yield from self._settle(changed_at)
        report.check(WINDOW_CHANGES[4], restored, self._states())
        self._check_glued(GLUE_STEPS[4], changed_at, quiet)

        # 6. Close; the parent judges the exit code and time.
        report.step("6. close")
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
            SCENARIO_CHECK,
            self.finished,
            self.problem or ("" if self.finished else "the app quit before the end"),
        )
        report.check("the window closes", not self.window.isVisible())
        report.check(CLOSED_CHECK, not self.window.host.isVisible())
        # What has_pending_threads() counts: QThreads whose native teardown is still pending.
        pending = [type(p._worker or p._thread).__name__ for p in list(thread_cleanup._pending)]
        report.check(
            "no background threads are pending", not thread_cleanup.has_pending_threads(), pending
        )
        shots = report.data["screenshots"]
        report.check(
            "the screenshots are saved", all(shots.get(name) for name in SCREENSHOTS), shots
        )
        report.check("nothing touches the network", not attempts, attempts)
        report.data["overlay_shows_and_hides"] = len(self.resyncs.calls)
        report.save()


# ---- entry point ------------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", required=True, help="directory for smoke.json and screenshots")
    parser.add_argument(
        "--seconds",
        type=float,
        default=DEFAULT_SECONDS,
        help="how long to watch Channel 2 play after the zap, at least 1 (default: %(default)s)",
    )
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    out = Path(args.out).resolve()
    seconds = max(MIN_SECONDS, args.seconds)
    return run_app(out, seconds) if args.child else run_smoke(out, seconds)


if __name__ == "__main__":
    sys.exit(main())
