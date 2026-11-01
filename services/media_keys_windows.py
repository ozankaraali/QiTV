"""Windows's media keys: the System Media Transport Controls (SMTC) of QiTV's window, the media
panel over the volume flyout. Windows sends the keyboard's media keys to the window in front,
else to the player listed there: QiTV lists itself and says what it plays. See
services.media_keys.

Through the Windows Runtime's COM interfaces with ctypes (no native build, no package):
ISystemMediaTransportControlsInterop.GetForWindow gives the window's controls; the vtable
slots below follow the Windows SDK's windows.media.h, after IInspectable's six.
"""

from __future__ import annotations

import ctypes
import logging
import uuid
from ctypes import POINTER, byref, c_int32, c_int64, c_long, c_uint32, c_void_p
from typing import Any, Optional

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
CLASS_SMTC = "Windows.Media.SystemMediaTransportControls"

# ISystemMediaTransportControls
GET_PLAYBACK_STATUS, PUT_PLAYBACK_STATUS, GET_DISPLAY_UPDATER = 6, 7, 8
GET_IS_ENABLED, PUT_IS_ENABLED = 10, 11
PUT_IS_PLAY_ENABLED, PUT_IS_STOP_ENABLED, PUT_IS_PAUSE_ENABLED = 13, 15, 17
PUT_IS_PREVIOUS_ENABLED, PUT_IS_NEXT_ENABLED = 25, 27
ADD_BUTTON_PRESSED, REMOVE_BUTTON_PRESSED = 32, 33
# ISystemMediaTransportControlsDisplayUpdater
PUT_TYPE, GET_VIDEO_PROPERTIES, UPDATE = 7, 13, 17
# IVideoDisplayProperties
GET_TITLE, PUT_TITLE = 6, 7
# ISystemMediaTransportControlsButtonPressedEventArgs
GET_BUTTON = 6
# IInspectable / IUnknown
RELEASE = 2

# MediaPlaybackStatus
_STATUS = {"playing": 3, "paused": 4, "": 2}  # Playing, Paused, Stopped
_STATES = {3: "playing", 4: "paused"}
MEDIA_TYPE_VIDEO = 2  # MediaPlaybackType.Video
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


class WindowsMediaControls:
    """The window's System Media Transport Controls."""

    def __init__(self, window_id: int) -> None:
        self._hwnd = window_id
        self._combase = WinDLL("combase")
        for name, restype, argtypes in (
            ("RoInitialize", HRESULT, [c_int32]),
            ("RoGetActivationFactory", HRESULT, [c_void_p, POINTER(GUID), POINTER(c_void_p)]),
            ("WindowsCreateString", HRESULT, [ctypes.c_wchar_p, c_uint32, POINTER(c_void_p)]),
            ("WindowsDeleteString", HRESULT, [c_void_p]),
            ("WindowsGetStringRawBuffer", ctypes.c_wchar_p, [c_void_p, POINTER(c_uint32)]),
        ):
            function = getattr(self._combase, name)
            function.restype, function.argtypes = restype, argtypes
        self._smtc: Optional[int] = None
        self._video: Optional[int] = None  # the display updater's video properties
        self._updater: Optional[int] = None
        self._handler: Optional[int] = None
        self._token = c_int64(0)

    def _string(self, text: str) -> c_void_p:
        made = c_void_p()
        units = len(text.encode("utf-16-le")) // 2
        _check(self._combase.WindowsCreateString(text, units, byref(made)), "WindowsCreateString")
        return made

    def listen(self) -> None:
        # The GUI thread's COM is Qt's (OLE, single-threaded): S_FALSE or RPC_E_CHANGED_MODE
        # say it is there already.
        self._combase.RoInitialize(0)
        name = self._string(CLASS_SMTC)
        interop = c_void_p()
        try:
            _check(
                self._combase.RoGetActivationFactory(name, byref(IID_INTEROP), byref(interop)),
                "RoGetActivationFactory",
            )
        finally:
            self._combase.WindowsDeleteString(name)
        smtc = c_void_p()
        try:
            _call(
                interop.value or 0,
                6,
                "GetForWindow",
                (c_void_p, self._hwnd),
                (POINTER(GUID), byref(IID_SMTC)),
                (POINTER(c_void_p), byref(smtc)),
            )
        finally:
            _release(interop.value)
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

    def now_playing(self, state: str, title: str, live: bool) -> None:
        if state:
            text = self._string(title or "QiTV")
            try:
                _call(self._video or 0, PUT_TITLE, "put_Title", (c_void_p, text))
            finally:
                self._combase.WindowsDeleteString(text)
            _call(self._updater or 0, UPDATE, "Update")
        self._smtc_call(PUT_PLAYBACK_STATUS, "put_PlaybackStatus", (c_int32, _STATUS[state]))
        # Nothing plays: QiTV leaves the panel (the keys go to the other players).
        self._smtc_call(PUT_IS_ENABLED, "put_IsEnabled", (ctypes.c_ubyte, 1 if state else 0))

    def close(self) -> None:
        if not self._smtc:
            return
        try:
            self._smtc_call(REMOVE_BUTTON_PRESSED, "remove_ButtonPressed", (c_int64, self._token))
            self._smtc_call(PUT_PLAYBACK_STATUS, "put_PlaybackStatus", (c_int32, _STATUS[""]))
            self._smtc_call(PUT_IS_ENABLED, "put_IsEnabled", (ctypes.c_ubyte, 0))
        finally:
            for this in (self._video, self._updater, self._smtc):
                _release(this)
            self._video = self._updater = self._smtc = None

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
