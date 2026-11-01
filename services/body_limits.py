"""How much of an answer a provider (or any host a list names) can make QiTV read.

A playlist or a Player API list was read whole into memory, a logo whole from whatever host a
list named, a guide whole onto the disk: a hostile or broken server could fill either. Each
read stops at a generous limit, far above any real list, logo or guide, with TooLarge.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from services.i18n import N_

logger = logging.getLogger(__name__)

MB = 1024 * 1024
LIST_MAX = 512 * MB  # an M3U playlist, a Player API or a portal's JSON list
LOGO_MAX = 5 * MB  # a channel's or a title's picture
XMLTV_MAX = 2048 * MB  # a guide as downloaded (a packed one is unpacked as it is read)
_CHUNK = 1 << 16

# A worker's error (catalog.friendly_error passes it on as it is; screens translate it).
TOO_LARGE_MESSAGE = N_("The provider's answer is too large to load")


class TooLarge(ValueError):
    """An answer larger than its limit: the read stopped there, the connection closed."""

    def __init__(self, limit: int) -> None:
        super().__init__(TOO_LARGE_MESSAGE)
        self.limit = limit


def _declared(headers: Any) -> int:
    try:
        return int(headers.get("Content-Length") or -1)
    except TypeError, ValueError:
        return -1


def check_declared(headers: Any, limit: int) -> None:
    """TooLarge when the answer says (Content-Length) it is larger than `limit`."""
    if _declared(headers) > limit:
        logger.warning("An answer declared larger than %d MB was refused", limit // MB)
        raise TooLarge(limit)


def read_capped(response: Any, limit: int) -> bytes:
    """A requests response's body, read up to `limit` bytes (decoded, as requests gives it):
    TooLarge, and the connection closed, past that. The response keeps the body, so that
    `content`, `text` and `json()` read it as before."""
    try:
        check_declared(response.headers, limit)
        body = bytearray()
        for chunk in response.iter_content(_CHUNK):
            body += chunk
            if len(body) > limit:
                logger.warning("An answer larger than %d MB was cut off", limit // MB)
                raise TooLarge(limit)
    except TooLarge:
        response.close()
        raise
    content = bytes(body)
    response._content = content  # what requests' own read keeps (`content` reads it)
    return content


def capped(limit: int) -> dict[str, list[Callable[..., Any]]]:
    """requests' `hooks=`: each answer's body is read (`read_capped`) before requests would
    read it whole; a larger one raises TooLarge from the request itself."""

    def read(response: Any, *args: Any, **kwargs: Any) -> Any:
        read_capped(response, limit)
        return response

    return {"response": [read]}


async def read_capped_async(response: Any, limit: int) -> bytes:
    """An aiohttp response's body, read up to `limit` bytes; TooLarge past that."""
    if response.content_length is not None and response.content_length > limit:
        raise TooLarge(limit)
    body = bytearray()
    async for chunk in response.content.iter_chunked(_CHUNK):
        body += chunk
        if len(body) > limit:
            raise TooLarge(limit)
    return bytes(body)
