# Phase 0 Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prepare branch `tv/vlc` for the TV redesign:
- land the behaviour-preserving tidy-up;
- fix the probable bugs it found, each with a regression test;
- add design tokens;
- run the test suite in CI on all four platforms;
- record the new plan in AGENTS.md.

**Architecture:** Small, independent changes to the existing v1.13.8 service and mixin layer. No new runtime dependencies and no UI changes. Each bug fix follows test-driven development: a failing regression test first, then the minimal fix.

**Tech Stack:** Python 3.14, PySide6 6.11.2, python-vlc, requests, aiohttp, `unittest` and `uv`.

**Spec:** `docs/superpowers/specs/2026-10-15-tv-on-vlc-design.md`. This plan implements §10 (bugs) and the Phase 0 row of §12.

## Global Constraints

- Python `>=3.14,<3.15`. Run everything through `uv run …` from the repository root `<repo>`, on branch `tv/vlc`.
- Tests use `unittest`. Run the suite with `uv run python -m unittest discover -s tests -v`; Qt tests set `QT_QPA_PLATFORM=offscreen`.
- Formatting: black and isort with line length 100. Lint: `uv run flake8 <files>`, which selects only E9, F63, F7 and F82. mypy must be no worse than baseline on touched modules.
- Logging uses `logging.getLogger(__name__)`. No `print` and no silent `except: pass`.
- Never create QPixmap/QIcon in worker threads.
- Add no new runtime dependencies in Phase 0. Add no native or compiled dependencies ever (spec D1/D2).
- Do not edit `video_player.py`, `channel_list.py`, `mixins/display_mixin.py` or `widgets/*.py`. They are replaced in Phases 1–3.
- Commits end with the trailer `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`. Do not push and do not touch `main`.

## Review Focus

These inputs are implied by the spec but not exercised by any existing test. Each now has a test in the task named:
1. **Old image indexes:** a v1.13.8 user's image `index.json` contains `null` entries and entries without an extension. Loading and eviction must not crash, and must delete the right file (Task 4).
2. **XMLTV times without a timezone** (`20261015233000`): the overnight correction still applies and keeps the input format (Task 5).
3. **Provider overrides the global HTTPS setting:** a provider whose own `prefer_https` is `False` while the global setting is `True` must stay on HTTP, and the reverse (Task 2).
4. **STB seasons listing episode numbers as strings** (`["1", "2"]`): next-season autoplay still builds episodes (Task 6).
5. **M3U files with old-Mac `\r`-only line endings or a UTF-8 BOM:** channel names, user agents and URLs come out clean (Task 7).

---

### Task 1: Land the behaviour-preserving tidy-up

The tidy-up lives uncommitted in worktree `<repo>/.claude/worktrees/agent-a642403a20b2d2dbf` on branch `tidy/v1.13.8`, based on `16459ee`.

**Files:**
- Modify (from the patch): `config_manager.py`, `content_loader.py`, `epg_manager.py`, `image_loader.py`, `image_manager.py`, `mixins/content_loading_mixin.py`, `mixins/export_mixin.py`, `mixins/playback_mixin.py`, `provider_manager.py`, `services/m3u.py`, `services/provider_api.py`, `sleep_manager.py`, `update_checker.py`, `workers.py`
- Create (from the worktree): `tests/test_provider_api.py`

**Interfaces:**
- Produces, in `services/provider_api.py` (later tasks use these):
  - `stb_portal_base(url: str, prefer_https: bool = False) -> str`, which returns `scheme://netloc` and upgrades `http` to `https` when preferred;
  - `stb_endpoint(base: str) -> str`;
  - `stb_create_link_url(base, content_type, cmd, *, is_episode=False, series=None) -> str`;
  - `https_fallback_candidates(url: str, prefer_https: bool) -> list[str]`.

- [ ] **Step 1: Record baselines on `tv/vlc`**

```bash
cd <repo> && git switch tv/vlc && uv sync --frozen --dev
uv run python -m unittest discover -s tests 2>&1 | tail -3
```
Expected: `Ran 14 tests` … `OK`.

- [ ] **Step 2: Export the tidy-up as a patch and apply it**

```bash
W=<repo>/.claude/worktrees/agent-a642403a20b2d2dbf
SP=<scratchpad>
git -C "$W" diff > "$SP/tidy.patch"
git apply --check "$SP/tidy.patch" && git apply "$SP/tidy.patch"
cp "$W/tests/test_provider_api.py" tests/test_provider_api.py
git status --short
```
Expected: the 14 files listed above show as modified, plus `?? tests/test_provider_api.py`.

- [ ] **Step 3: Verify tests, lint, types and imports**

```bash
uv run python -m unittest discover -s tests 2>&1 | tail -3
uv run flake8 config_manager.py content_loader.py epg_manager.py image_loader.py image_manager.py mixins/content_loading_mixin.py mixins/export_mixin.py mixins/playback_mixin.py provider_manager.py services/m3u.py services/provider_api.py sleep_manager.py update_checker.py workers.py tests/test_provider_api.py
uv run mypy config_manager.py content_loader.py epg_manager.py image_loader.py image_manager.py mixins/content_loading_mixin.py mixins/export_mixin.py mixins/playback_mixin.py provider_manager.py services/m3u.py services/provider_api.py sleep_manager.py update_checker.py workers.py 2>&1 | tail -1
QT_QPA_PLATFORM=offscreen uv run python -c "import channel_list, video_player, workers, provider_manager, epg_manager, content_loader, config_manager; print('imports ok')"
```
Expected:
- `Ran 17 tests` … `OK`.
- flake8 prints nothing.
- mypy ends with `Found 33 errors in 3 files` or fewer. The baseline before the tidy-up was 38 errors in 4 files.
- `imports ok`.

- [ ] **Step 4: Review the diff for behaviour changes**

Run `git diff --stat` and read `git diff` per file. Reject the task if any of these changed:
- a public method signature;
- a signal;
- a config key;
- an on-disk cache format;
- the threading model.

Expected:
- `except A, B:` without parentheses appears in places; this is valid PEP 758 syntax on 3.14.
- Removed functions have no remaining callers. Check with `git grep -n "<name>"` for `refresh_epg`, `_filter_and_sort_programs`, `get_image_from_url`, `get_image_from_base64` and `open_in_vlc`; each must give no hits outside `activity.md`.

- [ ] **Step 5: Commit**

```bash
git add -A config_manager.py content_loader.py epg_manager.py image_loader.py image_manager.py mixins/content_loading_mixin.py mixins/export_mixin.py mixins/playback_mixin.py provider_manager.py services/m3u.py services/provider_api.py sleep_manager.py update_checker.py workers.py tests/test_provider_api.py
git commit -F - <<'EOF'
refactor: tidy the service layer without behaviour changes

Merge copy-pasted STB/Xtream URL builders, EPG cache loaders and config
migrations into shared helpers; remove the unused off-thread image path
and other dead code; replace silent exception swallowing with debug logs.
Adds provider URL helper tests (14 -> 17 tests).

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
EOF
```

- [ ] **Step 6: Remove the spent worktree**

```bash
git worktree remove <repo>/.claude/worktrees/agent-a642403a20b2d2dbf --force
git branch -D tidy/v1.13.8 worktree-agent-a642403a20b2d2dbf 2>/dev/null; git worktree list
```
Expected: `git worktree list` shows only `<repo>  <sha> [tv/vlc]`.

---

### Task 2: Consistent provider URLs (per-provider HTTPS and STB export base)

Fixes spec §10 items 4 and 5:
- Some STB requests ignore the provider's own `prefer_https` override.
- STB exports pass the raw portal URL (`http://host/c/`) as the base, producing `…/c//play/live.php`.

**Files:**
- Modify: `services/provider_api.py` (add `provider_prefers_https`)
- Modify: `services/export.py` (`save_stb_content` normalizes its base)
- Modify: `mixins/playback_mixin.py` (`create_link`)
- Modify: `mixins/export_mixin.py` (every `stb_portal_base(...)` call, and `export_shown_channels` / `export_content_cached`)
- Modify: `mixins/content_loading_mixin.py` (every `.get("prefer_https", ...)` site, and the STB branch of `load_season_episodes`)
- Test: `tests/test_provider_urls.py`

**Interfaces:**
- Consumes: `stb_portal_base`, `stb_endpoint` and `stb_create_link_url` (Task 1).
- Produces: `provider_prefers_https(provider: dict, global_default: bool) -> bool` in `services/provider_api.py`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_provider_urls.py`:
```python
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mixins.content_loading_mixin import ContentLoadingMixin
from mixins.export_mixin import ExportMixin
from mixins.playback_mixin import PlaybackMixin
from services.export import save_stb_content
from services.provider_api import provider_prefers_https


class _Response:
    status_code = 200
    content = b"{}"

    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


def _host(cls, provider, global_https):
    host = cls.__new__(cls)
    host.provider_manager = SimpleNamespace(
        current_provider=provider, headers={}, current_provider_content={}
    )
    host.config_manager = SimpleNamespace(prefer_https=global_https, ssl_verify=True)
    host.content_type = "itv"
    host.sanitize_url = lambda url: url
    return host


class ProviderPrefersHttpsTests(unittest.TestCase):
    def test_provider_value_overrides_global(self):
        self.assertTrue(provider_prefers_https({"prefer_https": True}, False))
        self.assertFalse(provider_prefers_https({"prefer_https": False}, True))

    def test_missing_or_none_falls_back_to_global(self):
        self.assertTrue(provider_prefers_https({}, True))
        self.assertFalse(provider_prefers_https({"prefer_https": None}, False))


class CreateLinkHttpsTests(unittest.TestCase):
    def _created_url(self, provider, global_https):
        host = _host(PlaybackMixin, provider, global_https)
        calls = []

        def fake_get(url, **kwargs):
            calls.append(url)
            return _Response({"js": {"cmd": "ffmpeg http://stream.test/1.ts"}})

        with patch("mixins.playback_mixin.requests.get", side_effect=fake_get):
            link = host.create_link({"cmd": "ffmpeg http://localhost/ch/1_"})
        self.assertEqual(link, "http://stream.test/1.ts")
        return calls[0]

    def test_provider_https_preference_is_used(self):
        url = self._created_url({"url": "http://portal.test/c/", "prefer_https": True}, False)
        self.assertTrue(url.startswith("https://portal.test/"), url)

    def test_provider_can_opt_out_of_global_https(self):
        url = self._created_url({"url": "http://portal.test/c/", "prefer_https": False}, True)
        self.assertTrue(url.startswith("http://portal.test/"), url)


class StbSeriesFetchHttpsTests(unittest.TestCase):
    def test_sync_series_listing_uses_provider_preference(self):
        provider = {"url": "http://portal.test/c/", "prefer_https": True}
        host = _host(ExportMixin, provider, False)
        calls = []

        def fake_get(url, **kwargs):
            calls.append(url)
            return _Response({"js": {"data": []}})

        with patch("mixins.export_mixin.requests.get", side_effect=fake_get):
            host._fetch_series_list_sync({"category_id": "1", "id": "9"}, 0, "added", "seasons")
        self.assertTrue(calls[0].startswith("https://portal.test/server/load.php"), calls[0])


class StbEpisodeLoadingHttpsTests(unittest.TestCase):
    def test_stb_episodes_use_provider_preference(self):
        provider = {"type": "STB", "url": "http://portal.test/c/", "prefer_https": True}
        host = _host(ContentLoadingMixin, provider, False)
        host.current_category = {"id": "1"}
        host.current_series = {"id": "9"}
        host.cancel_content_loading = lambda: None
        host._start_content_worker = lambda *args, **kwargs: None
        host.cancel_button = SimpleNamespace(setText=lambda text: None)
        captured = {}

        def fake_loader(**kwargs):
            captured.update(kwargs)
            return object()

        with patch("mixins.content_loading_mixin.ContentLoader", side_effect=fake_loader):
            host.load_season_episodes({"id": "2"})
        self.assertEqual(captured["url"], "https://portal.test/server/load.php")


class StbExportBaseTests(unittest.TestCase):
    def test_raw_portal_url_is_reduced_to_scheme_and_host(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "out.m3u"
            items = [{"name": "One", "cmd": "ffmpeg http://localhost/ch/123_"}]
            save_stb_content("http://portal.test/c/", items, "00:1A:79:00:00:01", str(path))
            text = path.read_text(encoding="utf-8")
        self.assertIn(
            "http://portal.test/play/live.php?mac=00:1A:79:00:00:01&stream=123&extension=m3u8",
            text,
        )
        self.assertNotIn("//play", text.replace("http://", ""))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run python -m unittest tests.test_provider_urls -v`

Expected: an ImportError for `provider_prefers_https`. Once Step 3's helper exists, these fail:
- `test_provider_https_preference_is_used`
- `test_provider_can_opt_out_of_global_https`
- `test_sync_series_listing_uses_provider_preference`
- `test_stb_episodes_use_provider_preference`
- `test_raw_portal_url_is_reduced_to_scheme_and_host`

- [ ] **Step 3: Add the helper**

Append to `services/provider_api.py`:
```python
def provider_prefers_https(provider: Dict[str, Any], global_default: bool) -> bool:
    """A provider's own 'prefer_https' wins; unset (missing/None) falls back to the global setting."""
    value = provider.get("prefer_https")
    return global_default if value is None else bool(value)
```

- [ ] **Step 4: Use the helper everywhere a provider's HTTPS preference is resolved**

1. In `mixins/playback_mixin.py`, `create_link`: replace
   `stb_portal_base(selected_provider.get("url", ""), self.config_manager.prefer_https),` with
   `stb_portal_base(selected_provider.get("url", ""), provider_prefers_https(selected_provider, self.config_manager.prefer_https)),`.
   Also replace every `selected_provider.get("prefer_https", self.config_manager.prefer_https)` in the file with `provider_prefers_https(selected_provider, self.config_manager.prefer_https)`. Extend the import to: `from services.provider_api import provider_prefers_https, stb_create_link_url, stb_portal_base`.
2. In `mixins/content_loading_mixin.py`:
   - Replace every `X.get("prefer_https", self.config_manager.prefer_https)` (where X is `provider` or `selected_provider`) with `provider_prefers_https(X, self.config_manager.prefer_https)`.
   - In the STB branch of `load_season_episodes`, change `url = stb_endpoint(stb_portal_base(selected_provider.get("url", "")))` to `url = stb_endpoint(stb_portal_base(selected_provider.get("url", ""), provider_prefers_https(selected_provider, self.config_manager.prefer_https)))`.
   - Add `provider_prefers_https` to its `services.provider_api` import.
3. In `mixins/export_mixin.py`:
   - Every `stb_portal_base(selected_provider.get("url", "")...)` call (in `fetch_and_export_all_live_channels`, `fetch_and_export_all_series` and `_fetch_series_list_sync`) becomes `stb_portal_base(selected_provider.get("url", ""), provider_prefers_https(selected_provider, self.config_manager.prefer_https))`.
   - In `export_shown_channels` and `export_content_cached`, replace `base_url = provider.get("url", "")` with `base_url = stb_portal_base(provider.get("url", ""), provider_prefers_https(provider, self.config_manager.prefer_https))`.
   - Add `provider_prefers_https` to its `services.provider_api` import.
4. Verify that no unresolved site remains:
```bash
git grep -n 'get("prefer_https"' -- '*.py' ':!services/provider_api.py' ':!options.py'
uv run python - <<'EOF'
import ast, pathlib
bad = []
for path in sorted(pathlib.Path("mixins").glob("*.py")):
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "stb_portal_base":
            arg = node.args[1] if len(node.args) > 1 else None
            if not (isinstance(arg, ast.Call) and getattr(arg.func, "id", None) == "provider_prefers_https"):
                bad.append(f"{path}:{node.lineno}")
print("\n".join(bad) or "all stb_portal_base calls use provider_prefers_https")
EOF
```
Expected: the `git grep` prints nothing. The `options.py` sites are excluded because they edit a provider draft, not resolve a preference. The script prints `all stb_portal_base calls use provider_prefers_https`.

- [ ] **Step 5: Normalize the base inside `save_stb_content` (defence at the root)**

In `services/export.py`, add `from services.provider_api import stb_portal_base` and make the first line of `save_stb_content` body:
```python
    base_url = stb_portal_base(base_url)  # callers may pass a full portal URL like http://host/c/
```
`stb_portal_base` with `prefer_https=False` keeps the scheme, so a caller that already chose HTTPS is preserved.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run python -m unittest tests.test_provider_urls -v && uv run python -m unittest discover -s tests 2>&1 | tail -3`

Expected: all 7 new tests pass; the full suite reports `Ran 24 tests` and `OK`.

- [ ] **Step 7: Lint, format, commit**

```bash
uv run black services/provider_api.py services/export.py mixins/playback_mixin.py mixins/export_mixin.py mixins/content_loading_mixin.py tests/test_provider_urls.py
uv run isort services/provider_api.py services/export.py mixins/playback_mixin.py mixins/export_mixin.py mixins/content_loading_mixin.py tests/test_provider_urls.py
uv run flake8 services/provider_api.py services/export.py mixins/playback_mixin.py mixins/export_mixin.py mixins/content_loading_mixin.py tests/test_provider_urls.py
git add services/provider_api.py services/export.py mixins/playback_mixin.py mixins/export_mixin.py mixins/content_loading_mixin.py tests/test_provider_urls.py
git commit -m "fix: honour per-provider HTTPS and normalize STB export bases" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: STB profile request sends a plain `prehash`

Fixes spec §10 item 1: `"prehash": {prehash}` is a Python set, so `urlencode` sends `prehash=%7B%27…%27%7D`.

**Files:**
- Modify: `provider_manager.py` (`StbHandshake.run`, the `params` dict)
- Test: `tests/test_stb_handshake.py`

**Interfaces:** Consumes `StbHandshake(prefer_https=False, ssl_verify=True, token="").run(url, mac, ...) -> bool`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_stb_handshake.py`:
```python
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from provider_manager import StbHandshake

PREHASH = "2614ddf9829ba9d284f389d88e8c669d81f6a5c2"


class _Response:
    status_code = 200

    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class StbHandshakeTests(unittest.TestCase):
    def test_profile_request_sends_plain_prehash(self):
        calls = []

        def fake_get(url, **kwargs):
            calls.append(url)
            if "action=handshake" in url:
                return _Response({"js": {"token": "TOKEN"}})
            return _Response({"js": {"id": 1, "name": "Fixture"}})

        with patch("provider_manager.requests.get", side_effect=fake_get):
            self.assertTrue(StbHandshake().run("http://portal.test", "00:1A:79:00:00:01"))

        profile = next(url for url in calls if "action=get_profile" in url)
        self.assertEqual(parse_qs(urlsplit(profile).query)["prehash"], [PREHASH])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run python -m unittest tests.test_stb_handshake -v`
Expected: FAIL. The actual value is `["{'2614ddf9829ba9d284f389d88e8c669d81f6a5c2'}"]`.

- [ ] **Step 3: Fix**

In `provider_manager.py`, in the `params` dict inside `StbHandshake.run`, change `"prehash": {prehash},` to `"prehash": prehash,`.

- [ ] **Step 4: Run to verify it passes, then run the suite**

Run: `uv run python -m unittest tests.test_stb_handshake -v && uv run python -m unittest discover -s tests 2>&1 | tail -3`
Expected: PASS; `Ran 25 tests` `OK`.

- [ ] **Step 5: Commit**

```bash
uv run black provider_manager.py tests/test_stb_handshake.py && uv run isort tests/test_stb_handshake.py
git add provider_manager.py tests/test_stb_handshake.py
git commit -m "fix: send the STB profile prehash as a string, not a set" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Image cache accounting and eviction

Fixes spec §10 item 2:
- New downloads never add to `current_cache_size`, so nothing is ever evicted.
- Eviction guesses the extension from a `"qicon"` key that only exists for entries found at start-up, and crashes on `null` entries.
- `_load_index` also builds QPixmap/QIcon objects for every unindexed file, which is GUI work nobody reads.

**Files:**
- Modify: `image_manager.py` (`_touch_cache_entry`, `remove_icon_from_cache`, `_load_index`, `save_index`, `_manage_cache_size`; add `_entry_paths`; drop the unused `QIcon`/`QPixmap` imports)
- Test: `tests/test_image_cache.py`

**Interfaces:**
- Consumes the public API, unchanged:
  - `ImageManager(config_manager, max_cache_size=...)`
  - `async cache_image_from_url(session, url, iconified)` and `async cache_image_from_base64(image_str, iconified)`, both `-> str | None`
  - `clear_cache()`, `remove_icon_from_cache(url)`, `save_index()`
- Produces: cache entries of the form `{"size": int, "ext": "png" | "jpg", "last_access": str}`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_image_cache.py`:
```python
import asyncio
import base64
import json
import os
import tempfile
from types import SimpleNamespace
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from image_manager import ImageManager

# The pre-fix ImageManager builds QPixmaps while indexing; keep that from aborting the run.
APP = QApplication.instance() or QApplication([])


def _data_url(payload: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(payload).decode("ascii")


class ImageCacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.config = SimpleNamespace(get_config_dir=lambda: self.directory.name)
        self.cache_dir = os.path.join(self.directory.name, "cache", "image")

    def tearDown(self):
        self.directory.cleanup()

    def _store(self, manager, payload, iconified):
        return asyncio.run(manager.cache_image_from_base64(_data_url(payload), iconified))

    def test_new_images_are_counted(self):
        manager = ImageManager(self.config, max_cache_size=10_000)
        self._store(manager, b"a" * 100, True)
        self._store(manager, b"b" * 200, False)
        self.assertEqual(manager.current_cache_size, 300)

    def test_touching_an_existing_image_does_not_double_count(self):
        manager = ImageManager(self.config, max_cache_size=10_000)
        self._store(manager, b"a" * 100, True)
        self._store(manager, b"a" * 100, True)
        self.assertEqual(manager.current_cache_size, 100)

    def test_oldest_images_are_evicted_with_their_real_extension(self):
        manager = ImageManager(self.config, max_cache_size=250)
        first = self._store(manager, b"a" * 100, False)  # jpg
        second = self._store(manager, b"b" * 100, True)  # png
        third = self._store(manager, b"c" * 100, True)
        self.assertFalse(os.path.exists(first))
        self.assertTrue(os.path.exists(second))
        self.assertTrue(os.path.exists(third))
        self.assertEqual(manager.current_cache_size, 200)

    def test_a_single_oversized_image_is_kept(self):
        manager = ImageManager(self.config, max_cache_size=50)
        path = self._store(manager, b"a" * 100, True)
        self.assertTrue(os.path.exists(path))
        self.assertEqual(len(manager.cache), 1)

    def test_legacy_index_with_null_and_extensionless_entries(self):
        os.makedirs(self.cache_dir, exist_ok=True)
        legacy_hash = "0" * 64
        with open(os.path.join(self.cache_dir, f"{legacy_hash}.jpg"), "wb") as f:
            f.write(b"x" * 120)
        index = {
            "f" * 64: None,
            legacy_hash: {"size": 120, "last_access": "2026-01-01 00:00:00"},
        }
        with open(os.path.join(self.cache_dir, "index.json"), "w", encoding="utf-8") as f:
            json.dump(index, f)

        manager = ImageManager(self.config, max_cache_size=150)
        self.assertEqual(manager.current_cache_size, 120)
        newest = self._store(manager, b"n" * 100, True)

        self.assertFalse(os.path.exists(os.path.join(self.cache_dir, f"{legacy_hash}.jpg")))
        self.assertTrue(os.path.exists(newest))
        self.assertEqual(manager.current_cache_size, 100)

    def test_unindexed_files_are_indexed_without_gui_objects(self):
        os.makedirs(self.cache_dir, exist_ok=True)
        with open(os.path.join(self.cache_dir, f"{'1' * 64}.png"), "wb") as f:
            f.write(b"p" * 10)
        manager = ImageManager(self.config)
        entry = manager.cache["1" * 64]
        self.assertEqual(entry["ext"], "png")
        self.assertEqual(entry["size"], 10)
        self.assertNotIn("qicon", entry)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run python -m unittest tests.test_image_cache -v`

Expected: every test except `test_a_single_oversized_image_is_kept` fails:
- sizes stay 0;
- nothing is evicted;
- `KeyError: 'ext'`.

- [ ] **Step 3: Implement**

In `image_manager.py`:
- Remove `from PySide6.QtGui import QIcon, QPixmap`.
- Replace `_touch_cache_entry`, `remove_icon_from_cache`, `_load_index`, `save_index` and `_manage_cache_size` with the following, and add `_entry_paths`:
```python
    def _touch_cache_entry(self, key_hash, cache_path):
        # Update cache metadata without creating GUI objects
        size = os.path.getsize(cache_path)
        entry = self.cache.get(key_hash) or {}
        self.current_cache_size += size - entry.get("size", 0)
        entry["size"] = size
        entry["ext"] = os.path.splitext(cache_path)[1].lstrip(".")
        entry["last_access"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.cache[key_hash] = entry
        # maintain LRU order
        self.cache.move_to_end(key_hash)

    def _entry_paths(self, key_hash, entry):
        """Files an entry may own; entries from older indexes did not record their extension."""
        exts = [entry["ext"]] if entry and entry.get("ext") else ["png", "jpg"]
        return [os.path.join(self.cache_dir, f"{key_hash}.{ext}") for ext in exts]

    def remove_icon_from_cache(self, url):
        url_hash = self._hash_string(url + "png")
        entry = self.cache.pop(url_hash, None)
        self.current_cache_size -= (entry or {}).get("size", 0)
        path = os.path.join(self.cache_dir, f"{url_hash}.png")
        if os.path.exists(path):
            os.remove(path)

    def _load_index(self):
        self.cache.clear()
        if os.path.exists(self.index_file):
            with open(self.index_file, "r", encoding="utf-8") as f:
                try:
                    data = json.loads(f.read())
                    self.cache = OrderedDict(data) if data else OrderedDict()
                except (json.JSONDecodeError, IOError) as e:
                    logger.warning(f"Error loading index file: {e}")

        # Index files that exist on disk but not in the index (metadata only)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for filename in os.listdir(self.cache_dir):
            image_hash, ext = os.path.splitext(filename)
            if ext in (".png", ".jpg") and not self.cache.get(image_hash):
                self.cache[image_hash] = {
                    "size": os.path.getsize(os.path.join(self.cache_dir, filename)),
                    "ext": ext.lstrip("."),
                    "last_access": now,
                }

        self.current_cache_size = sum(entry.get("size", 0) for entry in self.cache.values() if entry)

    def save_index(self):
        with open(self.index_file, "w", encoding="utf-8") as f:
            f.write(json.dumps(dict(self.cache), option=json.OPT_INDENT_2).decode("utf-8"))

    def _manage_cache_size(self):
        # Evict least recently used entries; always keep the entry just touched.
        while self.current_cache_size > self.max_cache_size and len(self.cache) > 1:
            oldest_hash, oldest_data = self.cache.popitem(last=False)
            self.current_cache_size -= (oldest_data or {}).get("size", 0)
            for path in self._entry_paths(oldest_hash, oldest_data):
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
```
`remove_icon_from_cache` now subtracts the size it recorded, which is the same accounting `_manage_cache_size` uses, rather than re-reading the file size.

- [ ] **Step 4: Run to verify they pass, then run the suite**

Run: `uv run python -m unittest tests.test_image_cache -v && uv run python -m unittest discover -s tests 2>&1 | tail -3`
Expected: 6 PASS; `Ran 31 tests` `OK`. The `test_catalog_refresh` logo tests must also stay green.

- [ ] **Step 5: Lint and commit**

```bash
uv run black image_manager.py tests/test_image_cache.py && uv run isort image_manager.py tests/test_image_cache.py && uv run flake8 image_manager.py tests/test_image_cache.py
git add image_manager.py tests/test_image_cache.py
git commit -m "fix: count new cache images and evict the right files" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: EPG programmes that end after midnight

Fixes spec §10 item 3: `_index_programs` computes the next-day `stop_time` and then discards it. The stored programme keeps the raw `@stop`, which is earlier than `@start`. The old comparison was also a string comparison, which is wrong when start and stop carry different UTC offsets.

**Files:**
- Modify: `epg_manager.py` (add module function `normalize_programme_stop`; use it in `_index_programs`)
- Test: `tests/test_epg_times.py`

**Interfaces:**
- Produces: `normalize_programme_stop(start: str, stop: str) -> str` in `epg_manager.py`. It accepts XMLTV `YYYYmmddHHMMSS ±zzzz` or `YYYYmmddHHMMSS`, and returns `stop` in the same format, moved forward one day when it is earlier than `start`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_epg_times.py`:
```python
import os
import tempfile
from types import SimpleNamespace
import unittest

from epg_manager import EpgManager, normalize_programme_stop

XMLTV = """<?xml version="1.0" encoding="UTF-8"?>
<tv>
  <channel id="news.test"><display-name>News</display-name></channel>
  <programme channel="news.test" start="20261015233000 +0200" stop="20261015003000 +0200">
    <title>Late News</title>
  </programme>
</tv>
"""


class NormalizeProgrammeStopTests(unittest.TestCase):
    def test_overnight_stop_moves_to_next_day(self):
        self.assertEqual(
            normalize_programme_stop("20261015233000 +0200", "20261015003000 +0200"),
            "20261016003000 +0200",
        )

    def test_regular_programme_is_unchanged(self):
        self.assertEqual(
            normalize_programme_stop("20261015200000 +0200", "20261015210000 +0200"),
            "20261015210000 +0200",
        )

    def test_different_offsets_are_compared_as_instants(self):
        # 23:30 +0200 == 21:30 UTC; 22:15 +0000 is later, so not overnight.
        self.assertEqual(
            normalize_programme_stop("20261015233000 +0200", "20261015221500 +0000"),
            "20261015221500 +0000",
        )

    def test_times_without_timezone(self):
        self.assertEqual(
            normalize_programme_stop("20261015233000", "20261015003000"), "20261016003000"
        )

    def test_unparseable_values_are_returned_unchanged(self):
        self.assertEqual(normalize_programme_stop("garbage", "20261015003000"), "20261015003000")


class IndexProgrammesTests(unittest.TestCase):
    def test_indexed_programme_carries_corrected_stop(self):
        manager = EpgManager.__new__(EpgManager)
        manager.config_manager = SimpleNamespace(
            xmltv_channel_map=SimpleNamespace(get_keys=lambda key: None)
        )
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "guide.xml")
            with open(path, "w", encoding="utf-8") as f:
                f.write(XMLTV)
            programs = manager._index_programs(path)
        [programme] = programs["news.test"]
        self.assertEqual(programme["@stop"], "20261016003000 +0200")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run python -m unittest tests.test_epg_times -v`
Expected: ImportError for `normalize_programme_stop`.

- [ ] **Step 3: Implement**

In `epg_manager.py`, add below `_xtream_cache_key`:
```python
_XMLTV_FORMATS = ("%Y%m%d%H%M%S %z", "%Y%m%d%H%M%S")


def normalize_programme_stop(start: str, stop: str) -> str:
    """Move a stop time that precedes its start (programme ends after midnight) to the next day.

    Accepts XMLTV times with or without a UTC offset; unparseable values are returned unchanged.
    """
    for fmt in _XMLTV_FORMATS:
        try:
            start_dt = datetime.strptime(start, fmt)
            stop_dt = datetime.strptime(stop, fmt)
        except ValueError:
            continue
        if stop_dt < start_dt:
            return (stop_dt + timedelta(days=1)).strftime(fmt)
        return stop
    return stop
```
In `_index_programs`, replace the block from `# Fix stop_time < start_time…` through `tmp_lists.setdefault(...)` with:
```python
            program_data = xml_to_dict(programme)["programme"]
            program_data["@stop"] = normalize_programme_stop(start_time, stop_time)
            tmp_lists.setdefault(channel_id, []).append(program_data)
```
Check that `datetime` and `timedelta` are still imported at the top. They are, from the same module's existing code. Run `git grep -n "^from datetime" epg_manager.py` to confirm.

- [ ] **Step 4: Run to verify they pass, then run the suite**

Run: `uv run python -m unittest tests.test_epg_times -v && uv run python -m unittest discover -s tests 2>&1 | tail -3`
Expected: 6 PASS; `Ran 37 tests` `OK`.

- [ ] **Step 5: Lint and commit**

```bash
uv run black epg_manager.py tests/test_epg_times.py && uv run isort epg_manager.py tests/test_epg_times.py && uv run flake8 epg_manager.py tests/test_epg_times.py
git add epg_manager.py tests/test_epg_times.py
git commit -m "fix: keep the next-day stop time for overnight EPG programmes" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: STB next-season autoplay

Fixes spec §10 item 6:
- STB seasons list their episode numbers under `"series"`, not `"episodes"`, so `_get_first_episode_of_next_season` always returns `None` for STB.
- When a first episode is found, the STB branch passes it on raw, without normalizing it.

**Files:**
- Modify: `mixins/playback_mixin.py` (`_get_first_episode_of_next_season`, `_build_episode_list_from_season`; add `_season_episode_entries`)
- Test: `tests/test_autoplay_next_season.py`

**Interfaces:**
- Consumes:
  - `_build_stb_episode_item(episode_num: int, season_data: dict) -> dict`, which returns `current_series` copied with `number`, `ename`, `cmd` (the season's) and `series` set;
  - `_build_xtream_episode_item(episode_data: dict) -> dict`.
- Produces: `_season_episode_entries(season: dict) -> list` (static method).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_autoplay_next_season.py`:
```python
import os
from types import SimpleNamespace
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mixins.playback_mixin import PlaybackMixin


def _host(provider_type, seasons, current_season, content=None):
    host = PlaybackMixin.__new__(PlaybackMixin)
    host.provider_manager = SimpleNamespace(
        current_provider={"type": provider_type, "username": "u", "password": "p", "url": "http://x"},
        current_provider_content=content or {},
    )
    host.current_series = {"id": "S1", "name": "Show"}
    host.current_season = current_season
    host._current_seasons_list = seasons
    host._current_episode_list = []
    host._current_episode_index = -1
    return host


class NextSeasonAutoplayTests(unittest.TestCase):
    def test_stb_next_season_starts_at_its_first_episode(self):
        s1 = {"id": "1", "series": [1, 2], "cmd": "CMD1"}
        s2 = {"id": "2", "series": [1, 2, 3], "cmd": "CMD2"}
        host = _host("STB", [s1, s2], s1)

        episode = host._get_first_episode_of_next_season()

        self.assertIsNotNone(episode)
        self.assertEqual(episode["series"], 1)
        self.assertEqual(episode["cmd"], "CMD2")
        self.assertEqual(episode["number"], "1")
        self.assertIs(host.current_season, s2)
        self.assertEqual(len(host._current_episode_list), 3)
        self.assertEqual(host._current_episode_index, 0)

    def test_stb_episode_numbers_as_strings(self):
        s1 = {"id": "1", "series": ["1"], "cmd": "CMD1"}
        s2 = {"id": "2", "series": ["4", "5"], "cmd": "CMD2"}
        host = _host("STB", [s1, s2], s1)

        episode = host._get_first_episode_of_next_season()

        self.assertEqual(episode["series"], 4)
        self.assertEqual(len(host._current_episode_list), 2)

    def test_last_season_has_no_next_episode(self):
        s1 = {"id": "1", "series": [1], "cmd": "CMD1"}
        host = _host("STB", [s1], s1)
        self.assertIsNone(host._get_first_episode_of_next_season())

    def test_xtream_next_season_still_works(self):
        s1 = {"id": "1", "episodes": [{"id": "e0", "episode_num": 1}]}
        s2 = {
            "id": "2",
            "episodes": [{"id": "e1", "episode_num": 1, "title": "Pilot", "container_extension": "mkv"}],
        }
        content = {"series": {"resolved_base": "http://x"}}
        host = _host("XTREAM", [s1, s2], s1, content)

        episode = host._get_first_episode_of_next_season()

        self.assertEqual(episode["cmd"], "http://x/series/u/p/e1.mkv")
        self.assertEqual(episode["name"], "Pilot")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run python -m unittest tests.test_autoplay_next_season -v`
Expected: the two STB tests FAIL (they get `None`); the other two PASS.

- [ ] **Step 3: Implement**

In `mixins/playback_mixin.py`, add above `_get_next_episode`:
```python
    @staticmethod
    def _season_episode_entries(season: Dict[str, Any]) -> List[Any]:
        """Xtream seasons list episode dicts under 'episodes'; STB seasons list numbers under 'series'."""
        return season.get("episodes") or season.get("series") or []
```
In `_get_first_episode_of_next_season`, replace everything from `# Load next season's episodes` to the end of the method with:
```python
        next_season = self._current_seasons_list[next_season_idx]
        episode_list = self._build_episode_list_from_season(next_season)
        if not episode_list:
            return None

        # Update playback context to the new season
        self.current_season = next_season
        self._current_episode_list = episode_list
        self._current_episode_index = 0
        return episode_list[0]
```
In `_build_episode_list_from_season`:
- Change `episodes = season_data.get("episodes", [])` to `episodes = self._season_episode_entries(season_data)`.
- Extend the STB branch so that digit strings work:
```python
                if isinstance(ep, int):
                    episode_list.append(self._build_stb_episode_item(ep, season_data))
                elif isinstance(ep, str) and ep.strip().isdigit():
                    episode_list.append(self._build_stb_episode_item(int(ep), season_data))
                elif isinstance(ep, dict):
                    ep_num = ep.get("episode_num") or ep.get("series") or 1
                    episode_list.append(self._build_stb_episode_item(int(ep_num), season_data))
```

- [ ] **Step 4: Run to verify they pass, then run the suite**

Run: `uv run python -m unittest tests.test_autoplay_next_season -v && uv run python -m unittest discover -s tests 2>&1 | tail -3`
Expected: 4 PASS; `Ran 41 tests` `OK`.

- [ ] **Step 5: Lint and commit**

```bash
uv run black mixins/playback_mixin.py tests/test_autoplay_next_season.py && uv run isort mixins/playback_mixin.py tests/test_autoplay_next_season.py && uv run flake8 mixins/playback_mixin.py tests/test_autoplay_next_season.py
git add mixins/playback_mixin.py tests/test_autoplay_next_season.py
git commit -m "fix: find STB next-season episodes for autoplay" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: M3U line endings

Fixes spec §10 item 7: `parse_m3u` splits on `\n` only, so CRLF playlists leave `\r` at the end of channel names and `#EXTVLCOPT` user agents.

**Files:**
- Modify: `services/m3u.py` (`parse_m3u`)
- Test: `tests/test_m3u_parse.py`

**Interfaces:** Consumes `parse_m3u(data: str, categorize: bool = False)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_m3u_parse.py`:
```python
import unittest

from services.m3u import parse_m3u

ENTRY = [
    "#EXTM3U",
    '#EXTINF:-1 tvg-id="one.test" group-title="News",Channel One',
    "#EXTVLCOPT:http-user-agent=UA/1",
    "http://stream.test/1.ts",
]


class ParseM3uLineEndingTests(unittest.TestCase):
    def _assert_clean(self, data):
        [item] = parse_m3u(data)
        self.assertEqual(item["name"], "Channel One")
        self.assertEqual(item["user_agent"], "UA/1")
        self.assertEqual(item["cmd"], "http://stream.test/1.ts")
        self.assertEqual(item["xmltv_id"], "one.test")

    def test_lf(self):
        self._assert_clean("\n".join(ENTRY) + "\n")

    def test_crlf(self):
        self._assert_clean("\r\n".join(ENTRY) + "\r\n")

    def test_classic_mac_cr(self):
        self._assert_clean("\r".join(ENTRY) + "\r")

    def test_utf8_bom(self):
        self._assert_clean("﻿" + "\r\n".join(ENTRY))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run python -m unittest tests.test_m3u_parse -v`
Expected:
- `test_crlf` and `test_utf8_bom` FAIL (`'Channel One\r'`);
- `test_classic_mac_cr` FAILS (no items);
- `test_lf` PASSES.

- [ ] **Step 3: Implement**

In `services/m3u.py`, `parse_m3u`, change `lines = data.split("\n")` to:
```python
    lines = data.lstrip("﻿").splitlines()
```

- [ ] **Step 4: Run to verify they pass, then run the suite**

Run: `uv run python -m unittest tests.test_m3u_parse -v && uv run python -m unittest discover -s tests 2>&1 | tail -3`
Expected: 4 PASS; `Ran 45 tests` `OK`.

- [ ] **Step 5: Commit**

```bash
uv run black services/m3u.py tests/test_m3u_parse.py && uv run isort tests/test_m3u_parse.py && uv run flake8 services/m3u.py tests/test_m3u_parse.py
git add services/m3u.py tests/test_m3u_parse.py
git commit -m "fix: parse M3U playlists with CRLF, CR and BOM cleanly" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Design tokens

Spec §5.6. These are the single source of the TV shell's colours and radii, used from Phase 1.

**Files:**
- Create: `ui/__init__.py` (empty docstring module), `ui/theme.py`
- Test: `tests/test_theme.py`

**Interfaces:**
- Produces:
  - `ui.theme.PALETTE` (a frozen `Palette` dataclass with the fields `canvas`, `panel`, `panel_raised`, `text`, `text_muted_alpha`, `accent`, `brand`, `live`, `ok`, `warn`);
  - `RADIUS_CARD = 10`, `RADIUS_PANEL = 14`, `FOCUS_FILL_ALPHA = 0.22`;
  - `qcolor(value: str, alpha: float | None = None) -> QColor`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_theme.py`:
```python
import unittest

from ui.theme import FOCUS_FILL_ALPHA, PALETTE, RADIUS_CARD, RADIUS_PANEL, qcolor


class ThemeTests(unittest.TestCase):
    def test_spec_values(self):
        self.assertEqual(PALETTE.canvas, "#0b0f14")
        self.assertEqual(PALETTE.panel, "rgba(8,11,15,0.86)")
        self.assertEqual(PALETTE.panel_raised, "#0e1217")
        self.assertEqual(PALETTE.text, "#eef2f6")
        self.assertEqual(PALETTE.accent, "#e08a5f")
        self.assertEqual(PALETTE.brand, "#C96B43")
        self.assertEqual(PALETTE.live, "#d6453a")
        self.assertEqual(PALETTE.ok, "#5fbf7a")
        self.assertEqual(PALETTE.warn, "#e0b24a")
        self.assertAlmostEqual(PALETTE.text_muted_alpha, 0.62)
        self.assertEqual((RADIUS_CARD, RADIUS_PANEL), (10, 14))
        self.assertAlmostEqual(FOCUS_FILL_ALPHA, 0.22)

    def test_hex(self):
        color = qcolor("#e08a5f")
        self.assertEqual(color.name(), "#e08a5f")
        self.assertEqual(color.alpha(), 255)

    def test_rgba(self):
        color = qcolor(PALETTE.panel)
        self.assertEqual((color.red(), color.green(), color.blue()), (8, 11, 15))
        self.assertEqual(color.alpha(), round(0.86 * 255))

    def test_alpha_override(self):
        self.assertAlmostEqual(qcolor(PALETTE.text, 0.62).alphaF(), 0.62, places=2)

    def test_invalid(self):
        with self.assertRaises(ValueError):
            qcolor("not-a-colour")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run python -m unittest tests.test_theme -v`
Expected: `ModuleNotFoundError: No module named 'ui'`.

- [ ] **Step 3: Implement**

Create `ui/__init__.py`:
```python
"""TV shell user interface (spec docs/superpowers/specs/2026-10-15-tv-on-vlc-design.md §5)."""
```
Create `ui/theme.py`:
```python
"""Design tokens for the TV shell (spec §5.6). Colours are CSS-style strings; qcolor() converts."""

from dataclasses import dataclass
import re

from PySide6.QtGui import QColor

_RGBA = re.compile(r"rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*([0-9.]+)\s*\)")


@dataclass(frozen=True)
class Palette:
    canvas: str = "#0b0f14"
    panel: str = "rgba(8,11,15,0.86)"
    panel_raised: str = "#0e1217"
    text: str = "#eef2f6"
    text_muted_alpha: float = 0.62
    accent: str = "#e08a5f"
    brand: str = "#C96B43"
    live: str = "#d6453a"
    ok: str = "#5fbf7a"
    warn: str = "#e0b24a"


PALETTE = Palette()
RADIUS_CARD = 10
RADIUS_PANEL = 14
FOCUS_FILL_ALPHA = 0.22


def qcolor(value: str, alpha: float | None = None) -> QColor:
    """Build a QColor from '#rrggbb' or 'rgba(r,g,b,a)'; `alpha` (0–1) overrides the alpha."""
    match = _RGBA.fullmatch(value.strip())
    if match:
        red, green, blue = (int(part) for part in match.groups()[:3])
        color = QColor(red, green, blue)
        color.setAlpha(round(float(match.group(4)) * 255))
    else:
        color = QColor(value)
        if not value.startswith("#") or not color.isValid():
            raise ValueError(f"Unsupported colour: {value!r}")
    if alpha is not None:
        color.setAlphaF(alpha)
    return color
```

- [ ] **Step 4: Run to verify they pass, then run the suite**

Run: `uv run python -m unittest tests.test_theme -v && uv run python -m unittest discover -s tests 2>&1 | tail -3`
Expected: 5 PASS; `Ran 50 tests` `OK`.

- [ ] **Step 5: Lint, type-check, commit**

```bash
uv run black ui tests/test_theme.py && uv run isort ui tests/test_theme.py && uv run flake8 ui tests/test_theme.py && uv run mypy ui/theme.py
git add ui/__init__.py ui/theme.py tests/test_theme.py
git commit -m "feat: add TV shell design tokens" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```
Expected: mypy prints `Success: no issues found in 1 source file`.

---

### Task 9: Run the tests in CI on all four platforms; release only from `main`

The spec (§11) says the CI workflow gains `unittest` on every platform. Today the workflow runs only on pushes to `main` and publishes a release from every run. Pushes to `tv/vlc` must build and test without releasing.

**Files:**
- Modify: `.github/workflows/main.yml`

**Interfaces:** none (CI only).

- [ ] **Step 1: Edit the triggers**

Replace the top `on:` block with:
```yaml
on:
  push:
    branches:
      - main
      - tv/vlc
  workflow_dispatch:
```

- [ ] **Step 2: Add a test step to each of the four build jobs**

Add it immediately after each job's `- name: Sync dependencies` step (in `build-linux`, `build-windows`, `build-macos-universal` and `build-macos-intel`):
```yaml
      - name: Run tests
        run: uv run python -m unittest discover -s tests -v
        env:
          QT_QPA_PLATFORM: offscreen
```

- [ ] **Step 3: Restrict the release job to `main`**

In the `release:` job, add `if: github.ref == 'refs/heads/main'` between `needs:` and `runs-on:`:
```yaml
  release:
    needs: [build-linux, build-windows, build-macos-universal, build-macos-intel]
    if: github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
```

- [ ] **Step 4: Validate the YAML and its structure**

```bash
uv run --with pyyaml python - <<'EOF'
import yaml
wf = yaml.safe_load(open(".github/workflows/main.yml"))
on = wf[True] if True in wf else wf["on"]
assert on["push"]["branches"] == ["main", "tv/vlc"], on
assert "workflow_dispatch" in on
for job in ("build-linux", "build-windows", "build-macos-universal", "build-macos-intel"):
    names = [step.get("name") for step in wf["jobs"][job]["steps"]]
    assert names.index("Run tests") == names.index("Sync dependencies") + 1, (job, names)
assert wf["jobs"]["release"]["if"] == "github.ref == 'refs/heads/main'"
print("workflow ok")
EOF
```
Expected: `workflow ok`. (PyYAML parses `on:` as the boolean key `True`; the script handles both forms.)

- [ ] **Step 5: Commit (do not push)**

```bash
git add .github/workflows/main.yml
git commit -m "ci: run tests on all platforms; release only from main" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```
Pushing `tv/vlc` to trigger CI needs the owner's go-ahead. Ask at the end of Phase 0.

---

### Task 10: Record the new plan in AGENTS.md

**Files:**
- Modify: `AGENTS.md`

**Interfaces:** none.

- [ ] **Step 1: Replace the `Current Work Plan (Living TODO)` heading line and add the TV plan above item 1)**

Insert this block directly under the line `Current Work Plan (Living TODO)`:
```
- TV redesign on VLC (branch `tv/vlc`, spec `docs/superpowers/specs/2026-10-15-tv-on-vlc-design.md`). No native builds: VLC from official installers, pure-Python live pipeline. Each phase has a plan in `docs/superpowers/plans/`.
  - [x] Phase 0 Foundation: tidy-up landed; fixed STB prehash, image cache eviction, overnight EPG stop, per-provider HTTPS, STB export bases, STB next-season autoplay, M3U line endings; design tokens (`ui/theme.py`); CI runs tests on all platforms and releases only from `main`
  - [ ] Phase 1 TV shell (Live TV): single window, native VLC surface + glued overlay host, zap banner/column/drawer, NavModel/ZapContext, transport
  - [ ] Phase 2 Live pipeline: TS segmenter, RAM/disk rings, loopback server, timeline, stay-live, connection budget, time-shift UI, CI playback smoke
  - [ ] Phase 3 Library + guide; remove old windows; release 2.0.0
  - [ ] Phase 4 PiP + swap
```

- [ ] **Step 2: Add a Recent Changes entry**

Insert as the first bullet under `Recent Changes (for context)`:
```
- Phase 0 (`tv/vlc`): Restarted from v1.13.8 after retiring the MPV line. Landed a behaviour-preserving service-layer tidy-up and fixed seven bugs it surfaced, each with a regression test (STB profile prehash sent as a set; image cache never evicting and deleting wrong files; overnight EPG stop discarded; global instead of per-provider HTTPS; `…/c//play` STB export URLs; STB next-season autoplay; CRLF/CR/BOM M3U names). Added design tokens and cross-platform CI test runs; releases now publish only from `main`.
```

- [ ] **Step 3: Verify the suite one final time and commit**

```bash
uv run python -m unittest discover -s tests 2>&1 | tail -3
git add AGENTS.md
git commit -m "docs: record the TV redesign plan in AGENTS.md" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
git log --oneline 16459ee..HEAD
```
Expected: `Ran 50 tests` `OK`; 12 commits after `16459ee`: the spec, the plan, and one commit for each of Tasks 1–10.

---

## Notes for executors

- **Spec §10 item 8** (the synchronous `create_link` in the context menu, `mixins/display_mixin.py`) is deliberately **not** fixed here. That module is replaced in Phase 3, and the Phase 1/3 plans must not carry the pattern over.
- **Test counts:** the expected counts assume each task lands in order. If a count differs, check whether an earlier task added or skipped tests before assuming a failure.
