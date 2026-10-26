"""The provider a playing item belongs to (QiTV 2.1, mixed providers).

A catalog item is stamped with its provider, which need not be the one the page browses:
its stream, budget slot, limit, history record and STB retry follow the item. An unstamped
item (a local file, a test's channel) belongs to the browsed provider, as before 2.1.
"""

from __future__ import annotations

from datetime import tzinfo
from typing import Optional, cast
from urllib.parse import urlsplit

from services import vlc_profile
from services.navigation import FAVORITES_GROUP, RECENT_GROUP
from services.provider_ids import ALL_PROVIDERS, provider_of


def pick_source(catalog, item: dict, group_key: str) -> str:
    """The TV page's source a channel of `group_key` is listed under: the browsed one for
    ★/⟲ and the mixed lists, else the channel's own provider (so a channel started from
    outside the browsed list, or zapped after browsing elsewhere, still reveals its list)."""
    browsed = catalog.source()
    provider = provider_of(item)
    if not provider or browsed == ALL_PROVIDERS or group_key in (FAVORITES_GROUP, RECENT_GROUP):
        return cast(str, browsed)
    return provider


def provider_name(catalog, item: dict) -> str:
    """The item's provider; the browsed one when the item is not stamped."""
    return provider_of(item) or cast(str, catalog.current_provider_name())


def _browsed(catalog, item: dict) -> bool:
    name = provider_of(item)
    return not name or name == catalog.current_provider_name()


def provider_dict(catalog, item: dict) -> dict:
    """The item's provider settings; `{"name": ...}` alone when that provider is gone."""
    if _browsed(catalog, item):
        return cast(dict, catalog.current_provider())
    name = provider_of(item)
    try:
        return cast(dict, catalog.provider_entry(name))
    except KeyError:
        return {"name": name}


def provider_type(catalog, item: dict) -> str:
    """The item's provider type (STB, XTREAM, ...), upper case."""
    if _browsed(catalog, item):
        return cast(str, catalog.provider_type())
    return str(provider_dict(catalog, item).get("type", "")).upper()


def stream_agent(catalog, item: dict) -> Optional[str]:
    """The user agent the item's stream is asked for with (vlc_profile.stream_agent): its own,
    an STB portal's box's (2.8), or None for VLC's own."""
    return vlc_profile.stream_agent(item, provider_type(catalog, item))


def user_agent(catalog, item: dict) -> str:
    """`stream_agent`, or VLC's own: what the time-shift helper and the probes send."""
    return vlc_profile.user_agent_for(item, provider_type(catalog, item))


def max_connections(catalog, item: dict) -> Optional[int]:
    """What the item's provider reported as its simultaneous streams (None: unknown)."""
    if _browsed(catalog, item):
        return cast(Optional[int], catalog.max_connections())
    return cast(Optional[int], catalog.max_connections_of(provider_of(item)))


def archive_zone(catalog, item: dict) -> Optional[tzinfo]:
    """The time zone the item's provider's archive addresses are in (catch-up; None: not
    known)."""
    if _browsed(catalog, item):
        zone = getattr(catalog, "archive_zone", None)
        return zone() if zone is not None else None
    zone_of = getattr(catalog, "archive_zone_of", None)
    return zone_of(provider_of(item)) if zone_of is not None else None


def xtream_zone_at(catalog, url: str) -> Optional[tzinfo]:
    """The time zone of a signed-in provider whose streams are on `url`'s server (an M3U
    playlist's xc channel's), when one knows it."""
    find = getattr(catalog, "xtream_zone_at", None)
    try:
        host = urlsplit(str(url or "")).netloc.lower()
    except ValueError:
        return None
    return find(host) if find is not None and host else None


def playlist_source(catalog, item: dict) -> str:
    """The item's playlist's catch-up template for every channel ("" for none)."""
    if _browsed(catalog, item):
        source = getattr(catalog, "catchup_source", None)
        return str(source() or "") if source is not None else ""
    source_of = getattr(catalog, "catchup_source_of", None)
    return str(source_of(provider_of(item)) or "") if source_of is not None else ""


def sign_in_again(catalog, item: dict) -> None:
    """An STB retry: the item's own portal signs in again before its next link. The browsed
    provider does not (as before 2.1: the next link it makes has a fresh play token), nor one
    that is not set up now: its sign-in is under way (the other picture's retry, say), and the
    next link waits for it, or the link asks for one."""
    name = provider_of(item)
    if not name or name == catalog.current_provider_name():
        return
    hub = getattr(catalog, "catalogs", None)
    if hub is not None and not hub.is_ready(name):
        return
    catalog.sign_in_again(name)
