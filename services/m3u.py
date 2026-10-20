import re
import sys
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from services.catchup import SOURCE_KEY, TYPE_KEY, m3u_archive_fields, playlist_fields
from services.i18n import N_

_ATTRIBUTE = re.compile(r'([A-Za-z][\w-]*)="([^"]*)"')
# The attributes catch-up reads (services.catchup.m3u_archive_fields); the #EXTM3U line's set
# them for every channel. A categorized playlist keeps that line's template once
# (`catchup_source` beside its channels), not on each channel that takes it.
_CATCHUP_ATTRIBUTES = (
    "catchup",
    "catchup-type",
    "catchup-days",
    "catchup-source",
    "timeshift",
    "tvg-rec",
)
PLAYLIST_SOURCE_KEY = "catchup_source"  # the #EXTM3U line's template, beside the channels


def _attributes(line: str) -> Dict[str, str]:
    """An #EXTINF or #EXTM3U line's attributes, by lower-case name (the first of each)."""
    found: Dict[str, str] = {}
    for name, value in _ATTRIBUTE.findall(line):
        found.setdefault(name.lower(), value)
    return found


def decode_playlist(response) -> str:
    """Decode an HTTP playlist response.

    When the server declares no charset, ``response.text`` would fall back to ISO-8859-1 and
    mangle UTF-8 names; decode the bytes as UTF-8 (dropping any BOM) instead.
    """
    content_type = response.headers.get("Content-Type", "") or ""
    if "charset" in content_type.lower():
        return response.text
    return response.content.decode("utf-8-sig", errors="replace")


def parse_m3u(data: str, categorize: bool = False):
    """Parse M3U playlist.

    Args:
        data: M3U playlist content
        categorize: If True, return categorized structure with categories and sorted_channels.
                   If False, return flat list (default for backward compatibility)

    Returns:
        If categorize=False: List of items
        If categorize=True: Dict with 'categories', 'contents', 'sorted_channels'
    """
    lines = re.split(r"\r\n|\r|\n", data.lstrip("\ufeff"))
    items = []
    item = {}
    id_counter = 0
    defaults: Dict[str, str] = {}  # the #EXTM3U line's catch-up attributes

    for line in lines:
        if line.startswith("#EXTM3U"):
            header = _attributes(line)
            defaults = {k: header[k] for k in _CATCHUP_ATTRIBUTES if header.get(k)}
        elif line.startswith("#EXTINF"):
            tvg_id_match = re.search(r'tvg-id="([^"]+)"', line)
            tvg_logo_match = re.search(r'tvg-logo="([^"]+)"', line)
            group_title_match = re.search(r'group-title="([^"]+)"', line)
            user_agent_match = re.search(r'user-agent="([^"]+)"', line)
            referrer_match = re.search(r'http-referrer="([^"]+)"', line)
            item_name_match = re.search(r",([^,]+)$", line)

            tvg_id = tvg_id_match.group(1) if tvg_id_match else None
            tvg_logo = tvg_logo_match.group(1) if tvg_logo_match else None
            group_title = group_title_match.group(1) if group_title_match else None
            user_agent = user_agent_match.group(1) if user_agent_match else None
            item_name = item_name_match.group(1) if item_name_match else None

            id_counter += 1
            item = {
                "id": id_counter,
                "group": group_title,
                "xmltv_id": tvg_id,
                "name": item_name,
                "logo": tvg_logo,
                "user_agent": user_agent,
            }
            if referrer_match:  # only then: a channel without one stays as it was
                item["referrer"] = referrer_match.group(1)
            if "catchup" in line or "timeshift" in line or "tvg-rec" in line or defaults:
                # Only a channel with an archive gets its keys (catch-up, 2.2), each type's
                # name kept once in memory.
                archive = (playlist_fields if categorize else m3u_archive_fields)(
                    _attributes(line), defaults
                )
                if TYPE_KEY in archive:
                    archive[TYPE_KEY] = sys.intern(archive[TYPE_KEY])
                if SOURCE_KEY in archive:
                    archive[SOURCE_KEY] = sys.intern(archive[SOURCE_KEY])
                item.update(archive)

        elif line.startswith("#EXTVLCOPT:http-user-agent="):
            user_agent = line.split("=", 1)[1]
            item["user_agent"] = user_agent

        elif line.startswith("#EXTVLCOPT:http-referrer="):
            # VLC's own option name; requests and VLC send it as the Referer header.
            item["referrer"] = line.split("=", 1)[1].strip()

        elif line.startswith("http"):
            # Round-trip through urlparse: it drops stray tabs and other control whitespace.
            item["cmd"] = urlparse(line).geturl()
            items.append(item)

    # Return flat list if not categorizing
    if not categorize:
        return items

    # Build categorized structure
    result = _build_categorized_structure(items)
    if defaults.get("catchup-source"):
        result[PLAYLIST_SOURCE_KEY] = defaults["catchup-source"]
    return result


def split_group_title(title: Optional[str]) -> List[str]:
    """The groups an M3U group-title names, in order.

    iptv-org writes a channel of several categories as one title, "Animation;Kids;Music": its
    parts are stripped, empty ones dropped and a repeated one kept once. A title without ";"
    is one group, as it is. No title: no group.
    """
    if not title:
        return []
    if ";" not in title:
        return [title]
    return list(dict.fromkeys(part.strip() for part in title.split(";") if part.strip()))


def split_cached_groups(payload: Any) -> Any:
    """A playlist cached before group titles were split ("A;B" as one group), with its groups
    rebuilt from its channels: no download. Any other payload comes back as it is.

    Split groups never keep a ";", so one in a group id marks the old shape.
    """
    if not isinstance(payload, dict):
        return payload
    categories, contents = payload.get("categories"), payload.get("contents")
    if not isinstance(categories, list) or not isinstance(contents, list):
        return payload
    if not any(isinstance(c, dict) and ";" in str(c.get("id", "")) for c in categories):
        return payload
    if not all(isinstance(item, dict) for item in contents):
        return payload
    return _build_categorized_structure(contents)


def _build_categorized_structure(items: List[Dict]) -> Dict:
    """Build categorized structure from M3U items with group-title support.

    Returns dict with:
        - categories: List of category dicts with id and title
        - contents: List of all items with tv_genre_id field added
        - sorted_channels: Dict mapping category_id -> list of item indices
    """
    sorted_channels: Dict[str, List[int]] = {}
    contents: List[Dict] = []
    seen: Dict[tuple, int] = {}  # a channel's line but for its group -> its place in contents
    listed: set[tuple[str, int]] = set()

    # Group items by category. The group title is also the category id: it is stable
    # across refreshes/processes and cannot collide. A channel of several groups is listed
    # in each, as one item: "All" (the contents) has it once. So is one whose lines differ only
    # in their group (a playlist grouped by country repeats a channel of several countries).
    for item in items:
        # The group's id and title, stored in English (catalog.shown_label shows it)
        category_ids = split_group_title(item.get("group")) or [N_("Uncategorized")]
        key = _repeat_key(item)
        idx = seen.get(key)
        if idx is None:
            idx = seen[key] = len(contents)
            # Add tv_genre_id for compatibility with STB/Xtream structure
            item["tv_genre_id"] = category_ids[0]
            item["number"] = str(idx + 1)
            contents.append(item)
        for category_id in category_ids:
            if (category_id, idx) not in listed:
                listed.add((category_id, idx))
                sorted_channels.setdefault(category_id, []).append(idx)

    # Build categories list
    categories = [{"id": "*", "title": N_("All")}]
    categories.extend({"id": group, "title": group} for group in sorted(sorted_channels))

    return {
        "categories": categories,
        "contents": contents,
        "sorted_channels": sorted_channels,
    }


def _repeat_key(item: Dict) -> tuple:
    """A channel's line but for its group (and the parser's own id and number): two lines
    alike in it are one channel."""
    return tuple(
        sorted(
            (key, repr(value))
            for key, value in item.items()
            if key not in ("id", "group", "tv_genre_id", "number")
        )
    )
