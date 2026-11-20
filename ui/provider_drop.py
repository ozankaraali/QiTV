"""Providers dropped on the TV window (QiTV 2.2): a playlist file, or a provider's link.

A dropped .m3u or .m3u8 file with a network link in it becomes an M3U playlist provider reading
that file (the window reads it first: services/local_playlist.py; one of local files plays as a
list, an HLS one as one film). A dropped link
(a URL dragged from a browser, or text) is read as the add-provider form's Paste link reads it
(services.provider_link): an Xtream link with its login, or a playlist's address, becomes a
provider at once; a portal's address needs its MAC, so it opens the add-provider form filled
in, unless the link names its MAC. The window adds them and says so in a toast
(TVWindow.add_dropped_providers). Subtitle files dropped with them still go to what plays.

Pure functions over paths, texts and provider dicts (no widgets); nothing here logs a link.
`same_provider`, `normal_address` and `unique_name` are services.provider_changes' now.
"""

from __future__ import annotations

from collections.abc import Iterable
import os
from typing import Optional

from services.provider_changes import normal_address, same_provider, unique_name  # noqa: F401
from services.provider_link import M3U, STB, XTREAM, ProviderLink, parse_provider_link

PLAYLIST_EXTENSIONS = (".m3u", ".m3u8")


def playlist_files(paths: Iterable[str]) -> list[str]:
    """The playlist files among dropped local `paths` (.m3u, .m3u8)."""
    return [p for p in paths if p and os.path.splitext(p)[1].lower() in PLAYLIST_EXTENSIONS]


def links(texts: Iterable[str]) -> list[ProviderLink]:
    """The provider links among dropped texts (a URL each, or lines of text), once each."""
    found: dict[tuple[str, str, str], ProviderLink] = {}
    for text in texts:
        for line in str(text or "").splitlines():
            link = parse_provider_link(line)
            if link is not None:
                found.setdefault((link.type, link.url, link.username), link)
    return list(found.values())


def file_entry(path: str) -> dict:
    """A provider for a playlist file: named after the file."""
    name = os.path.splitext(os.path.basename(path))[0] or path
    return {"type": M3U, "name": name, "url": path}


def link_entry(link: ProviderLink) -> Optional[dict]:
    """A provider for a link; None for a portal without its MAC (the form asks for it)."""
    entry = {"type": link.type, "name": link.name or link.url, "url": link.url}
    if link.type == XTREAM:
        entry.update(username=link.username, password=link.password)
    elif link.type == STB:
        if not link.mac:
            return None
        entry.update(mac=link.mac, serial_number="", device_id="")
    return entry
