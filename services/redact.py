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
_ADDRESS = re.compile(r"\b[a-z][a-z0-9+.\-]*://\S*", re.IGNORECASE)
_QUERY = re.compile(r"\S*\?\S*=\S*")  # a query string carries "=": "Who?Me" stays


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


def scrub(text: object) -> str:
    """Drop addresses and query strings from a message: they can carry credentials."""
    cleaned = _QUERY.sub(_HIDDEN, _ADDRESS.sub(_HIDDEN, str(text)))
    return " ".join(cleaned.split())


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
