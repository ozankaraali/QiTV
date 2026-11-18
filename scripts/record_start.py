"""A recorded start: QiTV's start under Xvfb, openbox and a compositor, filmed and checked.

    uv run python scripts/record_start.py SCENARIO [--check] [--out DIR] [--app PATH]

Developer tool, run on a Linux box with Xvfb, openbox, xcompmgr, xdotool, xwininfo, xprop and
ffmpeg (never on the owner's Mac, never in CI). It starts QiTV muted with a throwaway settings
folder, films the screen at 30 fps with `ffmpeg -f x11grab`, samples the windows every 100 ms,
and says whether the start reads as one motion. SCENARIO is one of:

    plain        a source start with no file
    file         a source start with tests/fixtures/smoke.mp4
    handover     QiTV running, then a second start with a file (it hands over)
    reduced      a source start with a file and QITV_REDUCED_MOTION=1
    onefile      the one-file build (--app, default dist/qitv), no file
    onefile-file the one-file build with a file

Windows are told apart by name and size from `xwininfo -root -tree` and by visibility from
`xdotool search --onlyvisible`: the splash helper's window is "qitv-splash" (240x240), the main
window is titled "QiTV", the OverlayHost (a Tool window that follows it) is the large top-level
"main.py"; PyInstaller's still (the Tk window) is the small square one. The overlay is never
counted as a second appearance of the window.

--check asserts (exit 1 when one fails):
    the splash shows within 1.5 s of the start (a one-file build: of the exec, the still counts);
    the main window appears once, with one id;
    a one-file start: from the still (the outline) the glyph never goes out, and it fills
    before the open;
    the visible window count (splash, still, main) never goes 1 -> 0 -> 1, on the windows and on
        the filmed frames (an empty frame between the first mark and the shown window);
    the splash is gone at most 0.5 s after the window is fully shown (its opacity is 1);
    a hand-over start shows no second window, and its splash is gone 0.5 s after it ends;
    reduced motion: the splash never moves.

The pure analysis (`analyse`) takes the sampled timeline, so tests/test_record_start.py feeds it
synthetic ones. Outputs go to --out: `task5-<scenario>.mp4` (8 s), `task5-<scenario>-sheet.png`
(at most 2000 px wide) and `task5-<scenario>.json` (the samples and the verdicts).
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]

SCENARIOS = ("plain", "file", "handover", "reduced", "onefile", "onefile-file")
FIXTURE = ROOT / "tests" / "fixtures" / "smoke.mp4"
FIXTURE_SECOND = ROOT / "tests" / "fixtures" / "film" / "h264_aac.mkv"

SCREEN = (1280, 800)
FPS = 30
CLIP_S = 8.0
CLIP_LEAD_S = 0.5
SAMPLE_S = 0.1
SPLASH_NAME = "qitv-splash"
SPLASH_PX = 240
MAIN_NAME = "QiTV"
OVERLAY_NAME = "main.py"
OVERLAY_MIN_PX = 300
STILL_PX = 100
SHEET_FRAMES = 30
SHEET_COLUMNS = 10
SHEET_TILE_PX = 200
GRAB_W, GRAB_H = 160, 100  # the frames the analysis reads: gray, 1/8 of the screen

SPLASH_WITHIN_S = 1.5
SPLASH_AFTER_FULL_S = 0.5
OCCUPIED_DIFF = 0.3  # mean gray levels away from the empty screen: a mark, a window
FULL_OPACITY = 0.999
# The glyph's brightness: the mean gray of the middle of the tile in the small frames. The still
# is the mark's outline drawn whole, unfilled (ruling P2-12): from it to the open the level never
# falls below the outline's (out, it falls to the dark tile's), and the fill brings it to the
# filled level before the open.
GLYPH_BOX = 5  # half-side in small-frame pixels: inside the 12 px tile
GLYPH_KEEPS = 0.9  # of the outline's level (the still's, filmed alone)
GLYPH_FILLED = 1.6  # times the outline's level: filled
GLYPH_SETTLE_S = 0.1  # the still's first filmed frame may be half drawn


@dataclass
class Sample:
    """What the screen held at one moment (seconds, the epoch)."""

    t: float
    splash: bool = False  # the helper's window "qitv-splash" is visible
    still: bool = False  # PyInstaller's still (the small square Tk window) is visible
    mains: tuple[str, ...] = ()  # ids of the visible main windows ("QiTV")
    overlays: tuple[str, ...] = ()  # ids of the visible overlay top-levels
    opacity: Optional[float] = None  # the main window's opacity (None: not set, i.e. 1)
    splash_pos: Optional[tuple[int, int]] = None
    note: str = ""  # what a blip was (the main window's map state when it left the list)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class Events:
    """What the run itself knows (the epoch)."""

    t_exec: float
    t_second: Optional[float] = None
    t_second_exit: Optional[float] = None
    t_end: Optional[float] = None  # when the quit was asked: samples after it are not judged
    reduced: bool = False
    onefile: bool = False
    frame_times: list[float] = field(default_factory=list)
    frame_diffs: list[float] = field(default_factory=list)  # mean gray distance from empty
    glyph_levels: list[float] = field(default_factory=list)  # the glyph's brightness per frame


@dataclass
class Report:
    checks: list[Check]
    timeline: dict[str, Any]

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)


def _count(s: Sample) -> int:
    """Windows that make the start: the splash, PyInstaller's still, and the main window."""
    return int(s.splash) + int(s.still) + len(s.mains)


def _first(samples: Sequence[Sample], pred) -> Optional[Sample]:
    return next((s for s in samples if pred(s)), None)


def _rel(t: Optional[float], t0: float) -> Optional[float]:
    return None if t is None else round(t - t0, 3)


def analyse(samples: Sequence[Sample], ev: Events, scenario: str = "plain") -> Report:
    """The verdicts on a sampled start. Pure: no screen, no clock."""
    handover = scenario == "handover"
    t0 = ev.t_second if handover and ev.t_second is not None else ev.t_exec
    end = ev.t_end if ev.t_end is not None else float("inf")
    live = [s for s in samples if s.t <= end]
    # a hand-over's second start is judged from its own moment; the first start from its own
    first_part = [s for s in live if not handover or ev.t_second is None or s.t < ev.t_second]
    part = [s for s in live if s.t >= t0] if handover else first_part
    checks: list[Check] = []

    seen = _first(part, lambda s: s.splash or s.still)
    shown = _first(first_part, lambda s: bool(s.mains))
    full = None
    if shown is not None:
        full = _first(
            [s for s in first_part if s.t >= shown.t],
            lambda s: bool(s.mains) and (s.opacity is None or s.opacity >= FULL_OPACITY),
        )
    last_splash = None
    for s in part:
        if s.splash or s.still:
            last_splash = s
    gone = None
    if last_splash is not None:
        gone = _first(part, lambda s: s.t > last_splash.t)
    timeline: dict[str, Any] = {
        "scenario": scenario,
        "splash_first": _rel(seen.t if seen else None, t0),
        "main_first": _rel(shown.t if shown else None, ev.t_exec),
        "main_full": _rel(full.t if full else None, ev.t_exec),
        "splash_gone": _rel(gone.t if gone else None, t0),
    }
    if handover:
        timeline["second_exit"] = _rel(ev.t_second_exit, t0)

    # 1. the splash shows within 1.5 s of the start
    if handover:
        if seen is None:
            checks.append(Check("splash_shown", True, "no splash: the hand-over answered first"))
        else:
            checks.append(
                Check(
                    "splash_shown",
                    seen.t - t0 <= SPLASH_WITHIN_S,
                    f"the splash is up {seen.t - t0:.2f} s after the second start",
                )
            )
    elif seen is None:
        checks.append(Check("splash_shown", False, "no splash or still was ever on screen"))
    else:
        dt = seen.t - t0
        checks.append(
            Check(
                "splash_shown",
                dt <= SPLASH_WITHIN_S,
                f"the {'still' if seen.still else 'splash'} is up {dt:.2f} s after the start"
                f" (limit {SPLASH_WITHIN_S} s)",
            )
        )

    # 2. the main window appears once
    ids = {m for s in first_part for m in s.mains}
    # a window at opacity 0 is not on screen: only its visible time counts as an appearance
    rises = _rises([_on_screen(s) for s in first_part])
    if handover:
        later = {m for s in part for m in s.mains}
        extra = later - ids
        checks.append(
            Check(
                "main_once",
                len(ids) == 1 and not extra and all(s.mains for s in part),
                f"the running window {sorted(ids)} stays the only one; "
                f"the second start added {sorted(extra) or 'none'}",
            )
        )
    else:
        checks.append(
            Check(
                "main_once",
                len(ids) == 1 and rises == 1,
                f"{rises} appearance(s), {len(ids)} window id(s) {sorted(ids)}",
            )
        )

    # 3. the visible count never goes 1 -> 0 -> 1 (windows, then the filmed frames)
    span = part if handover else first_part
    counts = [_count(s) for s in span]
    dip = _dip(counts)
    note = ""
    if handover:
        dip = dip or any(c == 0 for c in counts)
    elif dip and _empty_frames_near(ev, span):
        note = " (the frames show an empty screen there too)"
    elif dip and ev.frame_times:
        dip = False  # a sampler race: the filmed frames never go empty there
        note = " (a 100 ms sample saw none, the filmed frames show the screen occupied)"
    checks.append(Check("count_no_dip", not dip, "window count over time: " + _runs(counts) + note))
    gap = _frame_gap(
        ev, samples, shown_t=(full.t if full else None), first_t=(seen.t if seen else None)
    )
    if handover or gap is None:
        checks.append(Check("frames_no_gap", True, "not judged on frames (nothing to compare)"))
    else:
        checks.append(Check("frames_no_gap", gap[0] == 0, gap[1]))

    # 4. the splash is gone at most 0.5 s after the window is fully shown
    if handover:
        if gone is None and last_splash is not None:
            checks.append(Check("splash_gone", False, "a splash is still up at the end"))
        elif last_splash is None:
            checks.append(Check("splash_gone", True, "no splash was shown"))
        else:
            assert gone is not None and ev.t_second_exit is not None
            lag = gone.t - ev.t_second_exit
            checks.append(
                Check(
                    "splash_gone",
                    lag <= SPLASH_AFTER_FULL_S,
                    f"the splash is gone {lag:+.2f} s after the second start ended"
                    f" (limit {SPLASH_AFTER_FULL_S} s)",
                )
            )
    elif full is None:
        checks.append(Check("splash_gone", False, "the window was never fully shown"))
    elif last_splash is None:
        checks.append(Check("splash_gone", False, "no splash to be gone"))
    elif gone is None:
        checks.append(Check("splash_gone", False, "the splash is still up at the end"))
    else:
        lag = gone.t - full.t
        checks.append(
            Check(
                "splash_gone",
                lag <= SPLASH_AFTER_FULL_S,
                f"the splash is gone {lag:+.2f} s after the window is fully shown"
                f" (limit {SPLASH_AFTER_FULL_S} s)",
            )
        )

    # 4b. from the still (its outline), the mark never goes out, and it fills before the open
    if ev.onefile and not ev.reduced:
        checks.append(_glyph_held(ev, seen.t if seen else None, part))

    # 5. reduced motion: nothing glides
    if ev.reduced:
        spots = {s.splash_pos for s in part if s.splash and s.splash_pos is not None}
        checks.append(
            Check(
                "no_glide",
                len(spots) <= 1,
                f"the splash stood at {sorted(spots)}",
            )
        )
    timeline["overlay_seen"] = any(s.overlays for s in live)
    timeline["moves"] = len({s.splash_pos for s in part if s.splash and s.splash_pos})
    return Report(checks, timeline)


def _on_screen(s: Sample) -> bool:
    return bool(s.mains) and (s.opacity is None or s.opacity > 0)


def _glyph_held(ev: Events, first_t: Optional[float], part: Sequence[Sample]) -> Check:
    """From the still to the open (the window's first opacity above 0), the glyph's brightness
    never falls below the outline's (the still's level, filmed while it shows alone): it never
    goes out; and it reaches the filled level (GLYPH_FILLED times that) before the open. A
    recording that cannot say is a failed check, with its reason."""
    if not ev.glyph_levels:
        return Check("glyph_held", False, "not measured: no frames were filmed")
    if first_t is None:
        return Check("glyph_held", False, "not measured: no still or splash was ever seen")
    opened = _first(part, lambda s: bool(s.mains) and (s.opacity or 0.0) > 0.0)
    if opened is None:
        return Check("glyph_held", False, "the window never opened")
    helper = _first(part, lambda s: s.splash)
    alone_until = helper.t if helper is not None else opened.t
    frames = list(zip(ev.frame_times, ev.glyph_levels))
    begin = first_t + GLYPH_SETTLE_S
    alone = [lv for t, lv in frames if begin <= t <= alone_until - SAMPLE_S]
    span = [lv for t, lv in frames if begin <= t <= opened.t - SAMPLE_S]
    if len(alone) < 3 or len(span) < 3:
        return Check(
            "glyph_held",
            False,
            f"not measured: {len(alone)} frame(s) of the still alone, {len(span)} frame(s)"
            " between the still and the open (need 3 each)",
        )
    outline = sorted(alone)[len(alone) // 2]
    low, peak = min(span), max(span)
    return Check(
        "glyph_held",
        low >= GLYPH_KEEPS * outline and peak >= GLYPH_FILLED * outline,
        f"the glyph's level from the still to the open: the outline's {outline:.1f}, lowest"
        f" {low:.1f}, highest {peak:.1f} (it must keep {GLYPH_KEEPS:.0%} of the outline's,"
        f" and reach {GLYPH_FILLED}x it, filled)",
    )


def _rises(flags: Sequence[bool]) -> int:
    """How many times a thing goes from absent to present (a first sample present counts)."""
    n, prev = 0, False
    for f in flags:
        if f and not prev:
            n += 1
        prev = f
    return n


def _dip(counts: Sequence[int]) -> bool:
    """Whether the count goes from at least one to zero and back to at least one."""
    seen, zero = False, False
    for c in counts:
        if c > 0 and zero:
            return True
        if c > 0:
            seen = True
        elif seen:
            zero = True
    return False


def _empty_frames_near(ev: Events, span: Sequence[Sample]) -> bool:
    """Whether a filmed frame is empty (the screen as it was before the start) where the window
    count was zero after it had been above zero."""
    seen = False
    for s in span:
        c = _count(s)
        if c > 0:
            seen = True
        elif seen:
            for t, d in zip(ev.frame_times, ev.frame_diffs):
                if abs(t - s.t) <= SAMPLE_S and d <= OCCUPIED_DIFF:
                    return True
    return False


def _runs(counts: Sequence[int]) -> str:
    out: list[str] = []
    for c in counts:
        if out and out[-1].split("x")[0] == str(c):
            n = int(out[-1].split("x")[1]) + 1
            out[-1] = f"{c}x{n}"
        else:
            out.append(f"{c}x1")
    return " ".join(out)


def _frame_gap(
    ev: Events, samples: Sequence[Sample], shown_t: Optional[float], first_t: Optional[float]
) -> Optional[tuple[int, str]]:
    """Empty filmed frames between the first mark and the fully shown window. None: no frames."""
    if not ev.frame_times or first_t is None or shown_t is None:
        return None
    inside = [
        d for t, d in zip(ev.frame_times, ev.frame_diffs) if first_t - SAMPLE_S <= t <= shown_t
    ]
    # the first occupied frame starts the span; empty ones after it are the gap
    start = next((i for i, d in enumerate(inside) if d > OCCUPIED_DIFF), None)
    if start is None:
        return None
    empties = sum(1 for d in inside[start:] if d <= OCCUPIED_DIFF)
    return empties, (
        f"{empties} empty frame(s) of {len(inside) - start} between the first mark and the "
        f"shown window ({1000 / FPS:.0f} ms each)"
    )


# ---------------------------------------------------------------------------- the recording
def _need(*tools: str) -> None:
    missing = [t for t in tools if shutil.which(t) is None]
    if missing:
        sys.exit(
            f"record_start needs {', '.join(missing)} (apt install xdotool x11-utils ffmpeg ...)"
        )


_TREE = re.compile(
    r'^(\s+)(0x[0-9a-f]+) (?:"(.*?)"|\(has no name\)): \(.*?\)\s+(\d+)x(\d+)\+(-?\d+)\+(-?\d+)'
    r"\s+\+(-?\d+)\+(-?\d+)"
)


def parse_tree(text: str) -> list[dict[str, Any]]:
    """`xwininfo -root -tree` lines: indent, id, name, size and absolute position."""
    out = []
    for line in text.splitlines():
        m = _TREE.match(line)
        if m:
            out.append(
                {
                    "depth": len(m.group(1)),
                    "id": m.group(2),
                    "name": m.group(3) or "",
                    "w": int(m.group(4)),
                    "h": int(m.group(5)),
                    "x": int(m.group(8)),
                    "y": int(m.group(9)),
                }
            )
    return out


def classify(tree: list[dict[str, Any]], visible: set[str], opacity: Optional[float]) -> Sample:
    """One sample from a window tree and the set of visible ids."""
    sample = Sample(t=0.0, opacity=opacity)
    mains = [w for w in tree if w["name"] == MAIN_NAME and w["id"] in visible]
    main_depth = mains[0]["depth"] if mains else None
    sample.mains = tuple(w["id"] for w in mains)
    for w in tree:
        if w["id"] not in visible:
            continue
        if w["name"] == SPLASH_NAME and w["w"] == w["h"] == SPLASH_PX:
            sample.splash = True
            sample.splash_pos = (w["x"], w["y"])
        elif w["name"] == OVERLAY_NAME and w["w"] >= OVERLAY_MIN_PX and w["depth"] == main_depth:
            sample.overlays += (w["id"],)
        elif w["w"] == w["h"] == STILL_PX and w["name"] not in (SPLASH_NAME, MAIN_NAME):
            sample.still = True
    return sample


def _run(cmd: list[str], env: dict[str, str], timeout: float = 2.0) -> str:
    try:
        return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout).stdout
    except subprocess.TimeoutExpired, OSError:
        return ""


class Sampler(threading.Thread):
    """Samples the windows every 100 ms until stopped. A tool thread: no Qt here."""

    def __init__(self, env: dict[str, str]) -> None:
        super().__init__(daemon=True)
        self.env = env
        self.samples: list[Sample] = []
        self._last_main = ""
        self._done = threading.Event()

    def stop(self) -> None:
        self._done.set()
        self.join(5)

    def run(self) -> None:
        while not self._done.is_set():
            began = time.time()
            visible: set[str] = set()
            for field_ in ("--name", "--class"):  # the splash has a class and no title
                found = _run(["xdotool", "search", "--onlyvisible", field_, "."], self.env)
                visible |= {"0x%x" % int(i) for i in found.split()}
            tree = parse_tree(_run(["xwininfo", "-root", "-tree"], self.env))
            if not tree:  # a tool call that failed or timed out says nothing about the screen
                self._done.wait(SAMPLE_S)
                continue
            sample = classify(tree, visible, None)
            if sample.mains:
                sample.opacity = self._opacity(sample.mains[0])
            if sample.mains:
                self._last_main = sample.mains[0]
            elif self._last_main:
                state = _run(["xwininfo", "-id", self._last_main], self.env)
                m = re.search(r"Map State: (\w+)", state)
                sample.note = f"{self._last_main}: {m.group(1) if m else 'gone'}"
            sample.t = began
            self.samples.append(sample)
            self._done.wait(max(0.0, SAMPLE_S - (time.time() - began)))

    def _opacity(self, win: str) -> Optional[float]:
        out = _run(["xprop", "-id", win, "_NET_WM_WINDOW_OPACITY"], self.env)
        m = re.search(r"=\s*(\d+)", out)
        return int(m.group(1)) / 0xFFFFFFFF if m else None


class Grabber:
    """Films the screen: a lossless file for the clip and small gray frames, stamped on arrival,
    for the analysis."""

    def __init__(self, env: dict[str, str], capture: Path) -> None:
        self.frames: list[tuple[float, bytes]] = []
        w, h = SCREEN
        cmd = [
            "ffmpeg", "-loglevel", "error", "-f", "x11grab", "-framerate", str(FPS),
            "-video_size", f"{w}x{h}", "-i", env["DISPLAY"],
            "-fps_mode", "passthrough", "-c:v", "libx264", "-preset", "ultrafast", "-qp", "0",
            "-pix_fmt", "yuv444p", str(capture),
            "-fps_mode", "passthrough", "-vf", f"scale={GRAB_W}:{GRAB_H}:flags=area,format=gray",
            "-f", "rawvideo", "-",
        ]  # fmt: skip
        self.proc = subprocess.Popen(
            cmd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0
        )
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self) -> None:
        size = GRAB_W * GRAB_H
        out = self.proc.stdout
        assert out is not None
        while True:
            buf = b""
            while len(buf) < size:
                chunk = out.read(size - len(buf))
                if not chunk:
                    return
                buf += chunk
            self.frames.append((time.time(), buf))

    def stop(self) -> None:
        try:
            assert self.proc.stdin is not None
            self.proc.stdin.write(b"q")
            self.proc.stdin.flush()
        except OSError:
            pass
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.thread.join(5)


def require_frames(frames: Sequence[Any]) -> None:
    """ffmpeg filmed nothing (no screen grab): that is the failure, not a missing file later."""
    if not frames:
        raise SystemExit("record_start: ffmpeg filmed no frames (is x11grab available?)")


def glyph_levels(frames: list[tuple[float, bytes]]) -> list[float]:
    """The mean gray of the middle of the screen, where the mark's tile and glyph stand."""
    cx, cy = GRAB_W // 2, GRAB_H // 2
    out = []
    for _, buf in frames:
        total = 0
        for y in range(cy - GLYPH_BOX, cy + GLYPH_BOX):
            row = y * GRAB_W
            total += sum(buf[row + cx - GLYPH_BOX : row + cx + GLYPH_BOX])
        out.append(total / (4 * GLYPH_BOX * GLYPH_BOX))
    return out


def frame_diffs(frames: list[tuple[float, bytes]], base: bytes) -> list[float]:
    """Mean gray distance of each frame from the empty screen."""
    out = []
    for _, buf in frames:
        total = sum(abs(a - b) for a, b in zip(buf, base))
        out.append(total / len(base))
    return out


def _wait_for(display: str) -> None:
    sock = Path(f"/tmp/.X11-unix/X{display.lstrip(':')}")
    for _ in range(100):
        if sock.exists():
            return
        time.sleep(0.1)
    raise RuntimeError("the X server did not start")


def _free_display() -> str:
    n = 90
    while Path(f"/tmp/.X11-unix/X{n}").exists() or Path(f"/tmp/.X{n}-lock").exists():
        n += 1
    return f":{n}"


def _app_command(scenario: str, app: Path, film: Optional[Path]) -> list[str]:
    base = [str(app)] if scenario.startswith("onefile") else [sys.executable, str(ROOT / "main.py")]
    return base + ([str(film)] if film else [])


def _control(env: dict[str, str], *args: str) -> None:
    try:
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "qitv_control.py"), *args],
            env=env, capture_output=True, timeout=15,
        )  # fmt: skip
    except subprocess.TimeoutExpired:
        pass


def record(scenario: str, out: Path, app: Path) -> tuple[list[Sample], Events, dict[str, Any]]:
    """Run the scenario under a private X server; return the samples, the events and the files."""
    _need("Xvfb", "openbox", "xcompmgr", "xdotool", "xwininfo", "xprop", "ffmpeg")
    work = Path(tempfile.mkdtemp(prefix=f"record-{scenario}-"))
    display = _free_display()
    env = dict(os.environ, DISPLAY=display)
    procs: list[subprocess.Popen] = []
    xs = subprocess.Popen(
        ["Xvfb", display, "-screen", "0", f"{SCREEN[0]}x{SCREEN[1]}x24", "-nolisten", "tcp"],
        stderr=subprocess.DEVNULL,
    )
    procs.append(xs)
    try:
        _wait_for(display)
        wm = subprocess.Popen(["openbox"], env=env, stderr=subprocess.DEVNULL)
        procs.append(wm)
        time.sleep(1.0)
        procs.append(subprocess.Popen(["xcompmgr"], env=env, stderr=subprocess.DEVNULL))
        time.sleep(0.5)
        reduced = scenario == "reduced"
        film = None if scenario in ("plain", "onefile") else FIXTURE
        cfg = work / "config"
        cfg.mkdir()
        app_env = dict(
            env, QT_QPA_PLATFORM="xcb", QITV_SMOKE_MUTE="1", QITV_CONTROL="1",
            QITV_CONFIG_DIR=str(cfg),
        )  # fmt: skip
        if reduced:
            app_env["QITV_REDUCED_MOTION"] = "1"
        capture = work / "capture.mkv"
        grab = Grabber(env, capture)
        sampler = Sampler(env)
        sampler.start()
        time.sleep(1.0)
        base = grab.frames[-1][1] if grab.frames else bytes(GRAB_W * GRAB_H)
        ev = Events(t_exec=time.time(), reduced=reduced, onefile=scenario.startswith("onefile"))
        log = open(work / "qitv.out", "wb")
        first = subprocess.Popen(
            _app_command(scenario, app, film), env=app_env, stdout=log, stderr=log, cwd=ROOT
        )
        procs.append(first)
        origin = ev.t_exec
        if scenario == "handover":
            time.sleep(7.0)  # the first start is up, its picture running
            ev.t_second = time.time()
            second = subprocess.Popen(
                _app_command(scenario, app, FIXTURE_SECOND),
                env=app_env, stdout=log, stderr=log, cwd=ROOT,
            )  # fmt: skip
            try:
                second.wait(30)
            except subprocess.TimeoutExpired:
                second.kill()
            ev.t_second_exit = time.time()
            origin = ev.t_second
            time.sleep(CLIP_S - CLIP_LEAD_S - (ev.t_second_exit - origin) + 0.5)
        else:
            time.sleep(CLIP_S - CLIP_LEAD_S + 0.5)
        ev.t_end = time.time()
        _control(app_env, "quit")
        try:
            first.wait(20)
        except subprocess.TimeoutExpired:
            first.terminate()
        time.sleep(0.5)
        sampler.stop()
        grab.stop()
        log.close()
        samples = sampler.samples
        frames = grab.frames
        require_frames(frames)
        ev.frame_times = [t for t, _ in frames]
        ev.frame_diffs = frame_diffs(frames, base)
        ev.glyph_levels = glyph_levels(frames)
        files = _clip(out, scenario, capture, ev, origin, samples, work)
        files["log"] = str(work / "qitv.out")
        files["settings_log"] = str(next(cfg.rglob("*.log"), ""))
        return samples, ev, files
    finally:
        for p in reversed(procs):
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
        for p in procs:
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()


def _clip(
    out: Path, scenario: str, capture: Path, ev: Events, origin: float, samples: list[Sample],
    work: Path,
) -> dict[str, Any]:  # fmt: skip
    """The 8 s clip and the contact sheet, cut by frame number from the lossless capture."""
    out.mkdir(parents=True, exist_ok=True)
    times = ev.frame_times
    if not times:
        return {}
    start = max(0, _index_at(times, origin - CLIP_LEAD_S))
    stop = min(len(times) - 1, start + int(CLIP_S * FPS))
    mp4 = out / f"task5-{scenario}.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(capture), "-vf",
         f"select=between(n\\,{start}\\,{stop}),setpts=N/{FPS}/TB,scale=960:-2",
         "-r", str(FPS), "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
         "-pix_fmt", "yuv420p", str(mp4)],
        check=True, capture_output=True,
    )  # fmt: skip
    # the sheet: SHEET_FRAMES frames across the motion, each labelled with its time
    on = [s.t for s in samples if s.splash or s.still or s.mains]
    lo = max(times[start], (min(on) if on else origin) - 0.15)
    last = max([s.t for s in samples if s.splash or s.still] or [lo + 3.0])
    hi = min(times[stop], lo + 5.0, max(last + 0.5, lo + 1.0))
    picks = sorted(
        {_index_at(times, lo + (hi - lo) * i / (SHEET_FRAMES - 1)) for i in range(SHEET_FRAMES)}
    )
    font = _font()
    labels = ",".join(
        f"drawtext=fontfile={font}:text='{times[n] - origin:+.2f}':enable='eq(n\\,{n})':x=4:y=4:"
        "fontsize=16:fontcolor=white:box=1:boxcolor=black@0.6"
        for n in picks
    )
    select = "+".join(f"eq(n\\,{n})" for n in picks)
    rows = -(-len(picks) // SHEET_COLUMNS)
    sheet = out / f"task5-{scenario}-sheet.png"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(capture), "-vf",
         f"scale={SHEET_TILE_PX}:-2,{labels},select='{select}',"
         f"tile={SHEET_COLUMNS}x{rows}:padding=2:color=black",
         "-frames:v", "1", str(sheet)],
        check=True, capture_output=True,
    )  # fmt: skip
    return {"mp4": str(mp4), "sheet": str(sheet)}


def _font() -> str:
    for cand in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/opentype/inter/Inter-Regular.otf",
    ):
        if Path(cand).exists():
            return cand
    return ""


def _index_at(times: Sequence[float], t: float) -> int:
    for i, ft in enumerate(times):
        if ft >= t:
            return i
    return len(times) - 1


def render(report: Report) -> str:
    lines = ["timeline (seconds from the start): " + json.dumps(report.timeline)]
    for c in report.checks:
        lines.append(f"  [{'ok' if c.ok else 'FAIL'}] {c.name}: {c.detail}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("scenario", choices=SCENARIOS)
    ap.add_argument("--check", action="store_true", help="exit 1 when a check fails")
    ap.add_argument("--out", type=Path, default=ROOT / "build" / "record")
    ap.add_argument("--app", type=Path, default=ROOT / "dist" / "qitv")
    args = ap.parse_args(argv)
    if "DBUS_SESSION_BUS_ADDRESS" not in os.environ and shutil.which("dbus-run-session"):
        os.execvp("dbus-run-session", ["dbus-run-session", "--", sys.executable, *sys.argv])
    args.out.mkdir(parents=True, exist_ok=True)
    samples, ev, files = record(args.scenario, args.out, args.app)
    report = analyse(samples, ev, args.scenario)
    print(render(report))
    print("files:", json.dumps(files))
    (args.out / f"task5-{args.scenario}.json").write_text(
        json.dumps(
            {
                "timeline": report.timeline,
                "checks": [asdict(c) for c in report.checks],
                "events": {
                    k: v
                    for k, v in asdict(ev).items()
                    if not k.startswith("frame_") and k != "glyph_levels"
                },
                "glyph_levels": [round(v, 1) for v in ev.glyph_levels],
                "frame_times": [round(t - ev.t_exec, 3) for t in ev.frame_times],
                "samples": [asdict(s) for s in samples],
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    return 1 if args.check and not report.ok else 0


if __name__ == "__main__":
    raise SystemExit(main())
