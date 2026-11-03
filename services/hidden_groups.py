"""Groups hidden to tidy the lists (2.9.1): a live group or a Library genre the viewer doesn't
want to see, without a PIN.

A provider may list hundreds of groups in languages nobody here watches. "Hide group" (a
group's menu in the channel list, a genre's in the Library) leaves it out of the lists of
groups that read a browsed catalog (the channel list, the guide's, the Library's genres, the
phone's lists), at once and for good, until Settings → Providers → Hidden groups shows it
again. Unlike parental controls (services.parental) nothing is locked and nothing else
changes: its channels and titles still come up in searches, All channels, the history and
Continue, and play as before.

The config keeps {"version": 1, "groups": [{"key", "section", "provider", "title"}]} as
`hidden_groups` (the keys as parental controls make them); older builds ignore it.
"""

from __future__ import annotations

import logging
from typing import Any

from services.parental import group_key

logger = logging.getLogger(__name__)

VERSION = 1
CONFIG_KEY = "hidden_groups"


class HiddenGroups:
    def __init__(self, config: Any) -> None:
        self._config = config
        self.version = 0  # changes whenever what is hidden changes (lists rebuild)
        self._keys: set[str] = set()
        self._load()

    def _data(self) -> dict:
        data = getattr(self._config, CONFIG_KEY, None)
        if isinstance(data, dict) and data.get("version") == VERSION:
            return data
        return {"version": VERSION, "groups": []}

    def _load(self) -> None:
        self._keys = {
            str(g["key"]) for g in self.entries() if isinstance(g.get("key"), str) and g["key"]
        }
        self.version += 1

    def entries(self) -> list[dict]:
        """What is hidden, as kept (the newest last)."""
        groups = self._data().get("groups")
        return [dict(g) for g in groups or [] if isinstance(g, dict) and g.get("key")]

    def _store(self, groups: list[dict]) -> None:
        setattr(self._config, CONFIG_KEY, {"version": VERSION, "groups": groups})
        try:
            self._config.save_config()
        except Exception as e:  # kept for this run
            logger.warning("The hidden groups could not be saved: %s", type(e).__name__)
        self._load()

    def hide(self, section: str, provider: str, group_id: str, title: str = "") -> bool:
        """Hide the group; False when it was hidden already."""
        key = group_key(section, provider, group_id)
        if key in self._keys:
            return False
        entry = {"key": key, "section": section, "provider": provider}
        self._store(self.entries() + [{**entry, "title": title or str(group_id)}])
        return True

    def show_again(self, key: str) -> bool:
        kept = [g for g in self.entries() if g.get("key") != key]
        if len(kept) == len(self.entries()):
            return False
        self._store(kept)
        return True

    def hides(self, section: str, provider: str, group_id: str) -> bool:
        return bool(self._keys) and group_key(section, provider, str(group_id)) in self._keys

    def rename_providers(self, renames: Any) -> bool:
        """A provider renamed in Settings (services.provider_rename) keeps its hidden groups."""
        names = getattr(renames, "names", {}) or {}
        changed = False
        groups = []
        for entry in self.entries():
            new = names.get(entry.get("provider"))
            if new:
                group_id = str(entry["key"]).rsplit("\x1f", 1)[-1]
                entry = {
                    **entry,
                    "provider": new,
                    "key": group_key(str(entry.get("section")), new, group_id),
                }
                changed = True
            groups.append(entry)
        if changed:
            self._store(groups)
        return changed
