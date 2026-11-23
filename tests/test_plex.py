"""Plex (2.14): the PIN sign-in at plex.tv, the account's servers and their connections."""

import json
from pathlib import Path
import unittest
from urllib.parse import parse_qsl

from services import media_server as ms, plex

FIXTURES = Path(__file__).parent / "fixtures" / "plex"


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class _Response:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body
        if isinstance(body, str):  # a playlist (Plex's conversion), not JSON
            self.content = body.encode()
        else:
            self.content = b"" if body is None else json.dumps(body).encode()
        self.text = self.content.decode()
        self.headers = headers or {}
        self.closed = False

    def json(self):
        return self._body

    def close(self):
        self.closed = True


class FakeHttp:
    """requests.Session as the clients use it: answers by (method, path), records the asks."""

    def __init__(self, base, answers):
        self.base = base
        self.answers = answers  # (method, path) -> (status, body[, headers]) or an error
        self.asked = []
        self.responses = []
        self.headers = {}
        self.closed = False

    def request(self, method, url, params=None, headers=None, timeout=None, verify=True, **kw):
        path, _, query = url[len(self.base) :].partition("?")  # an address's own query apart
        self.asked.append(
            {
                "method": method,
                "path": path,
                "params": dict(params or {}),
                "query": dict(parse_qsl(query)),
                "headers": headers,
                "verify": verify,
                "stream": bool(kw.get("stream")),
            }
        )
        answer = self.answers.get((method, path), (404, None))
        if isinstance(answer, BaseException):  # (a request that fails: a time-out, say)
            raise answer
        response = _Response(*answer)
        self.responses.append(response)
        return response

    def close(self):
        self.closed = True


def account(answers):
    http = FakeHttp(plex.PLEX_TV, answers)
    return plex.PlexAccount("client-1", "2.14.0", session=http), http


class ClientHeadersTest(unittest.TestCase):
    def test_qitv_says_only_its_name_version_and_a_random_id(self):
        headers = plex.client_headers("client-1", "2.14.0")
        self.assertEqual(
            headers,
            {
                "Accept": "application/json",
                "X-Plex-Product": "QiTV",
                "X-Plex-Version": "2.14.0",
                "X-Plex-Client-Identifier": "client-1",
            },
        )
        self.assertEqual(plex.client_headers("c", "v", "tok")["X-Plex-Token"], "tok")

    def test_a_new_client_id_is_random(self):
        self.assertNotEqual(plex.new_client_id(), plex.new_client_id())
        self.assertEqual(len(plex.new_client_id()), 32)


class PinTest(unittest.TestCase):
    def test_a_code_is_asked_for_without_strong(self):
        answer = {"id": 1234567, "code": "AB12", "expiresIn": 900, "authToken": None}
        client, http = account({("POST", "/api/v2/pins"): (201, answer)})
        pin = client.create_pin()
        self.assertEqual(pin, plex.Pin(1234567, "AB12", 900))
        asked = http.asked[0]
        self.assertNotIn("strong", asked["params"])  # the 4-character code, for plex.tv/link
        self.assertEqual(asked["headers"]["X-Plex-Client-Identifier"], "client-1")
        self.assertNotIn("X-Plex-Token", asked["headers"])

    def test_a_code_without_its_parts_is_an_error(self):
        client, _ = account({("POST", "/api/v2/pins"): (201, {"id": "x"})})
        with self.assertRaises(ms.MediaServerError):
            client.create_pin()

    def test_a_code_not_entered_yet_gives_no_token_then_the_token(self):
        path = ("GET", "/api/v2/pins/1234567")
        client, http = account({path: (200, {"id": 1234567, "authToken": None})})
        self.assertEqual(client.check_pin(1234567), "")
        http.answers[path] = (200, {"id": 1234567, "authToken": "account-token"})
        self.assertEqual(client.check_pin(1234567), "account-token")

    def test_a_code_gone_asks_to_sign_in_again(self):
        client, _ = account({})  # 404: expired
        with self.assertRaises(ms.SignInNeeded):
            client.check_pin(1)

    def test_a_failure_names_its_status_never_the_address(self):
        client, _ = account({("POST", "/api/v2/pins"): (500, {"error": "x"})})
        with self.assertRaises(ms.MediaServerError) as caught:
            client.create_pin()
        self.assertEqual(str(caught.exception), "plex.tv answered 500")


class ServersTest(unittest.TestCase):
    def test_the_accounts_servers_with_their_tokens_and_connections_best_first(self):
        resources = fixture("resources.json")
        client, http = account({("GET", "/api/v2/resources"): (200, resources)})
        servers = client.servers("account-token")
        self.assertEqual([s.name for s in servers], ["Home Media", "A Friend's Films"])
        self.assertEqual(http.asked[0]["headers"]["X-Plex-Token"], "account-token")
        self.assertEqual(http.asked[0]["params"]["includeRelay"], 1)
        home = servers[0]
        self.assertEqual(home.machine_id, "machine-home")
        self.assertEqual(home.access_token, "server-token-home")
        self.assertTrue(home.owned)
        self.assertEqual(
            [c.uri for c in home.connections],
            [
                "https://10-0-0-5.example.plex.direct:32400",
                "http://10.0.0.5:32400",
                "https://203-0-113-7.example.plex.direct:32400",
                "https://relay.example.plex.direct:8443",
            ],
        )
        self.assertEqual(
            [(c.local, c.relay) for c in home.connections],
            [(True, False), (True, False), (False, False), (False, True)],
        )
        self.assertFalse(servers[1].owned)  # shared with the account

    def test_players_and_devices_are_not_servers(self):
        resources = [
            {"name": "A Phone", "provides": "client,player", "clientIdentifier": "p1"},
            {"name": "No id", "provides": "server"},
        ]
        self.assertEqual(plex.servers_of(resources), [])
        self.assertEqual(plex.servers_of({"not": "a list"}), [])

    def test_a_server_without_an_access_token_is_left_out(self):  # review M9
        resources = fixture("resources.json")
        del resources[0]["accessToken"]
        self.assertEqual([s.name for s in plex.servers_of(resources)], ["A Friend's Films"])

    def test_a_server_token_is_never_in_its_repr(self):
        server = plex.Server("Home", "m", "secret-token")
        self.assertNotIn("secret-token", repr(server))

    def test_order_is_home_remote_relay_https_first(self):
        c = plex.Connection
        mixed = [
            c("https://relay", relay=True),
            c("http://remote"),
            c("https://remote"),
            c("http://home", local=True),
            c("https://home", local=True),
        ]
        self.assertEqual(
            [x.uri for x in plex.ordered(mixed)],
            ["https://home", "http://home", "https://remote", "http://remote", "https://relay"],
        )


class ReachableTest(unittest.TestCase):
    def servers(self):
        return plex.servers_of(fixture("resources.json"))

    def test_each_server_is_reached_by_its_best_connection_that_answers(self):
        answering = {"http://10.0.0.5:32400", "https://203-0-113-7.example.plex.direct:32400"}
        asked = []

        def probe(uri, server):
            asked.append((uri, server.machine_id))
            return uri in answering

        found = plex.reachable(self.servers(), probe)
        self.assertEqual(
            [(server.name, uri) for server, uri in found],
            [("Home Media", "http://10.0.0.5:32400")],  # its plex.direct name didn't answer
        )
        # Every connection of every server was tried (at once: none waits for another).
        self.assertEqual(len(asked), 5)

    def test_a_server_none_of_whose_connections_answer_is_left_out(self):
        self.assertEqual(plex.reachable(self.servers(), lambda uri, server: False), [])

    def test_a_probe_that_raises_is_a_connection_that_did_not_answer(self):
        def probe(uri, server):
            if "relay" in uri:
                raise OSError("down")
            return "relay" not in uri and "10-0-0-5" in uri

        found = plex.reachable(self.servers(), probe)
        self.assertEqual([uri for _, uri in found], ["https://10-0-0-5.example.plex.direct:32400"])

    def test_identity_is_asked_without_the_token_and_must_be_that_server(self):  # review I1
        base = "https://10-0-0-5.example.plex.direct:32400"
        identity = {"MediaContainer": {"machineIdentifier": "machine-home"}}
        http = FakeHttp(base, {("GET", "/identity"): (200, identity)})
        server = self.servers()[0]
        self.assertTrue(plex.answers(base, server, "client-1", "2.14.0", session=http))
        self.assertNotIn("X-Plex-Token", http.asked[0]["headers"])
        self.assertIs(http.asked[0]["verify"], True)  # its certificate always checked
        other = plex.Server("Other", "machine-other", "t")
        self.assertFalse(plex.answers(base, other, "client-1", "2.14.0", session=http))
        self.assertTrue(http.closed is False)  # a session given is the caller's

    def test_the_token_goes_only_where_the_certificate_proved_the_server(self):  # review I1
        identity = {"MediaContainer": {"machineIdentifier": "machine-home"}}
        server = self.servers()[0]
        secure = "https://10-0-0-5.example.plex.direct:32400"

        class Asks(FakeHttp):  # an identity that wants the token
            def request(self, method, url, params=None, headers=None, **kw):
                answer = super().request(method, url, params=params, headers=headers, **kw)
                if "X-Plex-Token" in (headers or {}):
                    from tests.test_plex import _Response

                    return _Response(200, identity)
                return answer

        http = Asks(secure, {("GET", "/identity"): (401, None)})
        self.assertTrue(plex.answers(secure, server, "c", "v", session=http))
        self.assertEqual(
            [("X-Plex-Token" in a["headers"], a["verify"]) for a in http.asked],
            [(False, True), (True, True)],
        )
        plain = "http://10.0.0.5:32400"  # a stranger's machine at a hotel, maybe
        http = Asks(plain, {("GET", "/identity"): (401, None)})
        self.assertFalse(plex.answers(plain, server, "c", "v", session=http))
        self.assertEqual([("X-Plex-Token" in a["headers"]) for a in http.asked], [False])

    def test_a_plex_direct_address_is_always_checked(self):
        self.assertTrue(plex.must_verify("https://10-0-0-5.example.plex.direct:32400", False))
        self.assertFalse(plex.must_verify("https://media.example.test:32400", False))
        self.assertTrue(plex.must_verify("https://media.example.test:32400", True))


if __name__ == "__main__":
    unittest.main()
