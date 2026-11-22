"""Provider types beyond the IPTV ones: Jellyfin and Emby media servers (2.8), Plex (2.14).

Kept apart from `services.media_server` so the catalog, the provider list and the UI can ask
"is this a media server?" without importing the client.
"""

from __future__ import annotations

from typing import Any

JELLYFIN, EMBY, PLEX = "JELLYFIN", "EMBY", "PLEX"
MEDIA_SERVERS = (JELLYFIN, EMBY, PLEX)


def is_media_server(provider_type: Any) -> bool:
    return str(provider_type or "").upper() in MEDIA_SERVERS
