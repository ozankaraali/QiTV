import logging
import re

logger = logging.getLogger(__name__)


def _write_export_file(content_data, file_path, url_formatter, extinf_formatter):
    """
    Common export logic for M3U files.

    Args:
        content_data: List of content items to export
        file_path: Path to write the M3U file
        url_formatter: Callable(item) -> url or None; if None, item is skipped
        extinf_formatter: Callable(item) -> EXTINF line string
    """
    try:
        with open(file_path, "w", encoding="utf-8") as file:
            file.write("#EXTM3U\n")
            count = 0
            for item in content_data:
                url = url_formatter(item)
                if url:
                    extinf = extinf_formatter(item)
                    file.write(f"{extinf}\n{url}\n")
                    count += 1
            logger.info(f"Items exported: {count}")
            logger.info(f"Content list has been saved to {file_path}")
    except IOError as e:
        logger.warning(f"Error saving content list: {e}")


def save_m3u_content(content_data, file_path):
    def url_formatter(item):
        return item.get("cmd")

    def extinf_formatter(item):
        name = item.get("name", "Unknown")
        logo = item.get("logo", "")
        group = item.get("group", "")
        xmltv_id = item.get("xmltv_id", "")
        return f'#EXTINF:-1 tvg-id="{xmltv_id}" tvg-logo="{logo}" group-title="{group}" ,{name}'

    _write_export_file(content_data, file_path, url_formatter, extinf_formatter)


def save_stb_content(base_url, content_data, mac, file_path):
    def url_formatter(item):
        cmd_url = item.get("cmd", "").replace("ffmpeg ", "")
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
        name = item.get("name", "Unknown")
        logo = item.get("logo", "")
        xmltv_id = item.get("xmltv_id", "")
        return f'#EXTINF:-1 tvg-id="{xmltv_id}" tvg-logo="{logo}" ,{name}'

    _write_export_file(content_data, file_path, url_formatter, extinf_formatter)
