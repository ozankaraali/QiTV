"""A local M3U provider whose channels play tests/fixtures/smoke.mp4: no network, no credentials.

Used by the TV window tests, the TV smoke script and manual runs, always inside a temporary
QITV_CONFIG_DIR. Set QITV_CONFIG_DIR, call write_local_provider(dir), then build the managers
and call seed_channels(provider_manager, provider) before the window starts.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

FIXTURE_DIR = Path(__file__).resolve().parent
SMOKE_MP4 = FIXTURE_DIR / "smoke.mp4"
PROVIDER_NAME = "Local fixture"
GROUP = "Fixture"


def channel_urls() -> list[str]:
    """Two file:// URLs of the same clip; they differ, so a zap changes the played URL."""
    return [SMOKE_MP4.as_uri(), f"{FIXTURE_DIR.as_uri()}/./{SMOKE_MP4.name}"]


def channels() -> list[dict]:
    """The playlist's entries as the M3U parser would store them."""
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
        for number, url in enumerate(channel_urls(), start=1)
    ]


def write_local_provider(
    config_dir: str | os.PathLike, providers: Optional[list[dict]] = None
) -> dict:
    """Write the playlist, config.json and cache/provider/index.json under `config_dir`.

    Returns the fixture provider. `providers` replaces the provider list written to the index
    (pass [] for a first launch without providers).
    """
    root = Path(config_dir)
    root.mkdir(parents=True, exist_ok=True)
    playlist = root / "fixture.m3u"
    lines = ["#EXTM3U"]
    for channel in channels():
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


def seed_channels(provider_manager, provider: dict) -> None:
    """Store channels() as the provider's fresh live content (written by the cache writer).

    The M3U parser keeps only http(s) entries, so the file:// playlist alone loads no
    channels. The cached content stands in for the parsed playlist; it stays fresh for
    ProviderManager.CONTENT_CACHE_TTL.
    """
    provider_manager.current_provider = provider
    provider_manager.current_provider_content = {
        "itv": {
            "categories": [{"id": "*", "title": "All"}, {"id": GROUP, "title": GROUP}],
            "contents": channels(),
            "sorted_channels": {GROUP: [0, 1]},
        }
    }
    provider_manager.mark_content_fresh("itv")
    provider_manager.save_provider()
