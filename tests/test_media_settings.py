"""Settings → Verify on a Jellyfin or Emby server (2.8): it signs in with the password and
hands back the server's access (the token); the client names QiTV, never the computer."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import socket
import threading
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ui.settings_dialog import ProviderVerificationWorker


class _Server(BaseHTTPRequestHandler):
    seen: list = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        self.seen.append((self.path, self.headers.get("X-Emby-Authorization", "")))
        if self.path == "/Users/AuthenticateByName" and body.get("Pw") == "pw":
            payload = json.dumps({"AccessToken": "T", "User": {"Id": "U"}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(401)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


class VerifyMediaServerTests(unittest.TestCase):
    def setUp(self):
        _Server.seen = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Server)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def verify(self, password):
        provider = {
            "type": "JELLYFIN",
            "name": "Home",
            "url": self.url,
            "username": "me",
            "password": password,
        }
        worker = ProviderVerificationWorker(1, provider, True)
        got = {"access": None, "why": "", "verified": None}
        worker.signed_in.connect(lambda _rid, access: got.update(access=access))
        worker.reason.connect(lambda _rid, why: got.update(why=why))
        worker.result.connect(lambda _rid, _provider, ok: got.update(verified=ok))
        worker.run()
        return got

    def test_a_sign_in_hands_back_the_servers_access(self):
        got = self.verify("pw")
        self.assertTrue(got["verified"])
        self.assertEqual((got["access"]["token"], got["access"]["user_id"]), ("T", "U"))
        self.assertTrue(got["access"]["device_id"])
        _path, header = _Server.seen[-1]
        self.assertIn('Client="QiTV"', header)
        self.assertNotIn(socket.gethostname(), header)

    def test_a_wrong_password_says_so(self):
        got = self.verify("nope")
        self.assertFalse(got["verified"])
        self.assertIsNone(got["access"])
        self.assertEqual(got["why"], "The user name or the password is not right.")


if __name__ == "__main__":
    unittest.main()
