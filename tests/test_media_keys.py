"""The system's media keys (2.8.13): macOS gives them only to the app it lists as Now Playing;
QiTV lists itself and acts on each as on the keyboard's key. Its media panel shows a song's
artist, album and cover (Plan 4): each system's calls are faked here, as Windows's COM objects
run anywhere."""

import ctypes
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication
from timer_fixtures import wait_until

import services.media_keys as media_keys
from services.media_keys import MediaKeys

app = QApplication.instance() or QApplication([])
_K = Qt.Key


class FakeSystem:
    def __init__(self):
        self.calls = []
        self.shown = []  # each now_playing with its six values

    def listen(self):
        self.calls.append("listen")

    def now_playing(self, state, title, live, *, artist="", album="", art_path=""):
        self.calls.append((state, title, live))
        self.shown.append((state, title, live, artist, album, art_path))

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

    def test_show_dedups_on_all_fields(self):
        # A song's artist, album and cover come after its title (its tags, its cover's file):
        # each is news to the system; the same six values again are not.
        song = dict(artist="Wexlo Tide", album="Harbour Lights", art_path="/covers/cover-1.jpg")
        self.keys.show("playing", "Pell", False, artist="Wexlo Tide")  # told before it listens
        self.keys.start()
        self.keys.show("playing", "Pell", False, artist="Wexlo Tide")
        self.keys.show("playing", "Pell", False, artist="Wexlo Tide", album="Harbour Lights")
        self.keys.show("buffering", "Pell", False, **song)
        self.keys.show("playing", "Pell", False, **song)
        self.keys.show("playing", "Pell", False, **{**song, "art_path": "/covers/cover-2.jpg"})
        self.keys.show("paused", "Pell", False, **{**song, "art_path": "/covers/cover-2.jpg"})
        self.keys.show("ended", "Pell", False, **song)  # nothing plays: none of them either
        self.keys.show("error", "Rook", False, artist="Wexlo Tide")
        self.assertEqual(
            self.system.shown,
            [
                ("playing", "Pell", False, "Wexlo Tide", "", ""),
                ("playing", "Pell", False, "Wexlo Tide", "", ""),  # once it listens
                ("playing", "Pell", False, "Wexlo Tide", "Harbour Lights", ""),
                ("playing", "Pell", False, *song.values()),
                ("playing", "Pell", False, "Wexlo Tide", "Harbour Lights", "/covers/cover-2.jpg"),
                ("paused", "Pell", False, "Wexlo Tide", "Harbour Lights", "/covers/cover-2.jpg"),
                ("", "", False, "", "", ""),
            ],
        )


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
            keys.show("paused", "Pell", False, artist="Wexlo Tide", album="Harbour Lights")
            self.assertEqual(system.shown_title(), "Pell")
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


# The Now Playing keys as the fake Objective-C runtime names them.
_MAC_KEYS = {11: "title", 12: "live", 13: "type", 14: "artist", 15: "album", 16: "artwork"}


class FakeObjC:
    """The Objective-C runtime as MacNowPlaying sends to it, anywhere: each object made is
    remembered by its id, as what it holds."""

    CLASSES = {
        b"NSString": 1,
        b"NSNumber": 2,
        b"NSDictionary": 3,
        b"NSImage": 4,
        b"MPMediaItemArtwork": 5,
    }

    def __init__(self, *, artwork=True):
        self.artwork = artwork  # MPMediaItemArtwork answers initWithImage:
        self.objects: dict = {}
        self.read: list[str] = []  # the files an NSImage was made of
        self.released: list = []
        self.info = None  # what Now Playing was given (a dict, None for none)
        self.state = None
        self._next = 100

    def objc_getClass(self, name):
        return self.CLASSES.get(name, 0)

    def sel_registerName(self, name):
        return name

    def objc_autoreleasePoolPush(self):
        return 9

    def objc_autoreleasePoolPop(self, pool):
        pass

    def _made(self, value):
        self._next += 1
        self.objects[self._next] = value
        return self._next

    def send(self, receiver, selector, restype=ctypes.c_void_p, *args):
        values = [v for _t, v in args]
        if selector == b"stringWithUTF8String:":
            return self._made(values[0].decode("utf-8"))
        if selector in (b"numberWithBool:", b"numberWithUnsignedInteger:"):
            return self._made(values[0])
        if selector == b"dictionaryWithObjects:forKeys:count:":
            count = values[2]
            objects = (ctypes.c_void_p * count).from_address(values[0].value)
            keys = (ctypes.c_void_p * count).from_address(values[1].value)
            return self._made(
                {_MAC_KEYS[k]: self.objects[o] for k, o in zip(list(keys), list(objects))}
            )
        if selector == b"setNowPlayingInfo:":
            self.info = self.objects[values[0]] if values[0] else None
        elif selector == b"setPlaybackState:":
            self.state = values[0]
        elif selector == b"instancesRespondToSelector:":
            return receiver == 5 and values[0] == b"initWithImage:" and self.artwork
        elif selector == b"alloc":
            return self._made(("alloc", receiver))
        elif selector == b"initWithContentsOfFile:":
            path = self.objects[values[0]]
            self.read.append(path)
            self.objects[receiver] = ("image", path)
            return receiver
        elif selector == b"initWithImage:":
            if not self.artwork:
                raise AssertionError("initWithImage: sent where it isn't answered")
            self.objects[receiver] = ("artwork", self.objects[values[0]])
            return receiver
        elif selector == b"release":
            self.released.append(self.objects[receiver])
        return None


def fake_mac(objc):
    """A MacNowPlaying over `objc`, made as its __init__ makes it on a Mac."""
    from services.media_keys_mac import MacNowPlaying

    system = MacNowPlaying.__new__(MacNowPlaying)
    system._objc, system._send = objc, objc.send
    system._center, system._info, system._target = 7, 8, None
    keys = {name: key for key, name in _MAC_KEYS.items()}
    system._title_key, system._live_key, system._type_key = (
        keys["title"],
        keys["live"],
        keys["type"],
    )
    system._artist_key, system._album_key = keys["artist"], keys["album"]
    system._artwork_key = keys["artwork"]
    system._artwork = ("", None)
    system._artwork_answered = None
    return system


class MacNowPlayingInfoTests(unittest.TestCase):
    """What Now Playing is given, over a fake Objective-C runtime (anywhere)."""

    def test_mac_sets_artist_album(self):
        objc = FakeObjC()
        system = fake_mac(objc)
        song = dict(artist="Wexlo Tide", album="Harbour Lights", art_path="/covers/cover-1.jpg")
        system.now_playing("playing", "Pell", False, **song)
        artwork = ("artwork", ("image", "/covers/cover-1.jpg"))
        self.assertEqual(
            objc.info,
            {
                "title": "Pell",
                "live": False,
                "type": 1,  # MPNowPlayingInfoMediaTypeAudio
                "artist": "Wexlo Tide",
                "album": "Harbour Lights",
                "artwork": artwork,
            },
        )
        self.assertEqual(objc.state, 1)
        self.assertEqual(objc.released, [("image", "/covers/cover-1.jpg")])  # the artwork has it
        system.now_playing("paused", "Pell", False, **song)  # its cover isn't read again
        self.assertEqual(objc.read, ["/covers/cover-1.jpg"])
        self.assertEqual(objc.info["artwork"], artwork)
        # A film: its title alone, as before; the song's artwork goes.
        system.now_playing("playing", "A film", False)
        self.assertEqual(objc.info, {"title": "A film", "live": False, "type": 2})
        self.assertIn(artwork, objc.released)
        # A song with no tags and no cover: its title alone too.
        system.now_playing("playing", "Lone", False)
        self.assertEqual(objc.info["type"], 2)
        system.now_playing("", "", False)
        self.assertIsNone(objc.info)
        self.assertEqual(objc.state, 3)

    def test_mac_without_artwork_by_image_shows_the_song_and_says_so_once(self):
        # A macOS whose MPMediaItemArtwork no longer answers initWithImage: (only the block
        # initializer, which ctypes can't make): no artwork, said once in the log.
        objc = FakeObjC(artwork=False)
        system = fake_mac(objc)
        with self.assertLogs("services.media_keys_mac", "DEBUG") as logs:
            system.now_playing("playing", "Pell", False, artist="W", art_path="/c/cover-1.jpg")
            system.now_playing("playing", "Rook", False, artist="W", art_path="/c/cover-2.jpg")
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(objc.info, {"title": "Rook", "live": False, "type": 1, "artist": "W"})
        self.assertEqual(objc.read, [])


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


class FakeWindowsRuntime:
    """The Windows Runtime as WindowsMediaControls calls it, anywhere: combase's strings and
    activation factories, and COM objects of the test's own whose methods log their calls (an
    HSTRING as its text, an object as its name)."""

    def __init__(self, *, thumbnails=True):
        import services.media_keys_windows as windows

        self.windows = windows
        self.thumbnails = thumbnails  # CreateFromUri answers
        self.log: list[tuple] = []
        self.released: list[str] = []
        self.strings: dict[int, str] = {}  # an HSTRING's handle: its text
        self.deleted: list[int] = []
        self.names: dict[int, str] = {}
        self._kept: list = []
        hstring, pointer, out = "s", "p", "o"
        self.album = self.com("album", {7: ("put_AlbumTitle", [hstring])})
        self.music = self.com(
            "music",
            {7: ("music put_Title", [hstring]), 11: ("put_Artist", [hstring])},
            interfaces={bytes(windows.IID_MUSIC_2): self.album},
        )
        self.video = self.com("video", {7: ("video put_Title", [hstring])})
        self.updater = self.com(
            "updater",
            {
                7: ("put_Type", ["i"]),
                11: ("put_Thumbnail", [pointer]),
                12: ("get_MusicProperties", [out], self.music),
                17: ("Update", []),
            },
        )
        self.smtc = self.com(
            "smtc", {7: ("put_PlaybackStatus", ["i"]), 11: ("put_IsEnabled", ["b"])}
        )
        self.uri = self.com("uri", {})
        self.reference = self.com("reference", {})
        self.factories = {
            "Windows.Foundation.Uri": self.com(
                "uri factory", {6: ("CreateUri", [hstring, out], self.uri)}
            ),
            "Windows.Storage.Streams.RandomAccessStreamReference": self.com(
                "stream reference statics",
                {7: ("CreateFromUri", [pointer, out], self.reference)},
            ),
        }
        self.combase = SimpleNamespace(
            WindowsCreateString=self._create_string,
            WindowsDeleteString=self._delete_string,
            RoGetActivationFactory=self._factory,
        )

    _TYPES = {
        "s": ctypes.c_void_p,
        "p": ctypes.c_void_p,
        "i": ctypes.c_int32,
        "b": ctypes.c_ubyte,
        "o": ctypes.POINTER(ctypes.c_void_p),
    }

    def com(self, name, methods, *, interfaces=None):
        """A COM object: `methods` maps a vtable slot to (its name in the log, its arguments'
        kinds, and for an out pointer the object it gives); any other slot fails."""
        w = self.windows

        def query(this, iid, out):
            found = (interfaces or {}).get(bytes(iid.contents))
            out[0] = found
            return w.S_OK if found else w.E_NOINTERFACE

        def release(_this):
            self.released.append(name)
            return 1

        entries = [w._QUERY(query), w._COUNT(lambda _this: 1), w._COUNT(release)]
        for slot in range(3, max([5, *methods]) + 1):
            if slot in methods:
                entries.append(self._method(*methods[slot]))
            else:
                entries.append(
                    w.WINFUNCTYPE(w.HRESULT, ctypes.c_void_p)(
                        lambda _this, slot=slot: self.log.append((name, "slot", slot)) or -1
                    )
                )
        table = (ctypes.c_void_p * len(entries))(
            *(ctypes.cast(e, ctypes.c_void_p) for e in entries)
        )
        holder = (ctypes.c_void_p * 1)(ctypes.cast(table, ctypes.c_void_p))
        self._kept += [entries, table, holder]
        address = ctypes.addressof(holder)
        self.names[address] = name
        return address

    def _method(self, label, kinds, gives=None):
        w = self.windows

        def method(_this, *args):
            if label == "CreateFromUri" and not self.thumbnails:
                self.log.append((label, "failed"))
                return -2147024894  # 0x80070002: not found
            shown = []
            for kind, value in zip(kinds, args):
                if kind == "o":
                    value[0] = gives
                elif kind == "s":
                    shown.append(self.strings.get(value or 0, ""))  # NULL: the empty string
                elif kind == "p":
                    shown.append(self.names.get(value or 0, value))
                else:
                    shown.append(value)
            self.log.append((label, *shown))
            return w.S_OK

        types = [self._TYPES[k] for k in kinds]
        return w.WINFUNCTYPE(w.HRESULT, ctypes.c_void_p, *types)(method)

    def _create_string(self, text, units, out):
        handle = 0x5000 + len(self.strings) + 1
        self.strings[handle] = text
        out._obj.value = handle
        return 0

    def _delete_string(self, handle):
        self.deleted.append(getattr(handle, "value", handle))
        return 0

    def _factory(self, name, iid, out):
        out._obj.value = self.factories[self.strings[name.value]]
        return 0

    def controls(self):
        """WindowsMediaControls over these, listening as WindowsMediaControls.listen leaves it."""
        controls = self.windows.WindowsMediaControls(1, combase=self.combase)
        controls._smtc, controls._updater, controls._video = self.smtc, self.updater, self.video
        return controls


class WindowsDisplayTests(unittest.TestCase):
    """What the window's media controls show, over COM objects of the test's own (anywhere)."""

    def test_windows_sets_music_properties_and_thumbnail(self):
        runtime = FakeWindowsRuntime()
        controls = runtime.controls()
        cover = str(Path(os.sep, "covers", "cover-1.jpg").absolute())
        song = dict(artist="Wexlo Tide", album="Harbour Lights", art_path=cover)
        controls.now_playing("playing", "Pell", False, **song)
        self.assertEqual(
            runtime.log,
            [
                ("get_MusicProperties",),
                ("put_Type", 1),  # MediaPlaybackType.Music
                ("music put_Title", "Pell"),
                ("put_Artist", "Wexlo Tide"),
                ("put_AlbumTitle", "Harbour Lights"),
                ("CreateUri", Path(cover).as_uri()),
                ("CreateFromUri", "uri"),
                ("put_Thumbnail", "reference"),
                ("Update",),
                ("put_PlaybackStatus", 3),
                ("put_IsEnabled", 1),
            ],
        )
        for made in ("uri", "reference", "uri factory", "stream reference statics"):
            self.assertIn(made, runtime.released)  # the updater keeps its own reference
        runtime.log.clear()
        controls.now_playing("paused", "Pell", False, **song)  # the same cover: made once
        self.assertNotIn(("CreateUri", Path(cover).as_uri()), runtime.log)
        self.assertIn(("put_PlaybackStatus", 4), runtime.log)
        # A film: the video properties' title alone, as before, and no thumbnail.
        runtime.log.clear()
        controls.now_playing("playing", "A film", False)
        self.assertEqual(
            runtime.log,
            [
                ("put_Type", 2),
                ("video put_Title", "A film"),
                ("put_Thumbnail", None),
                ("Update",),
                ("put_PlaybackStatus", 3),
                ("put_IsEnabled", 1),
            ],
        )
        self.assertEqual(sorted(runtime.deleted), sorted(runtime.strings))  # every HSTRING

    def test_windows_without_a_thumbnail_still_shows_the_song_and_says_so_once(self):
        runtime = FakeWindowsRuntime(thumbnails=False)
        controls = runtime.controls()
        covers = [str(Path(os.sep, "covers", f"cover-{n}.jpg").absolute()) for n in (1, 2)]
        with self.assertLogs("services.media_keys_windows", "DEBUG") as logs:
            controls.now_playing("playing", "Pell", False, artist="W", art_path=covers[0])
            controls.now_playing("playing", "Rook", False, artist="W", art_path=covers[1])
        self.assertEqual(len(logs.records), 1)
        self.assertIn(("CreateFromUri", "failed"), runtime.log)
        self.assertIn(("put_Artist", "W"), runtime.log)
        self.assertEqual(runtime.log.count(("Update",)), 2)
        self.assertNotIn("put_Thumbnail", [entry[0] for entry in runtime.log])


def _has_qt_dbus():
    try:
        import PySide6.QtDBus  # noqa: F401
    except ImportError:
        return False
    return True


class FakeBus:
    """A session bus that takes everything and sends nothing on (Mpris's connection)."""

    def __init__(self):
        self.sent = []

    def isConnected(self):
        return True

    def send(self, message):
        self.sent.append(message)
        return True

    def registerVirtualObject(self, path, player):
        return True

    def registerService(self, name):
        return True

    def unregisterService(self, name):
        return True

    def unregisterObject(self, path):
        pass


@unittest.skipUnless(_has_qt_dbus(), "no Qt D-Bus module here")
class MprisMetadataTests(unittest.TestCase):
    """What a desktop reads of what plays (no session bus needed: Mpris's own answers)."""

    def test_mpris_metadata_has_artist_album_art(self):
        from services.media_keys_linux import PLAYER, Mpris

        bus = FakeBus()
        mpris = Mpris(connection=bus)
        cover = str(Path(os.sep, "covers", "cover 1.jpg").absolute())
        mpris.now_playing(
            "playing", "Pell", False, artist="Wexlo Tide", album="Harbour Lights", art_path=cover
        )
        metadata = mpris._all(PLAYER)["Metadata"]
        self.assertEqual(metadata["xesam:title"], "Pell")
        self.assertEqual(metadata["xesam:artist"], ["Wexlo Tide"])  # MPRIS: a list of names
        self.assertEqual(metadata["xesam:album"], "Harbour Lights")
        self.assertEqual(metadata["mpris:artUrl"], Path(cover).as_uri())
        self.assertIn("%20", metadata["mpris:artUrl"])
        changed = bus.sent[-1].arguments()[1]  # PropertiesChanged says so too
        self.assertEqual(changed["Metadata"]["xesam:album"], "Harbour Lights")
        # A film (or a song with none of them): its title alone, as before.
        mpris.now_playing("playing", "A film", False)
        self.assertEqual(set(mpris._all(PLAYER)["Metadata"]), {"mpris:trackid", "xesam:title"})
        mpris.now_playing("", "", False)
        self.assertEqual(set(mpris._all(PLAYER)["Metadata"]), {"mpris:trackid"})


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


class PanelCoverTests(unittest.TestCase):
    """The cover the system's media panel shows: the audio screen's, as a small file written
    on a thread, one at a time, gone when QiTV closes (ui.song_jobs.PanelCover)."""

    def test_the_panels_cover_is_a_small_file_of_its_own_one_at_a_time(self):
        from ui.song_jobs import PANEL_SIDE, PanelCover

        cover = PanelCover()
        self.addCleanup(cover.close)
        written = []
        cover.written.connect(lambda: written.append(1))
        photo = QImage(1200, 800, QImage.Format.Format_RGB32)
        photo.fill(QColor("#d8653f"))
        self.assertEqual(cover.path_for(photo), "")  # its file is written on a thread first
        self.assertTrue(wait_until(lambda: cover.path_for(photo)))
        first = cover.path_for(photo)
        self.assertEqual(written, [1])
        saved = QImage(first)
        self.assertEqual((saved.width(), saved.height()), (PANEL_SIDE, PANEL_SIDE * 2 // 3))
        self.assertTrue(first.endswith(".jpg"))
        folder = os.path.dirname(first)
        self.assertEqual(os.listdir(folder), [os.path.basename(first)])
        self.assertEqual(cover.path_for(photo), first)  # the same cover: the same file
        # A logo with see-through parts: a PNG, a file of its own (a panel that keeps a picture
        # by its file's name sees a new one), the first gone.
        logo = QImage(64, 64, QImage.Format.Format_ARGB32)
        logo.fill(Qt.GlobalColor.transparent)
        self.assertEqual(cover.path_for(logo), "")
        self.assertTrue(wait_until(lambda: cover.path_for(logo)))
        second = cover.path_for(logo)
        self.assertNotEqual(second, first)
        self.assertTrue(second.endswith(".png"))
        self.assertEqual(QImage(second).size(), logo.size())  # never made larger
        self.assertEqual(os.listdir(folder), [os.path.basename(second)])
        self.assertEqual(cover.path_for(None), "")  # QiTV's drawn stand-in: none
        # Asked for one, then another before the first is written: the newer one is the file.
        third = QImage(300, 300, QImage.Format.Format_RGB32)
        third.fill(QColor("#203040"))
        cover.path_for(photo.copy())
        self.assertEqual(cover.path_for(third), "")
        self.assertTrue(wait_until(lambda: cover.path_for(third)))
        self.assertEqual(os.listdir(folder), [os.path.basename(cover.path_for(third))])
        cover.close()  # QiTV closes: the folder goes
        self.assertFalse(os.path.exists(folder))
        self.assertEqual(cover.path_for(photo), "")


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

    def test_the_panel_hears_the_audio_screen_as_it_changes(self):
        # Task 8: a song's facts and cover reach its screen after its state, and the screen
        # goes when a picture shows, with no state change: the panel hears each, its cover as
        # a file of its own once written (no libVLC: the controller's answers are the test's).
        window, screen = self.window, self.window.audio_screen
        system = FakeSystem()
        window.media_keys._system = system
        target = SimpleNamespace(title="02 Pell", live=False)
        facts = [("Pell", "Wexlo Tide", "Harbour Lights")]
        controller = window.controller
        with (
            mock.patch.object(controller, "now_playing", return_value=target),
            mock.patch.object(controller, "panel_facts", side_effect=lambda: facts[0]),
        ):
            window._show_now_playing("playing")
            song = ("playing", "Pell", False, "Wexlo Tide", "Harbour Lights")
            self.assertEqual(system.shown[-1], (*song, ""))
            cover = QImage(64, 64, QImage.Format.Format_RGB32)
            cover.fill(QColor("#d8653f"))
            screen.set_cover(cover, [])
            self.assertTrue(wait_until(lambda: bool(system.shown[-1][5])))
            art = system.shown[-1][5]
            self.assertEqual(system.shown[-1][:5], song)
            self.assertTrue(os.path.isfile(art))
            facts[0] = ("Rook", "Wexlo Tide", "Harbour Lights")
            screen.show_song("Rook", "Wexlo Tide", "Harbour Lights")
            self.assertTrue(wait_until(lambda: system.shown[-1][1] == "Rook"))
            self.assertEqual(system.shown[-1][5], art)  # the same cover: the same file
            facts[0] = None  # a picture shows: the screen goes, its facts with it
            screen.hide()
            self.assertTrue(
                wait_until(lambda: system.shown[-1] == ("playing", "02 Pell", False, "", "", ""))
            )


if __name__ == "__main__":
    unittest.main()
