"""The store of links that played (services/network_streams.py)."""

import json
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
            self.assertEqual(NetworkStreams(self.path).entries(), [], text)
        self.assertEqual(NetworkStreams(self.path.with_name("missing.json")).entries(), [])

    def test_round_trip_bytes(self):
        s = NetworkStreams(self.path)
        e = s.keep("http://h.example/a.mkv", "Film", "FILM")
        s.opened(e.id, 3.0)
        data = s.save_bytes()
        self.assertEqual(json.loads(data)["v"], 1)
        ns.write(self.path, data)
        again = NetworkStreams(self.path)
        self.assertEqual(again.entries(), s.entries())
        other = NetworkStreams(self.path.with_name("o.json"))
        other.load(data)
        self.assertEqual(other.entries(), s.entries())

    def test_default_name(self):
        self.assertEqual(
            ns.default_name("https://cdn.example/live/channel.m3u8?t=1"), "cdn.example — channel"
        )
        self.assertEqual(ns.default_name("http://cdn.example/"), "cdn.example")
        self.assertEqual(ns.default_name("http://cdn.example/a/index.m3u8"), "cdn.example — a")


if __name__ == "__main__":
    unittest.main()
