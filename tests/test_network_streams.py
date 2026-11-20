"""The store of links that played (services/network_streams.py)."""

import json
import os
from pathlib import Path
import tempfile
import unittest

from services import network_streams as ns
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


if __name__ == "__main__":
    unittest.main()
