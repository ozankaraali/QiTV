"""Favorites: one list of names in `config.favorites`, for channels and titles alike.

The format is v1.13.8's, so existing configs keep their favorites: an item is kept by its
name, or by its title when it has no name. NavModel (the pinned Favorites group), the
Library (My list) and the player (Ctrl+D, the context menu) all use these helpers.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def favorite_key(item: dict) -> str:
    """How `item` is kept in the list: its name, else its title ("" when it has neither)."""
    return str(item.get("name") or item.get("title") or "")


def names(config) -> list[str]:
    """The stored names, in the order they were added."""
    return list(config.favorites or [])


def is_favorite(config, item: dict) -> bool:
    key = favorite_key(item)
    return bool(key) and key in names(config)


def toggle(config, item: dict) -> bool:
    """Add `item` to the favorites or remove it; True when it is a favorite now.

    An item with neither a name nor a title is left alone (False). The config is not saved
    here: see save().
    """
    key = favorite_key(item)
    if not key:
        return False
    stored = names(config)
    added = key not in stored
    config.favorites = [*stored, key] if added else [name for name in stored if name != key]
    return added


def save(config) -> bool:
    """Write the config; False (and a warning) when that failed."""
    try:
        config.save_config()
    except Exception as e:
        logger.warning("Couldn't save the favorites (%s)", type(e).__name__)
        return False
    return True
