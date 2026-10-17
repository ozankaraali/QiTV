"""Which provider an item came from (QiTV 2.1, mixed providers).

Every catalog item carries its provider's name under PROVIDER_KEY, so channels, films and
episodes from several providers can share one list and still play, favorite and resume at
the right one.
"""

from __future__ import annotations

from typing import Iterable

PROVIDER_KEY = "_provider"
ALL_PROVIDERS = "@all"
ALL_PROVIDERS_TITLE = "All providers"

_GROUP_SEPARATOR = "\x1f"


def provider_of(item: dict) -> str:
    """The provider an item came from; "" when it is not stamped."""
    return str(item.get(PROVIDER_KEY) or "")


def stamp(items: Iterable[dict], provider: str) -> None:
    """Mark items as coming from `provider`, in place (an earlier stamp is overwritten)."""
    for item in items:
        item[PROVIDER_KEY] = provider


def with_provider(item: dict, provider: str) -> dict:
    """A stamped shallow copy: history and search results keep the stored item untouched."""
    copy = dict(item)
    copy[PROVIDER_KEY] = provider
    return copy


def mixed_group_id(provider: str, group_id: str) -> str:
    """One group of one provider inside a list that mixes providers."""
    return f"{provider}{_GROUP_SEPARATOR}{group_id}"


def split_group_id(key: str) -> tuple[str, str]:
    """(provider, group id) of a mixed group id; ("", key) for an ordinary one."""
    provider, separator, group_id = key.partition(_GROUP_SEPARATOR)
    if not separator:
        return "", key
    return provider, group_id
