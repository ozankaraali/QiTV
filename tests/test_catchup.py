"""Provider catch-up's services (2.3): which channels have an archive, its addresses in the
panel's time zone, the probe against local fake servers, and what QiTV remembers."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from http_fixtures import LocalSource

from services.catchup import (
    ARCHIVE_KEY,
    PROBE_BYTES,
    XTREAM,
    Answer,
    Archive,
    address_start,
    archive_of,
    archive_urls,
    expand,
    judge,
    m3u_archive_fields,
    m3u_archive_url,
    probe,
    provider_zone,
    xtream_archive_fields,
    xtream_archive_urls,
    xtream_zone_fields,
)
from services.catchup_memory import ANSWERED_S, FAILED_S, CatchupMemory
from services.m3u import parse_m3u
from services.workers import ArchiveProbeWorker

LIVE_TS = Path(__file__).resolve().parent / "fixtures" / "live.ts"
# 14:05:30 UTC on a winter day (the panel's zone is an hour ahead then), and a summer one.
WINTER = datetime(2026, 1, 20, 14, 5, 30, tzinfo=timezone.utc)
SUMMER = datetime(2026, 7, 20, 14, 5, 30, tzinfo=timezone.utc)
NOW = WINTER + timedelta(hours=2)


class FieldTests(unittest.TestCase):
    def test_an_xtream_stream_keeps_its_days_only_with_an_archive(self):
        self.assertEqual(
            xtream_archive_fields({"tv_archive": 1, "tv_archive_duration": 3}), {ARCHIVE_KEY: 3}
        )
        self.assertEqual(
            xtream_archive_fields({"tv_archive": "1", "tv_archive_duration": "7"}), {ARCHIVE_KEY: 7}
        )
        self.assertEqual(xtream_archive_fields({"tv_archive": 1}), {ARCHIVE_KEY: 1})
        self.assertEqual(
            xtream_archive_fields({"tv_archive": 1, "tv_archive_duration": 90}), {ARCHIVE_KEY: 31}
        )
        for stream in ({}, {"tv_archive": 0, "tv_archive_duration": 3}, {"tv_archive": "x"}):
            with self.subTest(stream=stream):
                self.assertEqual(xtream_archive_fields(stream), {})

    def test_m3u_types_need_what_they_fill_in(self):
        self.assertEqual(
            m3u_archive_fields({"catchup": "append", "catchup-source": "?utc={utc}"}),
            {ARCHIVE_KEY: 1, "catchup": "append", "catchup_source": "?utc={utc}"},
        )
        self.assertEqual(
            m3u_archive_fields({"catchup": "flussonic-hls", "catchup-days": "5"}),
            {ARCHIVE_KEY: 5, "catchup": "flussonic"},
        )
        self.assertEqual(
            m3u_archive_fields({"timeshift": "2"}), {ARCHIVE_KEY: 2, "catchup": "shift"}
        )
        self.assertEqual(m3u_archive_fields({"tvg-rec": "3"}), {ARCHIVE_KEY: 3, "catchup": "shift"})
        self.assertEqual(
            m3u_archive_fields({"catchup-source": "http://a.test/{utc}"}),
            {ARCHIVE_KEY: 1, "catchup": "default", "catchup_source": "http://a.test/{utc}"},
        )
        self.assertEqual(m3u_archive_fields({"catchup": "append"}), {})  # nothing to append
        self.assertEqual(m3u_archive_fields({"catchup": "vod", "catchup-days": "3"}), {})
        self.assertEqual(m3u_archive_fields({}), {})

    def test_the_playlists_line_sets_every_channel_and_a_channel_wins(self):
        defaults = {"catchup": "shift", "catchup-days": "4"}
        self.assertEqual(m3u_archive_fields({}, defaults), {ARCHIVE_KEY: 4, "catchup": "shift"})
        self.assertEqual(
            m3u_archive_fields({"catchup": "xc", "catchup-days": "2"}, defaults),
            {ARCHIVE_KEY: 2, "catchup": "xc"},
        )

    def test_archive_of_reads_what_the_loaders_kept(self):
        self.assertEqual(archive_of({ARCHIVE_KEY: 3}), Archive(3, XTREAM))
        self.assertEqual(
            archive_of({ARCHIVE_KEY: 2, "catchup": "append", "catchup_source": "?t={utc}"}),
            Archive(2, "append", "?t={utc}"),
        )
        for item in (
            {},
            {ARCHIVE_KEY: 0},
            {ARCHIVE_KEY: True},
            {ARCHIVE_KEY: "3"},
            {ARCHIVE_KEY: 3, "catchup": "nope"},
            {ARCHIVE_KEY: 3, "catchup": "default"},
        ):
            with self.subTest(item=item):
                self.assertIsNone(archive_of(item))


class M3uParseTests(unittest.TestCase):
    def test_channels_with_an_archive_get_its_keys_and_the_others_none(self):
        playlist = "\n".join(
            [
                '#EXTM3U url-tvg="http://guide.test/epg.xml"',
                '#EXTINF:-1 tvg-id="a" catchup="append" catchup-days="3" '
                'catchup-source="?utc={utc}&lutc={lutc}",Harbour News',
                "http://stream.test/a.ts",
                '#EXTINF:-1 tvg-id="b" timeshift="2",Lighthouse Sport',
                "http://stream.test/b.ts",
                '#EXTINF:-1 tvg-id="c",Quiet Pier',
                "http://stream.test/c.ts",
            ]
        )
        first, second, third = parse_m3u(playlist)
        self.assertEqual(archive_of(first), Archive(3, "append", "?utc={utc}&lutc={lutc}"))
        self.assertEqual(archive_of(second), Archive(2, "shift"))
        self.assertNotIn(ARCHIVE_KEY, third)
        self.assertNotIn("catchup", third)

    def test_the_header_line_gives_every_channel_an_archive(self):
        playlist = "\n".join(
            [
                '#EXTM3U catchup="flussonic" catchup-days="5"',
                "#EXTINF:-1,Harbour News",
                "http://fs.test/harbour/index.m3u8?token=x",
            ]
        )
        [item] = parse_m3u(playlist)
        self.assertEqual(archive_of(item), Archive(5, "flussonic"))


class ZoneTests(unittest.TestCase):
    def test_the_sign_in_gives_the_panels_zone_and_offset(self):
        info = {
            "timezone": "Europe/Berlin",
            "time_now": "2026-01-20 15:05:30",
            "timestamp_now": str(int(WINTER.timestamp())),
        }
        self.assertEqual(xtream_zone_fields(info), {"timezone": "Europe/Berlin", "tz_offset": 60})
        self.assertEqual(xtream_zone_fields({}), {})
        self.assertEqual(xtream_zone_fields(None), {})
        self.assertEqual(xtream_zone_fields({"time_now": "garbage", "timestamp_now": "1"}), {})

    def test_a_known_name_wins_then_the_offset_then_utc(self):
        berlin = provider_zone("Europe/Berlin", 0)
        self.assertEqual(WINTER.astimezone(berlin).utcoffset(), timedelta(hours=1))
        self.assertEqual(SUMMER.astimezone(berlin).utcoffset(), timedelta(hours=2))
        self.assertEqual(
            provider_zone("Mars/Olympus_Mons", 180).utcoffset(None), timedelta(hours=3)
        )
        self.assertEqual(provider_zone(None, None), timezone.utc)
        self.assertEqual(provider_zone("", True), timezone.utc)


class CatalogZoneTests(unittest.TestCase):
    def test_the_catalog_gives_the_zone_its_live_channels_loaded_with(self):
        from tests.test_catalog_service import make_service

        provider = {"name": "X", "type": "XTREAM", "url": "h:1", "username": "u", "password": "p"}
        service, providers, _factory, _ = make_service(provider)
        self.assertEqual(service.archive_zone(), timezone.utc)  # nothing loaded (or older)
        providers.current_provider_content["itv"] = {"timezone": "Nowhere/Here", "tz_offset": -90}
        self.assertEqual(
            WINTER.astimezone(service.archive_zone()).utcoffset(), timedelta(hours=-1.5)
        )
        providers.current_provider_content["itv"] = {"timezone": "Europe/Berlin"}
        self.assertEqual(SUMMER.astimezone(service.archive_zone()).utcoffset(), timedelta(hours=2))


class XtreamAddressTests(unittest.TestCase):
    CMD = "http://panel.test:8080/live/viewer/secret/123.ts"

    def test_the_path_and_the_php_forms_in_the_panels_time(self):
        berlin = provider_zone("Europe/Berlin")
        path, php = xtream_archive_urls(self.CMD, WINTER, 3600, berlin)
        self.assertEqual(
            path, "http://panel.test:8080/timeshift/viewer/secret/60/2026-01-20:15-05/123.ts"
        )
        self.assertEqual(
            php,
            "http://panel.test:8080/streaming/timeshift.php?username=viewer&password=secret"
            "&stream=123&start=2026-01-20:15-05&duration=60",
        )
        summer = xtream_archive_urls(self.CMD, SUMMER, 90, berlin)[0]
        self.assertIn("/2/2026-07-20:16-05/123.ts", summer)  # 90 s: two minutes

    def test_a_panel_under_a_path_a_playlist_address_and_an_hls_channel(self):
        utc = timezone.utc
        under = xtream_archive_urls("http://p.test/xc/live/u/p/7.m3u8", WINTER, 60, utc)[0]
        self.assertEqual(under, "http://p.test/xc/timeshift/u/p/1/2026-01-20:14-05/7.m3u8")
        bare = xtream_archive_urls("http://p.test:81/u/p/7", WINTER, 60, utc)[0]
        self.assertEqual(bare, "http://p.test:81/timeshift/u/p/1/2026-01-20:14-05/7.ts")
        self.assertEqual(xtream_archive_urls("http://p.test/news.m3u8", WINTER, 60, utc), [])
        self.assertEqual(xtream_archive_urls("rtsp://p.test/live/u/p/7.ts", WINTER, 60, utc), [])

    def test_the_accounts_password_finds_one_with_a_slash(self):
        cmd = "http://p.test/live/viewer/se/cret/9.ts"
        path = xtream_archive_urls(
            cmd, WINTER, 60, timezone.utc, username="viewer", password="se/cret"
        )[0]
        self.assertEqual(path, "http://p.test/timeshift/viewer/se/cret/1/2026-01-20:14-05/9.ts")
        php = xtream_archive_urls(
            cmd, WINTER, 60, timezone.utc, username="viewer", password="se/cret"
        )[1]
        self.assertIn("password=se%2Fcret", php)

    def test_an_address_starts_at_its_whole_minute(self):
        self.assertEqual(address_start(Archive(1, XTREAM), WINTER), WINTER.replace(second=0))
        self.assertEqual(address_start(Archive(1, "xc"), WINTER), WINTER.replace(second=0))
        self.assertEqual(address_start(Archive(1, "shift"), WINTER), WINTER)


class TemplateTests(unittest.TestCase):
    END = WINTER + timedelta(minutes=30)

    def fill(self, template):
        return expand(template, WINTER, self.END, NOW)

    def test_the_common_placeholders(self):
        start, end, now = (int(t.timestamp()) for t in (WINTER, self.END, NOW))
        self.assertEqual(self.fill("{utc}|${start}|{start}"), f"{start}|{start}|{start}")
        self.assertEqual(self.fill("{utcend}|${end}"), f"{end}|{end}")
        self.assertEqual(self.fill("{lutc}|${now}|${timestamp}"), f"{now}|{now}|{now}")
        self.assertEqual(self.fill("{duration}|${duration}|{duration:60}"), "1800|1800|30")
        self.assertEqual(self.fill("{offset:60}|${offset}"), f"{(now - start) // 60}|{now - start}")
        self.assertEqual(self.fill("{Y}-{m}-{d}T{H}:{M}:{S}"), "2026-01-20T14:05:30")
        self.assertEqual(
            self.fill("{utc:Y-m-d:H-M}|${start:YmdHMS}"), "2026-01-20:14-05|20260120140530"
        )
        self.assertEqual(self.fill("{unknown}|{duration:x}"), "{unknown}|{duration:x}")

    def test_each_m3u_type(self):
        start = int(WINTER.timestamp())
        now = int(NOW.timestamp())

        def url(kind, cmd, source=""):
            return m3u_archive_url(Archive(1, kind, source), cmd, WINTER, 1800, NOW)

        self.assertEqual(
            url("default", "http://x.test/1", "http://arc.test/{utc}/{duration}.ts"),
            f"http://arc.test/{start}/1800.ts",
        )
        self.assertEqual(
            url("append", "http://x.test/1.ts", "?from={utc}"), f"http://x.test/1.ts?from={start}"
        )
        self.assertEqual(
            url("shift", "http://x.test/1.ts?t=1"), f"http://x.test/1.ts?t=1&utc={start}&lutc={now}"
        )
        self.assertEqual(
            url("flussonic", "http://fs.test/news/index.m3u8?token=x"),
            f"http://fs.test/news/index-{start}-1800.m3u8?token=x",
        )
        self.assertEqual(
            url("flussonic", "http://fs.test/news/mpegts"),
            f"http://fs.test/news/timeshift_abs-{start}.ts",
        )
        self.assertEqual(
            url("flussonic", "http://fs.test/news"), f"http://fs.test/news/timeshift_abs-{start}.ts"
        )
        self.assertEqual(
            url("xc", "http://p.test:81/live/u/p/7.ts"),
            "http://p.test:81/timeshift/u/p/30/2026-01-20:14-05/7.ts",
        )
        self.assertIsNone(url("xc", "http://x.test/news.ts"))

    def test_archive_urls_dispatches_and_keeps_web_addresses_only(self):
        xtream = archive_urls(
            Archive(3, XTREAM), XtreamAddressTests.CMD, WINTER, 60, NOW, zone=timezone.utc
        )
        self.assertEqual(len(xtream), 2)
        self.assertEqual(
            archive_urls(Archive(1, "default", "file:///etc/{utc}"), "http://x/1", WINTER, 60, NOW),
            [],
        )


def _ts_head() -> bytes:
    return LIVE_TS.read_bytes()[: 4 * PROBE_BYTES]


class JudgeTests(unittest.TestCase):
    def test_what_each_answer_means(self):
        ts = _ts_head()
        self.assertIs(judge(200, "video/mp2t", ts), Answer.PLAYS)
        self.assertIs(judge(206, "application/octet-stream", ts[5:]), Answer.PLAYS)  # mid-packet
        self.assertIs(
            judge(200, "application/x-mpegurl", b"#EXTM3U\n#EXTINF:4,\na.ts\n"), Answer.PLAYS
        )
        self.assertIs(
            judge(200, "text/html; charset=utf-8", b"<html>Not found</html>"), Answer.NONE
        )
        self.assertIs(judge(200, "application/json", b'{"error": "no archive"}'), Answer.NONE)
        self.assertIs(judge(200, "video/mp2t", b""), Answer.NONE)
        self.assertIs(judge(200, "video/mp2t", b"\x00" * 100), Answer.PLAYS)  # a video type
        self.assertIs(judge(200, "", b"\x01\x02" * 50), Answer.UNKNOWN)
        for status in (404, 410, 400):
            self.assertIs(judge(status, "video/mp2t", ts), Answer.NONE)
        for status in (401, 403, 407, 429, 458):
            self.assertIs(judge(status, "text/html", b""), Answer.REFUSED)
        for status in (500, 502, 302):
            self.assertIs(judge(status, "video/mp2t", ts), Answer.UNKNOWN)


class ProbeTests(unittest.TestCase):
    """The probe against a fake provider on 127.0.0.1: it reads the first bytes and closes."""

    def setUp(self):
        ts = _ts_head()

        def ranged(handler, _count):
            if handler.headers.get("Range"):
                return 416, {}, b""
            return 200, {"Content-Type": "video/mp2t"}, ts

        self.source = LocalSource(
            {
                "/archive.ts": (206, {"Content-Type": "video/mp2t"}, ts[:PROBE_BYTES]),
                "/archive.m3u8": (200, {"Content-Type": "application/x-mpegurl"}, b"#EXTM3U\n"),
                "/page": (200, {"Content-Type": "text/html"}, b"<html>No archive</html>"),
                "/limit": (403, {"Content-Type": "text/html"}, b"Forbidden"),
                "/norange.ts": ranged,
            }
        )
        self.addCleanup(self.source.close)

    def url(self, path):
        return f"{self.source.url}{path}"

    def test_answers_from_a_fake_provider(self):
        self.assertIs(probe(self.url("/archive.ts")), Answer.PLAYS)
        self.assertIs(probe(self.url("/archive.m3u8")), Answer.PLAYS)
        self.assertIs(probe(self.url("/missing.ts")), Answer.NONE)  # 404
        self.assertIs(probe(self.url("/page")), Answer.NONE)
        self.assertIs(probe(self.url("/limit")), Answer.REFUSED)

    def test_it_asks_for_the_first_bytes_with_the_channels_headers(self):
        probe(self.url("/archive.ts"), headers={"User-Agent": "Box/1.0", "Referer": "http://r/"})
        _path, headers = self.source.requests[-1]
        self.assertEqual(headers["User-Agent"], "Box/1.0")
        self.assertEqual(headers["Referer"], "http://r/")
        self.assertEqual(headers["Range"], f"bytes=0-{PROBE_BYTES - 1}")

    def test_a_refused_range_is_asked_again_without_one(self):
        self.assertIs(probe(self.url("/norange.ts")), Answer.PLAYS)
        self.assertEqual([p for p, _ in self.source.requests], ["/norange.ts", "/norange.ts"])

    def test_no_server_proves_nothing(self):
        port = self.source.server.server_port
        self.source.close()
        self.assertIs(probe(f"http://127.0.0.1:{port}/archive.ts", timeout=2), Answer.UNKNOWN)

    def test_the_worker_tries_the_next_form_but_stops_at_a_refusal(self):
        found = []
        worker = ArchiveProbeWorker([self.url("/missing.ts"), self.url("/archive.ts")], {})
        worker.finished.connect(found.append)
        worker.run()
        self.assertEqual(found, [(1, "plays")])
        found.clear()
        self.source.requests.clear()
        worker = ArchiveProbeWorker([self.url("/limit"), self.url("/archive.ts")], {})
        worker.finished.connect(found.append)
        worker.run()
        self.assertEqual(found, [(-1, "refused")])
        self.assertNotIn("/archive.ts", [p for p, _ in self.source.requests])


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.now = 1_000_000.0
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = os.path.join(directory.name, "catchup.json")

    def memory(self):
        memory = CatchupMemory(self.path, now=lambda: self.now)
        self.addCleanup(memory.flush)  # before the directory goes
        return memory

    def test_a_failure_lasts_three_days_and_an_answer_one(self):
        memory = self.memory()
        memory.remember("a", False)
        memory.remember("b", True, form=1)
        self.assertTrue(memory.failed("a"))
        self.assertIsNone(memory.answered("a"))
        self.assertEqual(memory.answered("b"), 1)
        self.assertFalse(memory.failed("b"))
        self.now += ANSWERED_S + 1
        self.assertIsNone(memory.answered("b"))
        self.assertTrue(memory.failed("a"))
        self.now += FAILED_S
        self.assertFalse(memory.failed("a"))
        self.now = 0.0  # the clock was set back: nothing is in force
        self.assertFalse(memory.failed("a"))

    def test_the_file_is_versioned_and_read_back(self):
        memory = self.memory()
        memory.remember("a", False)
        memory.remember("b", True)
        memory.flush()
        with open(self.path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["channels"]["a"], {"answered": False, "used": self.now})
        self.assertEqual(data["channels"]["b"], {"answered": True, "used": self.now})
        again = self.memory()
        self.assertTrue(again.failed("a"))
        self.assertEqual(again.answered("b"), 0)
        data["channels"]["c"] = {"answered": "yes", "used": 1}
        data["channels"]["d"] = {"answered": True, "used": 1, "form": 99}
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        self.assertEqual(sorted(self.memory()._entries), ["a", "b"])  # the bad ones left out

    def test_an_answer_replaces_a_failure(self):
        memory = self.memory()
        memory.remember("a", False)
        memory.remember("a", True)
        self.assertFalse(memory.failed("a"))
        memory.forget("a")
        self.assertIsNone(memory.answered("a"))


if __name__ == "__main__":
    unittest.main()
