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

    def report(self):
        return {}

    def simulate(self, name):
        return media_keys.on_command(name)


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
            self.assertTrue(media_keys.on_command(name))
        self.assertEqual(
            self.pressed,
            [
                _K.Key_MediaTogglePlayPause.value,
                _K.Key_MediaNext.value,
                _K.Key_MediaPrevious.value,
                _K.Key_MediaStop.value,
            ],
        )
        self.assertFalse(media_keys.on_command("rate"))  # none of QiTV's

    def test_play_and_pause_act_only_when_they_change_something(self):
        self.keys.start()
        self.keys.show("playing", "One", True)
        self.assertTrue(media_keys.on_command("play"))  # it plays already
        self.assertEqual(self.pressed, [])
        self.assertTrue(media_keys.on_command("pause"))
        self.assertEqual(self.pressed, [_K.Key_MediaTogglePlayPause.value])
        self.keys.show("paused", "One", True)
        self.assertTrue(media_keys.on_command("pause"))  # headphones off while paused
        self.assertEqual(len(self.pressed), 1)
        self.assertTrue(media_keys.on_command("play"))
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
        self.assertFalse(media_keys.on_command("toggle"))
        self.assertEqual(self.pressed, [])

    def test_nothing_listens_without_macos_windows(self):
        keys = MediaKeys()  # offscreen here: no system to listen to
        self.assertFalse(keys.start())
        keys.show("playing", "One", True)  # nothing to tell, nothing breaks
        keys.stop()

    def test_the_systems_copy_of_a_keyboard_press_is_that_press(self):
        # Windows, Linux: a focused window gets the key, and the system may send it on too.
        self.keys.start()
        self.keys.show("playing", "One", True)
        self.keys.keyboard(_K.Key_MediaTogglePlayPause.value)
        self.assertTrue(media_keys.on_command("pause"))
        self.assertTrue(media_keys.on_command("toggle"))
        self.assertEqual(self.pressed, [])  # the window acted on it already
        self.assertTrue(media_keys.on_command("next"))  # another key: its own press
        self.assertEqual(self.pressed, [_K.Key_MediaNext.value])
        self.keys._keyboard = (_K.Key_MediaTogglePlayPause.value, 0.0)  # long ago
        self.assertTrue(media_keys.on_command("pause"))
        self.assertEqual(len(self.pressed), 2)

    def test_what_plays_before_the_system_listens_is_told_once_it_does(self):
        self.keys.show("playing", "One", True)
        self.assertEqual(self.system.calls, [("playing", "One", True)])
        keys = MediaKeys(system=FakeSystem())
        self.addCleanup(keys.stop)
        keys.show("paused", "Two", False)
        keys._system.calls.clear()
        keys.start()
        self.assertEqual(keys._system.calls, ["listen", ("paused", "Two", False)])

    def test_the_desktop_asking_for_the_window_raises_it(self):
        raised = []
        self.keys.raiseRequested.connect(lambda: raised.append(1))
        self.keys.start()
        self.assertTrue(media_keys.on_command("raise"))
        self.assertEqual(raised, [1])

    def test_a_system_that_fails_to_listen_leaves_the_keys_as_they_were(self):
        class Failing(FakeSystem):
            def listen(self):
                raise OSError("no command center")

        keys = MediaKeys(system=Failing())
        self.assertFalse(keys.start())
        self.assertFalse(media_keys.on_command("toggle"))


@unittest.skipUnless(
    sys.platform == "darwin" and os.environ.get("QITV_TEST_NOW_PLAYING") == "1",
    "lists this process as Now Playing for a moment (QITV_TEST_NOW_PLAYING=1 on a Mac)",
)
class MacNowPlayingTests(unittest.TestCase):
    """The Objective-C calls themselves; paused, so nothing playing elsewhere is taken over."""

    def test_listed_shown_pressed_and_left(self):
        from services.media_keys_mac import MacNowPlaying

        system = MacNowPlaying()
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


class WindowsHandlerTests(unittest.TestCase):
    """The COM objects of QiTV's own (Windows's ButtonPressed handler and, for the smoke test,
    its event) run anywhere: Windows's calling convention is C's off Windows."""

    def test_each_button_is_its_command(self):
        import ctypes

        import services.media_keys_windows as windows

        system = FakeSystem()
        keys = MediaKeys(system=system)
        self.addCleanup(keys.stop)
        pressed = []
        keys.pressed.connect(pressed.append)
        keys.start()
        keys.show("playing", "One", True)
        handler = windows._handler()
        table = ctypes.cast(handler, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        invoke = windows._INVOKE(table[3])
        for button in (1, 0, 6, 7, 2, 9):  # Pause, Play (it plays), Next, Previous, Stop, Ch-
            self.assertEqual(invoke(handler, None, windows._pressed_event(button)), 0)
        self.assertEqual(
            pressed,
            [
                _K.Key_MediaTogglePlayPause.value,
                _K.Key_MediaNext.value,
                _K.Key_MediaPrevious.value,
                _K.Key_MediaStop.value,
            ],
        )
        query, found = windows._QUERY(table[0]), ctypes.c_void_p()
        self.assertEqual(
            query(handler, ctypes.byref(windows.IID_BUTTON_HANDLER), ctypes.byref(found)), 0
        )
        self.assertEqual(found.value, handler)
        self.assertEqual(query(handler, ctypes.byref(windows.IID_IAGILE), ctypes.byref(found)), 0)
        self.assertNotEqual(query(handler, ctypes.byref(windows.IID_SMTC), ctypes.byref(found)), 0)
        self.assertIsNone(found.value)


def _session_bus():
    try:
        from PySide6.QtDBus import QDBusConnection
    except ImportError:
        return None
    bus = QDBusConnection.sessionBus()
    return bus if bus.isConnected() else None


@unittest.skipUnless(_session_bus() is not None, "no D-Bus session bus here")
class MprisTests(unittest.TestCase):
    """QiTV as an MPRIS player, asked through the bus as a desktop asks (a second connection)."""

    def setUp(self):
        from PySide6.QtDBus import QDBusConnection

        from services.media_keys_linux import Mpris

        self.keys = MediaKeys(system=Mpris())
        self.addCleanup(self.keys.stop)
        self.pressed = []
        self.keys.pressed.connect(self.pressed.append)
        self.assertTrue(self.keys.start())
        self.desktop = QDBusConnection.connectToBus(QDBusConnection.BusType.SessionBus, "desktop")
        self.addCleanup(QDBusConnection.disconnectFromBus, "desktop")

    def ask(self, interface, member, *arguments):
        from PySide6.QtDBus import QDBusMessage, QDBusPendingCallWatcher

        service = self.keys.system.report()["service"]
        call = QDBusMessage.createMethodCall(service, "/org/mpris/MediaPlayer2", interface, member)
        call.setArguments(list(arguments))
        watcher = QDBusPendingCallWatcher(self.desktop.asyncCall(call))
        self.assertTrue(wait_until(watcher.isFinished))
        reply = watcher.reply()
        self.assertNotEqual(
            reply.type(), QDBusMessage.MessageType.ErrorMessage, reply.errorMessage()
        )
        return reply

    def test_the_desktop_reads_what_plays_and_presses_the_keys(self):
        properties, player = "org.freedesktop.DBus.Properties", "org.mpris.MediaPlayer2.Player"
        self.keys.show("playing", "Kanal é", True)
        # The types MPRIS gives (PySide's createReply(list) made both "av").
        self.assertEqual(self.ask(properties, "GetAll", player).signature(), "a{sv}")
        self.assertEqual(self.ask(properties, "Get", player, "PlaybackStatus").signature(), "v")
        self.ask(player, "Next")
        self.ask(player, "PlayPause")
        self.assertTrue(wait_until(lambda: len(self.pressed) == 2))
        self.assertEqual(self.pressed, [_K.Key_MediaNext.value, _K.Key_MediaTogglePlayPause.value])


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
