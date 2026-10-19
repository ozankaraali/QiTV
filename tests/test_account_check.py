"""Plain failure reasons (2.2): an account check that opens no stream says why a channel or a
provider failed (services.account_check, services.failure_reasons), and the TV shell's error
toast says it while it still shows."""

from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
import socket
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import requests

from services import failure_reasons as reasons
from services.account_check import AccountChecks, check_account, check_stb, check_xtream
from services.failure_reasons import Reason
from services.live.refusals import STREAM_LIMIT
from tests import test_tv_controller as tv
from ui.settings_dialog import ProviderVerificationWorker
from ui.tv_controller import ERROR_MS

NOW = datetime(2026, 10, 19, 20, 0, tzinfo=timezone.utc)
PAST = int((NOW - timedelta(days=16)).timestamp())  # 3 Oct 2026
FUTURE = int((NOW + timedelta(days=30)).timestamp())
SECRET = "s3cret-pass"


class _Handler(BaseHTTPRequestHandler):
    """A fictional panel and portal: the test sets what each request answers."""

    owner = None

    def do_GET(self):
        url = urlsplit(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        self.owner.requests.append((url.path, query.get("action", "")))
        status, body, delay = self.owner.answer(url.path, query)
        if delay:
            time.sleep(delay)
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except ConnectionError:
            pass

    def log_message(self, *_args):
        pass


class ServerCase(unittest.TestCase):
    def setUp(self):
        self.requests = []
        handler = type("Handler", (_Handler,), {"owner": self})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server.daemon_threads = True
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
        thread.daemon = True
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_port}"

    def answer(self, path, query):
        raise NotImplementedError


class XtreamCheckTests(ServerCase):
    def setUp(self):
        super().setUp()
        self.reply = (200, {"user_info": {"auth": 1, "status": "Active"}}, 0.0)

    def answer(self, path, query):
        if path != "/player_api.php" or "action" in query:
            return 404, b"", 0.0
        return self.reply

    def check(self, **kwargs):
        return check_xtream(self.base, "user", SECRET, now=NOW, **kwargs)

    def user_info(self, **fields):
        self.reply = (200, {"user_info": {"auth": 1, **fields}, "server_info": {}}, 0.0)

    def test_an_active_account_is_fine(self):
        self.user_info(status="Active", exp_date=str(FUTURE), max_connections="1", active_cons="0")
        reason = self.check()
        self.assertEqual(reason.kind, reasons.OK)
        self.assertFalse(reason.plain)
        self.assertEqual(self.requests, [("/player_api.php", "")])  # no stream, no action

    def test_an_expired_account_says_since_when(self):
        self.user_info(status="Expired", exp_date=str(PAST))
        reason = self.check()
        self.assertEqual(reason, Reason(reasons.EXPIRED, "Your subscription expired on 3 Oct 2026"))
        self.assertTrue(reason.plain)

    def test_an_expiry_in_the_past_is_expired_whatever_the_status(self):
        self.user_info(status="Active", exp_date=PAST)
        self.assertEqual(self.check().kind, reasons.EXPIRED)
        self.user_info(status="Expired", exp_date=None)
        self.assertEqual(self.check().text, "Your subscription has expired")

    def test_the_connection_limit_says_how_many(self):
        self.user_info(status="Active", max_connections="1", active_cons="1")
        self.assertEqual(
            self.check(), Reason(reasons.LIMIT, "Your account allows 1 stream; one is in use")
        )
        self.user_info(status="Active", max_connections=2, active_cons=2)
        self.assertEqual(self.check().text, "Your account allows 2 streams; both are in use")
        self.user_info(status="Active", max_connections="3", active_cons="1")
        self.assertEqual(self.check().kind, reasons.OK)  # room left: not the limit

    def test_a_wrong_password_is_said_so(self):
        for body in ({"user_info": {"auth": 0}}, [], {}):
            with self.subTest(body=body):
                self.reply = (200, body, 0.0)
                self.assertEqual(
                    self.check(), Reason(reasons.WRONG_LOGIN, "The username or password is wrong")
                )
        self.reply = (513, b"", 0.0)  # some panels' empty answer to a wrong password
        self.assertEqual(self.check().kind, reasons.WRONG_LOGIN)

    def test_a_banned_account(self):
        self.user_info(status="Banned")
        self.assertEqual(self.check().text, "The provider has disabled this account")

    def test_refusals_say_their_status(self):
        cases = {
            403: "The server refused QiTV (403)",
            404: "The server has nothing at this address (404)",
            407: "A proxy on your network asks for a sign-in (407)",
            458: "The provider blocked this connection (458); it may not serve your region",
            451: "The provider blocked this connection (451); it may not serve your region",
            502: "The provider's server has a problem (502)",
        }
        for status, text in cases.items():
            with self.subTest(status=status):
                self.reply = (status, b"<h1>no</h1>", 0.0)
                reason = self.check()
                self.assertEqual(reason.text, text)
                self.assertTrue(reason.plain)

    def test_a_server_that_does_not_answer_in_time(self):
        self.reply = (200, {"user_info": {"auth": 1}}, 1.0)
        reason = self.check(timeout=(2.0, 0.2))
        self.assertEqual(reason, Reason(reasons.TIMEOUT, "The provider took too long to answer"))

    def test_a_server_that_cannot_be_reached(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        reason = check_xtream(f"http://127.0.0.1:{port}", "user", SECRET, now=NOW)
        self.assertEqual(reason, Reason(reasons.UNREACHABLE, "Couldn't reach the provider"))

    def test_an_answer_that_is_not_the_api_says_nothing(self):
        self.reply = (200, b"<html>panel</html>", 0.0)
        self.assertFalse(self.check().plain)

    def test_the_check_logs_no_credential(self):
        provider = {"type": "XTREAM", "name": "X", "url": self.base, "username": "u"}
        provider["password"] = SECRET
        self.reply = (403, b"", 0.0)
        with self.assertLogs("services", "DEBUG") as logs:
            check_account(provider, now=NOW)
        output = "\n".join(logs.output)
        self.assertIn("forbidden", output)
        for secret in (SECRET, "127.0.0.1", "player_api"):
            self.assertNotIn(secret, output)

    def test_check_account_reads_the_provider(self):
        self.user_info(status="Expired")
        provider = {"type": "XTREAM", "name": "X", "url": self.base, "username": "u"}
        self.assertEqual(check_account({**provider, "password": "p"}).kind, reasons.EXPIRED)
        self.assertEqual(check_account(provider).kind, reasons.WRONG_LOGIN)  # no password
        self.assertIsNone(check_account({"type": "XTREAM", "name": "X", "url": ""}))
        self.assertIsNone(check_account({"type": "M3UPLAYLIST", "name": "M", "url": self.base}))
        # A playlist from an Xtream panel is that panel's account (services.provider_link).
        playlist = f"{self.base}/get.php?username=u&password=p&type=m3u_plus"
        playlist_provider = {"type": "M3UPLAYLIST", "name": "M", "url": playlist}
        self.assertEqual(check_account(playlist_provider).kind, reasons.EXPIRED)
        self.assertEqual(self.requests[-1], ("/player_api.php", ""))


class StbCheckTests(ServerCase):
    MAC = "00:1A:79:00:00:01"

    def setUp(self):
        super().setUp()
        handshake_log = logging.getLogger("services.provider_manager")
        self.addCleanup(handshake_log.setLevel, handshake_log.level)
        handshake_log.setLevel(logging.ERROR)  # each refusal's warning: not this test's subject
        self.paths = {"/portal.php": (404, b""), "/server/load.php": None}
        self.profile = {"id": 7, "name": "Fixture"}

    def answer(self, path, query):
        fixed = self.paths.get(path, (404, b""))
        if fixed is not None:
            return (*fixed, 0.0)
        if query.get("action") == "handshake":
            return 200, {"js": {"token": "TOKEN"}}, 0.0
        return 200, {"js": self.profile}, 0.0

    def check(self):
        return check_stb(self.base, self.MAC, now=NOW)

    def test_a_known_device_is_fine(self):
        self.assertEqual(self.check().kind, reasons.OK)
        actions = [action for _path, action in self.requests]
        self.assertNotIn("create_link", actions)  # nothing plays

    def test_an_unknown_device(self):
        self.profile = {"id": None, "name": None}
        self.assertEqual(self.check(), Reason(reasons.DEVICE, reasons.DEVICE_MESSAGE))

    def test_a_blocked_or_expired_account(self):
        self.profile = {"id": 7, "name": "Fixture", "blocked": "1"}
        self.assertEqual(self.check().kind, reasons.DISABLED)
        self.profile = {"id": 7, "name": "F", "expire_billing_date": "2026-10-03 00:00:00"}
        self.assertEqual(self.check().text, "Your subscription expired on 3 Oct 2026")
        self.profile = {"id": 7, "name": "F", "expire_billing_date": "0000-00-00 00:00:00"}
        self.assertEqual(self.check().kind, reasons.OK)  # no date: unlimited

    def test_a_refusal_on_every_path_says_its_status(self):
        for status, kind in ((403, reasons.FORBIDDEN), (458, reasons.BLOCKED)):
            with self.subTest(status=status):
                self.paths = {"/portal.php": (404, b""), "/server/load.php": (status, b"")}
                self.assertEqual(self.check().kind, kind)
        self.paths = {"/portal.php": (404, b""), "/server/load.php": (404, b"")}
        self.assertEqual(self.check().kind, reasons.NOT_FOUND)  # the address is wrong
        self.paths = {"/portal.php": (401, b""), "/server/load.php": (401, b"")}
        self.assertEqual(self.check().kind, reasons.DEVICE)  # a portal has no password

    def test_a_portal_that_does_not_answer(self):
        with patch(
            "services.provider_manager.requests.get", side_effect=requests.exceptions.ReadTimeout()
        ):
            self.assertEqual(self.check().kind, reasons.TIMEOUT)
        with patch(
            "services.provider_manager.requests.get", side_effect=requests.ConnectionError()
        ):
            self.assertEqual(self.check().kind, reasons.UNREACHABLE)


class WordsTests(unittest.TestCase):
    def test_limits(self):
        self.assertEqual(reasons.limit_text(1, 1), "Your account allows 1 stream; one is in use")
        self.assertEqual(
            reasons.limit_text(3, 3), "Your account allows 3 streams; all 3 are in use"
        )
        self.assertEqual(reasons.limit_text(4, 2), "Your account allows 4 streams; 2 are in use")

    def test_the_strongest_status(self):
        self.assertEqual(reasons.strongest_status([404, 403]), 403)
        self.assertEqual(reasons.strongest_status([404, 407, 458]), 407)
        self.assertEqual(reasons.strongest_status([404, 404]), 404)
        self.assertEqual(reasons.strongest_status([404, 502]), 502)
        self.assertIsNone(reasons.strongest_status([]))

    def test_no_url_or_credential_can_reach_a_text(self):
        # Every text is fixed or built from numbers and a date.
        self.assertIsNone(reasons.status_reason(None))
        self.assertIsNone(reasons.status_reason(302))
        body = {"user_info": {"auth": 1, "status": "http://x/?password=p", "message": SECRET}}
        self.assertNotIn(SECRET, reasons.xtream_reason(body, NOW).text)


class FakeRunner:
    def __init__(self):
        self.jobs = []

    def run_worker(self, worker, on_finished, on_error=None, context=None):
        self.jobs.append((worker, on_finished, context))
        return worker

    def answer(self, index, reason):
        _worker, on_finished, context = self.jobs[index]
        on_finished(context, reason)


class AccountChecksTests(unittest.TestCase):
    def setUp(self):
        self.runner = FakeRunner()
        self.now = 1000.0
        self.checks = AccountChecks(
            type("Config", (), {"prefer_https": False, "ssl_verify": True})(),
            runner=self.runner,
            clock=lambda: self.now,
        )
        self.provider = {"name": "Harbor", "type": "XTREAM", "url": "http://panel.test"}

    def test_at_most_one_check_a_minute_per_provider(self):
        heard = []
        self.assertTrue(self.checks.explain(self.provider, heard.append))
        self.checks.explain(self.provider, heard.append)  # under way: waits for the same one
        self.assertEqual(len(self.runner.jobs), 1)
        expired = Reason(reasons.EXPIRED, reasons.EXPIRED_MESSAGE)
        self.runner.answer(0, expired)
        self.assertEqual(heard, [expired, expired])
        self.now += 59
        self.checks.explain(self.provider, heard.append)  # the last answer, at once
        self.assertEqual((len(self.runner.jobs), heard[-1]), (1, expired))
        self.checks.explain({**self.provider, "name": "Other"}, heard.append)
        self.assertEqual(len(self.runner.jobs), 2)  # another provider has its own minute
        self.now += 2
        self.checks.explain(self.provider, heard.append)
        self.assertEqual(len(self.runner.jobs), 3)  # a minute later: asked again

    def test_nothing_to_ask_and_a_closed_window(self):
        self.assertFalse(self.checks.explain({"name": "M", "type": "XTREAM"}, print))
        plain = {"name": "Free", "type": "M3UPLAYLIST", "url": "https://lists.example.test/a.m3u"}
        self.assertFalse(self.checks.explain(plain, print))  # a playlist has no account
        panel = {**plain, "url": "http://panel.example.test/get.php?username=a&password=b"}
        self.assertTrue(self.checks.explain(panel, print))  # an Xtream panel's playlist has
        self.assertEqual(len(self.runner.jobs), 1)
        self.runner.jobs.clear()
        heard = []
        self.checks.explain(self.provider, heard.append)
        self.checks.close()
        self.runner.answer(0, Reason(reasons.OK, ""))
        self.assertEqual(heard, [])
        self.assertFalse(self.checks.explain(self.provider, heard.append))


class FakeChecks:
    """AccountChecks as the controller sees it: the test answers each ask."""

    def __init__(self):
        self.asks = []

    def explain(self, provider, answer):
        self.asks.append((provider, answer))
        return True

    def close(self):
        pass

    def answer(self, reason, index=-1):
        self.asks[index][1](reason)


LIMIT = Reason(reasons.LIMIT, "Your account allows 1 stream; one is in use")
EXPIRED = Reason(reasons.EXPIRED, "Your subscription expired on 3 Oct 2026")


class ReasonCase:
    """A controller test case whose controllers ask FakeChecks why."""

    def setUp(self):
        self.checks = FakeChecks()
        super().setUp()

    def make_controller(self, column, **extra):
        return super().make_controller(column, account_checks=self.checks, **extra)


class ControllerReasonTests(ReasonCase, tv.ControllerTestCase):
    def setUp(self):
        super().setUp()
        self.controller.start()

    def test_a_failed_link_says_why_at_its_provider(self):
        self.tune(0)
        self.catalog.linkFailed.emit(self.catalog.links[-1][0], "Portal down")
        self.assertEqual([p["name"] for p, _ in self.checks.asks], ["Home"])
        self.checks.answer(EXPIRED)
        self.assertEqual(
            self.toast.messages()[-2:],
            [
                ("One: Portal down", "error"),
                ("One: Your subscription expired on 3 Oct 2026", "error"),
            ],
        )

    def test_an_account_that_is_fine_leaves_the_failure_as_it_was(self):
        self.tune(0)
        self.catalog.linkFailed.emit(self.catalog.links[-1][0], "Portal down")
        shown = list(self.toast.messages())
        self.checks.answer(Reason(reasons.OK, ""))
        self.checks.answer(None)
        self.assertEqual(self.toast.messages(), shown)

    def test_a_reason_after_another_message_or_too_late_is_dropped(self):
        self.tune(0)
        self.catalog.linkFailed.emit(self.catalog.links[-1][0], "Portal down")
        self.controller._toast("Added to favorites")
        self.checks.answer(EXPIRED)
        self.assertNotIn(EXPIRED.text, " ".join(t for t, _ in self.toast.messages()))
        self.tune(1)
        self.catalog.linkFailed.emit(self.catalog.links[-1][0], "Portal down")
        later = time.monotonic() + ERROR_MS / 1000 + 1
        with patch("ui.tv_controller.time.monotonic", return_value=later):
            self.checks.answer(EXPIRED)
        self.assertNotIn(EXPIRED.text, " ".join(t for t, _ in self.toast.messages()))

    def test_a_provider_that_failed_its_sign_in(self):
        self.catalog.hub_failed("Home", "Couldn't reach the provider")
        self.assertIn("Home", [p["name"] for p, _ in self.checks.asks])
        self.checks.answer(Reason(reasons.WRONG_LOGIN, reasons.WRONG_LOGIN_MESSAGE))
        self.assertEqual(
            self.toast.messages()[-1], ("Home: The username or password is wrong", "error")
        )

    def test_another_providers_failure_asks_that_provider(self):
        self.controller.tune(tv.HARBOR_ONE, group_key="10", channels=[tv.HARBOR_ONE])
        self.catalog.linkFailed.emit(self.catalog.links[-1][0], "Portal down")
        self.assertEqual(self.checks.asks[-1][0]["name"], "Harbor IPTV")


class PipelineReasonTests(ReasonCase, tv.PipelineCase):

    def test_the_connection_limit_says_how_many_are_in_use(self):
        self.start_pipeline(0)
        self.live.failed.emit(STREAM_LIMIT)
        self.assertEqual(
            self.toast.messages(), [("Your provider allows 1 stream at a time", "error")]
        )
        self.checks.answer(LIMIT)
        self.assertEqual(
            self.toast.messages()[-1], ("One: Your account allows 1 stream; one is in use", "error")
        )

    def test_a_stream_that_failed_for_an_expired_account(self):
        self.catalog.ptype = "M3UPLAYLIST"
        self.start_pipeline(0)
        self.live.failed.emit("The stream could not be opened")
        self.checks.answer(EXPIRED)
        self.assertEqual(
            self.toast.messages(),
            [
                ("One: The stream could not be opened", "error"),
                ("One: Your subscription expired on 3 Oct 2026", "error"),
            ],
        )


class VerifyReasonTests(ServerCase):
    """Settings' Verify Provider says why in plain words."""

    def answer(self, path, query):
        return self.reply

    def verify(self, **provider):
        said, verified = [], []
        worker = ProviderVerificationWorker(1, provider, False, True)
        worker.reason.connect(lambda _id, text: said.append(text))
        worker.result.connect(lambda _id, _provider, ok: verified.append(ok))
        worker.run()
        return verified[0], said[0]

    def test_an_expired_account_does_not_verify_and_says_why(self):
        self.reply = (200, {"user_info": {"auth": 1, "status": "Expired"}}, 0.0)
        provider = {"type": "XTREAM", "url": self.base, "username": "u", "password": "p"}
        self.assertEqual(self.verify(**provider), (False, "Your subscription has expired"))

    def test_an_account_at_its_limit_still_verifies(self):
        info = {"auth": 1, "status": "Active", "max_connections": "1", "active_cons": "1"}
        self.reply = (200, {"user_info": info}, 0.0)
        provider = {"type": "XTREAM", "url": self.base, "username": "u", "password": "p"}
        self.assertEqual(self.verify(**provider), (True, ""))


if __name__ == "__main__":
    unittest.main()
