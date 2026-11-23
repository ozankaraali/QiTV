"""Plex (2.14): the sign-in's one-shot jobs (a code, a look at it, the account's servers), run
off the GUI thread; each emits plain data, never an address, and a failure as words."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from services import media_server as ms, plex, plex_addresses as pa, plex_workers as pw
from tests.test_plex import fixture


class FakeAccount:
    def __init__(self, pin=None, token="", servers=(), fail=None):
        self.pin, self.token, self._servers, self.fail = pin, token, list(servers), fail
        self.closed = False
        self.asked = []

    def create_pin(self):
        if self.fail:
            raise self.fail
        return self.pin

    def check_pin(self, pin_id):
        self.asked.append(pin_id)
        if self.fail:
            raise self.fail
        return self.token

    def servers(self, token):
        self.asked.append(token)
        if self.fail:
            raise self.fail
        return self._servers

    def close(self):
        self.closed = True


def run(job):
    out = {}
    job.finished.connect(lambda value: out.setdefault("finished", value))
    job.error.connect(lambda text: out.setdefault("error", text))
    job.run()
    return out


HOME = plex.Server(
    "Home Media",
    "machine-home",
    "server-token",
    True,
    (plex.Connection("https://home.example.plex.direct:32400", True),),
)
FRIEND = plex.Server(
    "A Friend's Films",
    "machine-friend",
    "friend-token",
    False,
    (plex.Connection("https://friend.example.plex.direct:32400"),),
)


class PinJobTest(unittest.TestCase):
    def test_a_code(self):
        account = FakeAccount(pin=plex.Pin(7, "AB12", 900))
        out = run(pw.PinJob("client-1", account=lambda client_id: account))
        self.assertEqual(out["finished"], {"id": 7, "code": "AB12", "expires_in": 900})
        self.assertTrue(account.closed)

    def test_plex_tv_unreachable_is_said_without_its_address(self):
        account = FakeAccount(fail=ConnectionError("https://plex.tv/api/v2/pins?x"))
        with self.assertLogs("services.plex_workers", "INFO"):
            out = run(pw.PinJob("client-1", account=lambda client_id: account))
        self.assertEqual(out["error"], "plex.tv could not be reached (ConnectionError).")
        self.assertNotIn("http", out["error"])


class PinCheckJobTest(unittest.TestCase):
    def test_not_yet_then_the_token(self):
        account = FakeAccount(token="")
        out = run(pw.PinCheckJob("client-1", 7, account=lambda client_id: account))
        self.assertEqual(out["finished"], {"token": ""})
        account.token = "account-token"
        out = run(pw.PinCheckJob("client-1", 7, account=lambda client_id: account))
        self.assertEqual(out["finished"], {"token": "account-token"})
        self.assertEqual(account.asked, [7, 7])

    def test_an_expired_code_says_so(self):
        account = FakeAccount(fail=ms.SignInNeeded("404"))
        out = run(pw.PinCheckJob("client-1", 7, account=lambda client_id: account))
        self.assertEqual(out["finished"], {"token": "", "expired": True})


class ServersJobTest(unittest.TestCase):
    def test_the_servers_that_answer_each_by_its_best_connection(self):
        account = FakeAccount(servers=[HOME, FRIEND])
        probed = []

        def probe(uri, server):
            probed.append(server.machine_id)
            return server is HOME

        job = pw.ServersJob(
            "client-1", "account-token", account=lambda client_id: account, probe=probe
        )
        out = run(job)
        self.assertEqual(
            out["finished"],
            {
                "listed": 2,
                "servers": [
                    {
                        "name": "Home Media",
                        "machine_id": "machine-home",
                        "token": "server-token",
                        "owned": True,
                        "url": "https://home.example.plex.direct:32400",
                        "connections": [
                            {
                                "uri": "https://home.example.plex.direct:32400",
                                "local": True,
                                "relay": False,
                            }
                        ],
                    }
                ],
            },
        )
        self.assertEqual(sorted(probed), ["machine-friend", "machine-home"])
        self.assertEqual(account.asked, ["account-token"])

    def test_each_server_comes_with_its_connections_to_look_again_later(self):  # review I2
        account = FakeAccount(servers=[HOME])
        job = pw.ServersJob("c", "t", account=lambda client_id: account, probe=lambda u, s: True)
        out = run(job)
        self.assertEqual(
            out["finished"]["servers"][0]["connections"],
            [{"uri": "https://home.example.plex.direct:32400", "local": True, "relay": False}],
        )

    def test_an_account_token_refused_says_so_for_a_new_code(self):  # review M8
        account = FakeAccount(fail=ms.SignInNeeded("401"))
        job = pw.ServersJob("c", "t", account=lambda client_id: account, probe=None)
        self.assertEqual(run(job), {"finished": {"refused": True}})


PROVIDER = {
    "type": "PLEX",
    "name": "Home Media",
    "url": "https://10-0-0-5.example.plex.direct:32400",
    "token": "server-token",
    "server_id": "machine-home",
    "device_id": "client-1",
}
BASE = PROVIDER["url"]


class FakeServer:
    """A PlexClient answering from the fixtures."""

    def __init__(self, fail=None, media=None, converts=None):
        self.base, self.token, self.client_id = BASE, "server-token", "client-1"
        self.version = "2.14.0"
        self.fail = fail
        self._media = media
        self.converts = converts  # what `convert` answers (None: the session's playlist)
        self.conversions = []
        self.closed = False
        self.asked = []

    def _raise(self):
        if self.fail:
            raise self.fail

    def libraries(self):
        self._raise()
        return [
            {"id": "1", "name": "Films", "type": "movie"},
            {"id": "2", "name": "Series", "type": "show"},
        ]

    def items(self, library_id, kind):
        self.asked.append((library_id, kind))
        name = "movies_all.json" if kind == plex.MOVIE else "shows_all.json"
        return fixture(name)["MediaContainer"]["Metadata"]

    def seasons_and_episodes(self, show_id):
        self._raise()
        self.asked.append(show_id)
        return (
            fixture("show_children.json")["MediaContainer"]["Metadata"],
            fixture("show_allLeaves.json")["MediaContainer"]["Metadata"],
        )

    def media(self, item_id):
        self._raise()
        self.asked.append(item_id)
        if self._media is not None:
            return self._media
        return fixture("movie_metadata.json")["MediaContainer"]["Metadata"][0]["Media"]

    def convert(self, params):
        self._raise()
        self.conversions.append(params)
        if isinstance(self.converts, Exception):
            raise self.converts
        if self.converts is not None:
            return self.converts
        return (
            f"{BASE}{plex.TRANSCODE}/session/{params['session']}/base/index.m3u8"
            f"?X-Plex-Session-Identifier={params['X-Plex-Session-Identifier']}"
        )

    def close(self):
        self.closed = True


def served(server):
    return lambda provider, verify: server


class SectionWorkerTest(unittest.TestCase):
    def test_films_from_the_film_libraries_series_from_the_series_ones(self):
        server = FakeServer()
        out = run(pw.PlexSectionWorker(PROVIDER, "vod", True, served(server)))
        payload = out["finished"]
        self.assertEqual([c["title"] for c in payload["categories"]][-1], "Films")
        self.assertEqual(len(payload["contents"]), 3)
        self.assertNotIn("delta", payload)  # whole, every time
        self.assertEqual(server.asked, [("1", plex.MOVIE)])
        self.assertTrue(server.closed)
        server = FakeServer()
        out = run(pw.PlexSectionWorker(PROVIDER, "series", True, served(server), saved={}))
        self.assertEqual([i["name"] for i in out["finished"]["contents"]], ["Quiet Street"])
        self.assertEqual(server.asked, [("2", plex.SHOW)])

    def test_a_refused_token_asks_to_sign_in_again(self):
        server = FakeServer(fail=ms.SignInNeeded("401"))
        with self.assertLogs("services.plex_workers", "WARNING"):
            out = run(pw.PlexSectionWorker(PROVIDER, "vod", True, served(server)))
        self.assertEqual(out["error"], ms.SIGN_IN_AGAIN)

    def test_a_failure_says_why_never_the_address_or_the_token(self):
        error = ConnectionError(BASE + "/library/sections?X-Plex-Token=server-token")
        with self.assertLogs("services.plex_workers", "WARNING") as logs:
            out = run(pw.PlexSectionWorker(PROVIDER, "vod", True, served(FakeServer(error))))
        self.assertEqual(out["error"], "The server could not be reached (ConnectionError).")
        for text in (out["error"], *logs.output):
            self.assertNotIn("server-token", text)
            self.assertNotIn("plex.direct", text)


class SeriesWorkerTest(unittest.TestCase):
    def test_a_series_seasons_and_episodes(self):
        server = FakeServer()
        out = run(pw.PlexSeriesWorker(PROVIDER, "201", True, served(server)))
        seasons = out["finished"]["seasons"]
        self.assertEqual([s["name"] for s in seasons], ["Season 1", "Season 2"])
        self.assertEqual(server.asked, ["201"])


class LinkWorkerTest(unittest.TestCase):
    def link(self, server=None, **choices):
        choices = {"screen_4k": False, "four_k": ms.FOUR_K_SCREEN, **choices}
        return run(
            pw.PlexLinkWorker(
                PROVIDER, "102", True, client=served(server or FakeServer()), **choices
            )
        )

    def test_the_settings_pick_as_its_file_is(self):
        server = FakeServer()
        answer = self.link(server)["finished"]
        self.assertTrue(answer["link"].startswith(BASE + "/library/parts/902/"))
        self.assertEqual(answer["quality"], ms.Quality(source="902"))
        self.assertEqual([v["id"] for v in answer["versions"]], ["2", "902"])
        self.assertNotIn("conversions", answer)  # 2.14: the ⚙ offers the server's conversions
        self.assertEqual(server.conversions, [])
        on_4k = self.link(screen_4k=True)["finished"]
        self.assertTrue(on_4k["link"].startswith(BASE + "/library/parts/2/"))

    def test_the_menus_version_as_its_file_is(self):
        answer = self.link(quality=ms.Quality(source="2"))["finished"]
        self.assertTrue(answer["link"].startswith(BASE + "/library/parts/2/"))
        self.assertEqual(answer["quality"], ms.Quality(source="2"))
        self.assertNotIn("file", answer)  # its link is the file

    def test_a_conversion_is_the_servers_of_the_version_it_converts(self):  # 2.14
        server = FakeServer()
        answer = self.link(server, quality=ms.Quality(height=720, bitrate=4_000_000))["finished"]
        (params,) = server.conversions
        self.assertEqual(
            (params["path"], params["mediaIndex"], params["videoResolution"]),
            ("/library/metadata/102", 1, "1280x720"),  # the settings' pick: 1080p, second
        )
        self.assertEqual((params["maxVideoBitrate"], params["X-Plex-Version"]), (4000, "2.14.0"))
        self.assertEqual(params["X-Plex-Client-Identifier"], "client-1")
        self.assertTrue(answer["link"].startswith(BASE + plex.TRANSCODE + "/session/"))
        self.assertEqual(answer["quality"], ms.Quality("902", 720, 4_000_000))  # of 1080p
        self.assertNotIn("refused", answer)
        # The version's own file, for the history (review M5: never the session's address).
        self.assertTrue(answer["file"].startswith(BASE + "/library/parts/902/"))
        self.assertEqual([v["id"] for v in answer["versions"]], ["2", "902"])
        server = FakeServer()  # the 4K version chosen before: that one converted
        self.link(server, quality=ms.Quality("2", 1080, 8_000_000), screen_4k=True)
        self.assertEqual(server.conversions[0]["mediaIndex"], 0)

    def test_a_version_already_as_small_plays_as_its_file_is(self):
        small = [
            {
                "id": 7,
                "bitrate": 1500,
                "width": 1280,
                "height": 720,
                "Part": [{"key": "/library/parts/7/1/file.mp4", "container": "mp4"}],
            }
        ]
        server = FakeServer(media=small)
        answer = self.link(server, quality=ms.Quality(height=720, bitrate=4_000_000))["finished"]
        self.assertEqual(server.conversions, [])  # the server's processor for nothing
        self.assertNotIn("refused", self.link(server, quality=ms.CONVERSIONS[1])["finished"])
        self.assertTrue(answer["link"].startswith(BASE + "/library/parts/7/"))
        self.assertEqual(answer["quality"], ms.Quality(source="7"))

    def test_a_conversion_the_server_refuses_plays_the_file_as_it_is(self):
        converted = ms.Quality(height=720, bitrate=4_000_000)
        answer = self.link(FakeServer(converts=""), quality=converted)["finished"]
        self.assertTrue(answer["link"].startswith(BASE + "/library/parts/902/"))
        self.assertEqual(answer["quality"], ms.Quality(source="902"))  # the ⚙ ticks the file
        self.assertIs(answer["refused"], True)  # the TV says so
        refused = ms.MediaServerError("The server answered 400")
        with self.assertLogs("services.plex_workers", "INFO") as logs:
            answer = self.link(FakeServer(converts=refused), quality=converted)["finished"]
        self.assertEqual(answer["quality"], ms.Quality(source="902"))
        self.assertIs(answer["refused"], True)
        self.assertIn("The server answered 400", logs.output[0])
        for text in logs.output:  # never the address or the token
            self.assertNotIn("server-token", text)
            self.assertNotIn("plex.direct", text)

    def test_a_conversion_that_fails_on_its_way_plays_the_file(self):  # review N1
        # A connection lost or a time-out while the server is asked to convert never escapes
        # the worker as such: the file plays as it is, and the TV says the server didn't.
        for error in (ConnectionError("x"), TimeoutError("y"), ValueError("not JSON")):
            with self.subTest(error=type(error).__name__):
                server = FakeServer(converts=error)
                with self.assertLogs("services.plex_workers", "INFO") as logs:
                    out = self.link(server, quality=ms.Quality(height=720, bitrate=4_000_000))
                answer = out["finished"]
                self.assertTrue(answer["link"].startswith(BASE + "/library/parts/902/"))
                self.assertIs(answer["refused"], True)
                self.assertNotIn("server-token", "".join(logs.output))

    def test_a_refused_token_while_converting_asks_to_sign_in_again(self):
        server = FakeServer(converts=ms.SignInNeeded("401"))
        with self.assertLogs("services.plex_workers", "WARNING"):
            out = self.link(server, quality=ms.Quality(height=720, bitrate=4_000_000))
        self.assertEqual(out["error"], ms.SIGN_IN_AGAIN)

    def test_only_4k_with_never_or_no_file_says_so(self):
        meta = fixture("movie_metadata.json")["MediaContainer"]["Metadata"][0]["Media"][:1]
        out = self.link(FakeServer(media=meta), four_k=ms.FOUR_K_NEVER)
        self.assertEqual(out["error"], ms.ONLY_4K_TEXT)
        out = self.link(FakeServer(media=[]))
        self.assertEqual(out["error"], "This title has no file to play.")


class CheckAccessTest(unittest.TestCase):
    def test_a_server_answering_its_token(self):
        server = FakeServer()
        self.assertEqual(pw.check_access(PROVIDER, True, served(server)), (True, ""))
        self.assertTrue(server.closed)
        refused = FakeServer(fail=ms.SignInNeeded("401"))
        self.assertEqual(
            pw.check_access(PROVIDER, True, served(refused)), (False, ms.SIGN_IN_AGAIN)
        )
        with self.assertLogs("services.plex_workers", "INFO"):
            ok, why = pw.check_access(PROVIDER, True, served(FakeServer(fail=OSError("x"))))
        self.assertEqual((ok, why), (False, "The server could not be reached (OSError)."))

    def test_the_client_is_the_providers_own(self):
        made = pw.client_for(PROVIDER, False)
        self.addCleanup(made.close)
        self.assertEqual(
            (made.base, made.token, made.client_id, made.verify),
            (BASE, "server-token", "client-1", True),  # a plex.direct one: always checked
        )
        other = pw.client_for({**PROVIDER, "url": "https://media.example.test:32400"}, False)
        self.addCleanup(other.close)
        self.assertFalse(other.verify)


AWAY = "https://203-0-113-7.example.plex.direct:32400"
RELAY = "https://relay.example.plex.direct:8443"
CONNECTED = {
    **PROVIDER,
    "connections": [
        {"uri": BASE, "local": True, "relay": False},
        {"uri": AWAY, "local": False, "relay": False},
        {"uri": RELAY, "local": False, "relay": True},
    ],
}


class Unreachable(FakeServer):
    def __init__(self, base):
        super().__init__(fail=ConnectionError("no route"))
        self.base = base


class LookingAgainTest(unittest.TestCase):
    """Review I2: a server not reached where it was is looked for among its connections
    again, and asked where it answers; a list's load looks for a better one when it is not
    at its best (back home: at home again, not through the relay)."""

    def setUp(self):
        pa.forget()
        self.addCleanup(pa.forget)
        pa.known([CONNECTED])
        self.servers = {BASE: Unreachable(BASE), AWAY: FakeServer(), RELAY: FakeServer()}
        self.servers[AWAY].base = AWAY
        self.servers[RELAY].base = RELAY
        self.found = []

    def client(self, provider, verify):
        return self.servers[pa.base_of(provider)]

    def finder(self, answer):
        def find(provider):
            self.found.append(provider.get("server_id"))
            return answer

        return find

    def test_a_title_not_reached_at_home_plays_from_where_the_server_answers(self):
        worker = pw.PlexLinkWorker(
            CONNECTED,
            "102",
            True,
            screen_4k=False,
            four_k=ms.FOUR_K_SCREEN,
            client=self.client,
            finder=self.finder(AWAY),
        )
        out = run(worker)
        self.assertTrue(out["finished"]["link"].startswith(AWAY + "/library/parts/"))
        self.assertEqual(self.found, ["machine-home"])
        self.assertEqual(pa.base_of(CONNECTED), AWAY)  # asked there from now on
        self.assertTrue(self.servers[BASE].closed)

    def test_found_nowhere_says_the_server_could_not_be_reached(self):
        worker = pw.PlexSeriesWorker(
            CONNECTED, "201", True, client=self.client, finder=self.finder("")
        )
        with self.assertLogs("services.plex_workers", "WARNING"):
            out = run(worker)
        self.assertEqual(out["error"], "The server could not be reached (ConnectionError).")
        self.assertEqual(pa.base_of(CONNECTED), BASE)

    def test_a_list_loaded_away_from_its_best_looks_for_it_first(self):
        pa.moved("machine-home", RELAY)  # signed in away, Remote Access off
        self.servers[BASE] = FakeServer()  # home again
        worker = pw.PlexSectionWorker(CONNECTED, "vod", True, self.client, finder=self.finder(BASE))
        out = run(worker)
        self.assertEqual(len(out["finished"]["contents"]), 3)
        self.assertEqual(pa.base_of(CONNECTED), BASE)
        self.assertEqual(self.found, ["machine-home"])
        self.found.clear()
        run(pw.PlexSectionWorker(CONNECTED, "vod", True, self.client, finder=self.finder(BASE)))
        self.assertEqual(self.found, [])  # at its best: not looked for again

    def test_the_connection_check_looks_again_too(self):
        ok = pw.check_access(CONNECTED, True, self.client, finder=self.finder(AWAY))
        self.assertEqual(ok, (True, ""))
        self.assertEqual(pa.base_of(CONNECTED), AWAY)

    def test_the_looking_asks_each_connection_without_the_token_in_order(self):
        asked = []

        def answers(uri, server, client_id, version, session=None):
            asked.append((uri, server.machine_id, server.access_token))
            return uri != BASE

        from unittest import mock

        with mock.patch.object(plex, "answers", side_effect=answers):
            self.assertEqual(pw.refind(CONNECTED), AWAY)
        self.assertEqual({a[0] for a in asked}, {BASE, AWAY, RELAY})
        self.assertEqual(pw.refind(PROVIDER), "")  # none kept: nowhere else to look


if __name__ == "__main__":
    unittest.main()
