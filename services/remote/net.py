"""The computer's place on the home network, for the phone remote's home door."""

from __future__ import annotations

import ipaddress
import logging
import socket
import struct
import sys
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
_RFC1918 = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
# Interface names: a home network's (Ethernet, Wi-Fi) and a VPN's.
_LAN = ("en", "eth", "wlan", "wl")
_TUNNEL = ("utun", "tun", "ppp", "wg", "ipsec")
# SIOCGIFADDR: an interface's IPv4 address (ifreq: 16-byte name, then the sockaddr_in).
_SIOCGIFADDR = 0xC0206921 if sys.platform == "darwin" else 0x8915


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


def _interfaces() -> list[tuple[str, str]]:
    """(name, IPv4 address) of each interface that has one, in the system's order; empty
    where the system does not tell (Windows)."""
    try:
        import fcntl
    except ImportError:
        return []
    found = []
    try:
        names = socket.if_nameindex()
    except OSError:
        return []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        for _index, name in names:
            ifreq = struct.pack("256s", name.encode()[:15])
            try:
                answer = fcntl.ioctl(probe.fileno(), _SIOCGIFADDR, ifreq)
            except OSError:
                continue  # no IPv4 address
            found.append((name, socket.inet_ntoa(answer[20:24])))
    return found


def _usable(addr: Optional[str]) -> bool:
    return addr is not None and is_private(addr) and not addr.startswith("127.")


def home_ipv4() -> Optional[str]:
    """This computer's address on the home network, or None: the default route's, unless that
    is a VPN tunnel's (a phone on the Wi-Fi cannot reach it) and an Ethernet or Wi-Fi interface
    has a home network's address."""
    route = _source_address(socket.AF_INET, "192.0.2.1")
    interfaces = _interfaces()
    on = {addr: name for name, addr in interfaces}
    if _usable(route) and not on.get(route or "", "").startswith(_TUNNEL):
        return route
    for name, addr in interfaces:
        if name.startswith(_LAN) and any(ipaddress.ip_address(addr) in net for net in _RFC1918):
            return addr
    return route if _usable(route) else None


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
