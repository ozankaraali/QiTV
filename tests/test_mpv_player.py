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


class BufferedRecorder:
    def __init__(self):
        self.stopped = False

    def playback(self, target):
        return "http://127.0.0.1/reader.m3u8", 100.0, (target or 110.0) - 100.0

    def request_stop(self):
        self.stopped = True


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
        self.reply(["loadfile"], {"playlist_entry_id": entry_id})
        self.feed({"event": "start-file", "playlist_entry_id": entry_id}, {"event": "file-loaded"})
        self.observe("duration", 100)
        self.observe("time-pos", 12)

    def observe(self, name, value):
        observer = next(
            key for key, observed in self.player._active.observers.items() if observed == name
        )
        self.feed({"event": "property-change", "name": name, "id": observer, "data": value})

    def buffered(self, paused=False):
        self.loaded("live", 1)
        item = self.player._active
        item.is_live = item.buffered = True
        item.buffer_base = 100.0
        item.paused = paused
        self.observe("pause", paused)
        recorder = BufferedRecorder()
        self.player._recorder = recorder
        self.player._buffer_source = item
        self.player._buffer_state = {"start": 100.0, "end": 130.0}
        self.player._buffer_started = True
        return recorder

    def stopped_reply(self, entry_id):
        self.reply(["script-message-to", "qitv", "stop", str(entry_id)])
        self.feed({"event": "client-message", "args": ["qitv", "stopped", str(entry_id)]})

    def finish_replacement(self, old_entry, new_entry):
        self.reply(["get_property", "duration"], 100)
        self.reply(["get_property", "time-pos"], 12)
        self.stopped_reply(old_entry)
        self.feed({"event": "end-file", "playlist_entry_id": old_entry, "reason": "stop"})
        self.feed({"event": "property-change", "id": 0, "data": True})
        self.reply(["loadfile"], {"playlist_entry_id": new_entry})
        self.feed({"event": "start-file", "playlist_entry_id": new_entry}, {"event": "file-loaded"})

    def test_rapid_replacement_flushes_old_identity_and_loads_only_latest_intent(self):
        self.loaded("A", 10)
        old_observers = dict(self.player._active.observers)
        self.player.play("https://provider.invalid/B", content_id="B")
        self.player.play("https://provider.invalid/C", content_id="C")
        self.reply(["get_property", "duration"], 100)
        self.reply(["get_property", "time-pos"], 13.25)
        self.stopped_reply(10)
        # Natural EOF arriving while stop is in flight must not auto-play A's next item.
        self.feed({"event": "end-file", "playlist_entry_id": 10, "reason": "eof"})
        self.feed({"event": "property-change", "id": 0, "data": True})
        self.reply(["get_property", "idle-active"], True)
        loads = [
            request["command"][1]
            for request in self.socket.commands
            if request["command"][0] == "loadfile"
        ]
        self.assertEqual(loads, ["https://provider.invalid/A", "https://provider.invalid/C"])
        self.assertEqual(self.ends, [])
        final_index = self.timeline.index(("position", ("A", 13250, 100000)))
        stop_index = self.timeline.index(("command", ["script-message-to", "qitv", "stop", "10"]))
        self.assertLess(final_index, stop_index)
        self.reply(["loadfile"], {"playlist_entry_id": 12})
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
        self.reply(["loadfile"], {"playlist_entry_id": 1})
        self.stopped_reply(1)
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

    def test_paused_buffer_seek_waits_for_seekable_gop_not_forward_cache_duration(self):
        self.loaded("live", 1)
        item = self.player._active
        item.is_live = item.buffered = True
        item.resume = 1000
        item.paused = True
        start = len(self.socket.commands)
        self.observe(
            "demuxer-cache-state",
            {"cache-duration": 3, "seekable-ranges": [{"start": 0, "end": 0.96}]},
        )
        self.assertEqual(self.socket.commands[start:], [])
        self.observe(
            "demuxer-cache-state",
            {"cache-duration": 3, "seekable-ranges": [{"start": 0, "end": 1.96}]},
        )
        self.assertEqual(self.socket.commands[-1]["command"], ["seek", 1.0, "absolute+exact"])
        self.observe("time-pos", 0.0)
        self.assertEqual(item.resume, 1000)
        self.observe("time-pos", 1.0)
        self.assertEqual(item.resume, 0)
        self.assertEqual(self.socket.commands[-1]["command"], ["set_property", "pause", True])

    def test_repeated_buffer_seek_preserves_playing_and_paused_intent(self):
        self.buffered()
        self.player.seek_buffer(111)
        self.finish_replacement(1, 2)
        self.observe("pause", True)  # Reader initialization, not user intent.
        self.player.seek_buffer(112)
        self.assertFalse(self.player._pending.paused)
        self.finish_replacement(2, 3)
        self.observe("pause", True)
        self.feed({"event": "client-message", "args": ["qitv", "buffer-seek", "113", "yes"]})
        self.assertFalse(self.player._pending.paused)
        self.finish_replacement(3, 4)
        self.observe("pause", True)
        self.player.toggle_pause()  # User now requests pause during initialization.
        self.player.seek_buffer(114)
        self.assertTrue(self.player._pending.paused)
        self.finish_replacement(4, 5)
        self.observe("pause", True)
        self.observe("demuxer-cache-state", {"seekable-ranges": [{"start": 0, "end": 20}]})
        self.observe("time-pos", 14)
        self.assertEqual(self.socket.commands[-1]["command"], ["set_property", "pause", True])
        self.player.seek_buffer(115)
        self.assertTrue(self.player._pending.paused)

    def test_finished_buffer_seek_does_not_expose_stale_implementation_pause(self):
        self.buffered()
        self.player.seek_buffer(111)
        self.finish_replacement(1, 2)
        self.observe("pause", True)
        self.observe("demuxer-cache-state", {"seekable-ranges": [{"start": 0, "end": 20}]})
        self.observe("time-pos", 11)
        self.assertEqual(self.socket.commands[-1]["command"], ["set_property", "pause", False])
        # A new key event can beat MPV's acknowledgement of pause=no.
        self.player.seek_buffer(112)
        self.assertFalse(self.player._pending.paused)

    def test_native_open_detaches_recorder_and_disables_old_buffer_controls(self):
        recorder = self.buffered()
        self.feed({"event": "end-file", "playlist_entry_id": 1, "reason": "stop"})
        self.assertTrue(recorder.stopped)
        self.feed({"event": "start-file", "playlist_entry_id": 50}, {"event": "file-loaded"})
        self.reply(["get_property", "playlist"], [{"id": 50, "filename": "/music/native.flac"}])
        self.assertEqual(self.player._active.url, "/music/native.flac")
        self.assertFalse(self.player._active.buffered)
        self.assertEqual(self.player._active.content_id, "")
        self.observe("duration", 80)
        self.observe("time-pos", 5)
        start = len(self.socket.commands)
        self.player.go_live()
        self.feed({"event": "client-message", "args": ["qitv", "buffer-seek", "112", "no"]})
        self.player._recording_updated(recorder, {"start": 100.0, "end": 500.0})
        self.player._recording_finished(recorder)
        self.assertIsNone(self.player._buffer_source)
        self.assertIsNone(self.player._pending)
        self.assertEqual(self.player._active.entry_id, 50)
        self.assertFalse(any(c["command"][0] == "loadfile" for c in self.socket.commands[start:]))
        published = [
            c["command"][2]
            for c in self.socket.commands
            if c["command"][:2] == ["set_property", "user-data/qitv-timeshift"]
        ]
        self.assertFalse(published[-1]["active"])
        self.feed({"event": "end-file", "playlist_entry_id": 50, "reason": "eof"})
        self.assertEqual(self.ends, [])
        self.assertEqual(self.positions, [])  # Neither native file nor live stream is VOD resume.

    def test_native_start_overtakes_buffer_snapshot_and_old_end(self):
        recorder = self.buffered()
        old_observers = dict(self.player._active.observers)
        self.player.seek_buffer(111)
        self.feed({"event": "start-file", "playlist_entry_id": 50}, {"event": "file-loaded"})
        self.reply(["get_property", "playlist"], [{"id": 50, "filename": "/video/native.mkv"}])
        start = len(self.socket.commands)
        self.reply(["get_property", "time-pos"], 12)
        self.reply(["get_property", "duration"], 100)
        self.feed({"event": "end-file", "playlist_entry_id": 1, "reason": "stop"})
        for observer, name in old_observers.items():
            self.feed({"event": "property-change", "id": observer, "name": name, "data": 99})
        self.assertTrue(recorder.stopped)
        self.assertIsNone(self.player._pending)
        self.assertEqual(self.player._active.url, "/video/native.mkv")
        self.assertEqual(self.player._active.position, 0)
        self.assertFalse(
            any(
                c["command"][0] == "loadfile"
                or c["command"][:3] == ["script-message-to", "qitv", "stop"]
                for c in self.socket.commands[start:]
            )
        )

    def test_native_choice_after_stop_request_wins_over_pending_reader(self):
        self.buffered()
        self.player.seek_buffer(111)
        self.reply(["get_property", "time-pos"], 12)
        self.reply(["get_property", "duration"], 100)
        self.stopped_reply(1)
        self.feed({"event": "end-file", "playlist_entry_id": 1, "reason": "stop"})
        self.reply(["get_property", "idle-active"], False)
        self.assertIsNone(self.player._active)
        self.assertIsNotNone(self.player._pending)
        self.feed({"event": "start-file", "playlist_entry_id": 50}, {"event": "file-loaded"})
        self.reply(["get_property", "idle-active"], True)  # Stale end-file query.
        self.assertEqual(self.player._active.entry_id, 50)
        self.assertIsNone(self.player._pending)
        loads = [c["command"][1] for c in self.socket.commands if c["command"][0] == "loadfile"]
        self.assertEqual(loads, ["https://provider.invalid/live"])

    def test_start_before_load_reply_uses_returned_entry_identity(self):
        self.player.play("/music/owned.flac", content_id="owned", is_live=False)
        self.feed({"event": "start-file", "playlist_entry_id": 7}, {"event": "file-loaded"})
        self.assertFalse(self.player._active.loaded)
        self.reply(["loadfile"], {"playlist_entry_id": 7})
        self.assertTrue(self.player._active.loaded)
        self.assertEqual(self.player._active.content_id, "owned")
        self.feed({"event": "end-file", "playlist_entry_id": 7, "reason": "eof"})
        self.assertEqual(self.ends, ["owned"])

    def test_native_start_before_load_reply_is_not_misattributed_to_owned_media(self):
        self.player.play("/music/owned.flac", content_id="owned", is_live=False)
        self.feed({"event": "start-file", "playlist_entry_id": 8}, {"event": "file-loaded"})
        self.reply(["loadfile"], {"playlist_entry_id": 7})
        self.reply(["get_property", "playlist"], [{"id": 8, "filename": "/music/native.flac"}])
        self.assertEqual(self.player._active.url, "/music/native.flac")
        self.assertEqual(self.player._active.content_id, "")
        self.feed({"event": "end-file", "playlist_entry_id": 7, "reason": "eof"})
        self.assertEqual(self.player._active.entry_id, 8)
        self.assertEqual(self.ends, [])

    def test_playlist_redirect_retains_owned_identity_for_first_resolved_file(self):
        self.player.play("/music/list.m3u", content_id="playlist", is_live=False)
        self.reply(["loadfile"], {"playlist_entry_id": 7})
        self.feed(
            {"event": "start-file", "playlist_entry_id": 7},
            {
                "event": "end-file",
                "playlist_entry_id": 7,
                "reason": "redirect",
                "playlist_insert_id": 8,
                "playlist_insert_num_entries": 2,
            },
            {"event": "start-file", "playlist_entry_id": 8},
            {"event": "file-loaded"},
        )
        self.assertEqual(self.player._active.content_id, "playlist")
        self.feed({"event": "end-file", "playlist_entry_id": 8, "reason": "eof"})
        self.assertEqual(self.ends, ["playlist"])
        self.feed({"event": "start-file", "playlist_entry_id": 9}, {"event": "file-loaded"})
        self.assertEqual(self.player._active.content_id, "")

    def test_audio_control_window_survives_replacement_until_actual_idle(self):
        self.player.play("/music/audio.flac", is_live=False)
        self.reply(["loadfile"], {"playlist_entry_id": 1})
        self.feed({"event": "start-file", "playlist_entry_id": 1}, {"event": "file-loaded"})
        start = self.timeline.index(("command", ["set_property", "force-window", "yes"]))
        load = next(i for i, row in enumerate(self.timeline) if row[1][0] == "loadfile")
        self.assertLess(start, load)
        self.player.play("/music/second.flac", is_live=False)
        self.finish_replacement(1, 2)
        self.assertFalse(
            any(
                c["command"] == ["set_property", "force-window", "no"] for c in self.socket.commands
            )
        )
        self.feed({"event": "end-file", "playlist_entry_id": 2, "reason": "eof"})
        self.assertNotEqual(
            self.socket.commands[-1]["command"], ["set_property", "force-window", "no"]
        )
        self.feed({"event": "property-change", "id": 0, "data": True})
        self.assertEqual(
            self.socket.commands[-1]["command"], ["set_property", "force-window", "no"]
        )


if __name__ == "__main__":
    unittest.main()
