"""Secure DNS (services.secure_dns, 2.9.1): names looked up over HTTPS, the rest as before."""

import socket
import unittest
from unittest import mock

from services import secure_dns


class SecureDnsTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(secure_dns.enable, False)
        self.asked = []

        def ask(server, path):
            self.asked.append((server, path))
            if "type=A&" in path + "&" and "blocked.example" in path:
                return [("203.0.113.7", 600)]
            return []

        patch = mock.patch.object(secure_dns, "_ask", side_effect=ask)
        patch.start()
        self.addCleanup(patch.stop)

    def test_only_names_out_there_are_looked_up(self):
        for host in ("127.0.0.1", "::1", "[2001:db8::1]", "localhost", "tv.local", "nas", b""):
            self.assertIsNone(secure_dns._plain(host), host)
        self.assertEqual(secure_dns._plain("Panel.Example."), "panel.example")
        self.assertEqual(secure_dns._plain(b"panel.example"), "panel.example")

    def test_on_a_blocked_name_resolves_by_the_secure_resolver_and_is_kept(self):
        self.assertIs(socket.getaddrinfo, secure_dns._system_getaddrinfo)  # off: untouched
        secure_dns.enable(True)
        found = socket.getaddrinfo("blocked.example", 8080, type=socket.SOCK_STREAM)
        self.assertEqual({info[4][0] for info in found}, {"203.0.113.7"})
        self.assertEqual({info[4][1] for info in found}, {8080})
        self.assertEqual(self.asked[0][0], "1.1.1.1")  # Cloudflare's first
        asked = len(self.asked)
        socket.getaddrinfo("blocked.example", 443)
        self.assertEqual(len(self.asked), asked)  # kept for its TTL
        secure_dns.enable(False)
        self.assertIs(socket.getaddrinfo, secure_dns._system_getaddrinfo)

    def test_a_name_no_secure_resolver_knows_is_asked_of_the_system(self):
        secure_dns.enable(True)
        with mock.patch.object(
            secure_dns, "_system_getaddrinfo", return_value=[("system",)]
        ) as system:
            self.assertEqual(socket.getaddrinfo("unknown.example", 80), [("system",)])
        system.assert_called_once_with("unknown.example", 80, 0, 0, 0, 0)
        self.assertEqual([s for s, _p in self.asked], ["1.1.1.1"] * 2 + ["8.8.8.8"] * 2)

    def test_a_number_never_asks_anyone(self):
        secure_dns.enable(True)
        socket.getaddrinfo("1.1.1.1", 443)  # the resolver's own: no lookup of its own
        self.assertEqual(self.asked, [])


if __name__ == "__main__":
    unittest.main()
