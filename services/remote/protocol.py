"""The phone remote's wire vocabulary: constants, encodings, the buttons and the messages."""

from __future__ import annotations

import base64

RELAY_URL = "https://remote.qitv.app"
DEFAULT_PORT = 5384


def b64e(data: bytes) -> str:
    """base64url without padding, as every id, key and code travels."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64d(text: str) -> bytes:
    """The inverse of b64e; raises ValueError for anything that isn't base64url."""
    if not isinstance(text, str):
        raise ValueError("not text")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, TypeError) as error:
        raise ValueError(f"not base64url: {error}") from error
