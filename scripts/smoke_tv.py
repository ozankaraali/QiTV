"""Smoke test of the TV window with real libVLC on the local fixture provider (spec §11).

    uv run python scripts/smoke_tv.py --out smoke-out [--seconds 3] [--pipeline] [--pip]
    QITV_SMOKE=1 <app> --smoke --out smoke-frozen --pipeline --pip --fixtures tests/fixtures

The app runs in a child process: an isolated QITV_CONFIG_DIR, the local M3U fixture
(tests/fixtures/local_provider.py), the real TVWindow and libVLC. The script drives it:
drawer → play → zap → the transport over the whole host, then the overlay host against the
video surface after a move and resize, in and out of fullscreen and after a minimize, then a
close and a clean exit. In the packaged app, main.py's `--smoke` entry runs this script (the
parent and the child are the app itself); the fixtures come from the checkout (`--fixtures`).

Before the close the app records which libVLC it loaded (libvlc, libvlccore and plugin
paths, as the loader sees them, and python-vlc's choice) in smoke.json; a packaged app must
have loaded them all from its own bundle (an installed VLC would mix in or stand in).

`--pipeline` adds the live time-shift pipeline before the close. This script serves
tests/fixtures/live.ts on 127.0.0.1 as an endless real-time channel (live_source.LiveSource),
Channel 3 of the playlist, and the app has time-shift consent (its buffer on disk). Down zaps
to it: it must play from the loopback stream; a 4 s pause leaves it at least 3 s behind live;
L (Go live) is live within 6 s, at most target + 0.75 s (stay-live's band) + the newest
segment's length (the starts it can land on are a segment apart) + 0.5 s (VLC's open) behind,
and at the end of the 6 s stay-live is closing in (rate above 1x) or inside the band; ← moves
the position back 8-12 s; 1.5x is applied and returns to 1x once stay-live is back at live;
then Down zaps to Channel 4 (a second such source) and Up back to Channel 3 as soon as
Channel 4's upstream delivers: each source, and both together, never had more than one
connection open at once (spec §11; the sources see a client's close at once); after the
close no helper process and nothing in the timeshift directory remain. Lags and
positions are the LiveSession timeline's (`timelineChanged`), not VLC's. A run killed for a
hang has its live helper killed too.

`--pip` adds picture in picture and swap (spec §7) after that, on the same two sources (it
serves them without --pipeline too): Channel 3 (A), then Down to Channel 4 (B), and P opens
A small, muted. Checked: both play from the loopback stream; A's mute, asked for before its
media opened, holds once it plays; the small picture is a quarter of the player's width in
the bottom-right corner (raised above the transport while that shows) and the PiP label
covers it in the overlay host's coordinates; S makes A large without restarting either
player (their play and open counts stay) and moves the sound to A; after a 4 s pause of A
and two more swaps A is still at least 3 s behind live; the two players keep a volume and a
mute of their own, read back from libVLC; after a move and resize the geometry holds; P
closes the small picture (B): one session plays on, its budget ticket is back and the live
helper reports a single session. With the PiP up, each source never has more than one
connection open and both together two. libVLC's audio output modules are recorded, and on
Windows it must be DirectSound (per-player volume; services/playback.default_args).

Every check is written to <out>/smoke.json as {"name", "ok", "detail"}; any failed check makes
the exit code non-zero. The overlay gate (spec D4) passes only when every expected "overlay: …"
check and every window change they rely on is present and passed; a platform that fails it
gets the Qt-drawn frame fallback. With --pipeline, the pipeline gate likewise needs every
"pipeline: …" check present and passed, and with --pip the PiP gate every "pip: …" check and
the helper and timeshift directory checks after the close. Screenshots are the overlay host's
grab() composited over black (OS capture needs permissions), plus libVLC snapshots of the
video; the PiP screenshots draw both players' snapshots where their surfaces are, under the
overlays. Nothing here touches the network: Python-level lookups and connections beyond this
machine are refused and recorded as a failure. Logs go to <out>/runner.log and <out>/app.log
as well (the Windows app has no console).

libVLC gets a dummy audio output unless CI is set (QITV_SMOKE_MUTE=1 forces it, =0 plays
the fixtures' tone), so a local run on a desktop someone uses stays silent. CI plays to
the real output. The dummy output keeps no volume or mute, so the PiP's audio read-backs are
only recorded in a muted run (as they are on a machine where libVLC could open no audio
output at all).
"""

from __future__ import annotations

import argparse
from collections import Counter
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
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Generator, Optional
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6 import __version__ as PYSIDE_VERSION  # noqa: E402
from PySide6.QtCore import (  # noqa: E402
    QEvent,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    Qt,
    QTimer,
    qVersion,
)
from PySide6.QtGui import QGuiApplication, QImage, QMouseEvent, QPainter  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
import qdarktheme  # noqa: E402

from services import playback, thread_cleanup  # noqa: E402
from services.config_manager import ConfigManager  # noqa: E402
from services.epg_manager import EpgManager  # noqa: E402
from services.image_manager import ImageManager  # noqa: E402
from services.playback import VLC_LOG_ENV, PlaybackSession, PlaybackState  # noqa: E402
from services.provider_manager import ProviderManager  # noqa: E402
from services.sessions import MAIN_PURPOSE  # noqa: E402
from services.sessions import PURPOSE as PIP_PURPOSE  # noqa: E402
from services.vlc_profile import PlayKind  # noqa: E402
from tests.fixtures.audio_check import reading_matches, should_mute  # noqa: E402
from tests.fixtures.live_source import LIVE_TS, LiveSource, opens_in, peak_of  # noqa: E402
from tests.fixtures.local_provider import (  # noqa: E402
    FIXTURE_DIR,
    channel_urls,
    seed_channels,
    write_local_provider,
)
from ui import tv_window  # noqa: E402
from ui.pip import DEFAULT_CORNER, WIDTH_FRACTION, lifted_rect  # noqa: E402
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
# The packaged app only: everything libVLC loaded (libvlc, libvlccore, plugins) is the app's own.
BUNDLED_VLC_CHECK = "the packaged app plays with its own libVLC and plugins"

MUTE_ENV = "QITV_SMOKE_MUTE"  # "1"/"0": dummy audio output or real sound; unset: dummy unless CI

RUNNER_LOG = "runner.log"
APP_LOG = "app.log"
FAULTS = "faults.txt"  # Python stacks of a hang or crash when there is no stderr (Windows app)

# ---- the live time-shift pipeline (--pipeline) ----
PIPELINE_PREFIX = "pipeline: "
PIPELINE_SCREENSHOTS = ("04-timeshift.png", "05-catching-up.png")
PIPELINE_VIDEO = "04-video.png"
PIPELINE_BUDGET_S = 150.0  # what the pipeline phase may add before the hang dump
LIVE_CHANNEL = "Channel 3"
LIVE_PLAY_TIMEOUT_MS = 20_000  # the release of Channel 2, up to 6 s for ready, VLC's open
WARM_UP_MS = 1_000  # play this long before the pause
PAUSE_MS = 4_000
PAUSED_LAG_S = 3.0
STATE_CHANGE_MS = 3_000  # for VLC to pause or play again
FRESH_TIMELINE_MS = 2_000  # LiveSession emits its timeline every 250 ms
GO_LIVE_MS = 6_000
GO_LIVE_BAND_S = 0.75  # the top of stay-live's band above the target
GO_LIVE_OPEN_S = 0.5  # VLC opening the reopened stream
HISTORY_S = 14.0  # buffered behind the position before the seek: 10 s back stays inside it
HISTORY_TIMEOUT_MS = 20_000
SEEK_BACK = (8.0, 12.0)  # seconds the position may move back for ← (10 s, segment-aligned)
SPEED = 1.5
SPEED_TIMEOUT_MS = 3_000
CATCH_UP_TIMEOUT_MS = 60_000  # at 1.5x the lag shrinks by only 0.5 s a second
RATE_TOLERANCE = 0.01
ZAP_CHANNEL = "Channel 4"  # the second paced source: Down from Channel 3, then Up back
ZAP_OPEN_TIMEOUT_MS = 15_000  # Channel 3's release, then Channel 4's upstream delivering

TUNED_CHECK = f"{PIPELINE_PREFIX}{LIVE_CHANNEL} plays from the loopback time-shift stream"
ON_DISK_CHECK = f"{PIPELINE_PREFIX}its buffer is on disk in the timeshift directory"
PAUSE_CHECK = f"{PIPELINE_PREFIX}a 4 s pause leaves it at least 3 s behind live"
GO_LIVE_CHECK = (
    f"{PIPELINE_PREFIX}Go live is live within 6 s, at most a segment past the band, and closing in"
)
SEEK_CHECK = f"{PIPELINE_PREFIX}seeking back 10 s moves the position back 8-12 s"
SPEED_CHECK = f"{PIPELINE_PREFIX}1.5x is applied while time-shifted"
LIVE_AGAIN_CHECK = f"{PIPELINE_PREFIX}the speed returns to 1x once back at live"
NO_HELPER_CHECK = f"{PIPELINE_PREFIX}no live helper process remains after the close"
EMPTY_DIR_CHECK = f"{PIPELINE_PREFIX}the timeshift directory is empty after the close"
ZAP_CHECK = (
    f"{PIPELINE_PREFIX}zapping {LIVE_CHANNEL} → {ZAP_CHANNEL} → {LIVE_CHANNEL} never has more "
    "than one source connection open"
)
PIPELINE_CHECKS = (
    TUNED_CHECK,
    ON_DISK_CHECK,
    PAUSE_CHECK,
    GO_LIVE_CHECK,
    SEEK_CHECK,
    SPEED_CHECK,
    LIVE_AGAIN_CHECK,
    ZAP_CHECK,
    NO_HELPER_CHECK,
    EMPTY_DIR_CHECK,
)

# ---- picture in picture and swap (--pip) ----
PIP_PREFIX = "pip: "
PIP_SCREENSHOTS = ("pip-01-open.png", "pip-02-swapped.png")
PIP_BUDGET_S = 120.0  # what the PiP phase may add before the hang dump
PIP_OPEN_TIMEOUT_MS = 20_000  # its link, the helper's session, up to 6 s for ready, VLC's open
SWAP_TIMEOUT_MS = 3_000
SWAP_SETTLE_MS = 1_000  # a player a swap restarted would have opened again by now
GEOMETRY_TIMEOUT_MS = 2_000  # for a lift (or its settling back) to finish
AUDIO_APPLY_MS = 3_000  # PlaybackSession applies a wanted mute once the audio output opens
AUDIO_SAMPLES = 5
AUDIO_SETTLE_MS = 500  # for a shared audio session to report one player's change to the other
SMALL_LEVEL, LARGE_LEVEL = 20, 80  # set on the two players (the small one stays muted)
RELEASE_WAIT_MS = 5_000  # BudgetedStream gives a ticket back within 2.5 s of the stop
HELPER_WATCH_MS = 1_500  # the helper reports each session's state several times a second
PIP_STEPS = ("when it opens", "after a move and resize")
DIRECTSOUND = "directsound"
AUDIO_OUTPUT_STARTED = "main debug: output '"  # libVLC started an audio output
AUDIO_OUTPUT_FAILED = "failed to create audio output"
_AUDIO_MODULE = re.compile(r'using audio output module "([^"]+)"')

PIP_OPEN_CHECK = (
    f"{PIP_PREFIX}P opens {LIVE_CHANNEL} small while {ZAP_CHANNEL} plays large, both from the "
    "time-shift pipeline"
)
MUTE_HELD_CHECK = (
    f"{PIP_PREFIX}the small picture's mute, asked for before its media opened, holds once it plays"
)
SWAP_CHECK = f"{PIP_PREFIX}S makes {LIVE_CHANNEL} large; both play on and neither restarts"
SWAP_AUDIO_CHECK = f"{PIP_PREFIX}after the swap {LIVE_CHANNEL} is heard and {ZAP_CHANNEL} is muted"
SWAP_UPSTREAM_CHECK = f"{PIP_PREFIX}the swap opens no new upstream connection on either source"
SHIFTED_CHECK = (
    f"{PIP_PREFIX}after a 4 s pause and two swaps the large picture is still at least 3 s "
    "behind live"
)
LEVELS_CHECK = f"{PIP_PREFIX}each player keeps a volume and a mute of its own"
PIP_WINDOW_CHANGE = f"{PIP_PREFIX}the window moves and resizes"
ONE_SESSION_CHECK = f"{PIP_PREFIX}P closes the small picture and one session plays on"
TICKET_CHECK = f"{PIP_PREFIX}the small picture's budget ticket is released"
HELPER_SESSION_CHECK = f"{PIP_PREFIX}the live helper reports a single session"
PEAK_CHECK = (
    f"{PIP_PREFIX}with picture-in-picture each source has one connection open at most, and "
    "both together two"
)
DIRECTSOUND_CHECK = f"{PIP_PREFIX}libVLC plays audio through DirectSound on Windows"
_STREAM_PATH = re.compile(r"/[^/]+/[^/]+(/stream/[0-9]+\.ts)$")  # /<token>/<session>/stream/…


def glue_check(when: str) -> str:
    return f"{OVERLAY_PREFIX}host covers the video surface {when}"


def child_window_check(when: str) -> str:
    return f"{OVERLAY_PREFIX}host is a child window of the TV window {when}"


def panel_check(when: str) -> str:
    """macOS: overlays show while QiTV is inactive, and never float over other apps."""
    return f"{OVERLAY_PREFIX}host stays up while QiTV is inactive, at the window's level {when}"


def gate_checks(cocoa: bool) -> list[str]:
    """Every check the overlay gate needs, by name."""
    names = list(WINDOW_CHANGES)
    for when in GLUE_STEPS:
        names.append(glue_check(when))
        if cocoa:
            names += [child_window_check(when), panel_check(when)]
    return names + [MINIMIZED_CHECK, CLOSED_CHECK]


def pip_corner_check(when: str) -> str:
    return (
        f"{PIP_PREFIX}the small picture is a quarter of the width, in the bottom-right corner "
        f"{when}"
    )


def pip_label_check(when: str) -> str:
    return f"{PIP_PREFIX}the PiP label covers the small picture in host coordinates {when}"


def pip_gate_checks(directsound: bool) -> list[str]:
    """Every check the PiP gate needs, by name (`directsound`: Windows with real audio)."""
    names = [PIP_OPEN_CHECK, MUTE_HELD_CHECK]
    for when in PIP_STEPS:
        names += [pip_corner_check(when), pip_label_check(when)]
    names += [
        SWAP_CHECK,
        SWAP_AUDIO_CHECK,
        SWAP_UPSTREAM_CHECK,
        SHIFTED_CHECK,
        LEVELS_CHECK,
        PIP_WINDOW_CHANGE,
        ONE_SESSION_CHECK,
        TICKET_CHECK,
        HELPER_SESSION_CHECK,
        PEAK_CHECK,
        NO_HELPER_CHECK,
        EMPTY_DIR_CHECK,
    ]
    return names + ([DIRECTSOUND_CHECK] if directsound else [])


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


def _directsound_expected(data: dict[str, Any]) -> bool:
    """Windows with a real audio output: the DirectSound check applies."""
    return sys.platform == "win32" and not data.get("muted")


def _summarize(data: dict[str, Any], pipeline: bool, pip: bool = False) -> None:
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
    gates = [data["overlay_gate"]]
    if pipeline:
        data["pipeline_gate"] = {
            "ok": False,
            "failed": [n for n in failed if n.startswith(PIPELINE_PREFIX)],
            "missing": [n for n in PIPELINE_CHECKS if n not in outcome],
        }
        gate = data["pipeline_gate"]
        gate["ok"] = not gate["failed"] and not gate["missing"]
        gates.append(gate)
    if pip:
        expected = pip_gate_checks(_directsound_expected(data))
        data["pip_gate"] = {
            "ok": False,
            "failed": [n for n in failed if n.startswith(PIP_PREFIX) or n in expected],
            "missing": [n for n in expected if n not in outcome],
        }
        gate = data["pip_gate"]
        gate["ok"] = not gate["failed"] and not gate["missing"]
        gates.append(gate)
    data["failed"] = failed
    data["ok"] = not failed and all(gate["ok"] for gate in gates)


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


def _kill_helper(report: Report) -> None:
    """After a hung app was killed: kill its live helper too, if it still runs (best effort)."""
    facts = report.data.get("pipeline")
    if not isinstance(facts, dict):
        return
    pid = facts.get("helper_pid")
    if not isinstance(pid, int) or not _process_alive(pid):
        return
    try:
        os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))  # Windows: TerminateProcess
    except OSError as e:
        logger.warning("Could not kill the hung app's live helper (pid %d): %s", pid, e)
        return
    facts["helper_killed"] = True
    logger.warning("Killed the hung app's live helper (pid %d)", pid)


def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _child_command(args: list[str]) -> list[str]:
    """The app under test: this script again, or the packaged app's `--smoke` entry."""
    if _frozen():
        return [sys.executable, "--smoke", *args]  # main.py; QITV_SMOKE=1 is inherited
    return [sys.executable, "-X", "faulthandler", str(Path(__file__).resolve()), *args]


def _hang_budget(seconds: float, pipeline: bool, pip: bool = False) -> float:
    """Seconds after which the app dumps its Python stacks (it keeps going)."""
    extra = (PIPELINE_BUDGET_S if pipeline else 0.0) + (PIP_BUDGET_S if pip else 0.0)
    return HANG_DUMP_S + seconds + extra


def run_smoke(out: Path, seconds: float, fixtures: Path, pipeline: bool, pip: bool = False) -> int:
    stale = (
        *SCREENSHOTS,
        *PIPELINE_SCREENSHOTS,
        *PIP_SCREENSHOTS,
        VIDEO_SNAPSHOT,
        PIPELINE_VIDEO,
        FAULTS,
    )
    for name in (REPORT, *stale, HANG_STACK, VLC_LOG):
        (out / name).unlink(missing_ok=True)
    limit = _hang_budget(seconds, pipeline, pip) + KILL_GRACE_S
    args = ["--child", "--out", str(out), "--seconds", str(seconds), "--fixtures", str(fixtures)]
    args += (["--pipeline"] if pipeline else []) + (["--pip"] if pip else [])
    # Channel 3 and Channel 4 (the zap; the two pictures): two paced sources of the same clip.
    sources = [LiveSource(fixtures / LIVE_TS.name) for _ in range(2)] if pipeline or pip else []
    for source in sources:
        args += ["--live-url", source.url]
        logger.info("Serving %s at %s", LIVE_TS.name, source.url)
    hang_stack = None
    try:
        with tempfile.TemporaryDirectory(
            prefix="qitv-smoke-", ignore_cleanup_errors=True
        ) as config:
            env = dict(os.environ, QITV_CONFIG_DIR=config)
            env[VLC_LOG_ENV] = str(out / VLC_LOG)
            process = subprocess.Popen(
                _child_command(args), cwd=None if _frozen() else ROOT, env=env
            )
            try:
                code: Optional[int] = process.wait(timeout=limit)
            except subprocess.TimeoutExpired:
                hang_stack = _native_stack(process.pid, out)
                process.kill()
                process.wait()
                code = None
            exited_at = time.time()
    finally:
        for source in sources:
            source.close()

    report = Report(out / REPORT)
    try:
        report.data = json.loads((out / REPORT).read_text(encoding="utf-8"))
        report.data.setdefault("checks", [])
    except (OSError, ValueError) as e:
        report.check("the app writes its report", False, f"{e} (exit code {code})")
    report.data["hang_stack"] = hang_stack
    if code is None:
        _kill_helper(report)
    stopped = f"stopped after {limit:.0f} s" if code is None else f"exit code {code}"
    if _frozen() and BUNDLED_VLC_CHECK not in report.names():
        report.check(BUNDLED_VLC_CHECK, False, f"never recorded ({report.data.get('step')})")
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
    if sources:
        _judge_sources(report, sources, pipeline, pip)
    if pip:
        _judge_audio_outputs(report, out / VLC_LOG)
    report.data["frozen"] = _frozen()
    _summarize(report.data, pipeline, pip)
    report.save()
    gates = [("overlay", report.data["overlay_gate"])]
    if pipeline:
        gates.append(("pipeline", report.data["pipeline_gate"]))
    if pip:
        gates.append(("PiP", report.data["pip_gate"]))
    logger.info(
        "Smoke %s: %d checks, %d failed%s; %s. Report: %s",
        "passed" if report.data["ok"] else "FAILED",
        len(report.data["checks"]),
        len(report.data["failed"]),
        f" ({'; '.join(report.data['failed'])})" if report.data["failed"] else "",
        "; ".join(
            f"{name} gate {'passed' if gate['ok'] else 'FAILED'}"
            + (f" (missing: {'; '.join(gate['missing'])})" if gate["missing"] else "")
            for name, gate in gates
        ),
        out / REPORT,
    )
    return 0 if report.data["ok"] else 1


def _judge_sources(report: Report, sources: list[LiveSource], pipeline: bool, pip: bool) -> None:
    """From what the sources saw: the zap (before the PiP phase) never had more than one
    connection open, and with the PiP each source one and both together two."""
    facts = report.data.setdefault("pipeline", {})
    first, second = sources
    facts["source_connections"] = first.connections
    channels = {LIVE_CHANNEL: first, ZAP_CHANNEL: second}
    facts["sources"] = {
        name: {"connections": source.connections, "peak_concurrent": source.peak}
        for name, source in channels.items()
    }
    together = peak_of(first.events + second.events)
    facts["sources_peak_concurrent"] = together
    # The sources' clock is this process's monotonic one; the app records wall-clock times.
    started_at = (report.data.get("pip") or {}).get("started_at")
    pip_from = None
    if isinstance(started_at, (int, float)):
        pip_from = started_at - (time.time() - time.monotonic())
    if pipeline:
        zap = facts.get("zap") or {}
        zapped = bool(zap.get("opened")) and bool(zap.get("back"))
        peaks = [peak_of(source.events, end=pip_from) for source in sources]
        both = peak_of(first.events + second.events, end=pip_from)
        report.check(
            ZAP_CHECK,
            zapped and peaks == [1, 1] and both == 1,
            f"peak concurrent connections"
            f"{' before the PiP phase' if pip_from is not None else ''}: {LIVE_CHANNEL} "
            f"{peaks[0]}, {ZAP_CHANNEL} {peaks[1]}, both {both}; connections "
            f"{first.connections} and {second.connections}; {ZAP_CHANNEL} delivered before Up: "
            f"{zap.get('opened')}, back on {LIVE_CHANNEL}: {zap.get('back')}",
        )
    if pip:
        if pip_from is None:
            report.check(PEAK_CHECK, False, "the PiP phase never started")
            return
        peaks = [peak_of(source.events, start=pip_from) for source in sources]
        both = peak_of(first.events + second.events, start=pip_from)
        report.data["pip"]["sources_peak_concurrent"] = {
            LIVE_CHANNEL: peaks[0],
            ZAP_CHANNEL: peaks[1],
            "both": both,
        }
        report.check(
            PEAK_CHECK,
            peaks == [1, 1] and both == 2,
            f"peak concurrent connections from the PiP phase on: {LIVE_CHANNEL} {peaks[0]}, "
            f"{ZAP_CHANNEL} {peaks[1]}, both {both}",
        )
        window = report.data["pip"].get("swap_window")
        if not isinstance(window, list) or len(window) != 2:
            report.check(SWAP_UPSTREAM_CHECK, False, "the swap never happened")
            return
        offset = time.time() - time.monotonic()
        opens = [
            opens_in(source.events, window[0] - offset, window[1] - offset) for source in sources
        ]
        report.check(
            SWAP_UPSTREAM_CHECK,
            opens == [0, 0],
            f"connections opened during the swap: {LIVE_CHANNEL} {opens[0]}, {ZAP_CHANNEL} "
            f"{opens[1]}",
        )


def _audio_output_facts(log: Path) -> dict[str, Any]:
    """Which audio output modules libVLC loaded, and how often one started or failed to."""
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {"error": repr(e)}
    return {
        "modules": dict(Counter(_AUDIO_MODULE.findall(text))),
        "started": text.count(AUDIO_OUTPUT_STARTED),
        "failed": text.count(AUDIO_OUTPUT_FAILED),
    }


def _no_audio_reason(log: Path) -> Optional[str]:
    """Why no audio output could be judged, when libVLC's log shows it never started one."""
    facts = _audio_output_facts(log)
    if "error" in facts or facts["started"] or not facts["failed"]:
        return None
    modules = ", ".join(facts["modules"]) or "none"
    return (
        f"libVLC never started an audio output on this machine ({facts['failed']} failures; "
        f"modules: {modules})"
    )


def _judge_audio_outputs(report: Report, log: Path) -> None:
    """Record (and log) the audio outputs libVLC used; on Windows they must be DirectSound."""
    facts = _audio_output_facts(log)
    report.data.setdefault("pip", {})["audio_output"] = facts
    logger.info("libVLC audio output: %s", facts)
    if _directsound_expected(report.data):
        modules = facts.get("modules") or {}
        report.check(DIRECTSOUND_CHECK, bool(modules) and set(modules) == {DIRECTSOUND}, facts)


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


def _loaded_images() -> list[str]:
    """The paths of the shared libraries this process has loaded (best effort)."""
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None)
        libc._dyld_image_count.restype = ctypes.c_uint32
        libc._dyld_get_image_name.restype = ctypes.c_char_p
        libc._dyld_get_image_name.argtypes = [ctypes.c_uint32]
        names = (libc._dyld_get_image_name(i) for i in range(libc._dyld_image_count()))
        return [name.decode(errors="replace") for name in names if name]
    if sys.platform == "win32":
        psapi = ctypes.WinDLL("psapi")  # type: ignore[attr-defined]
        kernel32 = ctypes.WinDLL("kernel32")  # type: ignore[attr-defined]
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.GetModuleFileNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32]
        psapi.EnumProcessModules.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        modules = (ctypes.c_void_p * 4096)()
        needed = ctypes.c_uint32()
        process = kernel32.GetCurrentProcess()
        if not psapi.EnumProcessModules(
            process, modules, ctypes.sizeof(modules), ctypes.byref(needed)
        ):
            return []
        paths = []
        name = ctypes.create_unicode_buffer(32768)
        for module in modules[: min(needed.value // ctypes.sizeof(ctypes.c_void_p), 4096)]:
            if kernel32.GetModuleFileNameW(module, name, len(name)):
                paths.append(name.value)
        return paths
    try:
        with open("/proc/self/maps", encoding="utf-8", errors="replace") as maps:
            fields = (line.split(None, 5) for line in maps)
            return sorted({f[5].strip() for f in fields if len(f) == 6 and f[5].startswith("/")})
    except OSError:
        return []


def _libvlc_facts() -> dict[str, Any]:
    """Which libVLC this process loaded, and from where: python-vlc's view and the loader's."""
    vlc = playback.vlc
    found: dict[str, list[str]] = {"libvlc": [], "libvlccore": [], "plugins": []}
    for path in _loaded_images():
        name = os.path.basename(path).lower()
        if "_plugin." in name:
            found["plugins"].append(path)
        elif name.startswith("libvlccore."):
            found["libvlccore"].append(path)
        elif name.startswith("libvlc."):
            found["libvlc"].append(path)
    plugin_dirs: dict[str, int] = {}
    for path in found["plugins"]:
        directory = os.path.dirname(path)
        plugin_dirs[directory] = plugin_dirs.get(directory, 0) + 1
    return {
        "python_vlc_library": getattr(getattr(vlc, "dll", None), "_name", None),
        "python_vlc_plugin_path": getattr(vlc, "plugin_path", None),
        "VLC_PLUGIN_PATH": os.environ.get("VLC_PLUGIN_PATH"),
        "libvlc": found["libvlc"],
        "libvlccore": found["libvlccore"],
        "plugin_dirs": plugin_dirs,
        "bundle": getattr(sys, "_MEIPASS", None),
    }


def _bundled(facts: dict[str, Any]) -> bool:
    """libvlc, libvlccore and at least one plugin are loaded, all from the app's bundle."""
    bundle = facts.get("bundle")
    if not bundle:
        return False
    top = os.path.normcase(os.path.realpath(bundle)) + os.sep

    def inside(path: Any) -> bool:
        return isinstance(path, str) and os.path.normcase(os.path.realpath(path)).startswith(top)

    paths = [*facts["libvlc"], *facts["libvlccore"], *facts["plugin_dirs"]]
    loaded = all(facts[key] for key in ("libvlc", "libvlccore", "plugin_dirs"))
    return loaded and inside(facts["python_vlc_library"]) and all(inside(p) for p in paths)


def _process_alive(pid: int) -> bool:
    """Whether process `pid` still runs; a zombie (exited, not yet reaped) does not."""
    if sys.platform == "win32":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(0x1000, 0, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: someone else's process
        try:
            code = ctypes.c_uint32()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        stat = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=10
        ).stdout.strip()
    except OSError, subprocess.SubprocessError:
        return True
    return bool(stat) and not stat.startswith("Z")


def _entries(directory: Optional[str]) -> list[str]:
    """What `directory` holds (none when it does not exist)."""
    if not directory or not os.path.isdir(directory):
        return []
    return sorted(os.listdir(directory))


def _redacted(url: Optional[str]) -> str:
    """A loopback stream URL without its token and session id."""
    if not url:
        return str(url)
    parts = urlsplit(url)
    path = _STREAM_PATH.sub(r"/…\1", parts.path)
    return f"{parts.scheme}://{parts.netloc}{path}"


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


def _cocoa_panel(child: Any, parent: Any) -> tuple[bool, int, int]:
    """(hidesOnDeactivate, level) of the child's NSWindow, and the parent's level."""
    window = _ns_window(child)
    hides = bool(_send(window, b"hidesOnDeactivate", ctypes.c_bool))
    return (
        hides,
        _send(window, b"level", ctypes.c_long),
        _send(_ns_window(parent), b"level", ctypes.c_long),
    )


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


def run_app(
    out: Path,
    seconds: float,
    fixtures: Path,
    live_urls: list[str],
    pipeline: bool = False,
    pip: bool = False,
) -> int:
    """Start the TV window over the fixture provider and drive it; returns app.exec()'s code."""
    # The packaged app has no -X faulthandler, and the Windows one no stderr.
    faults = sys.stderr if sys.stderr is not None else open(out / FAULTS, "w", encoding="utf-8")
    faulthandler.enable(file=faults)
    budget = _hang_budget(seconds, pipeline, pip)
    faulthandler.dump_traceback_later(budget, file=faults)  # a hang shows where; keeps going
    attempts: list[str] = []
    _refuse_network(attempts)
    report = Report(out / REPORT)
    report.data.update(seconds=seconds, screenshots={}, video={})
    if live_urls:  # local fixtures: no credentials
        report.data["pipeline"] = {"source_url": live_urls[0], "source_urls": live_urls}
    report.step("start the app")

    config_dir = os.environ["QITV_CONFIG_DIR"]
    provider = write_local_provider(config_dir, fixture_dir=fixtures, live_urls=live_urls)
    app = QApplication([sys.argv[0]])
    config = ConfigManager()
    if live_urls:
        # Time-shift consent: the buffer goes to disk, so the empty directory at the end
        # shows that it was cleaned up (the smallest size the setting allows).
        config.timeshift_enabled = True
        config.timeshift_max_mib = 256
    images = ImageManager(config, config.max_cache_image_size * 1024 * 1024)
    providers = ProviderManager(config)
    epg = EpgManager(config, providers)
    seed_channels(providers, provider, fixture_dir=fixtures, live_urls=live_urls)
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

    if should_mute(os.environ):
        report.data["muted"] = True

        def muted(extra_args: Optional[list[str]] = None) -> Any:
            return playback.create_instance(
                [*(extra_args or playback.default_args()), "--aout=dummy"]
            )

        tv_window.create_instance = muted  # the window's player and its PiP play to nothing
    window = TVWindow(app, config, providers, images, epg)
    driver = Driver(window, report, out, seconds, fixtures, live_urls, pipeline, pip)
    window.show()
    QTimer.singleShot(0, driver.start)
    code = app.exec()
    report.step("after the app quit")
    driver.after_exit(code, attempts)
    return code


class Driver:
    """Runs the scenario as a generator: each yield is the delay in ms before the next step."""

    def __init__(
        self,
        window: Any,
        report: Report,
        out: Path,
        seconds: float,
        fixtures: Path,
        live_urls: list[str],
        pipeline: bool = False,
        pip: bool = False,
    ) -> None:
        self.window = window
        self.report = report
        self.out = out
        self.seconds = seconds
        self.fixtures = fixtures
        self.live_urls = live_urls
        self.pipeline = pipeline and bool(live_urls)
        self.pip = pip and bool(live_urls)
        self.cocoa = QGuiApplication.platformName() == "cocoa"
        self.resyncs = Resyncs(window.host)
        self.timeline: list[tuple[float, dict[str, Any]]] = []  # (when, LiveSession state)
        # The PiP phase: each slot's timeline, the helper's session updates, the play counts.
        self.slot_timeline: list[tuple[float, str, dict[str, Any]]] = []  # (when, slot, state)
        self.helper_updates: list[tuple[float, str]] = []  # (when, session id)
        self.plays: dict[str, dict[str, int]] = {}  # slot -> {"play": n, "open": n}
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
            hides, level, window_level = _cocoa_panel(host, self.window)
            self.report.check(
                panel_check(when),
                not hides and level == window_level,
                f"hidesOnDeactivate {hides}, level {level}, window level {window_level}",
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

    def _player(self) -> Any:
        """libVLC's player, or None without libVLC or while a worker stops its input.

        PlaybackSession ends inputs on a worker that holds the player's input lock; has_vout
        and the other calls below take it too, so they must wait until the job is done.
        """
        session = self.window.session
        if getattr(session, "_job", None) is not None:
            return None
        return getattr(session, "_player", None)

    def _video_facts(self, snapshot: str = VIDEO_SNAPSHOT) -> Optional[dict[str, Any]]:
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
        path = self.out / snapshot
        facts["snapshot"] = (
            snapshot
            if player.video_take_snapshot(0, str(path), 0, 0) == 0 and path.exists()
            else None
        )
        return facts

    # ---- the scenario -----------------------------------------------------------------------

    def _scenario(self) -> Generator[int, None, None]:
        w, report = self.window, self.report
        drawer, banner, session = w.overlays.drawer, w.overlays.banner, w.session
        first, second = channel_urls(self.fixtures)
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

        if self.pipeline:
            yield from self._pipeline()
        if self.pip:
            yield from self._pip()

        # 6. Close; the parent judges the exit code and time, after_exit the helper and buffer.
        report.step("6. close")
        self._close()

    # ---- the live time-shift pipeline (--pipeline) --------------------------------------------

    def _pipeline(self) -> Generator[int, None, None]:
        """Channel 3 through the time-shift pipeline; the close after it is the last step."""
        w, report = self.window, self.report
        live, engine, session = w.live_session, w.engine, w.session
        facts = report.data["pipeline"]
        if live is None or engine is None:
            report.check(TUNED_CHECK, False, "no time-shift pipeline: libVLC is unavailable")
            return
        facts["timeshift_dir"] = str(engine.root_dir)
        live.timelineChanged.connect(self._on_timeline)

        # 1-2. Down zaps from Channel 2 to Channel 3, the paced live source: it plays from
        # the loopback stream once the pipeline session is ready.
        report.step("pipeline 1. tune Channel 3")
        self._key(Qt.Key.Key_Escape)  # close the drawer: Down zaps again
        yield from self._wait(lambda: not w.overlays.drawer.isVisible(), 2000)
        self._key(Qt.Key.Key_Down)
        tuned = yield from self._wait(self._pipeline_playing, LIVE_PLAY_TIMEOUT_MS)
        report.check(TUNED_CHECK, tuned, self._stream_facts())
        if not tuned:
            return  # the checks still missing fail the pipeline gate
        facts["helper_pid"] = self._helper_pid()
        buffers = [n for n in _entries(facts["timeshift_dir"]) if n.startswith(".qitv-timeshift-")]
        report.check(ON_DISK_CHECK, bool(buffers), f"{facts['timeshift_dir']}: {buffers}")
        yield WARM_UP_MS
        facts["video"] = self._video_facts(PIPELINE_VIDEO)  # inspection only, no check
        report.save()

        # 3. Pause for 4 s, then resume: it plays on time-shifted, at least 3 s behind.
        report.step("pipeline 3. pause and resume")
        self._key(Qt.Key.Key_Space)
        paused = yield from self._wait(
            lambda: session.state() is PlaybackState.PAUSED, STATE_CHANGE_MS
        )
        yield PAUSE_MS
        self._key(Qt.Key.Key_Space)
        playing = yield from self._wait(
            lambda: session.state() is PlaybackState.PLAYING, STATE_CHANGE_MS
        )
        state = yield from self._fresh(time.monotonic(), lambda s: not s["paused"])
        report.check(
            PAUSE_CHECK,
            paused and playing and state is not None and state["lag"] >= PAUSED_LAG_S,
            f"VLC paused {paused}, playing again {playing}; {self._describe(state)}",
        )
        self._capture(PIPELINE_SCREENSHOTS[0])  # Space shows the transport: the time-shift bar

        # 4. Go live (L): live within 6 s, at most a segment past stay-live's band (plus VLC's
        # open), and closing in by the end of the 6 s. VLC's position jitters by about half a
        # second, so the lag is judged against bounds, not step by step.
        report.step("pipeline 4. Go live")
        pressed = time.monotonic()
        self._key(Qt.Key.Key_L)
        segment = self._newest_span()  # what Go live chose among, a moment ago
        yield GO_LIVE_MS
        report.check(GO_LIVE_CHECK, *self._judge_go_live(pressed, segment))

        # 5. Seek back 10 s (←): the position moves back 8-12 s (segments are about 2 s).
        report.step("pipeline 5. seek back 10 s")
        buffered = yield from self._wait(lambda: self._history() >= HISTORY_S, HISTORY_TIMEOUT_MS)
        before = yield from self._fresh(time.monotonic())
        pressed = time.monotonic()
        self._key(Qt.Key.Key_Left)
        after = yield from self._fresh(pressed)
        low, high = SEEK_BACK
        moved = None if before is None or after is None else before["position"] - after["position"]
        report.check(
            SEEK_CHECK,
            moved is not None and low <= moved <= high,
            f"moved back {moved if moved is None else round(moved, 2)} s: before "
            f"{self._describe(before)}; after {self._describe(after)}"
            + ("" if buffered else f"; less than {HISTORY_S:g} s was buffered behind it"),
        )

        # 6. 1.5x while time-shifted (the transport's speed chip): applied, then back to 1x
        # once stay-live reaches live. Catching up gains only 0.5 s a second.
        report.step("pipeline 6. 1.5x speed")
        yield from self._wait(lambda: session.state() is PlaybackState.PLAYING, STATE_CHANGE_MS)
        requested = time.monotonic()
        w.overlays.transport.speedRequested.emit(SPEED)
        state = yield from self._fresh(
            requested, lambda s: s["rate"] == SPEED and s["mode"] == "catching_up", SPEED_TIMEOUT_MS
        )
        vlc_rate = yield from self._vlc_rate(SPEED)
        report.check(
            SPEED_CHECK,
            state is not None and vlc_rate is not None and abs(vlc_rate - SPEED) < RATE_TOLERANCE,
            f"VLC rate {vlc_rate}; {self._describe(state)}",
        )
        self._move_mouse()
        yield from self._wait(w.overlays.transport.isVisible, TRANSPORT_TIMEOUT_MS)
        yield 200
        self._capture(PIPELINE_SCREENSHOTS[1])

        report.step("pipeline 6. back at live")
        state = yield from self._fresh(
            requested, lambda s: s["mode"] == "live" and s["rate"] == 1.0, CATCH_UP_TIMEOUT_MS
        )
        took = time.monotonic() - requested
        vlc_rate = yield from self._vlc_rate(1.0)
        report.check(
            LIVE_AGAIN_CHECK,
            state is not None and vlc_rate is not None and abs(vlc_rate - 1.0) < RATE_TOLERANCE,
            f"after {took:.1f} s: VLC rate {vlc_rate}; {self._describe(state)}",
        )

        # 7. Down to Channel 4, and Up back as soon as its upstream delivers: each opens only
        # once the other's upstream is closed. The parent judges it by what the sources saw.
        report.step("pipeline 7. zap to Channel 4 and back")
        first, second = self.live_urls[0], self.live_urls[1]
        self._key(Qt.Key.Key_Down)
        opened = yield from self._wait(lambda: self._delivering(second), ZAP_OPEN_TIMEOUT_MS)
        self._key(Qt.Key.Key_Up)
        back = yield from self._wait(
            lambda: self._tuned(first) and self._pipeline_playing(), LIVE_PLAY_TIMEOUT_MS
        )
        facts["zap"] = {"opened": opened, "back": back}
        report.save()

    def _on_timeline(self, state: dict) -> None:
        self.timeline.append((time.monotonic(), dict(state)))

    def _fresh(
        self,
        since: float,
        predicate: Callable[[dict[str, Any]], bool] = lambda _state: True,
        timeout_ms: int = FRESH_TIMELINE_MS,
    ) -> Generator[int, None, Optional[dict[str, Any]]]:
        """The first timeline state emitted after `since` that satisfies `predicate`."""
        found: list[dict[str, Any]] = []

        def seen() -> bool:
            for at, state in self.timeline:
                if at > since and predicate(state):
                    found.append(state)
                    return True
            return False

        yield from self._wait(seen, timeout_ms)
        return found[0] if found else None

    def _judge_go_live(self, pressed: float, segment: Optional[float]) -> tuple[bool, str]:
        """The Go live check over the 6 s after L; records its bound and lag series."""
        end = pressed + GO_LIVE_MS / 1000
        window = [(at - pressed, s) for at, s in self.timeline if pressed < at <= end]
        facts = self.report.data["pipeline"]
        if not window or segment is None:
            facts["go_live"] = {"segment": segment, "series": []}
            return False, f"{len(window)} timeline states after L; newest complete span {segment}"
        target = float(window[0][1]["target"])
        bound = target + GO_LIVE_BAND_S + segment + GO_LIVE_OPEN_S
        landed = next(
            ((t, s) for t, s in window if s["mode"] == "live" and s["lag"] <= bound), None
        )
        last_t, last = window[-1]
        closing = last["rate"] > 1.0 or last["lag"] <= target + GO_LIVE_BAND_S
        lags = [round(float(s["lag"]), 3) for _, s in window]
        lowest = min(lags)
        facts["go_live"] = {
            "target": target,
            "segment": round(segment, 3),
            "bound": round(bound, 3),
            "landed_after_s": round(landed[0], 3) if landed is not None else None,
            "min_lag": round(lowest, 3),
            "end": {
                "after_s": round(last_t, 3),
                "lag": round(last["lag"], 3),
                "rate": last["rate"],
                "mode": last["mode"],
            },
            "series": [[round(t, 3), round(s["lag"], 3), s["mode"], s["rate"]] for t, s in window],
        }
        bounds = (
            f"bound {bound:.3f} = target {target:g} + band {GO_LIVE_BAND_S:g} + segment "
            f"{segment:.3f} + open {GO_LIVE_OPEN_S:g}"
        )
        if landed is None:
            arrived = "never live within the bound"
        else:
            arrived = f"live at lag {landed[1]['lag']:.3f} after {landed[0]:.2f} s"
        detail = (
            f"{arrived} ({bounds}); min lag {lowest:.3f}; after {last_t:.2f} s lag "
            f"{last['lag']:.3f}, rate {last['rate']}, mode {last['mode']}"
            f"{'' if closing else ' (not closing in)'}; lags: {lags}"
        )
        return landed is not None and closing, detail

    def _newest_span(self) -> Optional[float]:
        """The newest complete span's length (LiveSession keeps its session id private)."""
        engine, sid = self.window.engine, getattr(self.window.live_session, "_sid", None)
        spans = engine.spans(sid) if engine is not None and sid is not None else []
        return spans[-1].end - spans[-1].start if spans else None

    def _history(self) -> float:
        """Seconds buffered behind the latest position."""
        if not self.timeline:
            return 0.0
        state = self.timeline[-1][1]
        return float(state["position"]) - float(state["start"])

    @staticmethod
    def _describe(state: Optional[dict[str, Any]]) -> str:
        if state is None:
            return "no such timeline state"
        keys = ("mode", "rate", "paused", "lag", "target", "position", "start", "live_edge")
        return ", ".join(
            f"{k} {round(state[k], 3) if isinstance(state[k], float) else state[k]}" for k in keys
        )

    def _pipeline_playing(self, live: Any = None, session: Any = None) -> bool:
        """The live session plays from the loopback stream (by default the main slot's)."""
        if live is None or session is None:
            live, session = self.window.live_session, self.window.session
        url = session.url or ""
        return (
            live.is_pipeline()
            and session.kind is PlayKind.LIVE_PIPELINE
            and urlsplit(url).hostname == "127.0.0.1"
            and _STREAM_PATH.search(urlsplit(url).path) is not None
            and session.state() is PlaybackState.PLAYING
        )

    def _tuned(self, url: str) -> bool:
        """The live session's channel is `url` (LiveSession keeps it private)."""
        return getattr(self.window.live_session, "_url", None) == url

    def _delivering(self, url: str) -> bool:
        """The live session tuned `url`, and its buffer holds bytes from that upstream."""
        engine, sid = self.window.engine, getattr(self.window.live_session, "_sid", None)
        if not self._tuned(url) or engine is None or sid is None:
            return False
        return bool(engine.snapshot(sid).get("bytes"))

    def _stream_facts(self) -> str:
        live, session = self.window.live_session, self.window.session
        return (
            f"mode {live.mode!r}, {session.state().value} {session.kind} from "
            f"{_redacted(session.url)}"
        )

    def _helper_pid(self) -> Optional[int]:
        """The live helper's pid: the engine keeps it private, so it is read here only."""
        supervisor = getattr(self.window.engine, "_supervisor", None)
        return getattr(getattr(supervisor, "_process", None), "pid", None)

    def _vlc_rate(self, expected: float) -> Generator[int, None, Optional[float]]:
        """libVLC's playback rate once it is `expected` (or after a few seconds)."""
        rates: list[float] = []

        def read() -> bool:
            player = self._player()
            if player is not None:
                rates.append(float(player.get_rate()))
            return bool(rates) and abs(rates[-1] - expected) < RATE_TOLERANCE

        yield from self._wait(read, SPEED_TIMEOUT_MS)
        return rates[-1] if rates else None

    # ---- picture in picture and swap (--pip) --------------------------------------------------

    def _pip(self) -> Generator[int, None, None]:
        """Channel 3 (A) small beside Channel 4 (B), swapped, paused, levelled, moved and
        closed (module docstring); the close after it is the last step."""
        w, report = self.window, self.report
        sessions, engine = w.sessions, w.engine
        facts: dict[str, Any] = report.data.setdefault("pip", {})
        facts["started_at"] = time.time()  # the parent judges the sources from here on
        report.save()
        if sessions is None or engine is None:
            report.check(PIP_OPEN_CHECK, False, "no picture-in-picture: libVLC is unavailable")
            return
        pipeline = report.data["pipeline"]
        pipeline.setdefault("timeshift_dir", str(engine.root_dir))
        self._count_plays(sessions)
        sessions.slotTimeline.connect(self._on_slot_timeline)
        engine.sessionUpdated.connect(self._on_helper_update)
        a_url, b_url = self.live_urls[0], self.live_urls[1]

        # 1. A, then B; P opens the channel before B small: A.
        report.step("pip 1. tune Channel 3, then Channel 4")
        drawer = w.overlays.drawer
        if drawer.isVisible():
            self._key(Qt.Key.Key_Escape)  # Down zaps again
            yield from self._wait(lambda: not drawer.isVisible(), 2000)
        if not self._slot_tuned(sessions.large(), a_url):
            self._key(Qt.Key.Key_Down)  # Channel 2 → 3 (without --pipeline)
        tuned_a = yield from self._wait(
            lambda: self._slot_playing(sessions.large(), a_url), LIVE_PLAY_TIMEOUT_MS
        )
        if pipeline.get("helper_pid") is None:
            pipeline["helper_pid"] = self._helper_pid()
        self._key(Qt.Key.Key_Down)
        tuned_b = yield from self._wait(
            lambda: self._slot_playing(sessions.large(), b_url), LIVE_PLAY_TIMEOUT_MS
        )
        report.step("pip 1. open picture-in-picture")
        self._key(Qt.Key.Key_P)

        def both_open() -> bool:
            small = sessions.small()
            return (
                small is not None
                and self._slot_playing(small, a_url)
                and self._slot_playing(sessions.large(), b_url)
            )

        opened = yield from self._wait(both_open, PIP_OPEN_TIMEOUT_MS)
        report.check(
            PIP_OPEN_CHECK,
            tuned_a and tuned_b and opened,
            f"{LIVE_CHANNEL} played first: {tuned_a}, then {ZAP_CHANNEL}: {tuned_b}; "
            f"{self._slots_facts()}",
        )
        if not opened:
            return  # the checks still missing fail the PiP gate
        a_slot, b_slot = sessions.small(), sessions.large()
        facts["slots"] = {LIVE_CHANNEL: a_slot.name, ZAP_CHANNEL: b_slot.name}
        yield from self._check_mute_held(a_slot)

        # 2. A quarter of the width in the bottom-right corner, the label over it.
        report.step("pip 2. the small picture's geometry")
        yield from self._check_pip_geometry(PIP_STEPS[0])
        self._capture_pip(PIP_SCREENSHOTS[0])

        # 3. S: A is large, nothing restarts, and the sound follows it.
        report.step("pip 3. swap")
        before = self._play_counts()
        swap_from = time.time()
        self._key(Qt.Key.Key_S)
        swapped = yield from self._wait(self._large_is(a_slot), SWAP_TIMEOUT_MS)
        yield SWAP_SETTLE_MS
        after = self._play_counts()
        facts["swap_window"] = [swap_from, time.time()]  # the parent counts reconnects in it

        def both_playing() -> bool:
            return self._slot_playing(a_slot, a_url) and self._slot_playing(b_slot, b_url)

        playing = both_playing()
        facts["play_counts"] = {"before_swap": before, "after_swap": after}
        report.check(
            SWAP_CHECK,
            swapped and playing and before == after,
            f"large: {sessions.large().name} ({LIVE_CHANNEL} plays in {a_slot.name}); both "
            f"playing: {playing}; plays and media opens before {before}, after {after}",
        )
        self._check_audio(SWAP_AUDIO_CHECK, {a_slot.name: (False, None), b_slot.name: (True, None)})
        self._capture_pip(PIP_SCREENSHOTS[1])

        # 4. A paused for 4 s, then two swaps: A, large again, is still behind live.
        report.step("pip 4. pause, then swap twice")
        playback = a_slot.playback
        self._key(Qt.Key.Key_Space)
        paused = yield from self._wait(
            lambda: playback.state() is PlaybackState.PAUSED, STATE_CHANGE_MS
        )
        yield PAUSE_MS
        self._key(Qt.Key.Key_Space)
        resumed = yield from self._wait(
            lambda: playback.state() is PlaybackState.PLAYING, STATE_CHANGE_MS
        )
        swaps: list[bool] = []
        for large in (b_slot, a_slot):
            yield from self._wait(both_playing, STATE_CHANGE_MS)  # a swap waits for an open
            self._key(Qt.Key.Key_S)
            swapped_to = yield from self._wait(self._large_is(large), SWAP_TIMEOUT_MS)
            swaps.append(swapped_to)
        state = yield from self._slot_fresh(
            a_slot.name, time.monotonic(), lambda s: not s["paused"]
        )
        report.check(
            SHIFTED_CHECK,
            paused
            and resumed
            and all(swaps)
            and sessions.large() is a_slot
            and state is not None
            and state["lag"] >= PAUSED_LAG_S,
            f"VLC paused {paused}, playing again {resumed}, swapped {swaps}, large "
            f"{sessions.large().name}; {self._describe(state)}",
        )

        # The two players' own volume and mute (Windows: per-player DirectSound buffers).
        # The small one's first: with one audio session for both, the large one's would win.
        report.step("pip 4. a volume and a mute for each player")
        small, large = b_slot, a_slot
        sessions.set_level(small.name, SMALL_LEVEL)
        sessions.set_muted(small.name, True)
        sessions.set_level(large.name, LARGE_LEVEL)
        sessions.set_muted(large.name, False)
        yield AUDIO_SETTLE_MS
        self._check_audio(
            LEVELS_CHECK, {large.name: (False, LARGE_LEVEL), small.name: (True, SMALL_LEVEL)}
        )

        # 5. Move and resize: the small picture and its label follow.
        report.step("pip 5. move and resize")
        before_rect = w.geometry()
        changed_at = time.monotonic()
        w.move(w.pos().x() - 40, w.pos().y() - 30)
        w.resize(1000, 580)
        yield from self._settle(changed_at)
        after_rect = w.geometry()
        report.check(  # small screens may clamp the size: it only has to change
            PIP_WINDOW_CHANGE,
            after_rect.topLeft() != before_rect.topLeft()
            and after_rect.size() != before_rect.size(),
            f"{_rect(before_rect)} -> {_rect(after_rect)}",
        )
        yield from self._check_pip_geometry(PIP_STEPS[1])

        # 6. P closes the small picture (B): A plays on alone, B's ticket and session go.
        report.step("pip 6. close picture-in-picture")
        provider = w.catalog.current_provider_name()
        closing, stays = sessions.small(), sessions.large()
        tuner = closing.tuner
        self._key(Qt.Key.Key_P)
        closed = yield from self._wait(
            lambda: not sessions.pip_open() and closing.playback.state() is PlaybackState.IDLE,
            SWAP_TIMEOUT_MS,
        )
        released = yield from self._wait(
            lambda: not tuner.releasing() and PIP_PURPOSE not in w.budget.holders(provider),
            RELEASE_WAIT_MS,
        )
        frame, label = w.pip_frame, w.pip_label
        # The large picture must still be on screen: it plays on whichever surface the swap
        # left it (Batch B C1: closing a swapped PiP blacked it out).
        surface = w.pip_surface if stays.name == "pip" else w.surface
        alone = (
            closed
            and sessions.large() is stays
            and self._slot_playing(stays, a_url)
            and frame.small_rect().isEmpty()
            and not label.isVisible()
            and surface.isVisible()
        )
        report.check(
            ONE_SESSION_CHECK,
            alone,
            f"PiP open {sessions.pip_open()}; {stays.name} ({LIVE_CHANNEL}) "
            f"{stays.playback.state().value}, {closing.name} ({ZAP_CHANNEL}) "
            f"{closing.playback.state().value}; small rect {_rect(frame.small_rect())}, label "
            f"{'shown' if label.isVisible() else 'hidden'}; {stays.name}'s surface "
            f"{'visible' if surface.isVisible() else 'HIDDEN'}",
        )
        holders = w.budget.holders(provider)
        report.check(
            TICKET_CHECK,
            released and holders == [MAIN_PURPOSE],
            f"the provider's budget holders: {holders}; the closed player still releasing: "
            f"{tuner.releasing()}",
        )
        known = getattr(engine, "_sessions", {})  # the helper confirms each close
        single = yield from self._wait(lambda: len(known) == 1, RELEASE_WAIT_MS)
        since = time.monotonic()
        yield HELPER_WATCH_MS
        updated = {sid for at, sid in self.helper_updates if at >= since}
        large_sid = getattr(stays.live, "_sid", None)
        report.check(
            HELPER_SESSION_CHECK,
            single and large_sid is not None and updated == {large_sid},
            f"sessions the engine holds: {len(known)}; sessions the helper updated in "
            f"{HELPER_WATCH_MS} ms: {len(updated)}, {LIVE_CHANNEL}'s among them: "
            f"{large_sid in updated}",
        )
        facts["play_counts"]["end"] = self._play_counts()
        report.save()

    def _count_plays(self, sessions: Any) -> None:
        """Count each player's plays and libVLC media opens (a restart is one of either)."""
        for slot in (sessions.large(), sessions.other()):
            counts = self.plays.setdefault(slot.name, {"play": 0, "open": 0})
            playback = slot.playback
            for method, key in (("play", "play"), ("_open", "open")):
                original = getattr(playback, method)

                def counted(*args: Any, _call=original, _key=key, _counts=counts, **kwargs: Any):
                    _counts[_key] += 1
                    return _call(*args, **kwargs)

                setattr(playback, method, counted)  # PlaybackSession looks them up on itself

    def _play_counts(self) -> dict[str, dict[str, int]]:
        return {name: dict(counts) for name, counts in self.plays.items()}

    def _on_slot_timeline(self, name: str, state: dict) -> None:
        self.slot_timeline.append((time.monotonic(), name, dict(state)))

    def _on_helper_update(self, sid: str, _snapshot: dict) -> None:
        self.helper_updates.append((time.monotonic(), sid))

    def _slot_fresh(
        self,
        name: str,
        since: float,
        predicate: Callable[[dict[str, Any]], bool] = lambda _state: True,
        timeout_ms: int = FRESH_TIMELINE_MS,
    ) -> Generator[int, None, Optional[dict[str, Any]]]:
        """The first timeline state of slot `name` emitted after `since` satisfying `predicate`."""
        found: list[dict[str, Any]] = []

        def seen() -> bool:
            for at, slot, state in self.slot_timeline:
                if slot == name and at > since and predicate(state):
                    found.append(state)
                    return True
            return False

        yield from self._wait(seen, timeout_ms)
        return found[0] if found else None

    @staticmethod
    def _slot_tuned(slot: Any, url: str) -> bool:
        """The slot's live session's channel is `url` (LiveSession keeps it private)."""
        return getattr(slot.live, "_url", None) == url

    def _large_is(self, slot: Any) -> Callable[[], bool]:
        sessions = self.window.sessions
        return lambda: sessions.large() is slot

    def _slot_playing(self, slot: Any, url: str) -> bool:
        return self._slot_tuned(slot, url) and self._pipeline_playing(slot.live, slot.playback)

    def _slots_facts(self) -> str:
        sessions = self.window.sessions
        channels = dict(zip(self.live_urls, (LIVE_CHANNEL, ZAP_CHANNEL)))
        parts = []
        for role, slot in (("large", sessions.large()), ("small", sessions.small())):
            if slot is None:
                parts.append(f"{role}: none")
                continue
            playback = slot.playback
            channel = channels.get(str(getattr(slot.live, "_url", "")), "another channel")
            parts.append(
                f"{role} {slot.name}: {channel}, mode {slot.live.mode!r}, "
                f"{playback.state().value} {playback.kind} from {_redacted(playback.url)}"
            )
        return "; ".join(parts)

    @staticmethod
    def _audio_of(playback: Any) -> Optional[tuple[int, int]]:
        """libVLC's (mute, volume) of a player: -1 without an audio output; None while a
        worker holds the player."""
        player = getattr(playback, "_player", None)
        if player is None or getattr(playback, "_job", None) is not None:
            return None
        return int(player.audio_get_mute()), int(player.audio_get_volume())

    def _judge_vlc_audio(self, ok: bool, detail: str) -> tuple[bool, str]:
        """libVLC's read-back only counts where an audio output keeps a volume and a mute."""
        if self.report.data.get("muted"):
            return True, (
                f"informational (muted run: libVLC's dummy audio output keeps no volume or "
                f"mute): {detail}"
            )
        if not ok:
            reason = _no_audio_reason(self.out / VLC_LOG)
            if reason is not None:
                return True, f"informational ({reason}): {detail}"
        return ok, detail

    def _check_mute_held(self, slot: Any) -> Generator[int, None, None]:
        """The small picture opened muted: the mute holds once its media plays."""
        sessions, playback = self.window.sessions, slot.playback
        applied = yield from self._wait(
            lambda: not getattr(playback, "_audio_due", True), AUDIO_APPLY_MS
        )
        readings: list[Optional[tuple[int, int]]] = []
        for _ in range(AUDIO_SAMPLES):
            readings.append(self._audio_of(playback))
            yield 100
        seen = [r for r in readings if r is not None]
        muted = sessions.muted(slot.name)
        ok, detail = self._judge_vlc_audio(
            bool(seen) and all(mute == 1 for mute, _ in seen),
            f"wanted muted, the manager says {'muted' if muted else 'unmuted'}; applied to the "
            f"audio output: {applied}; libVLC (mute, volume): {readings}",
        )
        self.report.data["pip"]["mute_held"] = readings
        self.report.check(MUTE_HELD_CHECK, muted and ok, detail)

    def _check_audio(self, name: str, wanted: dict[str, tuple[bool, Optional[int]]]) -> None:
        """Each slot's (muted, level or None) as the manager and libVLC say."""
        sessions = self.window.sessions
        slots = {slot.name: slot for slot in (sessions.large(), sessions.other())}
        manager_ok = vlc_ok = True
        parts, readings = [], {}
        for slot_name, (muted, level) in wanted.items():
            got = self._audio_of(slots[slot_name].playback)
            readings[slot_name] = got
            manager_ok = manager_ok and sessions.muted(slot_name) == muted
            manager_ok = manager_ok and (level is None or sessions.level(slot_name) == level)
            # libVLC's level is the manager's, whether or not the check names one (Review I1)
            vlc_ok = vlc_ok and reading_matches(muted, sessions.level(slot_name), got)
            parts.append(
                f"{slot_name}: wanted {'muted' if muted else 'unmuted'}"
                + ("" if level is None else f" at {level}")
                + f", the manager says {'muted' if sessions.muted(slot_name) else 'unmuted'} at "
                f"{sessions.level(slot_name)}, libVLC (mute, volume) {got}"
            )
        ok, detail = self._judge_vlc_audio(vlc_ok, "; ".join(parts))
        self.report.data["pip"].setdefault("audio", {})[name] = readings
        self.report.check(name, manager_ok and ok, detail)

    def _check_pip_geometry(self, when: str) -> Generator[int, None, None]:
        """The small picture's rect once it holds still, and the PiP label over it."""
        w = self.window
        frame, page, host, label = w.pip_frame, w.player_page, w.host, w.pip_label
        seen: list[QRect] = []

        def still() -> bool:
            rect = frame.small_rect()
            held = bool(seen) and seen[-1] == rect
            seen.append(QRect(rect))
            return held and not frame.lifting()

        yield from self._wait(still, GEOMETRY_TIMEOUT_MS)
        rect, size = frame.small_rect(), page.size()
        lift = int(getattr(frame, "_lift", 0))  # raised above the transport while it shows
        expected = lifted_rect(size, DEFAULT_CORNER, lift)
        width = round(size.width() * WIDTH_FRACTION)
        self.report.check(
            pip_corner_check(when),
            not rect.isEmpty()
            and rect.width() == width
            and frame.corner() == DEFAULT_CORNER
            and rect == expected,
            f"small picture {_rect(rect)} in a {size.width()}x{size.height()} player; expected "
            f"{_rect(expected)} ({f'raised {lift} px above the transport' if lift else 'unraised'}"
            f"), width {width}, corner {frame.corner()}",
        )
        in_host = QRect(host.mapFromGlobal(page.mapToGlobal(rect.topLeft())), rect.size())
        self.report.check(
            pip_label_check(when),
            host.isVisible() and label.isVisible() and label.geometry() == in_host,
            f"label {_rect(label.geometry())} {'shown' if label.isVisible() else 'hidden'}; the "
            f"small picture in host coordinates {_rect(in_host)}",
        )

    def _capture_pip(self, name: str) -> None:
        """Both players' video where their surfaces are, under the overlay host's rendering."""
        w = self.window
        sessions, host, page = w.sessions, w.host, w.player_page
        pixmap = host.grab()
        image = QImage(pixmap.size(), QImage.Format.Format_ARGB32)
        image.setDevicePixelRatio(pixmap.devicePixelRatio())
        image.fill(0xFF000000)
        painter = QPainter(image)
        layers = [(sessions.large(), host.rect())]
        small = sessions.small()
        if small is not None:
            rect = w.pip_frame.small_rect()
            target = QRect(host.mapFromGlobal(page.mapToGlobal(rect.topLeft())), rect.size())
            layers.append((small, target))
        videos = {}
        for slot, target in layers:
            frame = self._snapshot(slot.playback)
            videos[slot.name] = frame is not None
            if frame is not None:
                painter.drawImage(QRectF(target), frame)
        painter.drawPixmap(0, 0, pixmap)
        painter.end()
        self.report.data["screenshots"][name] = image.save(str(self.out / name))
        self.report.data["pip"].setdefault("screenshot_video", {})[name] = videos
        self.report.save()

    def _snapshot(self, playback: Any) -> Optional[QImage]:
        """libVLC's snapshot of the player's video (None without a video output)."""
        player = getattr(playback, "_player", None)
        if player is None or getattr(playback, "_job", None) is not None:
            return None
        path = self.out / ".pip-snapshot.png"
        path.unlink(missing_ok=True)
        try:
            if player.video_take_snapshot(0, str(path), 0, 0) != 0 or not path.exists():
                return None
            image = QImage(str(path))
            return None if image.isNull() else image
        finally:
            path.unlink(missing_ok=True)

    def _close(self) -> None:
        report = self.report
        try:  # while libVLC still holds what it plays with
            facts = _libvlc_facts()
        except Exception as e:  # diagnostics only: never stop the close
            facts = {"error": repr(e)}
        report.data["libvlc_loaded"] = facts
        if _frozen():
            report.check(BUNDLED_VLC_CHECK, "error" not in facts and _bundled(facts), facts)
        report.data["close_requested_at"] = time.time()
        report.save()
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
        if self.live_urls:
            self._after_pipeline()
        shots = report.data["screenshots"]
        expected = (
            SCREENSHOTS
            + (PIPELINE_SCREENSHOTS if self.pipeline else ())
            + (PIP_SCREENSHOTS if self.pip else ())
        )
        report.check("the screenshots are saved", all(shots.get(name) for name in expected), shots)
        report.check("nothing touches the network", not attempts, attempts)
        report.data["overlay_shows_and_hides"] = len(self.resyncs.calls)
        report.save()

    def _after_pipeline(self) -> None:
        """Step 7: the close stopped the live helper and removed its buffer."""
        report, facts = self.report, self.report.data["pipeline"]
        pid = facts.get("helper_pid")
        if pid is None:
            report.check(NO_HELPER_CHECK, False, "the helper's pid was never recorded")
        else:
            alive = _process_alive(pid)
            report.check(NO_HELPER_CHECK, not alive, f"pid {pid} {'runs' if alive else 'is gone'}")
        directory = facts.get("timeshift_dir")
        left = _entries(directory)
        report.check(
            EMPTY_DIR_CHECK,
            directory is not None and not left,
            f"{directory}: {left}" if directory else "the pipeline never ran",
        )
        facts["timeline_states"] = len(self.timeline)


# ---- entry point ------------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--out", required=True, help="directory for smoke.json and screenshots")
    parser.add_argument(
        "--seconds",
        type=float,
        default=DEFAULT_SECONDS,
        help="how long to watch Channel 2 play after the zap, at least 1 (default: %(default)s)",
    )
    parser.add_argument(
        "--pipeline",
        action="store_true",
        help="also play a live channel through the time-shift pipeline",
    )
    parser.add_argument(
        "--pip",
        action="store_true",
        help="also open picture-in-picture, swap and close it (two live channels)",
    )
    parser.add_argument(
        "--fixtures",
        help="the directory with smoke.mp4 and live.ts (required in the packaged app, which "
        "has none; default: the checkout's tests/fixtures)",
    )
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--live-url", action="append", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.fixtures is None and _frozen():
        parser.error("--fixtures is required in the packaged app")
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.FileHandler(out / (APP_LOG if args.child else RUNNER_LOG), "w", encoding="utf-8")
    ]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )
    fixtures = Path(args.fixtures or FIXTURE_DIR).resolve()
    seconds = max(MIN_SECONDS, args.seconds)
    if args.child:
        return run_app(out, seconds, fixtures, args.live_url or [], args.pipeline, args.pip)
    try:
        return run_smoke(out, seconds, fixtures, args.pipeline, args.pip)
    except Exception:
        logger.exception("The smoke test could not run")
        return 2


if __name__ == "__main__":
    sys.exit(main())
