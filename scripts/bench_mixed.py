"""Scale bench for QiTV 2.1's mixed providers: 4 providers, 200,000+ channels.

    uv run python scripts/bench_mixed.py --providers 4 --channels 50000 --films 20000 --out <dir>

Builds fictional provider caches in `<out>/config` (nothing reaches a network; every URL is
127.0.0.1:9), runs the TV window offscreen with libVLC muted on "All providers", and times what
the user waits for. It writes `<out>/bench.json` and exits 1 when a target is missed:

    drawer open <= 200 ms       a group open <= 200 ms      a key step <= 50 ms
    Library genres <= 300 ms    first search results <= 1,500 ms
    no GUI-thread stall > 100 ms while browsing

The first `--providers // 2` providers are Xtream-shaped (channels + 14% extra, `--films` films,
a few hundred series); the others are M3U playlists of `--channels` channels. The caches are
built in a child process (so the bench's memory is the app's) and kept for later runs; pass
`--rebuild` to make them again.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import random
import subprocess
import sys
import time
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logger = logging.getLogger("bench_mixed")

TARGETS_MS = {
    "drawer_open": 200,
    "group_open": 200,
    "key_step": 50,
    "library_genres": 300,
    "first_search": 1500,
    "stall": 100,
}

PROVIDER_NAMES = ["Harbor IPTV", "Atlas TV", "Demo TV", "Meridian"]
COUNTRIES = ["TR", "DE", "UK", "US", "FR", "ES", "IT", "NL", "PL", "RU", "AR", "BR", "IN", "PT"]
THEMES = [
    "Sport", "News", "Movies", "Kids", "Music", "Drama", "Docs", "Series", "Comedy", "Local",
    "Regional", "Premium", "Radio", "Adult-free", "Culture",
]  # fmt: skip
WORDS = [
    "Kanal", "Star", "Prime", "Max", "Plus", "One", "Nova", "Vision", "Arena", "Pulse", "Atlas",
    "Delta", "Orbit", "Metro", "Cinema", "Action", "Family", "World", "Trend", "Zone", "Mega",
    "Super", "Ultra", "Active", "Classic", "Gold", "Blue", "Red", "Sun", "Moon",
]  # fmt: skip
SUFFIX = ["", " HD", " FHD", " 4K", " SD", " HEVC", " +1", " RAW"]


def _names(rng: random.Random, count: int, tag: str) -> list[str]:
    out = []
    for i in range(count):
        out.append(
            f"{rng.choice(WORDS)} {rng.choice(WORDS)} {rng.choice(THEMES)} {i}"
            f"{rng.choice(SUFFIX)}"
        )
    return out


def _grouped(rng: random.Random, count: int, groups: int, kind: str) -> dict:
    """A section payload: `count` items spread over `groups` skewed groups."""
    titles = []
    for g in range(groups):
        titles.append(f"{rng.choice(COUNTRIES)} | {rng.choice(THEMES)} {g}")
    positions: dict[str, list[int]] = {t: [] for t in titles}
    weights = [1.0 / (1 + g) ** 0.7 for g in range(groups)]
    for pos, title in enumerate(rng.choices(titles, weights=weights, k=count)):
        positions[title].append(pos)
    positions = {t: p for t, p in positions.items() if p}
    names = _names(rng, count, kind)
    contents = [
        {
            "id": pos + 1,
            "number": str(pos + 1),
            "name": name,
            "cmd": f"http://127.0.0.1:9/{kind}/{pos + 1}.ts",
            "logo": f"http://127.0.0.1:9/logo/{kind}/{pos + 1}.png",
            "epg_channel_id": f"{kind}{pos + 1}.x",
            "added": "1700000000",
        }
        for pos, name in enumerate(names)
    ]
    categories = [{"id": "*", "title": "All"}]
    categories += [{"id": t, "title": t} for t in positions]
    return {"categories": categories, "contents": contents, "sorted_channels": positions}


def build_fixture(out: Path, providers: int, channels: int, films: int) -> None:
    """Write `out/config`: providers and fresh caches (what mixed_config does, at scale)."""
    import orjson

    from services.config_manager import ConfigManager
    from services.provider_manager import ProviderManager

    config_dir = out / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    os.environ["QITV_CONFIG_DIR"] = str(config_dir)
    config = ConfigManager()
    config.selected_provider_name = ""
    config.tv_all_providers = True
    config.library_all_providers = True
    manager = ProviderManager(config)
    typed = max(1, providers // 2)
    entries = []
    for n in range(providers):
        name = PROVIDER_NAMES[n] if n < len(PROVIDER_NAMES) else f"Provider {n + 1}"
        if n < typed:
            entries.append(
                {
                    "type": "XTREAM",
                    "name": name,
                    "url": "http://127.0.0.1:9",
                    "username": f"user{n}",
                    "password": "secret",
                }
            )
        else:
            entries.append(
                {"type": "M3UPLAYLIST", "name": name, "url": f"http://127.0.0.1:9/{n}.m3u"}
            )
    manager.providers = entries
    manager.save_providers()
    rng = random.Random(2110)
    for n, provider in enumerate(manager.providers):
        if n < typed:
            live = int(channels * 1.14)
            content = {
                "itv": _grouped(rng, live, 320, "live"),
                "vod": _grouped(rng, films, 40, "movie"),
                "series": _grouped(rng, max(300, films // 20), 30, "series"),
            }
        else:
            content = {"itv": _grouped(rng, channels, 180, "live")}
        cache = dict(content)
        cache["_cache"] = {
            "identity": manager.provider_cache_identity(provider),
            "refreshed_at": {key: time.time() for key in content},
        }
        with open(manager._provider_cache_name(provider["name"]), "wb") as f:
            f.write(orjson.dumps(cache))
        logger.info("%s: %d live channels", provider["name"], len(content["itv"]["contents"]))
    config.save_config()


# ---- the run ---------------------------------------------------------------------------------


def rss_mib() -> float:
    out = subprocess.run(
        ["ps", "-o", "rss=", "-p", str(os.getpid())], capture_output=True, text=True
    ).stdout
    return round(int(out.strip() or 0) / 1024, 1)


class Bench:
    def __init__(self, app: Any, window: Any) -> None:
        from PySide6.QtCore import QElapsedTimer, QTimer

        self.app = app
        self.window = window
        self.results: dict[str, Any] = {}
        self.profile: set[str] = set()
        self.clock = QElapsedTimer()
        self.clock.start()
        self._last_beat = 0
        self.max_gap = 0.0
        self.sampled: dict[str, int] = {}
        self.label = "start"  # the step running now, for the stall report
        self.worst_step = ""
        self.stalls: list[tuple[int, str]] = []
        self.recording = False
        self.beat = QTimer()
        self.beat.setInterval(5)
        self.beat.timeout.connect(self._tick)
        self.beat.start()

    def _tick(self) -> None:
        now = self.clock.nsecsElapsed()
        if self.recording and self._last_beat:
            gap = (now - self._last_beat) / 1e6
            if gap > self.max_gap:
                self.max_gap, self.worst_step = gap, self.label
            if gap > 100:
                self.stalls.append((round(gap), self.label))
        self._last_beat = now

    def pump(self) -> None:
        self.app.processEvents()
        time.sleep(0.001)

    def settle(self, done: Callable[[], bool], timeout: float = 30.0) -> bool:
        end = time.perf_counter() + timeout
        while time.perf_counter() < end:
            self.pump()
            if done():
                return True
        return False

    def idle(self, quiet: float = 0.05) -> None:
        """Drain the event loop until it stays empty for `quiet` seconds."""
        deadline = time.perf_counter() + quiet
        while time.perf_counter() < deadline:
            self.app.processEvents()
            time.sleep(0.001)

    def step(self, name: str, action: Callable[[], None]) -> float:
        """Run a key press and let the events it caused run: ms until the screen is current."""
        self.label = name
        t0 = time.perf_counter()
        action()
        self.app.processEvents()
        ms = round((time.perf_counter() - t0) * 1000, 1)
        self.results[name] = ms
        logger.info("%-28s %8.1f ms", name, ms)
        return ms

    def timed(
        self,
        name: str,
        step: Callable[[], None],
        done: Callable[[], bool] = lambda: True,
        timeout: float = 30.0,
    ):
        """Run `step`, then pump until `done()`: ms from the call until it is on screen."""
        self.label = name
        profiler = None
        if name in self.profile:
            import cProfile

            profiler = cProfile.Profile()
            profiler.enable()
        t0 = time.perf_counter()
        step()
        ok = self.settle(done, timeout)
        self.app.processEvents()
        if TRACE:
            dump_trace(name)
        if profiler is not None:
            import pstats

            profiler.disable()
            print(f"--- profile of {name} ---", file=sys.stderr)
            stats = pstats.Stats(profiler, stream=sys.stderr).sort_stats("cumulative")
            stats.print_stats(28)
            if os.environ.get("BENCH_CALLERS"):
                stats.print_callers(os.environ["BENCH_CALLERS"])
        ms = (time.perf_counter() - t0) * 1000
        self.results[name] = round(ms, 1)
        if not ok:
            self.results[name + "_timeout"] = True
        logger.info("%-28s %8.1f ms%s", name, ms, "" if ok else "  (TIMED OUT)")
        return ms


TRACED = {  # where the GUI thread spends its time: "module:Class.method"
    "ui.overlays.drawer:Drawer._refresh": 0,
    "ui.overlays.drawer:Drawer._fill": 0,
    "ui.overlays.drawer:_numbers": 0,
    "ui.overlays.drawer:_shown_rows": 0,
    "ui.overlays.drawer:_NodeModel.set_nodes": 0,
    "ui.overlays.drawer:Drawer._show_current": 0,
    "ui.overlays.drawer:Drawer._relayout": 0,
    "services.navigation:NavModel.columns": 0,
    "services.navigation:NavModel.children": 0,
    "services.navigation:NavModel._group_items": 0,
    "services.navigation:NavModel._cached": 0,
    "services.navigation:NavModel.reset_to": 0,
    "services.browsed_catalog:BrowsedCatalog.items": 0,
    "services.browsed_catalog:BrowsedCatalog.groups": 0,
    "services.browsed_catalog:BrowsedCatalog.content_version": 0,
    "services.browsed_catalog:BrowsedCatalog._mixed_items": 0,
    "services.browsed_catalog:BrowsedCatalog._mixed_groups": 0,
    "services.browsed_catalog:BrowsedCatalog._versions": 0,
    "services.browsed_catalog:BrowsedCatalog.source": 0,
    "services.library:LibraryModel._loaded_items": 0,
    "services.library:LibraryModel._new_items": 0,
    "services.library:LibraryModel._pinned_genres": 0,
    "services.library:LibraryModel._fingerprint": 0,
    "services.library:LibraryModel.genres": 0,
    "services.library:LibraryModel.items": 0,
    "services.library:LibraryModel.letter_index": 0,
    "services.library:LibraryModel.available_sorts": 0,
    "services.library:LibraryModel._on_section_ready": 0,
    "services.library:LibraryModel.set_section": 0,
    "ui.library_controller:LibraryController._on_model_changed": 0,
    "ui.library_controller:LibraryController._refresh_grid": 0,
    "ui.library_controller:LibraryController.open": 0,
    "ui.library.genre_list:GenreList.set_genres": 0,
    "ui.library.poster_grid:PosterGrid.set_sections": 0,
    "ui.library.poster_grid:PosterGrid.set_letter_index": 0,
    "ui.library_controller:LibraryController._show_hits": 0,
    "ui.library.search_session:SearchSession._set_hits": 0,
    "ui.library.search_session:SearchSession._on_done": 0,
    "services.search:build_index": 0,
    "services.search:search_index": 0,
    "services.provider_manager:ProviderManager.get_all_providers_cached_content": 0,
}  # fmt: skip
TRACE: dict[str, list] = {}


def install_trace() -> None:
    """Wrap the named functions to total their time and calls (nested calls count in both)."""
    import functools
    import importlib

    for spec in TRACED:
        module_name, qualified = spec.split(":")
        owner: Any = importlib.import_module(module_name)
        *path, name = qualified.split(".")
        for part in path:
            owner = getattr(owner, part)
        original = getattr(owner, name)
        if isinstance(original, staticmethod):
            continue
        TRACE[spec] = [0.0, 0]

        def make(fn, key):
            @functools.wraps(fn)
            def timed(*a, **k):
                t0 = time.perf_counter()
                try:
                    return fn(*a, **k)
                finally:
                    entry = TRACE[key]
                    entry[0] += time.perf_counter() - t0
                    entry[1] += 1

            return timed

        setattr(owner, name, make(original, spec))


def dump_trace(label: str) -> None:
    rows = sorted(TRACE.items(), key=lambda kv: -kv[1][0])
    print(f"--- trace after {label} ---", file=sys.stderr)
    for key, (total, calls) in rows:
        if calls:
            print(f"{total * 1000:9.1f} ms {calls:8d}x  {key}", file=sys.stderr)
        TRACE[key] = [0.0, 0]


def start_sampler(bench: "Bench") -> None:
    """While the GUI thread is late for its beat, note where every thread is (--sample)."""
    import threading
    import traceback

    seen: dict[str, int] = {}

    def loop() -> None:
        main_id = threading.main_thread().ident
        while True:
            time.sleep(0.01)
            late = (bench.clock.nsecsElapsed() - bench._last_beat) / 1e6
            if not bench.recording or late < 60:
                continue
            for ident, frame in sys._current_frames().items():
                where = traceback.extract_stack(frame)[-3:]
                text = " < ".join(
                    f"{os.path.basename(f.filename)}:{f.lineno} {f.name}" for f in where
                )
                key = ("MAIN " if ident == main_id else "other ") + bench.label + " | " + text
                seen[key] = seen.get(key, 0) + 1

    threading.Thread(target=loop, daemon=True).start()
    bench.sampled = seen


def _drawer_ready(window) -> Callable[[], bool]:
    drawer = window.overlays.drawer
    nav = window.nav

    def done() -> bool:
        col = drawer._columns["right"]
        parent = nav.path[-1] if nav.path else None
        return (
            drawer.isVisible()
            and bool(col.nodes)
            and not (parent is not None and nav.is_loading(parent))
        )

    return done


def run(args) -> int:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["QITV_SMOKE_MUTE"] = "1"
    os.environ["QITV_CONFIG_DIR"] = str(Path(args.out) / "config")
    from PySide6.QtWidgets import QApplication

    from services import playback
    from services.config_manager import ConfigManager
    from services.epg_manager import EpgManager
    from services.image_manager import ImageManager
    from services.provider_manager import ProviderManager
    from ui import tv_window
    from ui.keymap import Action
    from ui.tv_window import TVWindow

    sys.setswitchinterval(args.switch)  # as main.py sets it
    if args.trace:
        install_trace()
    app = QApplication.instance() or QApplication(sys.argv)
    if playback.vlc is not None:

        def muted(extra_args: Optional[list[str]] = None) -> Any:
            return playback.create_instance(
                [*(extra_args or playback.default_args()), "--aout=dummy", "--vout=dummy"]
            )

        tv_window.create_instance = muted
    config = ConfigManager()
    images = ImageManager(config, config.max_cache_image_size * 1024 * 1024)
    manager = ProviderManager(config)
    epg = EpgManager(config, manager)
    window = TVWindow(app, config, manager, images, epg)
    window.resize(1280, 720)
    window.show()
    bench = Bench(app, window)
    import gc

    gc_times: list[tuple[int, int, str]] = []
    gc_start: list[float] = []

    def watch_gc(phase: str, info: dict) -> None:
        if phase == "start":
            gc_start.append(time.perf_counter())
        elif gc_start:
            ms = (time.perf_counter() - gc_start.pop()) * 1000
            if ms > 20:
                gc_times.append((round(ms), info.get("generation", -1), bench.label))

    gc.callbacks.append(watch_gc)
    if args.sample:
        start_sampler(bench)
    bench.profile = set(args.profile)
    res = bench.results
    res["gc_over_20ms"] = gc_times
    names = window.catalogs.names()
    bench.settle(lambda: all(window.catalogs.is_ready(n) for n in names), 60)
    bench.idle(0.5)
    res["rss_start_mib"] = rss_mib()
    res["providers"] = len(names)
    handle = window.controller.handle
    drawer = window.overlays.drawer
    nav = window.nav
    bench.recording = True

    # -- the drawer on All providers: opens on the mixed All (every channel) ------------------
    bench.timed("drawer_open", lambda: handle(Action.OPEN_DRAWER), _drawer_ready(window))
    right = drawer._columns["right"]
    res["all_rows"] = len(right.nodes)
    bench.idle()

    # a key step inside the 200,000-row list
    steps = [bench.step("key_all_list", lambda: handle(Action.NAV_DOWN)) for _ in range(20)]
    res["key_step_all_list_max"] = round(max(steps), 1)
    res["key_step_all_list_mean"] = round(sum(steps) / len(steps), 1)

    # back to the groups: Left (focus the sources column), Left (up one layer), Right
    def to_groups() -> None:
        handle(Action.NAV_LEFT)
        handle(Action.NAV_LEFT)

    bench.timed("up_to_groups", to_groups, _drawer_ready(window))
    bench.idle()
    res["group_rows"] = len(drawer._columns["right"].nodes)

    # a key step across a provider's heading
    col = drawer._columns["right"]
    heads = [i for i, n in enumerate(col.nodes) if n.layer == "header"]
    if len(heads) >= 2:
        target = heads[1] - 1
        drawer.focus_side = "right"
        drawer._select(col, target)
        res["key_step_header"] = bench.step("key_header", lambda: handle(Action.NAV_DOWN))
        col.current and res.setdefault("key_step_header_on", col.current.layer)

    # open one provider's group (the biggest of the second provider), then the mixed All
    group_rows = [i for i, n in enumerate(col.nodes) if n.layer == "group" and n.key != "*"]
    pick = group_rows[len(group_rows) // 2] if group_rows else 0
    drawer.focus_side = "right"
    drawer._select(col, pick)
    bench.timed("group_open", lambda: handle(Action.NAV_RIGHT), _drawer_ready(window))
    bench.idle()
    res["group_items"] = len(drawer._columns["right"].nodes)
    gsteps = [bench.step("key_group_list", lambda: handle(Action.NAV_DOWN)) for _ in range(20)]
    res["key_step_group_list_max"] = round(max(gsteps), 1)

    # typing a filter in a group, then in the mixed All
    bench.step("filter_group_1char", lambda: handle(Action.NAV_TEXT, "k"))
    bench.step("filter_group_clear", lambda: handle(Action.NAV_BACKSPACE))
    bench.idle()

    handle(Action.NAV_LEFT)
    handle(Action.NAV_LEFT)
    bench.idle()
    col = drawer._columns["right"]
    drawer.focus_side = "right"
    first_all = next((i for i, n in enumerate(col.nodes) if n.key == "*"), 0)
    drawer._select(col, first_all)
    bench.timed("all_open", lambda: handle(Action.NAV_RIGHT), _drawer_ready(window))
    bench.idle()
    bench.step("filter_all_1char", lambda: handle(Action.NAV_TEXT, "k"))
    bench.step("filter_all_2char", lambda: handle(Action.NAV_TEXT, "a"))
    bench.step("filter_all_clear", lambda: handle(Action.ESCAPE))
    bench.idle()

    # The find (typing above ends before its pause): the genres at once, every provider's
    # channels from a worker; its first find folds every channel's title there. GUI stalls
    # meanwhile are recorded under these steps.
    finder = window.finder

    def type_find(text: str) -> None:
        for char in text:
            handle(Action.NAV_TEXT, char)

    bench.timed("find_first", lambda: type_find("spor"), lambda: not finder.searching)
    res["find_genres"], res["find_channels"] = len(finder.genres), len(finder.channels)
    bench.timed("find_more", lambda: handle(Action.NAV_TEXT, "t"), lambda: not finder.searching)
    bench.idle()
    bench.timed("find_again", lambda: type_find(" 1"), lambda: not finder.searching)
    bench.step("find_end", lambda: handle(Action.ESCAPE))
    bench.step("drawer_close", lambda: handle(Action.ESCAPE))
    bench.idle()
    res["drawer_open_again"] = None
    bench.timed("drawer_open_again", lambda: handle(Action.OPEN_DRAWER), _drawer_ready(window))
    bench.step("drawer_close_again", lambda: handle(Action.ESCAPE))
    bench.idle()

    # -- the guide over the group the zap context holds -------------------------------------
    try:
        bench.timed("guide_open", lambda: handle(Action.GUIDE))
        handle(Action.ESCAPE)
        bench.idle()
    except Exception as e:  # noqa: BLE001 - the bench reports, it does not stop
        res["guide_open_error"] = repr(e)

    # -- the Library on All providers ---------------------------------------------------------
    library = window.library_controller

    bench.timed("library_genres", lambda: window.screens.open_library("movies"))
    bench.idle(0.1)
    t0 = time.perf_counter()
    genres = window.screens.library_model.genres()
    res["library_genres_call"] = round((time.perf_counter() - t0) * 1000, 1)
    res["genres"] = len(genres)

    # -- search: the first query loads every cache and folds the titles ------------------------
    def search_first() -> None:
        window.screens.open_search()
        library._search.set_query("kanal")
        library._search.run_now()

    bench.timed("first_search", search_first, lambda: library._search.hits is not None, 60)
    res["first_search_hits"] = len(library._search.hits or [])
    bench.idle(0.1)

    def search_again() -> None:
        library._search.set_query("kanal star")
        library._search.run_now()

    library._search._hits = None
    bench.timed("second_search", search_again, lambda: library._search.hits is not None, 60)

    bench.idle(0.2)
    res["max_gui_stall_ms"] = round(bench.max_gap, 1)
    res["worst_stall_step"] = bench.worst_step
    for key, count in sorted(bench.sampled.items(), key=lambda kv: -kv[1])[:25]:
        print(f"sample {count:3d}  {key}", file=sys.stderr)
    res["stalls_over_100ms"] = bench.stalls
    import gc

    gc.collect()
    res["rss_after_mib"] = rss_mib()
    window.close()
    bench.idle(0.3)
    return report(args, res)


def report(args, res: dict) -> int:
    key_step = max(
        res.get("key_step_all_list_max", 0),
        res.get("key_step_header", 0),
        res.get("key_step_group_list_max", 0),
    )
    checks = {
        "drawer_open": (res.get("drawer_open", 1e9), TARGETS_MS["drawer_open"]),
        "group_open": (
            max(res.get("group_open", 1e9), res.get("all_open", 0)),
            TARGETS_MS["group_open"],
        ),
        "key_step": (key_step, TARGETS_MS["key_step"]),
        "library_genres": (res.get("library_genres", 1e9), TARGETS_MS["library_genres"]),
        "first_search": (res.get("first_search", 1e9), TARGETS_MS["first_search"]),
        "stall": (res.get("max_gui_stall_ms", 1e9), TARGETS_MS["stall"]),
    }
    res["targets"] = {
        k: {"value": round(v, 1), "limit": limit, "ok": v <= limit}
        for k, (v, limit) in checks.items()
    }
    out = Path(args.out) / "bench.json"
    out.write_text(json.dumps(res, indent=2), encoding="utf-8")
    for k, t in res["targets"].items():
        logger.info("%-16s %9.1f / %-6s %s", k, t["value"], t["limit"], "ok" if t["ok"] else "OVER")
    return 0 if all(t["ok"] for t in res["targets"].values()) else 1


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--providers", type=int, default=4)
    parser.add_argument("--channels", type=int, default=50000)
    parser.add_argument("--films", type=int, default=20000)
    parser.add_argument("--out", required=True)
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--switch", type=float, default=0.0005, help="sys.setswitchinterval(s)")
    parser.add_argument("--sample", action="store_true", help="show where threads are in a stall")
    parser.add_argument("--trace", action="store_true", help="time the hot functions per step")
    parser.add_argument(
        "--profile", action="append", default=[], metavar="STEP", help="print a cProfile of STEP"
    )
    parser.add_argument("--build-only", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if args.build_only:
        build_fixture(out, args.providers, args.channels, args.films)
        return 0
    if args.rebuild or not (out / "config" / "config.json").exists():
        import shutil

        shutil.rmtree(out / "config", ignore_errors=True)
        subprocess.run(
            [sys.executable, __file__, "--build-only", "--providers", str(args.providers)]
            + ["--channels", str(args.channels), "--films", str(args.films)]
            + ["--out", str(out)],
            check=True,
        )
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
