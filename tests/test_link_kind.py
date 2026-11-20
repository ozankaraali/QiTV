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
from services.open_paths import EXTRA_MUSIC_EXTENSIONS, EXTRA_VIDEO_EXTENSIONS, MEDIA_EXTENSIONS
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
            "http://panel.example:8080/live/user/pass/1.ts",
            "http://panel.example:8080/live/user/pass/1",
            "http://panel.example:8080/live/user/pass/1.m3u8",
            "rtmp://cdn.example/app/key",
            "rtsp://cdn.example/cam",
            "udp://@239.0.0.1:1234",
        ):
            self.assertEqual(classify(url), LIVE, url)

    def test_every_media_file_type_qitv_opens_is_a_film_or_asks_its_first_bytes(self):
        # Final review I2: a song or a podcast (.flac, .mpg…) played as a live channel, and
        # replayed from its start for ever. Audio that may be internet radio and the
        # containers live channels come in (.mp3 .aac .ogg .opus .m4a .flac .wav, .flv .ts)
        # are what their answer says (probe_text: a size is a file); any other is a film.
        ambiguous = {"mp3", "aac", "ogg", "opus", "m4a", "flac", "wav", "flv", "ts"}
        kinds = MEDIA_EXTENSIONS | EXTRA_VIDEO_EXTENSIONS | EXTRA_MUSIC_EXTENSIONS
        self.assertLessEqual(ambiguous, kinds)
        for ext in sorted(kinds):
            with self.subTest(ext):
                url = f"https://cdn.example/media/Episode.{ext.upper()}?token=1"
                self.assertEqual(classify(url), PROBE if ext in ambiguous else FILM)
        for ext in ("mp4", "mkv", "avi", "mov", "wmv", "mpg", "mpeg", "webm", "m4v"):
            with self.subTest(ext):  # video files: films by their address
                self.assertEqual(classify(f"http://cdn.example/a/b.{ext}"), FILM)
        # An Xtream live path stays live by its address, whatever its extension, and so does
        # Xtream's short form of a live stream, U/P/<number>.ts, the common M3U one: its panel
        # may allow only so many streams, and QiTV may not know it (re-review R2).
        self.assertEqual(classify("http://panel.example/live/u/p/1.ts"), LIVE)
        for url in ("http://panel.example:8080/demo/secret/12.ts", "https://p.example/u/p/7.TS"):
            with self.subTest(url):
                self.assertEqual(classify(url), LIVE)
        for url in ("http://cdn.example/live/channel.ts", "http://cdn.example/a/b/c/12.ts"):
            with self.subTest(url):  # any other .ts: its answer says
                self.assertEqual(classify(url), PROBE)

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

    def test_a_sized_answer_is_a_film_and_one_without_an_end_live(self):
        # Final review I2: what the answer's headers say, when its first bytes say nothing.
        binary = "ID3\x04\x00\x00\x00\x00\x00\x00\ufffd\ufffd"
        live = "#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:6,\na.ts\n"
        cases = (
            (binary, "audio/mpeg", 3_000_000, FILM),  # a podcast without its extension
            (binary, "video/mp4; charset=binary", 9_000_000, FILM),
            (binary, "application/octet-stream", 500_000, FILM),
            (binary, "", 500_000, FILM),
            (binary, "audio/mpeg", None, LIVE),  # internet radio: it never ends
            (binary, "video/mp2t", None, LIVE),  # a live channel
            (binary, "video/mp4", 0, LIVE),
            ("<html>Sign in</html>", "text/html", 2_000, LIVE),  # no media: VLC says why
            ("#EXTM3U\n#EXTINF:-1,A\na.ts\n", "audio/x-mpegurl", 40, LIVE),  # a playlist
            ("#EXTM3U\n#EXTINF:-1,A\na.ts\n", "", 40, LIVE),
            (live, "application/vnd.apple.mpegurl", 300, LIVE),  # its tags decide
            (live + "#EXT-X-ENDLIST\n", "audio/mpegurl", None, FILM),
            ("\ufffd\ufffd", "video/x-flv", None, LIVE),  # a live .flv: no size
            ("\x00\x00\x00\x18ftypmp42", "application/mp4", 9_000_000, FILM),
            ("OggS\x00\x02", "application/ogg", 4_000_000, FILM),
            # Re-review R1: a list or a manifest with a size is no file that ends: as before.
            ('<?xml version="1.0"?><MPD type="dynamic">', "application/dash+xml", 900, LIVE),
            ("[playlist]\nFile1=http://radio.example/coast\n", "audio/x-scpls", 80, LIVE),
            ("[playlist]\nFile1=http://radio.example/coast\n", "", 80, LIVE),  # untyped too
            ('<?xml version="1.0"?><playlist>', "application/xspf+xml", 300, LIVE),
            ("<asx version='3.0'><entry>", "video/x-ms-asf", 300, LIVE),
            ('{"url": "http://radio.example/coast"}', "application/json", 40, LIVE),
            (binary, "application/x-shockwave-flash", 9_000, LIVE),  # nothing VLC plays as a file
        )
        for text, content_type, length, kind in cases:
            with self.subTest(content_type=content_type, length=length, text=text[:12]):
                got = probe_text(text, "https://cdn.example/watch?v=7", content_type, length)
                self.assertEqual(got, kind)
        channels = "#EXTM3U\n#EXTINF:-1,A\nhttp://cdn.example/a.ts\n"
        got = probe_text(channels, "https://cdn.example/tv", "audio/x-mpegurl", len(channels))
        self.assertIsInstance(got, Provider)

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
