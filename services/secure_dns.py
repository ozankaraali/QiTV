"""Secure DNS (2.9.1): QiTV looks up its providers' names over HTTPS, past an internet provider
that blocks them by name.

Some internet providers keep a list of IPTV panels and answer their names wrongly, or not at
all. With Settings → General → Secure DNS on, every name QiTV's own connections look up (the
providers' lists and guides, their logos, the time-shift reader of each channel, the film relay,
OpenSubtitles, TMDB) is asked of Cloudflare's resolver (1.1.1.1), else Google's (8.8.8.8), by
their numbers and over HTTPS (RFC 8484's JSON form), else of the computer's own DNS as before.
Only the lookup changes: each connection still checks the provider's own certificate for its
name. VLC, which plays a channel QiTV can't time-shift, asks the computer's DNS itself.

`enable(True)` puts a stand-in for `socket.getaddrinfo` in place in this process (QiTV's, and
the time-shift helper's: services.live.engine tells it); answers are kept for their TTL (one
minute to an hour). Off (the default), nothing is changed. No Qt here.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import logging
import socket
import ssl
import threading
import time
from typing import Any, Optional
from urllib.parse import quote

logger = logging.getLogger(__name__)

# Asked by their numbers (no lookup of their own), each in its JSON form.
RESOLVERS = (
    ("1.1.1.1", "/dns-query?name={name}&type={kind}"),
    ("8.8.8.8", "/resolve?name={name}&type={kind}"),
)
TIMEOUT_S = 3.0
MIN_TTL_S, MAX_TTL_S = 60, 3600
_A, _AAAA = 1, 28

_system_getaddrinfo = socket.getaddrinfo
_lock = threading.Lock()
_cache: dict[str, tuple[float, list[str]]] = {}
_on = False
_context: Optional[ssl.SSLContext] = None


def enabled() -> bool:
    return _on


def enable(on: bool) -> None:
    """Secure DNS on or off in this process, from the next lookup on."""
    global _on
    on = bool(on)
    if on == _on:
        return
    _on = on
    socket.getaddrinfo = _getaddrinfo if on else _system_getaddrinfo
    with _lock:
        _cache.clear()
    logger.info("Secure DNS %s", "on" if on else "off")


def _plain(host: Any) -> Optional[str]:
    """`host` as a name to look up; None for a number, this computer or the local network."""
    if isinstance(host, bytes):
        host = host.decode("ascii", "ignore")
    if not isinstance(host, str) or not host:
        return None
    name = host.strip("[]").rstrip(".").lower()
    try:
        ipaddress.ip_address(name)
        return None
    except ValueError:
        pass
    if name == "localhost" or name.endswith((".local", ".localhost", ".lan", ".home.arpa")):
        return None
    if "." not in name:
        return None  # a name of the local network
    return name


def _getaddrinfo(
    host: Any, port: Any, family: int = 0, type: int = 0, proto: int = 0, flags: int = 0
):  # (socket.getaddrinfo's names: asyncio passes them so)
    name = _plain(host)
    if not _on or name is None or flags & socket.AI_NUMERICHOST:
        return _system_getaddrinfo(host, port, family, type, proto, flags)
    found = []
    for address in lookup(name):
        try:
            found.extend(
                _system_getaddrinfo(
                    address, port, family, type, proto, flags | socket.AI_NUMERICHOST
                )
            )
        except socket.gaierror:
            continue  # an IPv6 answer where only IPv4 is asked for
    if found:
        return found
    return _system_getaddrinfo(host, port, family, type, proto, flags)


def lookup(name: str) -> list[str]:
    """`name`'s addresses as a secure resolver gives them ([] when none answered)."""
    now = time.monotonic()
    with _lock:
        kept = _cache.get(name)
    if kept is not None and kept[0] > now:
        return list(kept[1])
    for server, path in RESOLVERS:
        addresses: list[str] = []
        ttl = MAX_TTL_S
        for kind in ("A", "AAAA"):
            answer = _ask(server, path.format(name=quote(name), kind=kind))
            if answer is None:
                break  # this resolver didn't answer: the next one
            for record in answer:
                addresses.append(record[0])
                ttl = min(ttl, record[1])
            if addresses:
                break  # an IPv4 answer is enough
        if addresses:
            ttl = max(MIN_TTL_S, min(MAX_TTL_S, ttl))
            with _lock:
                _cache[name] = (now + ttl, addresses)
            return addresses
    return []


def _ask(server: str, path: str) -> Optional[list[tuple[str, int]]]:
    """One question to `server`: its (address, TTL) answers; None when it didn't answer."""
    try:
        connection = http.client.HTTPSConnection(server, timeout=TIMEOUT_S, context=_tls())
        try:
            connection.request("GET", path, headers={"Accept": "application/dns-json"})
            response = connection.getresponse()
            if response.status != 200:
                return None
            data = json.loads(response.read(65536))
        finally:
            connection.close()
    except (OSError, ValueError, http.client.HTTPException) as error:
        logger.debug("Secure DNS: %s didn't answer (%s)", server, type(error).__name__)
        return None
    if not isinstance(data, dict) or data.get("Status") not in (0, 3):  # 3: no such name
        return None
    out: list[tuple[str, int]] = []
    for record in data.get("Answer") or []:
        if isinstance(record, dict) and record.get("type") in (_A, _AAAA):
            address, ttl = record.get("data"), record.get("TTL")
            if isinstance(address, str):
                out.append((address, int(ttl) if isinstance(ttl, int) else MAX_TTL_S))
    return out


def _tls() -> ssl.SSLContext:
    """The resolvers' certificates checked against certifi's authorities (requests' own)."""
    global _context
    if _context is None:
        try:
            import certifi

            _context = ssl.create_default_context(cafile=certifi.where())
        except Exception:  # no certifi: the system's
            _context = ssl.create_default_context()
    return _context
