"""Whether a provider answers, as Settings checks it before keeping it (blocking).

An STB portal or an Xtream panel is asked as the account check asks it (services.account_check:
no stream is opened); a playlist must answer a HEAD (Free radio's directory a GET), or be a file
on this computer; a Jellyfin or Emby server is signed in to. Each is asked over HTTPS first, and
one that answered only over HTTP says so (2.7.1). A media server's sign-in hands back its access
(a token, the user's id and the device's id) to keep in place of the password, which is used
here only; `check_kept` asks one already added (its token kept, no password) with its token
instead. A Plex server (2.14) is always asked with its token, which its sign-in by a code gave.

`check` runs on a worker thread (ui.provider_check runs it for Settings) and answers an
`Outcome`, plain data. Nothing here logs a URL or a credential: the failure's type and host.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import os
from typing import Any

from services import failure_reasons as reasons, free_radio
from services.account_check import check_account
from services.lazy_import import lazy_module
from services.provider_api import (
    CLIENT_HEADERS,
    https_fallback_candidates,
    known_http_only,
    remember_http_only,
)
from services.provider_types import is_media_server, is_plex
from services.redact import safe_exc

logger = logging.getLogger(__name__)
requests = lazy_module("requests")

PLAYLIST_TYPES = ("M3UPLAYLIST", "M3USTREAM")
# An account at its stream limit is still a working account, and a portal profile dated in the
# past alone is only warned of (such dates go stale).
_WORKING = (reasons.OK, reasons.LIMIT, reasons.DATED)
# What a media server's sign-in hands back to keep (never the password).
ACCESS_KEYS = ("token", "user_id", "device_id")


@dataclass(frozen=True)
class Outcome:
    """A check's answer. `why`: the plain reason it failed, or the note on a working one (a
    portal profile dated in the past), in the active language; "" when there is none.
    `http_only`: the provider answered only over HTTP. `access`: a media server's sign-in."""

    ok: bool
    why: str = ""
    http_only: bool = False
    access: dict[str, Any] = field(default_factory=dict)


def check(provider: dict, ssl_verify: bool, password: str = "") -> Outcome:
    """Ask `provider` whether it answers (blocking: run it on a worker). `ssl_verify` is the
    global setting, which the provider's own overrides; `password`, when given, is asked in
    place of the provider's (a media server's is never stored). `provider` is left as it is."""
    if password:
        provider = {**provider, "password": password}
    verify = bool(provider.get("ssl_verify", ssl_verify))
    kind = str(provider.get("type") or "")
    ok, why, http_only = False, "", False
    access: dict[str, Any] = {}
    try:
        if kind in ("STB", "XTREAM"):
            found = check_account(provider, ssl_verify=verify)
            ok = found is not None and found.kind in _WORKING
            if found is not None and found.kind != reasons.LIMIT and found.plain:
                why = found.text  # why it failed, or (DATED) the warning
        elif kind in PLAYLIST_TYPES:
            ok = verify_url(str(provider.get("url") or ""), verify_ssl=verify)
        elif is_plex(kind):  # 2.14: its token, from its sign-in by a code (never a password)
            ok, why = _plex_access(provider, verify)
        elif is_media_server(kind):
            ok, why, access = _sign_in(provider, verify)
        http_only = ok and not is_media_server(kind) and answered_only_over_http(provider, verify)
    except Exception as e:
        # requests exceptions name the URL, credentials and all: the type and host only.
        logger.warning("Provider verification failed: %s", safe_exc(e))
    return Outcome(ok, why, http_only, access)


def _sign_in(provider: dict, verify: bool) -> tuple[bool, str, dict[str, Any]]:
    """A Jellyfin or Emby server's sign-in (2.8): (signed in, why not, its access)."""
    from services import media_workers

    ok, value = media_workers.sign_in(  # the calling thread's: it blocks only the check
        str(provider.get("url") or ""),
        str(provider.get("type") or ""),
        str(provider.get("username") or ""),
        str(provider.get("password") or ""),
        verify,
        device_id=str(provider.get("device_id") or ""),
    )
    if not ok:
        return False, str(value), {}
    return True, "", {key: value[key] for key in ACCESS_KEYS if key in value}


def _plex_access(provider: dict, verify: bool) -> tuple[bool, str]:
    """A Plex server asked with its kept token (2.14): (it answered, why not)."""
    from services import plex_workers

    return plex_workers.check_access(provider, verify)


def check_kept(provider: dict, ssl_verify: bool) -> Outcome:
    """`check` for a provider as it is kept (Settings' Check the connection, blocking): a
    Jellyfin or Emby server, which keeps no password, is asked with its kept token instead of
    a sign-in. A form being edited (another user name, no password typed) is never asked so:
    it signs in with `check`."""
    kind = str(provider.get("type") or "")
    if not is_media_server(kind) or not provider.get("token") or provider.get("password"):
        return check(provider, ssl_verify)
    if is_plex(kind):
        return check(provider, ssl_verify)  # (Plex's is always its kept token)
    from services import media_workers

    try:
        ok, why = media_workers.check_access(provider, bool(provider.get("ssl_verify", ssl_verify)))
    except Exception as e:
        logger.warning("Provider verification failed: %s", safe_exc(e))
        return Outcome(False)
    return Outcome(ok, why)


def answered_only_over_http(provider: dict, verify: bool) -> bool:
    """Whether the working `provider`, asked over HTTPS first, answered only over HTTP: an
    Xtream panel at neither its address's HTTPS form nor the HTTPS port it advertises; a
    portal or a playlist at its address's HTTPS form."""
    url = str(provider.get("url") or "").strip()
    if not url.startswith("http://"):
        return False
    if provider.get("type") == "XTREAM":
        from services.workers import xtream_answers_https

        username, password = provider.get("username"), provider.get("password")
        return not xtream_answers_https(
            url, str(username or ""), str(password or ""), verify_ssl=verify
        )
    return known_http_only(url)


def verify_url(url: str, *, verify_ssl: bool = True) -> bool:
    """Whether the playlist at `url` answers (over HTTPS first: an HTTP address that answers
    only over HTTP is remembered so, `known_http_only`), or is a file on this computer."""
    if not url.startswith(("http://", "https://")):
        return os.path.isfile(url)
    if free_radio.is_directory_url(url):
        return _directory_answers(url, verify_ssl)
    try:
        for candidate in https_fallback_candidates(url, True):
            try:
                response = requests.head(
                    candidate, timeout=5, headers=CLIENT_HEADERS, verify=verify_ssl
                )
            except requests.RequestException:
                continue
            if response.status_code == 200:
                if candidate.startswith("http://"):
                    remember_http_only(candidate)
                return True
        return False
    except Exception as e:
        logger.warning("Error verifying URL: %s", safe_exc(e))
        return False


def _directory_answers(url: str, verify_ssl: bool) -> bool:
    """Whether the radio directory (Free radio, 2.13) answers for its list at `url`: it finds
    nothing at a HEAD, so a GET, closed once its status came (none of the list is read)."""
    try:
        response = requests.get(
            url, timeout=5, headers=free_radio.headers_for(url), verify=verify_ssl, stream=True
        )
    except requests.RequestException as e:
        logger.warning("Error verifying URL: %s", safe_exc(e))
        return False
    try:
        return bool(response.status_code == 200)
    finally:
        response.close()
