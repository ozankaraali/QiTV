"""Whether an edited provider is still the same one (Settings, "Address and login").

A pasted or typed link can turn a provider into another one (an Xtream provider given a
stream link became an "M3U Stream", and its lists were lost), so the UI asks first. This is
the pure decision; the question itself is the UI's.
"""

from __future__ import annotations

from services.provider_changes import normal_address
from services.provider_types import is_media_server

SAME = "SAME"
OTHER_KIND = "OTHER_KIND"
OTHER_HOST = "OTHER_HOST"
OTHER_ACCOUNT = "OTHER_ACCOUNT"

_PLAYLISTS = ("M3UPLAYLIST", "M3USTREAM")


def _kind(provider: dict) -> str:
    return str(provider.get("type") or "").upper()


def _host(provider: dict) -> str:
    """The server as compared: a playlist's whole normal address, otherwise its host (and
    non-default port) without the path."""
    address = normal_address(str(provider.get("url") or ""))
    if _kind(provider) in _PLAYLISTS or not address.startswith("//"):
        return address
    return address[2:].split("/", 1)[0].split("?", 1)[0]


def _text(provider: dict, key: str) -> str:
    return str(provider.get(key) or "").strip()


def judge(old: dict, new: dict) -> str:
    """How `new` relates to `old`: SAME, OTHER_KIND (another type), OTHER_HOST (another
    server; for a media server the host with the user name) or OTHER_ACCOUNT (the same
    server with another user name, or a portal with another MAC). A password, a name or a
    path after the host never decides."""
    if _kind(old) != _kind(new):
        return OTHER_KIND
    if _host(old) != _host(new):
        return OTHER_HOST
    kind = _kind(old)
    if kind in _PLAYLISTS:
        return SAME
    if _text(old, "username").casefold() != _text(new, "username").casefold():
        return OTHER_HOST if is_media_server(kind) else OTHER_ACCOUNT
    if kind == "STB" and _text(old, "mac").casefold() != _text(new, "mac").casefold():
        return OTHER_ACCOUNT
    return SAME
