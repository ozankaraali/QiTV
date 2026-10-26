"""Provider types beyond the IPTV ones (2.8): Jellyfin and Emby media servers.

Kept apart from `services.media_server` so the catalog, the provider list and the UI can ask
"is this a media server?" without importing the client.
"""

from __future__ import annotations

from typing import Any

JELLYFIN, EMBY = "JELLYFIN", "EMBY"
MEDIA_SERVERS = (JELLYFIN, EMBY)


def is_media_server(provider_type: Any) -> bool:
    return str(provider_type or "").upper() in MEDIA_SERVERS
