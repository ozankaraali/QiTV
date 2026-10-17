"""Favorites: stars in `config.favorites`, for channels and titles alike.

A star is a name (or the title when an item has no name) at one provider; stars of earlier
versions are bare names that match at every provider (see entries and migrate). NavModel (the pinned Favorites group), the
Library (My list) and the player (Ctrl+D, the context menu) all use these helpers.
"""

from __future__ import annotations

import logging

from services.provider_ids import provider_of
from services.redact import safe_message

logger = logging.getLogger(__name__)


def favorite_key(item: dict) -> str:
    """How `item` is kept in the list: its name, else its title ("" when it has neither)."""
    return str(item.get("name") or item.get("title") or "")


ANY_PROVIDER = "*"


def _normal(entry) -> dict | None:
    """One stored entry as {"name", "provider"[, "off"]}; a bare string is an old star."""
    if isinstance(entry, str):
        return {"name": entry, "provider": ANY_PROVIDER} if entry else None
    if isinstance(entry, dict) and entry.get("name"):
        out = {"name": str(entry["name"]), "provider": str(entry.get("provider") or ANY_PROVIDER)}
        if entry.get("off"):
            out["off"] = True
        return out
    return None


def entries(config) -> list[dict]:
    """The stored entries as dicts, in the order they were added.

    {"name", "provider"} is a star at one provider; provider "*" is an old star that matches
    that name at every provider; {"name", "provider", "off": True} un-stars an old star at that
    provider. Bare strings (a config not migrated yet) read as old stars.
    """
    return [e for e in map(_normal, config.favorites or []) if e is not None]


def names(config) -> list[str]:
    """The starred names (each once), in the order they were added."""
    return list(dict.fromkeys(e["name"] for e in entries(config) if not e.get("off")))


def fingerprint(config) -> tuple:
    """Changes whenever a star is added, removed or moved to another provider."""
    return tuple((e["name"], e["provider"], bool(e.get("off"))) for e in entries(config))


def matcher(config):
    """A function item -> bool for many items at once (None when nothing is starred)."""
    every = entries(config)
    live = [e for e in every if not e.get("off")]
    if not live:
        return None
    off = {(e["name"], e["provider"]) for e in every if e.get("off")}
    stars = {(e["name"], e["provider"]) for e in live}
    named = {e["name"] for e in live}

    def check(item: dict) -> bool:
        key = favorite_key(item)
        if not key or key not in named:
            return False
        provider = provider_of(item)
        if not provider:
            return True  # not stamped: by name, as before
        if (key, provider) in stars:
            return True
        return (key, ANY_PROVIDER) in stars and (key, provider) not in off

    return check


def is_favorite(config, item: dict) -> bool:
    check = matcher(config)
    return check is not None and check(item)


def _is_entry(entry, key: str, provider: str, off: bool) -> bool:
    e = _normal(entry)
    return bool(e and e["name"] == key and e["provider"] == provider and bool(e.get("off")) == off)


def toggle(config, item: dict) -> bool:
    """Add `item` to the favorites or remove it; True when it is a favorite now.

    A star belongs to the item's provider; an old star ("*") is un-starred at one provider with
    an "off" entry, so the others keep it. An item with no provider (not stamped) stars and
    un-stars by name at every provider, and is kept as a bare name, as before. An item with
    neither a name nor a title is left alone (False). The config is not saved here: see save().
    """
    key = favorite_key(item)
    if not key:
        return False
    provider = provider_of(item)
    raw = list(config.favorites or [])
    if not is_favorite(config, item):
        if not provider:
            config.favorites = [*raw, key]
            return True
        # Starring again where an old star was switched off just lifts the exclusion.
        lifted = [e for e in raw if not _is_entry(e, key, provider, True)]
        if len(lifted) != len(raw):
            config.favorites = lifted
        else:
            config.favorites = [*raw, {"name": key, "provider": provider}]
        return True
    if not provider:
        config.favorites = [e for e in raw if (_normal(e) or {}).get("name") != key]
        return False
    kept = [e for e in raw if not _is_entry(e, key, provider, False)]
    if any(_is_entry(e, key, ANY_PROVIDER, False) for e in kept):
        kept.append({"name": key, "provider": provider, "off": True})
    config.favorites = kept
    return False


def migrate(config) -> bool:
    """Turn bare names into {"name", "provider": "*"}; True when anything changed."""
    raw = list(config.favorites or [])
    if not any(isinstance(e, str) for e in raw):
        return False
    config.favorites = [_normal(e) if isinstance(e, str) else e for e in raw if _normal(e)]
    return True


def save(config) -> bool:
    """Write the config; False (and a warning) when that failed."""
    try:
        config.save_config()
    except Exception as e:
        logger.warning("Couldn't save the favorites: %s", safe_message(e))
        return False
    return True
