"""Where each Plex server is reached now, and its pictures' addresses (2.14; no Qt, no network).

A Plex provider keeps its server's connections (at home, remote, relayed) with the address it
was reached at when it was signed in (`url`). The workers ask the server at the address it was
last reached at (`base_of`): when it can't be reached there (a laptop away from home, a router
that gave the server a new address), they look for it among its connections again and tell
where they found it (`moved`), and a list's load looks for a better one whenever it is not at
its best (back home: at home again, not through Plex's relay).

A title's picture is kept as `plex-image://<server id><path>`, never as an address: it holds no
token (a list or the watch history in config.json keeps none), and it stays the same whatever
address or token the server has, so the image cache keeps it. It becomes an address only when
it is fetched (`resolve_image`): the server's picture resizer, at Jellyfin's size (a poster
360 px high), at the server's address now, with its token.

Thread-safe: the GUI thread tells the providers (`known`), the workers and the image loaders'
threads read and move.
"""

from __future__ import annotations

import threading
from typing import Any, Iterable, Optional
from urllib.parse import urlencode

IMAGE_SCHEME = "plex-image"
_PREFIX = IMAGE_SCHEME + "://"
POSTER_W, POSTER_H = 240, 360  # as media_server.image_url's maxHeight=360

_lock = threading.Lock()
# server id -> {"stored": its provider's url, "base": where it is reached now, "token": …}
_servers: dict[str, dict[str, str]] = {}


def known(providers: Iterable[Any]) -> None:
    """The Plex providers as listed now (the GUI thread, as they change). A server signed in
    again (another stored address) is reached there first; one moved meanwhile stays where
    it was found; one no longer listed is forgotten."""
    seen: set[str] = set()
    with _lock:
        for provider in providers:
            if not isinstance(provider, dict) or str(provider.get("type") or "").upper() != "PLEX":
                continue
            server_id = str(provider.get("server_id") or "")
            url = str(provider.get("url") or "").rstrip("/")
            if not server_id or not url:
                continue
            seen.add(server_id)
            held = _servers.get(server_id)
            base = held["base"] if held and held["stored"] == url else url
            _servers[server_id] = {
                "stored": url,
                "base": base,
                "token": str(provider.get("token") or ""),
            }
        for server_id in [s for s in _servers if s not in seen]:
            del _servers[server_id]


def moved(server_id: str, base: str) -> None:
    """A worker found the server at `base`: it is asked there from now on."""
    if not server_id or not base:
        return
    with _lock:
        held = _servers.get(server_id)
        if held is not None:
            held["base"] = base.rstrip("/")


def base_of(provider: dict) -> str:
    """Where provider `provider`'s server is reached now (its stored address until moved)."""
    url = str(provider.get("url") or "").rstrip("/")
    with _lock:
        held = _servers.get(str(provider.get("server_id") or ""))
        if held is not None and held["stored"] == url:
            return held["base"]
    return url


def forget() -> None:
    """Every server (tests)."""
    with _lock:
        _servers.clear()


def image_ref(server_id: str, path: Any) -> str:
    """A picture of the server by its path (`/library/metadata/…/thumb/…`): "" for none."""
    path = str(path or "")
    if not server_id or not path.startswith("/"):
        return ""
    return f"{_PREFIX}{server_id}{path}"


def is_image_ref(url: Any) -> bool:
    return isinstance(url, str) and url.startswith(_PREFIX)


def resolve_image(url: str) -> Optional[str]:
    """The address a picture is fetched from now (the server's resizer, with its token);
    None for a server not listed (removed, or not known yet)."""
    if not is_image_ref(url):
        return None
    rest = url[len(_PREFIX) :]
    server_id, slash, path = rest.partition("/")
    if not slash:
        return None
    with _lock:
        held = dict(_servers.get(server_id) or {})
    if not held.get("base") or not held.get("token"):
        return None
    query = urlencode(
        {
            "width": POSTER_W,
            "height": POSTER_H,
            "minSize": 1,
            "upscale": 1,
            "url": "/" + path,
            "X-Plex-Token": held["token"],
        }
    )
    return f"{held['base']}/photo/:/transcode?{query}"
