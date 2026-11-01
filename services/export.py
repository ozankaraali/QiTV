import logging
import os
import re
from typing import Any

from services.provider_api import stb_portal_base
from services.stream_address import playable

logger = logging.getLogger(__name__)

# What could end a playlist line (the provider's text is written as it came): control
# characters, and the separators str.splitlines also breaks at.
_LINE_BREAKS = re.compile(r"[\x00-\x1f\x7f\x85\u2028\u2029]")


def _text(value: Any, default: str = "") -> str:
    """A provider's value for an #EXTINF line, on one line (a missing one: `default`)."""
    if value is None or value == "":
        return default
    return _LINE_BREAKS.sub(" ", str(value))


def _attribute(value: Any) -> str:
    """A provider's value for a quoted #EXTINF attribute: on one line, and its double quotes
    made single (M3U has no escape), so it cannot close the quote and add attributes."""
    return _text(value).replace('"', "'")


def _link(value: str) -> str:
    """A link on one line: control characters dropped (a URL has none)."""
    return _LINE_BREAKS.sub("", value)


def _extinf(item: dict, group: bool = True) -> str:
    """An item's #EXTINF line. The name follows the first comma outside the quoted values
    (services.m3u reads it so), so a name with commas comes back whole."""
    attributes = f'tvg-id="{_attribute(item.get("xmltv_id"))}" '
    attributes += f'tvg-logo="{_attribute(item.get("logo"))}" '
    if group:
        attributes += f'group-title="{_attribute(item.get("group"))}" '
    return f"#EXTINF:-1 {attributes},{_text(item.get('name'), 'Unknown')}"


def _open_private(file_path: str):
    """The export file to write, readable by the user alone: its links carry the account's
    sign-in. A file it replaces keeps no looser mode."""
    # O_BINARY (Windows): a text-mode descriptor under the text wrapper wrote "\r\r\n".
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    descriptor = os.open(file_path, flags, 0o600)
    try:
        os.chmod(file_path, 0o600)  # an older file of another mode
        return os.fdopen(descriptor, "w", encoding="utf-8")
    except BaseException:
        os.close(descriptor)
        raise


def _write_export_file(content_data, file_path, url_formatter, extinf_formatter) -> int:
    """
    Common export logic for M3U files.

    Returns the number of entries written; raises OSError when the file can't be written.

    Args:
        content_data: List of content items to export
        file_path: Path to write the M3U file
        url_formatter: Callable(item) -> url or None; if None, item is skipped
        extinf_formatter: Callable(item) -> EXTINF line string
    """
    try:
        with _open_private(file_path) as file:
            file.write("#EXTM3U\n")
            count = refused = 0
            for item in content_data:
                url = _link(str(url_formatter(item) or ""))
                if url and not playable(url):  # a player opening the file would follow it
                    refused += 1
                elif url:
                    extinf = extinf_formatter(item)
                    file.write(f"{extinf}\n{url}\n")
                    count += 1
            if refused:
                logger.warning("Items left out (no stream's address): %s", refused)
            logger.info("Items exported: %s", count)
            logger.info("Content list has been saved to %s", file_path)
        return count
    except OSError as e:
        logger.warning("Error saving content list: %s", type(e).__name__)
        raise


def save_m3u_content(content_data, file_path) -> int:
    def url_formatter(item):
        return item.get("cmd")

    return _write_export_file(content_data, file_path, url_formatter, _extinf)


def save_stb_content(base_url, content_data, mac, file_path) -> int:
    base_url = stb_portal_base(base_url)  # callers may pass a full portal URL like http://host/c/

    def url_formatter(item):
        cmd_url = (item.get("cmd") or "").replace("ffmpeg ", "")
        if "localhost" in cmd_url:
            id_match = re.search(r"/(ch|vod)/(\d+)_", cmd_url)
            if id_match:
                content_type = id_match.group(1)
                content_id = id_match.group(2)
                if content_type == "ch":
                    cmd_url = (
                        f"{base_url}/play/live.php?mac={mac}&stream={content_id}&extension=m3u8"
                    )
                elif content_type == "vod":
                    cmd_url = (
                        f"{base_url}/play/vod.php?mac={mac}&stream={content_id}&extension=m3u8"
                    )
        return cmd_url or None

    def extinf_formatter(item):
        return _extinf(item, group=False)

    return _write_export_file(content_data, file_path, url_formatter, extinf_formatter)
