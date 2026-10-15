# Phase 1A TV Services Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the widget-free services the TV shell runs on:
- provider and catalog access;
- the layered navigation model;
- zapping with debounce, digit entry and previous channel;
- normalized now/next EPG;
- VLC media profiles;
- a polled libVLC playback session;
- watch-state bookkeeping and external players.

All of it is unit-tested without network access or a real UI.

**Architecture:** New modules under `services/` wrap the existing v1.13.8 workers, ProviderManager, EpgManager and ConfigManager.
- Background work runs through a `JobRunner`, which tests replace with a synchronous `ImmediateRunner`.
- Workers come from a `WorkerFactory`, which tests replace with fakes.
- Nothing here creates widgets.
- Phase 1B (separate plan) builds the window and overlays on these interfaces.

**Tech Stack:** Python 3.14, PySide6 6.11.2 (QtCore only in these modules), python-vlc 3.0.21203 against libVLC 3 from the official VLC installers, `unittest`, `uv`.

**Spec:** `docs/superpowers/specs/2026-10-15-tv-on-vlc-design.md`: §4.1 (modules), §4.2 (data flow), §5.2–5.3 (layers, zapping), §6.6 (VLC profile), §6.9 (Xtream `max_connections`, recorded here and enforced in Phase 2), §8 (settings, migration). Reference map of the existing code: `.superpowers/sdd/phase1-interface-map.md`.

## Global Constraints

- Python `>=3.14,<3.15`. Run everything through `uv run …` from `/Users/ozankaraali/QiTV` on branch `tv/vlc`.
- **No native or compiled dependencies and no new runtime dependencies** (spec D1/D2).
- Never block the GUI thread on network or disk-heavy work: catalog, EPG, link and provider setup always go through `JobRunner`.
- **Thread affinity:**
  - `JobRunner` callbacks must be bound methods of QObjects living on the GUI thread, connected queued.
  - Plain lambdas are not used as cross-thread slots.
  - Never create QPixmap/QIcon off the GUI thread.
- Logging uses `logging.getLogger(__name__)`. No `print` and no silent `except: pass` (log at debug level at least).
- Tests use `unittest` with `QT_QPA_PLATFORM=offscreen`, run as `uv run python -m unittest discover -s tests -v`.
- Lint: `uv run flake8 --select=E9,F63,F7,F82 <files>`. Format: black and isort, line length 100. mypy: `uv run mypy <new module>` reports no errors in new modules.
- Do not edit `video_player.py`, `channel_list.py`, `mixins/*.py` or `widgets/*.py`; the old UI stays untouched until Phase 3. `workers.py` and `config_manager.py` may be extended exactly as tasks state.
- Spec values used here (copy exactly):
  - zapping: rest before tuning 400 ms; burst window 1 s; digit commit 1.2 s; digits ≤ 4;
  - Live latency choices `low` / `normal` / `stable`, default `normal`;
  - Mouse wheel choices `channel` / `volume`, default `channel`;
  - *Start with last channel* default on.
- Commits end with these two lines:
  ```
  Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_011UFrnH8jUNCzkFNLKJaoRD
  ```
  Stage files explicitly; never use `git add -A`.

## Review Focus

1. **Results that arrive after the provider changed:** switching provider while a section, group, link or EPG job is in flight must drop the late result. It must not mix it into the new provider's catalog or play the old channel (Tasks 5 and 6).
2. **Empty and odd catalogs:**
   - an M3U legacy flat list;
   - a provider with zero categories;
   - categories whose count is 0 (hidden);
   - STB on-demand groups not yet loaded (count `None`).
   The drawer must never crash or show an empty "All" (Task 5).
3. **Zapping edge cases:**
   - wrap-around at both ends;
   - a single-channel group;
   - digits for a number that doesn't exist (no tune, digits cleared);
   - leading zeros (`"007"` matches channel `7`);
   - "previous channel" before any tune (Task 8).
4. **EPG data:**
   - missing or `None` titles; titles given as a list of language variants;
   - a stop time not after the start (dropped);
   - STB naive times (local) mixed with XMLTV offsets;
   - an EPG manager that raises while it is being reloaded (empty result, no exception) (Tasks 2 and 5).
5. **libVLC sessions that never reach Playing** (a bad URL): the session must report an error state within its timeout, and `stop()` must be safe in any state, including before `play()` (Task 9).

---

### Task 1: Config keys for the TV shell

**Files:**
- Modify: `config_manager.py`
- Test: `tests/test_config_tv.py`

**Interfaces:**
- Produces these `ConfigManager` properties:
  - `live_latency: str` (`"low"`, `"normal"` or `"stable"`)
  - `mouse_wheel_action: str` (`"channel"` or `"volume"`)
  - `start_with_last_channel: bool`
  - `last_live_channel: dict | None`, shaped `{"provider_name": str, "group_id": str, "item": dict}`
- Produces `window_positions["tv_window"] = {x, y, width, height}`.
- Produces class constants `LIVE_LATENCY_CHOICES`, `MOUSE_WHEEL_CHOICES` and `TV_DEFAULTS_VERSION = 1`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_config_tv.py`:
```python
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from config_manager import ConfigManager


class TvConfigTests(unittest.TestCase):
    def _load(self, saved):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "config.json"
        if saved is not None:
            path.write_text(json.dumps(saved), encoding="utf-8")
        patches = (
            patch.object(ConfigManager, "_get_config_path", return_value=str(path)),
            patch.object(ConfigManager, "_migrate_old_config"),
        )
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return ConfigManager(), path

    def test_new_install_has_tv_defaults(self):
        config, _ = self._load(None)
        self.assertEqual(config.live_latency, "normal")
        self.assertEqual(config.mouse_wheel_action, "channel")
        self.assertTrue(config.start_with_last_channel)
        self.assertIsNone(config.last_live_channel)
        self.assertTrue(config.channel_logos)
        self.assertTrue(config.channel_epg)
        self.assertIn("tv_window", config.config["window_positions"])

    def test_tv_window_starts_where_the_larger_old_window_was(self):
        saved = {
            "window_positions": {
                "channel_list": {"x": 1, "y": 2, "width": 400, "height": 800},
                "video_player": {"x": 50, "y": 60, "width": 1200, "height": 700},
            }
        }
        config, _ = self._load(saved)
        self.assertEqual(
            config.config["window_positions"]["tv_window"],
            {"x": 50, "y": 60, "width": 1200, "height": 700},
        )

    def test_existing_tv_window_is_kept(self):
        tv = {"x": 9, "y": 9, "width": 900, "height": 500}
        config, _ = self._load({"window_positions": {"tv_window": tv}})
        self.assertEqual(config.config["window_positions"]["tv_window"], tv)

    def test_one_time_migration_turns_on_logos_and_epg(self):
        config, path = self._load({"channel_logos": False, "channel_epg": False})
        self.assertTrue(config.channel_logos)
        self.assertTrue(config.channel_epg)
        config.channel_epg = False
        config.save_config()
        reloaded = ConfigManager()
        self.assertFalse(reloaded.channel_epg, "the migration must run only once")
        self.assertEqual(
            json.loads(path.read_text(encoding="utf-8"))["tv_defaults_version"],
            ConfigManager.TV_DEFAULTS_VERSION,
        )

    def test_invalid_choices_fall_back_to_defaults(self):
        config, _ = self._load({"live_latency": "turbo", "mouse_wheel_action": 3})
        self.assertEqual(config.live_latency, "normal")
        self.assertEqual(config.mouse_wheel_action, "channel")
        config.live_latency = "low"
        self.assertEqual(config.live_latency, "low")

    def test_last_live_channel_round_trips(self):
        config, _ = self._load(None)
        record = {"provider_name": "P", "group_id": "7", "item": {"id": 1, "name": "One"}}
        config.last_live_channel = record
        config.save_config()
        self.assertEqual(ConfigManager().last_live_channel, record)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run python -m unittest tests.test_config_tv -v`
Expected: FAIL/ERROR (`AttributeError: 'ConfigManager' object has no attribute 'live_latency'`, …).

- [ ] **Step 3: Implement in `config_manager.py`**

1. Below `_config_property`, add:
```python
def _one_of(choices, default):
    """Coerce a stored value to one of `choices`, falling back to `default`."""

    def coerce(value):
        return value if value in choices else default

    return coerce
```
2. In `class ConfigManager`:
   - set `DEFAULT_OPTION_CHANNEL_EPG = True` and `DEFAULT_OPTION_CHANNEL_LOGO = True`;
   - add these constants after the existing `DEFAULT_OPTION_*` block:
```python
    DEFAULT_OPTION_LIVE_LATENCY = "normal"
    LIVE_LATENCY_CHOICES = ("low", "normal", "stable")
    DEFAULT_OPTION_MOUSE_WHEEL = "channel"
    MOUSE_WHEEL_CHOICES = ("channel", "volume")
    DEFAULT_OPTION_START_WITH_LAST_CHANNEL = True
    TV_DEFAULTS_VERSION = 1
    DEFAULT_TV_WINDOW = {"x": 80, "y": 80, "width": 1280, "height": 720}
```
3. In `default_config()`:
   - add `"tv_window": dict(ConfigManager.DEFAULT_TV_WINDOW)` to `"window_positions"`;
   - add the keys `"live_latency": ConfigManager.DEFAULT_OPTION_LIVE_LATENCY`, `"mouse_wheel_action": ConfigManager.DEFAULT_OPTION_MOUSE_WHEEL`, `"start_with_last_channel": ConfigManager.DEFAULT_OPTION_START_WITH_LAST_CHANNEL`, `"last_live_channel": None`, `"tv_defaults_version": ConfigManager.TV_DEFAULTS_VERSION`.
4. In `update_patcher`, immediately after `window_positions = self.config.setdefault("window_positions", {})` and **before** the loop that fills missing windows, insert:
```python
        if "tv_window" not in window_positions:
            window_positions["tv_window"] = self._initial_tv_window(window_positions)
            need_update = True
```
   Then add `"live_latency"`, `"mouse_wheel_action"`, `"start_with_last_channel"` (with their defaults) and `"last_live_channel": None` to `missing_defaults`. After the `missing_defaults` loop, insert:
```python
        if self.config.get("tv_defaults_version", 0) < ConfigManager.TV_DEFAULTS_VERSION:
            # 2.0 is a TV: the zap banner and channel list need logos and now/next.
            self.config["channel_logos"] = True
            self.config["channel_epg"] = True
            self.config["tv_defaults_version"] = ConfigManager.TV_DEFAULTS_VERSION
            need_update = True
```
5. Add this static method next to `default_config`:
```python
    @staticmethod
    def _initial_tv_window(window_positions):
        """Place the single TV window where the larger of the two old windows was."""
        keys = ("x", "y", "width", "height")
        sized = [
            geometry
            for geometry in (window_positions.get("video_player"), window_positions.get("channel_list"))
            if isinstance(geometry, dict) and all(isinstance(geometry.get(k), int) for k in keys)
        ]
        if not sized:
            return dict(ConfigManager.DEFAULT_TV_WINDOW)
        best = max(sized, key=lambda geometry: geometry["width"] * geometry["height"])
        return {key: best[key] for key in keys}
```
6. Add these properties next to the other `_config_property` declarations:
```python
    live_latency = _config_property(
        "live_latency",
        DEFAULT_OPTION_LIVE_LATENCY,
        coerce=_one_of(LIVE_LATENCY_CHOICES, DEFAULT_OPTION_LIVE_LATENCY),
    )
    mouse_wheel_action = _config_property(
        "mouse_wheel_action",
        DEFAULT_OPTION_MOUSE_WHEEL,
        coerce=_one_of(MOUSE_WHEEL_CHOICES, DEFAULT_OPTION_MOUSE_WHEEL),
    )
    start_with_last_channel = _config_property(
        "start_with_last_channel", DEFAULT_OPTION_START_WITH_LAST_CHANNEL, coerce=bool
    )
    last_live_channel = _config_property("last_live_channel", None)
```

- [ ] **Step 4: Run to verify they pass, then run the full suite**

Run: `uv run python -m unittest tests.test_config_tv -v && uv run python -m unittest discover -s tests 2>&1 | tail -3`

Expected: 6 PASS and a green suite. If `tests/test_config_manager.py` asserted the old `False` defaults for logos and EPG, update only those assertions to the new defaults, and say so in the report.

- [ ] **Step 5: Lint, format, commit**

```bash
uv run black config_manager.py tests/test_config_tv.py && uv run isort config_manager.py tests/test_config_tv.py
uv run flake8 --select=E9,F63,F7,F82 config_manager.py tests/test_config_tv.py
git add config_manager.py tests/test_config_tv.py
git commit -F - <<'EOF'
feat: add TV shell settings and one-time 2.0 defaults

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_011UFrnH8jUNCzkFNLKJaoRD
EOF
```

---

### Task 2: Normalized now/next EPG (`services/epg_view.py`)

**Files:**
- Create: `services/epg_view.py`
- Test: `tests/test_epg_view.py`

**Interfaces:**
- Produces:
  - `Programme(title: str, start: datetime, stop: datetime, description: str = "")`: frozen; times are timezone-aware. Methods `is_on(now) -> bool` and `progress(now) -> float` (0..1).
  - `parse_programme(raw: dict) -> Programme | None`: accepts XMLTV dicts (`@start`/`@stop`/`title`/`desc`) and STB dicts (`time`/`time_to`/`name`/`descr`).
  - `normalize(raw_programmes) -> list[Programme]`, sorted by start.
  - `now_next(programmes, now) -> tuple[Programme | None, Programme | None]`.
  - `programmes_for_channel(epg_manager, channel: dict, now: datetime, limit: int = 8) -> list[Programme]`: programmes that have not ended yet. Returns `[]` on any manager error.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_epg_view.py`:
```python
from datetime import datetime, timedelta, timezone
import unittest

from services.epg_view import Programme, normalize, now_next, parse_programme, programmes_for_channel

PLUS2 = timezone(timedelta(hours=2))


def xmltv(start, stop, title="Show", desc=None):
    raw = {"@start": start, "@stop": stop, "title": {"__text": title}}
    if desc is not None:
        raw["desc"] = {"__text": desc}
    return raw


class ParseProgrammeTests(unittest.TestCase):
    def test_xmltv(self):
        p = parse_programme(xmltv("20261015200000 +0200", "20261015210000 +0200", "News", "Daily"))
        self.assertEqual(p.title, "News")
        self.assertEqual(p.description, "Daily")
        self.assertEqual(p.start, datetime(2026, 10, 15, 20, 0, tzinfo=PLUS2))
        self.assertEqual(p.stop - p.start, timedelta(hours=1))

    def test_xmltv_title_list_and_missing_title(self):
        raw = xmltv("20261015200000 +0200", "20261015210000 +0200")
        raw["title"] = [{"__text": ""}, {"__text": "Nachrichten", "@lang": "de"}]
        self.assertEqual(parse_programme(raw).title, "Nachrichten")
        raw["title"] = None
        self.assertEqual(parse_programme(raw).title, "Untitled")

    def test_stb_naive_times_are_local(self):
        raw = {"name": "Film", "descr": "d", "time": "2026-10-15 20:00:00", "time_to": "2026-10-15 22:00:00"}
        p = parse_programme(raw)
        self.assertEqual(p.start, datetime(2026, 10, 15, 20, 0).astimezone())
        self.assertIsNotNone(p.start.tzinfo)
        self.assertEqual(p.title, "Film")

    def test_invalid_entries_are_dropped(self):
        self.assertIsNone(parse_programme({"foo": 1}))
        self.assertIsNone(parse_programme(xmltv("20261015210000 +0200", "20261015200000 +0200")))
        self.assertIsNone(parse_programme(xmltv("bad", "20261015200000 +0200")))


class NowNextTests(unittest.TestCase):
    def setUp(self):
        self.programmes = normalize(
            [
                xmltv("20261015210000 +0200", "20261015220000 +0200", "Late"),
                xmltv("20261015200000 +0200", "20261015210000 +0200", "Prime"),
                xmltv("20261015190000 +0200", "20261015200000 +0200", "Early"),
            ]
        )

    def test_sorted(self):
        self.assertEqual([p.title for p in self.programmes], ["Early", "Prime", "Late"])

    def test_now_and_next(self):
        now = datetime(2026, 10, 15, 20, 30, tzinfo=PLUS2)
        current, upcoming = now_next(self.programmes, now)
        self.assertEqual((current.title, upcoming.title), ("Prime", "Late"))
        self.assertAlmostEqual(current.progress(now), 0.5)

    def test_gap_has_no_current_but_a_next(self):
        now = datetime(2026, 10, 15, 18, 0, tzinfo=PLUS2)
        current, upcoming = now_next(self.programmes, now)
        self.assertIsNone(current)
        self.assertEqual(upcoming.title, "Early")

    def test_after_last(self):
        now = datetime(2026, 10, 16, 1, 0, tzinfo=PLUS2)
        self.assertEqual(now_next(self.programmes, now), (None, None))


class ProgrammesForChannelTests(unittest.TestCase):
    def test_filters_ended_and_limits(self):
        manager = type("M", (), {})()
        manager.get_programs_for_channel = lambda channel, start, limit: [
            xmltv("20261015190000 +0200", "20261015200000 +0200", "Ended"),
            xmltv("20261015200000 +0200", "20261015210000 +0200", "On"),
            xmltv("20261015210000 +0200", "20261015220000 +0200", "Next"),
        ]
        now = datetime(2026, 10, 15, 20, 30, tzinfo=PLUS2)
        result = programmes_for_channel(manager, {"id": 1}, now, limit=1)
        self.assertEqual([p.title for p in result], ["On"])

    def test_manager_errors_give_empty_list(self):
        manager = type("M", (), {})()

        def boom(*args):
            raise RuntimeError("dictionary changed size during iteration")

        manager.get_programs_for_channel = boom
        self.assertEqual(programmes_for_channel(manager, {"id": 1}, datetime.now().astimezone()), [])


class ProgrammeTests(unittest.TestCase):
    def test_progress_clamps(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        p = Programme("T", start, start + timedelta(hours=1))
        self.assertEqual(p.progress(start - timedelta(hours=1)), 0.0)
        self.assertEqual(p.progress(start + timedelta(hours=2)), 1.0)
        self.assertTrue(p.is_on(start))
        self.assertFalse(p.is_on(start + timedelta(hours=1)))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run python -m unittest tests.test_epg_view -v`
Expected: `ModuleNotFoundError: No module named 'services.epg_view'`.

- [ ] **Step 3: Implement `services/epg_view.py`**

```python
"""Normalized EPG programmes for the TV shell: one shape for STB and XMLTV data (no Qt)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

_XMLTV_FORMATS = ("%Y%m%d%H%M%S %z", "%Y%m%d%H%M%S")
_STB_FORMAT = "%Y-%m-%d %H:%M:%S"


@dataclass(frozen=True)
class Programme:
    title: str
    start: datetime  # timezone-aware
    stop: datetime  # timezone-aware
    description: str = ""

    def is_on(self, now: datetime) -> bool:
        return self.start <= now < self.stop

    def progress(self, now: datetime) -> float:
        span = (self.stop - self.start).total_seconds()
        if span <= 0:
            return 0.0
        return min(1.0, max(0.0, (now - self.start).total_seconds() / span))


def _text(value: Any) -> str:
    """XMLTV text arrives as {"__text": ...}, a list of language variants, or a plain value."""
    if isinstance(value, list):
        for entry in value:
            text = _text(entry)
            if text:
                return text
        return ""
    if isinstance(value, dict):
        return str(value.get("__text") or "").strip()
    return str(value or "").strip()


def _aware(moment: datetime) -> datetime:
    # Naive times (STB portals, offset-less XMLTV) are local wall-clock times.
    return moment if moment.tzinfo else moment.astimezone()


def _parse_xmltv_time(value: Any) -> Optional[datetime]:
    for fmt in _XMLTV_FORMATS:
        try:
            return _aware(datetime.strptime(str(value).strip(), fmt))
        except ValueError:
            continue
    return None


def _parse_stb_time(value: Any) -> Optional[datetime]:
    try:
        return _aware(datetime.strptime(str(value).strip(), _STB_FORMAT))
    except ValueError:
        return None


def parse_programme(raw: dict) -> Optional[Programme]:
    if "@start" in raw:
        start, stop = _parse_xmltv_time(raw.get("@start")), _parse_xmltv_time(raw.get("@stop"))
        title, description = _text(raw.get("title")), _text(raw.get("desc"))
    elif "time" in raw:
        start, stop = _parse_stb_time(raw.get("time")), _parse_stb_time(raw.get("time_to"))
        title, description = _text(raw.get("name")), _text(raw.get("descr"))
    else:
        return None
    if start is None or stop is None or stop <= start:
        return None
    return Programme(title or "Untitled", start, stop, description)


def normalize(raw_programmes: Optional[Iterable[dict]]) -> list[Programme]:
    parsed = [p for p in (parse_programme(raw) for raw in raw_programmes or []) if p is not None]
    parsed.sort(key=lambda p: p.start)
    return parsed


def now_next(
    programmes: list[Programme], now: datetime
) -> tuple[Optional[Programme], Optional[Programme]]:
    current = next((p for p in programmes if p.is_on(now)), None)
    boundary = current.stop if current else now
    upcoming = next((p for p in programmes if p.start >= boundary), None)
    return current, upcoming


def programmes_for_channel(epg_manager, channel: dict, now: datetime, limit: int = 8) -> list[Programme]:
    """Programmes that have not ended yet, oldest first; [] if the EPG is unavailable."""
    try:
        raw = epg_manager.get_programs_for_channel(channel, None, 0)
    except Exception as e:  # the manager may be reloading on a worker thread
        logger.debug("EPG lookup failed for %s: %s", channel.get("name"), e)
        return []
    return [p for p in normalize(raw) if p.stop > now][:limit]
```

- [ ] **Step 4: Run to verify they pass, then run the suite.** Run: `uv run python -m unittest tests.test_epg_view -v && uv run python -m unittest discover -s tests 2>&1 | tail -3`. Expected: 12 PASS and a green suite.

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run black services/epg_view.py tests/test_epg_view.py && uv run isort services/epg_view.py tests/test_epg_view.py
uv run flake8 --select=E9,F63,F7,F82 services/epg_view.py tests/test_epg_view.py && uv run mypy services/epg_view.py
git add services/epg_view.py tests/test_epg_view.py
git commit -F - <<'EOF'
feat: normalize STB and XMLTV programmes into now/next

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_011UFrnH8jUNCzkFNLKJaoRD
EOF
```

---

### Task 3: VLC media profiles (`services/vlc_profile.py`)

Spec §6.6 changes from v1.13.8:
- remove `:adaptive-logic=lowest` (it always picked the lowest HLS quality);
- remove `:no-audio-time-stretch` (catch-up at more than 1× needs time-stretch);
- live caching follows the *Live latency* setting;
- HLS starts closer to live through `adaptive-livedelay`.

**Files:**
- Create: `services/vlc_profile.py`
- Test: `tests/test_vlc_profile.py`

**Interfaces:**
- Produces:
  - `PlayKind` (an Enum of str) with `LIVE`, `VOD_FILE`, `VOD_STREAM` and `UNKNOWN`;
  - `classify(url: str, is_live: bool | None) -> PlayKind`;
  - `media_options(kind: PlayKind, *, latency: str = "normal", user_agent: str | None = None) -> list[str]`;
  - `DEFAULT_USER_AGENT = "VLC/3.0.20"`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_vlc_profile.py`:
```python
import unittest

from services.vlc_profile import DEFAULT_USER_AGENT, PlayKind, classify, media_options


def option(options, name):
    matches = [o for o in options if o.startswith(f":{name}=") or o == f":{name}"]
    return matches[0].split("=", 1)[1] if matches and "=" in matches[0] else (matches[0] if matches else None)


class ClassifyTests(unittest.TestCase):
    def test_containers_are_vod_files(self):
        for url in ("http://h/movie/u/p/1.mkv", "http://h/a.MP4?token=1", "http://h/x.webm"):
            self.assertIs(classify(url, True), PlayKind.VOD_FILE, url)

    def test_hints(self):
        self.assertIs(classify("http://h/live/u/p/1.ts", True), PlayKind.LIVE)
        self.assertIs(classify("http://h/series/1.m3u8", False), PlayKind.VOD_STREAM)
        self.assertIs(classify("http://h/stream", None), PlayKind.UNKNOWN)


class MediaOptionsTests(unittest.TestCase):
    def test_removed_v1_options(self):
        for kind in PlayKind:
            options = media_options(kind)
            self.assertNotIn(":adaptive-logic=lowest", options)
            self.assertNotIn(":no-audio-time-stretch", options)

    def test_live_latency_drives_caching(self):
        low = media_options(PlayKind.LIVE, latency="low")
        stable = media_options(PlayKind.LIVE, latency="stable")
        self.assertEqual(option(low, "network-caching"), "300")
        self.assertEqual(option(stable, "network-caching"), "3000")
        self.assertEqual(option(low, "adaptive-livedelay"), "3000")
        self.assertEqual(option(media_options(PlayKind.LIVE), "network-caching"), "1000")
        self.assertEqual(option(media_options(PlayKind.LIVE, latency="??"), "network-caching"), "1000")

    def test_unknown_is_live_like_but_never_below_1500ms(self):
        options = media_options(PlayKind.UNKNOWN, latency="low")
        self.assertEqual(option(options, "network-caching"), "1500")
        self.assertIn(":clock-jitter=0", options)

    def test_vod_file_profile(self):
        options = media_options(PlayKind.VOD_FILE)
        self.assertEqual(option(options, "network-caching"), "4000")
        self.assertIn(":input-fast-seek", options)
        self.assertNotIn(":clock-jitter=0", options)

    def test_user_agent(self):
        self.assertIn(f":http-user-agent={DEFAULT_USER_AGENT}", media_options(PlayKind.LIVE))
        self.assertIn(":http-user-agent=Custom/1", media_options(PlayKind.LIVE, user_agent="Custom/1"))

    def test_common_options(self):
        for kind in PlayKind:
            options = media_options(kind)
            self.assertIn(":http-reconnect=true", options)
            self.assertIn(":avcodec-hw=any", options)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify they fail.** Run: `uv run python -m unittest tests.test_vlc_profile -v`. Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement `services/vlc_profile.py`**

```python
"""libVLC media options per playback kind (pure; spec §6.6)."""

from enum import Enum
from typing import Optional
from urllib.parse import urlsplit

DEFAULT_USER_AGENT = "VLC/3.0.20"
_VOD_CONTAINERS = (".mkv", ".mp4", ".avi", ".webm", ".mov", ".m4v")
# Live network caching per Settings → Live latency (ms) and HLS distance from the live edge.
_LIVE_CACHING_MS = {"low": 300, "normal": 1000, "stable": 3000}
_LIVE_DELAY_MS = {"low": 3000, "normal": 6000, "stable": 12000}
_UNKNOWN_MIN_CACHING_MS = 1500


class PlayKind(str, Enum):
    LIVE = "live"  # a live channel played directly from the provider
    VOD_FILE = "vod_file"  # a container file: mkv/mp4/avi/webm/mov/m4v
    VOD_STREAM = "vod_stream"  # known on-demand content in a stream format (ts/m3u8)
    UNKNOWN = "unknown"  # M3U entries: may be live or on demand


def classify(url: str, is_live: Optional[bool]) -> PlayKind:
    if urlsplit(url).path.lower().endswith(_VOD_CONTAINERS):
        return PlayKind.VOD_FILE
    if is_live is True:
        return PlayKind.LIVE
    if is_live is False:
        return PlayKind.VOD_STREAM
    return PlayKind.UNKNOWN


def media_options(
    kind: PlayKind, *, latency: str = "normal", user_agent: Optional[str] = None
) -> list[str]:
    options = [
        f":http-user-agent={user_agent or DEFAULT_USER_AGENT}",
        ":avcodec-hw=any",
        ":avcodec-fast",
        ":http-reconnect=true",
    ]
    if kind in (PlayKind.LIVE, PlayKind.UNKNOWN):
        caching = _LIVE_CACHING_MS.get(latency, _LIVE_CACHING_MS["normal"])
        if kind is PlayKind.UNKNOWN:
            caching = max(caching, _UNKNOWN_MIN_CACHING_MS)
        delay = _LIVE_DELAY_MS.get(latency, _LIVE_DELAY_MS["normal"])
        options += [
            f":network-caching={caching}",
            f":live-caching={caching}",
            ":clock-jitter=0",
            f":adaptive-livedelay={delay}",
        ]
    elif kind is PlayKind.VOD_FILE:
        options += [
            ":network-caching=4000",
            ":file-caching=2000",
            ":input-timeshift-granularity=0",
            ":input-fast-seek",
            ":http-forward-cookies=true",
        ]
    else:
        options += [":network-caching=2000", ":file-caching=1500", ":live-caching=1000"]
    return options
```

- [ ] **Step 4: Run to verify they pass, then run the suite.** Expected: 8 PASS and a green suite.

- [ ] **Step 5: Lint, type-check, commit** (`feat: add VLC media profiles with live latency`), with the same command pattern as Task 2 for `services/vlc_profile.py` and `tests/test_vlc_profile.py`.

---

### Task 4: Job runner (`services/jobs.py`)

**Files:**
- Create: `services/jobs.py`
- Test: `tests/test_jobs.py`

**Interfaces:**
- Produces:
  - `JobRunner(QObject)`:
    - `run_worker(worker, on_finished, on_error=None) -> worker`. The worker is a QObject with `run()`, `finished(object)` and `error(str)`. It runs on a new QThread; callbacks are delivered queued to the GUI thread, and the thread is retired through `ThreadCleanup`.
    - `pending() -> int`.
  - `ImmediateRunner`, with the same interface: `run_worker` connects the signals directly and calls `worker.run()` synchronously. It is used only in tests.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_jobs.py`:
```python
import os
import threading
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from services.jobs import ImmediateRunner, JobRunner


class _Worker(QObject):
    finished = Signal(object)
    error = Signal(str)

    def __init__(self, fail=False):
        super().__init__()
        self.fail = fail

    def run(self):
        if self.fail:
            self.error.emit("boom")
        else:
            self.finished.emit({"thread": threading.get_ident()})


class _Receiver(QObject):
    def __init__(self):
        super().__init__()
        self.results, self.errors, self.threads = [], [], []

    def on_result(self, payload):
        self.results.append(payload)
        self.threads.append(threading.get_ident())

    def on_error(self, message):
        self.errors.append(message)


class JobRunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_worker_runs_off_thread_and_result_arrives_on_gui_thread(self):
        runner, receiver = JobRunner(), _Receiver()
        runner.run_worker(_Worker(), receiver.on_result, receiver.on_error)
        for _ in range(200):
            if receiver.results and runner.pending() == 0:
                break
            QTest.qWait(10)
        self.assertEqual(len(receiver.results), 1)
        self.assertNotEqual(receiver.results[0]["thread"], threading.get_ident())
        self.assertEqual(receiver.threads, [threading.get_ident()])
        self.assertEqual(runner.pending(), 0)

    def test_errors_are_delivered(self):
        runner, receiver = JobRunner(), _Receiver()
        runner.run_worker(_Worker(fail=True), receiver.on_result, receiver.on_error)
        for _ in range(200):
            if receiver.errors and runner.pending() == 0:
                break
            QTest.qWait(10)
        self.assertEqual(receiver.errors, ["boom"])

    def test_immediate_runner_is_synchronous(self):
        receiver = _Receiver()
        ImmediateRunner().run_worker(_Worker(), receiver.on_result, receiver.on_error)
        self.assertEqual(len(receiver.results), 1)
        self.assertEqual(receiver.results[0]["thread"], threading.get_ident())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify they fail.** Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement `services/jobs.py`**

```python
"""Run QObject workers off the GUI thread; deliver their results on it."""

import logging
from typing import Callable, Optional

from PySide6.QtCore import QObject, Qt, QThread

from services.thread_cleanup import ThreadCleanup

logger = logging.getLogger(__name__)


class JobRunner(QObject):
    """Owns background jobs until their native threads are joined (see ThreadCleanup).

    Callbacks must be bound methods of QObjects that live on the GUI thread: they are
    connected queued, so they always run there.
    """

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._jobs: dict[QThread, QObject] = {}

    def run_worker(self, worker: QObject, on_finished: Callable, on_error: Optional[Callable] = None):
        thread = QThread()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(on_finished, Qt.QueuedConnection)
        if on_error is not None:
            worker.error.connect(on_error, Qt.QueuedConnection)
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)
        self._jobs[thread] = worker
        ThreadCleanup(thread, worker).finished.connect(self._retire)
        thread.start()
        return worker

    def _retire(self, thread: QThread) -> None:
        self._jobs.pop(thread, None)

    def pending(self) -> int:
        return len(self._jobs)


class ImmediateRunner:
    """Test double: runs a worker synchronously on the calling thread with direct signals."""

    def run_worker(self, worker: QObject, on_finished: Callable, on_error: Optional[Callable] = None):
        worker.finished.connect(on_finished)
        if on_error is not None:
            worker.error.connect(on_error)
        worker.run()
        return worker

    def pending(self) -> int:
        return 0
```

- [ ] **Step 4: Verify.** Expected: 3 PASS and a green suite. If `ThreadCleanup.finished` delivers no argument, adjust `_retire` to take the thread from the cleanup object's closure instead (read `services/thread_cleanup.py` first) and note it in the report.

- [ ] **Step 5: Lint, type-check, commit** (`feat: add a job runner for background workers`).

---

### Task 5: Catalog service: providers, sections, groups, items, EPG

**Files:**
- Create: `services/catalog.py`
- Modify: `workers.py`:
  - add `max_connections` to the XtreamLoaderWorker result;
  - add `ProviderSetupWorker`, `EpgLoadWorker` and `StbListWorker` at the end of the file.
- Test: `tests/test_catalog_service.py`

**Interfaces:**
- Consumes:
  - `JobRunner` / `ImmediateRunner` (Task 4);
  - `programmes_for_channel` and `Programme` (Task 2);
  - `provider_prefers_https`, `stb_endpoint` and `stb_portal_base` (services/provider_api.py);
  - ProviderManager: `providers`, `current_provider`, `current_provider_content`, `headers`, `set_current_provider(progress)`, `clear_current_provider_cache()`, `is_content_stale(ct)`, `mark_content_fresh(ct)`, `save_provider()`;
  - EpgManager: `set_current_epg()`, `force_refresh_current_epg()`, `get_programs_for_channel(channel, start, limit)`;
  - ConfigManager: `selected_provider_name`, `prefer_https`, `ssl_verify`, `channel_epg`, `save_config()`.
- Produces in `services/catalog.py`:
  - `SECTION_CONTENT_TYPES = {"live": "itv", "movies": "vod", "series": "series"}` and `ALL_GROUP_ID = "*"`;
  - `Group(section: str, id: str, title: str, count: int | None)`: a frozen dataclass;
  - `WorkerFactory`, whose methods are listed in Step 3;
  - `CatalogService(QObject)` with:
    - signals `providerReady(str)`, `providerFailed(str, str)`, `sectionReady(str)`, `sectionFailed(str, str)`, `groupReady(str, str)`, `groupFailed(str, str, str)`, `epgReady()`;
    - methods `provider_names()`, `current_provider_name()`, `provider_type()`, `select_provider(name=None, *, force_refresh=False)`, `sections()`, `request_section(section, *, force=False)`, `groups(section) -> list[Group]`, `items(section, group_id) -> list[dict] | None`, `request_group(section, group_id)`, `max_connections() -> int | None`, `epg_ready() -> bool`, `programmes(channel, now, limit=8) -> list[Programme]`, `is_live(section) -> bool | None`.

- [ ] **Step 1: Add the workers to `workers.py`**

1. In `XtreamLoaderWorker.run`, add this key to the final `result` dict:
```python
                "max_connections": _max_connections(user_info),
```
   and add this module-level helper above `class XtreamLoaderWorker`:
```python
def _max_connections(user_info) -> Optional[int]:
    """Xtream reports the account's concurrent stream limit as a string or int."""
    try:
        value = int(str(user_info.get("max_connections", "")).strip())
    except (AttributeError, ValueError):
        return None
    return value if value > 0 else None
```
2. Append to `workers.py`:
```python
class ProviderSetupWorker(QObject):
    """Select the configured provider (STB handshake, disk cache) without loading EPG."""

    finished = Signal(object)
    error = Signal(str)
    progress = Signal(str)

    def __init__(self, provider_manager, force_refresh: bool = False):
        super().__init__()
        self.provider_manager = provider_manager
        self.force_refresh = force_refresh

    def run(self):
        try:
            self.provider_manager.set_current_provider(self.progress)
            if self.force_refresh:
                self.provider_manager.clear_current_provider_cache()
            self.finished.emit({"name": self.provider_manager.current_provider.get("name", "")})
        except Exception as e:
            logger.warning("Provider setup failed: %s", e)
            self.error.emit(str(e))


class EpgLoadWorker(QObject):
    finished = Signal(object)
    error = Signal(str)

    def __init__(self, epg_manager, force_refresh: bool = False):
        super().__init__()
        self.epg_manager = epg_manager
        self.force_refresh = force_refresh

    def run(self):
        try:
            if self.force_refresh:
                self.epg_manager.force_refresh_current_epg()
            else:
                self.epg_manager.set_current_epg()
            self.finished.emit({})
        except Exception as e:
            logger.warning("EPG load failed: %s", e)
            self.error.emit(str(e))


class StbListWorker(QObject):
    """Run one STB ContentLoader listing synchronously inside this worker's thread."""

    finished = Signal(object)
    error = Signal(str)

    def __init__(self, **loader_kwargs):
        super().__init__()
        self.loader_kwargs = loader_kwargs

    def run(self):
        from content_loader import ContentLoader

        results: list = []
        errors: list = []
        loader = ContentLoader(**self.loader_kwargs)
        loader.content_loaded.connect(results.append, Qt.DirectConnection)
        loader.error.connect(errors.append, Qt.DirectConnection)
        try:
            loader.run()
        except Exception as e:
            self.error.emit(str(e))
            return
        if results:
            self.finished.emit(results[-1])
        else:
            self.error.emit(errors[-1] if errors else "The portal returned no data")
```
   Ensure `Optional` and `Qt` are imported at the top of `workers.py` (add them to the existing `typing` / `PySide6.QtCore` imports if missing).

- [ ] **Step 2: Write the failing tests**

Create `tests/test_catalog_service.py`:
```python
from datetime import datetime, timedelta, timezone
import os
from types import SimpleNamespace
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QObject, Signal
from PySide6.QtWidgets import QApplication

from services.catalog import ALL_GROUP_ID, CatalogService, Group
from services.jobs import ImmediateRunner


class FakeWorker(QObject):
    finished = Signal(object)
    error = Signal(str)

    def __init__(self, result=None, fail=None, on_run=None):
        super().__init__()
        self.result, self.fail, self.on_run = result, fail, on_run

    def run(self):
        if self.on_run:
            self.on_run()
        if self.fail:
            self.error.emit(self.fail)
        else:
            self.finished.emit(self.result)


class FakeFactory:
    """Records calls; returns FakeWorkers with canned results."""

    def __init__(self):
        self.calls = []
        self.results = {}

    def _worker(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        spec = self.results.get(name, {})
        return FakeWorker(spec.get("result"), spec.get("fail"), spec.get("on_run"))

    def __getattr__(self, name):
        return lambda *args, **kwargs: self._worker(name, *args, **kwargs)


class FakeProviders:
    def __init__(self, provider):
        self.providers = [provider]
        self.current_provider = provider
        self.current_provider_content = {}
        self.headers = {"Authorization": "Bearer T"}
        self.stale = set()
        self.saved = 0

    def is_content_stale(self, ct):
        return ct in self.stale

    def mark_content_fresh(self, ct):
        self.stale.discard(ct)

    def save_provider(self):
        self.saved += 1


def make_service(provider, factory=None):
    config = SimpleNamespace(
        selected_provider_name=provider["name"],
        prefer_https=False,
        ssl_verify=True,
        channel_epg=True,
        save_config=lambda: None,
    )
    providers = FakeProviders(provider)
    epg = SimpleNamespace(get_programs_for_channel=lambda channel, start, limit: [])
    factory = factory or FakeFactory()
    service = CatalogService(config, providers, epg, runner=ImmediateRunner(), factory=factory)
    return service, providers, factory, epg


def collect(signal):
    seen = []
    signal.connect(lambda *args: seen.append(args))
    return seen


XTREAM_PAYLOAD = {
    "categories": [
        {"id": "*", "title": "All"},
        {"id": "10", "title": "News"},
        {"id": "11", "title": "Empty"},
    ],
    "contents": [
        {"id": 1, "number": "1", "name": "One", "tv_genre_id": "10", "cmd": "http://x/1.ts"},
        {"id": 2, "number": "2", "name": "Two", "tv_genre_id": "10", "cmd": "http://x/2.ts"},
    ],
    "sorted_channels": {"10": [0, 1], "11": []},
    "max_connections": 2,
}


class CatalogServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def pump(self):
        QCoreApplication.processEvents()

    def test_sections_by_provider_type(self):
        service, *_ = make_service({"name": "X", "type": "XTREAM", "url": "h:1"})
        self.assertEqual(service.sections(), ["live", "movies", "series"])
        service, *_ = make_service({"name": "M", "type": "M3UPLAYLIST", "url": "/tmp/a.m3u"})
        self.assertEqual(service.sections(), ["live"])

    def test_provider_setup_then_epg(self):
        service, _, factory, _ = make_service({"name": "X", "type": "XTREAM", "url": "h:1"})
        factory.results["provider_setup"] = {"result": {"name": "X"}}
        factory.results["epg"] = {"result": {}}
        ready, epg = collect(service.providerReady), collect(service.epgReady)
        service.select_provider()
        self.assertEqual(ready, [("X",)])
        self.assertEqual(len(epg), 1)
        self.assertTrue(service.epg_ready())

    def test_provider_failure_is_reported(self):
        service, _, factory, _ = make_service({"name": "X", "type": "XTREAM", "url": "h:1"})
        factory.results["provider_setup"] = {"fail": "handshake refused"}
        failed = collect(service.providerFailed)
        service.select_provider()
        self.assertEqual(failed, [("X", "handshake refused")])

    def test_xtream_section_groups_and_items(self):
        provider = {"name": "X", "type": "XTREAM", "url": "h:1", "username": "u", "password": "p"}
        service, providers, factory, _ = make_service(provider)
        factory.results["xtream"] = {"result": XTREAM_PAYLOAD}
        ready = collect(service.sectionReady)
        service.request_section("live")
        self.assertEqual(ready, [("live",)])
        self.assertIs(providers.current_provider_content["itv"], XTREAM_PAYLOAD)
        self.assertEqual(providers.saved, 1)
        groups = service.groups("live")
        self.assertEqual(
            groups, [Group("live", "*", "All", 2), Group("live", "10", "News", 2)]
        )  # the empty group is hidden
        self.assertEqual([i["name"] for i in service.items("live", "10")], ["One", "Two"])
        self.assertEqual(len(service.items("live", ALL_GROUP_ID)), 2)
        self.assertEqual(service.max_connections(), 2)
        name, args, kwargs = factory.calls[-1]
        self.assertEqual(name, "xtream")
        self.assertEqual(args[:4], ("h:1", "u", "p", "itv"))

    def test_cached_fresh_section_does_not_reload(self):
        service, providers, factory, _ = make_service({"name": "X", "type": "XTREAM", "url": "h:1"})
        providers.current_provider_content["itv"] = XTREAM_PAYLOAD
        ready = collect(service.sectionReady)
        service.request_section("live")
        self.assertEqual(ready, [])  # deferred to the event loop
        self.pump()
        self.assertEqual(ready, [("live",)])
        self.assertEqual(factory.calls, [])

    def test_stale_section_reloads(self):
        service, providers, factory, _ = make_service({"name": "X", "type": "XTREAM", "url": "h:1"})
        providers.current_provider_content["itv"] = XTREAM_PAYLOAD
        providers.stale.add("itv")
        factory.results["xtream"] = {"result": XTREAM_PAYLOAD}
        service.request_section("live")
        self.assertEqual(factory.calls[-1][0], "xtream")

    def test_legacy_flat_m3u_list_is_one_group(self):
        service, providers, _, _ = make_service({"name": "M", "type": "M3UPLAYLIST", "url": "/a"})
        providers.current_provider_content["itv"] = [{"name": "A"}, {"name": "B"}]
        self.assertEqual(service.groups("live"), [Group("live", "*", "All", 2)])
        self.assertEqual(len(service.items("live", "*")), 2)

    def test_m3u_stream_is_a_single_channel(self):
        service, providers, _, _ = make_service({"name": "S", "type": "M3USTREAM", "url": "http://s"})
        service.request_section("live")
        self.pump()
        self.assertEqual(service.items("live", "*")[0]["cmd"], "http://s")

    def test_stb_on_demand_groups(self):
        provider = {"name": "S", "type": "STB", "url": "http://portal/c/"}
        service, providers, factory, _ = make_service(provider)
        providers.current_provider_content["vod"] = {
            "categories": [{"id": "*", "title": "All"}, {"id": "5", "title": "Films"}],
            "contents": {},
        }
        self.assertEqual(service.groups("movies"), [Group("movies", "5", "Films", None)])
        self.assertIsNone(service.items("movies", "5"))
        factory.results["stb_list"] = {"result": {"category_id": "5", "items": [{"id": 9, "name": "F"}]}}
        ready = collect(service.groupReady)
        service.request_group("movies", "5")
        self.assertEqual(ready, [("movies", "5")])
        self.assertEqual(service.items("movies", "5"), [{"id": 9, "name": "F"}])
        self.assertEqual(service.groups("movies"), [Group("movies", "5", "Films", 1)])
        kwargs = factory.calls[-1][2]
        self.assertEqual(kwargs["url"], "http://portal/server/load.php")
        self.assertEqual(kwargs["category_id"], "5")

    def test_results_after_provider_switch_are_dropped(self):
        provider = {"name": "X", "type": "XTREAM", "url": "h:1"}
        service, providers, factory, _ = make_service(provider)
        # The section job "finishes" only after a provider switch has happened.
        factory.results["xtream"] = {
            "result": XTREAM_PAYLOAD,
            "on_run": lambda: service.select_provider(),
        }
        factory.results["provider_setup"] = {"result": {"name": "X"}}
        factory.results["epg"] = {"result": {}}
        ready = collect(service.sectionReady)
        service.request_section("live")
        self.assertEqual(ready, [])
        self.assertNotIn("itv", providers.current_provider_content)

    def test_programmes_need_ready_epg(self):
        service, _, factory, epg = make_service({"name": "X", "type": "XTREAM", "url": "h:1"})
        epg.get_programs_for_channel = lambda c, s, l: [
            {"@start": "20261015200000 +0000", "@stop": "20261015210000 +0000", "title": {"__text": "On"}}
        ]
        now = datetime(2026, 10, 15, 20, 30, tzinfo=timezone.utc)
        self.assertEqual(service.programmes({"id": 1}, now), [])
        factory.results["provider_setup"] = {"result": {"name": "X"}}
        factory.results["epg"] = {"result": {}}
        service.select_provider()
        self.assertEqual([p.title for p in service.programmes({"id": 1}, now)], ["On"])

    def test_is_live(self):
        service, *_ = make_service({"name": "X", "type": "XTREAM", "url": "h:1"})
        self.assertTrue(service.is_live("live"))
        self.assertFalse(service.is_live("movies"))
        service, *_ = make_service({"name": "M", "type": "M3UPLAYLIST", "url": "/a"})
        self.assertIsNone(service.is_live("live"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run to verify they fail.** Expected: `ModuleNotFoundError: services.catalog`.

- [ ] **Step 4: Implement `services/catalog.py`**

```python
"""Provider catalog for the TV shell: providers, sections, groups and items (spec §4.1).

All network work runs through a JobRunner; every job is tagged with the provider
generation so results that arrive after a provider switch are dropped.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
from typing import Any, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from services.epg_view import Programme, programmes_for_channel
from services.jobs import JobRunner
from services.provider_api import provider_prefers_https, stb_endpoint, stb_portal_base

logger = logging.getLogger(__name__)

SECTION_CONTENT_TYPES = {"live": "itv", "movies": "vod", "series": "series"}
ALL_GROUP_ID = "*"


@dataclass(frozen=True)
class Group:
    section: str
    id: str
    title: str
    count: Optional[int] = None  # None: an STB group that has not been fetched yet


class WorkerFactory:
    """Creates the real workers; tests substitute a fake with the same method names."""

    def provider_setup(self, provider_manager, force_refresh):
        from workers import ProviderSetupWorker

        return ProviderSetupWorker(provider_manager, force_refresh)

    def epg(self, epg_manager, force_refresh):
        from workers import EpgLoadWorker

        return EpgLoadWorker(epg_manager, force_refresh)

    def m3u(self, url, verify_ssl, prefer_https):
        from workers import M3ULoaderWorker

        return M3ULoaderWorker(url, verify_ssl=verify_ssl, prefer_https=prefer_https)

    def xtream(self, base_url, username, password, content_type, verify_ssl, prefer_https):
        from workers import XtreamLoaderWorker

        return XtreamLoaderWorker(
            base_url, username, password, content_type, verify_ssl=verify_ssl, prefer_https=prefer_https
        )

    def stb_categories(self, url, headers, content_type, verify_ssl, prefer_https):
        from workers import STBCategoriesWorker

        return STBCategoriesWorker(
            url, headers, content_type, verify_ssl=verify_ssl, prefer_https=prefer_https
        )

    def stb_list(self, **loader_kwargs):
        from workers import StbListWorker

        return StbListWorker(**loader_kwargs)

    def xtream_series(self, **kwargs):
        from workers import XtreamSeriesInfoWorker

        return XtreamSeriesInfoWorker(**kwargs)

    def link(self, **kwargs):
        from workers import LinkCreatorWorker

        return LinkCreatorWorker(**kwargs)


class CatalogService(QObject):
    providerReady = Signal(str)
    providerFailed = Signal(str, str)
    sectionReady = Signal(str)
    sectionFailed = Signal(str, str)
    groupReady = Signal(str, str)
    groupFailed = Signal(str, str, str)
    epgReady = Signal()

    def __init__(
        self,
        config_manager,
        provider_manager,
        epg_manager,
        runner=None,
        factory: Optional[WorkerFactory] = None,
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        self.config = config_manager
        self.provider_manager = provider_manager
        self.epg_manager = epg_manager
        self.runner = runner if runner is not None else JobRunner(self)
        self.factory = factory or WorkerFactory()
        self._generation = 0
        self._epg_ready = False
        self._jobs: dict[QObject, tuple[str, int, Any]] = {}

    # --- providers -------------------------------------------------------------------
    def provider_names(self) -> list[str]:
        return [p.get("name", "") for p in self.provider_manager.providers]

    def current_provider(self) -> dict:
        return self.provider_manager.current_provider or {}

    def current_provider_name(self) -> str:
        return self.current_provider().get("name", "")

    def provider_type(self) -> str:
        return str(self.current_provider().get("type", "")).upper()

    def select_provider(self, name: Optional[str] = None, *, force_refresh: bool = False) -> None:
        if name is not None and name != self.config.selected_provider_name:
            self.config.selected_provider_name = name
            self.config.save_config()
        self._generation += 1
        self._epg_ready = False
        worker = self.factory.provider_setup(self.provider_manager, force_refresh)
        self._start(worker, "provider", force_refresh, self._on_provider_ready, self._on_provider_error)

    def _on_provider_ready(self, payload) -> None:
        job = self._take_job()
        if job is None:
            return
        force_refresh = job[2]
        self.providerReady.emit(self.current_provider_name())
        worker = self.factory.epg(self.epg_manager, force_refresh)
        self._start(worker, "epg", None, self._on_epg_ready, self._on_epg_error)

    def _on_provider_error(self, message: str) -> None:
        if self._take_job() is not None:
            self.providerFailed.emit(self.current_provider_name(), message)

    def _on_epg_ready(self, payload) -> None:
        if self._take_job() is not None:
            self._epg_ready = True
            self.epgReady.emit()

    def _on_epg_error(self, message: str) -> None:
        if self._take_job() is not None:
            logger.info("EPG unavailable: %s", message)

    def epg_ready(self) -> bool:
        return self._epg_ready

    def programmes(self, channel: dict, now: datetime, limit: int = 8) -> list[Programme]:
        if not self._epg_ready or not self.config.channel_epg:
            return []
        return programmes_for_channel(self.epg_manager, channel, now, limit)

    def max_connections(self) -> Optional[int]:
        for ct in SECTION_CONTENT_TYPES.values():
            content = self.provider_manager.current_provider_content.get(ct)
            if isinstance(content, dict) and content.get("max_connections"):
                return int(content["max_connections"])
        return None

    # --- sections --------------------------------------------------------------------
    def sections(self) -> list[str]:
        if self.provider_type() in ("STB", "XTREAM"):
            return ["live", "movies", "series"]
        return ["live"]

    def is_live(self, section: str) -> Optional[bool]:
        if self.provider_type() in ("STB", "XTREAM"):
            return section == "live"
        return None  # M3U entries may be live or on demand

    def request_section(self, section: str, *, force: bool = False) -> None:
        ct = SECTION_CONTENT_TYPES[section]
        content = self.provider_manager.current_provider_content.get(ct)
        if not force and self._usable(content) and not self.provider_manager.is_content_stale(ct):
            QTimer.singleShot(0, lambda: self.sectionReady.emit(section))
            return
        provider, ptype = self.current_provider(), self.provider_type()
        verify = provider.get("ssl_verify", self.config.ssl_verify)
        https = provider_prefers_https(provider, self.config.prefer_https)
        if ptype == "M3USTREAM":
            item = {"id": 1, "name": provider.get("name") or "Stream", "cmd": provider.get("url", "")}
            self._store_section(
                ct,
                {
                    "categories": [{"id": ALL_GROUP_ID, "title": "All"}],
                    "contents": [item],
                    "sorted_channels": {ALL_GROUP_ID: [0]},
                },
            )
            QTimer.singleShot(0, lambda: self.sectionReady.emit(section))
            return
        if ptype == "M3UPLAYLIST":
            worker = self.factory.m3u(provider.get("url", ""), verify, https)
        elif ptype == "XTREAM":
            worker = self.factory.xtream(
                provider.get("url", ""),
                provider.get("username", ""),
                provider.get("password", ""),
                ct,
                verify,
                https,
            )
        elif ptype == "STB":
            worker = self.factory.stb_categories(
                provider.get("url", ""), self.provider_manager.headers, ct, verify, https
            )
        else:
            QTimer.singleShot(0, lambda: self.sectionFailed.emit(section, f"Unsupported provider: {ptype}"))
            return
        self._start(worker, "section", section, self._on_section_loaded, self._on_section_error)

    def _on_section_loaded(self, payload) -> None:
        job = self._take_job()
        if job is None:
            return
        section = job[2]
        self._store_section(SECTION_CONTENT_TYPES[section], payload)
        self.sectionReady.emit(section)

    def _on_section_error(self, message: str) -> None:
        job = self._take_job()
        if job is not None:
            self.sectionFailed.emit(job[2], message)

    def _store_section(self, ct: str, payload) -> None:
        self.provider_manager.current_provider_content[ct] = payload
        self.provider_manager.mark_content_fresh(ct)
        self.provider_manager.save_provider()

    @staticmethod
    def _usable(content) -> bool:
        if isinstance(content, list):
            return bool(content)
        return isinstance(content, dict) and "categories" in content

    # --- groups and items --------------------------------------------------------------
    def _content(self, section: str):
        return self.provider_manager.current_provider_content.get(SECTION_CONTENT_TYPES[section])

    def groups(self, section: str) -> list[Group]:
        content = self._content(section)
        if isinstance(content, list):
            return [Group(section, ALL_GROUP_ID, "All", len(content))] if content else []
        if not isinstance(content, dict):
            return []
        contents = content.get("contents")
        sorted_map = content.get("sorted_channels")
        groups = []
        for category in content.get("categories", []) or []:
            group_id = str(category.get("id", ""))
            title = str(category.get("title") or category.get("name") or group_id)
            count: Optional[int]
            if isinstance(sorted_map, dict):
                if group_id == ALL_GROUP_ID:
                    count = len(contents) if isinstance(contents, list) else 0
                else:
                    count = len(sorted_map.get(group_id, []))
            elif isinstance(contents, dict):
                if group_id == ALL_GROUP_ID:
                    continue  # fetching every on-demand title at once is too heavy for a TV list
                loaded = contents.get(group_id)
                count = len(loaded) if isinstance(loaded, list) else None
            else:
                count = None
            if count == 0:
                continue
            groups.append(Group(section, group_id, title, count))
        return groups

    def items(self, section: str, group_id: str) -> Optional[list[dict]]:
        content = self._content(section)
        if isinstance(content, list):
            return list(content)
        if not isinstance(content, dict):
            return None
        contents = content.get("contents")
        sorted_map = content.get("sorted_channels")
        if isinstance(sorted_map, dict) and isinstance(contents, list):
            if group_id == ALL_GROUP_ID:
                return list(contents)
            return [contents[i] for i in sorted_map.get(group_id, []) if 0 <= i < len(contents)]
        if isinstance(contents, dict):
            loaded = contents.get(group_id)
            return list(loaded) if isinstance(loaded, list) else None
        return None

    def request_group(self, section: str, group_id: str) -> None:
        if self.items(section, group_id) is not None:
            QTimer.singleShot(0, lambda: self.groupReady.emit(section, group_id))
            return
        provider = self.current_provider()
        worker = self.factory.stb_list(
            url=stb_endpoint(
                stb_portal_base(
                    provider.get("url", ""), provider_prefers_https(provider, self.config.prefer_https)
                )
            ),
            headers=self.provider_manager.headers,
            content_type=SECTION_CONTENT_TYPES[section],
            category_id=group_id,
            verify_ssl=provider.get("ssl_verify", self.config.ssl_verify),
        )
        self._start(worker, "group", (section, group_id), self._on_group_loaded, self._on_group_error)

    def _on_group_loaded(self, payload) -> None:
        job = self._take_job()
        if job is None:
            return
        section, group_id = job[2]
        content = self.provider_manager.current_provider_content.setdefault(
            SECTION_CONTENT_TYPES[section], {}
        )
        content.setdefault("contents", {})[group_id] = list(payload.get("items") or [])
        self.provider_manager.save_provider()
        self.groupReady.emit(section, group_id)

    def _on_group_error(self, message: str) -> None:
        job = self._take_job()
        if job is not None:
            section, group_id = job[2]
            self.groupFailed.emit(section, group_id, message)

    # --- job bookkeeping -------------------------------------------------------------
    def _start(self, worker, kind: str, data, on_finished, on_error) -> None:
        self._jobs[worker] = (kind, self._generation, data)
        self.runner.run_worker(worker, on_finished, on_error)

    def _take_job(self):
        """Pop the sending worker's job; None when it is unknown or from an older provider."""
        job = self._jobs.pop(self.sender(), None)
        if job is None or job[1] != self._generation:
            return None
        return job
```

- [ ] **Step 5: Verify, then commit**

```bash
uv run python -m unittest tests.test_catalog_service -v && uv run python -m unittest discover -s tests 2>&1 | tail -3
uv run black services/catalog.py workers.py tests/test_catalog_service.py && uv run isort services/catalog.py workers.py tests/test_catalog_service.py
uv run flake8 --select=E9,F63,F7,F82 services/catalog.py workers.py tests/test_catalog_service.py && uv run mypy services/catalog.py
git add services/catalog.py workers.py tests/test_catalog_service.py
git commit -F - <<'EOF'
feat: add a catalog service for providers, sections and groups

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_011UFrnH8jUNCzkFNLKJaoRD
EOF
```
Expected: 12 PASS and a green suite. `sender()` works under `ImmediateRunner` because the signals are connected directly. If `self.sender()` returns `None` in that mode on this PySide build, pass the worker through the job instead: connect with `functools.partial(on_finished_with_worker, worker)` **only in ImmediateRunner** is not allowed. Report BLOCKED instead and describe the observation.

---

### Task 6: Catalog service: series, episodes and stream links

**Files:**
- Modify: `services/catalog.py`
- Test: `tests/test_catalog_series.py`

**Interfaces:**
- Consumes: CatalogService internals (`_start`, `_take_job`, `_content`, the factory methods `xtream_series`, `stb_list` and `link`).
- Produces on `CatalogService`:
  - signals `seasonsReady(str series_id)`, `seasonsFailed(str, str)`, `episodesReady(str series_id, str season_id)`, `episodesFailed(str, str, str)`, `linkReady(int, str)` and `linkFailed(int, str)`;
  - methods:
    - `seasons(series) -> list[dict] | None`, `request_seasons(series)`;
    - `episodes(series, season) -> list[dict] | None`, `request_episodes(series, season)`;
    - `request_link(item, *, section, is_episode=False) -> int` (the request id).
- Module functions `normalize_seasons(series_name: str, items: list[dict]) -> list[dict]` and `format_xtream_episodes(season, stream_base, stream_ext, username, password) -> list[dict]`. Both are ported verbatim from `mixins/content_loading_mixin.py`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_catalog_series.py`:
```python
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from services.catalog import format_xtream_episodes, normalize_seasons
from tests.test_catalog_service import FakeFactory, collect, make_service


class SeriesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_normalize_seasons(self):
        items = normalize_seasons(
            "Show",
            [{"id": "2", "name": "Season 2"}, {"id": "x", "name": "Book IV"}, {"id": "y", "name": "Extras"}],
        )
        self.assertEqual([i["number"] for i in items], ["2", "4", "3"])
        self.assertEqual(items[0]["name"], "Show.Season 2")
        self.assertEqual(items[0]["o_name"], "Season 2")

    def test_format_xtream_episodes(self):
        season = {"episodes": [{"id": "77", "episode_num": "3", "title": "Pilot", "container_extension": "mkv", "info": {"movie_image": "i.jpg"}}]}
        [ep] = format_xtream_episodes(season, "http://b", "ts", "u", "p")
        self.assertEqual(ep["cmd"], "http://b/series/u/p/77.mkv")
        self.assertEqual((ep["number"], ep["name"], ep["logo"]), ("3", "Pilot", "i.jpg"))

    def test_xtream_seasons_and_episodes(self):
        provider = {"name": "X", "type": "XTREAM", "url": "h:1", "username": "u", "password": "p"}
        service, providers, factory, _ = make_service(provider)
        providers.current_provider_content["series"] = {"stream_base": "http://b", "stream_ext": "ts"}
        factory.results["xtream_series"] = {
            "result": {"resolved_base": "http://b", "seasons": [{"id": "1", "name": "Season 1", "episodes": [{"id": "5", "episode_num": 1}]}]}
        }
        ready = collect(service.seasonsReady)
        series = {"id": 42, "name": "Show"}
        self.assertIsNone(service.seasons(series))
        service.request_seasons(series)
        self.assertEqual(ready, [("42",)])
        [season] = service.seasons(series)
        self.assertEqual(season["name"], "Show.Season 1")
        [episode] = service.episodes(series, season)
        self.assertEqual(episode["cmd"], "http://b/series/u/p/5.ts")

    def test_stb_seasons_and_episodes(self):
        provider = {"name": "S", "type": "STB", "url": "http://portal/c/"}
        service, providers, factory, _ = make_service(provider)
        series = {"id": 9, "name": "Show", "category_id": "3"}
        factory.results["stb_list"] = {"result": {"items": [{"id": "9:1", "name": "Season 1", "cmd": "C1", "series": [1, 2]}]}}
        service.request_seasons(series)
        [season] = service.seasons(series)
        kwargs = factory.calls[-1][2]
        self.assertEqual((kwargs["movie_id"], kwargs["season_id"], kwargs["category_id"]), (9, 0, "3"))
        self.assertIsNone(service.episodes(series, season))
        factory.results["stb_list"] = {
            "result": {"season_id": "9:1", "items": [{"id": "9:1", "cmd": "C1", "series": [1, 2]}]}
        }
        ready = collect(service.episodesReady)
        service.request_episodes(series, season)
        self.assertEqual(ready, [("9", "9:1")])
        episodes = service.episodes(series, season)
        self.assertEqual([e["series"] for e in episodes], [1, 2])
        self.assertEqual(episodes[0]["cmd"], "C1")
        self.assertEqual(episodes[0]["name"], "Show")

    def test_links(self):
        service, _, factory, _ = make_service({"name": "X", "type": "XTREAM", "url": "h:1"})
        ready, failed = collect(service.linkReady), collect(service.linkFailed)
        rid = service.request_link({"cmd": " http://x/1.ts "}, section="live")
        empty = service.request_link({"cmd": ""}, section="live")
        QCoreApplication.processEvents()
        self.assertEqual(ready, [(rid, "http://x/1.ts")])
        self.assertEqual(failed, [(empty, "This item has no stream address")])

    def test_stb_link_uses_worker_and_drops_stale(self):
        provider = {"name": "S", "type": "STB", "url": "http://portal/c/"}
        service, _, factory, _ = make_service(provider)
        factory.results["link"] = {"result": {"link": " http://stream/1 "}}
        ready = collect(service.linkReady)
        rid = service.request_link({"cmd": "ffmpeg http://localhost/ch/1_"}, section="live")
        self.assertEqual(ready, [(rid, "http://stream/1")])
        kwargs = factory.calls[-1][2]
        self.assertEqual(kwargs["content_type"], "itv")
        factory.results["link"] = {"result": {"link": "http://stream/2"}, "on_run": lambda: service.select_provider()}
        factory.results["provider_setup"] = {"result": {"name": "S"}}
        factory.results["epg"] = {"result": {}}
        service.request_link({"cmd": "x"}, section="live")
        self.assertEqual(len(ready), 1)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify they fail.** Expected: ImportError for `format_xtream_episodes`.

- [ ] **Step 3: Implement**

1. Port the module functions:
   - `normalize_seasons(series_name, items)`: move the per-item loop body of `ContentLoadingMixin.update_seasons_list` (`mixins/content_loading_mixin.py`, from `for i, item in enumerate(items):` to the end of its `except` block) **verbatim**, replacing `self.current_series.get("name", ...)` with `series_name`. It returns `items` after mutating them in place, exactly as before. Import `find_roman_token` from `workers` and `re`.
   - `format_xtream_episodes(season, stream_base, stream_ext, username, password)`: move the body of the `for idx, ep in enumerate(episodes, start=1):` loop in `ContentLoadingMixin.load_season_episodes` (Xtream branch) **verbatim**. Start it with `episodes = season.get("episodes", []) or []` and return `formatted_episodes`.
   - Do not delete the mixin code (the old UI still uses it until Phase 3). Duplication between the old mixin and the new service is accepted until Phase 3 deletes the mixin; say this in the commit body.
2. Add the signals listed in Interfaces to `CatalogService`, plus `import itertools`, and in `__init__` add `self._link_ids = itertools.count(1)`.
3. Add these methods to `CatalogService`:
```python
    # --- series -----------------------------------------------------------------------
    def _series_cache(self) -> dict:
        content = self.provider_manager.current_provider_content.setdefault("series", {})
        return content if isinstance(content, dict) else {}

    def seasons(self, series: dict) -> Optional[list[dict]]:
        cached = self._series_cache().get("seasons", {}).get(str(series.get("id")))
        return list(cached) if isinstance(cached, list) else None

    def request_seasons(self, series: dict) -> None:
        if self.seasons(series) is not None:
            series_id = str(series.get("id"))
            QTimer.singleShot(0, lambda: self.seasonsReady.emit(series_id))
            return
        provider = self.current_provider()
        verify = provider.get("ssl_verify", self.config.ssl_verify)
        https = provider_prefers_https(provider, self.config.prefer_https)
        if self.provider_type() == "XTREAM":
            worker = self.factory.xtream_series(
                base_url=provider.get("url", ""),
                username=provider.get("username", ""),
                password=provider.get("password", ""),
                series_id=str(series.get("id")),
                resolved_base=self._series_cache().get("resolved_base", ""),
                verify_ssl=verify,
                prefer_https=https,
            )
        else:
            worker = self.factory.stb_list(
                url=stb_endpoint(stb_portal_base(provider.get("url", ""), https)),
                headers=self.provider_manager.headers,
                content_type="series",
                category_id=series.get("category_id") or series.get("tv_genre_id", ""),
                movie_id=series.get("id"),
                season_id=0,
                action="get_ordered_list",
                sortby="name",
                verify_ssl=verify,
            )
        self._start(worker, "seasons", series, self._on_seasons_loaded, self._on_seasons_error)

    def _on_seasons_loaded(self, payload) -> None:
        job = self._take_job()
        if job is None:
            return
        series = job[2]
        cache = self._series_cache()
        if payload.get("resolved_base"):
            cache["resolved_base"] = payload["resolved_base"]
        raw = payload.get("seasons") if "seasons" in payload else payload.get("items")
        items = normalize_seasons(series.get("name", "Unknown Series"), list(raw or []))
        cache.setdefault("seasons", {})[str(series.get("id"))] = items
        self.provider_manager.save_provider()
        self.seasonsReady.emit(str(series.get("id")))

    def _on_seasons_error(self, message: str) -> None:
        job = self._take_job()
        if job is not None:
            self.seasonsFailed.emit(str(job[2].get("id")), message)

    def episodes(self, series: dict, season: dict) -> Optional[list[dict]]:
        if self.provider_type() == "XTREAM":
            provider, cache = self.current_provider(), self._series_cache()
            return format_xtream_episodes(
                season,
                cache.get("stream_base", "") or cache.get("resolved_base", ""),
                cache.get("stream_ext", "ts"),
                provider.get("username", ""),
                provider.get("password", ""),
            )
        cached = (
            self._series_cache().get("episodes", {}).get(str(series.get("id")), {}).get(str(season.get("id")))
        )
        return list(cached) if isinstance(cached, list) else None

    def request_episodes(self, series: dict, season: dict) -> None:
        if self.episodes(series, season) is not None:
            ids = (str(series.get("id")), str(season.get("id")))
            QTimer.singleShot(0, lambda: self.episodesReady.emit(*ids))
            return
        provider = self.current_provider()
        worker = self.factory.stb_list(
            url=stb_endpoint(
                stb_portal_base(
                    provider.get("url", ""), provider_prefers_https(provider, self.config.prefer_https)
                )
            ),
            headers=self.provider_manager.headers,
            content_type="series",
            category_id=series.get("category_id") or series.get("tv_genre_id", ""),
            movie_id=series.get("id"),
            season_id=season.get("id"),
            action="get_ordered_list",
            sortby="added",
            verify_ssl=provider.get("ssl_verify", self.config.ssl_verify),
        )
        self._start(worker, "episodes", (series, season), self._on_episodes_loaded, self._on_episodes_error)

    def _on_episodes_loaded(self, payload) -> None:
        job = self._take_job()
        if job is None:
            return
        series, season = job[2]
        season_id = str(season.get("id"))
        selected = next(
            (item for item in payload.get("items") or [] if str(item.get("id")) == season_id), None
        )
        episodes = []
        for number in (selected or {}).get("series", []) or []:
            episode = dict(series)
            episode.update(
                {"number": f"{number}", "ename": f"Episode {number}", "cmd": selected.get("cmd"), "series": number}
            )
            episodes.append(episode)
        self._series_cache().setdefault("episodes", {}).setdefault(str(series.get("id")), {})[season_id] = episodes
        self.provider_manager.save_provider()
        self.episodesReady.emit(str(series.get("id")), season_id)

    def _on_episodes_error(self, message: str) -> None:
        job = self._take_job()
        if job is not None:
            series, season = job[2]
            self.episodesFailed.emit(str(series.get("id")), str(season.get("id")), message)

    # --- links ------------------------------------------------------------------------
    def request_link(self, item: dict, *, section: str, is_episode: bool = False) -> int:
        """Resolve a playable URL. STB portals mint a fresh link; others play `cmd` directly."""
        request_id = next(self._link_ids)
        if self.provider_type() != "STB":
            url = str(item.get("cmd") or "").strip()
            if url:
                QTimer.singleShot(0, lambda: self.linkReady.emit(request_id, url))
            else:
                QTimer.singleShot(
                    0, lambda: self.linkFailed.emit(request_id, "This item has no stream address")
                )
            return request_id
        provider = self.current_provider()
        worker = self.factory.link(
            base_url=provider.get("url", ""),
            headers=self.provider_manager.headers,
            content_type=SECTION_CONTENT_TYPES[section],
            cmd=item.get("cmd", ""),
            is_episode=is_episode,
            series_param=item.get("series") if is_episode else None,
            verify_ssl=provider.get("ssl_verify", self.config.ssl_verify),
            prefer_https=provider_prefers_https(provider, self.config.prefer_https),
        )
        self._start(worker, "link", request_id, self._on_link_ready, self._on_link_error)
        return request_id

    def _on_link_ready(self, payload) -> None:
        job = self._take_job()
        if job is None:
            return
        url = str(payload.get("link") or "").strip()
        if url:
            self.linkReady.emit(job[2], url)
        else:
            self.linkFailed.emit(job[2], "The portal returned an empty link")

    def _on_link_error(self, message: str) -> None:
        job = self._take_job()
        if job is not None:
            self.linkFailed.emit(job[2], message)
```

- [ ] **Step 4: Verify** (`tests.test_catalog_series`, then the full suite), **lint and type-check**, then **commit** with `feat: add series, episodes and stream links to the catalog service`.

---

### Task 7: Navigation model (`services/navigation.py`)

**Files:**
- Create: `services/navigation.py`
- Test: `tests/test_navigation.py`

**Interfaces:**
- Consumes these CatalogService members (Tasks 5–6):
  - `provider_names()`, `current_provider_name()`, `select_provider(name)`, `sections()`;
  - `request_section`, `groups`, `items`, `request_group`;
  - `seasons`, `request_seasons`, `episodes`, `request_episodes`;
  - the signals `providerReady`, `sectionReady`, `groupReady`, `seasonsReady`, `episodesReady`.
- Consumes ConfigManager `favorites: list[str]` and `watch_history: list[dict]`.
- Produces:
  - `NavNode(layer: str, key: str, title: str, payload: Any = None, count: int | None = None)`: frozen dataclass. `layer` is one of `"source"`, `"section"`, `"group"`, `"item"`, `"season"`, `"episode"`.
  - Constants `SECTION_TITLES = {"live": "Live TV", "movies": "Movies", "series": "Series"}`, `FAVORITES_GROUP = "@favorites"`, `RECENT_GROUP = "@recent"`.
  - `NavModel(QObject)`:
    - signal `changed()`;
    - `path: list[NavNode]` (read-only copy via the `path` property);
    - `children(parent: NavNode | None) -> list[NavNode] | None` (`None` means loading has started);
    - `open(node)`, which descends into `node` (or, for a source, selects that provider);
    - `up() -> bool`;
    - `columns() -> tuple[tuple[NavNode | None, list | None], tuple[NavNode | None, list | None]]`, the two neighbouring layers the drawer shows as `(parent, children)` pairs;
    - `is_playable(node) -> bool`;
    - `section_of(node) -> str | None`;
    - `reset_to(section="live", group_id=None)`.

Behaviour:
- **Root children** are one `source` node per provider.
- **Opening a source** sets `path = [source]`. If it is not the current provider, it calls `catalog.select_provider(name)`.
- **Section nodes:** `children(source)` returns one per `catalog.sections()`.
- **Groups of the live section:** pinned groups come first, but only when they are non-empty:
  - `@favorites`: channels of the current live content whose `name` is in `config.favorites`;
  - `@recent`: up to 20 history entries for the current provider with `item_type == "channel"`, deduplicated, using `item_data`.
  - Then come `catalog.groups(section)`.
- **Children of a group** are `item` nodes from `catalog.items`. If those are `None`, the model calls `request_group` and returns `None`.
- **Item nodes:**
  - live or movies items are playable leaves;
  - series items have seasons, then episodes.
- **Season/episode children:** returning `None` triggers `request_seasons` / `request_episodes`.
- Every catalog ready signal emits `changed`. A `providerReady` for a different provider resets the path to `[source]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_navigation.py`:
```python
import os
from types import SimpleNamespace
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from services.catalog import Group
from services.navigation import FAVORITES_GROUP, RECENT_GROUP, NavModel, NavNode


class FakeCatalog(QObject):
    providerReady = Signal(str)
    sectionReady = Signal(str)
    groupReady = Signal(str, str)
    seasonsReady = Signal(str)
    episodesReady = Signal(str, str)

    def __init__(self):
        super().__init__()
        self.provider = "Home"
        self.selected = []
        self.requests = []
        self.live = {
            "10": [{"id": 1, "name": "One", "number": "1"}, {"id": 2, "name": "Two", "number": "2"}]
        }
        self.movies = {}
        self.season_map = {}

    def provider_names(self):
        return ["Home", "Backup"]

    def current_provider_name(self):
        return self.provider

    def select_provider(self, name=None, **kwargs):
        self.selected.append(name)

    def sections(self):
        return ["live", "movies", "series"]

    def request_section(self, section, **kwargs):
        self.requests.append(("section", section))

    def groups(self, section):
        if section == "live":
            return [Group("live", "10", "News", 2)]
        return [Group(section, "5", "Films", None)]

    def items(self, section, group_id):
        if section == "live":
            return self.live.get(group_id) if group_id != "*" else self.live["10"]
        return self.movies.get(group_id)

    def request_group(self, section, group_id):
        self.requests.append(("group", section, group_id))

    def seasons(self, series):
        return self.season_map.get(series["id"])

    def request_seasons(self, series):
        self.requests.append(("seasons", series["id"]))

    def episodes(self, series, season):
        return None

    def request_episodes(self, series, season):
        self.requests.append(("episodes", series["id"], season["id"]))


class NavModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.catalog = FakeCatalog()
        self.config = SimpleNamespace(
            favorites=["Two"],
            watch_history=[
                {"provider_name": "Home", "item_type": "channel", "item_data": {"id": 1, "name": "One"}},
                {"provider_name": "Other", "item_type": "channel", "item_data": {"id": 3, "name": "X"}},
                {"provider_name": "Home", "item_type": "movie", "item_data": {"id": 4, "name": "M"}},
            ],
        )
        self.nav = NavModel(self.catalog, self.config)

    def test_root_is_sources(self):
        sources = self.nav.children(None)
        self.assertEqual([(n.layer, n.title) for n in sources], [("source", "Home"), ("source", "Backup")])

    def test_open_other_source_selects_provider(self):
        backup = self.nav.children(None)[1]
        self.nav.open(backup)
        self.assertEqual(self.catalog.selected, ["Backup"])
        self.assertEqual(self.nav.path, [backup])

    def test_sections_and_pinned_live_groups(self):
        home = self.nav.children(None)[0]
        self.nav.open(home)
        sections = self.nav.children(home)
        self.assertEqual([n.title for n in sections], ["Live TV", "Movies", "Series"])
        self.nav.open(sections[0])
        self.assertIn(("section", "live"), self.catalog.requests)
        groups = self.nav.children(sections[0])
        self.assertEqual([n.key for n in groups], [FAVORITES_GROUP, RECENT_GROUP, "10"])
        favorites = self.nav.children(groups[0])
        self.assertEqual([n.title for n in favorites], ["Two"])
        recent = self.nav.children(groups[1])
        self.assertEqual([n.title for n in recent], ["One"])

    def test_columns_show_two_neighbouring_layers(self):
        self.nav.reset_to("live", "10")
        (left_parent, left), (right_parent, right) = self.nav.columns()
        self.assertEqual(left_parent.key, "live")
        self.assertEqual(right_parent.key, "10")
        self.assertEqual([n.title for n in right], ["One", "Two"])
        self.assertTrue(self.nav.up())
        (left_parent, _), (right_parent, _) = self.nav.columns()
        self.assertEqual((left_parent.layer, right_parent.key), ("source", "live"))

    def test_on_demand_group_requests_and_reports_loading(self):
        self.nav.reset_to("movies")
        films = [n for n in self.nav.children(self.nav.path[-1]) if n.key == "5"][0]
        self.assertIsNone(self.nav.children(films))
        self.assertIn(("group", "movies", "5"), self.catalog.requests)
        changes = []
        self.nav.changed.connect(lambda: changes.append(1))
        self.catalog.movies["5"] = [{"id": 9, "name": "Film"}]
        self.catalog.groupReady.emit("movies", "5")
        self.assertEqual(changes, [1])
        self.assertEqual([n.title for n in self.nav.children(films)], ["Film"])

    def test_playable_and_series_layers(self):
        self.nav.reset_to("live", "10")
        channel = self.nav.children(self.nav.path[-1])[0]
        self.assertTrue(self.nav.is_playable(channel))
        series = NavNode("item", "s1", "Show", {"id": "s1", "name": "Show"})
        self.nav.reset_to("series", "5")
        self.assertFalse(self.nav.is_playable(series))
        self.assertIsNone(self.nav.children(series))
        self.assertIn(("seasons", "s1"), self.catalog.requests)

    def test_provider_change_resets_path(self):
        self.nav.reset_to("live", "10")
        self.catalog.provider = "Backup"
        self.catalog.providerReady.emit("Backup")
        self.assertEqual([n.layer for n in self.nav.path], ["source"])
        self.assertEqual(self.nav.path[0].key, "Backup")

    def test_up_at_root_returns_false(self):
        home = self.nav.children(None)[0]
        self.nav.open(home)
        self.nav.open(self.nav.children(home)[0])
        self.assertTrue(self.nav.up())
        self.assertFalse(self.nav.up())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify they fail.** Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement `services/navigation.py`**

```python
"""Layered navigation for the drawer: Source → Section → Group → Item → Season → Episode."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Optional

from PySide6.QtCore import QObject, Signal

logger = logging.getLogger(__name__)

SECTION_TITLES = {"live": "Live TV", "movies": "Movies", "series": "Series"}
FAVORITES_GROUP = "@favorites"
RECENT_GROUP = "@recent"
_RECENT_LIMIT = 20


@dataclass(frozen=True)
class NavNode:
    layer: str
    key: str
    title: str
    payload: Any = None
    count: Optional[int] = None

    def __hash__(self):  # payload dicts are unhashable; identity is layer + key
        return hash((self.layer, self.key))

    def __eq__(self, other):
        return isinstance(other, NavNode) and (self.layer, self.key) == (other.layer, other.key)


def _item_key(item: dict) -> str:
    return str(item.get("id", item.get("cmd", item.get("name", ""))))


def _item_node(item: dict, layer: str = "item") -> NavNode:
    return NavNode(layer, _item_key(item), str(item.get("name") or item.get("title") or "Untitled"), item)


class NavModel(QObject):
    changed = Signal()

    def __init__(self, catalog, config, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.catalog = catalog
        self.config = config
        self._path: list[NavNode] = []
        for signal in (
            catalog.sectionReady,
            catalog.groupReady,
            catalog.seasonsReady,
            catalog.episodesReady,
        ):
            signal.connect(self._emit_changed)
        catalog.providerReady.connect(self._on_provider_ready)

    @property
    def path(self) -> list[NavNode]:
        return list(self._path)

    # --- structure ---------------------------------------------------------------------
    def _source(self) -> NavNode:
        name = self.catalog.current_provider_name()
        return NavNode("source", name, name, name)

    def section_of(self, node: Optional[NavNode] = None) -> Optional[str]:
        chain = self._path if node is None else self._path + [node]
        for entry in chain:
            if entry.layer == "section":
                return entry.key
        return None

    def children(self, parent: Optional[NavNode]) -> Optional[list[NavNode]]:
        if parent is None:
            return [NavNode("source", name, name, name) for name in self.catalog.provider_names()]
        if parent.layer == "source":
            return [
                NavNode("section", s, SECTION_TITLES.get(s, s.title()), s) for s in self.catalog.sections()
            ]
        if parent.layer == "section":
            pinned = self._pinned_groups() if parent.key == "live" else []
            groups = [NavNode("group", g.id, g.title, g, g.count) for g in self.catalog.groups(parent.key)]
            return pinned + groups
        if parent.layer == "group":
            return self._group_items(parent)
        if parent.layer == "item" and self.section_of(parent) == "series":
            seasons = self.catalog.seasons(parent.payload)
            if seasons is None:
                self.catalog.request_seasons(parent.payload)
                return None
            return [_item_node(s, "season") for s in seasons]
        if parent.layer == "season":
            series = next((n for n in reversed(self._path) if n.layer == "item"), None)
            if series is None:
                return []
            episodes = self.catalog.episodes(series.payload, parent.payload)
            if episodes is None:
                self.catalog.request_episodes(series.payload, parent.payload)
                return None
            return [_item_node(e, "episode") for e in episodes]
        return []

    def _group_items(self, group: NavNode) -> Optional[list[NavNode]]:
        if group.key == FAVORITES_GROUP:
            return [_item_node(i) for i in self._favorite_items()]
        if group.key == RECENT_GROUP:
            return [_item_node(i) for i in self._recent_items()]
        section = self.section_of(group) or "live"
        items = self.catalog.items(section, group.key)
        if items is None:
            self.catalog.request_group(section, group.key)
            return None
        return [_item_node(i) for i in items]

    def _all_live_items(self) -> list[dict]:
        seen, result = set(), []
        for group in self.catalog.groups("live"):
            for item in self.catalog.items("live", group.id) or []:
                key = _item_key(item)
                if key not in seen:
                    seen.add(key)
                    result.append(item)
        return result

    def _favorite_items(self) -> list[dict]:
        names = set(self.config.favorites or [])
        return [i for i in self._all_live_items() if i.get("name") in names] if names else []

    def _recent_items(self) -> list[dict]:
        provider = self.catalog.current_provider_name()
        seen, result = set(), []
        for entry in self.config.watch_history or []:
            if entry.get("provider_name") != provider or entry.get("item_type") != "channel":
                continue
            item = entry.get("item_data") or {}
            key = _item_key(item)
            if key not in seen:
                seen.add(key)
                result.append(item)
            if len(result) >= _RECENT_LIMIT:
                break
        return result

    def _pinned_groups(self) -> list[NavNode]:
        pinned = []
        favorites = self._favorite_items()
        if favorites:
            pinned.append(NavNode("group", FAVORITES_GROUP, "★ Favorites", None, len(favorites)))
        recent = self._recent_items()
        if recent:
            pinned.append(NavNode("group", RECENT_GROUP, "⟲ Recent", None, len(recent)))
        return pinned

    # --- movement ----------------------------------------------------------------------
    def is_playable(self, node: NavNode) -> bool:
        if node.layer == "episode":
            return True
        return node.layer == "item" and self.section_of(node) in ("live", "movies")

    def open(self, node: NavNode) -> None:
        if node.layer == "source":
            if node.key != self.catalog.current_provider_name():
                self.catalog.select_provider(node.key)
            self._path = [node]
        else:
            depth = {"section": 1, "group": 2, "item": 3, "season": 4, "episode": 5}[node.layer]
            self._path = self._path[:depth] + [node]
            if node.layer == "section":
                self.catalog.request_section(node.key)
        self.changed.emit()

    def up(self) -> bool:
        if len(self._path) <= 1:
            return False
        self._path.pop()
        self.changed.emit()
        return True

    def reset_to(self, section: str = "live", group_id: Optional[str] = None) -> None:
        source = self._source()
        section_node = NavNode("section", section, SECTION_TITLES.get(section, section), section)
        self._path = [source, section_node]
        self.catalog.request_section(section)
        if group_id is not None:
            group = next((g for g in self.children(section_node) or [] if g.key == group_id), None)
            self._path.append(group or NavNode("group", group_id, group_id))
        self.changed.emit()

    def columns(self):
        if not self._path:
            return (None, self.children(None)), (None, None)
        left_parent = self._path[-2] if len(self._path) >= 2 else None
        right_parent = self._path[-1]
        return (left_parent, self.children(left_parent)), (right_parent, self.children(right_parent))

    # --- catalog events ------------------------------------------------------------------
    def _emit_changed(self, *args) -> None:
        self.changed.emit()

    def _on_provider_ready(self, name: str) -> None:
        if not self._path or self._path[0].key != name:
            self._path = [self._source()]
        self.changed.emit()
```

- [ ] **Step 4: Verify** (`tests.test_navigation`, then the full suite), **lint and type-check**, then **commit** with `feat: add the layered navigation model`.

---

### Task 8: Zapping (`services/zapping.py`)

**Files:**
- Create: `services/zapping.py`
- Test: `tests/test_zapping.py`

**Interfaces:**
- Produces:
  - `ZapContext(group_key: str = "", channels: list[dict] | None = None)` with:
    - `group_key`, `channels`, `current_index: int`;
    - `current() -> dict | None`;
    - `set_current(channel) -> bool`;
    - `peek(delta) -> tuple[int, dict] | None` (wraps around);
    - `by_number(text: str) -> dict | None` (leading zeros ignored; falls back to the 1-based list position);
    - `same(a, b) -> bool` (static; compares `id`, then `cmd`, then `name`).
  - `ZapController(QObject)`, constructed as `ZapController(rest_ms=400, burst_ms=1000, digits_ms=1200, max_digits=4, clock=time.monotonic)`. It has:
    - signals `tuneRequested(dict)`, `highlightChanged(dict, int)`, `columnRequested(bool)`, `digitsChanged(str)`, `numberNotFound(str)`;
    - attribute `context: ZapContext`;
    - methods `set_context(context)`, `tuned(channel)` (call after any tune, from any source), `step(delta)`, `digit(d: str)`, `previous()`, `close_column()`.

Behaviour:
- **`step(delta)`:**
  - If the last step was more than `burst_ms` ago and the column is closed, it moves and emits `tuneRequested` immediately (the banner case).
  - Otherwise it opens the column (`columnRequested(True)`), moves the highlight (`highlightChanged`) and restarts a single-shot `rest_ms` timer. When the timer fires, it emits `tuneRequested(highlighted)`, unless that is already the tuned channel.
- **`digit`:**
  - Appends to the buffer, up to `max_digits`, and emits `digitsChanged(buffer)`.
  - Each digit restarts the `digits_ms` timer. When it fires, a matching channel is tuned; a missing one emits `numberNotFound(buffer)`. Either way it then emits `digitsChanged("")`.
- **`previous()`** tunes the channel that was current before the last different tune. It does nothing if there is none.
- **`tuned(channel)`** updates `context.current_index` (when the channel is in the context) and the previous-channel memory, and cancels a pending rest timer.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_zapping.py`:
```python
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from services.zapping import ZapContext, ZapController

CHANNELS = [{"id": i, "name": f"C{i}", "number": str(i)} for i in (1, 2, 3, 7)]


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class ZapContextTests(unittest.TestCase):
    def test_peek_wraps(self):
        ctx = ZapContext("g", CHANNELS)
        ctx.set_current(CHANNELS[0])
        self.assertEqual(ctx.peek(-1), (3, CHANNELS[3]))
        ctx.set_current(CHANNELS[3])
        self.assertEqual(ctx.peek(1), (0, CHANNELS[0]))

    def test_single_and_empty(self):
        self.assertIsNone(ZapContext("g", []).peek(1))
        one = ZapContext("g", [CHANNELS[0]])
        one.set_current(CHANNELS[0])
        self.assertEqual(one.peek(1), (0, CHANNELS[0]))

    def test_by_number(self):
        ctx = ZapContext("g", CHANNELS)
        self.assertEqual(ctx.by_number("007"), CHANNELS[3])
        self.assertIsNone(ctx.by_number("9"))
        unnumbered = ZapContext("g", [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}])
        self.assertEqual(unnumbered.by_number("2")["id"], "b")


class ZapControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.clock = Clock()
        self.zap = ZapController(rest_ms=40, burst_ms=1000, digits_ms=60, clock=self.clock)
        self.zap.set_context(ZapContext("g", CHANNELS))
        self.zap.tuned(CHANNELS[0])
        self.tunes, self.columns, self.digits, self.missing = [], [], [], []
        self.zap.tuneRequested.connect(self.tunes.append)
        self.zap.columnRequested.connect(self.columns.append)
        self.zap.digitsChanged.connect(self.digits.append)
        self.zap.numberNotFound.connect(self.missing.append)

    def test_single_press_tunes_immediately(self):
        self.zap.step(1)
        self.assertEqual(self.tunes, [CHANNELS[1]])
        self.assertEqual(self.columns, [])

    def test_burst_opens_column_and_tunes_after_rest(self):
        self.zap.step(1)
        self.zap.tuned(CHANNELS[1])
        self.clock.now += 0.3
        self.zap.step(1)
        self.clock.now += 0.2
        self.zap.step(1)
        self.assertEqual(self.columns, [True])
        self.assertEqual(self.tunes, [CHANNELS[1]])
        QTest.qWait(120)
        self.assertEqual(self.tunes, [CHANNELS[1], CHANNELS[3]])

    def test_rest_on_already_tuned_channel_does_nothing(self):
        self.zap.step(1)
        self.zap.tuned(CHANNELS[1])
        self.clock.now += 0.1
        self.zap.step(1)
        self.zap.step(-1)
        QTest.qWait(120)
        self.assertEqual(self.tunes, [CHANNELS[1]])

    def test_digits(self):
        self.zap.digit("0")
        self.zap.digit("7")
        self.assertEqual(self.digits, ["0", "07"])
        QTest.qWait(150)
        self.assertEqual(self.tunes, [CHANNELS[3]])
        self.assertEqual(self.digits[-1], "")

    def test_unknown_number(self):
        self.zap.digit("9")
        QTest.qWait(150)
        self.assertEqual((self.tunes, self.missing, self.digits[-1]), ([], ["9"], ""))

    def test_max_digits(self):
        for d in "12345":
            self.zap.digit(d)
        self.assertEqual(self.digits[-1], "1234")

    def test_previous_channel(self):
        fresh = ZapController(clock=self.clock)
        seen = []
        fresh.tuneRequested.connect(seen.append)
        fresh.previous()
        self.assertEqual(seen, [])
        self.zap.tuned(CHANNELS[2])
        self.zap.previous()
        self.assertEqual(self.tunes, [CHANNELS[0]])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify they fail.** Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement `services/zapping.py`**

```python
"""Channel up/down, number entry and previous channel (spec §5.3)."""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from PySide6.QtCore import QObject, QTimer, Signal

logger = logging.getLogger(__name__)


class ZapContext:
    """The ordered channels ↑/↓ moves through: the group the current channel was tuned from."""

    def __init__(self, group_key: str = "", channels: Optional[list[dict]] = None):
        self.group_key = group_key
        self.channels = list(channels or [])
        self.current_index = -1

    @staticmethod
    def same(a: Optional[dict], b: Optional[dict]) -> bool:
        if not a or not b:
            return False
        for key in ("id", "cmd", "name"):
            if a.get(key) is not None and b.get(key) is not None:
                return str(a.get(key)) == str(b.get(key))
        return False

    def index_of(self, channel: dict) -> int:
        return next((i for i, c in enumerate(self.channels) if self.same(c, channel)), -1)

    def set_current(self, channel: dict) -> bool:
        index = self.index_of(channel)
        if index >= 0:
            self.current_index = index
        return index >= 0

    def current(self) -> Optional[dict]:
        if 0 <= self.current_index < len(self.channels):
            return self.channels[self.current_index]
        return None

    def peek(self, delta: int, start: Optional[int] = None) -> Optional[tuple[int, dict]]:
        if not self.channels:
            return None
        base = self.current_index if start is None else start
        if base < 0:  # nothing tuned yet: +1 starts at the first channel, -1 at the last
            base = -1 if delta > 0 else 0
        index = (base + delta) % len(self.channels)
        return index, self.channels[index]

    def by_number(self, text: str) -> Optional[dict]:
        digits = text.lstrip("0") or "0"
        for channel in self.channels:
            number = str(channel.get("number") or "").strip().lstrip("0") or None
            if number == digits:
                return channel
        if not any(str(c.get("number") or "").strip() for c in self.channels):
            position = int(digits)
            if 1 <= position <= len(self.channels):
                return self.channels[position - 1]
        return None


class ZapController(QObject):
    tuneRequested = Signal(dict)
    highlightChanged = Signal(dict, int)
    columnRequested = Signal(bool)
    digitsChanged = Signal(str)
    numberNotFound = Signal(str)

    def __init__(
        self,
        rest_ms: int = 400,
        burst_ms: int = 1000,
        digits_ms: int = 1200,
        max_digits: int = 4,
        clock: Callable[[], float] = time.monotonic,
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        self.context = ZapContext()
        self._burst_s = burst_ms / 1000
        self._max_digits = max_digits
        self._clock = clock
        self._last_step = float("-inf")
        self._column_open = False
        self._highlight: Optional[int] = None
        self._tuned: Optional[dict] = None
        self._previous: Optional[dict] = None
        self._digits = ""
        self._rest = QTimer(self, singleShot=True, interval=rest_ms)
        self._rest.timeout.connect(self._tune_highlight)
        self._digit_timer = QTimer(self, singleShot=True, interval=digits_ms)
        self._digit_timer.timeout.connect(self._commit_digits)

    def set_context(self, context: ZapContext) -> None:
        self.context = context
        if self._tuned is not None:
            context.set_current(self._tuned)

    def tuned(self, channel: dict) -> None:
        if self._tuned is not None and not ZapContext.same(self._tuned, channel):
            self._previous = self._tuned
        self._tuned = channel
        self.context.set_current(channel)
        self._rest.stop()

    def step(self, delta: int) -> None:
        now = self._clock()
        burst = (now - self._last_step) < self._burst_s
        self._last_step = now
        start = self._highlight if (self._column_open and self._highlight is not None) else None
        target = self.context.peek(delta, start)
        if target is None:
            return
        index, channel = target
        if not burst and not self._column_open:
            self.tuneRequested.emit(channel)
            return
        if not self._column_open:
            self._column_open = True
            self.columnRequested.emit(True)
        self._highlight = index
        self.highlightChanged.emit(channel, index)
        self._rest.start()

    def close_column(self) -> None:
        if self._column_open:
            self._column_open = False
            self._highlight = None
            self.columnRequested.emit(False)

    def _tune_highlight(self) -> None:
        if self._highlight is None:
            return
        channel = self.context.channels[self._highlight]
        if not ZapContext.same(channel, self._tuned):
            self.tuneRequested.emit(channel)

    def digit(self, d: str) -> None:
        if not d.isdigit() or len(self._digits) >= self._max_digits:
            return
        self._digits += d
        self.digitsChanged.emit(self._digits)
        self._digit_timer.start()

    def _commit_digits(self) -> None:
        text, self._digits = self._digits, ""
        channel = self.context.by_number(text) if text else None
        if channel is not None:
            self.tuneRequested.emit(channel)
        elif text:
            self.numberNotFound.emit(text)
        self.digitsChanged.emit("")

    def previous(self) -> None:
        if self._previous is not None:
            self.tuneRequested.emit(self._previous)
```
- [ ] **Step 4: Verify, lint, type-check, commit** (`feat: add zapping with debounce, digits and previous channel`).

---

### Task 9: Playback session (`services/playback.py`)

A polled wrapper around one libVLC media player. It is safe on the GUI thread and has no VLC event callbacks.

**Files:**
- Create: `services/playback.py`
- Create: `tests/fixtures/smoke.mp4`, copied from the MPV branch: `git show main:tests/fixtures/mpv-smoke.mp4 > tests/fixtures/smoke.mp4` (35 KB H.264/AAC, generated originally for QiTV tests).
- Test: `tests/test_playback_session.py`

**Interfaces:**
- Consumes: `PlayKind` and `media_options` (Task 3).
- Produces:
  - `PlaybackState` (an Enum of str): `IDLE`, `OPENING`, `BUFFERING`, `PLAYING`, `PAUSED`, `ENDED`, `ERROR`.
  - `create_instance(extra_args: list[str] | None = None) -> vlc.Instance | None`. It returns None if libVLC cannot load. The default args are `["--no-video-title-show", "--quiet"]`.
  - `PlaybackSession(instance, parent=None, *, poll_ms=250, open_timeout_ms=20000, stall_ms=6000)` with:
    - signals `stateChanged(str)`, `positionChanged(int, int)` (ms, length ms; at least once a second while playing), `errorOccurred(str)`, `ended()`, `seekableChanged(bool)`;
    - `set_window(win_id: int)`, which dispatches to `set_nsobject` on macOS, `set_hwnd` on Windows and `set_xwindow` elsewhere;
    - `play(url, *, kind, latency="normal", user_agent=None, start_ms=None)`;
    - `stop()`, `toggle_pause()`, `set_paused(bool)`, `seek(ms)`, `seek_relative(ms)`, `set_rate(float)`;
    - `volume() -> int`, `set_volume(int)`, `toggle_mute() -> bool`;
    - `audio_tracks() -> list[tuple[int, str]]`, `set_audio_track(int)`, `subtitle_tracks() -> list[tuple[int, str]]`, `set_subtitle_track(int)`;
    - `state() -> PlaybackState`, `time_ms() -> int`, `length_ms() -> int`, `is_seekable() -> bool`, `url -> str | None`, `kind -> PlayKind | None`;
    - `release()`.
  - Stall recovery (live and unknown kinds only): if `time_ms()` is unchanged for `stall_ms` while PLAYING or BUFFERING, cycle the audio track. On the third stall in one play, restart the same URL and options once. This is the v1.13.8 watchdog, ported.
  - Resume: when `start_ms` is given, seek there once the media becomes seekable.
  - Open timeout: if neither PLAYING nor PAUSED is reached within `open_timeout_ms`, the session goes to ERROR and emits `errorOccurred("Timed out opening the stream")`.

- [ ] **Step 1: Copy the fixture and write the failing tests**

```bash
mkdir -p tests/fixtures && git show main:tests/fixtures/mpv-smoke.mp4 > tests/fixtures/smoke.mp4 && ls -l tests/fixtures/smoke.mp4
```
Create `tests/test_playback_session.py`:
```python
import os
from pathlib import Path
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from services.playback import PlaybackSession, PlaybackState, create_instance
from services.vlc_profile import PlayKind

FIXTURE = Path(__file__).parent / "fixtures" / "smoke.mp4"
INSTANCE = create_instance(["--vout=dummy", "--aout=dummy", "--no-video-title-show", "--quiet"])


def wait_for(predicate, timeout_ms=8000):
    waited = 0
    while not predicate() and waited < timeout_ms:
        QTest.qWait(50)
        waited += 50
    return predicate()


@unittest.skipIf(INSTANCE is None, "libVLC is not available")
class PlaybackSessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.session = PlaybackSession(INSTANCE, poll_ms=50, open_timeout_ms=3000)
        self.states = []
        self.session.stateChanged.connect(self.states.append)
        self.addCleanup(self.session.release)

    def test_stop_is_safe_before_play(self):
        self.session.stop()
        self.assertIs(self.session.state(), PlaybackState.IDLE)

    def test_plays_pauses_seeks_and_stops(self):
        self.session.play(FIXTURE.as_uri(), kind=PlayKind.VOD_FILE)
        self.assertTrue(wait_for(lambda: self.session.state() is PlaybackState.PLAYING))
        self.assertTrue(wait_for(lambda: self.session.length_ms() > 0))
        self.session.set_paused(True)
        self.assertTrue(wait_for(lambda: self.session.state() is PlaybackState.PAUSED))
        self.session.set_paused(False)
        self.assertTrue(wait_for(lambda: self.session.state() is PlaybackState.PLAYING))
        self.session.stop()
        self.assertIs(self.session.state(), PlaybackState.IDLE)
        self.assertIn("playing", self.states)

    def test_resume_position(self):
        self.session.play(FIXTURE.as_uri(), kind=PlayKind.VOD_FILE, start_ms=1000)
        self.assertTrue(wait_for(lambda: self.session.time_ms() >= 900))

    def test_bad_stream_reports_error(self):
        errors = []
        self.session.errorOccurred.connect(errors.append)
        self.session.play("http://127.0.0.1:9/missing.ts", kind=PlayKind.LIVE)
        self.assertTrue(wait_for(lambda: self.session.state() is PlaybackState.ERROR, 6000))
        self.assertTrue(errors)

    def test_ended_before_playing_is_an_error(self):
        errors = []
        self.session.errorOccurred.connect(errors.append)
        missing = (FIXTURE.parent / "does-not-exist.ts").as_uri()
        self.session.play(missing, kind=PlayKind.LIVE)
        self.assertTrue(wait_for(lambda: self.session.state() is PlaybackState.ERROR, 6000))
        self.assertTrue(errors)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify they fail.** Expected: `ModuleNotFoundError: services.playback`.

- [ ] **Step 3: Implement `services/playback.py`**

```python
"""One libVLC media player, polled on the GUI thread (no VLC callbacks cross threads)."""

from __future__ import annotations

from enum import Enum
import logging
import platform
from typing import Optional

from PySide6.QtCore import QElapsedTimer, QObject, QTimer, Signal

from services.vlc_profile import PlayKind, media_options

logger = logging.getLogger(__name__)

try:
    import vlc
except (ImportError, OSError) as e:  # libVLC missing or broken
    vlc = None
    logger.warning("libVLC unavailable: %s", e)

_STALL_AUDIO_RETRIES = 2


class PlaybackState(str, Enum):
    IDLE = "idle"
    OPENING = "opening"
    BUFFERING = "buffering"
    PLAYING = "playing"
    PAUSED = "paused"
    ENDED = "ended"
    ERROR = "error"


def create_instance(extra_args: Optional[list[str]] = None):
    if vlc is None:
        return None
    try:
        return vlc.Instance(extra_args or ["--no-video-title-show", "--quiet"])
    except Exception as e:
        logger.warning("Could not create a libVLC instance: %s", e)
        return None


def _map_state(state) -> PlaybackState:
    mapping = {
        vlc.State.Opening: PlaybackState.OPENING,
        vlc.State.Buffering: PlaybackState.BUFFERING,
        vlc.State.Playing: PlaybackState.PLAYING,
        vlc.State.Paused: PlaybackState.PAUSED,
        vlc.State.Ended: PlaybackState.ENDED,
        vlc.State.Error: PlaybackState.ERROR,
    }
    return mapping.get(state, PlaybackState.IDLE)


class PlaybackSession(QObject):
    stateChanged = Signal(str)
    positionChanged = Signal(int, int)
    errorOccurred = Signal(str)
    ended = Signal()
    seekableChanged = Signal(bool)

    def __init__(
        self,
        instance,
        parent: Optional[QObject] = None,
        *,
        poll_ms: int = 250,
        open_timeout_ms: int = 20000,
        stall_ms: int = 6000,
    ):
        super().__init__(parent)
        self._instance = instance
        self._player = instance.media_player_new()
        self._player.video_set_mouse_input(False)
        self._player.video_set_key_input(False)
        self._state = PlaybackState.IDLE
        self._url: Optional[str] = None
        self._kind: Optional[PlayKind] = None
        self._options: list[str] = []
        self._start_ms: Optional[int] = None
        self._seekable = False
        self._open_timeout_ms = open_timeout_ms
        self._stall_ms = stall_ms
        self._opened = QElapsedTimer()
        self._stall_clock = QElapsedTimer()
        self._last_time = -1
        self._stalls = 0
        self._restarted = False
        self._poll = QTimer(self, interval=poll_ms)
        self._poll.timeout.connect(self._tick)

    # --- properties -------------------------------------------------------------------
    @property
    def url(self) -> Optional[str]:
        return self._url

    @property
    def kind(self) -> Optional[PlayKind]:
        return self._kind

    def state(self) -> PlaybackState:
        return self._state

    def time_ms(self) -> int:
        return max(0, int(self._player.get_time() or 0))

    def length_ms(self) -> int:
        return max(0, int(self._player.get_length() or 0))

    def is_seekable(self) -> bool:
        return bool(self._player.is_seekable())

    # --- output -----------------------------------------------------------------------
    def set_window(self, win_id: int) -> None:
        system = platform.system()
        if system == "Darwin":
            self._player.set_nsobject(int(win_id))
        elif system == "Windows":
            self._player.set_hwnd(int(win_id))
        else:
            self._player.set_xwindow(int(win_id))

    # --- control ----------------------------------------------------------------------
    def play(
        self,
        url: str,
        *,
        kind: PlayKind,
        latency: str = "normal",
        user_agent: Optional[str] = None,
        start_ms: Optional[int] = None,
    ) -> None:
        self._url, self._kind = url, kind
        self._options = media_options(kind, latency=latency, user_agent=user_agent)
        self._start_ms = start_ms if start_ms and start_ms > 0 else None
        self._stalls, self._restarted = 0, False
        self._start_media()

    def _start_media(self) -> None:
        media = self._instance.media_new(self._url)
        for option in self._options:
            media.add_option(option)
        self._player.set_media(media)
        media.release()
        self._seekable = False
        self._ever_played = False
        self._last_time = -1
        self._opened.start()
        self._stall_clock.start()
        self._set_state(PlaybackState.OPENING)
        if self._player.play() == -1:
            self._fail("VLC could not start playback")
            return
        self._poll.start()

    def stop(self) -> None:
        self._poll.stop()
        self._player.stop()
        self._set_state(PlaybackState.IDLE)

    def toggle_pause(self) -> None:
        self._player.pause()

    def set_paused(self, paused: bool) -> None:
        self._player.set_pause(1 if paused else 0)

    def seek(self, ms: int) -> None:
        if self.is_seekable():
            self._player.set_time(max(0, int(ms)))

    def seek_relative(self, ms: int) -> None:
        self.seek(self.time_ms() + ms)

    def set_rate(self, rate: float) -> None:
        self._player.set_rate(float(rate))

    def volume(self) -> int:
        return max(0, int(self._player.audio_get_volume()))

    def set_volume(self, value: int) -> None:
        self._player.audio_set_volume(max(0, min(100, int(value))))

    def toggle_mute(self) -> bool:
        muted = not bool(self._player.audio_get_mute())
        self._player.audio_set_mute(muted)
        return muted

    def audio_tracks(self) -> list[tuple[int, str]]:
        return [(int(i), self._decode(n)) for i, n in (self._player.audio_get_track_description() or [])]

    def set_audio_track(self, track_id: int) -> None:
        self._player.audio_set_track(int(track_id))

    def subtitle_tracks(self) -> list[tuple[int, str]]:
        return [(int(i), self._decode(n)) for i, n in (self._player.video_get_spu_description() or [])]

    def set_subtitle_track(self, track_id: int) -> None:
        self._player.video_set_spu(int(track_id))

    @staticmethod
    def _decode(name) -> str:
        return name.decode("utf-8", "replace") if isinstance(name, bytes) else str(name)

    def release(self) -> None:
        self._poll.stop()
        try:
            self._player.stop()
            self._player.release()
        except Exception as e:
            logger.debug("Releasing the media player failed: %s", e)

    # --- polling ----------------------------------------------------------------------
    def _set_state(self, state: PlaybackState) -> None:
        if state is not self._state:
            self._state = state
            self.stateChanged.emit(state.value)

    def _fail(self, message: str) -> None:
        self._poll.stop()
        self._set_state(PlaybackState.ERROR)
        self.errorOccurred.emit(message)

    def _tick(self) -> None:
        state = _map_state(self._player.get_state())
        if state is PlaybackState.ERROR:
            self._fail("The stream could not be played")
            return
        if state is PlaybackState.ENDED:
            if not self._ever_played:
                self._fail("The stream could not be opened")
                return
            self._poll.stop()
            self._set_state(PlaybackState.ENDED)
            self.ended.emit()
            return
        if state is PlaybackState.IDLE and self._state is PlaybackState.OPENING:
            state = PlaybackState.OPENING  # libVLC reports NothingSpecial briefly after play()
        if state in (PlaybackState.PLAYING, PlaybackState.PAUSED):
            self._ever_played = True
        self._set_state(state)
        if state in (PlaybackState.OPENING, PlaybackState.BUFFERING) and not self._ever_played:
            if self._opened.elapsed() > self._open_timeout_ms:
                self._fail("Timed out opening the stream")
            return
        seekable = self.is_seekable()
        if seekable != self._seekable:
            self._seekable = seekable
            self.seekableChanged.emit(seekable)
        if self._start_ms is not None and seekable and state is PlaybackState.PLAYING:
            self._player.set_time(self._start_ms)
            self._start_ms = None
        if state in (PlaybackState.PLAYING, PlaybackState.PAUSED):
            self.positionChanged.emit(self.time_ms(), self.length_ms())
        self._watch_stall(state)

    def _watch_stall(self, state: PlaybackState) -> None:
        if self._kind not in (PlayKind.LIVE, PlayKind.UNKNOWN):
            return
        current = self.time_ms()
        if state not in (PlaybackState.PLAYING, PlaybackState.BUFFERING) or current != self._last_time:
            self._last_time = current
            self._stall_clock.restart()
            return
        if self._stall_clock.elapsed() < self._stall_ms:
            return
        self._stall_clock.restart()
        self._stalls += 1
        if self._stalls <= _STALL_AUDIO_RETRIES:
            logger.warning("Playback stalled (attempt %d); cycling the audio track", self._stalls)
            track = self._player.audio_get_track()
            self._player.audio_set_track(-1)
            QTimer.singleShot(500, lambda: self._player.audio_set_track(track))
        elif not self._restarted:
            logger.warning("Playback stalled again; restarting the stream")
            self._restarted = True
            self._player.stop()
            self._start_media()
```

- [ ] **Step 4: Verify.** Run `uv run python -m unittest tests.test_playback_session -v`. Expected: 5 PASS locally (VLC.app is installed). In an environment without libVLC they SKIP. Then run the full suite.

- [ ] **Step 5: Lint, type-check, commit** (`feat: add a polled libVLC playback session`). Include `tests/fixtures/smoke.mp4` in the commit, and add `!tests/fixtures/*.mp4` to `.gitignore` if mp4 files are ignored there (check with `git check-ignore -v tests/fixtures/smoke.mp4`).

---

### Task 10: Watch state (`services/watch_state.py`)

**Files:**
- Create: `services/watch_state.py`
- Test: `tests/test_watch_state.py`

**Interfaces:**
- Consumes these ConfigManager members:
  - `add_to_watch_history(item, item_type, link, provider_name)`;
  - `last_watched`, `last_live_channel`;
  - `get_playback_position(cid) -> dict | None`, `save_playback_position(cid, position_ms, duration_ms)`, `clear_playback_position(cid)`;
  - `save_config()`.

  Read `config_manager.py` to confirm these signatures before writing code.
- Produces `WatchState(config, *, flush_interval_s=30.0, clock=time.monotonic)` with:
  - `content_id(provider_name, item, *, item_type) -> str`. This is `"provider:id"` like v1.13.8, except that for `item_type == "episode"` it is `"provider:id:s<season>:e<series|number>"`. The fallback when no id/cmd/name exists is a sha1 of the sorted JSON, never `hash()`.
  - `record_play(provider_name, item, *, item_type, link, group_id=None)`: adds to history; sets `last_watched`; for `item_type == "channel"` also sets `last_live_channel = {"provider_name", "group_id", "item"}`; then saves the config.
  - `resume_position(cid) -> int | None`: the saved ms when it is more than 5000 and below 90% of the duration.
  - `update_position(cid, position_ms, duration_ms)`: saves in memory, and calls `save_config()` at most once per `flush_interval_s`.
  - `clear_position(cid)` and `flush()`, which always saves.

- [ ] **Step 1: Write failing tests** covering:
  - episode ids differing by season and episode while movie ids keep the v1.13.8 format `"P:42"`;
  - a stable fallback id across calls;
  - `record_play` for a channel setting both `last_watched` and `last_live_channel` and calling `save_config` once;
  - a VOD play not touching `last_live_channel`;
  - the resume thresholds: 4000 ms gives None, 50% gives the position, 95% gives None;
  - `update_position` called twice within the interval saving once, and again after the fake clock passes 30 s.

  Use a `SimpleNamespace` config whose methods record calls, and whose `get_playback_position` returns from a dict. Write each case as its own `test_*` method with concrete values (for example item `{"id": 42, "name": "Film"}` and provider `"P"`).

- [ ] **Step 2: Run to verify they fail; Step 3: implement** these exact behaviours:
```python
"""History, last-watched, last live channel, resume points (spec §4.1 PlaybackSession bookkeeping)."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

RESUME_MIN_MS = 5000
RESUME_MAX_RATIO = 0.90


class WatchState:
    def __init__(self, config, *, flush_interval_s: float = 30.0, clock: Callable[[], float] = time.monotonic):
        self.config = config
        self._flush_interval_s = flush_interval_s
        self._clock = clock
        self._last_flush = clock()
        self._dirty = False

    @staticmethod
    def content_id(provider_name: str, item: dict, *, item_type: str) -> str:
        key = item.get("id") or item.get("stream_id") or item.get("series_id")
        if key is None:
            key = item.get("cmd") or item.get("name")
        if key is None:
            blob = json.dumps(item, sort_keys=True, default=str).encode("utf-8")
            key = hashlib.sha1(blob).hexdigest()
        cid = f"{provider_name}:{key}"
        if item_type == "episode":
            season = item.get("season") or item.get("season_id") or ""
            episode = item.get("series") or item.get("episode_num") or item.get("number") or ""
            cid += f":s{season}:e{episode}"
        return cid

    def record_play(self, provider_name: str, item: dict, *, item_type: str, link: str, group_id=None) -> None:
        self.config.add_to_watch_history(item, item_type, link, provider_name)
        self.config.last_watched = {
            "item_data": item,
            "item_type": item_type,
            "link": link,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "provider_name": provider_name,
        }
        if item_type == "channel":
            self.config.last_live_channel = {
                "provider_name": provider_name,
                "group_id": "" if group_id is None else str(group_id),
                "item": item,
            }
        self.config.save_config()
        self._mark_flushed()

    def resume_position(self, cid: str) -> Optional[int]:
        saved = self.config.get_playback_position(cid) or {}
        position, duration = int(saved.get("position") or 0), int(saved.get("duration") or 0)
        if position <= RESUME_MIN_MS:
            return None
        if duration > 0 and position / duration >= RESUME_MAX_RATIO:
            return None
        return position

    def update_position(self, cid: str, position_ms: int, duration_ms: int) -> None:
        self.config.save_playback_position(cid, position_ms, duration_ms)
        self._dirty = True
        if self._clock() - self._last_flush >= self._flush_interval_s:
            self.flush()

    def clear_position(self, cid: str) -> None:
        self.config.clear_playback_position(cid)
        self.flush()

    def flush(self) -> None:
        self.config.save_config()
        self._mark_flushed()

    def _mark_flushed(self) -> None:
        self._dirty = False
        self._last_flush = self._clock()
```
  If `ConfigManager.save_playback_position` has a different signature, adapt the call and note it in the report.

- [ ] **Step 4: Verify, lint, type-check, commit** (`feat: add watch-state bookkeeping for the TV shell`).

---

### Task 11: External players (`services/external_players.py`)

**Files:**
- Create: `services/external_players.py`
- Test: `tests/test_external_players.py`

**Interfaces:**
- Produces:
  - `launch_vlc(url: str) -> tuple[bool, str]`
  - `launch_mpv(url: str) -> tuple[bool, str]`

  Each returns `(started, message)`. The message is empty on success, and otherwise is the user-facing text v1.13.8 put in its QMessageBox.

- [ ] **Step 1: Port.** Move the logic of `PlaybackMixin._launch_vlc` and `PlaybackMixin._launch_mpv` (`mixins/playback_mixin.py`) into these functions **verbatim**, including every platform path and the python-mpv attempt, with these changes:
  - replace each `QMessageBox.*(...)` with `return False, "<the same text>"`;
  - replace each successful path's end with `return True, ""`;
  - turn instance state used for single-instance reuse (`self._external_vlc_*`, `self._external_mpv_player`) into module-level variables with the same semantics.

  Keep the mixin methods untouched; Phase 3 deletes them.
- [ ] **Step 2: Tests**, with `unittest.mock.patch` on `platform.system`, `shutil.which`, `os.path.exists` and `subprocess.Popen`:
  - macOS with VLC.app present calls `Popen(["open", "-a", <app path>, url])`, or whatever exact argv the ported code builds;
  - Linux with `which("vlc")` returning None gives `(False, msg)` with a non-empty msg;
  - MPV missing everywhere (python-mpv import patched to raise ImportError, and every path absent) gives `(False, msg)`.

  Write the assertions against the ported code's real argv.
- [ ] **Step 3: Verify, lint, commit** (`feat: move external VLC/MPV launching into a service`).

---

## Self-review notes for the executor

- Tasks 5–6 call `self.sender()`. It works with queued connections (JobRunner) and direct connections (ImmediateRunner).
- The duplication between `services/catalog.py` and `mixins/content_loading_mixin.py` (season normalization and Xtream episode formatting) is deliberate and temporary: Phase 3 deletes the mixin.

---

### Task 12: XMLTV downloads unpack by content, not by headers (EPG for the zap banner)

The Phase 0 final review found two problems in the existing fetchers:
- `EpgManager._fetch_epg_from_xtream` gunzips `r.content` when `Content-Encoding: gzip` is set, but `requests` has already decoded it. The second gunzip fails, and the source is marked unavailable until a forced refresh.
- `_fetch_epg_from_url` decompresses only when `Content-Type` is exactly `application/gzip` or `application/zip`. Common `.xml.gz` feeds are served as `application/octet-stream` and are never unpacked.

The zap banner's now/next depends on both.

**Files:**
- Modify: `epg_manager.py` (add the module function `write_xmltv_body`; use it in both fetchers)
- Test: `tests/test_epg_download.py`

**Interfaces:**
- Produces `write_xmltv_body(response, path: str) -> None`. It:
  - streams `response.iter_content(65536)` to `path + ".download"`;
  - inspects the first bytes: `1f 8b` means gunzip to `path`; `PK\x03\x04` means extract the first `*.xml` member to `path` (raising `ValueError("no XML in archive")` if there is none); anything else is moved to `path`;
  - always removes the `.download` file.

- [ ] **Step 1: Write the failing tests:**
  - plain XML is written as is;
  - a gzip body with an `application/octet-stream` header is unpacked;
  - an already-decoded body (plain XML bytes with a `Content-Encoding: gzip` header on the fake response) is written as is;
  - a zip body with `channel.xml` inside is extracted;
  - a zip without XML raises `ValueError` and leaves no temp file.

  Use a fake response: `SimpleNamespace(headers=..., iter_content=lambda n: iter([data[:10], data[10:]]))`.

- [ ] **Step 2: Run RED. Step 3: implement.** Replace the content-type and encoding branches in both fetchers with `write_xmltv_body(r, xmltv_file_path)`. In `_fetch_epg_from_url`, raise the timeout from 10 to 30 to match Xtream, and use `provider_prefers_https`-independent `verify=self.config_manager.ssl_verify` as before. Keep the surrounding cache logic unchanged.

- [ ] **Step 4: Verify. Step 5: Commit** (`fix: unpack XMLTV downloads by their bytes, not their headers`).
