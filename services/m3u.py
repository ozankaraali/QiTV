import re
from typing import Dict, List, Optional
from urllib.parse import urlparse


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

    for line in lines:
        if line.startswith("#EXTINF"):
            tvg_id_match = re.search(r'tvg-id="([^"]+)"', line)
            tvg_logo_match = re.search(r'tvg-logo="([^"]+)"', line)
            group_title_match = re.search(r'group-title="([^"]+)"', line)
            user_agent_match = re.search(r'user-agent="([^"]+)"', line)
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

        elif line.startswith("#EXTVLCOPT:http-user-agent="):
            user_agent = line.split("=", 1)[1]
            item["user_agent"] = user_agent

        elif line.startswith("http"):
            # Round-trip through urlparse: it drops stray tabs and other control whitespace.
            item["cmd"] = urlparse(line).geturl()
            items.append(item)

    # Return flat list if not categorizing
    if not categorize:
        return items

    # Build categorized structure
    return _build_categorized_structure(items)


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


def _build_categorized_structure(items: List[Dict]) -> Dict:
    """Build categorized structure from M3U items with group-title support.

    Returns dict with:
        - categories: List of category dicts with id and title
        - contents: List of all items with tv_genre_id field added
        - sorted_channels: Dict mapping category_id -> list of item indices
    """
    sorted_channels: Dict[str, List[int]] = {}

    # Group items by category. The group title is also the category id: it is stable
    # across refreshes/processes and cannot collide. A channel of several groups is listed
    # in each, as one item: "All" (the contents) has it once.
    for idx, item in enumerate(items):
        category_ids = split_group_title(item.get("group")) or ["Uncategorized"]
        # Add tv_genre_id for compatibility with STB/Xtream structure
        item["tv_genre_id"] = category_ids[0]
        item["number"] = str(idx + 1)
        for category_id in category_ids:
            sorted_channels.setdefault(category_id, []).append(idx)

    # Build categories list
    categories = [{"id": "*", "title": "All"}]
    categories.extend({"id": group, "title": group} for group in sorted(sorted_channels))

    return {
        "categories": categories,
        "contents": items,
        "sorted_channels": sorted_channels,
    }
