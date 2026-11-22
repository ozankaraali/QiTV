"""Plex (2.14): the sign-in's one-shot jobs (a code, a look at it, the account's servers), run
off the GUI thread; each emits plain data, never an address, and a failure as words."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from services import media_server as ms, plex, plex_workers as pw


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
            "client-1", "account-token", True, account=lambda client_id: account, probe=probe
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
        job = pw.ServersJob("c", "t", True, account=lambda client_id: account, probe=None)
        with self.assertLogs("services.plex_workers", "INFO"):
            out = run(job)
        self.assertEqual(out["error"], "plex.tv refused the sign-in. Try again with a new code.")


if __name__ == "__main__":
    unittest.main()
