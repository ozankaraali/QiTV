import re
from typing import Dict, List
from urllib.parse import urlparse


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
    lines = data.split("\n")
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
            urlobject = urlparse(line)
            item["cmd"] = urlobject.geturl()
            items.append(item)

    # Return flat list if not categorizing
    if not categorize:
        return items

    # Build categorized structure
    return _build_categorized_structure(items)


def _build_categorized_structure(items: List[Dict]) -> Dict:
    """Build categorized structure from M3U items with group-title support.

    Returns dict with:
        - categories: List of category dicts with id and title
        - contents: List of all items with tv_genre_id field added
        - sorted_channels: Dict mapping category_id -> list of item indices
    """
    categories_map: Dict[str, str] = {}
    sorted_channels: Dict[str, List[int]] = {}
    contents: List[Dict] = []

    # Group items by category
    for idx, item in enumerate(items):
        group = item.get("group") or "Uncategorized"
        category_id = str(abs(hash(group)) % 1000000)  # Generate stable category ID

        # Add to categories map
        if category_id not in categories_map:
            categories_map[category_id] = group

        # Add tv_genre_id for compatibility with STB/Xtream structure
        item["tv_genre_id"] = category_id
        item["number"] = str(idx + 1)
        contents.append(item)

        # Add to sorted_channels mapping
        if category_id not in sorted_channels:
            sorted_channels[category_id] = []
        sorted_channels[category_id].append(idx)

    # Build categories list
    categories = [{"id": "*", "title": "All"}]
    for cat_id, cat_name in sorted(categories_map.items(), key=lambda x: x[1]):
        categories.append({"id": cat_id, "title": cat_name})

    return {
        "categories": categories,
        "contents": contents,
        "sorted_channels": sorted_channels,
    }
