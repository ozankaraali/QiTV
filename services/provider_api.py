import logging
from typing import Dict, Optional
from urllib.parse import quote, urlencode

logger = logging.getLogger(__name__)


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
        logger.warning("Failed to parse base from URL %s: %s", url, e)
        return url


def stb_endpoint(base: str) -> str:
    """Return the STB load.php endpoint given a base host URL."""
    return f"{base}/server/load.php"


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


def _ensure_base(url: str) -> str:
    """Normalize a provider URL to a base 'scheme://host[:port]' string.

    Accepts inputs like 'domain:8080', 'http://domain:8080', 'https://domain', etc.
    """
    try:
        if "://" not in url:
            # Default to http if scheme not provided; auth may instruct https later
            url = f"http://{url}"
        # Trim trailing slash
        if url.endswith("/"):
            url = url[:-1]
        # Reduce to scheme://netloc if path present
        return base_from_url(url)
    except Exception:
        return url


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
    base = _ensure_base(base)
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


def xtream_get_php_url(
    base: str, username: str, password: str, extra: Optional[Dict[str, str]] = None
) -> str:
    """Build the legacy get.php URL for M3U retrieval using the resolved base.

    Example: {base}/get.php?username=U&password=P&type=m3u
    """
    return _build_xtream_url(base, "get.php", username, password, extra)


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


def xtream_choose_stream_base(server_info: Dict[str, str]) -> str:
    """Choose the preferred streaming base.

    Many panels expose API over HTTPS but deliver stream over HTTP.
    Prefer http://host:port when a port is provided; otherwise fall back
    to the resolved base selection.
    """
    try:
        host = server_info.get("url") or ""
        port = str(server_info.get("port") or "").strip()
        if port and port != "0":
            return f"http://{host}:{port}"
        # Fallback to resolved base (may be https)
        return xtream_choose_resolved_base(server_info)
    except Exception:
        return ""


def xtream_xmltv_url(base: str, username: str, password: str) -> str:
    """Build the XMLTV EPG URL for Xtream providers.

    Returns full EPG list for all streams in XMLTV format.
    Example: {base}/xmltv.php?username=U&password=P
    """
    return _build_xtream_url(base, "xmltv.php", username, password)


def xtream_epg_url(
    base: str, username: str, password: str, stream_id: str, limit: Optional[int] = None
) -> str:
    """Build the EPG data table URL for a specific Xtream stream.

    Gets all EPG listings for a single stream (like STB portal).
    Example: {base}/player_api.php?username=U&password=P&action=get_simple_data_table&stream_id=XXX&limit=X
    """
    extra: Dict[str, str] = {"action": "get_simple_data_table", "stream_id": stream_id}
    if limit is not None:
        extra["limit"] = str(limit)
    return _build_xtream_url(base, "player_api.php", username, password, extra)
