"""The store of links that played (services/network_streams.py)."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from services import i18n, network_streams as ns
from services.live_sites import KICK, TWITCH, SitePage
from services.network_streams import NetworkStreams, stream_id


class StreamIdTests(unittest.TestCase):
    def test_a_fresh_token_keeps_the_same_entry(self):
        a = stream_id("https://cdn.example/live/chan.m3u8?token=aaa&exp=1")
        b = stream_id("https://cdn.example/live/chan.m3u8?token=bbb&exp=2")
        self.assertEqual(a, b)
        self.assertEqual(len(a), 16)

    def test_a_script_query_is_the_identity(self):
        a = stream_id("http://cdn.example/play.php?id=5&t=x")
        self.assertEqual(a, stream_id("http://cdn.example/play.php?t=x&id=5"))
        self.assertNotEqual(a, stream_id("http://cdn.example/play.php?id=6&t=x"))
        # a last segment with no extension counts as a script too
        self.assertNotEqual(
            stream_id("http://cdn.example/stream?id=5"), stream_id("http://cdn.example/stream?id=6")
        )

    def test_scheme_and_default_port_dont_split_an_entry(self):
        base = stream_id("http://CDN.example/a/b.ts")
        self.assertEqual(base, stream_id("https://cdn.example/a/b.ts"))
        self.assertEqual(base, stream_id("http://cdn.example:80/a/b.ts"))
        self.assertEqual(base, stream_id("https://cdn.example:443/a/b.ts#frag"))
        self.assertNotEqual(base, stream_id("http://cdn.example:8080/a/b.ts"))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "network_streams.json"

    def test_keep_updates_url_and_keeps_the_name(self):
        s = NetworkStreams(self.path)
        first = s.keep("https://cdn.example/x/ch.m3u8?token=1", "News", "LIVE")
        second = s.keep("https://cdn.example/x/ch.m3u8?token=2", "other", "FILM")
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(s.entries()), 1)
        got = s.get(first.id)
        self.assertEqual(got.name, "News")
        self.assertEqual(got.url, "https://cdn.example/x/ch.m3u8?token=2")
        self.assertEqual(got.kind, "FILM")

    def test_failed_then_opened_clears_the_reason(self):
        s = NetworkStreams(self.path)
        e = s.keep("http://h.example/a.ts", "A", "LIVE")
        s.failed(e.id, "timed out")
        self.assertEqual(s.get(e.id).failed, "timed out")
        s.opened(e.id, 12.5)
        self.assertEqual(s.get(e.id).failed, "")
        self.assertEqual(s.get(e.id).opened, 12.5)

    def test_newest_opened_first_rename_set_url_remove(self):
        s = NetworkStreams(self.path)
        a = s.keep("http://h.example/a.ts", "A", "LIVE")
        b = s.keep("http://h.example/b.ts", "B", "LIVE")
        s.opened(a.id, 10)
        s.opened(b.id, 5)
        self.assertEqual([e.name for e in s.entries()], ["A", "B"])
        s.rename(b.id, "Bee")
        s.set_url(b.id, "http://h.example/b2.ts")
        self.assertEqual(s.get(b.id).name, "Bee")
        self.assertEqual(s.get(b.id).url, "http://h.example/b2.ts")
        s.remove(a.id)
        self.assertIsNone(s.get(a.id))
        self.assertEqual(len(s.entries()), 1)

    def test_a_broken_file_reads_as_empty(self):
        for text in ("{not json", "[]", '{"v": 1, "entries": "x"}', '{"v": 1, "entries": [3]}'):
            self.path.write_text(text, encoding="utf-8")
            self.assertEqual(NetworkStreams.read(self.path).entries(), [], text)
        missing = NetworkStreams.read(self.path.with_name("missing.json"))
        self.assertEqual(missing.entries(), [])
        self.assertFalse(missing.broken)  # nothing to set aside

    def test_making_a_store_reads_nothing(self):
        self.path.write_text('{"v": 1, "entries": [{"id": "a", "url": "http://h/a.ts"}]}')
        self.assertEqual(NetworkStreams(self.path).entries(), [])  # the GUI thread's
        self.assertEqual(len(NetworkStreams.read(self.path).entries()), 1)  # a worker's

    def test_one_bad_row_keeps_the_others(self):
        rows = [
            {"id": "a", "name": "A", "url": "http://h.example/a.ts", "kind": "LIVE"},
            {"name": "no id"},
            "text",
            {"id": "b", "url": "http://h.example/b.mkv", "kind": "FILM", "opened": "soon"},
            {"id": "c", "url": "http://h.example/c.ts", "opened": 1e400},  # inf in JSON
            {"id": "d", "url": "", "kind": "LIVE"},  # no address
        ]
        store = NetworkStreams(self.path)
        self.assertTrue(store.load(json.dumps({"v": 1, "entries": rows}).encode()))
        self.assertEqual(sorted(e.id for e in store.entries()), ["a", "b", "c"])
        self.assertEqual(store.get("b").opened, 0.0)  # never a time that is no number
        self.assertEqual(store.get("c").opened, 0.0)
        self.assertFalse(store.broken)

    def test_a_time_too_large_for_a_number_reads_as_never(self):
        rows = [
            {"id": "a", "url": "http://h.example/a.ts", "opened": 10**400},
            {"id": "b", "url": "http://h.example/b.ts", "opened": 5.0},
        ]
        store = NetworkStreams(self.path)
        self.assertTrue(store.load(json.dumps({"v": 1, "entries": rows}).encode()))
        self.assertEqual([(e.id, e.opened) for e in store.entries()], [("b", 5.0), ("a", 0.0)])
        self.assertTrue(store.merge(b'{"v": 1, "entries": [[[[[]]]]]}'))  # never raises

    def test_a_file_that_cannot_be_set_aside_is_never_written_over(self):
        self.path.write_text("{not json", encoding="utf-8")
        with mock.patch.object(ns, "set_file_aside", return_value=None):
            with self.assertRaises(OSError):
                ns.save(self.path, b'{"v": 1, "entries": []}', set_aside=True)
        self.assertEqual(self.path.read_text(), "{not json")

    def test_an_odd_kind_or_time_is_kept_plainly(self):
        store = NetworkStreams(self.path)
        entry = store.keep("http://h.example/a.ts", "A", "film")
        self.assertEqual(entry.kind, "LIVE")  # only FILM is a film
        self.assertEqual(store.keep("http://h.example/a.ts", "A", "FILM").kind, "FILM")
        store.opened(entry.id, float("nan"))
        self.assertEqual(store.get(entry.id).opened, 0.0)

    def test_a_broken_file_is_set_aside_before_the_first_write(self):
        self.path.write_text("{not json", encoding="utf-8")
        store = NetworkStreams.read(self.path)
        self.assertTrue(store.broken)
        store.keep("http://h.example/a.ts", "A", "LIVE")
        ns.save(self.path, store.save_bytes(), set_aside=store.broken)
        aside = [n for n in os.listdir(self.dir.name) if n.startswith(self.path.name + ".broken-")]
        self.assertEqual(len(aside), 1)
        self.assertEqual((Path(self.dir.name) / aside[0]).read_text(), "{not json")
        self.assertEqual(len(NetworkStreams.read(self.path).entries()), 1)

    def test_the_file_read_late_keeps_what_played_since(self):
        older = NetworkStreams(self.path)
        kept = older.keep("http://h.example/a.ts?token=1", "Harbour News", "LIVE")
        older.keep("http://h.example/b.ts", "B", "LIVE")
        older.failed(kept.id, "timed out")
        store = NetworkStreams(self.path)  # the file not read yet: a link plays meanwhile
        played = store.keep("http://h.example/a.ts?token=2", "h.example — a", "LIVE")
        store.opened(played.id, 50.0)
        self.assertTrue(store.merge(older.save_bytes()))
        entry = store.get(kept.id)
        self.assertEqual(entry.name, "Harbour News")  # the name it was given stays
        self.assertEqual(entry.url, "http://h.example/a.ts?token=2")  # what played wins
        self.assertEqual((entry.opened, entry.failed), (50.0, ""))
        self.assertEqual(len(store.entries()), 2)

    def test_a_link_item_is_told_by_its_provider_or_its_address(self):
        url = "http://h.example/a.ts?token=1"
        item = {"id": stream_id(url), "name": "A", "cmd": url, "_provider": "Panel"}
        self.assertTrue(ns.is_link(item))  # its id is its address's
        self.assertTrue(ns.is_link({"id": 7, "cmd": "http://h/1.ts", "_provider": "@network"}))
        self.assertFalse(ns.is_link({"id": 7, "cmd": url, "_provider": "Panel"}))
        self.assertFalse(ns.is_link({"id": stream_id(url), "cmd": "", "_provider": "Panel"}))

    def test_round_trip_bytes(self):
        s = NetworkStreams(self.path)
        e = s.keep("http://h.example/a.mkv", "Film", "FILM")
        s.opened(e.id, 3.0)
        data = s.save_bytes()
        self.assertEqual(json.loads(data)["v"], 1)
        ns.write(self.path, data)
        again = NetworkStreams.read(self.path)
        self.assertEqual(again.entries(), s.entries())
        other = NetworkStreams(self.path.with_name("o.json"))
        other.load(data)
        self.assertEqual(other.entries(), s.entries())

    def test_a_failure_is_said_plainly(self):
        cases = (
            (("The stream could not be opened", "http", 401), "401: the link's access ran out"),
            (("The stream could not be opened", "", None), "The link could not be opened"),
            ((ns.STREAM_REFUSED, "http", 403), "403: the link was refused"),
            ((ns.STREAM_REFUSED, "", None), "403: the link was refused"),
            ((ns.STREAM_LIMIT, "http", 429), "The link's server allows no more streams now"),
            (("x", "http", 404), "404: nothing is at this address"),
            (("x", "http", 410), "404: nothing is at this address"),
            (("x", "timeout", None), "The link didn't answer in time"),
        )
        for (message, kind, status), why in cases:
            with self.subTest(message=message, kind=kind, status=status):
                self.assertEqual(ns.failure_reason(message, kind, status), why)

    def test_default_name(self):
        self.assertEqual(
            ns.default_name("https://cdn.example/live/channel.m3u8?t=1"), "cdn.example — channel"
        )
        self.assertEqual(ns.default_name("http://cdn.example/"), "cdn.example")
        self.assertEqual(ns.default_name("http://cdn.example/a/index.m3u8"), "cdn.example — a")

    def test_a_site_page_is_named_by_its_channel(self):
        # Known before the site is asked: the page names the channel.
        self.assertEqual(
            ns.default_name("https://kick.com/examplechannel"), "examplechannel · Kick"
        )
        self.assertEqual(
            ns.default_name("https://www.twitch.tv/otherchannel"), "otherchannel · Twitch"
        )

    def test_a_site_page_keeps_its_name_once_renamed(self):
        store = NetworkStreams(Path("unused.json"))
        page = "https://kick.com/examplechannel"
        entry = store.keep(page, ns.default_name(page), ns.LIVE)
        self.assertEqual((entry.url, entry.name), (page, "examplechannel · Kick"))
        store.rename(entry.id, "Evening show")
        store.keep(page, "examplechannel · Kick", ns.LIVE)
        self.assertEqual(store.get(entry.id).name, "Evening show")

    def test_why_a_site_page_did_not_play(self):
        kick, twitch = SitePage(KICK, "examplechannel"), SitePage(TWITCH, "otherchannel")
        refused = "refused QiTV's request: try again later, or paste the stream link"
        cases = (
            ("offline", kick, "examplechannel isn't live now"),
            ("unknown", kick, "No channel named examplechannel on Kick"),
            ("refused", kick, f"Kick {refused}"),
            ("refused", twitch, f"Twitch {refused}"),
            ("network", kick, "The link could not be opened"),  # (not reached, nor said why)
            ("timeout", kick, "The link didn't answer in time"),  # (it never answered in time)
            ("anything else", twitch, "The link could not be opened"),
        )
        for reason, page, why in cases:
            with self.subTest(reason=reason, site=page.site):
                self.assertEqual(ns.site_failure(reason, page), why)
                self.assertEqual(ns.shown_reason(why), why)  # (in English)
        # Kept in English, each shows in the active language with its channel and site.
        catalog = {
            "{channel} isn't live now": "{channel} şu anda yayında değil",
            "No channel named {channel} on {site}": "{site} üzerinde {channel} adlı bir kanal yok",
            "The link didn't answer in time": "Bağlantı zamanında yanıt vermedi",
        }
        with mock.patch.object(i18n, "_catalog", catalog):
            self.assertEqual(
                ns.shown_reason("examplechannel isn't live now"),
                "examplechannel şu anda yayında değil",
            )
            self.assertEqual(
                ns.shown_reason("No channel named other_channel-2 on Twitch"),
                "Twitch üzerinde other_channel-2 adlı bir kanal yok",
            )
            self.assertEqual(
                ns.shown_reason("The link didn't answer in time"),
                "Bağlantı zamanında yanıt vermedi",
            )
            self.assertEqual(ns.shown_reason("Something else"), "Something else")
        self.assertTrue(ns.names_its_channel("examplechannel isn't live now"))
        self.assertFalse(ns.names_its_channel("The link didn't answer in time"))


class SiteSpellingTests(unittest.TestCase):
    """Owner 2026-11-25: one site channel saved under several spellings (www., letter case, a
    query) is one row of Network streams, keyed by its page; older builds' duplicates merge as
    the list loads, under an id one of them had (what else keys by it stays), and the file stays
    one older 2.x builds read."""

    PAGES = (
        "https://kick.com/examplechannel",
        "https://www.kick.com/ExampleChannel",
        "kick.com/examplechannel?ref=share",
        "https://m.kick.com/examplechannel/",
    )

    @staticmethod
    def old_id(url):
        """The id an older build gave the row: its address alone, as any link's."""
        with mock.patch.object(ns, "site_page", lambda url: None):
            return stream_id(url)

    def test_a_site_channels_spellings_are_one_stream(self):
        ids = {stream_id(page) for page in self.PAGES}
        self.assertEqual(ids, {stream_id("https://kick.com/examplechannel")})
        self.assertEqual(
            stream_id("https://twitch.tv/OtherChannel"),
            stream_id("https://www.twitch.tv/otherchannel"),
        )
        self.assertNotEqual(
            stream_id("https://kick.com/examplechannel"), stream_id("https://kick.com/otherchannel")
        )
        self.assertNotEqual(  # (another site's channel of that name is another stream)
            stream_id("https://kick.com/examplechannel"),
            stream_id("https://twitch.tv/examplechannel"),
        )
        # The way it is written most often keeps the id older builds gave it.
        page = "https://kick.com/examplechannel"
        self.assertEqual(stream_id(page), self.old_id(page))

    def rows(self, *rows):
        return json.dumps({"v": 1, "entries": list(rows)}).encode("utf-8")

    def test_older_builds_duplicates_merge_as_the_list_loads(self):
        www, plain = self.PAGES[1], "https://kick.com/examplechannel?ref=x"
        data = self.rows(
            {"id": self.old_id(www), "name": "Evening show", "url": www, "kind": "LIVE",
             "opened": 10.0, "failed": ""},
            {"id": self.old_id(plain), "name": "examplechannel · Kick", "url": plain,
             "kind": "LIVE", "opened": 30.0, "failed": "examplechannel isn't live now"},
            {"id": "other", "name": "B", "url": "http://h.example/b.ts", "kind": "LIVE"},
        )  # fmt: skip
        store = NetworkStreams(Path("unused.json"))
        self.assertTrue(store.load(data))
        kept = [e for e in store.entries() if e.id != "other"]
        self.assertEqual(len(kept), 1)
        entry = kept[0]
        self.assertIn(entry.id, (self.old_id(www), self.old_id(plain)))  # one it had
        self.assertEqual(entry.name, "Evening show")  # the name given, not the default
        self.assertEqual(entry.opened, 30.0)  # the newest play
        self.assertEqual(entry.failed, "examplechannel isn't live now")  # and how it went
        self.assertEqual(entry.kind, "LIVE")
        self.assertEqual(len(store.entries()), 2)
        # Playing it again by any spelling keeps it in that one row.
        again = store.keep(self.PAGES[3], "", "LIVE")
        self.assertEqual(again.id, entry.id)
        self.assertEqual(len(store.entries()), 2)
        # Saved as older 2.x builds read it: the same format, one row for the channel.
        saved = json.loads(store.save_bytes())
        self.assertEqual(saved["v"], 1)
        self.assertEqual(len(saved["entries"]), 2)
        self.assertEqual(
            set(saved["entries"][0]), {"id", "name", "url", "kind", "opened", "failed"}
        )

    def test_the_row_under_its_pages_own_id_is_the_one_kept(self):
        page, www = "https://kick.com/examplechannel", self.PAGES[1]
        data = self.rows(
            {"id": self.old_id(www), "name": "examplechannel · Kick", "url": www, "kind": "LIVE",
             "opened": 50.0},
            {"id": stream_id(page), "name": "examplechannel · Kick", "url": page, "kind": "LIVE",
             "opened": 5.0},
        )  # fmt: skip
        store = NetworkStreams(Path("unused.json"))
        store.load(data)
        self.assertEqual([e.id for e in store.entries()], [stream_id(page)])
        self.assertEqual(store.get(stream_id(page)).opened, 50.0)

    def test_a_row_kept_while_the_file_was_read_merges_with_its_duplicate(self):
        www = self.PAGES[1]
        data = self.rows(
            {"id": self.old_id(www), "name": "Evening show", "url": www, "kind": "LIVE",
             "opened": 10.0},
        )  # fmt: skip
        store = NetworkStreams(Path("unused.json"))
        played = store.keep("https://kick.com/examplechannel", "", "LIVE")  # before the read
        store.opened(played.id, 60.0)
        self.assertTrue(store.merge(data))
        entries = store.entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual((entries[0].name, entries[0].opened), ("Evening show", 60.0))

    def test_other_links_of_one_stream_are_left_as_they_are(self):
        # (Two rows of one stream may be the viewer's: a link whose address was replaced.)
        data = self.rows(
            {"id": "a", "name": "A", "url": "http://h.example/a.ts?token=1", "kind": "LIVE"},
            {"id": "b", "name": "B", "url": "http://h.example/a.ts?token=2", "kind": "LIVE"},
        )
        store = NetworkStreams(Path("unused.json"))
        store.load(data)
        self.assertEqual(len(store.entries()), 2)


if __name__ == "__main__":
    unittest.main()


class SiteMediaStoreTests(unittest.TestCase):
    """2.15: a Kick or Twitch video or clip is one row whichever of its pages named it, a film,
    named by its id until its site gives its title; why it gave none says it plainly."""

    def test_one_video_or_clip_is_one_stream_and_none_other(self):
        uuid = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
        self.assertEqual(
            stream_id(f"https://kick.com/examplechannel/videos/{uuid}"),
            stream_id(f"kick.com/video/{uuid}"),
        )
        self.assertEqual(
            stream_id("https://www.twitch.tv/videos/123?t=1h2m"), stream_id("twitch.tv/videos/123")
        )
        self.assertEqual(
            stream_id("https://clips.twitch.tv/Slug"),
            stream_id("twitch.tv/examplechannel/clip/Slug"),
        )
        self.assertNotEqual(stream_id("clips.twitch.tv/Slug"), stream_id("clips.twitch.tv/slug"))
        self.assertNotEqual(stream_id("twitch.tv/videos/123"), stream_id("twitch.tv/videos/124"))
        self.assertNotEqual(
            stream_id("kick.com/examplechannel?clip=clip_1"), stream_id("kick.com/examplechannel")
        )

    def test_its_name_before_its_title_is_its_id_and_site(self):
        self.assertEqual(ns.default_name("twitch.tv/videos/123"), "123 · Twitch")
        self.assertEqual(ns.default_name("kick.com/examplechannel?clip=clip_1"), "clip_1 · Kick")

    def test_why_a_video_did_not_play_is_said_alone_and_translated(self):
        from services.live_sites import CLIP, VIDEO, SiteMedia

        video = SiteMedia(TWITCH, VIDEO, "123")
        gone = ns.site_failure("unknown", video)
        self.assertEqual(gone, "This video is gone from Twitch")
        private = ns.site_failure("private", SiteMedia(KICK, CLIP, "clip_1"))
        self.assertEqual(private, "This video is private or for subscribers only on Kick")
        self.assertEqual(ns.site_failure("refused", video), ns.SITE_REFUSED.format(site="Twitch"))
        self.assertEqual(ns.site_failure("timeout", video), ns.TIMED_OUT)
        self.assertEqual(ns.site_failure("network", video), ns.FAILED)
        for why in (gone, private):
            self.assertTrue(ns.names_its_channel(why))
            self.assertTrue(ns.is_site_failure(why))
            self.assertFalse(ns.is_offline(why))
        self.addCleanup(i18n.set_language, "en")
        i18n.set_language("de")
        self.assertNotEqual(ns.shown_reason(gone), gone)
        self.assertIn("Twitch", ns.shown_reason(gone))

    def test_an_older_builds_rows_of_one_video_merge_as_a_film(self):
        rows = [
            {"id": "aaaa", "name": "x", "url": "https://www.twitch.tv/videos/123", "kind": "LIVE"},
            {
                "id": "bbbb",
                "name": "y",
                "url": "twitch.tv/videos/123?t=5",
                "kind": "LIVE",
                "opened": 5,
            },
        ]
        store = NetworkStreams(Path("unused"))
        store.load(json.dumps({"v": 1, "entries": rows}).encode("utf-8"))
        self.assertEqual(len(store.entries()), 1)
        self.assertEqual(store.entries()[0].kind, "FILM")
