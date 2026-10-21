"""The computer's place on the home network, for the phone remote's home door."""

from __future__ import annotations

import ipaddress
import logging
import socket
from typing import Optional

logger = logging.getLogger(__name__)

_PRIVATE = tuple(
    ipaddress.ip_network(n)
    for n in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "127.0.0.0/8",
        "fc00::/7",
        "fe80::/10",
        "::1/128",
    )
)


def is_private(addr: object) -> bool:
    """True for addresses of a home network or this computer (the only ones the home door
    answers)."""
    if not isinstance(addr, str):
        return False
    try:
        ip = ipaddress.ip_address(addr.split("%")[0])  # fe80::1%en0
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return any(ip in net for net in _PRIVATE if net.version == ip.version)


def _source_address(family: int, target: str) -> Optional[str]:
    # Connecting a UDP socket sends nothing; it only asks the system which address it would use.
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as probe:
            probe.connect((target, 9))
            return str(probe.getsockname()[0])
    except OSError:
        return None


def home_ipv4() -> Optional[str]:
    """This computer's address on the home network (the default route's), or None."""
    addr = _source_address(socket.AF_INET, "192.0.2.1")
    if addr is None or not is_private(addr) or addr.startswith("127."):
        return None
    return addr


def ipv6_prefixes() -> list[str]:
    """The first four hextets (the /64) of this computer's global IPv6 address, written in
    full ("2001:0db8:0001:0002"); empty when it has none."""
    addr = _source_address(socket.AF_INET6, "2001:db8::1")
    if addr is None:
        return []
    try:
        ip = ipaddress.IPv6Address(addr.split("%")[0])
    except ValueError:
        return []
    if not ip.is_global:
        return []
    return [":".join(ip.exploded.split(":")[:4])]
