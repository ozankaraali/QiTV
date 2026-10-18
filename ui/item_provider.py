"""The provider a playing item belongs to (QiTV 2.1, mixed providers).

A catalog item is stamped with its provider, which need not be the one the page browses:
its stream, budget slot, limit, history record and STB retry follow the item. An unstamped
item (a local file, a test's channel) belongs to the browsed provider, as before 2.1.
"""

from __future__ import annotations

from typing import Optional

from services.catalog import ALL_GROUP_ID
from services.navigation import FAVORITES_GROUP, RECENT_GROUP
from services.provider_ids import ALL_PROVIDERS, provider_of


def pick_source(catalog, item: dict, group_key: str) -> str:
    """The TV page's source a channel of `group_key` is listed under: the browsed one for
    ★/⟲ and the mixed lists, else the channel's own provider (so a channel started from
    outside the browsed list, or zapped after browsing elsewhere, still reveals its list)."""
    browsed = catalog.source()
    provider = provider_of(item)
    if not provider or browsed == ALL_PROVIDERS or group_key in (FAVORITES_GROUP, RECENT_GROUP):
        return browsed
    return provider


def provider_name(catalog, item: dict) -> str:
    """The item's provider; the browsed one when the item is not stamped."""
    return provider_of(item) or catalog.current_provider_name()


def _browsed(catalog, item: dict) -> bool:
    name = provider_of(item)
    return not name or name == catalog.current_provider_name()


def provider_dict(catalog, item: dict) -> dict:
    """The item's provider settings; `{"name": ...}` alone when that provider is gone."""
    if _browsed(catalog, item):
        return catalog.current_provider()
    name = provider_of(item)
    try:
        return catalog.provider_entry(name)
    except KeyError:
        return {"name": name}


def provider_type(catalog, item: dict) -> str:
    """The item's provider type (STB, XTREAM, ...), upper case."""
    if _browsed(catalog, item):
        return catalog.provider_type()
    return str(provider_dict(catalog, item).get("type", "")).upper()


def max_connections(catalog, item: dict) -> Optional[int]:
    """What the item's provider reported as its simultaneous streams (None: unknown)."""
    if _browsed(catalog, item):
        return catalog.max_connections()
    return catalog.max_connections_of(provider_of(item))


def sign_in_again(catalog, item: dict) -> None:
    """An STB retry: the item's own portal signs in again before its next link. The browsed
    provider does not (as before 2.1: the next link it makes has a fresh play token)."""
    name = provider_of(item)
    if name and name != catalog.current_provider_name():
        catalog.sign_in_again(name)
