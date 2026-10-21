import logging
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from services.redact import redact_url, safe_exc
from services.vlc_profile import DEFAULT_USER_AGENT

logger = logging.getLogger(__name__)

# Some panels' proxies answer the requests library's own agent ("python-requests/…") with
# 403: every provider request names the player the streams already go out as.
CLIENT_HEADERS = {"User-Agent": DEFAULT_USER_AGENT}


def base_from_url(url: str) -> str:
    """Extract base 'scheme://netloc' from a URL string."""
    try:
        # Avoid external dependency; simple split
        scheme_sep = url.find("://")
        if scheme_sep == -1:
            return url
        scheme = url[:scheme_sep]
        rest = url[scheme_sep + 3 :]
        netloc = rest.split("/", 1)[0]
        return f"{scheme}://{netloc}"
    except Exception as e:
        logger.warning("Failed to parse base from URL %s: %s", redact_url(url), safe_exc(e))
        return url


def https_fallback_candidates(url: str, prefer_https: bool) -> List[str]:
    """Return the URLs to try in order: the HTTPS form first when it is preferred."""
    if prefer_https and url.startswith("http://"):
        return ["https://" + url[len("http://") :], url]
    return [url]


def stb_portal_base(url: str, prefer_https: bool = False) -> str:
    """Reduce an STB portal URL to 'scheme://netloc', upgrading http when HTTPS is preferred."""
    parts = urlsplit(url)
    scheme = "https" if prefer_https and parts.scheme == "http" else parts.scheme
    return f"{scheme}://{parts.netloc}"


def stb_endpoint(base: str) -> str:
    """Return the STB load.php endpoint given a base host URL."""
    return f"{base}/server/load.php"


def stb_create_link_url(
    base: str, content_type: str, cmd: str, *, is_episode: bool = False, series: Any = None
) -> str:
    """Build the STB create_link URL that resolves an item's cmd into a playable link.

    Episodes are requested as VOD with their episode number in ``series``. Unlike
    :func:`stb_request_url`, ``cmd`` keeps '/' unescaped and ``series`` is passed as-is,
    matching what portals have always received from this client.
    """
    if is_episode:
        link_type = "vod" if content_type == "series" else content_type
        series_query = f"&series={series}"
    else:
        link_type = content_type
        series_query = ""
    return (
        f"{stb_endpoint(base)}?type={link_type}&action=create_link"
        f"&cmd={quote(cmd)}{series_query}&JsHttpRequest=1-xml"
    )


def stb_archive_link_url(base: str, programme_id: str) -> str:
    """Build the STB create_link URL of a programme's recording in the portal's archive
    (catch-up): `type=tv_archive` with `cmd` "auto /media/<programme id>.mpg", and the other
    parameters as set-top boxes send them (an empty `series` and `forced_storage`,
    `disable_ad=0`, `download=0`). Its answer's `js.cmd` is the link, as a channel's is.

    Some portals also take a `position` (seconds into the programme): it is not sent, the
    player seeks within the recording instead, which every portal's link allows.
    """
    cmd = quote(f"auto /media/{programme_id}.mpg")
    return (
        f"{stb_endpoint(base)}?type=tv_archive&action=create_link&cmd={cmd}"
        "&series=&forced_storage=&disable_ad=0&download=0&JsHttpRequest=1-xml"
    )


def stb_request_url(
    base: str, content_type: str, action: str, params: Optional[Dict[str, str]] = None
) -> str:
    """Build a full STB request URL with consistent query encoding and required flags.

    Always appends JsHttpRequest flag. The 'params' dictionary is URL-encoded
    using RFC 3986 quoting (spaces as %20, not '+').
    """
    query = {"type": content_type, "action": action}
    if params:
        query.update(params)
    query["JsHttpRequest"] = "1-xml"
    # urlencode will quote parameter values; use RFC 3986 quoting via quote
    return f"{stb_endpoint(base)}?{urlencode(query, doseq=True, quote_via=quote)}"


# -----------------------------
# Xtream Codes helpers (Player API v2)
# -----------------------------


# What a server address may end in besides the path the panel is served under: an API file.
_XTREAM_FILES = ("player_api.php", "get.php", "xmltv.php", "panel_api.php")


def _ensure_base(url: str) -> str:
    """Normalize an Xtream server address to its base, 'scheme://host[:port][/path]'.

    Accepts 'domain:8080', 'http://domain:8080/', 'https://domain/player_api.php?…' and
    the like. A panel served under a path ('http://domain/xc') keeps it: its API and
    streams are under it. An address without one (most saved providers) gives
    'scheme://host[:port]' as before; an API file pasted with it ('…/player_api.php',
    'get.php', …), a trailing slash, a query or a fragment are dropped.
    """
    try:
        url = url.strip()
        if "://" not in url:
            # Default to http if scheme not provided; auth may instruct https later
            url = f"http://{url}"
        parts = urlsplit(url)
        segments = [s for s in parts.path.split("/") if s]
        if segments and segments[-1].lower() in _XTREAM_FILES:
            segments.pop()
        base = f"{parts.scheme}://{parts.netloc}"
        return f"{base}/{'/'.join(segments)}" if segments else base
    except Exception:
        return url


# This session's saved addresses with a path whose panel answered at its server's root instead
# (a stray path, 'http://host:8080/c/', that 2.2.0 and older cut off), by their base.
_answered_at_root: Dict[str, str] = {}


def xtream_base(url: str) -> str:
    """An Xtream server address's base: where its API files and streams are (its server's root
    once the panel answered there instead of under the address's path)."""
    base = _ensure_base(url)
    return _answered_at_root.get(base, base)


def xtream_sign_in_bases(url: str) -> List[str]:
    """Where to ask the panel of the address `url`, in order: its base, then, when that has a
    path, the server's root, once (`xtream_elsewhere`; `remember_xtream_root` when the root
    answers)."""
    base = xtream_base(url)
    root = base_from_url(base)
    return [base, root] if root != base else [base]


def xtream_elsewhere(response: Any) -> bool:
    """Whether a Player API answer under an address's path says the panel is not there: a 404,
    or a body that is not the API's JSON (a web server's page)."""
    if response.status_code == 404:
        return True
    if not response.content:
        return False
    try:
        response.json()
    except ValueError:
        return True
    return False


def remember_xtream_root(url: str, root: str) -> None:
    """The panel of the address `url` answered at its server's `root`: every address built for
    it uses the root from now on (this session)."""
    base = _ensure_base(url)
    if root != base and _answered_at_root.get(base) != root:
        _answered_at_root[base] = root
        logger.info(
            "The Xtream panel did not answer under its address's path but at its server's root;"
            " the root is used."
        )


def _xtream_path(url: str) -> str:
    """The path an Xtream server address's panel is served under ('/xc'), or ''."""
    return urlsplit(xtream_base(url)).path


def _build_xtream_url(
    base: str,
    endpoint: str,
    username: str,
    password: str,
    extra: Optional[Dict[str, str]] = None,
) -> str:
    """Build an Xtream Codes URL with consistent query encoding.

    All Xtream URLs follow the pattern: {base}/{endpoint}?username=U&password=P[&extra...]
    This factory centralizes URL construction and query encoding.
    """
    base = xtream_base(base)
    query = {"username": username, "password": password}
    if extra:
        query.update(extra)
    return f"{base}/{endpoint}?{urlencode(query, doseq=True, quote_via=quote)}"


def xtream_player_api_url(
    base: str,
    username: str,
    password: str,
    action: Optional[str] = None,
    extra: Optional[Dict[str, str]] = None,
) -> str:
    """Build a Player API URL.

    Example: {base}/player_api.php?username=U&password=P[&action=...]
    """
    params = {}
    if action:
        params["action"] = action
    if extra:
        params.update(extra)
    return _build_xtream_url(base, "player_api.php", username, password, params or None)


def xtream_signed_in(body: Any) -> bool:
    """Whether a Player API sign-in reply (no action) accepted the account.

    ``body`` is the parsed JSON, or None when the panel sent no JSON (some answer a
    wrong password with an empty 513). An accepted reply looks like:
    {"user_info": {"auth": 1, "status": "Active", "exp_date": "1794855950", ...},
     "server_info": {...}}
    A refused one is {"user_info": {"auth": 0}}, [] or None, depending on the panel.
    Verified means the account can watch: an expired, banned or disabled one is refused.
    """
    user_info = body.get("user_info") if isinstance(body, dict) else None
    if not isinstance(user_info, dict) or str(user_info.get("auth", "")).strip() != "1":
        return False
    status = str(user_info.get("status") or "Active").strip()
    return status.lower() == "active"


def xtream_choose_resolved_base(
    server_info: Dict[str, str], input_base: Optional[str] = None, prefer_https: bool = False
) -> str:
    """Choose the correct base from Player API server_info payload.

    Respect the user's original scheme when provided (do not auto-upgrade to
    HTTPS). If no input_base is given, use the panel-advertised server_protocol.

    This avoids forcing HTTPS on providers that only serve HTTP or present
    self-signed certificates.
    """
    try:
        host = (server_info.get("url") or "").strip()
        if not host:
            return ""
        if input_base and _xtream_path(input_base):
            # A panel under a path sits behind a web server it doesn't know of: the host
            # and port it advertises are its own, not that server's. The user's address is
            # where its API and streams are (HTTPS when preferred).
            base = xtream_base(input_base)
            if prefer_https and base.startswith("http://"):
                base = "https://" + base[len("http://") :]
            return base

        # Determine preferred scheme from input_base if provided
        preferred_scheme = None
        preferred_port = None
        if input_base:
            try:
                b = base_from_url(_ensure_base(input_base))  # scheme://netloc
                scheme_sep = b.find("://")
                if scheme_sep != -1:
                    preferred_scheme = b[:scheme_sep]
                    netloc = b[scheme_sep + 3 :]
                    if ":" in netloc:
                        preferred_port = netloc.split(":", 1)[1]
            except Exception:
                pass

        # Determine scheme preference
        if prefer_https:
            scheme = "https"
        else:
            # Fall back to server-advertised protocol if no user preference
            scheme = (preferred_scheme or server_info.get("server_protocol") or "http").strip()

        # Choose port based on chosen scheme, prioritizing user's input port
        if preferred_port:
            port = preferred_port
        elif scheme == "https":
            # Prefer https_port only when scheme is explicitly https
            port = (
                str(server_info.get("https_port") or "").strip()
                or str(server_info.get("port") or "").strip()
            )
        else:
            port = str(server_info.get("port") or "").strip()

        # Omit standard ports
        if (scheme == "https" and port in ("", "443", "0")) or (
            scheme == "http" and port in ("", "80", "0")
        ):
            return f"{scheme}://{host}"
        return f"{scheme}://{host}:{port}"
    except Exception:
        # Fallback to empty; caller should handle
        return ""


def xtream_xmltv_url(base: str, username: str, password: str) -> str:
    """Build the XMLTV EPG URL for Xtream providers.

    Returns full EPG list for all streams in XMLTV format.
    Example: {base}/xmltv.php?username=U&password=P
    """
    return _build_xtream_url(base, "xmltv.php", username, password)


def xtream_stream_url(
    base: str, kind: str, username: str, password: str, stream_id: Any, ext: str
) -> str:
    """A stream's address on an Xtream panel: {base}/{kind}/{username}/{password}/{id}.{ext},
    `kind` being live, movie or series (an episode)."""
    return f"{base}/{kind}/{username}/{password}/{stream_id}.{ext}"


def xtream_hls(url: str, username: str, password: str) -> str:
    """An Xtream channel's MPEG-TS address (`{base}/live/{username}/{password}/{id}.ts`) as the
    HLS one the panel serves beside it (`….m3u8`), which a phone's browser plays; any other
    address as it is."""
    parts = urlsplit(url)
    marker = f"/live/{username}/{password}/"
    at = parts.path.find(marker) if username and password else -1
    if at < 0:
        return url
    stem, dot, ext = parts.path[at + len(marker) :].rpartition(".")
    if not dot or ext.lower() != "ts" or not stem or "/" in stem:
        return url
    return urlunsplit(parts._replace(path=f"{parts.path[: at + len(marker)]}{stem}.m3u8"))


def provider_prefers_https(provider: Dict[str, Any], global_default: bool) -> bool:
    """A provider's own 'prefer_https' wins; unset (missing/None) falls back to the global setting."""
    value = provider.get("prefer_https")
    return global_default if value is None else bool(value)
