"""Plex (2.14): the sign-in's one-shot jobs (a code, a look at it, the account's servers), run
off the GUI thread; each emits plain data, never an address, and a failure as words."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from services import media_server as ms, plex, plex_workers as pw
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
                    }
                ],
            },
        )
        self.assertEqual(sorted(probed), ["machine-friend", "machine-home"])
        self.assertEqual(account.asked, ["account-token"])

    def test_an_account_token_refused_asks_for_a_new_code(self):
        account = FakeAccount(fail=ms.SignInNeeded("401"))
        job = pw.ServersJob("c", "t", account=lambda client_id: account, probe=None)
        with self.assertLogs("services.plex_workers", "INFO"):
            out = run(job)
        self.assertEqual(out["error"], "plex.tv refused the sign-in. Try again with a new code.")


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

    def __init__(self, fail=None, media=None):
        self.base, self.token, self.client_id = BASE, "server-token", "client-1"
        self.fail = fail
        self._media = media
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

    def test_the_settings_pick_never_a_conversion(self):
        answer = self.link()["finished"]
        self.assertTrue(answer["link"].startswith(BASE + "/library/parts/902/"))
        self.assertEqual(answer["quality"], ms.Quality(source="902"))
        self.assertEqual([v["id"] for v in answer["versions"]], ["2", "902"])
        self.assertIs(answer["conversions"], False)
        on_4k = self.link(screen_4k=True)["finished"]
        self.assertTrue(on_4k["link"].startswith(BASE + "/library/parts/2/"))

    def test_the_menus_version_and_no_conversion_even_when_one_is_asked(self):
        answer = self.link(quality=ms.Quality(source="2"))["finished"]
        self.assertTrue(answer["link"].startswith(BASE + "/library/parts/2/"))
        carried = ms.Quality(height=720, bitrate=4_000_000)  # a Jellyfin title's, carried on
        answer = self.link(quality=carried)["finished"]
        self.assertTrue(answer["link"].startswith(BASE + "/library/parts/902/"))
        self.assertNotIn("transcode", answer["link"])
        self.assertEqual(answer["quality"], ms.Quality(source="902"))

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


if __name__ == "__main__":
    unittest.main()
