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
    base = _ensure_base(base)
    query = {"username": username, "password": password}
    if action:
        query["action"] = action
    if extra:
        query.update(extra)
    return f"{base}/player_api.php?{urlencode(query, doseq=True, quote_via=quote)}"


def xtream_get_php_url(
    base: str, username: str, password: str, extra: Optional[Dict[str, str]] = None
) -> str:
    """Build the legacy get.php URL for M3U retrieval using the resolved base.

    Example: {base}/get.php?username=U&password=P&type=m3u
    """
    base = _ensure_base(base)
    query = {"username": username, "password": password}
    if extra:
        query.update(extra)
    return f"{base}/get.php?{urlencode(query, doseq=True, quote_via=quote)}"


def xtream_choose_resolved_base(server_info: Dict[str, str]) -> str:
    """Choose the correct base from Player API server_info payload.

    Prefers https when https_port is provided; falls back to the advertised
    server_protocol and port.
    """
    try:
        host = server_info.get("url") or ""
        # prefer https if available
        https_port = str(server_info.get("https_port") or "").strip()
        if https_port and https_port != "0":
            port = https_port
            scheme = "https"
        else:
            scheme = server_info.get("server_protocol") or "http"
            port = str(server_info.get("port") or "").strip()

        # default ports: omit if standard
        if (scheme == "https" and port in ("", "443")) or (scheme == "http" and port in ("", "80")):
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
