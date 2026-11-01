"""Log-safe descriptions of URLs and exceptions.

Provider URLs carry credentials: Xtream puts the username and password in every query,
and STB profile requests carry the MAC. requests exceptions repeat the URL in their text,
so logs get the exception type and a redacted host only.
"""

from __future__ import annotations

import re
import traceback
from typing import Optional
from urllib.parse import urlsplit

_HIDDEN = "…"
# An address, its "://" as written, JSON-escaped (http:\/\/) or percent-encoded (http%3A%2F%2F).
_ADDRESS = re.compile(r"\b[a-z][a-z0-9+.\-]*(?::|%3A)(?:\\?/|%2F){2}\S*", re.IGNORECASE)
_QUERY = re.compile(r"\S*\?\S*=\S*")  # a query string carries "=": "Who?Me" stays
# What else names an account, each kept up to its sign and the rest hidden (a privacy review):
_SECRETS = (
    # An Xtream stream's path, wherever its host went: /live/<user>/<password>/<id>.ts.
    re.compile(r"(/(?:live|movie|series|timeshifts?)/)[^/\s]+/[^/\s]+(?:/\S*)?", re.IGNORECASE),
    # requests' "Max retries exceeded with url: /…", and a request line's path.
    re.compile(r"(\burl: )/\S*"),
    re.compile(r"(\b(?:GET|HEAD|POST|PUT|DELETE|OPTIONS|PATCH) )/\S*"),
    # A header that carries a sign-in: its whole value (a cookie's holds the MAC).
    re.compile(
        r"(\b(?:Authorization|Proxy-Authorization|Cookie|Set-Cookie|X-Emby-Token"
        r"|X-Emby-Authorization|X-MediaBrowser-Token|X-Api-Key)\s*[:=]\s*)[^\r\n]*",
        re.IGNORECASE,
    ),
    re.compile(r"(\b(?:Bearer|Basic)\s+)[A-Za-z0-9\-._~+/=]{4,}"),
    re.compile(r"(\bToken\s*=\s*\"?)[^\"\s,]+", re.IGNORECASE),  # MediaBrowser … Token="…"
    # A JSON (or Python) value under a credential's key: "password": "…".
    re.compile(
        r"""(["'](?:username|password|passwd|token|access_token|api_key|apikey|mac|serial"""
        r"""|sn|device_id|device_id2|signature)["']\s*:\s*["'])[^"']*""",
        re.IGNORECASE,
    ),
    # A query's credential outside a query string ("api_key=…&static=true", a form body).
    re.compile(
        r"(\b(?:username|password|passwd|token|access_token|api_key|apikey|mac|sn|serial"
        r"|device_id|device_id2|signature)=)[^&\s;,]+",
        re.IGNORECASE,
    ),
)
# A MAC address (an STB portal's device), its colons as written or percent-encoded.
_MAC = re.compile(r"\b[0-9A-F]{2}(?:(?::|-|%3A)[0-9A-F]{2}){5}\b", re.IGNORECASE)


def redact_url(url: object) -> str:
    """``scheme://host/…``: the URL without userinfo, port, path, query or fragment."""
    try:
        parts = urlsplit(str(url))
        host = parts.hostname
    except ValueError:
        return _HIDDEN
    if not parts.scheme or not host:
        return _HIDDEN
    if ":" in host:
        host = f"[{host}]"  # IPv6
    return f"{parts.scheme}://{host}/{_HIDDEN}"


def _request_url(exc: BaseException) -> Optional[str]:
    """The URL of the request a requests exception failed on, when it names one."""
    for owner in (getattr(exc, "request", None), getattr(exc, "response", None)):
        url = getattr(owner, "url", None)
        if isinstance(url, str) and url:
            return url
    return None


def safe_exc(exc: BaseException) -> str:
    """The exception's type, and the redacted host of its request when known.

    Never the exception's text: it can carry the full URL and its query string.
    """
    name = type(exc).__name__
    url = _request_url(exc)
    return f"{name} ({redact_url(url)})" if url else name


def mask(text: object) -> str:
    """Hide the addresses and query strings in a text, its lines as they are, and what else
    names an account: an Xtream stream's path, a request's path, a sign-in header's value, a
    credential's JSON value or query field, a MAC address."""
    masked = _QUERY.sub(_HIDDEN, _ADDRESS.sub(_HIDDEN, str(text)))
    for secret in _SECRETS:
        masked = secret.sub(lambda found: found.group(1) + _HIDDEN, masked)
    return _MAC.sub(_HIDDEN, masked)


def scrub(text: object) -> str:
    """Drop addresses and query strings from a message: they can carry credentials."""
    return " ".join(mask(text).split())


def safe_message(exc: BaseException) -> str:
    """The exception's type and its message, without addresses, query strings or file names.

    An OSError keeps its errno and reason (``[Errno 13] Access is denied``), never the paths
    its text names.
    """
    name = type(exc).__name__
    if isinstance(exc, OSError) and exc.strerror:
        text = f"[Errno {exc.errno}] {exc.strerror}" if exc.errno is not None else exc.strerror
    else:
        text = scrub(exc)
    return f"{name}: {text}" if text else name


def safe_traceback(exc: BaseException) -> str:
    """The exception's stack frames without its message, for DEBUG logs."""
    frames = "".join(traceback.format_tb(exc.__traceback__))
    return f"Traceback (most recent call last):\n{frames}{safe_exc(exc)}"
