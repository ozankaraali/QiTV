"""The TV window's Cocoa details: the overlay attached as a child window, so macOS moves it with
the player, the keys given back to the window after a click on the video, and for the desktop
picture in picture (ui/desktop_pip.py) whether a window is on the desktop shown and a window
that shows on every desktop."""

import ctypes
import ctypes.util
import functools
import logging
from typing import Any, Optional

from PySide6.QtGui import QGuiApplication

logger = logging.getLogger(__name__)
_NS_WINDOW_ABOVE = 1
# NSWindowCollectionBehavior: on every desktop (Space), and allowed beside a fullscreen window.
_CAN_JOIN_ALL_SPACES = 1 << 0
_FULL_SCREEN_AUXILIARY = 1 << 8


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


def hide_pointer_until_moved() -> bool:
    """macOS: hide the pointer until the mouse moves ([NSCursor setHiddenUntilMouseMoves:YES]).

    AppKit shows it again on the next move, over any view, libVLC's included. Nothing happens
    elsewhere (False).
    """
    if QGuiApplication.platformName() != "cocoa":
        return False
    try:
        objc = _objc()
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        cursor = objc.objc_getClass(b"NSCursor")
        if not cursor:
            return False
        _send(cursor, b"setHiddenUntilMouseMoves:", None, (ctypes.c_bool, True))
        return True
    except Exception as e:  # never let a Cocoa detail break the player
        logger.debug("Could not hide the pointer: %s", e)
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


def on_active_space(widget) -> Optional[bool]:
    """macOS: whether `widget`'s window is on a desktop (Space) shown now ([NSWindow
    isOnActiveSpace]); None elsewhere, or when it cannot tell."""
    if QGuiApplication.platformName() != "cocoa":
        return None
    try:
        ns_window = _send(int(widget.winId()), b"window")
        if not ns_window:
            return None
        return bool(_send(ns_window, b"isOnActiveSpace", ctypes.c_bool))
    except Exception as e:  # never let a Cocoa detail break the player
        logger.debug("Could not tell the window's desktop: %s", e)
        return None


def join_all_spaces(widget) -> bool:
    """macOS: `widget`'s window shows on every desktop, beside a fullscreen one too, and stays
    when QiTV is not the active app. Nothing happens elsewhere (False)."""
    if QGuiApplication.platformName() != "cocoa":
        return False
    try:
        ns_window = _send(int(widget.winId()), b"window")
        if not ns_window:
            return False
        behavior = _CAN_JOIN_ALL_SPACES | _FULL_SCREEN_AUXILIARY
        _send(ns_window, b"setCollectionBehavior:", None, (ctypes.c_ulong, behavior))
        _send(ns_window, b"setHidesOnDeactivate:", None, (ctypes.c_bool, False))
        return True
    except Exception as e:  # never let a Cocoa detail break the player
        logger.debug("Could not show the window on every desktop: %s", e)
        return False
