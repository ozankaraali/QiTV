"""What a link is (services/link_kind.py)."""

import unittest

from services.link_kind import (
    FILM,
    LIVE,
    NONE,
    PROBE,
    Provider,
    address,
    classify,
    host_of,
    owner_of,
    probe_text,
    xtream_account,
)
from services.provider_link import M3U, STB, XTREAM


class ClassifyTests(unittest.TestCase):
    def test_film_and_live_by_extension_and_xtream_paths(self):
        for url in (
            "http://cdn.example/a/b.mp4",
            "https://cdn.example/b.MKV?token=1",
            "http://cdn.example/b.avi",
            "http://cdn.example/b.webm",
            "http://cdn.example/b.mov",
            "http://cdn.example/b.m4v",
            "http://panel.example:8080/movie/user/pass/55.mkv",
            "http://panel.example/movie/user/pass/55",
        ):
            self.assertEqual(classify(url), FILM, url)
        for url in (
            "http://cdn.example/a/b.ts",
            "http://panel.example:8080/live/user/pass/1.ts",
            "http://panel.example:8080/live/user/pass/1",
            "http://panel.example:8080/live/user/pass/1.m3u8",
            "rtmp://cdn.example/app/key",
            "rtsp://cdn.example/cam",
            "udp://@239.0.0.1:1234",
        ):
            self.assertEqual(classify(url), LIVE, url)

    def test_accounts_and_playlists_are_providers(self):
        got = classify("http://panel.example:8080/get.php?username=u&password=p&type=m3u_plus")
        self.assertIsInstance(got, Provider)
        self.assertEqual(got.link.type, XTREAM)
        self.assertEqual(got.link.username, "u")
        got = classify("http://panel.example/player_api.php?username=u&password=p")
        self.assertEqual(got.link.type, XTREAM)
        got = classify("http://cdn.example/list/all.m3u")
        self.assertEqual(got.link.type, M3U)
        got = classify("http://portal.example/c/")
        self.assertEqual(got.link.type, STB)

    def test_m3u8_needs_a_probe(self):
        self.assertEqual(classify("https://cdn.example/a/index.m3u8?t=1"), PROBE)
        self.assertEqual(classify("https://cdn.example/watch"), PROBE)
        self.assertEqual(classify("https://cdn.example/stream.php?id=5"), PROBE)

    def test_not_a_url(self):
        for text in ("", "hello world", "ftp://x.example/a.mp4", "file:///a.mp4", "http://"):
            self.assertEqual(classify(text), NONE, text)

    def test_xtream_stream_link_names_its_account(self):
        acct = xtream_account("http://panel.example:8080/live/user/pass/1.ts")
        self.assertEqual(acct.type, XTREAM)
        self.assertEqual((acct.username, acct.password), ("user", "pass"))
        self.assertEqual(acct.url, "http://panel.example:8080")
        self.assertIsNotNone(xtream_account("http://panel.example/series/u/p/9.mkv"))
        self.assertIsNone(xtream_account("http://cdn.example/a/b.ts"))
        self.assertIsNone(
            xtream_account("http://panel.example/get.php?username=u&password=p&type=m3u")
        )


class ProbeTests(unittest.TestCase):
    def test_probe_live_and_vod_media_playlists(self):
        live = "#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXT-X-MEDIA-SEQUENCE:100\n#EXTINF:6,\na.ts\n"
        self.assertEqual(probe_text(live), LIVE)
        vod = live + "#EXT-X-ENDLIST\n"
        self.assertEqual(probe_text(vod), FILM)
        typed = "#EXTM3U\n#EXT-X-PLAYLIST-TYPE:VOD\n#EXT-X-TARGETDURATION:6\n#EXTINF:6,\na.ts\n"
        self.assertEqual(probe_text(typed), FILM)
        master = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000\nlow.m3u8\n"
        self.assertEqual(probe_text(master), LIVE)
        self.assertEqual(probe_text("something else"), LIVE)

    def test_a_channel_list_m3u8_is_a_provider(self):
        text = (
            "#EXTM3U\n#EXTINF:-1 tvg-id=\"a\",Chan A\nhttp://cdn.example/a.ts\n"
            "#EXTINF:-1,Chan B\nhttps://cdn.example/b.m3u8\n"
        )
        got = probe_text(text, "http://cdn.example/channels.m3u8")
        self.assertIsInstance(got, Provider)
        self.assertEqual(got.link.type, M3U)

    def test_a_probe_cannot_name_the_playlist_without_its_address(self):
        text = "#EXTM3U\n#EXTINF:-1,A\nhttp://cdn.example/a.ts\n"
        got = probe_text(text, "http://cdn.example/list.m3u8")
        self.assertEqual(got.link.url, "http://cdn.example/list.m3u8")
        # Without the address there is no provider to offer: VLC tries it as one stream.
        self.assertEqual(probe_text(text), LIVE)


class AddressTests(unittest.TestCase):
    def test_a_links_host_is_compared_as_a_providers(self):
        self.assertEqual(
            address('  "http://cdn.example/a.ts"  \nsecond'), "http://cdn.example/a.ts"
        )
        cases = (
            ("http://demo:secret@Panel.Example:8080/live/1.ts", "panel.example:8080"),
            ("panel.example:8080/get.php", "panel.example:8080"),  # typed without its scheme
            ("https://cdn.example/watch?v=7", "cdn.example"),  # no port: none added
            ("", ""),
            ("http://[::1]:8000/x", "[::1]:8000"),
        )
        for url, host in cases:
            with self.subTest(url):
                self.assertEqual(host_of(url), host)

    def test_a_default_port_written_out_is_the_same_host(self):
        # Final review I1: `panel.example:80` was another host than `panel.example`, so a link
        # written with its scheme's default port booked under no provider.
        same = (
            ("http://panel.example:80/live/demo/secret/1.ts", "http://panel.example"),
            ("https://panel.example:443/x.m3u8", "https://panel.example"),
            ("http://PANEL.Example/x", "https://panel.example/y"),  # one host, either scheme
            ("panel.example:80/get.php", "http://panel.example"),  # typed without its scheme
            ("http://[::1]:80/x", "http://[::1]/y"),
        )
        for link, provider in same:
            with self.subTest(link):
                self.assertEqual(host_of(link), host_of(provider))
        other = (
            ("http://panel.example:8080/x", "http://panel.example"),
            ("http://panel.example:443/x", "http://panel.example"),  # not http's own port
            ("https://panel.example:80/x", "https://panel.example"),
        )
        for link, provider in other:
            with self.subTest(link):
                self.assertNotEqual(host_of(link), host_of(provider))

    def test_a_providers_stream_hosts_and_zone_lookup_compare_alike(self):
        # The catalog's stream hosts and the catch-up zone's lookup use the same host form.
        from services.catalog import _host
        from ui import item_provider

        for url in ("http://Streams.test:80", "https://streams.test:443/", "http://streams.test"):
            with self.subTest(url):
                self.assertEqual(_host(url), host_of("http://streams.test"))
        asked = []

        class Catalog:
            def xtream_zone_at(self, host):
                asked.append(host)

        item_provider.xtream_zone_at(Catalog(), "http://Panel.test:80/live/u/p/1.ts")
        self.assertEqual(asked, [host_of("http://panel.test")])


class OwnerTests(unittest.TestCase):
    PANEL = {
        "type": "XTREAM",
        "name": "Panel",
        "url": "http://panel.example:8080",
        "username": "Demo",
        "password": "secret",
    }
    PLAYLIST = {"type": "M3UPLAYLIST", "name": "Lists", "url": "http://panel.example:8080/a.m3u"}

    def providers(self, streams=()):
        return [
            (self.PLAYLIST, {"panel.example:8080"}),
            (self.PANEL, {"panel.example:8080", *streams}),
        ]

    def test_an_xtream_link_names_its_configured_account(self):
        for url in (
            "http://panel.example:8080/live/demo/secret/12.ts",
            "https://panel.example:8080/movie/demo/secret/55.mkv",
            "http://panel.example:8080/demo/secret/12",  # Xtream's short form
            "http://panel.example:8080/demo/secret/12.m3u8",
            "http://cdn.example/live/demo/secret/12.ts",  # where its streams are
        ):
            with self.subTest(url):
                self.assertEqual(owner_of(url, self.providers({"cdn.example"})), "Panel")
        for url in (
            "http://panel.example:8080/live/other/secret/12.ts",  # another account there
            "http://panel.example:8080/other/secret/12",
            "http://panel.example:9090/live/demo/secret/12.ts",  # another port: another host
            "http://panel.example:8080/demo/secret/news",  # no stream number
            "http://panel.example:8080/hls/demo/12",  # too short for the short form
            "http://cdn.example/live/demo/secret/12.ts",  # not known as its streams' host
            "not a link",
        ):
            with self.subTest(url):
                self.assertEqual(owner_of(url, self.providers()), "")

    def test_a_default_port_written_out_on_either_side_still_names_the_account(self):
        # Final review I1: hosts as `host_of` gives them, on both sides.
        portless = dict(self.PANEL, url="http://panel.example")
        written = dict(self.PANEL, url="http://panel.example:80")
        for provider, url in (
            (portless, "http://panel.example:80/live/demo/secret/12.ts"),
            (written, "http://panel.example/live/demo/secret/12.ts"),
            (portless, "https://panel.example:443/demo/secret/12"),
        ):
            with self.subTest(url):
                hosts = {host_of(provider["url"])}
                self.assertEqual(owner_of(url, [(provider, hosts)]), "Panel")


if __name__ == "__main__":
    unittest.main()
