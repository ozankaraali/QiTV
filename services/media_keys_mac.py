"""macOS's media keys: QiTV listed as Now Playing (MediaPlayer's MPRemoteCommandCenter and
MPNowPlayingInfoCenter, through the Objective-C runtime: no native build). See
services.media_keys."""

from __future__ import annotations

import ctypes
import ctypes.util
from typing import Any, Optional

from services.media_keys import on_command

_MEDIA_PLAYER = "/System/Library/Frameworks/MediaPlayer.framework/MediaPlayer"
_TARGET_CLASS = b"QiTVMediaCommandTarget"
# MPRemoteCommandHandlerStatus
_SUCCESS, _NO_ITEM = 0, 110
# MPNowPlayingPlaybackState
_STATES = {"playing": 1, "paused": 2, "": 3}
_MEDIA_TYPE_VIDEO = 2  # MPNowPlayingInfoMediaTypeVideo
# MPRemoteCommandCenter's command, the target's method for it, the command's name.
_COMMANDS = (
    (b"togglePlayPauseCommand", b"qitvToggle:", "toggle"),
    (b"playCommand", b"qitvPlay:", "play"),
    (b"pauseCommand", b"qitvPause:", "pause"),
    (b"nextTrackCommand", b"qitvNext:", "next"),
    (b"previousTrackCommand", b"qitvPrevious:", "previous"),
    (b"stopCommand", b"qitvStop:", "stop"),
)
# - (MPRemoteCommandHandlerStatus)qitvToggle:(MPRemoteCommandEvent *)event
_IMP = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
_imps: list[Any] = []  # the methods' C entry points, alive as long as the process


def _method(name: str) -> Any:
    def handle(_self: int, _cmd: int, _event: int) -> int:
        return _SUCCESS if on_command(name) else _NO_ITEM

    return _IMP(handle)


class MacNowPlaying:
    """MPRemoteCommandCenter's commands to a target object of QiTV's own class, and
    MPNowPlayingInfoCenter's state and title."""

    def __init__(self) -> None:
        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc") or "libobjc.dylib")
        for name, restype, argtypes in (
            ("objc_getClass", ctypes.c_void_p, [ctypes.c_char_p]),
            ("sel_registerName", ctypes.c_void_p, [ctypes.c_char_p]),
            (
                "objc_allocateClassPair",
                ctypes.c_void_p,
                [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t],
            ),
            (
                "class_addMethod",
                ctypes.c_bool,
                [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p],
            ),
            ("objc_registerClassPair", None, [ctypes.c_void_p]),
            ("objc_autoreleasePoolPush", ctypes.c_void_p, []),
            ("objc_autoreleasePoolPop", None, [ctypes.c_void_p]),
        ):
            function = getattr(objc, name)
            function.restype, function.argtypes = restype, argtypes
        self._objc = objc
        self._msg_send = ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value or 0
        player = ctypes.cdll.LoadLibrary(_MEDIA_PLAYER)
        self._title_key = ctypes.c_void_p.in_dll(player, "MPMediaItemPropertyTitle").value
        self._live_key = ctypes.c_void_p.in_dll(
            player, "MPNowPlayingInfoPropertyIsLiveStream"
        ).value
        self._type_key = ctypes.c_void_p.in_dll(player, "MPNowPlayingInfoPropertyMediaType").value
        commands = self._class(b"MPRemoteCommandCenter")
        info = self._class(b"MPNowPlayingInfoCenter")
        self._center = self._send(commands, b"sharedCommandCenter")
        self._info = self._send(info, b"defaultCenter")
        if not self._center or not self._info:
            raise RuntimeError("no MediaPlayer command center")
        self._target: Optional[int] = None

    def _class(self, name: bytes) -> int:
        found = self._objc.objc_getClass(name)
        if not found:
            raise RuntimeError(f"no {name.decode()} class")
        return int(found)

    def _send(self, receiver: int, selector: bytes, restype: Any = ctypes.c_void_p, *args) -> Any:
        """[receiver selector …]; each of `args` is a (ctypes type, value) pair."""
        function = ctypes.CFUNCTYPE(
            restype, ctypes.c_void_p, ctypes.c_void_p, *(t for t, _ in args)
        )(self._msg_send)
        return function(receiver, self._objc.sel_registerName(selector), *(v for _, v in args))

    def _target_class(self) -> int:
        """QiTV's target class, made once a process (a second QiTV window would find it)."""
        made = self._objc.objc_getClass(_TARGET_CLASS)
        if made:
            return int(made)
        made = self._objc.objc_allocateClassPair(self._class(b"NSObject"), _TARGET_CLASS, 0)
        if not made:
            raise RuntimeError("couldn't make the media keys' target class")
        for _command, selector, name in _COMMANDS:
            imp = _method(name)
            _imps.append(imp)
            added = self._objc.class_addMethod(
                made,
                self._objc.sel_registerName(selector),
                ctypes.cast(imp, ctypes.c_void_p),
                b"q@:@",
            )
            if not added:
                raise RuntimeError(f"couldn't add {selector.decode()}")
        self._objc.objc_registerClassPair(made)
        return int(made)

    def listen(self) -> None:
        pool = self._objc.objc_autoreleasePoolPush()
        try:
            target = self._send(self._send(self._target_class(), b"alloc"), b"init")
            if not target:
                raise RuntimeError("couldn't make the media keys' target")
            self._target = target
            for command, selector, _name in _COMMANDS:
                each = self._send(self._center, command)
                self._send(each, b"setEnabled:", None, (ctypes.c_bool, True))
                self._send(
                    each,
                    b"addTarget:action:",
                    None,
                    (ctypes.c_void_p, target),
                    (ctypes.c_void_p, self._objc.sel_registerName(selector)),
                )
        finally:
            self._objc.objc_autoreleasePoolPop(pool)

    def now_playing(self, state: str, title: str, live: bool) -> None:
        pool = self._objc.objc_autoreleasePoolPush()
        try:
            info = None
            if state:
                keys = (ctypes.c_void_p * 3)(self._title_key, self._live_key, self._type_key)
                values = (ctypes.c_void_p * 3)(
                    self._string(title or "QiTV"),
                    self._send(
                        self._class(b"NSNumber"),
                        b"numberWithBool:",
                        ctypes.c_void_p,
                        (ctypes.c_bool, live),
                    ),
                    self._send(
                        self._class(b"NSNumber"),
                        b"numberWithUnsignedInteger:",
                        ctypes.c_void_p,
                        (ctypes.c_ulong, _MEDIA_TYPE_VIDEO),
                    ),
                )
                if not all(values):  # a nil in a dictionary is an Objective-C exception
                    raise RuntimeError("couldn't make what Now Playing shows")
                info = self._send(
                    self._class(b"NSDictionary"),
                    b"dictionaryWithObjects:forKeys:count:",
                    ctypes.c_void_p,
                    (ctypes.c_void_p, ctypes.cast(values, ctypes.c_void_p)),
                    (ctypes.c_void_p, ctypes.cast(keys, ctypes.c_void_p)),
                    (ctypes.c_ulong, 3),
                )
            self._send(self._info, b"setNowPlayingInfo:", None, (ctypes.c_void_p, info))
            self._send(self._info, b"setPlaybackState:", None, (ctypes.c_ulong, _STATES[state]))
        finally:
            self._objc.objc_autoreleasePoolPop(pool)

    def playback_state(self) -> int:
        """MPNowPlayingInfoCenter's state as set (a check's)."""
        return int(self._send(self._info, b"playbackState", ctypes.c_ulong))

    def shown_title(self) -> str:
        """The title Now Playing shows ("" for none; a check's)."""
        info = self._send(self._info, b"nowPlayingInfo")
        if not info:
            return ""
        title = self._send(
            info, b"objectForKey:", ctypes.c_void_p, (ctypes.c_void_p, self._title_key)
        )
        if not title:
            return ""
        text = self._send(title, b"UTF8String", ctypes.c_char_p)
        return text.decode("utf-8") if text else ""

    def report(self) -> dict:
        state = {1: "playing", 2: "paused"}.get(self.playback_state(), "")
        return {"state": state, "title": self.shown_title()}

    def simulate(self, name: str) -> bool:
        selector = next(s for _c, s, n in _COMMANDS if n == name)
        return self.press(selector) == _SUCCESS

    def press(self, selector: bytes) -> int:
        """Calls the target's method as the command center would (a check's)."""
        if not self._target:
            raise RuntimeError("not listening")
        return int(self._send(self._target, selector, ctypes.c_long, (ctypes.c_void_p, None)))

    def _string(self, text: str) -> Optional[int]:
        made = self._send(
            self._class(b"NSString"),
            b"stringWithUTF8String:",
            ctypes.c_void_p,
            (ctypes.c_char_p, text.encode("utf-8", "replace")),
        )
        return int(made) if made else None

    def close(self) -> None:
        pool = self._objc.objc_autoreleasePoolPush()
        try:
            if self._target:
                for command, _selector, _name in _COMMANDS:
                    each = self._send(self._center, command)
                    self._send(each, b"removeTarget:", None, (ctypes.c_void_p, self._target))
                self._send(self._target, b"release", None)
                self._target = None
            self._send(self._info, b"setNowPlayingInfo:", None, (ctypes.c_void_p, None))
            self._send(self._info, b"setPlaybackState:", None, (ctypes.c_ulong, _STATES[""]))
        finally:
            self._objc.objc_autoreleasePoolPop(pool)
