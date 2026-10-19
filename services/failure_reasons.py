"""Why a provider failed, in plain words: the one place QiTV words a provider's failures (no
widgets).

The catalog's failure messages (`friendly_error`), an HTTP status, and an account check's answer
(`services.account_check`: Xtream's `user_info`, an STB portal's handshake and profile) all come
here for their words. Every text is fixed or built from numbers and dates only: never a URL,
a host or a credential.

The catalog's messages stay English (services.catalog compares them, and `status_message` gives
them so); `shown_message` shows one in the active language. A `Reason`'s text is made in the
active language.

The time-shift helper's own refusal texts (services/live/refusals.py: STREAM_LIMIT and
STREAM_REFUSED) stay there, free of imports for the helper's process; the account check that
follows a refusal says it in these words, with the account's numbers.

A `Reason` is what an account check found: its `kind` (one of the constants below) and its
text. Only a `plain` one says more than the failure already did (`OK`: the account is fine;
`UNEXPECTED`: the answer said nothing).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
from typing import Any, Iterable, Optional

from services.i18n import N_, tr, tr_n, translate

# --- the catalog's fixed failure messages (services.catalog re-exports them) -----------------
# English, as the catalog compares them: `translate` (or `shown_message`) shows them.
UNREACHABLE_MESSAGE = N_("Couldn't reach the provider")
LOGIN_REFUSED_MESSAGE = N_("The provider refused the login")
TOO_SLOW_MESSAGE = N_("The provider took too long to answer")
UNEXPECTED_ANSWER_MESSAGE = N_("The provider sent an unexpected answer")
# HTTP answers worth their own words (the code stays: it is what a provider's support asks for).
FORBIDDEN_MESSAGE = N_("The server refused QiTV (403)")
NOT_FOUND_MESSAGE = N_("The server has nothing at this address (404)")
PROXY_MESSAGE = N_("A proxy on your network asks for a sign-in (407)")
# The English start of a block's catalog message (services.catalog lets a message starting
# with it through) and of a server error's; their whole texts: blocked_text, server_text.
BLOCKED_PREFIX = "The provider blocked this connection"
SERVER_TROUBLE_PREFIX = "The provider's server has a problem"
# What an account check found.
WRONG_LOGIN_MESSAGE = N_("The username or password is wrong")
EXPIRED_MESSAGE = N_("Your subscription has expired")
DISABLED_MESSAGE = N_("The provider has disabled this account")
DEVICE_MESSAGE = N_("The provider rejected this device (check the MAC address or subscription)")

# Reason kinds.
OK = "ok"
UNEXPECTED = "unexpected"
EXPIRED = "expired"
# A portal's profile dated in the past while the portal still answers: such dates go stale on
# some portals, so it is said as the portal's word (and Verify Provider only warns of it).
DATED = "dated"
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
# A block's catalog message, as `status_message` words it (in English).
_BLOCKED_MESSAGE = re.compile(
    re.escape(BLOCKED_PREFIX) + r" \((\d+)\); it may not serve your region"
)


@dataclass(frozen=True)
class Reason:
    kind: str
    text: str  # in the active language: what Verify Provider and the toasts show
    # A LIMIT's numbers, as the account reported them (the GUI compares them with QiTV's own
    # streams at that provider, `own_streams_text`).
    limit: Optional[int] = field(default=None, compare=False)
    active: Optional[int] = field(default=None, compare=False)

    @property
    def plain(self) -> bool:
        """Whether it says why (an account check that found the account fine says nothing)."""
        return self.kind not in (OK, UNEXPECTED)


def limit_text(limit: int, active: int) -> str:
    """ "Your account allows 1 stream; one is in use" (the account's own count)."""
    if active <= 1 or limit == 1:
        return tr_n(
            "Your account allows {n} stream; one is in use",
            "Your account allows {n} streams; one is in use",
            limit,
        )
    if limit == 2:
        return tr("Your account allows 2 streams; both are in use")
    if active >= limit:
        return tr_n(
            # i18n: n (the streams the account allows) is 3 or more here
            "Your account allows {n} stream; all {n} are in use",
            "Your account allows {n} streams; all {n} are in use",
            limit,
        )
    return tr_n(
        # i18n: n (the streams the account allows) is 3 or more; active (in use) 2 or more
        "Your account allows {n} stream; {active} are in use",
        "Your account allows {n} streams; {active} are in use",
        limit,
        active=active,
    )


def own_streams_text(limit: Optional[int]) -> Optional[str]:
    """A LIMIT whose streams in use are all QiTV's own (its other picture, say): None when
    the limit is not known (the failure's own words stay)."""
    if not limit:
        return None
    if limit == 1:
        return tr("QiTV's other picture is using the account's only stream")
    return tr_n(
        # i18n: n (the streams the account allows) is 2 or more here
        "QiTV is already using all {n} streams your account allows",
        "QiTV is already using all {n} streams your account allows",
        limit,
    )


def _day(when: datetime) -> str:
    """ "3 Oct 2026", the month as the active language names it."""
    from PySide6.QtCore import QDate

    from services.i18n import app_locale

    return str(app_locale().toString(QDate(when.year, when.month, when.day), "d MMM yyyy"))


def expired_text(when: Optional[datetime]) -> str:
    if when is None:
        return translate(EXPIRED_MESSAGE)
    # i18n: date as "3 Oct 2026"
    return tr("Your subscription expired on {date}", date=_day(when))


def dated_text(when: datetime) -> str:
    # i18n: date as "3 Oct 2026"
    return tr("The portal says your subscription ended on {date}", date=_day(when))


def blocked_text(status: int) -> str:
    return tr(
        # i18n: status is the server's answer code (451, 456 or 458)
        "The provider blocked this connection ({status}); it may not serve your region",
        status=status,
    )


def server_text(status: int) -> str:
    # i18n: status is the server's answer code (500 to 599)
    return tr("The provider's server has a problem ({status})", status=status)


def status_message(status: int) -> Optional[str]:
    """The catalog's words for a refusal status that has its own: 403, 404, 407 and the blocks
    (None: the caller's own message, also for a 401 or a server error, which mean more than
    one thing to a catalog request). English, as the catalog keeps its messages
    (`shown_message` shows one)."""
    if status == 403:
        return FORBIDDEN_MESSAGE
    if status == 404:
        return NOT_FOUND_MESSAGE
    if status == 407:
        return PROXY_MESSAGE
    if status in BLOCKED_STATUS:
        return f"{BLOCKED_PREFIX} ({status}); it may not serve your region"
    return None


def shown_message(message: str) -> str:
    """A catalog failure message (kept in English: services.catalog compares them) in the
    active language, where it is shown; any other text comes back as `translate` gives it."""
    blocked = _BLOCKED_MESSAGE.fullmatch(message)
    if blocked is not None:
        return blocked_text(int(blocked.group(1)))
    return translate(message)


def status_reason(status: Optional[int]) -> Optional[Reason]:
    """What an HTTP error status means (None for a success or a status with no words)."""
    if status is None:
        return None
    if status in _WRONG_LOGIN_STATUS:
        return Reason(WRONG_LOGIN, translate(WRONG_LOGIN_MESSAGE))
    if status == 403:
        return Reason(FORBIDDEN, translate(FORBIDDEN_MESSAGE))
    if status == 404:
        return Reason(NOT_FOUND, translate(NOT_FOUND_MESSAGE))
    if status == 407:
        return Reason(PROXY, translate(PROXY_MESSAGE))
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
        # [] or {} : the panel's refusal
        return Reason(WRONG_LOGIN, translate(WRONG_LOGIN_MESSAGE))
    status = str(user_info.get("status") or "").strip().lower()
    expires = _date(user_info.get("exp_date"))
    if status == "expired" or (expires is not None and expires <= now):
        return Reason(
            EXPIRED, expired_text(expires if expires is not None and expires <= now else None)
        )
    if status in ("banned", "disabled"):
        return Reason(DISABLED, translate(DISABLED_MESSAGE))
    if str(user_info.get("auth", "")).strip() != "1":
        return Reason(WRONG_LOGIN, translate(WRONG_LOGIN_MESSAGE))
    limit, active = _int(user_info.get("max_connections")), _int(user_info.get("active_cons"))
    if limit and limit > 0 and active is not None and active >= limit:
        return Reason(LIMIT, limit_text(limit, active), limit=limit, active=active)
    return Reason(OK, "")


def stb_profile_reason(profile: Any, now: datetime) -> Optional[Reason]:
    """What a portal's get_profile answer (its "js") says beyond the device being known: a
    blocked account, or an expiry date in the past (DATED: the portal's word, as such dates
    go stale on some portals). None when it says nothing of either."""
    if not isinstance(profile, dict):
        return None
    if str(profile.get("blocked", "")).strip() == "1":
        return Reason(DISABLED, translate(DISABLED_MESSAGE))
    for key in _EXPIRY_KEYS:
        expires = _date(profile.get(key))
        if expires is not None and expires <= now:
            return Reason(DATED, dated_text(expires))
    return None


def unreachable(timed_out: bool) -> Reason:
    if timed_out:
        return Reason(TIMEOUT, translate(TOO_SLOW_MESSAGE))
    return Reason(UNREACHABLE, translate(UNREACHABLE_MESSAGE))
