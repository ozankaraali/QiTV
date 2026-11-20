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
        got = probe_text(text)
        self.assertIsInstance(got, Provider)
        self.assertEqual(got.link.type, M3U)

    def test_a_probe_cannot_name_the_playlist_without_its_address(self):
        text = "#EXTM3U\n#EXTINF:-1,A\nhttp://cdn.example/a.ts\n"
        got = probe_text(text, "http://cdn.example/list.m3u8")
        self.assertEqual(got.link.url, "http://cdn.example/list.m3u8")


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


if __name__ == "__main__":
    unittest.main()
