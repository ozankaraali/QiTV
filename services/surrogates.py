"""Provider text without lone surrogates (no Qt).

A server that cuts a name in the middle of an emoji sends half of its UTF-16 pair as a JSON
escape ("\\ud83d"). The standard json module, which requests' ``.json()`` uses, reads it as a
lone surrogate: a str that no UTF-8 file can hold. orjson then refuses all of config.json once
such a channel is in the watch history, and the recording schedule cannot be encoded. orjson
refuses such JSON outright, so a page of an STB portal's list (or its whole guide) was lost.

Every parse of what a provider (or a media server, TMDB, OpenSubtitles) sends passes through
here: each lone surrogate becomes U+FFFD, the replacement character a UTF-8 decoder puts for a
broken byte. An XMLTV guide cannot hold one (the XML parser refuses the document) and a
playlist read as UTF-8 neither, but one read in a charset the server names may (UTF-7): a
playlist is mended too.
"""

from __future__ import annotations

import json
import re
from typing import Any

import orjson

REPLACEMENT = "\ufffd"  # what a lone surrogate becomes
_SURROGATE = re.compile("[\ud800-\udfff]")


def mended_text(text: str) -> str:
    """`text` with each lone surrogate replaced by U+FFFD; two halves of a pair side by side
    (joined from two strings) are their character again."""
    if text.isascii() or not _SURROGATE.search(text):
        return text
    return text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")


def mended(value: Any) -> Any:
    """`value` (parsed JSON: dicts, lists, strings, numbers) with each lone surrogate in its
    strings and keys replaced by U+FFFD; its dicts and lists are mended in place. Data without
    one (nearly always) is found so at orjson's speed and returned untouched; a 100,000-channel
    list takes about a tenth of its parse."""
    if isinstance(value, str):
        return mended_text(value)
    if not isinstance(value, (dict, list)):
        return value
    try:
        orjson.dumps(value)  # refuses a lone surrogate, as a config save would
        return value
    except TypeError:  # orjson's JSONEncodeError is one: a lone surrogate, or a type it can't
        pass  # write (a set, a huge int): the walk below passes those over
    _mend_in_place(value)
    return value


def _mend_in_place(root: dict | list) -> None:
    """Every string in `root`'s dicts and lists mended, without recursion (deep JSON)."""
    seen: set[int] = set()
    stack: list[Any] = [root]
    while stack:
        node = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, dict):
            if any(isinstance(key, str) and not key.isascii() for key in node):
                items = list(node.items())  # keys mended, in their order
                node.clear()
                for key, item in items:
                    node[mended_text(key) if isinstance(key, str) else key] = item
            for key, item in node.items():
                if isinstance(item, str):
                    if not item.isascii():
                        node[key] = mended_text(item)  # a value set: the dict keeps its size
                elif isinstance(item, (dict, list)):
                    stack.append(item)
        elif isinstance(node, list):
            for index, item in enumerate(node):
                if isinstance(item, str):
                    if not item.isascii():
                        node[index] = mended_text(item)
                elif isinstance(item, (dict, list)):
                    stack.append(item)


def response_json(response: Any) -> Any:
    """``response.json()``, mended. Raises what it raises (ValueError for no JSON)."""
    return mended(response.json())


def loads(raw: bytes | str) -> Any:
    """``orjson.loads(raw)``; JSON that orjson refuses for a lone surrogate is read by the
    standard json instead, and mended. Raises orjson's JSONDecodeError when neither reads it."""
    try:
        return orjson.loads(raw)
    except orjson.JSONDecodeError as error:
        refused = error
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):  # UnicodeDecodeError is a ValueError
        raise refused from None
    return mended(value)
