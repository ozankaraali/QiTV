"""Protocol races not reliably reproducible with a real media decoder.

The transport feeds actual newline-framed MPV replies into the controller;
the platform smoke additionally exercises the real QProcess/player lifecycle.
"""

import json
import os
from types import SimpleNamespace
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtNetwork import QLocalSocket
from PySide6.QtWidgets import QApplication

from mpv_player import MpvPlayer


class MemorySocket:
    def __init__(self, timeline):
        self.commands = []
        self.incoming = b""
        self.timeline = timeline

    def state(self):
        return QLocalSocket.ConnectedState

    def write(self, payload):
        message = json.loads(payload)
        self.commands.append(message)
        self.timeline.append(("command", message["command"]))
        return len(payload)

    def readAll(self):
        result, self.incoming = self.incoming, b""
        return result


class ConnectedPlayer(MpvPlayer):
    def is_running(self):
        return True


class MpvProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.timeline = []
        self.player = ConnectedPlayer(SimpleNamespace(ssl_verify=True, keyboard_remote_mode=False))
        self.socket = MemorySocket(self.timeline)
        self.player._socket = self.socket
        self.player._ready = True
        self.positions = []
        self.ends = []
        self.errors = []
        self.player.positionChanged.connect(lambda *args: self.positions.append(args))
        self.player.positionChanged.connect(lambda *args: self.timeline.append(("position", args)))
        self.player.mediaEnded.connect(self.ends.append)
        self.player.errorOccurred.connect(self.errors.append)
        self.replied = set()

    def tearDown(self):
        self.player._socket = None
        self.player.deleteLater()

    def feed(self, *messages):
        self.socket.incoming = b"".join(
            json.dumps(message).encode() + b"\n" for message in messages
        )
        self.player._read(self.socket)

    def reply(self, command, data=None, error="success"):
        request = next(
            request
            for request in self.socket.commands
            if request["command"][: len(command)] == command
            and request["request_id"] not in self.replied
        )
        self.replied.add(request["request_id"])
        self.feed({"request_id": request["request_id"], "error": error, "data": data})

    def loaded(self, content_id, entry_id):
        self.player.play(
            "https://provider.invalid/" + content_id, content_id=content_id, is_live=False
        )
        self.reply(["loadfile"])
        self.feed({"event": "start-file", "playlist_entry_id": entry_id}, {"event": "file-loaded"})
        self.observe("duration", 100)
        self.observe("time-pos", 12)

    def observe(self, name, value):
        observer = next(
            key for key, observed in self.player._active.observers.items() if observed == name
        )
        self.feed({"event": "property-change", "name": name, "id": observer, "data": value})

    def test_rapid_replacement_flushes_old_identity_and_loads_only_latest_intent(self):
        self.loaded("A", 10)
        old_observers = dict(self.player._active.observers)
        self.player.play("https://provider.invalid/B", content_id="B")
        self.player.play("https://provider.invalid/C", content_id="C")
        self.reply(["get_property", "duration"], 100)
        self.reply(["get_property", "time-pos"], 13.25)
        self.reply(["stop"])
        # Natural EOF arriving while stop is in flight must not auto-play A's next item.
        self.feed({"event": "end-file", "playlist_entry_id": 10, "reason": "eof"})
        self.reply(["get_property", "idle-active"], True)
        loads = [
            request["command"][1]
            for request in self.socket.commands
            if request["command"][0] == "loadfile"
        ]
        self.assertEqual(loads, ["https://provider.invalid/A", "https://provider.invalid/C"])
        self.assertEqual(self.ends, [])
        final_index = self.timeline.index(("position", ("A", 13250, 100000)))
        stop_index = self.timeline.index(("command", ["stop"]))
        self.assertLess(final_index, stop_index)
        self.reply(["loadfile"])
        self.feed({"event": "start-file", "playlist_entry_id": 12}, {"event": "file-loaded"})
        self.observe("duration", 80)
        self.observe("time-pos", 2)
        for observer, name in old_observers.items():
            self.feed({"event": "property-change", "id": observer, "name": name, "data": 99})
        self.player._flush_position()
        self.assertEqual(self.positions[-1], ("C", 2000, 80000))
        self.feed({"event": "end-file", "playlist_entry_id": 10, "reason": "eof"})
        self.assertEqual(self.ends, [])
        self.feed({"event": "end-file", "playlist_entry_id": 12, "reason": "eof"})
        self.assertEqual(self.ends, ["C"])

    def test_cancel_before_start_file_does_not_wait_for_an_end_event(self):
        self.player.play("https://provider.invalid/A", content_id="A")
        self.player.play("https://provider.invalid/B", content_id="B")
        self.reply(["loadfile"])
        self.reply(["stop"])
        self.reply(["get_property", "idle-active"], True)
        loads = [
            request["command"][1]
            for request in self.socket.commands
            if request["command"][0] == "loadfile"
        ]
        self.assertEqual(loads, ["https://provider.invalid/A", "https://provider.invalid/B"])
        self.assertEqual(self.ends, [])

    def test_shutdown_flushes_snapshot_before_quit_without_emitting_eof(self):
        self.loaded("A", 1)
        self.player.shutdown()
        self.reply(["get_property", "time-pos"], 18.125)
        self.reply(["get_property", "duration"], 100)
        final_index = self.timeline.index(("position", ("A", 18125, 100000)))
        quit_index = self.timeline.index(("command", ["quit"]))
        self.assertLess(final_index, quit_index)
        self.feed({"event": "end-file", "playlist_entry_id": 1, "reason": "eof"})
        self.assertEqual(self.ends, [])

    def test_fragmented_multiple_frames_preserve_unicode_content_identity(self):
        self.loaded("épisode", 1)
        observer = next(
            key for key, name in self.player._active.observers.items() if name == "time-pos"
        )
        frames = (
            json.dumps({"event": "property-change", "id": observer, "data": 25}).encode()
            + b"\n"
            + b'{"event":"end-file","playlist_entry_id":1,"reason":"eof"}\n'
        )
        for chunk in (frames[:20], frames[20:45], frames[45:]):
            self.socket.incoming = chunk
            self.player._read(self.socket)
        self.assertEqual(self.ends, ["épisode"])
        self.assertEqual(self.positions[-1], ("épisode", 25000, 100000))

    def test_lost_ipc_reports_once_and_flushes_without_fabricating_eof(self):
        self.loaded("A", 1)
        self.socket = self.player._socket = None
        self.player._check_disconnected(object())  # A retired process's callback.
        self.assertEqual(self.errors, [])
        self.player._check_disconnected(self.player._process)
        self.player._check_disconnected(self.player._process)
        self.assertEqual(len(self.errors), 1)
        self.assertEqual(self.ends, [])
        self.assertEqual(self.positions[-1], ("A", 12000, 100000))

    def test_decoder_error_never_exposes_provider_credentials(self):
        self.loaded("A", 1)
        secret = "https://user:password@provider.invalid/user/password/movie?token=secret"
        self.feed(
            {"event": "end-file", "playlist_entry_id": 1, "reason": "error", "file_error": secret}
        )
        self.assertEqual(self.ends, [])
        self.assertEqual(len(self.errors), 1)
        for credential in ("password", "token=secret", "provider.invalid"):
            self.assertNotIn(credential, self.errors[0])


if __name__ == "__main__":
    unittest.main()
