"""The TV window's Cocoa details: the overlay attached as a child window, so macOS moves it with
the player (spec §5.7), and the keys given back to the window after a click on the video."""

import ctypes
import ctypes.util
import functools
import logging
from typing import Any

from PySide6.QtGui import QGuiApplication

logger = logging.getLogger(__name__)
_NS_WINDOW_ABOVE = 1


@functools.cache
def _objc() -> Any:
    objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc") or "libobjc.dylib")
    objc.sel_registerName.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]
    return objc


def _send(receiver: int, selector: bytes, restype: Any = ctypes.c_void_p, *args: Any) -> Any:
    """[receiver selector …]; each of `args` is a (ctypes type, value) pair."""
    objc = _objc()
    function = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *(t for t, _ in args))(
        ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value or 0
    )
    return function(receiver, objc.sel_registerName(selector), *(v for _, v in args))


def animates_restore() -> bool:
    """macOS: a restored window flies out of the Dock, and the platform reports it landed.

    AppKit sends windowDidDeminiaturize (Qt: a spontaneous WindowStateChange on the QWindow)
    after the genie animation, whoever restores the window. Qt reports a restore it asked
    for itself (showNormal()) at once, and isMiniaturized is already NO during the animation
    then (checked on macOS 13): only the platform's report marks the end.
    """
    return QGuiApplication.platformName() == "cocoa"


def reclaim_first_responder(window) -> bool:
    """macOS: give the keys back to `window`'s own view after a click on a video surface.

    A mouse down on the video makes libVLC's VLCVideoLayerView, which libVLC puts inside the
    surface's NSView, the window's first responder. Keys then reach VLC's view first: they
    still travel on to Qt, but macOS beeps on the way. `window` is the top-level widget; its
    winId() is Qt's QNSView, made first responder again ([[view window] makeFirstResponder:
    view]). Nothing happens elsewhere (False).
    """
    if QGuiApplication.platformName() != "cocoa":
        return False
    try:
        view = int(window.winId())
        ns_window = _send(view, b"window")
        if not ns_window:
            return False
        return bool(
            _send(ns_window, b"makeFirstResponder:", ctypes.c_bool, (ctypes.c_void_p, view))
        )
    except Exception as e:  # never let a Cocoa detail break the player
        logger.debug("Could not give the keys back to the window: %s", e)
        return False


def attach_child_window(parent, child) -> bool:
    # Only the cocoa platform plugin hands out NSView pointers as winId()
    if QGuiApplication.platformName() != "cocoa":
        return False
    try:
        parent_window = _send(int(parent.winId()), b"window")
        child_window = _send(int(child.winId()), b"window")
        if not parent_window or not child_window:
            return False
        _send(
            parent_window,
            b"addChildWindow:ordered:",
            None,
            (ctypes.c_void_p, child_window),
            (ctypes.c_long, _NS_WINDOW_ABOVE),
        )
        # Qt makes a Qt::Tool panel hide whenever QiTV is not the active app, at the
        # floating level. Overlays must show over the inactive window too (a wheel zap
        # shows the banner), and a child at the window's own level never floats over other
        # apps. AppKit's addChildWindow did both on macOS 13, but says nothing of it.
        _send(child_window, b"setHidesOnDeactivate:", None, (ctypes.c_bool, False))
        level = _send(parent_window, b"level", ctypes.c_long)
        _send(child_window, b"setLevel:", None, (ctypes.c_long, level))
        return True
    except Exception as e:  # never let a Cocoa detail break the player
        logger.debug("Could not attach the overlay as a child window: %s", e)
        return False
