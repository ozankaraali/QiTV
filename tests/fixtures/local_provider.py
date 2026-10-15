"""A local M3U provider whose channels play tests/fixtures/smoke.mp4: no network, no credentials.

Used by the TV window tests, the TV smoke script and manual runs, always inside a temporary
QITV_CONFIG_DIR. Set QITV_CONFIG_DIR, call write_local_provider(dir), then build the managers
and call seed_channels(provider_manager, provider) before the window starts.

`fixture_dir` is where smoke.mp4 is (the packaged app has no fixtures of its own: the smoke
test passes the checkout's). With `live_url`, a third channel plays that live stream (the
smoke test's `tests/fixtures/live_source.LiveSource`), and each of `live_urls` one more
channel after it; pass the same values to both calls.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional, Sequence

FIXTURE_DIR = Path(__file__).resolve().parent
SMOKE_MP4 = FIXTURE_DIR / "smoke.mp4"
PROVIDER_NAME = "Local fixture"
GROUP = "Fixture"


def channel_urls(fixture_dir: str | os.PathLike = FIXTURE_DIR) -> list[str]:
    """Two file:// URLs of the same clip; they differ, so a zap changes the played URL."""
    directory = Path(fixture_dir).resolve()
    return [(directory / SMOKE_MP4.name).as_uri(), f"{directory.as_uri()}/./{SMOKE_MP4.name}"]


def channels(
    fixture_dir: str | os.PathLike = FIXTURE_DIR,
    live_url: Optional[str] = None,
    live_urls: Sequence[str] = (),
) -> list[dict]:
    """The playlist's entries as the M3U parser would store them."""
    urls = channel_urls(fixture_dir) + ([live_url] if live_url else []) + list(live_urls)
    return [
        {
            "id": number,
            "group": GROUP,
            "xmltv_id": None,
            "name": f"Channel {number}",
            "logo": None,
            "user_agent": None,
            "cmd": url,
            "tv_genre_id": GROUP,
            "number": str(number),
        }
        for number, url in enumerate(urls, start=1)
    ]


def write_local_provider(
    config_dir: str | os.PathLike,
    providers: Optional[list[dict]] = None,
    *,
    fixture_dir: str | os.PathLike = FIXTURE_DIR,
    live_url: Optional[str] = None,
    live_urls: Sequence[str] = (),
) -> dict:
    """Write the playlist, config.json and cache/provider/index.json under `config_dir`.

    Returns the fixture provider. `providers` replaces the provider list written to the index
    (pass [] for a first launch without providers).
    """
    root = Path(config_dir)
    root.mkdir(parents=True, exist_ok=True)
    playlist = root / "fixture.m3u"
    lines = ["#EXTM3U"]
    for channel in channels(fixture_dir, live_url, live_urls):
        lines += [f'#EXTINF:-1 group-title="{GROUP}",{channel["name"]}', channel["cmd"]]
    playlist.write_text("\n".join(lines) + "\n", encoding="utf-8")
    provider = {"type": "M3UPLAYLIST", "name": PROVIDER_NAME, "url": str(playlist)}
    # ProviderManager._load_providers reads cache/provider/index.json as a JSON list.
    index = root / "cache" / "provider" / "index.json"
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text(json.dumps([provider] if providers is None else providers), encoding="utf-8")
    config = {"selected_provider_name": PROVIDER_NAME, "check_updates": False}
    (root / "config.json").write_text(json.dumps(config), encoding="utf-8")
    return provider


def seed_channels(
    provider_manager,
    provider: dict,
    *,
    fixture_dir: str | os.PathLike = FIXTURE_DIR,
    live_url: Optional[str] = None,
    live_urls: Sequence[str] = (),
) -> None:
    """Store channels() as the provider's fresh live content (written by the cache writer).

    The M3U parser keeps only http(s) entries, so the file:// playlist alone loads no
    channels. The cached content stands in for the parsed playlist; it stays fresh for
    ProviderManager.CONTENT_CACHE_TTL.
    """
    contents = channels(fixture_dir, live_url, live_urls)
    provider_manager.current_provider = provider
    provider_manager.current_provider_content = {
        "itv": {
            "categories": [{"id": "*", "title": "All"}, {"id": GROUP, "title": GROUP}],
            "contents": contents,
            "sorted_channels": {GROUP: list(range(len(contents)))},
        }
    }
    provider_manager.mark_content_fresh("itv")
    provider_manager.save_provider()
