"""Providers and cached catalogs for the mixed-providers tests (a helper, not a test module).

Everything is fictional and points at 127.0.0.1: nothing here reaches a network.
"""

import os
import time
from unittest import mock

from job_fixtures import ImmediateRunner
import orjson

from services.config_manager import ConfigManager
from services.provider_manager import ProviderManager

DEMO = {"type": "M3UPLAYLIST", "name": "Demo TV", "url": "http://127.0.0.1:9/demo.m3u"}
HARBOR = {
    "type": "XTREAM",
    "name": "Harbor IPTV",
    "url": "http://127.0.0.1:9",
    "username": "harbor",
    "password": "secret",
}


def live_content(names: list[str], groups: dict[str, list[int]]) -> dict:
    """A live section payload: `names` are the channels, `groups` maps a group title to the
    positions (in `names`) of its channels."""
    contents = [
        {
            "id": position + 1,
            "number": str(position + 1),
            "name": name,
            "cmd": f"http://127.0.0.1:9/live/{position + 1}.ts",
        }
        for position, name in enumerate(names)
    ]
    categories = [{"id": "*", "title": "All"}]
    categories += [{"id": title, "title": title} for title in groups]
    return {
        "categories": categories,
        "contents": contents,
        "sorted_channels": {title: list(positions) for title, positions in groups.items()},
    }


def mixed_config(
    tmpdir: str, providers: list[dict], contents: dict[str, dict]
) -> tuple[ConfigManager, ProviderManager]:
    """A ConfigManager in `tmpdir` and a ProviderManager over `providers`, each provider's
    content already cached on disk (valid identity, fresh).

    `contents` maps a provider name to its cache by content type ({"itv": live_content(...)});
    a bare live section payload is taken as the "itv" entry. No provider is selected.
    """
    with mock.patch.dict(os.environ, {"QITV_CONFIG_DIR": tmpdir}):
        config = ConfigManager()
    config.selected_provider_name = ""
    manager = ProviderManager(config)
    manager.providers = [dict(p) for p in providers]
    manager.save_providers()
    for provider in manager.providers:
        content = contents.get(provider["name"])
        if content is None:
            continue
        if "categories" in content:
            content = {"itv": content}
        cache = dict(content)
        cache["_cache"] = {
            "identity": manager.provider_cache_identity(provider),
            "refreshed_at": {key: time.time() for key in content},
        }
        with open(manager._provider_cache_name(provider["name"]), "wb") as f:
            f.write(orjson.dumps(cache))
    return config, manager


class HeldSetups(ImmediateRunner):
    """Runs every job at once, except the setups of the providers named, which wait for
    release(name): one provider loads after the others."""

    def __init__(self, *held: str):
        self.held = set(held)
        self.waiting: list[tuple] = []

    def run_worker(self, worker, on_finished, on_error=None, context=None):
        if getattr(context, "kind", "") == "provider" and context.data[0] in self.held:
            self.waiting.append((worker, on_finished, on_error, context))
            return worker
        return super().run_worker(worker, on_finished, on_error, context)

    def release(self, name: str) -> None:
        self.held.discard(name)
        jobs = [job for job in self.waiting if job[3].data[0] == name]
        self.waiting = [job for job in self.waiting if job[3].data[0] != name]
        for worker, on_finished, on_error, context in jobs:
            super().run_worker(worker, on_finished, on_error, context)
