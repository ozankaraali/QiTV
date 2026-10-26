import unittest
from unittest import mock

from services.remote import net


def home(route, interfaces):
    """home_ipv4 with the default route's address and the interface list given."""
    with (
        mock.patch.object(net, "_source_address", return_value=route),
        mock.patch.object(net, "_interfaces", return_value=interfaces),
    ):
        return net.home_ipv4()


class HomeAddressTests(unittest.TestCase):
    def test_the_default_route_on_the_wifi_is_kept(self):
        ifaces = [("lo0", "127.0.0.1"), ("en0", "192.168.1.20"), ("utun3", "10.8.0.2")]
        self.assertEqual(home("192.168.1.20", ifaces), "192.168.1.20")

    def test_a_full_tunnel_vpn_gives_way_to_the_wifi(self):
        # The VPN's own address is private too, but a phone on the Wi-Fi cannot reach it.
        ifaces = [("lo0", "127.0.0.1"), ("en0", "192.168.1.20"), ("utun3", "10.8.0.2")]
        self.assertEqual(home("10.8.0.2", ifaces), "192.168.1.20")

    def test_a_tunnel_with_a_public_address_gives_way_to_the_lan(self):
        ifaces = [("eth0", "10.0.0.5"), ("wg0", "100.96.0.3")]
        self.assertEqual(home("100.96.0.3", ifaces), "10.0.0.5")

    def test_linux_wireless_names_count_as_lan(self):
        for name in ("wlan0", "wlp2s0", "enp3s0", "eth1"):
            with self.subTest(name=name):
                ifaces = [(name, "172.16.4.9"), ("tun0", "10.8.0.2")]
                self.assertEqual(home("10.8.0.2", ifaces), "172.16.4.9")

    def test_each_tunnel_kind_gives_way(self):
        for name in ("utun0", "tun0", "ppp0", "wg0", "ipsec0"):
            with self.subTest(name=name):
                ifaces = [("en1", "192.168.0.7"), (name, "10.66.0.2")]
                self.assertEqual(home("10.66.0.2", ifaces), "192.168.0.7")

    def test_only_a_tunnel_keeps_todays_answer(self):
        ifaces = [("lo0", "127.0.0.1"), ("utun3", "10.8.0.2")]
        self.assertEqual(home("10.8.0.2", ifaces), "10.8.0.2")

    def test_a_link_local_lan_address_is_no_home(self):
        ifaces = [("en0", "169.254.3.4"), ("utun3", "10.8.0.2")]
        self.assertEqual(home("10.8.0.2", ifaces), "10.8.0.2")

    def test_no_interface_list_keeps_the_default_route(self):
        self.assertEqual(home("192.168.1.20", []), "192.168.1.20")
        self.assertIsNone(home("203.0.113.5", []))
        self.assertIsNone(home("127.0.0.1", []))
        self.assertIsNone(home(None, []))

    def test_no_route_finds_the_lan(self):
        self.assertEqual(home(None, [("en0", "192.168.1.20")]), "192.168.1.20")

    def test_the_interface_list_never_raises(self):
        # The system's own list (a local question; nothing is sent).
        found = net._interfaces()
        self.assertIsInstance(found, list)
        for name, addr in found:
            self.assertIsInstance(name, str)
            self.assertIsInstance(addr, str)


if __name__ == "__main__":
    unittest.main()
