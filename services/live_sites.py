"""Resolve a Kick or Twitch channel page to a fresh master playlist address, and (2.15) a
video's or clip's page to the address it plays as a film at its source quality.

No Qt here. A site's playlist address carries a short-lived token, so it is asked again for
every play and never logged or stored; logs name the site, the channel and the outcome only.

A video or clip (`site_media`: Twitch `twitch.tv/videos/<id>`, `clips.twitch.tv/<slug>` and
`twitch.tv/<channel>/clip/<slug>`; Kick `kick.com/<channel>/videos/<uuid>`,
`kick.com/video/<uuid>`, `kick.com/<channel>/clips/clip_<id>` and a channel page's
`?clip=clip_<id>`) resolves (`resolve_media`) to its source variant's media playlist (a Twitch
clip: its MP4 file), named "title · Twitch". One that is gone says "unknown", one that is
private or for subscribers only "private". A Twitch video still being recorded lists no end:
VLC would play it as a live stream from its newest piece, unseekable, so its listing as it
stands is kept as a local file that ends there (`keep_dir`): it plays as a film as far as it
is listed. A Kick one says its end already (it plays as listed).

The running broadcast's own recording (`running_recording`, 2.15: rewind past QiTV's buffer)
is the Twitch stream's `archiveVideo` (an EVENT playlist that grows), or the Kick channel's
newest video while it is live (its playlist says ENDLIST while it grows in steps): its source
variant's address (no token), where it starts (Kick: its first piece's PROGRAM-DATE-TIME;
Twitch: the video's start), and whether its pieces are dated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import logging
import os
from pathlib import Path
import random
import re
import tempfile
import threading
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit

from services.lazy_import import lazy_module

log = logging.getLogger(__name__)
# The window shows before the network libraries load: Open Link's kinds read `site_page`.
requests = lazy_module("requests")
m3u8 = lazy_module("m3u8")
if TYPE_CHECKING:  # the real module, for annotations only: the lazy one loads later
    import requests as requests_types


KICK, TWITCH = "kick", "twitch"
SITE_NAMES = {KICK: "Kick", TWITCH: "Twitch"}

KICK_API = "https://kick.com/api/v2/channels"
KICK_VIDEO_API = "https://kick.com/api/v1/video"
KICK_CLIP_API = "https://kick.com/api/v2/clips"
TWITCH_GQL = "https://gql.twitch.tv/gql"
TWITCH_USHER = "https://usher.ttvnw.net/api/channel/hls"
TWITCH_VOD = "https://usher.ttvnw.net/vod"
TWITCH_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
TWITCH_QUERY_HASH = "ed230aa1e33e07eebb8928504583da78a5173989fadfb1ac94be06a04f3cdbe9"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
)
TIMEOUT_S = 10

_HOSTS = {"kick.com": KICK, "twitch.tv": TWITCH}
_PAGE_HOSTS = {KICK: "kick.com", TWITCH: "www.twitch.tv"}  # where a channel's page is
_RESERVED = {
    KICK: {
        "categories", "browse", "search", "following", "video", "videos", "clips",
        "category", "api", "dashboard", "settings",
    },
    TWITCH: {
        "directory", "videos", "search", "settings", "subscriptions", "inventory",
        "downloads", "jobs", "p", "friends", "wallet", "drops", "following", "moderator",
        "popout",
    },
}  # fmt: skip
_CHANNEL = {
    KICK: re.compile(r"[a-z0-9][a-z0-9_-]{0,63}"),
    TWITCH: re.compile(r"[a-z0-9_]{1,25}"),
}
VIDEO, CLIP = "video", "clip"
_TWITCH_VIDEO = re.compile(r"[0-9]{1,20}")
_TWITCH_CLIP = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_KICK_VIDEO = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_KICK_CLIP = re.compile(r"clip_[A-Za-z0-9]{1,64}")
_TITLE_MAX = 90  # a title longer than this is cut (a row's name, not the whole text)
# The codecs VLC plays a film's source in, the engine's own list (H.264, HEVC): a site's other
# variants (AV1 beside them) come after these.
_FILM_CODECS = ("avc1", "avc3", "hvc1", "hev1")


@dataclass(frozen=True)
class SitePage:
    site: str  # KICK | TWITCH
    channel: str  # lower-case slug/login


@dataclass(frozen=True)
class SiteMedia:
    """A video or clip of a site (2.15). `ident`: a Twitch video's number, a Twitch clip's
    slug, a Kick video's UUID or a Kick clip's id, as written (a slug's letter case counts);
    `channel`: the channel its page named ("" when none did), which is not part of what it
    is."""

    site: str  # KICK | TWITCH
    kind: str  # VIDEO | CLIP
    ident: str
    channel: str = field(default="", compare=False)


@dataclass(frozen=True)
class Resolved:
    url: str  # the master playlist (tokenized; never logged)
    name: str  # "examplechannel · Kick"


@dataclass(frozen=True)
class Recording:
    """The running broadcast's recording on its site (2.15): `url`, its source variant's media
    playlist (no token; never logged); `start`, the Unix second its first piece starts at;
    `dated`, its pieces carry PROGRAM-DATE-TIME (Kick), else they are placed from `start` by
    their lengths (Twitch); `steps`, its playlist says it ends while it still grows, in steps
    (Kick: re-read about every 30 s, the end ignored); `until`, the Unix second its listing
    reached as it was asked (None: not known)."""

    url: str
    start: float
    dated: bool
    steps: bool
    until: float | None = None


class SiteError(Exception):
    """``reason`` is "offline", "unknown", "refused" or "network"; ``timed_out``: a "network"
    whose site never answered in time (to connect, or to read)."""

    def __init__(self, reason: str, timed_out: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.timed_out = timed_out


def site_page(url: str) -> SitePage | None:
    """The channel page a link names, or None (not a site, or not a channel page)."""
    text = (url or "").strip()
    if not text:
        return None
    if "://" not in text:
        text = "//" + text
    try:
        parts = urlsplit(text)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return None
    if parts.username is not None or parts.password is not None or port is not None:
        return None  # (a site's page names no user and no port: another address)
    for prefix in ("www.", "m."):
        if host.startswith(prefix):
            host = host[len(prefix) :]
            break
    site = _HOSTS.get(host)
    if site is None or parts.scheme not in ("", "http", "https"):
        return None
    segments = parts.path.strip("/").split("/")
    if len(segments) != 1:
        return None
    channel = segments[0].lower()
    if channel in _RESERVED[site] or not _CHANNEL[site].fullmatch(channel):
        return None
    if site == KICK and _query_clip(parts.query) is not None:
        return None  # (2.15: a clip on its channel's page: `site_media`)
    return SitePage(site, channel)


def _query_clip(query: str) -> Optional[str]:
    """A Kick channel page's `?clip=clip_<id>` (2.15), or None."""
    for value in parse_qs(query).get("clip", []):
        if _KICK_CLIP.fullmatch(value):
            return value
    return None


def site_media(url: str) -> SiteMedia | None:
    """The video or clip a link names (2.15), or None (not a site's, or no video or clip)."""
    text = (url or "").strip()
    if not text:
        return None
    if "://" not in text:
        text = "//" + text
    try:
        parts = urlsplit(text)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return None
    if parts.username is not None or parts.password is not None or port is not None:
        return None
    if parts.scheme not in ("", "http", "https"):
        return None
    for prefix in ("www.", "m."):
        if host.startswith(prefix):
            host = host[len(prefix) :]
            break
    segments = [segment for segment in parts.path.split("/") if segment]
    if host == "clips.twitch.tv":
        if len(segments) == 1 and _TWITCH_CLIP.fullmatch(segments[0]) and segments[0] != "embed":
            return SiteMedia(TWITCH, CLIP, segments[0])
        return None
    site = _HOSTS.get(host)
    if site == TWITCH:
        if len(segments) == 2 and segments[0].lower() == "videos":
            if _TWITCH_VIDEO.fullmatch(segments[1]):
                return SiteMedia(TWITCH, VIDEO, segments[1])
        elif len(segments) == 3 and segments[1].lower() == "clip":
            channel = segments[0].lower()
            if _CHANNEL[TWITCH].fullmatch(channel) and _TWITCH_CLIP.fullmatch(segments[2]):
                return SiteMedia(TWITCH, CLIP, segments[2], channel)
        return None
    if site != KICK:
        return None
    if len(segments) == 2 and segments[0].lower() == "video":
        if _KICK_VIDEO.fullmatch(segments[1]):
            return SiteMedia(KICK, VIDEO, segments[1].lower())
        return None
    if not segments or not _CHANNEL[KICK].fullmatch(segments[0].lower()):
        return None
    channel = segments[0].lower()
    if channel in _RESERVED[KICK]:
        return None
    if len(segments) == 3 and segments[1].lower() == "videos":
        if _KICK_VIDEO.fullmatch(segments[2]):
            return SiteMedia(KICK, VIDEO, segments[2].lower(), channel)
    elif len(segments) == 3 and segments[1].lower() == "clips":
        if _KICK_CLIP.fullmatch(segments[2]):
            return SiteMedia(KICK, CLIP, segments[2], channel)
    elif len(segments) == 1:
        clip = _query_clip(parts.query)
        if clip is not None:
            return SiteMedia(KICK, CLIP, clip, channel)
    return None


def media_address(media: SiteMedia) -> str:
    """The video's or clip's own page, however it was written (kept and copied as such)."""
    if media.site == TWITCH:
        if media.kind == VIDEO:
            return f"https://www.twitch.tv/videos/{media.ident}"
        return f"https://clips.twitch.tv/{media.ident}"
    if media.kind == VIDEO:
        return f"https://kick.com/video/{media.ident}"
    if media.channel:
        return f"https://kick.com/{media.channel}/clips/{media.ident}"
    return f"https://kick.com/clips/{media.ident}"


def media_key(media: SiteMedia) -> str:
    """What one video or clip is keyed by, whichever of its pages names it (a Kick clip's
    channel too: not part of it)."""
    return f"{media.site}:{media.kind}:{media.ident}"


def media_name(media: SiteMedia, title: str = "") -> str:
    """The video's or clip's name as QiTV lists it: "title · Twitch", else its id's."""
    shown = " ".join((title or "").split())
    if len(shown) > _TITLE_MAX:
        shown = shown[: _TITLE_MAX - 1].rstrip() + "…"
    return f"{shown or media.ident} · {SITE_NAMES[media.site]}"


def page_address(page: SitePage) -> str:
    """The channel page's own address, however it was written (kept and copied as such)."""
    return f"https://{_PAGE_HOSTS[page.site]}/{page.channel}"


def page_name(page: SitePage) -> str:
    """The channel's name as QiTV lists it: "examplechannel · Kick"."""
    return f"{page.channel} · {SITE_NAMES[page.site]}"


def resolve(
    page: SitePage,
    stop: threading.Event | None = None,
    session: requests_types.Session | None = None,
    kick_api: str = KICK_API,
    twitch_gql: str = TWITCH_GQL,
    twitch_usher: str = TWITCH_USHER,
) -> Resolved:
    """A fresh master playlist for the channel; raises ``SiteError`` when there is none."""
    http = session or requests.Session()
    try:
        if page.site == KICK:
            url = _kick(page.channel, http, stop, kick_api)
        elif page.site == TWITCH:
            url = _twitch(page.channel, http, stop, twitch_gql, twitch_usher)
        else:
            raise SiteError("unknown")
    except SiteError as error:
        log.info("%s: %s is %s.", SITE_NAMES.get(page.site, page.site), page.channel, error.reason)
        raise
    finally:
        if session is None:
            http.close()
    log.info("%s: %s is live.", SITE_NAMES[page.site], page.channel)
    return Resolved(url, page_name(page))


def resolve_media(
    media: SiteMedia,
    stop: threading.Event | None = None,
    session: requests_types.Session | None = None,
    *,
    keep_dir: str | None = None,
    kick_video_api: str = KICK_VIDEO_API,
    kick_clip_api: str = KICK_CLIP_API,
    twitch_gql: str = TWITCH_GQL,
    twitch_vod: str = TWITCH_VOD,
) -> Resolved:
    """The address a video or clip plays at now, at its source quality, and its name
    ("title · Twitch"); raises ``SiteError`` ("unknown": gone, "private": private or for
    subscribers only, "refused", "network") when there is none."""
    http = session or requests.Session()
    site = SITE_NAMES.get(media.site, media.site)
    try:
        if media.site == TWITCH and media.kind == VIDEO:
            url, title = _twitch_video(media.ident, http, stop, twitch_gql, twitch_vod, keep_dir)
        elif media.site == TWITCH:
            url, title = _twitch_clip(media.ident, http, stop, twitch_gql)
        elif media.kind == VIDEO:
            url, title = _kick_video(media.ident, http, stop, kick_video_api)
        else:
            url, title = _kick_clip(media.ident, http, stop, kick_clip_api)
    except SiteError as error:
        log.info("%s: a %s is %s.", site, media.kind, error.reason)
        raise
    finally:
        if session is None:
            http.close()
    log.info("%s: a %s plays.", site, media.kind)
    return Resolved(url, media_name(media, title))


def running_recording(
    page: SitePage,
    stop: threading.Event | None = None,
    session: requests_types.Session | None = None,
    *,
    kick_api: str = KICK_API,
    twitch_gql: str = TWITCH_GQL,
    twitch_vod: str = TWITCH_VOD,
) -> Recording | None:
    """The live channel's running broadcast's recording on its site (2.15), or None (past
    broadcasts are off, or it lists none yet). Raises ``SiteError`` when the site was not
    reached or refused."""
    http = session or requests.Session()
    site = SITE_NAMES.get(page.site, page.site)
    try:
        if page.site == TWITCH:
            found = _twitch_recording(page.channel, http, stop, twitch_gql, twitch_vod)
        else:
            found = _kick_recording(page.channel, http, stop, kick_api)
    except SiteError as error:
        log.info("%s: %s's recording: %s.", site, page.channel, error.reason)
        raise
    finally:
        if session is None:
            http.close()
    log.info("%s: %s %s.", site, page.channel, "is recorded" if found else "keeps no recording")
    return found


def _request(
    http: requests_types.Session,
    method: str,
    url: str,
    stop: threading.Event | None,
    **kwargs: Any,
) -> requests_types.Response:
    """One request with the browser User-Agent and no cookies. Every failure of the
    transport is "network"; the exception text is dropped (it can carry the address)."""
    if stop is not None and stop.is_set():
        raise SiteError("network")  # cancelled: the caller has stopped listening
    headers = {"User-Agent": USER_AGENT}
    headers.update(kwargs.pop("headers", {}))
    try:
        return http.request(method, url, headers=headers, timeout=TIMEOUT_S, **kwargs)
    except requests.RequestException as error:
        log.debug("A request to a site failed: %s.", type(error).__name__)
        raise SiteError("network", isinstance(error, requests.Timeout)) from None


def _json(response: requests_types.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _kick(
    channel: str, http: requests_types.Session, stop: threading.Event | None, api: str
) -> str:
    response = _request(http, "GET", f"{api}/{quote(channel, safe='')}", stop)
    status = response.status_code
    if status == 404:
        raise SiteError("unknown")
    if status in (403, 429, 503):
        raise SiteError("refused")
    if status != 200:
        raise SiteError("network")
    data = _json(response)
    if not isinstance(data, dict):
        raise SiteError("refused")  # a bot-check page or a changed answer
    live = data.get("livestream")
    if not isinstance(live, dict) or not live.get("is_live"):
        raise SiteError("offline")
    playback = data.get("playback_url")
    if not isinstance(playback, str) or not playback:
        raise SiteError("refused")
    return playback


def _twitch(
    channel: str,
    http: requests_types.Session,
    stop: threading.Event | None,
    gql: str,
    usher: str,
) -> str:
    body = {
        "operationName": "PlaybackAccessToken",
        "extensions": {"persistedQuery": {"version": 1, "sha256Hash": TWITCH_QUERY_HASH}},
        "variables": {
            "isLive": True,
            "login": channel,
            "isVod": False,
            "vodID": "",
            "playerType": "embed",
            "platform": "site",
        },
    }
    response = _request(http, "POST", gql, stop, json=body, headers={"Client-ID": TWITCH_CLIENT_ID})
    if response.status_code != 200:
        log.warning("Twitch's answer changed or was refused (HTTP %s).", response.status_code)
        raise SiteError("refused")
    data = _json(response)
    if isinstance(data, dict) and isinstance(data.get("errors"), list) and data["errors"]:
        # A rotted query hash ("PersistedQueryNotFound"), or any error beside what came.
        log.warning("Twitch's answer changed: it answered with an error.")
        raise SiteError("refused")
    answer = data.get("data") if isinstance(data, dict) else None
    if isinstance(answer, dict) and "streamPlaybackAccessToken" in answer:
        grant = answer["streamPlaybackAccessToken"]
        if grant is None:  # (measured: a login nobody has; an offline channel has a grant)
            raise SiteError("unknown")
    else:
        grant = None
    token = grant.get("value") if isinstance(grant, dict) else None
    signature = grant.get("signature") if isinstance(grant, dict) else None
    if not isinstance(token, str) or not isinstance(signature, str) or not token or not signature:
        log.warning("Twitch's answer changed: no playback access.")
        raise SiteError("refused")
    query = urlencode(
        {
            "sig": signature,
            "token": token,
            "allow_source": "true",
            "fast_bread": "true",
            "platform": "web",
            "playlist_include_framerate": "true",
            "multigroup_video": "true",
            "supported_codecs": "av1,h265,h264",
            "p": random.randint(0, 999999),
        },
        quote_via=quote,
        safe=",",
    )
    master = f"{usher}/{quote(channel, safe='')}.m3u8?{query}"
    response = _request(http, "GET", master, stop, stream=True)
    try:
        status = response.status_code
    finally:
        response.close()
    if status == 404:
        raise SiteError("offline")
    if status == 403:  # a channel for its subscribers only, or one kept from this region
        raise SiteError("refused")
    if status != 200:
        raise SiteError("network")
    return master


# ---- 2.15: videos, clips and the running broadcast's recording -----------------------------


def _gql(http: requests_types.Session, stop: threading.Event | None, gql: str, body: dict) -> dict:
    """One Twitch GQL request: its `data` (a dict); "refused" for an error or a changed
    answer."""
    response = _request(http, "POST", gql, stop, json=body, headers={"Client-ID": TWITCH_CLIENT_ID})
    if response.status_code != 200:
        log.warning("Twitch's answer changed or was refused (HTTP %s).", response.status_code)
        raise SiteError("refused")
    data = _json(response)
    if isinstance(data, dict) and isinstance(data.get("errors"), list) and data["errors"]:
        log.warning("Twitch's answer changed: it answered with an error.")
        raise SiteError("refused")
    answer = data.get("data") if isinstance(data, dict) else None
    if not isinstance(answer, dict):
        log.warning("Twitch's answer changed: no data.")
        raise SiteError("refused")
    return answer


def _vod_grant(
    ident: str, http: requests_types.Session, stop: threading.Event | None, gql: str
) -> tuple[str, str]:
    """A Twitch video's playback access (token, signature); "unknown" when it has none."""
    body = {
        "operationName": "PlaybackAccessToken",
        "extensions": {"persistedQuery": {"version": 1, "sha256Hash": TWITCH_QUERY_HASH}},
        "variables": {
            "isLive": False,
            "login": "",
            "isVod": True,
            "vodID": ident,
            "playerType": "embed",
            "platform": "site",
        },
    }
    answer = _gql(http, stop, gql, body)
    if "videoPlaybackAccessToken" not in answer:
        log.warning("Twitch's answer changed: no playback access.")
        raise SiteError("refused")
    grant = answer["videoPlaybackAccessToken"]
    if grant is None:
        raise SiteError("unknown")  # (measured 2026-11-25: a video nobody has)
    token = grant.get("value") if isinstance(grant, dict) else None
    signature = grant.get("signature") if isinstance(grant, dict) else None
    if not isinstance(token, str) or not isinstance(signature, str) or not token or not signature:
        log.warning("Twitch's answer changed: no playback access.")
        raise SiteError("refused")
    return token, signature


def _vod_master(
    ident: str,
    http: requests_types.Session,
    stop: threading.Event | None,
    gql: str,
    vod: str,
) -> tuple[str, str]:
    """A Twitch video's master playlist (its address, tokenized: never logged, and its text)."""
    token, signature = _vod_grant(ident, http, stop, gql)
    query = urlencode(
        {
            "sig": signature,
            "token": token,
            "allow_source": "true",
            "platform": "web",
            "playlist_include_framerate": "true",
            "supported_codecs": "av1,h265,h264",
            "p": random.randint(0, 999999),
        },
        quote_via=quote,
        safe=",",
    )
    master = f"{vod}/{quote(ident, safe='')}.m3u8?{query}"
    response = _request(http, "GET", master, stop)
    status = response.status_code
    if status == 403:
        raise SiteError("private")  # for its channel's subscribers only, or kept private
    if status in (404, 410):
        raise SiteError("unknown")
    if status != 200:
        raise SiteError("network")
    return master, response.text


def _source_variant(master_url: str, text: str) -> str:
    """The source variant's media playlist of a master, absolute: of those VLC plays a film
    in (H.264 or HEVC; else any), the tallest picture, then the higher frame rate, then the
    higher bandwidth (Kick's masters declare a 1080p60 source at less bandwidth than its
    720p60, measured 2026-11-25); the address itself when it is a media playlist. "refused"
    for a master that names none."""
    try:
        playlist = m3u8.loads(text, uri=master_url)
    except Exception:
        raise SiteError("refused") from None
    if not playlist.is_variant:
        if playlist.segments:
            return master_url
        raise SiteError("refused")
    best: tuple[tuple[int, int, int, int], str] | None = None
    for variant in playlist.playlists:
        info = variant.stream_info
        codecs = (info.codecs or "").lower()
        film = not codecs or any(
            codec.strip().split(".", 1)[0] in _FILM_CODECS for codec in codecs.split(",")
        )
        size = info.resolution
        height = int(size[1]) if isinstance(size, tuple) and len(size) == 2 else 0
        rate = round(float(info.frame_rate or 0))
        rank = (1 if film else 0, height, rate, int(info.bandwidth or 0))
        if best is None or rank > best[0]:
            best = (rank, urljoin(master_url, variant.uri))
    if best is None:
        raise SiteError("refused")
    return best[1]


def _variant_of(master_url: str, http: requests_types.Session, stop: threading.Event | None) -> str:
    """The source variant of the master at `master_url` (one request)."""
    response = _request(http, "GET", master_url, stop)
    if response.status_code in (404, 410):
        raise SiteError("unknown")
    if response.status_code == 403:
        raise SiteError("private")
    if response.status_code != 200:
        raise SiteError("network")
    return _source_variant(master_url, response.text)


def _title(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _twitch_video(
    ident: str,
    http: requests_types.Session,
    stop: threading.Event | None,
    gql: str,
    vod: str,
    keep_dir: str | None,
) -> tuple[str, str]:
    query = "query($id:ID!){video(id:$id){title status}}"
    answer = _gql(http, stop, gql, {"query": query, "variables": {"id": ident}})
    video = answer.get("video")
    if not isinstance(video, dict):
        raise SiteError("unknown")  # (measured 2026-11-25: a video nobody has is null)
    master, text = _vod_master(ident, http, stop, gql, vod)
    variant = _source_variant(master, text)
    if video.get("status") != "RECORDING":
        return variant, _title(video.get("title"))
    # Still recorded: its listing lists no end (VLC would play it live, unseekable).
    response = _request(http, "GET", variant, stop)
    if response.status_code != 200:
        raise SiteError("network")
    return _kept_listing(ident, variant, response.text, keep_dir), _title(video.get("title"))


def _kept_listing(ident: str, url: str, text: str, keep_dir: str | None) -> str:
    """A recording's listing as it stands, its addresses absolute and its end said, kept as a
    local file (its `file://` address): VLC plays it as a film, as far as it is listed."""
    try:
        playlist = m3u8.loads(text, uri=url)
    except Exception:
        raise SiteError("refused") from None
    if not playlist.segments:
        raise SiteError("refused")
    for segment in playlist.segments:
        segment.uri = urljoin(url, segment.uri)
        init = segment.init_section
        if init is not None and init.uri:
            init.uri = urljoin(url, init.uri)
    playlist.is_endlist = True
    playlist.playlist_type = "vod"
    folder = keep_dir or os.path.join(tempfile.gettempdir(), "qitv-sites")
    name = hashlib.sha1(ident.encode("utf-8")).hexdigest()[:16] + ".m3u8"
    path = os.path.join(folder, name)
    try:
        os.makedirs(folder, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(playlist.dumps())
    except OSError as error:
        log.warning("A recording's listing could not be kept (%s).", type(error).__name__)
        raise SiteError("network") from None
    return Path(path).as_uri()


def _twitch_clip(
    slug: str, http: requests_types.Session, stop: threading.Event | None, gql: str
) -> tuple[str, str]:
    query = (
        "query($s:ID!){clip(slug:$s){title"
        ' playbackAccessToken(params:{platform:"web",playerType:"site"}){signature value}'
        " videoQualities{quality frameRate sourceURL}}}"
    )
    answer = _gql(http, stop, gql, {"query": query, "variables": {"s": slug}})
    clip = answer.get("clip")
    if not isinstance(clip, dict):
        raise SiteError("unknown")  # (measured 2026-11-25: a slug nobody has is null)
    grant = clip.get("playbackAccessToken")
    token = grant.get("value") if isinstance(grant, dict) else None
    signature = grant.get("signature") if isinstance(grant, dict) else None
    qualities = clip.get("videoQualities")
    sources = [
        q["sourceURL"]
        for q in (qualities if isinstance(qualities, list) else [])
        if isinstance(q, dict) and isinstance(q.get("sourceURL"), str) and q["sourceURL"]
    ]
    if not isinstance(token, str) or not isinstance(signature, str) or not token or not sources:
        log.warning("Twitch's answer changed: no clip to play.")
        raise SiteError("refused")
    query_text = urlencode({"sig": signature, "token": token}, quote_via=quote)
    return f"{sources[0]}?{query_text}", _title(clip.get("title"))  # (best listed first)


def _kick_json(url: str, http: requests_types.Session, stop: threading.Event | None) -> dict:
    response = _request(http, "GET", url, stop)
    status = response.status_code
    if status in (404, 410):
        raise SiteError("unknown")
    if status in (403, 429, 503):
        raise SiteError("refused")
    if status != 200:
        raise SiteError("network")
    data = _json(response)
    if not isinstance(data, dict):
        raise SiteError("refused")  # a bot-check page or a changed answer
    return data


def _kick_video(
    uuid: str, http: requests_types.Session, stop: threading.Event | None, api: str
) -> tuple[str, str]:
    data = _kick_json(f"{api}/{quote(uuid, safe='')}", http, stop)
    if data.get("deleted_at") or data.get("is_pruned"):
        raise SiteError("unknown")
    if data.get("is_private"):
        raise SiteError("private")
    source = data.get("source")
    if not isinstance(source, str) or not source:
        raise SiteError("unknown")
    live = data.get("livestream")
    title = _title(live.get("session_title")) if isinstance(live, dict) else ""
    return _variant_of(source, http, stop), title


def _kick_clip(
    ident: str, http: requests_types.Session, stop: threading.Event | None, api: str
) -> tuple[str, str]:
    data = _kick_json(f"{api}/{quote(ident, safe='')}", http, stop)
    clip = data.get("clip")
    if not isinstance(clip, dict):
        raise SiteError("unknown")
    privacy = clip.get("privacy")
    if isinstance(privacy, str) and privacy and privacy != "public":
        raise SiteError("private")
    source = clip.get("clip_url") or clip.get("video_url")
    if not isinstance(source, str) or not source:
        raise SiteError("unknown")
    return _variant_of(source, http, stop), _title(clip.get("title"))


def _when(text: Any) -> float | None:
    """A site's time (ISO 8601, "2026-11-25T09:58:35.031Z" or "2026-11-25 09:58:38", UTC)
    as Unix seconds, or None."""
    if not isinstance(text, str) or not text:
        return None
    try:
        when = datetime.fromisoformat(text.strip().replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.timestamp()


def _twitch_recording(
    channel: str,
    http: requests_types.Session,
    stop: threading.Event | None,
    gql: str,
    vod: str,
) -> Recording | None:
    query = (
        "query($l:String!){user(login:$l){stream{archiveVideo{id status createdAt lengthSeconds}}}}"
    )
    answer = _gql(http, stop, gql, {"query": query, "variables": {"l": channel}})
    user = answer.get("user")
    stream = user.get("stream") if isinstance(user, dict) else None
    video = stream.get("archiveVideo") if isinstance(stream, dict) else None
    if not isinstance(video, dict) or video.get("status") != "RECORDING":
        return None  # past broadcasts are off (or it isn't live)
    ident, start = video.get("id"), _when(video.get("createdAt"))
    if not isinstance(ident, str) or not _TWITCH_VIDEO.fullmatch(ident) or start is None:
        return None
    master, text = _vod_master(ident, http, stop, gql, vod)
    length = video.get("lengthSeconds")
    until = start + float(length) if isinstance(length, (int, float)) else None
    return Recording(_source_variant(master, text), start, dated=False, steps=False, until=until)


def _kick_recording(
    channel: str, http: requests_types.Session, stop: threading.Event | None, api: str
) -> Recording | None:
    data = _kick_json_list(f"{api}/{quote(channel, safe='')}/videos", http, stop)
    row = data[0] if data else None
    if not isinstance(row, dict) or row.get("is_live") is not True:
        return None
    source = row.get("source")
    if not isinstance(source, str) or not source:
        return None  # past broadcasts are off
    variant = _variant_of(source, http, stop)
    response = _request(http, "GET", variant, stop)
    if response.status_code != 200:
        return None
    try:
        playlist = m3u8.loads(response.text, uri=variant)
    except Exception:
        return None
    first = playlist.segments[0] if playlist.segments else None
    dated = first.program_date_time if first is not None else None
    listed = sum(float(segment.duration or 0) for segment in playlist.segments)
    if dated is not None:
        start = (dated if dated.tzinfo else dated.replace(tzinfo=timezone.utc)).timestamp()
        return Recording(variant, start, dated=True, steps=True, until=start + listed)
    start = _when(row.get("start_time"))
    if start is None:
        return None
    return Recording(variant, start, dated=False, steps=True, until=start + listed)


def _kick_json_list(url: str, http: requests_types.Session, stop: threading.Event | None) -> list:
    response = _request(http, "GET", url, stop)
    status = response.status_code
    if status in (404, 410):
        raise SiteError("unknown")
    if status in (403, 429, 503):
        raise SiteError("refused")
    if status != 200:
        raise SiteError("network")
    data = _json(response)
    if isinstance(data, dict):
        data = data.get("data")
    return data if isinstance(data, list) else []
