"""macOS's media keys: QiTV listed as Now Playing (MediaPlayer's MPRemoteCommandCenter and
MPNowPlayingInfoCenter, through the Objective-C runtime: no native build). See
services.media_keys.

A song's cover is an MPMediaItemArtwork made with initWithImage: (deprecated, but answered on
macOS); its other initializer takes a block, which ctypes can't make. Its NSImage is made of
the cover file's bytes (NSData, read on TVWindow's thread with the file): no file is read on
the GUI thread. A macOS that no longer answers it shows the song without its cover (each
reason said once in the log).
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
from typing import Any, Optional

from services.media_keys import on_command

logger = logging.getLogger(__name__)

_MEDIA_PLAYER = "/System/Library/Frameworks/MediaPlayer.framework/MediaPlayer"
_TARGET_CLASS = b"QiTVMediaCommandTarget"
# MPRemoteCommandHandlerStatus
_SUCCESS, _NO_ITEM = 0, 110
# MPNowPlayingPlaybackState
_STATES = {"playing": 1, "paused": 2, "": 3}
_MEDIA_TYPE_AUDIO, _MEDIA_TYPE_VIDEO = 1, 2  # MPNowPlayingInfoMediaType
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
        self._artist_key = ctypes.c_void_p.in_dll(player, "MPMediaItemPropertyArtist").value
        self._album_key = ctypes.c_void_p.in_dll(player, "MPMediaItemPropertyAlbumTitle").value
        self._artwork_key = ctypes.c_void_p.in_dll(player, "MPMediaItemPropertyArtwork").value
        commands = self._class(b"MPRemoteCommandCenter")
        info = self._class(b"MPNowPlayingInfoCenter")
        self._center = self._send(commands, b"sharedCommandCenter")
        self._info = self._send(info, b"defaultCenter")
        if not self._center or not self._info:
            raise RuntimeError("no MediaPlayer command center")
        self._target: Optional[int] = None
        self._artwork: tuple[str, Optional[int]] = ("", None)  # the cover file's, kept (+1)
        self._artwork_answered: Optional[bool] = None  # initWithImage: is answered
        self._said: set[str] = set()  # why a cover didn't show, each logged once

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

    def now_playing(
        self,
        state: str,
        title: str,
        live: bool,
        *,
        artist: str = "",
        album: str = "",
        art_path: str = "",
        art_bytes: bytes = b"",
        music: bool = False,
    ) -> None:
        pool = self._objc.objc_autoreleasePoolPush()
        try:
            info = None
            artwork = self._artwork_of(art_path, art_bytes) if state else self._artwork_of("", b"")
            if state:
                number = self._class(b"NSNumber")
                pairs = [
                    (self._title_key, self._string(title or "QiTV")),
                    (
                        self._live_key,
                        self._send(
                            number, b"numberWithBool:", ctypes.c_void_p, (ctypes.c_bool, live)
                        ),
                    ),
                    (
                        self._type_key,
                        self._send(
                            number,
                            b"numberWithUnsignedInteger:",
                            ctypes.c_void_p,
                            (ctypes.c_ulong, _MEDIA_TYPE_AUDIO if music else _MEDIA_TYPE_VIDEO),
                        ),
                    ),
                ]
                if artist:
                    pairs.append((self._artist_key, self._string(artist)))
                if album:
                    pairs.append((self._album_key, self._string(album)))
                if artwork:
                    pairs.append((self._artwork_key, artwork))
                if not all(value for _key, value in pairs):  # a nil: an Objective-C exception
                    raise RuntimeError("couldn't make what Now Playing shows")
                count = len(pairs)
                keys = (ctypes.c_void_p * count)(*(key for key, _value in pairs))
                values = (ctypes.c_void_p * count)(*(value for _key, value in pairs))
                info = self._send(
                    self._class(b"NSDictionary"),
                    b"dictionaryWithObjects:forKeys:count:",
                    ctypes.c_void_p,
                    (ctypes.c_void_p, ctypes.cast(values, ctypes.c_void_p)),
                    (ctypes.c_void_p, ctypes.cast(keys, ctypes.c_void_p)),
                    (ctypes.c_ulong, count),
                )
            self._send(self._info, b"setNowPlayingInfo:", None, (ctypes.c_void_p, info))
            self._send(self._info, b"setPlaybackState:", None, (ctypes.c_ulong, _STATES[state]))
        finally:
            self._objc.objc_autoreleasePoolPop(pool)

    def _artwork_of(self, path: str, data: bytes) -> Optional[int]:
        """The cover file at `path` (its bytes `data`) as an MPMediaItemArtwork, made once per
        file and kept until another (None: none, or none can be made; Now Playing shows the
        song all the same). The file is TVWindow's own small one (ui.song_jobs.PanelCover)."""
        kept_path, kept = self._artwork
        if path == kept_path:
            return kept
        self._artwork = (path, None)
        if kept:
            self._send(kept, b"release", None)
        if not path or not self._answers_artwork():
            return None
        try:
            image = self._image_of(data)
            if not image:
                self._say("image", "Now Playing shows no cover: its picture couldn't be read")
                return None
            artwork = self._send(
                self._send(self._class(b"MPMediaItemArtwork"), b"alloc"),
                b"initWithImage:",
                ctypes.c_void_p,
                (ctypes.c_void_p, image),
            )
            self._send(image, b"release", None)  # the artwork keeps it
        except Exception as e:  # never in the way of the song's title
            self._say("failed", f"Now Playing shows no cover: {e}")
            return None
        self._artwork = (path, int(artwork) if artwork else None)
        return self._artwork[1]

    def _image_of(self, data: bytes) -> Optional[int]:
        """An NSImage of a picture file's bytes (+1; None: none): NSData copies them."""
        if not data:
            return None
        made = self._send(
            self._class(b"NSData"),
            b"dataWithBytes:length:",
            ctypes.c_void_p,
            (ctypes.c_char_p, data),
            (ctypes.c_ulong, len(data)),
        )
        if not made:
            return None
        image = self._send(
            self._send(self._class(b"NSImage"), b"alloc"),
            b"initWithData:",
            ctypes.c_void_p,
            (ctypes.c_void_p, made),
        )
        return int(image) if image else None

    def _say(self, kind: str, message: str) -> None:
        """A reason a cover didn't show, logged once a session."""
        if kind not in self._said:
            self._said.add(kind)
            logger.debug("%s", message)

    def _answers_artwork(self) -> bool:
        if self._artwork_answered is None:
            try:
                self._artwork_answered = bool(
                    self._send(
                        self._class(b"MPMediaItemArtwork"),
                        b"instancesRespondToSelector:",
                        ctypes.c_bool,
                        (ctypes.c_void_p, self._objc.sel_registerName(b"initWithImage:")),
                    )
                )
            except Exception:  # no such class: an older or a stripped macOS
                self._artwork_answered = False
            if not self._artwork_answered:
                self._say(
                    "initializer",
                    "Now Playing shows no covers: no MPMediaItemArtwork initWithImage:",
                )
        return self._artwork_answered

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
            self._artwork_of("", b"")  # the cover's artwork goes with it
        finally:
            self._objc.objc_autoreleasePoolPop(pool)
