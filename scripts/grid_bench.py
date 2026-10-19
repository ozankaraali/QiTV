"""What the multiview costs: N tiles playing at once, measured, muted and offscreen.

    QT_QPA_PLATFORM=offscreen uv run python scripts/grid_bench.py --tiles 4 --out <dir>
        [--seconds 20] [--decoding hardware,software] [--clip <file.ts>]

N paced copies of a local clip (tests/fixtures/live.ts, 320×180, unless `--clip` names
another MPEG-TS file, a 1080p test pattern made with ffmpeg say) are served on 127.0.0.1 as
the fixture provider's live channels (tests/fixtures/live_source.LiveSource). For each
decoding mode a child process runs the real TV window on Qt's offscreen platform in a
temporary QITV_CONFIG_DIR, libVLC muted (`--aout=dummy`): offscreen libVLC opens no video
display, so it decodes every frame and shows none. The multiview opens with N tiles and each
tile plays one channel through the time-shift pipeline (memory buffers: the focused tile's
the memory per stream, the others' `multiview_tile_ram_mib`). After a warm-up, it measures
for `--seconds`:
- the GUI process's CPU (every thread: libVLC's demuxers and decoders, Qt), and the
  time-shift helper's (`ps`), in % of one core;
- per tile, the frames libVLC decoded and lost (its media statistics), as frames per second,
  and the tile's lag behind live (its LiveSession timeline).
Results go to `<out>/grid-bench.json`, and a table to the log. Nothing reaches a network
and nothing is heard or shown.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Generator, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.fixtures.live_source import LIVE_TS, LiveSource  # noqa: E402
from tests.fixtures.local_provider import FIXTURE_DIR  # noqa: E402

logger = logging.getLogger("grid_bench")

RESULT = "grid-bench.json"
OPEN_TIMEOUT_S = 30.0  # for a tile's channel to play from its buffer
WARM_UP_S = 5.0
POLL_MS = 100
CHILD_GRACE_S = 120.0  # beyond the run's own length: then the child is stopped


def _cpu_seconds(pid: int) -> Optional[float]:
    """A process's CPU time so far, as `ps` reads it ("[[dd-]hh:]mm:ss.ss"); None if gone."""
    try:
        text = subprocess.run(
            ["ps", "-o", "time=", "-p", str(pid)], capture_output=True, text=True, timeout=5
        ).stdout.strip()
    except OSError, subprocess.SubprocessError:
        return None
    if not text:
        return None
    days, _, clock = text.rpartition("-")
    seconds = 0.0
    for part in clock.split(":"):
        seconds = seconds * 60 + float(part)
    return seconds + (float(days) * 86400 if days else 0.0)


def _media_stats(playback: Any) -> Optional[dict[str, int]]:
    """libVLC's statistics of what a player plays (None without one)."""
    from services.playback import vlc

    player = getattr(playback, "_player", None)
    media = player.get_media() if player is not None else None
    if vlc is None or media is None:
        return None
    stats = vlc.MediaStats()
    if not media.get_stats(stats):
        return None
    return {
        "decoded_video": int(stats.decoded_video),
        "displayed": int(stats.displayed_pictures),
        "lost": int(stats.lost_pictures),
        "demux_corrupted": int(stats.demux_corrupted),
    }


# ---- the child: the TV window with N tiles -------------------------------------------------


class _Run:
    """The scenario, as a generator stepped on the GUI thread (each yield: ms to wait)."""

    def __init__(self, window: Any, args: argparse.Namespace, out: Path) -> None:
        self.window = window
        self.args = args
        self.out = out
        self.result: dict[str, Any] = {"mode": args.mode, "tiles": args.tiles}
        self.lags: dict[str, list[float]] = {}
        self._steps = self._scenario()

    def step(self) -> None:
        from PySide6.QtCore import QTimer

        try:
            delay = next(self._steps)
        except StopIteration:
            return
        except Exception as error:
            logger.exception("The bench stopped")
            self.result["error"] = repr(error)
            self._finish()
            return
        QTimer.singleShot(delay, self.step)

    def _wait(self, predicate: Callable[[], bool], seconds: float) -> Generator[int, None, bool]:
        deadline = time.monotonic() + seconds
        while not predicate() and time.monotonic() < deadline:
            yield POLL_MS
        return bool(predicate())

    def _scenario(self) -> Generator[int, None, None]:
        from services.catalog import ALL_GROUP_ID
        from services.playback import PlaybackState
        from ui.keymap import Action

        window, args = self.window, self.args
        controller, sessions = window.controller, window.sessions
        catalog = window.catalog
        yield 1500  # the window starts; with no last channel the list opens
        window.overlays.drawer.close_drawer()
        loaded = yield from self._wait(lambda: bool(catalog.items("live", ALL_GROUP_ID)), 20)
        if not loaded or sessions is None:
            raise RuntimeError("no channels, or no libVLC")
        channels = list(catalog.items("live", ALL_GROUP_ID))
        live = [c for c in channels if str(c.get("cmd", "")).startswith("http")]
        controller.handle(Action.MULTIVIEW)
        grid = controller._grid
        if grid is None or not grid.shown():
            raise RuntimeError("the multiview did not show")
        if args.tiles != len(grid.names()):
            grid.set_cells(args.tiles)
        names = grid.names()
        if len(names) != args.tiles:
            raise RuntimeError(f"{args.tiles} tiles are not offered with {args.mode} decoding")
        sessions.slotTimeline.connect(self._on_timeline)
        opened: dict[str, float] = {}
        for index, name in enumerate(names):
            controller.focus_tile(name)
            started = time.monotonic()
            controller.tune(live[index], group_key=ALL_GROUP_ID, channels=channels)
            slot = sessions.slot(name)

            def playing(slot: Any = slot) -> bool:
                return slot.live.mode == "pipeline" and (
                    slot.playback.state() is PlaybackState.PLAYING
                )

            if not (yield from self._wait(playing, OPEN_TIMEOUT_S)):
                raise RuntimeError(f"tile {index + 1} did not play from its buffer")
            opened[name] = round(time.monotonic() - started, 2)
        controller.focus_tile(names[0])
        self.result["opened_s"] = opened
        yield int(WARM_UP_S * 1000)
        helper = self._helper_pid()
        before = self._sample(names, helper)
        self.lags = {name: [] for name in names}
        yield int(args.seconds * 1000)
        after = self._sample(names, helper)
        self._report(names, before, after)
        self._finish()

    def _helper_pid(self) -> Optional[int]:
        supervisor = getattr(self.window.engine, "_supervisor", None)
        return getattr(getattr(supervisor, "_process", None), "pid", None)

    def _sample(self, names: list[str], helper: Optional[int]) -> dict[str, Any]:
        sessions = self.window.sessions
        return {
            "wall": time.monotonic(),
            "gui": time.process_time(),
            "helper": _cpu_seconds(helper) if helper is not None else None,
            "tiles": {name: _media_stats(sessions.slot(name).playback) for name in names},
        }

    def _on_timeline(self, name: str, state: dict) -> None:
        if name in self.lags and isinstance(state.get("lag"), (int, float)):
            self.lags[name].append(float(state["lag"]))

    def _report(self, names: list[str], before: dict, after: dict) -> None:
        wall = after["wall"] - before["wall"]
        result = self.result
        result["seconds"] = round(wall, 1)
        result["gui_cpu_pct"] = round(100 * (after["gui"] - before["gui"]) / wall, 1)
        if before["helper"] is not None and after["helper"] is not None:
            result["helper_cpu_pct"] = round(100 * (after["helper"] - before["helper"]) / wall, 1)
        tiles = []
        sessions = self.window.sessions
        for number, name in enumerate(names, 1):
            first, last = before["tiles"][name], after["tiles"][name]
            tile: dict[str, Any] = {"tile": number, "slot": name}
            tile["mode"] = sessions.slot(name).live.mode
            tile["focused"] = number == 1
            if first is not None and last is not None:
                tile["decoded_fps"] = round(
                    (last["decoded_video"] - first["decoded_video"]) / wall, 1
                )
                tile["lost"] = last["lost"] - first["lost"]
                tile["displayed"] = last["displayed"] - first["displayed"]
                tile["corrupted"] = last["demux_corrupted"] - first["demux_corrupted"]
            lags = self.lags.get(name) or []
            if lags:
                tile["lag_s"] = round(sum(lags) / len(lags), 2)
            tiles.append(tile)
        result["per_tile"] = tiles

    def _finish(self) -> None:
        (self.out / f"{self.args.mode}.json").write_text(
            json.dumps(self.result, indent=2), encoding="utf-8"
        )
        self.window.close()


def run_child(args: argparse.Namespace, out: Path) -> int:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from services import playback
    from services.config_manager import ConfigManager
    from services.epg_manager import EpgManager
    from services.image_manager import ImageManager
    from services.provider_manager import ProviderManager
    from tests.fixtures.local_provider import seed_channels, write_local_provider
    from ui import tv_window

    urls = args.live_url or []
    hardware = args.mode == "hardware"
    config_dir = os.environ["QITV_CONFIG_DIR"]
    provider = write_local_provider(config_dir, fixture_dir=FIXTURE_DIR, live_urls=urls)
    app = QApplication([sys.argv[0]])
    config = ConfigManager()
    config.hardware_decoding = hardware
    config.timeshift_enabled = False  # memory buffers, as the tiles' always are
    images = ImageManager(config, config.max_cache_image_size * 1024 * 1024)
    providers = ProviderManager(config)
    epg = EpgManager(config, providers)
    seed_channels(providers, provider, fixture_dir=FIXTURE_DIR, live_urls=urls)

    def muted(extra_args: Optional[list[str]] = None) -> Any:
        return playback.create_instance(
            [*(extra_args or playback.default_args(hardware)), "--aout=dummy"]
        )

    tv_window.create_instance = muted  # every player plays to nothing
    window = tv_window.TVWindow(app, config, providers, images, epg)
    window.resize(1600, 900)
    window.show()
    run = _Run(window, args, out)
    QTimer.singleShot(0, run.step)
    return app.exec()


# ---- the parent: the sources, a child per mode, the table ----------------------------------


def _table(results: list[dict[str, Any]]) -> str:
    lines = ["mode       tiles  GUI CPU  helper CPU  per tile: decoded fps / lost / lag"]
    for result in results:
        if "error" in result:
            lines.append(f"{result['mode']:<10} {result['tiles']:>5}  {result['error']}")
            continue
        tiles = "; ".join(
            f"{t['tile']}{'*' if t['focused'] else ''} {t.get('decoded_fps', '?')} / "
            f"{t.get('lost', '?')} / {t.get('lag_s', '?')} s"
            for t in result.get("per_tile", [])
        )
        lines.append(
            f"{result['mode']:<10} {result['tiles']:>5}  {result.get('gui_cpu_pct', '?'):>6}%"
            f"  {result.get('helper_cpu_pct', '?'):>9}%  {tiles}"
        )
    return "\n".join(lines)


def run_parent(args: argparse.Namespace, out: Path) -> int:
    clip = Path(args.clip).resolve() if args.clip else LIVE_TS
    sources = [LiveSource(clip) for _ in range(args.tiles)]
    results = []
    try:
        for mode in args.decoding.split(","):
            command = [sys.executable, __file__, "--child", "--mode", mode, "--out", str(out)]
            command += ["--tiles", str(args.tiles), "--seconds", str(args.seconds)]
            for source in sources:
                command += ["--live-url", source.url]
            with tempfile.TemporaryDirectory(prefix="qitv-grid-bench-") as config:
                env = dict(os.environ, QITV_CONFIG_DIR=config, QT_QPA_PLATFORM="offscreen")
                logger.info("Measuring %d tiles with %s decoding", args.tiles, mode)
                try:
                    subprocess.run(
                        command,
                        cwd=ROOT,
                        env=env,
                        timeout=args.seconds + WARM_UP_S + CHILD_GRACE_S,
                    )
                except subprocess.TimeoutExpired:
                    logger.error("The %s run did not finish in time", mode)
            path = out / f"{mode}.json"
            if path.exists():
                results.append(json.loads(path.read_text(encoding="utf-8")))
            else:
                results.append({"mode": mode, "tiles": args.tiles, "error": "no result"})
    finally:
        for source in sources:
            source.close()
    summary = {"clip": clip.name, "results": results}
    (out / RESULT).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    logger.info("Clip %s, %d tiles:\n%s", clip.name, args.tiles, _table(results))
    return 0 if all("error" not in r for r in results) else 1


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--out", required=True, help="directory for grid-bench.json")
    parser.add_argument("--tiles", type=int, default=4, choices=(2, 4, 6, 9))
    parser.add_argument("--seconds", type=float, default=20.0, help="how long to measure")
    parser.add_argument(
        "--decoding",
        default="hardware,software",
        help="the decoding modes to measure, comma separated (default: %(default)s)",
    )
    parser.add_argument("--clip", help="an MPEG-TS file to serve (default: tests/fixtures/live.ts)")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--mode", help=argparse.SUPPRESS)
    parser.add_argument("--live-url", action="append", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    if args.child:
        return run_child(args, out)
    return run_parent(args, out)


if __name__ == "__main__":
    sys.exit(main())
