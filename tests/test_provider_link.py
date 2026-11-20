"""One provider link fills the Add card (2.2): services.provider_link reads it (the card itself
is tests/test_provider_cards.py's). Fictional hosts."""

import unittest

from services.provider_link import (
    M3U,
    STB,
    XTREAM,
    ProviderLink,
    parse_provider_link,
    xtream_address,
)

PANEL = "http://panel.example.test:8080"


class ParseTests(unittest.TestCase):
    def test_xtream_links_give_server_username_and_password(self):
        for text in (
            f"{PANEL}/get.php?username=ann&password=pw1&type=m3u_plus&output=ts",
            f"{PANEL}/player_api.php?username=ann&password=pw1",
            f"  {PANEL}/xmltv.php?username=ann&password=pw1\n",
            f"{PANEL}/live/ann/pw1/123.ts",
            "panel.example.test:8080/get.php?password=pw1&username=ann",  # as typed, no scheme
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    parse_provider_link(text), ProviderLink(XTREAM, PANEL, "ann", "pw1")
                )

    def test_the_scheme_and_an_encoded_password_survive(self):
        link = parse_provider_link(
            "https://panel.example.test/player_api.php?username=ann&password=p%40ss%26w"
        )
        self.assertEqual((link.url, link.password), ("https://panel.example.test", "p@ss&w"))
        self.assertEqual(link.name, "panel.example.test")

    def test_a_playlist_address_is_an_m3u_playlist(self):
        for text, url in (
            ("https://lists.example.test/tv/all.m3u8?token=abc#top", None),
            ("http://lists.example.test/news.M3U", None),
            ("http://lists.example.test/get.php?type=m3u", None),  # a panel's, no login in it
        ):
            with self.subTest(text=text):
                link = parse_provider_link(text)
                self.assertEqual(link.type, M3U)
                self.assertEqual(link.url, url or text.split("#")[0])
                self.assertEqual((link.username, link.password), ("", ""))

    def test_a_portal_address_is_an_stb_portal(self):
        cases = {
            "http://portal.example.test:8080/c/": "http://portal.example.test:8080/c/",
            "http://portal.example.test:8080/c": "http://portal.example.test:8080/c/",
            "http://portal.example.test/stalker_portal/c/index.html": (
                "http://portal.example.test/stalker_portal/c/"
            ),
            "http://portal.example.test/stalker_portal/server/load.php": (
                "http://portal.example.test/stalker_portal/"
            ),
        }
        for text, url in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_provider_link(text), ProviderLink(STB, url))

    def test_a_portal_link_may_name_its_mac(self):
        link = parse_provider_link("http://portal.example.test/c/?mac=00:1a:79:aa:bb:cc")
        self.assertEqual(link.mac, "00:1A:79:AA:BB:CC")
        self.assertEqual(parse_provider_link("http://portal.example.test/c/?mac=nope").mac, "")

    def test_anything_else_is_no_link(self):
        for text in (
            "",
            "not a link",
            PANEL,  # a bare server: which type?
            f"{PANEL}/get.php?username=ann",  # no password
            "ftp://lists.example.test/all.m3u",
            "http://lists.example.test:port/all.m3u",
            "file:///home/someone/all.m3u",
        ):
            with self.subTest(text=text):
                self.assertIsNone(parse_provider_link(text))

    def test_the_password_stays_out_of_its_repr(self):
        link = parse_provider_link(f"{PANEL}/get.php?username=ann&password=pw1")
        self.assertNotIn("pw1", repr(link))

    def test_a_playlist_under_a_c_folder_is_a_playlist(self):
        link = parse_provider_link("https://lists.example.test/lists/c/news.m3u")
        self.assertEqual(link, ProviderLink(M3U, "https://lists.example.test/lists/c/news.m3u"))

    def test_a_panel_served_under_a_path_keeps_it(self):
        for text in (
            f"{PANEL}/xc/get.php?username=ann&password=pw1",
            f"{PANEL}/xc/live/ann/pw1/123.ts",
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    parse_provider_link(text), ProviderLink(XTREAM, f"{PANEL}/xc", "ann", "pw1")
                )
        timeshift = f"{PANEL}/timeshift/ann/pw1/60/2026-10-19:20-00/123.ts"
        self.assertEqual(parse_provider_link(timeshift), ProviderLink(XTREAM, PANEL, "ann", "pw1"))
        # A playlist under a folder named like a stream kind is still a playlist.
        self.assertEqual(parse_provider_link(f"{PANEL}/live/lists/all/news.m3u").type, M3U)

    def test_passwords_keep_their_characters(self):
        cases = {
            f"{PANEL}/get.php?username=ann&password=a+b%2Bc": "a+b+c",  # "+" is no space
            f"{PANEL}/live/ann/p%40ss/7.m3u8": "p@ss",
            f'{PANEL}/get.php?username=ann&password="quoted"': '"quoted"',
            f"<{PANEL}/get.php?username=ann&password=<pw>>": "<pw>",  # brackets around the link
            f"'{PANEL}/get.php?username=ann&password=it's'": "it's",
        }
        for text, password in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_provider_link(text).password, password)


class XtreamAddressTests(unittest.TestCase):
    def test_a_file_path_is_no_panels(self):
        # An Xtream address typed or pasted that is no link: a file's path (a film, a page) is
        # no panel's, so only its server is kept; a panel served under a path keeps it, and an
        # API file at its end goes, with its query.
        cases = {
            f"{PANEL}/media/film.mp4": PANEL,
            f"{PANEL}/index.html?lang=en": PANEL,
            f"{PANEL}/xc/clip.MKV#t=3": PANEL,
            f"{PANEL}/xc/player_api.php": f"{PANEL}/xc",
            f"{PANEL}/get.php?type=m3u": PANEL,
            f"{PANEL}/xc": f"{PANEL}/xc",  # as typed
            f"{PANEL}/xc/": f"{PANEL}/xc/",
            f"{PANEL}/v1.2": f"{PANEL}/v1.2",  # a folder's dot is no file's
            PANEL: PANEL,
            "https://[::1]:8080/a/film.mp4": "https://[::1]:8080",
            "words, not an address": "words, not an address",
        }
        for text, kept in cases.items():
            with self.subTest(text=text):
                self.assertEqual(xtream_address(text), kept)


if __name__ == "__main__":
    unittest.main()
