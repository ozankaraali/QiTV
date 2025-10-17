import re
from urllib.parse import urlparse


def parse_m3u(data: str):
    lines = data.split("\n")
    result = []
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
            result.append(item)
    return result
