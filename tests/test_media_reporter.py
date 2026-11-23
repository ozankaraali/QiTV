"""Telling a Jellyfin or Emby server where its title plays (2.8): start, progress every 10 s,
stopped when something else plays or nothing does; never for another provider's stream. A Plex
server too (2.14), whose conversion of a title is stopped there with its play."""

import threading
import time
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

    def test_a_plex_server_found_at_another_of_its_addresses_is_still_told(self):  # N1
        """Signed in away on the relay, the server is found at home later: its streams come
        from its home address, and their progress is still told."""
        home = "https://10-0-0-5.example.plex.direct:32400"
        relay = "https://relay.example.plex.direct:8443"
        server = {
            "type": "PLEX",
            "url": relay,
            "token": "P",
            "device_id": "C",
            "server_id": "machine-home",
            "connections": [
                {"uri": home, "local": True, "relay": False},
                {"uri": relay, "local": False, "relay": True},
            ],
        }
        sent = []
        reporter = MediaReporter(
            lambda: [SERVER, server],
            send=lambda server, event, ref, position, paused: sent.append(
                (server["server_id"], event, ref.base, position)
            ),
            clock=self.clock,
        )
        source = {"Id": "9", "part": "/library/parts/9/17/file.mkv"}
        reporter.playing(plex.stream_url(home, source, "P", "C", "102"))
        self.clock.now += PROGRESS_S
        reporter.position(30_000)
        reporter.stopped()
        self.assertEqual(
            sent,
            [
                ("machine-home", "start", home, 0),
                ("machine-home", "progress", home, 30_000),
                ("machine-home", "stopped", home, 30_000),
            ],
        )

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
            {"token": "P", "client_id": "C", "verify": True},  # plex.direct: always checked
        )
        self.assertEqual(args, ("stopped", ref, 3, False))
        self.assertEqual(closed, "closed")

    PLEX_BASE = "https://10-0-0-5.example.plex.direct:32400"
    PLEX = {"type": "PLEX", "url": PLEX_BASE, "token": "P", "device_id": "C"}
    CONVERTED = (
        PLEX_BASE + "/video/:/transcode/universal/session/s1/base/index.m3u8"
        "?X-Plex-Token=P&X-Plex-Client-Identifier=C&X-Plex-Product=QiTV"
        "&X-Plex-Session-Identifier=102-s1"
    )

    def test_a_plex_conversion_is_told_as_a_play_of_its_own(self):  # 2.14
        sent = []
        reporter = MediaReporter(
            lambda: [SERVER, self.PLEX],
            send=lambda server, event, ref, position, paused: sent.append(
                (event, ref.item_id, ref.converted, ref.transcode)
            ),
            clock=self.clock,
        )
        reporter.playing(self.CONVERTED)
        reporter.playing(self.CONVERTED)  # opened again (a cast begins): the same play
        source = {"Id": "9", "part": "/library/parts/9/17/file.mkv"}
        reporter.playing(plex.stream_url(self.PLEX_BASE, source, "P", "C", "102"))  # as it is
        self.assertEqual(
            sent,
            [
                ("start", "102", True, "s1"),
                ("stopped", "102", True, "s1"),  # the conversion's play stops (and with it,
                ("start", "102", False, ""),  # its session on the server: _send_over_http)
            ],
        )

    def test_the_play_the_server_heard_start_is_known_by_its_address(self):  # 2.14
        self.assertEqual(self.reporter.active_url(), "")
        self.reporter.playing(url())
        self.assertEqual(self.reporter.active_url(), url())
        self.reporter.playing("http://elsewhere.test/live.ts")  # another provider's: it stops
        self.assertEqual(self.reporter.active_url(), "")

    def test_a_plex_conversion_ends_on_the_server_with_its_play(self):  # 2.14
        ref = plex.parse_stream_url(self.CONVERTED, [self.PLEX_BASE])
        made = []

        class Client:
            def __init__(self, base, **kwargs):
                made.append(("made", kwargs["token"]))

            def report(self, event, *args):
                made.append(("report", event))

            def stop_conversion(self, session):
                made.append(("stop", session))

            def close(self):
                made.append("closed")

        class Untold(Client):  # the timeline did not take it: the session ends all the same
            def report(self, event, *args):
                raise OSError("unreachable")

        with mock.patch.object(plex, "PlexClient", Client):
            for event in ("start", "progress"):
                media_reporter._send_over_http(self.PLEX, event, ref, 3, False)
            self.assertNotIn(("stop", "s1"), made)
            made.clear()
            media_reporter._send_over_http(self.PLEX, "stopped", ref, 3, False)
            self.assertEqual(made, [("made", "P"), ("report", "stopped"), ("stop", "s1"), "closed"])
            made.clear()
            source = {"Id": "9", "part": "/library/parts/9/1/f.mkv"}
            file = plex.parse_stream_url(
                plex.stream_url(self.PLEX_BASE, source, "P", "C", "7"), [self.PLEX_BASE]
            )
            media_reporter._send_over_http(self.PLEX, "stopped", file, 3, False)
            self.assertEqual(made, [("made", "P"), ("report", "stopped"), "closed"])  # no session
        made.clear()
        with mock.patch.object(plex, "PlexClient", Untold):
            with self.assertRaises(OSError):
                media_reporter._send_over_http(self.PLEX, "stopped", ref, 3, False)
        self.assertEqual(made, [("made", "P"), ("stop", "s1"), "closed"])

    def test_a_conversion_never_played_has_its_session_stopped(self):  # 2.14 review I2
        sent = []
        reporter = MediaReporter(
            lambda: [SERVER, self.PLEX],
            send=lambda server, event, ref, position, paused: sent.append((event, ref.transcode)),
            clock=self.clock,
        )
        reporter.abandoned(self.CONVERTED)
        self.assertEqual(sent, [("abandoned", "s1")])  # never told started: only its session
        sent.clear()
        reporter.playing(self.CONVERTED)
        reporter.abandoned(self.CONVERTED)  # (the one playing: never)
        source = {"Id": "9", "part": "/library/parts/9/17/file.mkv"}
        reporter.abandoned(plex.stream_url(self.PLEX_BASE, source, "P", "C", "102"))  # a file
        reporter.abandoned(url())  # Jellyfin's: its server ends its own
        reporter.abandoned("http://elsewhere.test/live.ts")
        self.assertEqual(sent, [("start", "s1")])

    def test_an_abandoned_conversion_is_only_stopped_on_the_server(self):  # 2.14 review I2
        ref = plex.parse_stream_url(self.CONVERTED, [self.PLEX_BASE])
        made = []

        class Client:
            def __init__(self, base, **kwargs):
                pass

            def report(self, event, *args):
                made.append(("report", event))

            def stop_conversion(self, session):
                made.append(("stop", session))

            def close(self):
                made.append("closed")

        with mock.patch.object(plex, "PlexClient", Client):
            media_reporter._send_over_http(self.PLEX, "abandoned", ref, 0, False)
        self.assertEqual(made, [("stop", "s1"), "closed"])

    def test_qitv_closing_waits_a_moment_for_the_stop_to_go_out(self):  # 2.14 review M3
        sent = []

        def slow(server, event, ref, position, paused):
            time.sleep(0.2)
            sent.append(event)

        reporter = MediaReporter(lambda: [self.PLEX])
        with mock.patch.object(media_reporter, "_send_over_http", slow):
            reporter.playing(self.CONVERTED)
            reporter.shutdown()
            self.assertEqual(sent, ["start", "stopped"])  # out before QiTV goes
        release = threading.Event()  # a server that doesn't answer: QiTV closes all the same

        def stuck(*args):
            release.wait(5)

        late = MediaReporter(lambda: [self.PLEX])
        with mock.patch.object(media_reporter, "_send_over_http", stuck):
            late.playing(self.CONVERTED)
            began = time.monotonic()
            late.shutdown()
            waited = time.monotonic() - began
            release.set()
            late._thread.join(5)
        self.assertLess(waited, media_reporter.CLOSE_WAIT_S + 0.5)

    def test_a_failed_stop_is_one_log_line_without_the_address_or_the_token(self):  # 2.14
        reporter = MediaReporter(lambda: [self.PLEX], clock=self.clock)

        def fails(*args):
            raise ms.MediaServerError("The server answered 500")

        with mock.patch.object(media_reporter, "_send_over_http", fails):
            with self.assertLogs("services.media_reporter", "INFO") as logs:
                reporter.playing(self.CONVERTED)
                reporter.shutdown()
                reporter._thread.join(5)
        self.assertEqual(len(logs.output), 2)  # the start's, the stop's
        for text in logs.output:
            self.assertNotIn("X-Plex-Token", text)
            self.assertNotIn("plex.direct", text)

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
