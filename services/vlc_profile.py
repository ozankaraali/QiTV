"""libVLC media options per playback kind (pure; spec §6.6)."""

from enum import Enum
from typing import Optional
from urllib.parse import urlsplit

DEFAULT_USER_AGENT = "VLC/3.0.20"
_VOD_CONTAINERS = (".mkv", ".mp4", ".avi", ".webm", ".mov", ".m4v")
# Live network caching per Settings → Live latency (ms) and HLS distance from the live edge.
_LIVE_CACHING_MS = {"low": 300, "normal": 1000, "stable": 3000}
_LIVE_DELAY_MS = {"low": 3000, "normal": 6000, "stable": 12000}
_UNKNOWN_MIN_CACHING_MS = 1500


class PlayKind(str, Enum):
    LIVE = "live"  # a live channel played directly from the provider
    LIVE_PIPELINE = "live_pipeline"  # a live channel served from the loopback time-shift server
    VOD_FILE = "vod_file"  # a container file: mkv/mp4/avi/webm/mov/m4v
    VOD_STREAM = "vod_stream"  # known on-demand content in a stream format (ts/m3u8)
    UNKNOWN = "unknown"  # M3U entries: may be live or on demand


def classify(url: str, is_live: Optional[bool]) -> PlayKind:
    if urlsplit(url).path.lower().endswith(_VOD_CONTAINERS):
        return PlayKind.VOD_FILE
    if is_live is True:
        return PlayKind.LIVE
    if is_live is False:
        return PlayKind.VOD_STREAM
    return PlayKind.UNKNOWN


def media_options(
    kind: PlayKind,
    *,
    latency: str = "normal",
    user_agent: Optional[str] = None,
    referrer: Optional[str] = None,
) -> list[str]:
    """`referrer`: the channel's M3U http-referrer (2.1), sent with VLC's own requests."""
    options = [
        f":http-user-agent={user_agent or DEFAULT_USER_AGENT}",
        # No ":avcodec-hw" here: a media option overrides the instance's, and the decoder is
        # Settings' choice (services.playback.decoder_args).
        ":avcodec-fast",
    ]
    if referrer:
        options.append(f":http-referrer={referrer}")
    if kind is PlayKind.LIVE_PIPELINE:
        # Loopback segments arrive on time from QiTV's own server, so VLC only needs a small
        # buffer and must not retry on its own: the pipeline reconnects upstream.
        return options + [
            ":network-caching=300",
            ":live-caching=300",
            ":clock-jitter=0",
            ":ts-trust-pcr=0",
            ":http-reconnect=false",
        ]
    options.append(":http-reconnect=true")
    if kind in (PlayKind.LIVE, PlayKind.UNKNOWN):
        caching = _LIVE_CACHING_MS.get(latency, _LIVE_CACHING_MS["normal"])
        if kind is PlayKind.UNKNOWN:
            caching = max(caching, _UNKNOWN_MIN_CACHING_MS)
        delay = _LIVE_DELAY_MS.get(latency, _LIVE_DELAY_MS["normal"])
        options += [
            f":network-caching={caching}",
            f":live-caching={caching}",
            ":clock-jitter=0",
            f":adaptive-livedelay={delay}",
        ]
    elif kind is PlayKind.VOD_FILE:
        options += [
            ":network-caching=4000",
            ":file-caching=2000",
            ":input-timeshift-granularity=0",
            ":input-fast-seek",
            ":http-forward-cookies=true",
        ]
    else:
        options += [":network-caching=2000", ":file-caching=1500", ":live-caching=1000"]
    return options
