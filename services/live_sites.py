"""Resolve a Kick or Twitch channel page to a fresh master playlist address.

No Qt here. A site's playlist address carries a short-lived token, so it is asked again for
every play and never logged or stored; logs name the site, the channel and the outcome only.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import random
import re
import threading
from urllib.parse import quote, urlencode, urlsplit

import requests

log = logging.getLogger(__name__)


class _NoAddresses(logging.Filter):
    """urllib3 logs each request line at DEBUG, and the usher address carries the token."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "token=" not in record.getMessage()


logging.getLogger("urllib3.connectionpool").addFilter(_NoAddresses())

KICK, TWITCH = "kick", "twitch"
SITE_NAMES = {KICK: "Kick", TWITCH: "Twitch"}

KICK_API = "https://kick.com/api/v2/channels"
TWITCH_GQL = "https://gql.twitch.tv/gql"
TWITCH_USHER = "https://usher.ttvnw.net/api/channel/hls"
TWITCH_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
TWITCH_QUERY_HASH = "ed230aa1e33e07eebb8928504583da78a5173989fadfb1ac94be06a04f3cdbe9"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
)
TIMEOUT_S = 10

_HOSTS = {"kick.com": KICK, "twitch.tv": TWITCH}
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


@dataclass(frozen=True)
class SitePage:
    site: str  # KICK | TWITCH
    channel: str  # lower-case slug/login


@dataclass(frozen=True)
class Resolved:
    url: str  # the master playlist (tokenized; never logged)
    name: str  # "examplechannel · Kick"


class SiteError(Exception):
    """``reason`` is "offline", "unknown", "refused" or "network"."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


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
    except ValueError:
        return None
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
    return SitePage(site, channel)


def resolve(
    page: SitePage,
    stop: threading.Event | None = None,
    session: requests.Session | None = None,
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
    return Resolved(url, f"{page.channel} · {SITE_NAMES[page.site]}")


def _request(http, method, url, stop, **kwargs):
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
        raise SiteError("network") from None


def _json(response):
    try:
        return response.json()
    except ValueError:
        return None


def _kick(channel, http, stop, api):
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


def _twitch(channel, http, stop, gql, usher):
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
    grant = None
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        grant = data["data"].get("streamPlaybackAccessToken")
    token = grant.get("value") if isinstance(grant, dict) else None
    signature = grant.get("signature") if isinstance(grant, dict) else None
    if not isinstance(token, str) or not isinstance(signature, str) or not token or not signature:
        # A missing channel and a rotted query hash both read this way: not told apart.
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
    if status != 200:
        raise SiteError("network")
    return master
