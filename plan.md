# QiTV Codebase Refinement Plan

## Overview
Simplify and refine the QiTV codebase by reducing boilerplate, extracting helpers, and consolidating duplicated patterns.

**Reference:** Codebase analysis from exploration

---

## Task List

```json
[
  {
    "category": "refactor",
    "description": "Extract config property factory in config_manager.py",
    "steps": [
      "Create _config_property() factory function that generates getter/setter",
      "Replace all repetitive @property definitions with factory calls",
      "Verify all config values still load/save correctly",
      "Run the app to confirm settings work"
    ],
    "passes": true
  },
  {
    "category": "refactor",
    "description": "Extract seek helper in video_player.py",
    "steps": [
      "Create _try_seek(target_ms, fallback_pos) helper method",
      "Replace 4 duplicated try/except seek patterns with helper calls",
      "Extract magic numbers (60ms, 140ms) into named constants",
      "Test that seeking still works in the video player"
    ],
    "passes": true
  },
  {
    "category": "refactor",
    "description": "Simplify widget show/hide cascade in options.py",
    "steps": [
      "Create WidgetGroup helper class with show()/hide() methods",
      "Group related EPG widgets into WidgetGroup instances",
      "Replace verbose show/hide calls in on_epg_source_changed()",
      "Verify EPG source switching still works in options dialog"
    ],
    "passes": true
  },
  {
    "category": "refactor",
    "description": "Consolidate image cache operations in image_manager.py",
    "steps": [
      "Create _get_cached() helper for cache lookup with LRU update",
      "Create _store_cached() helper for cache storage with eviction",
      "Refactor get_image_from_base64, get_image_from_url, cache_image_from_url to use helpers",
      "Verify images still load and cache correctly"
    ],
    "passes": true
  },
  {
    "category": "refactor",
    "description": "Create Xtream URL builder factory in provider_api.py",
    "steps": [
      "Create _build_xtream_url(base, endpoint, username, password, extra) helper",
      "Refactor xtream_player_api_url, xtream_live_streams_url, etc. to use factory",
      "Keep same function signatures for backwards compatibility",
      "Verify Xtream provider still works"
    ],
    "passes": true
  },
  {
    "category": "refactor",
    "description": "Consolidate export functions in services/export.py",
    "steps": [
      "Extract common export logic into _write_export_file() helper",
      "Refactor save_m3u_content() and save_stb_content() to use helper",
      "Pass URL formatter as callback parameter",
      "Test export functionality still works"
    ],
    "passes": true
  },
  {
    "category": "cleanup",
    "description": "Standardize json imports across codebase",
    "steps": [
      "Change image_manager.py to use orjson instead of standard json",
      "Verify no other files use standard json inconsistently",
      "Test image caching still works"
    ],
    "passes": true
  },
  {
    "category": "cleanup",
    "description": "Extract resize edge constants in video_player.py",
    "steps": [
      "Create RESIZE_EDGE_PIXELS constant (currently hardcoded as 10)",
      "Replace all hardcoded 10-pixel checks with constant",
      "Add brief comment explaining the constant"
    ],
    "passes": true
  }
]
```

---

## Agent Instructions

1. Read `activity.md` first to understand current state
2. Find next task with `"passes": false`
3. Complete all steps for that task
4. Verify by running: `python main.py` (start app, check it loads)
5. Update task to `"passes": true`
6. Log completion in `activity.md`
7. Make a git commit for that task
8. Repeat until all tasks pass

**Important:** Only modify the `passes` field. Do not remove or rewrite tasks.

---

## Completion Criteria
All tasks marked with `"passes": true`
