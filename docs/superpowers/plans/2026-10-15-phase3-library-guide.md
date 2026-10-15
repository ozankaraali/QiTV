# Phase 3 Library, Guide and 2.0 Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish QiTV 2.0's TV-first UI and retire the v1.13.8 windows. This phase adds:
- the full-screen Netflix-style **Library** for Movies and Series: genre column, wrapping poster grid, inline detail strip, A–Z rail, and Seasons → Episodes inside the screen;
- the **EPG guide** overlay (`G`);
- the **Next-episode card** and next-movie autoplay;
- **favorites** and context actions;
- **Search everything**, across all providers;
- **exports**, a welcome card, and a complete menu bar.

It then deletes the old UI, sets the version to **2.0.0**, and refreshes the README with screenshots of synthetic demo content.

**Architecture:**
- Pure services hold the logic and are tested headless:
  - `services/library.py` (`LibraryModel`);
  - `services/search.py`;
  - `services/next_episode.py`;
  - `services/exporter.py`;
  - `services/text_fold.py` (from Phase 1B).
- Widgets are dumb painters with small APIs:
  - `ui/library/*`;
  - `ui/overlays/guide.py`;
  - `ui/overlays/next_card.py`;
  - `ui/images.py` (`PosterCache`).
- `ui/library_controller.py` holds the library's behaviour, the way `ui/tv_controller.py` holds the player's.
- `TVWindow` hosts two screens in a `QStackedWidget`: **Player** (the `VideoSurface` with its glued `OverlayHost`) and **Library**, which is ordinary Qt painting with no video behind it (spec §5.4).

**Tech Stack:** Python 3.14, PySide6 6.11.2 (QtWidgets), python-vlc / libVLC 3, `unittest`, `uv`. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-15-tv-on-vlc-design.md`:
- §5.1 (guide, cards);
- §5.2 (Favorites, Continue watching, Search everything);
- §5.3 (keys `G`, mouse back/forward);
- §5.4 (Library);
- §5.5 (VOD Next for episodes);
- §8 (welcome screen);
- §12 (Phase 3: release);
- §14 (version 2.0.0).

Approved mockup: `docs/superpowers/specs/assets/2026-10-15-tv-on-vlc/05-library.png`, **option 1** (genre column on the left).

Earlier plans give the consumed interfaces: Phase 1A (`services/*`), Phase 1B (`ui/*`, `TVController`, `TVWindow`) and Phase 2B (`LiveSession`). **Read the actual code** of each consumed module, because review rounds changed several contracts after the plans were written.

## Global Constraints

- Python `>=3.14,<3.15`. Run everything through `uv run …` from `/Users/ozankaraali/QiTV` on branch `tv/vlc`.
- No native or compiled dependencies and no new runtime dependencies.
- Never block the GUI thread on network or heavy disk work. All provider requests and cache reads go through `services/jobs.JobRunner` workers (`on_finished(context, payload)` / `on_error(context, message)` on the GUI thread) or the existing `CatalogService` requests. Never build QPixmap/QIcon off the GUI thread.
- **Text matching** for filters, search, genre lists and the A–Z index uses `services.text_fold.fold` on both sides (casefold, NFKD, combining marks dropped, `ı`→`i`).
- **Units:** in the Library, 1 u = 1% of the Library screen's width. In overlays, 1 u = 1% of the overlay host's width (as in Phase 1B).
- Colours come only from `ui.theme` (`PALETTE`, `qcolor`, `RADIUS_CARD`, `RADIUS_PANEL`, `FOCUS_FILL_ALPHA`), and painting helpers from `ui/overlays/painting.py` (`Units`, `draw_*`).
- Focus style everywhere: an accent ring at 1.5 px logical plus a 22% accent fill (`FOCUS_FILL_ALPHA`).
- **Keys** are resolved in `ui/keymap.py`. Widgets never take keyboard focus for navigation: `TVWindow` routes keys to the active controller (player or library). Text entry (genre filter, search) receives text through the controller as well.
- Lists that can hold thousands of entries are virtualized: model/view or painting only the visible rows. There are no per-row or per-poster widgets.
- Logging uses `logging.getLogger(__name__)`; no `print`, and no silent `except: pass`.
- Tests use `unittest` with `QT_QPA_PLATFORM=offscreen`. Widgets created in tests are closed with `addCleanup`. Lint: `uv run flake8 --select=E9,F63,F7,F82 <files>`. Format with black/isort (line length 100). New modules pass `uv run mypy <module>`.
- Commits end with these two lines; stage files explicitly and never use `git add -A`:
  ```
  Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_011UFrnH8jUNCzkFNLKJaoRD
  ```
- **Security:** tests and demo content use only local fixtures and fictional names. Real provider URLs, credentials and broadcast footage never enter the repo, logs, screenshots or CI.

## Rulings carried from the controller (binding for this plan)

- **R1 (Continue for series):** "Continue" in Series lists the **episodes** that have a resume point (from watch history), because history stores episode dicts, not their series. Enter resumes that episode directly.
- **R2 (New):** "✦ New" lists the 48 most recently added titles among the *loaded* items. Its `added` field is a unix time or `YYYY-MM-DD[ HH:MM:SS]`. The group is hidden when no loaded item has an `added` value. STB categories load on demand, so "New" grows as genres are opened.
- **R3 (Complete export):** "Complete series (fetch all)" stays STB-only, as in v1.13.8. Xtream and M3U users export cached content.
- **R4 (Guide window):** the guide starts at the current half-hour and spans 2.5 h, scrolling forward up to 24 h. Past programmes are not shown, because `programmes_for_channel` drops ended programmes.
- **R5 (Library and live):** opening the Library stops live playback (spec §5.4). *Live TV* (tab, `Esc` at the Library's top level, or the View menu) returns to the Player and re-tunes `config.last_live_channel`.

## Review Focus

1. **Huge catalogs:** a genre with 20,000 titles and a Search across three providers with 200,000 cached items. The grid must stay responsive (painting only the visible rows). Search runs in a worker with generation guards, so stale results never overwrite newer ones (Tasks 3, 5 and 10).
2. **Posters that are missing, broken, slow or huge:**
   - the grid shows a generated title card at once and swaps in the poster when it arrives;
   - a broken URL is retried at most twice;
   - full-size images are never kept in memory (Tasks 1 and 5).
3. **Episode autoplay across a season boundary whose next season is not loaded yet:** the card waits for the episodes request, then plays the first episode of that season. If the request fails, it toasts and stops cleanly. The final episode of the final season never loops (Tasks 3 and 11).
4. **Switching providers from a Search result or the Source pill while something is loading or playing:**
   - the pending action runs only after the new provider is ready;
   - late results from the old provider are ignored;
   - nothing plays twice (Tasks 10 and 12).
5. **Deleting the old UI** must not drop a behaviour that only lived there: cache freshness, cache-write ordering, provider HTTPS preferences, export URL shapes, and next-season autoplay. Every old test is either retargeted to the new service that owns the behaviour, or deleted with a written reason (Task 13).

---

### Task 1: Poster cache

**Files:**
- Modify: `ui/logos.py`
- Test: `tests/test_image_cache_ui.py`

**Interfaces:**
- Consumes `ImageLoader(urls, image_manager, iconified=…, verify_ssl=…)` (it emits `progress_updated(current, total, {"rank", "cache_path"})`) and `ThreadCleanup`.
- Produces in `ui/logos.py`:
  - `ImageCache(QObject)`, the current `LogoCache` logic generalized. Its class attributes:
    - `MAX_SIDE` (logical px, scaled by devicePixelRatio);
    - `MAX_ENTRIES`;
    - `ICONIFIED`;
    - `MAX_RETRIES = 2`.

    It has a `logoReady(str)` signal (the name is kept for both caches), `pixmap(url) -> QPixmap | None`, `request(urls)`, `shutdown()`, and a hook `_enabled() -> bool`.
  - `LogoCache(ImageCache)`: `MAX_SIDE = 192`, `MAX_ENTRIES = 2000`, `ICONIFIED = True`; `_enabled()` returns `config.channel_logos`. Its behaviour is unchanged.
  - `PosterCache(ImageCache)`: `MAX_SIDE = 360`, `MAX_ENTRIES = 400`, `ICONIFIED = False`; `_enabled()` always returns True. Posters are the Library's content.

- [ ] **Step 1: Write failing tests** with a fake `ImageLoader` patched into `ui.logos`. Patch it so each batch emits `progress_updated` for the ranks whose paths a test map provides, then `finished`.
  1. `PosterCache` stores a 1000×1500 image bounded to 360 px on its long side at DPR 1, and emits `logoReady(url)`.
  2. `PosterCache.request` works when `config.channel_logos` is False; `LogoCache.request` does nothing then.
  3. Eviction: with `MAX_ENTRIES` patched to 3, requesting 5 distinct URLs keeps the 3 most recent.
  4. A URL whose loads fail is retried at most twice, and then never requested again.
  5. After `shutdown()`, late progress is ignored.
  6. The existing `LogoCache` tests still pass unchanged.
- [ ] **Steps 2–5:** run RED, implement (move the logic into `ImageCache`; keep `LogoCache`'s public behaviour identical), run GREEN, run the suite, lint/type, commit (`feat: add a bounded poster cache`).

---

### Task 2: Library model (`services/library.py`)

**Files:**
- Create: `services/library.py`
- Test: `tests/test_library_model.py`

**Interfaces:**
- Consumes:
  - `CatalogService`: `groups(section)`, `items(section, group_id)`, `request_section`, `request_group`, `current_provider_name()`, and the signals `sectionReady`, `sectionFailed`, `groupReady`, `groupFailed`, `providerReady`;
  - `WatchState.content_id` and `resume_position`;
  - `config.favorites` (a list of names), `config.watch_history`, `config.playback_positions` and `config.save_config()`;
  - `services.text_fold.fold`.
- Produces:
  ```python
  CONTINUE, NEW, MY_LIST = "@continue", "@new", "@mylist"

  @dataclass(frozen=True)
  class Genre:
      id: str
      title: str
      count: int | None
      pinned: bool = False

  class Sort(str, Enum):
      ADDED = "added"; AZ = "az"; YEAR = "year"; RATING = "rating"

  SORT_LABELS = {Sort.ADDED: "Recently added", Sort.AZ: "A–Z", Sort.YEAR: "Year", Sort.RATING: "Rating"}

  @dataclass(frozen=True)
  class Details:
      title: str
      year: str        # "" when unknown
      duration: str    # "1h 38m", "42m" or ""
      genre: str
      rating: str      # "7.8" or ""
      plot: str
      resume_ms: int | None
      progress: float | None   # 0..1 when the duration is known
      favorite: bool
      kind: str        # "movie" | "series" | "episode"

  class LibraryModel(QObject):
      changed = Signal()          # genres, items or sort changed
      failed = Signal(str)        # the selected genre or section failed (message)
      def __init__(self, catalog, config, watch, parent=None): ...
      section: str                # "movies" | "series" (read-only property)
      def set_section(self, section: str) -> None
      def genres(self, filter_text: str = "") -> list[Genre]
      genre_id: str | None        # read-only property
      def select_genre(self, genre_id: str) -> None
      def items(self) -> list[dict] | None      # None while loading
      sort: Sort                  # read-only property
      def set_sort(self, sort: Sort) -> None
      def available_sorts(self) -> list[Sort]
      def letter_index(self) -> dict[str, int]  # only for Sort.AZ, else {}
      def details(self, item: dict) -> Details
      def item_kind(self, item: dict) -> str
      def progress(self, item: dict) -> float | None
      def is_favorite(self, item: dict) -> bool
      def toggle_favorite(self, item: dict) -> bool   # new state; saves config
  ```

**Behaviour:**
- **`set_section(section)`:**
  - calls `catalog.request_section(section)`;
  - selects the first non-empty pinned genre, otherwise the first catalog genre, once the section is ready;
  - emits `changed`.
- **`genres(filter_text)`:**
  - pinned genres come first, each only when non-empty, in the order Continue ("⟲ Continue"), New ("✦ New"), My list ("★ My list");
  - then `catalog.groups(section)` as `Genre(id, title, count)`;
  - a non-empty filter keeps the genres whose `fold(title)` contains `fold(filter_text)`, pinned ones included.
- **`select_genre(id)`:**
  - for catalog genres, calls `catalog.request_group(section, id)` when `items()` is None;
  - `groupReady` / `groupFailed` for the selected genre emit `changed` / `failed(message)`;
  - events for other genres or sections are ignored.
- **Pinned items:**
  - **Continue:**
    - movies: `watch_history` entries for the current provider with `item_type == "movie"` and `resume_position(content_id) is not None`, most recent first;
    - series: entries with `item_type == "episode"` (ruling R1), most recent first;
    - de-duplicated by content id.
  - **New (R2):** all loaded items of the section, de-duplicated by `id` (falling back to `cmd` or `name`), with a parsable `added`, sorted by added (newest first) and limited to 48.
  - **My list:** loaded items whose `name` is in `config.favorites`.
- **Sorting** (`items()` applies the current sort; the sort is stable):
  - `ADDED`: by added, newest first; items without `added` keep provider order after the dated ones.
  - `AZ`: by `fold(title)`, with numbers compared naturally ("Title 2" < "Title 10").
  - `YEAR`: the year comes from `year`, the first 4 digits of `releasedate`/`releaseDate`/`release_date`, or a trailing `(YYYY)` in the name. Newest first; items without a year go last.
  - `RATING`: `rating` / `rating_imdb` as float, or `rating_5based × 2`. Highest first; items without a rating go last.
  - `available_sorts()` returns all four, minus `ADDED` when no item has `added`.
  - The default sort is `ADDED` when available, else `AZ`.
  - Pinned genres ignore the sort: they keep their own order.
- **`letter_index()`:**
  - the key is the first character of `fold(title)` when it is `a`–`z`, otherwise `"#"`;
  - the value is the first index with that key;
  - only for `Sort.AZ`; otherwise `{}`.
- **`details(item)`:**
  - `title` = name or title;
  - `year` as above;
  - `duration` from `duration` ("01:38:00" or "98" minutes), `duration_secs`, or `time` (minutes), formatted "1h 38m" / "42m";
  - `genre` from `genre` / `genres_str`, else the selected genre's title;
  - `rating` formatted with one decimal;
  - `plot` from `plot` / `description`;
  - `resume_ms` = `watch.resume_position(content_id)`;
  - `progress` = resume_ms / the saved duration from `config.playback_positions[cid]["duration"]` when that is above 0;
  - `favorite`;
  - `kind`: `"episode"` for Continue entries in series, otherwise `"series"` or `"movie"` by section.
- **`content_id(item)`:** uses `WatchState.content_id(provider, item, item_type=…)`, with item_type `"movie"`, `"serie"` (the v1.13.8 spelling) or `"episode"`.
- **`toggle_favorite`** adds or removes `item["name"]` in `config.favorites`, calls `save_config()`, and emits `changed` when a My list genre is affected.
- **`providerReady`** resets the section state and re-runs `set_section(self.section)`.

- [ ] **Step 1: Write failing tests** with a `FakeCatalog` (QObject with the listed signals; `groups`/`items` from dicts; it records `request_*` calls), a `SimpleNamespace` config, and a real `WatchState`:
  1. `set_section("movies")` requests the section. After `sectionReady`, `genres()` lists the catalog groups, and pinned groups appear only when non-empty.
  2. Continue for movies lists only movies with a resume point, most recent first. For series it lists episodes (R1).
  3. New lists the 48 newest by `added`, mixing unix and ISO formats, and is hidden when nothing has `added`.
  4. My list follows `config.favorites`. `toggle_favorite` adds and removes, saves config and emits `changed`.
  5. `genres("İst")` keeps "İstanbul Films" and drops "Drama".
  6. Each sort orders a fixed sample as specified, including natural numbers in A–Z and the trailing `(YYYY)` year.
  7. `letter_index()` maps "#" for "10 Things", "a" for "Arrival", and so on, and is `{}` for other sorts.
  8. `details()` formats durations ("01:38:00" → "1h 38m", "42" → "42m", `duration_secs` 5400 → "1h 30m") and ratings ("7.83" → "7.8", `rating_5based` "4" → "8.0").
  9. `select_genre` requests an unloaded group once. `groupFailed` for it emits `failed`; events for another genre are ignored.
  10. `providerReady` resets and re-requests the section.
- [ ] **Steps 2–5:** run RED, implement, run GREEN, run the suite, lint/type, commit (`feat: add the library model`).

---

### Task 3: Next-episode resolution and search (pure services)

**Files:**
- Create: `services/next_episode.py`, `services/search.py`
- Modify: `services/navigation.py` (make the item and episode key helpers public)
- Test: `tests/test_next_episode.py`, `tests/test_search.py`

**Interfaces:**
- `services/navigation.py`:
  - rename `_item_key(item)` to `item_key(item)`;
  - extract the episode key from `_item_node` into `episode_key(item)`;
  - update the internal callers. There is no behaviour change, and existing tests stay green.
- `services/next_episode.py` (complete code):
  ```python
  """Next episode / next movie resolution for autoplay (pure; no Qt)."""

  from __future__ import annotations

  from dataclasses import dataclass
  from typing import Callable, Optional, Union

  Key = Callable[[dict], str]


  @dataclass(frozen=True)
  class NextUp:
      season: Optional[dict]
      item: dict


  @dataclass(frozen=True)
  class NeedEpisodes:
      """The following season's episodes are not loaded yet: request them, then resolve again."""

      season: dict


  def _index(items: list[dict], target: dict, key: Key) -> int:
      wanted = key(target)
      return next((i for i, item in enumerate(items) if key(item) == wanted), -1)


  def next_episode(
      seasons: list[dict],
      episodes_of: Callable[[dict], Optional[list[dict]]],
      season: dict,
      episode: dict,
      *,
      season_key: Key,
      episode_key: Key,
  ) -> Union[NextUp, NeedEpisodes, None]:
      current = episodes_of(season)
      if not current:
          return None
      i = _index(current, episode, episode_key)
      if i < 0:
          return None
      if i < len(current) - 1:
          return NextUp(season, current[i + 1])
      s = _index(seasons, season, season_key)
      if s < 0:
          return None
      for following in seasons[s + 1 :]:
          episodes = episodes_of(following)
          if episodes is None:
              return NeedEpisodes(following)
          if episodes:
              return NextUp(following, episodes[0])
      return None


  def next_movie(items: list[dict], current: dict, *, key: Key) -> Optional[dict]:
      i = _index(items, current, key)
      return items[i + 1] if 0 <= i < len(items) - 1 else None
  ```
- `services/search.py` (complete code, plus `SearchWorker`):
  ```python
  """Search everything: folded multi-term matching over provider caches (pure)."""

  from __future__ import annotations

  from dataclasses import dataclass
  from typing import Any, Iterator

  from PySide6.QtCore import QObject, Signal

  from services.text_fold import fold

  CONTENT_SECTIONS = {"itv": "live", "vod": "movies", "series": "series"}
  MIN_CHARS = 2
  LIMIT_PER_SECTION = 300


  @dataclass(frozen=True)
  class Hit:
      provider: str
      section: str
      title: str
      item: dict


  def _title(item: dict) -> str:
      value = item.get("name") or item.get("title") or ""
      return value if isinstance(value, str) else str(value)


  def iter_items(content: Any) -> Iterator[dict]:
      if isinstance(content, list):
          yield from (i for i in content if isinstance(i, dict))
          return
      if not isinstance(content, dict):
          return
      contents = content.get("contents")
      if isinstance(contents, list):
          yield from (i for i in contents if isinstance(i, dict))
      elif isinstance(contents, dict):
          for group in contents.values():
              if isinstance(group, list):
                  yield from (i for i in group if isinstance(i, dict))


  def search_caches(
      caches: list[tuple[str, dict]], text: str, *, limit_per_section: int = LIMIT_PER_SECTION
  ) -> list[Hit]:
      words = fold(text).split()
      if sum(len(w) for w in words) < MIN_CHARS:
          return []
      hits: list[Hit] = []
      for provider, cache in caches:
          for content_type, section in CONTENT_SECTIONS.items():
              first: list[Hit] = []
              rest: list[Hit] = []
              seen: set[str] = set()
              for item in iter_items(cache.get(content_type)):
                  title = _title(item)
                  folded = fold(title)
                  if not all(w in folded for w in words):
                      continue
                  ident = str(item.get("id") or item.get("cmd") or title)
                  if ident in seen:
                      continue
                  seen.add(ident)
                  bucket = first if folded.startswith(words[0]) else rest
                  bucket.append(Hit(provider, section, title, item))
                  if len(first) >= limit_per_section:
                      break
              hits.extend((first + rest)[:limit_per_section])
      return hits


  class SearchWorker(QObject):
      """JobRunner worker: loads every provider's cached content (disk) unless given, then searches."""

      finished = Signal(object)  # (caches, hits)
      error = Signal(str)

      def __init__(self, provider_manager, text: str, caches=None):
          super().__init__()
          self._provider_manager = provider_manager
          self._text = text
          self._caches = caches

      def run(self) -> None:
          caches = self._caches
          if caches is None:
              caches = self._provider_manager.get_all_providers_cached_content()
          self.finished.emit((caches, search_caches(caches, self._text)))
  ```

- [ ] **Step 1: Write failing tests:**
  - `tests/test_next_episode.py`, porting the four cases of `tests/test_autoplay_next_season.py` onto `next_episode` with `season_key=item_key` and `episode_key=episode_key`:
    - STB next season starts at its first episode;
    - STB episode numbers as strings;
    - the last season has no next;
    - Xtream next season (episode dicts shaped as `services.catalog.format_xtream_episodes` returns them).

    Add:
    - `NeedEpisodes` when the next season is unloaded;
    - an empty following season is skipped;
    - an unknown episode gives None;
    - `next_movie` in the middle and at the end.
  - `tests/test_search.py`:
    - multi-term matching ("salt road" finds "Salt Roads (2024)");
    - accent/Turkish folding ("istanbul" finds "İstanbul");
    - prefix hits come first;
    - de-duplication across STB groups;
    - list (M3U), dict-with-list (Xtream/STB live) and dict-of-lists (STB on-demand) caches;
    - categories are never matched as items;
    - the per-section limit;
    - fewer than 2 characters gives `[]`;
    - `SearchWorker` under `ImmediateRunner` with a fake `provider_manager` emits `(caches, hits)` and, when given `caches`, does not call `get_all_providers_cached_content`.
- [ ] **Steps 2–5:** run RED, implement, run GREEN, run the suite, lint/type, commit (`feat: add next-episode resolution and search`).

---

### Task 4: Export service (`services/exporter.py`)

**Files:**
- Create: `services/exporter.py`
- Test: `tests/test_exporter.py`

**Interfaces:**
- Consumes:
  - `services.export.save_m3u_content(items, path)` and `save_stb_content(base_url, items, mac, path)`;
  - `services.provider_api` (`stb_portal_base`, `provider_prefers_https`, `stb_endpoint`);
  - the provider dict and `provider_manager.current_provider_content`;
  - `services.search.iter_items`.
- Produces:
  ```python
  class ExportKind(str, Enum):
      CACHED = "cached"; ALL_LIVE = "all_live"; COMPLETE_SERIES = "complete_series"

  def export_available(kind: ExportKind, provider: dict, section: str) -> str | None
      # None when allowed, else the user-facing reason (fixed strings below)
  def collect_items(content: dict, section: str) -> list[dict]
  def write_items(provider: dict, items: list[dict], path: str) -> int   # returns the count written

  class ExportWorker(QObject):              # JobRunner worker
      progress = Signal(int, int, str)      # done, total, label
      finished = Signal(object)             # int count
      error = Signal(str)
      def __init__(self, kind, provider, content, section, path, *, verify_ssl, prefer_https,
                   headers, cancel: threading.Event): ...
      def run(self) -> None
  ```

**Behaviour:**
- **`export_available`:**
  - `COMPLETE_SERIES` is allowed only for STB with section `"series"`. Otherwise it returns "Complete export is only available for STB series. Use “Cached content” instead.";
  - `ALL_LIVE` is allowed when the live content is loaded. Otherwise it returns "Load Live TV first.";
  - `CACHED` returns "Nothing is loaded in this section yet." when `collect_items` is empty.
- **`collect_items(content, section)`:** iterates `content[{"live": "itv", "movies": "vod", "series": "series"}[section]]` with `iter_items`, de-duplicated by `id` (falling back to `cmd` or `name`).
- **`write_items`:**
  - STB: `save_stb_content(stb_portal_base(provider["url"]), items, provider.get("mac", ""), path)`;
  - everything else: `save_m3u_content(items, path)`.

  It **never** passes the provider dict where a list is expected; that was the v1.13.8 crash in `export_content_cached`. It returns `len(items)`.
- **`ExportWorker.run`:**
  - `CACHED` / `ALL_LIVE`: `collect_items`, then `write_items`, emitting `progress(1, 1, "Writing…")`.
  - `COMPLETE_SERIES` (STB, R3): port `fetch_and_export_all_series`, `fetch_seasons_sync` and `fetch_episodes_sync` from `mixins/export_mixin.py` with the same request shapes. Use `requests` with `timeout=(10, 30)`, `verify=verify_ssl`, the provider headers and the per-provider HTTPS preference.
    - It checks `cancel.is_set()` before each series and emits `progress(done, total, series_name)`.
    - Cancelling emits `error("Export cancelled")`.
    - A network error on one series is logged and skipped, and the export continues.
    - The episodes are then written with `save_stb_content`.
  - Exceptions become `error("Export failed: <ExceptionType>")`. Messages never include URLs or credentials.

- [ ] **Step 1: Write failing tests:**
  1. `CACHED` for an M3U provider writes a file with one `#EXTINF` per item. The v1.13.8 crash shape (a provider dict instead of a list) cannot occur through `write_items`.
  2. `CACHED` for STB writes `…/play/live.php?...` URLs from the portal base, never `//play` (Phase 0 regression).
  3. `ALL_LIVE` without live content gives "Load Live TV first."
  4. `COMPLETE_SERIES` for Xtream gives the R3 message.
  5. `COMPLETE_SERIES` against a local `ThreadingHTTPServer` fake portal returns 2 series × 2 seasons × 2 episodes. It writes 8 entries, and progress reaches `(2, 2, …)`.
  6. Setting `cancel` during the first series gives `error("Export cancelled")` and no file.
  7. A failing series (HTTP 500) is skipped, and the others are written.
- [ ] **Steps 2–5:** run RED, implement, run GREEN, run the suite, lint/type, commit (`feat: add the export service`).

---

### Task 5: Poster grid with inline detail strip and A–Z rail (`ui/library/poster_grid.py`)

**Files:**
- Create: `ui/library/__init__.py`, `ui/library/poster_grid.py`
- Test: `tests/test_poster_grid.py`

**Interfaces:**
- Consumes `PosterCache` (Task 1), `Details` (Task 2), the theme, and painting `Units`.
- Produces `PosterGrid(QAbstractScrollArea)`:
  ```python
  activated = Signal(int)            # Enter / double-click / ▶ button (flat index)
  startOverRequested = Signal(int)
  favoriteToggled = Signal(int)
  focusChanged = Signal(int)
  sortRequested = Signal(str)        # Sort value
  leftEdge = Signal()                # ← in the first column
  upEdge = Signal()                  # ↑ in the first row
  def __init__(self, posters, parent=None)
  def set_header(self, title: str, count_text: str, sorts: list[tuple[str, str]], active: str)
  def set_sections(self, sections: list[tuple[str, list[dict]]])   # [("", items)] in normal mode
  def set_details_provider(self, fn: Callable[[dict], Details | None])
  def set_progress_provider(self, fn: Callable[[dict], float | None])
  def set_letter_index(self, index: dict[str, int] | None)          # None hides the rail
  def set_message(self, text: str | None)       # "Loading…", an error, or "Nothing here"
  def current_index(self) -> int                # -1 when empty
  def set_current_index(self, i: int, *, reveal: bool = True)
  def move(self, dx: int, dy: int)              # dx = ±1 column, dy = ±1 row
  def page(self, delta: int); def home(self); def end(self)
  def jump_to_letter(self, letter: str)
  def item(self, i: int) -> dict | None
  def index_at(self, pos: QPoint) -> int; def button_at(self, pos: QPoint) -> str | None
  def strip_rect(self) -> QRect                 # the detail strip in viewport coordinates
  ```

**Behaviour and painting** (mockup 05, option 1; u = 1% of the grid's parent library width, so the grid takes `units_width` from its parent like the drawer views):
- **Header** (fixed above the viewport, 5u high): the title at font 1.7u bold, then the count at 1.0u and opacity 0.5. Sort chips are right-aligned at font 1.0u, separated by " · ". The active chip is in text colour, the others at 0.5. Clicking a chip emits `sortRequested(value)`.
- **Layout:**
  - gap 1.2u;
  - columns = `max(3, (viewport_w + gap) // (9.5u + gap))`;
  - poster width fills the row;
  - poster height = width × 1.5;
  - section titles (search mode) at font 1.3u bold, 3u high, before their section's rows. An untitled section has no title row.
- **Posters:**
  - rounded at `RADIUS_CARD`;
  - the image is scaled to cover (centre crop);
  - **without an image:** a vertical gradient from a hue derived from `fold(title)` (a stable hash, saturation 0.45, value 0.42 → 0.18), with the title at font 1.1u bold, word-wrapped at the bottom-left with a 0.8u margin;
  - **resume progress** (`progress_provider`): a 0.45u bar at the bottom inside a 0.8u inset, the track at text 0.25 and the fill in accent;
  - **focus:** the accent ring plus a slight lift (draw 0.3u larger).
- **Detail strip:**
  - inserted after the row containing the current item, 13u high, with a 1.2u gap above and below;
  - background `PALETTE.panel_raised` with a left-to-right gradient into the accent at 0.12;
  - title at 2.2u bold;
  - meta line at 1.1u and opacity 0.6: `year · duration · genre · ★ rating`, omitting empty parts, then `· mm:ss watched` in accent when `resume_ms`;
  - plot at 1.15u, at most 2 lines, elided;
  - buttons right-aligned, 4u high, radius 0.8u:
    - movie with resume: `Start over` (raised), `★` (raised square; accent text when favourite), `▶ Resume` (accent fill, dark text);
    - movie without resume: `★`, `▶ Play`;
    - series: `★`, `▶ Episodes`;
    - episode (Continue in series): `Start over`, `▶ Resume`.
  - Clicks: `▶` emits `activated`, `Start over` emits `startOverRequested`, `★` emits `favoriteToggled`.
- **A–Z rail:**
  - shown only when `set_letter_index` got a non-empty dict and the item count is at least 60;
  - 1.6u wide at the right edge of the viewport; the letters `#A…Z` at font 0.85u spread over the height;
  - letters in the index at 0.6, others at 0.2;
  - clicking a letter calls `jump_to_letter`.
- **Movement:**
  - `move(-1, 0)` in column 0 emits `leftEdge`; `move(0, -1)` in the first row emits `upEdge`;
  - `move(+1, 0)` at the last column or the last item does nothing;
  - `move(0, ±1)` keeps the column, clamped to the last item of a shorter row;
  - in search mode, rows flow across section boundaries;
  - `page(±1)` moves by the number of fully visible rows; `home`/`end` go to the first or last item.
  - Each change emits `focusChanged` and, with `reveal`, scrolls so that the current row and its strip are fully visible.
- **Mouse:**
  - hovering a poster for 250 ms makes it current (`reveal=False`), so moving the mouse across the grid does not make the strip jump;
  - clicking a non-current poster makes it current; clicking the current poster or double-clicking activates;
  - the wheel scrolls the viewport.
- **Messages:** `set_message(text)` paints the text centred at 1.35u with opacity 0.7 instead of the grid.
- **Posters on demand:** only the visible rows plus one row ahead are passed to `posters.request` (de-duplicated). `logoReady(url)` repaints only when the URL is visible.

- [ ] **Step 1: Write failing tests** (offscreen; a 1280×720 parent; a fake poster cache with `pixmap()` returning None and recording `request`):
  1. 7 columns at 1280 px parent width (9.5u target), and at least 3 columns at 400 px.
  2. `move` semantics: the right edge, `leftEdge` at column 0, `upEdge` at row 0, column clamping into a short last row.
  3. The strip follows the current row: `strip_rect().top()` is below the current poster's bottom, and the next row's top is below the strip.
  4. Clicking the `▶` button in the strip emits `activated(current)`, `★` emits `favoriteToggled`, and `Start over` emits `startOverRequested`, for a details provider with resume.
  5. Hover dwell: after 300 ms over another poster it becomes current; after 100 ms it does not.
  6. `jump_to_letter("m")` with an index makes the mapped item current.
  7. With 20,000 items, `set_sections` plus a paint (`grab()`) takes under 150 ms, and `request` is called with at most `(visible rows + 1) × columns` URLs.
  8. Sections: two titled sections; `move(0, 1)` from the last row of the first section goes to the second section.
  9. `set_message("Loading…")` paints without items (`grab()` non-null).
  10. A sort chip click emits `sortRequested("az")`.
- [ ] **Steps 2–5:** run RED, implement, run GREEN, run the suite, lint/type. Render `scratchpad/library/grid.png` (1280×720 library-sized, 30 fake titles, two with posters, one focused with resume) and view it against mockup 05. Commit (`feat: add the poster grid with inline details`).

---

### Task 6: Genre column and top navigation (`ui/library/genre_list.py`, `ui/library/top_nav.py`)

**Files:**
- Create: `ui/library/genre_list.py`, `ui/library/top_nav.py`
- Test: `tests/test_genre_nav.py`

**Interfaces:**
- `GenreList(QWidget)`:
  ```python
  genreSelected = Signal(str)       # debounced highlight change (preview)
  genreActivated = Signal(str)      # Enter / → / click: move focus to the grid
  def set_genres(self, genres: list[Genre])           # pinned first, as the model returns them
  def set_current(self, genre_id: str | None)
  def current_id(self) -> str | None
  def move(self, delta: int); def page(self, delta: int)
  def type_text(self, text: str); def backspace(self) -> None
  def escape(self) -> bool          # True when it cleared a non-empty filter
  filter_text: str                  # property; the controller passes it to LibraryModel.genres()
  filterChanged = Signal(str)
  def set_focused(self, focused: bool)
  ```
- `TopNav(QWidget)`:
  ```python
  tabRequested = Signal(str)        # "live" | "movies" | "series"
  searchRequested = Signal()
  sourceRequested = Signal(QPoint)  # global position under the pill, for a provider QMenu
  def set_active(self, tab: str)    # "live" | "movies" | "series" | "search"
  def set_source_name(self, name: str)
  def set_tabs(self, tabs: list[str])                 # Movies/Series hidden for M3U providers
  def set_focused(self, focused: bool)
  def move(self, delta: int)        # focus between the tabs and pills
  def activate(self) -> None
  ```

**Painting** (mockup 05, option 1):
- **GenreList:**
  - width 18u, background `PALETTE.panel_raised`, a 1 px right border at text 0.06;
  - the filter field at the top: 1.2u margin, 3.2u high, radius 0.8u, border at text 0.12, text `"⌕ Find a genre…"` at opacity 0.45 when empty, otherwise `"⌕ " + filter + caret`, at font 1.05u;
  - section labels `PINNED` and `GENRES · N` at font 0.85u, opacity 0.45, letter-spacing 0.08em;
  - rows 3.2u high: title at 1.15u, count right-aligned at 0.95u and opacity 0.45;
  - the current row gets the focus style when the list is focused, otherwise a 10% text fill;
  - model/view virtualized (QListView with uniform item sizes and a delegate).
- **Filtering:** `type_text` appends, and the controller re-calls `set_genres(model.genres(filter))`. The current genre stays current when it is still listed; otherwise the first match becomes current (and is previewed).
- **Preview:** a highlight change emits `genreSelected` after 250 ms idle (a single-shot timer). `genreActivated` flushes it first.
- **TopNav:**
  - 6u high, background `PALETTE.canvas`, a bottom border at text 0.06;
  - `QiTV` at 1.6u bold;
  - the tabs Live TV / Movies / Series at 1.3u: the active one in text colour with a 0.25u accent underline, the others at 0.62;
  - right-aligned pills with a 1 px border at text 0.15 and radius 1.6u: `⌕ Search` and `<source name> ▾`;
  - keyboard focus draws the focus style on the focused tab or pill.

- [ ] **Step 1: Write failing tests:**
  1. GenreList shows pinned genres under "PINNED" and the rest under "GENRES · N" (checks through a `rows()` accessor that returns the painted labels in order).
  2. `move(3)` quickly emits `genreSelected` once, after the debounce, with the final id.
  3. `type_text("doc")` then `escape()` returns True and clears; a second `escape()` returns False.
  4. Activating flushes the pending preview: `genreSelected` comes before `genreActivated`.
  5. TopNav `set_tabs(["live"])` hides Movies/Series. Clicking the Movies tab emits `tabRequested("movies")`; clicking the source pill emits `sourceRequested` with a point under the pill.
  6. TopNav keyboard: `move(1)` twice then `activate()` from Live TV emits `tabRequested("series")`.
  7. `grab()` non-null for both, at 1280 and 800 px widths.
- [ ] **Steps 2–5:** run RED, implement, run GREEN, run the suite, lint/type. Render `scratchpad/library/genres.png` and view it. Commit (`feat: add the genre column and library navigation`).

---

### Task 7: Series view (`ui/library/series_view.py`)

**Files:**
- Create: `ui/library/series_view.py`
- Test: `tests/test_series_view.py`

**Interfaces:**
```python
class SeriesView(QWidget):
    seasonSelected = Signal(object)            # season dict (preview/load)
    episodeActivated = Signal(object, object)  # (season, episode)
    startOverRequested = Signal(object, object)
    retryRequested = Signal()
    backRequested = Signal()                   # Esc / ← from the seasons row's first chip
    def __init__(self, posters, parent=None)
    def set_series(self, series: dict, details: Details)
    def set_seasons(self, seasons: list[dict] | None, error: str | None = None)
    def set_episodes(self, season: dict, episodes: list[dict] | None, error: str | None = None)
    def set_progress_provider(self, fn: Callable[[dict], float | None])
    def current_season(self) -> dict | None
    def current_episode(self) -> dict | None
    def move(self, dx: int, dy: int); def activate(self); def escape(self) -> bool
```

**Layout:**
- **Header** (34% of the height):
  - the poster on the left, 14u wide at 2:3;
  - the title at 2.6u bold;
  - the meta line (as in the grid strip);
  - the plot at 1.15u, at most 3 lines.
- **Seasons row:**
  - chips "Season N" (or the season's name) at 1.1u, height 3.4u, radius 1.7u;
  - the current chip uses the focus style when the row is focused, otherwise an accent underline.
- **Episodes list** (virtualized):
  - rows 5u high: `E{n}` in accent at 1.1u bold, the title at 1.2u, the duration right-aligned at 1.0u opacity 0.5;
  - a progress bar under the title (0.35u) when there is a resume point;
  - `✓` in OK colour when progress ≥ 0.9.
- **Loading and error:**
  - "Loading…" while `None`;
  - an error shows "Couldn't load — press Enter to retry", and `activate` then emits `retryRequested`.

**Keys:**
- Focus starts on the episodes list when episodes exist, otherwise on the seasons row.
- In the seasons row: `move(±1, 0)` changes the season and emits `seasonSelected` after 250 ms idle; `move(0, 1)` goes to the episodes.
- In the episodes: `move(0, -1)` from the first episode goes to the seasons row.
- `activate()` on an episode emits `episodeActivated(season, episode)`.
- `escape()` emits `backRequested` and returns True.

- [ ] **Step 1: Write failing tests:**
  1. `set_seasons([…3])` then `move(1, 0)` twice quickly emits `seasonSelected` once, with the third season.
  2. `set_episodes(season, [...])` then `activate()` emits `episodeActivated(season, first episode)`.
  3. From the first episode, `move(0, -1)` focuses the seasons row (asserted through a `focus_zone` property).
  4. The error state's `activate()` emits `retryRequested`.
  5. Progress ≥ 0.9 paints the check mark (through a `row_badges(i)` accessor that returns `"✓"`).
  6. 2,000 episodes paint in under 100 ms.
- [ ] **Steps 2–5:** run RED, implement, run GREEN, run the suite, lint/type. Render `scratchpad/library/series.png` and view it. Commit (`feat: add the series view`).

---

### Task 8: Guide overlay (`ui/overlays/guide.py`)

**Files:**
- Create: `ui/overlays/guide.py`
- Test: `tests/test_guide.py`

**Interfaces:**
```python
class Guide(Overlay):
    tuneRequested = Signal(object)        # channel dict
    closed = Signal()
    def __init__(self, logos, programmes: Callable[[dict, datetime, int], list[Programme]],
                 now: Callable[[], datetime], parent=None)
    def open_guide(self, channels: list[dict], current: int)
    def close_guide(self)
    def move(self, delta: int)            # channel rows
    def page(self, delta: int)
    def shift(self, delta: int)           # ±1 = ±30 minutes, clamped to [now slot, now + 24 h]
    def activate(self)                    # Enter: tuneRequested(current channel)
    def escape(self) -> bool              # closes
    def current_channel(self) -> dict | None
    def window_start(self) -> datetime
```

**Painting** (spec §5.1; R4):
- Full host area. Background `PALETTE.panel` at 0.94.
- Time header 4u high: half-hour ticks with labels at font 1.0u and opacity 0.6.
- **Channel column** 22u wide: number (accent, font 1.1u), logo (3u tile via `draw_logo_tile`), and name at 1.1u, elided.
- **Rows:** 5u high, virtualized (only the visible rows are painted and queried).
- **Programme boxes:**
  - inset 0.2u, radius 0.6u, fill text at 0.06;
  - the programme that is on now gets text at 0.1 with a 0.2u accent left edge;
  - the title at 1.05u, elided, with the time at 0.85u and opacity 0.55 beneath it when the box is at least 8u wide;
  - "No programme information" fills a row with no programmes.
- The window spans 2.5 h from `window_start` (the current half-hour, R4).
- The **now-line** is a 0.2u accent vertical line across the rows, with a small `▼ now` label in the header.
- The **current row** has the focus style across its full width.
- The **footer** (5u) shows the current row's programme on now (or the first upcoming one): its title at 1.2u bold, the time range, and up to 2 lines of description at 1.0u and opacity 0.7.
- **Data:** `programmes(channel, window_start, 12)` for each visible row, cached per (channel key, window_start). The cache is cleared on `open_guide` and on `shift`.

- [ ] **Step 1: Write failing tests:**
  1. `open_guide` sets the current row, and `move(1)` changes `current_channel`.
  2. `activate()` emits `tuneRequested` with the current channel.
  3. `shift(-1)` at the start does nothing. `shift(1)` moves `window_start` by 30 minutes, and `shift` never exceeds now + 24 h.
  4. With 1,000 channels, a `grab()` queries `programmes` only for the visible rows (≤ 20 calls at 720 px).
  5. `escape()` closes and emits `closed`.
  6. A box layout helper places a 30-minute programme at 1/5 of the time area's width.
- [ ] **Steps 2–5:** run RED, implement, run GREEN, run the suite, lint/type. Render `scratchpad/overlays/guide.png` and view it. Commit (`feat: add the guide overlay`).

---

### Task 9: Next-episode card (`ui/overlays/next_card.py`) and transport Next

**Files:**
- Create: `ui/overlays/next_card.py`
- Modify: `ui/overlays/transport.py`
- Test: `tests/test_next_card.py`, `tests/test_transport.py` (extend)

**Interfaces:**
- `NextCard(Overlay)`:
  ```python
  playNow = Signal()
  cancelled = Signal()
  def show_next(self, title: str, subtitle: str, seconds: int = 10)
  def activate(self)        # play now
  def escape(self) -> bool  # cancel; True when it was visible
  def remaining(self) -> int
  ```
  - Bottom-right corner, 2.4u from the right and 14u from the bottom, 30u wide, background `PALETTE.panel_raised` at 0.96, radius `RADIUS_PANEL`.
  - Label `"Next episode in N s"` (or `"Next in N s"` for movies, chosen by the caller's title) at 1.0u and opacity 0.6. The title is at 1.4u bold and the subtitle at 1.05u.
  - Buttons `▶ Play now` (accent fill) and `Cancel` (raised).
  - A countdown bar along the bottom edge.
  - At 0 it emits `playNow` once and hides. Clicking the buttons emits the matching signal and hides.
- `Transport`:
  - new signal `nextRequested()`;
  - `set_next_available(bool)` shows a `⏭` button after `+10` in VOD mode, with the tooltip-free label "Next".

- [ ] **Step 1: Write failing tests:**
  - the countdown emits `playNow` exactly once after `seconds` (use `seconds=1` and `qWait`);
  - `escape()` emits `cancelled` and stops the timer;
  - clicking `Play now` emits `playNow`;
  - the transport Next button appears only when available and emits `nextRequested` on click.
- [ ] **Steps 2–5:** run RED, implement, run GREEN, run the suite, lint/type. Render `scratchpad/overlays/next.png`. Commit (`feat: add the next-episode card and transport Next`).

---

### Task 10: Library screen and controller

**Files:**
- Create: `ui/library/library_screen.py`, `ui/library_controller.py`
- Modify: `ui/keymap.py` (library context)
- Test: `tests/test_library_controller.py`, `tests/test_keymap.py` (extend)

**Interfaces:**
- `LibraryScreen(QWidget)` composes `TopNav`, `GenreList`, `PosterGrid` and `SeriesView`, plus a search field painted in the grid header in search mode. It exposes them as attributes `nav`, `genres`, `grid`, `series`. It has `set_mode("browse" | "series" | "search")`. Layout: the top nav across the top; the genre column on the left (hidden in search mode); the grid or series view filling the rest. The background is `PALETTE.canvas`.
- `ui/keymap.py`: `resolve(..., context="player" | "drawer" | "library")`. The Phase 1B `drawer_open` keyword becomes `context`, and the old parameter is kept as an alias. Library context:
  - arrows give `NAV_*`;
  - PgUp/PgDn give `NAV_PAGE_*`;
  - Home/End give the new `NAV_HOME` / `NAV_END`;
  - Enter gives `NAV_ACTIVATE`;
  - Esc gives `ESCAPE`;
  - Backspace gives `NAV_BACKSPACE`;
  - printable text gives `NAV_TEXT`;
  - `/` and Ctrl+F give the new `SEARCH`;
  - Ctrl+D gives the new `FAVORITE`;
  - `F` with Ctrl gives `FULLSCREEN`.
  New actions: `SEARCH`, `FAVORITE`, `NAV_HOME`, `NAV_END`, `LIBRARY`, `GUIDE` (exists), `OPEN_FILE`.
- `LibraryController(QObject)`, constructed as `LibraryController(*, config, catalog, model: LibraryModel, watch, screen: LibraryScreen, provider_manager, jobs: JobRunner, play: Callable[[dict, str, dict], None], tune_live: Callable[[dict], None], back_to_tv: Callable[[], None])`:
  - `play(item, kind, context)` is supplied by the window. `kind` is `"movie" | "episode"`. `context` carries `{"start_over": bool, "series": dict | None, "season": dict | None, "episodes": list[dict] | None, "genre_items": list[dict] | None}`, which the player uses for resume and autoplay.
  - `open(section: str)`, `open_search()`, `handle(action, payload="") -> bool`, `refresh()`.
  - `on_provider_ready(name)` runs a pending cross-provider action.

**Behaviour:**
- **Focus zones:** `nav`, `genres`, `grid`, `series`, `search`. Keys go to the focused zone:
  - **grid:** arrows call `move`; `leftEdge` focuses genres (browse mode); `upEdge` focuses nav; Enter activates; Ctrl+D toggles the favourite of the current item; Home/End/PgUp/PgDn work.
  - **genres:** ↑/↓ move; → or Enter focuses the grid; text filters; Backspace deletes; Esc clears the filter, else goes to nav.
  - **nav:** ←/→ move; Enter activates; ↓ returns to the previous zone.
  - **series:** keys go to `SeriesView`; `backRequested` returns to the grid with the series still current.
  - **search:** text edits the query; ↓ focuses the results grid; Esc clears the query, else leaves search.
  - **Esc at the top level** (nav, or genres with an empty filter) calls `back_to_tv()` (R5).
- **Activation:**
  - **movie:** `play(item, "movie", {"start_over": False, "genre_items": model.items()})`;
  - **series:** switch to series mode;
    - `set_series(item, model.details(item))`;
    - `catalog.seasons(item)`, or `catalog.request_seasons(item)` and wait for `seasonsReady` / `seasonsFailed`;
    - select the first season, or the season of the most recent history entry for that series when known;
    - episodes come through `catalog.episodes` / `request_episodes` (`episodesReady` / `episodesFailed`);
  - **episode** (from SeriesView or Continue): `play(episode, "episode", {"series": …, "season": …, "episodes": …})`. For Continue entries without a series dict, `series` is None, so autoplay is unavailable for that play.
  - `startOverRequested` passes `start_over=True`.
  - `favoriteToggled` calls `model.toggle_favorite` and refreshes the strip.
- **Model wiring:**
  - `model.changed` refreshes the genres (with the current filter) and the grid:
    - header: genre title, `"{n:,} titles"`, sorts from `available_sorts()` with `SORT_LABELS`;
    - `set_sections([("", items)])`;
    - `set_letter_index(model.letter_index())`;
    - details and progress providers from the model;
  - `model.failed(msg)` gives `grid.set_message("Couldn't load — press Enter to retry")`, and Enter retries;
  - `genreSelected` calls `model.select_genre`; `sortRequested` calls `model.set_sort`.
- **Search** (Review Focus 1 and 4):
  - each query change restarts a 250 ms debounce;
  - then a `SearchWorker` runs through `jobs` with a generation number. The first search loads the caches; later searches pass the cached snapshot, which is dropped on `providerReady` and on `refresh()`;
  - results with an older generation are ignored;
  - the grid shows sections `"{provider} · Live TV (n)"`, `"… · Movies (n)"` and `"… · Series (n)"`. Live channels are drawn with their logo centred on a dark tile (a poster provider shim).
  - **Activating a hit:**
    - for the current provider: live calls `tune_live(channel)`, which the window implements as switching to the player and tuning with group `"*"`; movie/series/episode behave as above;
    - for another provider: remember `(provider, section, item)` as the single pending action and call `catalog.select_provider(name)`. On `providerReady(name)` matching it, run the action (for series, wait for `sectionReady("series")` before `request_seasons`). A newer pending action replaces the older one; a different provider becoming ready drops it.
- **Source pill:** a `QMenu` of `catalog.provider_names()` (checked = current). Selecting one calls `catalog.select_provider(name)`, and the library re-opens the current section when it is ready.
- **Tabs:** `live` calls `back_to_tv()`; `movies`/`series` call `open(section)`; `search` calls `open_search()`. For M3U providers (`catalog.sections() == ["live"]`), only Live TV and Search are shown.

- [ ] **Step 1: Write failing tests** (fakes for the catalog and provider manager, `ImmediateRunner` for jobs, a real `LibraryModel` over the fake catalog, a real `LibraryScreen` offscreen):
  1. `open("movies")` shows the genres and the grid. ← from grid column 0 focuses genres, and → returns.
  2. Activating a movie calls `play(item, "movie", {...start_over False...})`; Start over passes True.
  3. Activating a series loads seasons then episodes through the fake catalog, and activating an episode calls `play(episode, "episode", {series, season, episodes})`.
  4. Esc from the genres (empty filter) calls `back_to_tv`.
  5. Search: typing "sal" twice quickly runs one search after the debounce. A stale generation result is ignored (simulate by finishing an older job after a newer one).
  6. A hit from another provider calls `select_provider`. `providerReady` for that provider runs the play; a different provider becoming ready drops it.
  7. Ctrl+D on the grid toggles the favourite (config favourites change), and the strip shows the accent star.
  8. `model.failed` shows the retry message; Enter re-selects the genre (a new `request_group`).
  9. Keymap library context: `/` gives `SEARCH`, Ctrl+D gives `FAVORITE`, Home gives `NAV_HOME`, and `a` gives `NAV_TEXT`.
- [ ] **Steps 2–5:** run RED, implement, run GREEN, run the suite, lint/type. Render `scratchpad/library/screen.png` (1280×800, browse mode, Documentary selected, one focused movie with resume) and view it against mockup 05 option 1. Fix visual mismatches. Commit (`feat: add the library screen and controller`).

---

### Task 11: Player integration: guide, autoplay, favorites and context actions

**Files:**
- Modify: `ui/tv_controller.py`, `ui/overlays/drawer.py` (context signal), `ui/keymap.py`
- Test: `tests/test_tv_controller.py` (extend), `tests/test_drawer.py` (extend)

**Behaviour:**
- **Guide:**
  - `GUIDE` (`G`), during live, opens `guide.open_guide(zap.context.channels, current index)`;
  - while the guide is open, keys route to it: ↑/↓ `move`, PgUp/PgDn `page`, ←/→ `shift`, Enter `activate`, Esc `escape`, and `G` closes;
  - `tuneRequested(channel)` calls `tune(channel, group_key=zap.context.group_key, channels=zap.context.channels)` and closes the guide;
  - `Overlays` gains `guide` and `next_card` fields; the window builds them. The context menu is a `QMenu` created on demand.
- **Autoplay** (Review Focus 3):
  - `play_vod(item, kind, context)` is the single VOD entry point, used by the drawer and the library. It requests the link and plays with resume (`start_ms` from `watch.resume_position`, or 0 when `context["start_over"]`). It remembers `self._vod = (item, kind, context)` and calls `transport.set_next_available(...)` (True for an episode with series context, or a movie with `genre_items`).
  - On `session.ended` for VOD, the controller computes the next item:
    - episode: `next_episode(catalog.seasons(series), lambda s: catalog.episodes(series, s), season, episode, season_key=item_key, episode_key=episode_key)`;
    - movie: `next_movie(genre_items, item, key=item_key)`.
  - Autoplay requires `config.auto_play_episodes` or `config.auto_play_movies`.
  - `NeedEpisodes(season)` calls `catalog.request_episodes(series, season)` and resolves again on `episodesReady`. `episodesFailed` toasts `"Couldn't load the next season"` and stops.
  - `NextUp` shows `next_card.show_next(title, subtitle)` with subtitle `"S{season} · E{episode}"` or the genre title. `playNow` plays it with the advanced context; `cancelled` leaves the player on the final frame and shows the transport.
  - The transport's `nextRequested` resolves and plays immediately, without the card.
  - External players never autoplay (as in v1.13.8).
  - **Keys while the card is visible:** Enter plays now; Esc cancels.
- **Favorites:**
  - `FAVORITE` (Ctrl+D) toggles `config.favorites` for the highlighted drawer node (live items) when the drawer is open, otherwise for the current channel;
  - toast `"Added to favorites"` / `"Removed from favorites"`;
  - `NavModel` picks the change up through its favourites fingerprint.
- **Context actions:**
  - `Drawer` gains `contextRequested(object node, QPoint global_pos)`, emitted on a right-click on an item, episode or channel row;
  - the controller shows a themed `QMenu` with:
    - "Add to favorites" / "Remove from favorites" (live items only);
    - "Play in VLC" / "Play in MPV" (request the link, then `external.launch_vlc` / `launch_mpv`; toast on failure);
    - "Copy stream link" (request the link, then `QGuiApplication.clipboard().setText(url)` and toast `"Link copied"`);
  - link requests here are separate from the tune's single in-flight slot, but are still serialized one at a time.
- **Mouse back/forward** (spec §5.3): `XButton1` with the drawer open gives `left()`; closed, it opens the drawer. `XButton2` with the drawer open gives `right()`.

- [ ] **Step 1: Write failing tests** (fakes as in Phase 1B Task 7):
  1. `G` during live opens the guide with the zap channels; guide `tuneRequested` tunes and closes it.
  2. An episode ends with autoplay on: the card is shown, and `playNow` plays the next episode with resume.
  3. The episode ends at a season boundary whose next season is unloaded: `request_episodes` is called, and after `episodesReady` the card shows the first episode of the next season. `episodesFailed` toasts and shows no card.
  4. The last episode of the last season: no card.
  5. A movie ends with `auto_play_movies`: the card names the next movie in `genre_items`.
  6. Ctrl+D with the drawer highlighting a channel adds it to favorites and toasts.
  7. The context "Copy stream link" sets the clipboard after `linkReady`.
  8. `XButton1` opens the drawer when it is closed.
- [ ] **Steps 2–5:** run RED, implement, run GREEN, run the suite, lint/type, commit (`feat: add the guide, autoplay, favorites and context actions to the player`).

---

### Task 12: Window integration: screens, menus, exports, welcome

**Files:**
- Modify: `ui/tv_window.py`, `ui/tv_controller.py`
- Create: `ui/overlays/welcome.py`, `ui/shortcuts_dialog.py`
- Test: `tests/test_tv_window.py` (extend), `tests/test_welcome.py`

**Behaviour:**
- **New `TVController` methods** (used below):
  - `stop_for_library()`: stops live playback and releases the connection-budget ticket;
  - `resume_live()`: re-tunes `config.last_live_channel` through the autotune path, or opens the drawer when there is none;
  - `tune_live(channel)`: tunes with `group_key="*"` and `channels=catalog.items("live", "*")`.
- **Screens:**
  - `TVWindow` central widget = `QStackedWidget` with `player_page` (the `VideoSurface`) and `library_page` (`LibraryScreen`);
  - the `OverlayHost` hides whenever the player page is not current (it already follows the surface's visibility; verify and test it).
  - **`open_library(section)`:**
    - stops live playback (`controller.stop_for_library()`, which stops the live session or VLC and releases the connection-budget ticket);
    - switches the page;
    - calls `library_controller.open(section)`.
  - **Drawer integration:** choosing the Movies or Series *section* in the drawer opens the library at that section; the drawer closes.
  - **`play(item, kind, context)` from the library:**
    - switches to the player page;
    - `controller.play_vod(item, kind, context)`;
    - remembers `return_to_library = True`.
  - **`ESCAPE` during VOD with no overlay open and not fullscreen**, when `return_to_library` is set: stop and return to the library with its state (genre, current item, scroll) preserved.
  - **`back_to_tv()`:**
    - switches to the player page;
    - `controller.resume_live()` re-tunes `config.last_live_channel` (R5) through the existing autotune path, or opens the drawer when there is none.
  - **Key routing:** `keyPressEvent` resolves with `context="library"` when the library page is current, otherwise `"drawer"` when the drawer is open (or the guide or card routing from Task 11), otherwise `"player"`.
- **Menus** (replacing the Phase 1B menu):
  - *QiTV*: About QiTV (themed message with the version from `pyproject`/`__version__` and the links), Check for Updates… (`update_checker.check_for_updates(config_manager, manual=True)`), Settings… (Ctrl+,), Quit (Ctrl+Q);
  - *File*: Open File… (Ctrl+O, a `QFileDialog` for media files, played as VOD), and an Export submenu:
    - Cached Content…;
    - All Live Channels…;
    - Complete Series (Fetch All)…;
  - *View*: Live TV (Ctrl+1), Movies (Ctrl+2), Series (Ctrl+3), Search (Ctrl+F), Guide (G), Channel List (Return), Fullscreen (F), Mini Player (Alt+P);
  - *Playback*: Play/Pause (Space), Mute (M), Previous Channel (Backspace), Go Live (L);
  - *Providers*: an exclusive `QActionGroup` of provider names, rebuilt after Settings closes; selecting one calls `controller.select_provider(name)`;
  - *Help*: Keyboard Shortcuts… (`ShortcutsDialog`: a themed two-column table generated from one list in `ui/keymap.py`, `SHORTCUTS: list[tuple[str, str]]`, so the docs never drift), Report an Issue… (opens `https://github.com/ozankaraali/QiTV/issues` with `QDesktopServices`).
  - Menu shortcuts use `Qt.ApplicationShortcut` only where `keyPressEvent` does not already handle the key (as in Phase 1B).
- **Exports:** each menu entry:
  1. checks `export_available(kind, provider, section)`, where the section is the library's current section or `"live"` on the player page, and toasts the reason when it is not None;
  2. asks for a path (`QFileDialog.getSaveFileName`, default `~/Downloads/<provider>-<kind>.m3u`, filter `M3U files (*.m3u)`);
  3. runs `ExportWorker` through the `JobRunner` with a `threading.Event` cancel. Progress shows as a persistent toast `"Exporting… {done}/{total}"`; `Esc` while exporting cancels; completion toasts `"Exported {n} items to {filename}"`.

  Only one export runs at a time; a second request toasts `"An export is already running"`.
- **Welcome card** (`ui/overlays/welcome.py`, spec §8): shown on the player page when `catalog.provider_names() == []`, replacing the Phase 1B toast. It is a centred card 44u wide with:
  - "Welcome to QiTV" at 2.4u bold;
  - "Add your IPTV provider (STB portal, Xtream or M3U playlist) to start watching." at 1.2u and opacity 0.7;
  - an accent `Add provider` button that opens `OptionsDialog` on its provider page and emits `addRequested()`.

  It hides once a provider is ready.
- **`closeEvent`:** additionally cancels a running export, shuts down the `PosterCache`, and closes the library controller's pending search.

- [ ] **Step 1: Write failing tests** (offscreen; the local M3U fixture provider from Phase 1B Task 8, extended with a second, fake Xtream-shaped provider served by a local HTTP fixture that returns 2 VOD categories with 3 movies each and 1 series with 1 season of 2 episodes, using `file://` media):
  1. View → Movies opens the library page, stops live (the session state becomes STOPPED or IDLE) and hides the overlay host.
  2. Activating a movie switches to the player and plays it. Esc returns to the library with the same current item.
  3. *Live TV* re-tunes the last channel.
  4. Export → Cached Content with a patched `QFileDialog` writes the file, and the completion toast appears.
  5. With no providers configured, the welcome card is visible and its button emits `addRequested`.
  6. The Providers menu lists both providers and switching works.
  7. Every key in `SHORTCUTS` resolves to an action in the matching context (the shortcuts table cannot drift from the keymap).
- [ ] **Steps 2–5:** run RED, implement, run GREEN, run the suite, lint/type. Run the app manually once with the two local fixture providers: browse movies, play one, return, search, export. Describe what you saw in the report. Commit (`feat: integrate the library, menus, exports and welcome card`).

---

### Task 13: Delete the old UI and retarget its tests

**Files:**
- Delete: `channel_list.py`, `video_player.py`, `mixins/` (all), `widgets/sidebar.py`, `widgets/top_bar.py`, `widgets/menu_bar.py`, `widgets/delegates.py`, `widgets/autoplay_dialogs.py` (and `widgets/` if empty), plus `content_loader.py` if nothing else imports it. Keep `services/export.py` (the exporter uses it). Run `grep -rn` before deleting each one.
- Modify: `qitv-macos.spec`, `qitv-windows.spec`, `qitv-linux.spec` (hidden imports and datas that named the deleted modules; add `ui`, `ui.overlays`, `ui.library` and `services.live` packages if PyInstaller misses them), `pyproject.toml` (mypy/flake8 excludes that named deleted files), `AGENTS.md`.
- Retarget: `tests/test_catalog_refresh.py`, `tests/test_autoplay_next_season.py`, `tests/test_provider_urls.py`, `tests/test_thread_cleanup.py`, `tests/test_config_manager.py`, `tests/test_config_tv.py`, `tests/test_provider_api.py` (wherever they import the deleted modules).

**Behaviour:** for **every** test that imports a deleted module, write one line in the report saying which case applies:
- **(a) Retargeted:** the behaviour exists in a new module. Rewrite the test against that module (e.g. `test_autoplay_next_season` against `services.next_episode`; the HTTPS-preference and export URL tests against `services.provider_api` / `services.exporter`; the catalog freshness and cache-write tests against `CatalogService` and `ProviderManager`).
- **(b) Ported:** the behaviour lived only in the old UI but is still needed. Port the logic into the owning service with the test. Candidates: six-hour freshness and `is_content_stale` checks before refresh; pending cache writes not resurrecting an invalidated cache; same-name provider edits rejecting the old cache; a numeric sort that keeps display values.
- **(c) Obsolete:** the behaviour belongs to a UI concept that no longer exists (tree-widget sorting columns, retired logo loaders of the old list). Delete the test with a written reason.

Also:
- remove `Keyboard/Remote Mode` remnants and other dead config UI;
- run `uv run mypy services ui main.py config_manager.py epg_manager.py provider_manager.py` and fix new errors in touched code (record the baseline count before and after);
- build the macOS app once (`uv run pyinstaller qitv-macos.spec`), launch it with an isolated `QITV_CONFIG_DIR`, and confirm the TV window opens.

- [ ] **Steps:** inventory the imports, decide (a)/(b)/(c) per test, retarget/port with RED→GREEN where logic moves, delete, run the full suite three times in a row (to check for flakiness), lint/type, build and launch once, and commit (`refactor: remove the v1.13.8 windows and mixins`).

---

### Task 14: Version 2.0.0, README, release notes and screenshots

**Files:**
- Modify: `pyproject.toml` (version `2.0.0`), `uv.lock` (`uv lock`), `README.md`, `AGENTS.md`
- Create: `docs/release-notes-2.0.0.md`, `scripts/make_demo_content.py`, `docs/screenshots/*.png` (or whatever README image path the repo uses)
- Test: `uv run python -m unittest discover -s tests`; the README rendered and checked

**Behaviour:**
- **Version:** `2.0.0` in `pyproject.toml`. The PyInstaller specs read it already; verify the About box and the update checker comparison (`compare_versions("2.0.0", …)`).
- **`scripts/make_demo_content.py --out <dir>`** generates fictional demo content locally:
  - a local M3U with 24 fictional channels in 4 groups (names such as "Northwind News", "Harbor Sports 1");
  - generated logos (initials on coloured squares, PNG via QImage/QPainter);
  - a local fake Xtream-shaped server script for the library: 3 genres × 12 fictional titles, generated gradient posters with titles, 1 fictional series with 2 seasons;
  - synthetic video from `tests/fixtures/live.ts` (Phase 2B) or `tests/fixtures/smoke.mp4`.

  No real channels, posters or broadcast footage.
- **Screenshots**, captured with `QWidget.grab()` of the window, composited with the overlay host as the smoke test does, at 1600×900:
  1. live with the zap banner;
  2. the fast-zap column;
  3. the drawer with groups;
  4. time-shift with the transport bar behind live;
  5. the library grid with the detail strip;
  6. the series view;
  7. the guide.

  View each one, and re-take any with clipped text or demo artefacts.
- **README:** rewrite it for 2.0:
  - what QiTV is;
  - installing;
  - adding a provider;
  - a **Watching TV** section with the key table from `SHORTCUTS` (generated by a small helper once, then pasted);
  - time-shift (RAM by default, disk with consent, size slider);
  - live latency;
  - Library (Movies/Series, Continue, New, My list, search, A–Z);
  - the guide;
  - favorites;
  - exports;
  - portable mode;
  - external players;
  - the source build in a collapsed section.

  Keep the existing README's tone. Caption the screenshots as demo content. Remove anything MPV-specific.
- **Release notes** (`docs/release-notes-2.0.0.md`): highlights, keys, and these notes:
  - 2.0 replaces the v1.14 MPV line;
  - resume points of old **episodes** do not carry over (new episode ids, a Phase 1A ledger item);
  - the minimum OS is unchanged from v1.13.8;
  - the time-shift disk consent is new;
  - the "Keyboard/Remote Mode" setting is gone, because its behaviour is now the default.
- **AGENTS.md:** replace the "Current Work Plan" header line for 2.0 with the TV-shell architecture summary (one line per module group). Add a "Recent Changes" entry for 2.0 with the verification facts from this phase.

- [ ] **Steps:** bump the version and lock, write the demo script and take the screenshots, write the README and release notes, check the README rendering (headings, tables, image paths) with a local Markdown preview or `python -m markdown`, run the suite, and commit (`docs: prepare QiTV 2.0.0`).
