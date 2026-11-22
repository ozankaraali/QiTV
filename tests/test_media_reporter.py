"""Telling a Jellyfin or Emby server where its title plays (2.8): start, progress every 10 s,
stopped when something else plays or nothing does; never for another provider's stream."""

import unittest
from unittest import mock

from services import media_reporter, media_server as ms, plex
from services.media_reporter import PROGRESS_S, MediaReporter

BASE = "http://server.test:8096"
SERVER = {"type": "JELLYFIN", "url": BASE, "user_id": "U", "device_id": "D", "token": "T"}


def url(item="f1"):
    return ms.stream_url(BASE, item, {"Id": "ms1", "Container": "mkv"}, "T", "ps1")


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class MediaReporterTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.clock = FakeClock()
        self.reporter = MediaReporter(
            lambda: [SERVER],
            send=lambda server, event, ref, position, paused: self.sent.append(
                (event, ref.item_id, position, paused)
            ),
            clock=self.clock,
        )

    def test_a_plex_servers_title_is_told_too(self):  # 2.14
        plex_base = "https://10-0-0-5.example.plex.direct:32400"
        server = {"type": "PLEX", "url": plex_base, "token": "P", "device_id": "C"}
        source = {"Id": "9", "part": "/library/parts/9/17/file.mkv"}
        sent = []
        reporter = MediaReporter(
            lambda: [SERVER, server],
            send=lambda server, event, ref, position, paused: sent.append(
                (server["type"], event, ref.item_id, ref.source_id, position)
            ),
            clock=self.clock,
        )
        reporter.playing(plex.stream_url(plex_base, source, "P", "C", "102"))
        reporter.position(5_000)
        self.clock.now += PROGRESS_S
        reporter.position(12_000)
        reporter.stopped()
        self.assertEqual(
            sent,
            [
                ("PLEX", "start", "102", "9", 0),
                ("PLEX", "progress", "102", "9", 12_000),
                ("PLEX", "stopped", "102", "9", 12_000),
            ],
        )
        # A Plex address QiTV did not make (no play session of its own) is never told.
        sent.clear()
        reporter.playing(plex_base + "/library/parts/9/17/file.mkv?X-Plex-Token=P")
        self.assertEqual(sent, [])

    def test_a_plex_report_goes_through_plexs_client(self):  # 2.14
        plex_base = "https://10-0-0-5.example.plex.direct:32400"
        server = {"type": "PLEX", "url": plex_base, "token": "P", "device_id": "C"}
        url = plex.stream_url(
            plex_base, {"Id": "9", "part": "/library/parts/9/1/f.mkv"}, "P", "C", "7"
        )
        ref = plex.parse_stream_url(url, [plex_base])
        made = []

        class Client:
            def __init__(self, base, **kwargs):
                made.append((base, kwargs))

            def report(self, *args):
                made.append(args)

            def close(self):
                made.append("closed")

        with mock.patch.object(plex, "PlexClient", Client):
            media_reporter._send_over_http(dict(server, ssl_verify=False), "stopped", ref, 3, False)
        (base, kwargs), args, closed = made
        self.assertEqual(base, plex_base)
        self.assertEqual(
            {k: kwargs[k] for k in ("token", "client_id", "verify")},
            {"token": "P", "client_id": "C", "verify": False},
        )
        self.assertEqual(args, ("stopped", ref, 3, False))
        self.assertEqual(closed, "closed")

    def test_a_server_without_its_own_check_follows_the_settings(self):  # 2.8
        servers = []
        reporter = MediaReporter(
            lambda: [dict(SERVER)],
            send=lambda server, *_rest: servers.append(server),
            clock=self.clock,
            verify=lambda: False,  # Settings: no certificate check
        )
        reporter.playing(url())
        self.assertIs(servers[-1]["ssl_verify"], False)
        reporter = MediaReporter(
            lambda: [dict(SERVER, ssl_verify=True)],
            send=lambda server, *_rest: servers.append(server),
            clock=self.clock,
            verify=lambda: False,
        )
        reporter.playing(url())
        self.assertIs(servers[-1]["ssl_verify"], True)  # its own

    def test_a_server_title_says_it_started_and_where_it_is_every_ten_seconds(self):
        self.reporter.playing(url())
        self.reporter.position(1_000)
        self.clock.now += PROGRESS_S - 1
        self.reporter.position(9_000)
        self.clock.now += 1
        self.reporter.position(10_000)
        self.assertEqual(self.sent, [("start", "f1", 0, False), ("progress", "f1", 10_000, False)])

    def test_another_title_stops_the_first_where_it_was(self):
        self.reporter.playing(url("f1"))
        self.reporter.position(42_000)
        self.reporter.playing(url("f2"))
        self.assertEqual(
            self.sent,
            [
                ("start", "f1", 0, False),
                ("stopped", "f1", 42_000, False),
                ("start", "f2", 0, False),
            ],
        )

    def test_another_providers_stream_stops_it_and_is_never_told(self):
        self.reporter.playing(url())
        self.reporter.playing("http://panel.test/movie/u/p/7.mkv")
        self.reporter.position(5_000)
        self.assertEqual([event for event, *_ in self.sent], ["start", "stopped"])

    def test_a_stop_says_so_once(self):
        self.reporter.playing(url())
        self.reporter.position(3_000, paused=True)
        self.reporter.stopped()
        self.reporter.stopped()
        self.assertEqual(self.sent[-1], ("stopped", "f1", 3_000, True))
        self.assertEqual([e for e, *_ in self.sent].count("stopped"), 1)

    def test_the_same_title_opened_again_is_not_a_new_start(self):
        self.reporter.playing(url())
        self.reporter.playing(url())  # reopened (a cast, a seek past the buffer)
        self.assertEqual([e for e, *_ in self.sent], ["start"])

    def test_a_quality_change_stops_the_first_play_and_starts_the_new_one(self):  # 2.8
        sessions = []
        reporter = MediaReporter(
            lambda: [SERVER],
            send=lambda server, event, ref, position, paused: sessions.append(
                (event, ref.play_session, ref.converted, position)
            ),
            clock=self.clock,
        )
        converted = ms.converted_url(BASE, "f1", {"Id": "ms1"}, "T", "ps2", 4_000_000, "D", 720)
        reporter.playing(url())
        self.clock.now += PROGRESS_S
        reporter.position(60_000)
        reporter.playing(converted)  # the ⚙ menu: the same title, converted, a new session
        self.clock.now += PROGRESS_S
        reporter.position(70_000)
        reporter.stopped()
        self.assertEqual(
            sessions,
            [
                ("start", "ps1", False, 0),
                ("progress", "ps1", False, 60_000),
                ("stopped", "ps1", False, 60_000),
                ("start", "ps2", True, 0),
                ("progress", "ps2", True, 70_000),
                ("stopped", "ps2", True, 70_000),
            ],
        )

    def test_a_position_of_another_stream_is_not_this_ones(self):  # 2.8
        # A title swapped small or on a phone reports with its link: once the large picture
        # plays another title, its reports are not the new one's.
        self.reporter.playing(url("f1"))
        self.reporter.playing(url("f2"))
        self.clock.now += PROGRESS_S
        self.reporter.position(5_000, url=url("f1"))
        self.reporter.position(7_000, url=url("f2"))
        self.assertEqual(self.sent[-1], ("progress", "f2", 7_000, False))
        self.assertNotIn(("progress", "f2", 5_000, False), self.sent)


if __name__ == "__main__":
    unittest.main()
