"""Ask a provider why something failed, without opening a stream (QiTV 2.2).

When a channel or a film fails to open, or a provider can't be reached, the account itself is
asked: Xtream's Player API sign-in (`player_api.php` without an action: its `user_info` has
the status, the expiry, the stream limit and the streams in use), an STB portal's profile.
Neither opens a stream, so the check never takes one of the account's connections. A portal
signed in already is asked on that session's own token (a new handshake would take a new one,
and portals bind one token to a MAC: the stream it plays would lose its own); one that is not
gets a fresh handshake. An M3U playlist from an Xtream panel (its get.php link,
services.provider_link) is asked as Xtream; another playlist has no account to ask.

`check_account` runs on a worker thread (blocking, a few seconds at most). `AccountChecks`
runs it on a `JobRunner` when the GUI asks, at most once a minute per provider and account (a
password or MAC fixed in Settings is asked again at once): a failure within the minute gets
the last answer (a check under way answers every caller). The answers
are `failure_reasons.Reason`s, plain data; their words are failure_reasons'. Nothing here logs
a URL, a host or a credential: only the reason's kind.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import logging
import time
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, Signal

from services import failure_reasons as reasons
from services.failure_reasons import Reason
from services.i18n import translate
from services.jobs import JobRunner
from services.lazy_import import lazy_module
from services.provider_api import (
    CLIENT_HEADERS,
    https_fallback_candidates,
    provider_prefers_https,
    xtream_player_api_url,
)
from services.provider_link import XTREAM, parse_provider_link

logger = logging.getLogger(__name__)
requests = lazy_module("requests")

CHECK_EVERY_S = 60.0  # at most one check a minute per provider
TIMEOUT_S = (4.0, 6.0)  # connect, read: a check under way holds a quitting QiTV back


def check_xtream(
    base: str,
    username: str,
    password: str,
    *,
    prefer_https: bool = False,
    ssl_verify: bool = True,
    now: Optional[datetime] = None,
    timeout: Any = TIMEOUT_S,
) -> Reason:
    """The account's state from its Player API sign-in answer (HTTPS first when preferred)."""
    now = now or datetime.now(timezone.utc)
    url = xtream_player_api_url(base, username, password)
    statuses: list[int] = []
    timed_out = False
    for candidate in https_fallback_candidates(url, prefer_https):
        try:
            response = requests.get(
                candidate, timeout=timeout, headers=CLIENT_HEADERS, verify=ssl_verify
            )
        except requests.exceptions.Timeout:
            timed_out = True
            continue
        except requests.RequestException:
            continue
        try:
            if response.status_code != 200:
                statuses.append(int(response.status_code))
                continue
            try:
                body = response.json() if response.content else None
            except ValueError:
                return Reason(reasons.UNEXPECTED, "")  # not the Player API's JSON
            return reasons.xtream_reason(body, now)
        finally:
            response.close()
    return _from_statuses(statuses, timed_out)


def check_stb(
    url: str,
    mac: str,
    *,
    serial_number: str = "",
    device_id: str = "",
    prefer_https: bool = False,
    ssl_verify: bool = True,
    now: Optional[datetime] = None,
    session: Optional[dict] = None,
) -> Reason:
    """The portal's answer to a profile request (no play link is made). With `session` (the
    headers of the provider's signed-in session, its token among them) only the profile is
    asked for: a fresh handshake would take a new token, and portals bind one token to a MAC,
    so the stream that session plays would lose its own. Without one, a fresh handshake."""
    from services.provider_manager import HANDSHAKE_REJECTED, HANDSHAKE_UNREACHABLE, StbHandshake

    now = now or datetime.now(timezone.utc)
    handshake = StbHandshake(prefer_https=prefer_https, ssl_verify=ssl_verify)
    if session:
        known = handshake.check_profile(url, mac, session)
        return _from_profile(handshake, known, now)
    accepted = handshake.run(url, mac, serial_number=serial_number, device_id=device_id)
    found = reasons.stb_profile_reason(handshake.profile, now)
    if found is not None:
        return found
    if accepted:
        return Reason(reasons.OK, "")
    by_status = reasons.status_reason(reasons.strongest_status(handshake.statuses))
    if by_status is not None and by_status.kind == reasons.WRONG_LOGIN:
        by_status = None  # a portal has no password: its 401 refuses the device
    if by_status is not None and by_status.kind != reasons.NOT_FOUND:
        return by_status
    if handshake.failure == HANDSHAKE_REJECTED:
        return Reason(reasons.DEVICE, translate(reasons.DEVICE_MESSAGE))
    if by_status is not None:
        return by_status  # every path answered 404: the portal's address is wrong
    if handshake.failure == HANDSHAKE_UNREACHABLE:
        return reasons.unreachable(handshake.timed_out)
    return Reason(reasons.UNEXPECTED, "")


def _from_profile(handshake: Any, known: bool, now: datetime) -> Reason:
    """What a profile request on the session's own token found. A profile that does not
    know the device, a 401 or a 404 may only mean that token went stale or the path was the
    other one: they say nothing."""
    found = reasons.stb_profile_reason(handshake.profile, now)
    if found is not None:
        return found
    if known:
        return Reason(reasons.OK, "")
    if handshake.profile is not None:
        return Reason(reasons.UNEXPECTED, "")
    by_status = reasons.status_reason(reasons.strongest_status(handshake.statuses))
    if by_status is not None and by_status.kind not in (reasons.WRONG_LOGIN, reasons.NOT_FOUND):
        return by_status
    if handshake.statuses:
        return Reason(reasons.UNEXPECTED, "")
    return reasons.unreachable(handshake.timed_out)


def _from_statuses(statuses: list[int], timed_out: bool) -> Reason:
    found = reasons.status_reason(reasons.strongest_status(statuses))
    if found is not None:
        return found
    if statuses:
        return Reason(reasons.UNEXPECTED, "")
    return reasons.unreachable(timed_out)


_ACCOUNT_FIELDS = ("type", "url", "username", "password", "mac", "serial_number", "device_id")


def account_key(provider: dict) -> str:
    """A digest of what signs `provider` in (its credentials among it, never kept as text)."""
    fields = [str(provider.get(key) or "") for key in _ACCOUNT_FIELDS]
    return hashlib.sha256("\0".join(fields).encode("utf-8")).hexdigest()


def has_account(provider: dict) -> bool:
    """Whether `provider` has an account to ask: an Xtream panel or an STB portal, also a
    playlist from an Xtream panel (no thread is started for a plain playlist's failure)."""
    kind = str(provider.get("type", "")).upper()
    url = str(provider.get("url") or "").strip()
    if not url:
        return False
    if kind in ("XTREAM", "STB"):
        return True
    link = parse_provider_link(url) if kind in ("M3UPLAYLIST", "M3USTREAM") else None
    return link is not None and link.type == XTREAM


def check_account(
    provider: dict,
    *,
    prefer_https: bool = False,
    ssl_verify: bool = True,
    now: Optional[datetime] = None,
    session: Optional[dict] = None,
) -> Optional[Reason]:
    """Ask `provider`'s account (blocking: run it on a worker). `prefer_https` and `ssl_verify`
    are the global settings, which the provider's own override; `session`, an STB portal's
    signed-in headers (see check_stb). None: nothing to ask."""
    kind = str(provider.get("type", "")).upper()
    url = str(provider.get("url") or "").strip()
    if not url:
        return None
    reason = _check(provider, kind, url, prefer_https, ssl_verify, now, session)
    if reason is not None:
        logger.debug("Account check (%s): %s", kind, reason.kind)
    return reason


def _check(
    provider: dict,
    kind: str,
    url: str,
    prefer_https: bool,
    ssl_verify: bool,
    now: Optional[datetime],
    session: Optional[dict],
) -> Optional[Reason]:
    https = provider_prefers_https(provider, prefer_https)
    verify = bool(provider.get("ssl_verify", ssl_verify))
    if kind == "XTREAM":
        username, password = provider.get("username"), provider.get("password")
        if not username or not password:
            return Reason(reasons.WRONG_LOGIN, translate(reasons.WRONG_LOGIN_MESSAGE))
        return check_xtream(
            url, str(username), str(password), prefer_https=https, ssl_verify=verify, now=now
        )
    if kind == "STB":
        return check_stb(
            url,
            str(provider.get("mac") or ""),
            serial_number=str(provider.get("serial_number") or ""),
            device_id=str(provider.get("device_id") or ""),
            prefer_https=https,
            ssl_verify=verify,
            now=now,
            session=session,
        )
    link = parse_provider_link(url) if kind in ("M3UPLAYLIST", "M3USTREAM") else None
    if link is not None and link.type == XTREAM:  # a playlist from an Xtream panel
        return check_xtream(
            link.url, link.username, link.password, prefer_https=https, ssl_verify=verify, now=now
        )
    return None


class AccountCheckWorker(QObject):
    """One account check off the GUI thread: finished(Reason or None)."""

    finished = Signal(object)
    error = Signal(str)

    def __init__(
        self,
        provider: dict,
        prefer_https: bool,
        ssl_verify: bool,
        session: Optional[dict] = None,
    ) -> None:
        super().__init__()
        self._provider = dict(provider)
        self._prefer_https = prefer_https
        self._ssl_verify = ssl_verify
        self._session = dict(session) if session else None  # copied on the GUI thread

    def run(self) -> None:
        try:
            reason = check_account(
                self._provider,
                prefer_https=self._prefer_https,
                ssl_verify=self._ssl_verify,
                session=self._session,
            )
        except Exception as e:  # the text can carry the URL: its type only
            logger.info("The account check failed (%s)", type(e).__name__)
            reason = None
        self.finished.emit(reason)


Answer = Callable[[Optional[Reason]], None]


class AccountChecks(QObject):
    """Account checks on failures, at most once a minute per provider (see the module)."""

    def __init__(
        self,
        config: Any,
        *,
        runner: Any = None,
        clock: Callable[[], float] = time.monotonic,
        parent: Optional[QObject] = None,
    ) -> None:
        """`config` gives the global prefer_https and ssl_verify; `runner` runs the workers
        (a JobRunner of its own by default)."""
        super().__init__(parent)
        self._config = config
        self._runner = runner if runner is not None else JobRunner(self)
        self._clock = clock
        # Keyed by the provider's name and account (`account_key`): an account fixed in
        # Settings under the same name is asked again at once.
        self._checked: dict[tuple[str, str], tuple[float, Optional[Reason]]] = {}
        self._waiting: dict[tuple[str, str], list[Answer]] = {}  # under way, and who waits
        self._closed = False

    def explain(self, provider: dict, answer: Answer, session: Optional[dict] = None) -> bool:
        """Ask `provider`'s account why it failed; `answer(reason)` is called on the GUI
        thread (at once with the last answer when it was asked less than a minute ago).
        `session`: an STB portal's signed-in headers, asked with (see check_stb). False when
        there is nothing to ask (no name, or no account: a plain playlist)."""
        name = str(provider.get("name") or "")
        if self._closed or not name or not has_account(provider):
            return False
        key = (name, account_key(provider))
        waiting = self._waiting.get(key)
        if waiting is not None:
            waiting.append(answer)
            return True
        last = self._checked.get(key)
        if last is not None and self._clock() - last[0] < CHECK_EVERY_S:
            answer(last[1])
            return True
        self._waiting[key] = [answer]
        self._checked[key] = (self._clock(), None)  # the minute starts with the ask
        worker = AccountCheckWorker(
            provider,
            bool(getattr(self._config, "prefer_https", False)),
            bool(getattr(self._config, "ssl_verify", True)),
            session,
        )
        self._runner.run_worker(worker, self._on_answer, self._on_error, context=key)
        return True

    def close(self) -> None:
        """No more answers: the window closes (a check under way ends on its own)."""
        self._closed = True
        self._waiting.clear()

    def _on_answer(self, key: tuple[str, str], reason: Optional[Reason]) -> None:
        when = self._checked.get(key, (self._clock(), None))[0]
        self._checked[key] = (when, reason)
        if reason is not None:
            logger.info("Account check: %s", reason.kind)
        for answer in self._waiting.pop(key, []):
            if not self._closed:
                answer(reason)

    def _on_error(self, key: tuple[str, str], _message: str) -> None:
        self._on_answer(key, None)
