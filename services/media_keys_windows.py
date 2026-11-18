"""Windows's media keys: the System Media Transport Controls (SMTC) of QiTV's window, the media
panel over the volume flyout. Windows sends the keyboard's media keys to the window in front,
else to the player listed there: QiTV lists itself and says what it plays. See
services.media_keys.

Through the Windows Runtime's COM interfaces with ctypes (no native build, no package):
ISystemMediaTransportControlsInterop.GetForWindow gives the window's controls; the vtable
slots below follow the Windows SDK's windows.media.h, windows.foundation.h and
windows.storage.streams.h (as Wine's IDL lists them too), after IInspectable's six.

A song (Plan 4) shows as music: its title, artist and album, and its cover as the thumbnail,
a stream reference made from the cover file's URI (RandomAccessStreamReference.CreateFromUri:
no StorageFile, no asynchronous call). Windows reads that file itself, when it shows it.
"""

from __future__ import annotations

import ctypes
from ctypes import POINTER, byref, c_int32, c_int64, c_long, c_uint32, c_void_p
import logging
from pathlib import Path
from typing import Any, Optional
import uuid

from services.media_keys import on_command

logger = logging.getLogger(__name__)

HRESULT = c_long
# Windows's calling convention (elsewhere C's: the COM objects of QiTV's own run in the tests).
WINFUNCTYPE: Any = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
WinDLL: Any = getattr(ctypes, "WinDLL", ctypes.CDLL)
S_OK, E_NOINTERFACE = 0, -2147467262  # 0x80004002


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid(text: str) -> GUID:
    return GUID.from_buffer_copy(uuid.UUID(text).bytes_le)


IID_IUNKNOWN = _guid("00000000-0000-0000-c000-000000000046")
IID_IAGILE = _guid("94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90")  # IAgileObject: any thread may call
IID_INTEROP = _guid("ddb0472d-c911-4a1f-86d9-dc3d71a95f5a")  # ISystemMediaTransportControlsInterop
IID_SMTC = _guid("99fa3ff4-1742-42a6-902e-087d41f965ec")  # ISystemMediaTransportControls
# TypedEventHandler<SystemMediaTransportControls,
#                   SystemMediaTransportControlsButtonPressedEventArgs>
IID_BUTTON_HANDLER = _guid("0557e996-7b23-5bae-aa81-ea0d671143a4")
IID_MUSIC_2 = _guid("00368462-97d3-44b9-b00f-008afcefaf18")  # IMusicDisplayProperties2
IID_URI_FACTORY = _guid("44a9796f-723e-4fdf-a218-033e75b0c084")  # IUriRuntimeClassFactory
# IRandomAccessStreamReferenceStatics
IID_STREAM_REFERENCE_STATICS = _guid("857309dc-3fbf-4e7d-986f-ef3b1a07a964")
CLASS_SMTC = "Windows.Media.SystemMediaTransportControls"
CLASS_URI = "Windows.Foundation.Uri"
CLASS_STREAM_REFERENCE = "Windows.Storage.Streams.RandomAccessStreamReference"

# ISystemMediaTransportControls
GET_PLAYBACK_STATUS, PUT_PLAYBACK_STATUS, GET_DISPLAY_UPDATER = 6, 7, 8
GET_IS_ENABLED, PUT_IS_ENABLED = 10, 11
PUT_IS_PLAY_ENABLED, PUT_IS_STOP_ENABLED, PUT_IS_PAUSE_ENABLED = 13, 15, 17
PUT_IS_PREVIOUS_ENABLED, PUT_IS_NEXT_ENABLED = 25, 27
ADD_BUTTON_PRESSED, REMOVE_BUTTON_PRESSED = 32, 33
# ISystemMediaTransportControlsDisplayUpdater
PUT_TYPE, PUT_THUMBNAIL, GET_MUSIC_PROPERTIES, GET_VIDEO_PROPERTIES, UPDATE = 7, 11, 12, 13, 17
# IVideoDisplayProperties (and IMusicDisplayProperties' Title)
GET_TITLE, PUT_TITLE = 6, 7
# IMusicDisplayProperties
PUT_ARTIST = 11
# IMusicDisplayProperties2
PUT_ALBUM_TITLE = 7
# IUriRuntimeClassFactory
CREATE_URI = 6
# IRandomAccessStreamReferenceStatics
CREATE_FROM_URI = 7
# ISystemMediaTransportControlsButtonPressedEventArgs
GET_BUTTON = 6
# IInspectable / IUnknown
QUERY_INTERFACE, RELEASE = 0, 2

# MediaPlaybackStatus
_STATUS = {"playing": 3, "paused": 4, "": 2}  # Playing, Paused, Stopped
_STATES = {3: "playing", 4: "paused"}
MEDIA_TYPE_MUSIC, MEDIA_TYPE_VIDEO = 1, 2  # MediaPlaybackType
# SystemMediaTransportControlsButton → the command
_BUTTONS = {0: "play", 1: "pause", 2: "stop", 6: "next", 7: "previous"}
_NAMES = {name: button for button, name in _BUTTONS.items()}


def _check(hr: int, what: str) -> None:
    if hr < 0:
        raise OSError(f"{what} failed: HRESULT 0x{hr & 0xFFFFFFFF:08x}")


def _call(this: int, index: int, what: str, *args: tuple[Any, Any]) -> None:
    """this->vtable[index](this, …): each of `args` is a (ctypes type, value) pair."""
    table = ctypes.cast(this, POINTER(POINTER(c_void_p))).contents
    function = WINFUNCTYPE(HRESULT, c_void_p, *(t for t, _ in args))(table[index])
    _check(function(this, *(v for _, v in args)), what)


def _release(this: Optional[int]) -> None:
    if this:
        table = ctypes.cast(this, POINTER(POINTER(c_void_p))).contents
        WINFUNCTYPE(c_uint32, c_void_p)(table[RELEASE])(this)


# ---- a COM object of QiTV's own: the ButtonPressed handler (and the smoke test's event) ------

_QUERY = WINFUNCTYPE(HRESULT, c_void_p, POINTER(GUID), POINTER(c_void_p))
_COUNT = WINFUNCTYPE(c_uint32, c_void_p)
_INVOKE = WINFUNCTYPE(HRESULT, c_void_p, c_void_p, c_void_p)
_GET_BUTTON = WINFUNCTYPE(HRESULT, c_void_p, POINTER(c_int32))
_kept: list[Any] = []  # every object and entry point made here, alive as long as the process


def _same(a: GUID, b: GUID) -> bool:
    return bytes(a) == bytes(b)


def _com_object(methods: list[Any], interfaces: tuple[GUID, ...]) -> int:
    """A static COM object: QueryInterface (IUnknown, IAgileObject and `interfaces`), AddRef
    and Release that never free, then `methods` (C entry points)."""

    def query(this: int, iid: Any, out: Any) -> int:
        wanted = iid.contents
        if any(_same(wanted, known) for known in (IID_IUNKNOWN, IID_IAGILE, *interfaces)):
            out[0] = this
            return S_OK
        out[0] = None
        return E_NOINTERFACE

    def count(_this: int) -> int:
        return 1

    entries = [_QUERY(query), _COUNT(count), _COUNT(count), *methods]
    table = (c_void_p * len(entries))(*(ctypes.cast(e, c_void_p) for e in entries))
    holder = (c_void_p * 1)(ctypes.cast(table, c_void_p))
    _kept.extend((entries, table, holder))
    return ctypes.addressof(holder)


def _handler() -> int:
    """The ButtonPressed handler: Invoke(sender, args) reads the button and hands its command
    on (on a thread of the system's: MediaKeys emits a queued signal)."""

    def invoke(_this: int, _sender: int, args: int) -> int:
        try:
            button = c_int32(-1)
            _call(args, GET_BUTTON, "get_Button", (POINTER(c_int32), byref(button)))
            name = _BUTTONS.get(button.value)
            if name is not None:
                on_command(name)
        except Exception as e:  # never raise into the system's thread
            logger.warning("A media key from Windows failed: %s", e)
        return S_OK

    return _com_object([_INVOKE(invoke)], (IID_BUTTON_HANDLER,))


def _pressed_event(button: int) -> int:
    """A ButtonPressed event's arguments for `button` (the smoke test's): IInspectable's three
    unused slots, then get_Button."""

    def unused(*_args: Any) -> int:
        return E_NOINTERFACE

    unused_slot = WINFUNCTYPE(HRESULT, c_void_p, c_void_p, c_void_p)(unused)

    def get_button(_this: int, out: Any) -> int:
        out[0] = button
        return S_OK

    return _com_object([unused_slot, unused_slot, unused_slot, _GET_BUTTON(get_button)], ())


def _query(this: int, iid: GUID, what: str) -> int:
    """this->QueryInterface(iid): the interface, held (released by the caller)."""
    found = c_void_p()
    _call(
        this, QUERY_INTERFACE, what, (POINTER(GUID), byref(iid)), (POINTER(c_void_p), byref(found))
    )
    if not found.value:
        raise OSError(f"{what} gave nothing")
    return found.value


class WindowsMediaControls:
    """The window's System Media Transport Controls."""

    def __init__(self, window_id: int, *, combase: Any = None) -> None:
        """`combase`: the Windows Runtime's functions (combase.dll; a test's own off Windows)."""
        self._hwnd = window_id
        if combase is None:
            combase = WinDLL("combase")
            for name, restype, argtypes in (
                ("RoInitialize", HRESULT, [c_int32]),
                ("RoGetActivationFactory", HRESULT, [c_void_p, POINTER(GUID), POINTER(c_void_p)]),
                ("WindowsCreateString", HRESULT, [ctypes.c_wchar_p, c_uint32, POINTER(c_void_p)]),
                ("WindowsDeleteString", HRESULT, [c_void_p]),
                ("WindowsGetStringRawBuffer", ctypes.c_wchar_p, [c_void_p, POINTER(c_uint32)]),
            ):
                function = getattr(combase, name)
                function.restype, function.argtypes = restype, argtypes
        self._combase = combase
        self._smtc: Optional[int] = None
        self._video: Optional[int] = None  # the display updater's video properties
        # ... and its music properties, with their album's interface (fetched once a song
        # plays; False: Windows gave none, the song shows as a film does).
        self._music: Any = None
        self._album: Optional[int] = None
        # The cover file the thumbnail was asked for, and whether one shows.
        self._thumbnail: tuple[str, bool] = ("", False)
        self._thumbnail_said = False  # a thumbnail Windows couldn't make was logged
        self._updater: Optional[int] = None
        self._handler: Optional[int] = None
        self._token = c_int64(0)

    def _string(self, text: str) -> c_void_p:
        made = c_void_p()
        units = len(text.encode("utf-16-le")) // 2
        _check(self._combase.WindowsCreateString(text, units, byref(made)), "WindowsCreateString")
        return made

    def _factory(self, class_name: str, iid: GUID) -> int:
        """The activation factory of `class_name` as `iid` (released by the caller)."""
        name = self._string(class_name)
        factory = c_void_p()
        try:
            _check(
                self._combase.RoGetActivationFactory(name, byref(iid), byref(factory)),
                f"RoGetActivationFactory({class_name})",
            )
        finally:
            self._combase.WindowsDeleteString(name)
        if not factory.value:
            raise OSError(f"no {class_name} factory")
        return factory.value

    def listen(self) -> None:
        # The GUI thread's COM is Qt's (OLE, single-threaded): S_FALSE or RPC_E_CHANGED_MODE
        # say it is there already.
        self._combase.RoInitialize(0)
        interop = self._factory(CLASS_SMTC, IID_INTEROP)
        smtc = c_void_p()
        try:
            _call(
                interop,
                6,
                "GetForWindow",
                (c_void_p, self._hwnd),
                (POINTER(GUID), byref(IID_SMTC)),
                (POINTER(c_void_p), byref(smtc)),
            )
        finally:
            _release(interop)
        self._smtc = smtc.value
        for index in (
            PUT_IS_PLAY_ENABLED,
            PUT_IS_PAUSE_ENABLED,
            PUT_IS_STOP_ENABLED,
            PUT_IS_NEXT_ENABLED,
            PUT_IS_PREVIOUS_ENABLED,
        ):
            self._smtc_call(index, "an IsEnabled", (ctypes.c_ubyte, 1))
        updater = c_void_p()
        self._smtc_call(
            GET_DISPLAY_UPDATER, "get_DisplayUpdater", (POINTER(c_void_p), byref(updater))
        )
        self._updater = updater.value
        _call(self._updater or 0, PUT_TYPE, "put_Type", (c_int32, MEDIA_TYPE_VIDEO))
        video = c_void_p()
        _call(
            self._updater or 0,
            GET_VIDEO_PROPERTIES,
            "get_VideoProperties",
            (POINTER(c_void_p), byref(video)),
        )
        self._video = video.value
        self._handler = _handler()
        self._smtc_call(
            ADD_BUTTON_PRESSED,
            "add_ButtonPressed",
            (c_void_p, self._handler),
            (POINTER(c_int64), byref(self._token)),
        )
        self._smtc_call(PUT_IS_ENABLED, "put_IsEnabled", (ctypes.c_ubyte, 0))  # till it plays

    def _smtc_call(self, index: int, what: str, *args: tuple[Any, Any]) -> None:
        if not self._smtc:
            raise OSError("no media controls")
        _call(self._smtc, index, what, *args)

    def now_playing(
        self,
        state: str,
        title: str,
        live: bool,
        *,
        artist: str = "",
        album: str = "",
        art_path: str = "",
    ) -> None:
        if state:
            updater = self._updater or 0
            if (artist or album or art_path) and self._music_properties():
                _call(updater, PUT_TYPE, "put_Type", (c_int32, MEDIA_TYPE_MUSIC))
                self._put_text(self._music, PUT_TITLE, "put_Title", title or "QiTV")
                self._put_text(self._music, PUT_ARTIST, "put_Artist", artist)
                if self._album:
                    self._put_text(self._album, PUT_ALBUM_TITLE, "put_AlbumTitle", album)
            else:
                _call(updater, PUT_TYPE, "put_Type", (c_int32, MEDIA_TYPE_VIDEO))
                self._put_text(self._video or 0, PUT_TITLE, "put_Title", title or "QiTV")
            self._show_thumbnail(art_path)
            _call(updater, UPDATE, "Update")
        self._smtc_call(PUT_PLAYBACK_STATUS, "put_PlaybackStatus", (c_int32, _STATUS[state]))
        # Nothing plays: QiTV leaves the panel (the keys go to the other players).
        self._smtc_call(PUT_IS_ENABLED, "put_IsEnabled", (ctypes.c_ubyte, 1 if state else 0))

    def _put_text(self, this: int, index: int, what: str, text: str) -> None:
        made = self._string(text)
        try:
            _call(this, index, what, (c_void_p, made))
        finally:
            self._combase.WindowsDeleteString(made)

    def _music_properties(self) -> bool:
        """The display updater's music properties, fetched the first time a song plays (False:
        Windows gave none; its album's interface may be missing alone)."""
        if self._music is None:
            music = c_void_p()
            try:
                _call(
                    self._updater or 0,
                    GET_MUSIC_PROPERTIES,
                    "get_MusicProperties",
                    (POINTER(c_void_p), byref(music)),
                )
                if not music.value:
                    raise OSError("get_MusicProperties gave nothing")
            except OSError as e:
                logger.debug("Windows shows songs as films (no music properties): %s", e)
                self._music = False
                return False
            self._music = music.value
            try:
                self._album = _query(music.value, IID_MUSIC_2, "IMusicDisplayProperties2")
            except OSError as e:
                logger.debug("Windows shows no album titles: %s", e)
        return bool(self._music)

    def _show_thumbnail(self, path: str) -> None:
        """The cover at `path` as the thumbnail ("": none). One Windows can't make leaves none
        (the last song's goes), said once in the log; the title, artist and album show all
        the same."""
        asked, shown = self._thumbnail
        if path == asked:
            return
        reference: Optional[int] = None
        try:
            if path:
                try:
                    reference = self._stream_reference(path)
                except (OSError, ValueError) as e:  # (ValueError: a path with no URI)
                    self._no_thumbnail(e)
            if reference or shown:  # a new one, or the last one goes
                _call(self._updater or 0, PUT_THUMBNAIL, "put_Thumbnail", (c_void_p, reference))
                shown = bool(reference)
        except OSError as e:
            self._no_thumbnail(e)
        finally:
            _release(reference)  # the updater holds its own
        self._thumbnail = (path, shown)

    def _no_thumbnail(self, error: Exception) -> None:
        if not self._thumbnail_said:
            self._thumbnail_said = True
            logger.debug("Windows shows no cover: %s", error)

    def _stream_reference(self, path: str) -> int:
        """RandomAccessStreamReference.CreateFromUri(file URI of `path`), held."""
        uri, reference = c_void_p(), c_void_p()
        factory = self._factory(CLASS_URI, IID_URI_FACTORY)
        try:
            text = self._string(Path(path).as_uri())
            try:
                _call(
                    factory,
                    CREATE_URI,
                    "CreateUri",
                    (c_void_p, text),
                    (POINTER(c_void_p), byref(uri)),
                )
            finally:
                self._combase.WindowsDeleteString(text)
        finally:
            _release(factory)
        try:
            statics = self._factory(CLASS_STREAM_REFERENCE, IID_STREAM_REFERENCE_STATICS)
            try:
                _call(
                    statics,
                    CREATE_FROM_URI,
                    "CreateFromUri",
                    (c_void_p, uri.value),
                    (POINTER(c_void_p), byref(reference)),
                )
            finally:
                _release(statics)
        finally:
            _release(uri.value)
        if not reference.value:
            raise OSError("CreateFromUri gave nothing")
        return reference.value

    def close(self) -> None:
        if not self._smtc:
            return
        try:
            self._smtc_call(REMOVE_BUTTON_PRESSED, "remove_ButtonPressed", (c_int64, self._token))
            self._smtc_call(PUT_PLAYBACK_STATUS, "put_PlaybackStatus", (c_int32, _STATUS[""]))
            self._smtc_call(PUT_IS_ENABLED, "put_IsEnabled", (ctypes.c_ubyte, 0))
        finally:
            music = self._music or None
            for this in (self._album, music, self._video, self._updater, self._smtc):
                _release(this)
            self._album = self._music = self._video = self._updater = self._smtc = None

    def report(self) -> dict:
        status, enabled = c_int32(-1), ctypes.c_ubyte(0)
        self._smtc_call(
            GET_PLAYBACK_STATUS, "get_PlaybackStatus", (POINTER(c_int32), byref(status))
        )
        self._smtc_call(GET_IS_ENABLED, "get_IsEnabled", (POINTER(ctypes.c_ubyte), byref(enabled)))
        text = c_void_p()
        _call(self._video or 0, GET_TITLE, "get_Title", (POINTER(c_void_p), byref(text)))
        try:
            length = c_uint32(0)
            title = self._combase.WindowsGetStringRawBuffer(text, byref(length)) or ""
        finally:
            self._combase.WindowsDeleteString(text)
        return {
            "state": _STATES.get(status.value, ""),
            "title": title,
            "enabled": bool(enabled.value),
            "status": status.value,
        }

    def simulate(self, name: str) -> bool:
        """Calls the handler as Windows would, with an event of QiTV's own for the button."""
        if name == "toggle":  # Windows has none: Pause while playing, else Play
            name = "pause" if self.report()["state"] == "playing" else "play"
        button = _NAMES.get(name)
        if button is None or not self._handler:
            return False
        event = _pressed_event(button)
        table = ctypes.cast(self._handler, POINTER(POINTER(c_void_p))).contents
        invoke = _INVOKE(table[3])
        return bool(invoke(self._handler, self._smtc, event) == S_OK)
