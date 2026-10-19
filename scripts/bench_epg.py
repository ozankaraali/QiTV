"""Bench for reading an XMLTV guide: how long it takes and how much memory it needs.

    uv run python scripts/bench_epg.py --channels 3000 --days 7 --per-day 25 --out <dir>

Writes a fictional XMLTV file to `<out>` (kept for later runs; `--rebuild` makes it again)
and measures, each in a fresh child process (so a peak is that step's own):

    index      EpgManager._index_programs on the file
    refresh    a Local File guide's whole refresh: parse, save (.guide), index file
    download   with --gzip: a URL guide served gzipped (nothing reaches a network): the
               body unpacked to disk, parsed and saved

For each, over `--repeat` runs: wall time (the fastest and the median), peak RSS (and the
interpreter's RSS before the step), the guide's size (channels, keys, programmes, the .guide
file) and a digest of its content, equal across versions whose result is the same.
`--tracemalloc` adds a run under tracemalloc for the peak and the retained size of Python
allocations (slower). Results go to `<out>/bench-epg.json`.

The file: `--channels` channels with two display-names each, `--days` days of about
`--per-day` programmes each with title, sub-title, desc, categories, episode-num, icon and a
nested rating; 5% of the channels are declared after their programmes and 2% are not declared
at all; some text has entities and non-ASCII letters.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

WORDS = [
    "Orbit", "Harbor", "Meridian", "Nova", "Pulse", "Atlas", "Delta", "Metro", "Zenith",
    "Café", "Müzik", "Özel", "Noticias", "Ciné", "Sabah", "Akşam", "Hafta", "Família",
    "Morning", "Evening", "Report", "Journal", "Kitchen", "Garden", "Travel", "Quiz", "Arena",
    "Studio", "Live", "Classic", "Story", "Planet", "Science", "History", "Weather", "Market",
]  # fmt: skip
CATEGORIES = [
    "News", "Sports", "Movie", "Series", "Documentary", "Kids", "Music", "Talk", "Comedy",
    "Drama", "Lifestyle", "Cooking", "Travel", "Nature", "Magazine",
]  # fmt: skip
LANGS = ["en", "de", "tr", "es", "fr"]
DURATIONS = [15, 20, 30, 30, 45, 60, 60, 60, 90, 120]
START = datetime(2026, 10, 19, tzinfo=timezone.utc)


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _channel_xml(cid: str, number: int, name: str) -> str:
    return (
        f'  <channel id="{cid}">\n'
        f"    <display-name>{_esc(name)}</display-name>\n"
        f"    <display-name>{number} {_esc(name.upper())} HD</display-name>\n"
        f'    <icon src="http://img.bench.test/c/{number}.png" />\n'
        f"  </channel>\n"
    )


def _programme_xml(rng: random.Random, cid: str, start: datetime, stop: datetime) -> str:
    lang = rng.choice(LANGS)
    title = f"{rng.choice(WORDS)} {rng.choice(WORDS)}"
    if rng.random() < 0.1:
        title += " & " + rng.choice(WORDS)
    words = [rng.choice(WORDS).lower() for _ in range(rng.randint(18, 36))]
    desc = " ".join(words).capitalize() + "."
    season, episode = rng.randint(1, 12), rng.randint(1, 30)
    cats = rng.sample(CATEGORIES, rng.choice((1, 1, 2)))
    lines = [
        f'  <programme start="{start:%Y%m%d%H%M%S} +0000" stop="{stop:%Y%m%d%H%M%S} +0000" '
        f'channel="{cid}">',
        f'    <title lang="{lang}">{_esc(title)}</title>',
        f'    <sub-title lang="{lang}">Episode {episode}</sub-title>',
        f'    <desc lang="{lang}">{_esc(desc)}</desc>',
        *(f'    <category lang="en">{c}</category>' for c in cats),
        f'    <episode-num system="xmltv_ns">{season - 1}.{episode - 1}.0/1</episode-num>',
        f'    <icon src="http://img.bench.test/p/{rng.randrange(10**6)}.jpg" />',
        f'    <rating system="age"><value>{rng.choice((0, 6, 12, 16, 18))}</value></rating>',
        "  </programme>",
    ]
    return "\n".join(lines) + "\n"


def build_xmltv(path: Path, channels: int, days: int, per_day: int, seed: int = 7) -> None:
    """Write the fictional guide to `path` (through a temporary file)."""
    rng = random.Random(seed)
    ids = [f"c{n:05d}.bench.test" for n in range(channels)]
    names = [f"{rng.choice(WORDS)} {rng.choice(CATEGORIES)} {n}" for n in range(channels)]
    late = set(rng.sample(range(channels), channels // 20))  # declared after their programmes
    rest = [n for n in range(channels) if n not in late]
    undeclared = set(rng.sample(rest, channels // 50))  # never declared
    temporary = path.with_suffix(path.suffix + ".tmp")
    with open(temporary, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        f.write('<tv generator-info-name="qitv-bench">\n')
        for n in range(channels):
            if n not in late and n not in undeclared:
                f.write(_channel_xml(ids[n], n, names[n]))
        for n in range(channels):
            moment = START
            end = START + timedelta(days=days)
            per_slot = max(1, per_day)
            while moment < end:
                minutes = rng.choice(DURATIONS) * 25 // per_slot
                stop = moment + timedelta(minutes=max(5, minutes))
                f.write(_programme_xml(rng, ids[n], moment, stop))
                moment = stop
        for n in sorted(late):
            f.write(_channel_xml(ids[n], n, names[n]))
        f.write("</tv>\n")
    os.replace(temporary, path)


def _peak_rss_mib() -> float:
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Bytes on macOS, KiB elsewhere.
    return round(peak / (1 << 20 if sys.platform == "darwin" else 1 << 10), 1)


class _Config:
    """What EpgManager reads of the configuration, with a few user channel mappings."""

    def __init__(self, directory: str) -> None:
        from services.multikeydict import MultiKeyDict

        self._directory = directory
        self.channel_epg = True
        self.epg_source = "Local File"
        self.epg_file = ""
        self.epg_url = ""
        self.epg_expiration = 3600
        self.epg_list_window_hours = 0
        self.prefer_https = False
        self.ssl_verify = True
        self.xmltv_channel_map = MultiKeyDict()
        for n in range(0, 200, 10):
            self.xmltv_channel_map[(f"c{n:05d}.bench.test", f"My Channel {n}")] = None

    def get_config_dir(self) -> str:
        return self._directory


class _GzResponse:
    """A streamed 200 answer whose body is a file's bytes (the .gz guide)."""

    status_code = 200
    headers: dict[str, str] = {}

    def __init__(self, path: str) -> None:
        self._path = path

    def iter_content(self, size: int):
        with open(self._path, "rb") as f:
            while chunk := f.read(size):
                yield chunk

    def close(self) -> None:
        pass


def _digest(guide: Any) -> str:
    import orjson

    h = hashlib.sha256()
    for keys, programmes in guide.items():
        h.update(orjson.dumps([list(keys), programmes]))
    return h.hexdigest()[:16]


def child(mode: str, path: str, trace: bool) -> dict[str, Any]:
    """Run one measured step in this (fresh) process and return its numbers."""
    import gc
    import tempfile
    from types import SimpleNamespace

    from services import epg_manager as epg
    from services.epg_store import PackedGuide

    work = tempfile.TemporaryDirectory(prefix="qitv-bench-epg-")
    config = _Config(work.name)
    manager = epg.EpgManager(config, SimpleNamespace(current_provider={}))
    if mode == "download":
        epg.requests = SimpleNamespace(get=lambda url, **kw: _GzResponse(path))  # type: ignore
    gc.collect()
    if trace:
        import tracemalloc

        tracemalloc.start()
    rss_before = _peak_rss_mib()
    t0 = time.perf_counter()
    if mode == "index":
        guide = manager._index_programs(path)
    elif mode == "refresh":
        manager._fetch_epg_from_file(epg._cache_key(path), path)
        guide = manager.epg
    else:
        manager._download_epg_from_url("http://127.0.0.1:9/guide.xml.gz")
        guide = manager.epg
    wall = time.perf_counter() - t0
    out: dict[str, Any] = {"mode": mode, "wall_s": round(wall, 2)}
    gc.collect()
    if trace:
        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        return {
            "traced_wall_s": out["wall_s"],  # slower under tracemalloc
            "traced_peak_mib": round(peak / (1 << 20), 1),
            "traced_kept_mib": round(current / (1 << 20), 1),
        }
    out["rss_before_mib"] = rss_before
    out["peak_rss_mib"] = _peak_rss_mib()
    out["channels"] = len(guide)
    out["keys"] = len(guide._keys_map)
    if isinstance(guide, PackedGuide):
        files = [f for f in os.listdir(manager._cache_dir()) if f.endswith(".guide")]
        out["guide_file_mib"] = round(
            sum(os.path.getsize(os.path.join(manager._cache_dir(), f)) for f in files) / (1 << 20),
            1,
        )
    out["programmes"] = sum(len(programmes) for _keys, programmes in guide.items())
    out["digest"] = _digest(guide)
    work.cleanup()
    return out


def _run_child(mode: str, path: Path, trace: bool) -> dict[str, Any]:
    cmd = [sys.executable, __file__, "--child", mode, "--file", str(path)]
    if trace:
        cmd.append("--tracemalloc")
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    done = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=ROOT)
    if done.returncode != 0:
        sys.stderr.write(done.stderr)
        raise SystemExit(f"{mode} failed")
    result: dict[str, Any] = json.loads(done.stdout.strip().splitlines()[-1])
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--channels", type=int, default=3000)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--per-day", type=int, default=25)
    parser.add_argument("--out", type=Path, help="where the guide and bench-epg.json go")
    parser.add_argument("--gzip", action="store_true", help="also time a gzipped download")
    parser.add_argument("--tracemalloc", action="store_true")
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--repeat", type=int, default=3, help="runs per step (time: min, median)")
    parser.add_argument("--only", choices=("index", "refresh", "download"), action="append")
    parser.add_argument("--child", choices=("index", "refresh", "download"), help=argparse.SUPPRESS)
    parser.add_argument("--file", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.child:
        print(json.dumps(child(args.child, args.file, args.tracemalloc)))
        return 0
    if args.out is None:
        parser.error("--out is required")

    args.out.mkdir(parents=True, exist_ok=True)
    xml = args.out / f"epg-{args.channels}x{args.days}x{args.per_day}.xml"
    if args.rebuild or not xml.exists():
        t0 = time.perf_counter()
        build_xmltv(xml, args.channels, args.days, args.per_day)
        print(f"built {xml.name} in {time.perf_counter() - t0:.1f} s", file=sys.stderr)
    gz = xml.with_suffix(".xml.gz")
    if args.gzip and (args.rebuild or not gz.exists()):
        with open(xml, "rb") as src, gzip.open(gz, "wb", compresslevel=6) as dst:
            while chunk := src.read(1 << 20):
                dst.write(chunk)

    results: dict[str, Any] = {
        "file": xml.name,
        "file_mib": round(xml.stat().st_size / (1 << 20), 1),
        "steps": [],
    }
    if args.gzip:
        results["gz_mib"] = round(gz.stat().st_size / (1 << 20), 1)
    modes = args.only or ["index", "refresh"] + (["download"] if args.gzip else [])
    for mode in modes:
        source = gz if mode == "download" else xml
        runs = [_run_child(mode, source, False) for _ in range(max(1, args.repeat))]
        walls = sorted(run["wall_s"] for run in runs)
        step = {**runs[0], "wall_s": walls[0], "wall_median_s": walls[len(walls) // 2]}
        step["peak_rss_mib"] = max(run["peak_rss_mib"] for run in runs)
        if len({run["digest"] for run in runs}) != 1:
            raise SystemExit(f"{mode}: the runs' guides differ")
        if args.tracemalloc:
            step.update(_run_child(mode, source, True))
        results["steps"].append(step)
        print(json.dumps(step), file=sys.stderr)
    (args.out / "bench-epg.json").write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
