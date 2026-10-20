import json
import os
from types import SimpleNamespace
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from services.catalog import ALL_GROUP_ID, Group
from services.m3u import decode_playlist, parse_m3u, split_cached_groups
from services.navigation import FAVORITES_GROUP, NavModel
from services.zapping import ZapContext
from tests.test_catalog_service import make_service

ENTRY = [
    "#EXTM3U",
    '#EXTINF:-1 tvg-id="one.test" group-title="News",Channel One',
    "#EXTVLCOPT:http-user-agent=UA/1",
    "http://stream.test/1.ts",
]


class ParseM3uLineEndingTests(unittest.TestCase):
    def _assert_clean(self, data):
        [item] = parse_m3u(data)
        self.assertEqual(item["name"], "Channel One")
        self.assertEqual(item["user_agent"], "UA/1")
        self.assertEqual(item["cmd"], "http://stream.test/1.ts")
        self.assertEqual(item["xmltv_id"], "one.test")

    def test_lf(self):
        self._assert_clean("\n".join(ENTRY) + "\n")

    def test_crlf(self):
        self._assert_clean("\r\n".join(ENTRY) + "\r\n")

    def test_classic_mac_cr(self):
        self._assert_clean("\r".join(ENTRY) + "\r")

    def test_utf8_bom(self):
        self._assert_clean("\ufeff" + "\r\n".join(ENTRY))


class ParseM3uHttpOptionTests(unittest.TestCase):
    """A channel's http-user-agent and http-referrer, from #EXTVLCOPT lines or #EXTINF
    attributes (iptv-org writes both), stay on its item, cached playlists included."""

    def test_extvlcopt_lines_give_the_agent_and_referrer(self):
        data = "\n".join(
            [
                "#EXTM3U",
                '#EXTINF:-1 group-title="News",Channel One',
                "#EXTVLCOPT:http-referrer=https://site.test/live/",
                "#EXTVLCOPT:http-user-agent=UA/1 (X; Y)",
                "http://stream.test/1.m3u8",
            ]
        )
        [item] = parse_m3u(data)
        self.assertEqual(item["referrer"], "https://site.test/live/")
        self.assertEqual(item["user_agent"], "UA/1 (X; Y)")

    def test_extinf_attributes_give_them_too(self):
        data = (
            "#EXTM3U\n"
            '#EXTINF:-1 http-referrer="https://site.test/" http-user-agent="UA/2",Two\n'
            "http://stream.test/2.m3u8\n"
        )
        [item] = parse_m3u(data)
        self.assertEqual((item["referrer"], item["user_agent"]), ("https://site.test/", "UA/2"))

    def test_a_channel_without_them_is_as_before(self):
        data = "#EXTM3U\n#EXTINF:-1,Plain\nhttp://stream.test/3.ts\n#EXTINF:-1,Next\nhttp://s/4\n"
        plain, following = parse_m3u(data)
        self.assertNotIn("referrer", plain)
        self.assertIsNone(plain["user_agent"])
        self.assertNotIn("referrer", following)  # nothing carries over to the next channel

    def test_a_cached_playlist_keeps_them(self):
        data = (
            "#EXTM3U\n"
            '#EXTINF:-1 group-title="A;B",One\n#EXTVLCOPT:http-referrer=https://site.test/\n'
            "http://stream.test/1.m3u8\n"
        )
        payload = json.loads(json.dumps(parse_m3u(data, categorize=True)))
        [item] = payload["contents"]
        self.assertEqual(item["referrer"], "https://site.test/")
        # A playlist cached before "A;B" titles were split is rebuilt from its items alike.
        payload["categories"] = [{"id": "A;B", "title": "A;B"}]
        [rebuilt] = split_cached_groups(payload)["contents"]
        self.assertEqual(rebuilt["referrer"], "https://site.test/")


class ParseM3uUnicodeTests(unittest.TestCase):
    def test_c1_and_unicode_separators_do_not_split_lines(self):
        for ch in ("\x85", "\u2028", "\u2029", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e"):
            data = f'#EXTM3U\n#EXTINF:-1 group-title="G{ch}x",Name{ch}y\nhttp://s.test/1.ts\n'
            [item] = parse_m3u(data)
            self.assertEqual(item["name"], f"Name{ch}y")
            self.assertEqual(item["group"], f"G{ch}x")


class _Resp:
    def __init__(self, content, content_type=None):
        self.content = content
        self.headers = {} if content_type is None else {"Content-Type": content_type}
        # requests decodes text/* without charset as ISO-8859-1
        self.text = content.decode("iso-8859-1" if content_type is None else "utf-8", "replace")


class DecodePlaylistTests(unittest.TestCase):
    PLAYLIST = '#EXTM3U\n#EXTINF:-1 group-title="أفلام",Мохнатый канал\nhttp://s.test/1.ts\n'

    def test_no_charset_decodes_utf8_and_parses(self):
        resp = _Resp(("\ufeff" + self.PLAYLIST).encode("utf-8"), "text/plain")
        resp.text = resp.content.decode("iso-8859-1")
        [item] = parse_m3u(decode_playlist(resp))
        self.assertEqual(item["group"], "أفلام")
        self.assertEqual(item["name"], "Мохнатый канал")

    def test_missing_content_type_header(self):
        resp = _Resp(self.PLAYLIST.encode("utf-8"))
        self.assertIn("أفلام", decode_playlist(resp))

    def test_declared_charset_uses_response_text(self):
        resp = _Resp(b"x", "text/plain; charset=windows-1251")
        resp.text = "from-text"
        self.assertEqual(decode_playlist(resp), "from-text")

    def test_invalid_bytes_are_replaced(self):
        resp = _Resp(b"#EXTM3U\n\xff\xfe", "application/octet-stream")
        self.assertIn("\ufffd", decode_playlist(resp))


if __name__ == "__main__":
    unittest.main()


# iptv-org writes channels of several categories as one group-title: "Animation;Kids;Music".
MULTI_GROUP = "\n".join(
    [
        "#EXTM3U",
        '#EXTINF:-1 tvg-id="toons.test" group-title="Animation;Kids;Music",Toons',
        "http://stream.test/toons.ts",
        '#EXTINF:-1 group-title="Kids",Blocks',
        "http://stream.test/blocks.ts",
        '#EXTINF:-1 group-title="Music",Beats',
        "http://stream.test/beats.ts",
        '#EXTINF:-1 group-title="Animation; ;Animation;Kids",Doodles',
        "http://stream.test/doodles.ts",
        '#EXTINF:-1 group-title="News & Weather",Daily',
        "http://stream.test/daily.ts",
    ]
)


def names(items):
    return [item["name"] for item in items]


class GroupTitleSplitTests(unittest.TestCase):
    """A group title with ";" names several groups (M3U only)."""

    def setUp(self):
        self.playlist = parse_m3u(MULTI_GROUP, categorize=True)

    def members(self, group):
        contents = self.playlist["contents"]
        return names(contents[i] for i in self.playlist["sorted_channels"][group])

    def test_a_channel_of_two_groups_is_in_both(self):
        self.assertEqual(self.members("Animation"), ["Toons", "Doodles"])
        self.assertEqual(self.members("Kids"), ["Toons", "Blocks", "Doodles"])
        self.assertEqual(self.members("Music"), ["Toons", "Beats"])

    def test_parts_are_stripped_and_empty_or_repeated_ones_dropped(self):
        # "Animation; ;Animation;Kids": Animation and Kids, Doodles once in each.
        groups = [g for g, members in self.playlist["sorted_channels"].items() if 3 in members]
        self.assertEqual(groups, ["Animation", "Kids"])

    def test_the_groups_keep_their_order_and_one_channel_stays_one_item(self):
        titles = [c["title"] for c in self.playlist["categories"]]
        self.assertEqual(titles, ["All", "Animation", "Kids", "Music", "News & Weather"])
        self.assertEqual(
            names(self.playlist["contents"]), ["Toons", "Blocks", "Beats", "Doodles", "Daily"]
        )
        toons = self.playlist["contents"][0]
        self.assertEqual(toons["group"], "Animation;Kids;Music")  # exports write it back as is
        self.assertEqual(toons["tv_genre_id"], "Animation")

    def test_a_title_without_a_semicolon_is_unchanged(self):
        playlist = parse_m3u(
            '#EXTM3U\n#EXTINF:-1 group-title=" Kids ",A\nhttp://s.test/a.ts\n'
            "#EXTINF:-1,B\nhttp://s.test/b.ts\n",
            categorize=True,
        )
        self.assertEqual(playlist["sorted_channels"], {" Kids ": [0], "Uncategorized": [1]})


class RepeatedChannelTests(unittest.TestCase):
    """A playlist grouped by country repeats a channel of several countries, line for line but
    for its group: one channel, listed in each group and once in All."""

    PLAYLIST = (
        "#EXTM3U\n"
        '#EXTINF:-1 tvg-id="One.us" group-title="Canada",One\nhttp://s.test/one.m3u8\n'
        '#EXTINF:-1 tvg-id="Two.ca" group-title="Canada",Two\nhttp://s.test/two.m3u8\n'
        '#EXTINF:-1 tvg-id="One.us" group-title="United States",One\nhttp://s.test/one.m3u8\n'
        '#EXTINF:-1 tvg-id="Uno.us" group-title="United States",One\nhttp://s.test/one.m3u8\n'
    )

    def test_one_channel_in_each_of_its_groups_and_once_in_all(self):
        playlist = parse_m3u(self.PLAYLIST, categorize=True)
        contents = playlist["contents"]
        # The last line differs (its tvg-id): another channel, though its stream is the same.
        self.assertEqual([c["xmltv_id"] for c in contents], ["One.us", "Two.ca", "Uno.us"])
        self.assertEqual([c["number"] for c in contents], ["1", "2", "3"])
        self.assertEqual(playlist["sorted_channels"], {"Canada": [0, 1], "United States": [0, 2]})
        self.assertEqual(contents[0]["group"], "Canada")  # as its first line says

    def test_a_line_repeated_in_its_own_group_is_listed_once(self):
        line = '#EXTINF:-1 group-title="News",Daily\nhttp://s.test/daily.m3u8\n'
        playlist = parse_m3u("#EXTM3U\n" + line + line, categorize=True)
        self.assertEqual(len(playlist["contents"]), 1)
        self.assertEqual(playlist["sorted_channels"], {"News": [0]})


class MultiGroupChannelTests(unittest.TestCase):
    """One channel in several groups stays one channel: All, zapping and Favorites."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        provider = {"name": "Home", "type": "M3UPLAYLIST", "url": "http://playlist.test/a.m3u"}
        self.catalog, _providers, factory, _epg = make_service(provider)
        factory.results["m3u"] = {"result": parse_m3u(MULTI_GROUP, categorize=True)}
        self.catalog.request_section("live")
        self.config = SimpleNamespace(favorites=[], watch_history=[])
        self.nav = NavModel(self.catalog, self.config)

    def test_all_lists_and_counts_each_channel_once(self):
        all_items = self.catalog.items("live", ALL_GROUP_ID)
        self.assertEqual(names(all_items), ["Toons", "Blocks", "Beats", "Doodles", "Daily"])
        self.assertEqual(self.catalog.groups("live")[0], Group("live", ALL_GROUP_ID, "All", 5))

    def test_the_same_channel_in_each_group(self):
        toons = [
            next(i for i in self.catalog.items("live", g) if i["name"] == "Toons")
            for g in ("Animation", "Kids", "Music")
        ]
        self.assertTrue(all(item is toons[0] for item in toons))  # one identity everywhere

    def test_zapping_in_a_group_walks_that_groups_channels_only(self):
        self.nav.reset_to("live", "Kids")
        channels = [node.payload for node in self.nav.children(self.nav.path[-1])]
        self.assertEqual(names(channels), ["Toons", "Blocks", "Doodles"])
        zap = ZapContext("Kids", channels)
        zap.set_current(channels[0])
        walked = []
        for _ in range(3):
            index, channel = zap.peek(1)
            zap.current_index = index
            walked.append(channel["name"])
        self.assertEqual(walked, ["Blocks", "Doodles", "Toons"])

    def test_a_favorite_in_several_groups_shows_once(self):
        self.config.favorites = ["Toons"]
        self.nav.reset_to("live")
        pinned = self.nav.children(self.nav.path[1])[0]
        self.assertEqual((pinned.key, pinned.count), (FAVORITES_GROUP, 1))
        self.assertEqual([n.title for n in self.nav.children(pinned)], ["Toons"])
