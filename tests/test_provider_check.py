"""The provider check: what Settings' Verify and the drawer's cards ask a provider
(services.provider_check, blocking), and the worker that asks it off the GUI thread
(ui.provider_check). Fictional hosts."""

import os
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QApplication
import requests
import shiboken6
from timer_fixtures import wait_until

from services import failure_reasons as reasons, media_workers, provider_api, provider_check
from services.failure_reasons import Reason
from services.jobs import JobRunner
from services.provider_check import Outcome, check, check_kept
from ui.provider_check import ProviderCheck

PANEL = {
    "type": "XTREAM",
    "name": "Panel",
    "url": "https://panel.example.test:8080",
    "username": "viewer",
    "password": "secret",
}
PORTAL = {"type": "STB", "name": "Portal", "url": "https://portal.example.test/c/", "mac": "00:1A"}
SERVER = {
    "type": "JELLYFIN",
    "name": "Home",
    "url": "https://media.example.test",
    "username": "ann",
}
ACCESS = {"token": "T", "user_id": "U", "device_id": "D"}
WRONG = "The user name or the password is not right."
CLOSED = "http://127.0.0.1:9"  # a closed port on the loopback: every ask fails at once


class _HeldRunner:
    """A runner that holds each job until the test runs it, in any order."""

    def __init__(self):
        self.jobs = []

    def run_worker(self, worker, on_finished, on_error=None, context=None):
        self.jobs.append((worker, on_finished, on_error, context))
        return worker

    def run(self, index):
        worker, on_finished, _on_error, context = self.jobs[index]
        worker.finished.connect(lambda payload: on_finished(context, payload))
        worker.run()

    def fail(self, index):
        _worker, _on_finished, on_error, context = self.jobs[index]
        on_error(context, "The background task failed")


class CheckTests(unittest.TestCase):
    def setUp(self):
        for memory in (
            mock.patch.object(provider_api, "_http_only", set()),
            mock.patch.dict("services.workers._https_bases", clear=True),
        ):
            memory.start()
            self.addCleanup(memory.stop)

    def test_xtream_and_portal_use_the_account_check(self):
        asked = []

        def account(provider, *, ssl_verify):
            asked.append((dict(provider), ssl_verify))
            return found

        dated = "The portal says your subscription ended on 3 Oct 2026"
        expired = "Your subscription has expired"
        answers = (
            (Reason(reasons.OK, ""), Outcome(True)),
            # At its stream limit, an account still works; a dated profile is only noted.
            (Reason(reasons.LIMIT, "Your account allows 1 stream; one is in use"), Outcome(True)),
            (Reason(reasons.DATED, dated), Outcome(True, dated)),
            (Reason(reasons.EXPIRED, expired), Outcome(False, expired)),
            (Reason(reasons.UNEXPECTED, "Something"), Outcome(False)),
            (None, Outcome(False)),
        )
        with mock.patch.object(provider_check, "check_account", side_effect=account):
            for found, expected in answers:
                for provider in (PANEL, PORTAL):
                    with self.subTest(kind=provider["type"], found=found):
                        self.assertEqual(check(provider, True), expected)
            self.assertEqual({verify for _provider, verify in asked}, {True})
            # The provider's own Verify SSL wins over the global setting.
            found = Reason(reasons.OK, "")
            check({**PANEL, "ssl_verify": False}, True)
            self.assertIs(asked[-1][1], False)
            # A password typed apart is the one asked; the caller's provider is left alone.
            provider = {key: value for key, value in PANEL.items() if key != "password"}
            check(provider, True, password="typed")
            self.assertEqual(asked[-1][0]["password"], "typed")
            self.assertNotIn("password", provider)

    def test_an_http_address_that_answers_only_over_http_says_so(self):
        found = Reason(reasons.OK, "")
        panel = {**PANEL, "url": "http://panel.example.test:8080"}
        with (
            mock.patch.object(provider_check, "check_account", return_value=found),
            mock.patch("services.workers.xtream_answers_https", return_value=False) as https,
        ):
            self.assertEqual(check(panel, True), Outcome(True, "", True))
            https.assert_called_once_with(
                "http://panel.example.test:8080", "viewer", "secret", verify_ssl=True
            )
            https.return_value = True
            self.assertEqual(check(panel, True), Outcome(True))
            self.assertEqual(check(PANEL, True), Outcome(True))  # an HTTPS address: not asked
            self.assertEqual(https.call_count, 2)
        with (
            mock.patch.object(provider_check, "check_account", return_value=found),
            mock.patch("services.workers.xtream_answers_https", side_effect=OSError("down")),
            self.assertLogs("services.provider_check", "WARNING"),
        ):
            # The account answered: an HTTPS probe that breaks leaves it working, without a note.
            self.assertEqual(check(panel, True), Outcome(True))

    def test_playlists_use_a_head_or_the_file(self):
        heads, answering = [], {}

        def head(url, **kwargs):
            heads.append((url, kwargs["headers"]["User-Agent"], kwargs["verify"]))
            if url not in answering:
                raise requests.ConnectionError("refused")
            return SimpleNamespace(status_code=answering[url])

        with mock.patch.object(provider_check.requests, "head", side_effect=head):
            answering["https://lists.example.test/a.m3u"] = 200
            playlist = {"type": "M3UPLAYLIST", "url": "https://lists.example.test/a.m3u"}
            self.assertEqual(check(playlist, True), Outcome(True))
            agent = provider_api.CLIENT_HEADERS["User-Agent"]
            self.assertEqual(heads, [("https://lists.example.test/a.m3u", agent, True)])
            # Over HTTPS first; an address that answers over HTTP alone says so.
            heads.clear()
            answering["http://lists.example.test/b.m3u"] = 200
            stream = {"type": "M3USTREAM", "url": "http://lists.example.test/b.m3u"}
            self.assertEqual(check(stream, True), Outcome(True, "", True))
            self.assertEqual(
                [url for url, _agent, _verify in heads],
                ["https://lists.example.test/b.m3u", "http://lists.example.test/b.m3u"],
            )
            answering["https://lists.example.test/c.m3u"] = 404
            missing = {"type": "M3UPLAYLIST", "url": "https://lists.example.test/c.m3u"}
            self.assertEqual(check(missing, True), Outcome(False))
            heads.clear()
            self.assertEqual(check({**playlist, "ssl_verify": False}, True), Outcome(True))
            self.assertIs(heads[-1][2], False)
            # A file on this computer: it is there, or not; nothing is asked.
            heads.clear()
            with tempfile.NamedTemporaryFile(suffix=".m3u") as handle:
                self.assertEqual(check({"type": "M3UPLAYLIST", "url": handle.name}, True).ok, True)
                gone = {"type": "M3UPLAYLIST", "url": handle.name + ".gone"}
                self.assertEqual(check(gone, True), Outcome(False))
            self.assertEqual(heads, [])

    def test_media_servers_sign_in_and_hand_back_access_without_the_password(self):
        calls = []

        def sign_in(url, kind, username, password, verify, device_id=""):
            calls.append((url, kind, username, password, verify, device_id))
            if password != "pw":
                return False, WRONG
            return True, {**ACCESS, "password": password}  # a careless answer keeps it too

        with (
            mock.patch.object(media_workers, "sign_in", side_effect=sign_in),
            mock.patch("services.workers.xtream_answers_https", side_effect=AssertionError),
        ):
            provider = dict(SERVER)
            outcome = check(provider, True, password="pw")
            self.assertEqual(outcome, Outcome(True, "", False, ACCESS))
            self.assertNotIn("password", outcome.access)
            self.assertEqual(provider, SERVER)
            self.assertEqual(calls[-1], (SERVER["url"], "JELLYFIN", "ann", "pw", True, ""))
            # The old window's form holds the password in the provider; a known device stays.
            emby = {**SERVER, "type": "EMBY", "url": "http://media.example.test", "device_id": "D"}
            outcome = check({**emby, "password": "pw", "ssl_verify": False}, True)
            self.assertEqual(outcome, Outcome(True, "", False, ACCESS))  # never probed for HTTPS
            self.assertEqual(calls[-1][1:], ("EMBY", "ann", "pw", False, "D"))
            self.assertEqual(check(SERVER, True, password="nope"), Outcome(False, WRONG))

    def test_a_media_server_already_added_is_asked_with_its_kept_sign_in(self):
        # No password is kept (only the sign-in's token): Settings' Check the connection asks
        # the server with the token, never a sign-in with an empty password.
        kept = {**SERVER, **ACCESS}
        with (
            mock.patch.object(media_workers, "sign_in", side_effect=AssertionError),
            mock.patch.object(media_workers, "check_access", return_value=(True, "")) as asked,
        ):
            self.assertEqual(check_kept(kept, True), Outcome(True))
            asked.assert_called_once_with(kept, True)
        with mock.patch.object(media_workers, "check_access", return_value=(False, "Again")):
            outcome = check_kept({**kept, "ssl_verify": False}, True)
        self.assertEqual(outcome, Outcome(False, "Again"))
        with mock.patch.object(media_workers, "check_access", side_effect=RuntimeError("x")):
            with self.assertLogs("services.provider_check", "WARNING"):
                self.assertEqual(check_kept(kept, True), Outcome(False))
        # A form (the old window's Verify, the address card) signs in, even with a token: a
        # user name changed without its password is never passed on the old token.
        with (
            mock.patch.object(media_workers, "check_access", side_effect=AssertionError),
            mock.patch.object(media_workers, "sign_in", return_value=(False, WRONG)) as signed,
        ):
            self.assertEqual(check({**kept, "username": "bob"}, True), Outcome(False, WRONG))
            self.assertEqual(signed.call_args.args[2:4], ("bob", ""))
        # Any other provider as kept is checked as ever.
        with mock.patch.object(provider_check, "check", return_value=Outcome(True)) as plain:
            self.assertEqual(check_kept(PANEL, False), Outcome(True))
        plain.assert_called_once_with(PANEL, False)


class ProviderCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_a_stale_answer_is_dropped(self):
        runner = _HeldRunner()
        asker = ProviderCheck(runner=runner)
        answers = []
        asker.answered.connect(lambda request, outcome: answers.append((request, outcome)))
        asked = []

        def answer(provider, ssl_verify, password=""):
            asked.append((dict(provider), ssl_verify, password))
            return Outcome(provider["name"] == "Second")

        provider = dict(PANEL, name="First")
        first = asker.start(provider, True)
        provider["name"] = "Changed"  # the caller's dict changes; the check asks what was sent
        second = asker.start(dict(PANEL, name="Second"), False, password="pw")
        self.assertNotEqual(first, second)
        with mock.patch.object(provider_check, "check", side_effect=answer):
            runner.run(1)
            runner.run(0)  # the first answers last: dropped
        self.assertEqual(answers, [(second, Outcome(True))])
        self.assertEqual(
            [(p["name"], verify, password) for p, verify, password in asked],
            [("Second", False, "pw"), ("First", True, "")],
        )
        # A job that fails answers as a failed check.
        third = asker.start(PANEL, True)
        runner.fail(2)
        self.assertEqual(answers[-1], (third, Outcome(False)))
        # An answer after its asker is gone is dropped quietly.
        asker.start(PANEL, True)
        shiboken6.delete(asker)
        with mock.patch.object(provider_check, "check", return_value=Outcome(True)):
            runner.run(3)
        self.assertEqual(len(answers), 2)

    def test_the_worker_creates_no_qt_objects(self):
        # The check runs on a thread of its own and hands back plain data.
        runner = JobRunner()
        asker = ProviderCheck(runner=runner)
        answers, threads = [], []
        asker.answered.connect(lambda request, outcome: answers.append(outcome))

        def answer(provider, ssl_verify, password=""):
            threads.append(threading.get_ident())
            return Outcome(True)

        with mock.patch.object(provider_check, "check", side_effect=answer):
            asker.start(PANEL, True)
            self.assertTrue(wait_until(lambda: answers and not runner.pending()))
        self.assertEqual(answers, [Outcome(True)])
        self.assertNotIsInstance(answers[0], QObject)
        self.assertNotEqual(threads, [threading.get_ident()])
        # What it asks, for every kind, makes no Qt object on that thread.
        with tempfile.NamedTemporaryFile(suffix=".m3u") as handle:
            providers = [
                {**PANEL, "url": CLOSED},
                {**PORTAL, "url": f"{CLOSED}/c/"},
                {"type": "M3UPLAYLIST", "url": f"{CLOSED}/list.m3u"},
                {"type": "M3UPLAYLIST", "url": handle.name},
                {**SERVER, "url": CLOSED},
            ]
            made, outcomes, errors = [], [], []

            def ask_all():
                try:
                    before = [w for w in shiboken6.getAllValidWrappers() if isinstance(w, QObject)]
                    for provider in providers:
                        outcomes.append(check(provider, True, password="pw"))
                    after = [w for w in shiboken6.getAllValidWrappers() if isinstance(w, QObject)]
                    known = {id(w) for w in before}  # `before` holds them: no id is reused
                    made.extend(type(w).__name__ for w in after if id(w) not in known)
                except Exception as e:  # the test's thread reports it
                    errors.append(e)

            sign_in_worker = mock.patch.object(
                media_workers, "MediaSignInWorker", side_effect=AssertionError
            )
            with sign_in_worker, self.assertLogs(level="DEBUG"):
                worker = threading.Thread(target=ask_all)
                worker.start()
                worker.join(30)  # this thread waits: nothing else makes a wrapper meanwhile
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual([outcome.ok for outcome in outcomes], [False, False, False, True, False])
        self.assertEqual(made, [])


if __name__ == "__main__":
    unittest.main()
