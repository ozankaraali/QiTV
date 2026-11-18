"""The system's media keys (2.8.13): macOS gives them only to the app it lists as Now Playing;
QiTV lists itself and acts on each as on the keyboard's key. Its media panel shows a song's
artist, album and cover (Plan 4): each system's calls are faked here, as Windows's COM objects
run anywhere."""

import ctypes
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QIODevice, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication
from timer_fixtures import pause, wait_until

import services.media_keys as media_keys
from services.media_keys import MediaKeys

app = QApplication.instance() or QApplication([])
_K = Qt.Key


class FakeSystem:
    def __init__(self):
        self.calls = []
        self.shown = []  # each now_playing: state, title, live, artist, album, art_path, music
        self.art_bytes = []  # ... and the cover's bytes it was given

    def listen(self):
        self.calls.append("listen")

    def now_playing(
        self, state, title, live, *, artist="", album="", art_path="", art_bytes=b"", music=False
    ):
        self.calls.append((state, title, live))
        self.shown.append((state, title, live, artist, album, art_path, music))
        self.art_bytes.append(art_bytes)

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
        # each is news to the system, as music is; the same values again are not (the cover's
        # bytes go with its file).
        song = dict(artist="Wexlo Tide", album="Tidewick Isle", art_path="/covers/cover-1.jpg")
        self.keys.show("playing", "Pell", False, music=True)  # told before it listens
        self.keys.start()
        self.keys.show("playing", "Pell", False, music=True)
        self.keys.show("playing", "Pell", False)  # (not music: news)
        self.keys.show("playing", "Pell", False, artist="Wexlo Tide", music=True)
        self.keys.show("playing", "Pell", False, music=True, artist="W", album="Tidewick Isle")
        self.keys.show("buffering", "Pell", False, music=True, art_bytes=b"one", **song)
        self.keys.show("playing", "Pell", False, music=True, art_bytes=b"one", **song)
        two = {**song, "art_path": "/covers/cover-2.jpg"}
        self.keys.show("playing", "Pell", False, music=True, art_bytes=b"two", **two)
        self.keys.show("paused", "Pell", False, music=True, art_bytes=b"two", **two)
        self.keys.show("ended", "Pell", False, music=True, **song)  # nothing plays: nothing
        self.keys.show("error", "Rook", False, artist="Wexlo Tide", music=True)
        self.assertEqual(
            self.system.shown,
            [
                ("playing", "Pell", False, "", "", "", True),
                ("playing", "Pell", False, "", "", "", True),  # once it listens
                ("playing", "Pell", False, "", "", "", False),
                ("playing", "Pell", False, "Wexlo Tide", "", "", True),
                ("playing", "Pell", False, "W", "Tidewick Isle", "", True),
                ("playing", "Pell", False, *song.values(), True),
                ("playing", "Pell", False, *two.values(), True),
                ("paused", "Pell", False, *two.values(), True),
                ("", "", False, "", "", "", False),
            ],
        )
        self.assertEqual(self.system.art_bytes, [b""] * 5 + [b"one", b"two", b"two", b""])


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
            picture = QImage(8, 8, QImage.Format.Format_RGB32)
            picture.fill(QColor("#d8653f"))
            buffer = QBuffer()
            buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            picture.save(buffer, "PNG")
            keys.show(
                "paused",
                "Pell",
                False,
                artist="Wexlo Tide",
                album="Tidewick Isle",
                art_path="/qitv-test/cover-1.png",  # (never read: its bytes are)
                art_bytes=bytes(buffer.data().data()),
                music=True,
            )
            self.assertEqual(system.shown_title(), "Pell")
            self.assertTrue(system._artwork[1])  # an artwork made of the cover's bytes
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
        b"NSData": 6,
    }

    def __init__(self, *, artwork=True):
        self.artwork = artwork  # MPMediaItemArtwork answers initWithImage:
        self.objects: dict = {}
        self.read: list[bytes] = []  # the bytes an NSImage was made of
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
        elif selector == b"dataWithBytes:length:":
            data, length = values
            return self._made(("data", data[:length]))
        elif selector == b"initWithData:":
            data = self.objects[values[0]][1]
            self.read.append(data)
            if data.startswith(b"boom"):
                raise OSError("the runtime failed")
            if data.startswith(b"broken"):
                return None  # NSImage can't read it
            self.objects[receiver] = ("image", data)
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
    system._said = set()
    return system


class MacNowPlayingInfoTests(unittest.TestCase):
    """What Now Playing is given, over a fake Objective-C runtime (anywhere)."""

    def test_mac_sets_artist_album(self):
        objc = FakeObjC()
        system = fake_mac(objc)
        song = dict(
            artist="Wexlo Tide",
            album="Tidewick Isle",
            art_path="/covers/cover-1.jpg",
            art_bytes=b"jpeg one",
            music=True,
        )
        system.now_playing("playing", "Pell", False, **song)
        artwork = ("artwork", ("image", b"jpeg one"))  # made of the file's bytes, not the file
        self.assertEqual(
            objc.info,
            {
                "title": "Pell",
                "live": False,
                "type": 1,  # MPNowPlayingInfoMediaTypeAudio
                "artist": "Wexlo Tide",
                "album": "Tidewick Isle",
                "artwork": artwork,
            },
        )
        self.assertEqual(objc.state, 1)
        self.assertEqual(objc.released, [("image", b"jpeg one")])  # the artwork has it
        system.now_playing("paused", "Pell", False, **song)  # its cover isn't made again
        self.assertEqual(objc.read, [b"jpeg one"])
        self.assertEqual(objc.info["artwork"], artwork)
        # A film: a video, its title alone, as before; the song's artwork goes.
        system.now_playing("playing", "A film", False)
        self.assertEqual(objc.info, {"title": "A film", "live": False, "type": 2})
        self.assertIn(artwork, objc.released)
        # A song with no tags and no cover is music all the same; so is a radio channel.
        system.now_playing("playing", "Lone", False, music=True)
        self.assertEqual(objc.info, {"title": "Lone", "live": False, "type": 1})
        system.now_playing("playing", "Radio One", True, music=True)
        self.assertEqual(objc.info, {"title": "Radio One", "live": True, "type": 1})
        system.now_playing("", "", False)
        self.assertIsNone(objc.info)
        self.assertEqual(objc.state, 3)

    def test_mac_without_artwork_by_image_shows_the_song_and_says_so_once(self):
        # A macOS whose MPMediaItemArtwork no longer answers initWithImage: (only the block
        # initializer, which ctypes can't make): no artwork, said once in the log.
        objc = FakeObjC(artwork=False)
        system = fake_mac(objc)
        cover = dict(art_bytes=b"jpeg", music=True)
        with self.assertLogs("services.media_keys_mac", "DEBUG") as logs:
            system.now_playing("playing", "Pell", False, artist="W", art_path="/c/1.jpg", **cover)
            system.now_playing("playing", "Rook", False, artist="W", art_path="/c/2.jpg", **cover)
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(objc.info, {"title": "Rook", "live": False, "type": 1, "artist": "W"})
        self.assertEqual(objc.read, [])

    def test_mac_says_each_reason_for_no_cover_once(self):
        # A picture NSImage can't read, and a runtime that fails: each said once a session,
        # and the song shows all the same.
        objc = FakeObjC()
        system = fake_mac(objc)
        with self.assertLogs("services.media_keys_mac", "DEBUG") as logs:
            for n, data in enumerate((b"broken 1", b"broken 2", b"boom 1", b"boom 2")):
                system.now_playing(
                    "playing",
                    f"Song {n}",
                    False,
                    art_path=f"/c/{n}.jpg",
                    art_bytes=data,
                    music=True,
                )
                self.assertEqual(objc.info, {"title": f"Song {n}", "live": False, "type": 1})
        self.assertEqual(len(logs.records), 2, [r.getMessage() for r in logs.records])
        self.assertEqual(len(objc.read), 4)


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

    def __init__(self, *, thumbnails=True, music=True):
        import services.media_keys_windows as windows

        self.windows = windows
        self.thumbnails = thumbnails  # CreateFromUri answers
        self.music_given = music  # get_MusicProperties answers
        self.log: list[tuple] = []
        self.released: list[str] = []
        self.strings: dict[int, str] = {}  # an HSTRING's handle: its text
        self.deleted: list[int] = []
        self.names: dict[int, str] = {}
        self.held = {"music put_Title": "", "video put_Title": "", "put_PlaybackStatus": 0}
        self._kept: list = []
        hstring, pointer, out = "s", "p", "o"
        self.album = self.com("album", {7: ("put_AlbumTitle", [hstring])})
        self.music = self.com(
            "music",
            {
                6: ("get_Title", ["os"], lambda: self._made(self.held["music put_Title"])),
                7: ("music put_Title", [hstring]),
                11: ("put_Artist", [hstring]),
            },
            interfaces={bytes(windows.IID_MUSIC_2): self.album},
        )
        self.video = self.com(
            "video",
            {
                6: ("get_Title", ["os"], lambda: self._made(self.held["video put_Title"])),
                7: ("video put_Title", [hstring]),
            },
        )
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
            "smtc",
            {
                6: ("get_PlaybackStatus", ["oi"], lambda: self.held["put_PlaybackStatus"]),
                7: ("put_PlaybackStatus", ["i"]),
                10: ("get_IsEnabled", ["ob"], 1),
                11: ("put_IsEnabled", ["b"]),
                33: ("remove_ButtonPressed", ["q"]),
            },
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
            WindowsGetStringRawBuffer=lambda handle, _length: self.strings[handle.value],
            RoGetActivationFactory=self._factory,
        )

    _TYPES = {
        "s": ctypes.c_void_p,
        "p": ctypes.c_void_p,
        "i": ctypes.c_int32,
        "b": ctypes.c_ubyte,
        "q": ctypes.c_int64,
        "o": ctypes.POINTER(ctypes.c_void_p),
        "os": ctypes.POINTER(ctypes.c_void_p),
        "oi": ctypes.POINTER(ctypes.c_int32),
        "ob": ctypes.POINTER(ctypes.c_ubyte),
    }

    def com(self, name, methods, *, interfaces=None):
        """A COM object: `methods` maps a vtable slot to (its name in the log, its arguments'
        kinds, and for an out pointer what it gives); any other slot fails."""
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
            if (label == "CreateFromUri" and not self.thumbnails) or (
                label == "get_MusicProperties" and not self.music_given
            ):
                self.log.append((label, "failed"))
                return -2147467259  # 0x80004005: E_FAIL
            shown = []
            for kind, value in zip(kinds, args):
                if kind.startswith("o"):
                    value[0] = gives() if callable(gives) else gives
                    continue
                if kind == "s":
                    value = self.strings.get(value or 0, "")  # NULL: the empty string
                elif kind == "p":
                    value = self.names.get(value or 0, value)
                shown.append(value)
            if label in self.held:
                self.held[label] = shown[0]
            if not label.startswith("get_") or label == "get_MusicProperties":
                self.log.append((label, *shown))
            return w.S_OK

        types = [self._TYPES[k] for k in kinds]
        return w.WINFUNCTYPE(w.HRESULT, ctypes.c_void_p, *types)(method)

    def _made(self, text):
        handle = 0x5000 + len(self.strings) + 1
        self.strings[handle] = text
        return handle

    def _create_string(self, text, units, out):
        out._obj.value = self._made(text)
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
        song = dict(artist="Wexlo Tide", album="Tidewick Isle", art_path=cover, music=True)
        controls.now_playing("playing", "Pell", False, **song)
        self.assertEqual(
            runtime.log,
            [
                ("put_Type", 1),  # MediaPlaybackType.Music, before its properties are asked
                ("get_MusicProperties",),
                ("music put_Title", "Pell"),
                ("put_Artist", "Wexlo Tide"),
                ("put_AlbumTitle", "Tidewick Isle"),
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
        self.assertEqual(controls.report()["title"], "Pell")  # the song's, as it shows
        runtime.log.clear()
        controls.now_playing("paused", "Pell", False, **song)  # the same cover: made once
        self.assertNotIn(("CreateUri", Path(cover).as_uri()), runtime.log)
        self.assertNotIn(("get_MusicProperties",), runtime.log)  # asked once
        self.assertIn(("put_PlaybackStatus", 4), runtime.log)
        # A film: a video, its title alone, as before, and no thumbnail.
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
        self.assertEqual(controls.report()["title"], "A film")
        # A song with no tags and no cover is music all the same.
        runtime.log.clear()
        controls.now_playing("playing", "Lone", False, music=True)
        lone = [("put_Type", 1), ("music put_Title", "Lone"), ("put_Artist", "")]
        self.assertEqual(runtime.log[:3], lone)
        # Closing releases each interface once.
        controls.close()
        controls.close()
        for name in ("music", "album", "video", "updater", "smtc"):
            self.assertEqual(runtime.released.count(name), 1, name)
        self.assertEqual(sorted(runtime.deleted), sorted(runtime.strings))  # every HSTRING

    def test_windows_without_a_thumbnail_still_shows_the_song_and_says_so_once(self):
        runtime = FakeWindowsRuntime(thumbnails=False)
        controls = runtime.controls()
        covers = [str(Path(os.sep, "covers", f"cover-{n}.jpg").absolute()) for n in (1, 2)]
        song = dict(artist="W", music=True)
        with self.assertLogs("services.media_keys_windows", "DEBUG") as logs:
            controls.now_playing("playing", "Pell", False, art_path=covers[0], **song)
            controls.now_playing("playing", "Rook", False, art_path=covers[1], **song)
        self.assertEqual(len(logs.records), 1)
        self.assertIn(("CreateFromUri", "failed"), runtime.log)
        self.assertIn(("put_Artist", "W"), runtime.log)
        self.assertEqual(runtime.log.count(("Update",)), 2)
        self.assertNotIn("put_Thumbnail", [entry[0] for entry in runtime.log])

    def test_windows_without_music_properties_shows_songs_as_films_and_says_so_once(self):
        runtime = FakeWindowsRuntime(music=False)
        controls = runtime.controls()
        with self.assertLogs("services.media_keys_windows", "DEBUG") as logs:
            controls.now_playing("playing", "Pell", False, artist="W", music=True)
            controls.now_playing("playing", "Rook", False, artist="W", music=True)
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(
            runtime.log[:4],
            [
                ("put_Type", 1),
                ("get_MusicProperties", "failed"),
                ("put_Type", 2),  # a film's look: its title
                ("video put_Title", "Pell"),
            ],
        )
        self.assertEqual(runtime.log.count(("get_MusicProperties", "failed")), 1)  # not again
        self.assertIn(("video put_Title", "Rook"), runtime.log)
        self.assertEqual(controls.report()["title"], "Rook")


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
            "playing", "Pell", False, artist="Wexlo Tide", album="Tidewick Isle", art_path=cover
        )
        metadata = mpris._all(PLAYER)["Metadata"]
        self.assertEqual(metadata["xesam:title"], "Pell")
        self.assertEqual(metadata["xesam:artist"], ["Wexlo Tide"])  # MPRIS: a list of names
        self.assertEqual(metadata["xesam:album"], "Tidewick Isle")
        self.assertEqual(metadata["mpris:artUrl"], Path(cover).as_uri())
        self.assertIn("%20", metadata["mpris:artUrl"])
        changed = bus.sent[-1].arguments()[1]  # PropertiesChanged says so too
        self.assertEqual(changed["Metadata"]["xesam:album"], "Tidewick Isle")
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

    @unittest.skipUnless(shutil.which("gdbus"), "gdbus prints the types the bus carries")
    def test_the_desktop_reads_the_artist_as_a_string_array(self):
        # MPRIS's xesam:artist is "as"; an "av" is one a desktop drops. gdbus prints the types
        # as they go: ['Wexlo Tide'] for "as" ([<'Wexlo Tide'>] would be "av").
        from PySide6.QtCore import QProcess

        song = dict(artist="Wexlo Tide", album="Tidewick Isle", music=True)
        self.keys.show("playing", "Pell", False, **song)
        service = self.keys.system.report()["service"]
        process = QProcess()
        args = ["call", "--session", "--dest", service, "--object-path", "/org/mpris/MediaPlayer2"]
        args += ["--method", "org.freedesktop.DBus.Properties.Get"]
        process.start("gdbus", [*args, "org.mpris.MediaPlayer2.Player", "Metadata"])
        stopped = QProcess.ProcessState.NotRunning
        self.assertTrue(wait_until(lambda: process.state() == stopped, 10_000))
        answer = bytes(process.readAllStandardOutput().data()).decode()
        self.assertIn("'xesam:artist': <['Wexlo Tide']>", answer)
        self.assertIn("'xesam:album': <'Tidewick Isle'>", answer)


class PanelCoverTests(unittest.TestCase):
    """The cover the system's media panel shows: the audio screen's, as a small file written
    on a thread, named by its pixels, one at a time, gone when QiTV closes
    (ui.song_jobs.PanelCover)."""

    def test_the_panels_cover_is_a_small_file_of_its_own_one_at_a_time(self):
        from ui.song_jobs import PANEL_SIDE, PanelCover

        cover = PanelCover()
        self.addCleanup(cover.close)
        written = []
        cover.written.connect(lambda: written.append(1))
        photo = QImage(1200, 800, QImage.Format.Format_RGB32)
        photo.fill(QColor("#d8653f"))
        self.assertEqual(cover.file_for(photo), ("", b""))  # written on a thread first
        self.assertTrue(wait_until(lambda: cover.file_for(photo)[0]))
        first, data = cover.file_for(photo)
        with open(first, "rb") as file:
            self.assertEqual(file.read(), data)  # its bytes too (read on the thread)
        self.assertEqual(written, [1])
        saved = QImage(first)
        self.assertEqual((saved.width(), saved.height()), (PANEL_SIDE, PANEL_SIDE * 2 // 3))
        self.assertTrue(first.endswith(".jpg"))
        folder = os.path.dirname(first)
        self.assertEqual(os.listdir(folder), [os.path.basename(first)])
        # The same picture again (an album's next track's cover comes as a fresh QImage): its
        # file at once and after, never written again, nothing new for the panel.
        before = os.stat(first).st_mtime_ns
        again = photo.copy()
        self.assertEqual(cover.file_for(again)[0], first)
        self.assertTrue(wait_until(lambda: len(written) == 2))
        self.assertEqual(cover.file_for(again)[0], first)
        self.assertEqual(os.stat(first).st_mtime_ns, before)
        # A logo with see-through parts: a PNG, a file of its own (a panel that keeps a picture
        # by its file's name sees a new one); the last cover shows until it lands, then goes.
        logo = QImage(64, 64, QImage.Format.Format_ARGB32)
        logo.fill(Qt.GlobalColor.transparent)
        self.assertEqual(cover.file_for(logo)[0], first)
        self.assertTrue(wait_until(lambda: cover.file_for(logo)[0] != first))
        second = cover.file_for(logo)[0]
        self.assertTrue(second.endswith(".png"))
        self.assertEqual(QImage(second).size(), logo.size())  # never made larger
        self.assertTrue(wait_until(lambda: os.listdir(folder) == [os.path.basename(second)]))
        self.assertEqual(cover.file_for(None), ("", b""))  # QiTV's drawn stand-in: none
        # Asked for one, then another before the first is written: the newer one is the file.
        third = QImage(300, 300, QImage.Format.Format_RGB32)
        third.fill(QColor("#203040"))
        cover.file_for(photo)
        self.assertEqual(cover.file_for(third), ("", b""))
        self.assertTrue(wait_until(lambda: cover.file_for(third)[0]))
        name = os.path.basename(cover.file_for(third)[0])
        self.assertTrue(wait_until(lambda: os.listdir(folder) == [name]))
        cover.close()  # QiTV closes: the folder goes
        self.assertFalse(os.path.exists(folder))
        self.assertEqual(cover.file_for(photo), ("", b""))

    def test_an_earlier_qitvs_stale_cover_folder_is_swept(self):
        # One that crashed keeps its folder: a day old, it goes at the first cover written;
        # a newer one (another QiTV's) and any other folder stay.
        from ui.song_jobs import PanelCover

        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        long_ago = time.time() - 2 * 24 * 3600
        folders = {}
        for name in ("qitv-panel-crashed", "qitv-panel-running", "not-qitvs"):
            folders[name] = os.path.join(root, name)
            os.mkdir(folders[name])
            with open(os.path.join(folders[name], "cover-1.jpg"), "wb") as file:
                file.write(b"jpeg")
            if name != "qitv-panel-running":
                os.utime(folders[name], (long_ago, long_ago))
        image = QImage(32, 32, QImage.Format.Format_RGB32)
        image.fill(QColor("#203040"))
        with mock.patch.object(tempfile, "tempdir", root):
            cover = PanelCover()
            self.addCleanup(cover.close)
            cover.file_for(image)
            self.assertTrue(wait_until(lambda: cover.file_for(image)[0]))
        self.assertTrue(cover.file_for(image)[0].startswith(root))
        self.assertFalse(os.path.exists(folders["qitv-panel-crashed"]))
        self.assertTrue(os.path.exists(folders["qitv-panel-running"]))
        self.assertTrue(os.path.exists(folders["not-qitvs"]))


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
        facts = [("Pell", "Wexlo Tide", "Tidewick Isle")]
        controller = window.controller
        with (
            mock.patch.object(controller, "now_playing", return_value=target),
            mock.patch.object(controller, "panel_facts", side_effect=lambda: facts[0]),
        ):
            window._show_now_playing("playing")
            song = ("playing", "Pell", False, "Wexlo Tide", "Tidewick Isle")
            self.assertEqual(system.shown[-1], (*song, "", True))  # music, its cover to come
            cover = QImage(64, 64, QImage.Format.Format_RGB32)
            cover.fill(QColor("#d8653f"))
            screen.set_cover(cover, [])
            self.assertTrue(wait_until(lambda: bool(system.shown[-1][5])))
            art = system.shown[-1][5]
            self.assertEqual(system.shown[-1], (*song, art, True))
            with open(art, "rb") as file:
                self.assertEqual(system.art_bytes[-1], file.read())  # macOS's picture's
            facts[0] = ("Rook", "Wexlo Tide", "Tidewick Isle")
            screen.show_song("Rook", "Wexlo Tide", "Tidewick Isle")
            self.assertTrue(wait_until(lambda: system.shown[-1][1] == "Rook"))
            self.assertEqual(system.shown[-1][5], art)  # the same cover: the same file
            told = len(system.shown)
            same = cover.copy()  # the next track's cover: the same picture, a fresh QImage
            screen.set_cover(same, [])
            pause(300)  # its file is looked for on a thread, and found
            self.assertEqual(len(system.shown), told)  # nothing new for the panel
            facts[0] = None  # a picture shows: the screen goes, its facts with it
            screen.hide()
            film = ("playing", "02 Pell", False, "", "", "", False)
            self.assertTrue(wait_until(lambda: system.shown[-1] == film))

    def test_a_radio_channels_name_and_logo_reach_the_panel(self):
        # Ruling 2 (Task 8): a radio channel on the audio screen is music, its name the title,
        # its logo the cover; no artist (its stream's programme is no artist).
        window, controller = self.window, self.window.controller
        system = FakeSystem()
        window.media_keys._system = system  # as on macOS
        self.open_first_channel()
        self.assertTrue(wait_until(lambda: controller.now_playing() is not None))
        window.session.stateChanged.emit("playing")
        target = controller.now_playing()
        # Its screen shows (no picture for a while): as the controller's watch leaves it.
        controller._sound, controller._sound_kind, controller._sound_shown = target, "radio", True
        window.audio_screen.show_song(target.title, "Wexlo Tide - Pell", "")
        logo = QImage(48, 48, QImage.Format.Format_ARGB32)
        logo.fill(Qt.GlobalColor.transparent)
        window.audio_screen.set_cover(logo, [])
        self.assertTrue(wait_until(lambda: bool(system.shown[-1][5])), system.shown)
        _state, title, live, artist, album, art, music = system.shown[-1]
        self.assertEqual((title, live, artist, album, music), ("Channel 1", True, "", "", True))
        self.assertTrue(art.endswith(".png"))
        self.assertTrue(os.path.isfile(art))


if __name__ == "__main__":
    unittest.main()
