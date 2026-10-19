"""Why a provider failed, in plain words: the one place QiTV words a provider's failures (no Qt).

The catalog's failure messages (`friendly_error`), an HTTP status, and an account check's answer
(`services.account_check`: Xtream's `user_info`, an STB portal's handshake and profile) all come
here for their words. Every text is fixed or built from numbers and dates only: never a URL,
a host or a credential.

A `Reason` is what an account check found: its `kind` (one of the constants below) and its
text. Only a `plain` one says more than the failure already did (`OK`: the account is fine;
`UNEXPECTED`: the answer said nothing).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

# --- the catalog's fixed failure messages (services.catalog re-exports them) -----------------
UNREACHABLE_MESSAGE = "Couldn't reach the provider"
LOGIN_REFUSED_MESSAGE = "The provider refused the login"
TOO_SLOW_MESSAGE = "The provider took too long to answer"
UNEXPECTED_ANSWER_MESSAGE = "The provider sent an unexpected answer"
# HTTP answers worth their own words (the code stays: it is what a provider's support asks for).
FORBIDDEN_MESSAGE = "The server refused QiTV (403)"
NOT_FOUND_MESSAGE = "The server has nothing at this address (404)"
PROXY_MESSAGE = "A proxy on your network asks for a sign-in (407)"
BLOCKED_PREFIX = "The provider blocked this connection"
SERVER_TROUBLE_PREFIX = "The provider's server has a problem"
# What an account check found.
WRONG_LOGIN_MESSAGE = "The username or password is wrong"
EXPIRED_MESSAGE = "Your subscription has expired"
DISABLED_MESSAGE = "The provider has disabled this account"
DEVICE_MESSAGE = "The provider rejected this device (check the MAC address or subscription)"

# Reason kinds.
OK = "ok"
UNEXPECTED = "unexpected"
EXPIRED = "expired"
LIMIT = "limit"
WRONG_LOGIN = "wrong_login"
DISABLED = "disabled"
DEVICE = "device"
FORBIDDEN = "forbidden"
NOT_FOUND = "not_found"
PROXY = "proxy"
BLOCKED = "blocked"
SERVER = "server"
UNREACHABLE = "unreachable"
TIMEOUT = "timeout"

# Answers a provider gives when it will not serve this client, its region or its network:
# 451 (unavailable for legal reasons) and the panels' own 456/458.
BLOCKED_STATUS = (451, 456, 458)
# An answer with no body that some panels give a wrong password (xtream_signed_in's note).
_WRONG_LOGIN_STATUS = (401, 513)
# Of several answers (portal paths, HTTPS then HTTP), the one that explains most wins: a proxy
# or a block says more than a refusal, and a "not found" may only be the wrong portal path.
_STATUS_RANK = {407: 6, 451: 5, 456: 5, 458: 5, 403: 4, 401: 3, 513: 3}
_EXPIRY_KEYS = ("expire_billing_date", "end_date", "expire_date", "tariff_expired_date")
_NO_DATE = ("", "0", "0000-00-00", "0000-00-00 00:00:00", "null", "none")


@dataclass(frozen=True)
class Reason:
    kind: str
    text: str

    @property
    def plain(self) -> bool:
        """Whether it says why (an account check that found the account fine says nothing)."""
        return self.kind not in (OK, UNEXPECTED)


def _streams(count: int) -> str:
    return "1 stream" if count == 1 else f"{count} streams"


def limit_text(limit: int, active: int) -> str:
    """ "Your account allows 1 stream; one is in use" (the account's own count)."""
    if active <= 1:
        used = "one is in use"
    elif limit == 2:
        used = "both are in use"
    elif active >= limit:
        used = f"all {limit} are in use"
    else:
        used = f"{active} are in use"
    return f"Your account allows {_streams(limit)}; {used}"


def expired_text(when: Optional[datetime]) -> str:
    if when is None:
        return EXPIRED_MESSAGE
    return f"Your subscription expired on {when.day} {when.strftime('%b %Y')}"


def blocked_text(status: int) -> str:
    return f"{BLOCKED_PREFIX} ({status}); it may not serve your region"


def server_text(status: int) -> str:
    return f"{SERVER_TROUBLE_PREFIX} ({status})"


def status_message(status: int) -> Optional[str]:
    """The words for a refusal status that has its own: 403, 404, 407 and the blocks (None:
    the caller's own message, also for a 401 or a server error, which mean more than one
    thing to a catalog request)."""
    reason = status_reason(status)
    if reason is None or reason.kind not in (FORBIDDEN, NOT_FOUND, PROXY, BLOCKED):
        return None
    return reason.text


def status_reason(status: Optional[int]) -> Optional[Reason]:
    """What an HTTP error status means (None for a success or a status with no words)."""
    if status is None:
        return None
    if status in _WRONG_LOGIN_STATUS:
        return Reason(WRONG_LOGIN, WRONG_LOGIN_MESSAGE)
    if status == 403:
        return Reason(FORBIDDEN, FORBIDDEN_MESSAGE)
    if status == 404:
        return Reason(NOT_FOUND, NOT_FOUND_MESSAGE)
    if status == 407:
        return Reason(PROXY, PROXY_MESSAGE)
    if status in BLOCKED_STATUS:
        return Reason(BLOCKED, blocked_text(status))
    if 500 <= status < 600:
        return Reason(SERVER, server_text(status))
    return None


def strongest_status(statuses: Iterable[int]) -> Optional[int]:
    """Of the error statuses several attempts got, the one that explains most; a 404 only when
    every attempt got one (a portal tried at its other path answers 404 first)."""
    statuses = list(statuses)
    if not statuses:
        return None
    ranked = [s for s in statuses if s in _STATUS_RANK]
    if ranked:
        return max(ranked, key=lambda s: _STATUS_RANK[s])
    if all(s == 404 for s in statuses):
        return 404
    others = [s for s in statuses if s != 404]
    return others[-1]


def _int(value: Any) -> Optional[int]:
    try:
        return int(str(value).strip())
    except TypeError, ValueError:
        return None


def _date(value: Any) -> Optional[datetime]:
    """A provider's expiry: a Unix time (Xtream's exp_date) or a portal's "YYYY-mm-dd[ HH:MM:SS]",
    taken as UTC; None for no date (unlimited)."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if text.lower() in _NO_DATE:
        return None
    stamp = _int(text)
    if stamp is not None:
        if stamp <= 0:
            return None
        try:
            return datetime.fromtimestamp(stamp, timezone.utc)
        except OverflowError, OSError, ValueError:
            return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def xtream_reason(body: Any, now: datetime) -> Reason:
    """What an Xtream Player API sign-in answer (no action) says about the account.

    `body` is the parsed JSON (None when the panel sent none). `user_info` carries `auth`,
    `status` ("Active", "Expired", "Banned", "Disabled"), `exp_date` (a Unix time or null),
    `max_connections` and `active_cons`.
    """
    user_info = body.get("user_info") if isinstance(body, dict) else None
    if not isinstance(user_info, dict):
        return Reason(WRONG_LOGIN, WRONG_LOGIN_MESSAGE)  # [] or {} : the panel's refusal
    status = str(user_info.get("status") or "").strip().lower()
    expires = _date(user_info.get("exp_date"))
    if status == "expired" or (expires is not None and expires <= now):
        return Reason(
            EXPIRED, expired_text(expires if expires is not None and expires <= now else None)
        )
    if status in ("banned", "disabled"):
        return Reason(DISABLED, DISABLED_MESSAGE)
    if str(user_info.get("auth", "")).strip() != "1":
        return Reason(WRONG_LOGIN, WRONG_LOGIN_MESSAGE)
    limit, active = _int(user_info.get("max_connections")), _int(user_info.get("active_cons"))
    if limit and limit > 0 and active is not None and active >= limit:
        return Reason(LIMIT, limit_text(limit, active))
    return Reason(OK, "")


def stb_profile_reason(profile: Any, now: datetime) -> Optional[Reason]:
    """What a portal's get_profile answer (its "js") says beyond the device being known: a
    blocked account, or an expiry in the past. None when it says nothing of either."""
    if not isinstance(profile, dict):
        return None
    if str(profile.get("blocked", "")).strip() == "1":
        return Reason(DISABLED, DISABLED_MESSAGE)
    for key in _EXPIRY_KEYS:
        expires = _date(profile.get(key))
        if expires is not None and expires <= now:
            return Reason(EXPIRED, expired_text(expires))
    return None


def unreachable(timed_out: bool) -> Reason:
    return (
        Reason(TIMEOUT, TOO_SLOW_MESSAGE) if timed_out else Reason(UNREACHABLE, UNREACHABLE_MESSAGE)
    )
