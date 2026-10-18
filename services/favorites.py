"""Favorites: stars for channels and titles alike.

A star is a name (or the title when an item has no name) at one provider, kept in
`config.provider_favorites`; `config.favorites` stays a list of the starred names, the only
thing builds before 2.1 read (and they fail on anything but strings). Stars of earlier
versions are bare names that match at every provider (see entries and migrate). NavModel
(the pinned Favorites group), the Library (My list) and the player (Ctrl+D, the context
menu) all use these helpers.
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
    provider. They live in `config.provider_favorites`; until that exists, the bare names of
    `config.favorites` read as old stars.
    """
    stored = getattr(config, "provider_favorites", None)
    if stored is None:
        stored = config.favorites or []
    return [e for e in map(_normal, stored) if e is not None]


def _store(config, kept: list[dict]) -> None:
    """Write the entries, and the starred names to `favorites`: older builds read only that
    (a list of strings; they fail on anything else)."""
    config.provider_favorites = kept
    config.favorites = names(config)


def names(config) -> list[str]:
    """The starred names (each once), in the order they were added: a name starred at any
    provider counts. Older builds read this list as stars at every provider; they cannot be
    told where, and a name left out would read as un-starred there (see migrate)."""
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
    un-stars by name at every provider (it stars as "*"). An item with neither a name nor a
    title is left alone (False). The config is not saved here: see save().
    """
    key = favorite_key(item)
    if not key:
        return False
    provider = provider_of(item)
    raw = entries(config)
    if not is_favorite(config, item):
        if not provider:
            _store(config, [*raw, {"name": key, "provider": ANY_PROVIDER}])
            return True
        # Starring again where an old star was switched off just lifts the exclusion.
        lifted = [e for e in raw if not _is_entry(e, key, provider, True)]
        if len(lifted) != len(raw):
            _store(config, lifted)
        else:
            _store(config, [*raw, {"name": key, "provider": provider}])
        return True
    if not provider:
        _store(config, [e for e in raw if e["name"] != key])
        return False
    kept = [e for e in raw if not _is_entry(e, key, provider, False)]
    if any(_is_entry(e, key, ANY_PROVIDER, False) for e in kept):
        kept.append({"name": key, "provider": provider, "off": True})
    _store(config, kept)
    return False


def migrate(config) -> bool:
    """Bring the stars up to date at start; True when the config changed (save it then).

    The first time, `provider_favorites` starts from the bare names (each an old star, "*").
    Later, names an older build starred or un-starred since (it writes `favorites` only) are
    taken in: starred at every provider, or un-starred at every one. `favorites` itself stays
    a list of names, which is all older builds read (entries an earlier 2.1 build wrote there
    become names again).
    """
    changed = False
    if getattr(config, "provider_favorites", None) is None:
        config.provider_favorites = entries(config)
        changed = True
    if any(not isinstance(e, str) for e in config.favorites or []):
        config.favorites = names(config)  # entries an earlier build wrote there: names only
        changed = True
    elif _take_older_builds_edits(config):
        changed = True
    return changed


def _take_older_builds_edits(config) -> bool:
    """`favorites` names other stars than `provider_favorites` (it is written from them):
    an older build changed it. Its new names are starred at every provider (their exclusions
    go), and the names it dropped are un-starred at every provider."""
    written = [name for name in config.favorites or [] if name]
    listed = set(names(config))
    if set(written) == listed:
        return False  # the same stars (maybe in another order)
    added = [name for name in dict.fromkeys(written) if name not in listed]
    kept = [e for e in entries(config) if e["name"] in listed.intersection(written)]
    _store(config, kept + [{"name": name, "provider": ANY_PROVIDER} for name in added])
    return True


def save(config) -> bool:
    """Write the config; False (and a warning) when that failed."""
    try:
        config.save_config()
    except Exception as e:
        logger.warning("Couldn't save the favorites: %s", safe_message(e))
        return False
    return True
