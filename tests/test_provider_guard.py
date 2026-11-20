"""The guard that tells a new provider from the same one. Fictional hosts."""

import unittest

from services.provider_guard import OTHER_ACCOUNT, OTHER_HOST, OTHER_KIND, SAME, judge

PANEL = {
    "type": "XTREAM",
    "name": "Panel",
    "url": "http://panel.test:8080",
    "username": "viewer",
    "password": "secret",
}
PORTAL = {
    "type": "STB",
    "name": "Portal",
    "url": "http://portal.test/c",
    "mac": "00:1A:79:00:00:01",
}


class ProviderGuardTests(unittest.TestCase):
    def test_a_new_password_is_the_same_provider(self):
        self.assertEqual(judge(PANEL, {**PANEL, "password": "other", "name": "Renamed"}), SAME)

    def test_another_kind_is_another_provider(self):
        link = {"type": "M3USTREAM", "name": "Panel", "url": "http://panel.test:8080/live/1.ts"}
        self.assertEqual(judge(PANEL, link), OTHER_KIND)

    def test_another_host_and_another_account(self):
        self.assertEqual(judge(PANEL, {**PANEL, "url": "http://elsewhere.test:8080"}), OTHER_HOST)
        self.assertEqual(judge(PANEL, {**PANEL, "username": "guest"}), OTHER_ACCOUNT)
        lists = {"type": "M3UPLAYLIST", "url": "http://lists.test/a.m3u"}
        self.assertEqual(judge(lists, {**lists, "url": "http://lists.test/b.m3u"}), OTHER_HOST)
        server = {"type": "JELLYFIN", "url": "http://media.test:8096", "username": "ann"}
        self.assertEqual(judge(server, {**server, "username": "bob"}), OTHER_ACCOUNT)

    def test_scheme_and_default_ports_dont_matter(self):
        self.assertEqual(
            judge(
                {**PANEL, "url": "http://Panel.test:80/"}, {**PANEL, "url": "https://panel.test"}
            ),
            SAME,
        )
        self.assertEqual(judge(PANEL, {**PANEL, "url": "http://panel.test:8080/player_api"}), SAME)

    def test_another_mac_on_the_same_portal_is_another_account(self):
        self.assertEqual(judge(PORTAL, {**PORTAL, "mac": "00:1A:79:00:00:02"}), OTHER_ACCOUNT)
        self.assertEqual(judge(PORTAL, {**PORTAL, "mac": "00:1a:79:00:00:01"}), SAME)

    def test_empty_and_case_only_username_differences_are_the_same(self):
        bare = {"type": "XTREAM", "url": "http://panel.test:8080"}
        self.assertEqual(judge(bare, {**bare, "password": "x"}), SAME)
        self.assertEqual(judge(PANEL, {**PANEL, "username": "VIEWER"}), SAME)

    def test_a_portal_with_a_username_compares_it_too(self):
        portal = {**PORTAL, "username": "ann"}
        self.assertEqual(judge(portal, {**portal, "username": "bob"}), OTHER_ACCOUNT)
        self.assertEqual(judge(portal, {**portal, "username": "ANN"}), SAME)


if __name__ == "__main__":
    unittest.main()
