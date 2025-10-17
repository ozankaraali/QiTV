import logging
import re

logger = logging.getLogger(__name__)


def save_m3u_content(content_data, file_path):
    try:
        with open(file_path, "w", encoding="utf-8") as file:
            file.write("#EXTM3U\n")
            count = 0
            for item in content_data:
                name = item.get("name", "Unknown")
                logo = item.get("logo", "")
                group = item.get("group", "")
                xmltv_id = item.get("xmltv_id", "")
                cmd_url = item.get("cmd")

                if cmd_url:
                    item_str = f'#EXTINF:-1 tvg-id="{xmltv_id}" tvg-logo="{logo}" group-title="{group}" ,{name}\n{cmd_url}\n'
                    count += 1
                    file.write(item_str)
            logger.info(f"Items exported: {count}")
            logger.info(f"Content list has been saved to {file_path}")
    except IOError as e:
        logger.warning(f"Error saving content list: {e}")


def save_stb_content(base_url, content_data, mac, file_path):
    try:
        with open(file_path, "w", encoding="utf-8") as file:
            file.write("#EXTM3U\n")
            count = 0
            for item in content_data:
                name = item.get("name", "Unknown")
                logo = item.get("logo", "")
                xmltv_id = item.get("xmltv_id", "")
                cmd_url = item.get("cmd", "").replace("ffmpeg ", "")

                if "localhost" in cmd_url:
                    id_match = re.search(r"/(ch|vod)/(\d+)_", cmd_url)
                    if id_match:
                        content_type = id_match.group(1)
                        content_id = id_match.group(2)
                        if content_type == "ch":
                            cmd_url = f"{base_url}/play/live.php?mac={mac}&stream={content_id}&extension=m3u8"
                        elif content_type == "vod":
                            cmd_url = f"{base_url}/play/vod.php?mac={mac}&stream={content_id}&extension=m3u8"

                item_str = f'#EXTINF:-1 tvg-id="{xmltv_id}" tvg-logo="{logo}" ,{name}\n{cmd_url}\n'
                count += 1
                file.write(item_str)
            logger.info(f"Items exported: {count}")
            logger.info(f"Content list has been saved to {file_path}")
    except IOError as e:
        logger.warning(f"Error saving content list: {e}")
