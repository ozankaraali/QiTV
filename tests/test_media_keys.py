"""The system's media keys (2.8.13): macOS gives them only to the app it lists as Now Playing;
QiTV lists itself and acts on each as on the keyboard's key."""

import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

import services.media_keys as media_keys
from services.media_keys import MediaKeys
from timer_fixtures import wait_until

app = QApplication.instance() or QApplication([])
_K = Qt.Key


class FakeSystem:
    def __init__(self):
        self.calls = []

    def listen(self):
        self.calls.append("listen")

    def now_playing(self, state, title, live):
        self.calls.append((state, title, live))

    def close(self):
        self.calls.append("close")


class MediaKeysTests(unittest.TestCase):
    def setUp(self):
        self.system = FakeSystem()
        self.keys = MediaKeys(system=self.system)
        self.addCleanup(self.keys.stop)
        self.pressed = []
        self.keys.pressed.connect(self.pressed.append)

    def test_a_command_is_the_keyboards_media_key(self):
        self.assertTrue(self.keys.start())
        self.assertEqual(self.system.calls, ["listen"])
        for name in ("toggle", "next", "previous", "stop"):
            self.assertTrue(media_keys._on_command(name))
        self.assertEqual(
            self.pressed,
            [
                _K.Key_MediaTogglePlayPause.value,
                _K.Key_MediaNext.value,
                _K.Key_MediaPrevious.value,
                _K.Key_MediaStop.value,
            ],
        )
        self.assertFalse(media_keys._on_command("rate"))  # none of QiTV's

    def test_play_and_pause_act_only_when_they_change_something(self):
        self.keys.start()
        self.keys.show("playing", "One", True)
        self.assertTrue(media_keys._on_command("play"))  # it plays already
        self.assertEqual(self.pressed, [])
        self.assertTrue(media_keys._on_command("pause"))
        self.assertEqual(self.pressed, [_K.Key_MediaTogglePlayPause.value])
        self.keys.show("paused", "One", True)
        self.assertTrue(media_keys._on_command("pause"))  # headphones off while paused
        self.assertEqual(len(self.pressed), 1)
        self.assertTrue(media_keys._on_command("play"))
        self.assertEqual(len(self.pressed), 2)

    def test_the_system_hears_what_plays_once_per_change(self):
        self.keys.start()
        for state in ("opening", "buffering", "playing", "playing"):
            self.keys.show(state, "One", True)  # a stream opening still plays
        self.keys.show("paused", "One", True)
        self.keys.show("ended", "One", True)  # nothing plays: no title either
        self.keys.show("error", "Two", False)
        self.keys.show("playing", "A film", False)
        self.assertEqual(
            self.system.calls[1:],
            [("playing", "One", True), ("paused", "One", True), ("", "", False)]
            + [("playing", "A film", False)],
        )

    def test_stopping_leaves_now_playing_and_hears_no_more(self):
        self.keys.start()
        self.keys.stop()
        self.assertEqual(self.system.calls[-1], "close")
        self.assertFalse(media_keys._on_command("toggle"))
        self.assertEqual(self.pressed, [])

    def test_nothing_listens_without_macos_windows(self):
        keys = MediaKeys()  # offscreen here: no system to listen to
        self.assertFalse(keys.start())
        keys.show("playing", "One", True)  # nothing to tell, nothing breaks
        keys.stop()

    def test_a_system_that_fails_to_listen_leaves_the_keys_as_they_were(self):
        class Failing(FakeSystem):
            def listen(self):
                raise OSError("no command center")

        keys = MediaKeys(system=Failing())
        self.assertFalse(keys.start())
        self.assertFalse(media_keys._on_command("toggle"))


@unittest.skipUnless(
    sys.platform == "darwin" and os.environ.get("QITV_TEST_NOW_PLAYING") == "1",
    "lists this process as Now Playing for a moment (QITV_TEST_NOW_PLAYING=1 on a Mac)",
)
class MacNowPlayingTests(unittest.TestCase):
    """The Objective-C calls themselves; paused, so nothing playing elsewhere is taken over."""

    def test_listed_shown_pressed_and_left(self):
        system = media_keys._MacNowPlaying()
        keys = MediaKeys(system=system)
        pressed = []
        keys.pressed.connect(pressed.append)
        self.assertTrue(keys.start())
        try:
            keys.show("paused", "Kanal é", True)
            self.assertEqual(system.playback_state(), 2)  # MPNowPlayingPlaybackStatePaused
            self.assertEqual(system.shown_title(), "Kanal é")
            self.assertEqual(system.press(b"qitvToggle:"), 0)  # handled
            self.assertEqual(system.press(b"qitvNext:"), 0)
            self.assertEqual(pressed, [_K.Key_MediaTogglePlayPause.value, _K.Key_MediaNext.value])
        finally:
            keys.stop()
        self.assertEqual(system.playback_state(), 3)  # stopped
        self.assertEqual(system.shown_title(), "")


from tests.test_tv_window import WindowCase  # noqa: E402


class WindowMediaKeysTests(WindowCase):
    def test_a_media_command_acts_as_the_keyboards_key_and_the_system_hears_the_channel(self):
        system = FakeSystem()
        self.window.media_keys._system = system  # as on macOS
        self.open_first_channel()
        zap = self.window.zap
        self.assertTrue(wait_until(lambda: zap.context.current() is not None))
        self.window.session.stateChanged.emit("playing")
        self.assertIn(("playing", "Channel 1", True), system.calls)
        self.window.overlays.drawer.close_drawer()
        self.window.media_keys.pressed.emit(_K.Key_MediaNext.value)  # ⏭: the next channel
        self.assertTrue(wait_until(lambda: zap.context.current()["name"] == "Channel 2"))


if __name__ == "__main__":
    unittest.main()
